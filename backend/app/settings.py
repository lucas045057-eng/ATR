from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_path: Path = Path("/data/runtime.sqlite3")
    repository_roots: str = "/demo,/repos"
    collector_url: str = "http://collector:8090"
    collector_token: SecretStr = SecretStr("")
    operator_token: SecretStr = SecretStr("")
    operator_id: str = "local-operator"
    model_provider: str = "openai-compatible"
    model_base_url: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_name: str = ""
    model_wire_api: Literal['chat_completions', 'responses'] = 'chat_completions'
    model_reasoning_effort: Literal['low', 'medium', 'high'] | None = None
    agent_decision_protocol: Literal["legacy_v22", "minimal_v2", "ab_v1"] = "ab_v1"
    ab_soft_model_calls: int = Field(default=6, ge=1, le=100)
    ab_soft_tool_calls: int = Field(default=16, ge=1, le=500)
    ab_supplement_enabled: bool = True
    model_timeout_seconds: float = Field(default=45, ge=2, le=180)
    allowed_health_urls: str = ""
    max_source_files: int = Field(default=1000, ge=1, le=1000)
    max_source_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    artifact_root: Path = Path("./data/artifacts")
    upload_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    extraction_max_bytes: int = Field(default=200 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    artifact_max_files: int = Field(default=2000, ge=1, le=2000)
    artifact_max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    artifact_read_bytes: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024)
    media_timeout_seconds: int = Field(default=120, ge=1, le=120)
    initial_timeout_seconds: int = Field(default=900, ge=1, le=900)
    media_max_pages: int = Field(default=50, ge=1, le=50)
    media_max_image_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    media_max_pdf_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    artifact_max_depth: int = Field(default=16, ge=1, le=32)
    extraction_timeout_seconds: int = Field(default=45, ge=1, le=120)
    external_model_enabled: bool = False
    model_json_supported: bool = True
    model_vision_supported: bool = False
    model_context_window: int = Field(default=32768, ge=1024)
    model_max_output_tokens: int = Field(default=1500, ge=64, le=8000)
    initial_input_tokens: int = Field(default=8000, ge=1)
    investigation_input_tokens: int = Field(default=12000, ge=1)
    initial_model_calls: int = Field(default=6, ge=1, le=20)
    investigation_model_rounds: int = Field(default=8, ge=1, le=100)
    investigation_tool_calls: int = Field(default=20, ge=1, le=500)
    task_health_model_rounds: int = Field(default=3, ge=1, le=100)
    task_health_tool_calls: int = Field(default=4, ge=1, le=500)
    task_focused_model_rounds: int = Field(default=5, ge=1, le=100)
    task_focused_tool_calls: int = Field(default=6, ge=1, le=500)
    task_rca_model_rounds: int = Field(default=12, ge=1, le=100)
    task_rca_tool_calls: int = Field(default=20, ge=1, le=500)
    investigation_no_progress_rounds: int = Field(default=2, ge=1, le=5)
    investigation_minimum_useful_score: float = Field(default=3.5, ge=0, le=10)
    investigation_high_value_score: float = Field(default=4.5, ge=0, le=10)
    budget_total_tokens: int = Field(default=100000, ge=1)
    code_search_limit: int = Field(default=20, ge=1, le=100)
    model_input_cost_per_million: float | None = Field(default=None, ge=0)
    model_output_cost_per_million: float | None = Field(default=None, ge=0)
    ssh_credentials: str = "{}"  # Operator-owned reference -> key file path, never a private key.
    ssh_enabled: bool = False

    @field_validator("operator_id")
    @classmethod
    def validate_operator_id(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 80:
            raise ValueError("OPERATOR_ID must contain between 1 and 80 characters")
        return value

    def validate_secrets(self) -> None:
        operator_token = self.operator_token.get_secret_value()
        collector_token = self.collector_token.get_secret_value()
        if len(operator_token) < 32 or "replace-this" in operator_token:
            raise ValueError("Set OPERATOR_TOKEN in .env to a private value of at least 32 characters")
        if len(collector_token) < 32 or "replace-this" in collector_token:
            raise ValueError("Set COLLECTOR_TOKEN in .env to a private value of at least 32 characters")

    def repository_root_paths(self) -> tuple[Path, ...]:
        roots = tuple(Path(item.strip()).resolve() for item in self.repository_roots.split(",") if item.strip())
        if not roots:
            raise ValueError("Configure at least one read-only repository root")
        return roots

    def authenticate_operator(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate, self.operator_token.get_secret_value())

    def authenticate_collector(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate, self.collector_token.get_secret_value())

    def health_url_allowlist(self) -> tuple[str, ...]:
        urls = []
        for item in self.allowed_health_urls.split(","):
            url = item.strip()
            if not url:
                continue
            parts = urlsplit(url)
            if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
                    or parts.fragment or parts.query):
                raise ValueError("ALLOWED_HEALTH_URLS must be exact http(s) URLs without credentials, queries, or fragments")
            urls.append(url)
        return tuple(urls)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_secrets()
    settings.health_url_allowlist()
    for root in settings.repository_root_paths():
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Configured repository root does not exist: {root}")
    return settings
