import importlib.util
from copy import deepcopy

import pytest
from app.agent.model_schema import output_contract
from app.agent.verification import MECHANISMS


def registry():
    assert importlib.util.find_spec("app.agent.mechanisms") is not None
    from app.agent.mechanisms import MechanismRegistry
    return MechanismRegistry()


def inputs():
    facts = [
        {"fact_id": "db-state", "kind": "service_state", "target_node_id": "db", "value": "inactive", "evidence_id": "e-db", "call_id": "c-db", "freshness": "current"},
        {"fact_id": "app-state", "kind": "service_state", "target_node_id": "app", "value": "failed", "evidence_id": "e-app", "call_id": "c-app", "freshness": "current", "startup_cycle": "boot-2"},
        {"fact_id": "app-log", "kind": "log_observation", "target_node_id": "app", "value": {"mechanisms": ["dependency_unavailable_at_startup"]}, "evidence_id": "e-log", "call_id": "c-log", "freshness": "current", "startup_cycle": "boot-2"},
        {"fact_id": "proxy-log", "kind": "log_observation", "target_node_id": "proxy", "value": {"mechanisms": ["upstream_unavailable"]}, "evidence_id": "e-proxy", "call_id": "c-proxy", "freshness": "current"},
        {"fact_id": "entry", "kind": "http_status", "target_node_id": "proxy", "value": 502, "evidence_id": "e-http", "call_id": "c-http", "freshness": "current"},
    ]
    relations = [
        {"from_node_id": "app", "to_node_id": "db", "relation": "depends_on", "attributes": {"verification_status": "confirmed", "endpoint_id": "mysql:3306", "source_refs": ["config:4"]}},
        {"from_node_id": "proxy", "to_node_id": "app", "relation": "depends_on", "attributes": {"verification_status": "confirmed", "endpoint_id": "app:8000", "source_refs": ["nginx:7"]}},
    ]
    scope = {"target_node_ids": ["db", "app", "proxy"], "current_cycles": {"app": "boot-2"}}
    return facts, relations, scope


def test_new_modes_are_executable_and_accepted_by_existing_schema():
    assert {"dependency_unavailable_at_startup", "upstream_unavailable"} <= set(MECHANISMS)
    contract = output_contract(True, {"decision_protocol": "ab_v1"})
    modes = contract["$defs"]["Hypothesis"]["properties"]["failure_mode"]["enum"]
    assert {"dependency_unavailable_at_startup", "upstream_unavailable"} <= set(modes)


def test_current_consumer_log_and_normal_stopped_dependency_support_startup():
    facts, relations, scope = inputs()
    result = registry().verify("dependency_unavailable_at_startup", facts, relations, scope)
    assert result["status"] == "supported"
    assert result["required_gaps"] == []
    assert result["participants"] == {"consumer": "app", "dependency": "db"}
    assert {item["observed_on"] for item in result["supports"]} == {"db", "app"}


@pytest.mark.parametrize("change", ["wrong_database", "missing_endpoint", "old_cycle", "copied_call", "healthy_dependency", "request_only"])
def test_missing_or_contradictory_facts_cannot_complete_rule(change):
    facts, relations, scope = deepcopy(inputs())
    if change == "wrong_database":
        facts[0]["target_node_id"] = "other-db"
    elif change == "missing_endpoint":
        del relations[0]["attributes"]["endpoint_id"]
    elif change == "old_cycle":
        facts[2]["startup_cycle"] = "boot-1"
    elif change == "copied_call":
        facts[2]["call_id"] = facts[1]["call_id"]
    elif change == "healthy_dependency":
        facts.append({**facts[0], "fact_id": "restored", "evidence_id": "e-restored", "call_id": "c-restored", "value": "running"})
    else:
        facts[2]["value"]["mechanisms"] = ["database_unavailable"]
    result = registry().verify("dependency_unavailable_at_startup", facts, relations, scope)
    assert result["status"] != "supported"
    assert result["required_gaps"] or result["counterevidence"]


def test_running_proxy_propagates_502_without_needing_proxy_failure():
    facts, relations, scope = inputs()
    facts.append({**facts[1], "target_node_id": "proxy", "fact_id": "proxy-state", "value": "running"})
    result = registry().verify("upstream_unavailable", facts, relations, scope)
    assert result["status"] == "supported"


def test_proxy_rule_requires_current_502_and_authorized_target():
    facts, relations, scope = inputs()
    facts[-1]["value"] = 503
    assert registry().verify("upstream_unavailable", facts, relations, scope)["status"] != "supported"
    facts[-1]["value"] = 502
    scope["target_node_ids"].remove("app")
    assert registry().verify("upstream_unavailable", facts, relations, scope)["status"] != "supported"
