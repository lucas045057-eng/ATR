"""The only completion authority for the ab_v1 investigation protocol."""
from __future__ import annotations

from urllib.parse import urlsplit

from app.agent.mechanisms import FAILED, HEALTHY, MechanismRegistry
from app.tools.redaction import redact_text

REQUIRED_CHAIN = ("dependency_unavailable_at_startup", "upstream_unavailable")


def evaluate_state(state: dict) -> dict:
    from app.agent.decision_runtime import facts_from_state

    facts = facts_from_state(state)
    cycles = {fact["target_node_id"]: fact["startup_cycle"] for fact in facts
              if fact.get("kind") == "service_state" and fact.get("startup_cycle")}
    scope = {"target_node_ids": state.get("scope_node_ids", []), "current_cycles": cycles}
    registry = MechanismRegistry()
    rules = applicable_rules(state, facts)
    assessments = [registry.verify(rule, facts, state.get("graph", {}).get("edges", []), scope)
                   for rule in rules]
    unknowns = state.get("report", {}).get("model_unknowns", [])
    if not unknowns:
        unknowns = [unknown for hypothesis in state.get("report", {}).get("hypotheses", [])
                    for unknown in hypothesis.get("missing_evidence", [])]
    return ResultEvaluator().evaluate({**state, "facts": facts, "investigation_scope": scope,
                                      'required_rule_ids': rules,
                                      "report": {"model_unknowns": unknowns}}, assessments)


def applicable_rules(state, facts):
    # Runs without a frozen goal retain the original complete-chain contract.
    if not state.get('investigation_goal'):
        return state.get('required_rule_ids', list(REQUIRED_CHAIN))
    if 'required_rule_ids' in state:
        return state['required_rule_ids']
    modes = {mode for fact in facts if fact.get('kind') == 'log_observation'
             and fact.get('freshness') == 'current'
             for mode in fact.get('value', {}).get('mechanisms', [])}
    return [rule for rule in MechanismRegistry.schema_modes() if rule in modes]


def verified_entry_impact(state, stopped):
    """An exact local published socket proves a direct entry, not load-balancer topology."""
    entries = []
    by_evidence = {e.get('evidence_id'): e for e in state.get('evidence', [])}
    for endpoint in state.get('authorized_endpoints', []):
        parsed = urlsplit(endpoint['url'])
        if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', '::1'}:
            continue
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        owners = {}
        for fact in stopped:
            evidence = by_evidence.get(fact['evidence_id'], {})
            payload = evidence.get('payload', {})
            if (fact['target_node_id'] != endpoint['target_node_id'] or evidence.get('tool_id') != 'container.status'
                    or evidence.get('call_id') != fact['call_id'] or not payload.get('container_id')
                    or payload.get('startup_cycle') != fact.get('startup_cycle')):
                continue
            for binding in payload.get('published_endpoints', []):
                addresses = {parsed.hostname, '0.0.0.0' if parsed.hostname == '127.0.0.1' else '::'}
                if binding.get('protocol') == 'tcp' and binding.get('host_port') == port and binding.get('host_ip') in addresses:
                    owners[payload['container_id']] = fact['evidence_id']
        refused = [e for e in state.get('evidence', []) if e.get('tool_id') == 'health.get'
                   and e.get('target_node_id') == endpoint['target_node_id']
                   and e.get('evidence_id') in {f['evidence_id'] for f in state.get('facts', [])
                                               if f.get('kind') == 'endpoint_reachability' and f.get('value') is False
                                               and f.get('freshness') == 'current'}
                   and e.get('payload', {}).get('endpoint_id') == endpoint['endpoint_id']
                   and e.get('payload', {}).get('url') == endpoint['url']
                   and e.get('payload', {}).get('failure_kind') == 'connection_refused']
        if len(owners) == 1 and refused:
            entries.append({**endpoint, 'evidence_ids': list(owners.values()),
                            'entry_evidence_ids': [e['evidence_id'] for e in refused],
                            'runtime_ids': list(owners), 'mapping': 'configured_socket_and_current_connection_refusal'})
    return entries


class ResultEvaluator:
    def evaluate(self, state: dict, assessments: list[dict]) -> dict:
        registry = MechanismRegistry()
        facts = state.get("facts", [])
        relations = state.get("graph", {}).get("edges", [])
        scope = state.get("investigation_scope", {})
        goal = state.get('investigation_goal')
        required = applicable_rules(state, facts)
        if required and not goal:
            required = list(dict.fromkeys([*REQUIRED_CHAIN, *required]))
        verified, gaps, counter, rule_assessments = [], list(state.get('runtime_gaps', [])), [], []
        proposals = {item.get("rule_id"): item for item in assessments if isinstance(item, dict)}
        for rule_id in required:
            actual = registry.verify(rule_id, facts, relations, scope)
            rule_assessments.append({**actual, 'assessment': 'refuted' if actual['counterevidence'] else
                                     'supported' if actual['status'] == 'supported' else 'insufficient_evidence'})
            counter.extend(actual["counterevidence"])
            gaps.extend(actual["required_gaps"])
            proposal = proposals.get(rule_id, {})
            citations = proposal.get("supports", [])
            citation_set = {(item.get("fact_id"), item.get("evidence_id"), item.get("observed_on"))
                            for item in citations if isinstance(item, dict)}
            actual_set = {(item["fact_id"], item["evidence_id"], item["observed_on"])
                          for item in actual["supports"]}
            if (actual["status"] == "supported" and proposal.get("status") == "supported"
                    and actual_set and citation_set == actual_set
                    and proposal.get("participants") == actual["participants"]):
                verified.append(actual)
            else:
                gaps.append({"rule_id": rule_id, "target_ids": list(actual["participants"].values()),
                             "requirement_id": "verified_segment_and_citations"})
        by_rule = {item["rule_id"]: item for item in verified}
        if all(rule in by_rule for rule in REQUIRED_CHAIN):
            consumer = by_rule[REQUIRED_CHAIN[0]]["participants"].get("consumer")
            upstream = by_rule[REQUIRED_CHAIN[1]]["participants"].get("upstream")
            if consumer != upstream:
                gaps.append({"rule_id": "complete_chain", "target_ids": [consumer, upstream],
                             "requirement_id": "shared_application_identity"})
        ambiguities = state.get("critical_ambiguities", [])
        current = [f for f in facts if f.get('freshness') == 'current' and f.get('call_id')
                   and f.get('evidence_id') and f.get('target_node_id') in set(scope.get('target_node_ids', []))]
        direct = [fact for fact in (current if goal else facts) if fact.get("kind") == "service_state" and fact.get("value") in FAILED
                  or fact.get("kind") == "http_status" and isinstance(fact.get("value"), int) and fact["value"] >= 500
                  or fact.get('kind') == 'endpoint_reachability' and fact.get('value') is False]
        focus = state.get('focus_node_id')
        focused = [f for f in current if f['target_node_id'] == focus]
        contradictions = [f for f in focused if f.get('kind') == 'http_status' and f.get('value') == 200]
        if any(f.get('kind') == 'service_state' and f.get('value') in FAILED for f in focused):
            contradictions = [f for f in focused if f.get('kind') == 'http_status'
                              or f.get('kind') == 'service_state' and f.get('value') in HEALTHY]
        counter.extend(contradictions)
        explanations = []
        stopped = [f for f in focused if f.get('kind') == 'service_state' and f.get('value') in FAILED
                   and (f.get('startup_cycle') or f.get('runtime_identity_verified'))]
        replica_count = state.get('focus_instance_count', 1)
        stopped_ids = {f.get('runtime_id') for f in stopped if f.get('runtime_id')}
        all_stopped = replica_count == 1 or len(stopped_ids) == replica_count
        blockers = [g for g in state.get('runtime_gaps', [])
                    if not (goal == 'direct_fault' and stopped and g.get('tool_id') == 'health.get')]
        if goal and stopped and all_stopped and not contradictions and not ambiguities and not blockers:
            for fact in stopped:
                explanations.append({'target_node_id': focus, 'kind': 'instance_unavailable',
                                     'symptom': state.get('incident', {}).get('symptom'),
                                     'scope': 'instance', 'evidence_ids': [fact['evidence_id']],
                                     'trigger_cause_status': 'unknown'})
            if goal == 'direct_fault':
                gaps = [g for g in gaps if g.get('tool_id') != 'health.get']
        entries = verified_entry_impact(state, stopped) if explanations else []
        health_required = set(state.get("required_health_checks", []))
        evidence_ids = set(state.get("current_evidence_ids", []))
        health_completed = {check.get("check_id") for check in state.get("health_checks", [])
                            if check.get("status") == "healthy" and check.get("evidence_id") in evidence_ids}
        health_complete = bool(health_required) and health_required <= health_completed
        supported = bool(required) and set(required) <= by_rule.keys() and not gaps and not counter and not ambiguities
        if goal:
            supported = supported and bool({*REQUIRED_CHAIN, 'bind_failure', 'permission_denied'} & by_rule.keys())
        if goal == 'trigger_cause' and direct and not supported and not required:
            gaps.append({'rule_id': 'trigger_cause', 'target_ids': [focus] if focus else [],
                         'requirement_id': 'current_trigger_evidence'})
        fault_signal = direct or any(fact.get("kind") == "log_observation"
                                    and (fact.get("value", {}).get("failure_signal") or fact.get("value", {}).get("mechanisms"))
                                    for fact in facts)
        if supported:
            status = "root_cause_supported"
        elif goal == 'direct_fault' and explanations:
            status = 'direct_cause_supported'
        elif verified or fault_signal:
            status = "partial"
        elif health_complete and not ambiguities and not gaps:
            status = "no_fault_observed"
        else:
            status = "unresolved"
        unknowns = state.get("report", {}).get("model_unknowns", [])
        return {"schema_version": "ab-result-1", "mechanism_registry_version": registry.version,
                "diagnosis_status": status, "completion_satisfied": status in {"root_cause_supported", "direct_cause_supported", "no_fault_observed"},
                "verified_segments": verified, "required_gaps": gaps, "counterevidence": counter,
                "critical_ambiguities": ambiguities, "model_unknowns": [redact_text(str(item)) for item in unknowns],
                "direct_faults": direct, 'investigation_goal': goal,
                'rule_assessments': rule_assessments + ([{'rule_id': rule, 'assessment': 'not_applicable'}
                                                        for rule in registry.schema_modes() if rule not in required] if goal else [])
                                   + [{'rule_id': g['rule_id'], 'target_ids': g.get('target_ids', []),
                                       'assessment': 'verification_unavailable', 'reason': g['requirement_id']}
                                      for g in state.get('runtime_gaps', []) if g.get('tool_id')],
                'direct_findings': direct, 'direct_explanations': explanations,
                'trigger_cause_status': 'verified' if status == 'root_cause_supported' else 'unknown',
                'impact_scope': {'level': 'entry' if entries else 'instance' if explanations else 'observed_scope',
                                 'target_node_ids': sorted({f['target_node_id'] for f in direct}),
                                 'entry_impact_verified': bool(entries), 'endpoints': entries}}
