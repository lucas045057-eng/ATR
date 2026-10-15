"""Minimal model decisions and deterministic Runtime evidence interpretation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from app.agent.log_events import event_fingerprint, normalize_log_events, parse_log_time, parse_time
from app.agent.mechanisms import connection_endpoints
from app.agent.model_schema import allowed_service_nodes
from app.agent.verification import FAILED, HEALTHY, LOG_TOOLS, MECHANISMS, NON_EVENTS, STATUS_TOOLS
from app.context.model_errors import (
    ModelCallError,
    ModelDecisionError,
    diagnostic,
    safe_validation_errors,
)

_HYPOTHESIS_FIELDS = frozenset({
    "target_node_id", "failure_mode", "reasoning", "missing_information", "confidence",
})
_CANDIDATE_FIELDS = frozenset({'cited_evidence_ids', 'testable_prediction'})
_IGNORED_LEGACY_FIELDS = frozenset({
    "evidence_links", "verification_predictions", "target_role", "causal_chain", "prediction", "status",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _DecisionEnvelope(_StrictModel):
    summary: str = Field(min_length=1, max_length=2000)
    hypotheses: list[Any] = Field(max_length=3)
    next_investigation: str = Field(max_length=1000)
    next_action_id: str | None
    finish: bool
    finish_reason: str | None = Field(max_length=500)


class _Hypothesis(_StrictModel):
    target_node_id: str
    failure_mode: str
    reasoning: str = Field(min_length=20, max_length=1200)
    missing_information: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] = Field(max_length=8)
    confidence: float = Field(ge=0, le=1)
    cited_evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    testable_prediction: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("confidence", mode="before")
    @classmethod
    def allow_json_integer_number(cls, value):
        return float(value) if type(value) is int else value


def _parse_time(value: str | None) -> datetime | None:
    return parse_time(value)


def _parse_log_time(line: str) -> datetime | None:
    return parse_log_time(line)


def _enum(values):
    values = list(dict.fromkeys(values))
    return {"type": "string", "enum": values} if values else {"not": {}}


def minimal_output_contract(state: dict[str, Any]) -> dict[str, Any]:
    """Build the model-facing contract from the exact current Runtime candidates."""
    nodes = allowed_service_nodes(state)
    actions = state.get("available_actions", [])
    action_ids = [a["action_id"] for a in actions if isinstance(a, dict) and isinstance(a.get("action_id"), str)]
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["summary", "hypotheses", "next_investigation", "next_action_id", "finish", "finish_reason"],
        "$defs": {"AllowedServiceNodeId": _enum(nodes)},
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            "hypotheses": {"type": "array", "maxItems": 3, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["target_node_id", "failure_mode", "reasoning", "missing_information", "confidence"],
                "properties": {
                    "target_node_id": {"$ref": "#/$defs/AllowedServiceNodeId"},
                    "failure_mode": _enum(MECHANISMS),
                    "reasoning": {"type": "string", "minLength": 20, "maxLength": 1200},
                    "missing_information": {"type": "array", "maxItems": 8,
                                             "items": {"type": "string", "minLength": 1, "maxLength": 300}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            }},
            "next_investigation": {"type": "string", "maxLength": 1000,
                                   "description": "Reasoning only; never parsed as a command or tool argument."},
            "next_action_id": {
                "type": ["string", "null"], "enum": [*dict.fromkeys(action_ids), None],
                "description": "Choose one exact action_id from available_actions, or null.",
            },
            "finish": {"type": "boolean", "description": "A suggestion; Runtime and Evidence Gate determine the outcome."},
            "finish_reason": {"type": ["string", "null"], "maxLength": 500},
        },
    }
    schema["allOf"] = [{
        "if": {"properties": {"finish": {"const": True}}},
        "then": {"properties": {"next_action_id": {"type": "null", "const": None}},
                 "description": "A finish decision cannot also schedule an action."},
    }]
    if action_ids:
        schema["allOf"].append({
            "if": {"properties": {"finish": {"const": False}}},
            "then": {"properties": {"next_action_id": {"enum": list(dict.fromkeys(action_ids))}},
                     "description": "When continuing, select one exact currently available action."},
        })
    schema["properties"]["next_investigation"]["description"] = (
        "Reasoning only; it is never parsed as a command or tool argument."
    )
    schema["properties"]["finish"]["description"] = "A suggestion; Runtime and Evidence Gate determine the outcome."
    if state.get('investigation_goal'):
        properties = schema['properties']['hypotheses']['items']['properties']
        properties['failure_mode'] = {
            'type': 'string', 'minLength': 1, 'maxLength': 120,
            'description': 'A candidate mechanism, including new mechanisms. Runtime verifies registered causal claims only.'}
        properties['cited_evidence_ids'] = {'type': 'array', 'maxItems': 8, 'items':
                                           _enum([e['evidence_id'] for e in state.get('evidence', [])])}
        properties['testable_prediction'] = {'type': 'string', 'minLength': 1, 'maxLength': 500,
                                             'description': 'A falsifiable prediction, never an executable command.'}
    return schema


def facts_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract bounded facts from current Runtime evidence without copying log contents."""
    nodes = {node["id"]: node for node in state.get("graph", {}).get("nodes", [])
             if node.get("kind") == "service"}
    service_ids = {node.get("name"): node_id for node_id, node in nodes.items()}
    window_start = _parse_time(state.get("incident", {}).get("window_start"))
    now = datetime.now(timezone.utc) + timedelta(seconds=5)
    facts: list[dict[str, Any]] = []

    def add(evidence, target_id, kind, value):
        fact_key = json.dumps([evidence.get("evidence_id"), target_id, kind, value], sort_keys=True)
        payload = evidence.get('payload', {})
        incomplete = any(payload.get(k) for k in ('partial', 'truncated', 'timeout'))
        wrong_observer = (state.get('decision_protocol') == 'ab_v1' and kind in {'http_status', 'endpoint_reachability'}
                          and (payload.get('observation_location') != 'remote_ssh' or not payload.get('endpoint_id')))
        facts.append({
            "fact_id": hashlib.sha256(fact_key.encode()).hexdigest()[:24],
            "kind": kind,
            "evidence_id": evidence.get("evidence_id"), "target_node_id": target_id,
            "tool_id": evidence.get("tool_id"), "value": value,
            "observed_at": evidence.get("observed_at"),
            "source": evidence.get("tool_id"),
            "freshness": 'incomplete' if incomplete else 'unverified_observer' if wrong_observer else evidence.get("freshness_state", "current"),
            "independent_observation": bool(evidence.get("call_id")),
            "call_id": evidence.get("call_id"),
            "observation_id": evidence.get("observation_id") or evidence.get("payload", {}).get("observation_id"),
            "observed_on": target_id,
            "startup_cycle": evidence.get("startup_cycle") or evidence.get("payload", {}).get("startup_cycle")
                             or evidence.get("payload", {}).get("started_at"),
            **({'runtime_id': evidence.get('runtime_id') or payload.get('runtime_id') or payload.get('container_id') or payload.get('name'),
                'runtime_identity_verified': evidence.get('runtime_identity_verified', False)}
               if state.get('investigation_goal') else {}),
        })

    for evidence in state.get("evidence", []):
        payload = evidence.get("payload", {})
        if not isinstance(payload, dict) or payload.get("source_type", "Runtime") != "Runtime":
            continue
        if evidence.get("freshness_state", "current") != "current":
            continue
        observed = _parse_time(evidence.get("observed_at"))
        if not observed or not window_start or not window_start <= observed <= now:
            continue
        targets = set()
        direct = evidence.get("target_node_id")
        if direct in nodes:
            targets.add(direct)
        for container in payload.get("containers", []) if isinstance(payload.get("containers"), list) else []:
            if isinstance(container, dict) and container.get("service") in service_ids:
                targets.add(service_ids[container["service"]])
                state_value = container.get("state")
                if evidence.get("tool_id") in STATUS_TOOLS and isinstance(state_value, str):
                    add(evidence, service_ids[container["service"]], "service_state", state_value.casefold())
        if evidence.get("tool_id") in STATUS_TOOLS and direct in nodes and isinstance(payload.get("state"), str):
            add(evidence, direct, "service_state", payload["state"].casefold())
        if evidence.get("tool_id") == "health.get" and direct in nodes and type(payload.get("status_code")) is int:
            add(evidence, direct, "http_status", payload["status_code"])
        if (state.get('investigation_goal') and evidence.get('tool_id') == 'health.get' and direct in nodes
                and payload.get('reachable') is False and payload.get('failure_kind') == 'connection_refused'):
            add(evidence, direct, 'endpoint_reachability', False)
        if evidence.get("tool_id") not in LOG_TOOLS:
            continue
        text = payload.get("text")
        if not isinstance(text, str):
            continue
        for target_id in targets:
            for event in normalize_log_events(text, {**payload, **evidence}):
                line, stamp = event.raw_line, event.event_time_utc
                started = _parse_time(payload.get('started_at')) if state.get('decision_protocol') == 'ab_v1' else None
                if not stamp or stamp < window_start or started and stamp < started or stamp > observed + timedelta(seconds=5) or NON_EVENTS.search(line):
                    continue
                lowered = line.casefold()
                failure_signal = any(
                    marker in lowered for marker in ("error", "failed", "refused", "unavailable", "exception", "traceback")
                )
                mechanisms = [mode for mode, pattern in MECHANISMS.items() if re.search(pattern, line, re.I)]
                if failure_signal or mechanisms:
                    add(evidence, target_id, "log_observation",
                        {"failure_signal": failure_signal, "mechanisms": sorted(mechanisms),
                         'terminal_failure': bool(not re.search(r'\b(?:request|GET|POST|PUT|PATCH|DELETE)\s+\S*[/]', line, re.I)
                                                  and re.search(r'\b(?:startup|initializ\w*)\b.{0,100}\b(?:failed|abort\w*|permission denied|address already in use)\b'
                                                                r'|\b(?:fatal|failed to start|failed at step|exit\w* because)\b', line, re.I)),
                         "endpoint_ids": connection_endpoints(line), "event_id": event_fingerprint(event),
                         "event_time_utc": stamp.isoformat()})
    return facts


def claims_from_hypotheses(hypotheses: list[dict[str, Any]], facts: list[dict[str, Any]]):
    links_by_hypothesis = []
    claims = []
    for hypothesis in hypotheses:
        target = hypothesis["target_node_id"]
        mode = hypothesis["failure_mode"]
        related = [fact for fact in facts if fact["target_node_id"] == target]
        states = [fact for fact in related if fact["kind"] == "service_state"]
        logs = [fact for fact in related if fact["kind"] == "log_observation"]
        failed = [fact for fact in states if fact["value"] in FAILED]
        healthy = [fact for fact in states if fact["value"] in HEALTHY]
        matching_logs = [fact for fact in logs if mode in fact["value"]["mechanisms"]]
        independent = any(
            status.get("call_id") and log.get("call_id") and status["call_id"] != log["call_id"]
            for status in failed for log in matching_logs
        )
        if healthy and failed:
            claim_status = "unverified"
        elif healthy:
            claim_status = "refuted"
        elif failed and matching_logs and independent:
            claim_status = "corroborated_candidate"
        else:
            claim_status = "unverified"
        claim_id = hashlib.sha256(f"{target}:{mode}".encode()).hexdigest()[:24]
        claims.append({
            "claim_id": claim_id, "target_node_id": target, "failure_mode": mode,
            "status": claim_status,
            "support_evidence_ids": sorted({fact["evidence_id"] for fact in failed + matching_logs}),
            "refute_evidence_ids": sorted({fact["evidence_id"] for fact in healthy}),
        })
        relations = {}
        for fact in failed:
            relations[fact["evidence_id"]] = "supports"
        for fact in matching_logs:
            relations[fact["evidence_id"]] = "supports"
        for fact in healthy:
            relations[fact["evidence_id"]] = "refutes"
        links_by_hypothesis.append([
            {"evidence_id": evidence_id, "relation": relation,
             "explanation": "Runtime classified a current target observation for deterministic Evidence Gate review."}
            for evidence_id, relation in sorted(relations.items())
        ])
    return claims, links_by_hypothesis


def adapt_minimal_decision(value: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Validate the small model protocol and adapt it into the unchanged Evidence Gate input."""
    try:
        decision = _DecisionEnvelope.model_validate(value)
    except ValidationError as exc:
        raise ModelCallError(diagnostic(exc, "validate")) from None

    nodes = allowed_service_nodes(state)
    actions = state.get("available_actions", [])
    action_ids = {a.get("action_id") for a in actions if isinstance(a, dict)}
    if decision.next_action_id is not None and decision.next_action_id not in action_ids:
        raise ModelDecisionError(("next_action_id",), "action_not_allowed")
    if decision.finish and decision.next_action_id is not None:
        raise ModelDecisionError(("next_action_id",), "finish_action_conflict")
    if not decision.finish and actions and decision.next_action_id is None:
        raise ModelDecisionError(("next_action_id",), "action_not_allowed")

    hypotheses: list[dict[str, Any]] = []
    rejected = []
    for index, raw in enumerate(decision.hypotheses):
        if not isinstance(raw, dict):
            rejected.append({"path": f"hypotheses[{index}]", "type": "validation_error"})
            continue
        node_id = raw.get("target_node_id")
        node = nodes.get(node_id) if isinstance(node_id, str) else None
        if node is None:
            raise ModelDecisionError(("hypotheses", index, "target_node_id"), "unknown_target")
        fields = _HYPOTHESIS_FIELDS | (_CANDIDATE_FIELDS if state.get('investigation_goal') else frozenset())
        extra_fields = set(raw) - fields - _IGNORED_LEGACY_FIELDS
        if extra_fields:
            error = ModelCallError({"code": "model_schema_error", "exception_type": "ValidationError",
                                    "stage": "validate", "validation_kind": "format",
                                    "validation_errors": [{"path": f"hypotheses[{index}]", "type": "extra_forbidden", "stage": "validate"}]})
            raise error
        if set(raw) & _IGNORED_LEGACY_FIELDS:
            rejected.append({"path": f"hypotheses[{index}].non_authoritative_fields",
                             "type": "ignored_legacy_reasoning"})
        mode = raw.get("failure_mode")
        if (not isinstance(mode, str) or not mode or len(mode) > 120
                or not state.get('investigation_goal') and mode not in MECHANISMS):
            rejected.append({"path": f"hypotheses[{index}].failure_mode", "type": "invalid_mechanism"})
            continue
        try:
            parsed = _Hypothesis.model_validate({key: raw[key] for key in fields if key in raw})
        except ValidationError as exc:
            rejected.extend({"path": item["path"], "type": item["type"]}
                            for item in safe_validation_errors(
                                exc.errors(include_url=False, include_context=False), ("hypotheses", index)))
            continue
        citations = {e['evidence_id'] for e in state.get('evidence', []) if e.get('target_node_id') == node_id}
        if not set(parsed.cited_evidence_ids) <= citations:
            raise ModelDecisionError(('hypotheses', index, 'cited_evidence_ids'), 'unknown_evidence')
        hypotheses.append({
            "target_node_id": parsed.target_node_id, "target_role": node.get("role", "unknown"),
            "failure_mode": parsed.failure_mode,
            "causal_chain": ["候选故障机制由 Runtime 限定", "因果关系须由当前证据和 Evidence Gate 核验"],
            **({'cited_evidence_ids': parsed.cited_evidence_ids, 'candidate_status': 'unverified'}
               if state.get('investigation_goal') else {}),
            "prediction": (parsed.testable_prediction or (parsed.missing_information[0] if parsed.missing_information else
                           "提出可区分当前候选解释的检查。") if state.get('investigation_goal') else
                           "当前故障窗口应同时满足目标服务失败状态与对应机制日志的确定性校验。"),
            "rationale": parsed.reasoning, "missing_evidence": parsed.missing_information,
            "confidence": parsed.confidence, "verification_predictions": [],
        })

    facts = facts_from_state(state)
    claims, links = claims_from_hypotheses(hypotheses, facts)
    for hypothesis, evidence_links in zip(hypotheses, links, strict=True):
        hypothesis["evidence_links"] = evidence_links
    selected = next((action for action in actions if decision.next_action_id
                     and action.get("action_id") == decision.next_action_id), None)
    action = ({"action_id": decision.next_action_id,
               "rationale": decision.next_investigation or "已选择 Runtime 提供的授权只读候选。"}
              if selected else None)
    return {
        "report": {
            "analysis_status": "model", "summary": decision.summary,
            "hypotheses": hypotheses, "next_action": decision.next_investigation,
            "action": action, "scope_requests": [],
        },
        "facts": facts,
        "claims": claims,
        "finish": decision.finish,
        "finish_reason": decision.finish_reason,
        "audit": {"rejected_hypothesis_count": len(rejected), "rejected_hypotheses": rejected[:8]},
    }


def classify_outcome(state: dict[str, Any], gate: dict[str, Any], decision: dict[str, Any] | None = None):
    """Classify observations without upgrading direct symptoms into an unsupported RCA."""
    if state.get("decision_protocol") == "ab_v1":
        from app.agent.result_evaluator import evaluate_state
        result = evaluate_state(state)
        complete = result["completion_satisfied"]
        actions = state.get("available_actions", [])
        return {**result, "finding_level": {
                    "root_cause_supported": "root_cause_confirmed", 'direct_cause_supported': 'direct_fault_located',
                    "partial": "direct_fault_cause_unknown",
                    "unresolved": "evidence_insufficient", "no_fault_observed": "no_fault_observed_in_scope",
                }[result["diagnosis_status"]],
                "root_cause_confirmed": result["diagnosis_status"] == "root_cause_supported",
                "should_stop": complete or not actions,
                "stop_reason": "completion_satisfied" if complete else "no_new_actions" if not actions else None,
                "runtime_action_id": actions[0].get("action_id") if actions and not complete else None}
    decision = decision or {}
    facts = facts_from_state(state)
    scope = set(state.get("scope_node_ids", []))
    statuses = [fact for fact in facts if fact["kind"] == "service_state" and fact["target_node_id"] in scope]
    latest: dict[str, dict[str, Any]] = {}
    for fact in statuses:
        current = latest.get(fact["target_node_id"])
        if current is None or str(fact.get("observed_at", "")) > str(current.get("observed_at", "")):
            latest[fact["target_node_id"]] = fact
    direct_faults = [
        {"target_node_id": fact["target_node_id"],
         "service": next((node.get("name") for node in state.get("graph", {}).get("nodes", [])
                          if node.get("id") == fact["target_node_id"]), None),
         "state": fact["value"], "evidence_id": fact["evidence_id"]}
        for fact in latest.values() if fact["value"] in FAILED
    ]
    scope_complete = bool(scope and scope <= latest.keys()) and not state.get("scope_truncated", False)
    failure_logs = any(fact["kind"] == "log_observation" and fact["target_node_id"] in scope
                       and (fact["value"].get("failure_signal") or fact["value"].get("mechanisms"))
                       for fact in facts)
    no_fault_in_scope = scope_complete and not failure_logs and all(
        fact["value"] in HEALTHY for fact in latest.values()
    )
    if gate.get("status") == "supported":
        finding_level = "root_cause_confirmed"
    elif direct_faults:
        finding_level = "direct_fault_cause_unknown"
    elif no_fault_in_scope:
        finding_level = "no_fault_observed_in_scope"
    else:
        finding_level = "evidence_insufficient"
    actions = state.get("available_actions", [])
    no_actions = not actions
    action_selected = decision.get("action_selected", bool(decision.get("report", {}).get("action")))
    runtime_action_id = None
    if decision.get("fatal_error"):
        should_stop, stop_reason = True, decision["fatal_error"]
    elif gate.get("status") == "supported":
        should_stop, stop_reason = True, "evidence_supported"
    elif no_actions:
        should_stop, stop_reason = True, "no_new_actions"
    elif decision.get("finish") and no_fault_in_scope:
        should_stop, stop_reason = True, "model_recommended_finish"
    elif decision.get("finish") or not action_selected:
        # A finish suggestion cannot abandon a directly observed fault or an incomplete scope.
        # The candidate pool is Runtime-ranked and already policy-checked.
        runtime_action_id = actions[0].get("action_id") if actions else None
        should_stop = runtime_action_id is None
        stop_reason = "no_new_actions" if should_stop else None
    else:
        should_stop, stop_reason = False, None
    return {
        "finding_level": finding_level, "root_cause_confirmed": gate.get("status") == "supported",
        "direct_faults": sorted(direct_faults, key=lambda item: item["target_node_id"]),
        "stop_reason": stop_reason, "should_stop": should_stop,
        "runtime_action_id": runtime_action_id,
    }
