from __future__ import annotations

import json
import re

from app.agent.adaptive import (
    AdaptiveInvestigationPlanner,
    action_id,
    current_graph,
    public_action,
)
from app.agent.discovery import authorization_digest, discover_for_run
from app.agent.workflow import build_workflow
from app.business.service import BusinessService
from app.common import dumps, now, uid
from app.context.budget import ContextBudgetManager
from app.knowledge import counterexamples
from app.system.scope import binding_is_current
from app.tools.business import BusinessToolRegistry


def investigate_business(database, settings, incident_id, business_id, actor, job_id=None, resume_run_id=None, target_id=None):
    from app.api.v1 import _model_configuration, emit, pattern_matches
    from app.connectors.service import RuntimeService

    profile = BusinessService(database, settings).get(business_id)
    if not target_id:
        raise PermissionError("故障调查缺少明确的 SSH 目标绑定")
    runtime = RuntimeService(database, settings)
    try:
        selected_target = runtime.get(business_id, target_id)
    except KeyError as exc:
        raise PermissionError("所选 SSH 目标已删除或不属于当前业务") from exc
    readiness = runtime.investigation_readiness(selected_target)
    if not readiness["eligible"]:
        raise PermissionError(f"所选 SSH 目标不可用：{readiness['reason']}")
    try:
        runtime.require_credentials(business_id, target_id)
    except PermissionError as exc:
        raise PermissionError("所选 SSH 目标的会话凭据不可用") from exc
    target_signature = authorization_digest(selected_target)
    target_snapshot = {key: selected_target.get(key) for key in (
        "id", "name", "host", "port", "username", "connector", "auth_mode", "execution_mode",
        "access_authorized", "permission_boundary", "docker_reader", "docker_reads_enabled",
    )}
    target_snapshot["authorization_digest"] = target_signature
    conn = database.connect()
    try:
        incident = dict(
            conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        )
        snapshot = dict(
            conn.execute(
                "SELECT * FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1",
                (incident["system_id"],),
            ).fetchone()
        )
        sid = snapshot["id"]
        nodes = [
            {**dict(r), "attributes": json.loads(r["attributes_json"])}
            for r in conn.execute("SELECT * FROM graph_nodes WHERE snapshot_id=?", (sid,))
        ]
        edges = [
            {**dict(r), "attributes": json.loads(r["attributes_json"])}
            for r in conn.execute("SELECT * FROM graph_edges WHERE snapshot_id=?", (sid,))
        ]
        conflicts = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM graph_conflicts WHERE snapshot_id=? AND status='open'", (sid,)
            )
        ]
        bindings = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM service_bindings WHERE business_id=? AND target_id=? AND status='confirmed'",
                (business_id, target_id),
            )
        ]
        targets = {target_id: selected_target}
        old_run = dict(conn.execute("SELECT * FROM diagnosis_runs WHERE id=? AND incident_id=?", (resume_run_id, incident_id)).fetchone()) if resume_run_id else None
    finally:
        conn.close()
    bindings = [b for b in bindings if binding_is_current(b)]
    run_id, stamp = resume_run_id or uid(), now()
    symptom = json.loads(incident["symptom_json"])
    paused = json.loads(old_run["summary_json"]) if old_run else None
    if paused:
        old_snapshot = json.loads(old_run["target_snapshot_json"] or "{}")
        if old_run["target_id"] != target_id or old_snapshot.get("id") != target_id:
            raise PermissionError("恢复请求与原调查 SSH 目标不一致")
        if old_snapshot.get("authorization_digest") != target_signature:
            raise PermissionError("原调查目标授权或执行配置已变化，不能恢复")
        if old_run["status"] != "awaiting_review" or paused.get("stage") != "awaiting_review":
            raise PermissionError("此调查未等待范围确认")
        if profile["profile_version"] != paused["profile_version"] or profile["review_status"] != "approved":
            raise PermissionError("业务画像版本已变化，需重新审核并发起调查")
        if paused.get("authorization_digests") and paused["authorization_digests"] != {target_id: target_signature}:
            raise PermissionError("原调查授权记录与 Run 目标不一致，不能恢复")
        manager = ContextBudgetManager(database, settings)
        manager.remaining_seconds(paused["budget_id"])
        if manager.get(paused["budget_id"])["tool_calls"] >= manager.get(paused["budget_id"])["limits"]["max_tools"]:
            raise PermissionError("调查工具预算已耗尽")
        bindings = [b for b in bindings if b["target_id"] == target_id and b["kind"] in {"service", "container"}]
        if not bindings:
            raise PermissionError("还没有有效且已确认的服务或容器映射")
    with database.transaction() as tx:
        if paused:
            tx.execute("UPDATE diagnosis_runs SET status='running',ended_at=NULL,graph_snapshot_id=? WHERE id=?", (sid, run_id))
        else:
            tx.execute(
            "INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status,summary_json,target_id,target_snapshot_json) VALUES(?,?,?,?,?,?,?,'running',?,?,?)",
            (
                run_id,
                incident_id,
                sid,
                symptom.get("mode", "normal"),
                dumps(_model_configuration(settings)),
                dumps(
                    {
                        "max_tools": settings.investigation_tool_calls,
                        "max_rounds": settings.investigation_model_rounds,
                    }
                ),
                stamp,
                dumps({"stage": "profile_and_scope", "business_id": business_id}),
                target_id,
                dumps(target_snapshot),
            ),
            )
        emit(
            tx,
            incident["system_id"],
            run_id,
            "run",
            run_id,
            "business_profile_loaded",
            {
                "business_id": business_id,
                "profile_version": profile["profile_version"],
                "review_status": profile["review_status"],
            },
            actor,
        )
        if job_id:
            tx.execute(
                "UPDATE jobs SET result_json=? WHERE id=?", (dumps({"run_id": run_id}), job_id)
            )
    manager = ContextBudgetManager(database, settings)
    budget_id = paused["budget_id"] if paused else manager.create("investigation", business_id, run_id)
    registry = BusinessToolRegistry(database, settings, business_id, run_id, sid, budget_id, target_id)
    graph = current_graph({"snapshot_id": sid, "nodes": nodes, "edges": edges, "open_conflicts": conflicts}, bindings)
    nodes, edges = graph["nodes"], graph["edges"]
    adaptive = symptom.get("mode", "normal") in {"normal", "graph_adaptive"}
    service_nodes = {
        n["name"]: n for n in nodes if n["kind"] == "service" and n["role"] != "runtime"
    }
    if (not any(b["kind"] in {"service", "container"} and b["service"] in service_nodes for b in bindings)
            and profile["review_status"] == "approved" and targets and not paused):
        anchor = next(iter(service_nodes.values()))["id"] if service_nodes else None
        summary = discover_for_run(database, settings, profile, targets, registry, anchor, budget_id)
        status = "awaiting_review" if summary["stage"] == "awaiting_review" else "unresolved"
        with database.transaction() as tx:
            tx.execute("UPDATE diagnosis_runs SET status=?,ended_at=?,summary_json=? WHERE id=?", (status, None if status == "awaiting_review" else now(), dumps(summary), run_id))
            emit(tx, incident["system_id"], run_id, "run", run_id, "discovery_" + summary["stage"],
                 {"collection_status": summary["collection_status"], "proposal_count": len(summary["mapping_proposals"])}, actor)
        return {"id": run_id, "run_id": run_id, "status": status, "stop_reason": summary["stop_reason"], "unresolved_reason": summary["unresolved_reason"]}
    evidence, code_context, history, iterations, matches = [], [], [], [], []
    reason, stop_reason, model_error = None, None, None
    if profile["review_status"] != "approved":
        reason = "业务画像尚未人工审核，未执行深入调查"
    elif not bindings:
        reason = "尚无经过人工确认的运行对象；需要先确认探索范围"
    # Start with the mentioned service, or an approved application entry. Add its declared dependencies.
    usable = [
        b
        for b in bindings
        if b["service"] in service_nodes and b["kind"] in {"service", "container"}
    ]
    usable.sort(
        key=lambda b: (
            b["service"].casefold() not in symptom["text"].casefold(),
            service_nodes[b["service"]]["role"] != "application",
            b["service"],
        )
    )
    root = usable[0] if usable else None
    if not root and not reason:
        reason = "当前映射只有普通进程，没有已授权的服务状态/日志入口"
    root_id = service_nodes[root["service"]]["id"] if root else None
    planner = AdaptiveInvestigationPlanner(graph, bindings, targets, symptom["text"], root_id, settings)
    if reason:
        stop_reason = "permission_blocked"
    elif conflicts:
        reason, stop_reason = "当前静态配置与观测存在待审核冲突", "evidence_conflict"
    selected_names = {root["service"]} if root else set()
    if root:
        root_id = service_nodes[root["service"]]["id"]
        selected_names.update(
            n["name"]
            for n in nodes
            if any(
                e["relation"] == "depends_on"
                and e["from_node_id"] == root_id
                and e["to_node_id"] == n["id"]
                and e["certainty"] != "inferred"
                for e in edges
            )
        )
    planned = [b for b in usable if b["service"] in selected_names]
    roles = {n["role"] for n in service_nodes.values()}
    role_ids = {
        role: [n["id"] for n in service_nodes.values() if n["role"] == role] for role in roles
    }
    role_by_id = {n["id"]: n["role"] for n in nodes}
    topology = {
        f"{role_by_id.get(e['from_node_id'])}->{role_by_id.get(e['to_node_id'])}:{e['relation']}"
        for e in edges
    }
    signals = {(f"{b['kind']}.logs", service_nodes[b["service"]]["role"]) for b in usable}
    if symptom.get("mode") in {"normal", "graph_pattern"}:
        with database.transaction() as tx:
            matches = pattern_matches(
                tx,
                run_id,
                sid,
                symptom["text"],
                roles,
                topology,
                signals,
                bool(conflicts),
                role_ids,
            )
    suggested = next((m for m in matches if m["decision"] == "suggested"), None)
    if suggested and not adaptive:
        suggested_roles = set(suggested["pattern"]["roles"])
        narrowed = [b for b in planned if service_nodes[b["service"]]["role"] in suggested_roles]
        planned = narrowed or planned

    def action_for(binding, suffix):
        kind = binding["kind"]
        return {
            "tool_id": f"{kind}.{suffix}",
            "target_node_id": service_nodes[binding["service"]]["id"],
            "target_id": binding["target_id"],
            "arguments": {"name": binding["runtime_id"]}
            if kind == "service"
            else {"container_id": binding["runtime_id"]},
            "service": binding["service"],
            "role": service_nodes[binding["service"]]["role"],
            "binding_id": binding["id"],
        }

    def execute(action):
        nonlocal reason, stop_reason
        budget = manager.get(budget_id)
        if budget["tool_calls"] >= budget["limits"]["max_tools"]:
            reason, stop_reason = "工具调用预算已耗尽", "tool_budget_exhausted"
            return {"call_id": None, "error": "BudgetExceeded"}
        action["action_id"] = action_id(action)
        if action.get("binding_id"):
            with database.transaction() as tx:
                binding = tx.execute("SELECT * FROM service_bindings WHERE id=? AND business_id=? AND target_id=?",
                                     (action["binding_id"], business_id, target_id)).fetchone()
            if action.get("target_id") != target_id or not binding or not binding_is_current(binding):
                manager.consume_tool(budget_id)
                call_id = uid()
                denial_code = "target_binding_mismatch" if action.get("target_id") != target_id else "binding_not_current"
                with database.transaction() as tx:
                    tx.execute(
                        "INSERT INTO tool_calls(id,run_id,snapshot_id,purpose,tool_id,adapter_version,args_redacted_json,risk,policy_version,status,request_id,started_at,ended_at,error_code,target_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (call_id, run_id, sid, "diagnosis", action["tool_id"], "adaptive-v2.2", dumps(action["arguments"]), "LOW", "current-binding-v2.2", "denied", uid(), now(), now(), denial_code, target_id),
                    )
                    emit(tx, incident["system_id"], run_id, "tool", call_id, "read_tool_denied", {"reason": denial_code}, actor)
                reason, stop_reason = ("工具目标与 Run 绑定不一致，已拒绝执行" if denial_code == "target_binding_mismatch"
                                       else "服务绑定已经过期或撤销，需要重新确认范围"), "permission_blocked"
                history.append({**public_action(action), "target_id": action.get("target_id"), "call_id": call_id, "status": "denied", "new_information": False})
                return {"call_id": call_id, "error": "PermissionError"}
        with database.transaction() as tx:
            emit(
                tx,
                incident["system_id"],
                run_id,
                "tool",
                action["tool_id"],
                "read_tool_selected",
                {
                    "tool_id": action["tool_id"],
                    "service": action["service"],
                    "target_node_id": action["target_node_id"],
                },
                actor,
            )
        result = registry.execute(action)
        result.update(target_id=action.get("target_id"), action_id=action["action_id"], arguments=action["arguments"])
        history.append(
            {
                **public_action(action),
                "target_id": action.get("target_id"),
                "tool_id": action["tool_id"],
                "target_node_id": action["target_node_id"],
                "call_id": result["call_id"],
                "status": result.get("status", "succeeded" if result.get("evidence_id") else "failed"),
                "new_information": bool(result.get("evidence_id")),
            }
        )
        if result.get("error_code") == "ssh_credential_unavailable":
            reason, stop_reason = "SSH 密码会话未提供、已过期或后端重启后失效；请在所选目标页面重新输入密码", "ssh_credential_unavailable"
        elif result.get("status") == "denied" or result.get("error") == "PermissionError":
            reason, stop_reason = "服务器权限拒绝了关键只读检查，需要审核业务范围", "permission_blocked"
        elif result.get("error") == "BudgetExceeded":
            budget_reason = manager.get(budget_id)["stop_reason"]
            stop_reason, reason = {
                "retrieval_limit": ("context_budget_exhausted", "读取资料预算已耗尽"),
                "tool_call_limit": ("tool_budget_exhausted", "工具调用预算已耗尽"),
                "execution_time_limit": ("execution_time_limit", "调查执行时间预算已耗尽"),
                "provider_usage_exceeded_reservation": (
                    "provider_usage_exceeded_reservation",
                    "供应商实际用量超过本次预留预算，已停止后续工具和模型请求，需要人工审核预算",
                ),
            }.get(budget_reason, (budget_reason or "context_budget_exhausted", "调查预算已阻止后续工具执行"))
        if result.get("evidence_id"):
            if action["tool_id"].startswith("code."):
                code_context.append(result["payload"])
            else:
                evidence.append(result)
        return result

    if not reason:
        seed = [root] if adaptive else planned[: max(1, settings.investigation_tool_calls // 2)]
        for binding in seed:
            execute(action_for(binding, "status"))
            if not reason:
                execute(action_for(binding, "logs"))
    workflow = build_workflow(settings, database, budget_id)
    report = {"summary": reason or "正在收集可核验的证据", "next_action": "", "hypotheses": []}
    gate = {"status": "unresolved", "hypotheses": [], "rationale": reason or "证据不足"}
    for iteration in range(settings.investigation_model_rounds if not reason else 0):
        used = {(h["tool_id"], h["target_node_id"]) for h in history}
        actions = planner.candidates(evidence, code_context, history) if adaptive else [
            action_for(b, suffix)
            for b in planned
            for suffix in ("status", "logs")
            if (f"{b['kind']}.{suffix}", service_nodes[b["service"]]["id"]) not in used
        ]
        if root and not adaptive:
            keyword = next(
                iter(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", symptom["text"])), root["service"]
            )
            if ("code.search", root_id) not in used:
                actions.append(
                    {
                        "tool_id": "code.search",
                        "target_node_id": root_id,
                        "service": root["service"],
                        "role": service_nodes[root["service"]]["role"],
                        "arguments": {
                            "keyword": keyword,
                            "limit": min(5, settings.code_search_limit),
                        },
                    }
                )
            for context in code_context:
                for result in context.get("results", [])[:5]:
                    if not any(c.get("file_id") == result["file_id"] for c in code_context):
                        # One context action per target per round; supplied file ID is policy owned.
                        actions.append(
                            {
                                "tool_id": "code.context",
                                "target_node_id": root_id,
                                "service": root["service"],
                                "role": service_nodes[root["service"]]["role"],
                                "arguments": {"file_id": result["file_id"], "max_lines": 50},
                            }
                        )
                        break
        budget = manager.get(budget_id)
        if budget["tool_calls"] >= budget["limits"]["max_tools"]:
            reason, stop_reason = "工具调用预算已耗尽", "tool_budget_exhausted"
            break
        with database.transaction() as tx:
            tx.execute(
                "UPDATE diagnosis_runs SET summary_json=? WHERE id=?",
                (
                    dumps(
                        {
                            "stage": "reasoning",
                            "round": iteration + 1,
                            "business_id": business_id,
                            "budget_id": budget_id,
                            "hypotheses": planner.hypotheses,
                            "hypothesis_history": planner.hypothesis_history,
                            "iterations": iterations,
                        }
                    ),
                    run_id,
                ),
            )
        state = workflow.invoke(
            {
                "incident": {"symptom": symptom["text"], "window_start": incident["window_start"]},
                "graph": graph,
                "evidence": evidence,
                "available_actions": [
                    public_action(a) if adaptive else {k: a[k] for k in ("tool_id", "target_node_id", "service", "role")}
                    for a in actions
                ],
                "action_history": history,
                "business_profile": {
                    "business_id": business_id,
                    "profile_version": profile["profile_version"],
                    "name": profile["name"],
                    "services": [
                        {"name": s["name"], "role": s["role"]} for s in profile["services"][:20]
                    ],
                    "unknowns": profile["unknowns"][:4],
                },
                "runtime_bindings": [
                    {k: b[k] for k in ("service", "kind", "runtime_id")} for b in planned
                ],
                "code_context": code_context,
                "patterns": [
                    {"checks": m["pattern"]["checks"], "binding": m["binding"]}
                    for m in matches
                    if m["decision"] == "suggested"
                ],
                "adaptive": adaptive,
                "previous_hypotheses": planner.hypotheses,
                "hypothesis_history": planner.hypothesis_history,
                "scope_requests": planner.scope_requests,
            }
        )
        if state.get("model_error"):
            model_error = state["model_error"]
            reason, stop_reason = model_error["message"], model_error["code"]
            # Keep prior hypotheses, counter-evidence and report intact on a failed round.
            gate = {**gate, "status": "unresolved", "supported_index": None, "rationale": reason}
            iterations.append({"number": iteration + 1, "gate": gate, "model_error": model_error,
                               "proposed_action": None, "executed_action": None, "policy_outcome": "model_failed",
                               "hypotheses": [], "candidate_actions": [public_action(a) for a in actions]})
            break
        report, gate = state["report"], state["gate"]
        lifecycle = planner.update(report, gate, iteration + 1) if adaptive else gate.get("hypotheses", [])
        if adaptive and gate["status"] == "supported" and not any(h["status"] == "supported" for h in lifecycle):
            gate = {**gate, "status": "unresolved", "supported_index": None,
                    "rationale": "已有反证仍未解释，不能因模型遗漏反证而确认假设", "hypotheses": lifecycle}
        proposal = report.get("action")
        action = next(
            (
                a
                for a in actions
                if isinstance(proposal, dict)
                and ((adaptive and a.get("action_id") == proposal.get("action_id"))
                     or (not adaptive and a["tool_id"] == proposal.get("tool_id")
                         and a["target_node_id"] == proposal.get("target_node_id")))
            ),
            None,
        )
        outcome = {
            "number": iteration + 1,
            "gate": gate,
            "proposed_action": proposal,
            "executed_action": None,
            "policy_outcome": None,
            "hypotheses": lifecycle,
            "candidate_actions": [public_action(a) for a in actions],
        }
        iterations.append(outcome)
        if gate["status"] == "supported":
            stop_reason = "evidence_supported"
            break
        if adaptive and planner.requested_scope(report):
            reason, stop_reason = "关键服务尚未授权，已记录范围扩展申请", "permission_blocked"
            outcome["policy_outcome"] = "scope_request"
            break
        if iteration == settings.investigation_model_rounds - 1:
            reason, stop_reason = "调查轮数上限已达到", "round_limit"
            break
        if not action:
            reason = state.get("provider_error") or (
                "模型提出的检查不在授权范围"
                if proposal
                else "没有可继续区分假设的授权检查或模型尚未配置"
            )
            outcome["policy_outcome"] = "rejected" if proposal else "no_action"
            stop_reason = "action_rejected" if proposal else "permission_blocked" if adaptive and planner.scope_requests else "no_new_actions"
            if adaptive and not proposal and planner.hypothesis_history and not any(h["status"] != "refuted" for h in lifecycle):
                reason, stop_reason = "现有主要假设与反证冲突，且没有新的区分方向", "evidence_conflict"
            break
        result = execute(action)
        outcome.update(
            executed_action=public_action(action),
            policy_outcome="allowed" if result.get("evidence_id") else "denied" if result.get("error") == "PermissionError" else "failed",
            tool_call_id=result["call_id"],
            evidence_id=result.get("evidence_id"),
        )
        if result.get("error") in {"BudgetExceeded", "PermissionError"} or result.get("status") == "denied":
            stop_reason = stop_reason or ("tool_budget_exhausted" if result.get("error") == "BudgetExceeded" else "permission_blocked")
            reason = reason or "只读工具未能取得有效观测：" + result["error"]
            break
    supported_id = None
    with database.transaction() as tx:
        for i, item in enumerate(iterations, 1):
            step_id = uid()
            primary_id = None
            primary_score = 0.0
            for hypothesis in item.get("hypotheses", item["gate"].get("hypotheses", [])):
                hid = uid()
                valid_target = (
                    hypothesis["target_node_id"]
                    if hypothesis["target_node_id"] in {n["id"] for n in nodes}
                    else None
                )
                tx.execute(
                    "INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?)",
                    (
                        hid,
                        run_id,
                        valid_target,
                        hypothesis["failure_mode"],
                        dumps(hypothesis["causal_chain"]),
                        hypothesis["prediction"],
                        hypothesis["status"],
                        hypothesis["confidence"],
                    ),
                )
                primary_id = primary_id or hid
                for link in hypothesis.get("evidence_links", []):
                    tx.execute(
                        "INSERT OR IGNORE INTO hypothesis_evidence VALUES(?,?,?,?)",
                        (
                            hid,
                            link["evidence_id"],
                            "support" if link["relation"] == "supports" else "refute",
                            link["explanation"],
                        ),
                    )
                primary_score = max(primary_score, hypothesis["confidence"])
                if hypothesis["status"] == "supported":
                    supported_id = hid
            tx.execute(
                "INSERT INTO diagnostic_steps VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    step_id,
                    run_id,
                    i,
                    supported_id or primary_id,
                    dumps(item),
                    0,
                    primary_score,
                    item["gate"]["status"],
                    item["gate"]["rationale"],
                    now(),
                ),
            )
            step_hypotheses = item.get("hypotheses", item["gate"].get("hypotheses", []))
            primary_hypothesis = next((h for h in step_hypotheses if h["status"] == "supported"), step_hypotheses[0] if step_hypotheses else None)
            for hypothesis in [primary_hypothesis] if primary_hypothesis else []:
                for link in hypothesis.get("evidence_links", []):
                    tx.execute(
                        "INSERT OR IGNORE INTO step_evidence VALUES(?,?,?,?)",
                        (
                            step_id,
                            link["evidence_id"],
                            "support" if link["relation"] == "supports" else "refute",
                            link["explanation"],
                        ),
                    )
        status = "supported" if supported_id else "unresolved"
        summary = {
            "stage": "stopped" if model_error else "completed",
            "model_status": "failed" if model_error else "offline" if report.get("analysis_status") == "offline" else "completed" if iterations else "not_called",
            "model_error": model_error,
            "analysis_status": "model_failed" if model_error else report.get("analysis_status"),
            "business_id": business_id,
            "profile_version": profile["profile_version"],
            "summary": report.get("summary"),
            "next_action": report.get("next_action"),
            "gate": gate,
            "unresolved_reason": reason if not supported_id else None,
            "stop_reason": "model_budget_exhausted" if stop_reason == "model_budget_exceeded" else stop_reason or "no_new_actions",
            "hypotheses": planner.hypotheses,
            "hypothesis_history": planner.hypothesis_history,
            "scope_requests": planner.scope_requests,
            "budget_id": budget_id,
            "iterations": iterations,
            "code_sources": [
                {
                    k: c[k]
                    for k in (
                        "file_id",
                        "path",
                        "source_digest",
                        "profile_version",
                        "start_line",
                        "end_line",
                    )
                    if k in c
                }
                for c in code_context
                if "file_id" in c
            ],
            "pattern_matches": matches,
            "evidence_ids": [e["evidence_id"] for e in evidence],
            "connector_types": sorted({e["payload"].get("connector", "unknown") for e in evidence}),
        }
        if paused:
            summary.update({k: paused[k] for k in ("discovery_ids", "mapping_proposals", "discovery_evidence_ids", "authorization_digests")})
            summary["resumed_after_review"] = True
        ended = now()
        summary["counterexamples"] = counterexamples(summary, status)
        tx.execute(
            "UPDATE diagnosis_runs SET status=?,supported_at=?,ended_at=?,final_hypothesis_id=?,summary_json=? WHERE id=?",
            (status, ended if supported_id else None, ended, supported_id, dumps(summary), run_id),
        )
        emit(
            tx,
            incident["system_id"],
            run_id,
            "run",
            run_id,
            f"run_{status}",
            {"reason": reason, "evidence_count": len(evidence)},
            actor,
        )
        if summary["counterexamples"]:
            emit(tx, incident["system_id"], run_id, "run", run_id, "investigation_counterexamples_retained", {"counterexamples": summary["counterexamples"]}, actor)
    return {
        "id": run_id,
        "run_id": run_id,
        "status": status,
        "unresolved_reason": reason if not supported_id else None,
        "stop_reason": stop_reason or "no_new_actions",
    }
