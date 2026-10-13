"""Registered causal contracts; adjacent objects never corroborate by proximity."""
from __future__ import annotations

from dataclasses import dataclass

FAILED = {"failed", "exited", "dead", "inactive", "restarting"}
HEALTHY = {"active", "running", "healthy"}


@dataclass(frozen=True)
class MechanismRule:
    rule_id: str
    pattern: str
    roles: tuple[str, ...]


RULES = (
    MechanismRule("startup_failure", r"\bstartup\b.*\b(?:failed|abort\w*|exit\w*)|application exited because", ("target",)),
    MechanismRule("permission_denied", r"permission denied|operation not permitted|access denied", ("target",)),
    MechanismRule("bind_failure", r"address already in use|cannot bind|failed to bind|bind.*failed", ("target",)),
    MechanismRule("database_unavailable", r"fatal.*(?:database|shutdown|startup)|database.*(?:failed|unavailable)|cannot.*(?:database|bind)", ("target",)),
    MechanismRule("redis_unavailable", r"redis.*(?:failed|unavailable)|fatal.*(?:config|bind)|cannot.*bind", ("target",)),
    MechanismRule("dependency_unavailable_at_startup", r"(?:startup|initializ\w*).*?(?:dependency|database|mysql|connect).*?(?:failed|refused|unavailable)|(?:dependency|database|mysql|connect).*?(?:failed|refused|unavailable).*?startup", ("consumer", "dependency")),
    MechanismRule("upstream_unavailable", r"(?:connect\(\) failed|connection refused|no live upstreams).*?upstream|upstream.*?(?:connect\(\) failed|connection refused|unavailable)", ("proxy", "upstream")),
)


class MechanismRegistry:
    version = "ab-mechanisms-1"

    @staticmethod
    def schema_modes() -> list[str]:
        return [rule.rule_id for rule in RULES]

    @staticmethod
    def patterns() -> dict[str, str]:
        return {rule.rule_id: rule.pattern for rule in RULES}

    def verify(self, rule_id: str, facts: list[dict], relations: list[dict], scope: dict) -> dict:
        if rule_id not in self.schema_modes():
            raise ValueError("Unknown mechanism rule")
        authorized = set(scope.get("target_node_ids", []))
        facts = [fact for fact in facts if fact.get("target_node_id") in authorized
                 and fact.get("freshness") == "current" and fact.get("evidence_id") and fact.get("call_id")]
        role_names = {rule.rule_id: rule.roles for rule in RULES}[rule_id]
        candidates = []
        for edge in relations:
            left, right = edge.get("from_node_id"), edge.get("to_node_id")
            if edge.get("relation") != "depends_on" or left not in authorized or right not in authorized:
                continue
            attributes = edge.get("attributes", {})
            gaps, supports, counter = [], [], []

            def gap(requirement):
                gaps.append({"rule_id": rule_id, "target_ids": [left, right], "requirement_id": requirement})

            if (attributes.get("verification_status") != "confirmed" or not attributes.get("endpoint_id")
                    or not attributes.get("source_refs")):
                gap("verified_endpoint_mapping")

            def require(node, kind, predicate, requirement):
                found = [fact for fact in facts if fact["target_node_id"] == node
                         and fact.get("kind") == kind and predicate(fact)]
                if not found:
                    gap(requirement)
                else:
                    supports.append(found[-1])
                return found

            def unavailable(fact):
                return fact.get("value") in FAILED

            if rule_id == "dependency_unavailable_at_startup":
                states = require(left, "service_state", unavailable, "consumer_failed_state")
                require(right, "service_state", unavailable, "dependency_unavailable_state")
                cycle = scope.get("current_cycles", {}).get(left)
                if not cycle:
                    gap("current_startup_identity")
                logs = require(left, "log_observation", lambda fact: rule_id in fact.get("value", {}).get("mechanisms", [])
                               and bool(cycle) and fact.get("startup_cycle") == cycle, "current_startup_connection_failure")
                if states and (not cycle or states[-1].get("startup_cycle") != cycle):
                    gap("consumer_state_startup_identity")
                if states and logs and (states[-1]["call_id"] == logs[-1]["call_id"]
                                       or states[-1].get("observation_id") and states[-1].get("observation_id") == logs[-1].get("observation_id")):
                    gap("independent_consumer_status_and_log")
                counter = [fact for fact in facts if fact["target_node_id"] in {left, right}
                           and fact.get("kind") == "service_state" and fact.get("value") in HEALTHY]
            elif rule_id == "upstream_unavailable":
                states = require(right, "service_state", unavailable, "upstream_failed_state")
                http = require(left, "http_status", lambda fact: fact.get("value") == 502, "current_entry_502")
                logs = require(left, "log_observation", lambda fact: rule_id in fact.get("value", {}).get("mechanisms", []), "proxy_upstream_connection_failure")
                if logs and any(item[-1]["call_id"] == logs[-1]["call_id"] for item in (states, http) if item):
                    gap("independent_upstream_observations")
                counter = [fact for fact in facts if fact.get("kind") == "service_state" and fact["target_node_id"] == right and fact.get("value") in HEALTHY
                           or fact.get("kind") == "http_status" and fact["target_node_id"] == left and fact.get("value") == 200]
            else:
                gap("single_target_rule_uses_legacy_verifier")
            citations = [{"fact_id": fact.get("fact_id"), "evidence_id": fact["evidence_id"],
                          "call_id": fact["call_id"], "observed_on": fact["target_node_id"],
                          "asserts_about": rule_id} for fact in supports]
            candidates.append({"rule_id": rule_id, "registry_version": self.version,
                               "participants": dict(zip(role_names, (left, right))),
                               "status": "supported" if not gaps and not counter else "unresolved",
                               "supports": citations, "counterevidence": counter, "required_gaps": gaps})
        supported = next((item for item in candidates if item["status"] == "supported"), None)
        if supported:
            return supported
        if candidates:
            return min(candidates, key=lambda item: len(item["required_gaps"]))
        return {"rule_id": rule_id, "registry_version": self.version, "status": "unresolved",
                "participants": {}, "supports": [], "counterevidence": [],
                "required_gaps": [{"rule_id": rule_id, "target_ids": [], "requirement_id": "authorized_directed_relationship"}]}
