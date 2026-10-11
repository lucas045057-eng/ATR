"""Explicit opt-in tests against an isolated OpenSSH fixture. Password supplied in memory by harness."""
import base64
import json
import time

import paramiko
import pytest
from app.connectors import credentials
from app.connectors.credentials import SSH_SESSIONS, CredentialUnavailable
from app.connectors.ssh import SSHConnector
from app.settings import Settings
from linux_ssh_acceptance import fixture_config  # noqa: F401
from pydantic import SecretStr

PASSWORD = None


def password_connector(fixture_config, password=None, **changes):  # noqa: F811
    assert PASSWORD, "Run scripts/verify_ssh_password.py; never supply a password in CLI args or env"
    cfg = {**fixture_config["config"], "id": "password-linux-fixture", "port": 32025,
           "username": "atrpassword", "auth_mode": "password", **changes}
    settings = Settings(ssh_enabled=True)
    cfg["credential_ref"] = SSH_SESSIONS.put(cfg, settings.operator_id, SecretStr(password or PASSWORD))
    return SSHConnector(cfg, settings)


def raw_client(fixture_config, username="atrpassword"):  # noqa: F811
    cfg = fixture_config["config"]
    key_type, encoded = cfg["host_public_key"].split()
    client = paramiko.SSHClient()
    client.get_host_keys().add("[127.0.0.1]:32025", key_type, paramiko.RSAKey(data=base64.b64decode(encoded)))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect("127.0.0.1", port=32025, username=username, password=PASSWORD,
                       allow_agent=False, look_for_keys=False, timeout=3, auth_timeout=3, banner_timeout=3)
    except Exception:
        client.close()
        raise
    return client


def test_password_login_and_fixed_read(fixture_config):  # noqa: F811
    remote = password_connector(fixture_config)
    assert "Linux" in remote.read("host.info", {})["text"]
    assert "PORT=8123" in remote.read("config.read", {"path": "/srv/atr-fixture/app.conf"})["text"]


def test_wrong_password(fixture_config):  # noqa: F811
    with pytest.raises(paramiko.AuthenticationException):
        password_connector(fixture_config, password="incorrect-synthetic").read("host.info", {})


def test_wrong_host_key_with_password(fixture_config):  # noqa: F811
    wrong = paramiko.RSAKey.generate(2048)
    with pytest.raises(paramiko.BadHostKeyException):
        password_connector(fixture_config, host_public_key=f"{wrong.get_name()} {wrong.get_base64()}").read("host.info", {})


def test_root_denied_on_server_and_client(fixture_config):  # noqa: F811
    with pytest.raises(PermissionError, match="root"):
        password_connector(fixture_config, username="root").read("host.info", {})
    with pytest.raises(paramiko.AuthenticationException):
        raw_client(fixture_config, username="root")


@pytest.mark.parametrize("tool,args", [
    ("shell.exec", {"command": "touch /srv/atr-fixture/owned"}),
    ("service.restart", {"name": "ssh"}), ("service.logs", {"name": "store.service"}),
    ("container.list", {}), ("config.read", {"path": "/etc/shadow"}),
])
def test_password_gateway_policy_is_strict(fixture_config, tool, args):  # noqa: F811
    with pytest.raises(PermissionError):
        password_connector(fixture_config, allowed_paths=["/"]).read(tool, args)


@pytest.mark.parametrize("command", ["touch /srv/atr-fixture/owned", "sudo id", "scp -t /srv/atr-fixture/app.conf"])
def test_password_forcecommand_rejects_other_exec(fixture_config, command):  # noqa: F811
    client = raw_client(fixture_config)
    try:
        _, out, _ = client.exec_command(command, timeout=3)
        assert out.channel.recv_exit_status() != 0
    finally:
        client.close()


def test_password_sftp_shell_pty_and_forwarding_rejected(fixture_config):  # noqa: F811
    client = raw_client(fixture_config)
    try:
        with pytest.raises((paramiko.SSHException, EOFError, OSError)):
            client.open_sftp()
        channel = client.get_transport().open_session(timeout=3)
        with pytest.raises(paramiko.SSHException):
            channel.get_pty()
        channel.close()
        channel = client.get_transport().open_session(timeout=3)
        channel.settimeout(3)
        channel.invoke_shell()
        assert channel.recv_exit_status() != 0
        channel.close()
        with pytest.raises(paramiko.ChannelException):
            client.get_transport().open_channel("direct-tcpip", ("127.0.0.1", 22), ("127.0.0.1", 12000), timeout=3)
        with pytest.raises(paramiko.SSHException):
            client.get_transport().request_port_forward("127.0.0.1", 0)
    finally:
        client.close()


def test_expired_session_cannot_reconnect(fixture_config, monkeypatch):  # noqa: F811
    monkeypatch.setattr(credentials, "TTL_SECONDS", 1)
    remote = password_connector(fixture_config)
    assert "Linux" in remote.read("host.info", {})["text"]
    time.sleep(1.05)
    with pytest.raises(CredentialUnavailable):
        remote.read("host.info", {})
    assert PASSWORD not in json.dumps(SSH_SESSIONS.status(remote.config, remote.settings.operator_id))
