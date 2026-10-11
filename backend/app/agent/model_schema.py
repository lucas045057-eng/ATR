"""Validate complete model proposals before usage success or any follow-up action."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.verification import prediction_error_field
from app.context.model_errors import ModelCallError, ModelDecisionError, diagnostic


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class EvidenceLink(StrictModel):
    evidence_id: str
    relation: Literal["supports", "refutes"]
    explanation: str


class Prediction(StrictModel):
    target_node_id: str
    tool_id: str
    field: Literal["state", "status_code", "listening", "log_signal"]
    operator: Literal["eq", "ne", "contains"]
    value: str | int | bool


class Hypothesis(StrictModel):
    target_node_id: str
    target_role: str
    failure_mode: str
    causal_chain: list[str] = Field(min_length=2, max_length=12)
    prediction: str
    rationale: str
    evidence_links: list[EvidenceLink] = Field(max_length=24)
    missing_evidence: list[str] = Field(max_length=12)
    confidence: float = Field(ge=0, le=1)
    status: Literal["candidate", "supported", "refuted", "unresolved"] | None = None
    verification_predictions: list[Prediction] | None = Field(default_factory=list, max_length=4)

    @field_validator("verification_predictions", mode="before")
    @classmethod
    def empty_predictions(cls, value):
        return [] if value is None else value


class ScopeRequest(StrictModel):
    service: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Report(StrictModel):
    summary: str
    hypotheses: list[Hypothesis] = Field(max_length=3)
    next_action: str | None
    action: dict[str, Any] | None
    scope_requests: list[ScopeRequest] | None = Field(default_factory=list, max_length=12)

    @field_validator("scope_requests", mode="before")
    @classmethod
    def empty_scope(cls, value):
        return [] if value is None else value

    @field_validator("next_action", mode="before")
    @classmethod
    def empty_next_action(cls, value):
        return "" if value is None else value


class AdaptiveAction(StrictModel):
    action_id: str
    rationale: str


class LegacyAction(StrictModel):
    tool_id: str
    target_node_id: str
    rationale: str


def output_contract(adaptive):
    """The prompt uses the same schema and mode-specific action fields as validation."""
    schema = Report.model_json_schema()
    schema["properties"]["action"] = {"anyOf": [
        (AdaptiveAction if adaptive else LegacyAction).model_json_schema(), {"type": "null"}]}

    def compact(value):
        if isinstance(value, dict):
            return {k: compact(v) for k, v in value.items() if k not in {"title", "default"}}
        if isinstance(value, list):
            return [compact(v) for v in value]
        return value

    return compact(schema)


def validate_report(data, state):
    report = Report.model_validate(data)
    nodes = {n["id"]: n for n in state.get("graph", {}).get("nodes", []) if n.get("kind") == "service"}
    evidence = {e["evidence_id"] for e in state.get("evidence", [])}
    prediction_tools = {"service.status", "container.status", "compose.status", "health.get", "network.listeners", "service.logs", "container.logs"}
    for index, hypothesis in enumerate(report.hypotheses):
        path = ("hypotheses", index)
        node = nodes.get(hypothesis.target_node_id)
        if not node:
            raise ModelDecisionError(path + ("target_node_id",), "unknown_target")
        if node.get("role") != hypothesis.target_role:
            raise ModelDecisionError(path + ("target_role",), "target_role_mismatch")
        for link_index, link in enumerate(hypothesis.evidence_links):
            if link.evidence_id not in evidence:
                raise ModelDecisionError(path + ("evidence_links", link_index, "evidence_id"), "unknown_evidence")
        for prediction_index, prediction in enumerate(hypothesis.verification_predictions):
            prediction_path = path + ("verification_predictions", prediction_index)
            if prediction.target_node_id not in nodes:
                raise ModelDecisionError(prediction_path + ("target_node_id",), "unknown_prediction_target")
            if prediction.tool_id not in prediction_tools:
                raise ModelDecisionError(prediction_path + ("tool_id",), "prediction_tool_not_allowed")
            if invalid_field := prediction_error_field(prediction.model_dump()):
                raise ModelDecisionError(prediction_path + (invalid_field,), "invalid_prediction")
    if report.action is not None:
        actions = state.get("available_actions", [])
        try:
            action = (AdaptiveAction if state.get("adaptive") else LegacyAction).model_validate(report.action)
        except ValidationError as exc:
            error = diagnostic(exc, "validate", field_prefix=("action",))
            error["validation_kind"] = "decision"
            raise ModelCallError(error) from None
        if state.get("adaptive"):
            allowed = any(a.get("action_id") == action.action_id for a in actions)
        else:
            allowed = any(a.get("tool_id") == action.tool_id and a.get("target_node_id") == action.target_node_id for a in actions)
        if not allowed:
            raise ModelDecisionError(("action", "action_id" if state.get("adaptive") else "tool_id"), "action_not_allowed")
    return report.model_dump()
