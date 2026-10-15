"""Current evidence proves a scoped fault without inventing its trigger."""
# ruff: noqa: F811
from copy import deepcopy

import pytest
from app.agent.mechanisms import MechanismRegistry
from app.agent.result_evaluator import ResultEvaluator
from test_offline_investigation import investigation, run_to_completion  # noqa: F401


def direct_state():
    return {'investigation_goal': 'direct_fault', 'focus_node_id': 'worker',
            'scope_node_ids': ['worker'],
            'investigation_scope': {'target_node_ids': ['worker'], 'current_cycles': {'worker': 'boot'}},
            'incident': {'symptom': 'worker unavailable'},
            'facts': [{'fact_id': 'f', 'kind': 'service_state', 'value': 'exited',
                       'target_node_id': 'worker', 'evidence_id': 'e', 'call_id': 'c',
                       'freshness': 'current', 'startup_cycle': 'boot'}],
            'graph': {'edges': []}}


def test_stopped_instance_completes_direct_goal_without_global_chain():
    result = ResultEvaluator().evaluate(direct_state(), [])
    assert result['diagnosis_status'] == 'direct_cause_supported'
    assert result['completion_satisfied']
    assert result['verified_segments'] == []
    assert result['required_gaps'] == []
    assert result['trigger_cause_status'] == 'unknown'
    assert result['impact_scope']['level'] == 'instance'
    assert result['direct_explanations'][0]['target_node_id'] == 'worker'


@pytest.mark.parametrize('change', ['stale', 'incomplete', 'unbound', 'contradiction', 'ambiguity', 'capability'])
def test_invalid_or_contradicted_status_cannot_explain_symptom(change):
    state = direct_state()
    if change in {'stale', 'incomplete'}:
        state['facts'][0]['freshness'] = change
    elif change == 'unbound':
        state['facts'][0]['target_node_id'] = 'another'
    elif change == 'contradiction':
        state['facts'].append({**state['facts'][0], 'fact_id': 'counter', 'value': 'running',
                               'evidence_id': 'counter', 'call_id': 'counter'})
    elif change == 'ambiguity':
        state['critical_ambiguities'] = [{'subject_key': 'worker'}]
    else:
        state['runtime_gaps'] = [{'rule_id': 'runtime_read', 'target_ids': ['worker'],
                                  'requirement_id': 'authorized_identity_not_verified'}]
    result = ResultEvaluator().evaluate(state, [])
    assert not result['completion_satisfied']
    assert result['diagnosis_status'] != 'root_cause_supported'


def test_running_is_not_application_health():
    state = direct_state()
    state['facts'][0]['value'] = 'running'
    assert ResultEvaluator().evaluate(state, [])['diagnosis_status'] == 'unresolved'


def test_unavailable_entry_probe_does_not_remove_verified_instance_conclusion():
    state = direct_state()
    state['runtime_gaps'] = [{'rule_id': 'runtime_read', 'target_ids': ['worker'],
                              'tool_id': 'health.get', 'requirement_id': 'capability_unavailable'}]
    result = ResultEvaluator().evaluate(state, [])
    assert result['diagnosis_status'] == 'direct_cause_supported'
    assert not result['required_gaps']
    assert not result['impact_scope']['entry_impact_verified']
    assert any(a['assessment'] == 'verification_unavailable' for a in result['rule_assessments'])


def test_one_stopped_replica_requires_entry_or_other_replica_evidence():
    state = direct_state()
    state['focus_instance_count'] = 2
    assert not ResultEvaluator().evaluate(state, [])['completion_satisfied']


@pytest.mark.parametrize('port,expected', [(19000, True), (19001, False)])
def test_entry_impact_requires_current_exact_port_ownership(port, expected):
    from app.common import now
    state = direct_state()
    state['authorized_endpoints'] = [{'target_node_id': 'worker', 'endpoint_id': 'edge',
                                      'url': 'http://127.0.0.1:19000/api'}]
    state['evidence'] = [{'evidence_id': 'e', 'call_id': 'c', 'target_node_id': 'worker',
                          'tool_id': 'container.status', 'observed_at': now(), 'payload': {
                              'container_id': 'a' * 64, 'state': 'exited', 'startup_cycle': 'boot',
                              'published_endpoints': [{'host_ip': '127.0.0.1', 'host_port': port,
                                                       'container_port': 8000, 'protocol': 'tcp'}]}}]
    state['facts'].append({**state['facts'][0], 'kind': 'endpoint_reachability', 'value': False,
                           'fact_id': 'probe', 'evidence_id': 'probe', 'call_id': 'probe'})
    state['evidence'].append({'evidence_id': 'probe', 'call_id': 'probe', 'target_node_id': 'worker',
                              'tool_id': 'health.get', 'observed_at': now(),
                              'payload': {'endpoint_id': 'edge', 'url': 'http://127.0.0.1:19000/api',
                                          'reachable': False, 'failure_kind': 'connection_refused',
                                          'observation_location': 'remote_ssh'}})
    result = ResultEvaluator().evaluate(state, [])
    assert result['impact_scope']['entry_impact_verified'] is expected
    assert result['impact_scope']['level'] == ('entry' if expected else 'instance')
    state['facts'] = state['facts'][:1]
    state['evidence'] = state['evidence'][:1]
    assert not ResultEvaluator().evaluate(state, [])['impact_scope']['entry_impact_verified']


def test_api_symptom_defaults_to_direct_goal_but_explicit_depth_is_preserved():
    from app.agent.ab_investigation import investigation_task
    assert investigation_task({'text': 'API 服务未响应'}) == 'focused_diagnosis'
    assert investigation_task({'text': '为什么退出，追查完整原因'}) == 'root_cause_investigation'
    assert investigation_task({'text': 'API 服务未响应', 'task_type_explicit': True,
                               'task_type': 'health_check'}) == 'health_check'


def test_unrelated_identity_gap_does_not_block_known_direct_fault(investigation):
    create, reads, _ = investigation
    broker = create(task_type='focused_diagnosis')
    from test_offline_investigation import verify_scope
    verify_scope(broker)
    broker.summary['scope_gaps'] = [{'rule_id': 'runtime_read', 'target_ids': [],
                                    'service': 'orders-store', 'requirement_id': 'authorized_identity_not_verified'}]
    summary, _ = run_to_completion(broker)
    assert summary['diagnosis_status'] == 'direct_cause_supported'
    assert [t for t, _ in reads] == ['service.list', 'service.status']


def test_missing_named_or_default_service_cannot_fall_back_to_another_application():
    from app.agent.ab_investigation import investigation_root
    graph = {'nodes': [{'id': name, 'name': name, 'kind': 'service', 'role': 'application'}
                       for name in ('parcel', 'billing')]}
    bindings = [{'service': 'billing', 'kind': 'service'}]
    assert investigation_root(graph, bindings, 'parcel unavailable', {}) is None
    assert investigation_root(graph, bindings, 'API 未响应', {'primary_service': 'parcel'}) is None
    assert investigation_root(graph, bindings, 'API 未响应', {'primary_service': 'billing'}) == 'billing'


def test_unknown_model_explanation_is_a_cited_candidate_never_verified():
    from app.agent.decision_runtime import adapt_minimal_decision, minimal_output_contract
    from app.common import now
    state = {'investigation_goal': 'direct_fault', 'decision_protocol': 'ab_v1',
             'graph': {'nodes': [{'id': 'worker', 'name': 'worker', 'kind': 'service', 'role': 'application'}], 'edges': []},
             'scope_node_ids': ['worker'], 'incident': {'window_start': now()}, 'evidence': [],
             'available_actions': [{'action_id': 'check', 'target_node_id': 'worker', 'tool_id': 'service.logs'}]}
    mode_schema = minimal_output_contract(state)['properties']['hypotheses']['items']['properties']['failure_mode']
    assert 'enum' not in mode_schema
    decision = adapt_minimal_decision({'summary': 'Candidate needs evidence', 'hypotheses': [{
        'target_node_id': 'worker', 'failure_mode': 'certificate_expired',
        'reasoning': 'The reported TLS failure could be caused by an expired certificate.',
        'missing_information': ['Check the current certificate expiry and failure log.'],
        'confidence': 0.99}], 'next_investigation': 'Read the permitted current failure log',
        'next_action_id': 'check', 'finish': False, 'finish_reason': None}, state)
    assert decision['report']['hypotheses'][0]['failure_mode'] == 'certificate_expired'
    assert all(c['status'] != 'supported' for c in decision['claims'])


def test_healthy_entry_refutes_api_outage_but_preserves_instance_anomaly():
    state = direct_state()
    state['facts'].append({**state['facts'][0], 'kind': 'http_status', 'value': 200,
                           'evidence_id': 'entry', 'call_id': 'http', 'fact_id': 'entry'})
    result = ResultEvaluator().evaluate(state, [])
    assert result['diagnosis_status'] == 'partial'
    assert not result['direct_explanations']
    assert result['direct_faults'] and result['counterevidence']


def test_trigger_goal_preserves_unknown_trigger_of_a_stopped_instance():
    state = direct_state()
    state['investigation_goal'] = 'trigger_cause'
    result = ResultEvaluator().evaluate(state, [])
    assert result['diagnosis_status'] == 'partial'
    assert not result['completion_satisfied']
    assert all(g['rule_id'] not in {'dependency_unavailable_at_startup', 'upstream_unavailable'}
               for g in result['required_gaps'])


@pytest.mark.parametrize('rule', ['bind_failure', 'permission_denied'])
def test_single_object_mechanisms_do_not_require_database_or_proxy(rule):
    state = direct_state()
    state['investigation_goal'] = 'trigger_cause'
    state['required_rule_ids'] = [rule]
    state['facts'].append({**state['facts'][0], 'kind': 'log_observation', 'fact_id': 'log',
                           'evidence_id': 'log', 'call_id': 'read-log',
                           'value': {'mechanisms': [rule], 'endpoint_ids': [], 'terminal_failure': True}})
    assessment = MechanismRegistry().verify(rule, state['facts'], [], state['investigation_scope'])
    assert assessment['status'] == 'supported'
    result = ResultEvaluator().evaluate(state, [assessment])
    assert result['diagnosis_status'] == 'root_cause_supported'
    assert not result['required_gaps']
    stale = deepcopy(state)
    stale['facts'][-1]['startup_cycle'] = 'old-boot'
    assert not ResultEvaluator().evaluate(stale, [assessment])['completion_satisfied']


def test_request_permission_error_does_not_prove_why_service_stopped():
    state = direct_state()
    state.update(investigation_goal='trigger_cause', required_rule_ids=['permission_denied'])
    state['facts'].append({**state['facts'][0], 'kind': 'log_observation', 'fact_id': 'log',
                           'evidence_id': 'log', 'call_id': 'read-log',
                           'value': {'mechanisms': ['permission_denied'], 'terminal_failure': False}})
    registry = MechanismRegistry()
    assessment = registry.verify('permission_denied', state['facts'], [], state['investigation_scope'])
    assert assessment['status'] != 'supported'
    assert not ResultEvaluator().evaluate(state, [assessment])['completion_satisfied']


def test_dependency_startup_claim_can_finish_without_a_proxy_business():
    from test_result_evaluator import complete_state
    state, _ = complete_state()
    state.update(investigation_goal='trigger_cause', focus_node_id='app',
                 required_rule_ids=['dependency_unavailable_at_startup'])
    state['facts'] = [f for f in state['facts'] if f['target_node_id'] != 'proxy']
    state['graph']['edges'] = state['graph']['edges'][:1]
    assessment = MechanismRegistry().verify('dependency_unavailable_at_startup', state['facts'],
                                            state['graph']['edges'], state['investigation_scope'])
    assert ResultEvaluator().evaluate(state, [assessment])['diagnosis_status'] == 'root_cause_supported'


def test_explicit_prediction_survives_an_empty_missing_information_list():
    from app.agent.decision_runtime import adapt_minimal_decision
    state = {'investigation_goal': 'direct_fault', 'graph': {'nodes': [
        {'id': 'worker', 'name': 'worker', 'kind': 'service', 'role': 'application'}], 'edges': []},
        'scope_node_ids': ['worker'], 'available_actions': []}
    result = adapt_minimal_decision({'summary': 'Needs verification', 'hypotheses': [{
        'target_node_id': 'worker', 'failure_mode': 'tls_expiry',
        'reasoning': 'A certificate date mismatch is a candidate requiring current evidence.',
        'confidence': 0.9, 'missing_information': [],
        'testable_prediction': 'The currently served certificate expiry precedes the observation date.'}],
        'next_investigation': '', 'next_action_id': None, 'finish': True, 'finish_reason': 'candidate'}, state)
    assert result['report']['hypotheses'][0]['prediction'] == 'The currently served certificate expiry precedes the observation date.'


@pytest.mark.parametrize('status', [200, 204, 401, 403, 500])
def test_any_http_response_refutes_an_instance_stopped_explanation_for_its_entry(status):
    state = direct_state()
    state['facts'].append({**state['facts'][0], 'kind': 'http_status', 'value': status,
                           'evidence_id': 'responded', 'call_id': 'responded', 'fact_id': 'responded'})
    result = ResultEvaluator().evaluate(state, [])
    assert result['diagnosis_status'] == 'partial'
    assert not result['direct_explanations']
    assert not result['impact_scope']['entry_impact_verified']


@pytest.mark.parametrize('message', ['request /startup: permission denied writing /data/report.csv',
                                    'request /initialize failed: permission denied',
                                    'request /fatal: permission denied',
                                    'request /export: startup failed: permission denied'])
def test_request_paths_cannot_supply_terminal_failure_semantics(message):
    from app.agent.decision_runtime import facts_from_state
    from app.common import now
    state = direct_state()
    state['decision_protocol'] = 'ab_v1'
    state['incident']['window_start'] = now()
    state['graph']['nodes'] = [{'id': 'worker', 'kind': 'service', 'name': 'worker'}]
    state['evidence'] = [{'evidence_id': 'log', 'call_id': 'log', 'tool_id': 'service.logs',
                          'target_node_id': 'worker', 'observed_at': now(), 'payload': {
                              'text': now() + ' ' + message, 'startup_cycle': 'boot'}}]
    logs = facts_from_state(state)
    assert logs and logs[0]['value']['terminal_failure'] is False


@pytest.mark.parametrize('names', [('parcel-edge', 'parcel-storage'), ('billing-http', 'ledger-worker')])
def test_direct_loop_stops_after_relevant_status_and_never_calls_model(investigation, monkeypatch, names):
    create, reads, _ = investigation
    monkeypatch.setattr('app.agent.ab_investigation.call_json',
                        lambda *a, **kw: pytest.fail('Direct status already answers this goal'))
    broker = create(names=names, task_type='focused_diagnosis')
    summary, calls = run_to_completion(broker)
    assert [tool for tool, _ in reads] == ['service.list', 'service.status']
    assert reads[-1][1]['name'] == names[0] + '.service'
    assert summary['diagnosis_status'] == 'direct_cause_supported'
    assert summary['execution_status'] == 'completed'
    assert summary['stop_reason'] == 'direct_fault_verified'
    report = summary['report']['fact_report']
    assert report['unknown_causes'] and not report['root_cause_verified']
    assert report['direct_explanations']
    assert not report['missing_evidence']
    assert len(calls) == 2


def test_running_instance_checks_entry_before_logs_or_other_services(investigation):
    create, reads, responses = investigation
    broker = create(task_type='focused_diagnosis')
    responses['service.status', 'orders-api.service'] = {'state': 'running', 'startup_cycle': 'fixture-cycle'}
    summary, _ = run_to_completion(broker)
    assert [tool for tool, _ in reads][:3] == ['service.list', 'service.status', 'health.get']
    assert summary['diagnosis_status'] != 'no_fault_observed'
    assert not any(f['kind'] == 'service_state' and f['value'] == 'exited'
                   for f in summary['report']['fact_report']['confirmed_facts'])


def test_live_entry_with_one_stopped_replica_finishes_with_scoped_anomaly_only(investigation):
    create, reads, responses = investigation
    broker = create(task_type='focused_diagnosis', replicas=2)
    responses['service.status', 'orders-api-replica-1.service'] = {'state': 'running', 'startup_cycle': 'second-cycle'}
    responses['health.get', None] = {'status_code': 200, 'endpoint_id': 'entry-probe',
                                     'url': 'http://127.0.0.1:19000/health',
                                     'observation_location': 'remote_ssh', 'redirected': False}
    summary, _ = run_to_completion(broker)
    assert summary['diagnosis_status'] == 'partial'
    assert summary['stop_reason'] == 'entry_healthy_instance_fault'
    assert not summary['diagnosis_result']['completion_satisfied']
    assert not summary['diagnosis_result']['impact_scope']['entry_impact_verified']
    assert len([t for t, _ in reads if t == 'service.status']) == 2
    assert not any(t in {'service.logs', 'config.read'} for t, _ in reads)


def test_progress_ignores_new_ids_and_same_check_is_never_replayed(investigation):
    create, reads, _ = investigation
    broker = create(task_type='root_cause_investigation')
    summary, _ = run_to_completion(broker)
    assert len([(t, str(a)) for t, a in reads]) == len(set((t, str(a)) for t, a in reads))
    assert all(h.get('question') and h.get('distinguishes') and h.get('changes_judgment')
               for h in summary['action_history'])


def test_exact_stopped_service_does_not_require_a_running_invocation(investigation):
    create, reads, responses = investigation
    broker = create(task_type='focused_diagnosis')
    responses['service.status', 'orders-api.service'] = {'state': 'inactive', 'name': 'orders-api.service'}
    summary, _ = run_to_completion(broker)
    assert summary['diagnosis_status'] == 'direct_cause_supported'
    assert [t for t, _ in reads] == ['service.list', 'service.status']


def test_confirmed_fact_survives_model_outage_without_another_model_request(investigation, monkeypatch):
    create, _, _ = investigation
    broker = create(task_type='focused_diagnosis')
    broker.summary['model_error'] = {'code': 'provider_unavailable', 'stage': 'request', 'retry_count': 0}
    broker.summary['model_status'] = 'failed'
    monkeypatch.setattr('app.agent.ab_investigation.call_json', lambda *a, **kw: pytest.fail('No retries'))
    summary, _ = run_to_completion(broker)
    assert summary['diagnosis_status'] == 'direct_cause_supported'
    assert summary['execution_status'] == 'completed'
    assert summary['report']['fact_report']['confirmed_facts']
    assert summary['analysis_status'] == 'offline'


def test_complex_chain_expands_only_after_current_connection_failures(investigation):
    from app.common import now
    from test_offline_investigation import verify_scope
    create, reads, responses = investigation
    broker = create(services=3, related=True, task_type='root_cause_investigation')
    verify_scope(broker)
    nodes = {n['name']: n['id'] for n in broker.graph['nodes'] if n['kind'] == 'service' and n['role'] != 'runtime'}
    proxy, app, db = [nodes['component-' + str(i)] for i in range(3)]
    broker.graph['edges'] = [
        {'from_node_id': proxy, 'to_node_id': app, 'relation': 'depends_on',
         'attributes': {'endpoint_id': 'component-1:8000', 'verification_status': 'confirmed', 'source_refs': ['runtime-config:1']}},
        {'from_node_id': app, 'to_node_id': db, 'relation': 'depends_on',
         'attributes': {'endpoint_id': 'component-2:3306', 'verification_status': 'confirmed', 'source_refs': ['runtime-config:2']}}]
    broker.planner.graph = broker.graph
    responses['service.status', 'component-0.service'] = {'state': 'running', 'startup_cycle': 'fixture-cycle'}
    responses['health.get', None] = {'status_code': 502, 'endpoint_id': 'entry-probe',
                                     'observation_location': 'remote_ssh', 'redirected': False}
    responses['service.logs', 'component-0.service'] = {'text': now() + ' upstream connection refused component-1:8000',
                                                       'startup_cycle': 'fixture-cycle', 'cycle_verified': True}
    responses['service.logs', 'component-1.service'] = {'text': now() + ' startup database connect failed component-2:3306',
                                                       'startup_cycle': 'fixture-cycle', 'cycle_verified': True}
    summary, _ = run_to_completion(broker)
    assert summary['diagnosis_status'] == 'root_cause_supported'
    tools = [(t, a.get('name')) for t, a in reads]
    assert tools.index(('service.logs', 'component-0.service')) < tools.index(('service.status', 'component-1.service'))
    assert tools.index(('service.logs', 'component-1.service')) < tools.index(('service.status', 'component-2.service'))
    assert {'dependency_unavailable_at_startup', 'upstream_unavailable'} <= {
        s['rule_id'] for s in summary['diagnosis_result']['verified_segments']}


def test_offline_connection_failure_checks_consumer_configuration_without_assuming_dependency(investigation, monkeypatch):
    from app.common import now
    from app.context.model import ModelUnavailable
    create, reads, responses = investigation
    broker = create(related=True, task_type='root_cause_investigation')
    responses['service.logs', 'orders-api.service'] = {
        'text': now() + ' startup database connection refused orders-store:5432',
        'startup_cycle': 'fixture-cycle', 'cycle_verified': True}
    requests = []

    def unavailable(*args, **kwargs):
        requests.append(True)
        raise ModelUnavailable('Isolated provider unavailable')

    monkeypatch.setattr('app.agent.ab_investigation.call_json', unavailable)
    summary, _ = run_to_completion(broker)
    assert len(requests) == 1
    assert [t for t, _ in reads][-2:] == ['config.list', 'config.read']
    assert not any(a.get('name') == 'orders-store.service' for _, a in reads)
    assert summary['diagnosis_status'] == 'partial'
    assert any(g['requirement_id'] == 'verified_endpoint_mapping' and g['target_ids'] == [broker.root]
               for g in summary['diagnosis_result']['required_gaps'])
