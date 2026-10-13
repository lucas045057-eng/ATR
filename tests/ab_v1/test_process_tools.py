import os
from datetime import datetime, timedelta, timezone

import pytest
from app.agent.ab_investigation import clarification_candidates
from app.agent.adaptive import AdaptiveInvestigationPlanner
from app.agent.decision_runtime import facts_from_state
from app.common import now
from app.connectors import remote_reads
from app.connectors.capabilities import validate_payload
from app.connectors.diagnostics import ToolParseError
from app.connectors.models import RuntimeTargetCreate
from app.connectors.policy import check
from app.connectors.remote_reads import execute
from pydantic import ValidationError

START='a1234567-1234-1234-1234-123456789012:4321'
ROW={'pid':7,'name':'api','state':'running','start_time':START,'started_at':'2026-10-13T00:00:00+00:00'}
SOURCE={'file_id':'api-log','service':'api','path':'/opt/myapp/api.log','pid':7,'start_time':START}


def test_pid_reuse_never_reads_the_new_process(monkeypatch):
    monkeypatch.setattr(remote_reads,'process_identity',lambda pid:{**ROW,'start_time':START+'0'})
    with pytest.raises(PermissionError,match='reused'):
        execute({'processes':[ROW]},'process.status',{'pid':7,'start_time':START})


def test_exited_process_preserves_only_confirmed_start_identity(monkeypatch):
    def gone(pid):
        raise FileNotFoundError
    monkeypatch.setattr(remote_reads,'process_identity',gone)
    monkeypatch.setattr(remote_reads,'kernel_process_clock',lambda:(START.split(':')[0],1760000000,100))
    result=execute({'processes':[ROW]},'process.status',{'pid':7,'start_time':START})
    assert result['state']=='exited' and result['startup_cycle']==START
    with pytest.raises(PermissionError):
        execute({'processes':[]},'process.status',{'pid':7,'start_time':START})
    monkeypatch.setattr(remote_reads,'kernel_process_clock',lambda:('different-boot',1760000000,100))
    with pytest.raises(PermissionError,match='boot'):
        execute({'processes':[ROW]},'process.status',{'pid':7,'start_time':START})


def test_fixed_log_requires_matching_approved_process_identity():
    config={'log_files':[SOURCE]}
    with pytest.raises(PermissionError):
        check(config,'log.tail',{'file_id':'api-log'},[])
    assert check(config,'log.tail',{'file_id':'api-log'},[('process','7:'+START)])['file_id']=='api-log'
    with pytest.raises(ValidationError):
        RuntimeTargetCreate(name='t',allowed_paths=['/opt/myapp'],log_files=[{**SOURCE,'path':'/root/secret.log'}])


def test_log_denied_by_os_is_not_empty_success(monkeypatch):
    monkeypatch.setattr(remote_reads,'process_identity',lambda pid:ROW)
    def denied(*args):
        raise PermissionError('unreadable')
    with pytest.raises(PermissionError,match='unreadable'):
        execute({'processes':[ROW],'log_files':[SOURCE]},'log.tail',{'file_id':'api-log'},secure_open=denied)


def test_log_read_checks_same_process_before_and_after(monkeypatch,tmp_path):
    path=tmp_path/'api.log'
    path.write_text('2026-10-13T01:00:00Z ready\n')
    calls=[]
    def identity(pid):
        calls.append(pid)
        return ROW if len(calls)==1 else {**ROW,'start_time':START+'0'}
    monkeypatch.setattr(remote_reads,'process_identity',identity)
    with pytest.raises(PermissionError,match='reused'):
        execute({'processes':[ROW],'log_files':[SOURCE]},'log.tail',{'file_id':'api-log'},
                secure_open=lambda *args:os.open(path,os.O_RDONLY))
    assert calls==[7,7]


def test_enriched_inventory_binds_selection_to_pid_and_start(monkeypatch):
    monkeypatch.setattr(remote_reads.os,'listdir',lambda path:['7','8','sys'])
    monkeypatch.setattr(remote_reads,'process_identity',lambda pid:{**ROW,'pid':pid})
    result=execute({},'process.list',{'limit':1})
    assert result['processes'][0]['id']=='7:'+START and result['partial'] and result['next_cursor']=='0:7'
    candidates,_=clarification_candidates([{'evidence_id':'discovery','payload':result}])
    assert candidates[0]['kind']=='process' and candidates[0]['runtime_id']=='7:'+START
    assert clarification_candidates([{'evidence_id':'old','payload':{'processes':[{'id':'7','pid':7,'name':'api'}]}}])[0]==[]


def test_forged_remote_process_identity_is_rejected():
    base={'protocol':remote_reads.PROTOCOL,'tool_version':2,'observation_location':'remote_ssh','observed_host':'test',
          'pid':7,'start_time':START,'startup_cycle':START,'state':'running'}
    validate_payload('process.status',{'pid':7,'start_time':START},base,{})
    with pytest.raises(ToolParseError):
        validate_payload('process.status',{'pid':8,'start_time':START},base,{})


def test_planner_uses_exact_process_grant_and_its_fixed_log():
    graph={'nodes':[{'id':'api','name':'api','kind':'service','role':'application'}],'edges':[]}
    binding={'id':'binding','kind':'process','runtime_id':'7:'+START,'service':'api','target_id':'target',
             'status':'confirmed','freshness_status':'confirmed','observed_at':now(),'expires_at':'2099-01-01T00:00:00+00:00'}
    config={'connector':'mock','log_files':[SOURCE],'allowed_paths':[],'docker_reads_enabled':False}
    planner=AdaptiveInvestigationPlanner(graph,[binding],{'target':config},'排查','api',None,include_processes=True)
    actions=planner.candidates([],[],[],include_authorized_scope=True)
    assert {a['tool_id'] for a in actions}=={'process.status','log.tail'}
    assert next(a for a in actions if a['tool_id']=='process.status')['arguments']=={'pid':7,'start_time':START}
    legacy=AdaptiveInvestigationPlanner(graph,[binding],{'target':config},'排查','api',None)
    assert legacy.candidates([],[],[])==[]


def test_old_fixed_log_lines_cannot_prove_failure_in_current_process_cycle():
    instant=datetime.now(timezone.utc)
    started=instant-timedelta(seconds=10)
    state={'decision_protocol':'ab_v1','incident':{'window_start':(instant-timedelta(hours=1)).isoformat()},
           'graph':{'nodes':[{'id':'api','kind':'service','name':'api'}]},'evidence':[]}
    for name,offset in [('old',-30),('current',-5)]:
        state['evidence'].append({'evidence_id':name,'target_node_id':'api','tool_id':'log.tail','call_id':name,
                                 'observed_at':instant.isoformat(),'payload':{'source_type':'Runtime','started_at':started.isoformat(),
                                 'startup_cycle':START,'text':(instant+timedelta(seconds=offset)).isoformat()+' ERROR connection refused'}})
    facts=facts_from_state(state)
    assert {fact['evidence_id'] for fact in facts}=={'current'}
