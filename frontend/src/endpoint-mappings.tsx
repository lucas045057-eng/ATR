import React,{useEffect,useState} from 'react';
import {api,Row} from './api';

export function EndpointMappings({base,targetId,profile,bindings,refreshKey,onSaved}:{base:string;targetId:string;profile:Row;bindings:Row[];refreshKey:string;onSaved:()=>Promise<void>}) {
  const [candidates,setCandidates]=useState<Row[]>([]);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [revision,setRevision]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();setCandidates([]);setError('');
    api(`${base}/targets/${targetId}/endpoint-mapping-candidates`,{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted)setCandidates(value);})
      .catch(reason=>{if(!controller.signal.aborted)setError(reason.message);});
    return()=>controller.abort();
  },[base,targetId,refreshKey,revision]);
  const confirmed=new Set(bindings.filter(row=>row.target_id===targetId&&row.runtime_current).map(row=>'service:'+row.service));
  const relations=(profile.dependencies||[]).filter((row:Row)=>row.relation==='depends_on'&&confirmed.has(row.from)&&confirmed.has(row.to));
  return <section className="panel business-block"><h2>确认服务连接地址</h2>
    <p>先读取当前服务器的授权配置，再核对哪个消费者连接哪个依赖。确认后，调查才能把日志中的连接失败与这条关系对应起来。</p>
    <button className="btn small" disabled={busy} onClick={()=>setRevision(value=>value+1)}>刷新配置中的连接候选</button>
    {!relations.length&&<p>需要先确认依赖两端属于当前服务器的业务服务，并在业务资料中保留有方向的依赖关系。</p>}
    {!candidates.length&&<p>当前没有完整、有效的配置连接候选。请使用下方的配置读取功能读取明确授权的文件。</p>}
    {!!relations.length&&!!candidates.length&&<form className="form" onSubmit={async event=>{
      event.preventDefault();const form=new FormData(event.currentTarget);setBusy(true);setError('');
      try{await api(`${base}/targets/${targetId}/endpoint-mappings`,{method:'POST',body:JSON.stringify({expected_version:profile.profile_version,dependency_fact_id:form.get('relationship'),candidate_id:form.get('candidate')})});await onSaved();}
      catch(reason){setError((reason as Error).message);}finally{setBusy(false);}
    }}>
      <label>有方向的依赖<select name="relationship" required>{relations.map((row:Row)=><option key={row.fact_id} value={row.fact_id}>{row.from.replace('service:','')} → {row.to.replace('service:','')}</option>)}</select></label>
      <label>当前配置中的地址<select name="candidate" required>{candidates.map(row=><option key={row.candidate_id} value={row.candidate_id}>{row.endpoint_id} · {row.path} · 行 {row.lines.join('、')}</option>)}</select></label>
      <label><input type="checkbox" required/>我已核对所选配置是当前消费者实际使用的连接配置，地址指向所选依赖。</label>
      <button className="btn" disabled={busy}>确认这条连接关系</button>
    </form>}
    {error&&<p role="alert" className="alert error">{error}</p>}
    {(profile.facts||[]).filter((fact:Row)=>fact.predicate==='verified_endpoint'&&fact.verification_status==='confirmed'&&fact.object?.target_id===targetId).map((fact:Row)=><p key={fact.id}>已审核：{fact.subject.replace('service:','')} → {fact.object.dependency.replace('service:','')} · {fact.object.endpoint_id}。运行对象或配置变化后需要重新核对。</p>)}
  </section>;
}
