"""Legacy list-only sudo gateway; no flags, shell parameters or CLI fallback."""
import re

from app.connectors.diagnostics import ToolParseError
from app.connectors.mock import inventory_payload

COMMAND = "sudo -n /usr/local/sbin/atr-docker-read list"


def parse_list(raw, args):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ToolParseError(output_bytes=len(raw)) from exc
    if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise ToolParseError(output_bytes=len(raw))
    items, invalid = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = re.split(r"\t+| {2,}", line.strip(), maxsplit=2)
        if len(fields) != 3:
            fields = line.split(maxsplit=2)
        if fields and fields[0].upper() in {"NAME", "NAMES"} and len(fields) == 3 and fields[1].upper() == "IMAGE" and fields[2].upper() == "STATUS":
            continue
        state = None
        if len(fields) == 3:
            status = fields[2]
            for prefix, value in (("Up", "running"), ("Exited", "exited"), ("Created", "created"),
                                  ("Restarting", "restarting"), ("Paused", "paused"), ("Dead", "dead"),
                                  ("Removing", "removing"), ("running", "running"), ("exited", "exited"),
                                  ("created", "created"), ("paused", "paused")):
                if status == prefix or status.startswith(prefix + " "):
                    state = value
                    break
        if (len(fields) != 3 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", fields[0])
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}", fields[1])
                or state is None or len(fields[2]) > 256 or any(c["id"] == fields[0] for c in items)):
            invalid.append(number)
            if len(invalid) > 16:
                raise ToolParseError(line_number=number, invalid_item_count=len(invalid), output_bytes=len(raw))
            continue
        items.append({"id": fields[0], "identity_kind": "name", "name": fields[0], "kind": "container",
                      "image": fields[1], "state": state, "status": fields[2], "capabilities": ["container.list"]})
    if invalid and not items:
        raise ToolParseError(line_number=invalid[0], invalid_item_count=len(invalid), output_bytes=len(raw))
    result = inventory_payload("containers", items, args)
    result.update(source="restricted_docker_reader", protocol="legacy_text_list", capabilities=["container.list"],
                  server_enforced_operations=["list"])
    if invalid:
        result.update(partial=True, invalid_records=len(invalid), invalid_line_numbers=invalid, limit_reason="invalid_records")
    return result
