import json

import pytest
from app.connectors import remote_reads

CID = 'a' * 64


def inspect_result(monkeypatch, bindings, state='running'):
    row = {'id': CID, 'name': '/isolated-http', 'status': state,
           'started_at': '2026-10-15T00:00:00Z', 'finished_at': '0001-01-01T00:00:00Z',
           'exit_code': 0, 'health': None, 'networks': {},
           'compose_project': None, 'compose_service': None, 'port_bindings': bindings}

    def command(argv, deadline):
        assert argv[:3] == ['/usr/bin/docker', 'inspect', '--format']
        assert argv[-1] == CID
        assert '.HostConfig.PortBindings' in argv[3]
        assert not any(field in argv[3] for field in ('.Config.Env', '.Mounts', '.State.Health.Log'))
        return json.dumps(row)

    monkeypatch.setattr(remote_reads, 'command', command)
    return remote_reads.container_status(CID, 1)


@pytest.mark.parametrize('state', ['running', 'exited'])
def test_published_entry_survives_container_stop(monkeypatch, state):
    result = inspect_result(monkeypatch, {
        '8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '19000', 'ignored': 'secret'}],
    }, state)
    assert result['state'] == state
    assert result['published_endpoints'] == [
        {'host_ip': '127.0.0.1', 'host_port': 19000, 'container_port': 8000, 'protocol': 'tcp'},
    ]
    assert 'port_bindings' not in result
    assert 'secret' not in json.dumps(result)


def test_wildcard_ipv6_and_udp_keep_their_exact_protocol(monkeypatch):
    result = inspect_result(monkeypatch, {
        '8000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '1'},
                     {'HostIp': '::', 'HostPort': '65535'},
                     {'HostIp': '0:0:0:0:0:0:0:1', 'HostPort': '19001'}],
        '8000/udp': [{'HostIp': '127.0.0.1', 'HostPort': '19000'}],
    })
    assert result['published_endpoints'] == [
        {'host_ip': '0.0.0.0', 'host_port': 1, 'container_port': 8000, 'protocol': 'tcp'},
        {'host_ip': '::', 'host_port': 65535, 'container_port': 8000, 'protocol': 'tcp'},
        {'host_ip': '::1', 'host_port': 19001, 'container_port': 8000, 'protocol': 'tcp'},
        {'host_ip': '127.0.0.1', 'host_port': 19000, 'container_port': 8000, 'protocol': 'udp'},
    ]


@pytest.mark.parametrize('bindings', [None, {}, {'8000/tcp': None}, {'8000/tcp': []}])
def test_empty_bindings_do_not_invent_a_published_entry(monkeypatch, bindings):
    assert inspect_result(monkeypatch, bindings)['published_endpoints'] == []


@pytest.mark.parametrize('entry', [
    {'HostIp': '', 'HostPort': '19000'},
    {'HostIp': '127.0.0.1', 'HostPort': ''},
    {'HostIp': '', 'HostPort': ''},
])
def test_unspecified_docker_binding_cannot_prove_an_exact_entry(monkeypatch, entry):
    assert inspect_result(monkeypatch, {'8000/tcp': [entry]})['published_endpoints'] == []


@pytest.mark.parametrize('bindings', [
    [], '', False, 8000,
    {'8000/tcp': {}}, {'8000/tcp': ''}, {'8000/tcp': False},
    {'8000/tcp': [None]}, {'8000/tcp': [[]]}, {'8000/tcp': ['127.0.0.1:19000']},
    {'8000/tcp': [{}]},
])
def test_invalid_binding_structure_is_rejected(monkeypatch, bindings):
    with pytest.raises(ValueError):
        inspect_result(monkeypatch, bindings)


@pytest.mark.parametrize('port', ['0/tcp', '65536/tcp', '-1/tcp', '8000', '8000/TCP',
                                  '8000/http', '8000/tcp/udp', '8.0/tcp', ' 8000/tcp',
                                  '8000\n/tcp', '\u0668\u0660/tcp'])
def test_invalid_container_port_or_protocol_is_rejected(monkeypatch, port):
    with pytest.raises(ValueError):
        inspect_result(monkeypatch, {port: [{'HostIp': '127.0.0.1', 'HostPort': '19000'}]})


@pytest.mark.parametrize('port', [None, True, 19000, 19000.0, [], {}, '0', '65536',
                                  '-1', '19.0', ' 19000', '19000\n', '\u0661\u0669'])
def test_invalid_host_port_is_rejected_without_coercion(monkeypatch, port):
    with pytest.raises(ValueError):
        inspect_result(monkeypatch, {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': port}]})


@pytest.mark.parametrize('ip', [None, True, 2130706433, [], {}, 'localhost',
                                '127.0.0.999', '127.0.0.1:19000', '[::1]', '::gg',
                                'fe80::1%eth0', '127.0.0.1\n'])
def test_invalid_host_ip_is_rejected_without_coercion(monkeypatch, ip):
    with pytest.raises(ValueError):
        inspect_result(monkeypatch, {'8000/tcp': [{'HostIp': ip, 'HostPort': '19000'}]})
