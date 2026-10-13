"""Parent-only credential grants to the short-lived trusted SSH process."""
from __future__ import annotations

import json
import sys

from app.connectors.credentials import SSH_SESSIONS
from app.connectors.policy import check
from app.runtime.processes import run_bounded


def run_ssh_action(config: dict, settings, tool_id: str, arguments: dict, approved: set,
                   *, cancelled=None) -> dict:
    args = check(config, tool_id, arguments, approved)
    if config.get("auth_mode") == "password":
        authentication = SSH_SESSIONS.grant(config, settings.operator_id)
    else:
        key_filename = json.loads(settings.ssh_credentials).get(config.get("credential_ref"))
        if not key_filename:
            raise PermissionError("SSH credential reference unavailable")
        authentication = {"key_filename": key_filename}
    request = {"config": config, "owner": settings.operator_id, "tool_id": tool_id,
               "arguments": args, "approved": sorted(approved), "authentication": authentication}
    result = run_bounded([sys.executable, "-m", "app.runtime.ssh_worker"],
                         json.dumps(request).encode(), timeout=config["timeout_seconds"],
                         max_output=config["max_output_bytes"] + 1024, cancelled=cancelled)
    response = json.loads(result["stdout"])
    if response.get("error_type"):
        if response["error_type"] in {"AuthenticationException", "BadHostKeyException"}:
            SSH_SESSIONS.revoke(config, settings.operator_id)
        error = PermissionError("SSH execution rejected")
        error.code = response.get("error_code", "ssh_tool_failed")
        raise error
    return response["payload"]
