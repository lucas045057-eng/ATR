import importlib.util
import json
import sys
import time
from types import SimpleNamespace

import pytest


def test_ssh_worker_rejects_unregistered_tool_before_any_connection():
    assert importlib.util.find_spec("app.runtime.ssh_worker") is not None
    from app.runtime.processes import run_bounded
    request = {"config": {"connector": "ssh", "execution_mode": "forced_gateway"},
               "owner": "operator", "tool_id": "shell.exec", "arguments": {}, "approved": [],
               "authentication": {"password": "synthetic-secret", "ttl": 10}}
    result = run_bounded([sys.executable, "-m", "app.runtime.ssh_worker"], json.dumps(request).encode(), timeout=3)
    response = json.loads(result["stdout"])
    assert response["error_type"] == "PermissionError"
    assert b"synthetic-secret" not in result["stdout"] + result["stderr"]


def test_global_ssh_disable_rejects_before_resolving_or_dispatching():
    from app.runtime.ssh_channel import run_ssh_action
    with pytest.raises(PermissionError, match="disabled|revoked"):
        run_ssh_action({"connector": "ssh", "access_authorized": True},
                       SimpleNamespace(ssh_enabled=False), "host.info", {}, set())


def test_response_redacts_passwords_before_json_escaping():
    from app.runtime import ssh_worker
    assert hasattr(ssh_worker, "encode_response")
    password = 'quoted"back\\slash'
    encoded = ssh_worker.encode_response({"payload": {"text": "echo " + password}}, password)
    assert password not in json.loads(encoded)["payload"]["text"]


def test_ssh_child_rejects_original_expiry_instead_of_renewing_ttl():
    from app.runtime.processes import run_bounded
    request = {"config": {"connector": "ssh", "auth_mode": "password", "timeout_seconds": 10,
                          "execution_mode": "forced_gateway"},
               "owner": "operator", "tool_id": "host.info", "arguments": {}, "approved": [],
               "authentication": {"password": "synthetic-expired-secret", "deadline": time.monotonic() - 1}}
    result = run_bounded([sys.executable, "-m", "app.runtime.ssh_worker"], json.dumps(request).encode(), timeout=3)
    assert json.loads(result["stdout"])["error_type"] == "CredentialUnavailable"
