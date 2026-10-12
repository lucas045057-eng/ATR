"""Bounded graph references, including multi-round local Fake Provider acceptance."""
import json
from copy import deepcopy

import pytest
from app.agent.model_schema import SERVICE_TARGET_LIMIT, allowed_service_nodes, validate_report
from app.agent.workflow import _planner
from app.context.budget import BudgetExceeded, ContextBudgetManager
from app.context.model_errors import ModelDecisionError
from test_model_isolation import configure_client, fake_model  # noqa: F401
from test_model_output_compatibility import budget_regression_state
from test_v22_adaptive import candidate, hypothesis, investigate, prepared_profile, proposal, report

# Historical IDs are regression fixtures only; implementation reads each Run's graph.
API = 'd44a11fc-1a44-4fdf-89f9-a12f6ec5bf4e'
DB = '7981105e-47a6-4f15-95c9-019f484ffbbf'
DATASTORE = '2009d062-8279-4f00-99e0-b5a784838e3b'


def graph_state():
    return {'graph': {'nodes': [
        {'id': API, 'kind': 'service', 'name': 'api', 'role': 'application'},
        {'id': DB, 'kind': 'service', 'name': 'db', 'role': 'datastore'},
        {'id': DATASTORE, 'kind': 'datastore', 'name': 'PostgreSQL', 'role': 'datastore'},
        {'id': 'runtime-node', 'kind': 'service', 'name': 'runtime', 'role': 'runtime'},
    ]}, 'evidence': []}


def decision(node_id, role='application'):
    return report([{'target_node_id': node_id, 'target_role': role,
                   'failure_mode': 'service_unavailable', 'causal_chain': ['service failed', 'request failed'],
                   'prediction': 'service is failed', 'rationale': 'synthetic candidate only',
                   'evidence_links': [], 'missing_evidence': ['independent evidence'], 'confidence': 0.5}])


@pytest.mark.parametrize('node_id,role', [(API, 'application'), (DB, 'datastore')])
def test_legal_service_ids_pass(node_id, role):
    assert validate_report(decision(node_id, role), graph_state())['hypotheses'][0]['target_node_id'] == node_id


@pytest.mark.parametrize('node_id', [
    'api', 'db', 'a' * 64, 'd997a5d5-8898-4f99-bbe7-765e38facac9',
    'action-id', 'hypothesis-id', DATASTORE, 'runtime-node', 'invented-uuid',
])
def test_other_identifiers_cannot_replace_service_node_id(node_id):
    with pytest.raises(ModelDecisionError) as error:
        validate_report(decision(node_id), graph_state())
    assert error.value.error_type == 'unknown_target'
    assert error.value.path == ('hypotheses', 0, 'target_node_id')
    assert node_id not in str(error.value)


def test_valid_id_with_wrong_role_is_rejected():
    with pytest.raises(ModelDecisionError) as error:
        validate_report(decision(DB), graph_state())
    assert error.value.error_type == 'target_role_mismatch'


def test_prediction_cannot_reference_datastore_node():
    data = decision(API)
    data['hypotheses'][0]['verification_predictions'] = [
        {'target_node_id': DATASTORE, 'tool_id': 'container.status', 'field': 'state', 'operator': 'eq', 'value': 'running'}]
    with pytest.raises(ModelDecisionError) as error:
        validate_report(data, graph_state())
    assert error.value.error_type == 'unknown_prediction_target'


@pytest.mark.parametrize('count', [0, 2, SERVICE_TARGET_LIMIT])
def test_prompt_candidates_and_validator_share_each_run_graph(fake_model, count):  # noqa: F811
    database, settings, provider, _ = fake_model
    manager = ContextBudgetManager(database, settings)
    contexts = []

    def reply(context, _):
        contexts.append(context)
        targets = context['allowed_hypothesis_targets']
        return decision(targets[-1]['node_id'], targets[-1]['role']) if targets else report()

    provider['reply'] = reply
    # Different businesses and SSH targets must not reuse a prior graph's candidate IDs.
    for business, target in [('business-a', 'target-a'), ('business-b', 'target-b'), ('business-a', 'target-c')]:
        state = {'incident': {'symptom': 'synthetic service unavailable'}, 'adaptive': True,
                 'business_profile': {'id': business}, 'target_id': target,
                 'graph': {'nodes': [
                     {'id': f'{business}-{target}-{i:02d}', 'kind': 'service', 'name': f'svc-{i}',
                      'role': 'datastore' if i % 2 else 'application'} for i in range(count)]
                     + [{'id': 'non-service', 'kind': 'datastore', 'name': 'db', 'role': 'datastore'}], 'edges': []},
                 'evidence': [], 'available_actions': []}
        original = deepcopy(state)
        result = _planner(settings, state, database, manager.create('investigation'))
        assert 'model_error' not in result
        assert state == original
        candidates = contexts[-1]['allowed_hypothesis_targets']
        expected = allowed_service_nodes(state)
        assert {c['node_id'] for c in candidates} == set(expected)
        assert {n['id'] for n in contexts[-1]['system_graph']} == set(expected)
        assert all(c == {'service': expected[c['node_id']]['name'], 'node_id': c['node_id'],
                         'role': expected[c['node_id']]['role']} for c in candidates)
        for node in expected.values():
            validate_report(decision(node['id'], node['role']), state)
        with pytest.raises(ModelDecisionError):
            validate_report(decision('business-other-00'), state)
    prompt = provider['wire']['messages'][0]['content']
    for rule in ('allowed_hypothesis_targets', 'Never generate UUIDs', 'SSH Target IDs', 'rather than invent a reference'):
        assert rule in prompt
    assert provider['requests'] == 3


def test_candidate_overflow_stops_before_request_without_truncation(fake_model):  # noqa: F811
    database, settings, provider, _ = fake_model
    state = budget_regression_state()
    state['graph']['nodes'] = [{'id': f'service-{i}', 'kind': 'service', 'name': f'svc-{i}', 'role': 'application'}
                               for i in range(SERVICE_TARGET_LIMIT + 1)]
    before = deepcopy(state)
    with pytest.raises(BudgetExceeded):
        validate_report(decision('service-30'), state)
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    result = _planner(settings, state, database, bid)
    error = result['model_error']
    assert error['budget_reason'] == 'target_candidate_limit'
    assert error['candidate_count'] == 31 and error['candidate_limit'] == 30
    assert error['stage'] == 'budget' and result['report']['action'] is None
    assert manager.get(bid)['calls'] == provider['requests'] == 0
    assert manager.get(bid)['usage_records'] == [] and state == before


def test_candidates_remain_complete_in_every_round_with_authorized_budget(fake_model):  # noqa: F811
    database, settings, provider, _ = fake_model
    settings.investigation_input_tokens = 30000
    contexts = []

    def reply(context, _):
        contexts.append(context)
        c = context['allowed_hypothesis_targets'][0]
        return decision(c['node_id'], c['role'])

    provider['reply'] = reply
    state = budget_regression_state(unicode=True)
    expected = set(allowed_service_nodes(state))
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('investigation')
    for _ in range(2):
        result = _planner(settings, state, database, bid)
        assert 'model_error' not in result
        assert {c['node_id'] for c in contexts[-1]['allowed_hypothesis_targets']} == expected
        state['previous_hypotheses'] = result['report']['hypotheses']
        state['hypothesis_history'] += result['report']['hypotheses']
    assert contexts[0]['allowed_hypothesis_targets'] == contexts[1]['allowed_hypothesis_targets']
    assert manager.get(bid)['calls'] == provider['requests'] == 2


def test_second_round_invalid_target_stops_tools_and_preserves_audit(client, monkeypatch, fake_model):  # noqa: F811
    profile = prepared_profile(client, monkeypatch)
    _, settings, provider, _ = fake_model
    configure_client(settings)
    contexts = []

    def reply(context, round_number):
        contexts.append(context)
        h = hypothesis(context, 'catalog-api')
        if round_number == 1:
            return report([h], proposal(candidate(context, 'service.status', 'store')))
        h['target_node_id'] = 'PRIVATE-INVALID-TARGET'
        return report([h], proposal(candidate(context, 'service.logs', 'store')))

    provider['reply'] = reply
    result = investigate(client, profile, mode='graph_adaptive')
    assert result['status'] == 'unresolved'
    assert len(result['tool_calls']) == 3
    assert all(c['status'] == 'succeeded' for c in result['tool_calls'])
    error = result['summary']['model_error']
    assert error['validation_kind'] == 'decision'
    assert error['validation_errors'] == [{'path': 'hypotheses[0].target_node_id', 'type': 'unknown_target', 'stage': 'validate'}]
    assert contexts[0]['allowed_hypothesis_targets'] == contexts[1]['allowed_hypothesis_targets']
    assert provider['requests'] == 2
    assert 'PRIVATE-INVALID-TARGET' not in json.dumps(result)
    assert result['summary']['hypothesis_history']
    usage = result['context_budget']['usage_records']
    assert [u['status'] for u in usage] == ['succeeded', 'failed']
    assert all(u['diagnostic']['retry_count'] == 0 for u in usage)
