"""Isolation-only, one semantic correction per Run; never shipped in the API image."""

from app.agent.verification import PREDICTION_RULES
from app.context.model_errors import ModelCallError


def one_scoped_correction(original, audit):
    used = False

    def call(database, settings, budget_id, prompt, context, validate=None):
        nonlocal used
        try:
            return original(database, settings, budget_id, prompt, context, validate=validate)
        except ModelCallError as error:
            errors = error.diagnostic.get("validation_errors", [])
            if used or error.code != "model_schema_error" or not errors or any(
                e.get("type") not in {"counterevidence_target_mismatch", "invalid_prediction"} for e in errors
            ):
                raise
            used = True
            audit.append({"failed_attempt_id": error.diagnostic.get("request_attempt_id"),
                          "validation_errors": errors, "budget_id": budget_id})
            # No failed response is retained or resent; the original validator remains authoritative.
            tools = {e.get("tool_id") for e in context.get("current_evidence", []) + context.get("available_actions", [])}
            feedback = {"error": errors, "instruction": "Regenerate a complete decision. refutes may only use evidence listed for the same target below. Dependency outage claims need their own hypothesis. Follow exact prediction combinations. Never relabel evidence automatically; omit unproved claims or list missing evidence.",
                        "prediction_rules": {field: {"tools": sorted(allowed & tools), "operators": ops, "value_schema": schema}
                                             for field, (allowed, ops, schema) in PREDICTION_RULES.items() if allowed & tools},
                        "allowed_refutes": {
                            n["node_id"]: [e["evidence_id"] for e in context.get("current_evidence", [])
                                           if e.get("target_node_id") == n["node_id"]]
                            for n in context.get("allowed_hypothesis_targets", [])}}
            # Normal reserve()/settle() keeps both attempts within the same 8-call / 100000 budget.
            return original(database, settings, budget_id, prompt,
                            {**context, "decision_correction": feedback}, validate=validate)

    return call
