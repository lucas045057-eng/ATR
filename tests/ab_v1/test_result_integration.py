from datetime import datetime, timedelta, timezone

from app.agent.decision_runtime import classify_outcome
from app.agent.workflow import _reviewer
from test_baseline import gate_state


def test_ab_reviewer_never_promotes_a_local_supported_gate_to_complete_root():
    state = gate_state()
    state["decision_protocol"] = "ab_v1"
    state["scope_node_ids"] = ["app"]
    result = _reviewer(state)
    assert result["diagnosis_result"]["diagnosis_status"] == "partial"
    assert result["gate"]["status"] != "supported"


def test_ab_classification_ignores_legacy_gate_success():
    state = gate_state()
    state.update(decision_protocol="ab_v1", scope_node_ids=["app"], available_actions=[{"action_id": "db-check"}])
    result = classify_outcome(state, {"status": "supported"})
    assert result["root_cause_confirmed"] is False
    assert result["should_stop"] is False
    assert result["diagnosis_status"] == "partial"


def test_current_http_observation_becomes_a_cited_fact():
    from app.agent.decision_runtime import facts_from_state
    now = datetime.now(timezone.utc)
    state = {"graph": {"nodes": [{"id": "proxy", "kind": "service", "name": "proxy"}]},
             "incident": {"window_start": (now - timedelta(minutes=15)).isoformat()},
             "evidence": [{"evidence_id": "entry", "call_id": "read-http", "target_node_id": "proxy",
                           "tool_id": "health.get", "observed_at": now.isoformat(), "payload": {"status_code": 502}}]}
    facts = facts_from_state(state)
    assert facts[0]["kind"] == "http_status"
    assert facts[0]["value"] == 502
    assert facts[0]["observed_on"] == "proxy"


def test_fact_extraction_preserves_shared_observation_identity():
    from app.agent.decision_runtime import facts_from_state
    state = gate_state()
    for evidence in state["evidence"]:
        evidence["payload"]["observation_id"] = "same-acquisition"
    facts = facts_from_state(state)
    assert facts and all(fact["observation_id"] == "same-acquisition" for fact in facts)


def test_ab_http_without_remote_endpoint_identity_cannot_complete_a_chain():
    from app.agent.decision_runtime import facts_from_state
    instant = datetime.now(timezone.utc)
    state = {'decision_protocol':'ab_v1','graph':{'nodes':[{'id':'entry','kind':'service','name':'entry'}]},
             'incident':{'window_start':(instant-timedelta(minutes=15)).isoformat()},
             'evidence':[{'evidence_id':'e','call_id':'c','target_node_id':'entry','tool_id':'health.get',
                          'observed_at':instant.isoformat(),'payload':{'status_code':502}}]}
    assert facts_from_state(state)[0]['freshness'] == 'unverified_observer'
