import importlib.util
import json
import sys


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
