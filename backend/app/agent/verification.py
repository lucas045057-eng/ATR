"""Deterministic causal minimums; predictions never become executable arguments."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from itertools import combinations

FAILED = {"failed", "exited", "dead", "inactive", "restarting"}
HEALTHY = {"active", "running", "healthy"}
STATUS_TOOLS = {"service.status", "container.status", "compose.status"}
LOG_TOOLS = {"service.logs", "container.logs"}
MECHANISMS = {
    "startup_failure": r"\bstartup\b.*\b(?:failed|abort\w*|exit\w*)|application exited because",
    "permission_denied": r"permission denied|operation not permitted|access denied",
    "bind_failure": r"address already in use|cannot bind|failed to bind|bind.*failed",
    "database_unavailable": r"fatal.*(?:database|shutdown|startup)|database.*(?:failed|unavailable)|cannot.*(?:database|bind)",
    "redis_unavailable": r"redis.*(?:failed|unavailable)|fatal.*(?:config|bind)|cannot.*bind",
}
NON_EVENTS = re.compile(r"historical|example|test message|simulated example|not failed|no failure", re.I)


def verify_hypothesis(item, state, evidence_targets, allowed_targets, supports, parse_time, parse_log):
    nodes = {n["id"]: n for n in state.get("graph", {}).get("nodes", [])}
    target = str(item.get("target_node_id", ""))[:80]
    mode = str(item.get("failure_mode", "")).casefold()
    window = parse_time(state.get("incident", {}).get("window_start"))
    current = {}
    for e in state.get("evidence", []):
        payload = e.get("payload", {})
        seen = parse_time(e.get("observed_at"))
        if (isinstance(payload, dict) and payload.get("source_type", "Runtime") == "Runtime"
                and e.get("freshness_state", "current") == "current"
                and seen and window and window <= seen <= datetime.now(timezone.utc) + timedelta(seconds=5)
                and evidence_targets.get(e["evidence_id"], set()) & allowed_targets):
            current[e["evidence_id"]] = e

    def states(e, node_id):
        payload = e.get("payload", {})
        values = []
        if e.get("target_node_id") == node_id and isinstance(payload.get("state"), str):
            values.append(payload["state"])
        for container in payload.get("containers", []) if isinstance(payload.get("containers"), list) else []:
            if isinstance(container, dict) and container.get("service") == nodes.get(node_id, {}).get("name"):
                values.append(container.get("state"))
        return values

    def lines(e):
        observed = parse_time(e.get("observed_at"))
        return [line for line in str(e.get("payload", {}).get("text", "")).splitlines()
                if (stamp := parse_log(line)) and window <= stamp <= observed + timedelta(seconds=5)
                and not NON_EVENTS.search(line)]

    raw = item.get("verification_predictions")
    canonical = raw is None or raw == []
    # Existing text predictions remain compatible only when they describe a checkable service state.
    if canonical:
        predictable = mode in MECHANISMS and re.search(r"exit|fail|inactive|停止|退出|故障", str(item.get("prediction", "")), re.I)
        raw = [{"target_node_id": target, "tool_id": "runtime.status", "field": "state",
                "operator": "in", "value": sorted(FAILED)}] if predictable else [{}]
    if not isinstance(raw, list):
        raw = [{}]
    results, counter = [], set()
    for proposal in raw[:4]:
        p = proposal if isinstance(proposal, dict) else {}
        node, tool, field, op, value = (p.get(k) for k in ("target_node_id", "tool_id", "field", "operator", "value"))
        valid_node = isinstance(node, str) and node in allowed_targets and nodes.get(node, {}).get("kind") == "service"
        feasible = valid_node and isinstance(op, str) and (
            (field == "state" and (tool in STATUS_TOOLS if isinstance(tool, str) else False)
             and op in {"eq", "ne"} and isinstance(value, str) and value in FAILED | HEALTHY)
            or (canonical and field == "state" and tool == "runtime.status")
            or (field == "status_code" and tool == "health.get" and op in {"eq", "ne"}
                and isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599)
            or (field == "listening" and tool == "network.listeners" and op in {"eq", "ne"} and isinstance(value, bool))
            or (field == "log_signal" and isinstance(tool, str) and tool in LOG_TOOLS and op == "contains"
                and isinstance(value, str) and 6 <= len(value) <= 160
                and value.casefold() not in {"error", "failed", "exception", "unavailable", "timeout", "traceback"})
        )
        matches, opposite = [], []
        if feasible:
            for eid, e in current.items():
                if node not in evidence_targets[eid] or (e["tool_id"] != tool and not (canonical and e["tool_id"] in STATUS_TOOLS)):
                    continue
                if field == "state":
                    actual = states(e, node)
                elif field == "log_signal":
                    actual = lines(e)
                else:
                    actual = [e["payload"][field]] if field in e["payload"] else []
                if not actual:
                    continue
                def test(x):
                    if canonical:
                        return x in value
                    if op == "contains":
                        return str(value).casefold() in str(x).casefold()
                    return x == value if op == "eq" else x != value
                (matches if any(test(x) for x in actual) else opposite).append(eid)
        status = "infeasible" if not feasible else "refuted" if opposite else "verified" if matches else "missing"
        if opposite:
            counter.update(opposite)
        results.append({"target_node_id": node if isinstance(node, str) else None,
                        "tool_id": tool if isinstance(tool, str) else None,
                        "field": field if isinstance(field, str) else None,
                        "operator": op if isinstance(op, str) else None,
                        "value": value if isinstance(value, (str, int, float, bool, list)) else None,
                        "status": status, "evidence_ids": sorted(set(matches + opposite))})

    relevant = [e for eid, e in current.items() if eid in supports]
    failed_ids = {e["evidence_id"] for e in relevant if e["tool_id"] in STATUS_TOOLS and any(s in FAILED for s in states(e, target))}
    causal_ids = {e["evidence_id"] for e in relevant if e["tool_id"] in LOG_TOOLS
                  and target in evidence_targets[e["evidence_id"]] and mode in MECHANISMS
                  and any(re.search(MECHANISMS[mode], line, re.I) for line in lines(e))}
    if mode in MECHANISMS:
        counter.update(eid for eid, e in current.items() if e["tool_id"] in STATUS_TOOLS
                       and any(s in HEALTHY for s in states(e, target)))
    # A status and log copied from one acquisition cannot corroborate each other.
    independent = any(
        a.get("call_id") and b.get("call_id") and a["call_id"] != b["call_id"]
        and not (a["payload"].get("observation_id") and a["payload"].get("observation_id") == b["payload"].get("observation_id"))
        and {a["tool_id"].split(".")[-1], b["tool_id"].split(".")[-1]} == {"logs", "status"}
        for a, b in combinations([e for e in relevant if e["evidence_id"] in failed_ids | causal_ids], 2)
    )
    mechanism = bool(failed_ids and causal_ids and independent)
    predictions_verified = bool(results) and all(p["status"] == "verified" for p in results)
    passed = bool(mechanism and predictions_verified and not counter)
    return {
        "passed": passed, "mechanism_verified": mechanism,
        "classification": "possible_cause" if passed else "correlated_anomaly" if failed_ids else "phenomenon",
        "prediction_results": results, "counter_evidence": sorted(counter),
        "causal_evidence": sorted(causal_ids), "independent_sources": independent,
        "missing_evidence": ([] if mechanism else ["需要本次窗口内机制日志与独立目标状态"])
                            + ([] if predictions_verified else ["预测未通过可行性或当前证据核验"]),
    }
