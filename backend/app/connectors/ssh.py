from __future__ import annotations

import base64
import json
import time
from pathlib import Path


class SSHConnector:
    """The SSH exec request is constant; parameters are JSON on stdin, never shell text."""

    def __init__(self, config, settings):
        self.config, self.settings = config, settings

    def read(self, tool, args):
        import paramiko

        config = self.config
        if not self.settings.ssh_enabled or not config["access_authorized"]:
            raise PermissionError("SSH 接入尚未明确授权")
        if (
            config["permission_boundary"] != "forced_command_gateway"
            or len(config["boundary_attestation"]) < 10
        ):
            raise PermissionError("需确认服务器已配置受限账号和强制只读采集入口")
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
        key_path = json.loads(self.settings.ssh_credentials).get(config["credential_ref"])
        if not key_path or not Path(key_path).is_file():
            raise PermissionError("SSH 凭据引用未配置")
        client = paramiko.SSHClient()
        host = config["host"] if config["port"] == 22 else f"[{config['host']}]:{config['port']}"
        client.get_host_keys().add(host, parts[0], key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        channel = None
        try:
            client.connect(
                config["host"],
                port=config["port"],
                username=config["username"],
                key_filename=key_path,
                timeout=config["timeout_seconds"],
                auth_timeout=config["timeout_seconds"],
                banner_timeout=config["timeout_seconds"],
                allow_agent=False,
                look_for_keys=False,
            )
            channel = client.get_transport().open_session(timeout=config["timeout_seconds"])
            channel.settimeout(config["timeout_seconds"])
            channel.exec_command("atr-read-v1")
            request = json.dumps({"tool_id": tool, "arguments": args}).encode() + b"\n"
            channel.sendall(request)
            channel.shutdown_write()
            deadline = time.monotonic() + config["timeout_seconds"]
            out, err = bytearray(), bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("SSH 工具执行超时")
                if channel.recv_ready():
                    out.extend(channel.recv(4096))
                if channel.recv_stderr_ready():
                    err.extend(channel.recv_stderr(4096))
                if len(out) + len(err) > config["max_output_bytes"]:
                    raise ValueError("SSH 输出超过授权大小限制")
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                time.sleep(0.01)
            if channel.recv_exit_status() != 0:
                raise PermissionError("远程只读入口失败或账号权限不足")
            payload = json.loads(out)
            if not isinstance(payload, dict):
                raise ValueError("远程工具返回非法结构")
            payload["connector"] = "ssh"
            return payload
        finally:
            if channel is not None:
                channel.close()
            client.close()
