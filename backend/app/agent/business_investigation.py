from __future__ import annotations

import json
import re

from app.agent.workflow import build_workflow
from app.business.service import BusinessService
from app.common import dumps, now, uid
from app.context.budget import ContextBudgetManager
from app.system.scope import binding_is_current
from app.tools.business import BusinessToolRegistry


def investigate_business(database, settings, incident_id, business_id, actor, job_id=None):
    from app.api.v1 import _model_configuration, emit, pattern_matches

    profile = BusinessService(database, settings).get(business_id)
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
                "SELECT * FROM service_bindings WHERE business_id=? AND status='confirmed'",
                (business_id,),
            )
        ]
    finally:
        conn.close()
    bindings = [b for b in bindings if binding_is_current(b)]
    run_id, stamp = uid(), now()
    symptom = json.loads(incident["symptom_json"])
    with database.transaction() as tx:
        tx.execute(
            "INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status,summary_json) VALUES(?,?,?,?,?,?,?,'running',?)",
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
    budget_id = manager.create("investigation", business_id, run_id)
    registry = BusinessToolRegistry(database, settings, business_id, run_id, sid, budget_id)
    graph = {"snapshot_id": sid, "nodes": nodes, "edges": edges, "open_conflicts": conflicts}
    service_nodes = {
        n["name"]: n for n in nodes if n["kind"] == "service" and n["role"] != "runtime"
    }
    evidence, code_context, history, iterations, matches = [], [], [], [], []
    reason = None
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
    if suggested:
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
        }

    def execute(action):
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
        history.append(
            {
                "tool_id": action["tool_id"],
                "target_node_id": action["target_node_id"],
                "call_id": result["call_id"],
                "status": "succeeded" if result.get("evidence_id") else "failed",
            }
        )
        if result.get("evidence_id"):
            if action["tool_id"].startswith("code."):
                code_context.append(result["payload"])
            else:
                evidence.append(result)
        return result

    if not reason:
        for binding in planned[: max(1, settings.investigation_tool_calls // 2)]:
            execute(action_for(binding, "status"))
            execute(action_for(binding, "logs"))
    workflow = build_workflow(settings, database, budget_id)
    report = {"summary": reason or "正在收集可核验的证据", "next_action": "", "hypotheses": []}
    gate = {"status": "unresolved", "hypotheses": [], "rationale": reason or "证据不足"}
    for iteration in range(settings.investigation_model_rounds if not reason else 0):
        used = {(h["tool_id"], h["target_node_id"]) for h in history}
        actions = [
            action_for(b, suffix)
            for b in planned
            for suffix in ("status", "logs")
            if (f"{b['kind']}.{suffix}", service_nodes[b["service"]]["id"]) not in used
        ]
        if root:
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
        if manager.get(budget_id)["tool_calls"] >= settings.investigation_tool_calls:
            actions = []
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
                    {k: a[k] for k in ("tool_id", "target_node_id", "service", "role")}
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
            }
        )
        report, gate = state["report"], state["gate"]
        proposal = report.get("action")
        action = next(
            (
                a
                for a in actions
                if isinstance(proposal, dict)
                and a["tool_id"] == proposal.get("tool_id")
                and a["target_node_id"] == proposal.get("target_node_id")
            ),
            None,
        )
        outcome = {
            "number": iteration + 1,
            "gate": gate,
            "proposed_action": proposal,
            "executed_action": None,
            "policy_outcome": None,
        }
        iterations.append(outcome)
        if gate["status"] == "supported":
            break
        if iteration == settings.investigation_model_rounds - 1:
            reason = "调查轮数上限已达到"
            break
        if not action:
            reason = state.get("provider_error") or (
                "模型提出的检查不在授权范围"
                if proposal
                else "没有可继续区分假设的授权检查或模型尚未配置"
            )
            outcome["policy_outcome"] = "rejected" if proposal else "no_action"
            break
        result = execute(action)
        outcome.update(
            executed_action={
                k: action[k] for k in ("tool_id", "target_node_id", "service", "role")
            },
            policy_outcome="allowed",
            tool_call_id=result["call_id"],
            evidence_id=result.get("evidence_id"),
        )
    supported_id = None
    with database.transaction() as tx:
        for i, item in enumerate(iterations, 1):
            step_id = uid()
            primary_id = None
            primary_score = 0.0
            for hypothesis in item["gate"].get("hypotheses", []):
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
            for hypothesis in item["gate"].get("hypotheses", [])[:1]:
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
            "stage": "completed",
            "business_id": business_id,
            "profile_version": profile["profile_version"],
            "summary": report.get("summary"),
            "next_action": report.get("next_action"),
            "gate": gate,
            "unresolved_reason": reason if not supported_id else None,
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
        ended = now()
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
    return {
        "id": run_id,
        "run_id": run_id,
        "status": status,
        "unresolved_reason": reason if not supported_id else None,
    }
