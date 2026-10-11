"""Explicit Responses protocol through the isolated worker; local HTTP only."""

import json

import pytest
from app.context.budget import ContextBudgetManager
from app.context.model import call_json
from app.context.model_errors import ModelCallError
from app.settings import Settings
from test_model_isolation import fake_model  # noqa: F401


def responses_body(status='completed', usage=None):
    return {'id': 'resp_local', 'object': 'response', 'created_at': 1, 'status': status,
            'model': 'gpt-6.1-sol', 'error': None, 'incomplete_details': None if status == 'completed' else {'reason': 'max_output_tokens'},
            'output': [{'id': 'msg_local', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': json.dumps({'summary': 'synthetic', 'hypotheses': [], 'next_action': None, 'action': None}), 'annotations': []}]}],
            'usage': usage if usage is not None else {'input_tokens': 11, 'output_tokens': 5, 'total_tokens': 16}}


def configure_responses(settings):
    return settings.model_copy(update={'model_wire_api': 'responses', 'model_reasoning_effort': 'low'})


@pytest.mark.parametrize('root_base', [False, True])
def test_explicit_responses_uses_same_messages_and_verified_wire_usage(fake_model, root_base):  # noqa: F811
    database, settings, state, _ = fake_model
    settings = configure_responses(settings)
    if root_base:
        settings = settings.model_copy(update={'model_base_url': settings.model_base_url.removesuffix('/v1')})
    state['envelope'] = responses_body()
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    result = call_json(database, settings, bid, 'Return strict synthetic JSON', {'synthetic': True})
    assert result == {'summary': 'synthetic', 'hypotheses': [], 'next_action': None, 'action': None}
    assert state['path'] == '/v1/responses' and state['requests'] == 1
    request = state['wire']
    assert request['input'] == [{'role': 'system', 'content': 'Return strict synthetic JSON'}, {'role': 'user', 'content': '{"synthetic":true}'}]
    assert request['model'] == 'gpt-6.1-sol' and request['max_output_tokens'] == settings.model_max_output_tokens
    assert request['text'] == {'format': {'type': 'json_object'}} and request['reasoning'] == {'effort': 'low'}
    assert request['store'] is False and request['stream'] is False
    assert not {'messages', 'response_format', 'max_tokens', 'max_completion_tokens'} & request.keys()
    record = manager.get(bid)['usage_records'][0]
    assert record['usage_kind'] == 'actual' and record['input_tokens'] == 11 and record['output_tokens'] == 5
    assert manager.get(bid)['reserved_tokens'] == 0


@pytest.mark.parametrize('mutation', ['incomplete', 'refusal', 'tool_call', 'invalid_reference'])
def test_responses_invalid_output_stops_without_fallback(fake_model, mutation):  # noqa: F811
    database, settings, state, _ = fake_model
    settings = configure_responses(settings)
    body = responses_body('incomplete' if mutation == 'incomplete' else 'completed')
    if mutation == 'refusal':
        body['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'PRIVATE-RESPONSE'}]
    if mutation == 'tool_call':
        body['output'].append({'type': 'function_call', 'id': 'fc_local', 'call_id': 'local', 'name': 'PRIVATE-TOOL', 'arguments': '{}'})
    if mutation == 'invalid_reference':
        body['output'][0]['content'][0]['text'] = '{"summary":"synthetic","hypotheses":[],"next_action":null,"action":{"action_id":"PRIVATE-ACTION","rationale":"read"}}'
    state['envelope'] = body
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    from app.agent.model_schema import validate_report
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'Return strict JSON', {}, validate=lambda r: validate_report(r, {'graph': {'nodes': []}, 'evidence': [], 'available_actions': []}))
    assert caught.value.code == 'model_schema_error'
    assert state['requests'] == 1 and manager.get(bid)['reserved_tokens'] == 0
    record = manager.get(bid)['usage_records'][0]
    assert record['usage_kind'] == 'actual' and record['status'] == 'failed'
    assert 'PRIVATE-' not in json.dumps(record['diagnostic']) + str(caught.value)


@pytest.mark.parametrize('usage', [{'input_tokens': True, 'output_tokens': 5}, {'output_tokens': 5}, {'input_tokens': '11', 'output_tokens': 5}])
def test_responses_coerced_usage_is_not_actual(fake_model, usage):  # noqa: F811
    database, settings, state, _ = fake_model
    settings = configure_responses(settings)
    state['envelope'] = responses_body(usage=usage)
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    try:
        call_json(database, settings, bid, 'Return strict JSON', {})
    except ModelCallError:
        pass
    assert manager.get(bid)['usage_records'][0]['usage_kind'] == 'estimate' and state['requests'] == 1


def test_model_protocol_configuration_rejects_invalid_values():
    with pytest.raises(ValueError):
        Settings(_env_file=None, model_wire_api='PRIVATE-UNSUPPORTED')


def test_explicit_chat_reasoning_effort_preserves_existing_protocol(fake_model):  # noqa: F811
    database, settings, state, _ = fake_model
    settings = settings.model_copy(update={'model_reasoning_effort': 'low'})
    manager = ContextBudgetManager(database, settings)
    call_json(database, settings, manager.create('investigation'), 'Return JSON', {})
    assert state['path'] == '/v1/chat/completions' and state['requests'] == 1
    assert state['wire']['reasoning_effort'] == 'low'
    assert state['wire']['response_format'] == {'type': 'json_object'}


def test_completed_response_with_incomplete_details_is_rejected(fake_model):  # noqa: F811
    database, settings, state, _ = fake_model
    settings = configure_responses(settings)
    body = responses_body()
    body['incomplete_details'] = {'reason': 'max_output_tokens'}
    state['envelope'] = body
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'Return strict JSON', {})
    assert caught.value.code == 'model_schema_error' and state['requests'] == 1
    assert manager.get(bid)['usage_records'][0]['status'] == 'failed'


@pytest.mark.parametrize('model', ['anthropic/claude-3-5-sonnet-20241022', 'openai/chat_completions/gpt-6.1-sol'])
def test_explicit_responses_cannot_bridge_to_other_provider(fake_model, model):  # noqa: F811
    database, settings, state, _ = fake_model
    settings = configure_responses(settings).model_copy(update={'model_name': model})
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'Return strict JSON', {})
    assert state['requests'] == 0
    assert caught.value.diagnostic['stage'] == 'initialize' and caught.value.diagnostic['status_code'] is None
    assert manager.get(bid)['reserved_tokens'] == 0
