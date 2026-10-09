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
CAP = 48000


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
        "process.list": {"limit"},
        "service.list": {"limit"},
        "container.list": {"limit"},
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
        "process.list": ["/usr/bin/ps", "-eo", "pid,comm", "--no-headers"],
        "service.list": [
            "/usr/bin/systemctl",
            "list-units",
            "--type=service",
            "--all",
            "--no-pager",
            "--no-legend",
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
    code, text = bounded_command(commands[tool], include_stderr=tool == "container.logs")
    if code:
        raise PermissionError("Read command failed")
    if tool == "service.list":
        return {
            "services": [
                {
                    "id": p[0],
                    "name": p[0].removesuffix(".service"),
                    "kind": "service",
                    "state": p[2] if len(p) > 2 else "unknown",
                }
                for line in text.splitlines()[:limit]
                if (p := line.split())
            ]
        }
    if tool == "process.list":
        return {
            "processes": [
                {"id": p[0], "name": p[1], "kind": "process"}
                for line in text.splitlines()[:limit]
                if len(p := line.split()) == 2
            ]
        }
    if tool == "container.list":
        items = [json.loads(line) for line in text.splitlines()[:limit]]
        return {
            "containers": [
                {
                    "id": i["ID"],
                    "name": i["Names"],
                    "image": i["Image"],
                    "kind": "container",
                    "state": i.get("State", "unknown"),
                }
                for i in items
            ]
        }
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
