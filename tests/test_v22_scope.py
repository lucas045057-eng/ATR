from app.connectors.mock import MockConnector
from test_business import onboard
from test_runtime import discover, read, target


def setup_binding(client):
    profile = onboard(client)
    runtime = target(client, profile)
    snapshot = discover(client, profile, runtime)
    binding = next(m for m in snapshot['matches'] if m['kind'] == 'service')
    result = client.post(
        f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
        json={'decision': 'confirm', 'reason': '核对隔离业务服务与运行对象'},
    )
    assert result.status_code == 200
    return profile, runtime, binding


def current_binding(client, profile, binding):
    rows = client.get(f"/api/v1/businesses/{profile['business_id']}/bindings").json()
    return next(r for r in rows if r['id'] == binding['id'])


def test_identical_rediscovery_preserves_confirmed_binding_and_identity(client):
    profile, runtime, binding = setup_binding(client)
    snapshot = discover(client, profile, runtime)
    row = current_binding(client, profile, binding)
    assert row['status'] == 'confirmed'
    assert row['freshness_status'] == 'confirmed'
    assert row['runtime_current'] is True
    assert row['observed_at'] and row['source']['discovery_id'] == snapshot['id']
    assert any(m['id'] == binding['id'] for m in snapshot['matches'])


def test_failed_discovery_keeps_authorization_and_original_observation(client, monkeypatch):
    profile, runtime, binding = setup_binding(client)
    before = current_binding(client, profile, binding)
    original = MockConnector.read

    def disconnect(self, tool, args):
        if tool == 'service.list':
            raise TimeoutError('isolated inventory timeout')
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, 'read', disconnect)
    snapshot = discover(client, profile, runtime)
    after = current_binding(client, profile, binding)
    assert snapshot['collection_status'] == 'partial'
    assert after['status'] == 'confirmed'
    assert after['observed_at'] == before['observed_at']
    assert read(client, profile, runtime, 'service.status', {'name': 'catalog-api.service'})['payload']


def test_complete_empty_discovery_stales_fact_but_preserves_read_authorization(client, monkeypatch):
    profile, runtime, binding = setup_binding(client)
    original = MockConnector.read

    def empty(self, tool, args):
        if tool == 'service.list':
            return {'services': [], 'partial': False, 'truncated': False, 'timeout': False}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, 'read', empty)
    discover(client, profile, runtime)
    row = current_binding(client, profile, binding)
    assert row['status'] == 'confirmed'
    assert row['freshness_status'] == 'stale'
    assert row['runtime_current'] is False
    graph = client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()
    assert not any(n['name'] == 'runtime:catalog-api.service' for n in graph['nodes'])
    assert read(client, profile, runtime, 'service.status', {'name': 'catalog-api.service'})['payload']


def test_partial_empty_listing_cannot_prove_confirmed_service_disappeared(client, monkeypatch):
    profile, runtime, binding = setup_binding(client)
    original = MockConnector.read

    def empty_partial(self, tool, args):
        if tool == 'service.list':
            return {'services': [], 'partial': True, 'truncated': True, 'timeout': False}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, 'read', empty_partial)
    discover(client, profile, runtime)
    assert current_binding(client, profile, binding)['freshness_status'] == 'confirmed'


def test_expired_binding_is_not_current_runtime_fact(client):
    from app.settings import get_settings
    from app.storage.database import Database
    from app.system.business_graph import fuse_business_graph

    profile, runtime, binding = setup_binding(client)
    db = Database(get_settings().database_path)
    with db.transaction() as tx:
        tx.execute("UPDATE service_bindings SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (binding['id'],))
    fuse_business_graph(db, profile)
    assert current_binding(client, profile, binding)['runtime_current'] is False
    graph = client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()
    assert not any(n['name'] == 'runtime:catalog-api.service' for n in graph['nodes'])
