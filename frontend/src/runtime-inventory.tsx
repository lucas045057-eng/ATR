import React, { useState } from "react";
import { Row } from "./api";

const classification: Row = {unmatched: "未匹配", unrelated: "人工判定不相关", insufficient_evidence: "缺少业务身份依据", confirmed: "已确认"};

export function RuntimeInventory({discovery,onAuthorize,busy=false}: {discovery: Row;onAuthorize?:(value:Row)=>Promise<void>;busy?:boolean}) {
  const [page, setPage] = useState(0);
  const objects: Row[] = discovery.runtime_objects || [];
  const groups: Row[] = discovery.process_groups || [];
  const pages = Math.max(1, Math.ceil(groups.length / 20));
  const current = Math.min(page, pages - 1);
  const row = (o:Row) => <div className="business-row" key={o.id}><div><strong>{o.name}</strong><small>{o.state} · {classification[o.classification] || o.classification} · 依据 {o.evidence_id}</small>
    {onAuthorize && o.classification !== 'confirmed' && <ObjectApproval object={o} busy={busy} onAuthorize={onAuthorize}/>}</div></div>;
  return <div className="business-block">
    <h3>发现的运行对象</h3>
    {["service", "container"].map(kind => <details key={kind} open>
      <summary>{kind === "service" ? "systemd 服务" : "Docker 容器"} · {objects.filter(o => o.kind === kind).length} 项</summary>
      {objects.filter(o => o.kind === kind && o.classification !== "unmatched" && o.classification !== "unrelated").map(row)}
      <details><summary>未匹配或人工判定不相关（默认折叠）</summary>
        {objects.filter(o => o.kind === kind && ["unmatched", "unrelated"].includes(o.classification)).map(row)}
      </details>
    </details>)}
    <details><summary>进程线索 · {groups.reduce((n, g) => n + g.count, 0)} 个 PID / {groups.length} 组（默认折叠）</summary>
      <p className="muted">PID 只标识进程，不等于业务服务。同名进程按状态聚合，底层观测保留。这里只展示已采集页；清单不完整会单独提示。</p>
      {groups.slice(current * 20, (current + 1) * 20).map(g => <details key={g.name + ":" + g.state}>
        <summary>{g.name} · 状态 {g.state} · {g.count} 个进程</summary>
        {g.processes.map((p: Row) => <div key={p.pid}><p>PID {p.pid} · {classification[p.classification]} · 依据 {p.evidence_id}</p>{objects.filter(o=>o.kind==='process'&&(o.pid??o.id)===p.pid).map(row)}</div>)}
      </details>)}
      <button className="btn small" disabled={current === 0} onClick={() => setPage(current - 1)}>上一页</button>
      <span> 第 {current + 1}/{pages} 页 </span>
      <button className="btn small" disabled={current === pages - 1} onClick={() => setPage(current + 1)}>下一页</button>
    </details>
    {!objects.length && <details><summary>历史发现对象（原记录）</summary>{discovery.unassigned_runtime_objects?.join("、") || "无"}</details>}
  </div>;
}

function ObjectApproval({object:o,busy,onAuthorize}: {object:Row;busy:boolean;onAuthorize:(value:Row)=>Promise<void>}) {
  const [stable,setStable] = useState(false);
  const [owned,setOwned] = useState(false);
  const supportsStable = o.kind === 'service' || (o.kind === 'container' && Boolean(o.image?.trim()) && Array.isArray(o.ports) && Array.isArray(o.environment_keys));
  return <details data-testid="unknown-object-approval"><summary>独立审批此未知对象</summary><p>审批仅针对此实际观测候选；不会读取未知对象日志，也不会自动恢复原调查。</p>
    <form className="form" onSubmit={event=>{event.preventDefault();const data=new FormData(event.currentTarget);void onAuthorize({observation_id:o.observation_id,runtime_id:o.id,kind:o.kind,service:data.get('service'),role:data.get('role'),identity_mode:stable?'stable_object':'exact_instance',reason:data.get('reason')})}}>
      <small>实际身份 {o.id} · 观测 {o.observation_id} · 镜像 {o.image||'未提供'} · 端口 {Array.isArray(o.ports) ? o.ports.join('、') || '已采集，空集合' : '未采集'} · 环境键 {Array.isArray(o.environment_keys) ? o.environment_keys.join('、') || '已采集，空集合' : '未采集'}</small>
      <label>本业务中的名称<input name="service" required maxLength={255} defaultValue={o.name}/></label>
      <label>对象作用<select name="role" defaultValue="unknown">{Object.entries({unknown:'作用未知',proxy:'请求入口',application:'应用服务',datastore:'数据库',cache:'缓存',worker:'后台任务',queue:'消息队列'}).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>
      {supportsStable ? <label><input type="checkbox" checked={stable} onChange={event=>setStable(event.target.checked)}/>{o.kind === 'container' ? '明确允许稳定容器授权：同一目标、实际采集的名称、镜像、端口与环境键必须相同；容器 CID 可变化。' : '明确允许稳定 systemd 服务授权：同一目标、单元 ID 与名称必须相同。'}</label> : <p>{o.kind === 'container' ? '候选缺少完整镜像、端口或环境键字段，只能精确实例授权；未采集的字段不会按空集合处理。' : '普通进程只允许精确 PID 与启动身份授权。'}</p>}
      <label>审批依据<input name="reason" required minLength={5} maxLength={2000}/></label>
      <label><input type="checkbox" checked={owned} onChange={event=>setOwned(event.target.checked)}/>我确认此对象属于当前业务，并授权只读检查。</label>
      <button className="btn small" disabled={busy||!owned||!o.observation_id}>确认此对象授权</button>
    </form>
  </details>;
}
