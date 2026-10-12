"""Both model phases must advertise only decisions their strict validators accept."""

import json
from copy import deepcopy

import pytest
from app.agent import discovery
from app.agent.model_schema import output_contract, validate_report
from app.agent.workflow import _planner
from app.context.budget import ContextBudgetManager
from app.context.model_errors import ModelDecisionError, diagnostic
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_model_isolation import fake_model  # noqa: F401
from test_target_reference_contract import API, DB, decision, graph_state
from test_v22_adaptive import report


def mapping_context():
    return {
        "services": [{"name": "api", "role": "application"}, {"name": "db", "role": "datastore"}],
        "candidates": [
            {"candidate_key": "container-current", "kind": "container", "evidence_ids": ["e-api"]},
            {"candidate_key": "service-current", "kind": "service", "evidence_ids": ["e-db"]},
            {"candidate_key": "process-unverified", "kind": "process", "evidence_ids": ["e-pid"]},
            {
                "candidate_key": "process-verified",
                "kind": "process",
                "evidence_ids": ["e-process"],
                "business_identity_evidence": True,
            },
        ],
    }


def mapping_decision(kind="container", tools=None):
    evidence = {"container": "e-api", "service": "e-db", "process": "e-process"}[kind]
    return {
        "summary": "Synthetic metadata proposal, not authorization",
        "proposals": [
            {
                "service": "api",
                "role": "application",
                "candidate_key": kind + "-" + ("verified" if kind == "process" else "current"),
                "kind": kind,
                "evidence_ids": [evidence],
                "reasons": ["Metadata clue"],
                "counterevidence": [],
                "uncertainty": "Needs human review",
                "next_tools": tools or [],
            }
        ],
    }


def mapping_schema(context):
    return discovery.mapping_output_contract(context)


@pytest.mark.parametrize(
    "kind,tools",
    [
        ("container", []),
        ("container", ["container.list", "container.status", "container.logs"]),
        ("service", ["service.list", "service.status", "service.logs"]),
        ("process", ["process.list"]),
    ],
)
def test_valid_mapping_structure_agrees(kind, tools):
    context, data = mapping_context(), mapping_decision(kind, tools)
    schema = mapping_schema(context)
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(data)
    assert discovery.validate_mapping(data, context)["proposals"][0]["next_tools"] == tools


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_tool",
        "wrong_kind_tool",
        "unknown_candidate",
        "wrong_kind",
        "wrong_role",
        "unknown_service",
        "cross_candidate_evidence",
        "unverified_process",
        "duplicate_pair",
        "tool_object",
        "tool_scalar",
        "tool_null",
        "tool_number",
        "too_many_tools",
        "extra_action_id",
    ],
)
def test_mapping_schema_rejects_every_backend_invalid_combination(mutation):
    context, data = mapping_context(), mapping_decision("container", ["container.status"])
    p = data["proposals"][0]
    if mutation == "unknown_tool":
        p["next_tools"] = ["shell.exec"]
    elif mutation == "wrong_kind_tool":
        p["next_tools"] = ["service.status"]
    elif mutation == "unknown_candidate":
        p["candidate_key"] = "other-target-container"
    elif mutation == "wrong_kind":
        p["kind"] = "service"
    elif mutation == "wrong_role":
        p["role"] = "datastore"
    elif mutation == "unknown_service":
        p["service"] = "other-business"
    elif mutation == "cross_candidate_evidence":
        p["evidence_ids"] = ["e-db"]
    elif mutation == "unverified_process":
        p.update(
            candidate_key="process-unverified",
            kind="process",
            evidence_ids=["e-pid"],
            next_tools=["process.list"],
        )
    elif mutation == "duplicate_pair":
        data["proposals"].append(
            {**p, "reasons": ["Another explanation does not create a distinct mapping"]}
        )
    elif mutation == "tool_object":
        p["next_tools"] = [{"action_id": "invented", "tool_id": "container.status"}]
    elif mutation == "tool_scalar":
        p["next_tools"] = "container.status"
    elif mutation == "tool_null":
        p["next_tools"] = None
    elif mutation == "tool_number":
        p["next_tools"] = [1]
    elif mutation == "too_many_tools":
        p["next_tools"] = ["container.list"] * 7
    else:
        p["action_id"] = "invented"
    with pytest.raises((ValidationError, ModelDecisionError)):
        discovery.validate_mapping(data, context)
    assert not Draft202012Validator(mapping_schema(context)).is_valid(data)


def test_mapping_error_has_safe_specific_tool_category():
    data = mapping_decision("container", ["PRIVATE-INVALID-TOOL"])
    with pytest.raises(ModelDecisionError) as caught:
        discovery.validate_mapping(data, mapping_context())
    safe = diagnostic(caught.value, "validate")
    assert safe["validation_errors"] == [
        {"path": "proposals[0].next_tools", "type": "action_not_allowed", "stage": "validate"}
    ]
    assert "PRIVATE-" not in json.dumps(safe)


def test_hypothesis_node_and_role_are_one_schema_choice():
    data = decision(API, "datastore")
    with pytest.raises(ModelDecisionError):
        validate_report(data, graph_state())
    assert not Draft202012Validator(output_contract(True, graph_state())).is_valid(data)


def test_legacy_tool_and_target_are_one_schema_choice():
    state = {
        **graph_state(),
        "adaptive": False,
        "available_actions": [
            {"tool_id": "service.status", "target_node_id": API},
            {"tool_id": "container.logs", "target_node_id": DB},
        ],
    }
    data = report(
        action={
            "tool_id": "container.logs",
            "target_node_id": API,
            "rationale": "Invalid cross product",
        }
    )
    with pytest.raises(ModelDecisionError):
        validate_report(data, state)
    assert not Draft202012Validator(output_contract(False, state)).is_valid(data)
    data["action"]["target_node_id"] = DB
    assert Draft202012Validator(output_contract(False, state)).is_valid(data)
    validate_report(data, state)


def test_prediction_cannot_use_a_tool_absent_from_current_observations_and_candidates():
    state = {
        **graph_state(),
        "available_actions": [{"action_id": "status", "tool_id": "container.status"}],
    }
    data = decision(API)
    data["hypotheses"][0]["verification_predictions"] = [
        {
            "target_node_id": API,
            "tool_id": "service.logs",
            "field": "log_signal",
            "operator": "contains",
            "value": "startup failure",
        }
    ]
    assert not Draft202012Validator(output_contract(True, state)).is_valid(data)
    with pytest.raises(ModelDecisionError):
        validate_report(data, state)


def test_prompt_schema_uses_exactly_the_presented_action_pool(fake_model):  # noqa: F811
    database, settings, provider, _ = fake_model
    settings.investigation_input_tokens = 30000
    state = {
        **graph_state(),
        "adaptive": True,
        "incident": {"symptom": "Synthetic only"},
        "available_actions": [
            {"action_id": f"action-{i}", "tool_id": "container.status", "target_node_id": API}
            for i in range(15)
        ],
    }
    before = deepcopy(state)
    provider["reply"] = lambda context, _: report()
    result = _planner(
        settings, state, database, ContextBudgetManager(database, settings).create("investigation")
    )
    assert "model_error" not in result
    request = provider["wire"]
    context = json.loads(request["messages"][1]["content"])
    schema = json.loads(request["messages"][0]["content"].split("Schema: ", 1)[1])
    ids = schema["properties"]["action"]["anyOf"][0]["properties"]["action_id"]["enum"]
    assert set(ids) == {a["action_id"] for a in context["available_actions"]}
    assert state == before


@pytest.mark.parametrize(
    "mutation",
    [
        "hypotheses_null",
        "hypotheses_type",
        "hypotheses_capacity",
        "causal_chain_min",
        "causal_chain_max",
        "missing_evidence_type",
        "confidence_type",
        "confidence_range",
        "status_enum",
        "link_type",
        "link_relation",
        "link_unknown",
        "links_capacity",
        "predictions_capacity",
        "prediction_unknown_target",
        "prediction_operator",
        "prediction_value",
        "scope_requests_type",
        "unknown_action",
        "action_arguments",
        "extra_hypothesis_field",
        "extra_prediction_field",
        "wrong_role_nested",
    ],
)
def test_entire_investigation_contract_rejects_invalid_nested_fields(mutation):
    state = {
        **graph_state(),
        "adaptive": True,
        "evidence": [
            {
                "evidence_id": "api-e",
                "target_node_id": API,
                "tool_id": "container.status",
                "payload": {"state": "exited"},
            },
            {
                "evidence_id": "db-e",
                "target_node_id": DB,
                "tool_id": "container.status",
                "payload": {"state": "running"},
            },
        ],
        "available_actions": [
            {"action_id": "allowed", "tool_id": "container.logs", "target_node_id": API}
        ],
    }
    data = decision(API)
    h = data["hypotheses"][0]
    link = {"evidence_id": "api-e", "relation": "supports", "explanation": "Collected state"}
    p = {
        "target_node_id": API,
        "tool_id": "container.status",
        "field": "state",
        "operator": "eq",
        "value": "exited",
    }
    h.update(evidence_links=[link], verification_predictions=[p])
    if mutation == "hypotheses_null":
        data["hypotheses"] = None
    elif mutation == "hypotheses_type":
        data["hypotheses"] = {}
    elif mutation == "hypotheses_capacity":
        data["hypotheses"] *= 4
    elif mutation == "causal_chain_min":
        h["causal_chain"] = ["one"]
    elif mutation == "causal_chain_max":
        h["causal_chain"] = ["x"] * 13
    elif mutation == "missing_evidence_type":
        h["missing_evidence"] = "a list is required"
    elif mutation == "confidence_type":
        h["confidence"] = "0.5"
    elif mutation == "confidence_range":
        h["confidence"] = 1.01
    elif mutation == "status_enum":
        h["status"] = "confirmed"
    elif mutation == "link_type":
        h["evidence_links"] = ["api-e"]
    elif mutation == "link_relation":
        link["relation"] = "related"
    elif mutation == "link_unknown":
        link["evidence_id"] = "other-run"
    elif mutation == "links_capacity":
        h["evidence_links"] *= 25
    elif mutation == "predictions_capacity":
        h["verification_predictions"] *= 5
    elif mutation == "prediction_unknown_target":
        p["target_node_id"] = "ssh-target"
    elif mutation == "prediction_operator":
        p["operator"] = "contains"
    elif mutation == "prediction_value":
        p["value"] = True
    elif mutation == "scope_requests_type":
        data["scope_requests"] = ["db"]
    elif mutation == "unknown_action":
        data["action"] = {"action_id": "unknown", "rationale": "invalid"}
    elif mutation == "action_arguments":
        data["action"] = {"action_id": "allowed", "rationale": "invalid", "arguments": {}}
    elif mutation == "extra_hypothesis_field":
        h["other"] = True
    elif mutation == "wrong_role_nested":
        h["target_role"] = "datastore"
    else:
        p["other"] = True
    from app.context.model_errors import ModelCallError

    with pytest.raises((ValidationError, ModelDecisionError, ModelCallError)):
        validate_report(data, state)
    assert not Draft202012Validator(output_contract(True, state)).is_valid(data)


def test_mapping_candidate_budget_trimming_rebuilds_the_exact_contract():
    context = mapping_context()
    context["candidates"] = [
        {
            "candidate_key": "candidate-" + str(i) + "x" * 200,
            "kind": "container",
            "evidence_ids": ["e-" + str(i)],
            "allowed_next_tools": ["container.list", "container.status", "container.logs"],
        }
        for i in range(80)
    ]
    before = deepcopy(context)
    prompt, bounded = discovery.mapping_model_input(context, 15000)
    from app.context.budget import estimate_model_input
    from app.context.model import model_messages

    assert 0 < len(bounded["candidates"]) < len(context["candidates"])
    assert estimate_model_input(model_messages(prompt, bounded)) <= 15000
    schema = json.loads(prompt.split("只返回契约中的 JSON：", 1)[1])
    Draft202012Validator.check_schema(schema)
    assert set(schema["$defs"]["MappingProposal"]["properties"]["candidate_key"]["enum"]) == {
        c["candidate_key"] for c in bounded["candidates"]
    }
    assert context == before


def test_mapping_respects_a_restricted_docker_capability_without_granting_reads():
    context = mapping_context()
    c = context["candidates"][0]
    c["allowed_next_tools"] = sorted(
        discovery.mapping_tools(
            c, {"docker_reads_enabled": True, "docker_reader": "restricted_list"}
        )
    )
    assert c["allowed_next_tools"] == ["container.list"]
    data = mapping_decision("container", ["container.list"])
    assert Draft202012Validator(mapping_schema(context)).is_valid(data)
    discovery.validate_mapping(data, context)
    data["proposals"][0]["next_tools"] = ["container.logs"]
    assert not Draft202012Validator(mapping_schema(context)).is_valid(data)
    with pytest.raises(ModelDecisionError):
        discovery.validate_mapping(data, context)


def test_updated_candidates_and_no_actions_remain_strict():
    context = mapping_context()
    data = mapping_decision("container", ["container.status"])
    context["candidates"] = context["candidates"][1:]
    assert not Draft202012Validator(mapping_schema(context)).is_valid(data)
    with pytest.raises(ModelDecisionError):
        discovery.validate_mapping(data, context)
    state = {**graph_state(), "adaptive": True, "available_actions": []}
    final = report()
    assert Draft202012Validator(output_contract(True, state)).is_valid(final)
    validate_report(final, state)
    final["action"] = {"action_id": "expired", "rationale": "Rejected"}
    assert not Draft202012Validator(output_contract(True, state)).is_valid(final)
    with pytest.raises(ModelDecisionError):
        validate_report(final, state)


@pytest.mark.parametrize(
    "value,expected",
    [
        (200, True),
        (200.0, True),
        (100.0, True),
        (599.0, True),
        (200.5, False),
        ("200", False),
        (True, False),
        (99.0, False),
        (600.0, False),
    ],
)
def test_json_integer_prediction_schema_and_validator_agree(value, expected):
    state = {
        **graph_state(),
        "available_actions": [{"tool_id": "health.get", "action_id": "health"}],
    }
    data = decision(API)
    data["hypotheses"][0]["verification_predictions"] = [
        {
            "target_node_id": API,
            "tool_id": "health.get",
            "field": "status_code",
            "operator": "eq",
            "value": value,
        }
    ]
    assert Draft202012Validator(output_contract(True, state)).is_valid(data) == expected
    if expected:
        parsed = validate_report(data, state)
        assert type(parsed["hypotheses"][0]["verification_predictions"][0]["value"]) is int
    else:
        with pytest.raises((ValidationError, ModelDecisionError)):
            validate_report(data, state)


def test_json_integer_variant_crosses_local_worker_then_strict_validation(fake_model):  # noqa: F811
    from app.context.model import call_json

    database, settings, provider, _ = fake_model
    state = {
        **graph_state(),
        "available_actions": [{"tool_id": "health.get", "action_id": "health"}],
    }
    data = decision(API)
    data["hypotheses"][0]["verification_predictions"] = [
        {
            "target_node_id": API,
            "tool_id": "health.get",
            "field": "status_code",
            "operator": "eq",
            "value": 200.0,
        }
    ]
    provider["reply"] = lambda *_: data
    manager = ContextBudgetManager(database, settings)
    bid = manager.create("investigation")
    parsed = call_json(
        database,
        settings,
        bid,
        "Synthetic local JSON integer notation test",
        {},
        validate=lambda value: validate_report(value, state),
    )
    assert type(parsed["hypotheses"][0]["verification_predictions"][0]["value"]) is int
    assert parsed["hypotheses"][0]["verification_predictions"][0]["value"] == 200
    assert (
        manager.get(bid)["usage_records"][0]["status"] == "succeeded" and provider["requests"] == 1
    )


def test_complete_discovery_review_resume_and_multiround_worker_contract(
    client,
    fake_model,  # noqa: F811
    monkeypatch,
):
    from app.connectors.mock import MockConnector
    from conftest import wait_job
    from test_autonomous_discovery import mapping, start, unbound
    from test_model_isolation import configure_client
    from test_v22_adaptive import candidate, hypothesis, proposal

    original_read = MockConnector.read

    def fixture_services(self, tool, arguments):
        payload = original_read(self, tool, arguments)
        if tool == "service.list":
            payload["services"].append({"id": "store.service", "name": "store", "kind": "service"})
        return payload

    monkeypatch.setattr(MockConnector, "read", fixture_services)
    profile, target = unbound(client)
    _, settings, provider, _ = fake_model
    configure_client(settings)
    from app.settings import get_settings

    get_settings().investigation_input_tokens = 30000
    contexts, agreements = [], []

    def reply(context, _):
        contexts.append(context)
        if context.get("phase") == "discovery":
            data = mapping(context)
            store = next(
                c for c in context["candidates"] if c["kind"] == "service" and c["name"] == "store"
            )
            role = next(s["role"] for s in context["services"] if s["name"] == "store")
            p = {
                **data["proposals"][0],
                "service": "store",
                "role": role,
                "candidate_key": store["candidate_key"],
                "evidence_ids": store["evidence_ids"],
            }
            data["proposals"].append(p)
            schema = json.loads(
                provider["wire"]["messages"][0]["content"].split("只返回契约中的 JSON：", 1)[1]
            )
        else:
            number = sum(c.get("phase") != "discovery" for c in contexts)
            h = hypothesis(context, "catalog-api")
            h["missing_evidence"] = ["No confirmed causal result in this synthetic normal sample"]
            data = (
                report([h], proposal(candidate(context, "service.status", "store")))
                if number == 1
                else (
                    report([h], proposal(candidate(context, "service.logs", "store")))
                    if number == 2
                    else report()
                )
            )
            schema = json.loads(provider["wire"]["messages"][0]["content"].split("Schema: ", 1)[1])
        agreements.append(Draft202012Validator(schema).is_valid(data))
        return data

    provider["reply"] = reply
    first = start(client, profile)
    assert first["status"] == "awaiting_review" and len(contexts) == 1
    assert first["context_budget"]["tool_calls"] == 7
    assert not any(call["tool_id"].endswith(".logs") for call in first["tool_calls"])
    base = f"/api/v1/businesses/{profile['business_id']}"
    for p in first["summary"]["mapping_proposals"]:
        client.post(
            base + f"/bindings/{p['binding_id']}/review",
            json={
                "decision": "confirm",
                "reason": "Human review of isolated fixture, no production authorization",
            },
        ).raise_for_status()
    resumed = client.post(f"/api/v1/runs/{first['id']}/resume")
    assert resumed.status_code == 202, resumed.text
    job = wait_job(client, resumed.json()["job_id"], timeout=40)
    assert job["status"] == "completed", job
    final = client.get(f"/api/v1/runs/{first['id']}").json()
    assert (
        final["target_id"] == target["id"]
        and final["context_budget"]["id"] == first["context_budget"]["id"]
    )
    assert len(contexts) == provider["requests"] == 4 and all(agreements)
    assert final["summary"]["model_error"] is None and final["status"] == "unresolved"
    assert len(final["tool_calls"]) == final["context_budget"]["tool_calls"] == 11
    assert all(call["status"] == "succeeded" for call in final["tool_calls"])
    assert all(u["status"] == "succeeded" for u in final["context_budget"]["usage_records"])
    assert {e["evidence_id"] for e in contexts[1]["current_evidence"]}.isdisjoint(
        first["summary"]["discovery_evidence_ids"]
    )
