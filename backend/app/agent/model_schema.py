"""Validate complete model proposals before usage success or any follow-up action."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from app.agent.verification import (
    LOG_TOOLS,
    MECHANISMS,
    PREDICTION_RULES,
    STATUS_TOOLS,
    prediction_error_field,
)
from app.context.budget import BudgetExceeded
from app.context.model_errors import (
    ModelCallError,
    ModelDecisionError,
    diagnostic,
    safe_validation_errors,
)

SERVICE_TARGET_LIMIT = 30


def references(values):
    candidates = list(dict.fromkeys(values))
    return {"type": "string", "enum": candidates} if candidates else {"not": {}}


def role_constraints(roles, identity_field, role_field):
    return [
        {"if": {"properties": {identity_field: references(key for key, value in roles.items() if value == role)}},
         "then": {"properties": {role_field: {"const": role}}}}
        for role in dict.fromkeys(roles.values())
    ]


def compact_schema(value):
    if isinstance(value, dict):
        return {k: compact_schema(v) for k, v in value.items() if k not in {"title", "default"}}
    if isinstance(value, list):
        return [compact_schema(v) for v in value]
    return value


def observable_prediction_tools(state):
    observable = {a.get("tool_id") for a in state.get("available_actions", [])
                  if a.get("tool_id") in STATUS_TOOLS | LOG_TOOLS | {"health.get"}}
    for entry in state.get("evidence", []):
        payload, tool = entry.get("payload", {}), entry.get("tool_id")
        if (tool in STATUS_TOOLS and ("state" in payload or "containers" in payload)
                or tool in LOG_TOOLS and "text" in payload
                or tool == "health.get" and "status_code" in payload
                or tool == "network.listeners" and type(payload.get("listening")) is bool):
            observable.add(tool)
    return observable


def allowed_service_nodes(state):
    """One complete, bounded reference set for both prompt and validation."""
    nodes = {n["id"]: n for n in state.get("graph", {}).get("nodes", [])
             if n.get("kind") == "service" and n.get("role") != "runtime"}
    scope = state.get("scope_node_ids")
    if isinstance(scope, list):
        allowed = set(scope)
        nodes = {node_id: node for node_id, node in nodes.items() if node_id in allowed}
    if len(nodes) > SERVICE_TARGET_LIMIT:
        raise BudgetExceeded("target_candidate_limit", candidate_count=len(nodes),
                             candidate_limit=SERVICE_TARGET_LIMIT)
    return nodes


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

    @field_validator("value", mode="before")
    @classmethod
    def json_integer_notation(cls, value, info: ValidationInfo):
        bounds = PREDICTION_RULES["status_code"][2]
        # JSON Schema integer includes 200.0; preserve its exact, bounded integer meaning.
        if (info.data.get("field") == "status_code" and type(value) is float
                and bounds["minimum"] <= value <= bounds["maximum"] and value.is_integer()):
            return int(value)
        return value


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


def output_contract(adaptive, state=None):
    """The prompt uses the same schema and mode-specific action fields as validation."""
    schema = Report.model_json_schema()
    schema["properties"]["action"] = {"anyOf": [
        (AdaptiveAction if adaptive else LegacyAction).model_json_schema(), {"type": "null"}]}
    if state is not None:
        nodes = allowed_service_nodes(state)
        definitions = schema["$defs"]
        definitions["Hypothesis"]["allOf"] = role_constraints(
            {key: node.get("role") for key, node in nodes.items()}, "target_node_id", "target_role")
        definitions["Hypothesis"]["properties"]["failure_mode"]["description"] = (
            "Gate: " + ", ".join(MECHANISMS)
            + ". DB-connect exit: startup_failure; DB outage: database_unavailable; others unverified."
        )
        definitions["Hypothesis"]["properties"]["missing_evidence"]["description"] = (
            "Required gaps block confirmation."
        )
        definitions["AllowedServiceNodeId"] = references(nodes)
        definitions["AllowedEvidenceId"] = references(e["evidence_id"] for e in state.get("evidence", []))
        for name in ("Hypothesis", "Prediction"):
            definitions[name]["properties"]["target_node_id"] = {"$ref": "#/$defs/AllowedServiceNodeId"}
        definitions["EvidenceLink"]["properties"]["evidence_id"] = {"$ref": "#/$defs/AllowedEvidenceId"}
        definitions["EvidenceLink"]["properties"]["relation"]["description"] = (
            "refutes: same claim/target_node_id. Dependency outages need own hypotheses; dependency supports allowed."
        )
        # Dependency evidence may support a claim; refutation must belong to its exact target.
        if nodes and state.get("evidence"):
            definitions["Hypothesis"]["allOf"] = [
                {"if": {"properties": {"target_node_id": {"const": node_id}}},
                 "then": {"properties": {"target_role": {"const": nodes[node_id].get("role")},
                                          "evidence_links": {"items": {"anyOf": [
                     {"properties": {"relation": {"const": "supports"}}},
                     {"properties": {"evidence_id": references(
                         e["evidence_id"] for e in state.get("evidence", [])
                         if e.get("target_node_id") == node_id)}}]}}}}}
                for node_id in nodes
            ]
        # Predict only fields the current observations or executable candidates can supply.
        fields = {field: rule[0] for field, rule in PREDICTION_RULES.items()}
        observable = observable_prediction_tools(state)
        definitions["Prediction"]["properties"]["tool_id"] = references(sorted(observable))
        definitions["Prediction"]["properties"]["field"] = references(
            field for field, tools in fields.items() if tools & observable)
        definitions["Prediction"]["properties"]["operator"] = {}
        definitions["Prediction"]["properties"]["value"] = {}
        prediction_variants = [
            {"properties": {"field": {"const": field}, "tool_id": references(sorted(tools & observable)),
                            "operator": {"enum": operators}, "value": constraints}}
            for field, (tools, operators, constraints) in PREDICTION_RULES.items() if tools & observable
        ]
        if prediction_variants:
            definitions["Prediction"]["oneOf"] = prediction_variants
        else:
            definitions["Prediction"]["not"] = {}
        action = schema["properties"]["action"]["anyOf"][0]["properties"]
        actions = state.get("available_actions", [])
        if adaptive:
            action["action_id"] = references(a["action_id"] for a in actions)
        else:
            action["tool_id"] = references(a["tool_id"] for a in actions)
            action["target_node_id"] = references(a["target_node_id"] for a in actions)
            schema["properties"]["action"]["anyOf"][0]["allOf"] = [
                {"if": {"properties": {"tool_id": {"const": tool}}},
                 "then": {"properties": {"target_node_id": references(
                     a["target_node_id"] for a in actions if a["tool_id"] == tool)}}}
                for tool in dict.fromkeys(a["tool_id"] for a in actions)
            ]

    # JSON Schema requires a nonempty allOf; an empty reference set already prohibits items.
    if not schema.get("$defs", {}).get("Hypothesis", {}).get("allOf"):
        schema["$defs"]["Hypothesis"].pop("allOf", None)
    if not adaptive and not schema["properties"]["action"]["anyOf"][0].get("allOf"):
        schema["properties"]["action"]["anyOf"][0].pop("allOf", None)
    return compact_schema(schema)


def validate_report(data, state):
    report = Report.model_validate(data)
    nodes = allowed_service_nodes(state)
    evidence = {e["evidence_id"]: e for e in state.get("evidence", [])}
    prediction_tools = set().union(*(rule[0] for rule in PREDICTION_RULES.values()))
    observable = observable_prediction_tools(state)
    rejected_relations = []
    rejected_hypotheses = set()
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
            if link.relation == "refutes" and evidence[link.evidence_id].get("target_node_id") != hypothesis.target_node_id:
                rejected_relations.append({"loc": path + ("evidence_links", link_index, "relation"),
                                           "type": "counterevidence_target_mismatch"})
                rejected_hypotheses.add(index)
        for prediction_index, prediction in enumerate(hypothesis.verification_predictions):
            prediction_path = path + ("verification_predictions", prediction_index)
            if prediction.target_node_id not in nodes:
                raise ModelDecisionError(prediction_path + ("target_node_id",), "unknown_prediction_target")
            if prediction.tool_id not in prediction_tools:
                raise ModelDecisionError(prediction_path + ("tool_id",), "prediction_tool_not_allowed")
            if invalid_field := prediction_error_field(prediction.model_dump()):
                raise ModelDecisionError(prediction_path + (invalid_field,), "invalid_prediction")
        for prediction_index, prediction in enumerate(hypothesis.verification_predictions):
            if prediction.tool_id not in observable:
                raise ModelDecisionError(path + ("verification_predictions", prediction_index, "tool_id"), "prediction_tool_not_allowed")
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
    result = report.model_dump()
    if rejected_relations:
        rejected_indexes = sorted(rejected_hypotheses)
        for index in rejected_indexes:
            result["hypotheses"][index]["evidence_links"] = [
                link for link in result["hypotheses"][index]["evidence_links"]
                if not (link["relation"] == "refutes"
                        and evidence[link["evidence_id"]].get("target_node_id")
                        != result["hypotheses"][index]["target_node_id"])
            ]
        result["_decision_audit"] = {
            "rejected_relation_count": len(rejected_relations),
            "rejected_hypothesis_indexes": rejected_indexes,
            "validation_errors": safe_validation_errors(rejected_relations),
        }
    return result
