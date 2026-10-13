from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from app.connectors.unit_names import valid_service_name
from app.domain import StrictModel


class HealthEndpoint(StrictModel):
    endpoint_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
    url: str = Field(min_length=1, max_length=1000)
    service: str = Field(min_length=1, max_length=255)
    execution_location: Literal['remote_ssh'] = 'remote_ssh'

    @field_validator('url')
    @classmethod
    def exact_url(cls, value):
        from app.connectors.remote_reads import validate_url
        return validate_url(value)


class ProcessLogSource(StrictModel):
    file_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
    path: str = Field(min_length=1, max_length=240)
    service: str = Field(min_length=1, max_length=255)
    pid: int = Field(ge=1, le=4194304, strict=True)
    start_time: str = Field(pattern=r'^[a-f0-9-]{36}:[0-9]{1,20}$')

    @field_validator('path')
    @classmethod
    def exact_path(cls, value):
        return authorized_path(value)


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
    auth_mode: Literal["password", "private_key"] = "private_key"
    execution_mode: Literal["forced_gateway", "standard_ssh_readonly"] = "forced_gateway"
    credential_ref: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9_.-]*$")
    host_public_key: str = Field(default="", max_length=2048)
    allowed_paths: list[str] = Field(default_factory=list, max_length=20)
    health_endpoints: list[HealthEndpoint] = Field(default_factory=list, max_length=20)
    log_files: list[ProcessLogSource] = Field(default_factory=list, max_length=20)
    max_calls: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_output_bytes: int = Field(default=48000, ge=1024, le=64000)
    access_authorized: bool = False
    permission_boundary: Literal["mock", "forced_command_gateway", "client_readonly"] = "mock"
    boundary_attestation: str = Field(default="", max_length=1000)
    docker_reads_enabled: bool = True
    docker_reader: Literal["direct_cli", "restricted_list"] = "direct_cli"
    mock_scenario: Literal[
        "mixed", "datastore_conflict", "permission_denied", "timeout", "disconnect"
    ] = "mixed"

    @model_validator(mode='after')
    def endpoint_location(self):
        from urllib.parse import urlsplit
        ids = [row.endpoint_id for row in self.health_endpoints]
        if len(ids) != len(set(ids)):
            raise ValueError('健康端点标识不能重复')
        for row in self.health_endpoints:
            if urlsplit(row.url).hostname not in {self.host.lower(), '127.0.0.1', 'localhost', '::1'}:
                raise ValueError('健康端点只能观察当前 SSH 目标或该目标的 loopback')
        if len({row.file_id for row in self.log_files}) != len(self.log_files):
            raise ValueError('固定日志标识不能重复')
        for row in self.log_files:
            if not any(PurePosixPath(row.path).is_relative_to(PurePosixPath(root)) for root in self.allowed_paths):
                raise ValueError('固定日志路径必须位于当前明确授权的业务目录')
        return self

    @field_validator("allowed_paths")
    @classmethod
    def validate_paths(cls, values):
        return [authorized_path(v) for v in values]

    @field_validator("username")
    @classmethod
    def reject_root(cls, value):
        if value.casefold() == "root":
            raise ValueError("SSH 仅允许受限低权限账号，禁止 root")
        return value

    @field_validator("host_public_key")
    @classmethod
    def validate_public_key(cls, value):

        if value and not re.fullmatch(
            r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,2}", value
        ):
            raise ValueError("只接受完整的主机公钥；私钥必须留在凭据文件中")
        return value


class ToolRequest(StrictModel):
    tool_id: str = Field(min_length=1, max_length=80)
    arguments: dict = Field(default_factory=dict)
    read_protocol: Literal['ab_v1'] | None = None


class HealthEndpointSelection(StrictModel):
    health_endpoints: list[HealthEndpoint] = Field(max_length=20)


class ProcessLogSelection(StrictModel):
    log_files: list[ProcessLogSource] = Field(max_length=20)


class SSHPassword(StrictModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": False, "hide_input_in_errors": True}
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value):
        if any(c in value.get_secret_value() for c in ("\x00", "\r", "\n")):
            raise ValueError("密码包含不支持的控制字符")
        return value


class EmptyArgs(StrictModel):
    pass


class HealthArgs(StrictModel):
    endpoint_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')


class ProcessArgs(StrictModel):
    pid: int = Field(ge=1, le=4194304, strict=True)
    start_time: str = Field(pattern=r'^[a-f0-9-]{36}:[0-9]{1,20}$')


class LogFileArgs(StrictModel):
    file_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
    tail: int = Field(default=80, ge=1, le=200, strict=True)


class ListArgs(StrictModel):
    limit: int = Field(default=60, ge=1, le=100, strict=True)
    name_hints: list[str] = Field(default_factory=list, max_length=8)
    cursor: str = Field(
        default="",
        max_length=257,
    )

    @field_validator("cursor")
    @classmethod
    def validate_cursor(cls, value):
        if value and (value[:2] not in {"0:", "1:"} or not valid_service_name(value[2:])):
            raise ValueError("清单游标不合法")
        return value

    @field_validator("name_hints")
    @classmethod
    def validate_hints(cls, values):
        if any(not valid_service_name(v) for v in values):
            raise ValueError("名称线索仅接受有界服务或进程标识符")
        return values


class ServiceArgs(StrictModel):
    model_config = {"str_strip_whitespace": False, "hide_input_in_errors": True}
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not valid_service_name(value):
            raise ValueError("服务标识不合法")
        return value


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


class DockerReaderSelection(StrictModel):
    docker_reader: Literal["direct_cli", "restricted_list"]
