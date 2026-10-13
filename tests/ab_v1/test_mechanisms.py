import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from app.agent.model_schema import output_contract
from app.agent.verification import MECHANISMS
from app.agent.workflow import _reviewer
from test_baseline import gate_state


def registry():
    assert importlib.util.find_spec("app.agent.mechanisms") is not None
    from app.agent.mechanisms import MechanismRegistry
    return MechanismRegistry()


def inputs():
    facts = [
        {"fact_id": "db-state", "kind": "service_state", "target_node_id": "db", "value": "inactive", "evidence_id": "e-db", "call_id": "c-db", "freshness": "current"},
        {"fact_id": "app-state", "kind": "service_state", "target_node_id": "app", "value": "failed", "evidence_id": "e-app", "call_id": "c-app", "freshness": "current", "startup_cycle": "boot-2"},
        {"fact_id": "app-log", "kind": "log_observation", "target_node_id": "app", "value": {"mechanisms": ["dependency_unavailable_at_startup"], "endpoint_ids": ["mysql:3306"]}, "evidence_id": "e-log", "call_id": "c-log", "freshness": "current", "startup_cycle": "boot-2"},
        {"fact_id": "proxy-log", "kind": "log_observation", "target_node_id": "proxy", "value": {"mechanisms": ["upstream_unavailable"], "endpoint_ids": ["app:8000"]}, "evidence_id": "e-proxy", "call_id": "c-proxy", "freshness": "current"},
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


def test_legacy_single_target_gate_cannot_approve_new_cross_target_mode():
    state = gate_state()
    state["report"]["hypotheses"][0]["failure_mode"] = "dependency_unavailable_at_startup"
    stamp = state["evidence"][0]["observed_at"]
    state["evidence"][0]["payload"]["text"] = stamp + " startup database connection refused"
    assert _reviewer(state)["gate"]["status"] != "supported"


@pytest.mark.parametrize('rule,lines,edge_index', [
    ('dependency_unavailable_at_startup',['startup database connect failed other-db:3306',
                                         'background health check connection refused mysql:3306'],0),
    ('upstream_unavailable',['upstream connection refused other-app:8000',
                            'background health check connection refused app:8000'],1),
])
def test_unrelated_log_lines_cannot_supply_endpoint_for_another_mechanism(rule,lines,edge_index):
    from app.agent.decision_runtime import facts_from_state
    facts,relations,scope=inputs()
    observed=datetime.now(timezone.utc)
    stamp=(observed-timedelta(seconds=1)).isoformat()
    target=relations[edge_index]['from_node_id']
    state={'decision_protocol':'ab_v1','graph':{'nodes':[{'id':key,'kind':'service','name':key} for key in scope['target_node_ids']]},
           'incident':{'window_start':(observed-timedelta(minutes=2)).isoformat()},
           'evidence':[{'evidence_id':'actual-lines','call_id':'separate-call','tool_id':'container.logs',
                        'observed_at':observed.isoformat(),'target_node_id':target,'startup_cycle':'boot-2',
                        'payload':{'text':'\n'.join(stamp+' '+line for line in lines)}}]}
    derived=facts_from_state(state)
    assert derived
    combined=[fact for fact in facts if fact['kind']!='log_observation']+derived
    assert registry().verify(rule,combined,relations,scope)['status']=='unresolved'
    state['evidence'][0]['payload']['text']=stamp+' '+lines[0].replace('other-db','mysql').replace('other-app','app')
    assert registry().verify(rule,[fact for fact in facts if fact['kind']!='log_observation']+facts_from_state(state),relations,scope)['status']=='supported'
