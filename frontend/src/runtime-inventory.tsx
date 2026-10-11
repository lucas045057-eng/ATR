import React, { useState } from "react";
import { Row } from "./api";

const classification: Row = {unmatched: "未匹配", unrelated: "人工判定不相关", insufficient_evidence: "缺少业务身份依据", confirmed: "已确认"};

export function RuntimeInventory({discovery}: {discovery: Row}) {
  const [page, setPage] = useState(0);
  const objects: Row[] = discovery.runtime_objects || [];
  const groups: Row[] = discovery.process_groups || [];
  const pages = Math.max(1, Math.ceil(groups.length / 20));
  const current = Math.min(page, pages - 1);
  return <div className="business-block">
    <h3>发现的运行对象</h3>
    {["service", "container"].map(kind => <details key={kind} open>
      <summary>{kind === "service" ? "systemd 服务" : "Docker 容器"} · {objects.filter(o => o.kind === kind).length} 项</summary>
      {objects.filter(o => o.kind === kind && o.classification !== "unmatched" && o.classification !== "unrelated").map(o =>
        <div className="business-row" key={o.id}><strong>{o.name}</strong><small>{o.state} · {classification[o.classification]} · 依据 {o.evidence_id}</small></div>)}
      <details><summary>未匹配或人工判定不相关（默认折叠）</summary>
        {objects.filter(o => o.kind === kind && ["unmatched", "unrelated"].includes(o.classification)).map(o =>
          <div className="business-row" key={o.id}><strong>{o.name}</strong><small>{o.state} · {classification[o.classification]} · 依据 {o.evidence_id}</small></div>)}
      </details>
    </details>)}
    <details><summary>进程线索 · {groups.reduce((n, g) => n + g.count, 0)} 个 PID / {groups.length} 组（默认折叠）</summary>
      <p className="muted">PID 只标识进程，不等于业务服务。同名进程按状态聚合，底层观测保留。这里只展示已采集页；清单不完整会单独提示。</p>
      {groups.slice(current * 20, (current + 1) * 20).map(g => <details key={g.name + ":" + g.state}>
        <summary>{g.name} · 状态 {g.state} · {g.count} 个进程</summary>
        {g.processes.map((p: Row) => <p key={p.pid}>PID {p.pid} · {classification[p.classification]} · 依据 {p.evidence_id}</p>)}
      </details>)}
      <button className="btn small" disabled={current === 0} onClick={() => setPage(current - 1)}>上一页</button>
      <span> 第 {current + 1}/{pages} 页 </span>
      <button className="btn small" disabled={current === pages - 1} onClick={() => setPage(current + 1)}>下一页</button>
    </details>
    {!objects.length && <details><summary>历史发现对象（原记录）</summary>{discovery.unassigned_runtime_objects?.join("、") || "无"}</details>}
  </div>;
}
