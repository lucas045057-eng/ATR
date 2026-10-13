"""Dependency-free, fixed remote reads, shared by ordinary SSH and the gateway."""
from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROTOCOL = 'atr-read-ab-v1'
SCHEMA_VERSION = 1
TOOLS = {'capabilities': 1, 'health.get': 1, 'container.status': 2, 'container.logs': 2,
         'service.status': 2, 'service.logs': 2, 'process.status': 1, 'log.tail': 1}
CAP = 48000
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}$')
CID = re.compile(r'^[a-f0-9]{12,64}$')
FIXED_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')


def capabilities(legacy_tools=()):
    return {'protocol': PROTOCOL, 'schema_version': SCHEMA_VERSION,
            'tools': {**{tool: 1 for tool in legacy_tools}, **TOOLS},
            'time_format': 'ISO8601-aware', 'max_output_bytes': CAP,
            'tool_timeout_seconds': 8, 'observation_location': 'remote_ssh'}


def command(argv, deadline, include_stderr=False):
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                               env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    selector = selectors.DefaultSelector()
    data, errors = bytearray(), bytearray()
    try:
        for stream in (process.stdout, process.stderr):
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise TimeoutError('Remote read deadline')
            for key, _ in selector.select(.05):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    (data if key.fileobj is process.stdout else errors).extend(chunk)
                if len(data) + len(errors) > CAP - 3000:
                    raise ValueError('Remote output limit')
        if process.wait(timeout=max(.01, deadline-time.monotonic())):
            raise PermissionError('Fixed read failed')
        if include_stderr and errors:
            data.extend(b'\n' + errors)
        return data.decode('utf-8', errors='strict')
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()


def validate_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(ord(c) <= 32 for c in value) or len(value) > 1000):
        raise ValueError('Only an exact credential-free health URL is allowed')
    parsed.port  # Reject malformed ports.
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def health(endpoint, deadline):
    url = validate_url(endpoint['url'])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'Accept': 'application/json,text/plain;q=0.8'}, method='GET')
    try:
        response = opener.open(request, timeout=max(.01, min(3, deadline-time.monotonic())))
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = bytearray()
        while len(raw) < 8001:
            if time.monotonic() >= deadline:
                raise TimeoutError('Health read deadline')
            chunk = response.read1(min(4096, 8001-len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        return {'endpoint_id': endpoint['endpoint_id'], 'url': url,
                'status_code': response.code, 'redirected': 300 <= response.code < 400,
                'truncated': len(raw) > 8000, 'partial': len(raw) > 8000,
                'body': bytes(raw[:8000]).decode('utf-8', errors='replace')}


def container_status(cid, deadline):
    # Docker's environment, mounts, health output and credentials are never requested.
    template = ('{"id":{{json .Id}},"name":{{json .Name}},"status":{{json .State.Status}},'
                '"started_at":{{json .State.StartedAt}},"finished_at":{{json .State.FinishedAt}},'
                '"exit_code":{{json .State.ExitCode}},"health":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}},'
                '"networks":{{json .NetworkSettings.Networks}},'
                '"compose_project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                '"compose_service":{{json (index .Config.Labels "com.docker.compose.service")}}}')
    row = json.loads(command(['/usr/bin/docker', 'inspect', '--format', template, cid], deadline))
    if not isinstance(row, dict) or not CID.fullmatch(str(row.get('id', ''))) or not row['id'].startswith(cid):
        raise ValueError('Container identity mismatch')
    row['networks'] = {name: {key: item.get(key) for key in ('IPAddress', 'Aliases', 'DNSNames')}
                       for name, item in (row.get('networks') or {}).items()}
    row.update(state=row.pop('status'), container_id=row.pop('id'))
    row['name'] = row['name'].lstrip('/')
    row['startup_cycle'] = hashlib.sha256((row['container_id'] + ':' + row['started_at']).encode()).hexdigest()
    row['text'] = row['state'] + '\n'
    return row


def service_status(name, deadline):
    text = command(['/usr/bin/systemctl', 'show', '--no-pager',
                    '--property=Id,ActiveState,SubState,InvocationID,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic,ExecMainPID,ExecMainCode,ExecMainStatus',
                    '--', name], deadline)
    row = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    if not NAME.fullmatch(row.get('Id', '')) or row['Id'] not in {name, name + '.service'}:
        raise ValueError('Service identity mismatch')
    return {'name': row['Id'], 'state': row.get('ActiveState'), 'sub_state': row.get('SubState'),
            'startup_cycle': row.get('InvocationID') or None,
            'started_at_monotonic': row.get('ExecMainStartTimestampMonotonic'),
            'pid': row.get('ExecMainPID'), 'exit_code': row.get('ExecMainStatus'), 'text': text}


def process_identity(pid):
    path = f'/proc/{pid}/stat'
    with open(path, 'r', encoding='utf-8') as stream:
        text = stream.read(4097)
    if len(text) > 4096 or not text.startswith(str(pid) + ' ('):
        raise ValueError('Invalid process identity')
    close = text.rfind(')')
    fields = text[close+2:].split()
    if len(fields) < 20:
        raise ValueError('Incomplete process identity')
    with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as stream:
        boot_id = stream.read(80).strip()
    return {'pid': pid, 'name': text[text.find('(')+1:close], 'state': 'exited' if fields[0] in {'Z', 'X'} else 'running',
            'process_state': fields[0],
            'start_time': boot_id + ':' + fields[19]}


def secure_file(path, roots):
    from pathlib import PurePosixPath
    candidate = PurePosixPath(path)
    if (not candidate.is_absolute() or '..' in path.split('/')
            or not any(candidate.is_relative_to(PurePosixPath(root)) for root in roots)):
        raise PermissionError('Log path outside policy')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(candidate.parts[1:]):
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                            (os.O_DIRECTORY if index < len(candidate.parts)-2 else 0), dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def execute(policy, tool, args, *, secure_open=None):
    keys = {'capabilities': set(), 'health.get': {'endpoint_id'}, 'container.status': {'container_id'},
            'container.logs': {'container_id', 'tail'}, 'service.status': {'name'},
            'service.logs': {'name', 'tail'}, 'process.status': {'pid', 'start_time'}, 'log.tail': {'file_id', 'tail'}}
    if tool not in keys or type(args) is not dict or set(args) - keys[tool]:
        raise PermissionError('Unknown remote tool or arguments')
    tail = args.get('tail', 80)
    if type(tail) is not int or not 1 <= tail <= 200:
        raise ValueError('Invalid log bound')
    deadline = time.monotonic() + 8
    if tool == 'capabilities':
        return capabilities(policy.get('legacy_tools', ()))
    if tool == 'health.get':
        endpoint = next((row for row in policy.get('health_endpoints', [])
                         if row['endpoint_id'] == args.get('endpoint_id')), None)
        if endpoint is None:
            raise PermissionError('Endpoint outside server policy')
        result = health(endpoint, deadline)
    elif tool.startswith('container.'):
        cid = args.get('container_id', '')
        if (not isinstance(cid, str) or not CID.fullmatch(cid) or cid not in policy.get('containers', [])
                or not policy.get('docker_reads_enabled')):
            raise PermissionError('Container outside server policy')
        result = container_status(cid, deadline)
        if tool.endswith('logs'):
            before = result
            started = before['started_at']
            if started.startswith('0001-'):
                result = {**before, 'text': '', 'partial': True, 'limit_reason': 'no_startup_cycle'}
            else:
                text = command(['/usr/bin/docker', 'logs', '--timestamps', '--since', started,
                                '--tail', str(tail), cid], deadline, True)
                after = container_status(cid, deadline)
                result = {**before, 'text': text, 'cycle_verified': before['startup_cycle'] == after['startup_cycle'],
                          'partial': before['startup_cycle'] != after['startup_cycle']}
    elif tool.startswith('service.'):
        name = args.get('name', '')
        if not isinstance(name, str) or not NAME.fullmatch(name) or name not in policy.get('services', []):
            raise PermissionError('Service outside server policy')
        result = service_status(name, deadline)
        if tool.endswith('logs'):
            before = result
            cycle = before['startup_cycle']
            if not cycle or not re.fullmatch(r'[a-f0-9]{32}', cycle):
                result = {**before, 'text': '', 'partial': True, 'limit_reason': 'no_startup_cycle'}
            else:
                text = command(['/usr/bin/journalctl', '--no-pager', '--output=json', '-n', str(tail),
                                '-u', name, '_SYSTEMD_INVOCATION_ID=' + cycle], deadline)
                events, lines = [], []
                for line in text.splitlines():
                    row = json.loads(line)
                    micros = int(row['__REALTIME_TIMESTAMP'])
                    stamp = datetime.fromtimestamp(micros/1000000, timezone.utc).isoformat()
                    message = row.get('MESSAGE', '')
                    if not isinstance(message, str) or row.get('_SYSTEMD_INVOCATION_ID') != cycle:
                        raise ValueError('Journal identity mismatch')
                    events.append({'event_time': stamp, 'startup_cycle': cycle, 'raw_line': line})
                    lines.append(stamp + ' ' + message)
                after = service_status(name, deadline)
                result = {**before, 'text': '\n'.join(lines), 'events': events,
                          'cycle_verified': cycle == after['startup_cycle'], 'partial': cycle != after['startup_cycle']}
    elif tool == 'process.status':
        pid, start = args.get('pid'), args.get('start_time')
        if type(pid) is not int or not 1 <= pid <= 4194304 or not isinstance(start, str):
            raise ValueError('Process requires PID and start identity')
        allowed = any(row.get('pid') == pid and row.get('start_time') == start for row in policy.get('processes', []))
        if not allowed:
            raise PermissionError('Process outside server policy')
        try:
            result = process_identity(pid)
        except FileNotFoundError:
            result = {'pid': pid, 'start_time': start, 'state': 'exited'}
        if result['start_time'] != start:
            raise PermissionError('Process identity was reused')
        result['startup_cycle'] = start
        result['text'] = result['state']
    else:
        source = next((row for row in policy.get('log_files', []) if row.get('file_id') == args.get('file_id')), None)
        if source is None or secure_open is None:
            raise PermissionError('Log source outside server policy')
        fd = secure_open(source['path'], policy.get('allowed_paths', []))
        import stat
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError('Only regular logs')
            offset = max(0, info.st_size - (CAP - 3000))
            os.lseek(fd, offset, os.SEEK_SET)
            raw = os.read(fd, CAP-3000)
            if offset:
                raw = raw.partition(b'\n')[2]
            text = '\n'.join(raw.decode('utf-8', errors='strict').splitlines()[-tail:])
            result = {'file_id': source['file_id'], 'text': text, 'startup_cycle': source.get('startup_cycle'),
                      'partial': bool(offset), 'truncated': bool(offset), 'file_identity': [info.st_dev, info.st_ino]}
        finally:
            os.close(fd)
    result.update(protocol=PROTOCOL, tool_version=TOOLS[tool], observed_host=socket.gethostname(),
                  observation_location='remote_ssh', observed_at=datetime.now(timezone.utc).isoformat())
    if len(json.dumps(result).encode()) > CAP:
        raise ValueError('Remote response limit')
    return result


def remote_main():
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError('Remote request limit')
    request = json.loads(raw)
    # Ordinary SSH gets only the already checked whitelist from the trusted client.
    from_root = request['policy']
    result = execute(from_root, request['tool_id'], request['arguments'], secure_open=secure_file)
    sys.stdout.write(json.dumps(result))
