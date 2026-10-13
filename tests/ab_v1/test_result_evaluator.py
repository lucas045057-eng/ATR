import importlib.util
from copy import deepcopy

from app.agent.mechanisms import MechanismRegistry
from test_mechanisms import inputs


def evaluate(state, assessments):
    assert importlib.util.find_spec("app.agent.result_evaluator") is not None
    from app.agent.result_evaluator import ResultEvaluator
    return ResultEvaluator().evaluate(state, assessments)


def complete_state():
    facts, relations, scope = inputs()
    state = {"facts": facts, "graph": {"edges": relations}, "investigation_scope": scope,
             "required_rule_ids": ["dependency_unavailable_at_startup", "upstream_unavailable"],
             "report": {"model_unknowns": []}, "health_checks": []}
    registry = MechanismRegistry()
    assessments = [registry.verify(rule, facts, relations, scope) for rule in state["required_rule_ids"]]
    return state, assessments


def test_complete_chain_and_noncritical_unknowns_have_identical_support():
    state, assessments = complete_state()
    first = evaluate(state, assessments)
    state["report"]["model_unknowns"] = ["谁停止数据库未知"]
    second = evaluate(state, assessments)
    assert first["diagnosis_status"] == second["diagnosis_status"] == "root_cause_supported"
    assert second["completion_satisfied"] is True
    assert second["model_unknowns"] == ["谁停止数据库未知"]


def test_local_rule_or_claimed_complete_chain_cannot_finish_missing_proof():
    state, assessments = complete_state()
    assert evaluate(state, assessments[:1])["diagnosis_status"] == "partial"
    del assessments[1]["supports"]
    result = evaluate(state, assessments)
    assert result["diagnosis_status"] == "partial"
    assert result["completion_satisfied"] is False


def test_empty_unknown_array_cannot_hide_actual_endpoint_gap():
    state, assessments = complete_state()
    del state["graph"]["edges"][0]["attributes"]["endpoint_id"]
    assert evaluate(state, assessments)["diagnosis_status"] == "partial"


def test_model_omitted_counterevidence_still_blocks_complete_chain():
    state, assessments = complete_state()
    healthy = deepcopy(state["facts"][0])
    healthy.update(fact_id="healthy", evidence_id="e-healthy", call_id="c-healthy", value="running")
    state["facts"].append(healthy)
    result = evaluate(state, assessments)
    assert result["diagnosis_status"] == "partial"
    assert result["counterevidence"]


def test_individually_supported_segments_must_share_the_same_application():
    state, assessments = complete_state()
    assessments[1]["participants"]["upstream"] = "other-app"
    assert evaluate(state, assessments)["diagnosis_status"] == "partial"


def test_health_requires_all_predefined_checks_and_no_fault_evidence():
    state = {"facts": [], "required_rule_ids": [], "required_health_checks": ["app-state", "entry"],
             "health_checks": [{"check_id": "app-state", "status": "healthy", "evidence_id": "e-state"}],
             "current_evidence_ids": ["e-state", "e-http"]}
    assert evaluate(state, [])["diagnosis_status"] == "unresolved"
    state["health_checks"].append({"check_id": "entry", "status": "healthy", "evidence_id": "e-http"})
    assert evaluate(state, [])["diagnosis_status"] == "no_fault_observed"
