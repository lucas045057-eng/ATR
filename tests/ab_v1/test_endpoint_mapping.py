from datetime import datetime, timedelta, timezone

from app.agent.adaptive import current_graph
from app.common import dumps
from app.settings import get_settings
from app.storage.database import Database
from test_business import onboard
from test_runtime import discover, read, target


def test_reviewed_runtime_configuration_produces_frozen_directed_endpoint_mapping(client,monkeypatch):
    from app.connectors.mock import MockConnector
    original=MockConnector.read
    def configuration(self,tool,args):
        result=original(self,tool,args)
        if tool=='config.read':
            result['text']='DB_HOST=mysql\nDB_PORT=3306\nDB_PASSWORD=[REDACTED]\n'
        return result
    monkeypatch.setattr(MockConnector,'read',configuration)
    profile=onboard(client)
    runtime=target(client,profile,docker=True)
    base=f"/api/v1/businesses/{profile['business_id']}"
    discovery=discover(client,profile,runtime)
    for service in ('catalog-api','store'):
        binding=next(row for row in discovery['matches'] if row['business_service']==service and row['kind']=='container')
        assert client.post(base+f"/bindings/{binding['id']}/review",json={'decision':'confirm','reason':'核对当前运行对象与业务服务'}).status_code==200
    observation=read(client,profile,runtime,'config.read',{'path':'/srv/./catalog/app.conf'})
    response=client.get(base+f"/targets/{runtime['id']}/endpoint-mapping-candidates")
    assert response.status_code==200,response.text
    candidates=response.json()
    candidate=next(row for row in candidates if row['observation_id']==observation['observation_id'])
    assert candidate['endpoint_id']=='mysql:3306'
    assert 'REDACTED' not in dumps(candidates) and 'DB_PASSWORD' not in dumps(candidates)
    relation=next(row for row in profile['dependencies'] if row['relation']=='depends_on')
    payload={'expected_version':profile['profile_version'],'dependency_fact_id':relation['fact_id'],
             'candidate_id':candidate['candidate_id'],'reason':'核对当前消费者配置与数据库运行对象'}
    wrong=target(client,profile)
    assert client.post(base+f"/targets/{wrong['id']}/endpoint-mappings",json=payload).status_code==422
    saved=client.post(base+f"/targets/{runtime['id']}/endpoint-mappings",json=payload)
    assert saved.status_code==200,saved.text
    graph=client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()
    runtime_edges=[edge for edge in graph['edges'] if edge['relation'] in {'observed_as','runs_on'}]
    assert runtime_edges and all(edge['attributes']['source_refs'] for edge in runtime_edges)
    bindings=client.get(base+'/bindings').json()
    bounded=current_graph(graph,[row for row in bindings if row['target_id']==runtime['id']])
    edge=next(row for row in bounded['edges'] if row['relation']=='depends_on')
    assert edge['attributes']['endpoint_id']=='mysql:3306'
    assert edge['attributes']['source_refs'][0]['observation_id']==observation['observation_id']
    assert edge['attributes']['verification_status']=='confirmed'
    old=dumps(graph)
    db=Database(get_settings().database_path)
    before=current_graph(graph,bindings,database=db)
    assert next(edge for edge in before['edges'] if edge['relation']=='depends_on')['attributes']['endpoint_id']=='mysql:3306'
    monkeypatch.setattr(MockConnector,'read',original)
    read(client,profile,runtime,'config.read',{'path':'/srv/catalog/app.conf'})
    after=current_graph(before,bindings,database=db)
    assert 'endpoint_id' not in next(edge for edge in after['edges'] if edge['relation']=='depends_on')['attributes']
    with db.transaction() as tx:
        tx.execute("UPDATE service_bindings SET freshness_status='stale' WHERE target_id=? AND service='store'",(runtime['id'],))
    bindings=client.get(base+'/bindings').json()
    stale=current_graph(graph,bindings)
    assert 'endpoint_id' not in next(row for row in stale['edges'] if row['relation']=='depends_on')['attributes']
    assert dumps(graph)==old
    with db.transaction() as tx:
        tx.execute('UPDATE runtime_observations SET observed_at=? WHERE id=?',
                   ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),observation['observation_id']))
    assert not any(row['observation_id']==observation['observation_id'] for row in client.get(base+f"/targets/{runtime['id']}/endpoint-mapping-candidates").json())


def test_endpoint_mapping_candidates_ignore_partial_and_cross_target_configuration(client,monkeypatch):
    from app.connectors.mock import MockConnector
    original=MockConnector.read
    def partial(self,tool,args):
        result=original(self,tool,args)
        if tool=='config.read':
            result.update(text='upstream http://app:8000;',partial=True)
        return result
    monkeypatch.setattr(MockConnector,'read',partial)
    profile=onboard(client)
    runtime=target(client,profile)
    read(client,profile,runtime,'config.read',{'path':'/srv/catalog/app.conf'})
    response=client.get(f"/api/v1/businesses/{profile['business_id']}/targets/{runtime['id']}/endpoint-mapping-candidates")
    assert response.status_code==200,response.text
    assert response.json()==[]


def test_configuration_endpoint_candidates_do_not_reveal_credentials_or_comment_examples():
    from app.system.endpoint_mapping import configuration_endpoints
    values=configuration_endpoints('DATABASE_URL=mysql://user:12345@database:3306/data\nAPI_KEY=private:4321\n# example http://example:8000\n')
    assert values==[{'endpoint_id':'database:3306','lines':[1]}]
