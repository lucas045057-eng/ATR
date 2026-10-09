"""Opt-in actual Linux/OpenSSH tests: run this file explicitly against the local fixture."""

import base64
import json
import os
import time
from pathlib import Path

import paramiko
import pytest
from app.connectors.models import RuntimeTargetCreate, ToolRequest
from app.connectors.service import RuntimeService
from app.connectors.ssh import SSHConnector
from app.settings import Settings, get_settings
from app.storage.database import Database
from test_business import onboard


@pytest.fixture(scope="module")
def fixture_config():
    path = os.environ.get("ATR_LINUX_SSH_CONFIG")
    assert path, "Explicit local fixture configuration required"
    data = json.loads(Path(path).read_text())
    assert data["config"]["host"] == "127.0.0.1"
    return data


def connector(fixture_config, **changes):
    settings = Settings(ssh_enabled=True, ssh_credentials=json.dumps({"v22-fixture": fixture_config["key_path"]}))
    return SSHConnector({**fixture_config["config"], **changes}, settings)


def test_actual_pinned_host_and_configuration_read(fixture_config):
    remote = connector(fixture_config)
    assert "Linux" in remote.read("host.info", {})["text"]
    files = remote.read("config.list", {"path": "/srv/atr-fixture"})["files"]
    assert "/srv/atr-fixture/app.conf" in files
    assert "/srv/atr-fixture/link.conf" not in files
    assert "PORT=8123" in remote.read("config.read", {"path": "/srv/atr-fixture/app.conf"})["text"]


def test_wrong_pinned_host_is_rejected(fixture_config):
    wrong = paramiko.RSAKey.generate(2048)
    with pytest.raises(paramiko.BadHostKeyException):
        connector(fixture_config, host_public_key=f"{wrong.get_name()} {wrong.get_base64()}").read("host.info", {})


@pytest.mark.parametrize("tool,args", [
    ("shell.exec", {"command": "id"}), ("service.restart", {"name": "atr-fixture.service"}),
    ("service.status", {"name": "unapproved.service"}), ("container.list", {}),
    ("config.read", {"path": "/etc/passwd"}),
    ("config.read", {"path": "/srv/atr-fixture/../atr-fixture/app.conf"}),
    ("config.read", {"path": "/srv/atr-fixture/link.conf"}),
    ("config.read", {"path": "/srv/atr-fixture/linked/passwd"}),
    ("config.read", {"path": "/srv/atr-fixture/fifo.conf"}),
])
def test_server_policy_rejects_modifications_and_path_escapes(fixture_config, tool, args):
    with pytest.raises(PermissionError):
        # A malicious client can widen its own config, but the server still refuses.
        connector(fixture_config, allowed_paths=["/"]).read(tool, args)


@pytest.mark.parametrize("command", ["id", "touch /srv/atr-fixture/owned", "systemctl restart ssh"])
def test_openssh_forcecommand_denies_free_commands(fixture_config, command):
    config = fixture_config["config"]
    client = paramiko.SSHClient()
    parts = config["host_public_key"].split()
    client.get_host_keys().add(f"[127.0.0.1]:{config['port']}", parts[0], paramiko.RSAKey(data=base64.b64decode(parts[1], validate=True)))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect("127.0.0.1", port=config["port"], username=config["username"],
                       key_filename=fixture_config["key_path"], allow_agent=False, look_for_keys=False, timeout=3)
        _, stdout, stderr = client.exec_command(command, timeout=3)
        assert stdout.channel.recv_exit_status() != 0
        assert "PermissionError" in stderr.read().decode()
    finally:
        client.close()


def test_550_real_processes_do_not_displace_named_candidate(fixture_config):
    remote = connector(fixture_config)
    start = time.monotonic()
    inventory = remote.read("process.list", {"limit": 60})
    assert inventory["scanned"] >= 550
    assert inventory["truncated"]
    assert len(inventory["processes"]) <= 60
    candidate = remote.read("process.list", {"limit": 5, "name_hints": ["catalog-api"]})
    assert any(p["name"] == "catalog-api" for p in candidate["processes"])
    assert candidate["raw_bytes"] < 1024 * 1024
    assert len(json.dumps(candidate).encode()) <= 48000
    assert time.monotonic() - start < 12


def test_native_process_pages_do_not_overlap(fixture_config):
    remote = connector(fixture_config)
    first = remote.read("process.list", {"limit": 10})
    second = remote.read("process.list", {"limit": 10, "cursor": first["next_cursor"]})
    assert len(first["processes"]) == len(second["processes"]) == 10
    assert not {p["id"] for p in first["processes"]} & {p["id"] for p in second["processes"]}


def test_actual_timeout_is_bounded_and_abrupt_output_is_failed(fixture_config):
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        connector(fixture_config, port=32023, timeout_seconds=1).read("host.info", {})
    assert time.monotonic() - start < 4
    with pytest.raises(ValueError):
        connector(fixture_config, port=32024).read("host.info", {})


def test_actual_runtime_audit_and_redaction(client, fixture_config):
    profile = onboard(client)
    settings = get_settings()
    settings.ssh_enabled = True
    settings.ssh_credentials = json.dumps({"v22-fixture": fixture_config["key_path"]})
    runtime = RuntimeService(Database(settings.database_path), settings)
    target = runtime.create(profile["business_id"], RuntimeTargetCreate(**fixture_config["config"]))
    good = runtime.execute(profile["business_id"], target["id"], ToolRequest(tool_id="config.read", arguments={"path": "/srv/atr-fixture/app.conf"}))
    assert "synthetic-fixture-password" not in json.dumps(good)
    assert "[REDACTED]" in good["payload"]["text"]
    denied = runtime.execute(profile["business_id"], target["id"], ToolRequest(tool_id="config.read", arguments={"path": "/etc/passwd"}))
    assert denied["status"] == "denied"
    slow = runtime.create(profile["business_id"], RuntimeTargetCreate(**{**fixture_config["config"], "port": 32023, "timeout_seconds": 1}))
    timed = runtime.execute(profile["business_id"], slow["id"], ToolRequest(tool_id="host.info"))
    assert timed["status"] == "timed_out"
    with runtime.db.connect() as connection:
        rows = list(connection.execute("SELECT status,error,bytes FROM runtime_observations ORDER BY observed_at"))
        assert [r["status"] for r in rows] == ["succeeded", "denied", "timed_out"]
        assert all(r["status"] != "pending" for r in rows)
        assert rows[2]["error"] == "TimeoutError"
