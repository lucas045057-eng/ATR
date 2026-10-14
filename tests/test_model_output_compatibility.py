"""Output compatibility and safe validation diagnostics; only local fake HTTP."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.agent.model_schema import validate_report
from app.agent.workflow import _planner
from app.context.budget import ContextBudgetManager
from app.context.model import call_json
from app.context.model_errors import ModelCallError, diagnostic
from conftest import wait_job
from pydantic import ValidationError
from test_model_isolation import configure_client, fake_model  # noqa: F401
from test_provider_governance import start_investigation
from test_v22_adaptive import hypothesis, investigate, model_stub, prepared_profile, report


@pytest.mark.parametrize('variant', ['next_action_null', 'optional_arrays_null', 'optional_omitted'])
def test_empty_optional_values_do_not_fail_valid_investigation(client, monkeypatch, variant):
    profile = prepared_profile(client, monkeypatch, store=False)

    def reply(context, _):
        h = hypothesis(context, 'catalog-api')
        data = report([h])
        if variant == 'next_action_null':
            data['next_action'] = None
        elif variant == 'optional_arrays_null':
            h['verification_predictions'] = None
            h['status'] = None
            data['scope_requests'] = None
        else:
            del h['verification_predictions'], h['status']
        return data

    model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert result['summary']['model_error'] is None
    assert result['context_budget']['usage_records'][0]['status'] == 'succeeded'
    assert result['status'] == 'unresolved'  # Valid proposal still needs independent proof.
    assert len(result['tool_calls']) == 2
    if variant == 'next_action_null':
        assert result['summary']['next_action'] == ''


def test_validation_fields_are_safe_persisted_and_returned(client, fake_model, monkeypatch, caplog):  # noqa: F811
    _, settings, state, _ = fake_model
    profile = prepared_profile(client, monkeypatch, store=False)
    configure_client(settings)

    def reply(context, _):
        h = hypothesis(context, 'catalog-api')
        h['confidence'] = 'PRIVATE-RESPONSE-VALUE'
        h['evidence_links'][0]['relation'] = 'PRIVATE-RELATION'
        return report([h], **{'PRIVATE-EXTRA-FIELD': 'PRIVATE-EXTRA-VALUE'})

    state['reply'] = reply
    task = start_investigation(client, profile)
    job = wait_job(client, task['job_id'], timeout=20)
    result = client.get('/api/v1/runs/' + job['result']['id']).json()
    error = result['summary']['model_error']
    assert error['validation_kind'] == 'format'
    issues = error['validation_errors']
    assert {'path': 'hypotheses[0].confidence', 'type': 'float_type', 'stage': 'validate'} in issues
    assert {'path': 'hypotheses[0].evidence_links[0].relation', 'type': 'literal_error', 'stage': 'validate'} in issues
    assert {'path': '<extra_field>', 'type': 'extra_forbidden', 'stage': 'validate'} in issues
    record = result['context_budget']['usage_records'][0]
    assert record['diagnostic']['validation_errors'] == issues
    assert record['usage_kind'] == 'actual' and record['status'] == 'failed'
    assert result['context_budget']['reserved_tokens'] == 0
    assert len(result['tool_calls']) == len(result['evidence']) == 2
    assert state['requests'] == 1
    event = next(e for e in result['events'] if e['event_type'] == 'model_attempt_failed')
    saved = json.dumps(error) + json.dumps(record) + json.dumps(event) + caplog.text
    assert 'PRIVATE-' not in saved and 'synthetic-secret' not in saved
    assert 'hypotheses[0].confidence' in caplog.text
    assert error['exception_type'] == 'ValidationError'


@pytest.mark.parametrize('variant,path,error_type', [
    ('unknown_evidence', 'hypotheses[0].evidence_links[0].evidence_id', 'unknown_evidence'),
    ('unknown_target', 'hypotheses[0].target_node_id', 'unknown_target'),
    ('wrong_role', 'hypotheses[0].target_role', 'target_role_mismatch'),
    ('unknown_action', 'action.action_id', 'action_not_allowed'),
    ('extra_arguments', 'action.<extra_field>', 'extra_forbidden'),
    ('invalid_prediction', 'hypotheses[0].verification_predictions[0].value', 'invalid_prediction'),
])
def test_invalid_decision_has_field_reason_and_never_executes(client, monkeypatch, variant, path, error_type):
    profile = prepared_profile(client, monkeypatch, store=False)

    def reply(context, _):
        h = hypothesis(context, 'catalog-api')
        data = report([h])
        if variant == 'unknown_evidence':
            h['evidence_links'][0]['evidence_id'] = 'PRIVATE-UNKNOWN-ID'
        elif variant == 'unknown_target':
            h['target_node_id'] = 'PRIVATE-UNKNOWN-NODE'
        elif variant == 'wrong_role':
            h['target_role'] = 'PRIVATE-ROLE'
        elif variant == 'invalid_prediction':
            h['verification_predictions'][0]['value'] = True
        elif variant == 'unknown_action':
            data['action'] = {'action_id': 'PRIVATE-UNKNOWN-ACTION', 'rationale': 'PRIVATE-REASON'}
        else:
            data['action'] = {'action_id': context['available_actions'][0]['action_id'], 'rationale': 'read', 'arguments': {'command': 'PRIVATE-COMMAND'}}
        return data

    model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    error = result['summary']['model_error']
    assert error['validation_kind'] == 'decision'
    assert {'path': path, 'type': error_type, 'stage': 'validate'} in error['validation_errors']
    assert len(result['tool_calls']) == len(result['evidence']) == 2
    assert result['status'] == 'unresolved' and result['hypotheses'] == []
    assert 'PRIVATE-' not in json.dumps(error)


@pytest.mark.parametrize('wrapper', ['json_fence', 'plain_fence', 'whitespace'])
def test_single_json_document_wrappers_pass_local_worker(fake_model, wrapper):  # noqa: F811
    database, settings, state, _ = fake_model
    body = json.dumps(report(), ensure_ascii=False)
    content = {'json_fence': '```json\n' + body + '\n```', 'plain_fence': '```\n' + body + '\n```', 'whitespace': ' \r\n' + body + '\n '}[wrapper]
    state['choices'] = [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    result = call_json(database, settings, bid, 'local fake only', {}, validate=lambda value: validate_report(value, {}))
    assert result['summary'] == '隔离调查尚待因果核验'
    assert manager.get(bid)['usage_records'][0]['status'] == 'succeeded'
    assert state['requests'] == 1


@pytest.mark.parametrize('content', ['', 'Narration\n```json\n{}\n```', '```json\n{}\n```\n```json\n{}\n```', '{"action":null,"action":{"action_id":"invalid"}}'])
def test_ambiguous_or_empty_content_is_parse_failure(fake_model, content):  # noqa: F811
    database, settings, state, _ = fake_model
    state['choices'] = [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'local fake only', {}, validate=lambda value: validate_report(value, {}))
    assert caught.value.code == 'model_invalid_json'
    assert caught.value.diagnostic['stage'] == 'parse'
    assert manager.get(bid)['usage_records'][0]['status'] == 'failed'
    assert manager.get(bid)['usage_records'][0]['usage_kind'] == 'actual'


@pytest.mark.parametrize('variant', ['null_optionals', 'integer_confidence', 'empty_hypotheses'])
def test_valid_variants_pass_real_worker_and_keep_evidence_gate(client, fake_model, monkeypatch, variant):  # noqa: F811
    _, settings, state, _ = fake_model
    profile = prepared_profile(client, monkeypatch, store=False)
    configure_client(settings)

    def reply(context, _):
        h = hypothesis(context, 'catalog-api')
        data = report([h])
        if variant == 'null_optionals':
            h['verification_predictions'] = None
            data['scope_requests'] = None
            data['next_action'] = None
        elif variant == 'integer_confidence':
            h['confidence'] = 1
        else:
            data['hypotheses'] = []
        return data

    state['reply'] = reply
    task = start_investigation(client, profile)
    job = wait_job(client, task['job_id'], timeout=20)
    result = client.get('/api/v1/runs/' + job['result']['id']).json()
    assert result['summary']['model_error'] is None
    assert result['status'] == 'unresolved' and result['final_hypothesis_id'] is None
    assert result['context_budget']['usage_records'][0]['usage_kind'] == 'actual'
    assert result['context_budget']['usage_records'][0]['status'] == 'succeeded'
    assert state['requests'] == 1 and len(result['tool_calls']) == 2


@pytest.mark.parametrize('variant', ['confidence_bool', 'confidence_string', 'missing_evidence_null', 'missing_evidence_omitted', 'summary_null', 'action_missing', 'renamed_evidence', 'top_status', 'invalid_status'])
def test_required_fields_and_enums_stay_strict(client, monkeypatch, variant):
    profile = prepared_profile(client, monkeypatch, store=False)

    def reply(context, _):
        h = hypothesis(context, 'catalog-api')
        data = report([h])
        if variant == 'confidence_bool':
            h['confidence'] = True
        elif variant == 'confidence_string':
            h['confidence'] = '0.9'
        elif variant == 'missing_evidence_null':
            h['missing_evidence'] = None
        elif variant == 'missing_evidence_omitted':
            del h['missing_evidence']
        elif variant == 'summary_null':
            data['summary'] = None
        elif variant == 'action_missing':
            del data['action']
        elif variant == 'renamed_evidence':
            h['evidence_links'][0]['id'] = h['evidence_links'][0].pop('evidence_id')
        elif variant == 'top_status':
            data['status'] = 'unresolved'
        else:
            h['status'] = 'uncertain'
        return data

    model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert result['summary']['model_error']['code'] == 'model_schema_error'
    assert result['summary']['model_error']['validation_errors']
    assert result['context_budget']['usage_records'][0]['status'] == 'failed'
    assert len(result['tool_calls']) == 2


@pytest.mark.parametrize('tool,field,operator,value,error_field', [
    ('health.get', 'state', 'eq', 'failed', 'tool_id'),
    ('service.status', 'state', 'contains', 'failed', 'operator'),
    ('health.get', 'status_code', 'eq', '503', 'value'),
    ('network.listeners', 'listening', 'eq', 1, 'value'),
    ('service.logs', 'log_signal', 'contains', 'failed', 'value'),
])
def test_prediction_decision_reports_exact_invalid_component(client, monkeypatch, tool, field, operator, value, error_field):
    profile = prepared_profile(client, monkeypatch, store=False)

    def reply(context, _):
        h = hypothesis(context, 'catalog-api')
        h['verification_predictions'][0].update(tool_id=tool, field=field, operator=operator, value=value)
        return report([h])

    model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    error = result['summary']['model_error']
    assert error['validation_kind'] == 'decision'
    assert error['validation_errors'][0]['path'] == 'hypotheses[0].verification_predictions[0].' + error_field
    assert len(result['tool_calls']) == 2


def test_union_type_errors_show_field_path_without_internal_branch_names():
    from app.agent.model_schema import Report
    h = {'target_node_id': 'app', 'target_role': 'application', 'failure_mode': 'startup_failure',
         'causal_chain': ['service failed', 'request failed'], 'prediction': 'failed', 'rationale': 'local',
         'evidence_links': [], 'missing_evidence': [], 'confidence': 0.8,
         'verification_predictions': [{'target_node_id': 'app', 'tool_id': 'service.status', 'field': 'state', 'operator': 'eq', 'value': []}]}
    with pytest.raises(ValidationError) as caught:
        Report.model_validate(report([h]))
    errors = diagnostic(caught.value, 'validate')['validation_errors']
    assert {e['path'] for e in errors} == {'hypotheses[0].verification_predictions[0].value'}
    assert {e['type'] for e in errors} == {'string_type', 'int_type', 'bool_type'}


def budget_regression_state(unicode=False):
    ids = [f'{i:08d}-1234-5678-abcd-123456789012' for i in range(3)]
    names, roles = ['catalog-api', 'store', 'cache'], ['application', 'datastore', 'cache']
    nodes = [{'id': node_id, 'kind': 'service', 'name': name, 'role': role} for node_id, name, role in zip(ids, names, roles)]
    log = ('启动失败：依赖连接不可用，请保留当前证据。' if unicode else '2026-10-10T07:45:00Z connection failed while initializing service; retry limit reached; startup aborted. ')
    evidence = [{'evidence_id': f'evidence-{i:02d}-1234-5678-abcd-123456789012',
                 'call_id': f'call-{i:02d}-1234-5678-abcd-123456789012', 'target_node_id': ids[i % 3],
                 'tool_id': 'service.logs' if i % 2 else 'service.status',
                 'payload': {'connector': 'mock', 'state': 'failed', **({'text': (log * 40)[:700]} if i % 2 else {})}} for i in range(8)]
    actions = [{'action_id': f'{i:064x}', 'tool_id': 'config.read', 'target_node_id': ids[i % 3],
                'service': names[i % 3], 'role': roles[i % 3], 'arguments': {'path': f'/srv/catalog/service{i}.conf'},
                'scheduling': {'score': 3.4, 'permission_allowed': True, 'hypothesis_discrimination': 2, 'read_cost': 0.6}} for i in range(3)]
    hypotheses = [{'hypothesis_id': f'{i:064x}', 'target_node_id': ids[i % 3], 'failure_mode': 'startup_failure',
                   'status': 'refuted' if i == 1 else 'candidate',
                   'prediction': 'The service remains failed within the incident window; startup logs will identify a failed dependency connection.',
                   'support_evidence': [e['evidence_id'] for e in evidence[:4]],
                   'counter_evidence': [evidence[0]['evidence_id']] if i == 1 else [],
                   'missing_evidence': ['Need independent service state and mechanism logs']} for i in range(2)]
    return {'incident': {'symptom': 'catalog-api cannot serve requests; investigate startup failure'},
            'graph': {'nodes': nodes, 'edges': [{'from_node_id': ids[0], 'to_node_id': ids[i], 'relation': 'depends_on', 'certainty': 'observed'} for i in (1, 2)]},
            'adaptive': True, 'evidence': evidence, 'available_actions': actions,
            'previous_hypotheses': hypotheses, 'hypothesis_history': hypotheses,
            'runtime_bindings': [{'service': name, 'kind': 'service', 'runtime_id': name + '.service'} for name in names],
            'business_profile': {'name': 'catalog', 'services': [{'name': name, 'role': role} for name, role in zip(names, roles)]}}


@pytest.mark.parametrize('unicode', [False, True])
@pytest.mark.parametrize('input_limit', [12000, 30000])
def test_schema_prompt_uses_model_capacity_and_preserves_legacy_references(fake_model, monkeypatch, unicode, input_limit):  # noqa: F811
    database, settings, _, _ = fake_model
    settings.investigation_input_tokens = input_limit
    state = budget_regression_state(unicode)
    before, requests = deepcopy(state), []

    def completion(**request):
        requests.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(report())))],
                               usage=SimpleNamespace(prompt_tokens=64, completion_tokens=32))

    monkeypatch.setattr('app.context.model._completion', completion)
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    result = _planner(settings, state, database, bid)
    assert state == before
    budget = manager.get(bid)
    assert budget['limits']['max_input_tokens'] == input_limit
    assert 'model_error' not in result and result['report']['analysis_status'] == 'model'
    assert budget['calls'] == 1 and budget['usage_records'][0]['status'] == 'succeeded'
    messages = requests[0]['messages']
    measured = sum(len(m['content'].encode()) + 128 for m in messages) + 256
    assert measured <= manager.input_capacity()
    context = json.loads(messages[1]['content'])
    assert [e['evidence_id'] for e in context['current_evidence']] == [e['evidence_id'] for e in before['evidence']]
    assert all(e['payload']['state'] == 'failed' for e in context['current_evidence'])
    assert context['previous_hypotheses'][1]['counter_evidence'] == [before['evidence'][0]['evidence_id']]
    assert context['previous_hypotheses'][1]['status'] == 'refuted'
    assert [a['action_id'] for a in context['available_actions']] == [a['action_id'] for a in before['available_actions']]


def test_small_legacy_budget_does_not_stop_before_provider(fake_model, monkeypatch):  # noqa: F811
    from app.context.budget import BudgetLimits
    database, settings, state, _ = fake_model
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation', limits=BudgetLimits(max_input_tokens=1000))
    result = _planner(settings, budget_regression_state(), database, bid)
    assert 'model_error' not in result
    assert manager.get(bid)['calls'] == state['requests'] == 1
