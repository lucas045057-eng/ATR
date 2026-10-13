import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.connectors.capabilities import decode, require_tool, validate_capabilities
from app.connectors.diagnostics import CapabilityUnavailable, ToolParseError
from app.connectors.remote_reads import PROTOCOL, capabilities, execute
from app.connectors.ssh import SSHConnector


def reader(monkeypatch, responses):
    instance = SSHConnector({'_read_protocol': 'ab_v1', 'timeout_seconds': 10}, None)
    sent = []
    @contextmanager
    def connection():
        yield object()
    def receive(client, command, request, **kwargs):
        sent.append(json.loads(request))
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value).encode()
    monkeypatch.setattr(instance, 'connection', connection)
    monkeypatch.setattr(instance, 'receive', receive)
    return instance, sent


def test_negotiated_tool_uses_explicit_protocol(monkeypatch):
    payload = {'protocol': PROTOCOL, 'tool_version': 2, 'state': 'exited', 'observation_location': 'remote_ssh'}
    instance, sent = reader(monkeypatch, [capabilities(), payload])
    assert instance.read('container.status', {'container_id': 'a'*64})['state'] == 'exited'
    assert sent[-1]['protocol'] == PROTOCOL


def test_old_gateway_support_is_bounded_and_new_tools_are_gaps(monkeypatch):
    denied = PermissionError('unavailable')
    denied.remote_error_type = 'PermissionError'
    instance, sent = reader(monkeypatch, [denied, {'text': 'Linux 6.1 x86_64\n'}, {'state': 'running', 'text': 'running\n'}])
    assert instance.read('container.status', {})['read_adapter'] == 'tested-json-v1'
    assert 'protocol' not in sent[-1]
    instance, sent = reader(monkeypatch, [denied, {'text': 'Linux 6.1 x86_64\n'}])
    with pytest.raises(CapabilityUnavailable):
        instance.read('health.get', {'endpoint_id': 'health'})
    assert len(sent) == 2


@pytest.mark.parametrize('payload', [b'not JSON', b'\xff', b'[]', b'{}'])
def test_garbage_is_not_legacy_success(payload):
    with pytest.raises(ToolParseError):
        validate_capabilities(decode(payload))


def test_incompatible_capabilities_never_fall_back(monkeypatch):
    instance, sent = reader(monkeypatch, [{**capabilities(), 'schema_version': 999}])
    with pytest.raises(ToolParseError):
        instance.read('host.info', {})
    assert len(sent) == 1
    with pytest.raises(CapabilityUnavailable):
        require_tool({**capabilities(), 'tools': {'container.status': 1}}, 'container.status')


def test_current_logs_are_bound_to_two_matching_identity_reads(monkeypatch):
    from app.connectors import remote_reads
    statuses = [{'started_at': '2026-10-13T00:00:00Z', 'startup_cycle': 'old', 'container_id': 'a'*64},
                {'started_at': '2026-10-13T00:00:01Z', 'startup_cycle': 'new', 'container_id': 'a'*64}]
    calls = []
    monkeypatch.setattr(remote_reads, 'container_status', lambda *args: statuses.pop(0))
    def read(argv, *args):
        calls.append(argv)
        return '2026-10-13T00:00:00Z startup failed'
    monkeypatch.setattr(remote_reads, 'command', read)
    result = execute({'docker_reads_enabled': True, 'containers': ['a'*64]},
                     'container.logs', {'container_id': 'a'*64})
    assert result['partial'] and not result['cycle_verified']
    assert calls[0][calls[0].index('--since')+1] == '2026-10-13T00:00:00Z'


def test_legacy_denial_is_classified_by_real_channel_reader():
    pending = [b'PermissionError\n']
    channel = SimpleNamespace(settimeout=lambda value: None, exec_command=lambda value: None,
                              shutdown_write=lambda: None, recv_ready=lambda: False,
                              recv_stderr_ready=lambda: bool(pending), recv_stderr=lambda size: pending.pop(),
                              exit_status_ready=lambda: True, recv_exit_status=lambda: 1, close=lambda: None)
    client = SimpleNamespace(get_transport=lambda: SimpleNamespace(open_session=lambda **kwargs: channel))
    connector = SSHConnector({'timeout_seconds': 10, 'max_output_bytes': 48000}, None)
    with pytest.raises(PermissionError) as rejected:
        connector.receive(client, 'atr-read-v1')
    assert rejected.value.remote_error_type == 'PermissionError'
