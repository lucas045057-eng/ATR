"""Safe tool diagnostics. Never serialize exception text or remote output."""
from dataclasses import asdict, dataclass

from paramiko import AuthenticationException, BadHostKeyException
from pydantic import ValidationError


class ToolParseError(ValueError):
    code = "tool_parse_error"
    stage = "parse"

    def __init__(self, *, line_number=None, invalid_item_count=0, output_bytes=0):
        super().__init__("远程工具输出格式不兼容")
        self.line_number = line_number
        self.invalid_item_count = invalid_item_count
        self.output_bytes = output_bytes


class CapabilityUnavailable(PermissionError):
    code = "capability_unavailable"
    stage = "capability"


TERMINAL_CODES = {
    "ssh_authentication_failed", "ssh_host_key_mismatch", "ssh_identity_rejected",
    "ssh_credential_expired", "ssh_credential_required", "ssh_credential_unavailable",
    "authorization_revoked", "profile_version_changed", "global_budget_exhausted",
    "target_call_limit",
    "ssh_identity_timeout", "ssh_identity_output_limit",
}
MESSAGES = {
    "tool_parse_error": "输出格式不兼容；仅保存安全诊断，未将异常行作为证据",
    "tool_parameter_error": "工具参数不符合固定只读协议",
    "tool_permission_denied": "工具权限不足或远程固定命令执行失败",
    "ssh_authentication_failed": "SSH 认证失败，请重新核验低权限账号与会话凭据",
    "ssh_host_key_mismatch": "SSH 主机公钥不匹配，已拒绝连接",
    "ssh_identity_rejected": "SSH 身份检查未通过，未执行采集工具",
    "ssh_output_limit": "工具输出超过大小限制，未保留不完整证据",
    "tool_timeout": "工具执行超时，未将不完整输出作为证据",
    "capability_unavailable": "连接器未提供此能力；未执行命令或回退",
    "target_call_limit": "目标累计调用预算已用尽",
    "authorization_revoked": "目标访问授权已撤销",
    "tool_incomplete": "部分清单未采集或格式不兼容；建议仅基于有效记录分析",
    "tool_connection_error": "工具连接中断",
    "tool_failed": "工具执行失败",
    "ssh_credential_unavailable": "SSH 密码会话未提供、已过期或已清除；请重新输入密码（后端重启也会失效）",
    "ssh_identity_timeout": "SSH 身份检查超时；未执行采集工具",
    "ssh_identity_output_limit": "SSH 身份检查输出超限；未执行采集工具",
}


@dataclass
class ToolFailure:
    tool_id: str
    execution_mode: str
    error_code: str
    failure_stage: str
    status: str
    line_number: int | None = None
    output_bytes: int = 0
    invalid_item_count: int = 0
    retryable: bool = False
    partial: bool = False
    observation_id: str = ""

    def public(self):
        return {**asdict(self), "message": MESSAGES.get(self.error_code, "会话凭据不可用，请重新提供密码")}


def failure(exc, tool, mode, observation_id, stage):
    code = getattr(exc, "code", None)
    if isinstance(exc, BadHostKeyException):
        code, stage = "ssh_host_key_mismatch", "connect"
    elif isinstance(exc, AuthenticationException):
        code, stage = "ssh_authentication_failed", "authenticate"
    elif not code:
        code = ("tool_parameter_error" if stage == "parameters" or isinstance(exc, ValidationError)
                else "tool_timeout" if isinstance(exc, TimeoutError)
                else "tool_permission_denied" if isinstance(exc, PermissionError)
                else "tool_parse_error" if isinstance(exc, (ValueError, UnicodeError))
                else "tool_connection_error" if isinstance(exc, ConnectionError)
                else "tool_failed")
    stage = getattr(exc, "stage", stage)
    if code == "ssh_identity_rejected":
        stage = "identity"
    if code == "ssh_credential_unavailable":
        stage = "credentials"
    status = "timed_out" if isinstance(exc, TimeoutError) else "denied" if isinstance(exc, PermissionError) else "failed"
    return ToolFailure(tool, mode, code, stage, status,
                       getattr(exc, "line_number", None), getattr(exc, "output_bytes", 0),
                       getattr(exc, "invalid_item_count", 0), observation_id=observation_id).public()
