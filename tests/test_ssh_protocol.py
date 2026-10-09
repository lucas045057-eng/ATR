"""Real SSH protocol against a loopback-only isolated server, not a Linux business host."""

import json
import socket
import threading
import time

import paramiko
import pytest
from app.connectors.models import RuntimeTargetCreate
from app.connectors.ssh import SSHConnector
from app.settings import Settings


def serve_once(listener, host_key, mode, captured, errors):
    class RestrictedServer(paramiko.ServerInterface):
        def check_auth_publickey(self, username, key):
            return paramiko.AUTH_SUCCESSFUL

        def get_allowed_auths(self, username):
            return "publickey"

        def check_channel_request(self, kind, channel_id):
            return (
                paramiko.OPEN_SUCCEEDED
                if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            )

        def check_channel_exec_request(self, channel, command):
            captured.append(command)
            return command == b"atr-read-v1"

    transport = None
    try:
        listener.settimeout(5)
        connection, _ = listener.accept()
        transport = paramiko.Transport(connection)
        transport.add_server_key(host_key)
        transport.start_server(server=RestrictedServer())
        channel = transport.accept(4)
        if channel is None:
            return
        data = bytearray()
        while len(data) < 4096:
            chunk = channel.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        captured.append(json.loads(data))
        if mode == "timeout":
            time.sleep(1.5)
        elif mode == "success":
            channel.sendall(json.dumps({"os": "loopback protocol fixture"}).encode())
            channel.send_exit_status(0)
            channel.close()
    except (EOFError, OSError, paramiko.SSHException) as exc:
        errors.append(type(exc).__name__)
    finally:
        if transport:
            transport.close()
        listener.close()


@pytest.mark.parametrize("mode", ["success", "wrong_key", "timeout"])
def test_pinned_ssh_fixed_json_entry(tmp_path, mode):
    key = paramiko.RSAKey.generate(2048)
    private = tmp_path / "test-only.key"
    key.write_private_key_file(str(private))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    captured, errors = [], []
    worker = threading.Thread(
        target=serve_once, args=(listener, key, mode, captured, errors), daemon=True
    )
    worker.start()
    pinned = paramiko.RSAKey.generate(2048) if mode == "wrong_key" else key
    config = RuntimeTargetCreate(
        name="loopback test",
        connector="ssh",
        host="127.0.0.1",
        port=port,
        username="atr-read",
        credential_ref="test",
        host_public_key=f"{pinned.get_name()} {pinned.get_base64()}",
        access_authorized=True,
        permission_boundary="forced_command_gateway",
        boundary_attestation="This isolated fixture accepts one fixed read-only entry.",
        timeout_seconds=1,
    ).model_dump()
    connector = SSHConnector(
        config, Settings(ssh_enabled=True, ssh_credentials=json.dumps({"test": str(private)}))
    )
    try:
        if mode == "success":
            assert connector.read("host.info", {})["os"] == "loopback protocol fixture"
            assert captured == [b"atr-read-v1", {"tool_id": "host.info", "arguments": {}}]
        elif mode == "wrong_key":
            with pytest.raises(paramiko.BadHostKeyException):
                connector.read("host.info", {})
            assert captured == []
        else:
            with pytest.raises(TimeoutError):
                connector.read("host.info", {})
    finally:
        worker.join(5)
        assert not worker.is_alive()
