from __future__ import annotations

import json

from pydantic import Field

from app.common import now, uid
from app.domain import StrictModel


class BudgetExceeded(ValueError):
    pass


class BudgetLimits(StrictModel):
    max_input_tokens: int = Field(default=8000, ge=1, le=200000)
    max_calls: int = Field(default=6, ge=1, le=100)
    max_total_tokens: int = Field(default=100000, ge=1, le=1000000)
    max_tools: int = Field(default=20, ge=1, le=100)
    max_retrieved_bytes: int = Field(default=500000, ge=1, le=10000000)


class ContextBudgetManager:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    def create(self, phase, business_id=None, run_id=None, limits=None):
        initial = phase == "initial"
        limits = limits or BudgetLimits(
            max_input_tokens=self.settings.initial_input_tokens
            if initial
            else self.settings.investigation_input_tokens,
            max_calls=self.settings.initial_model_calls
            if initial
            else self.settings.investigation_model_rounds,
            max_tools=self.settings.investigation_tool_calls,
            max_total_tokens=self.settings.budget_total_tokens,
        )
        budget_id = uid()
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO context_budgets(id,business_id,run_id,phase,limits_json,created_at) VALUES(?,?,?,?,?,?)",
                (budget_id, business_id, run_id, phase, limits.model_dump_json(), now()),
            )
        return budget_id

    def get(self, budget_id):
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM context_budgets WHERE id=?", (budget_id,)).fetchone()
            if not row:
                raise ValueError("预算不存在")
            result = dict(row)
            result["limits"] = json.loads(result.pop("limits_json"))
            result["usage_records"] = [
                dict(x)
                for x in conn.execute(
                    "SELECT * FROM model_usage_records WHERE budget_id=? ORDER BY created_at",
                    (budget_id,),
                )
            ]
            return result
        finally:
            conn.close()

    def update(self, budget_id, limits):
        with self.db.transaction() as tx:
            if not tx.execute("SELECT 1 FROM context_budgets WHERE id=?", (budget_id,)).fetchone():
                raise ValueError("预算不存在")
            tx.execute(
                "UPDATE context_budgets SET limits_json=?,stop_reason=NULL WHERE id=?",
                (limits.model_dump_json(), budget_id),
            )

    def reserve(self, budget_id, messages, output_limit):
        # UTF-8 bytes + per-message framing is an upper estimate, not a billing count.
        estimate = sum(len(m["content"].encode("utf-8")) + 128 for m in messages) + 256
        reason = None
        record_id = uid()
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM context_budgets WHERE id=?", (budget_id,)).fetchone()
            limits = json.loads(row["limits_json"])
            reserved = estimate + output_limit
            if row["stop_reason"] == "provider_usage_exceeded_reservation":
                reason = "provider_usage_exceeded_reservation"
            elif estimate > limits["max_input_tokens"]:
                reason = "request_input_limit"
            elif reserved > self.settings.model_context_window:
                reason = "model_context_window"
            elif row["calls"] >= limits["max_calls"]:
                reason = "model_call_limit"
            elif (
                row["input_tokens"] + row["output_tokens"] + row["reserved_tokens"] + reserved
                > limits["max_total_tokens"]
            ):
                reason = "total_token_limit"
            if reason:
                tx.execute(
                    "UPDATE context_budgets SET stop_reason=? WHERE id=?", (reason, budget_id)
                )
            else:
                tx.execute(
                    "UPDATE context_budgets SET calls=calls+1,reserved_tokens=reserved_tokens+?,stop_reason=NULL WHERE id=?",
                    (reserved, budget_id),
                )
                tx.execute(
                    "INSERT INTO model_usage_records VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        budget_id,
                        self.settings.model_name,
                        estimate,
                        output_limit,
                        "estimate_reserved",
                        None,
                        "unknown",
                        "pending",
                        now(),
                    ),
                )
        if reason:
            raise BudgetExceeded(reason)
        return record_id, estimate, reserved

    def settle(self, budget_id, reservation, response=None, status="succeeded"):
        record_id, estimate, reserved = reservation
        usage = getattr(response, "usage", None)
        actual = bool(
            usage
            and getattr(usage, "prompt_tokens", None) is not None
            and getattr(usage, "completion_tokens", None) is not None
        )
        inputs = max(0, int(usage.prompt_tokens)) if actual else estimate
        outputs = (
            max(0, int(usage.completion_tokens))
            if actual
            else (
                len(str(getattr(response, "choices", "")).encode())
                if response is not None
                else reserved - estimate
            )
        )
        # Failed requests may have been billed. Keep the full conservative reservation.
        if not actual:
            outputs = min(reserved - estimate, outputs)
        price_in = self.settings.model_input_cost_per_million
        price_out = self.settings.model_output_cost_per_million
        cost = (
            (inputs * price_in + outputs * price_out) / 1000000
            if price_in is not None and price_out is not None
            else None
        )
        with self.db.transaction() as tx:
            row = tx.execute(
                "SELECT status FROM model_usage_records WHERE id=?", (record_id,)
            ).fetchone()
            if not row or row["status"] != "pending":
                raise ValueError("重复或未知预算结算")
            tx.execute(
                "UPDATE context_budgets SET reserved_tokens=reserved_tokens-?,input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?",
                (reserved, inputs, outputs, budget_id),
            )
            tx.execute(
                "UPDATE model_usage_records SET input_tokens=?,output_tokens=?,usage_kind=?,cost=?,cost_kind=?,status=? WHERE id=?",
                (
                    inputs,
                    outputs,
                    "actual" if actual else "estimate",
                    cost,
                    "estimate_configured_rates" if cost is not None else "unknown",
                    status,
                    record_id,
                ),
            )
            if inputs + outputs > reserved:
                tx.execute(
                    "UPDATE context_budgets SET stop_reason='provider_usage_exceeded_reservation' WHERE id=?",
                    (budget_id,),
                )

    def consume_tool(self, budget_id):
        self._consume(budget_id, "tool_calls", 1, "max_tools", "tool_call_limit")

    def retrieval(self, budget_id, size):
        self._consume(budget_id, "retrieved_bytes", size, "max_retrieved_bytes", "retrieval_limit")

    def _consume(self, budget_id, column, amount, limit_key, reason):
        exceeded = False
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM context_budgets WHERE id=?", (budget_id,)).fetchone()
            if row[column] + amount > json.loads(row["limits_json"])[limit_key]:
                tx.execute(
                    "UPDATE context_budgets SET stop_reason=? WHERE id=?", (reason, budget_id)
                )
                exceeded = True
            else:
                tx.execute(
                    f"UPDATE context_budgets SET {column}={column}+? WHERE id=?",
                    (amount, budget_id),
                )
        if exceeded:
            raise BudgetExceeded(reason)
