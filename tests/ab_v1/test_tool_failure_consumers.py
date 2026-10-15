import json

import pytest
from app.connectors.diagnostics import CapabilityUnavailable
from app.context.budget import BudgetExceeded
from app.settings import get_settings
from app.tools.business import BusinessToolRegistry
from test_native_result_consumers import native_run


@pytest.mark.parametrize('error,code,stage,status', [
    (ValueError('synthetic-private-output'), 'tool_parse_error', 'request', 'failed'),
    (PermissionError('synthetic-private-output'), 'tool_permission_denied', 'authorization', 'denied'),
    (TimeoutError('synthetic-private-output'), 'tool_timeout', 'request', 'timed_out'),
    (CapabilityUnavailable('synthetic-private-output'), 'capability_unavailable', 'capability', 'denied'),
    (BudgetExceeded('synthetic-private-output'), 'global_budget_exhausted', 'budget', 'failed'),
])
def test_registry_failure_keeps_safe_category_stage_and_call_in_api(client, monkeypatch, error, code, stage, status):
    run, database, profile, runtime = native_run(client, 'partial')
    registry = BusinessToolRegistry(database, get_settings(), profile['business_id'], run['id'],
                                    run['graph_snapshot_id'], run['summary']['budget_id'], runtime['id'])
    def reject(*args, **kwargs):
        raise error
    monkeypatch.setattr(registry.manager if isinstance(error, BudgetExceeded) else registry,
                        'consume_tool' if isinstance(error, BudgetExceeded) else '_validate_runtime_action', reject)
    failed = registry.execute({'tool_id':'service.status', 'target_id':runtime['id'],
                               'target_node_id':'unused', 'arguments':{}})
    assert failed['error_code'] == failed['error'] == code
    assert failed['failure_stage'] == stage and failed['status'] == status
    assert failed['diagnostic']['call_id'] == failed['call_id']
    assert failed['diagnostic']['tool_id'] == 'service.status'
    saved = client.get('/api/v1/runs/' + run['id']).json()
    call = next(item for item in saved['tool_calls'] if item['id'] == failed['call_id'])
    assert call['error_code'] == code and call['diagnostic']['failure_stage'] == stage
    assert 'synthetic-private-output' not in json.dumps(failed)
    assert 'synthetic-private-output' not in json.dumps(saved)


def test_remote_failure_category_is_retained_instead_of_its_python_type(client, monkeypatch):
    from app.connectors.mock import MockConnector
    run, database, profile, runtime = native_run(client, 'partial')
    registry = BusinessToolRegistry(database, get_settings(), profile['business_id'], run['id'],
                                    run['graph_snapshot_id'], run['summary']['budget_id'], runtime['id'])
    def reject(*args, **kwargs):
        raise ValueError('synthetic-private-output')
    monkeypatch.setattr(MockConnector, 'read', reject)
    failed = registry.execute({'tool_id':'host.info', 'target_id':runtime['id'],
                               'target_node_id':'unused', 'arguments':{}})
    assert failed['error'] == failed['error_code'] == 'tool_parse_error'
    assert failed['failure_stage'] == 'request'
    assert failed['diagnostic']['call_id'] == failed['call_id']


@pytest.mark.parametrize('terminated', [False, True])
def test_failure_audit_uses_lease_guard_without_reauthorizing_revoked_business_scope(client, monkeypatch, terminated):
    import threading
    run, database, profile, runtime = native_run(client, 'partial')
    checks = []
    def authorize(tx):
        checks.append('scope')
    def authorize_failure(tx):
        checks.append('lease')
        if terminated:
            raise InterruptedError('lease revoked')
    registry = BusinessToolRegistry(database, get_settings(), profile['business_id'], run['id'],
        run['graph_snapshot_id'], run['summary']['budget_id'], runtime['id'],
        supervision={'authorize':authorize, 'authorize_failure':authorize_failure, 'cancelled':threading.Event()})
    def reject(*args, **kwargs):
        raise PermissionError('scope revoked')
    monkeypatch.setattr(registry, '_validate_runtime_action', reject)
    action = {'tool_id':'host.info', 'target_id':runtime['id'], 'target_node_id':'unused', 'arguments':{}}
    if terminated:
        with pytest.raises(InterruptedError):
            registry.execute(action)
    else:
        assert registry.execute(action)['error_code'] == 'tool_permission_denied'
    assert checks[-1] == 'lease' and checks.count('lease') == 1


def test_registry_private_failure_callback_never_enters_connector_supervision(client, monkeypatch):
    import threading

    from app.connectors.service import RuntimeService
    run, database, profile, runtime = native_run(client, 'partial')
    def authorize(tx):
        pass
    registry = BusinessToolRegistry(database, get_settings(), profile['business_id'], run['id'],
        run['graph_snapshot_id'], run['summary']['budget_id'], runtime['id'],
        supervision={'authorize':authorize, 'authorize_failure':authorize, 'cancelled':threading.Event()})
    seen = []
    def failed(self, *args, supervision=None, **kwargs):
        seen.append(supervision)
        return {'observation_id':'synthetic-read', 'status':'failed', 'bytes':0,
                'error_code':'tool_connection_error', 'diagnostic':{'failure_stage':'request'}}
    monkeypatch.setattr(RuntimeService, 'execute', failed)
    result = registry.execute({'tool_id':'host.info', 'target_id':runtime['id'],
                               'target_node_id':'unused', 'arguments':{}})
    assert result['error_code'] == 'tool_connection_error'
    assert seen and 'authorize_failure' not in seen[0]
    assert seen[0]['authorize'] is authorize and registry.supervision['authorize_failure'] is authorize
