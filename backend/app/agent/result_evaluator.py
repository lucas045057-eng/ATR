"""The only completion authority for the ab_v1 investigation protocol."""
from __future__ import annotations

from app.agent.mechanisms import FAILED, MechanismRegistry
from app.tools.redaction import redact_text

REQUIRED_CHAIN = ("dependency_unavailable_at_startup", "upstream_unavailable")


def evaluate_state(state: dict) -> dict:
    from app.agent.decision_runtime import facts_from_state

    facts = facts_from_state(state)
    cycles = {fact["target_node_id"]: fact["startup_cycle"] for fact in facts
              if fact.get("kind") == "service_state" and fact.get("startup_cycle")}
    scope = {"target_node_ids": state.get("scope_node_ids", []), "current_cycles": cycles}
    registry = MechanismRegistry()
    assessments = [registry.verify(rule, facts, state.get("graph", {}).get("edges", []), scope)
                   for rule in REQUIRED_CHAIN]
    unknowns = state.get("report", {}).get("model_unknowns", [])
    if not unknowns:
        unknowns = [unknown for hypothesis in state.get("report", {}).get("hypotheses", [])
                    for unknown in hypothesis.get("missing_evidence", [])]
    return ResultEvaluator().evaluate({**state, "facts": facts, "investigation_scope": scope,
                                      "report": {"model_unknowns": unknowns}}, assessments)


class ResultEvaluator:
    def evaluate(self, state: dict, assessments: list[dict]) -> dict:
        registry = MechanismRegistry()
        facts = state.get("facts", [])
        relations = state.get("graph", {}).get("edges", [])
        scope = state.get("investigation_scope", {})
        required = state.get("required_rule_ids", list(REQUIRED_CHAIN))
        if required:
            required = list(dict.fromkeys([*REQUIRED_CHAIN, *required]))
        verified, gaps, counter = [], list(state.get('runtime_gaps', [])), []
        proposals = {item.get("rule_id"): item for item in assessments if isinstance(item, dict)}
        for rule_id in required:
            actual = registry.verify(rule_id, facts, relations, scope)
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
        direct = [fact for fact in facts if fact.get("kind") == "service_state" and fact.get("value") in FAILED
                  or fact.get("kind") == "http_status" and isinstance(fact.get("value"), int) and fact["value"] >= 500]
        health_required = set(state.get("required_health_checks", []))
        evidence_ids = set(state.get("current_evidence_ids", []))
        health_completed = {check.get("check_id") for check in state.get("health_checks", [])
                            if check.get("status") == "healthy" and check.get("evidence_id") in evidence_ids}
        health_complete = bool(health_required) and health_required <= health_completed
        supported = bool(required) and set(required) <= by_rule.keys() and not gaps and not counter and not ambiguities
        fault_signal = direct or any(fact.get("kind") == "log_observation"
                                    and (fact.get("value", {}).get("failure_signal") or fact.get("value", {}).get("mechanisms"))
                                    for fact in facts)
        if supported:
            status = "root_cause_supported"
        elif verified or fault_signal:
            status = "partial"
        elif health_complete and not ambiguities and not gaps:
            status = "no_fault_observed"
        else:
            status = "unresolved"
        unknowns = state.get("report", {}).get("model_unknowns", [])
        return {"schema_version": "ab-result-1", "mechanism_registry_version": registry.version,
                "diagnosis_status": status, "completion_satisfied": status in {"root_cause_supported", "no_fault_observed"},
                "verified_segments": verified, "required_gaps": gaps, "counterevidence": counter,
                "critical_ambiguities": ambiguities, "model_unknowns": [redact_text(str(item)) for item in unknowns],
                "direct_faults": direct}
