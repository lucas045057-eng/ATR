from __future__ import annotations

import json

from pydantic import Field

from app.common import now, uid
from app.context.model_errors import usage_counts
from app.domain import StrictModel


class BudgetExceeded(ValueError):
    def __init__(self, reason, **details):
        self.diagnostic = {"budget_reason": reason, **details}
        super().__init__(reason)


def estimate_model_input(messages):
    # UTF-8 bytes + per-message framing is an upper estimate, not a billing count.
    return sum(len(m["content"].encode("utf-8")) + 128 for m in messages) + 256


def interrupt_usage(tx, usage):
    """Close a pending reservation without inventing an end time or billed usage."""
    reserved = usage['input_tokens'] + usage['output_tokens']
    tx.execute("UPDATE context_budgets SET reserved_tokens=MAX(0,reserved_tokens-?),input_tokens=input_tokens+?,output_tokens=output_tokens+?,stop_reason='interrupted_request_usage_estimated' WHERE id=?",
               (reserved, usage['input_tokens'], usage['output_tokens'], usage['budget_id']))
    audit = json.loads(usage['diagnostics_json'])
    if audit:
        audit.update(usage_status='interrupted', usage_kind='estimate', request_stage='interrupted',
                     error_category='model_request_interrupted', ended_at=None, elapsed_ms=None, recovered_at=now())
    tx.execute("UPDATE model_usage_records SET status='interrupted',usage_kind='estimate',cost=NULL,cost_kind='unknown',diagnostics_json=? WHERE id=?",
               (json.dumps(audit), usage['id']))


class BudgetLimits(StrictModel):
    max_input_tokens: int = Field(default=8000, ge=1, le=200000)
    max_calls: int = Field(default=6, ge=1, le=100)
    max_total_tokens: int = Field(default=100000, ge=1, le=1000000)
    max_tools: int = Field(default=20, ge=1, le=500)
    max_retrieved_bytes: int = Field(default=500000, ge=1, le=10000000)
    max_execution_seconds: int = Field(default=1800, ge=1, le=86400)


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
            for record in result["usage_records"]:
                record["diagnostic"] = json.loads(record.pop("diagnostics_json", "{}"))
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

    def reserve(self, budget_id, messages, output_limit, *, authorize=None):
        estimate = estimate_model_input(messages)
        reason = None
        record_id = uid()
        with self.db.transaction() as tx:
            if authorize is not None:
                authorize(tx)
            row = tx.execute("SELECT * FROM context_budgets WHERE id=?", (budget_id,)).fetchone()
            limits = json.loads(row["limits_json"])
            reserved = estimate + output_limit
            if row["stop_reason"] in {"provider_usage_exceeded_reservation", "execution_time_limit", "tool_call_limit", "retrieval_limit"}:
                reason = row["stop_reason"]
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
                    "INSERT INTO model_usage_records(id,budget_id,model,input_tokens,output_tokens,usage_kind,cost,cost_kind,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
            raise BudgetExceeded(reason, estimated_input_tokens=estimate,
                                 input_limit=limits["max_input_tokens"])
        return record_id, estimate, reserved

    def start_attempt(self, budget_id, reservation):
        audit = {"request_attempt_id": reservation[0], "model": self.settings.model_name,
                 "provider": self.settings.model_provider, "started_at": now(), "retry_count": 0,
                 "request_stage": "initialize", "usage_status": "pending", "usage_kind": "estimate_reserved",
                 "estimated_input_tokens": reservation[1]}
        with self.db.transaction() as tx:
            row = tx.execute("SELECT run_id FROM context_budgets WHERE id=?", (budget_id,)).fetchone()
            audit["investigation_run_id"] = row["run_id"]
            tx.execute("UPDATE model_usage_records SET diagnostics_json=? WHERE id=?", (json.dumps(audit), reservation[0]))
        return audit

    def settle(self, budget_id, reservation, response=None, status="succeeded", audit=None):
        record_id, estimate, reserved = reservation
        usage = getattr(response, "usage", None)
        counts = usage_counts(usage)
        actual = counts is not None
        inputs = counts[0] if actual else estimate
        outputs = (
            counts[1]
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
            if price_in is not None and price_out is not None and (actual or status != "failed")
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
            if audit is not None:
                audit.update(usage_kind="actual" if actual else "estimate", usage_status=status)
                tx.execute("UPDATE model_usage_records SET diagnostics_json=? WHERE id=?", (json.dumps(audit), record_id))
                run_id = audit.get("investigation_run_id")
                if run_id:
                    run = tx.execute("SELECT i.system_id FROM diagnosis_runs r JOIN incidents i ON i.id=r.incident_id WHERE r.id=?", (run_id,)).fetchone()
                    sequence = tx.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
                    tx.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (uid(), run['system_id'], run_id, sequence,
                               "model_attempt", record_id, "model_attempt_failed" if status == "failed" else "model_attempt_completed",
                               "runtime", json.dumps(audit), now()))
            if inputs + outputs > reserved:
                tx.execute(
                    "UPDATE context_budgets SET stop_reason='provider_usage_exceeded_reservation' WHERE id=?",
                    (budget_id,),
                )

    def consume_tool(self, budget_id, *, authorize=None):
        self.remaining_seconds(budget_id)
        self._consume(budget_id, "tool_calls", 1, "max_tools", "tool_call_limit", authorize=authorize)

    def remaining_seconds(self, budget_id):
        row = self.get(budget_id)
        if row["stop_reason"] in {"execution_time_limit", "retrieval_limit", "tool_call_limit", "provider_usage_exceeded_reservation"}:
            raise BudgetExceeded(row["stop_reason"])
        remaining = row["limits"].get("max_execution_seconds", 1800) - row.get("elapsed_ms", 0) / 1000
        if remaining <= 0:
            raise BudgetExceeded("execution_time_limit")
        return remaining

    def record_time(self, budget_id, seconds):
        with self.db.transaction() as tx:
            tx.execute("UPDATE context_budgets SET elapsed_ms=elapsed_ms+? WHERE id=?", (max(0, seconds) * 1000, budget_id))
            row = tx.execute("SELECT * FROM context_budgets WHERE id=?", (budget_id,)).fetchone()
            if row["elapsed_ms"] >= json.loads(row["limits_json"]).get("max_execution_seconds", 1800) * 1000:
                tx.execute("UPDATE context_budgets SET stop_reason='execution_time_limit' WHERE id=?", (budget_id,))

    def retrieval(self, budget_id, size):
        self._consume(budget_id, "retrieved_bytes", size, "max_retrieved_bytes", "retrieval_limit")

    def _consume(self, budget_id, column, amount, limit_key, reason, *, authorize=None):
        exceeded = False
        with self.db.transaction() as tx:
            if authorize is not None:
                authorize(tx)
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
