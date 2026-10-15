"""P0 runs use temporary databases and synthetic targets/model transports only."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from app.agent.ab_investigation import InvestigationBroker, prepare_run
from app.common import dumps, now, uid
from app.connectors.mock import MockConnector
from app.context.budget import BudgetExceeded, ContextBudgetManager
from app.context.model_errors import ModelCallError
from app.jobs import reserve_investigation
from app.runtime.coordinator import get_coordinator
from app.settings import get_settings
from app.storage.database import Database
from app.system.scope import candidate_digest
from test_business import SAMPLE, onboard
from test_model_isolation import fake_model  # noqa: F401


@pytest.fixture
def investigation(client, monkeypatch):
    reads, brokers = [], []
    responses = {}
    inventories = {}

    def read(self, tool, arguments):
        reads.append((tool, dict(arguments)))
        response = responses.get((tool, arguments.get('name')))
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        common = {'connector': 'mock', 'observed_at': now()}
        if tool == 'service.list':
            return {**common, 'services': inventories[self.config['id']], 'partial': False}
        if tool.endswith('.status'):
            return {**common, 'state': 'exited', 'startup_cycle': 'fixture-cycle'}
        if tool.endswith('.logs'):
            return {**common, 'text': now() + ' application check completed',
                    'startup_cycle': 'fixture-cycle', 'cycle_verified': True}
        if tool == 'health.get':
            endpoint = next(e for e in self.config['health_endpoints']
                            if e['endpoint_id'] == arguments['endpoint_id'])
            return {**common, 'status_code': 503, 'endpoint_id': endpoint['endpoint_id'],
                    'url': endpoint['url'], 'observation_location': 'remote_ssh',
                    'protocol': 'atr-read-ab-v1', 'redirected': False}
        if tool == 'config.list':
            return {**common, 'files': [arguments['path'] + '/settings.json']}
        if tool == 'config.read':
            return {**common, 'text': 'DB_ENGINE=postgresql', 'path': arguments['path']}
        raise AssertionError('Unexpected test transport read: ' + tool)

    monkeypatch.setattr(MockConnector, 'read', read)

    def create(names=('orders-api', 'orders-store'), settings=None, services=2,
               task_type='root_cause_investigation', symptom=None, related=False):
        names = tuple(names) if services == 2 else tuple('component-' + str(i) for i in range(services))
        compose = 'services:\n' + ''.join('  ' + name + ': {build: .}\n' for name in names)
        if related:
            compose = compose.replace('  ' + names[0] + ': {build: .}',
                '  ' + names[0] + ': {build: ., depends_on: ' + json.dumps(names[1:]) + '}')
        profile = onboard(client, {**SAMPLE, 'compose.yaml': compose})
        target = client.post(f"/api/v1/businesses/{profile['business_id']}/targets", json={
            'name': 'Temporary P0 target', 'connector': 'mock',
            'access_authorized': True,
            'allowed_paths': ['/srv/fixture'], 'health_endpoints': [
                {'endpoint_id': 'entry-probe', 'service': names[0], 'url': 'http://127.0.0.1:19000/health'}],
        }).json()
        database = Database(get_settings().database_path)
        inventories[target['id']] = [{'id':name + '.service', 'name':name, 'kind':'service'} for name in names]
        with database.transaction() as tx:
            tx.execute('INSERT INTO investigation_defaults VALUES(?,?,?,?,?,?)',
                       (profile['business_id'], target['id'], '[]', names[0], 'test', now()))
            for name in names:
                tx.execute("""INSERT INTO service_bindings(id,business_id,target_id,service,runtime_id,
                    kind,score,reasons_json,evidence_ids_json,status,discovery_id,freshness_status,
                    observed_at,expires_at,source_json,candidate_digest)
                    VALUES(?,?,?,?,?,'service',1,'[]','[]','confirmed',?,'confirmed',?,?,
                    '{}',?)""",
                    (uid(), profile['business_id'], target['id'], name, name + '.service', uid(),
                     now(), (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                     candidate_digest(next(c for c in inventories[target['id']] if c['name']==name))))
        event = client.post('/api/v1/incidents', json={
            'system_id': profile['system_id'], 'symptom': symptom or names[0] + ' unavailable',
            'task_type': task_type,
        }).json()['id']
        job, _, _ = reserve_investigation(database, event, profile['business_id'], target['id'])
        selected = settings or get_settings()
        run_id = prepare_run(database, selected, event, profile['business_id'], target['id'], job, 'test')
        lease = get_coordinator(database).claim(run_id)
        broker = InvestigationBroker(database, selected, lease)
        brokers.append(broker)
        return broker

    yield create, reads, responses
    for broker in brokers:
        broker.coordinator.cancel(broker.run_id)
        broker.coordinator.reaped(broker.run_id, broker.lease['lease_token'])


def run_to_completion(broker, limit=100):
    for _ in range(limit):
        if not broker.advance()['continue']:
            break
    else:
        raise AssertionError('Deterministic investigation did not terminate')
    broker.coordinator.reaped(broker.run_id, broker.lease['lease_token'])
    with broker.db.connect() as tx:
        summary = json.loads(tx.execute('SELECT summary_json FROM diagnosis_runs WHERE id=?',
                                        (broker.run_id,)).fetchone()[0])
        calls = [dict(row) for row in tx.execute('SELECT * FROM tool_calls WHERE run_id=?', (broker.run_id,))]
    return summary, calls


def verify_scope(broker):
    while not broker.scope_verified:
        assert broker.advance()['continue']


def outage(monkeypatch, code='provider_unavailable', status=529):
    requests = []

    def complete(**request):
        requests.append(request)
        raise ModelCallError({'code': code, 'status_code': status, 'stage': 'request',
                              'exception_type': 'SyntheticTransportFailure'})

    monkeypatch.setattr('app.context.model._completion', complete)
    return requests


def model_settings(**updates):
    return get_settings().model_copy(update={
        'external_model_enabled': True, 'model_name': 'isolated-model',
        'model_base_url': 'http://127.0.0.1:9/v1',
        'model_configuration_revision': None, **updates,
    })


def test_baseline_reads_precede_any_model_decision(investigation, monkeypatch):
    from pydantic import SecretStr
    create, reads, _ = investigation
    observed = []

    def decision(database, settings, budget_id, prompt, context, validate, **kwargs):
        observed.append(list(reads))
        return validate({'summary': '尚无运行时证据', 'hypotheses': [], 'next_investigation': '',
                         'next_action_id': None, 'finish': True, 'finish_reason': 'stop'})

    monkeypatch.setattr('app.agent.ab_investigation.call_json', decision)
    broker = create(settings=model_settings(model_api_key=SecretStr('isolated-placeholder')))
    summary, _ = run_to_completion(broker)
    assert observed
    assert observed[0][0][0] == 'service.list'
    baseline = [(tool, args) for tool, args in observed[0] if tool != 'service.list']
    assert {tool for tool, _ in baseline} == {'service.status', 'service.logs', 'health.get'}
    assert len(baseline) == 5
    assert summary['report']['summary'] != '尚无运行时证据'


def test_successful_decision_then_529_keeps_new_evidence_without_an_extra_retry(investigation, monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr
    create, _, _ = investigation
    requests = []

    def complete(**request):
        requests.append(request)
        if len(requests) > 1:
            raise ModelCallError({'code': 'provider_unavailable', 'status_code': 529,
                                  'stage': 'request', 'exception_type': 'SyntheticTransportFailure'})
        context = json.loads(request['messages'][1]['content'])
        action = next(item for item in context['available_actions'] if item['tool_id'] == 'config.list')
        decision = {'summary': '尚无运行时证据', 'hypotheses': [], 'next_investigation': '',
                    'next_action_id': action['action_id'], 'finish': False, 'finish_reason': None}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=dumps(decision)))],
                               usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5, total_tokens=16))

    monkeypatch.setattr('app.context.model._completion', complete)
    broker = create(settings=model_settings(model_api_key=SecretStr('isolated-placeholder')))
    summary, calls = run_to_completion(broker)
    assert len(requests) == 2 and summary['model_failed_at_call'] == 2
    assert any(call['tool_id'] == 'config.list' and call['status'] == 'succeeded' for call in calls)
    assert len(summary['evidence']) >= 6
    assert summary['report']['summary'] != '尚无运行时证据'
    assert summary['analysis_status'] == 'offline'
    assert [usage['status'] for usage in broker.manager.get(broker.budget_id)['usage_records']] == ['succeeded', 'failed']


@pytest.mark.parametrize('names', [('orders-api', 'orders-store'), ('search-worker', 'index-store')])
@pytest.mark.parametrize('code,status', [
    ('provider_unavailable', 529), ('provider_unavailable', 500),
    ('provider_unavailable', 503), ('provider_connection_error', None), ('model_timeout', None),
])
def test_first_model_failure_finishes_readonly_without_a_second_request(
        investigation, monkeypatch, names, code, status):
    from pydantic import SecretStr
    create, reads, _ = investigation
    requests = outage(monkeypatch, code, status)
    broker = create(names, settings=model_settings(model_api_key=SecretStr('isolated-placeholder')))
    summary, calls = run_to_completion(broker)
    assert len(requests) == 1
    assert len([call for call in calls if call['tool_id'] in {'service.status', 'service.logs', 'health.get'}]) == 5
    assert len(reads) == len(calls)
    assert {call['target_id'] for call in calls} == {broker.target_id}
    assert summary['execution_status'] == 'completed'
    assert summary['analysis_status'] == 'offline'
    assert not summary['diagnosis_result']['completion_satisfied']
    report = summary['report']['fact_report']
    assert {fact['value'] for fact in report['confirmed_facts'] if fact['kind'] == 'service_state'} == {'exited'}
    assert report['direct_anomalies'] and report['missing_evidence'] and report['unknown_causes']
    usage = broker.manager.get(broker.budget_id)
    assert usage['calls'] == len(usage['usage_records']) == 1
    assert usage['reserved_tokens'] == 0


def test_two_failed_checks_do_not_abandon_other_authorized_baseline_checks(investigation):
    create, _, responses = investigation
    broker = create()
    for name in ('orders-api.service', 'orders-store.service'):
        responses[('service.status', name)] = TimeoutError('fixture timeout')
    summary, calls = run_to_completion(broker)
    assert {call['tool_id'] for call in calls} >= {'service.status', 'service.logs', 'health.get'}
    assert len([call for call in calls if call['tool_id'] == 'service.status']) == 2
    checks = summary['report']['fact_report']['checks']
    assert sum(check['tool_status'] == 'timed_out' for check in checks) == 2
    assert any(check['tool_status'] == 'succeeded' for check in checks)


def test_old_logs_are_successful_reads_but_not_current_failure_or_health_proof(investigation):
    create, _, responses = investigation
    broker = create()
    responses[('health.get', None)] = {'connector': 'mock', 'status_code': 200,
        'endpoint_id': 'entry-probe', 'url': 'http://127.0.0.1:19000/health',
        'observation_location': 'remote_ssh', 'redirected': False}
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for name in ('orders-api.service', 'orders-store.service'):
        responses[('service.status', name)] = {'connector': 'mock', 'state': 'running', 'startup_cycle': 'cycle'}
        responses[('service.logs', name)] = {'connector': 'mock', 'text': old + ' upstream connection refused 502',
                                            'startup_cycle': 'cycle', 'cycle_verified': True}
    summary, _ = run_to_completion(broker)
    checks = summary['report']['fact_report']['checks']
    old_logs = [row for row in checks if row['tool_id'] == 'service.logs']
    assert len(old_logs) == 2
    assert all(row['tool_status'] == 'succeeded' and row['evidence_status'] == 'out_of_window' for row in old_logs)
    assert not any(fact['kind'] == 'log_observation' for fact in summary['report']['fact_report']['confirmed_facts'])
    assert summary['diagnosis_status'] != 'no_fault_observed'


def test_offline_continues_remaining_checks_and_does_not_replay_prior_success(investigation):
    create, reads, _ = investigation
    broker = create()
    verify_scope(broker)
    actions = broker.planner.candidates([], [], [], include_authorized_scope=True)
    first = next(a for a in actions if a['tool_id'] == 'service.status')
    broker.collect(first)
    broker.summary['model_error'] = {'code': 'provider_unavailable', 'status_code': 529}
    broker.report = {'summary': '尚无运行时证据'}
    summary, calls = run_to_completion(broker)
    assert sum(call['tool_id'] == first['tool_id'] and json.loads(call['args_redacted_json']) == first['arguments']
               for call in calls) == 1
    assert len(reads) >= 5
    assert broker.manager.get(broker.budget_id)['calls'] == 0
    assert summary['report']['summary'] != '尚无运行时证据'


def test_read_denial_is_separate_from_evidence_insufficiency(investigation):
    create, _, responses = investigation
    broker = create()
    responses[('service.logs', 'orders-api.service')] = PermissionError('fixture denied')
    summary, _ = run_to_completion(broker)
    checks = summary['report']['fact_report']['checks']
    denied = next(row for row in checks if row['tool_id'] == 'service.logs' and row['tool_status'] == 'denied')
    assert denied['evidence_status'] == 'missing' and denied['evidence_id'] is None
    assert any(row['tool_id'] == 'health.get' and row['tool_status'] == 'succeeded' for row in checks)


def test_final_report_rechecks_expired_evidence_instead_of_reusing_a_cached_verdict(investigation):
    create, _, _ = investigation
    broker = create()
    verify_scope(broker)
    action = next(a for a in broker.planner.candidates([], [], []) if a['tool_id'] == 'service.status')
    broker.collect(action)
    assert broker.result['diagnosis_status'] == 'partial'
    next(e for e in broker.evidence if e['tool_id']=='service.status')['freshness_state'] = 'expired'
    broker.finish('isolated_expiration')
    with broker.db.connect() as tx:
        summary = json.loads(tx.execute('SELECT summary_json FROM diagnosis_runs WHERE id=?',
                                        (broker.run_id,)).fetchone()[0])
    assert summary['diagnosis_status'] == 'unresolved'
    report = summary['report']['fact_report']
    assert not report['confirmed_facts'] and not report['direct_anomalies']
    check = next(c for c in report['checks'] if c['tool_id']=='service.status')
    assert check['tool_status'] == 'succeeded'
    assert check['evidence_status'] == 'expired'


def test_incomplete_observation_is_not_reported_as_confirmed_fact(investigation):
    create, _, responses = investigation
    broker = create()
    responses[('service.status', 'orders-api.service')] = {'state': 'exited', 'partial': True}
    summary, _ = run_to_completion(broker)
    report = summary['report']['fact_report']
    check = next(row for row in report['checks'] if row['service'] == 'orders-api' and row['tool_id'] == 'service.status')
    assert check['tool_status'] == 'succeeded' and check['evidence_status'] == 'insufficient_for_gate'
    assert not any(fact['service'] == 'orders-api' and fact['kind'] == 'service_state' for fact in report['confirmed_facts'])


def test_exhausted_existing_tool_manager_stops_without_extra_tool_or_model_calls(investigation, monkeypatch):
    create, reads, _ = investigation
    broker = create()

    def exhausted(*args, **kwargs):
        raise BudgetExceeded('fixture existing budget exhausted')

    monkeypatch.setattr(ContextBudgetManager, 'consume_tool', exhausted)
    summary, calls = run_to_completion(broker)
    assert reads == [] and len(calls) == 1
    assert broker.manager.get(broker.budget_id)['calls'] == 0
    assert summary['stop_reason'] == 'global_budget_exhausted'
    assert summary['report']['fact_report']['missing_evidence']


def test_large_scope_baseline_is_not_starved_by_configuration_candidates(investigation):
    create, _, _ = investigation
    broker = create(services=15)
    _, calls = run_to_completion(broker)
    assert len([call for call in calls if call['tool_id'] == 'service.status']) == 15
    assert len([call for call in calls if call['tool_id'] == 'service.logs']) == 15


@pytest.mark.parametrize('task_type', ['health_check', 'focused_diagnosis'])
def test_task_scope_filtering_precedes_candidate_capacity(investigation, task_type):
    create, _, _ = investigation
    broker = create(services=15, related=True, symptom='component-0 unavailable', task_type=task_type)
    _, calls = run_to_completion(broker)
    assert {call['tool_id'] for call in calls} >= {'service.status', 'service.logs', 'health.get'}
    with broker.db.connect() as tx:
        nodes = {row[0] for row in tx.execute('SELECT target_node_id FROM evidence WHERE tool_call_id IN '
                                             '(SELECT id FROM tool_calls WHERE run_id=?)', (broker.run_id,))}
    assert nodes == {broker.root, next(e['target_node_id'] for e in broker.evidence if e['tool_id']=='service.list')}


@pytest.mark.parametrize('old_logs', [False, True])
def test_offline_gap_reads_are_not_starved_by_irrelevant_configuration_candidates(investigation, old_logs):
    create, _, responses = investigation
    symptom = ' '.join('component-' + str(i) for i in range(1, 14))
    broker = create(services=15, related=True, symptom=symptom)
    verify_scope(broker)
    if old_logs:
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        for index in range(15):
            responses[('service.logs', 'component-' + str(index) + '.service')] = {
                'text': old + ' application check completed', 'connector': 'mock'}
    missing_target = next(node['id'] for node in broker.graph['nodes'] if node['name'] == 'component-14')
    broker.graph['edges'] = [{'id': uid(), 'from_node_id': broker.root, 'to_node_id': missing_target,
                              'relation': 'depends_on', 'certainty': 'pending', 'attributes': {}}]
    _, calls = run_to_completion(broker)
    with broker.db.connect() as tx:
        config_nodes = {row[0] for row in tx.execute("SELECT target_node_id FROM evidence WHERE tool_call_id IN "
            "(SELECT id FROM tool_calls WHERE run_id=? AND tool_id='config.read')", (broker.run_id,))}
    assert config_nodes == {broker.root, missing_target}
    assert any(call['tool_id'] == 'config.read' for call in calls)


def test_fact_report_preserves_noncritical_unknowns_after_a_verified_chain():
    from app.agent.fact_report import build_fact_report
    from test_result_evaluator import complete_state, evaluate
    gate_state, assessments = complete_state()
    gate_state['report']['model_unknowns'] = ['Who issued the maintenance change remains unknown']
    result = evaluate(gate_state, assessments)
    assert result['diagnosis_status'] == 'root_cause_supported'
    state = {'scope_node_ids': [], 'graph': {'nodes': []}, 'incident': {'window_start': now()}, 'evidence': []}
    report = build_fact_report(state, result, [])
    assert any('maintenance change' in item for item in report['unknown_causes'])


def test_fact_report_exposes_critical_graph_conflict_as_a_missing_requirement():
    from app.agent.fact_report import build_fact_report
    from test_result_evaluator import complete_state, evaluate
    gate_state, assessments = complete_state()
    gate_state['critical_ambiguities'] = [{'id': 'conflict-1', 'subject_key': 'orders-store',
                                         'predicate': 'database_flavor'}]
    result = evaluate(gate_state, assessments)
    assert not result['completion_satisfied']
    state = {'scope_node_ids': [], 'graph': {'nodes': []}, 'incident': {'window_start': now()}, 'evidence': []}
    report = build_fact_report(state, result, [])
    assert any('database_flavor' in item for item in report['unknown_causes'])
    assert any(row['requirement_id'] == 'resolve_critical_ambiguity' for row in report['missing_evidence'])


def test_http_fact_and_check_preserve_the_exact_authorized_endpoint(investigation):
    create, _, _ = investigation
    broker = create()
    summary, _ = run_to_completion(broker)
    report = summary['report']['fact_report']
    fact = next(row for row in report['confirmed_facts'] if row['kind'] == 'http_status')
    check = next(row for row in report['checks'] if row['tool_id'] == 'health.get')
    assert fact['endpoint_id'] == check['endpoint_id'] == 'entry-probe'
    assert fact['url'] == check['url'] == 'http://127.0.0.1:19000/health'


def test_existing_time_limit_stops_before_any_transport_or_model_request(investigation, monkeypatch):
    create, reads, _ = investigation
    broker = create()
    monkeypatch.setattr(ContextBudgetManager, 'remaining_seconds', lambda *args: 0)
    summary, calls = run_to_completion(broker)
    assert reads == [] and calls == []
    assert summary['execution_status'] == 'timed_out'
    assert broker.manager.get(broker.budget_id)['calls'] == 0


def test_changed_target_authorization_cannot_be_used_by_offline_collection(investigation):
    create, reads, _ = investigation
    broker = create()
    with broker.db.transaction() as tx:
        config = json.loads(tx.execute('SELECT config_json FROM runtime_targets WHERE id=?',
                                        (broker.target_id,)).fetchone()[0])
        config['access_authorized'] = False
        tx.execute('UPDATE runtime_targets SET config_json=? WHERE id=?', (dumps(config), broker.target_id))
    summary, calls = run_to_completion(broker)
    assert reads == [] and calls and all(row['status'] == 'denied' for row in calls)
    assert summary['diagnosis_status'] == 'unresolved'
    assert not summary['report']['fact_report']['confirmed_facts']


def test_actual_loopback_http_529_is_a_single_audited_request(investigation, fake_model):  # noqa: F811
    create, _, _ = investigation
    _, transport_settings, state, _ = fake_model
    state['status'] = 529
    selected = get_settings().model_copy(update={
        'external_model_enabled': True, 'model_name': transport_settings.model_name,
        'model_base_url': transport_settings.model_base_url, 'model_api_key': transport_settings.model_api_key,
        'model_timeout_seconds': 15, 'model_configuration_revision': None,
    })
    broker = create(settings=selected)
    summary, calls = run_to_completion(broker)
    assert state['requests'] == 1
    assert summary['model_error']['status_code'] == 529
    assert summary['model_error']['retry_count'] == 0
    assert summary['analysis_status'] == 'offline' and len(calls) >= 5
    assert 'synthetic-secret' not in dumps(summary)
    assert 'NEVER-ECHO-REQUEST' not in dumps(summary)
