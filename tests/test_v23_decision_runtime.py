import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app.agent.decision_runtime import (
    adapt_minimal_decision,
    classify_outcome,
    minimal_output_contract,
)
from app.connectors.mock import MockConnector
from app.context.model_errors import ModelDecisionError
from app.settings import get_settings
from conftest import wait_job
from pydantic import SecretStr
from test_v22_adaptive import prepared_profile

API = "service-api"
DB = "service-db"
STATUS_ID = "evidence-api-status"
LOG_ID = "evidence-api-log"
DB_ID = "evidence-db-status"
ACTION_ID = "authorized-action-1"


def state(*, actions=None):
    observed = datetime.now(timezone.utc).isoformat()
    log_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    start = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    return {
        "incident": {"window_start": start},
        "graph": {
            "snapshot_current": True,
            "nodes": [
                {"id": API, "kind": "service", "name": "catalog-api", "role": "application"},
                {"id": DB, "kind": "service", "name": "postgres", "role": "database"},
            ],
            "edges": [{"from_node_id": API, "to_node_id": DB, "relation": "depends_on", "certainty": "confirmed"}],
            "open_conflicts": [],
        },
        "available_actions": actions if actions is not None else [
            {"action_id": ACTION_ID, "tool_id": "container.logs", "target_node_id": API,
             "service": "catalog-api", "role": "application", "target_id": "ssh-target-secret-id",
             "arguments": {"container_id": "container-secret-id"}},
        ],
        "evidence": [
            {"evidence_id": STATUS_ID, "target_node_id": API, "tool_id": "container.status",
             "observed_at": observed, "call_id": "call-status", "freshness_state": "current",
             "payload": {"state": "exited", "source_type": "Runtime"}},
            {"evidence_id": LOG_ID, "target_node_id": API, "tool_id": "container.logs",
             "observed_at": observed, "call_id": "call-logs", "freshness_state": "current",
             "payload": {"text": f"{log_time} startup failed: bind failure", "source_type": "Runtime"}},
            {"evidence_id": DB_ID, "target_node_id": DB, "tool_id": "container.status",
             "observed_at": observed, "call_id": "call-db", "freshness_state": "current",
             "payload": {"state": "running", "source_type": "Runtime"}},
        ],
        "scope_node_ids": [API, DB],
    }


def decision(**overrides):
    value = {
        "summary": "The API is stopped; its trigger still needs confirmation.",
        "hypotheses": [{"target_node_id": API, "failure_mode": "startup_failure",
                        "reasoning": "The service status and recent startup messages suggest a failed startup.",
                        "missing_information": [], "confidence": 0.7}],
        "next_investigation": "Inspect the authorized API logs for the startup trigger.",
        "next_action_id": ACTION_ID,
        "finish": False,
        "finish_reason": None,
    }
    value.update(overrides)
    return value


def test_minimal_contract_exposes_only_runtime_ids_and_no_tool_arguments():
    contract = minimal_output_contract(state())
    serialized = str(contract)
    assert "evidence_links" not in serialized
    assert "verification_predictions" not in serialized
    assert "arguments" not in serialized
    assert "container-secret-id" not in serialized
    assert contract["properties"]["next_action_id"]["enum"] == [ACTION_ID, None]
    assert contract["$defs"]["AllowedServiceNodeId"]["enum"] == [API, DB]


def test_runtime_resolves_only_the_exact_candidate_and_builds_gate_compatible_hypothesis():
    adapted = adapt_minimal_decision(decision(), state())
    assert adapted["report"]["action"] == {
        "action_id": ACTION_ID,
        "rationale": "Inspect the authorized API logs for the startup trigger.",
    }
    hypothesis = adapted["report"]["hypotheses"][0]
    assert hypothesis["target_role"] == "application"
    assert hypothesis["verification_predictions"] == []
    assert {link["evidence_id"] for link in hypothesis["evidence_links"]} == {STATUS_ID, LOG_ID}
    assert DB_ID not in {link["evidence_id"] for link in hypothesis["evidence_links"]}


def test_running_database_does_not_refute_api_database_connection_claim():
    from app.agent.workflow import _reviewer

    payload = decision(hypotheses=[{
        "target_node_id": API, "failure_mode": "database_unavailable",
        "reasoning": "The API may have failed while attempting to connect to its database dependency.",
        "missing_information": ["A database connection error in the API logs"], "confidence": 0.6,
    }], next_action_id=None, finish=True)
    adapted = adapt_minimal_decision(payload, state())
    reviewed = _reviewer({**state(), "report": adapted["report"]})
    assert reviewed["gate"]["status"] == "unresolved"
    assert DB_ID not in reviewed["gate"]["hypotheses"][0]["evidence_ids"]
    assert DB_ID not in reviewed["gate"]["hypotheses"][0]["verification"]["counter_evidence"]


def test_no_action_runtime_path_does_not_invoke_model(monkeypatch):
    from app.agent.workflow import _planner
    from app.settings import Settings

    settings = Settings(
        _env_file=None,
        collector_token="test-collector-" + "y" * 40,
    )
    settings.external_model_enabled = True
    settings.model_name = "must-not-be-called"
    settings.agent_decision_protocol = "minimal_v2"

    def unexpected_call(**_kwargs):
        raise AssertionError("runtime-only decisions must not call the provider")

    monkeypatch.setattr("app.context.model._completion", unexpected_call)
    result = _planner(settings, {**state(actions=[]), "decision_protocol": "minimal_v2", "runtime_only": True})
    assert result["report"]["action"] is None
    assert result["runtime_decision"]["action_selected"] is False


@pytest.mark.parametrize("patch,path,code", [
    ({"next_action_id": "not-authorized"}, ("next_action_id",), "action_not_allowed"),
    ({"hypotheses": [{"target_node_id": "unknown", "failure_mode": "startup_failure",
                      "reasoning": "The service status and recent startup messages suggest a failed startup.",
                      "missing_information": [], "confidence": 0.7}]},
     ("hypotheses", 0, "target_node_id"), "unknown_target"),
])
def test_unknown_runtime_references_are_fatal(patch, path, code):
    with pytest.raises(ModelDecisionError) as caught:
        adapt_minimal_decision(decision(**patch), state())
    assert caught.value.path == path
    assert caught.value.error_type == code


def test_bad_noncritical_hypothesis_is_isolated_while_valid_action_remains_usable():
    payload = decision(hypotheses=[
        {"target_node_id": API, "failure_mode": "made_up_mechanism",
         "reasoning": "This hypothesis uses a mechanism that is not in the approved catalog.",
         "missing_information": [], "confidence": 0.4},
    ])
    adapted = adapt_minimal_decision(payload, state())
    assert adapted["report"]["hypotheses"] == []
    assert adapted["report"]["action"]["action_id"] == ACTION_ID
    assert adapted["audit"]["rejected_hypothesis_count"] == 1


def test_model_supplied_cross_target_relation_is_ignored_and_audited():
    payload = decision()
    payload["hypotheses"][0]["evidence_links"] = [
        {"evidence_id": DB_ID, "relation": "refutes", "explanation": "Database is running."},
    ]
    adapted = adapt_minimal_decision(payload, state())
    links = adapted["report"]["hypotheses"][0]["evidence_links"]
    assert DB_ID not in {link["evidence_id"] for link in links}
    assert adapted["audit"]["rejected_hypothesis_count"] == 1
    assert adapted["report"]["action"]["action_id"] == ACTION_ID


def test_mixed_healthy_and_failed_observations_do_not_mark_claim_refuted():
    runtime_state = state()
    old_status = dict(runtime_state["evidence"][0])
    old_status.update(evidence_id="evidence-api-old-healthy", call_id="call-old-status",
                      observed_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat())
    old_status["payload"] = {**old_status["payload"], "state": "running"}
    runtime_state["evidence"].append(old_status)

    adapted = adapt_minimal_decision(decision(), runtime_state)

    assert adapted["claims"][0]["status"] == "unverified"


def test_outcome_separates_direct_api_fault_from_unconfirmed_trigger():
    result = adapt_minimal_decision(decision(), state())
    outcome = classify_outcome(state(), {"status": "unresolved", "hypotheses": []}, result)
    assert outcome["finding_level"] == "direct_fault_cause_unknown"
    assert outcome["direct_faults"] == [{"target_node_id": API, "service": "catalog-api",
                                          "state": "exited", "evidence_id": STATUS_ID}]
    assert outcome["root_cause_confirmed"] is False


def test_runtime_overrules_finish_when_direct_fault_has_unchecked_actions():
    runtime_state = state()
    adapted = adapt_minimal_decision(decision(next_action_id=None, finish=True), runtime_state)
    outcome = classify_outcome(runtime_state, {"status": "unresolved", "hypotheses": []}, adapted)
    assert outcome["finding_level"] == "direct_fault_cause_unknown"
    assert outcome["should_stop"] is False
    assert outcome["runtime_action_id"] == ACTION_ID


def test_complete_healthy_scope_can_end_as_no_fault_observed_not_healthy_claim():
    runtime_state = state()
    runtime_state["evidence"][0]["payload"]["state"] = "active"
    runtime_state["evidence"][2]["payload"]["state"] = "running"
    runtime_state["evidence"] = [runtime_state["evidence"][0], runtime_state["evidence"][2]]
    adapted = adapt_minimal_decision(decision(next_action_id=None, finish=True, hypotheses=[]), runtime_state)
    outcome = classify_outcome(runtime_state, {"status": "unresolved", "hypotheses": []}, adapted)
    assert outcome["finding_level"] == "no_fault_observed_in_scope"
    assert outcome["should_stop"] is True
    assert outcome["root_cause_confirmed"] is False


def test_truncated_authorized_scope_is_not_reported_as_healthy():
    runtime_state = state()
    runtime_state["evidence"] = [runtime_state["evidence"][0], runtime_state["evidence"][2]]
    runtime_state["evidence"][0]["payload"]["state"] = "active"
    runtime_state["evidence"][1]["payload"]["state"] = "running"
    runtime_state["scope_truncated"] = True
    adapted = adapt_minimal_decision(decision(next_action_id=None, finish=True, hypotheses=[]), runtime_state)

    outcome = classify_outcome(runtime_state, {"status": "unresolved", "hypotheses": []}, adapted)

    assert outcome["finding_level"] == "evidence_insufficient"
    assert outcome["should_stop"] is False


def test_empty_candidate_pool_is_encodable_as_null_and_runtime_stops_with_gap():
    empty = state(actions=[])
    empty["evidence"] = []
    contract = minimal_output_contract(empty)
    action_id_contract = contract["properties"]["next_action_id"]
    assert action_id_contract["type"] == ["string", "null"]
    assert action_id_contract["enum"] == [None]
    adapted = adapt_minimal_decision(decision(next_action_id=None, finish=True), empty)
    outcome = classify_outcome(empty, {"status": "unresolved", "hypotheses": []}, adapted)
    assert adapted["report"]["action"] is None
    assert outcome["finding_level"] == "evidence_insufficient"
    assert outcome["stop_reason"] == "no_new_actions"


def test_minimal_run_executes_exact_runtime_action_and_can_finish_without_another_tool(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch, store=False)
    original_read = MockConnector.read

    def clean_logs(self, tool, args):
        if tool == "service.logs":
            return {"connector": "mock", "text": "service started and ready"}
        return original_read(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", clean_logs)
    settings = get_settings()
    settings.agent_decision_protocol = "minimal_v2"
    settings.external_model_enabled = True
    settings.model_name = "minimal-test-stub"
    settings.model_api_key = SecretStr("test-minimal-key")
    contexts = []

    def completion(**kwargs):
        context = json.loads(kwargs["messages"][1]["content"])
        contexts.append(context)
        assert "runtime_bindings" not in context
        assert "runtime_id" not in json.dumps(context)
        candidates = context["available_actions"]
        assert candidates and all("arguments" not in action and "target_id" not in action for action in candidates)
        if len(contexts) == 1:
            selected = next(action for action in candidates if action["tool_id"] == "config.list")
            body = {"summary": "The API is stopped; inspect its reviewed configuration inventory next.",
                    "hypotheses": [], "next_investigation": "Read the authorized configuration inventory.",
                    "next_action_id": selected["action_id"], "finish": False, "finish_reason": None}
        else:
            body = {"summary": "Available evidence is sufficient for this bounded inspection.",
                    "hypotheses": [], "next_investigation": "No further check is requested.",
                    "next_action_id": None, "finish": True, "finish_reason": "bounded_check_complete"}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=64, completion_tokens=32),
        )

    monkeypatch.setattr("app.context.model._completion", completion)
    incident = client.post("/api/v1/incidents", json={"system_id": profile["system_id"],
                                                        "symptom": "catalog-api unavailable", "mode": "normal",
                                                        "task_type": "root_cause_investigation"})
    assert incident.status_code == 200
    queued = client.post(f"/api/v1/incidents/{incident.json()['id']}/runs")
    assert queued.status_code == 202
    job = wait_job(client, queued.json()["job_id"])
    run = client.get(f"/api/v1/runs/{job['result']['id']}").json()
    assert run["summary"]["decision_protocol"] == "minimal_v2"
    assert run["summary"]["stop_reason"] == "model_finished_without_useful_action"
    assert run["summary"]["task_completion_status"] == "stopped_with_gap"
    assert len(contexts) == 2
    assert sum(call["tool_id"] == "config.list" and call["status"] == "succeeded"
               for call in run["tool_calls"]) == 1


@pytest.mark.parametrize(("reported_state", "expect_override"), [("unknown", True), ("active", False)])
def test_task_finish_override_requires_a_non_refuted_current_claim(client, monkeypatch,
                                                                   reported_state, expect_override):
    profile = prepared_profile(client, monkeypatch, store=False)
    original_read = MockConnector.read

    def bounded_read(self, tool, args):
        if tool == "service.status":
            return {"connector": "mock", "state": reported_state}
        if tool == "service.logs":
            return {"connector": "mock", "text": "service started and ready"}
        if tool == "config.list":
            return {"connector": "mock", "files": []}
        return original_read(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", bounded_read)
    settings = get_settings()
    settings.agent_decision_protocol = "minimal_v2"
    settings.external_model_enabled = True
    settings.model_name = "minimal-finish-override-stub"
    settings.model_api_key = SecretStr("test-minimal-key")
    settings.investigation_model_rounds = 100
    settings.investigation_tool_calls = 500
    settings.task_rca_model_rounds = 12
    settings.task_rca_tool_calls = 20
    settings.investigation_no_progress_rounds = 5
    settings.investigation_minimum_useful_score = 3.5
    settings.investigation_high_value_score = 4.5
    contexts = []

    def completion(**kwargs):
        context = json.loads(kwargs["messages"][1]["content"])
        contexts.append(context)
        target_id = context["allowed_hypothesis_targets"][0]["node_id"]
        body = {
            "summary": "A relevant configuration check may resolve the current service hypothesis.",
            "hypotheses": [{
                "target_node_id": target_id,
                "failure_mode": "startup_failure",
                "reasoning": "A bounded configuration check can still test the active service hypothesis.",
                "missing_information": ["A relevant configuration observation is still missing."],
                "confidence": 0.4,
            }],
            "next_investigation": "No further check is requested by the model.",
            "next_action_id": None,
            "finish": True,
            "finish_reason": "model believes the current evidence is sufficient",
        }
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=64, completion_tokens=32),
        )

    monkeypatch.setattr("app.context.model._completion", completion)
    incident = client.post("/api/v1/incidents", json={
        "system_id": profile["system_id"], "symptom": "check catalog-api configuration",
        "mode": "normal", "task_type": "root_cause_investigation",
    })
    assert incident.status_code == 200, incident.text
    queued = client.post(f"/api/v1/incidents/{incident.json()['id']}/runs")
    assert queued.status_code == 202, queued.text
    job = wait_job(client, queued.json()["job_id"])
    run = client.get(f"/api/v1/runs/{job['result']['id']}").json()

    did_read_config = any(call["tool_id"] == "config.list" and call["status"] == "succeeded"
                          for call in run["tool_calls"])
    assert did_read_config is expect_override, {
                   "stop_reason": run["summary"].get("stop_reason"),
                   "task_completion_status": run["summary"].get("task_completion_status"),
                   "consecutive_no_progress_rounds": run["summary"].get("progress_summary", {}).get("consecutive_no_progress_rounds"),
                   "runtime_audit": run["summary"].get("runtime_decision_audit"),
                   "model_context_count": len(contexts),
                   "model_calls": run.get("context_budget", {}).get("calls"),
                   "actions": [(call["tool_id"], call["status"]) for call in run["tool_calls"]],
               }
    did_override_finish = any(item.get("runtime_action_override") == "high_value_check_for_active_hypothesis"
                              for item in run["summary"].get("runtime_decision_audit", []))
    assert did_override_finish is expect_override
    assert run["summary"]["gate"]["status"] == "unresolved"
    assert contexts, "the test must exercise the model finish recommendation"


def test_legacy_run_protocol_snapshot_without_field_uses_legacy():
    from app.agent.business_investigation import _decision_protocol_for_run

    assert _decision_protocol_for_run({"model_config_json": "{}"}, resumed=True) == "legacy_v22"


@pytest.mark.parametrize("snapshot", [
    '{"decision_protocol":"future_v99"}',
    "{malformed",
    '{"decision_protocol":[]}',
    '{"decision_protocol":{}}',
])
def test_unknown_or_malformed_run_protocol_snapshot_fails_closed(snapshot):
    from app.agent.business_investigation import _decision_protocol_for_run

    with pytest.raises(ValueError, match="Unsupported decision protocol"):
        _decision_protocol_for_run({"model_config_json": snapshot}, resumed=True)
