from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from app.agent.workflow import _reviewer
from test_baseline import gate_state


def hypothesis(state):
    return state["report"]["hypotheses"][0]


def status_prediction(**changes):
    return {"target_node_id": "app", "tool_id": "compose.status", "field": "state",
            "operator": "eq", "value": "exited", **changes}


def test_verified_startup_keeps_existing_minimum_gate_and_exposes_results():
    state = gate_state()
    hypothesis(state)["verification_predictions"] = [status_prediction()]
    result = _reviewer(state)["gate"]
    assert result["status"] == "supported"
    verified = result["hypotheses"][0]["verification"]
    assert verified["prediction_results"][0]["status"] == "verified"
    assert verified["classification"] == "possible_cause"
    assert verified["mechanism_verified"]


@pytest.mark.parametrize("line", ["ERROR unrelated backup failed", "startup succeeded; no error", "startup failed is a historical example"])
def test_unrelated_or_non_event_error_does_not_prove_startup_mechanism(line):
    state = gate_state()
    state["evidence"][0]["payload"]["text"] = datetime.now(timezone.utc).isoformat() + " " + line
    assert _reviewer(state)["gate"]["status"] == "unresolved"


@pytest.mark.parametrize("changes", [
    {"tool_id": "shell.exec"}, {"target_node_id": "not-current"}, {"field": "arbitrary"},
    {"operator": "execute"}, {"value": "running"},
])
def test_impossible_or_disproved_prediction_cannot_support(changes):
    state = gate_state()
    hypothesis(state)["verification_predictions"] = [status_prediction(**changes)]
    gate = _reviewer(state)["gate"]
    assert gate["status"] == "unresolved"
    assert gate["hypotheses"][0]["verification"]["prediction_results"][0]["status"] in {"infeasible", "refuted"}


def test_unreported_current_healthy_status_becomes_counterevidence():
    state = gate_state()
    healthy = deepcopy(state["evidence"][1])
    healthy.update(evidence_id="healthy", call_id="healthy-read")
    healthy["payload"]["containers"][0]["state"] = "running"
    state["evidence"].append(healthy)
    gate = _reviewer(state)["gate"]
    assert gate["status"] == "unresolved"
    assert any(link["evidence_id"] == "healthy" and link["relation"] == "refutes" for link in gate["hypotheses"][0]["evidence_links"])


@pytest.mark.parametrize("origin", ["same_call", "same_observation"])
def test_distinct_payloads_from_same_acquisition_are_not_independent(origin):
    state = gate_state()
    if origin == "same_call":
        state["evidence"][1]["call_id"] = state["evidence"][0]["call_id"]
    else:
        for entry in state["evidence"]:
            entry["payload"]["observation_id"] = "one-remote-observation"
    assert _reviewer(state)["gate"]["status"] == "unresolved"


def test_causal_event_after_observation_time_is_not_current_cause():
    state = gate_state()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    state["evidence"][0]["payload"]["text"] = future.isoformat() + " startup failed: connection refused"
    assert _reviewer(state)["gate"]["status"] == "unresolved"


@pytest.mark.parametrize("source", ["Static", "Pattern", "Experience"])
def test_relabelled_static_or_historical_input_cannot_prove_current_failure(source):
    state = gate_state()
    state["evidence"][0]["payload"]["source_type"] = source
    assert _reviewer(state)["gate"]["status"] == "unresolved"


def test_unknown_cause_is_a_candidate_even_with_high_confidence_and_valid_state():
    state = gate_state()
    hypothesis(state).update(failure_mode="cosmic_ray_corrupted_memory", confidence=1.0,
                             verification_predictions=[status_prediction()])
    gate = _reviewer(state)["gate"]
    assert gate["status"] == "unresolved"
    assert gate["hypotheses"][0]["verification"]["classification"] != "possible_cause"


@pytest.mark.parametrize("changes", [{"operator": []}, {"operator": {}}, {"tool_id": []}, {"target_node_id": {}}, {"value": {}}])
def test_malformed_prediction_is_infeasible_without_crashing_review(changes):
    state = gate_state()
    hypothesis(state)["verification_predictions"] = [status_prediction(**changes)]
    gate = _reviewer(state)["gate"]
    assert gate["status"] == "unresolved"
    assert gate["hypotheses"][0]["verification"]["prediction_results"][0]["status"] == "infeasible"
