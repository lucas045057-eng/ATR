import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from app.agent.ab_investigation import InvestigationBroker, prepare_run
from app.common import dumps, now, uid
from app.jobs import reserve_investigation
from app.runtime.coordinator import get_coordinator
from app.settings import get_settings
from app.storage.database import Database
from app.system.scope import candidate_digest
from test_business import SAMPLE, onboard
from test_result_evaluator import complete_state, evaluate
from test_runtime import target


def native_run(client,status='root_cause_supported',mode='graph_adaptive'):
    files={**SAMPLE,'compose.yaml':'services:\n  catalog-api: {build: ., depends_on: [store]}\n  store: {image: mysql:8.4}\n  edge-proxy: {image: nginx, depends_on: [catalog-api]}\n'}
    profile=onboard(client,files)
    runtime=target(client,profile)
    assert client.patch(f"/api/v1/businesses/{profile['business_id']}/targets/{runtime['id']}/health-endpoints",json={'health_endpoints':[{'endpoint_id':'proxy-health','url':'http://127.0.0.1:18000/health','service':'edge-proxy'}]}).status_code==200
    database=Database(get_settings().database_path)
    candidate={'kind':'service','id':'edge-proxy.service','name':'edge-proxy'}
    with database.transaction() as tx:
        tx.execute("INSERT INTO service_bindings(id,business_id,target_id,service,runtime_id,kind,score,reasons_json,evidence_ids_json,status,discovery_id,freshness_status,observed_at,expires_at,source_json,candidate_digest) VALUES(?,?,?,?,?,'service',1,'[]','[]','confirmed',?,'confirmed',?,?,'{}',?)",
                   (uid(),profile['business_id'],runtime['id'],'edge-proxy','edge-proxy.service',uid(),now(),(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),candidate_digest(candidate)))
    event=client.post('/api/v1/incidents',json={'system_id':profile['system_id'],'symptom':'catalog-api unavailable','mode':mode}).json()['id']
    job,_,_=reserve_investigation(database,event,profile['business_id'],runtime['id'])
    run_id=prepare_run(database,get_settings(),event,profile['business_id'],runtime['id'],job,'synthetic-operator')
    coordinator=get_coordinator(database)
    lease=coordinator.claim(run_id)
    # Keep this synthetic result-consumer fixture behind the real identity preflight.
    from app.connectors.mock import MockConnector
    original=MockConnector.read
    def inventory(self,tool,args):
        return {'connector':'mock','services':[candidate],'partial':False} if tool=='service.list' else original(self,tool,args)
    with patch.object(MockConnector,'read',inventory):
        broker=InvestigationBroker(database,get_settings(),lease)
        while not broker.scope_verified:
            assert broker.advance()['continue']
    state,assessments=complete_state()
    result=evaluate(state,assessments)
    with database.transaction() as tx:
        run=dict(tx.execute('SELECT * FROM diagnosis_runs WHERE id=?',(run_id,)).fetchone())
        nodes={row['name']:row['id'] for row in tx.execute("SELECT * FROM graph_nodes WHERE snapshot_id=? AND kind='service'",(run['graph_snapshot_id'],))}
        mapping={'app':nodes['catalog-api'],'db':nodes['store'],'proxy':nodes['edge-proxy']}
        for segment in result['verified_segments']:
            segment['participants']={role:mapping[node] for role,node in segment['participants'].items()}
            for citation in segment['supports']:
                citation['observed_on']=mapping[citation['observed_on']]
        citations={item['call_id']:item for segment in result['verified_segments'] for item in segment['supports']}
        for call,citation in citations.items():
            kind='container.logs' if 'log' in citation['evidence_id'] or 'proxy' in citation['evidence_id'] else 'health.get' if 'http' in citation['evidence_id'] else 'container.status'
            tx.execute("INSERT INTO tool_calls(id,run_id,snapshot_id,purpose,tool_id,adapter_version,args_redacted_json,risk,policy_version,status,request_id,started_at,ended_at,target_id) VALUES(?,?,?,'diagnosis',?,'synthetic-fixture','{}','LOW','fixture','succeeded',?,?,?,?)",
                       (call,run_id,run['graph_snapshot_id'],kind,uid(),now(),now(),runtime['id']))
            tx.execute("INSERT INTO evidence(id,tool_call_id,target_node_id,observed_at,payload_redacted_json,content_hash,truncated,freshness_state) VALUES(?,?,?,?,?,'synthetic-fixture',0,'current')",
                       (citation['evidence_id'],call,citation['observed_on'],now(),dumps({'source_type':'Runtime'})))
        summary=json.loads(run['summary_json'])
        if status!='root_cause_supported':
            result.update(diagnosis_status=status,completion_satisfied=status=='no_fault_observed',verified_segments=[],required_gaps=[] if status=='no_fault_observed' else [{'requirement_id':'current_logs','target_ids':[mapping['app']]}])
            if status=='no_fault_observed':
                result['direct_faults']=[]
        summary['diagnosis_result']=result
        tx.execute('UPDATE diagnosis_runs SET summary_json=? WHERE id=?',(dumps(summary),run_id))
    coordinator.finalize(run_id,'completed',result,lease_token=lease['lease_token'])
    coordinator.reaped(run_id,lease['lease_token'])
    return client.get(f'/api/v1/runs/{run_id}').json(),database,profile,runtime


def test_native_completed_results_are_counted_without_legacy_hypotheses(client):
    run,database,_,_=native_run(client)
    metrics=client.get('/api/v1/metrics').json()
    assert metrics['supported_runs']==1 and metrics['runs_completed']==1
    assert metrics['tt_rca_seconds_median'] is not None
    assert run['repair_eligible'] and run['mode']=='graph_adaptive'
    with database.connect() as tx:
        assert tx.execute('SELECT COUNT(*) FROM hypotheses').fetchone()[0]==0


def test_native_partial_result_is_an_unverified_counterexample_not_root_support(client):
    run,_,_,_=native_run(client,'partial')
    records=client.get('/api/v1/knowledge-counterexamples').json()
    selected=next(row for row in records if row['run_id']==run['id'])
    assert selected['counterexamples'][0]['knowledge_status']=='unverified_counterexample'
    assert selected['counterexamples'][0]['missing_evidence']
    assert not run['repair_eligible']
    assert client.get('/api/v1/metrics').json()['supported_runs']==0


def test_native_healthy_completion_is_counted_separately_and_cannot_capture_root_repair(client):
    run,_,_,_=native_run(client,'no_fault_observed')
    metrics=client.get('/api/v1/metrics').json()
    assert metrics['runs_completed']==1 and metrics['healthy_runs']==1 and metrics['supported_runs']==0
    assert metrics['tt_rca_seconds_median'] is None and not run['repair_eligible']


def test_native_adaptive_trial_uses_typed_segment_truth_and_persisted_arm(client):
    run,_,profile,_=native_run(client)
    truth=[{'rule_id':segment['rule_id'],'participants':{role:{'catalog-api':'catalog-api','store':'store','edge-proxy':'edge-proxy'}[next(node['name'] for node in client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()['nodes'] if node['id']==identifier)] for role,identifier in segment['participants'].items()}} for segment in run['diagnosis_result']['verified_segments']]
    response=client.post('/api/v1/evaluation-cases',json={'case_key':'native-segment-truth','system_id':profile['system_id'],'fault_class':'dependency_unavailable','protocol_version':'ab-segments-1','ground_truth':{'segments':truth}})
    assert response.status_code==200,response.text
    case=response.json()
    trial={'case_id':case['id'],'run_id':run['id'],'pair_key':'native-adaptive','arm':'graph_adaptive','repetition':1,'reset_digest':case['fixture_digest'],'verdict':'correct'}
    response=client.post('/api/v1/evaluation-trials',json=trial)
    assert response.status_code==200,response.text
    assert response.json()['metrics']['expected_verdict']=='correct'


def test_native_repair_uses_original_remote_target_and_separate_verification_audit(client,monkeypatch):
    from app.connectors.mock import MockConnector
    original=MockConnector.read
    reads=[]
    def remote(self,tool,args):
        if tool=='health.get':
            reads.append((self.config['id'] if 'id' in self.config else self.config['name'],args))
            return {'protocol':'atr-read-ab-v1','tool_version':1,'observation_location':'remote_ssh',
                    'observed_host':'synthetic-original-host','status_code':200,
                    'endpoint_id':args['endpoint_id'],'url':'http://127.0.0.1:18000/health','redirected':False}
        return original(self,tool,args)
    monkeypatch.setattr(MockConnector,'read',remote)
    run,database,profile,runtime=native_run(client)
    assert run['verification_endpoints'][0]['endpoint_id']=='proxy-health'
    with database.transaction() as tx:
        tx.execute("UPDATE context_budgets SET calls=8,tool_calls=20,stop_reason='tool_call_limit' WHERE run_id=?",(run['id'],))
    with database.connect() as tx:
        before=[tuple(row) for row in tx.execute('SELECT calls,tool_calls,elapsed_ms FROM context_budgets WHERE run_id=?',(run['id'],))]
    response=client.post(f"/api/v1/incidents/{run['incident_id']}/repair",json={'summary':'人工恢复当前业务服务','operator_verified':True,
                         'verification_summary':'核对原目标中已授权端点恢复为200','verification_url':'http://127.0.0.1:18000/health'})
    assert response.status_code==200,response.text
    assert len(reads)==1 and reads[0][1]=={'endpoint_id':'proxy-health'}
    with database.connect() as tx:
        verification=tx.execute("SELECT * FROM tool_calls WHERE run_id=? AND purpose='verification'",(run['id'],)).fetchone()
        assert verification['tool_id']=='health.get' and verification['target_id']==runtime['id']
        assert verification['request_id'] in [row[0] for row in tx.execute('SELECT id FROM runtime_observations WHERE target_id=?',(runtime['id'],))]
        after=[tuple(row) for row in tx.execute('SELECT calls,tool_calls,elapsed_ms FROM context_budgets WHERE run_id=?',(run['id'],))]
        assert after==before and tx.execute('SELECT COUNT(*) FROM hypotheses').fetchone()[0]==0
        cause=json.loads(tx.execute('SELECT root_cause_json FROM experiences').fetchone()[0])
        assert cause['schema_version']=='ab-result-1' and cause['verified_segments']
    saved=response.json()
    promoted=client.post(f"/api/v1/patterns/{saved['pattern_version_id']}/review",json={'decision':'approve','reason':'核对当前结果和原服务器恢复观测'})
    assert promoted.status_code==200,promoted.text
    with database.connect() as tx:
        assert tx.execute('SELECT business_id FROM experience_ownership WHERE experience_id=?',(saved['experience_id'],)).fetchone()[0]==profile['business_id']


@pytest.mark.parametrize('changes',[{'status_code':503},{'url':'http://127.0.0.1:18000/other'},
                                   {'endpoint_id':'other'},{'observation_location':'local'},{'redirected':True}])
def test_native_repair_keeps_failed_verification_audit_without_creating_experience(client,monkeypatch,changes):
    from app.connectors.mock import MockConnector
    original=MockConnector.read
    def remote(self,tool,args):
        if tool=='health.get':
            return {'protocol':'atr-read-ab-v1','tool_version':1,'observation_location':'remote_ssh',
                    'observed_host':'synthetic-original-host','status_code':200,'endpoint_id':'proxy-health',
                    'url':'http://127.0.0.1:18000/health','redirected':False,**changes}
        return original(self,tool,args)
    monkeypatch.setattr(MockConnector,'read',remote)
    run,database,_,_=native_run(client)
    response=client.post(f"/api/v1/incidents/{run['incident_id']}/repair",json={'summary':'人工恢复当前业务服务','operator_verified':True,
                         'verification_summary':'检查原目标的恢复状态','verification_endpoint_id':'proxy-health'})
    assert response.status_code==409,response.text
    with database.connect() as tx:
        assert tx.execute('SELECT COUNT(*) FROM experiences').fetchone()[0]==0
        assert tx.execute("SELECT COUNT(*) FROM tool_calls WHERE purpose='verification'").fetchone()[0]==1


def test_native_citation_owned_by_another_target_blocks_capture_before_verification(client):
    run,database,_,_=native_run(client)
    with database.transaction() as tx:
        tx.execute('UPDATE tool_calls SET target_id=NULL WHERE id=?',(run['diagnosis_result']['verified_segments'][0]['supports'][0]['call_id'],))
    response=client.post(f"/api/v1/incidents/{run['incident_id']}/repair",json={'summary':'人工恢复当前业务服务','operator_verified':True,
                         'verification_summary':'检查原目标的恢复状态','verification_endpoint_id':'proxy-health'})
    assert response.status_code==409,response.text
    with database.connect() as tx:
        assert tx.execute('SELECT COUNT(*) FROM experiences').fetchone()[0]==0
        assert tx.execute("SELECT COUNT(*) FROM tool_calls WHERE purpose='verification'").fetchone()[0]==0


def test_verification_rechecks_authorization_against_exact_dispatch_config(client,monkeypatch):
    from app.connectors.mock import MockConnector
    from app.tools.business import BusinessToolRegistry
    run,database,profile,runtime=native_run(client)
    validated=BusinessToolRegistry._validate_runtime_action
    reads=[]
    def change_after_validation(self,action):
        result=validated(self,action)
        with database.transaction() as tx:
            config=json.loads(tx.execute('SELECT config_json FROM runtime_targets WHERE id=?',(runtime['id'],)).fetchone()[0])
            config['health_endpoints'][0]['url']='http://127.0.0.1:18000/changed'
            tx.execute('UPDATE runtime_targets SET config_json=? WHERE id=?',(dumps(config),runtime['id']))
        return result
    monkeypatch.setattr(BusinessToolRegistry,'_validate_runtime_action',change_after_validation)
    monkeypatch.setattr(MockConnector,'read',lambda self,tool,args: reads.append((tool,args)) or {})
    response=client.post(f"/api/v1/incidents/{run['incident_id']}/repair",json={'summary':'人工恢复当前业务服务','operator_verified':True,
                         'verification_summary':'检查原目标的恢复状态','verification_endpoint_id':'proxy-health'})
    assert response.status_code==409,response.text
    assert reads==[]
    with database.connect() as tx:
        assert tx.execute('SELECT COUNT(*) FROM experiences').fetchone()[0]==0
        assert tx.execute("SELECT COUNT(*) FROM runtime_observations WHERE status='denied'").fetchone()[0]==1
