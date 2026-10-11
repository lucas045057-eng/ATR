"""Bounded real A/B experiment, run inside the existing API container.

--prepare/--self-check never contact a provider. --run permits four calls once.
--probe permits one explicitly named hypothesis (at most two calls), once.
No raw prompt, response, Authorization, key, or provider error text is persisted.
"""

import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from app.agent.model_schema import validate_report
from app.context.budget import estimate_model_input
from app.context.model import _completion, _parse_content, model_messages
from app.context.model_errors import ModelCallError, diagnostic, usage_counts
from app.settings import Settings

FIELDS = ('model', 'temperature', 'max_tokens', 'max_completion_tokens',
          'max_output_tokens', 'response_format', 'text', 'store', 'stream', 'reasoning', 'reasoning_effort', 'seed', 'n')
HISTORY_RUN = 'd8006d1b-3699-4c8d-83c2-5cb8749be3fd'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def synthetic_state():
    catalog, store = 'synthetic-catalog-api', 'synthetic-store'
    nodes = [{'id': catalog, 'kind': 'service', 'name': 'catalog-api', 'role': 'application'},
             {'id': store, 'kind': 'service', 'name': 'store', 'role': 'dependency'}]
    state = {
        'adaptive': True,
        'incident': {'symptom': '合成实验：catalog-api 请求间歇 504；检查应用和依赖证据，不得直接宣称根因。',
                     'window_start': '2026-10-10T00:00:00+00:00'},
        'graph': {'nodes': nodes, 'edges': [{'from_node_id': catalog, 'to_node_id': store,
                  'relation': 'depends_on', 'certainty': 'confirmed'}]},
        'business_profile': {'name': 'Catalog-AB-Synthetic', 'synthetic': True,
            'description': '全合成目录查询应用，应用依赖 store；配置和拓扑不证明依赖当前故障。',
            'authorized_services': ['catalog-api'], 'unauthorized_services': ['store', 'payments'],
            'policy': '只允许当前候选动作；store 必须先申请范围并由操作者授权。无敏感数据。'},
        'runtime_bindings': [{'target_node_id': catalog, 'runtime_object': 'synthetic/catalog-api', 'authorized': True},
                             {'target_node_id': store, 'runtime_object': 'synthetic/store', 'authorized': False}],
        'evidence': [], 'available_actions': [], 'action_history': [],
        'previous_hypotheses': [], 'hypothesis_history': [], 'scope_requests': [],
        'code_context': [{'synthetic': True, 'path': 'synthetic/catalog.py',
                         'snippet': 'lookup_catalog -> store_client.lookup; health checks process only, not dependencies'}],
    }
    for index, (tool, payload) in enumerate([
        ('container.status', {'state': 'running', 'health': 'unhealthy'}),
        ('health.get', {'status_code': 503, 'dependency_check': False}),
        ('network.listeners', {'listening': True, 'port': 8080}),
    ]):
        state['evidence'].append({'evidence_id': f'synthetic-evidence-{index}', 'call_id': f'synthetic-call-{index}',
            'target_node_id': catalog, 'tool_id': tool, 'observed_at': '2026-10-10T00:05:00+00:00', 'payload': payload})
    for index in range(3, 10):
        lines = [f'2026-10-10T00:04:{j:02d}Z synthetic request-{index}-{j} catalog lookup store connection refused; no credentials'
                 for j in range(5)]
        state['evidence'].append({'evidence_id': f'synthetic-evidence-{index}', 'call_id': f'synthetic-call-{index}',
            'target_node_id': catalog, 'tool_id': 'container.logs', 'observed_at': '2026-10-10T00:05:00+00:00',
            'payload': {'text': '\n'.join(lines), 'synthetic': True}})
    for index, tool in enumerate(('container.status', 'container.logs', 'health.get')):
        state['available_actions'].append({'action_id': f'synthetic-action-{index}', 'tool_id': tool,
            'target_node_id': catalog, 'role': 'application',
            'scheduling': {'score': 0.7, 'permission_allowed': True, 'hypothesis_discrimination': 0.5, 'read_cost': 1}})
    return state


def scenarios(settings):
    """Capture the existing planner's prompt and compaction using synthetic state only."""
    from app.agent import workflow
    state = synthetic_state()
    captured = {}

    def capture(database, config, budget_id, prompt, context, validate=None):
        captured['messages'] = model_messages(prompt, context)
        return {'summary': 'synthetic', 'hypotheses': [], 'next_action': None, 'action': None}

    budget = SimpleNamespace(get=lambda bid: {'limits': {'max_input_tokens': settings.investigation_input_tokens}})
    with patch('app.context.model.call_json', capture), patch.object(workflow, 'ContextBudgetManager', return_value=budget):
        result = workflow._planner(settings, state, object(), 'synthetic-budget-not-persisted')
    assert 'model_error' not in result and captured['messages']
    return {'simple': ([{'role': 'user', 'content': '只回复 OK，不要解释。'}], None),
            'investigation': (captured['messages'], state)}


def payload(request):
    template = os.environ.get('ATR_AB_DIRECT_TEMPLATE')
    if template:
        wire = json.loads(template)
        assert set(wire['key_order']) <= set(FIELDS) | {'messages', 'input'}
        return {k: request['messages'] if k in {'messages', 'input'} else wire['parameters'][k]
                for k in wire['key_order']}
    if os.environ.get('ATR_AB_RESPONSES') == '1':
        body = {'model': request['model'].removeprefix('openai/'), 'input': request['messages'],
                'stream': False, 'max_output_tokens': request['max_tokens'], 'store': False}
        if 'response_format' in request:
            body['text'] = {'format': request['response_format']}
        effort = os.environ.get('ATR_AB_RESPONSES_EFFORT')
        if effort:
            assert effort == 'low'
            body['reasoning'] = {'effort': effort}
        return body
    # Explicit production call_json fields; the local preparation verifies this against SDK wire JSON.
    body = {'messages': request['messages'], 'model': request['model'].removeprefix('openai/'),
            'max_tokens': request['max_tokens'], 'stream': False}
    if 'response_format' in request:
        body['response_format'] = request['response_format']
    return body


def safe_wire(request):
    body = json.loads(request.content)
    address = urlsplit(str(request.url))
    assert not address.username and not address.password and not address.query
    messages = body.get('messages', body.get('input', []))
    return {'origin': f'{address.scheme}://{address.netloc}', 'path': address.path,
            'method': request.method, 'keys': sorted(body), 'key_order': list(body),
            'parameters': {k: body[k] for k in FIELDS if k in body},
            'messages_sha256': digest(canonical(messages)),
            'messages_content_bytes': sum(len(m.get('content', '').encode()) if isinstance(m.get('content'), str)
                                          else len(canonical(m.get('content'))) for m in messages),
            'request_body_bytes': len(request.content), 'wire_sha256': digest(request.content),
            'semantic_sha256': digest(canonical(body)),
            'headers': {k: request.headers.get(k) for k in ('content-type', 'user-agent', 'accept', 'accept-encoding',
                        'connection', 'x-stainless-retry-count', 'x-stainless-read-timeout') if k in request.headers},
            'timeout_extensions': request.extensions.get('timeout')}


def safe_response(response):
    result = {'http_status': response.status_code, 'http_version': response.http_version,
              'response_body_bytes': len(response.content), 'response_body_sha256': digest(response.content)}
    try:
        body = response.json()
        reported_model = body.get('model') if isinstance(body, dict) else None
        if isinstance(reported_model, str) and re.fullmatch(r'(?:gpt|claude|o[0-9]|gemini)[a-zA-Z0-9_.:/-]{1,110}', reported_model):
            if not any(os.environ.get(k) and os.environ[k] in reported_model for k in ('MODEL_API_KEY', 'OPERATOR_TOKEN', 'COLLECTOR_TOKEN')):
                result['provider_reported_model'] = reported_model
        error = body.get('error') if isinstance(body, dict) else None
        if isinstance(error, dict):
            known_codes = {'invalid_request_error', 'server_error', 'overloaded_error', 'authentication_error',
                           'rate_limit_error', 'model_not_found', 'context_length_exceeded', 'unsupported_parameter',
                           'invalid_value', 'bad_request', 'insufficient_quota'}
            result['provider_error_fields'] = {k: error[k] for k in ('type', 'code') if error.get(k) in known_codes}
            if error.get('param') in FIELDS or error.get('param') == 'messages':
                result['provider_error_fields']['param'] = error['param']
            message = error.get('message')
            if isinstance(message, str):
                # Provider errors can echo partial prompts: retain fixed reason enums only.
                result['provider_error_mentions'] = [term for term in
                    ('max_completion_tokens', 'max_tokens', 'response_format', 'temperature', 'stream',
                     'overloaded', 'timeout', 'json', '/v1/chat/completions', 'instruction', 'system', 'developer',
                     'user', 'role', 'input', 'message', 'content', 'required', 'empty', 'missing', 'model',
                     'unsupported', 'quota', 'credit', 'balance', 'limit', '上下文', '额度', '余额', '不足',
                     '参数', '模型', '角色', '非流式', '指令', '必须', '为空', '渠道', '分组', '可用', '不支持',
                     '仅支持', '接口', 'responses', 'chat', 'completions', '权限', '令牌', '无效', '不存在',
                     '负载', '提供', '需要', '指定', 'context', 'payload', 'service', 'channel', 'disabled') if term in message.casefold()]
                reasons = {'missing_instructions': r'instructions?.{0,60}(?:required|missing|empty)|(?:required|missing|empty).{0,60}instructions?',
                           'missing_system_message': r'system.{0,60}(?:required|missing|empty)',
                           'streaming_required': r'(?:stream.{0,40}(?:required|true)|only.{0,30}stream)',
                           'unsupported_model': r'model.{0,60}(?:not supported|unsupported|not found|does not exist)',
                           'insufficient_balance': r'(?:insufficient|not enough).{0,30}(?:balance|credit|quota)|余额不足',
                           'no_available_model_channel': r'(?:无可用|没有可用).{0,100}渠道|no available.{0,100}(?:channel|model)',
                           'responses_endpoint_required': r'(?:only|仅支持).{0,50}responses',
                           'unsupported_model_chinese': r'模型.{0,50}(?:不支持|不存在|无效)',
                           'model_price_not_configured': r'has not been priced by the administrator yet|价格尚未由管理员配置'}
                result['provider_error_reasons'] = [name for name, expression in reasons.items()
                                                    if re.search(expression, message, re.IGNORECASE)]
        raw = body.get('usage') if isinstance(body, dict) else None
        responses = isinstance(body, dict) and body.get('object') == 'response'
        if responses and isinstance(raw, dict):
            raw = {**raw, 'prompt_tokens': raw.get('input_tokens'), 'completion_tokens': raw.get('output_tokens'),
                   'prompt_tokens_details': raw.get('input_tokens_details'),
                   'completion_tokens_details': raw.get('output_tokens_details')}
            result['response_envelope'] = 'responses'
            result['response_status'] = body.get('status') if body.get('status') in {'completed', 'incomplete', 'failed', 'in_progress', 'queued'} else 'other'
        counts = usage_counts(raw)
        result['usage_kind'] = 'actual' if counts else 'unavailable'
        if counts:
            result['usage'] = {'prompt_tokens': counts[0], 'completion_tokens': counts[1]}
            for section, key in [('prompt_tokens_details', 'cached_tokens'), ('completion_tokens_details', 'reasoning_tokens')]:
                value = raw.get(section, {}).get(key) if isinstance(raw.get(section), dict) else None
                if type(value) is int and value >= 0:
                    result['usage'][key] = value
            total = raw.get('total_tokens')
            if type(total) is int and total >= 0:
                result['usage']['total_tokens'] = total
        finish = body.get('choices', [{}])[0].get('finish_reason')
        result['finish_reason'] = finish if finish in {'stop', 'length', 'content_filter', 'tool_calls', 'function_call'} else 'other'
    except (ValueError, TypeError, AttributeError, IndexError):
        result['http_envelope_json_valid'] = False
    return result


def install_observer(path, offline):
    original = httpx.Client.send
    original_init, original_single, original_exit = httpx.Client.__init__, httpx.Client._send_single_request, os._exit
    observer_started = time.monotonic()
    observed = {'offline': offline, 'http_sends': 0, 'physical_http_sends': 0,
                'events': [], 'client_initializations_ms': []}

    def initialize(client, *args, **kwargs):
        began = time.monotonic()
        original_init(client, *args, **kwargs)
        observed['client_initializations_ms'].append(round((time.monotonic() - began) * 1000, 3))

    def single(client, request):
        assert observed['physical_http_sends'] == 0, 'No retry or redirect follow-up permitted'
        observed['physical_http_sends'] += 1
        save(path, observed)
        return original_single(client, request)

    def finish(code):
        observed['observer_before_exit_ms'] = round((time.monotonic() - observer_started) * 1000, 3)
        if observed.get('http_finished_observer_ms') is not None:
            observed['post_http_to_worker_exit_ms'] = round(observed['observer_before_exit_ms'] - observed['http_finished_observer_ms'], 3)
        save(path, observed)
        original_exit(code)

    def send(client, request, *args, **kwargs):
        observed['http_sends'] += 1
        assert observed['http_sends'] == 1, 'No automatic retries or redirects permitted'
        alternate = os.environ.get('ATR_AB_EXPERIMENT_PATH')
        if alternate:
            assert alternate == '/v1/chat/completions'
            observed['experiment_path_intervention'] = {'original_path': request.url.path, 'experimental_path': alternate}
            request.url = request.url.copy_with(path=alternate)
        observed['wire'] = safe_wire(request)
        began = time.monotonic()
        observed['http_started_observer_ms'] = round((began - observer_started) * 1000, 3)

        def trace(name, info):
            event = {'name': name, 'ms': round((time.monotonic() - began) * 1000, 3)}
            if name.endswith('receive_response_headers.complete'):
                value = info.get('return_value')
                if isinstance(value, tuple) and len(value) > 1 and type(value[1]) is int:
                    observed['http_status'] = value[1]
            observed['events'].append(event)
            save(path, observed)

        request.extensions['trace'] = trace
        observed['request_started_at'] = datetime.now(timezone.utc).isoformat()
        save(path, observed)
        try:
            if offline:
                request_body = json.loads(request.content)
                content = 'OK' if 'response_format' not in json.loads(request.content) else json.dumps(
                    {'summary': 'synthetic', 'hypotheses': [], 'next_action': None, 'action': None})
                stub = {'id': 'synthetic-response',
                    'object': 'chat.completion', 'created': 1, 'model': 'gpt-6.1-sol',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15,
                              'prompt_tokens_details': {'cached_tokens': 0}}}
                if 'input' in request_body:
                    content = json.dumps({'summary': 'synthetic', 'hypotheses': [], 'next_action': None, 'action': None}) if 'text' in request_body else 'OK'
                    stub = {'id': 'resp_synthetic', 'object': 'response', 'created_at': 1, 'status': 'completed',
                            'model': 'gpt-6.1-sol', 'error': None, 'incomplete_details': None,
                            'output': [{'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                                        'content': [{'type': 'output_text', 'text': content, 'annotations': []}]}],
                            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}
                response = httpx.Response(200, request=request, json=stub)
                # Reproduce the SDK client's normal response hooks without any network call.
                for hook in client.event_hooks.get('response', []):
                    hook(response)
            else:
                response = original(client, request, *args, **kwargs)
            observed.update(safe_response(response))
            return response
        except Exception as exc:
            observed['transport_exception_type'] = type(exc).__name__
            raise
        finally:
            observed['http_send_total_ms'] = round((time.monotonic() - began) * 1000, 3)
            observed['http_finished_observer_ms'] = round((time.monotonic() - observer_started) * 1000, 3)
            save(path, observed)

    httpx.Client.__init__ = initialize
    httpx.Client._send_single_request = single
    httpx.Client.send = send
    os._exit = finish
    return observed


def direct_worker():
    from app.context.model_worker import provider_request_id
    output = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8')
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    started = time.monotonic()
    request = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024))
    timing, stage, usage, trace_id = {}, 'initialize', None, None
    request_started = None
    try:
        responses_worker = os.environ.get('ATR_AB_LITELLM_RESPONSES') == '1'
        if responses_worker:
            from litellm import responses
            from litellm.llms.custom_httpx.http_handler import HTTPHandler
        with httpx.Client(timeout=request['timeout'], follow_redirects=True,
                          limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100)) as client:
            timing['worker_initialize_ms'] = round((time.monotonic() - started) * 1000, 3)
            stage = 'request'
            output.write(json.dumps({'worker_stage': stage, **timing}) + '\n')
            output.flush()
            request_started = time.monotonic()
            address = request['api_base'].rstrip('/') + ('/v1/responses' if os.environ.get('ATR_AB_RESPONSES') == '1' else '/chat/completions')
            if responses_worker:
                options = payload(request)
                options['model'] = request['model']
                result = responses(**options, api_key=request['api_key'], api_base=request['api_base'].rstrip('/') + '/v1',
                                   timeout=request['timeout'], num_retries=0, client=HTTPHandler(client=client))
                body = result.model_dump()
            else:
                response = client.post(address, headers={'Authorization': 'Bearer ' + request['api_key']}, json=payload(request))
                trace_id = provider_request_id(response, request['api_key'])
                response.raise_for_status()
                body = response.json()
            timing['network_ms'] = round((time.monotonic() - request_started) * 1000, 3)
            stage = 'parse'
            raw_usage = body.get('usage') or {}
            counts = usage_counts({'prompt_tokens': raw_usage.get('input_tokens'), 'completion_tokens': raw_usage.get('output_tokens')}) if os.environ.get('ATR_AB_RESPONSES') == '1' else usage_counts(raw_usage)
            usage = {'prompt_tokens': counts[0], 'completion_tokens': counts[1]} if counts else None
            if os.environ.get('ATR_AB_RESPONSES') == '1':
                assert body.get('status') == 'completed' and not body.get('error')
                content = ''.join(c['text'] for item in body.get('output', []) if item.get('type') == 'message'
                                  for c in item.get('content', []) if c.get('type') == 'output_text')
                assert content
            else:
                content = body['choices'][0]['message']['content'] or ''
            assert len(content.encode()) <= request['max_tokens'] * 16
            envelope = {'content': content, 'usage': usage}
    except Exception as exc:
        if request_started:
            timing.setdefault('network_ms', round((time.monotonic() - request_started) * 1000, 3))
        error = diagnostic(exc, stage, http_status=exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError) else diagnostic(exc, stage)
        envelope = {'error': error, 'usage': usage}
    timing['worker_elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
    envelope.update(timing=timing, provider_request_id=trace_id)
    output.write(json.dumps(envelope, ensure_ascii=False))
    output.flush()
    os._exit(0)


def invoke(route, request, trace_path, offline=False, wire_template=None):
    original = subprocess.run

    def run(command, **kwargs):
        assert command == [sys.executable, '-m', 'app.context.model_worker']
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', route, str(trace_path),
                   'offline' if offline else 'real']
        return original(command, **kwargs)

    environment = {'ATR_AB_DIRECT_TEMPLATE': json.dumps(wire_template)} if wire_template else {}
    with patch.dict(os.environ, environment), patch('app.context.model.subprocess.run', run):
        return _completion(**request)


def request_for(settings, messages, scenario, offline=False):
    request = {'model': settings.model_name if '/' in settings.model_name else 'openai/' + settings.model_name,
               'api_base': settings.model_base_url, 'api_key': 'synthetic-key' if offline else settings.model_api_key.get_secret_value(),
               'timeout': settings.model_timeout_seconds, 'max_tokens': settings.model_max_output_tokens,
               'num_retries': 0, 'messages': messages}
    if scenario == 'investigation':
        request['response_format'] = {'type': 'json_object'}
    return request


def snapshot():
    c = sqlite3.connect('file:' + os.environ['DATABASE_PATH'] + '?mode=ro', uri=True, timeout=3)
    result = {'active_jobs': c.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall(), 'tables': {}}
    for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        rows = c.execute('SELECT * FROM "' + table + '"').fetchall()
        serialized = sorted(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
        result['tables'][table] = {'count': len(rows), 'sha256': digest(canonical(serialized))}
    result['artifact_files'] = {str(p.relative_to('/data')): digest(p.read_bytes())
                               for p in Path(os.environ['ARTIFACT_ROOT']).rglob('*') if p.is_file()}
    result['config'] = {k: os.environ.get(k) for k in ('MODEL_PROVIDER', 'MODEL_NAME', 'MODEL_BASE_URL',
                        'MODEL_TIMEOUT_SECONDS', 'MODEL_MAX_OUTPUT_TOKENS', 'EXTERNAL_MODEL_ENABLED')}
    history = []
    for (raw,) in c.execute('SELECT diagnostics_json FROM model_usage_records'):
        d = json.loads(raw)
        if d.get('investigation_run_id') == HISTORY_RUN:
            allowed = ('request_attempt_id', 'investigation_run_id', 'model', 'provider', 'error_category',
                       'exception_type', 'request_stage', 'elapsed_ms', 'worker_initialize_ms', 'network_ms',
                       'parse_ms', 'http_status', 'retry_count', 'usage_kind', 'started_at', 'ended_at')
            history.append({k: d.get(k) for k in allowed})
    result['historical_timeout'] = history
    c.close()
    return result


def prepare(folder, settings):
    before = snapshot()
    assert not before['active_jobs']
    if (folder / 'before-data.json').exists():
        if (folder / 'ledger.json').exists():
            assert not json.loads((folder / 'ledger.json').read_text())['calls'], 'Real slots already consumed'
        assert json.loads((folder / 'before-data.json').read_text()) == before
    else:
        save(folder / 'before-data.json', before)
    environment = {'python': sys.version.split()[0], 'packages': {k: importlib.metadata.version(k)
        for k in ('httpx', 'httpcore', 'openai', 'litellm', 'pydantic')},
        'config': before['config'], 'proxy_env_present': {k: bool(os.environ.get(k)) for k in
            ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR')},
        'worker_source_sha256': digest(Path('/app/app/context/model_worker.py').read_bytes()),
        'outer_source_sha256': digest(Path('/app/app/context/model.py').read_bytes())}
    save(folder / 'environment.json', environment)
    templates = {}
    for scenario, (messages, state) in scenarios(settings).items():
        request = request_for(settings, messages, scenario, offline=True)
        wires = {}
        for route in ('direct', 'worker'):
            trace_path = folder / f'offline-{scenario}-{route}.json'
            response = invoke(route, request, trace_path, offline=True)
            if state:
                validate_report(_parse_content(response.choices[0].message.content), state)
            else:
                assert response.choices[0].message.content == 'OK'
            trace = json.loads(trace_path.read_text())
            assert trace['offline'] and trace['http_sends'] == 1
            wires[route] = trace['wire']
        # Preserve nominal parameter differences, then align the direct wire request with the actual SDK JSON.
        template = {k: wires['worker'][k] for k in ('key_order', 'parameters')}
        templates[scenario] = template
        aligned_path = folder / f'offline-{scenario}-direct-aligned.json'
        invoke('direct', request, aligned_path, offline=True, wire_template=template)
        aligned = json.loads(aligned_path.read_text())['wire']
        assert aligned['semantic_sha256'] == wires['worker']['semantic_sha256']
        assert aligned['wire_sha256'] == wires['worker']['wire_sha256']
        assert wires['direct']['path'] == wires['worker']['path']
        print(json.dumps({'offline_scenario': scenario, 'aligned_wire_equal': True,
                         'path': wires['worker']['path'], 'parameters': wires['worker']['parameters'],
                         'body_bytes': wires['worker']['request_body_bytes'],
                         'atr_byte_based_input_estimate': estimate_model_input(messages)}, ensure_ascii=False), flush=True)
    save(folder / 'prepared-wire.json', templates)
    save(folder / 'ledger.json', {'max_real_calls': 4, 'calls': []})


def run_experiment(folder, settings, extra_path=False, probe=None):
    assert settings.model_timeout_seconds == 120 and settings.external_model_enabled
    assert settings.model_api_key.get_secret_value() and settings.model_provider == 'openai-compatible'
    cases = scenarios(settings)
    if probe and probe.get('compact_catalog'):
        from app.agent import workflow
        full_state = synthetic_state()
        full_state['evidence'] = full_state['evidence'][:4]
        full_state['evidence'][-1]['payload']['text'] = '2026-10-10T00:04:00Z synthetic catalog lookup store connection refused; no credentials'
        captured = {}
        def capture(database, config, budget_id, prompt, context, validate=None):
            captured['messages'] = model_messages(prompt, context)
            return {'summary': 'synthetic', 'hypotheses': [], 'next_action': None, 'action': None}
        budget = SimpleNamespace(get=lambda bid: {'limits': {'max_input_tokens': settings.investigation_input_tokens}})
        with patch('app.context.model.call_json', capture), patch.object(workflow, 'ContextBudgetManager', return_value=budget):
            assert 'model_error' not in workflow._planner(settings, full_state, object(), 'synthetic-only')
        cases['investigation'] = (captured['messages'], full_state)
    templates = json.loads((folder / 'prepared-wire.json').read_text())
    ledger_path = folder / 'ledger.json'
    ledger = json.loads(ledger_path.read_text())
    if probe:
        assert all(r['status'] in {'failed', 'completed'} for r in ledger['calls'])
        previous = {r.get('probe_name') for r in ledger['calls']}
        assert probe['name'] not in previous, 'Each hypothesis probe runs only once'
        sequence = [(probe.get('scenario', 'simple'), r) for r in probe['routes']]
        assert len(sequence) <= 2
        ledger.update(max_real_calls=len(ledger['calls']) + len(sequence),
                      additional_reason='User explicitly removed call cap; bounded hypothesis probe: ' + probe['name'])
    elif extra_path:
        assert len(ledger['calls']) == 4 and all(r['status'] in {'failed', 'completed'} for r in ledger['calls'])
        ledger.update(max_real_calls=6, additional_reason='Initial pairs returned HTTP 400/529. Test only the /v1 path difference reported successful by the user; two calls announced before execution.')
        sequence = [('simple', 'worker'), ('simple', 'direct')]
    else:
        assert not ledger['calls'], 'This experiment cannot be rerun or resumed automatically'
        sequence = [('simple', 'direct'), ('simple', 'worker'), ('investigation', 'worker'), ('investigation', 'direct')]
    for scenario, route in sequence:
        assert len(ledger['calls']) < ledger['max_real_calls']
        experiment_id = str(uuid.uuid4())
        record = {'experiment_id': experiment_id, 'scenario': scenario, 'route': route,
                  'ordinal': len(ledger['calls']) + 1, 'started_at': datetime.now(timezone.utc).isoformat(),
                  'inner_timeout_seconds': 120, 'outer_hard_timeout_seconds': 130, 'retry_count': 0, 'status': 'reserved'}
        record['variant'] = 'explicit_v1_path' if extra_path else 'current_atr_path'
        if probe:
            record['probe_name'] = probe['name']
        ledger['calls'].append(record)
        save(ledger_path, ledger)  # A failed or interrupted request still consumes its slot.
        print(json.dumps({'experiment_id': experiment_id, 'scenario': scenario, 'route': route, 'status': 'starting'}), flush=True)
        messages, state = cases[scenario]
        if probe and probe.get('add_system'):
            messages = [{'role': 'system', 'content': 'You are a concise assistant. Follow the user request.'}] + messages
        if probe and probe.get('minimal_investigation'):
            messages = model_messages('Return one JSON object with exactly summary:string, hypotheses:[], next_action:null, action:null. Do not invent tools or evidence.',
                                      {'synthetic': True, 'symptom': 'catalog-api unhealthy; store dependency not authorized; insufficient evidence'})
            state = synthetic_state()
        record['atr_byte_based_input_estimate'] = estimate_model_input(messages)
        # A model-specific tokenizer for this alias is not available; this is explicitly a proxy estimate.
        try:
            import tiktoken
            encoding = tiktoken.get_encoding('o200k_base')
            record['local_tokenizer_estimate'] = sum(len(encoding.encode(m['content'])) + 4 for m in messages) + 2
            record['local_tokenizer'] = 'o200k_base proxy, not provider billing or verified model tokenizer'
        except Exception:
            record['local_tokenizer_estimate'] = None
        record['messages_sha256'] = digest(canonical(messages))
        trace_path = folder / (experiment_id + '.json')
        start, parse_ms, validation_ms, stage = time.monotonic(), 0, 0, 'request'
        try:
            request = request_for(settings, messages, scenario)
            if probe and probe.get('formal_options'):
                options = probe['formal_options']
                assert set(options) <= {'wire_api', 'reasoning_effort'}
                assert options.get('wire_api', 'chat_completions') in {'chat_completions', 'responses'}
                assert options.get('reasoning_effort') == 'low'
                request.update(options)
            template = templates[scenario]
            if probe and probe.get('direct_template'):
                template = probe['direct_template'] if route in {'direct', 'direct_responses'} else None
            if probe and probe.get('wire_parameters'):
                template = dict(template, parameters={**template['parameters'], **probe['wire_parameters']})
            environment = {'ATR_AB_EXPERIMENT_PATH': '/v1/chat/completions'} if extra_path else {}
            if probe and probe.get('path'):
                environment['ATR_AB_EXPERIMENT_PATH'] = probe['path']
            if probe and probe.get('responses_effort'):
                environment['ATR_AB_RESPONSES_EFFORT'] = probe['responses_effort']
            with patch.dict(os.environ, environment):
                response = invoke(route, request, trace_path,
                                  wire_template=template if route in {'direct', 'direct_responses'} and (route == 'direct' or probe.get('direct_template')) else None)
            record['worker_timing'] = response.model_metadata['timing']
            content = response.choices[0].message.content
            record['content_bytes'] = len(content.encode())
            stage, parse_start = 'parse', time.monotonic()
            if state:
                parsed = _parse_content(content)
                parse_ms = (time.monotonic() - parse_start) * 1000
                stage, validation_start = 'validate', time.monotonic()
                validate_report(parsed, state)
                validation_ms = (time.monotonic() - validation_start) * 1000
                record['strict_schema_result'] = 'valid'
            else:
                record['reply_ok'] = content.strip() == 'OK'
                parse_ms = (time.monotonic() - parse_start) * 1000
            record.update(status='completed', error_category=None, request_stage='completed')
        except Exception as exc:
            error = exc.diagnostic if isinstance(exc, ModelCallError) else diagnostic(exc, stage)
            if stage == 'parse':
                parse_ms = (time.monotonic() - parse_start) * 1000
            elif stage == 'validate':
                validation_ms = (time.monotonic() - validation_start) * 1000
            record.update(status='failed', error_category=error['code'], request_stage=error['stage'],
                          exception_type=error['exception_type'], http_status=error.get('status_code'),
                          validation_errors=error.get('validation_errors', []))
            record['worker_timing'] = getattr(exc, 'model_metadata', {}).get('timing')
            record['process_hard_timeout'] = error['exception_type'] == 'TimeoutExpired'
        record.update(total_ms=round((time.monotonic() - start) * 1000, 3), parse_ms=round(parse_ms, 3),
                      validate_ms=round(validation_ms, 3), ended_at=datetime.now(timezone.utc).isoformat())
        if trace_path.exists():
            observed = json.loads(trace_path.read_text())
            record['http_status'] = observed.get('http_status')
            record['usage'] = observed.get('usage')
            record['usage_kind'] = observed.get('usage_kind', 'unavailable')
            record['http_sends'] = observed['http_sends']
        save(ledger_path, ledger)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    after = snapshot()
    before = json.loads((folder / 'before-data.json').read_text())
    save(folder / 'after-data.json', after)
    preserved = {key: before[key] == after[key] for key in ('tables', 'artifact_files', 'config', 'historical_timeout')}
    save(folder / 'preservation.json', preserved)
    assert all(preserved.values()) and not after['active_jobs'], 'Business data/config changed'


def self_check(folder):
    # Untrusted content and headers must never be copied into the safe observation.
    secret = 'DO-NOT-PERSIST-SECRET'
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions',
        headers={'Authorization': 'Bearer ' + secret, 'x-unknown': secret},
        json={'messages': [{'role': 'user', 'content': secret}], 'model': 'gpt-6.1-sol', 'stream': False})
    assert secret not in json.dumps(safe_wire(request))
    response = httpx.Response(200, json={'choices': [{'message': {'content': secret}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 3}}, 'secret': secret})
    assert secret not in json.dumps(safe_response(response))
    assert safe_response(response)['usage']['cached_tokens'] == 3
    partial_echo = httpx.Response(400, json={'error': {'type': 'invalid_request_error', 'message': 'SENSITIVE-PROMPT-FRAGMENT'}})
    assert 'SENSITIVE-PROMPT-FRAGMENT' not in json.dumps(safe_response(partial_echo))
    assert safe_response(httpx.Response(200, json={'usage': {'prompt_tokens': True, 'completion_tokens': 2}}))['usage_kind'] == 'unavailable'
    invalid = {'summary': 'synthetic', 'hypotheses': [], 'next_action': None,
               'action': {'action_id': 'store-unauthorized', 'rationale': 'synthetic'}}
    try:
        validate_report(invalid, synthetic_state())
    except Exception as exc:
        assert diagnostic(exc, 'validate')['code'] == 'model_schema_error'
    else:
        raise AssertionError('Unauthorized action accepted')
    with patch('app.context.model.subprocess.run', side_effect=subprocess.TimeoutExpired('synthetic', 130)):
        try:
            _completion(timeout=120, max_tokens=1500)
        except ModelCallError as exc:
            assert exc.diagnostic['exception_type'] == 'TimeoutExpired' and exc.code == 'model_timeout'
        else:
            raise AssertionError('Hard timeout not detected')
    save(folder / 'self-check.json', {'redaction': True, 'strict_usage': True, 'unauthorized_action_rejected': True,
                                     'hard_timeout_distinguished': True, 'real_api_calls': 0})
    print('Offline self-check passed; real API calls=0')


if __name__ == '__main__':
    if sys.argv[1] == '--worker':
        if sys.argv[2] in {'direct_responses', 'worker_responses'}:
            os.environ['ATR_AB_RESPONSES'] = '1'
        if sys.argv[2] == 'worker_responses':
            os.environ['ATR_AB_LITELLM_RESPONSES'] = '1'
        install_observer(sys.argv[3], sys.argv[4] == 'offline')
        if sys.argv[2] == 'worker':
            from app.context.model_worker import main
            main()
        else:
            direct_worker()
    else:
        output_folder = Path(sys.argv[2])
        if sys.argv[1] == '--self-check':
            self_check(output_folder)
        elif sys.argv[1] == '--prepare':
            prepare(output_folder, Settings())
        elif sys.argv[1] == '--run':
            run_experiment(output_folder, Settings())
        elif sys.argv[1] == '--path-check':
            run_experiment(output_folder, Settings(), extra_path=True)
        elif sys.argv[1] == '--probe':
            run_experiment(output_folder, Settings(), probe=json.loads(Path(sys.argv[3]).read_text()))
        else:
            raise SystemExit('Expected --self-check, --prepare or --run')
