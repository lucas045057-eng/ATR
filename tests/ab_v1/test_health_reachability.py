import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from app.connectors.remote_reads import execute


def probe(url):
    return execute({'health_endpoints': [{'endpoint_id': 'entry', 'url': url}]},
                   'health.get', {'endpoint_id': 'entry'})


def assert_refused(result, url):
    assert {key: result[key] for key in (
        'endpoint_id', 'url', 'reachable', 'failure_kind', 'partial', 'truncated'
    )} == {'endpoint_id': 'entry', 'url': url, 'reachable': False,
          'failure_kind': 'connection_refused', 'partial': False, 'truncated': False}
    assert 'status_code' not in result
    assert 'body' not in result


def test_closed_loopback_port_returns_reachability_fact():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}/health'
    result = probe(url)
    assert_refused(result, url)
    assert result['protocol'] == 'atr-read-ab-v1'
    assert result['observed_at']


@pytest.mark.parametrize('status', [200, 503, 302])
def test_real_http_response_preserves_contract(status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/health'
            self.send_response(status)
            if status == 302:
                self.send_header('Location', '/must-not-follow')
            self.end_headers()
            self.wfile.write(b'health response')

        def log_message(self, *args):
            pass

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/health'
            result = probe(url)
        finally:
            server.shutdown()
            thread.join(timeout=3)
    assert result['endpoint_id'] == 'entry' and result['url'] == url
    assert result['status_code'] == status
    assert result['body'] == 'health response'
    assert result['redirected'] is (status == 302)
    assert result['partial'] is False and result['truncated'] is False


@pytest.mark.parametrize('wrapped', [True, False])
def test_refusal_does_not_expose_exception_details(monkeypatch, capsys, wrapped):
    secret = 'credential=private-marker'
    error = ConnectionRefusedError(secret)
    if wrapped:
        error = urllib.error.URLError(error)

    def fail_open(self, request, **kwargs):
        raise error

    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fail_open)
    url = 'http://127.0.0.1:12345/health'
    result = probe(url)
    assert_refused(result, url)
    captured = capsys.readouterr()
    assert secret not in json.dumps(result) + captured.out + captured.err


@pytest.mark.parametrize('error', [
    urllib.error.URLError(socket.gaierror('DNS failed')),
    urllib.error.URLError(TimeoutError('deadline')),
    urllib.error.URLError('Connection refused'),
    PermissionError('identity failure'),
    TimeoutError('deadline'),
])
def test_non_refusal_errors_remain_tool_errors(monkeypatch, error):
    def fail_open(self, request, **kwargs):
        raise error

    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fail_open)
    with pytest.raises(type(error)) as caught:
        probe('http://127.0.0.1:12345/health')
    assert caught.value is error
