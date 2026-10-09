from __future__ import annotations

from app.business.models import CodeContext, CodeSearch
from app.business.source_index import SourceIndex
from app.common import digest, dumps, now, uid
from app.connectors.models import ToolRequest
from app.connectors.service import RuntimeService
from app.context.budget import ContextBudgetManager


class BusinessToolRegistry:
    def __init__(self, database, settings, business_id, run_id, snapshot_id, budget_id):
        self.db, self.settings = database, settings
        self.business_id, self.run_id, self.sid, self.bid = (
            business_id,
            run_id,
            snapshot_id,
            budget_id,
        )
        self.manager = ContextBudgetManager(database, settings)

    def execute(self, action):
        call_id = uid()
        tool = action["tool_id"]
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO tool_calls(id,run_id,snapshot_id,purpose,tool_id,adapter_version,args_redacted_json,risk,policy_version,status,request_id,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    self.run_id,
                    self.sid,
                    "diagnosis",
                    tool,
                    "business-v2.2",
                    dumps(action.get("arguments", {})),
                    "LOW",
                    "scope-v2.2",
                    "pending",
                    uid(),
                    now(),
                ),
            )
        try:
            self.manager.consume_tool(self.bid)
            if tool == "code.search":
                payload = {
                    "results": SourceIndex(self.db, self.settings).search(
                        self.business_id, CodeSearch.model_validate(action["arguments"]), self.bid
                    ),
                    "source_type": "Static",
                }
            elif tool == "code.context":
                payload = SourceIndex(self.db, self.settings).context(
                    self.business_id, CodeContext.model_validate(action["arguments"]), self.bid
                )
                payload["source_type"] = "Static"
            else:
                observation = RuntimeService(self.db, self.settings).execute(
                    self.business_id,
                    action["target_id"],
                    ToolRequest(tool_id=tool, arguments=action["arguments"]),
                )
                if "payload" not in observation:
                    status = observation.get("status", "failed")
                    if status not in {"denied", "timed_out", "failed"}:
                        status = "failed"
                    error = observation.get("error", "runtime_read_failed")
                    with self.db.transaction() as tx:
                        tx.execute(
                            "UPDATE tool_calls SET status=?,ended_at=?,error_code=? WHERE id=?",
                            (status, now(), error, call_id),
                        )
                    return {
                        "call_id": call_id, "tool_id": tool, "status": status,
                        "error": error, "observation_id": observation["observation_id"],
                    }
                payload = {
                    **observation["payload"],
                    "observation_id": observation["observation_id"],
                    "source_type": "Runtime",
                }
                self.manager.retrieval(self.bid, observation["bytes"])
            evidence_id, stamp = uid(), now()
            content_hash = digest(payload)
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE tool_calls SET status='succeeded',ended_at=? WHERE id=?",
                    (stamp, call_id),
                )
                tx.execute(
                    "INSERT INTO evidence(id,tool_call_id,target_node_id,observed_at,payload_redacted_json,content_hash,truncated,freshness_state) VALUES(?,?,?,?,?,?,0,'current')",
                    (
                        evidence_id,
                        call_id,
                        action["target_node_id"],
                        stamp,
                        dumps(payload),
                        content_hash,
                    ),
                )
            return {
                "call_id": call_id,
                "evidence_id": evidence_id,
                "tool_id": tool,
                "target_node_id": action["target_node_id"],
                "observed_at": stamp,
                "payload": payload,
                "content_hash": content_hash,
            }
        except Exception as exc:
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE tool_calls SET status=?,ended_at=?,error_code=? WHERE id=?",
                    (
                        "timed_out"
                        if isinstance(exc, TimeoutError)
                        else "denied"
                        if isinstance(exc, PermissionError)
                        else "failed",
                        now(),
                        type(exc).__name__,
                        call_id,
                    ),
                )
            return {"call_id": call_id, "tool_id": tool, "error": type(exc).__name__,
                    "status": "timed_out" if isinstance(exc, TimeoutError) else "denied"
                    if isinstance(exc, PermissionError) else "failed"}
