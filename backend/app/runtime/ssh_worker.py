"""One approved SSH read. Authentication arrives over anonymous stdin only."""
from __future__ import annotations

import json
import re
import sys
import time
from types import SimpleNamespace

from pydantic import SecretStr

from app.connectors import credentials
from app.connectors.policy import check
from app.connectors.ssh import SSHConnector
from app.connectors.standard_ssh import StandardSSHConnector
from app.tools.registry import sanitize_payload


def encode_response(response: dict, password: str) -> str:
    def redact(value):
        if isinstance(value, str):
            return value.replace(password, "[REDACTED]") if password else value
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value
    return json.dumps(redact(response), ensure_ascii=False)


def main():
    authentication = {}
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("SSH request limit")
        request = json.loads(raw)
        config = request["config"]
        approved = {tuple(item) for item in request["approved"]}
        args = check(config, request["tool_id"], request["arguments"], approved)
        authentication = request["authentication"]
        owner = request["owner"]
        settings = SimpleNamespace(ssh_enabled=True, operator_id=owner, ssh_credentials="{}")
        if config.get("auth_mode") == "password":
            ttl = authentication["deadline"] - time.monotonic()
            if not isinstance(ttl, (int, float)) or ttl <= 0:
                raise credentials.CredentialUnavailable()
            credentials.TTL_SECONDS = min(ttl, config["timeout_seconds"])
            config["credential_ref"] = credentials.SSH_SESSIONS.put(config, owner, SecretStr(authentication["password"]))
        else:
            settings.ssh_credentials = json.dumps({config["credential_ref"]: authentication["key_filename"]})
        connector = (SSHConnector(config, settings) if config.get("execution_mode") == "forced_gateway"
                     else StandardSSHConnector({**config, "_approved": approved}, settings))
        response = {"payload": sanitize_payload(connector.read(request["tool_id"], args))}
        encoded = encode_response(response, authentication.get("password", ""))
    except Exception as exc:
        code = getattr(exc, "code", "ssh_tool_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "ssh_tool_failed"
        encoded = json.dumps({"error_type": type(exc).__name__, "error_code": code})
    finally:
        credentials.SSH_SESSIONS.clear()
    sys.stdout.buffer.write(encoded.encode("utf-8"))


if __name__ == "__main__":
    main()
