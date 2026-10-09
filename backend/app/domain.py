from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OnboardingRequest(StrictModel):
    source_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=120)
    compose_file: str = Field(default="compose.yaml", min_length=1, max_length=240)


class IncidentCreate(StrictModel):
    system_id: str = Field(min_length=1, max_length=80)
    symptom: str = Field(min_length=3, max_length=500)
    window_start: str | None = None
    mode: Literal["normal", "graph_only", "graph_pattern", "graph_adaptive", "alert_only"] = "normal"

    @field_validator("window_start")
    @classmethod
    def normalize_window_start(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("window_start must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("window_start must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat()


class ReviewRequest(StrictModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=5, max_length=1000)


class RepairRecord(StrictModel):
    summary: str = Field(min_length=5, max_length=1000)
    operator_verified: bool
    verification_summary: str = Field(min_length=5, max_length=1000)
    verification_url: str = Field(min_length=8, max_length=500)


class PatternMatchOutcome(StrictModel):
    validation: Literal["validated", "failed"] | None = None
    usefulness: Literal["useful", "not_useful"] | None = None


class EvaluationCaseCreate(StrictModel):
    case_key: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    system_id: str = Field(min_length=1, max_length=80)
    fault_class: str = Field(min_length=2, max_length=120)
    ground_truth: dict[str, Any]
    protocol_version: str = Field(default="paired-v1", min_length=1, max_length=40)


class EvaluationTrialCreate(StrictModel):
    case_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    pair_key: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    arm: Literal["graph_only", "graph_pattern", "graph_adaptive"]
    repetition: int = Field(ge=1, le=100)
    reset_digest: str = Field(min_length=8, max_length=128)
    verdict: Literal["correct", "incorrect", "unresolved"]


class EvaluationPairCreate(StrictModel):
    case_id: str = Field(min_length=1, max_length=80)
    pair_key: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    repetition: int = Field(ge=1, le=100)
    reset_digest: str = Field(min_length=8, max_length=128)
    graph_only_run_id: str = Field(min_length=1, max_length=80)
    graph_pattern_run_id: str = Field(min_length=1, max_length=80)
    graph_only_verdict: Literal["correct", "incorrect", "unresolved"]
    graph_pattern_verdict: Literal["correct", "incorrect", "unresolved"]
