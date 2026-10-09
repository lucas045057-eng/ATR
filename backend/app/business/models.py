from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.domain import StrictModel


class BusinessCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class Fact(StrictModel):
    id: str
    subject: str = Field(max_length=240)
    predicate: str = Field(max_length=80)
    object: Any
    source_type: Literal["Documented", "Static", "Runtime", "Inferred", "Human Verified"]
    source_locator: str = Field(max_length=500)
    source_digest: str
    observed_at: str
    confidence: float = Field(ge=0, le=1)
    verification_status: Literal["pending", "confirmed", "rejected", "stale"] = "pending"


class BusinessProfile(StrictModel):
    business_id: str
    name: str
    description: str = ""
    profile_version: int
    services: list[dict] = Field(default_factory=list)
    modules: list[dict] = Field(default_factory=list)
    dependencies: list[dict] = Field(default_factory=list)
    interfaces: list[dict] = Field(default_factory=list)
    configurations: list[dict] = Field(default_factory=list)
    datastores: list[dict] = Field(default_factory=list)
    observability: list[dict] = Field(default_factory=list)
    technology_stack: list[str] = Field(default_factory=list)
    business_flows: list[str] = Field(default_factory=list)
    source_references: list[dict] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    review_status: Literal["pending", "approved", "rejected"] = "pending"
    model_status: str = "pending"
    coverage: dict = Field(default_factory=dict)
    changes: dict = Field(default_factory=dict)


class BusinessReview(StrictModel):
    expected_version: int = Field(ge=1)
    subject_id: str = Field(default="profile", max_length=240)
    decision: Literal["approve", "reject", "set_role", "resolve_conflict"]
    reason: str = Field(min_length=5, max_length=1000)
    role: Literal["application", "proxy", "datastore", "cache", "worker", "unknown"] | None = None


class CodeSearch(StrictModel):
    keyword: str = Field(min_length=1, max_length=120)
    module: str | None = Field(default=None, max_length=240)
    limit: int = Field(default=20, ge=1, le=100)


class CodeContext(StrictModel):
    file_id: str = Field(min_length=1, max_length=80)
    symbol: str | None = Field(default=None, max_length=120)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=60, ge=1, le=160)
    profile_version: int | None = Field(default=None, ge=1)


class ManualMediaReview(StrictModel):
    text: str = Field(min_length=5, max_length=4000)
    expected_version: int = Field(ge=1)
