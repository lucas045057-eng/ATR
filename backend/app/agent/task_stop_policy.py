from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from typing import Any

from app.agent.log_events import LOG_TIMESTAMP, normalize_log_events, semantic_payload

TASK_TYPES = {"health_check", "focused_diagnosis", "root_cause_investigation"}
_DEFAULT_LIMITS = {
    "health_check": (3, 4),
    "focused_diagnosis": (5, 6),
    "root_cause_investigation": (12, 20),
}
_MAX_TASK_TARGETS = 20
_TIMESTAMP_PREFIX = LOG_TIMESTAMP
_RESOLVED_CLAIMS = {"supported", "refuted", "corroborated_candidate", "rejected"}


def soft_limits_from_settings(settings: Any, task_type: str | None) -> dict[str, int]:
    names = {
        "health_check": ("task_health_model_rounds", "task_health_tool_calls"),
        "focused_diagnosis": ("task_focused_model_rounds", "task_focused_tool_calls"),
        "root_cause_investigation": ("task_rca_model_rounds", "task_rca_tool_calls"),
    }
    pair = names.get(task_type, names["focused_diagnosis"])
    defaults = _DEFAULT_LIMITS.get(task_type, _DEFAULT_LIMITS["focused_diagnosis"])
    hard_rounds = getattr(settings, "investigation_model_rounds", 100)
    hard_tools = getattr(settings, "investigation_tool_calls", 500)
    return {"soft_model_rounds": min(getattr(settings, pair[0], defaults[0]), hard_rounds),
            "soft_tool_calls": min(getattr(settings, pair[1], defaults[1]), hard_tools)}


def _normalize_target_services(target_services: list[dict[str, Any]]) -> tuple[list[dict[str, str]], int]:
    valid: list[dict[str, str]] = []
    seen = set()
    omitted = 0
    for item in target_services:
        if not isinstance(item, dict):
            continue
        node_id, name = item.get("node_id"), item.get("name")
        if not isinstance(node_id, str) or not node_id or not isinstance(name, str) or not name:
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        normalized = {"node_id": node_id, "name": name[:120],
                      "role": str(item.get("role", "unknown"))[:80]}
        if len(valid) < _MAX_TASK_TARGETS:
            valid.append(normalized)
        else:
            omitted += 1
    return valid, omitted


def resolve_objective(task_type: str | None, target_services: list[dict[str, Any]], *,
                      soft_limits: dict[str, int] | None = None,
                      no_progress_rounds: int = 2, minimum_useful_score: float = 3.5,
                      high_value_score: float = 4.5) -> dict[str, Any]:
    """Resolve an explicit task choice against an already-authorized, bounded target scope."""
    kind = task_type if task_type in TASK_TYPES else "focused_diagnosis"
    valid_services, omitted_services = _normalize_target_services(target_services)
    primary = valid_services[0] if valid_services else None
    targets = valid_services if kind == "root_cause_investigation" else valid_services[:1]
    scope_omission_count = omitted_services if kind == "root_cause_investigation" else 0
    defaults = _DEFAULT_LIMITS[kind]
    limits = soft_limits or {}
    max_rounds = max(1, min(100, int(limits.get("soft_model_rounds", defaults[0]))))
    max_tools = max(1, min(500, int(limits.get("soft_tool_calls", defaults[1]))))
    if kind == "health_check":
        completion = ["已记录目标服务当前状态", "日志检查已完成或明确不可用"]
    elif kind == "focused_diagnosis":
        completion = ["已检查目标服务直接状态", "必要日志已检查或明确不可用；触发原因可保持未知"]
    else:
        completion = ["Evidence Gate 确认根因，或在授权检查耗尽、无进展或软上限时说明证据缺口"]
    return {
        "task_id": str(uuid.uuid4()),
        "task_type": kind,
        "target_services": targets,
        "target_node_ids": [item["node_id"] for item in targets],
        "scope_omission_count": scope_omission_count,
        "primary_target_node_id": primary["node_id"] if primary else None,
        "required_checks": ([
            {"check_type": "status", "target_node_id": primary["node_id"], "required": True},
            {"check_type": "logs", "target_node_id": primary["node_id"], "required": True},
        ] if primary else []),
        "optional_checks": (["authorized_related_service", "authorized_configuration_if_evidence_requires"]
                            if kind == "root_cause_investigation" else []),
        "completion_criteria": completion,
        "evidence_requirements": ["当前 Run 的授权目标证据", "状态证据与所需日志证据分别记录"],
        "stop_policy": {
            "version": "task-stop-v1",
            "soft_model_rounds": max_rounds,
            "soft_tool_calls": max_tools,
            "no_progress_rounds": max(1, min(5, int(no_progress_rounds))),
            "minimum_useful_score": max(0.0, min(10.0, float(minimum_useful_score))),
            "high_value_score": max(0.0, min(10.0, float(high_value_score))),
        },
    }


def make_task_snapshot(objective: dict[str, Any]) -> dict[str, Any]:
    return {"version": "task-stop-v1", "objective": deepcopy(objective),
            "stop_policy": deepcopy(objective["stop_policy"])}


def bind_objective_scope(objective: dict[str, Any], target_services: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind a previously unscoped awaiting-review objective to the now-confirmed scope."""
    result = deepcopy(objective)
    if result.get("target_node_ids"):
        return result
    kind = result.get("task_type", "focused_diagnosis")
    valid, omitted_services = _normalize_target_services(target_services)
    selected = valid if kind == "root_cause_investigation" else valid[:1]
    result["target_services"] = selected
    result["target_node_ids"] = [item["node_id"] for item in selected]
    result["scope_omission_count"] = omitted_services if kind == "root_cause_investigation" else 0
    result["primary_target_node_id"] = selected[0]["node_id"] if selected else None
    result["required_checks"] = ([
        {"check_type": "status", "target_node_id": selected[0]["node_id"], "required": True},
        {"check_type": "logs", "target_node_id": selected[0]["node_id"], "required": True},
    ] if selected else [])
    return result


def load_task_snapshot(model_config: dict[str, Any] | str | None) -> dict[str, Any] | None:
    if isinstance(model_config, str):
        try:
            model_config = json.loads(model_config or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Unsupported task stop policy snapshot") from exc
    if model_config is None:
        return None
    if not isinstance(model_config, dict):
        raise ValueError("Unsupported task stop policy snapshot")
    if "task_stop_policy" not in model_config:
        return None
    snapshot = model_config["task_stop_policy"]
    if not isinstance(snapshot, dict) or snapshot.get("version") != "task-stop-v1":
        raise ValueError("Unsupported task stop policy snapshot")
    objective = snapshot.get("objective")
    policy = snapshot.get("stop_policy")
    if not isinstance(objective, dict) or objective.get("task_type") not in TASK_TYPES:
        raise ValueError("Unsupported task stop policy snapshot")
    objective_policy = objective.get("stop_policy")
    if (not _valid_objective_snapshot(objective) or not _valid_stop_policy(policy)
            or objective_policy != policy):
        raise ValueError("Unsupported task stop policy snapshot")
    return deepcopy(snapshot)


def _valid_stop_policy(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("version") != "task-stop-v1":
        return False
    expected = {"version", "soft_model_rounds", "soft_tool_calls", "no_progress_rounds",
                "minimum_useful_score", "high_value_score"}
    if set(value) != expected:
        return False
    integer_bounds = {"soft_model_rounds": (1, 100), "soft_tool_calls": (1, 500),
                      "no_progress_rounds": (1, 5)}
    for key, (minimum, maximum) in integer_bounds.items():
        item = value.get(key)
        if type(item) is not int or not minimum <= item <= maximum:
            return False
    for key in ("minimum_useful_score", "high_value_score"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= item <= 10:
            return False
    return True


def _valid_objective_snapshot(value: dict[str, Any]) -> bool:
    task_id = value.get("task_id")
    targets = value.get("target_node_ids")
    services = value.get("target_services")
    primary = value.get("primary_target_node_id")
    checks = value.get("required_checks")
    omission_count = value.get("scope_omission_count", 0)
    if (not isinstance(task_id, str) or not task_id
            or not isinstance(targets, list) or len(targets) > _MAX_TASK_TARGETS
            or any(not isinstance(node_id, str) or not node_id for node_id in targets)
            or not isinstance(services, list) or len(services) != len(targets)
            or [item.get("node_id") for item in services if isinstance(item, dict)] != targets
            or not isinstance(omission_count, int) or isinstance(omission_count, bool) or omission_count < 0
            or primary != (targets[0] if targets else None)
            or not isinstance(checks, list)):
        return False
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
           or not isinstance(item.get("role"), str) for item in services):
        return False
    if any(not isinstance(check, dict) or check.get("check_type") not in {"status", "logs"}
           or check.get("target_node_id") != primary or type(check.get("required")) is not bool
           for check in checks):
        return False
    for key in ("optional_checks", "completion_criteria", "evidence_requirements"):
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            return False
    return True


def _check_id(task_id: str, action_id: str | None, target_node_id: str | None, tool_id: str) -> str:
    source = "|".join((task_id, action_id or "", target_node_id or "", tool_id))
    return hashlib.sha256(source.encode()).hexdigest()[:24]


def _plan_check(objective: dict[str, Any], action: dict[str, Any] | None, *,
                target_node_id: str | None, tool_id: str, required: bool) -> dict[str, Any]:
    action_ref = action.get("action_id") if action else None
    node_id = action.get("target_node_id") if action else target_node_id
    return {
        "check_id": _check_id(objective["task_id"], action_ref, node_id, tool_id),
        "action_id": action_ref,
        "target_node_id": node_id,
        "check_type": tool_id,
        "required": required,
        "status": "pending" if action else "unavailable",
        "evidence_ids": [],
        "result_summary": None if action else "当前授权候选中不可用",
    }


def build_investigation_plan(objective: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    actions = [a for a in actions if isinstance(a, dict) and isinstance(a.get("action_id"), str)]
    checks: list[dict[str, Any]] = []
    primary = objective.get("primary_target_node_id")
    for required in objective.get("required_checks", []):
        suffix = required["check_type"]
        candidate = next((a for a in actions if a.get("target_node_id") == primary
                          and a.get("tool_id", "").rsplit(".", 1)[-1] == suffix), None)
        tool_id = candidate.get("tool_id") if candidate else f"runtime.{suffix}"
        checks.append(_plan_check(objective, candidate, target_node_id=primary,
                                  tool_id=tool_id, required=True))
    plan = {"version": "task-plan-v1", "checks": checks}
    return merge_plan_candidates(objective, plan, actions)


def merge_plan_candidates(objective: dict[str, Any], plan: dict[str, Any],
                          actions: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(plan)
    if objective.get("task_type") != "root_cause_investigation":
        return result
    existing = {check.get("action_id") for check in result.get("checks", [])}
    allowed = set(objective.get("target_node_ids", []))
    for action in actions:
        if (not isinstance(action, dict) or action.get("target_node_id") not in allowed
                or not isinstance(action.get("action_id"), str) or action["action_id"] in existing):
            continue
        if not isinstance(action.get("tool_id"), str):
            continue
        result["checks"].append(_plan_check(objective, action, target_node_id=None,
                                            tool_id=action["tool_id"], required=False))
        existing.add(action["action_id"])
    return result


def sync_plan_with_history(plan: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(plan)
    by_action = {entry.get("action_id"): entry for entry in history if entry.get("action_id")}
    for check in result.get("checks", []):
        entry = by_action.get(check.get("action_id"))
        if not entry:
            continue
        evidence_id = entry.get("evidence_id")
        succeeded = entry.get("status") == "succeeded" and bool(evidence_id)
        check["status"] = "completed" if succeeded else "unavailable"
        check["evidence_ids"] = [evidence_id] if succeeded else []
        check["result_summary"] = "已采集当前只读证据" if succeeded else {
            "denied": "授权拒绝，已记录缺口",
            "failed": "读取失败，已记录缺口",
            "interrupted": "调用中断，已记录缺口",
        }.get(entry.get("status"), "未取得有效证据，已记录缺口")
    return result


def filter_actions_for_objective(objective: dict[str, Any], plan: dict[str, Any],
                                 actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed_nodes = set(objective.get("target_node_ids", []))
    if objective.get("task_type") in {"health_check", "focused_diagnosis"}:
        required_pending = {check.get("action_id") for check in plan.get("checks", [])
                            if check.get("required") and check.get("status") == "pending"}
        return [action for action in actions if action.get("action_id") in required_pending]
    return [action for action in actions if action.get("target_node_id") in allowed_nodes]


def _fingerprint(evidence: dict[str, Any]) -> str:
    tool_id = str(evidence.get("tool_id", ""))
    payload = evidence.get("payload") if isinstance(evidence.get("payload"), dict) else {}
    if tool_id.endswith(".status"):
        value = {"state": payload.get("state"), "containers": sorted(
            (item.get("service") or item.get("name"), item.get("state"))
            for item in payload.get("containers", []) if isinstance(item, dict))}
        value.update({key: payload[key] for key in ("pid", "start_time", "started_at", "startup_cycle", "container_id")
                      if key in payload})
    elif tool_id.endswith(".logs"):
        lines = []
        for event in normalize_log_events(str(payload.get("text", "")), evidence):
            normalized = event.message.strip()
            if normalized:
                lines.append(re.sub(r"\s+", " ", normalized))
        value = lines[-500:]
    else:
        value = semantic_payload(payload)
    encoded = json.dumps([evidence.get("target_node_id"), evidence.get("startup_cycle"), tool_id, value],
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def evaluate_progress(evidence: list[dict[str, Any]], previous_state: dict[str, Any] | None,
                      previous_claims: list[dict[str, Any]], current_claims: list[dict[str, Any]],
                      action_history: list[dict[str, Any]], hypothesis_history: list[dict[str, Any]],
                      round_number: int, required_checks_remaining: int = 0) -> dict[str, Any]:
    previous_state = previous_state if isinstance(previous_state, dict) else {}
    seen_fingerprints = set(previous_state.get("observation_fingerprints", []))
    current_fingerprints = {_fingerprint(item) for item in evidence}
    new_fingerprints = current_fingerprints - seen_fingerprints
    before_claims = previous_state.get("claim_state")
    if not isinstance(before_claims, dict):
        before_claims = {item.get("claim_id"): item.get("status") for item in previous_claims
                         if isinstance(item, dict) and item.get("claim_id")}
    now_claims = {item.get("claim_id"): item.get("status") for item in current_claims
                  if isinstance(item, dict) and item.get("claim_id")}
    resolved = sum(1 for key, status in now_claims.items()
                   if key in before_claims and before_claims[key] != status and status in _RESOLVED_CLAIMS)
    actions = [item.get("action_id") for item in action_history if item.get("action_id")]
    repeated_actions = len(actions) - len(set(actions))
    hypothesis_ids = {item.get("hypothesis_id") for item in hypothesis_history
                      if isinstance(item, dict) and item.get("hypothesis_id")}
    previous_hypotheses = set(previous_state.get("hypothesis_ids", []))
    occurrences: dict[str, int] = {}
    for item in hypothesis_history:
        if isinstance(item, dict) and isinstance(item.get("hypothesis_id"), str):
            occurrences[item["hypothesis_id"]] = occurrences.get(item["hypothesis_id"], 0) + 1
    repeated_hypotheses = sum(count - 1 for count in occurrences.values() if count > 1)
    productive = bool(new_fingerprints or resolved)
    consecutive = 0 if productive else int(previous_state.get("consecutive_no_progress_rounds", 0)) + 1
    progress = {
        "round_number": round_number,
        "new_fact_count": len(new_fingerprints),
        "new_relevant_fact_count": len(new_fingerprints),
        "new_hypothesis_count": len(hypothesis_ids - previous_hypotheses),
        "new_claim_resolution_count": resolved,
        "repeated_action_count": repeated_actions,
        "repeated_hypothesis_count": repeated_hypotheses,
        "required_checks_remaining": required_checks_remaining,
        "productive_round": productive,
        "consecutive_no_progress_rounds": consecutive,
    }
    state = {
        "observation_fingerprints": sorted(seen_fingerprints | current_fingerprints)[-512:],
        "claim_state": now_claims,
        "hypothesis_ids": sorted(previous_hypotheses | hypothesis_ids)[-256:],
        "consecutive_no_progress_rounds": consecutive,
    }
    return {"progress": progress, "progress_state": state}


def _high_value_actions(objective: dict[str, Any], plan: dict[str, Any], actions: list[dict[str, Any]],
                        active_hypothesis_nodes: set[str]) -> list[dict[str, Any]]:
    policy = objective["stop_policy"]
    pending_required = {check.get("action_id") for check in plan.get("checks", [])
                        if check.get("required") and check.get("status") == "pending"}
    useful = []
    for action in actions:
        action_id = action.get("action_id")
        if action_id in pending_required:
            useful.append(action)
            continue
        score = action.get("scheduling", {}).get("score", 0)
        if (objective.get("task_type") == "root_cause_investigation"
                and action.get("target_node_id") in active_hypothesis_nodes
                and isinstance(score, (int, float)) and score >= policy["high_value_score"]):
            useful.append(action)
    return useful


def _useful_actions(objective: dict[str, Any], plan: dict[str, Any], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending_required = {check.get("action_id") for check in plan.get("checks", [])
                        if check.get("required") and check.get("status") == "pending"}
    if objective.get("task_type") in {"health_check", "focused_diagnosis"}:
        return [action for action in actions if action.get("action_id") in pending_required]
    minimum_score = objective["stop_policy"]["minimum_useful_score"]
    return [action for action in actions
            if action.get("target_node_id") in set(objective.get("target_node_ids", []))
            and isinstance(action.get("scheduling", {}).get("score"), (int, float))
            and action["scheduling"]["score"] >= minimum_score]


def select_finish_override(objective: dict[str, Any], plan: dict[str, Any],
                           actions: list[dict[str, Any]], runtime_action_id: str | None,
                           active_hypothesis_nodes: set[str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Only continue past a model finish suggestion for a concrete, task-relevant gap."""
    candidate = next((item for item in actions if item.get("action_id") == runtime_action_id), None)
    active_nodes = active_hypothesis_nodes or set()
    pending_required = {check.get("action_id") for check in plan.get("checks", [])
                        if check.get("required") and check.get("status") == "pending"}
    if candidate and candidate.get("action_id") in pending_required:
        return candidate, "required_check_pending"
    high_value = _high_value_actions(objective, plan, actions, active_nodes)
    if not candidate or candidate not in high_value:
        candidate = high_value[0] if high_value else None
    if candidate:
        if candidate.get("target_node_id") in active_nodes:
            return candidate, "high_value_check_for_active_hypothesis"
        if candidate.get("action_id") in pending_required:
            return candidate, "required_check_pending"
    return None, None


def decide_stop(objective: dict[str, Any], plan: dict[str, Any], progress: dict[str, Any],
                gate_status: str, actions: list[dict[str, Any]], *, model_calls: int,
                tool_calls: int, active_hypothesis_nodes: set[str] | None = None,
                hard_stop_reason: str | None = None,
                soft_limit_final_step_used: bool = False,
                finding_level: str | None = None) -> dict[str, Any]:
    task_type = objective["task_type"]
    policy = objective["stop_policy"]
    active_nodes = active_hypothesis_nodes or set()
    checks = plan.get("checks", [])
    required = [check for check in checks if check.get("required")]
    unresolved_required = [check for check in required if check.get("status") == "pending"]
    completed_checks = [check for check in checks if check.get("status") == "completed"]
    unavailable_checks = [check for check in checks if check.get("status") == "unavailable"]
    remaining_checks = [check for check in checks if check.get("status") == "pending"]
    result = {
        "should_stop": False, "stop_reason": None, "task_completion_status": "in_progress",
        "finding_level": finding_level or progress.get("finding_level", "evidence_insufficient"),
        "root_cause_confirmed": gate_status == "supported",
        "completed_checks": completed_checks,
        "unavailable_checks": unavailable_checks,
        "remaining_checks": remaining_checks,
        "required_checks_remaining": len(unresolved_required),
        "soft_limit_final_step": False,
        "high_value_action_ids": [item.get("action_id") for item in
                                   _high_value_actions(objective, plan, actions, active_nodes)],
        "useful_action_ids": [item.get("action_id") for item in _useful_actions(objective, plan, actions)],
        "scope_omission_count": objective.get("scope_omission_count", 0),
    }

    def stop(reason: str, status: str = "stopped_with_gap"):
        result.update(should_stop=True, stop_reason=reason, task_completion_status=status)
        return result

    if hard_stop_reason:
        return stop(hard_stop_reason, "stopped_with_limit")
    if gate_status == "supported":
        result["finding_level"] = "root_cause_confirmed"
        status = "completed_with_gaps" if result["scope_omission_count"] else "completed"
        return stop("evidence_supported", status)
    if task_type in {"health_check", "focused_diagnosis"} and not unresolved_required and required:
        status = "completed_with_gaps" if unavailable_checks else "completed"
        return stop("task_completed", status)
    useful_action_ids = set(result["useful_action_ids"])
    if not useful_action_ids:
        return stop("no_further_useful_checks")
    actions = [action for action in actions if action.get("action_id") in useful_action_ids]

    high_value = bool(result["high_value_action_ids"])
    if (progress.get("consecutive_no_progress_rounds", 0) >= policy["no_progress_rounds"]
            and not high_value):
        return stop("no_progress")
    reached_soft_limit = (model_calls >= policy["soft_model_rounds"]
                          or tool_calls >= policy["soft_tool_calls"])
    if reached_soft_limit:
        if not soft_limit_final_step_used and high_value:
            result["soft_limit_final_step"] = True
            high_value_ids = set(result["high_value_action_ids"])
            result["useful_action_ids"] = [
                action_id for action_id in result["useful_action_ids"] if action_id in high_value_ids
            ]
            return result
        return stop("task_soft_limit", "stopped_with_limit")
    return result
