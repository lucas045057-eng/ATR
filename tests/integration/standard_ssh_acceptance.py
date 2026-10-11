"""Opt-in local real OpenSSH; systemd/Docker CLI outputs are explicitly synthetic."""
import json
import os
import time
from pathlib import Path

import paramiko
import pytest
from app.connectors.credentials import SSH_SESSIONS, CredentialUnavailable
from app.connectors.standard_ssh import IdentityRejected, StandardSSHConnector
from app.settings import Settings
from pydantic import SecretStr

PASSWORD = None  # Harness injects synthetic password in memory, never env or disk.


@pytest.fixture(scope="module")
def fixture_config():
    path = os.environ.get("ATR_LINUX_SSH_CONFIG")
    assert path, "Explicit local configuration required"
    config = json.loads(Path(path).read_text())
    assert config["config"]["host"] == "127.0.0.1"
    return config


def connector(fixture_config, auth="password", **changes):
    cfg = {**fixture_config["config"], "id": "standard-isolated", "port": 32026,
           "username": "atrstandard", "auth_mode": auth, "execution_mode": "standard_ssh_readonly",
           "permission_boundary": "client_readonly", "docker_reads_enabled": True,
           "_approved": {("service", "catalog-api.service"), ("container", "a" * 64)},
           "timeout_seconds": 10, **changes}
    settings = Settings(ssh_enabled=True, ssh_credentials=json.dumps({"v22-fixture": fixture_config["key_path"]}))
    if auth == "password":
        assert PASSWORD, "Memory-only synthetic credential required"
        cfg["credential_ref"] = SSH_SESSIONS.put(cfg, settings.operator_id, SecretStr(PASSWORD))
    return StandardSSHConnector(cfg, settings)


@pytest.mark.parametrize("auth", ["password", "private_key"])
@pytest.mark.parametrize("tool", ["host.info", "host.memory", "host.disk"])
def test_real_shell_hosts_without_gateway(fixture_config, auth, tool, monkeypatch):
    captured = []
    original = paramiko.Channel.exec_command
    def exec_command(self, command):
        captured.append(command)
        return original(self, command)
    monkeypatch.setattr(paramiko.Channel, "exec_command", exec_command)
    result = connector(fixture_config, auth).read(tool, {})
    assert result["identity"]["uid"] == 10004 and result["identity"]["precheck"] == "passed"
    assert result["server_enforced_readonly"] is False
    assert "atr-read-v1" not in captured and len(captured) == 7
    assert any(k in result for k in ("hostname", "memory_bytes", "disks"))


@pytest.mark.parametrize("tool,args,field", [
    ("process.list", {"limit": 5, "name_hints": ["catalog-api"]}, "processes"),
    ("network.listeners", {}, "listeners"),
    ("service.list", {}, "services"),
    ("service.status", {"name": "catalog-api.service"}, "state"),
    ("service.logs", {"name": "catalog-api.service"}, "text"),
    ("container.list", {}, "containers"),
    ("container.status", {"container_id": "a" * 64}, "state"),
    ("container.logs", {"container_id": "a" * 64}, "text"),
    ("config.list", {"path": "/srv/atr-fixture"}, "files"),
    ("config.read", {"path": "/srv/atr-fixture/app.conf"}, "text"),
])
def test_registered_fixed_read_operations(fixture_config, tool, args, field):
    payload = connector(fixture_config).read(tool, args)
    assert field in payload
    if tool == "process.list":
        assert any(p["name"] == "catalog-api" for p in payload[field])
    if tool == "config.list":
        assert "/srv/atr-fixture/link.conf" not in payload[field]
    if tool == "container.logs":
        assert "synthetic catalog stderr" in payload[field]


def test_config_path_shell_metacharacters_are_data(fixture_config):
    result = connector(fixture_config).read("config.read", {"path": "/srv/atr-fixture/shell'$(id);.conf"})
    assert result["text"] == "SAFE_SYNTHETIC_FILE\n"


@pytest.mark.parametrize("tool,args", [
    ("shell.exec", {"command": "touch /tmp/owned"}),
    ("service.status", {"name": "unrelated-backup.service"}),
    ("service.logs", {"name": "catalog-api.service;id"}),
    ("container.status", {"container_id": "b" * 64}),
    ("config.read", {"path": "/etc/passwd"}),
])
def test_client_policy_denials_not_server_isolation(fixture_config, tool, args):
    with pytest.raises((PermissionError, ValueError)):
        connector(fixture_config).read(tool, args)


@pytest.mark.parametrize("path", ["link.conf", "linked/passwd", "fifo.conf"])
def test_descriptor_walk_blocks_symlinks_and_special_files(fixture_config, path):
    with pytest.raises(PermissionError):
        connector(fixture_config).read("config.read", {"path": "/srv/atr-fixture/" + path})


def test_wrong_password_clears_session(fixture_config):
    remote = connector(fixture_config)
    remote.config["credential_ref"] = SSH_SESSIONS.put(remote.config, remote.settings.operator_id, SecretStr("synthetic-invalid"))
    with pytest.raises(paramiko.AuthenticationException):
        remote.read("host.info", {})
    assert SSH_SESSIONS.status(remote.config, remote.settings.operator_id)["state"] == "required"


def test_wrong_host_key(fixture_config):
    key = paramiko.RSAKey.generate(2048)
    remote = connector(fixture_config, host_public_key=f"{key.get_name()} {key.get_base64()}")
    with pytest.raises(paramiko.BadHostKeyException):
        remote.read("host.info", {})
    assert SSH_SESSIONS.status(remote.config, remote.settings.operator_id)["state"] == "required"


@pytest.mark.parametrize("username,error", [("root", PermissionError), ("atrrisk", IdentityRejected)])
def test_root_and_docker_group_denied(fixture_config, username, error):
    with pytest.raises(error):
        connector(fixture_config, username=username).read("host.info", {})


def test_real_channel_timeout_and_output_limit(fixture_config, monkeypatch):
    closed = []
    original = paramiko.Channel.close
    def close(self):
        closed.append(self.get_id())
        return original(self)
    monkeypatch.setattr(paramiko.Channel, "close", close)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        connector(fixture_config, port=32027, timeout_seconds=1).read("host.info", {})
    assert time.monotonic() - start < 4 and closed
    with pytest.raises(ValueError):
        connector(fixture_config, port=32028, max_output_bytes=1024).read("host.info", {})
    assert len(closed) >= 2


def test_expired_session_never_executes(fixture_config, monkeypatch):
    remote = connector(fixture_config)
    SSH_SESSIONS._entries[remote.config["credential_ref"]].deadline = time.monotonic() - 1
    monkeypatch.setattr(paramiko.Channel, "exec_command", lambda *a: pytest.fail("Expired credential must not execute"))
    with pytest.raises(CredentialUnavailable):
        remote.read("host.info", {})


def test_ordinary_account_can_write_home_outside_atr(fixture_config):
    # Honest boundary test: server shell remains capable of writes by other clients.
    remote = connector(fixture_config)
    with remote.connection() as client:
        result = remote.receive(client, "/usr/bin/touch /home/atrstandard/synthetic-other-client-write")
        assert result == b""
    with pytest.raises(PermissionError):
        remote.read("shell.exec", {"command": "/usr/bin/touch /tmp/owned"})
