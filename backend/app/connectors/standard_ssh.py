"""Ordinary SSH: fixed read commands; no server-side isolation claim."""
from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

from app.connectors.diagnostics import ToolParseError
from app.connectors.docker_reader import COMMAND as DOCKER_LIST_COMMAND
from app.connectors.docker_reader import parse_list
from app.connectors.mock import inventory_payload
from app.connectors.policy import check
from app.connectors.ssh import SSHConnector, SSHOutputLimit
from app.connectors.unit_names import valid_service_name

HIGH_RISK_GROUPS = {"root", "sudo", "wheel", "admin", "docker", "lxd", "lxc", "disk", "shadow", "libvirt"}
IDENTITY_COMMANDS = ("/usr/bin/uname -s", "/usr/bin/id -u", "/usr/bin/id -un",
                     "/usr/bin/id -g", "/usr/bin/id -G", "/usr/bin/id -Gn")
COMMANDS = {
    "host.info": ("/usr/bin/uname", "-s", "-n", "-r", "-m"),
    "host.memory": ("/usr/bin/free", "-b"),
    "host.disk": ("/usr/bin/df", "-P", "-h"),
    "process.list": ("/usr/bin/ps", "-eo", "pid,comm,stat", "--no-headers", "--sort=pid"),
    "network.listeners": ("/usr/bin/ss", "-lntu"),
    "service.list": ("/usr/bin/env", "LC_ALL=C", "LANG=C", "SYSTEMD_COLORS=0", "SYSTEMD_URLIFY=0",
                     "SYSTEMD_PAGER=cat", "/usr/bin/systemctl", "list-units", "--type=service", "--all", "--no-pager",
                     "--no-legend", "--plain", "--full"),
    "container.list": ("/usr/bin/docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"),
}

# Optional existing python3 only, never installed remotely. Walk by file descriptors
# to reject symlinks/FIFOs and avoid a realpath/head race. Requests are JSON on stdin.
FILE_READER = '''import os, stat, json, sys
r=json.loads(sys.stdin.buffer.read(8193))
p=r["path"]
assert p.startswith("/") and ".." not in p.split("/")
assert any(p==root or p.startswith(root.rstrip("/")+"/") for root in r["roots"])
fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
try:
 parts=[x for x in p.split("/") if x]
 for i,part in enumerate(parts):
  child=os.open(part,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|(os.O_DIRECTORY if i<len(parts)-1 else 0),dir_fd=fd)
  os.close(fd)
  fd=child
 mode=os.fstat(fd)
 if r["tool"]=="config.list":
  assert stat.S_ISDIR(mode.st_mode)
  files=[]
  with os.scandir(fd) as entries:
   for entry in entries:
    if entry.is_file(follow_symlinks=False): files.append(p.rstrip("/")+"/"+entry.name)
    if len(files)>100: raise ValueError("Directory limit")
  result={"files":sorted(files),"recursive":False}
 else:
  assert r["tool"]=="config.read" and stat.S_ISREG(mode.st_mode) and mode.st_size<=r["cap"]
  raw=os.read(fd,r["cap"]+1)
  assert len(raw)<=r["cap"]
  result={"path":p,"text":raw.decode("utf-8",errors="strict")}
 out=json.dumps(result).encode()
 assert len(out)<=r["cap"]
 sys.stdout.buffer.write(out)
finally:
 os.close(fd)
'''


class IdentityRejected(PermissionError):
    code = "ssh_identity_rejected"


def identity(values, username):
    if len(values) != 6:
        raise IdentityRejected("SSH 身份检查无法完成")
    os_name, uid, user, gid, gids, groups = [v.strip() for v in values]
    numbers, names = gids.split(), groups.split()
    if (os_name != "Linux" or not re.fullmatch(r"[0-9]{1,10}", uid)
            or not re.fullmatch(r"[0-9]{1,10}", gid) or int(uid) == 0 or int(gid) == 0
            or user != username or user.casefold() == "root" or not numbers or not names
            or len(numbers) != len(names) or gid not in numbers or len(names) > 128
            or any(not re.fullmatch(r"[0-9]{1,10}", n) or int(n) == 0 for n in numbers)
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", n) for n in names)
            or HIGH_RISK_GROUPS & {n.casefold() for n in names}):
        raise IdentityRejected("SSH 身份检查未通过")
    return {"uid": int(uid), "username": user, "primary_gid": int(gid),
            "primary_group": names[numbers.index(gid)], "groups": names, "group_ids": [int(n) for n in numbers],
            "precheck": "passed", "server_enforced_readonly": False,
            "warning": "UID 和权限组检查不证明账号无 sudoers、ACL、polkit 或其他写权限"}


def command_for(tool, args):
    if tool in {'capabilities', 'health.get', 'process.status', 'log.tail'}:
        source = Path(__file__).with_name('remote_reads.py').read_text(encoding='utf-8')
        return shlex.join(('/usr/bin/python3', '-I', '-c', source + '\nremote_main()'))
    if tool in COMMANDS:
        return shlex.join(COMMANDS[tool])
    if tool == "service.status":
        argv = ("/usr/bin/systemctl", "show", "--no-pager", "--property=ActiveState,SubState,Id", "--", args["name"])
    elif tool == "service.logs":
        argv = ("/usr/bin/journalctl", "--no-pager", "--output=short-iso", "-n", str(args["tail"]), "-u", args["name"])
    elif tool == "container.status":
        argv = ("/usr/bin/docker", "inspect", "--format", "{{.State.Status}}", args["container_id"])
    elif tool == "container.logs":
        argv = ("/usr/bin/docker", "logs", "--timestamps", "--tail", str(args["tail"]), args["container_id"])
    elif tool in {"config.list", "config.read"}:
        argv = ("/usr/bin/python3", "-I", "-c", FILE_READER)
    else:
        raise PermissionError("工具不在固定只读命令目录")
    return shlex.join(argv)


def parse(tool, raw, args):
    text = raw.decode("utf-8", errors="strict")
    if "\x00" in text or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise ValueError("远程工具返回非法控制字符")
    result = {"text": text}
    lines = [line for line in text.splitlines() if line.strip()]
    if tool in {"config.list", "config.read"}:
        result = json.loads(text)
        expected = {"path", "text"} if tool == "config.read" else {"files", "recursive"}
        if not isinstance(result, dict) or set(result) != expected:
            raise ValueError("远程读取结构不合法")
        if tool == "config.read" and (result.get("path") != args["path"] or not isinstance(result.get("text"), str)):
            raise ValueError("远程文件读取结构不合法")
        if tool == "config.list" and (not isinstance(result.get("files"), list) or result.get("recursive") is not False
            or len(result["files"]) > 100 or any(not isinstance(p, str) or not p.startswith(args["path"].rstrip("/") + "/")
                                                 for p in result["files"])):
            raise ValueError("远程目录结构不合法")
    elif tool == "host.info":
        fields = text.split()
        if len(fields) != 4 or fields[0] != "Linux":
            raise ValueError("主机信息格式不兼容")
        result.update(os=fields[0], hostname=fields[1], kernel=fields[2], architecture=fields[3])
    elif tool == "host.memory":
        row = next((line.split()[1:] for line in lines if line.startswith("Mem:")), [])
        if len(row) != 6 or any(not v.isascii() or not v.isdecimal() for v in row):
            raise ValueError("内存信息格式不兼容")
        result["memory_bytes"] = dict(zip(("total", "used", "free", "shared", "buff_cache", "available"), map(int, row)))
    elif tool == "host.disk":
        disks = []
        for line in lines[1:]:
            row = line.split(maxsplit=5)
            if len(row) != 6 or not re.fullmatch(r"[0-9]{1,3}%", row[4]) or int(row[4][:-1]) > 100:
                raise ValueError("磁盘信息格式不兼容")
            disks.append(dict(zip(("filesystem", "size", "used", "available", "use_percent", "mountpoint"), row)))
        if not disks:
            raise ValueError("磁盘信息为空")
        result["disks"] = disks
    elif tool in {"process.list", "service.list", "container.list"}:
        items = []
        invalid_lines = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            fields = line.split()
            if tool == "process.list":
                if (len(fields) not in {2, 3} or not re.fullmatch(r"[0-9]{1,10}", fields[0]) or len(fields[1]) > 128
                        or (len(fields) == 3 and not re.fullmatch(r"[RSDZTtWXIP][<NLSsl+]{0,8}", fields[2]))):
                    raise ValueError("进程清单格式不兼容")
                item = {"id": fields[0], "pid": int(fields[0]), "name": fields[1], "kind": "process",
                        "state": fields[2] if len(fields) == 3 else "unknown"}
            elif tool == "service.list":
                fields = line.split(maxsplit=4)
                if (len(fields) < 4 or not valid_service_name(fields[0]) or not fields[0].endswith(".service")
                        or fields[1] not in {"loaded", "not-found", "bad-setting", "error", "merged", "masked", "stub"}
                        or fields[2] not in {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "refreshing"}
                        or not re.fullmatch(r"[a-z][a-z-]{0,47}", fields[3])):
                    invalid_lines.append(line_number)
                    if len(invalid_lines) > 16:
                        raise ToolParseError(line_number=line_number, invalid_item_count=len(invalid_lines), output_bytes=len(raw))
                    continue
                item = {"id": fields[0], "name": fields[0].removesuffix(".service"), "kind": "service",
                        "load": fields[1], "state": fields[2], "active": fields[2], "sub": fields[3],
                        "description": fields[4] if len(fields) > 4 else ""}
            else:
                row = json.loads(line)
                if (not isinstance(row, dict) or not re.fullmatch(r"[a-f0-9]{12,64}", str(row.get("ID", "")))
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(row.get("Names", "")))):
                    raise ValueError("容器清单格式不兼容")
                item = {"id": row["ID"], "name": row["Names"], "kind": "container", "state": row.get("State", "unknown"), "image": row.get("Image", "")}
            items.append(item)
        field = {"process.list": "processes", "service.list": "services", "container.list": "containers"}[tool]
        result = inventory_payload(field, items, args)
        if invalid_lines:
            if not items:
                raise ToolParseError(line_number=invalid_lines[0], invalid_item_count=len(invalid_lines), output_bytes=len(raw))
            result.update(partial=True, invalid_records=len(invalid_lines), invalid_line_numbers=invalid_lines,
                          limit_reason="invalid_records")
    elif tool == "network.listeners":
        listeners = []
        for line in lines[1:]:
            row = line.split()
            if len(row) < 6 or not re.fullmatch(r"[0-9]{1,5}", row[4].rsplit(":", 1)[-1]):
                raise ValueError("监听端口格式不兼容")
            port = int(row[4].rsplit(":", 1)[-1])
            if not 0 <= port <= 65535:
                raise ValueError("监听端口越界")
            listeners.append({"port": port, "protocol": row[0]})
        result["listeners"] = listeners
    elif tool == "service.status":
        fields = dict(line.split("=", 1) for line in lines if "=" in line)
        if fields.get("Id") != args["name"] or fields.get("ActiveState") not in {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "refreshing"}:
            raise ValueError("服务状态格式不兼容")
        result.update(name=fields["Id"], state=fields["ActiveState"], sub_state=fields.get("SubState"))
    elif tool == "container.status":
        if text.strip() not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
            raise ValueError("容器状态格式不兼容")
        result["state"] = text.strip()
    return result


class StandardSSHConnector(SSHConnector):
    def check_boundary(self):
        if (self.config.get("execution_mode") != "standard_ssh_readonly"
                or self.config.get("permission_boundary") != "client_readonly"
                or len(self.config.get("boundary_attestation", "")) < 10):
            raise PermissionError("普通 SSH 需明确授权并声明客户端只读边界")

    def read(self, tool, args):
        args = check(self.config, tool, args, self.config.get("_approved", ()))
        if tool in {'capabilities', 'health.get', 'process.status', 'log.tail'} and self.config.get('_read_protocol') != 'ab_v1':
            from app.connectors.diagnostics import CapabilityUnavailable
            raise CapabilityUnavailable('新固定工具需要明确的 ab_v1 读取协议')
        restricted = self.config.get("docker_reader") == "restricted_list" and tool == "container.list"
        enhanced = self.config.get('_read_protocol') == 'ab_v1' and tool in {
            'capabilities', 'health.get', 'container.status', 'container.logs', 'service.status',
            'service.logs', 'process.list', 'process.status', 'log.tail'}
        command = DOCKER_LIST_COMMAND if restricted else '' if enhanced else command_for(tool, args)
        deadline, budget = time.monotonic() + self.config["timeout_seconds"], [0]
        with self.connection() as client:
            try:
                values = [self.receive(client, cmd, deadline=deadline, budget=budget).decode("utf-8", errors="strict")
                          for cmd in IDENTITY_COMMANDS]
                account = identity(values, self.config["username"])
            except TimeoutError as exc:
                exc.code, exc.stage = "ssh_identity_timeout", "identity"
                raise
            except (ValueError, UnicodeError, PermissionError) as exc:
                from app.connectors.credentials import CredentialUnavailable
                if isinstance(exc, SSHOutputLimit):
                    exc.code, exc.stage = "ssh_identity_output_limit", "identity"
                    raise
                if isinstance(exc, CredentialUnavailable):
                    raise
                raise IdentityRejected("SSH 身份检查无法完成") from exc
            stdin = None
            if self.config.get('_read_protocol') == 'ab_v1' and tool in {
                'capabilities', 'health.get', 'container.status', 'container.logs', 'service.status',
                'service.logs', 'process.list', 'process.status', 'log.tail',
            }:
                from app.connectors.capabilities import (
                    decode,
                    require_tool,
                    validate_capabilities,
                    validate_payload,
                )
                source = Path(__file__).with_name('remote_reads.py').read_text(encoding='utf-8')
                command = shlex.join(('/usr/bin/python3', '-I', '-c', source + '\nremote_main()'))
                approved = self.config.get('_approved', ())
                policy = {'services': [v for k, v in approved if k == 'service'],
                          'containers': [v for k, v in approved if k == 'container'],
                          'docker_reads_enabled': self.config['docker_reads_enabled'],
                          'health_endpoints': self.config.get('health_endpoints', []),
                          'allowed_paths': self.config['allowed_paths'],
                          'processes': [{'pid':int(v.split(':',1)[0]),'start_time':v.split(':',1)[1]}
                                        for k,v in approved if k=='process'],
                          'log_files': self.config.get('log_files', [])}

                def read_remote(identifier, arguments):
                    request = json.dumps({'policy': policy, 'tool_id': identifier, 'arguments': arguments}).encode()
                    if len(request) > 16384:
                        raise ValueError('固定读取授权清单过大')
                    return decode(self.receive(client, command, request, deadline=deadline, budget=budget))
                caps = validate_capabilities(read_remote('capabilities', {}))
                require_tool(caps, tool)
                result = caps if tool == 'capabilities' else read_remote(tool, args)
                if tool != 'capabilities':
                    validate_payload(tool, args, result, self.config)
                result.update(capabilities=caps, connector='ssh', identity=account)
                return result
            if tool.startswith("config."):
                stdin = json.dumps({"tool": tool, "path": args["path"], "roots": self.config["allowed_paths"],
                                    "cap": self.config["max_output_bytes"]}).encode()
                if len(stdin) > 8192:
                    raise ValueError("授权目录请求过大")
            raw = self.receive(client, command, stdin, deadline=deadline, budget=budget,
                               include_stderr=tool == "container.logs")
            try:
                result = parse_list(raw, args) if restricted else parse(tool, raw, args)
            except (ValueError, UnicodeError) as exc:
                if isinstance(exc, ToolParseError):
                    raise
                raise ToolParseError(output_bytes=len(raw)) from exc
            result["output_bytes"] = budget[0]
            result.update(connector="ssh", execution_mode="standard_ssh_readonly", identity=account,
                          access_authorized=True, server_enforced_readonly=False)
            return result
