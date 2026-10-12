"""Finite prompt references and accurate credential failure across real Run orchestration."""

import json
from copy import deepcopy

import pytest
from app.agent.model_schema import output_contract, validate_report
from app.agent.verification import MECHANISMS, prediction_error_field
from app.agent.workflow import _compact_model_context, _reviewer
from app.connectors.credentials import CredentialUnavailable
from app.connectors.mock import MockConnector
from app.context.budget import estimate_model_input
from app.context.model import model_messages
from app.context.model_errors import ModelDecisionError, diagnostic
from test_baseline import gate_state
from test_target_reference_contract import API, DATASTORE, DB, decision, graph_state
from test_v22_adaptive import (
    candidate,
    hypothesis,
    investigate,
    model_stub,
    prepared_profile,
    proposal,
    report,
)


def test_schema_constrains_root_nested_evidence_and_action_references():
    state = {
        **graph_state(),
        "adaptive": True,
        "evidence": [{"evidence_id": "e-current"}],
        "available_actions": [{"action_id": "a-current"}],
    }
    schema = output_contract(True, state)
    definitions = schema["$defs"]
    assert all(key in definitions["Hypothesis"]["properties"]["failure_mode"]["description"]
               for key in MECHANISMS)
    assert set(definitions["AllowedServiceNodeId"]["enum"]) == {API, DB}
    for name in ("Hypothesis", "Prediction"):
        assert definitions[name]["properties"]["target_node_id"] == {
            "$ref": "#/$defs/AllowedServiceNodeId"
        }
    assert definitions["AllowedEvidenceId"]["enum"] == ["e-current"]
    assert definitions["EvidenceLink"]["properties"]["evidence_id"] == {
        "$ref": "#/$defs/AllowedEvidenceId"
    }
    assert schema["properties"]["action"]["anyOf"][0]["properties"]["action_id"]["enum"] == [
        "a-current"
    ]


def test_empty_reference_sets_require_empty_hypotheses_links_and_null_action():
    schema = output_contract(True, {"graph": {"nodes": []}})
    assert schema["$defs"]["AllowedServiceNodeId"] == {"not": {}}
    assert schema["$defs"]["AllowedEvidenceId"] == {"not": {}}
    assert schema["properties"]["action"]["anyOf"][0]["properties"]["action_id"] == {"not": {}}


@pytest.mark.parametrize("state", [{"graph": {"nodes": []}}, graph_state()])
def test_schema_without_observable_predictions_is_valid_and_requires_empty_predictions(state):
    from jsonschema import Draft202012Validator

    schema = output_contract(True, state)
    Draft202012Validator.check_schema(schema)
    data = decision(API) if state["graph"]["nodes"] else {"summary": "No nodes", "hypotheses": [], "next_action": None, "action": None}
    assert Draft202012Validator(schema).is_valid(data)


def test_business_predictions_do_not_advertise_unobservable_listening_or_health():
    state = {**graph_state(), "evidence": [
        {"evidence_id": "status", "tool_id": "container.status", "payload": {"state": "exited"}},
        {"evidence_id": "log", "tool_id": "container.logs", "payload": {"text": "startup failed"}},
        {"evidence_id": "network", "tool_id": "network.listeners", "payload": {"listeners": []}},
    ], "available_actions": [{"action_id": "cfg", "tool_id": "config.read"}]}
    prediction = output_contract(True, state)["$defs"]["Prediction"]["properties"]
    assert set(prediction["tool_id"]["enum"]) == {"container.status", "container.logs"}
    assert set(prediction["field"]["enum"]) == {"state", "log_signal"}
    state["available_actions"].append({"action_id": "health", "tool_id": "health.get"})
    assert "status_code" in output_contract(True, state)["$defs"]["Prediction"]["properties"]["field"]["enum"]
    state["evidence"][-1]["payload"] = {"listening": False}
    assert "listening" in output_contract(True, state)["$defs"]["Prediction"]["properties"]["field"]["enum"]


def test_compaction_leaves_headroom_without_losing_reference_metadata():
    context = {
        "previous_hypotheses": [],
        "hypothesis_history": [],
        "allowed_hypothesis_targets": [{"node_id": API, "service": "api", "role": "application"}],
        "current_evidence": [
            {
                "evidence_id": "e-1",
                "target_node_id": API,
                "payload": {"text": "x" * 9000, "source_type": "Runtime"},
            }
        ],
    }
    assert 9000 < estimate_model_input(model_messages("bounded", context)) < 10000
    before = deepcopy(context)
    compacted = _compact_model_context("bounded", context, 10000)
    assert estimate_model_input(model_messages("bounded", compacted)) <= 9000
    assert compacted["allowed_hypothesis_targets"] == before["allowed_hypothesis_targets"]
    assert compacted["current_evidence"][0]["evidence_id"] == "e-1"
    assert compacted["current_evidence"][0]["payload"]["source_type"] == "Runtime"
    assert compacted["current_evidence"][0]["payload"]["truncated"]


@pytest.mark.parametrize(
    "bad", ["api", "a" * 64, "ssh-target", "action-id", "hypothesis-id", DATASTORE]
)
def test_every_nested_prediction_rejects_other_identifier_kinds(bad):
    data = decision(API)
    data["hypotheses"][0]["verification_predictions"] = [
        {
            "target_node_id": DB,
            "tool_id": "container.status",
            "field": "state",
            "operator": "eq",
            "value": "running",
        },
        {
            "target_node_id": bad,
            "tool_id": "container.status",
            "field": "state",
            "operator": "eq",
            "value": "running",
        },
    ]
    with pytest.raises(ModelDecisionError) as error:
        validate_report(data, graph_state())
    assert error.value.path == ("hypotheses", 0, "verification_predictions", 1, "target_node_id")
    assert error.value.error_type == "unknown_prediction_target"
    assert bad not in str(error.value)


def test_credential_expiry_is_not_reported_as_server_permission(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    original = MockConnector.read
    reads = 0

    def expire(self, tool, args):
        nonlocal reads
        reads += 1
        if reads == 3:
            raise CredentialUnavailable()
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", expire)
    contexts = model_stub(
        monkeypatch,
        lambda context, _: report(action=proposal(candidate(context, "service.status", "store"))),
    )
    result = investigate(client, profile, mode="graph_adaptive")
    assert result["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == "ssh_credential_unavailable"
    assert "重新" in result["summary"]["unresolved_reason"]
    assert len(result["tool_calls"]) == 3 and len(contexts) == 1
    assert result["tool_calls"][-1]["error_code"] == "ssh_credential_unavailable"
    assert len(result["summary"]["evidence_ids"]) == 2
    assert result["tool_calls"][-1]["status"] == "denied"
    assert "服务器权限拒绝" not in json.dumps(result["summary"], ensure_ascii=False)


@pytest.mark.parametrize("relation,evidence_id", [("supports", "db-state"), ("refutes", "api-state")])
def test_direct_refutations_and_dependency_support_remain_legal(relation, evidence_id):
    state = {**graph_state(), "evidence": [
        {"evidence_id": "api-state", "target_node_id": API},
        {"evidence_id": "db-state", "target_node_id": DB},
    ]}
    data = decision(API)
    data["hypotheses"][0]["evidence_links"] = [
        {"evidence_id": evidence_id, "relation": relation, "explanation": "Synthetic scoped evidence"}
    ]
    assert validate_report(data, state)["hypotheses"][0]["evidence_links"][0]["relation"] == relation


@pytest.mark.parametrize("target,relation,evidence_id,expected", [
    (API, "refutes", "api-state", True),
    (DB, "refutes", "db-state", True),
    (API, "supports", "db-state", True),
    (API, "refutes", "db-state", False),
    (DB, "refutes", "api-state", False),
    (API, "refutes", "unscoped", False),
])
def test_prompt_schema_binds_refutation_to_hypothesis_target(target, relation, evidence_id, expected):
    from jsonschema import Draft202012Validator

    state = {**graph_state(), "evidence": [
        {"evidence_id": "api-state", "target_node_id": API},
        {"evidence_id": "db-state", "target_node_id": DB},
        {"evidence_id": "unscoped", "target_node_id": None},
    ]}
    data = decision(target, "application" if target == API else "datastore")
    data["hypotheses"][0]["evidence_links"] = [
        {"evidence_id": evidence_id, "relation": relation, "explanation": "Synthetic scoped evidence"}
    ]
    assert Draft202012Validator(output_contract(True, state)).is_valid(data) == expected
    if expected:
        validate_report(data, state)
    else:
        with pytest.raises(ModelDecisionError) as caught:
            validate_report(data, state)
        assert caught.value.error_type == "counterevidence_target_mismatch"


def test_dependency_running_cannot_be_api_counterevidence():
    state = {**graph_state(), "evidence": [{"evidence_id": "PRIVATE-DB-EVIDENCE", "target_node_id": DB}]}
    data = decision(API)
    data["hypotheses"][0]["evidence_links"] = [{
        "evidence_id": "PRIVATE-DB-EVIDENCE", "relation": "refutes",
        "explanation": "The database is running; this does not contradict the application's endpoint fault.",
    }]
    with pytest.raises(ModelDecisionError) as caught:
        validate_report(data, state)
    error = diagnostic(caught.value, "validate")
    assert error["validation_errors"] == [{
        "path": "hypotheses[0].evidence_links[0].relation", "type": "counterevidence_target_mismatch", "stage": "validate",
    }]
    assert "PRIVATE-DB-EVIDENCE" not in json.dumps(error)


def test_cross_target_counterevidence_stops_before_next_tool(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)

    def reply(context, number):
        if number == 1:
            return report(action=proposal(candidate(context, "service.status", "store")))
        invalid = hypothesis(context, "catalog-api", failure="database_unavailable")
        db = next(e for e in context["current_evidence"] if e["target_node_id"] != invalid["target_node_id"])
        invalid["evidence_links"] = [{"evidence_id": db["evidence_id"], "relation": "refutes",
                                     "explanation": "Dependency state is not an application endpoint refutation."}]
        return report([invalid], proposal(candidate(context, "config.list")))

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile, mode="graph_adaptive")
    assert len(contexts) == 2 and len(result["tool_calls"]) == 3
    assert result["status"] == "unresolved"
    error = result["summary"]["model_error"]
    assert error["validation_errors"][0]["type"] == "counterevidence_target_mismatch"
    assert all(t["tool_id"] != "config.list" for t in result["tool_calls"])
    assert [u["status"] for u in result["context_budget"]["usage_records"]] == ["succeeded", "failed"]


def test_verified_failure_with_declared_missing_cause_is_not_confirmed():
    state = gate_state()
    state["report"]["hypotheses"][0]["missing_evidence"] = ["Dependency runtime and reviewed endpoint configuration"]
    gate = _reviewer(state)["gate"]
    assert gate["status"] == "unresolved"
    verified = gate["hypotheses"][0]["verification"]
    assert verified["mechanism_verified"] and not verified["passed"]
    assert "Dependency runtime and reviewed endpoint configuration" in verified["missing_evidence"]


@pytest.mark.parametrize("continue_check", [True, False])
def test_pending_cause_checks_are_not_skipped_by_verified_startup(client, monkeypatch, continue_check):
    profile = prepared_profile(client, monkeypatch)
    original = MockConnector.read

    def failed_api(self, tool, args):
        if tool == "service.status" and args.get("name") == "catalog-api.service":
            return {"connector": "mock", "state": "failed"}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", failed_api)

    def reply(context, number):
        item = hypothesis(context, "catalog-api", failure="startup_failure", all_evidence=True)
        item["missing_evidence"] = ["Dependency runtime check is needed before concluding cause"] if number == 1 else []
        action = proposal(candidate(context, "service.status", "store")) if number == 1 and continue_check else None
        return report([item], action)

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile, mode="graph_adaptive")
    assert result["summary"]["iterations"][0]["gate"]["status"] == "unresolved"
    assert result["status"] == ("supported" if continue_check else "unresolved")
    assert len(contexts) == (2 if continue_check else 1)
    assert len(result["tool_calls"]) == (3 if continue_check else 2)
    assert result["summary"]["stop_reason"] == ("evidence_supported" if continue_check else "no_new_actions")


@pytest.mark.parametrize("tool,field,operator,value", [
    ("container.status", "state", "eq", "exited"),
    ("container.status", "state", "ne", "running"),
    ("container.status", "state", "eq", "ready"),
    ("container.status", "state", "eq", True),
    ("container.status", "state", "contains", "exited"),
    ("container.logs", "state", "eq", "exited"),
    ("health.get", "status_code", "eq", 100),
    ("health.get", "status_code", "ne", 599),
    ("health.get", "status_code", "eq", 99),
    ("health.get", "status_code", "eq", 600),
    ("health.get", "status_code", "eq", True),
    ("health.get", "status_code", "eq", "200"),
    ("network.listeners", "listening", "eq", True),
    ("network.listeners", "listening", "ne", False),
    ("network.listeners", "listening", "eq", 1),
    ("container.logs", "log_signal", "contains", "listening on 5432"),
    ("container.logs", "log_signal", "contains", "x" * 160),
    ("container.logs", "log_signal", "contains", "x" * 161),
    ("container.logs", "log_signal", "eq", "listening on 5432"),
    ("container.logs", "log_signal", "contains", "FAILED"),
    ("container.logs", "log_signal", "contains", "Exception"),
    ("container.logs", "log_signal", "contains", "error"),
    ("container.logs", "log_signal", "contains", "tracebacK"),
    ("container.logs", "log_signal", "contains", "traceback\n"),
])
def test_prediction_prompt_schema_and_validator_share_exact_combinations(tool, field, operator, value):
    from jsonschema import Draft202012Validator

    state = {**graph_state(), "evidence": [
        {"evidence_id": "net", "tool_id": "network.listeners", "payload": {"listening": True}},
    ], "available_actions": [
        {"action_id": t, "tool_id": t} for t in ("container.status", "container.logs", "health.get")
    ]}
    p = {"target_node_id": API, "tool_id": tool, "field": field, "operator": operator, "value": value}
    data = decision(API)
    data["hypotheses"][0]["verification_predictions"] = [p]
    expected = prediction_error_field(p) is None
    assert Draft202012Validator(output_contract(True, state)).is_valid(data) == expected
