import React, { useCallback, useEffect, useRef, useState } from "react";
import { api, Row } from "./api";
import { RuntimeInventory } from "./runtime-inventory";
import { MediaReviewList } from "./media-review-list";
import { EndpointMappings } from "./endpoint-mappings";

const labels: Row = {
  Static: "静态解析",
  Documented: "资料描述",
  Runtime: "运行观测",
  Inferred: "模型推断",
  "Human Verified": "人工核验",
  pending: "待审核",
  confirmed: "已确认",
  rejected: "已否决",
  stale: "已失效",
  approved: "已审核",
  complete: "已解析",
  pending_manual: "待人工录入",
  optional_static_attachment: "可选静态附件",
  completed: "已完成",
  pending_model_configuration: "模型待配置",
};
const label = (s: string) => s.startsWith("model_error:") || s.startsWith("pending_model_error:") ? "模型调用失败" : s.startsWith("budget_exceeded:") ? "模型预算已耗尽" : labels[s] || s;
const mark = (s: string) => <span className={`badge ${s}`}>{label(s)}</span>;

export function BusinessPages({
  page,
  onSystemSelected,
}: {
  page: "onboarding" | "business" | "scope";
  onSystemSelected: (systemId: string) => void;
}) {
  const [businesses, setBusinesses] = useState<Row[]>([]);
  const [selected, setSelected] = useState(
    sessionStorage.getItem("atr_business_id") || "",
  );
  const [profile, setProfile] = useState<Row | null>(null);
  const [artifacts, setArtifacts] = useState<Row[]>([]);
  const [budgets, setBudgets] = useState<Row[]>([]);
  const [versions, setVersions] = useState<Row[]>([]);
  const [targets, setTargets] = useState<Row[]>([]);
  const [bindings, setBindings] = useState<Row[]>([]);
  const [discoveries, setDiscoveries] = useState<Row[]>([]);
  const [model, setModel] = useState<Row>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<Row | null>(null);
  const [code, setCode] = useState<Row | null>(null);
  const [search, setSearch] = useState<Row[]>([]);
  const [comparison, setComparison] = useState<Row | null>(null);
  const [connector, setConnector] = useState("mock");
  const [authMode, setAuthMode] = useState("private_key");
  const [executionMode, setExecutionMode] = useState("forced_gateway");
  const [chosenTarget, setChosenTarget] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const base = `/businesses/${selected}`;

  useEffect(() => {
    if (profile?.system_id) onSystemSelected(profile.system_id);
  }, [profile?.system_id, onSystemSelected]);

  const reload = useCallback(async () => {
    const [bs, cap] = await Promise.all([
      api("/businesses"),
      api("/model/capabilities"),
    ]);
    setBusinesses(bs);
    setModel(cap);
    if (!selected) {
      if (bs.length) setSelected(bs[0].id);
      return;
    }
    const [p, a, b, v, t, s, d] = await Promise.all([
      api(base),
      api(base + "/artifacts"),
      api(base + "/budgets"),
      api(base + "/versions"),
      api(base + "/targets"),
      api(base + "/bindings"),
      api(base + "/discoveries"),
    ]);
    setProfile(p);
    setArtifacts(a);
    setBudgets(b);
    setVersions(v);
    setTargets(t);
    setBindings(s);
    setDiscoveries(d);
    if (t.length && !chosenTarget) setChosenTarget(t[0].id);
  }, [selected, base, chosenTarget]);

  useEffect(() => {
    setProfile(null);
    setCode(null);
    setSearch([]);
    setComparison(null);
    sessionStorage.setItem("atr_business_id", selected);
    void reload().catch((e) => setError(e.message));
  }, [selected]);
  useEffect(() => {
    void reload().catch((e) => setError(e.message));
  }, [reload]);
  useEffect(() => {
    if (page !== "scope" || !targets.some((t) => t.auth_mode === "password")) return;
    const timer = setInterval(() => void reload().catch((e) => setError(e.message)), 15000);
    return () => clearInterval(timer);
  }, [page, targets, reload]);
  useEffect(() => {
    const saved = sessionStorage.getItem('atr_active_business_job');
    try {
      const state = saved ? JSON.parse(saved) : null;
      setJob(state?.business_id === selected ? {job_id: state.job_id} : null);
    } catch { sessionStorage.removeItem('atr_active_business_job'); }
  }, [selected]);
  useEffect(() => {
    if (!job?.job_id) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const state = await api(`/jobs/${job.job_id}`);
        if (cancelled) return;
        setJob({ ...state, job_id: state.id });
        setError("");
        if (["queued", "running"].includes(state.status))
          timer = setTimeout(poll, 1500);
        else {
          sessionStorage.removeItem('atr_active_business_job');
          setBusy(false);
          if (state.status !== "completed")
            setError(state.error || "任务未完成");
          await reload();
        }
      } catch (e) {
        if (!cancelled) {
          setError(`连接中断，任务状态暂时未知；恢复后继续读取同一任务。${(e as Error).message}`);
          timer = setTimeout(poll, 1500);
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [job?.job_id, reload]);

  const act = async (fn: () => Promise<any>) => {
    setBusy(true);
    setError("");
    try {
      const r = await fn();
      if (r?.job_id) {
        sessionStorage.setItem('atr_active_business_job', JSON.stringify({business_id: selected, job_id: r.job_id}));
        setJob(r);
      }
      else {
        await reload();
        setBusy(false);
      }
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  const review = async (
    subject_id = "profile",
    decision = "approve",
    role?: string,
  ) => {
    const reason = window.prompt("请填写核验依据或否决原因（至少 5 个字）");
    if (!reason || !profile) return;
    await act(() =>
      api(base + "/review", {
        method: "POST",
        body: JSON.stringify({
          expected_version: profile.profile_version,
          subject_id,
          decision,
          reason,
          role,
        }),
      }),
    );
  };
  const initial = budgets.find((b) => b.phase === "initial");
  const latestDiscovery = discoveries.find((d) => d.target_id === chosenTarget);
  const target = targets.find((t) => t.id === chosenTarget);
  const confirmed = bindings.filter(
    (b) => b.target_id === chosenTarget && b.status === "confirmed",
  );

  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">ATR V2.1</span>
          <h1>
            {page === "onboarding"
              ? "导入业务资料"
              : page === "business"
                ? "业务概览"
                : "服务器与服务"}
          </h1>
          <p>资料先建立骨架，运行事实通过受限读取与人工审核核验。</p>
        </div>
        <select
          aria-label="选择业务"
          className="business-select"
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value);
            setChosenTarget("");
          }}
        >
          <option value="">选择业务</option>
          {businesses.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}
            </option>
          ))}
        </select>
      </div>
      {error && (
        <div className="alert error" role="alert">
          {error}
          <button onClick={() => setError("")}>×</button>
        </div>
      )}
      {job && (
        <div className={`alert ${job.status === "failed" ? "error" : "success"}`}>
          <span>后台任务：{job.status}</span>
          {job.result?.files != null && (
            <span>
              已处理 {job.result.files} 文件，复用{" "}
              {job.result.parse_results_reused} 项解析
            </span>
          )}
          {busy && <span>正在处理…</span>}
        </div>
      )}
      {page === "onboarding" && (
        <>
          <ol className="wizard-steps">
            {[
              "业务名称",
              "资料导入",
              "静态/模型分析",
              "画像审核",
              "运行环境关联",
              "保存业务概览",
            ].map((x, i) => (
              <li key={x}>
                <b>{i + 1}</b>
                {x}
              </li>
            ))}
          </ol>
          <div className="two-column">
            <section className="panel">
              <h2>创建业务概览</h2>
              <form
                className="form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  void act(async () => {
                    const p = await api("/businesses", {
                      method: "POST",
                      body: JSON.stringify({
                        name: f.get("name"),
                        description: f.get("description"),
                      }),
                    });
                    setSelected(p.business_id);
                    return p;
                  });
                }}
              >
                <label>
                  业务名称
                  <input name="name" required maxLength={120} />
                </label>
                <label>
                  功能描述
                  <textarea name="description" rows={3} maxLength={2000} />
                </label>
                <p className="muted">
                  可以先保存空档案，覆盖状态会标记为资料不足。
                </p>
                <button className="btn primary" disabled={busy}>
                  创建并保存
                </button>
              </form>
            </section>
            <section className="panel">
              <h2>导入业务资料</h2>
              <form
                className="form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  const file = fileInput.current?.files?.[0];
                  if (!file) return;
                  void act(() =>
                    api(
                      base +
                        `/artifacts?filename=${encodeURIComponent(file.name)}&mode=${f.get("mode")}&slot=${encodeURIComponent(String(f.get("slot")))}`,
                      { method: "POST", body: file },
                    ),
                  );
                }}
              >
                <label>
                  源码压缩包、配置或文档
                  <input
                    aria-label="业务资料文件"
                    ref={fileInput}
                    type="file"
                    required
                    accept=".zip,.tar,.gz,.tgz,.py,.js,.ts,.tsx,.yaml,.yml,.json,.toml,.md,.txt,.conf,.png,.jpg,.jpeg,.pdf"
                  />
                </label>
                <label>
                  资料集名称
                  <input
                    name="slot"
                    defaultValue="source"
                    pattern="[A-Za-z0-9_.-]+"
                    maxLength={80}
                  />
                </label>
                <label>
                  导入方式
                  <select name="mode">
                    <option value="append">追加资料</option>
                    <option value="replace">
                      更新此资料集（删除的文件标记失效）
                    </option>
                  </select>
                </label>
                <p className="muted">
                  默认上传 ≤50MB，解包后 ≤200MB、≤2000
                  个文件。静态解析不会执行源码。文件直接上传后端。
                </p>
                <button className="btn primary" disabled={busy || !selected}>
                  上传并建立索引
                </button>
              </form>
            </section>
          </div>
          {profile && (
            <section className="panel business-block">
              <h2>分析与审核</h2>
              <p>
                画像 v{profile.profile_version} · {mark(profile.review_status)}{" "}
                · {mark(profile.model_status)}
              </p>
              {profile.model_error && <p role="alert" className="alert error">
                {profile.model_error.message} · 错误类型：{profile.model_error.exception_type}
              </p>}
              <p className="muted">
                模型：{model.model || "未配置"} · 外部分析：
                {model.external_analysis_enabled ? "已启用" : "未启用"} ·
                视觉：{model.vision_verified?'真实检测通过':model.vision_declared?'已声明，尚未检测':'尚未检测'} ·
                模型理解会使用资料摘要和安全页图，可疑秘密所在素材暂停外发。
              </p>
              <button
                className="btn"
                disabled={busy}
                onClick={() =>
                  void act(() => api(base + "/analyze", { method: "POST" }))
                }
              >
                按预算进行模型理解
              </button>{" "}
              <button className="btn" disabled={busy||!model.configured||!model.external_analysis_enabled} onClick={()=>act(()=>api('/model/probe-vision',{method:'POST'}))}>检测当前模型的视觉能力</button>
              <p>沿用当前模型服务发送一张生成的测试卡，最多等待 45 秒。检测通过后自动用于资料图片理解；密钥不进入资料内容。</p>
              <button
                className="btn primary"
                disabled={busy}
                onClick={() => void review()}
              >
                审核并保存档案
              </button>
              <p>
                服务 {profile.services.length} · 模块 {profile.modules.length} ·
                文件 {profile.coverage.files} · 待处理{" "}
                {profile.coverage.pending_files}
              </p>
              <ul className="unknown-list">
                {profile.unknowns.slice(0, 8).map((s: string) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </section>
          )}
          <section className="panel business-block">
            <h2>资料来源与处理状态</h2>
            {artifacts.length === 0 ? (
              <p className="muted">尚未导入资料</p>
            ) : (
              artifacts.map((a) => (
                <div className="business-row" key={a.id}>
                  <div>
                    <strong>{a.filename}</strong>
                    <small>
                      {(a.bytes / 1024).toFixed(1)} KB · {a.slot} ·{" "}
                      {a.content_digest.slice(0, 12)}
                    </small>
                    {a.error && <p role="alert">{a.error}</p>}
                  </div>
                  {mark(a.status)}
                  {a.status==='failed'&&<button className="btn small" disabled={busy} onClick={()=>void act(()=>api(base+`/artifacts/${a.id}/retry`,{method:'POST'}))}>从原资料断点恢复</button>}
                  <button
                    className="btn small"
                    disabled={busy || a.status === "removed"}
                    onClick={() =>
                      void act(() =>
                        api(base + `/artifacts/${a.id}`, { method: "DELETE" }),
                      )
                    }
                  >
                    移除资料（保留历史）
                  </button>
                </div>
              ))
            )}
          </section>
        </>
      )}
      {page === "business" && profile && (
        <>
          <div className="metric-strip">
            <Stat
              name="认知覆盖"
              value={
                profile.coverage.knowledge_level === "insufficient"
                  ? "资料不足"
                  : "初步骨架"
              }
            />
            <Stat
              name="静态文件"
              value={`${profile.coverage.static_parsed}/${profile.coverage.files}`}
            />
            <Stat
              name="运行验证"
              value={
                profile.coverage.runtime_verified ? "已采样核验" : "待采样核验"
              }
            />
            <Stat name="画像版本" value={`v${profile.profile_version}`} />
          </div>
          <section className="panel">
            <div className="panel-title">
              <div>
                <h2>{profile.name}</h2>
                <p>{profile.description}</p>
              </div>
              {mark(profile.review_status)}
            </div>
            <div className="chip-row">
              {profile.technology_stack.slice(0, 30).map((s: string) => (
                <span className="chip" key={s}>
                  {s}
                </span>
              ))}
            </div>
            <div className="business-actions">
              <button
                className="btn primary"
                disabled={busy}
                onClick={() => void review()}
              >
                确认档案
              </button>
              <button
                className="btn danger-outline"
                disabled={busy}
                onClick={() => void review("profile", "reject")}
              >
                否决档案
              </button>
              <select
                aria-label="对比历史版本"
                defaultValue=""
                onChange={async (e) => {
                  if (e.target.value)
                    setComparison(
                      await api(base + `?version=${e.target.value}`),
                    );
                }}
              >
                <option value="">对比历史版本</option>
                {versions.map((v) => (
                  <option key={v.version} value={v.version}>
                    v{v.version}
                  </option>
                ))}
              </select>
            </div>
            {comparison && (
              <p>
                对比 v{comparison.profile_version} → v{profile.profile_version}
                ：新增{" "}
                {
                  profile.source_references.filter(
                    (r: Row) =>
                      !comparison.source_references.some(
                        (p: Row) => p.path === r.path,
                      ),
                  ).length
                }
                ，变化{" "}
                {
                  profile.source_references.filter((r: Row) =>
                    comparison.source_references.some(
                      (p: Row) =>
                        p.path === r.path &&
                        p.content_digest !== r.content_digest,
                    ),
                  ).length
                }
                ，失效{" "}
                {
                  comparison.source_references.filter(
                    (p: Row) =>
                      !profile.source_references.some(
                        (r: Row) => r.path === p.path,
                      ),
                  ).length
                }
                。
              </p>
            )}
          </section>
          <div className="two-column business-block">
            <section className="panel">
              <h2>服务与角色</h2>
              {profile.services.map((s: Row) => (
                <div className="business-row" key={s.name}>
                  <div>
                    <strong>{s.name}</strong>
                    <small>
                      {s.image || "源码部署"} · {label(s.source_type)}
                    </small>
                  </div>
                  <select
                    aria-label={`${s.name}角色`}
                    value={s.role}
                    onChange={(e) =>
                      void review(
                        `service:${s.name}`,
                        "set_role",
                        e.target.value,
                      )
                    }
                  >
                    {[
                      "application",
                      "proxy",
                      "datastore",
                      "cache",
                      "worker",
                      "unknown",
                    ].map((r) => (
                      <option key={r}>{r}</option>
                    ))}
                  </select>
                </div>
              ))}
              <h3>服务与模块关系</h3>
              <DependencyGraph profile={profile} />
            </section>
            <section className="panel">
              <h2>关键接口与观测入口</h2>
              {profile.interfaces.slice(0, 40).map((i: Row, n: number) => (
                <div className="business-row" key={n}>
                  <code>
                    {i.method || "PORT"} {i.path || i.port}
                  </code>
                  <small>{i.module || i.service}</small>
                </div>
              ))}
              <h3>未知与冲突</h3>
              <ul className="unknown-list">
                {profile.unknowns.slice(0, 12).map((s: string) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
              {profile.conflicts.map((c: Row) => (
                <div key={c.id} className="conflict-row">
                  <div>
                    <strong>
                      {c.predicate} · {c.status}
                    </strong>
                    <small>{c.verification}</small>
                    <small>事实：{c.fact_ids.join(" · ")}</small>
                  </div>
                  {c.status === "open" && (
                    <button
                      className="btn small"
                      onClick={() => void review(c.id, "resolve_conflict")}
                    >
                      人工解释冲突
                    </button>
                  )}
                </div>
              ))}
            </section>
          </div>
          <section className="panel business-block">
            <h2>源码索引与按需读取</h2>
            <form
              className="business-actions"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                void act(async () => {
                  setSearch(
                    await api(base + "/code/search", {
                      method: "POST",
                      body: JSON.stringify({
                        keyword: f.get("keyword"),
                        limit: 20,
                      }),
                    }),
                  );
                });
              }}
            >
              <input
                aria-label="源码检索关键词"
                name="keyword"
                required
                placeholder="模块、函数或 API 路径"
              />
              <button className="btn" disabled={busy}>
                检索
              </button>
            </form>
            {search.map((r) => (
              <div className="business-row" key={r.file_id}>
                <div>
                  <strong>{r.path}</strong>
                  <small>
                    {r.content_digest.slice(0, 12)} ·{" "}
                    {(r.symbols || [])
                      .map((s: Row) => s.name)
                      .slice(0, 6)
                      .join(" · ")}
                  </small>
                </div>
                <button
                  className="btn small"
                  onClick={() =>
                    void act(async () => {
                      setCode(
                        await api(base + "/code/context", {
                          method: "POST",
                          body: JSON.stringify({
                            file_id: r.file_id,
                            max_lines: 60,
                          }),
                        }),
                      );
                    })
                  }
                >
                  读取局部源码
                </button>
              </div>
            ))}
            {code && (
              <>
                <p>
                  {code.path} · v{code.profile_version} ·{" "}
                  {code.source_digest.slice(0, 12)} · 行 {code.start_line}–
                  {code.end_line} · {code.bytes} bytes
                </p>
                <pre className="code-preview">{code.text}</pre>
              </>
            )}
            <MediaReviewList key={`${selected}:${profile.profile_version}`} profile={profile} busy={busy}
              onContinue={id=>void act(()=>api(base+`/files/${id}/media-continue`,{method:'POST',body:JSON.stringify({expected_version:profile.profile_version})}))}
              onBatch={(ids, action) => void act(() => api(base + '/files/media-batch-review', {
                method: 'POST', body: JSON.stringify({file_ids: ids, action, expected_version: profile.profile_version,
                  reason: action === 'optional' ? '人工批量分类明确图标或装饰资源' : '人工撤销静态附件分类，恢复原审核状态'}),
              }))}
              onManual={(id) => { const text = window.prompt('人工录入该资料的业务结构摘要'); if (text) void act(() => api(base + `/files/${id}/media-review`, {
                method: 'POST', body: JSON.stringify({text, expected_version: profile.profile_version}),
              })); }} />
          </section>
          <section className="panel business-block">
            <h2>可追溯事实与人工审核</h2>
            {profile.facts.slice(0, 120).map((f: Row) => (
              <div className="business-row" key={f.id}>
                <div>
                  <strong>
                    {f.subject} → {f.predicate}
                  </strong>
                  <small>
                    {typeof f.object === "string"
                      ? f.object
                      : JSON.stringify(f.object).slice(0, 180)}
                  </small>
                  <small>
                    {label(f.source_type)} · {f.source_locator} ·{" "}
                    {f.source_digest.slice(0, 12)}
                  </small>
                </div>
                {mark(f.verification_status)}
                {f.verification_status === "pending" && (
                  <>
                    <button
                      className="btn small"
                      onClick={() => void review(f.id)}
                    >
                      确认
                    </button>
                    <button
                      className="btn small danger-outline"
                      onClick={() => void review(f.id, "reject")}
                    >
                      否决
                    </button>
                  </>
                )}
              </div>
            ))}
          </section>
        </>
      )}
      {initial && page !== "scope" && (
        <section className="panel business-block">
          <h2>上下文预算与模型消耗</h2>
          <p>
            输入 {initial.input_tokens} · 输出 {initial.output_tokens} · 调用{" "}
            {initial.calls}/{initial.limits.max_calls} · 检索{" "}
            {initial.retrieved_bytes} bytes{" "}
            {initial.stop_reason && `· 停止：${initial.stop_reason}`}
          </p>
          <form
            className="business-actions"
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              void act(() =>
                api(base + `/budgets/${initial.id}`, {
                  method: "PUT",
                  body: JSON.stringify({
                    ...initial.limits,
                    max_input_tokens: Number(f.get("inputs")),
                    max_calls: Number(f.get("calls")),
                    max_total_tokens: Number(f.get("total")),
                  }),
                }),
              );
            }}
          >
            <label>
              单次最大输入
              <input
                name="inputs"
                type="number"
                min="1"
                defaultValue={initial.limits.max_input_tokens}
              />
            </label>
            <label>
              模型调用上限
              <input
                name="calls"
                type="number"
                min="1"
                max="100"
                defaultValue={initial.limits.max_calls}
              />
            </label>
            <label>
              累计 Token 上限
              <input
                name="total"
                type="number"
                min="1"
                defaultValue={initial.limits.max_total_tokens}
              />
            </label>
            <button className="btn" disabled={busy}>
              保存预算
            </button>
          </form>
          {initial.usage_records.map((u: Row) => (
            <p key={u.id} className="muted">
              {u.model} · {u.input_tokens}+{u.output_tokens} Tokens ·{" "}
              {u.usage_kind === "actual" ? "供应商实际 usage" : "明确估算"} ·
              费用 {u.cost == null ? "未知" : `估算 $${u.cost.toFixed(6)}`}
            </p>
          ))}
        </section>
      )}
      {page === "scope" && (
        <>
          <div className="two-column">
            <section className="panel">
              <h2>关联 Linux 环境</h2>
              <form
                className="form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  const passwordInput = e.currentTarget.elements.namedItem("ssh_password") as HTMLInputElement | null;
                  let password = passwordInput?.value || "";
                  f.delete("ssh_password");
                  if (passwordInput) passwordInput.value = "";
                  void act(async () => {
                    try {
                    const t = await api(base + "/targets", {
                      method: "POST",
                      body: JSON.stringify({
                        name: f.get("name"),
                        connector,
                        host: connector === "mock" ? "mock" : f.get("host"),
                        port: Number(f.get("port") || 22),
                        username: f.get("username") || "atr-read",
                        auth_mode: connector === "ssh" ? authMode : "private_key",
                        execution_mode: executionMode,
                        timeout_seconds: Number(f.get("timeout_seconds") || 10),
                        max_calls: Number(f.get("max_calls") || 100),
                        max_output_bytes: Number(f.get("max_output_bytes") || 48000),
                        credential_ref: f.get("credential_ref") || "",
                        host_public_key: f.get("host_public_key") || "",
                        allowed_paths: String(f.get("paths") || "")
                          .split(/\r?\n/)
                          .map((x) => x.trim())
                          .filter(Boolean),
                        access_authorized: f.get("authorized") === "on",
                        permission_boundary:
                          connector === "mock"
                            ? "mock"
                            : executionMode === "standard_ssh_readonly" ? "client_readonly" : "forced_command_gateway",
                        boundary_attestation: String(
                          f.get("attestation") || "",
                        ),
                        docker_reads_enabled: f.get("docker") === "on",
                        docker_reader: connector === "ssh" && executionMode === "standard_ssh_readonly" ? (f.get("docker_reader") || "direct_cli") : "direct_cli",
                        mock_scenario: f.get("scenario") || "mixed",
                      }),
                    });
                    setChosenTarget(t.id);
                    if (connector === "ssh" && authMode === "password") {
                      return await api(base + `/targets/${t.id}/credential`, {
                        method: "POST", body: JSON.stringify({ password }),
                      });
                    }
                    return t;
                    } finally { password = ""; }
                  });
                }}
              >
                <label>
                  环境名称
                  <input name="name" required defaultValue="隔离测试环境" />
                </label>
                <label>
                  连接方式
                  <select
                    value={connector}
                    onChange={(e) => setConnector(e.target.value)}
                  >
                    <option value="mock">隔离 Mock（可离线验收）</option>
                    <option value="ssh">SSH（需要显式授权）</option>
                  </select>
                </label>
                {connector === "ssh" ? (
                  <>
                    <label>SSH 执行方式
                      <select value={executionMode} onChange={(e) => {
                        setExecutionMode(e.target.value);
                        const form = e.currentTarget.form;
                        const declaration = form?.elements.namedItem("attestation") as HTMLTextAreaElement | null;
                        const authorized = form?.elements.namedItem("authorized") as HTMLInputElement | null;
                        if (declaration) declaration.value = "";
                        if (authorized) authorized.checked = false;
                      }}>
                        <option value="standard_ssh_readonly">普通 SSH 只读兼容（无需安装网关）</option>
                        <option value="forced_gateway">SSH 安全网关（atr-read-v1）</option>
                      </select>
                    </label>
                    {executionMode === "standard_ssh_readonly" && <p className="alert">
                      只读限制由 ATR 固定工具和 SSH 账号权限控制，不等同于服务器端强制隔离。
                      允许 AI 调用已授权只读工具；必须使用已授权低权限账号。账号可能仍具有其他程序可使用的写权限。
                    </p>}
                    {executionMode === "standard_ssh_readonly" && <label>Docker 读取方式
                      <select name="docker_reader" defaultValue="restricted_list">
                        <option value="restricted_list">受限网关（atr-docker-read · 仅清单）</option>
                        <option value="direct_cli">已有账号权限读取 Docker CLI</option>
                      </select>
                    </label>}
                    <label>
                      地址
                      <input name="host" required />
                    </label>
                    <div className="split-input">
                      <label>
                        端口
                        <input name="port" type="number" defaultValue="22" />
                      </label>
                      <label>
                        受限用户名
                        <input name="username" defaultValue="atr-read" />
                      </label>
                    </div>
                    <label>
                      认证方式
                      <select value={authMode} onChange={(e) => setAuthMode(e.target.value)}>
                        <option value="private_key">用户名 + 私钥（原有方式）</option>
                        <option value="password">用户名 + 密码（短期会话）</option>
                      </select>
                    </label>
                    {authMode === "password" ? <label>
                      SSH 密码
                      <input aria-label="SSH 密码" name="ssh_password" type="password" required maxLength={1024} autoComplete="off" />
                      <small>仅在后端内存保存 30 分钟；后端重启后需重新输入。</small>
                    </label> : <label>
                      凭据引用
                      <input
                        name="credential_ref"
                        required
                        placeholder="运维配置中的引用名称"
                      />
                    </label>}
                    <label>
                      已核对的主机公钥
                      <textarea
                        name="host_public_key"
                        required
                        placeholder="ssh-ed25519 AAAA…（公钥）"
                      />
                    </label>
                    <label>
                      权限边界确认
                      <textarea
                        name="attestation"
                        required
                        minLength={10}
                        placeholder={executionMode === "standard_ssh_readonly"
                          ? "确认低权限账号、主机和资料授权范围，并理解客户端只读限制及 AI 调查权限"
                          : "确认受限账号、强制只读入口和禁用转发已由服务器所有者配置"}
                      />
                    </label>
                    <label className="check-label">
                      <input name="authorized" type="checkbox" required />{" "}
                      已授权访问这台测试服务器
                    </label>
                    <div className="split-input">
                      <label>单次超时（秒）<input name="timeout_seconds" type="number" min={1} max={60} defaultValue={10} required /></label>
                      <label>最大调用次数<input name="max_calls" type="number" min={1} max={1000} defaultValue={100} required /></label>
                    </div>
                    <label>输出大小上限（字节）<input name="max_output_bytes" type="number" min={1024} max={64000} defaultValue={48000} required /></label>
                    <small>允许 ATR 已注册的只读工具；服务、资料目录和容器仍受授权范围限制，Docker 只读默认勾选，可关闭。</small>
                  </>
                ) : (
                  <label>
                    Mock 场景
                    <select name="scenario">
                      <option value="mixed">混合服务受控故障</option>
                      <option value="datastore_conflict">
                        资料 MySQL / 运行 PostgreSQL 冲突
                      </option>
                      <option value="permission_denied">权限不足</option>
                      <option value="timeout">执行超时</option>
                      <option value="disconnect">网络断开</option>
                    </select>
                  </label>
                )}
                <label>
                  授权业务目录（每行一个）
                  <textarea name="paths" rows={2} placeholder="/srv/catalog" />
                </label>
                <label className="check-label">
                  <input name="docker" type="checkbox" defaultChecked /> 已授权受限的 Docker
                  元数据读取入口
                </label>
                <button className="btn primary" disabled={busy || !selected}>
                  保存环境关联
                </button>
              </form>
            </section>
            <section className="panel">
              <h2>轻量发现与范围审核</h2>
              <select
                aria-label="运行环境"
                className="business-select"
                value={chosenTarget}
                onChange={(e) => setChosenTarget(e.target.value)}
              >
                <option value="">选择环境</option>
                {targets.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name} · {t.connector}
                  </option>
                ))}
              </select>
              {target && (
                <p>
                  {target.host} ·{" "}
                  {target.connector === "mock"
                    ? "明确的隔离 Mock 数据"
                    : target.execution_mode === "standard_ssh_readonly"
                      ? "普通 SSH · 客户端只读约束 · AI 可调用已授权工具"
                      : "SSH 固定只读网关"}{" "}
                  · 最大调用 {target.max_calls}
                </p>
              )}
              {target?.execution_mode === "standard_ssh_readonly" && <p className="alert">
                操作员已声明授权；每次采集检查账号身份。服务器端强制只读隔离：未验证。
                更换执行方式需创建并重新授权新目标，原有服务绑定不会自动沿用。
              </p>}
              {target?.execution_mode === "standard_ssh_readonly" && <label>当前目标 Docker 读取方式
                <select aria-label="当前 Docker 读取方式" value={target.docker_reader || "direct_cli"} disabled={busy}
                  onChange={(e) => { const reader = e.target.value; void act(() => api(base + `/targets/${chosenTarget}/docker-reader`, {
                    method: "PATCH", body: JSON.stringify({docker_reader: reader}),
                  })); }}>
                  <option value="direct_cli">已有账号权限读取 Docker CLI</option>
                  <option value="restricted_list">受限网关（仅清单，无状态和日志能力）</option>
                </select>
                <small>切换后容器映射需重新发现和审核；不会更改服务器权限。</small>
              </label>}
              {target?.auth_mode === "password" && <>
                <p role="status">
                  {target.credential_status?.state === "ready"
                    ? `密码会话有效至 ${new Date(target.credential_status.expires_at).toLocaleString()}`
                    : "密码会话未提供、已过期或已清除；后端重启后需重新输入。"}
                </p>
                <form className="form" onSubmit={(e) => {
                  e.preventDefault();
                  const input = e.currentTarget.elements.namedItem("refresh_password") as HTMLInputElement;
                  let password = input.value;
                  input.value = "";
                  void act(async () => {
                    try { return await api(base + `/targets/${chosenTarget}/credential`, {
                      method: "POST", body: JSON.stringify({ password }),
                    }); } finally { password = ""; }
                  });
                }}>
                  <label>重新提供 SSH 密码
                    <input name="refresh_password" type="password" required maxLength={1024} autoComplete="off" />
                  </label>
                  <button className="btn" disabled={busy}>更新密码会话</button>
                </form>
                <button className="btn danger-outline" disabled={busy} onClick={() => void act(() =>
                  api(base + `/targets/${chosenTarget}/credential`, { method: "DELETE" }))}>清除密码会话</button>
              </>}
              <button
                className="btn primary"
                disabled={busy || !chosenTarget || (target?.auth_mode === "password" && target.credential_status?.state !== "ready")}
                onClick={() =>
                  void act(() =>
                    api(base + `/targets/${chosenTarget}/discover`, {
                      method: "POST",
                    }),
                  )
                }
              >
                定向轻量发现
              </button>
              <p className="muted">
                只采集进程名称、端口、服务和授权目录的单层清单。分数只用于排序，深入读取需要范围确认。
              </p>
              {bindings
                .filter(
                  (b) => b.target_id === chosenTarget && b.status !== "stale" && (b.kind !== "process" || /^\d+:[a-f0-9-]{36}:\d+$/.test(b.runtime_id)),
                )
                .map((b) => (
                  <div key={b.id} className="business-row">
                    <div>
                      <strong>
                        {b.service} → {b.runtime_id.slice(0, 32)}
                      </strong>
                      <small>
                        排序分 {b.score} · {b.kind}
                      </small>
                      <small>{b.reasons.join("；")}</small>
                      <small>证据 {b.evidence_ids.join(", ")}</small>
                    </div>
                    {mark(b.status)}
                    {b.status === "pending" && (
                      <div>
                        <button
                          className="btn small"
                          disabled={busy}
                          onClick={() => {
                            const reason = window.prompt("确认服务范围的依据");
                            if (reason)
                              void act(() =>
                                api(base + `/bindings/${b.id}/review`, {
                                  method: "POST",
                                  body: JSON.stringify({
                                    decision: "confirm",
                                    reason,
                                  }),
                                }),
                              );
                          }}
                        >
                          确认
                        </button>
                        <button
                          className="btn small danger-outline"
                          disabled={busy}
                          onClick={() =>
                            void act(() =>
                              api(base + `/bindings/${b.id}/review`, {
                                method: "POST",
                                body: JSON.stringify({
                                  decision: "reject",
                                  reason: "人工判断不属于当前业务范围",
                                }),
                              }),
                            )
                          }
                        >
                          拒绝
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              {latestDiscovery && (
                <>
                  <p>
                    未匹配业务服务：
                    {latestDiscovery.unmatched_services?.join("、") || "无"}
                  </p>
                  <RuntimeInventory key={latestDiscovery.id} discovery={latestDiscovery} />
                  {latestDiscovery.incomplete_tools?.map((r: Row) => <p className="alert" key={r.observation_id}>
                    {r.tool_id} · 数据不完整 · {r.message} · {r.failure_stage} · 输出 {r.output_bytes} 字节 · 异常 {r.invalid_item_count} 项
                  </p>)}
                  {latestDiscovery.errors?.map((r: Row) => (
                    <p className="alert error" key={r.observation_id}>
                      {r.tool_id} · {label(r.status)} · {r.error_code || r.error} · 阶段 {r.failure_stage || "历史记录未知"}
                      {r.message ? ` · ${r.message}` : ""}
                      {r.line_number != null ? ` · 行 ${r.line_number}` : ""}
                      {r.output_bytes != null ? ` · 输出 ${r.output_bytes} 字节 · 异常记录 ${r.invalid_item_count || 0}` : ""}
                    </p>
                  ))}
                </>
              )}
            </section>
          </div>
          {target && (
            <EndpointMappings key={selected+':'+chosenTarget} base={base} targetId={chosenTarget} profile={profile||{}} bindings={bindings} refreshKey={String(job?.ended_at||'')} onSaved={reload}/>
          )}
          {target && (
            <section className="panel business-block">
              {confirmed.some(b=>b.kind==='process') && <form className="form" onSubmit={event=>{
                event.preventDefault();const form=new FormData(event.currentTarget);
                const binding=confirmed.find(b=>b.id===form.get('process_binding') && b.kind==='process');
                if(!binding)return;
                const [pid,...start]=binding.runtime_id.split(':');
                const source={file_id:String(form.get('log_id')),path:String(form.get('log_path')),service:binding.service,pid:Number(pid),start_time:start.join(':')};
                void act(()=>api(base+`/targets/${chosenTarget}/log-files`,{method:'PATCH',body:JSON.stringify({log_files:[...(target?.log_files||[]).filter((row:Row)=>row.file_id!==source.file_id),source]})}));
              }}>
                <h2>普通进程的固定日志</h2><p>选择已确认的启动身份，并填写授权业务目录内的日志文件。进程重启后需要重新确认。</p>
                <label>已确认的普通进程<select name="process_binding" required>{confirmed.filter(b=>b.kind==='process').map(b=><option key={b.id} value={b.id}>{b.service} · {b.runtime_id}</option>)}</select></label>
                <label>日志标识<input name="log_id" required maxLength={80} pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" /></label>
                <label>固定日志路径<input name="log_path" required maxLength={240} placeholder="授权业务目录内的完整路径" /></label>
                <button className="btn" disabled={busy}>保存固定日志</button>
              </form>}
              <h2>授权范围内追加读取</h2>
              <form
                className="business-actions"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  const tool = String(f.get("tool"));
                  const b = confirmed.find((x) => x.id === f.get("binding"));
                  const args: Row = tool.startsWith("config.")
                    ? { path: f.get("path") }
                    : ["service.status", "service.logs"].includes(tool)
                      ? { name: b?.runtime_id }
                      : ["container.status", "container.logs"].includes(tool)
                        ? { container_id: b?.runtime_id }
                        : tool === 'process.status' && b?.kind === 'process'
                          ? {pid:Number(b.runtime_id.split(':')[0]),start_time:b.runtime_id.split(':').slice(1).join(':')}
                          : tool === 'log.tail' ? {file_id:f.get('log_file')}
                          : tool === 'health.get' ? {endpoint_id:f.get('health_endpoint')} : {};
                  void act(() =>
                    api(base + `/targets/${chosenTarget}/tools`, {
                      method: "POST",
                      body: JSON.stringify({ tool_id: tool, arguments: args, read_protocol:'ab_v1' }),
                    }),
                  );
                }}
              >
                <select name="tool" aria-label="只读工具">
                  {[
                    "host.info",
                    "host.memory",
                    "host.disk",
                    "process.list",
                    "process.status",
                    "log.tail",
                    "health.get",
                    "network.listeners",
                    "service.list",
                    "container.list",
                    "config.list",
                    "service.status",
                    "service.logs",
                    "container.status",
                    "container.logs",
                    "config.read",
                  ].map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
                <select name="log_file" aria-label="固定日志标识"><option value="">选择固定日志</option>{(target?.log_files||[]).map((row:Row)=><option key={row.file_id} value={row.file_id}>{row.service} · {row.file_id}</option>)}</select>
                <select name="health_endpoint" aria-label="已授权健康端点"><option value="">选择健康端点</option>{(target?.health_endpoints||[]).map((row:Row)=><option key={row.endpoint_id} value={row.endpoint_id}>{row.service} · {row.endpoint_id}</option>)}</select>
                <select name="binding" aria-label="已确认服务">
                  <option value="">已确认服务</option>
                  {confirmed.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.service} · {b.kind}
                    </option>
                  ))}
                </select>
                <input
                  name="path"
                  aria-label="授权配置路径"
                  placeholder="明确授权的配置文件路径"
                />
                <button className="btn" disabled={busy}>
                  执行固定只读工具
                </button>
              </form>
              {job?.result?.payload && (
                <><p>{job.result.payload.identity?.precheck === "passed" ? "账号身份预检查通过 · 操作员已声明授权 · 服务器端强制只读隔离未验证" : "已采集运行观测"}</p><pre className="code-preview">
                  {JSON.stringify(job.result, null, 2)}
                </pre></>
              )}
              {job?.result?.error && <p className="alert error">{job.result.message || `${job.result.tool_id} · ${job.result.status} · ${job.result.error}`}</p>}
            </section>
          )}
        </>
      )}
      {!selected && page !== "onboarding" && (
        <p className="muted">请先在“导入业务资料”创建档案。</p>
      )}
    </>
  );
}

function Stat({ name, value }: { name: string; value: any }) {
  return (
    <div className="metric-card">
      <div className="metric-top">{name}</div>
      <strong>{value}</strong>
    </div>
  );
}

function DependencyGraph({ profile }: { profile: Row }) {
  const relations = profile.dependencies.slice(0, 20);
  const names = Array.from(
    new Set<string>(relations.flatMap((r: Row) => [r.from, r.to])),
  ).slice(0, 16);
  if (!names.length) return <p className="muted">尚无可追溯的服务关系</p>;
  const width = 650,
    height = Math.max(150, Math.ceil(names.length / 2) * 78 + 20);
  const position = (name: string) => {
    const i = names.indexOf(name);
    return { x: i % 2 ? 465 : 170, y: 40 + Math.floor(i / 2) * 78 };
  };
  return (
    <svg
      className="dependency-graph"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="业务服务和模块依赖图"
    >
      <defs>
        <marker
          id="dependency-arrow"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M0 0L10 5L0 10z" fill="var(--accent)" />
        </marker>
      </defs>
      {relations
        .filter((r: Row) => names.includes(r.from) && names.includes(r.to))
        .map((r: Row, i: number) => {
          const a = position(r.from),
            b = position(r.to);
          return (
            <g key={i}>
              <line
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke="var(--accent)"
                strokeDasharray={r.certainty === "inferred" ? "5 4" : undefined}
                markerEnd="url(#dependency-arrow)"
              />
              <title>
                {r.relation} · {r.certainty} · 事实 {r.fact_id}
              </title>
            </g>
          );
        })}
      {names.map((n) => {
        const p = position(n);
        return (
          <g key={n}>
            <rect
              x={p.x - 135}
              y={p.y - 18}
              width="270"
              height="36"
              rx="6"
              fill="var(--panel2)"
              stroke="var(--accent-line)"
            />
            <text
              x={p.x}
              y={p.y + 4}
              textAnchor="middle"
              fill="var(--text)"
              fontSize="12"
            >
              {n.length > 34 ? n.slice(0, 32) + "…" : n}
            </text>
            <title>{n}</title>
          </g>
        );
      })}
    </svg>
  );
}
