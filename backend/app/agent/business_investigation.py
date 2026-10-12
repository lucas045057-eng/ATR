from __future__ import annotations

import json
import re

from app.agent.adaptive import (
    AdaptiveInvestigationPlanner,
    action_id,
    current_graph,
    public_action,
)
from app.agent.decision_runtime import claims_from_hypotheses, classify_outcome, facts_from_state
from app.agent.discovery import authorization_digest, discover_for_run
from app.agent.task_stop_policy import (
    bind_objective_scope,
    build_investigation_plan,
    decide_stop,
    evaluate_progress,
    filter_actions_for_objective,
    load_task_snapshot,
    make_task_snapshot,
    merge_plan_candidates,
    resolve_objective,
    select_finish_override,
    soft_limits_from_settings,
    sync_plan_with_history,
)
from app.agent.workflow import build_workflow
from app.business.service import BusinessService
from app.common import dumps, now, uid
from app.context.budget import ContextBudgetManager
from app.knowledge import counterexamples
from app.system.scope import binding_is_current
from app.tools.business import BusinessToolRegistry


def _decision_protocol_for_run(old_run=None, *, resumed=False, configured="minimal_v2"):
    if not resumed:
        return configured if configured in {"legacy_v22", "minimal_v2"} else "legacy_v22"
    if old_run is None:
        raise ValueError("Unsupported decision protocol snapshot")
    try:
        snapshot = json.loads(old_run.get("model_config_json") or "{}") if old_run else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Unsupported decision protocol snapshot") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("Unsupported decision protocol snapshot")
    if "decision_protocol" not in snapshot:
        return "legacy_v22"
    protocol = snapshot["decision_protocol"]
    if isinstance(protocol, str) and protocol in {"legacy_v22", "minimal_v2"}:
        return protocol
    raise ValueError("Unsupported decision protocol snapshot")


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
    adaptive = symptom.get("mode", "normal") in {"normal", "graph_adaptive"}
    decision_protocol = _decision_protocol_for_run(
        old_run, resumed=bool(paused), configured=settings.agent_decision_protocol,
    )
    saved_task_snapshot = load_task_snapshot(old_run.get("model_config_json")) if paused else None
    task_policy_enabled = bool(saved_task_snapshot and decision_protocol == "minimal_v2" and adaptive)
    task_objective = saved_task_snapshot.get("objective") if task_policy_enabled else None
    task_stop_policy_snapshot = saved_task_snapshot if task_policy_enabled else None
    investigation_plan = paused.get("investigation_plan") if task_policy_enabled and isinstance(paused, dict) else None
    progress_state = paused.get("progress_state", {}) if task_policy_enabled and isinstance(paused, dict) else {}
    progress_summary = paused.get("progress_summary", {}) if task_policy_enabled and isinstance(paused, dict) else {}
    soft_limit_final_step_used = bool(paused.get("soft_limit_final_step_used")) if task_policy_enabled and isinstance(paused, dict) else False
    task_completion_status = "in_progress" if task_policy_enabled else None
    if not adaptive:
        decision_protocol = "legacy_v22"
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
    model_config = _model_configuration(settings, decision_protocol)
    initial_task_snapshot = None
    graph_service_names = {node["name"] for node in nodes if node["kind"] == "service" and node["role"] != "runtime"}
    has_confirmed_runtime_binding = any(
        binding["kind"] in {"service", "container"} and binding["service"] in graph_service_names
        for binding in bindings
    )
    initial_run_summary = {"stage": "profile_and_scope", "business_id": business_id}
    if not paused and adaptive and decision_protocol == "minimal_v2" and not has_confirmed_runtime_binding:
        task_type = symptom.get("task_type", "focused_diagnosis")
        initial_objective = resolve_objective(
            task_type, [], soft_limits=soft_limits_from_settings(settings, task_type),
            no_progress_rounds=settings.investigation_no_progress_rounds,
            minimum_useful_score=settings.investigation_minimum_useful_score,
            high_value_score=settings.investigation_high_value_score,
        )
        initial_task_snapshot = make_task_snapshot(initial_objective)
        model_config["task_stop_policy"] = initial_task_snapshot
        initial_run_summary.update(task_objective=initial_objective,
                                   task_policy_version=initial_task_snapshot["version"],
                                   task_completion_status="in_progress")
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
                dumps(model_config),
                dumps(
                    {
                        "max_tools": settings.investigation_tool_calls,
                        "max_rounds": settings.investigation_model_rounds,
                    }
                ),
                stamp,
                dumps(initial_run_summary),
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
    service_nodes = {
        n["name"]: n for n in nodes if n["kind"] == "service" and n["role"] != "runtime"
    }
    if (not any(b["kind"] in {"service", "container"} and b["service"] in service_nodes for b in bindings)
            and profile["review_status"] == "approved" and targets and not paused):
        anchor = next(iter(service_nodes.values()))["id"] if service_nodes else None
        summary = discover_for_run(database, settings, profile, targets, registry, anchor, budget_id)
        if initial_task_snapshot:
            summary.update(task_objective=initial_task_snapshot["objective"],
                           task_policy_version=initial_task_snapshot["version"],
                           task_completion_status="in_progress",
                           stop_reason="awaiting_scope_review")
        status = "awaiting_review" if summary["stage"] == "awaiting_review" else "unresolved"
        with database.transaction() as tx:
            tx.execute("UPDATE diagnosis_runs SET status=?,ended_at=?,summary_json=? WHERE id=?", (status, None if status == "awaiting_review" else now(), dumps(summary), run_id))
            emit(tx, incident["system_id"], run_id, "run", run_id, "discovery_" + summary["stage"],
                 {"collection_status": summary["collection_status"], "proposal_count": len(summary["mapping_proposals"])}, actor)
        return {"id": run_id, "run_id": run_id, "status": status, "stop_reason": summary["stop_reason"], "unresolved_reason": summary["unresolved_reason"]}
    evidence, code_context, history, iterations, matches = [], [], [], [], []
    reason, stop_reason, model_error = None, None, None
    consecutive_relation_error_rounds = 0
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
    if not paused and decision_protocol == "minimal_v2":
        task_type = symptom.get("task_type", "focused_diagnosis")
        soft_limits = soft_limits_from_settings(settings, task_type)
        task_services = [
            {"node_id": service_nodes[binding["service"]]["id"],
             "name": binding["service"], "role": service_nodes[binding["service"]]["role"]}
            for binding in planned
        ]
        task_objective = resolve_objective(
            task_type, task_services, soft_limits=soft_limits,
            no_progress_rounds=settings.investigation_no_progress_rounds,
            minimum_useful_score=settings.investigation_minimum_useful_score,
            high_value_score=settings.investigation_high_value_score,
        )
        task_stop_policy_snapshot = make_task_snapshot(task_objective)
        task_policy_enabled = True
        task_completion_status = "in_progress"
        with database.transaction() as tx:
            row = tx.execute("SELECT model_config_json FROM diagnosis_runs WHERE id=?", (run_id,)).fetchone()
            model_config = json.loads(row["model_config_json"] or "{}")
            model_config["task_stop_policy"] = task_stop_policy_snapshot
            tx.execute("UPDATE diagnosis_runs SET model_config_json=? WHERE id=?",
                       (dumps(model_config), run_id))
    elif paused and task_policy_enabled and task_objective and not task_objective.get("target_node_ids"):
        task_services = [
            {"node_id": service_nodes[binding["service"]]["id"],
             "name": binding["service"], "role": service_nodes[binding["service"]]["role"]}
            for binding in planned
        ]
        task_objective = bind_objective_scope(task_objective, task_services)
        task_stop_policy_snapshot = {**task_stop_policy_snapshot, "objective": task_objective}
        with database.transaction() as tx:
            model_config = json.loads(tx.execute(
                "SELECT model_config_json FROM diagnosis_runs WHERE id=?", (run_id,)
            ).fetchone()["model_config_json"] or "{}")
            model_config["task_stop_policy"] = task_stop_policy_snapshot
            tx.execute("UPDATE diagnosis_runs SET model_config_json=? WHERE id=?",
                       (dumps(model_config), run_id))
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

    if task_policy_enabled and task_objective:
        required_actions = []
        if root:
            for required_check in task_objective.get("required_checks", []):
                candidate = action_for(root, required_check["check_type"])
                candidate["action_id"] = action_id(candidate)
                required_actions.append(candidate)
        candidate_actions = planner.candidates(evidence, code_context, history) if not reason else []
        if not investigation_plan:
            investigation_plan = build_investigation_plan(
                task_objective, required_actions + candidate_actions,
            )
        else:
            investigation_plan = merge_plan_candidates(
                task_objective, investigation_plan, required_actions + candidate_actions,
            )
        investigation_plan = sync_plan_with_history(investigation_plan, history)
        with database.transaction() as tx:
            row = tx.execute("SELECT summary_json FROM diagnosis_runs WHERE id=?", (run_id,)).fetchone()
            run_summary = json.loads(row["summary_json"] or "{}")
            if not isinstance(run_summary, dict):
                run_summary = {}
            run_summary.update({
                "task_objective": task_objective,
                "task_policy_version": task_stop_policy_snapshot["version"],
                "investigation_plan": investigation_plan,
                "task_completion_status": task_completion_status,
                "progress_summary": progress_summary,
                "progress_state": progress_state,
                "soft_limit_final_step_used": soft_limit_final_step_used,
            })
            tx.execute("UPDATE diagnosis_runs SET summary_json=? WHERE id=?", (dumps(run_summary), run_id))

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
                history.append({**public_action(action), "target_id": action.get("target_id"), "call_id": call_id,
                                "status": "denied", "new_information": False, "evidence_id": None})
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
                "evidence_id": result.get("evidence_id"),
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
    if task_policy_enabled and investigation_plan is not None:
        investigation_plan = sync_plan_with_history(investigation_plan, history)
        progress_result = evaluate_progress(
            evidence, progress_state, [], [], history, planner.hypothesis_history, 0,
            sum(1 for check in investigation_plan["checks"]
                if check.get("required") and check.get("status") == "pending"),
        )
        progress_summary, progress_state = progress_result["progress"], progress_result["progress_state"]
    workflow = build_workflow(settings, database, budget_id, decision_protocol)
    report = {"summary": reason or "正在收集可核验的证据", "next_action": "", "hypotheses": []}
    gate = {"status": "unresolved", "hypotheses": [], "rationale": reason or "证据不足"}
    last_runtime_outcome = None
    last_runtime_facts = []
    last_runtime_claims = []
    runtime_decision_audits = []
    runtime_finish_override_audits = []
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
        if task_policy_enabled and task_objective and investigation_plan is not None:
            investigation_plan = merge_plan_candidates(task_objective, investigation_plan, actions)
            investigation_plan = sync_plan_with_history(investigation_plan, history)
            actions = filter_actions_for_objective(task_objective, investigation_plan, actions)
            budget_state = manager.get(budget_id)
            hard_stop = budget_state.get("stop_reason")
            if not hard_stop and budget_state["calls"] >= budget_state["limits"]["max_calls"]:
                hard_stop = "model_call_limit"
            if not hard_stop and budget_state["tool_calls"] >= budget_state["limits"]["max_tools"]:
                hard_stop = "tool_call_limit"
            if (not hard_stop and budget_state["input_tokens"] + budget_state["output_tokens"]
                    + budget_state["reserved_tokens"] >= budget_state["limits"]["max_total_tokens"]):
                hard_stop = "total_token_limit"
            scope_ids = task_objective.get("target_node_ids", [])
            finding_state = {"incident": {"window_start": incident["window_start"]},
                             "graph": graph, "evidence": evidence,
                             "scope_node_ids": scope_ids, "available_actions": actions,
                             "scope_truncated": bool(task_objective.get("scope_omission_count", 0))}
            current_finding = classify_outcome(finding_state, gate)
            task_decision = decide_stop(
                task_objective, investigation_plan, progress_summary, gate.get("status", "unresolved"),
                actions, model_calls=budget_state["calls"], tool_calls=budget_state["tool_calls"],
                active_hypothesis_nodes={h.get("target_node_id") for h in planner.hypotheses
                                         if h.get("status") != "refuted"},
                hard_stop_reason=hard_stop,
                soft_limit_final_step_used=soft_limit_final_step_used,
                finding_level=current_finding.get("finding_level"),
            )
            if task_decision["should_stop"]:
                task_completion_status = task_decision["task_completion_status"]
                stop_reason = task_decision["stop_reason"]
                reason = {
                    "task_completed": "已完成本次任务要求的授权状态与日志检查。",
                    "no_further_useful_checks": "当前授权范围内没有新的任务相关检查，调查以证据缺口结束。",
                    "no_progress": "连续检查没有产生新的任务相关证据或关键 Claim 变化，已停止重复调查。",
                    "task_soft_limit": "已达到本任务的模型或工具软上限，保留当前事实与证据缺口。",
                    "model_call_limit": "模型调用硬预算已耗尽。",
                    "tool_call_limit": "工具调用硬预算已耗尽。",
                    "total_token_limit": "累计 Token 硬预算已耗尽。",
                    "permission_blocked": "授权或安全边界阻止了后续检查。",
                }.get(stop_reason, "Runtime 根据任务目标与当前检查状态停止了调查。")
                last_runtime_outcome = {**current_finding, "should_stop": True,
                                        "stop_reason": stop_reason,
                                        "root_cause_confirmed": gate.get("status") == "supported"}
                report = {**report, "analysis_status": "runtime", "summary": reason,
                          "next_action": "按当前已采集证据报告结论和缺口。"}
                runtime_decision_audits.append({"task_stop_reason": stop_reason,
                                                "required_checks_remaining": task_decision["required_checks_remaining"],
                                                "remaining_check_types": [item["check_type"] for item in task_decision["remaining_checks"]]})
                break
            useful_action_ids = set(task_decision["useful_action_ids"])
            actions = [candidate for candidate in actions if candidate.get("action_id") in useful_action_ids]
            if task_decision["soft_limit_final_step"]:
                soft_limit_final_step_used = True
                runtime_finish_override_audits.append({"soft_limit_final_step": True,
                                                       "action_ids": task_decision["high_value_action_ids"][:4],
                                                       "reason": "任务级软上限到达，但仍有指向当前活跃假设的高价值授权检查。"})
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
                            "task_objective": task_objective if task_policy_enabled else None,
                            "task_policy_version": task_stop_policy_snapshot["version"] if task_stop_policy_snapshot else None,
                            "investigation_plan": investigation_plan if task_policy_enabled else None,
                            "task_completion_status": task_completion_status,
                            "progress_summary": progress_summary if task_policy_enabled else None,
                            "progress_state": progress_state if task_policy_enabled else None,
                            "soft_limit_final_step_used": soft_limit_final_step_used,
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
                "available_actions": (actions if decision_protocol == "minimal_v2" else [
                    public_action(a) if adaptive else {k: a[k] for k in ("tool_id", "target_node_id", "service", "role")}
                    for a in actions
                ]),
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
                "decision_protocol": decision_protocol,
                "runtime_only": decision_protocol == "minimal_v2" and not actions,
                "scope_node_ids": (task_objective.get("target_node_ids", []) if task_policy_enabled else
                                   [service_nodes[b["service"]]["id"] for b in planned
                                    if b["service"] in service_nodes]
                                   if decision_protocol == "minimal_v2" else
                                   [node["id"] for node in nodes
                                    if node.get("kind") == "service" and node.get("role") != "runtime"]),
                "scope_truncated": bool(task_policy_enabled and task_objective.get("scope_omission_count", 0)),
                "previous_hypotheses": planner.hypotheses,
                "hypothesis_history": planner.hypothesis_history,
                "scope_requests": planner.scope_requests,
                "task_objective": task_objective if task_policy_enabled else None,
                "investigation_plan": investigation_plan if task_policy_enabled else None,
            }
        )
        runtime_decision = state.get("runtime_decision", {})
        last_runtime_outcome = state.get("runtime_outcome")
        last_runtime_facts = runtime_decision.get("facts", [])
        last_runtime_claims = runtime_decision.get("claims", [])
        runtime_audit = state.get("runtime_decision_audit", {})
        if runtime_audit.get("rejected_hypothesis_count"):
            runtime_decision_audits.append(runtime_audit)
        if state.get("model_error"):
            model_error = state["model_error"]
            reason, stop_reason = model_error["message"], model_error["code"]
            if task_policy_enabled:
                task_completion_status = "stopped_with_error"
            # Keep prior hypotheses, counter-evidence and report intact on a failed round.
            gate = {**gate, "status": "unresolved", "supported_index": None, "rationale": reason}
            iterations.append({"number": iteration + 1, "gate": gate, "model_error": model_error,
                               "proposed_action": None, "executed_action": None, "policy_outcome": "model_failed",
                               "hypotheses": [], "candidate_actions": [public_action(a) for a in actions],
                               "runtime_outcome": last_runtime_outcome,
                               "runtime_decision_audit": runtime_audit})
            break
        report, gate = state["report"], state["gate"]
        runtime_override = None
        if task_policy_enabled and task_objective and state.get("runtime_decision", {}).get("finish"):
            latest_claim_status = {
                (claim.get("target_node_id"), claim.get("failure_mode")): claim.get("status")
                for claim in last_runtime_claims if isinstance(claim, dict)
            }
            active_nodes = set()
            for hypothesis in [*report.get("hypotheses", []), *planner.hypotheses]:
                if not isinstance(hypothesis, dict):
                    continue
                target_node_id = hypothesis.get("target_node_id")
                claim_key = (target_node_id, hypothesis.get("failure_mode"))
                current_status = latest_claim_status.get(claim_key, hypothesis.get("status"))
                if target_node_id and current_status != "refuted":
                    active_nodes.add(target_node_id)
            runtime_override, override_reason = select_finish_override(
                task_objective, investigation_plan, actions,
                (last_runtime_outcome or {}).get("runtime_action_id"), active_nodes,
            )
            if runtime_override and gate.get("status") != "supported":
                report = {**report, "action": {
                    "action_id": runtime_override["action_id"],
                    "rationale": "Runtime 为当前任务未完成的高价值证据缺口保留一次授权检查。",
                }, "next_action": "按当前活跃假设执行一项尚未完成的授权检查。"}
                runtime_audit = {**runtime_audit,
                                 "runtime_action_override": override_reason,
                                 "overridden_finish_reason": state.get("runtime_decision", {}).get("finish_reason"),
                                 "selected_action_id": runtime_override["action_id"],
                                 "selected_action_tool": runtime_override["tool_id"],
                                 "selected_action_target_node_id": runtime_override["target_node_id"],
                                 "evidence_gap": "活跃假设仍缺少该目标的独立只读观测"}
                runtime_finish_override_audits.append(runtime_audit)
                # The finish recommendation may already have marked a healthy-scope
                # outcome as terminal. Runtime has explicitly retained a useful,
                # authorized check, so clear that stale stop signal before dispatch.
                last_runtime_outcome = {**(last_runtime_outcome or {}),
                                        "should_stop": False,
                                        "stop_reason": None,
                                        "runtime_action_id": runtime_override["action_id"]}
            else:
                runtime_override = None
                stop_reason = "model_finished_without_useful_action"
                reason = "模型建议结束，Runtime 未发现能减少当前任务关键证据缺口的高价值检查。"
                task_completion_status = "stopped_with_gap"
                last_runtime_outcome = {**(last_runtime_outcome or {}),
                                        "should_stop": True,
                                        "stop_reason": stop_reason,
                                        "finding_level": classify_outcome(
                                            {"incident": {"window_start": incident["window_start"]},
                                             "graph": graph, "evidence": evidence,
                                             "scope_node_ids": task_objective.get("target_node_ids", []),
                                             "scope_truncated": bool(task_objective.get("scope_omission_count", 0)),
                                             "available_actions": actions}, gate,
                                        )["finding_level"]}
                runtime_finish_override_audits.append({"runtime_action_override": None,
                                                       "overridden_finish_reason": state.get("runtime_decision", {}).get("finish_reason"),
                                                       "stop_reason": stop_reason,
                                                       "candidate_action_count": len(actions)})
        elif (not task_policy_enabled and decision_protocol == "minimal_v2" and last_runtime_outcome
              and last_runtime_outcome.get("runtime_action_id")):
            runtime_override = next((candidate for candidate in actions
                                     if candidate.get("action_id") == last_runtime_outcome["runtime_action_id"]), None)
            if runtime_override:
                report = {**report, "action": {
                    "action_id": runtime_override["action_id"],
                    "rationale": "Runtime 根据未完成的授权证据范围继续执行评分最高的只读候选。",
                }, "next_action": "Runtime 选择当前评分最高且未执行的授权只读检查。"}
                runtime_audit = {**runtime_audit, "runtime_action_override": "finish_with_open_evidence"}
                if runtime_audit.get("rejected_hypothesis_count"):
                    runtime_decision_audits[-1] = runtime_audit
                else:
                    runtime_decision_audits.append(runtime_audit)
            else:
                last_runtime_outcome = {**last_runtime_outcome, "should_stop": True,
                                        "stop_reason": "action_not_allowed", "runtime_action_id": None}
        decision_audit = gate.get("decision_audit", {})
        rejected_count = decision_audit.get("rejected_relation_count", 0)
        consecutive_relation_error_rounds = consecutive_relation_error_rounds + 1 if rejected_count else 0
        if rejected_count:
            decision_audit = {**decision_audit, "consecutive_error_rounds": consecutive_relation_error_rounds}
            gate = {**gate, "decision_audit": decision_audit}
            with database.transaction() as tx:
                emit(tx, incident["system_id"], run_id, "model", run_id, "model_local_relations_rejected",
                     {"count": rejected_count, "validation_errors": decision_audit.get("validation_errors", [])}, actor)
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
            "runtime_outcome": last_runtime_outcome,
            "runtime_decision_audit": runtime_audit,
            "claims": last_runtime_claims,
        }
        iterations.append(outcome)
        if gate["status"] == "supported":
            stop_reason = "evidence_supported"
            if task_policy_enabled:
                task_completion_status = ("completed_with_gaps"
                                          if task_objective.get("scope_omission_count", 0) else "completed")
                reason = "Evidence Gate 已确认当前根因。"
                last_runtime_outcome = {**(last_runtime_outcome or {}),
                                        "finding_level": "root_cause_confirmed",
                                        "root_cause_confirmed": True,
                                        "should_stop": True,
                                        "stop_reason": "evidence_supported"}
            break
        if task_policy_enabled and last_runtime_outcome and last_runtime_outcome.get("should_stop"):
            stop_reason = last_runtime_outcome.get("stop_reason") or stop_reason or "no_further_useful_checks"
            outcome["policy_outcome"] = "runtime_finished"
            break
        if decision_protocol == "minimal_v2" and last_runtime_outcome and last_runtime_outcome.get("should_stop"):
            stop_reason = last_runtime_outcome.get("stop_reason") or "no_new_actions"
            reason = {
                "root_cause_confirmed": "Evidence Gate 已确认当前根因。",
                "direct_fault_cause_unknown": "已确认直接故障现象，但触发原因仍缺少证据。",
                "no_fault_observed_in_scope": "当前已授权检查范围内未发现已证实故障。",
                "evidence_insufficient": "当前授权范围内没有新的有效检查，证据不足以确认根因。",
            }.get(last_runtime_outcome.get("finding_level"), "Runtime 已结束本轮调查。")
            outcome["policy_outcome"] = "runtime_finished"
            break
        if consecutive_relation_error_rounds >= 2:
            reason, stop_reason = "连续两轮模型证据关系校验失败，已停止后续调用", "repeated_local_model_error"
            outcome["policy_outcome"] = "repeated_local_model_error"
            break
        if adaptive and planner.requested_scope(report):
            reason, stop_reason = "关键服务尚未授权，已记录范围扩展申请", "permission_blocked"
            outcome["policy_outcome"] = "scope_request"
            break
        if iteration == settings.investigation_model_rounds - 1:
            reason, stop_reason = "调查轮数上限已达到", "round_limit"
            if task_policy_enabled:
                task_completion_status = "stopped_with_limit"
            break
        if not action:
            reason = state.get("provider_error") or (
                "模型提出的检查不在授权范围"
                if proposal
                else "没有可继续区分假设的授权检查或模型尚未配置"
            )
            outcome["policy_outcome"] = "rejected" if proposal else "no_action"
            stop_reason = "action_rejected" if proposal else "permission_blocked" if adaptive and planner.scope_requests else "no_new_actions"
            if task_policy_enabled:
                task_completion_status = "stopped_with_gap"
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
        if task_policy_enabled and investigation_plan is not None:
            investigation_plan = sync_plan_with_history(investigation_plan, history)
            runtime_state_for_progress = {"incident": {"window_start": incident["window_start"]},
                                          "graph": graph, "evidence": evidence}
            current_facts = facts_from_state(runtime_state_for_progress)
            current_claims, _ = claims_from_hypotheses(planner.hypotheses, current_facts)
            required_remaining = sum(
                1 for check in investigation_plan["checks"]
                if check.get("required") and check.get("status") == "pending"
            )
            progress_result = evaluate_progress(
                evidence, progress_state, [], current_claims, history,
                planner.hypothesis_history, iteration + 1, required_remaining,
            )
            progress_summary, progress_state = progress_result["progress"], progress_result["progress_state"]
            outcome["progress"] = progress_summary
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
        if task_policy_enabled and task_objective:
            investigation_plan = sync_plan_with_history(investigation_plan or {"version": "task-plan-v1", "checks": []}, history)
            if supported_id:
                task_completion_status = ("completed_with_gaps"
                                          if task_objective.get("scope_omission_count", 0) else "completed")
                stop_reason = "evidence_supported"
            elif model_error:
                task_completion_status = "stopped_with_error"
            elif task_completion_status == "in_progress":
                required_checks = [check for check in investigation_plan["checks"] if check.get("required")]
                if (task_objective["task_type"] in {"health_check", "focused_diagnosis"}
                        and required_checks and all(check.get("status") != "pending" for check in required_checks)):
                    task_completion_status = ("completed_with_gaps" if any(
                        check.get("status") == "unavailable" for check in required_checks
                    ) else "completed")
                elif stop_reason and any(part in stop_reason for part in ("budget", "limit", "round")):
                    task_completion_status = "stopped_with_limit"
                else:
                    task_completion_status = "stopped_with_gap"
            completed_checks = [check for check in investigation_plan["checks"] if check.get("status") == "completed"]
            unavailable_checks = [check for check in investigation_plan["checks"] if check.get("status") == "unavailable"]
            remaining_checks = [check for check in investigation_plan["checks"] if check.get("status") == "pending"]
            if not reason and task_completion_status != "completed":
                reason = "调查已停止；未确认的原因或不可用检查作为证据缺口保留。"
            runtime_finish_override_audits.extend(runtime_decision_audits)
        relation_diagnostics = [error for item in iterations
                                for error in item.get("gate", {}).get("decision_audit", {}).get("validation_errors", [])]
        rejected_relation_count = sum(item.get("gate", {}).get("decision_audit", {}).get("rejected_relation_count", 0)
                                      for item in iterations)
        summary = {
            "stage": "stopped" if model_error else "completed",
            "model_status": "failed" if model_error else "offline" if report.get("analysis_status") == "offline" else "completed" if iterations else "not_called",
            "decision_protocol": decision_protocol,
            "model_error": model_error,
            "decision_audit": {
                "rejected_relation_count": rejected_relation_count,
                "consecutive_error_rounds": consecutive_relation_error_rounds,
                "validation_errors": relation_diagnostics[:24],
            },
            "analysis_status": "model_failed" if model_error else report.get("analysis_status"),
            "business_id": business_id,
            "profile_version": profile["profile_version"],
            "summary": report.get("summary"),
            "next_action": report.get("next_action"),
            "gate": gate,
            "runtime_outcome": last_runtime_outcome or classify_outcome(
                {"incident": {"window_start": incident["window_start"]}, "graph": graph,
                 "evidence": evidence, "scope_node_ids": (task_objective.get("target_node_ids", [])
                                                             if task_policy_enabled else
                                                             [service_nodes[b["service"]]["id"] for b in planned
                                                              if b["service"] in service_nodes]),
                 "scope_truncated": bool(task_policy_enabled and task_objective.get("scope_omission_count", 0)),
                 "available_actions": []}, gate,
            ) if decision_protocol == "minimal_v2" else None,
            "runtime_facts": last_runtime_facts,
            "claim_assessments": last_runtime_claims,
            "runtime_decision_audit": runtime_decision_audits[:16],
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
        if task_policy_enabled and task_objective:
            summary.update({
                "task_objective": task_objective,
                "task_policy_version": task_stop_policy_snapshot["version"],
                "task_completion_status": task_completion_status,
                "stop_reason": "model_budget_exhausted" if stop_reason == "model_budget_exceeded" else stop_reason or "no_further_useful_checks",
                "investigation_plan": investigation_plan,
                "completed_checks": completed_checks,
                "unavailable_checks": unavailable_checks,
                "remaining_checks": remaining_checks,
                "progress_summary": progress_summary,
                "progress_state": progress_state,
                "soft_limit_final_step_used": soft_limit_final_step_used,
                "task_action_history": [{key: entry.get(key) for key in
                                         ("action_id", "tool_id", "target_node_id", "service", "call_id", "status", "new_information")}
                                        for entry in history],
                "runtime_decision_audit": runtime_finish_override_audits[:16],
            })
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
