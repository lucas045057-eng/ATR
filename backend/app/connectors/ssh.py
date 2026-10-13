from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager
from pathlib import Path

from app.connectors.credentials import SSH_SESSIONS


class SSHOutputLimit(ValueError):
    code = "ssh_output_limit"


class SSHConnector:
    """The SSH exec request is constant; parameters are JSON on stdin, never shell text."""

    def __init__(self, config, settings):
        self.config, self.settings = config, settings

    def check_boundary(self):
        config = self.config
        if (config.get("execution_mode", "forced_gateway") != "forced_gateway"
                or config["permission_boundary"] != "forced_command_gateway"
                or len(config["boundary_attestation"]) < 10):
            raise PermissionError("需确认服务器已配置受限账号和强制只读采集入口")

    def check_session(self):
        if self.config.get("auth_mode", "private_key") == "password":
            SSH_SESSIONS.resolve(self.config, self.settings.operator_id)

    @contextmanager
    def connection(self):
        import paramiko

        config = self.config
        if config["username"].casefold() == "root":
            raise PermissionError("SSH 仅允许受限低权限账号，禁止 root")
        if not self.settings.ssh_enabled or not config["access_authorized"]:
            raise PermissionError("SSH 接入尚未明确授权")
        self.check_boundary()
        parts = config["host_public_key"].split()
        if len(parts) != 2:
            raise PermissionError("必须提供已确认的完整 SSH 主机公钥")
        key_classes = {
            "ssh-ed25519": paramiko.Ed25519Key,
            "ssh-rsa": paramiko.RSAKey,
            "ecdsa-sha2-nistp256": paramiko.ECDSAKey,
        }
        if parts[0] not in key_classes:
            raise PermissionError("SSH 主机公钥格式不受支持")
        try:
            key = key_classes[parts[0]](data=base64.b64decode(parts[1], validate=True))
        except Exception as exc:
            raise PermissionError("SSH 主机公钥无法验证") from exc
        password_mode = config.get("auth_mode", "private_key") == "password"
        if config.get("auth_mode", "private_key") not in {"password", "private_key"}:
            raise PermissionError("SSH 认证模式无效")
        if password_mode:
            owner = self.settings.operator_id

            class PasswordOnly(paramiko.auth_strategy.AuthStrategy):
                def authenticate(self, transport):
                    remaining = transport.auth_password(config["username"],
                        SSH_SESSIONS.resolve(config, owner).get_secret_value(), fallback=False)
                    if remaining or not transport.is_authenticated():
                        raise paramiko.AuthenticationException("SSH 密码认证失败")

            SSH_SESSIONS.resolve(config, owner)
            authentication = {"auth_strategy": PasswordOnly(ssh_config=None)}
        else:
            key_path = json.loads(self.settings.ssh_credentials).get(config["credential_ref"])
            if not key_path or not Path(key_path).is_file():
                raise PermissionError("SSH 凭据引用未配置")
            authentication = {"key_filename": key_path}
        client = paramiko.SSHClient()
        host = config["host"] if config["port"] == 22 else f"[{config['host']}]:{config['port']}"
        client.get_host_keys().add(host, parts[0], key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                config["host"],
                port=config["port"],
                username=config["username"],
                **authentication,
                timeout=config["timeout_seconds"],
                auth_timeout=config["timeout_seconds"],
                banner_timeout=config["timeout_seconds"],
                allow_agent=False,
                look_for_keys=False,
            )
            yield client
        except (paramiko.AuthenticationException, paramiko.BadHostKeyException):
            if password_mode:
                SSH_SESSIONS.revoke(config, owner)
            raise
        finally:
            client.close()

    def receive(self, client, command, stdin=None, *, deadline=None, budget=None, include_stderr=False):
        config = self.config
        deadline = deadline if deadline is not None else time.monotonic() + config["timeout_seconds"]
        budget = budget if budget is not None else [0]
        channel = None
        try:
            self.check_session()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("SSH 工具执行超时")
            channel = client.get_transport().open_session(timeout=remaining)
            channel.settimeout(remaining)
            self.check_session()
            channel.exec_command(command)
            if stdin is not None:
                channel.sendall(stdin)
            channel.shutdown_write()
            out, err = bytearray(), bytearray()
            while True:
                self.check_session()
                if time.monotonic() > deadline:
                    raise TimeoutError("SSH 工具执行超时")
                if channel.recv_ready():
                    chunk = channel.recv(4096)
                    out.extend(chunk)
                    budget[0] += len(chunk)
                if channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(4096)
                    err.extend(chunk)
                    budget[0] += len(chunk)
                if budget[0] > config["max_output_bytes"]:
                    raise SSHOutputLimit("SSH 输出超过授权大小限制")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                time.sleep(0.01)
            if channel.recv_exit_status() != 0:
                exc = PermissionError("远程只读工具失败或账号权限不足")
                # Only a known class name is retained, never arbitrary remote stderr.
                if bytes(err.strip()) in {b'PermissionError', b'ValueError'}:
                    exc.remote_error_type = err.strip().decode('ascii')
                raise exc
            self.check_session()
            return bytes(out + (b"\n" + err if include_stderr and err else b""))
        except Exception as exc:
            exc.output_bytes = budget[0]
            raise
        finally:
            if channel is not None:
                channel.close()

    def read(self, tool, args):
        with self.connection() as client:
            if self.config.get('_read_protocol') == 'ab_v1':
                from app.connectors.capabilities import (
                    LEGACY_TOOLS,
                    decode,
                    legacy_host,
                    require_tool,
                    validate_capabilities,
                    validate_legacy_payload,
                )
                from app.connectors.diagnostics import CapabilityUnavailable
                from app.connectors.remote_reads import PROTOCOL
                deadline, budget = time.monotonic() + self.config['timeout_seconds'], [0]

                def receive(request):
                    return decode(self.receive(client, 'atr-read-v1', json.dumps(request).encode() + b'\n',
                                               deadline=deadline, budget=budget))
                try:
                    caps = validate_capabilities(receive({'tool_id': 'capabilities', 'arguments': {}}))
                except PermissionError as exc:
                    if getattr(exc, 'remote_error_type', None) != 'PermissionError':
                        raise
                    caps = legacy_host(receive({'tool_id': 'host.info', 'arguments': {}}))
                    if tool not in LEGACY_TOOLS:
                        raise CapabilityUnavailable('旧网关未提供该工具；需要维护升级') from None
                    payload = validate_legacy_payload(tool, receive({'tool_id': tool, 'arguments': args}))
                    payload.update(capabilities=caps, connector='ssh', read_adapter='tested-json-v1')
                    return payload
                require_tool(caps, tool)
                payload = receive({'tool_id': tool, 'arguments': args, 'protocol': PROTOCOL})
                if tool in {'health.get', 'container.status', 'container.logs', 'service.status', 'service.logs',
                            'process.status', 'log.tail'}:
                    if (payload.get('protocol') != PROTOCOL or payload.get('tool_version') != caps['tools'][tool]
                            or payload.get('observation_location') != 'remote_ssh'):
                        from app.connectors.diagnostics import ToolParseError
                        raise ToolParseError()
                payload.update(capabilities=caps, connector='ssh')
                return payload
            request = json.dumps({"tool_id": tool, "arguments": args}).encode() + b"\n"
            out = self.receive(client, "atr-read-v1", request)
            from app.connectors.diagnostics import ToolParseError
            try:
                payload = json.loads(out)
                if not isinstance(payload, dict):
                    raise ToolParseError(output_bytes=len(out))
            except (ValueError, UnicodeError) as exc:
                raise ToolParseError(output_bytes=len(out)) from exc
            payload["connector"] = "ssh"
            return payload
