from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
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
    model_timeout_seconds: float = Field(default=45, ge=2, le=180)
    allowed_health_urls: str = ""
    max_source_files: int = Field(default=1000, ge=1, le=1000)
    max_source_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)

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
