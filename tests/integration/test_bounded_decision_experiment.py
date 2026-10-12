"""No production hooks: verify the isolated semantic correction's hard boundaries."""

import pytest
from app.agent.model_schema import validate_report
from app.context.budget import BudgetLimits, ContextBudgetManager
from app.context.model import call_json
from app.context.model_errors import ModelCallError
from bounded_decision_experiment import one_scoped_correction
from test_model_isolation import fake_model  # noqa: F401
from test_target_reference_contract import API, DB, decision, graph_state


@pytest.mark.parametrize("fix", [True, False])
def test_only_one_scoped_regeneration_keeps_both_audits_and_strict_validation(fake_model, fix):  # noqa: F811
    db, settings, provider, _ = fake_model
    settings.model_timeout_seconds = 10
    state = {**graph_state(), "evidence": [
        {"evidence_id": "api-event", "target_node_id": API},
        {"evidence_id": "db-event", "target_node_id": DB},
    ], "available_actions": [{"action_id": "status", "tool_id": "container.status", "target_node_id": API}]}
    context = {"allowed_hypothesis_targets": [{"node_id": API}, {"node_id": DB}],
               "current_evidence": state["evidence"], "available_actions": state["available_actions"]}
    contexts = []

    def reply(data, number):
        contexts.append(data)
        output = decision(API)
        output["hypotheses"][0]["verification_predictions"] = [{
            "target_node_id": API, "tool_id": "container.status", "field": "state", "operator": "eq",
            "value": "exited" if number == 2 and fix else "unregistered-state",
        }]
        return output

    provider["reply"] = reply
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("investigation")
    audit = []
    invoke = one_scoped_correction(call_json, audit)
    if fix:
        output = invoke(db, settings, bid, "bounded", context, validate=lambda x: validate_report(x, state))
        assert output["hypotheses"][0]["verification_predictions"][0]["value"] == "exited"
    else:
        with pytest.raises(ModelCallError):
            invoke(db, settings, bid, "bounded", context, validate=lambda x: validate_report(x, state))
    usage = manager.get(bid)
    assert usage["calls"] == provider["requests"] == 2 and len(audit) == 1
    assert usage["reserved_tokens"] == 0 and usage["input_tokens"] == 22 and usage["output_tokens"] == 10
    assert [u["status"] for u in usage["usage_records"]] == ["failed", "succeeded" if fix else "failed"]
    assert len({u["id"] for u in usage["usage_records"]}) == 2
    assert all(u["diagnostic"]["retry_count"] == 0 for u in usage["usage_records"])
    assert contexts[1]["decision_correction"]["allowed_refutes"] == {API: ["api-event"], DB: ["db-event"]}
    assert "decision_correction" not in context


def test_correction_cannot_bypass_model_call_budget(fake_model):  # noqa: F811
    db, settings, provider, _ = fake_model
    settings.model_timeout_seconds = 10
    state = {**graph_state(), "evidence": [{"evidence_id": "db-event", "target_node_id": DB}]}
    output = decision(API)
    output["hypotheses"][0]["verification_predictions"] = [{
        "target_node_id": API, "tool_id": "container.status", "field": "state", "operator": "eq",
        "value": "unregistered-state",
    }]
    state["available_actions"] = [{"action_id": "status", "tool_id": "container.status", "target_node_id": API}]
    provider["reply"] = lambda *_: output
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("investigation", limits=BudgetLimits(max_calls=1))
    from app.context.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        one_scoped_correction(call_json, [])(db, settings, bid, "bounded", {}, validate=lambda x: validate_report(x, state))
    assert manager.get(bid)["calls"] == provider["requests"] == 1


def test_invalid_prediction_gets_one_regeneration_with_exact_value_candidates(fake_model):  # noqa: F811
    db, settings, provider, _ = fake_model
    settings.model_timeout_seconds = 10
    context = {"available_actions": [{"tool_id": "container.status"}], "allowed_hypothesis_targets": [{"node_id": API}]}
    state = {**graph_state(), "available_actions": context["available_actions"]}

    def reply(data, number):
        output = decision(API)
        output["hypotheses"][0]["verification_predictions"] = [{"target_node_id": API,
            "tool_id": "container.status", "field": "state", "operator": "eq",
            "value": "unregistered-state" if number == 1 else "exited"}]
        if number == 2:
            assert "exited" in data["decision_correction"]["prediction_rules"]["state"]["value_schema"]["enum"]
        return output

    provider["reply"] = reply
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("investigation")
    audit = []
    result = one_scoped_correction(call_json, audit)(db, settings, bid, "bounded", context, validate=lambda x: validate_report(x, state))
    assert result["hypotheses"][0]["verification_predictions"][0]["value"] == "exited"
    assert manager.get(bid)["calls"] == provider["requests"] == 2 and len(audit) == 1


@pytest.mark.parametrize("status", [500, 429, 401])
def test_transport_or_provider_failure_is_never_corrected(fake_model, status):  # noqa: F811
    db, settings, provider, _ = fake_model
    settings.model_timeout_seconds = 10
    provider["status"] = status
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("investigation")
    audit = []
    with pytest.raises(ModelCallError):
        one_scoped_correction(call_json, audit)(db, settings, bid, "bounded", {})
    assert manager.get(bid)["calls"] == provider["requests"] == 1 and not audit
