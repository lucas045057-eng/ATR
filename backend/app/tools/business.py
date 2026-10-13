from __future__ import annotations

import json
import time

from app.agent.discovery import authorization_digest
from app.business.models import CodeContext, CodeSearch
from app.business.source_index import SourceIndex
from app.common import digest, dumps, now, uid
from app.connectors.models import ToolRequest
from app.connectors.service import RuntimeService
from app.context.budget import ContextBudgetManager
from app.system.scope import binding_is_current


class BusinessToolRegistry:
    def __init__(self, database, settings, business_id, run_id, snapshot_id, budget_id, target_id, *, supervision=None):
        self.db, self.settings = database, settings
        self.business_id, self.run_id, self.sid, self.bid = (
            business_id,
            run_id,
            snapshot_id,
            budget_id,
        )
        self.target_id = target_id
        self.supervision = supervision
        with self.db.connect() as connection:
            run = connection.execute("SELECT target_id FROM diagnosis_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["target_id"] != target_id:
            raise PermissionError("工具注册表与 Run 的 SSH 目标绑定不一致")
        self.manager = ContextBudgetManager(database, settings)

    def _authorize(self, tx):
        if self.supervision is not None:
            self.supervision['authorize'](tx)

    def _validate_runtime_action(self, action):
        tool = action["tool_id"]
        if action.get("target_id") != self.target_id:
            raise PermissionError("工具目标与 Run 的 SSH 目标不一致")
        with self.db.connect() as connection:
            run = connection.execute("SELECT target_snapshot_json FROM diagnosis_runs WHERE id=? AND target_id=?",
                                     (self.run_id, self.target_id)).fetchone()
            binding = connection.execute("SELECT * FROM service_bindings WHERE id=? AND business_id=? AND target_id=?",
                                         (action.get("binding_id"), self.business_id, self.target_id)).fetchone() if action.get("binding_id") else None
        if not run:
            raise PermissionError("Run 的 SSH 目标绑定已失效")
        runtime = RuntimeService(self.db, self.settings)
        try:
            current = runtime.get(self.business_id, self.target_id)
        except KeyError as exc:
            raise PermissionError("Run 的 SSH 目标已删除或不属于当前业务") from exc
        if not runtime.investigation_readiness(current)["eligible"]:
            raise PermissionError("Run 的 SSH 目标已停用或撤销授权")
        snapshot = json.loads(run["target_snapshot_json"] or "{}")
        if authorization_digest(current) != snapshot.get("authorization_digest"):
            raise PermissionError("Run 的 SSH 目标授权或执行配置已变化")
        if action.get("binding_id"):
            if not binding or binding["status"] != "confirmed" or not binding_is_current(binding):
                raise PermissionError("工具映射不属于 Run 的已确认有效 SSH 目标")
            if binding["kind"] not in {"service", "container"}:
                raise PermissionError("工具映射类型不允许用于本次调查")
            if not tool.startswith(binding["kind"] + ".") and not tool.startswith("config."):
                raise PermissionError("工具类型与已确认映射不一致")
            if binding["kind"] == "service" and tool.startswith("service.") and action.get("arguments", {}).get("name") != binding["runtime_id"]:
                raise PermissionError("服务参数与已确认映射不一致")
            if binding["kind"] == "container" and tool.startswith("container.") and action.get("arguments", {}).get("container_id") != binding["runtime_id"]:
                raise PermissionError("容器参数与已确认映射不一致")

    def execute(self, action):
        call_id = uid()
        tool = action["tool_id"]
        with self.db.transaction() as tx:
            self._authorize(tx)
            tx.execute(
                "INSERT INTO tool_calls(id,run_id,snapshot_id,purpose,tool_id,adapter_version,args_redacted_json,risk,policy_version,status,request_id,started_at,target_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    self.target_id,
                ),
            )
        started = time.monotonic()
        try:
            self.manager.consume_tool(self.bid, **({'authorize': self.supervision['authorize']} if self.supervision else {}))
            if not tool.startswith("code."):
                self._validate_runtime_action(action)
            if tool.startswith("config."):
                with self.db.connect() as c:
                    binding = c.execute("SELECT * FROM service_bindings WHERE id=? AND business_id=? AND target_id=?",
                        (action.get("binding_id"), self.business_id, action.get("target_id"))).fetchone()
                if not binding or not binding_is_current(binding) or binding["kind"] not in {"service", "container"}:
                    raise PermissionError("Agent 配置读取需要当前已确认的业务映射")
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
                    timeout_cap=min(10, self.manager.remaining_seconds(self.bid)) if self.supervision else self.manager.remaining_seconds(self.bid),
                    **({'supervision': self.supervision} if self.supervision else {}),
                )
                with self.db.transaction() as tx:
                    self._authorize(tx)
                    tx.execute("UPDATE tool_calls SET request_id=? WHERE id=?", (observation["observation_id"], call_id))
                self.manager.retrieval(self.bid, max(observation["bytes"], observation.get("payload", {}).get("output_bytes", 0)))
                if "payload" not in observation:
                    status = observation.get("status", "failed")
                    if status not in {"denied", "timed_out", "failed"}:
                        status = "failed"
                    error = observation.get("error", "runtime_read_failed")
                    with self.db.transaction() as tx:
                        tx.execute(
                            "UPDATE tool_calls SET status=?,ended_at=?,error_code=? WHERE id=?",
                            (status, now(), "ssh_credential_unavailable" if observation.get("error_code") == "ssh_credential_unavailable" else error, call_id),
                        )
                    return {
                        "call_id": call_id, "tool_id": tool, "status": status,
                        "error": error, "observation_id": observation["observation_id"],
                    "target_id": self.target_id,
                        "error_code": observation.get("error_code"), "diagnostic": observation.get("diagnostic", {}),
                    }
                payload = {
                    **observation["payload"],
                    "observation_id": observation["observation_id"],
                    "source_type": "Runtime",
                }
            evidence_id, stamp = uid(), now()
            content_hash = digest(payload)
            with self.db.transaction() as tx:
                self._authorize(tx)
                tx.execute(
                    "UPDATE tool_calls SET status='succeeded',ended_at=? WHERE id=?",
                    (stamp, call_id),
                )
                tx.execute(
                    "INSERT INTO evidence(id,tool_call_id,target_node_id,observed_at,payload_redacted_json,content_hash,truncated,freshness_state) VALUES(?,?,?,?,?,?,?,'current')",
                    (
                        evidence_id,
                        call_id,
                        action["target_node_id"],
                        stamp,
                        dumps(payload),
                        content_hash,
                        int(bool(payload.get("partial") or payload.get("truncated"))),
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
                "observation_id": observation.get("observation_id") if not tool.startswith("code.") else None,
                "target_id": self.target_id if not tool.startswith("code.") else None,
                "bytes": observation.get("bytes", 0) if not tool.startswith("code.") else 0,
                "diagnostic": observation.get("diagnostic", {}) if not tool.startswith("code.") else {},
            }
        except Exception as exc:
            if self.supervision is not None and self.supervision['cancelled'].is_set():
                raise InterruptedError('调查已终止') from None
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
                    "error_code": "global_budget_exhausted" if type(exc).__name__ == "BudgetExceeded" else None,
                    "status": "timed_out" if isinstance(exc, TimeoutError) else "denied"
                    if isinstance(exc, PermissionError) else "failed"}
        finally:
            self.manager.record_time(self.bid, time.monotonic() - started)
