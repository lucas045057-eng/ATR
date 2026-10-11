import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import { api } from './api'
import { BusinessPages } from './business'
import { ModelError, ModelAttempt } from './model-error'

type Row = Record<string, any>
type Page = 'systems' | 'investigation' | 'knowledge' | 'efficiency' | 'onboarding' | 'business' | 'scope'

const fmtDate = (value?: string) => value ? new Date(value).toLocaleString() : '—'
const badge = (value: string) => <span className={`badge ${value.toLowerCase().replaceAll('_', '-')}`}>{value}</span>

function App() {
  const [page, setPage] = useState<Page>('systems')
  const [systems, setSystems] = useState<Row[]>([])
  const [businesses, setBusinesses] = useState<Row[]>([])
  const [runtimeTargets, setRuntimeTargets] = useState<Row[]>([])
  const [targetBusinessId, setTargetBusinessId] = useState('')
  const [selectedTarget, setSelectedTarget] = useState('')
  const [incidents, setIncidents] = useState<Row[]>([])
  const [patterns, setPatterns] = useState<Row[]>([])
  const [knowledgeFailures, setKnowledgeFailures] = useState<Row[]>([])
  const [metrics, setMetrics] = useState<Row>({})
  const [evaluationCases, setEvaluationCases] = useState<Row[]>([])
  const [evaluationTrials, setEvaluationTrials] = useState<Row[]>([])
  const [runs, setRuns] = useState<Row[]>([])
  const [selectedSystem, setSelectedSystem] = useState('')
  const [selectedIncident, setSelectedIncident] = useState('')
  const [graph, setGraph] = useState<Row | null>(null)
  const [run, setRun] = useState<Row | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [runJob, setRunJob] = useState('')
  const [token, setToken] = useState(sessionStorage.getItem('atr_operator_token') || '')

  const refresh = useCallback(async () => {
    const [s, b, i, p, m, c, e, r, failures] = await Promise.all([api('/systems'), api('/businesses'), api('/incidents'), api('/patterns'), api('/metrics'), api('/evaluation-cases'), api('/evaluations'), api('/runs'), api('/knowledge-counterexamples')])
    setSystems(s); setIncidents(i); setPatterns(p); setMetrics(m); setEvaluationCases(c); setEvaluationTrials(e); setRuns(r)
    setBusinesses(b)
    setKnowledgeFailures(failures)
    if (!selectedSystem && s.length) setSelectedSystem(s[0].id)
  }, [selectedSystem])

  useEffect(() => { refresh().catch(e => setError(e.message)) }, [refresh])
  useEffect(() => { if(['systems','investigation','knowledge','efficiency'].includes(page)) void refresh().catch(e=>setError(e.message)) }, [page,refresh])
  useEffect(() => {
    if (!selectedSystem) { setGraph(null); return }
    api(`/systems/${selectedSystem}/graph`).then(setGraph).catch(() => setGraph(null))
  }, [selectedSystem])
  const currentBusiness = businesses.find(x => x.system_id === selectedSystem)
  useEffect(() => {
    if (page !== 'investigation' || !currentBusiness) {
      setRuntimeTargets([]); setTargetBusinessId(''); setSelectedTarget(''); return
    }
    let cancelled = false
    setTargetBusinessId('')
    api(`/businesses/${currentBusiness.id}/targets`).then((targets: Row[]) => {
      if (cancelled) return
      setRuntimeTargets(targets)
      setTargetBusinessId(currentBusiness.id)
      setSelectedTarget(previous => targets.some(t => t.id === previous && t.investigation_readiness?.eligible)
        ? previous
        : targets.length === 1 && targets.filter(t => t.investigation_readiness?.eligible).length === 1
          ? targets.find(t => t.investigation_readiness?.eligible)?.id || '' : '')
    }).catch(e => { if (!cancelled) { setRuntimeTargets([]); setTargetBusinessId(currentBusiness.id); setSelectedTarget(''); setError(e.message) } })
    return () => { cancelled = true }
  }, [currentBusiness?.id, page])
  useEffect(() => {
    if (!selectedIncident) { setRun(null); return }
    const incident = incidents.find(x => x.id === selectedIncident)
    if (!incident) return
    if (incident.latest_run_id) api(`/runs/${incident.latest_run_id}`).then(setRun).catch(() => setRun(null))
    else setRun(null)
  }, [selectedIncident, incidents])

  useEffect(() => {
    if (!runJob) return
    let stopped = false; let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const state = await api(`/jobs/${runJob}`)
        if (stopped) return
        const id = state.result?.run_id || state.result?.id
        if (id) setRun(await api(`/runs/${id}`))
        if (['queued','running'].includes(state.status)) timer = setTimeout(poll,1500)
        else {setRunJob(''); await refresh(); if(state.status!=='completed')setError(state.error || '调查被中断')}
      } catch(e) {if(!stopped){setError((e as Error).message);setRunJob('')}}
    }
    void poll()
    return () => {stopped=true;clearTimeout(timer)}
  }, [runJob,refresh])

  const act = async (fn: () => Promise<void>) => {
    setError(''); setNotice(''); setBusy(true)
    try { await fn(); await refresh() } catch (e) { setError(e instanceof Error ? e.message : '操作失败') }
    finally { setBusy(false) }
  }

  const saveToken = () => { sessionStorage.setItem('atr_operator_token', token); setError(''); setNotice('本次浏览器会话中已保存操作员令牌。'); void refresh().catch(e => setError(e.message)) }

  const onBoard = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const data = new FormData(event.currentTarget)
    void act(async () => {
      const result = await api('/systems/onboard', { method: 'POST', body: JSON.stringify({ source_key: data.get('source_key'), display_name: data.get('display_name'), compose_file: data.get('compose_file') || 'compose.yaml' }) })
      setSelectedSystem(result.system_id); setNotice(`系统已建模，图版本 v${result.version}。`)
    })
  }

  const createIncident = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form)
    void act(async () => {
      const occurred = String(data.get('window_start') || '')
      const created = await api('/incidents', { method: 'POST', body: JSON.stringify({ system_id: data.get('system_id'), symptom: data.get('symptom'), mode: data.get('mode'), window_start: occurred ? new Date(occurred).toISOString() : null }) })
      setSelectedIncident(created.id); setPage('investigation'); form.reset()
    })
  }

  const runIncident = (incidentId: string) => void act(async () => {
    const incident = incidents.find(x => x.id === incidentId)
    const business = businesses.find(x => x.system_id === incident?.system_id)
    if (business && (targetBusinessId !== business.id || !runtimeTargets.some(t => t.id === selectedTarget && t.investigation_readiness?.eligible)))
      throw new Error('请先为本次调查选择一个当前有效且已授权的 SSH 目标。')
    const result = await api(`/incidents/${incidentId}/runs`, { method: 'POST', body: JSON.stringify(business ? {target_id: selectedTarget} : {}) })
    setSelectedIncident(incidentId); setRunJob(result.job_id); setPage('investigation'); setNotice('只读调查已排入后台，可查看实际执行过程。')
    if (selectedSystem) setGraph(await api(`/systems/${selectedSystem}/graph`))
  })

  const reviewPattern = (patternId: string, decision: 'approve' | 'reject') => void act(async () => {
    const reason = window.prompt(decision === 'approve' ? '写下审核依据（包括复核过的适用范围）' : '写下拒绝原因', '')
    if (!reason) return
    await api(`/patterns/${patternId}/review`, { method: 'POST', body: JSON.stringify({ decision, reason }) })
    setNotice(decision === 'approve' ? 'Pattern 已提升为 Trusted。' : 'Pattern 已拒绝。')
  })

  const recordRepair = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selectedIncident) return
    const data = new FormData(event.currentTarget)
    void act(async () => {
      const result = await api(`/incidents/${selectedIncident}/repair`, { method: 'POST', body: JSON.stringify({ summary: data.get('repair'), operator_verified: data.get('confirmed') === 'on', verification_summary: data.get('verification'), verification_url: data.get('verification_url') }) })
      setNotice(`已保存 Experience，并创建 Candidate Pattern ${result.pattern_version_id.slice(0, 8)}。`)
    })
  }

  const recordMatchOutcome = (matchId: string, payload: Row) => void act(async () => {
    if (!run) return
    await api(`/pattern-matches/${matchId}/outcome`, { method: 'POST', body: JSON.stringify(payload) })
    setRun(await api(`/runs/${run.id}`))
  })

  const reviewConflict = (conflictId: string) => void act(async () => {
    const reason = window.prompt('说明你如何核实并解释这条冲突', '')
    if (!reason) return
    await api(`/conflicts/${conflictId}/review`, { method: 'POST', body: JSON.stringify({ decision: 'approve', reason }) })
    if (selectedSystem) setGraph(await api(`/systems/${selectedSystem}/graph`))
    setNotice('冲突已记录为人工解释，并绑定审核摘要。')
  })

  const createEvaluationCase = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form)
    void act(async () => {
      const created = await api('/evaluation-cases', { method: 'POST', body: JSON.stringify({
        case_key: data.get('case_key'), system_id: data.get('case_system'), fault_class: data.get('fault_class'),
        ground_truth: { operator_label: data.get('ground_truth') }, protocol_version: 'paired-v1',
      }) })
      form.reset(); setNotice(`评估用例 ${created.case_key} 已创建；ground truth 不会进入诊断流程。`)
    })
  }

  const createEvaluationPair = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form)
    void act(async () => {
      const result = await api('/evaluation-pairs', { method: 'POST', body: JSON.stringify({
        case_id: data.get('case_id'), pair_key: data.get('pair_key'), repetition: Number(data.get('repetition')),
        reset_digest: data.get('reset_digest'), graph_only_run_id: data.get('graph_only_run'),
        graph_pattern_run_id: data.get('graph_pattern_run'), graph_only_verdict: data.get('graph_only_verdict'),
        graph_pattern_verdict: data.get('graph_pattern_verdict'),
      }) })
      form.reset(); setNotice(`配对 ${result.pair_key} 的两条运行已原子记录。`)
    })
  }

  const currentSystem = systems.find(x => x.id === selectedSystem)
  const activeIncident = incidents.find(x => x.id === selectedIncident)
  const activeBusiness = businesses.find(x => x.system_id === (activeIncident?.system_id || selectedSystem))
  const activeTargetsLoaded = !!activeBusiness && targetBusinessId === activeBusiness.id
  const activeSelectedTarget = runtimeTargets.find(x => x.id === selectedTarget)
  const totals = useMemo(() => ({ nodes: graph?.nodes?.length || 0, conflicts: graph?.conflicts?.filter((x: Row) => x.status === 'open').length || 0 }), [graph])

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><span /></div><div><strong>ATR</strong><small>Adaptive Runtime</small></div></div>
      <div className="workspace-label">LOCAL WORKSPACE</div>
      <nav>
        {([['onboarding', '+', '新业务接入'], ['business', '▤', '业务档案'], ['scope', '◎', '服务器范围'], ['systems', '◈', '系统图谱'], ['investigation', '⌁', '故障调查'], ['knowledge', '✳', '知识审核'], ['efficiency', '◷', '效率分析']] as [Page, string, string][]).map(([key, icon, label]) =>
          <button key={key} className={`nav-item ${page === key ? 'active' : ''}`} onClick={() => setPage(key)}><span className="nav-icon">{icon}</span>{label}{key === 'investigation' && incidents.length > 0 && <i>{incidents.length}</i>}</button>)}
      </nav>
      <div className="sidebar-bottom">
        <div className="live-dot" /> <span>本地诊断工作区</span>
        <button className="token-button" onClick={() => document.getElementById('token-dialog')?.classList.toggle('open')}>操作员访问设置</button>
        <div id="token-dialog" className="token-dialog"><label>Operator Token</label><input type="password" value={token} onChange={e => setToken(e.target.value)} placeholder="粘贴 .env 中配置的令牌"/><button className="btn small" onClick={saveToken}>保存到本次会话</button></div>
      </div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="breadcrumb"><span>工作台</span><b>/</b><strong>{{onboarding:'新业务接入',business:'业务档案',scope:'服务器范围确认',systems:'系统图谱', investigation:'故障调查', knowledge:'知识审核', efficiency:'效率分析'}[page]}</strong></div>
        <div className="top-actions"><span className="environment"><span /> Local · Read only</span><button className="icon-button" title="刷新" onClick={() => refresh().catch(e => setError(e.message))}>↻</button><div className="avatar">OP</div></div></header>
      <div className="content">
        {error && <div className="alert error"><span>!</span>{error}<button onClick={() => setError('')}>×</button></div>}
        {notice && <div className="alert success"><span>✓</span>{notice}<button onClick={() => setNotice('')}>×</button></div>}
        {(page==='onboarding'||page==='business'||page==='scope')&&<BusinessPages page={page} onSystemSelected={setSelectedSystem}/>}

        {page === 'systems' && <>
          <div className="page-heading"><div><div className="eyebrow">UNDERSTAND BEFORE ACTION</div><h1>系统图谱</h1><p>从代码、部署和运行观察建立带来源的系统认知。</p></div><div className="heading-meta"><span className="sync"><i /> 本地数据已同步</span><button className="btn ghost" onClick={() => refresh().catch(e => setError(e.message))}>↻　刷新视图</button></div></div>
          <div className="metric-strip"><Metric label="已登记系统" value={systems.length} detail="授权目录内" icon="◈"/><Metric label="当前图节点" value={totals.nodes} detail={currentSystem?.name || '选择一个系统'} icon="⌘"/><Metric label="待处理冲突" value={totals.conflicts} detail="需要人工解释" icon="⚑" tone={totals.conflicts ? 'amber' : ''}/><Metric label="知识 Pattern" value={patterns.filter(x => x.status === 'trusted').length} detail="已审核版本" icon="✳"/></div>
          <div className="two-column">
            <section className="panel systems-panel"><div className="panel-title"><div><h2>已登记系统</h2><p>每个版本固定到一次仓库与部署快照</p></div><span className="count-pill">{systems.length} SYSTEMS</span></div>
              {systems.length === 0 ? <Empty title="尚未登记系统" text="把仓库放入授权目录，再从右侧开始建模。"/> : <div className="system-list">{systems.map((system, index) => <button key={system.id} onClick={() => setSelectedSystem(system.id)} className={`system-card ${selectedSystem === system.id ? 'selected' : ''}`}><span className={`system-symbol sym-${index%3}`}>{system.name?.slice(0,1)?.toUpperCase() || 'S'}</span><span className="system-info"><strong>{system.name}</strong><small>{system.source_path}</small><small className="system-rev">图版本 v{system.version || 0} · {String(system.repo_revision).slice(0, 12)}</small></span><span className="system-state">{badge(system.snapshot_status || 'current')}<b>›</b></span></button>)}</div>}
              {graph && <GraphPanel graph={graph} onReview={reviewConflict}/>}
            </section>
            <section className="panel onboard-panel"><div className="panel-title"><div><h2>登记新系统</h2><p>只扫描已存在的本地仓库，不执行项目代码。</p></div><span className="step-icon">01</span></div>
              <form className="form" onSubmit={onBoard}><label>授权目录中的仓库名 <input name="source_key" required pattern="[a-zA-Z0-9][a-zA-Z0-9._-]*" placeholder="例如 system-a"/><small>可用 demo 或 targets 目录下的子目录</small></label><label>显示名称 <input name="display_name" required placeholder="例如 订单 API"/></label><label>Compose 文件相对路径 <input name="compose_file" defaultValue="compose.yaml"/></label><div className="notice-box"><span>ⓘ</span><p>支持包含 2–8 个服务的 Compose 系统。仓库以只读方式扫描，无法访问目录外文件。</p></div><button className="btn primary full" disabled={busy}>{busy ? '正在建立系统图…' : '建立系统图'} <span>→</span></button></form>
            </section>
          </div>
        </>}

        {page === 'investigation' && <>
          <div className="page-heading"><div><div className="eyebrow">EVIDENCE-LED DIAGNOSIS</div><h1>故障调查</h1><p>先收集当前只读证据，再判断哪些结论有足够支持。</p></div><div className="heading-meta"><span className="run-limit">◉ 只读调用由配置预算限制</span></div></div>
          <div className="investigation-grid">
            <section className="panel incident-panel"><div className="panel-title"><div><h2>创建事件</h2><p>从已有系统图谱开始一次诊断。</p></div><span className="step-icon">02</span></div>
              <form className="form" onSubmit={createIncident}><label>目标系统<select name="system_id" required value={selectedSystem} onChange={e => setSelectedSystem(e.target.value)}><option value="">选择已登记系统</option>{systems.map(x => <option value={x.id} key={x.id}>{x.name}</option>)}</select></label><label>故障现象<textarea name="symptom" required minLength={3} rows={3} placeholder="例如：外部请求返回 502，发生在 14:20 左右"/></label><label>故障开始时间（可选）<input name="window_start" type="datetime-local"/><small>留空时以创建事件的时间作为起点；日志证据只接受该时间之后的错误记录。</small></label><label>调查模式<select name="mode"><option value="normal">{currentSystem?.source_path?.startsWith("business://") ? "自适应调查 · 默认" : "固定调查 · Demo 默认"}</option><option value="graph_adaptive" disabled={!currentSystem?.source_path?.startsWith("business://")}>自适应调查 · 需业务画像与范围</option><option value="graph_only">Graph-only · 配对基线</option><option value="graph_pattern">Graph + Pattern · 检索历史检查清单</option></select></label><button className="btn primary full" disabled={busy || !systems.length}>创建故障事件 <span>→</span></button></form>
              <div className="divider"/><div className="panel-title compact"><div><h2>事件列表</h2><p>共 {incidents.length} 个事件</p></div></div>
              <div className="incident-list">{incidents.length === 0 ? <Empty title="还没有故障事件" text="创建事件后即可开始调查。"/> : incidents.map(x => <button key={x.id} className={`incident-row ${selectedIncident === x.id ? 'selected' : ''}`} onClick={() => {setSelectedIncident(x.id); setSelectedSystem(x.system_id)}}><span className="incident-mark">⌁</span><span><strong>{x.symptom?.text || '未命名事件'}</strong><small>{x.system_name} · {fmtDate(x.created_at)}</small></span><span className="incident-right">{badge(x.status)}<b>›</b></span></button>)}</div>
            </section>
            <section className="panel run-panel"><div className="panel-title"><div><h2>诊断运行</h2><p>{activeIncident ? activeIncident.symptom?.text : '选择一个事件或创建新事件'}</p></div>{activeIncident && <button className="btn primary" disabled={busy||!!runJob||!!activeBusiness&&(!activeTargetsLoaded||!activeSelectedTarget?.investigation_readiness?.eligible)} onClick={() => runIncident(activeIncident.id)}>▶　{runJob?'调查进行中':'开始只读调查'}</button>}</div>
              {activeBusiness && <div className="target-picker"><label>本次调查 SSH 目标<select aria-label="本次调查 SSH 目标" value={activeTargetsLoaded ? selectedTarget : ''} disabled={busy||!!runJob||!activeTargetsLoaded} onChange={e => setSelectedTarget(e.target.value)}><option value="">{activeTargetsLoaded ? '请选择已授权目标' : '正在读取目标…'}</option>{runtimeTargets.map((target: Row) => <option key={target.id} value={target.id} disabled={!target.investigation_readiness?.eligible}>{target.name} · {target.host}:{target.port} · {target.execution_mode} · {target.investigation_readiness?.eligible ? '已授权' : target.investigation_readiness?.reason || '不可用'}</option>)}</select><small>{activeSelectedTarget ? `${activeSelectedTarget.name} · ${activeSelectedTarget.host}:${activeSelectedTarget.port} · ${activeSelectedTarget.execution_mode} · ${activeSelectedTarget.investigation_readiness?.status === 'mock' ? 'Mock 测试目标' : activeSelectedTarget.investigation_readiness?.eligible ? '授权有效' : activeSelectedTarget.investigation_readiness?.reason}` : runtimeTargets.length > 1 ? '配置了多个目标，请明确选择；不可用目标不会触发回退。调查开始后绑定固定。' : runtimeTargets.length === 0 ? '当前业务没有服务器目标，无法启动调查。' : '目标已失效或授权/凭据不可用，不能用于调查。'}</small></label></div>}
              {!run ? <div className="run-empty"><div className="empty-orbit"><span>⌁</span><i/><i/><i/></div><h3>等待调查运行</h3><p>运行会先读取服务状态和近期日志，所有调用都会进入审计时间线。</p><div className="run-steps"><span><b>01</b> 定位当前图版本</span><span><b>02</b> 采集状态与日志</span><span><b>03</b> 检查证据门</span></div></div> : <RunView run={run} onScope={() => setPage('scope')} onResume={() => void act(async () => {
                const result = await api(`/runs/${run.id}/resume`, {method: 'POST', body: '{}'});
                setRunJob(result.job_id); setNotice('在原调查预算内继续，后台会再次核验范围授权。');
              })} onRepair={recordRepair} onMatchOutcome={recordMatchOutcome} verificationDefault={currentSystem?.compose_project === 'atr-system-b' ? 'http://ingress_proxy/health' : 'http://edge_proxy/health'}/>}
            </section>
          </div>
        </>}

        {page === 'knowledge' && <>
          <div className="page-heading"><div><div className="eyebrow">HUMAN-REVIEWED KNOWLEDGE</div><h1>知识审核</h1><p>只有已确认诊断、记录修复并完成复核后，才可创建可迁移的检查 Pattern。</p></div><div className="heading-meta"><span className="count-pill">{patterns.length} VERSIONS</span></div></div>
          <div className="knowledge-summary"><Metric label="Candidate" value={patterns.filter(x => x.status === 'candidate').length} detail="等待人工审核" icon="◷" tone="amber"/><Metric label="Trusted" value={patterns.filter(x => x.status === 'trusted').length} detail="可进行匹配" icon="✳" tone="green"/><Metric label="Stale / Rejected" value={patterns.filter(x => ['stale','rejected'].includes(x.status)).length} detail="不参与自动检索" icon="⊘"/></div>
          <section className="panel knowledge-panel"><div className="panel-title"><div><h2>Pattern 版本</h2><p>审核绑定精确版本摘要；历史证据不会替代当前事件取证。</p></div><div className="legend"><span><i className="dot candidate"/> Candidate</span><span><i className="dot trusted"/> Trusted</span></div></div>
            {patterns.length === 0 ? <Empty title="知识库还没有 Pattern" text="在一次有证据支持的调查中记录人工修复和验证，再回来审核候选版本。"/> : <div className="pattern-list">{patterns.map(p => <article className="pattern-card" key={p.id}><div className="pattern-main"><div className="pattern-icon">✳</div><div className="pattern-title"><div><strong>{p.pattern_key}</strong>{badge(p.status)}</div><p>v{p.version} · 来源事件 {p.incident_id?.slice(0, 8)} · 摘要 {p.content_digest?.slice(0, 12)}</p></div><div className="pattern-date">{fmtDate(p.created_at || p.validity?.created_at)}</div></div><div className="pattern-body"><div><small>角色约束</small><div className="chip-row">{(p.roles || []).map((x: string) => <span className="chip" key={x}>{x}</span>)}</div></div><div><small>可复用检查顺序</small><div className="check-list">{(p.checks || []).slice(0, 4).map((x: Row, i: number) => <span key={i}><b>{String(i+1).padStart(2,'0')}</b>{x.tool_id}</span>)}</div></div><div><small>适用症状词</small><p className="muted">{(p.symptoms || []).join(' · ') || '—'}</p></div></div>{p.status === 'candidate' && <div className="pattern-actions"><span>请核对根因、修复验证及跨系统角色绑定后再审核。</span><div><button className="btn danger-outline" onClick={() => reviewPattern(p.id,'reject')}>拒绝</button><button className="btn primary" onClick={() => reviewPattern(p.id,'approve')}>审核并提升 Trusted</button></div></div>}</article>)}</div>}
          </section>
          <section className="panel evaluation-history"><div className="panel-title"><div><h2>待复核提醒与失败反例</h2><p>失败推断保留为未验证反例；可信知识的旧审核记录保留，复用前仍需核对当前资料和范围。</p></div></div>
            {patterns.filter(p => p.health?.needs_review).map(p => <p className="knowledge-alert" key={p.id}>{p.pattern_key}：需要重新核验 · {p.health.validation_failed || 0} 次验证失败 · {p.health.not_useful || 0} 次无帮助反馈{p.compatibility?.compatible === false ? ' · 适用版本已变化或反馈未通过' : ''}</p>)}
            {knowledgeFailures.length === 0 ? <p className="muted">暂无失败反例。</p> : knowledgeFailures.slice(0, 20).map(r => <div className="counterexample-row" key={r.run_id}><span>调查 {r.run_id.slice(0, 8)} · {r.counterexamples.length} 项未验证反例</span><button className="btn small" onClick={() => void act(async () => { const result = await api(`/runs/${r.run_id}`); setSelectedIncident(result.incident_id); setRun(result); setPage('investigation') })}>查看证据</button></div>)}
          </section>
        </>}

        {page === 'efficiency' && <>
          <div className="page-heading"><div><div className="eyebrow">OBSERVABILITY OF THE INVESTIGATOR</div><h1>效率分析</h1><p>只呈现由已保存运行和工具审计记录计算出的结果。</p></div><div className="heading-meta"><span className="sync"><i/> 数据来自本地审计库</span></div></div>
          <div className="metric-strip analytics"><Metric label="诊断运行" value={metrics.runs_total ?? 0} detail={`${metrics.runs_completed ?? 0} 已结束`} icon="⌁"/><Metric label="有证据支持" value={metrics.supported_runs ?? 0} detail={`${metrics.unresolved_runs ?? 0} 次未解决`} icon="✓" tone="green"/><Metric label="TT_RCA 中位数" value={metrics.tt_rca_seconds_median == null ? '—' : `${Math.round(metrics.tt_rca_seconds_median)}s`} detail="从运行开始到支持结论" icon="◷"/><Metric label="只读工具调用" value={metrics.diagnostic_tool_attempts ?? 0} detail={`${metrics.denied_or_failed_calls ?? 0} 次失败或拒绝`} icon="⌘"/></div>
          <div className="analytics-grid"><section className="panel"><div className="panel-title"><div><h2>知识复用漏斗</h2><p>各阶段按真实 Pattern 匹配记录计算</p></div></div><Funnel metrics={metrics}/></section><section className="panel"><div className="panel-title"><div><h2>指标说明</h2><p>样本不足时显示为空，不用预置数字填充。</p></div></div><div className="definition-list"><Definition name="TT_RCA" text="诊断运行开始至 supported_at 的秒数中位数；未支持运行不进入该统计。"/><Definition name="失败调用" text="被策略拒绝、超时或读取失败的工具调用数。"/><Definition name="Pattern 有用率" text="标记为 useful 的匹配数 ÷ 所有 Pattern 匹配数，由操作员记录。"/><Definition name="配对评估" text="只有同目标、同用例和同 reset digest 的 Graph / Graph+Pattern 运行组成完整配对。"/></div></section></div>
          <section className="panel evaluation-history"><div className="panel-title"><div><h2>三模式完整对比</h2><p>仅统计三种模式都完成的同组调查；评分依赖登记真值，token 可能为估算，不能直接当作生产准确率。</p></div><span className="count-pill">{metrics.three_arm_evaluation?.completed_groups || 0} GROUPS</span></div>
            <div className="comparison-scroll"><table className="comparison-table"><thead><tr><th>模式</th><th>完整根因正确</th><th>误报</th><th>未解决</th><th>平均工具 / 模型</th><th>平均 token</th></tr></thead><tbody>{Object.entries(metrics.three_arm_evaluation?.arms || {}).map(([arm, raw]) => { const m = raw as Row; const rate = (v: number | null) => v == null ? '—' : `${(v * 100).toFixed(1)}%`; return <tr key={arm}><td>{arm === 'graph_only' ? 'Graph-only' : arm === 'graph_adaptive' ? 'Adaptive' : 'Trusted Pattern'}</td><td>{rate(m.correct_rate)}</td><td>{rate(m.false_positive_rate)}</td><td>{rate(m.unresolved_rate)}</td><td>{m.mean_diagnostic_tool_calls == null ? '—' : `${m.mean_diagnostic_tool_calls.toFixed(1)} / ${m.mean_model_calls.toFixed(1)}`}</td><td>{m.mean_input_tokens == null ? '—' : Math.round(m.mean_input_tokens + m.mean_output_tokens)}</td></tr>})}</tbody></table></div>
          </section>
          <div className="evaluation-grid"><section className="panel"><div className="panel-title"><div><h2>登记评估用例</h2><p>ground truth 仅保存在评估表，不会进入 Agent 或诊断 API。</p></div><span className="count-pill">{evaluationCases.length} CASES</span></div><form className="form" onSubmit={createEvaluationCase}><label>用例编号<input name="case_key" required pattern="[a-zA-Z0-9][a-zA-Z0-9._-]*" placeholder="例如 db-startup-failure-v1"/></label><label>目标系统<select name="case_system" required defaultValue={selectedSystem}>{systems.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label><label>故障类别<input name="fault_class" required defaultValue="dependency_startup_failure"/></label><label>操作员确认的根因标签<textarea name="ground_truth" required minLength={5} rows={2} placeholder="仅供离线配对评估评分，不进入模型上下文"/></label><button className="btn primary" disabled={busy || !systems.length}>保存评估用例</button></form></section>
            <section className="panel"><div className="panel-title"><div><h2>登记同目标配对</h2><p>需要一次 Graph-only 与一次 Graph+Pattern 已完成运行。</p></div><span className="count-pill">{metrics.complete_paired_evaluations ?? 0} COMPLETE</span></div><form className="form" onSubmit={createEvaluationPair}><label>评估用例<select name="case_id" required>{evaluationCases.map(c => <option key={c.id} value={c.id}>{c.case_key} · {c.system_name}</option>)}</select></label><label>配对键与重复序号<div className="split-input"><input name="pair_key" required pattern="[a-zA-Z0-9][a-zA-Z0-9._-]*" placeholder="pair-01"/><input name="repetition" type="number" min="1" max="100" defaultValue="1" required/></div></label><label>复位摘要（固定用例版本）<select name="reset_digest" required>{evaluationCases.map(c => <option key={c.id} value={c.fixture_digest}>{c.case_key} · {c.fixture_digest}</option>)}</select><small>同一用例两臂使用相同摘要；操作员仍需按运行手册确认基线已复位。</small></label><label>Graph-only 运行<select name="graph_only_run" required>{runs.filter(r => r.mode === 'graph_only').map(r => <option key={r.id} value={r.id}>{r.system_name} · {r.symptom} · {r.id.slice(0,8)}</option>)}</select></label><label>Graph+Pattern 运行<select name="graph_pattern_run" required>{runs.filter(r => r.mode === 'graph_pattern').map(r => <option key={r.id} value={r.id}>{r.system_name} · {r.symptom} · {r.id.slice(0,8)}</option>)}</select></label><div className="split-input"><label>Graph-only 评分<select name="graph_only_verdict"><option value="correct">根因正确</option><option value="unresolved">未解决</option><option value="incorrect">根因错误</option></select></label><label>Graph+Pattern 评分<select name="graph_pattern_verdict"><option value="correct">根因正确</option><option value="unresolved">未解决</option><option value="incorrect">根因错误</option></select></label></div><button className="btn primary" disabled={busy || !evaluationCases.length}>原子保存配对结果</button></form></section></div>
          <section className="panel evaluation-history"><div className="panel-title"><div><h2>已记录的评估运行</h2><p>指标从运行事件派生，ground truth 不在此页面返回。</p></div><span className="count-pill">{evaluationTrials.length} TRIALS</span></div>{evaluationTrials.length === 0 ? <Empty title="暂无评估运行" text="创建用例并记录一组完整配对后，这里会展示每个评估臂的实际结果。"/> : <div className="evaluation-rows">{evaluationTrials.map(t => <div className="evaluation-row" key={t.id}><span className="eval-arm">{t.arm === 'graph_only' ? 'Graph-only' : t.arm === 'graph_adaptive' ? 'Graph + Adaptive' : 'Graph + Pattern'}</span><strong>{t.case_key}</strong><span>{t.pair_key} · #{t.repetition}</span>{badge(t.verdict)}<span>{t.metrics.diagnostic_tool_calls} calls · {t.metrics.diagnostic_steps} steps · {t.metrics.tt_rca_seconds == null ? '未支持' : `${Math.round(t.metrics.tt_rca_seconds)}s`}</span></div>)}</div>}</section>
        </>}
        <footer className="footer"><span>Adaptive Troubleshooting Runtime <b>·</b> Local prototype</span><span>只读诊断 · 人工审核 · 证据可追溯</span></footer>
      </div>
    </main>
  </div>
}

function Metric({ label, value, detail, icon, tone = '' }: {label: string; value: any; detail: string; icon: string; tone?: string}) {
  return <div className={`metric-card ${tone}`}><div className="metric-top"><span>{label}</span><i>{icon}</i></div><strong>{value}</strong><small>{detail}</small></div>
}
function Empty({ title, text }: {title: string; text: string}) { return <div className="empty"><div>◌</div><strong>{title}</strong><p>{text}</p></div> }
function GraphPanel({ graph, onReview }: {graph: Row; onReview: (id: string) => void}) {
  const nodeNames = Object.fromEntries(graph.nodes.map((node: Row) => [node.id, node.name]))
  return <div className="graph-section"><div className="graph-heading"><div><h3>当前系统图</h3><span>快照 v{graph.snapshot.version} · {graph.snapshot.repo_revision.slice(0,12)}</span></div><span className="count-pill">{graph.nodes.length} NODES</span></div><div className="graph-nodes">{graph.nodes.map((node: Row) => <div className="graph-node" key={node.id}><i className={`node-dot ${node.role}`}/><div><strong>{node.name}</strong><small>{node.kind} · {node.role}</small></div></div>)}</div>{graph.edges.length > 0 && <div className="edge-list">{graph.edges.map((edge: Row) => <span key={edge.id}>{nodeNames[edge.from_node_id] || '?'} <b>→</b> {edge.relation} <b>→</b> {nodeNames[edge.to_node_id] || '?'}</span>)}</div>}{graph.conflicts.filter((x: Row) => x.status === 'open').map((c: Row) => <div className="conflict-row" key={c.id}><span>⚑</span><div><strong>{c.property}</strong><small>{c.subject_key} · {c.severity}</small></div>{badge('open')}<button className="btn small" onClick={() => onReview(c.id)}>标记已解释</button></div>)}<details className="claims-details"><summary>来源事实 · {graph.claims.length} 项</summary><div className="claim-list">{graph.claims.slice(0, 20).map((c: Row) => <div key={c.id}><span>{c.source_type || c.truth_type}</span><strong>{c.property}</strong><small>{c.source_locator}</small></div>)}</div></details></div>
}
function RunView({ run, onRepair, onMatchOutcome, verificationDefault, onResume, onScope }: {run: Row; onResume: () => void; onScope: () => void; onRepair: (e: React.FormEvent<HTMLFormElement>) => void; onMatchOutcome: (id: string, payload: Row) => void; verificationDefault: string}) {
  const summary = run.summary || {}
  const gate = summary.gate || {}
  const budget = run.context_budget
  return <div className="run-view"><div className="run-status-line"><div><span className={`status-pulse ${run.status}`}/><strong>{run.status === 'supported' ? '证据门通过' : run.status === 'unresolved' ? '尚未建立根因' : run.status}</strong><small>运行 {run.id.slice(0,8)} · 图版本 {run.graph_snapshot_id?.slice(0,8)}</small></div>{badge(run.status)}</div>
    <div className="target-binding"><strong>Run 的 SSH 目标</strong>{run.target_binding_status === 'unknown' ? <span>历史运行：来源未知，未保存目标绑定</span> : <span>{run.runtime_target?.name || '目标名称未保存'} · {run.runtime_target?.host || '地址未知'}{run.runtime_target?.port ? `:${run.runtime_target.port}` : ''} · {run.runtime_target?.execution_mode || '执行方式未知'} · {run.target_id}</span>}</div>
    {summary.discovery_ids && <section className="panel"><h3>自主发现与范围建议</h3>
      <p>基础采集：{summary.collection_status || '已保留'}。候选证据用于范围审核，尚未构成根因结论。</p>
      {(summary.mapping_proposals || []).map((p: Row) => <div className="business-row" key={p.binding_id}>
        <div><strong>{p.service}（{p.role}） → {p.runtime_candidate || p.candidate_key} · {p.kind}</strong>
          <small>依据：{p.reasons.join('；')} · 证据 {p.evidence_ids.join(', ')}</small>
          <small>反证或冲突：{p.counterevidence.join('；') || '未提供'} · 不确定性：{p.uncertainty}</small>
          <small>审核后可考虑：{p.next_tools.join('、') || '无'}</small></div>
        {badge(p.state)}
      </div>)}
      {summary.stage === 'awaiting_review' && <><button className="btn" onClick={onScope}>审核运行范围</button>
        <button className="btn primary" onClick={onResume}>审核后继续调查</button></>}
      {(summary.tool_errors || []).map((e: Row, i: number) => <p className="alert error" key={i}>
        {e.tool_id} · {e.error_code} · 阶段 {e.failure_stage || 'budget'} · {e.message}
        {e.line_number != null && ` · 行 ${e.line_number}`} · 输出 {e.output_bytes || 0} 字节 · 异常 {e.invalid_item_count || 0} 项</p>)}
    </section>}
    <div className="diagnosis-summary"><div className="summary-label"><span>诊断摘要</span><span>{run.evidence?.length || 0} 条当前证据</span></div><p>{summary.model_error?.message || summary.summary || '本次运行没有可展示的摘要。'}</p>{summary.next_action && <div className="next-action"><span>建议的下一步</span><p>{summary.next_action}</p></div>}<div className="gate-note"><b>证据门：</b>{gate.rationale || '无支持结论。'}　当前冲突：{gate.open_conflict_count ?? 0}</div></div>
    <div className="business-block"><p className="muted">Agent 阶段：{summary.stage === 'stopped' ? '已停止' : summary.stage === 'completed' ? '已完成' : summary.stage === 'awaiting_review' ? '等待范围审核' : summary.stage||run.status} · 模型：{summary.model_status === 'failed' ? '调用失败' : summary.model_status === 'offline' ? '离线' : summary.model_status || '历史记录未提供'} {summary.round&&`· 第 ${summary.round} 轮`} · 数据来源：{summary.connector_types?.join('、')||'查看下方工具审计'}</p><ModelError error={summary.model_error}/>{summary.unresolved_reason&&!summary.model_error&&<p className="alert error">未解决原因：{summary.unresolved_reason}</p>}{budget&&<p>输入 {budget.input_tokens} · 输出 {budget.output_tokens} Tokens · 模型 {budget.calls}/{budget.limits.max_calls} 次 · 工具 {budget.tool_calls}/{budget.limits.max_tools} 次 · 读取 {budget.retrieved_bytes} bytes {budget.stop_reason&&`· 停止：${budget.stop_reason}`}</p>}{budget?.usage_records.map((u:Row)=><ModelAttempt key={u.id} usage={u}/>)}<details><summary>每次实际执行的工具与策略结果</summary>{run.tool_calls?.map((c:Row)=><div className="business-row" key={c.id}><strong>{c.tool_id}</strong>{badge(c.status)}<small>{fmtDate(c.started_at)} · {c.policy_version} · {c.error_code||c.id}</small>{c.actual_target_id && <small>实际目标：{c.actual_target?.name || '名称未知'} · {c.actual_target?.host || '地址未知'} · {c.actual_target?.execution_mode || '执行方式未知'} · {c.actual_target_id}</small>}{c.diagnostic?.error_code && <small>{c.diagnostic.message} · 阶段 {c.diagnostic.failure_stage} · 行 {c.diagnostic.line_number ?? "—"} · 输出 {c.diagnostic.output_bytes} 字节 · 异常 {c.diagnostic.invalid_item_count} 项</small>}</div>)}</details></div>
    <div className="run-section-title"><h3>假设与证据</h3><span>{run.hypotheses?.length || 0} HYPOTHESES</span></div>
    {!!summary.hypothesis_history?.length&&<details className="business-block"><summary>查看每轮假设与反证</summary>{summary.hypothesis_history.map((h:Row)=><div className="business-row" key={h.hypothesis_id+'-'+h.round}><strong>第 {h.round} 轮 · {h.failure_mode}</strong>{badge(h.status)}<p>{h.prediction}</p><small>支持 {h.support_evidence?.length||0} · 反证 {h.counter_evidence?.length||0}{h.missing_evidence?.length?` · 尚缺：${h.missing_evidence.join('、')}`:''}</small></div>)}</details>}
    {!!summary.scope_requests?.length&&<div className="alert">需要人工确认额外范围：{summary.scope_requests.map((r:Row)=>`${r.service}（${r.reason}）`).join('；')}</div>}
    <div className="hypothesis-list">{(run.hypotheses || []).map((h: Row) => <article className="hypothesis" key={h.id}><div className="hypothesis-head"><div><span className="hypothesis-num">H</span><strong>{h.failure_mode}</strong></div>{badge(h.status)}</div><p>{h.falsifiable_prediction}</p><div className="causal-chain">{(JSON.parse(h.causal_chain_json || '[]') as string[]).map((x, i) => <React.Fragment key={i}><span>{x}</span>{i < JSON.parse(h.causal_chain_json || '[]').length-1 && <b>→</b>}</React.Fragment>)}</div><div className="hypothesis-links">{h.evidence_links?.map((e:Row)=><p key={e.evidence_id+e.stance}><b>{e.stance==='support'?'支持':'反证'}</b> · {e.evidence_id.slice(0,12)} · {e.explanation}</p>)}</div></article>)}</div>
    <div className="run-section-title"><h3>逐轮诊断</h3><span>{run.diagnostic_steps?.length || 0} STEPS</span></div>
    <div className="iteration-list">{(run.diagnostic_steps || []).map((step: Row) => { const action = step.action || {}; const executed = action.executed_action; const proposed = action.proposed_action; const actionLabel = executed ? `${executed.tool_id} · ${executed.service} (${executed.role})` : proposed && step.review_outcome === 'supported' ? '证据门已通过，未执行后续动作' : proposed && action.policy_outcome === 'rejected' ? '建议检查被策略拒绝' : proposed ? '建议的检查未执行' : '本轮未提出额外检查'; return <article className="iteration-card" key={step.id}><div><strong>第 {step.sequence_no} 轮 · {badge(step.review_outcome)}</strong><small>置信分 {Number(step.score_before).toFixed(2)} → {Number(step.score_after).toFixed(2)}</small></div><p>{actionLabel}</p>{proposed?.rationale && <small>{proposed.rationale}</small>}<small>{action.evidence_ids?.length || 0} 条用于复核的证据{action.tool_status ? ` · 工具 ${action.tool_status}` : ''}</small></article> })}</div>
    {summary.pattern_matches && <><div className="run-section-title"><h3>可迁移检查建议</h3><span>历史 Pattern 不是当前证据</span></div><div className="pattern-match-list">{summary.pattern_matches.length === 0 ? <p className="muted">没有满足角色约束的 Trusted Pattern。</p> : summary.pattern_matches.map((m: Row) => <article className="pattern-match" key={m.match_id}><div><strong>{m.pattern.pattern_key} · v{m.pattern.version}</strong><span>{badge(m.decision)}　匹配分 {Number(m.score).toFixed(2)}</span><p>角色绑定：{(m.binding?.roles || []).join(' · ')}{m.executed_check_count == null ? '' : ` · 本次按 Pattern 采集 ${m.executed_check_count} 项`}</p><div className="match-checks">{(m.pattern.checks || []).map((c: Row, i: number) => <span key={i}>{i+1}. {c.tool_id}{c.target_role ? ` · ${c.target_role}` : ''}</span>)}</div></div>{m.decision === 'suggested' && <div className="match-actions"><div><small>检查已在当前目标验证？</small><button className="btn small" disabled={m.validation_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {validation:'validated'})}>是</button><button className="btn small" disabled={m.validation_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {validation:'failed'})}>否</button></div><div><small>对本次调查有帮助？</small><button className="btn small" disabled={m.usefulness_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {usefulness:'useful'})}>有帮助</button><button className="btn small" disabled={m.usefulness_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {usefulness:'not_useful'})}>没有</button></div><span>复核：{m.validation_outcome} · 有用性：{m.usefulness_outcome}</span></div>}</article>)}</div></>}
    <div className="run-section-title"><h3>审计时间线</h3><span>{run.events?.length || 0} EVENTS</span></div><div className="timeline">{(run.events || []).map((event: Row) => <div className="timeline-item" key={event.id}><i/><div><strong>{event.event_type}</strong><small>{fmtDate(event.created_at)} · {event.actor}</small></div></div>)}{(run.evidence || []).map((ev: Row) => <div className="timeline-item evidence-item" key={ev.id}><i/><div><strong>{ev.tool_id} · {ev.freshness_state}</strong><small>{fmtDate(ev.observed_at)} · {ev.content_hash.slice(0,12)}{ev.truncated ? ' · truncated' : ''}</small>{ev.payload.text && <pre>{ev.payload.text.slice(-1000)}</pre>}{ev.payload.containers && <pre>{ev.payload.containers.map((x: Row) => `${x.service || x.name}: ${x.state}`).join('\n')}</pre>}</div></div>)}</div>
    {run.status === 'supported' && <form className="repair-form" onSubmit={onRepair}><div className="run-section-title"><h3>记录人工确认与修复</h3><span>创建 Candidate</span></div><label>修复记录<textarea name="repair" required minLength={5} rows={2} placeholder="说明人工执行的修复内容；Agent 不会执行写操作"/></label><label>验证结果<textarea name="verification" required minLength={5} rows={2} placeholder="说明恢复后的验证请求、时间与观察结果"/></label><label>只读健康检查 URL（须在 ALLOWED_HEALTH_URLS 中）<input name="verification_url" required defaultValue={verificationDefault}/></label><label className="check-label"><input type="checkbox" name="confirmed" required/> 我已复核诊断结论、人工修复和后续验证</label><button className="btn primary">验证 HTTP 200 并保存 Candidate</button></form>}
  </div>
}
function Funnel({ metrics }: {metrics: Row}) {
  const values = [["检索匹配", metrics.patterns_retrieved || 0], ["达到建议门槛", metrics.patterns_suggested || 0], ["验证通过", metrics.patterns_validated || 0], ["被确认有用", metrics.patterns_useful || 0]] as [string, number][]
  const max = Math.max(1, ...values.map(x => x[1]))
  return <div className="funnel">{values.map(([name, value], i) => <div className="funnel-row" key={name}><span>{name}</span><div className="funnel-track"><i style={{width: `${Math.max(value ? 12 : 3, value/max*100)}%`, opacity: 1-i*.15}}/></div><b>{value}</b></div>)}<div className="funnel-rate"><span>建议通过率</span><strong>{metrics.pattern_suggestion_rate == null ? '—' : `${Math.round(metrics.pattern_suggestion_rate*100)}%`}</strong></div></div>
}
function Definition({ name, text }: {name:string;text:string}) { return <div className="definition"><strong>{name}</strong><p>{text}</p></div> }

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>)
