from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator

from app.domain import StrictModel


def authorized_path(value):
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or ".." in value.split("/")
        or "\x00" in value
        or "\\" in value
        or str(path) in {"/", "/etc", "/var", "/home", "/root", "/usr", "/proc", "/sys", "/dev"}
    ):
        raise ValueError("只允许明确的业务目录，禁止全盘或系统目录扫描")
    return str(path)


class RuntimeTargetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    connector: Literal["mock", "ssh"] = "mock"
    host: str = Field(
        default="mock", min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$"
    )
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="atr-read", max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")
    credential_ref: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9_.-]*$")
    host_public_key: str = Field(default="", max_length=2048)
    allowed_paths: list[str] = Field(default_factory=list, max_length=20)
    max_calls: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_output_bytes: int = Field(default=48000, ge=1024, le=64000)
    access_authorized: bool = False
    permission_boundary: Literal["mock", "forced_command_gateway"] = "mock"
    boundary_attestation: str = Field(default="", max_length=1000)
    docker_reads_enabled: bool = False
    mock_scenario: Literal[
        "mixed", "datastore_conflict", "permission_denied", "timeout", "disconnect"
    ] = "mixed"

    @field_validator("allowed_paths")
    @classmethod
    def validate_paths(cls, values):
        return [authorized_path(v) for v in values]

    @field_validator("host_public_key")
    @classmethod
    def validate_public_key(cls, value):
        import re

        if value and not re.fullmatch(
            r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,2}", value
        ):
            raise ValueError("只接受完整的主机公钥；私钥必须留在凭据文件中")
        return value


class ToolRequest(StrictModel):
    tool_id: str = Field(min_length=1, max_length=80)
    arguments: dict = Field(default_factory=dict)


class EmptyArgs(StrictModel):
    pass


class ListArgs(StrictModel):
    limit: int = Field(default=60, ge=1, le=100, strict=True)
    name_hints: list[str] = Field(default_factory=list, max_length=8)
    cursor: str = Field(
        default="",
        max_length=130,
        pattern=r"^(?:[01]:[A-Za-z0-9][A-Za-z0-9_.@-]{0,127})?$",
    )

    @field_validator("name_hints")
    @classmethod
    def validate_hints(cls, values):
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", v) for v in values):
            raise ValueError("名称线索仅接受有界服务或进程标识符")
        return values


class ServiceArgs(StrictModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")


class ServiceLogArgs(ServiceArgs):
    tail: int = Field(default=80, ge=1, le=200)


class ContainerArgs(StrictModel):
    container_id: str = Field(pattern=r"^[a-f0-9]{12,64}$")


class ContainerLogArgs(ContainerArgs):
    tail: int = Field(default=80, ge=1, le=200)


class ConfigArgs(StrictModel):
    path: str = Field(min_length=1, max_length=240)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value):
        return authorized_path(value)


class BindingReview(StrictModel):
    decision: Literal["confirm", "reject"]
    reason: str = Field(min_length=5, max_length=1000)
