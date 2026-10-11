import React, { useState } from "react";
import { Row } from "./api";

function optionalImage(r: Row) {
  if (r.media_type !== "image" || /architecture|topology|diagram|deployment|架构|拓扑|部署/i.test(r.path)) return false;
  return r.optional_suggested || /(?:^|\/)(?:favicon(?:-\d+x\d+)?|icon|app-icon|apple-touch-icon|logo|decorative(?:-[a-z0-9_-]+)?|icon-[a-z0-9_-]+)\.[^.]+$/i.test(r.path)
    || /(?:^|\/)(?:icons|favicons|decorations)\//i.test(r.path);
}

export function MediaReviewList({profile, busy, onBatch, onManual}: {
  profile: Row; busy: boolean; onBatch: (ids: string[], action: string) => void; onManual: (id: string) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const files: Row[] = profile.source_references;
  const icons = files.filter(r => optionalImage(r) && r.parse_state !== "optional_static_attachment");
  const optional = files.filter(r => r.parse_state === "optional_static_attachment");
  const labels: Row = {pending_manual: "待人工录入", complete: "已解析", optional_static_attachment: "可选静态附件"};
  const pages = Math.max(1, Math.ceil(files.length / 50));
  return <details><summary>查看文件清单、解析状态和摘要 · {files.length} 个文件</summary>
    <p>可选静态附件 {optional.length} 个 · 明确图标待分类 {icons.length} 个。文件、哈希和来源保留；附件分类不会确认业务事实。</p>
    <button className="btn small" disabled={busy || !icons.length} onClick={() => setSelected(icons.slice(0, 200).map(r => r.id))}>勾选明确图标（最多 200 个）</button>
    <button className="btn small" disabled={busy || !selected.length} onClick={() => onBatch(selected, "optional")}>批量设为可选附件</button>
    <button className="btn small" disabled={busy || !optional.length} onClick={() => onBatch(optional.slice(0, 200).map(r => r.id), "restore")}>撤销静态附件分类（最多 200 个）</button>
    {files.slice(page * 50, (page + 1) * 50).map(r => <div className="business-row" key={r.id}>
      {optionalImage(r) && r.parse_state !== "optional_static_attachment" && <input type="checkbox" aria-label={`选择附件 ${r.path}`} checked={selected.includes(r.id)}
        onChange={e => setSelected(e.target.checked ? [...selected, r.id] : selected.filter(id => id !== r.id))} />}
      <span>{r.path}</span><small>{labels[r.parse_state] || r.parse_state} · {r.content_digest.slice(0,12)} · v{profile.profile_version}</small>
      {["image", "pdf"].includes(r.media_type) && <button className="btn small" disabled={busy} onClick={() => onManual(r.id)}>人工录入</button>}
    </div>)}
    <button className="btn small" disabled={!page} onClick={() => setPage(page - 1)}>上一页</button>
    <span> 第 {page + 1}/{pages} 页 </span>
    <button className="btn small" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>下一页</button>
  </details>;
}
