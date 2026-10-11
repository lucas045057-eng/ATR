"""Real local OpenSSH + real sudo authorization, synthetic list-only container data."""
import json

import paramiko
import pytest
import standard_ssh_acceptance
from app.connectors.diagnostics import CapabilityUnavailable

connector = standard_ssh_acceptance.connector
fixture_config = standard_ssh_acceptance.fixture_config


@pytest.mark.parametrize("auth", ["password", "private_key"])
def test_real_sudo_list_only(fixture_config, auth, monkeypatch):
    captured = []
    original = paramiko.Channel.exec_command
    def record(self, command):
        captured.append(command)
        return original(self, command)
    monkeypatch.setattr(paramiko.Channel, "exec_command", record)
    result = connector(fixture_config, auth, docker_reader="restricted_list").read("container.list", {})
    assert result["source"] == "restricted_docker_reader" and result["capabilities"] == ["container.list"]
    assert captured[-1] == "sudo -n /usr/local/sbin/atr-docker-read list"
    assert [r["name"] for r in result["containers"]] == ["catalog-api", "unrelated-backup"]
    assert "docker" not in result["identity"]["groups"]
    for tool in ("container.status", "container.logs"):
        before = len(captured)
        with pytest.raises(CapabilityUnavailable):
            connector(fixture_config, auth, docker_reader="restricted_list").read(tool, {"container_id": "a" * 64})
        assert len(captured) == before
    assert "unrelated-backup" in json.dumps(result)


@pytest.mark.parametrize("command", ["sudo -n /usr/local/sbin/atr-docker-read status",
    "sudo -n /usr/local/sbin/atr-docker-read logs", "sudo -n /usr/local/sbin/atr-docker-read list --all",
    "sudo -n /usr/bin/docker ps", "sudo -n /bin/touch /tmp/atr-write-denied"])
def test_server_sudoers_reject_other_operations(fixture_config, command):
    remote = connector(fixture_config)
    with remote.connection() as client:
        with pytest.raises(PermissionError):
            remote.receive(client, command)
