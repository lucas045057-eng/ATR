from copy import deepcopy

import pytest
from app.agent.task_stop_policy import (
    bind_objective_scope,
    build_investigation_plan,
    decide_stop,
    evaluate_progress,
    filter_actions_for_objective,
    load_task_snapshot,
    resolve_objective,
    select_finish_override,
    soft_limits_from_settings,
    sync_plan_with_history,
)

API = {"node_id": "api-node", "name": "catalog-api", "role": "application"}
DB = {"node_id": "db-node", "name": "postgres", "role": "database"}


def action(action_id, tool_id, node_id="api-node", score=5.0):
    return {"action_id": action_id, "tool_id": tool_id, "target_node_id": node_id,
            "scheduling": {"score": score}}


def objective(task_type="focused_diagnosis", services=None):
    return resolve_objective(task_type, services or [API])


def plan_for(task_type="focused_diagnosis", actions=None):
    obj = objective(task_type, [API, DB] if task_type == "root_cause_investigation" else [API])
    return obj, build_investigation_plan(obj, actions or [
        action("api-status", "container.status"), action("api-logs", "container.logs")
    ])


def completed_required_plan(obj, plan):
    history = [{"action_id": check["action_id"], "status": "succeeded",
                "evidence_id": "ev-" + check["check_type"]}
               for check in plan["checks"] if check["required"] and check["action_id"]]
    return sync_plan_with_history(plan, history)


def test_health_check_stops_after_current_status_and_logs_without_root_cause_gate():
    obj, plan = plan_for("health_check")
    plan = completed_required_plan(obj, plan)
    result = decide_stop(obj, plan, {}, "unresolved", [], model_calls=0, tool_calls=2)
    assert result["should_stop"] is True
    assert result["task_completion_status"] == "completed"
    assert result["stop_reason"] == "task_completed"


def test_focused_exited_status_and_logs_complete_direct_diagnosis_without_gate_support():
    obj, plan = plan_for()
    plan = completed_required_plan(obj, plan)
    result = decide_stop(obj, plan, {"finding_level": "direct_fault_cause_unknown"},
                         "unresolved", [], model_calls=0, tool_calls=2)
    assert result["should_stop"] is True
    assert result["task_completion_status"] == "completed"
    assert result["finding_level"] == "direct_fault_cause_unknown"
    assert result["root_cause_confirmed"] is False


def test_running_state_is_scoped_observation_not_http_health_claim():
    obj, plan = plan_for()
    plan = completed_required_plan(obj, plan)
    result = decide_stop(obj, plan, {"finding_level": "no_fault_observed_in_scope"},
                         "unresolved", [], model_calls=0, tool_calls=2)
    assert result["should_stop"] is True
    assert result["finding_level"] == "no_fault_observed_in_scope"


def test_root_cause_task_does_not_complete_on_direct_fault_alone():
    obj, plan = plan_for("root_cause_investigation")
    plan = completed_required_plan(obj, plan)
    candidate = action("api-config", "config.list", "api-node", score=5.0)
    result = decide_stop(obj, plan, {"finding_level": "direct_fault_cause_unknown"},
                         "unresolved", [candidate], model_calls=1, tool_calls=2,
                         active_hypothesis_nodes={"api-node"})
    assert result["should_stop"] is False
    assert result["task_completion_status"] != "completed"


def test_identical_log_content_with_new_timestamp_is_not_new_progress():
    evidence = [{"tool_id": "container.logs", "target_node_id": "api-node",
                 "payload": {"text": "2026-10-12T10:00:00Z ERROR connection refused"}}]
    previous = evaluate_progress([], {}, {}, [], [], [], 1)
    first = evaluate_progress(evidence, previous["progress_state"], {}, [], [], [], 2)
    repeated = [{**evidence[0], "payload": {"text": "2026-10-12T10:05:00Z ERROR connection refused"}}]
    second = evaluate_progress(repeated, first["progress_state"], {}, [], [], [], 3)
    assert first["progress"]["new_relevant_fact_count"] == 1
    assert second["progress"]["new_relevant_fact_count"] == 0
    assert second["progress"]["consecutive_no_progress_rounds"] == 1


def test_pending_required_check_prevents_early_stop():
    obj, plan = plan_for()
    result = decide_stop(obj, plan, {}, "unresolved", [action("api-logs", "container.logs")],
                         model_calls=0, tool_calls=1)
    assert result["should_stop"] is False
    assert result["required_checks_remaining"] == 2


def test_no_legal_candidate_stops_without_another_model_request():
    obj, plan = plan_for("root_cause_investigation")
    plan = completed_required_plan(obj, plan)
    result = decide_stop(obj, plan, {}, "unresolved", [], model_calls=1, tool_calls=2)
    assert result["should_stop"] is True
    assert result["stop_reason"] == "no_further_useful_checks"
    assert result["task_completion_status"] == "stopped_with_gap"
    assert result["root_cause_confirmed"] is False


def test_nonempty_but_low_value_action_pool_stops_before_model_call():
    obj, plan = plan_for("root_cause_investigation")
    plan = completed_required_plan(obj, plan)
    low_value = action("low", "code.search", "api-node", score=2.0)
    result = decide_stop(obj, plan, {}, "unresolved", [low_value], model_calls=0,
                         tool_calls=2, active_hypothesis_nodes=set())
    assert result["should_stop"] is True
    assert result["stop_reason"] == "no_further_useful_checks"
    assert result["useful_action_ids"] == []


def test_two_no_progress_rounds_stop_when_no_high_value_check_remains():
    obj, plan = plan_for("root_cause_investigation")
    plan = completed_required_plan(obj, plan)
    low_value = action("db-status", "container.status", "db-node", score=4.0)
    result = decide_stop(obj, plan, {"consecutive_no_progress_rounds": 2}, "unresolved",
                         [low_value], model_calls=2, tool_calls=4,
                         active_hypothesis_nodes={"api-node"})
    assert result["should_stop"] is True
    assert result["stop_reason"] == "no_progress"


def test_two_no_progress_rounds_continue_only_for_active_hypothesis_high_value_check():
    obj, plan = plan_for("root_cause_investigation")
    plan = completed_required_plan(obj, plan)
    high_value = action("api-config", "config.list", "api-node", score=5.0)
    result = decide_stop(obj, plan, {"consecutive_no_progress_rounds": 2}, "unresolved",
                         [high_value], model_calls=2, tool_calls=4,
                         active_hypothesis_nodes={"api-node"})
    assert result["should_stop"] is False
    assert result["high_value_action_ids"] == ["api-config"]


def test_legacy_soft_limits_do_not_remove_useful_checks():
    obj = objective("root_cause_investigation", [API, DB])
    plan = build_investigation_plan(obj, [action("api-status", "container.status"),
                                          action("api-logs", "container.logs")])
    plan = completed_required_plan(obj, plan)
    candidate = action("api-config", "config.list", "api-node", score=5.0)
    low_value = action("api-status-extra", "container.logs", "db-node", score=4.0)
    policy = {**obj["stop_policy"], "soft_model_rounds": 1, "soft_tool_calls": 2}
    obj = {**obj, "stop_policy": policy}
    allowed = decide_stop(obj, plan, {}, "unresolved", [candidate, low_value], model_calls=1,
                          tool_calls=2, active_hypothesis_nodes={"api-node"})
    denied = decide_stop(obj, plan, {}, "unresolved", [candidate], model_calls=2,
                         tool_calls=3, active_hypothesis_nodes={"api-node"},
                         soft_limit_final_step_used=True)
    assert allowed["should_stop"] is False
    assert allowed["soft_limit_final_step"] is False
    assert allowed["useful_action_ids"] == ["api-config", "api-status-extra"]
    assert denied["should_stop"] is False
    assert denied["stop_reason"] is None


def test_low_value_action_does_not_override_root_cause_finish():
    obj, plan = plan_for("root_cause_investigation")
    plan = completed_required_plan(obj, plan)
    candidate = action("db-status", "container.status", "db-node", score=9.0)
    result = decide_stop(obj, plan, {}, "unresolved", [candidate], model_calls=1,
                         tool_calls=2, active_hypothesis_nodes={"api-node"})
    assert result["high_value_action_ids"] == []
    assert select_finish_override(obj, plan, [candidate], "db-status", {"api-node"}) == (None, None)


def test_finish_override_chooses_task_relevant_candidate_over_unrelated_runtime_candidate():
    obj, plan = plan_for("root_cause_investigation")
    plan = completed_required_plan(obj, plan)
    unrelated = action("db-status", "container.status", "db-node", score=9.0)
    relevant = action("api-config", "config.list", "api-node", score=5.0)
    selected, reason = select_finish_override(obj, plan, [unrelated, relevant], "db-status", {"api-node"})
    assert selected == relevant
    assert reason == "high_value_check_for_active_hypothesis"


def test_action_filter_is_scoped_and_does_not_repeat_completed_checks():
    obj, plan = plan_for()
    plan = completed_required_plan(obj, plan)
    candidates = [action("other", "container.logs", "db-node"),
                  action("api-again", "container.status", "api-node")]
    assert filter_actions_for_objective(obj, plan, candidates) == []


def test_plan_records_succeeded_and_unavailable_checks_without_repeating_them():
    obj, plan = plan_for()
    history = [{"action_id": "api-status", "status": "succeeded", "evidence_id": "ev-status"},
               {"action_id": "api-logs", "status": "denied", "evidence_id": None}]
    updated = sync_plan_with_history(plan, history)
    by_type = {check["check_type"]: check for check in updated["checks"]}
    assert by_type["container.status"]["status"] == "completed"
    assert by_type["container.logs"]["status"] == "unavailable"
    assert by_type["container.status"]["evidence_ids"] == ["ev-status"]


def test_legacy_budget_stop_does_not_override_supported_evidence():
    obj, plan = plan_for()
    plan = completed_required_plan(obj, plan)
    result = decide_stop(obj, plan, {}, "supported", [], model_calls=10, tool_calls=10,
                         hard_stop_reason="tool_budget_exhausted")
    assert result["should_stop"] is True
    assert result["stop_reason"] == "evidence_supported"


def test_resume_uses_snapshotted_objective_and_soft_limits_not_current_defaults():
    obj = objective("focused_diagnosis")
    snapshot = {"task_stop_policy": {"version": "task-stop-v1", "objective": obj,
                                     "stop_policy": deepcopy(obj["stop_policy"])}}
    restored = load_task_snapshot(snapshot)
    assert restored == snapshot["task_stop_policy"]
    assert load_task_snapshot({"decision_protocol": "minimal_v2"}) is None


def test_malformed_task_stop_snapshot_fails_closed_before_resume():
    obj = objective()
    malformed = {"version": "task-stop-v1", "objective": {**obj, "stop_policy": None},
                 "stop_policy": obj["stop_policy"]}
    with pytest.raises(ValueError, match="Unsupported task stop policy snapshot"):
        load_task_snapshot({"task_stop_policy": malformed})
    inconsistent = {"version": "task-stop-v1", "objective": obj,
                    "stop_policy": {**obj["stop_policy"], "soft_model_rounds": 100}}
    with pytest.raises(ValueError, match="Unsupported task stop policy snapshot"):
        load_task_snapshot({"task_stop_policy": inconsistent})


def test_root_cause_scope_limit_is_reported_on_new_and_resumed_objectives():
    services = [{"node_id": f"node-{index}", "name": f"service-{index}", "role": "application"}
                for index in range(21)]
    created = resolve_objective("root_cause_investigation", services)
    awaiting_review = resolve_objective("root_cause_investigation", [])
    resumed = bind_objective_scope(awaiting_review, services)

    assert len(created["target_node_ids"]) == 20
    assert created["scope_omission_count"] == 1
    assert resumed["scope_omission_count"] == 1
    supported = decide_stop(created, {"checks": []}, {}, "supported", [], model_calls=1, tool_calls=1)
    assert supported["task_completion_status"] == "completed_with_gaps"


def test_task_type_defaults_conservatively_without_keyword_escalation():
    from app.domain import IncidentCreate
    from pydantic import ValidationError

    request = IncidentCreate(system_id="system-1", symptom="why did catalog-api stop unexpectedly")
    assert request.task_type == "focused_diagnosis"
    with pytest.raises(ValidationError):
        IncidentCreate(system_id="system-1", symptom="check api", task_type="all_systems")


def test_task_soft_limits_are_capped_by_global_hard_budgets():
    class Settings:
        task_rca_model_rounds = 12
        task_rca_tool_calls = 20
        investigation_model_rounds = 6
        investigation_tool_calls = 10

    assert soft_limits_from_settings(Settings(), "root_cause_investigation") == {
        "soft_model_rounds": 6, "soft_tool_calls": 10,
    }


def test_awaiting_scope_resume_binds_confirmed_nodes_without_changing_task_or_policy():
    original = resolve_objective("root_cause_investigation", [])
    bound = bind_objective_scope(original, [API, DB])
    assert bound["task_id"] == original["task_id"]
    assert bound["task_type"] == original["task_type"]
    assert bound["stop_policy"] == original["stop_policy"]
    assert bound["target_node_ids"] == ["api-node", "db-node"]


def test_model_hypothesis_targets_are_limited_to_task_scope_not_whole_graph():
    from app.agent.decision_runtime import minimal_output_contract

    decision_state = {
        "graph": {"nodes": [
            {"id": "api-node", "kind": "service", "name": "catalog-api", "role": "application"},
            {"id": "db-node", "kind": "service", "name": "postgres", "role": "database"},
        ]},
        "scope_node_ids": ["api-node"],
        "available_actions": [],
    }
    assert minimal_output_contract(decision_state)["$defs"]["AllowedServiceNodeId"]["enum"] == ["api-node"]


def test_run_pair_compatibility_ignores_objective_instance_id_but_compares_stop_policy():
    import json

    from app.api.v1 import _run_pair_mismatch

    base = {"provider": "offline", "decision_protocol": "minimal_v2",
            "task_stop_policy": {"version": "task-stop-v1", "objective": {
                "task_id": "task-a", "task_type": "focused_diagnosis", "target_node_ids": ["api-node"]},
                "stop_policy": {"soft_model_rounds": 5, "soft_tool_calls": 6}}}
    left = {"graph_snapshot_id": "graph-1", "symptom_json": json.dumps({"text": "api stopped"}),
            "model_config_json": json.dumps(base)}
    right_config = deepcopy(base)
    right_config["task_stop_policy"]["objective"]["task_id"] = "task-b"
    right = {**left, "model_config_json": json.dumps(right_config)}
    assert _run_pair_mismatch(left, right) is None
    right_config["task_stop_policy"]["stop_policy"]["soft_model_rounds"] = 7
    right["model_config_json"] = json.dumps(right_config)
    assert _run_pair_mismatch(left, right) == "Paired runs must use the same model and prompt configuration"


def test_progress_records_claim_resolution_but_not_raw_claim_or_evidence_text():
    before = [{"claim_id": "claim-1", "status": "unverified"}]
    after = [{"claim_id": "claim-1", "status": "corroborated_candidate"}]
    result = evaluate_progress([], {}, before, after, [], [], 1)
    assert result["progress"]["new_claim_resolution_count"] == 1
    assert "raw" not in str(result["progress_state"])


def test_business_focused_diagnosis_stops_after_seeded_direct_fault_and_logs_without_gpt(client, monkeypatch):
    from app.common import now
    from app.connectors.mock import MockConnector
    from app.settings import get_settings
    from conftest import wait_job
    from test_v22_adaptive import prepared_profile

    profile = prepared_profile(client, monkeypatch, store=False)
    original_read = MockConnector.read

    def direct_fault(self, tool, args):
        if tool == "service.status":
            return {"connector": "mock", "state": "failed"}
        if tool == "service.logs":
            return {"connector": "mock", "text": f"{now()} service exited unexpectedly"}
        return original_read(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", direct_fault)
    settings = get_settings()
    settings.agent_decision_protocol = "minimal_v2"
    settings.external_model_enabled = False
    incident = client.post("/api/v1/incidents", json={
        "system_id": profile["system_id"], "symptom": "catalog-api stopped",
        "mode": "normal", "task_type": "focused_diagnosis",
    })
    assert incident.status_code == 200, incident.text
    queued = client.post(f"/api/v1/incidents/{incident.json()['id']}/runs")
    job = wait_job(client, queued.json()["job_id"])
    run = client.get(f"/api/v1/runs/{job['result']['id']}").json()
    assert run["summary"]["task_completion_status"] == "completed", run["summary"]["investigation_plan"]
    assert run["summary"]["runtime_outcome"]["finding_level"] == "direct_fault_cause_unknown"
    assert run["summary"]["gate"]["status"] == "unresolved"
    assert run["summary"]["model_status"] == "not_called"
    assert {item["check_type"] for item in run["summary"]["completed_checks"]} == {
        "service.status", "service.logs",
    }


def test_business_health_check_stops_with_scoped_healthy_observation_not_http_claim(client, monkeypatch):
    from app.common import now
    from app.connectors.mock import MockConnector
    from app.settings import get_settings
    from conftest import wait_job
    from test_v22_adaptive import prepared_profile

    profile = prepared_profile(client, monkeypatch, store=False)
    original_read = MockConnector.read

    def healthy_status(self, tool, args):
        if tool == "service.status":
            return {"connector": "mock", "state": "active"}
        if tool == "service.logs":
            return {"connector": "mock", "text": f"{now()} service started and ready"}
        return original_read(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", healthy_status)
    settings = get_settings()
    settings.agent_decision_protocol = "minimal_v2"
    settings.external_model_enabled = False
    incident = client.post("/api/v1/incidents", json={
        "system_id": profile["system_id"], "symptom": "check catalog-api status",
        "mode": "normal", "task_type": "health_check",
    })
    assert incident.status_code == 200, incident.text
    queued = client.post(f"/api/v1/incidents/{incident.json()['id']}/runs")
    job = wait_job(client, queued.json()["job_id"])
    run = client.get(f"/api/v1/runs/{job['result']['id']}").json()
    assert run["summary"]["task_completion_status"] == "completed", run["summary"]["investigation_plan"]
    assert run["summary"]["runtime_outcome"]["finding_level"] == "no_fault_observed_in_scope"
    assert run["summary"]["model_status"] == "not_called"
    assert run["summary"]["task_objective"]["task_type"] == "health_check"
