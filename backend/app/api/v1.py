from __future__ import annotations

import hashlib
import json
import re
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.agent.workflow import build_workflow
from app.api.dependencies import current_actor
from app.domain import (
    ClarificationAnswer,
    ConflictReviewRequest,
    EvaluationCaseCreate,
    EvaluationPairCreate,
    EvaluationTrialCreate,
    IncidentCreate,
    InvestigationRunRequest,
    OnboardingRequest,
    PatternMatchOutcome,
    RepairRecord,
    ReviewRequest,
)
from app.evaluation import assess_truth, compare_trials, run_metrics
from app.knowledge import (
    compatibility,
    counterexamples,
    feedback_health,
    graph_contract,
    successful_checks,
)
from app.run_outcomes import (
    native_verification_valid,
    outcome,
    supported_checks,
    verification_node_ids,
)
from app.settings import get_settings
from app.storage.database import Database
from app.system.inventory import SystemOnboarding
from app.tools.registry import ToolRegistry

router = APIRouter(prefix="/api/v1")


def db() -> Database:
    return Database(get_settings().database_path)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def decode(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {} if fallback is None else fallback


def _run_fixture_digest(run: Any) -> str:
    coverage = decode(run["fixture_coverage_json"])
    return hashlib.sha256(
        f"{run['fixture_repo_revision']}:{run['fixture_deployment_digest']}:{coverage.get('source_digest', '')}".encode()
    ).hexdigest()


def _normalized_run_symptom(run: Any) -> str:
    symptom = decode(run["symptom_json"]).get("text", "")
    return " ".join(str(symptom).split()).casefold()


def _normalized_model_configuration(run: Any) -> dict[str, Any]:
    configuration = decode(run["model_config_json"])
    if not isinstance(configuration, dict):
        return {}
    snapshot = configuration.get("task_stop_policy")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("objective"), dict):
        snapshot = {**snapshot, "objective": {key: value for key, value in snapshot["objective"].items()
                                                if key != "task_id"}}
        configuration = {**configuration, "task_stop_policy": snapshot}
    return configuration


def _run_pair_mismatch(left: Any, right: Any) -> str | None:
    if dict(left).get('target_id')!=dict(right).get('target_id'):
        return 'Paired runs must use the same frozen SSH target'
    if left["graph_snapshot_id"] != right["graph_snapshot_id"]:
        return "Paired runs must use the same graph snapshot"
    if _normalized_run_symptom(left) != _normalized_run_symptom(right):
        return "Paired runs must use the same symptom"
    if _normalized_model_configuration(left) != _normalized_model_configuration(right):
        return "Paired runs must use the same model and prompt configuration"
    return None


def _evaluation_run_query() -> str:
    return """SELECT r.*,i.system_id,i.symptom_json,
      g.repo_revision AS fixture_repo_revision,g.deployment_digest AS fixture_deployment_digest,
      g.coverage_json AS fixture_coverage_json
      FROM diagnosis_runs r JOIN incidents i ON i.id=r.incident_id
      JOIN graph_snapshots g ON g.id=r.graph_snapshot_id WHERE r.id=?"""


def _assess_run_truth(connection,case,run):
    try:
        return assess_truth(connection,case,run)
    except ValueError as error:
        raise HTTPException(409,str(error)) from error


def _model_configuration(settings: Any, decision_protocol: str | None = None) -> dict[str, Any]:
    configured = bool(settings.model_name and settings.model_api_key.get_secret_value())
    endpoint = settings.model_base_url.strip() if configured else ""
    protocol = decision_protocol or settings.agent_decision_protocol
    return {
        "provider": settings.model_provider,
        "model": settings.model_name if configured else "offline",
        "endpoint_digest": hashlib.sha256(endpoint.encode()).hexdigest() if endpoint else None,
        "prompt_version": "reasoner-runtime-v2.3" if protocol == "minimal_v2" else "planner-review-v2.2",
        "decision_protocol": protocol,
        "timeout_seconds": settings.model_timeout_seconds if configured else None,
    }


def emit(tx: Any, system_id: str, run_id: str, entity_type: str, entity_id: str,
         event_type: str, payload: dict[str, Any], actor: str) -> None:
    seq = tx.execute("SELECT COALESCE(MAX(sequence_no),0)+1 AS n FROM events WHERE run_id=?", (run_id,)).fetchone()["n"]
    tx.execute("INSERT INTO events(id,system_id,run_id,sequence_no,entity_type,entity_id,event_type,actor,payload_redacted_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (str(uuid.uuid4()), system_id, run_id, seq, entity_type, entity_id, event_type, actor,
                json.dumps(payload, ensure_ascii=False, sort_keys=True), utcnow()))


@router.get("/systems")
def list_systems() -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        rows = connection.execute("""SELECT s.*,g.id AS snapshot_id,g.version,g.coverage_json,g.status AS snapshot_status
          FROM systems s LEFT JOIN graph_snapshots g ON g.system_id=s.id
          AND g.version=(SELECT MAX(g2.version) FROM graph_snapshots g2 WHERE g2.system_id=s.id)
          ORDER BY s.created_at DESC""").fetchall()
        return [{**dict(row), "coverage": decode(row["coverage_json"]), "coverage_json": None} for row in rows]
    finally:
        connection.close()


@router.post("/systems/onboard")
def onboard(request: OnboardingRequest, actor: str = Depends(current_actor)) -> dict[str, Any]:
    settings = get_settings()
    try:
        return SystemOnboarding(db(), settings).create_snapshot(request.source_key, request.display_name, request.compose_file)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/systems/{system_id}/graph")
def graph_view(system_id: str) -> dict[str, Any]:
    connection = db().connect()
    try:
        system = connection.execute("SELECT * FROM systems WHERE id=?", (system_id,)).fetchone()
        if not system:
            raise HTTPException(status_code=404, detail="System not found")
        snapshot = connection.execute("SELECT * FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1", (system_id,)).fetchone()
        if not snapshot:
            raise HTTPException(status_code=404, detail="System graph not found")
        sid = snapshot["id"]
        nodes = [dict(row) for row in connection.execute("SELECT * FROM graph_nodes WHERE snapshot_id=? ORDER BY kind,name", (sid,))]
        edges = [dict(row) for row in connection.execute("SELECT * FROM graph_edges WHERE snapshot_id=? ORDER BY relation", (sid,))]
        conflicts = [dict(row) for row in connection.execute("SELECT * FROM graph_conflicts WHERE snapshot_id=? ORDER BY severity", (sid,))]
        claims = [dict(row) for row in connection.execute("SELECT * FROM fact_claims WHERE snapshot_id=? ORDER BY source_locator", (sid,))]
        for row in nodes:
            row["attributes"] = decode(row.pop("attributes_json"))
            binding_id = row['attributes'].get('binding_id')
            if binding_id:
                from app.system.scope import binding_is_current
                binding = connection.execute('SELECT * FROM service_bindings WHERE id=?', (binding_id,)).fetchone()
                current = bool(binding and binding_is_current(binding))
                row['attributes']['runtime_current'] = current
                if not current:
                    row['attributes']['freshness_status'] = 'stale'
        for row in edges:
            row["attributes"] = decode(row.pop("attributes_json"))
        for row in conflicts:
            row["claim_ids"] = decode(row.pop("claim_ids_json"), [])
        for row in claims:
            row["value"] = decode(row.pop("value_json"))
            row["source_type"] = row["value"].get("provenance", {}).get("source_type", row["truth_type"]) if isinstance(row["value"], dict) else row["truth_type"]
        return {"system": dict(system), "snapshot": {**dict(snapshot), "coverage": decode(snapshot["coverage_json"]), "coverage_json": None},
                "nodes": nodes, "edges": edges, "claims": claims, "conflicts": conflicts}
    finally:
        connection.close()


@router.get("/incidents")
def list_incidents() -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        rows = connection.execute("""SELECT i.*,s.name AS system_name,(SELECT COUNT(*) FROM diagnosis_runs r WHERE r.incident_id=i.id) AS run_count,
          (SELECT id FROM diagnosis_runs r WHERE r.incident_id=i.id ORDER BY started_at DESC LIMIT 1) AS latest_run_id
          FROM incidents i JOIN systems s ON s.id=i.system_id ORDER BY i.created_at DESC""").fetchall()
        return [{**dict(row), "symptom": decode(row["symptom_json"]), "symptom_json": None} for row in rows]
    finally:
        connection.close()


@router.post("/incidents")
def create_incident(request: IncidentCreate, actor: str = Depends(current_actor)) -> dict[str, Any]:
    connection = db().connect()
    try:
        system = connection.execute("SELECT id FROM systems WHERE id=?", (request.system_id,)).fetchone()
        if not system:
            raise HTTPException(status_code=404, detail="System not found")
        if request.mode == "graph_adaptive" and not connection.execute(
            "SELECT 1 FROM business_profiles WHERE system_id=?", (request.system_id,)
        ).fetchone():
            raise HTTPException(409, "Adaptive mode requires the reviewed business profile and runtime scope entry")
        incident_id, timestamp = str(uuid.uuid4()), utcnow()
        from app.agent.ab_investigation import default_window
        business_entry = connection.execute('SELECT 1 FROM business_profiles WHERE system_id=?', (request.system_id,)).fetchone()
        with db().transaction() as tx:
            tx.execute("INSERT INTO incidents(id,system_id,symptom_json,window_start,window_end,status,created_at) VALUES(?,?,?,?,?,?,?)",
                       (incident_id, request.system_id, json.dumps({"text": request.symptom, "mode": request.mode,
                                                                    "task_type": request.task_type,
                                                                    **({'task_type_explicit': 'task_type' in request.model_fields_set} if business_entry else {})}),
                        request.window_start or (default_window() if business_entry else timestamp), None, "open", timestamp))
        return {"id": incident_id, "system_id": request.system_id, "symptom": request.symptom,
                "mode": request.mode, "task_type": request.task_type,
                "created_at": timestamp, "status": "open"}
    finally:
        connection.close()


def pattern_matches(connection: Any, run_id: str, snapshot_id: str, symptom: str,
                    roles: set[str], topology: set[str], current_signals: set[tuple[str, str]],
                    has_conflicts: bool, role_node_ids: dict[str, list[str]],business_id=None) -> list[dict[str, Any]]:
    if business_id:
        from app.knowledge import owned_patterns
        patterns=owned_patterns(connection,business_id,snapshot_id)
    else:
        patterns = connection.execute('''SELECT p.* FROM pattern_versions p JOIN experiences e
            ON e.id=p.source_experience_id JOIN graph_snapshots g ON g.id=e.graph_snapshot_id
            WHERE p.status='trusted' AND g.system_id=(SELECT system_id FROM graph_snapshots WHERE id=?)
            ORDER BY p.pattern_key,p.version DESC''',(snapshot_id,)).fetchall()
    scored = []
    symptom_words = {w for w in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", symptom.casefold()) if len(w) > 1}
    for pattern in patterns:
        p_roles = set(decode(pattern["roles_json"], []))
        p_topology = set(decode(pattern["topology_json"], []))
        p_symptoms = set(decode(pattern["symptoms_json"], []))
        if not p_roles or not p_roles.issubset(roles):
            continue
        symptom_score = len(symptom_words & p_symptoms) / max(len(symptom_words | p_symptoms), 1)
        topology_score = len(p_topology & topology) / max(len(p_topology | topology), 1) if p_topology else 1.0
        # This component ranks a checklist only. Historical Pattern data is never added as current evidence.
        checks = [check for check in decode(pattern["checks_json"], [])
                  if isinstance(check, dict) and check.get("purpose") == "diagnosis"
                  and check.get("tool_id") in {"container.logs", "service.logs"}
                  and (not check.get("target_role") or check.get("target_role") in p_roles)]
        pattern_signals = {
            (check["tool_id"], role)
            for check in checks
            for role in ([check["target_role"]] if check.get("target_role") else p_roles)
        }
        available_pattern_signals = pattern_signals & current_signals
        signal_score = len(available_pattern_signals) / max(len(pattern_signals), 1)
        signals_available = bool(pattern_signals) and pattern_signals.issubset(current_signals)
        score = .45 * symptom_score + .35 * topology_score + .20 * signal_score
        compatible = compatibility(connection, pattern, snapshot_id)
        if not compatible["compatible"]:
            emit(connection, connection.execute("SELECT system_id FROM graph_snapshots WHERE id=?", (snapshot_id,)).fetchone()[0], run_id,
                 "pattern", pattern["id"], "pattern_reuse_blocked", compatible, "deterministic_knowledge_policy")
        scored.append({"pattern": pattern, "score": round(score, 4), "binding": {"roles": sorted(p_roles), "snapshot_id": snapshot_id, "compatibility": compatible}, "compatibility": compatible,
                       "checks": checks, "signals_available": signals_available, "topology_available": p_topology.issubset(topology)})
    scored.sort(key=lambda x: (not x["compatibility"]["compatible"], -x["score"]))
    compatible_items = [x for x in scored if x["compatibility"]["compatible"]]
    margin = compatible_items[0]["score"] - compatible_items[1]["score"] if len(compatible_items) > 1 else (compatible_items[0]["score"] if compatible_items else 0)
    for index, item in enumerate(scored):
        pattern = item["pattern"]
        checks = item["checks"]
        p_roles = set(item["binding"]["roles"])
        gate = (item["compatibility"]["compatible"] and item["score"] >= .70 and margin >= .15 and not has_conflicts
                and bool(checks) and item["signals_available"] and item["topology_available"])
        decision = "suggested" if gate and index == 0 else "not_selected"
        match_id = str(uuid.uuid4())
        connection.execute("INSERT INTO pattern_matches(id,run_id,pattern_version_id,score,rank,binding_json,gate_result,decision,validation_outcome,usefulness_outcome) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (match_id, run_id, pattern["id"], item["score"], index+1, json.dumps(item["binding"]),
                            "passed" if gate else "failed", decision, "pending", "pending"))
        item["match_id"] = match_id
        item["pattern"] = {"id": pattern["id"], "pattern_key": pattern["pattern_key"], "version": pattern["version"],
                           "roles": sorted(p_roles), "checks": checks,
                           "source_experience_id": pattern["source_experience_id"]}
        item["binding"]["role_node_ids"] = {role: role_node_ids.get(role, []) for role in sorted(p_roles)}
        connection.execute("UPDATE pattern_matches SET binding_json=? WHERE id=?", (json.dumps(item["binding"]), match_id))
        item["gate_result"] = "passed" if gate else "failed"
        item["decision"] = decision
        item["validation_outcome"] = "pending"
        item["usefulness_outcome"] = "pending"
    return scored


def execute_legacy_run(incident_id: str, actor: str, job_id: str | None = None) -> dict[str, Any]:
    database, settings = db(), get_settings()
    connection = database.connect()
    try:
        incident = connection.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        system = connection.execute("SELECT * FROM systems WHERE id=?", (incident["system_id"],)).fetchone()
        snapshot = connection.execute("SELECT * FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1", (system["id"],)).fetchone()
        if not snapshot:
            raise HTTPException(status_code=409, detail="Onboard this system before investigating")
        sid, run_id, started = snapshot["id"], str(uuid.uuid4()), utcnow()
        mode = decode(incident["symptom_json"]).get("mode", "normal")
        graph_rows = [dict(r) for r in connection.execute("SELECT * FROM graph_nodes WHERE snapshot_id=?", (sid,))]
        for n in graph_rows:
            n["attributes"] = decode(n.pop("attributes_json"))
        open_conflicts = [dict(r) for r in connection.execute("SELECT * FROM graph_conflicts WHERE snapshot_id=? AND status='open'", (sid,))]
        graph_edges = [dict(r) for r in connection.execute("SELECT * FROM graph_edges WHERE snapshot_id=?", (sid,))]
        topology = set()
        role_by_id = {n["id"]: n["role"] for n in graph_rows}
        for edge in graph_edges:
            pair = f"{role_by_id.get(edge['from_node_id'],'unknown')}->{role_by_id.get(edge['to_node_id'],'unknown')}:{edge['relation']}"
            topology.add(pair)
        roles = {n["role"] for n in graph_rows if n["kind"] == "service"}
        role_node_ids: dict[str, list[str]] = {}
        for node in graph_rows:
            if node["kind"] == "service":
                role_node_ids.setdefault(node["role"], []).append(node["id"])
        with database.transaction() as tx:
            tx.execute("INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status,summary_json) VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, incident_id, sid, mode, json.dumps(_model_configuration(settings, "legacy_v22"), sort_keys=True),
                        json.dumps({"tool_attempts_max": 15, "wall_seconds_max": 600}), started, "running", "{}"))
            emit(tx, system["id"], run_id, "run", run_id, "run_started", {"graph_snapshot_id": sid, "mode": mode}, actor)
        connection.commit()
    finally:
        connection.close()

    evidence: list[dict[str, Any]] = []
    from app.context.budget import ContextBudgetManager
    budget_id = ContextBudgetManager(database, settings).create("investigation", run_id=run_id)
    if job_id:
        with database.transaction() as tx:
            tx.execute("UPDATE jobs SET result_json=? WHERE id=?", (json.dumps({"run_id": run_id}), job_id))
    tool_registry = ToolRegistry(database, settings)
    status_result = tool_registry.execute(run_id, sid, system["compose_project"], "compose.status", {})
    if status_result.get("evidence_id"):
        evidence.append({"evidence_id": status_result["evidence_id"], "tool_id": status_result["tool_id"],
                         "call_id": status_result["call_id"], "target_node_id": None,
                         "observed_at": status_result["observed_at"],
                         "payload": status_result["payload"]})
    by_service = {n["name"]: n for n in graph_rows if n["kind"] == "service"}
    containers = status_result.get("payload", {}).get("containers", [])
    available_signals = {
        ("container.logs", node["role"])
        for container in containers
        if (node := by_service.get(container.get("service"))) is not None
    }
    run_matches: list[dict[str, Any]] = []
    if mode == "graph_pattern" and status_result.get("evidence_id"):
        with database.transaction() as tx:
            run_matches = pattern_matches(
                tx, run_id, sid, decode(incident["symptom_json"]).get("text", ""),
                roles, topology, available_signals, bool(open_conflicts), role_node_ids)
            for match in run_matches:
                emit(tx, system["id"], run_id, "pattern_match", match["match_id"], "pattern_retrieved",
                     {"pattern_version_id": match["pattern"]["id"], "decision": match["decision"],
                      "score": match["score"], "binding": match["binding"]}, actor)
    if containers:
        with database.transaction() as tx:
            for container in containers:
                node = by_service.get(container.get("service"))
                if not node:
                    continue
                tx.execute("INSERT INTO fact_claims(id,snapshot_id,subject_node_id,property,scope,value_json,truth_type,source_locator,source_revision,observed_at,extractor_version,confidence_class) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), sid, node["id"], "runtime_state", f"container:{container['service']}",
                            json.dumps(container.get("state", "unknown")), "runtime",
                            f"docker://{system['compose_project']}/{container.get('id','')[:12]}", system["repo_revision"],
                            status_result.get("observed_at", utcnow()), "collector-v1", "observed"))
    suggested_match = next((match for match in run_matches if match["decision"] == "suggested"), None)
    planned_containers = []
    if suggested_match:
        service_containers = {container.get("service"): container for container in containers}
        for check in suggested_match["pattern"].get("checks", []):
            target_role = check.get("target_role")
            if target_role:
                target_roles = {target_role}
            else:
                target_roles = set(suggested_match["pattern"].get("roles", []))
            for node in graph_rows:
                if node["kind"] != "service" or node["role"] not in target_roles:
                    continue
                container = service_containers.get(node["name"])
                if container and container not in planned_containers:
                    planned_containers.append(container)
    if not planned_containers:
        planned_containers = containers[:8]
    executed_pattern_checks = 0
    for container in planned_containers[:8]:
        service_node = by_service.get(container.get("service"))
        result = tool_registry.execute(run_id, sid, system["compose_project"], "container.logs",
                                       {"container_id": container["id"], "tail": 100}, service_node["id"] if service_node else None)
        if result.get("evidence_id"):
            if suggested_match and service_node and service_node["role"] in set(suggested_match["pattern"].get("roles", [])):
                executed_pattern_checks += 1
            evidence.append({"evidence_id": result["evidence_id"], "tool_id": result["tool_id"],
                             "call_id": result["call_id"],
                             "target_node_id": service_node["id"] if service_node else None,
                             "observed_at": result["observed_at"],
                             "payload": result["payload"]})
    if suggested_match:
        suggested_match["executed_check_count"] = executed_pattern_checks
        with database.transaction() as tx:
            emit(tx, system["id"], run_id, "pattern_match", suggested_match["match_id"], "pattern_checks_executed",
                 {"executed_check_count": executed_pattern_checks}, actor)
    graph_for_agent = {"snapshot_id": sid, "nodes": graph_rows, "edges": graph_edges, "open_conflicts": open_conflicts}
    runtime_containers: dict[str, dict[str, Any]] = {}
    for container in containers:
        node = by_service.get(container.get("service"))
        if node and container.get("id"):
            runtime_containers.setdefault(node["id"], container)
    ordered_service_nodes = sorted(
        (node for node in graph_rows if node["kind"] == "service" and node["id"] in runtime_containers),
        key=lambda node: (node["role"] != "application", node["role"], node["name"]))

    def available_diagnostic_actions() -> list[dict[str, str]]:
        used = {(item.get("tool_id"), item.get("target_node_id")) for item in evidence + action_history}
        actions = []
        for tool_id in ("container.logs", "container.processes"):
            for node in ordered_service_nodes:
                if (tool_id, node["id"]) in used:
                    continue
                actions.append({"tool_id": tool_id, "target_node_id": node["id"],
                                "role": node["role"], "service": node["name"]})
        return actions[:32]

    workflow = build_workflow(settings, database, budget_id, "legacy_v22")
    action_history: list[dict[str, Any]] = []
    iterations: list[dict[str, Any]] = []
    iteration_no = -1
    while True:
        iteration_no += 1
        available_actions = available_diagnostic_actions()
        result_state = workflow.invoke({
            "incident": {"symptom": decode(incident["symptom_json"]).get("text", ""),
                         "window_start": incident["window_start"]},
            "graph": graph_for_agent, "evidence": list(evidence),
            "available_actions": available_actions, "action_history": list(action_history),
        })
        report = result_state.get("report", {})
        gate = result_state.get("gate", {"status": "unresolved", "hypotheses": []})
        iteration = {"number": iteration_no + 1, "report": report, "gate": gate,
                     "proposed_action": report.get("action"), "executed_action": None,
                     "tool_call_id": None, "evidence_id": None, "tool_status": None,
                     "policy_outcome": None}
        iterations.append(iteration)
        if result_state.get("model_error"):
            iteration["policy_outcome"] = "model_failed"
            break
        if gate.get("status") == "supported":
            break
        proposal = report.get("action")
        if not isinstance(proposal, dict):
            break
        tool_id = proposal.get("tool_id")
        target_node_id = proposal.get("target_node_id")
        allowed_action = next((candidate for candidate in available_actions
                               if candidate["tool_id"] == tool_id
                               and candidate["target_node_id"] == target_node_id), None)
        if not allowed_action:
            iteration["policy_outcome"] = "rejected"
            with database.transaction() as tx:
                emit(tx, system["id"], run_id, "diagnostic_step", str(iteration_no + 1),
                     "diagnostic_action_rejected", {"tool_id": str(tool_id)[:80],
                     "target_node_id": str(target_node_id)[:80], "reason": "not_in_current_allowlist"}, actor)
            break

        iteration["executed_action"] = allowed_action
        iteration["policy_outcome"] = "allowed"
        with database.transaction() as tx:
            emit(tx, system["id"], run_id, "diagnostic_step", str(iteration_no + 1),
                 "diagnostic_action_approved", {"tool_id": tool_id,
                 "target_node_id": target_node_id,
                 "rationale": str(proposal.get("rationale", ""))[:500]}, actor)
        container = runtime_containers[target_node_id]
        arguments = {"container_id": container["id"]}
        if tool_id == "container.logs":
            arguments["tail"] = 100
        action_result = tool_registry.execute(run_id, sid, system["compose_project"], tool_id,
                                              arguments, target_node_id)
        iteration["tool_call_id"] = action_result.get("call_id")
        iteration["tool_status"] = "succeeded" if action_result.get("evidence_id") else "failed"
        iteration["evidence_id"] = action_result.get("evidence_id")
        if action_result.get("evidence_id"):
            evidence.append({"evidence_id": action_result["evidence_id"],
                             "tool_id": action_result["tool_id"],
                             "call_id": action_result["call_id"],
                             "target_node_id": target_node_id,
                             "observed_at": action_result["observed_at"],
                             "payload": action_result["payload"]})
        action_history.append({"tool_id": tool_id, "target_node_id": target_node_id,
                               "call_id": action_result.get("call_id"),
                               "status": iteration["tool_status"],
                               "evidence_id": action_result.get("evidence_id")})
        with database.transaction() as tx:
            emit(tx, system["id"], run_id, "diagnostic_step", str(iteration_no + 1),
                 "diagnostic_action_executed", {"tool_id": tool_id,
                 "target_node_id": target_node_id, "call_id": action_result.get("call_id"),
                 "status": iteration["tool_status"], "evidence_id": action_result.get("evidence_id")}, actor)

    final_iteration = iterations[-1]
    report, gate = final_iteration["report"], final_iteration["gate"]
    iteration_summaries = [{"number": item["number"], "review_outcome": item["gate"].get("status", "unresolved"),
                            "proposed_action": item["proposed_action"],
                            "executed_action": item["executed_action"],
                            "tool_call_id": item["tool_call_id"], "evidence_id": item["evidence_id"],
                            "tool_status": item["tool_status"], "policy_outcome": item["policy_outcome"]}
                           for item in iterations]
    summary = {"summary": report.get("summary", ""), "next_action": report.get("next_action", ""),
               "gate": gate, "provider_error": result_state.get("provider_error"),
               "evidence_ids": [e["evidence_id"] for e in evidence], "iterations": iteration_summaries}
    summary["budget_id"] = budget_id
    model_error = result_state.get("model_error")
    summary.update(stage="stopped" if model_error else "completed", model_error=model_error,
                   model_status="failed" if model_error else "offline" if report.get("analysis_status") == "offline" else "completed",
                   analysis_status="model_failed" if model_error else report.get("analysis_status"),
                   unresolved_reason=model_error["message"] if model_error else None,
                   stop_reason=model_error["code"] if model_error else "evidence_supported" if gate.get("status") == "supported" else "no_new_actions")
    if mode == "graph_pattern":
        summary["pattern_matches"] = run_matches
    supported_id = None
    with database.transaction() as tx:
        previous_score = 0.0
        graph_node_ids = {node["id"] for node in graph_rows}
        for sequence_no, item in enumerate(iterations, start=1):
            hypotheses = item["gate"].get("hypotheses", [])[:3]
            persisted_hypotheses = []
            for hyp in hypotheses:
                hid = str(uuid.uuid4())
                target = hyp.get("target_node_id")
                if target not in graph_node_ids:
                    target = None
                score = float(hyp.get("confidence", .25))
                tx.execute("INSERT INTO hypotheses(id,run_id,target_node_id,failure_mode,causal_chain_json,falsifiable_prediction,status,score) VALUES(?,?,?,?,?,?,?,?)",
                           (hid, run_id, target, str(hyp.get("failure_mode", "candidate"))[:500],
                            json.dumps(hyp.get("causal_chain", [])[:12]), str(hyp.get("prediction", ""))[:500],
                            hyp.get("status", "candidate"), score))
                persisted_hypotheses.append((hid, hyp, score))
                for link in hyp.get("evidence_links", []):
                    tx.execute("INSERT OR IGNORE INTO hypothesis_evidence VALUES(?,?,?,?)", (hid, link["evidence_id"], "support" if link["relation"] == "supports" else "refute", link["explanation"]))
                if hyp.get("status") == "supported" and supported_id is None:
                    supported_id = hid

            primary = next((entry for entry in persisted_hypotheses if entry[1].get("status") == "supported"),
                           persisted_hypotheses[0] if persisted_hypotheses else None)
            step_id = str(uuid.uuid4())
            after_score = primary[2] if primary else previous_score
            action_record = {"proposed_action": item["proposed_action"],
                             "executed_action": item["executed_action"],
                             "policy_outcome": item["policy_outcome"],
                             "tool_call_id": item["tool_call_id"],
                             "tool_status": item["tool_status"],
                             "evidence_id": item["evidence_id"],
                             "evidence_ids": primary[1].get("evidence_ids", []) if primary else []}
            rationale = (primary[1].get("rationale", "") if primary else report.get("summary", ""))
            tx.execute("INSERT INTO diagnostic_steps(id,run_id,sequence_no,hypothesis_id,action_json,score_before,score_after,review_outcome,rationale,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (step_id, run_id, sequence_no, primary[0] if primary else None,
                        json.dumps(action_record, ensure_ascii=False), previous_score, after_score,
                        item["gate"].get("status", "unresolved"), str(rationale)[:1000], utcnow()))
            if primary:
                for link in primary[1].get("evidence_links", []):
                    evidence_id = link.get("evidence_id")
                    if evidence_id in summary["evidence_ids"] and link.get("relation") in {"supports", "refutes"}:
                        tx.execute("INSERT OR IGNORE INTO step_evidence(step_id,evidence_id,stance,excerpt_locator) VALUES(?,?,?,?)",
                                   (step_id, evidence_id, "support" if link["relation"] == "supports" else "refute",
                                    "model_reference"))
            emit(tx, system["id"], run_id, "diagnostic_step", step_id, "diagnostic_step_reviewed",
                 {"sequence_no": sequence_no, "review_outcome": item["gate"].get("status", "unresolved"),
                  "evidence_count": len(primary[1].get("evidence_ids", [])) if primary else 0}, actor)
            previous_score = after_score
        status = "supported" if supported_id else "unresolved"
        summary["counterexamples"] = counterexamples(summary, status)
        ended = utcnow()
        tx.execute("UPDATE diagnosis_runs SET status=?,supported_at=?,ended_at=?,final_hypothesis_id=?,summary_json=? WHERE id=?",
                   (status, ended if supported_id else None, ended, supported_id, json.dumps(summary, ensure_ascii=False), run_id))
        emit(tx, system["id"], run_id, "run", run_id, "evidence_collected", {"count": len(evidence)}, actor)
        emit(tx, system["id"], run_id, "run", run_id, f"run_{status}", {"summary": summary["summary"][:500]}, actor)
        if summary["counterexamples"]:
            emit(tx, system["id"], run_id, "run", run_id, "investigation_counterexamples_retained", {"counterexamples": summary["counterexamples"]}, actor)
    return {"id": run_id, "incident_id": incident_id, "status": "supported" if supported_id else "unresolved",
            "summary": summary["summary"], "next_action": summary["next_action"], "evidence_count": len(evidence),
            "pattern_matches": summary.get("pattern_matches", []), "provider_error": summary.get("provider_error")}


@router.post("/incidents/{incident_id}/runs", status_code=202)
def start_run(incident_id: str, request: InvestigationRunRequest | None = None,
              actor: str = Depends(current_actor)) -> dict[str, Any]:
    from app.agent.business_investigation import investigate_business
    from app.connectors.service import RuntimeService
    from app.jobs import reserve_investigation, submit
    database, settings = db(), get_settings()
    conn = database.connect()
    try:
        incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(404, "Incident not found")
        business = conn.execute("SELECT id FROM business_profiles WHERE system_id=?", (incident["system_id"],)).fetchone()
        target_id = None
        if business:
            runtime = RuntimeService(database, settings)
            rows = conn.execute("SELECT id,config_json FROM runtime_targets WHERE business_id=? ORDER BY created_at,id", (business["id"],)).fetchall()
            configs = {row["id"]: runtime.public({"id": row["id"], **decode(row["config_json"])}) for row in rows}
            readiness = {key: runtime.investigation_readiness(value) for key, value in configs.items()}
            requested_id = request.target_id if request else None
            eligible = [key for key, state in readiness.items() if state["eligible"]]
            if requested_id is None:
                from app.system.authorization import defaults
                configured_target = defaults(conn, business['id'])['target_id']
                if configured_target:
                    if configured_target not in configs or not readiness[configured_target]['eligible']:
                        raise HTTPException(409, '默认 SSH 目标不可用于调查，请检查其授权和会话凭据；不会自动回退')
                    target_id = configured_target
                elif len(rows) == 1 and len(eligible) == 1:
                    target_id = eligible[0]
                elif len(rows) > 1:
                    raise HTTPException(409, "当前业务配置了多个 SSH 目标，请明确选择目标；不可用目标不会自动回退")
                else:
                    reasons = [state["reason"] for state in readiness.values() if state.get("reason")]
                    detail = "；".join(dict.fromkeys(reasons))
                    raise HTTPException(409, "当前业务没有可用于调查的已授权 SSH 目标" + (f"：{detail}" if detail else ""))
            else:
                if requested_id not in configs:
                    raise HTTPException(404, "所选 SSH 目标不存在或不属于当前业务")
                if not readiness[requested_id]["eligible"]:
                    raise HTTPException(409, f"所选 SSH 目标不可用于调查：{readiness[requested_id]['reason']}")
                target_id = requested_id
            try:
                runtime.require_credentials(business["id"], target_id)
            except PermissionError as exc:
                raise HTTPException(409, "所选 SSH 目标的会话凭据不可用，请重新提供凭据") from exc
        try:
            job_id, status, created = reserve_investigation(database, incident_id, business["id"] if business else None, target_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    finally:
        conn.close()
    protocol = request.decision_protocol if request and request.decision_protocol else settings.agent_decision_protocol
    if business and protocol == 'ab_v1' and decode(incident['symptom_json']).get('mode', 'normal') in {'normal', 'graph_adaptive'}:
        from app.agent.ab_investigation import dispatch_ready, prepare_run
        run_id = None
        if created:
            try:
                run_id = prepare_run(database, settings, incident_id, business['id'], target_id, job_id, actor)
            except Exception as exc:
                with database.transaction() as tx:
                    tx.execute("UPDATE jobs SET status='failed',error=?,ended_at=? WHERE id=?", (type(exc).__name__, utcnow(), job_id))
                raise HTTPException(409, str(exc) if isinstance(exc, ValueError) else '无法创建调查，请检查业务范围和配置') from exc
            dispatch_ready(database, settings)
        else:
            with database.connect() as tx:
                run_id = decode(tx.execute('SELECT result_json FROM jobs WHERE id=?', (job_id,)).fetchone()[0]).get('run_id')
        return {'job_id': job_id, 'run_id': run_id, 'status': status, 'reused': not created, 'decision_protocol': 'ab_v1'}
    def execute():
        execution_settings = settings.model_copy(update={'agent_decision_protocol': protocol})
        return investigate_business(database, execution_settings, incident_id, business["id"], actor, job_id, target_id=target_id) if business else execute_legacy_run(incident_id, actor, job_id)
    if created:
        submit(database, job_id, execute)
        with database.transaction() as tx:
            status = tx.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
    return {"job_id": job_id, "status": status, "reused": not created}


@router.get("/incidents/{incident_id}/runs")
def list_incident_runs(incident_id: str) -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        if not connection.execute("SELECT 1 FROM incidents WHERE id=?", (incident_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Incident not found")
        return [dict(row) for row in connection.execute(
            "SELECT id,status,started_at,supported_at,ended_at,graph_snapshot_id,mode,target_id FROM diagnosis_runs WHERE incident_id=? ORDER BY started_at DESC",
            (incident_id,))]
    finally:
        connection.close()


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_business_run(run_id: str, request: dict[str, Any] | None = None, actor: str = Depends(current_actor)):
    from app.agent.business_investigation import investigate_business
    from app.agent.discovery import authorization_digest
    from app.business.service import BusinessService
    from app.connectors.service import RuntimeService
    from app.jobs import reserve_investigation, submit
    from app.system.scope import binding_is_current
    database, settings = db(), get_settings()
    with database.connect() as tx:
        coordinated = tx.execute('SELECT 1 FROM run_executions WHERE run_id=?', (run_id,)).fetchone()
    if coordinated:
        if request is None:
            raise HTTPException(409, '继续调查需要当前确认问题的编号和版本')
        from app.agent.ab_investigation import resume_run
        try:
            answer = ClarificationAnswer.model_validate(request)
            return resume_run(database, settings, run_id, answer, actor=actor)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(409, '确认问题的编号或版本无效' if type(exc).__name__ == 'ValidationError' else str(exc)) from exc
    with database.connect() as c:
        row = c.execute("SELECT * FROM diagnosis_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "调查不存在")
        summary = decode(row["summary_json"])
        if row["status"] != "awaiting_review" or summary.get("stage") != "awaiting_review":
            raise HTTPException(409, "调查未处于待审核状态")
        business_id, incident_id = summary["business_id"], row["incident_id"]
        target_id = row["target_id"]
        target_snapshot = decode(row["target_snapshot_json"])
        if not target_id or target_snapshot.get("id") != target_id:
            raise HTTPException(409, "历史调查未保存 SSH 目标来源，不能安全恢复；请新建调查并明确选择目标")
        profile = BusinessService(database, settings).get(business_id)
        if profile["profile_version"] != summary["profile_version"] or profile["review_status"] != "approved":
            raise HTTPException(409, "业务画像版本变化，需要重新审核并发起调查")
        runtime = RuntimeService(database, settings)
        try:
            current = runtime.get(business_id, target_id)
        except KeyError as exc:
            raise HTTPException(409, "原调查 SSH 目标已删除，不能恢复；请新建调查") from exc
        target_readiness = runtime.investigation_readiness(current)
        if not target_readiness["eligible"]:
            if target_readiness["status"] == "credential_missing":
                raise HTTPException(403, "SSH 会话凭据不可用，请重新提供密码")
            raise HTTPException(409, "原调查 SSH 目标已停用或撤销授权，不能恢复")
        if authorization_digest(current) != target_snapshot.get("authorization_digest"):
            raise HTTPException(409, "原调查目标授权或执行配置已变化，不能恢复")
        if summary.get("authorization_digests") and summary["authorization_digests"] != {target_id: authorization_digest(current)}:
            raise HTTPException(409, "原调查授权记录与 Run 目标不一致，不能恢复")
        try:
            runtime.require_credentials(business_id, target_id)
        except PermissionError as exc:
            raise HTTPException(403, "SSH 会话凭据不可用，请重新提供密码") from exc
        current_bindings = [dict(b) for b in c.execute("SELECT * FROM service_bindings WHERE business_id=?", (business_id,))
                            if binding_is_current(b) and b["kind"] in {"service", "container"} and b["target_id"] == target_id]
        if not current_bindings:
            raise HTTPException(409, "请先确认有效的服务或容器映射")
    job_id, status, created = reserve_investigation(database, incident_id, business_id, target_id)
    if created:
        submit(database, job_id, lambda: investigate_business(database, settings, incident_id, business_id, actor, job_id, resume_run_id=run_id, target_id=target_id))
    return {"job_id": job_id, "status": status, "reused": not created, "run_id": run_id}


@router.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        rows = connection.execute("""SELECT r.id,r.incident_id,r.status,r.mode,r.started_at,r.supported_at,r.ended_at,r.model_config_json,
          i.system_id,s.name AS system_name,i.symptom_json FROM diagnosis_runs r
          JOIN incidents i ON i.id=r.incident_id JOIN systems s ON s.id=i.system_id
          ORDER BY r.started_at DESC LIMIT 250""").fetchall()
        results = []
        for row in rows:
            measured = outcome(connection, row)
            item = {key: value for key, value in dict(row).items() if key != 'model_config_json'}
            item.update(symptom=decode(row['symptom_json']).get('text', ''), symptom_json=None)
            if measured['native']:
                item.update(execution_status=measured['execution_status'], diagnosis_status=measured['diagnosis_status'],
                            completion_satisfied=measured['result'].get('completion_satisfied', False))
            results.append(item)
        return results
    finally:
        connection.close()


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    connection = db().connect()
    try:
        row = connection.execute("SELECT r.*,i.system_id,i.symptom_json FROM diagnosis_runs r JOIN incidents i ON i.id=r.incident_id WHERE r.id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Run not found")
        result = dict(row)
        result["summary"] = decode(result.pop("summary_json"))
        measured_outcome=outcome(connection,row)
        result['repair_eligible']=measured_outcome['root_supported']
        execution = connection.execute("SELECT 1 FROM run_executions WHERE run_id=?", (run_id,)).fetchone()
        if execution:
            from app.runtime.coordinator import get_coordinator
            coordinated = get_coordinator(db()).public(run_id)
            result.update(execution_status=coordinated["execution_status"], diagnosis_status=coordinated["diagnosis_status"],
                          diagnosis_result=decode(coordinated["result_json"]), clarification=decode(coordinated["clarification_json"]),
                          completion_satisfied=coordinated["completion_satisfied"])
        target_snapshot = decode(result.pop("target_snapshot_json", None))
        if not isinstance(target_snapshot, dict):
            target_snapshot = {}
        result['verification_endpoints']=[]
        if measured_outcome['native'] and measured_outcome['root_supported']:
            from app.agent.discovery import authorization_digest
            from app.connectors.service import RuntimeService
            try:
                current=RuntimeService(db(),get_settings()).get(result['summary']['business_id'],row['target_id'])
                if authorization_digest(current)==target_snapshot.get('authorization_digest'):
                    verification_nodes=verification_node_ids(measured_outcome['result'])
                    names={node[0] for identifier in verification_nodes for node in connection.execute('SELECT name FROM graph_nodes WHERE id=? AND snapshot_id=?',(identifier,row['graph_snapshot_id']))}
                    result['verification_endpoints']=[endpoint for endpoint in current.get('health_endpoints',[]) if endpoint['service'] in names]
            except (KeyError,ValueError):
                pass
        target_snapshot.pop("authorization_digest", None)
        result["runtime_target"] = target_snapshot or None
        result["target_binding_status"] = "bound" if result.get("target_id") else "unknown"
        from app.context.budget import ContextBudgetManager
        budget_row = connection.execute("SELECT id FROM context_budgets WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        result["context_budget"] = ContextBudgetManager(db(), get_settings()).get(budget_row["id"]) if budget_row else None
        if result["summary"].get("pattern_matches"):
            persisted = {match["id"]: dict(match) for match in connection.execute(
                "SELECT id,validation_outcome,usefulness_outcome FROM pattern_matches WHERE run_id=?", (run_id,))}
            for match in result["summary"]["pattern_matches"]:
                match.update(persisted.get(match.get("match_id"), {}))
        result["symptom"] = decode(result.pop("symptom_json"))
        result["hypotheses"] = [{**dict(h), "evidence_links": [dict(e) for e in connection.execute("SELECT evidence_id,stance,explanation FROM hypothesis_evidence WHERE hypothesis_id=?", (h["id"],))]} for h in connection.execute("SELECT * FROM hypotheses WHERE run_id=? ORDER BY score DESC", (run_id,))]
        result["diagnostic_steps"] = []
        for step in connection.execute("SELECT * FROM diagnostic_steps WHERE run_id=? ORDER BY sequence_no", (run_id,)):
            item = dict(step)
            item["action"] = decode(item.pop("action_json"))
            result["diagnostic_steps"].append(item)
        result["events"] = [dict(e) for e in connection.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence_no", (run_id,))]
        tool_failures = {event['entity_id']: decode(event['payload_redacted_json']) for event in result['events']
                         if event['entity_type'] == 'tool_call' and event['event_type'] in {'tool_failed', 'tool_denied', 'tool_timed_out'}}
        result["tool_calls"] = []
        for call in connection.execute("SELECT t.*,o.diagnostics_json,o.target_id AS actual_target_id FROM tool_calls t LEFT JOIN runtime_observations o ON o.id=t.request_id WHERE t.run_id=? ORDER BY t.started_at", (run_id,)):
            item = {**dict(call), "diagnostic": decode(call["diagnostics_json"] or "{}") or tool_failures.get(call['id'], {})}
            item.pop("diagnostics_json", None)
            actual_id = item.pop("actual_target_id", None)
            item["actual_target_id"] = actual_id
            if actual_id:
                if target_snapshot.get("id") == actual_id:
                    item["actual_target"] = target_snapshot
                else:
                    target_row = connection.execute("SELECT config_json FROM runtime_targets WHERE id=?", (actual_id,)).fetchone()
                    if target_row:
                        config = decode(target_row["config_json"])
                        item["actual_target"] = {"id": actual_id, "name": config.get("name"), "host": config.get("host"),
                                                  "port": config.get("port"), "execution_mode": config.get("execution_mode", "forced_gateway"),
                                                  "connector": config.get("connector")}
                    else:
                        item["actual_target"] = {"id": actual_id, "source": "runtime_observation"}
            else:
                item["actual_target"] = None
            result["tool_calls"].append(item)
        result["evidence"] = []
        for evidence in connection.execute("SELECT e.*,t.tool_id,t.status AS call_status FROM evidence e JOIN tool_calls t ON t.id=e.tool_call_id WHERE t.run_id=? ORDER BY e.observed_at", (run_id,)):
            item = dict(evidence)
            item["payload"] = decode(item.pop("payload_redacted_json"))
            result["evidence"].append(item)
        for key in ("events",):
            for item in result[key]:
                item["payload"] = decode(item.pop("payload_redacted_json"))
        return result
    finally:
        connection.close()


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    database = db()
    with database.connect() as connection:
        run = connection.execute("SELECT id FROM diagnosis_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "调查不存在")
        if not connection.execute("SELECT 1 FROM run_executions WHERE run_id=?", (run_id,)).fetchone():
            raise HTTPException(409, "历史协议不支持可靠取消；请等待旧调查结束")
    from app.runtime.coordinator import get_coordinator
    return get_coordinator(database).cancel(run_id)


@router.get("/patterns")
def list_patterns() -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        rows = connection.execute('''SELECT p.*,e.incident_id,e.created_at,o.business_id,o.proof_json AS ownership_proof
            FROM pattern_versions p JOIN experiences e ON e.id=p.source_experience_id
            LEFT JOIN experience_ownership o ON o.experience_id=e.id ORDER BY p.pattern_key,p.version DESC''').fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['ownership_state']='confirmed' if item['business_id'] else 'historical_only'
            item['ownership_proof']=decode(item['ownership_proof'],{})
            for field in ("roles", "topology", "symptoms", "checks", "exclusions", "validity"):
                item[field] = decode(item.pop(field+"_json"))
            latest = connection.execute("SELECT id FROM graph_snapshots WHERE system_id=(SELECT system_id FROM graph_snapshots WHERE id=?) ORDER BY version DESC LIMIT 1", (item["validity"].get("snapshot_id"),)).fetchone()
            item["health"] = feedback_health(connection, row["id"])
            item["compatibility"] = compatibility(connection, row, latest[0]) if latest else {"compatible": False, "reasons": ["source_snapshot_unavailable"]}
            item["health"]["needs_review"] = item["health"]["needs_review"] or not item["compatibility"]["compatible"]
            result.append(item)
        return result
    finally:
        connection.close()


@router.get("/knowledge-counterexamples")
def list_counterexamples() -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        rows = connection.execute("SELECT * FROM diagnosis_runs WHERE status NOT IN ('queued','running','waiting_input','awaiting_review') ORDER BY started_at DESC LIMIT 100").fetchall()
        result = []
        for row in rows:
            summary = decode(row["summary_json"])
            measured=outcome(connection,row)
            if measured['native']:
                summary={**summary,'diagnosis_result':measured['result'] or {'schema_version':'ab-result-1','diagnosis_status':'unresolved'}}
                examples=counterexamples(summary,measured['status'])
            else:
                examples = summary.get("counterexamples", counterexamples(summary, row["status"]))
            if examples:
                result.append({"run_id": row["id"], "graph_snapshot_id": row["graph_snapshot_id"], "status": row["status"], "counterexamples": examples})
        return result
    finally:
        connection.close()


@router.post("/incidents/{incident_id}/repair")
def record_repair(incident_id: str, request: RepairRecord, actor: str = Depends(current_actor)) -> dict[str, Any]:
    if not request.operator_verified:
        raise HTTPException(status_code=422, detail="A human must confirm diagnosis, repair and verification before capturing experience")
    connection = db().connect()
    try:
        run = connection.execute("SELECT r.*,i.system_id,s.compose_project FROM diagnosis_runs r JOIN incidents i ON i.id=r.incident_id JOIN systems s ON s.id=i.system_id WHERE r.incident_id=? ORDER BY r.started_at DESC LIMIT 1", (incident_id,)).fetchone()
        if not run:
            raise HTTPException(status_code=409, detail="A supported run is required before experience capture")
        measured=outcome(connection,run)
        if measured['native']:
            if not measured['root_supported']:
                raise HTTPException(409,'原生调查需要完整根因支持；部分异常或健康结果不能捕获根因经验')
            from app.knowledge import verify_native_repair
            try:
                checks=supported_checks(connection,run,measured['result'])
                verification=verify_native_repair(db(),get_settings(),run,request)
            except (ValueError,PermissionError) as error:
                raise HTTPException(409,str(error)) from error
            if not native_verification_valid(connection,run,verification['call_id'],verification['authorized_endpoint']):
                raise HTTPException(409,'原服务器入口未通过完整的授权 HTTP 200 恢复验证；调用记录已保留')
            cause=measured['result']
            target_ids={identifier for segment in cause['verified_segments'] for identifier in segment['participants'].values()}
        else:
            if run['status']!='supported' or not run['final_hypothesis_id']:
                raise HTTPException(409,'A supported run is required before experience capture')
            if not request.verification_url:
                raise HTTPException(422,'旧调查的恢复验证需要原来允许的 URL')
            verification = ToolRegistry(db(), get_settings()).execute(
                run["id"], run["graph_snapshot_id"], run["compose_project"], "health.get",
                {"url": request.verification_url}, purpose="verification")
            if verification.get("payload", {}).get("status_code") != 200:
                raise HTTPException(status_code=409, detail="The configured read-only verification URL did not return HTTP 200")
            hypothesis = connection.execute("SELECT * FROM hypotheses WHERE id=?", (run["final_hypothesis_id"],)).fetchone()
            cause={'failure_mode':hypothesis['failure_mode'],'causal_chain':decode(hypothesis['causal_chain_json'],[]),'prediction':hypothesis['falsifiable_prediction']}
            target_ids={hypothesis['target_node_id']}
            checks=successful_checks(connection,run['final_hypothesis_id'],run['graph_snapshot_id'])
        snapshot = connection.execute("SELECT * FROM graph_snapshots WHERE id=?", (run["graph_snapshot_id"],)).fetchone()
        nodes = [dict(n) for n in connection.execute("SELECT * FROM graph_nodes WHERE snapshot_id=?", (snapshot["id"],))]
        roles = [node['role'] for node in nodes if node['id'] in target_ids and node['role']!='unknown']
        role_by_id = {n["id"]: n["role"] for n in nodes}
        topology = sorted({f"{role_by_id.get(e['from_node_id'],'unknown')}->{role_by_id.get(e['to_node_id'],'unknown')}:{e['relation']}"
                           for e in connection.execute("SELECT * FROM graph_edges WHERE snapshot_id=?", (snapshot["id"],))})
        symptom = decode(connection.execute("SELECT symptom_json FROM incidents WHERE id=?", (incident_id,)).fetchone()["symptom_json"])["text"]
        symptom_terms = sorted({w for w in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", symptom.casefold()) if len(w) > 1})
        roles = sorted(set(roles) | {check["target_role"] for check in checks if check.get("target_role")})
        verifications = [verification["call_id"]]
        experience_id, pattern_id, timestamp = str(uuid.uuid4()), str(uuid.uuid4()), utcnow()
        pattern_body = {"roles": roles, "topology": topology, "symptoms": symptom_terms, "checks": checks,
                        "exclusions": ["Do not apply when graph contains unresolved conflicts", "Reverify when source, deployment or validation feedback changes"], "validity": graph_contract(connection, snapshot["id"])}
        digest = hashlib.sha256(json.dumps(pattern_body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with db().transaction() as tx:
            tx.execute("INSERT INTO experiences(id,incident_id,run_id,graph_snapshot_id,status,root_cause_json,repair_record_json,verification_call_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (experience_id, incident_id, run["id"], snapshot["id"], "verified", json.dumps(cause),
                        json.dumps({"summary": request.summary, "verification_summary": request.verification_summary,
                                    "verification_http_status": 200, "actor": actor,
                                    'verification_endpoint':verification.get('authorized_endpoint')}), json.dumps(verifications), timestamp))
            tx.execute("INSERT INTO pattern_versions(id,pattern_key,version,source_experience_id,status,roles_json,topology_json,symptoms_json,checks_json,exclusions_json,validity_json,schema_version,content_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pattern_id, f"pattern-{experience_id[:8]}", 1, experience_id, "candidate",
                        json.dumps(roles), json.dumps(topology), json.dumps(symptom_terms), json.dumps(checks),
                        json.dumps(pattern_body["exclusions"]), json.dumps(pattern_body["validity"]), 2, digest))
            from app.knowledge import refresh_business_ownership
            refresh_business_ownership(tx,experience_id)
            emit(tx, run["system_id"], run["id"], "experience", experience_id, "experience_candidate_created", {"pattern_id": pattern_id, "digest": digest}, actor)
        return {"experience_id": experience_id, "pattern_version_id": pattern_id, "status": "candidate", "content_digest": digest}
    finally:
        connection.close()


@router.post("/patterns/{pattern_id}/review")
def review_pattern(pattern_id: str, request: ReviewRequest, actor: str = Depends(current_actor)) -> dict[str, Any]:
    connection = db().connect()
    try:
        pattern = connection.execute("SELECT p.*,e.run_id,e.incident_id,i.system_id FROM pattern_versions p JOIN experiences e ON e.id=p.source_experience_id JOIN incidents i ON i.id=e.incident_id WHERE p.id=?", (pattern_id,)).fetchone()
        if not pattern:
            raise HTTPException(status_code=404, detail="Pattern version not found")
        if pattern["status"] != "candidate":
            raise HTTPException(status_code=409, detail="Only Candidate patterns can be reviewed for promotion")
        if request.decision == "approve":
            latest = connection.execute("SELECT id FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1", (pattern["system_id"],)).fetchone()
            if not latest or not compatibility(connection, pattern, latest[0])["compatible"]:
                raise HTTPException(409, "Pattern source or current graph changed; a new verified Candidate is required")
            experience = connection.execute("SELECT * FROM experiences WHERE id=?", (pattern["source_experience_id"],)).fetchone()
            verification_ids = decode(experience["verification_call_ids_json"], []) if experience else []
            source_run=connection.execute('SELECT * FROM diagnosis_runs WHERE id=?',(pattern['run_id'],)).fetchone()
            native=source_run and outcome(connection,source_run)['native']
            verified = False
            for call_id in verification_ids:
                if native:
                    verified=verified or native_verification_valid(connection,source_run,call_id,decode(experience['repair_record_json']).get('verification_endpoint'))
                    continue
                observation = connection.execute("SELECT t.status,t.purpose,e.payload_redacted_json FROM tool_calls t JOIN evidence e ON e.tool_call_id=t.id WHERE t.id=? AND t.run_id=?", (call_id, pattern["run_id"])).fetchone()
                if observation and observation["status"] == "succeeded" and observation["purpose"] == "verification" and decode(observation["payload_redacted_json"]).get("status_code") == 200:
                    verified = True
            if not experience or experience["status"] != "verified" or not verified or not decode(experience["repair_record_json"]).get("summary"):
                raise HTTPException(409, "Trusted promotion requires recorded repair and a real successful verification read")
        review_id, timestamp = str(uuid.uuid4()), utcnow()
        next_status = "trusted" if request.decision == "approve" else "rejected"
        with db().transaction() as tx:
            tx.execute("INSERT INTO reviews(id,actor_id,subject_type,pattern_version_id,subject_digest,decision,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (review_id, actor, "pattern", pattern_id, pattern["content_digest"], request.decision, request.reason, timestamp))
            tx.execute("UPDATE pattern_versions SET status=? WHERE id=? AND content_digest=?", (next_status, pattern_id, pattern["content_digest"]))
            emit(tx, pattern["system_id"], pattern["run_id"], "pattern", pattern_id, f"pattern_{next_status}",
                 {"review_id": review_id, "digest": pattern["content_digest"], "reason": request.reason}, actor)
        return {"review_id": review_id, "pattern_version_id": pattern_id, "status": next_status, "content_digest": pattern["content_digest"]}
    finally:
        connection.close()


@router.post("/conflicts/{conflict_id}/review")
def review_conflict(conflict_id: str, request: ConflictReviewRequest,
                    actor: str = Depends(current_actor)) -> dict[str, Any]:
    connection = db().connect()
    try:
        conflict = connection.execute("""SELECT c.*,g.system_id FROM graph_conflicts c
          JOIN graph_snapshots g ON g.id=c.snapshot_id WHERE c.id=?""", (conflict_id,)).fetchone()
        if not conflict:
            raise HTTPException(status_code=404, detail="Graph conflict not found")
        if conflict["status"] != "open":
            raise HTTPException(status_code=409, detail="Only open conflicts can be reviewed")
        review_id, timestamp = str(uuid.uuid4()), utcnow()
        digest_content = {"snapshot_id": conflict["snapshot_id"], "subject_key": conflict["subject_key"],
                          "property": conflict["property"], "scope": conflict["scope"],
                          "claim_ids": decode(conflict["claim_ids_json"], [])}
        digest = hashlib.sha256(json.dumps(digest_content, sort_keys=True).encode()).hexdigest()
        status = "resolved" if request.decision == "approve" else "open"
        with db().transaction() as tx:
            tx.execute("INSERT INTO reviews(id,actor_id,subject_type,conflict_id,subject_digest,decision,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (review_id, actor, "graph_conflict", conflict_id, digest, request.decision, request.reason, timestamp))
            tx.execute("UPDATE graph_conflicts SET status=?,resolution_review_id=? WHERE id=?",
                       (status, review_id if status == "resolved" else None, conflict_id))
            last_run = tx.execute("""SELECT r.id FROM diagnosis_runs r JOIN incidents i ON i.id=r.incident_id
                                   WHERE i.system_id=? ORDER BY r.started_at DESC LIMIT 1""",
                                  (conflict["system_id"],)).fetchone()
            payload = {"review_id": review_id, "digest": digest, "decision": request.decision, "reason": request.reason}
            if last_run:
                emit(tx, conflict["system_id"], last_run["id"], "graph_conflict", conflict_id,
                     f"conflict_{status}", payload, actor)
            else:
                tx.execute("INSERT INTO events(id,system_id,run_id,sequence_no,entity_type,entity_id,event_type,actor,payload_redacted_json,created_at) VALUES(?,?,NULL,?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), conflict["system_id"], 0, "graph_conflict", conflict_id,
                            f"conflict_{status}", actor, json.dumps(payload, sort_keys=True), timestamp))
        return {"review_id": review_id, "conflict_id": conflict_id, "status": status, "subject_digest": digest}
    finally:
        connection.close()


@router.post("/pattern-matches/{match_id}/outcome")
def record_pattern_outcome(match_id: str, request: PatternMatchOutcome,
                           actor: str = Depends(current_actor)) -> dict[str, Any]:
    if request.validation is None and request.usefulness is None:
        raise HTTPException(status_code=422, detail="Provide a validation or usefulness outcome")
    connection = db().connect()
    try:
        row = connection.execute("""SELECT m.*,r.incident_id,i.system_id FROM pattern_matches m
          JOIN diagnosis_runs r ON r.id=m.run_id JOIN incidents i ON i.id=r.incident_id WHERE m.id=?""", (match_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pattern match not found")
        if row["decision"] != "suggested":
            raise HTTPException(status_code=409, detail="Only a suggested match can receive validation feedback")
        if request.validation and row["validation_outcome"] != "pending":
            raise HTTPException(status_code=409, detail="Validation outcome is already recorded")
        if request.usefulness and row["usefulness_outcome"] != "pending":
            raise HTTPException(status_code=409, detail="Usefulness outcome is already recorded")
        with db().transaction() as tx:
            current = tx.execute("SELECT validation_outcome,usefulness_outcome FROM pattern_matches WHERE id=?", (match_id,)).fetchone()
            if (request.validation and current["validation_outcome"] != "pending") or (request.usefulness and current["usefulness_outcome"] != "pending"):
                raise HTTPException(409, "Feedback was already recorded")
            if request.validation:
                tx.execute("UPDATE pattern_matches SET validation_outcome=? WHERE id=?", (request.validation, match_id))
            if request.usefulness:
                tx.execute("UPDATE pattern_matches SET usefulness_outcome=? WHERE id=?", (request.usefulness, match_id))
            emit(tx, row["system_id"], row["run_id"], "pattern_match", match_id, "pattern_match_outcome",
                 {"validation": request.validation, "usefulness": request.usefulness}, actor)
            if feedback_health(tx, row["pattern_version_id"])["needs_review"]:
                emit(tx, row["system_id"], row["run_id"], "pattern", row["pattern_version_id"], "pattern_review_needed", {"reason": "negative_validation_feedback", "match_id": match_id}, actor)
        return {"match_id": match_id, "validation_outcome": request.validation or row["validation_outcome"],
                "usefulness_outcome": request.usefulness or row["usefulness_outcome"]}
    finally:
        connection.close()


@router.get("/metrics")
def metrics() -> dict[str, Any]:
    connection = db().connect()
    try:
        runs = connection.execute("SELECT * FROM diagnosis_runs").fetchall()
        outcomes=[outcome(connection,run) for run in runs]
        completed = [item for item in outcomes if item['completed']]
        durations = []
        for row, measured in zip(runs, outcomes, strict=True):
            if row["supported_at"] and measured['root_supported']:
                start = datetime.fromisoformat(row["started_at"])
                end = datetime.fromisoformat(row["supported_at"])
                durations.append(max(0, (end-start).total_seconds()))
        calls = connection.execute("SELECT status FROM tool_calls WHERE purpose='diagnosis'").fetchall()
        patterns = connection.execute("SELECT decision,validation_outcome,usefulness_outcome FROM pattern_matches").fetchall()
        paired_rows = connection.execute("SELECT pair_key,repetition,arm,metrics_json,verdict FROM evaluation_trials").fetchall()
        paired: dict[tuple[str, int], dict[str, Any]] = {}
        for trial in paired_rows:
            paired.setdefault((trial["pair_key"], trial["repetition"]), {})[trial["arm"]] = decode(trial["metrics_json"])
        complete_pairs = [pair for pair in paired.values() if "graph_only" in pair and "graph_pattern" in pair]
        call_reductions = [pair["graph_only"].get("diagnostic_tool_calls", 0) -
                           pair["graph_pattern"].get("diagnostic_tool_calls", 0) for pair in complete_pairs]
        def rate(n: int, d: int) -> float | None:
            return round(n/d, 4) if d else None
        return {"runs_total": len(runs), "runs_completed": len(completed), "supported_runs": sum(item['root_supported'] for item in outcomes),
                "direct_cause_runs":sum(item['direct_supported'] for item in outcomes),
                "goal_completed_runs":sum(item['completion_satisfied'] for item in outcomes),
                "healthy_runs":sum(item['healthy'] for item in outcomes),
                "partial_runs":sum(item.get('diagnosis_status')=='partial' for item in outcomes),
                "aborted_runs":sum(item.get('execution_status') in {'failed','cancelled','timed_out','interrupted'} for item in outcomes),
                "unresolved_runs": sum(item['completed'] and item['status']=='unresolved' for item in outcomes), "tt_rca_seconds_median": statistics.median(durations) if durations else None,
                "diagnostic_tool_attempts": len(calls), "denied_or_failed_calls": sum(c["status"] in {"denied", "failed", "timed_out"} for c in calls),
                "patterns_retrieved": len(patterns), "patterns_suggested": sum(p["decision"]=="suggested" for p in patterns),
                "patterns_validated": sum(p["validation_outcome"]=="validated" for p in patterns),
                "patterns_useful": sum(p["usefulness_outcome"]=="useful" for p in patterns),
                "pattern_suggestion_rate": rate(sum(p["decision"]=="suggested" for p in patterns), len(patterns)),
                "complete_paired_evaluations": len(complete_pairs),
                "three_arm_evaluation": compare_trials(paired_rows),
                "mean_tool_call_reduction_per_pair": round(sum(call_reductions)/len(call_reductions), 3) if call_reductions else None}
    finally:
        connection.close()


@router.post("/evaluation-cases")
def create_evaluation_case(request: EvaluationCaseCreate,
                           actor: str = Depends(current_actor)) -> dict[str, Any]:
    connection = db().connect()
    try:
        system = connection.execute("SELECT * FROM systems WHERE id=?", (request.system_id,)).fetchone()
        if not system:
            raise HTTPException(status_code=404, detail="System not found")
        snapshot = connection.execute("SELECT * FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1",
                                      (request.system_id,)).fetchone()
        if not snapshot:
            raise HTTPException(status_code=409, detail="A current graph snapshot is required")
        truth = json.dumps(request.ground_truth, ensure_ascii=False, sort_keys=True)
        if len(truth.encode("utf-8")) > 8_000:
            raise HTTPException(status_code=413, detail="Ground-truth record exceeds 8 KB")
        case_id = str(uuid.uuid4())
        coverage = decode(snapshot["coverage_json"])
        fixture_digest = hashlib.sha256(
            f"{system['repo_revision']}:{snapshot['deployment_digest']}:{coverage.get('source_digest','')}".encode()).hexdigest()
        try:
            with db().transaction() as tx:
                tx.execute("INSERT INTO evaluation_cases(id,case_key,system_fixture_ref,fixture_digest,fault_class,ground_truth_json,protocol_version) VALUES(?,?,?,?,?,?,?)",
                           (case_id, request.case_key, request.system_id, fixture_digest, request.fault_class,
                            truth, request.protocol_version))
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise HTTPException(status_code=409, detail="Evaluation case key already exists") from exc
            raise
        return {"id": case_id, "case_key": request.case_key, "system_id": request.system_id,
                "fault_class": request.fault_class, "fixture_digest": fixture_digest,
                "protocol_version": request.protocol_version}
    finally:
        connection.close()


@router.post("/evaluation-pairs")
def create_evaluation_pair(request: EvaluationPairCreate,
                           actor: str = Depends(current_actor)) -> dict[str, Any]:
    connection = db().connect()
    try:
        case = connection.execute("SELECT * FROM evaluation_cases WHERE id=?", (request.case_id,)).fetchone()
        if not case:
            raise HTTPException(status_code=404, detail="Evaluation case not found")
        if request.reset_digest != case["fixture_digest"]:
            raise HTTPException(status_code=409, detail="Reset digest must match the evaluation case fixture digest")
        if request.graph_only_run_id == request.graph_pattern_run_id:
            raise HTTPException(status_code=422, detail="The paired arms require separate runs")
        specifications = [("graph_only", request.graph_only_run_id, request.graph_only_verdict),
                          ("graph_pattern", request.graph_pattern_run_id, request.graph_pattern_verdict)]
        prepared = []
        for arm, run_id, verdict in specifications:
            run = connection.execute(_evaluation_run_query(), (run_id,)).fetchone()
            if not run:
                raise HTTPException(status_code=404, detail=f"{arm} run not found")
            if run["system_id"] != case["system_fixture_ref"] or run["mode"] != arm:
                raise HTTPException(status_code=409, detail="Both runs must use the case system and their matching mode")
            if _run_fixture_digest(run) != case["fixture_digest"]:
                raise HTTPException(status_code=409, detail="Run graph does not match the evaluation case fixture")
            measured_outcome=outcome(connection,run)
            if not measured_outcome['completed']:
                raise HTTPException(status_code=409, detail="Both runs must be complete")
            if (measured_outcome['status'] == "unresolved") != (verdict == "unresolved"):
                raise HTTPException(409, "An unresolved run must have an unresolved verdict")
            metrics_data = run_metrics(connection, run)
            assessment = _assess_run_truth(connection, case, run)
            if assessment:
                if verdict != assessment["expected_verdict"]:
                    raise HTTPException(409, "Verdict does not match the registered fixture ground truth")
                metrics_data.update(assessment)
            prepared.append((arm, run, verdict, metrics_data))
        mismatch = _run_pair_mismatch(prepared[0][1], prepared[1][1])
        if mismatch:
            raise HTTPException(status_code=409, detail=mismatch)
        existing = connection.execute("SELECT 1 FROM evaluation_trials WHERE pair_key=? AND repetition=? LIMIT 1",
                                      (request.pair_key, request.repetition)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Pair key and repetition have already been recorded")
        trial_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        with db().transaction() as tx:
            for trial_id, (arm, run, verdict, metrics_data) in zip(trial_ids, prepared, strict=True):
                tx.execute("INSERT INTO evaluation_trials(id,case_id,run_id,pair_key,arm,repetition,reset_digest,metrics_json,verdict,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (trial_id, case["id"], run["id"], request.pair_key, arm, request.repetition,
                            request.reset_digest, json.dumps(metrics_data), verdict, utcnow()))
                emit(tx, run["system_id"], run["id"], "evaluation_trial", trial_id, "evaluation_trial_recorded",
                     {"pair_key": request.pair_key, "arm": arm, "repetition": request.repetition,
                      "reset_digest": request.reset_digest, "verdict": verdict, "metrics": metrics_data}, actor)
        return {"pair_key": request.pair_key, "repetition": request.repetition,
                "case_id": case["id"], "trials": [
                    {"id": trial_id, "run_id": run["id"], "arm": arm, "verdict": verdict, "metrics": metrics_data}
                    for trial_id, (arm, run, verdict, metrics_data) in zip(trial_ids, prepared, strict=True)]}
    finally:
        connection.close()


@router.get("/evaluation-cases")
def list_evaluation_cases() -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        return [dict(row) for row in connection.execute("""SELECT c.id,c.case_key,c.system_fixture_ref AS system_id,
          s.name AS system_name,c.fixture_digest,c.fault_class,c.protocol_version
          FROM evaluation_cases c JOIN systems s ON s.id=c.system_fixture_ref ORDER BY c.case_key""")]
    finally:
        connection.close()


@router.post("/evaluation-trials")
def create_evaluation_trial(request: EvaluationTrialCreate,
                            actor: str = Depends(current_actor)) -> dict[str, Any]:
    connection = db().connect()
    try:
        case = connection.execute("SELECT * FROM evaluation_cases WHERE id=?", (request.case_id,)).fetchone()
        run = connection.execute(_evaluation_run_query(), (request.run_id,)).fetchone()
        if not case or not run:
            raise HTTPException(status_code=404, detail="Evaluation case or run not found")
        if request.reset_digest != case["fixture_digest"]:
            raise HTTPException(status_code=409, detail="Reset digest must match the evaluation case fixture digest")
        if _run_fixture_digest(run) != case["fixture_digest"]:
            raise HTTPException(status_code=409, detail="Run graph does not match the evaluation case fixture")
        if case["system_fixture_ref"] != run["system_id"]:
            raise HTTPException(status_code=409, detail="Run target does not match the evaluation case system")
        if run["mode"] != request.arm:
            raise HTTPException(status_code=409, detail="Run mode does not match the selected evaluation arm")
        measured_outcome=outcome(connection,run)
        if not measured_outcome['completed']:
            raise HTTPException(status_code=409, detail="Only completed supported or unresolved runs can be scored")
        if (measured_outcome['status'] == "unresolved") != (request.verdict == "unresolved"):
            raise HTTPException(409, "An unresolved run must have an unresolved verdict")
        siblings = connection.execute("SELECT * FROM evaluation_trials WHERE pair_key=? AND repetition=?",
                                      (request.pair_key, request.repetition)).fetchall()
        if any(sibling["case_id"] != request.case_id or sibling["reset_digest"] != request.reset_digest
               for sibling in siblings):
            raise HTTPException(status_code=409, detail="Paired runs must use the same case and reset digest")
        if any(sibling["run_id"] == request.run_id for sibling in siblings):
            raise HTTPException(status_code=409, detail="A run cannot be used in both arms of a pair")
        if any(sibling["arm"] == request.arm for sibling in siblings):
            raise HTTPException(status_code=409, detail="Each pair can contain only one trial per arm")
        for sibling in siblings:
            sibling_run = connection.execute(_evaluation_run_query(), (sibling["run_id"],)).fetchone()
            if not sibling_run or _run_fixture_digest(sibling_run) != case["fixture_digest"]:
                raise HTTPException(status_code=409, detail="Sibling run does not match the evaluation case fixture")
            mismatch = _run_pair_mismatch(sibling_run, run)
            if mismatch:
                raise HTTPException(status_code=409, detail=mismatch)
        measured = run_metrics(connection, run)
        assessment = _assess_run_truth(connection, case, run)
        if assessment:
            if request.verdict != assessment["expected_verdict"]:
                raise HTTPException(409, "Verdict does not match the registered fixture ground truth")
            measured.update(assessment)
        trial_id = str(uuid.uuid4())
        with db().transaction() as tx:
            tx.execute("INSERT INTO evaluation_trials(id,case_id,run_id,pair_key,arm,repetition,reset_digest,metrics_json,verdict,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (trial_id, request.case_id, request.run_id, request.pair_key, request.arm,
                        request.repetition, request.reset_digest, json.dumps(measured), request.verdict, utcnow()))
            emit(tx, run["system_id"], run["id"], "evaluation_trial", trial_id, "evaluation_trial_recorded",
                 {"pair_key": request.pair_key, "arm": request.arm, "repetition": request.repetition,
                  "verdict": request.verdict, "metrics": measured}, actor)
        return {"id": trial_id, "case_id": request.case_id, "run_id": request.run_id,
                "pair_key": request.pair_key, "arm": request.arm, "repetition": request.repetition,
                "reset_digest": request.reset_digest, "metrics": measured, "verdict": request.verdict}
    finally:
        connection.close()


@router.get("/evaluations")
def list_evaluations() -> list[dict[str, Any]]:
    connection = db().connect()
    try:
        rows = connection.execute("""SELECT t.*,c.case_key,c.fault_class,r.mode AS run_mode
          FROM evaluation_trials t JOIN evaluation_cases c ON c.id=t.case_id
          JOIN diagnosis_runs r ON r.id=t.run_id ORDER BY t.pair_key,t.repetition,t.arm""").fetchall()
        return [{**dict(row), "metrics": decode(row["metrics_json"]), "metrics_json": None}
                for row in rows]
    finally:
        connection.close()
