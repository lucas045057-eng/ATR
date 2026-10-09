"""Optional Linux forced-command gateway, installed/configured manually by the server owner.

No dependency installation is required. Supply --policy /etc/atr-read/policy.json.
Use a dedicated unprivileged SSH user and disable forwarding/PTY/user commands.
The policy must be owned by an administrator and unwritable by that user.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
ID = re.compile(r"^[a-f0-9]{12,64}$")
CURSOR = re.compile(r"^(?:[01]:[A-Za-z0-9][A-Za-z0-9_.@-]{0,127})?$")
CAP = 48000
SCAN_RECORDS = 2048
SCAN_BYTES = 1024 * 1024
RECORD_BYTES = 4096


def inventory_item(tool, line):
    if tool == "container.list":
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("ID"), str):
            raise ValueError("Invalid container row")
        if not ID.fullmatch(row["ID"]) or not isinstance(row.get("Names"), str):
            raise ValueError("Invalid container identity")
        image, state = row.get("Image", ""), row.get("State", "unknown")
        if not isinstance(image, str) or not isinstance(state, str):
            raise ValueError("Invalid container metadata")
        if not NAME.fullmatch(row["Names"]) or len(image) > 240 or len(state) > 32:
            raise ValueError("Unbounded container metadata")
        return {
            "id": row["ID"],
            "name": row["Names"],
            "image": image,
            "kind": "container",
            "state": state,
        }
    parts = line.split()
    if tool == "service.list":
        if len(parts) < 3 or not NAME.fullmatch(parts[0]) or len(parts[2]) > 32:
            raise ValueError("Invalid service row")
        return {
            "id": parts[0],
            "name": parts[0].removesuffix(".service"),
            "kind": "service",
            "state": parts[2],
        }
    if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdecimal():
        raise ValueError("Invalid process row")
    if len(parts[0]) > 128 or len(parts[1]) > 128:
        raise ValueError("Unbounded process metadata")
    return {"id": parts[0], "name": parts[1], "kind": "process"}


def inventory_key(item, hints):
    identities = {item["id"].casefold(), item["name"].casefold().removesuffix(".service")}
    exact = any(h.casefold().removesuffix(".service") in identities for h in hints)
    return (0 if exact else 1, item["id"])


def bounded_inventory(argv, tool, args, authorized=(), timeout=8):
    """Parse incrementally; retain one page, never the complete native command output."""
    limit = args.get("limit", 60)
    hints = [*args.get("name_hints", []), *authorized]
    cursor = args.get("cursor", "")
    after = (int(cursor[0]), cursor[2:]) if cursor else None
    items, pending = {}, bytearray()
    scanned = raw_bytes = invalid = 0
    reason, has_more = None, False

    def accept(line):
        nonlocal scanned, invalid, reason, has_more
        if not line.strip():
            return
        scanned += 1
        try:
            item = inventory_item(tool, line.decode("utf-8", errors="strict"))
            key = inventory_key(item, hints)
            if after is None or key > after:
                items[key] = item
                if len(items) > limit:
                    has_more = True
                if len(items) > limit + 1:
                    del items[max(items)]
        except (ValueError, TypeError, KeyError):
            invalid += 1
        if scanned >= SCAN_RECORDS:
            reason = "scan_limit"

    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        while selector.get_map() and reason is None:
            if time.monotonic() > deadline:
                reason = "timeout"
                break
            for key, _ in selector.select(0.1):
                remaining = SCAN_BYTES - raw_bytes
                if remaining <= 0:
                    reason = "byte_limit"
                    break
                chunk = os.read(key.fileobj.fileno(), min(4096, remaining))
                raw_bytes += len(chunk)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is not process.stdout:
                    continue  # Count stderr against the byte budget without retaining it.
                pending.extend(chunk)
                while (newline := pending.find(b"\n")) >= 0:
                    if newline > RECORD_BYTES:
                        reason = "record_limit"
                        break
                    line = bytes(pending[:newline])
                    del pending[: newline + 1]
                    accept(line)
                    if reason:
                        break
                if reason is None and len(pending) > RECORD_BYTES:
                    reason = "record_limit"
                if reason:
                    break
        if reason is None:
            if pending:
                accept(bytes(pending))
            try:
                code = process.wait(timeout=max(0.01, min(1, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                reason = "timeout"
            else:
                if code and not (argv[0] == "/usr/bin/pgrep" and code == 1):
                    raise PermissionError("Read command failed")
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                reason = "timeout"
        process.stdout.close()
        process.stderr.close()
    selected = sorted(items)[:limit]
    field = {
        "service.list": "services",
        "process.list": "processes",
        "container.list": "containers",
    }[tool]
    payload = {
        field: [items[key] for key in selected],
        "truncated": bool(reason or has_more),
        "partial": bool(reason or has_more or invalid),
        "timeout": reason == "timeout",
        "filtered": bool(args.get("name_hints")),
        "scanned": scanned,
        "name_hints": args.get("name_hints", []),
        "raw_bytes": raw_bytes,
        "invalid_records": invalid,
        "limit_reason": reason or ("page_limit" if has_more else None),
        "next_cursor": None,
    }
    # Reserve room for transport timestamps/connector metadata within the existing 48KB cap.
    while len(json.dumps(payload).encode()) > CAP - 512 and selected:
        selected.pop()
        payload[field].pop()
        payload.update(truncated=True, partial=True, limit_reason="output_limit")
    if payload["truncated"] and selected:
        payload["next_cursor"] = f"{selected[-1][0]}:{selected[-1][1]}"
    return payload


def bounded_command(argv, timeout=8, include_stderr=False):
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    data, errors = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            if time.monotonic() > deadline:
                raise TimeoutError("Tool timeout")
            for key, _ in selector.select(0.1):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.fileobj is process.stdout:
                    data.extend(chunk)
                else:
                    errors.extend(chunk)
                if len(data) + len(errors) > CAP:
                    raise ValueError("Output limit")
        code = process.wait(timeout=1)
        if include_stderr and errors:
            data.extend(b"\n[stderr]\n" + errors)
        return code, data.decode("utf-8", errors="replace")
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def secure_open(path, allowed_roots):
    """Walk every component with O_NOFOLLOW/openat to prevent symlink races."""
    if not path.startswith("/") or ".." in path.split("/"):
        raise PermissionError("Path traversal")
    candidate = Path(path)
    if not any(candidate.is_relative_to(Path(root)) for root in allowed_roots):
        raise PermissionError("Path outside policy")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = candidate.parts[1:]
        for index, part in enumerate(parts):
            child = os.open(
                part,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | (os.O_DIRECTORY if index < len(parts) - 1 else 0),
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def execute(policy, tool, args):
    allowed_keys = {
        "host.info": set(),
        "host.memory": set(),
        "host.disk": set(),
        "network.listeners": set(),
        "process.list": {"limit", "name_hints", "cursor"},
        "service.list": {"limit", "name_hints", "cursor"},
        "container.list": {"limit", "name_hints", "cursor"},
        "config.list": {"path"},
        "config.read": {"path"},
        "service.status": {"name"},
        "service.logs": {"name", "tail"},
        "container.status": {"container_id"},
        "container.logs": {"container_id", "tail"},
    }
    if tool not in allowed_keys or set(args) - allowed_keys[tool]:
        raise PermissionError("Unknown tool or arguments")
    tail, limit = args.get("tail", 80), args.get("limit", 60)
    if (
        type(tail) is not int
        or not 1 <= tail <= 200
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("Invalid bounds")
    hints, cursor = args.get("name_hints", []), args.get("cursor", "")
    if (
        type(hints) is not list
        or len(hints) > 8
        or any(type(h) is not str or not NAME.fullmatch(h) for h in hints)
        or type(cursor) is not str
        or not CURSOR.fullmatch(cursor)
    ):
        raise ValueError("Invalid discovery hints or cursor")
    if tool.startswith("config."):
        fd = secure_open(args.get("path", ""), policy.get("allowed_paths", []))
        try:
            metadata = os.fstat(fd)
            if tool == "config.list":
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError("Directory required")
                names = []
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if len(names) >= 100:
                            break
                        if entry.is_file(follow_symlinks=False):
                            names.append(str(Path(args["path"]) / entry.name))
                return {"files": sorted(names), "recursive": False}
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > CAP:
                raise PermissionError("Only bounded regular configuration files")
            return {
                "path": args["path"],
                "text": os.read(fd, CAP).decode("utf-8", errors="replace"),
            }
        finally:
            os.close(fd)
    commands = {
        "host.info": ["/usr/bin/uname", "-srm"],
        "host.memory": ["/usr/bin/free", "-b"],
        "host.disk": ["/usr/bin/df", "-P", "-h"],
        "network.listeners": ["/usr/bin/ss", "-lntu"],
        "process.list": ["/usr/bin/ps", "-eo", "pid,comm", "--no-headers", "--sort=pid"],
        "service.list": [
            "/usr/bin/systemctl",
            "list-units",
            "--type=service",
            "--all",
            "--no-pager",
            "--no-legend",
            "--plain",
            "--full",
        ],
    }
    if tool in {"service.status", "service.logs"}:
        name = args.get("name", "")
        if not NAME.fullmatch(name) or name not in policy.get("services", []):
            raise PermissionError("Service outside server-side policy")
        commands[tool] = (
            ["/usr/bin/systemctl", "show", "--property=ActiveState,SubState,Id", "--", name]
            if tool.endswith("status")
            else [
                "/usr/bin/journalctl",
                "--no-pager",
                "--output=short-iso",
                "-n",
                str(tail),
                "-u",
                name,
            ]
        )
    if tool.startswith("container."):
        if not policy.get("docker_reads_enabled", False):
            raise PermissionError("Docker reads disabled")
        cid = args.get("container_id", "")
        if tool != "container.list" and (
            not ID.fullmatch(cid) or cid not in policy.get("containers", [])
        ):
            raise PermissionError("Container outside server-side policy")
        if tool == "container.list":
            commands[tool] = [
                "/usr/bin/docker",
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ]
        elif tool == "container.status":
            commands[tool] = ["/usr/bin/docker", "inspect", "--format", "{{.State.Status}}", cid]
        else:
            commands[tool] = ["/usr/bin/docker", "logs", "--timestamps", "--tail", str(tail), cid]
    if tool in {"service.list", "process.list", "container.list"}:
        if hints:
            if tool == "service.list":
                commands[tool].extend(["--", *[f"*{h}*" for h in hints]])
            elif tool == "process.list":
                commands[tool] = [
                    "/usr/bin/pgrep",
                    "-i",
                    "-l",
                    "--",
                    "|".join(re.escape(h) for h in hints),
                ]
            else:
                for hint in hints:
                    commands[tool].extend(["--filter", f"name={re.escape(hint)}"])
        authorized = (
            policy.get("services", ())
            if tool == "service.list"
            else (policy.get("containers", ()) if tool == "container.list" else ())
        )
        return bounded_inventory(commands[tool], tool, args, authorized)
    code, text = bounded_command(commands[tool], include_stderr=tool == "container.logs")
    if code:
        raise PermissionError("Read command failed")
    result = {"text": text}
    if tool.endswith("status"):
        result["state"] = next(
            (
                line.split("=", 1)[1]
                for line in text.splitlines()
                if line.startswith("ActiveState=")
            ),
            text.strip(),
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    options = parser.parse_args()
    if os.environ.get("SSH_ORIGINAL_COMMAND") != "atr-read-v1":
        raise PermissionError("Fixed SSH entry required")
    # Server-owned file; it is never supplied by the client or the model.
    with open(options.policy, encoding="utf-8") as stream:
        policy = json.load(stream)
    request_bytes = sys.stdin.buffer.read(4097)
    if len(request_bytes) > 4096:
        raise ValueError("Request limit")
    request = json.loads(request_bytes)
    if set(request) != {"tool_id", "arguments"} or not isinstance(request["arguments"], dict):
        raise ValueError("Invalid request schema")
    payload = execute(policy, request["tool_id"], request["arguments"])
    payload["observed_at"] = datetime.now(timezone.utc).isoformat()
    output = json.dumps(payload).encode()
    if len(output) > 64000:
        raise ValueError("Response limit")
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        sys.exit(1)
