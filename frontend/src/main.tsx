import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import { api } from './api'
import { BusinessPages } from './business'
import { StructureView } from './structure-view'
import { ModelError, ModelAttempt } from './model-error'
import { applyTheme, initialTheme } from './theme'

type Row = Record<string, any>
type Page = 'systems' | 'investigation' | 'knowledge' | 'efficiency' | 'onboarding' | 'business' | 'scope' | 'structure'

const fmtDate = (value?: string) => value ? new Date(value).toLocaleString() : '—'
const badge = (value: string) => <span className={`badge ${value.toLowerCase().replaceAll('_', '-')}`}>{value}</span>

function App() {
  const [page, setPage] = useState<Page>('business')
  const [theme, setTheme] = useState(initialTheme)
  useEffect(() => applyTheme(theme), [theme])
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
  const [runJob, setRunJob] = useState(sessionStorage.getItem('atr_active_run_job') || '')
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
    if (runJob) sessionStorage.setItem('atr_active_run_job', runJob)
    else sessionStorage.removeItem('atr_active_run_job')
  }, [runJob])

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
        if (id) {const current = await api(`/runs/${id}`); if(stopped)return;setRun(current);setSelectedIncident(current.incident_id);setSelectedSystem(current.system_id)}
        setError('')
        if (['queued','running'].includes(state.status)) timer = setTimeout(poll,1500)
        else {setRunJob(''); await refresh(); if(state.status==='waiting_input')setNotice('调查正在等待范围确认，请查看当前问题。');else if(state.status==='cancelled')setNotice('本次调查已取消。');else if(state.status!=='completed')setError(state.error || '调查执行失败或中断，请查看已保留的证据。')}
      } catch(e) {if(!stopped){setError(`连接中断，调查状态暂时未知；恢复连接后将继续读取同一调查。${(e as Error).message}`);timer=setTimeout(poll,1500)}}
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
      const created = await api('/incidents', { method: 'POST', body: JSON.stringify({ system_id: data.get('system_id'), symptom: data.get('symptom'), mode: 'normal', task_type: data.get('task_type'), window_start: occurred ? new Date(occurred).toISOString() : null }) })
      setSelectedIncident(created.id); setPage('investigation'); form.reset()
    })
  }

  const runIncident = (incidentId: string) => void act(async () => {
    const incident = incidents.find(x => x.id === incidentId)
    const business = businesses.find(x => x.system_id === incident?.system_id)
    if (business && (targetBusinessId !== business.id || !runtimeTargets.some(t => t.id === selectedTarget && t.investigation_readiness?.eligible)))
      throw new Error('请先为本次调查选择一个当前有效且已授权的 SSH 目标。')
    const result = await api(`/incidents/${incidentId}/runs`, { method: 'POST', body: JSON.stringify(business ? {target_id: selectedTarget, decision_protocol: 'ab_v1'} : {}) })
    setSelectedIncident(incidentId); setRunJob(result.job_id); setPage('investigation'); setNotice('只读调查已排入后台，可查看实际执行过程。')
    if (selectedSystem) setGraph(await api(`/systems/${selectedSystem}/graph`))
  })

  const reviewPattern = (patternId: string, decision: 'approve' | 'reject') => void act(async () => {
    const reason = window.prompt(decision === 'approve' ? '写下审核依据（包括复核过的适用范围）' : '写下拒绝原因', '')
    if (!reason) return
    await api(`/patterns/${patternId}/review`, { method: 'POST', body: JSON.stringify({ decision, reason }) })
    setNotice(decision === 'approve' ? '排障经验已审核，可作为后续检查建议。' : '排障经验已否决。')
  })

  const recordRepair = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selectedIncident) return
    const data = new FormData(event.currentTarget)
    void act(async () => {
      const result = await api(`/incidents/${selectedIncident}/repair`, { method: 'POST', body: JSON.stringify({ summary: data.get('repair'), operator_verified: data.get('confirmed') === 'on', verification_summary: data.get('verification'), verification_url: data.get('verification_url') }) })
      setNotice(`已保存修复记录，并创建待审核排障经验 ${result.pattern_version_id.slice(0, 8)}。`)
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
        {([['onboarding', '+', '导入业务资料'], ['business', '▤', '业务概览'], ['scope', '◎', '服务器与服务'], ['structure', '◈', '业务结构图'], ['investigation', '⌁', '排查故障'], ['knowledge', '✳', '排障经验'], ['efficiency', '◷', '效果与成本']] as [Page, string, string][]).map(([key, icon, label]) =>
          <button key={key} className={`nav-item ${page === key ? 'active' : ''}`} onClick={() => setPage(key)}><span className="nav-icon">{icon}</span>{label}{key === 'investigation' && incidents.length > 0 && <i>{incidents.length}</i>}</button>)}
      </nav>
      <div className="sidebar-bottom">
        <div className="live-dot" /> <span>本地诊断工作区</span>
        <button className="token-button" onClick={() => document.getElementById('token-dialog')?.classList.toggle('open')}>操作员访问设置</button>
        <div id="token-dialog" className="token-dialog"><label htmlFor="operator-token">操作员访问令牌</label><input id="operator-token" type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} placeholder="粘贴 .env 中配置的令牌"/><button className="btn small" onClick={saveToken}>保存到本次会话</button></div>
      </div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="breadcrumb"><span>工作台</span><b>/</b><strong>{{onboarding:'导入业务资料',business:'业务概览',scope:'服务器与服务',structure:'业务结构图',systems:'旧版系统详情', investigation:'排查故障', knowledge:'排障经验', efficiency:'效果与成本'}[page]}</strong></div>
        <div className="top-actions"><span className="environment"><span /> 本地 · 只读排查</span><button className="btn small" aria-label="切换亮暗主题" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '☀ 亮主题' : '☾ 暗主题'}</button><button className="icon-button" title="刷新" onClick={() => refresh().catch(e => setError(e.message))}>↻</button><div className="avatar">OP</div></div></header>
      <div className="content">
        {error && <div className="alert error"><span>!</span>{error}<button onClick={() => setError('')}>×</button></div>}
        {notice && <div className="alert success"><span>✓</span>{notice}<button onClick={() => setNotice('')}>×</button></div>}
        {(page==='onboarding'||page==='business'||page==='scope')&&<BusinessPages page={page} onSystemSelected={setSelectedSystem}/>}
        {page==='business'&&<details className="business-block"><summary>旧版系统与审核详情</summary><p>原有系统、历史图版本和审核记录继续保留。</p><button className="btn" onClick={() => setPage('systems')}>查看旧版系统详情</button></details>}
        {page==='structure'&&<><select aria-label="选择图的业务" value={selectedSystem} onChange={event=>setSelectedSystem(event.target.value)}>{businesses.filter(item=>item.system_id).map(item=><option key={item.id} value={item.system_id}>{item.name}</option>)}</select>{currentBusiness&&<StructureView businessId={currentBusiness.id} theme={theme}/>}</>}

        {page === 'systems' && <>
          <div className="page-heading"><div><div className="eyebrow">UNDERSTAND BEFORE ACTION</div><h1>旧版系统详情</h1><p>从代码、部署和运行观察建立带来源的系统认知。</p></div><div className="heading-meta"><span className="sync"><i /> 本地数据已同步</span><button className="btn ghost" onClick={() => refresh().catch(e => setError(e.message))}>↻　刷新视图</button></div></div>
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
          <div className="page-heading"><div><div className="eyebrow">EVIDENCE-LED DIAGNOSIS</div><h1>排查故障</h1><p>先收集当前只读证据，再判断哪些结论有足够支持。</p></div><div className="heading-meta"><span className="run-limit">◉ 只读调用由配置预算限制</span></div></div>
          <div className="investigation-grid">
            <section className="panel incident-panel"><div className="panel-title"><div><h2>创建事件</h2><p>从已有系统图谱开始一次诊断。</p></div><span className="step-icon">02</span></div>
              <form className="form" onSubmit={createIncident}><label>目标系统<select name="system_id" required value={selectedSystem} onChange={e => setSelectedSystem(e.target.value)}><option value="">选择已登记系统</option>{systems.map(x => <option value={x.id} key={x.id}>{x.name}</option>)}</select></label><label>故障现象<textarea name="symptom" required minLength={3} rows={3} placeholder="例如：外部请求返回 502，发生在 14:20 左右"/></label>{currentSystem?.source_path?.startsWith("business://") ? <label>本次任务目标<select name="task_type" defaultValue="root_cause_investigation"><option value="health_check">健康检查 · 读取指定服务状态和日志</option><option value="focused_diagnosis">定向诊断 · 定位直接异常，触发原因可未知</option><option value="root_cause_investigation">根因调查 · 在授权范围内追查并通过证据核验确认</option></select><small>任务类型只控制检查目标和停止策略，不会扩大工具或服务器授权；Graph-only/Pattern 对照模式保留历史流程。</small></label> : <input type="hidden" name="task_type" value="focused_diagnosis"/>}<label>故障开始时间（可选）<input name="window_start" type="datetime-local"/><small>留空时默认回看提交前 15 分钟；旧日志会保留为历史线索。</small></label><p className="muted">直接按当前资料、授权服务和证据核验排查；历史经验仅提供检查建议。</p><button className="btn primary full" disabled={busy || !systems.length}>保存现象，准备调查 <span>→</span></button></form>
              <div className="divider"/><div className="panel-title compact"><div><h2>事件列表</h2><p>共 {incidents.length} 个事件</p></div></div>
              <div className="incident-list">{incidents.length === 0 ? <Empty title="还没有故障事件" text="创建事件后即可开始调查。"/> : incidents.map(x => <button key={x.id} className={`incident-row ${selectedIncident === x.id ? 'selected' : ''}`} onClick={() => {setSelectedIncident(x.id); setSelectedSystem(x.system_id)}}><span className="incident-mark">⌁</span><span><strong>{x.symptom?.text || '未命名事件'}</strong><small>{x.system_name} · {fmtDate(x.created_at)}</small></span><span className="incident-right">{badge(x.status)}<b>›</b></span></button>)}</div>
            </section>
            <section className="panel run-panel"><div className="panel-title"><div><h2>本次调查</h2><p>{activeIncident ? activeIncident.symptom?.text : '选择一个事件或创建新事件'}</p></div>{activeIncident && <button className="btn primary" disabled={busy||!!runJob||!!activeBusiness&&(!activeTargetsLoaded||!activeSelectedTarget?.investigation_readiness?.eligible)} onClick={() => runIncident(activeIncident.id)}>▶　{runJob?'调查进行中':'开始只读调查'}</button>}</div>
              {activeBusiness && <div className="target-picker"><label>本次调查 SSH 目标<select aria-label="本次调查 SSH 目标" value={activeTargetsLoaded ? selectedTarget : ''} disabled={busy||!!runJob||!activeTargetsLoaded} onChange={e => setSelectedTarget(e.target.value)}><option value="">{activeTargetsLoaded ? '请选择已授权目标' : '正在读取目标…'}</option>{runtimeTargets.map((target: Row) => <option key={target.id} value={target.id} disabled={!target.investigation_readiness?.eligible}>{target.name} · {target.host}:{target.port} · {target.execution_mode} · {target.investigation_readiness?.eligible ? '已授权' : target.investigation_readiness?.reason || '不可用'}</option>)}</select><small>{activeSelectedTarget ? `${activeSelectedTarget.name} · ${activeSelectedTarget.host}:${activeSelectedTarget.port} · ${activeSelectedTarget.execution_mode} · ${activeSelectedTarget.investigation_readiness?.status === 'mock' ? 'Mock 测试目标' : activeSelectedTarget.investigation_readiness?.eligible ? '授权有效' : activeSelectedTarget.investigation_readiness?.reason}` : runtimeTargets.length > 1 ? '配置了多个目标，请明确选择；不可用目标不会触发回退。调查开始后绑定固定。' : runtimeTargets.length === 0 ? '当前业务没有服务器目标，无法启动调查。' : '目标已失效或授权/凭据不可用，不能用于调查。'}</small></label></div>}
              {!run ? <div className="run-empty"><div className="empty-orbit"><span>⌁</span><i/><i/><i/></div><h3>等待调查运行</h3><p>运行会先读取服务状态和近期日志，所有调用都会进入审计时间线。</p><div className="run-steps"><span><b>01</b> 定位当前图版本</span><span><b>02</b> 采集状态与日志</span><span><b>03</b> 核验当前证据</span></div></div> : <RunView run={run} onScope={() => setPage('scope')} onResume={(answer = {}) => void act(async () => {
                const result = await api(`/runs/${run.id}/resume`, {method: 'POST', body: JSON.stringify(run.clarification?.question_id ? {question_id:run.clarification.question_id,version:run.clarification.version,...answer} : null)});
                setRunJob(result.job_id); setNotice('在原调查预算内继续，后台会再次核验范围授权。');
              })} onRepair={recordRepair} onMatchOutcome={recordMatchOutcome} verificationDefault={currentSystem?.compose_project === 'atr-system-b' ? 'http://ingress_proxy/health' : 'http://edge_proxy/health'}/>}
            </section>
          </div>
        </>}

        {page === 'knowledge' && <>
          <div className="page-heading"><div><div className="eyebrow">HUMAN-REVIEWED KNOWLEDGE</div><h1>排障经验</h1><p>把已核验的修复经历保存为排障经验，审核后仅用于建议下一次检查。</p></div><div className="heading-meta"><span className="count-pill">{patterns.length} VERSIONS</span></div></div>
          <div className="knowledge-summary"><Metric label="待审核经验" value={patterns.filter(x => x.status === 'candidate').length} detail="等待人工审核" icon="◷" tone="amber"/><Metric label="已审核经验" value={patterns.filter(x => x.status === 'trusted').length} detail="可进行匹配" icon="✳" tone="green"/><Metric label="已失效或否决" value={patterns.filter(x => ['stale','rejected'].includes(x.status)).length} detail="不参与自动检索" icon="⊘"/></div>
          <section className="panel knowledge-panel"><div className="panel-title"><div><h2>排障经验版本</h2><p>审核绑定精确版本摘要；历史证据不会替代当前事件取证。</p></div><div className="legend"><span><i className="dot candidate"/> 待审核</span><span><i className="dot trusted"/> 已审核</span></div></div>
            {patterns.length === 0 ? <Empty title="还没有排障经验" text="在一次有证据支持的调查中记录人工修复和验证，再回来审核候选版本。"/> : <div className="pattern-list">{patterns.map(p => <article className="pattern-card" key={p.id}><div className="pattern-main"><div className="pattern-icon">✳</div><div className="pattern-title"><div><strong>{p.pattern_key}</strong>{badge(p.status)}</div><p>v{p.version} · 来源事件 {p.incident_id?.slice(0, 8)} · 摘要 {p.content_digest?.slice(0, 12)}</p></div><div className="pattern-date">{fmtDate(p.created_at || p.validity?.created_at)}</div></div><div className="pattern-body"><div><small>角色约束</small><div className="chip-row">{(p.roles || []).map((x: string) => <span className="chip" key={x}>{x}</span>)}</div></div><div><small>可复用检查顺序</small><div className="check-list">{(p.checks || []).slice(0, 4).map((x: Row, i: number) => <span key={i}><b>{String(i+1).padStart(2,'0')}</b>{x.tool_id}</span>)}</div></div><div><small>适用症状词</small><p className="muted">{(p.symptoms || []).join(' · ') || '—'}</p></div></div>{p.status === 'candidate' && <div className="pattern-actions"><span>请核对根因、修复验证及跨系统角色绑定后再审核。</span><div><button className="btn danger-outline" onClick={() => reviewPattern(p.id,'reject')}>拒绝</button><button className="btn primary" onClick={() => reviewPattern(p.id,'approve')}>审核为可用经验</button></div></div>}</article>)}</div>}
          </section>
          <section className="panel evaluation-history"><div className="panel-title"><div><h2>待复核提醒与失败反例</h2><p>失败推断保留为未验证反例；可信知识的旧审核记录保留，复用前仍需核对当前资料和范围。</p></div></div>
            {patterns.filter(p => p.health?.needs_review).map(p => <p className="knowledge-alert" key={p.id}>{p.pattern_key}：需要重新核验 · {p.health.validation_failed || 0} 次验证失败 · {p.health.not_useful || 0} 次无帮助反馈{p.compatibility?.compatible === false ? ' · 适用版本已变化或反馈未通过' : ''}</p>)}
            {knowledgeFailures.length === 0 ? <p className="muted">暂无失败反例。</p> : knowledgeFailures.slice(0, 20).map(r => <div className="counterexample-row" key={r.run_id}><span>调查 {r.run_id.slice(0, 8)} · {r.counterexamples.length} 项未验证反例</span><button className="btn small" onClick={() => void act(async () => { const result = await api(`/runs/${r.run_id}`); setSelectedIncident(result.incident_id); setRun(result); setPage('investigation') })}>查看证据</button></div>)}
          </section>
        </>}

        {page === 'efficiency' && <>
          <div className="page-heading"><div><div className="eyebrow">OBSERVABILITY OF THE INVESTIGATOR</div><h1>效果与成本</h1><p>只呈现由已保存运行和工具审计记录计算出的结果。</p></div><div className="heading-meta"><span className="sync"><i/> 数据来自本地审计库</span></div></div>
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
function RuntimeQuestion({question, onResume, onScope}: {question: Row; onResume: (answer?: Row) => void; onScope: () => void}) {
  const [selected,setSelected] = useState<Record<string,Row>>({})
  const [owned,setOwned] = useState(false)
  const roles:Record<string,string> = {unknown:'作用未知',application:'应用服务',proxy:'请求入口',datastore:'数据库',cache:'缓存',worker:'后台任务',queue:'消息队列'}
  return <form className="form" onSubmit={event=>{event.preventDefault();onResume({selections:Object.values(selected),confirm_owned:owned})}}>
    <p>{question.text}</p><small>{question.impact}</small>
    {(question.options||[]).filter((option:Row)=>option.option_id).map((option:Row)=><div className="business-block" key={option.option_id}>
      <label><input type="checkbox" checked={!!selected[option.option_id]} onChange={event=>setSelected(current=>{
        const next={...current};if(event.target.checked)next[option.option_id]={option_id:option.option_id,service:option.suggested_service||option.name,role:'unknown'};else delete next[option.option_id];return next
      })}/>{option.name} · {option.kind==='container'?'容器':option.kind==='process'?'普通进程':'系统服务'}{option.previously_mapped?' · 原业务曾确认':''}</label>
      <details><summary>查看身份和来源</summary><small>完整身份：{option.runtime_id} · 来源：{option.evidence_id}</small></details>
      {selected[option.option_id]&&<><label>本业务中的名称<input required maxLength={255} value={selected[option.option_id].service} onChange={event=>setSelected(current=>({...current,[option.option_id]:{...current[option.option_id],service:event.target.value}}))}/></label>
        <label>已知作用<select value={selected[option.option_id].role} onChange={event=>setSelected(current=>({...current,[option.option_id]:{...current[option.option_id],role:event.target.value}}))}>{Object.entries(roles).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label></>}
    </div>)}
    {question.omitted_count>0&&<small>本页未展示另外 {question.omitted_count} 个候选；如需其他对象，可在服务器与服务重新发现并单独确认。</small>}
    <label><input type="checkbox" checked={owned} onChange={event=>setOwned(event.target.checked)}/>我确认所选对象属于本业务，并允许在原服务器权限内只读检查。</label>
    <small>只确认本次问题涉及的对象；未知关系仍待核对，业务资料不因此全部通过审核。</small>
    <button className="btn primary" type="submit" disabled={!owned||!Object.keys(selected).length}>确认所选对象并继续</button>
    <button className="btn" type="button" onClick={onScope}>打开服务器与服务</button>
    <button className="btn" type="button" onClick={()=>onResume()}>已在服务器与服务确认，继续</button>
  </form>
}
function RunView({ run, onRepair, onMatchOutcome, verificationDefault, onResume, onScope }: {run: Row; onResume: (answer?: Row) => void; onScope: () => void; onRepair: (e: React.FormEvent<HTMLFormElement>) => void; onMatchOutcome: (id: string, payload: Row) => void; verificationDefault: string}) {
  const [cancelError, setCancelError] = useState('')
  const summary = run.summary || {}
  const gate = summary.gate || {}
  const budget = run.context_budget
  const findingLabel = ({root_cause_confirmed: '证据核验已确认根因', direct_fault_cause_unknown: '直接故障已确认，触发原因未确认', no_fault_observed_in_scope: '当前授权范围内未观察到故障', evidence_insufficient: '证据不足，未确认根因'} as Record<string, string>)[summary.runtime_outcome?.finding_level]
  const taskLabels: Record<string,string> = {health_check:'健康检查', focused_diagnosis:'定向诊断', root_cause_investigation:'根因调查'}
  const taskStatusLabels: Record<string,string> = {completed:'已完成', completed_with_gaps:'已完成（存在检查或范围缺口）', stopped_with_gap:'已停止，证据仍有缺口', stopped_with_limit:'已达任务/预算上限', stopped_with_error:'因错误停止', in_progress:'进行中'}
  const stopReasonLabels: Record<string,string> = {task_completed:'已完成本次任务目标', evidence_supported:'证据核验已确认根因', no_further_useful_checks:'没有更多有用的授权检查', no_progress:'连续检查没有带来新信息', task_soft_limit:'达到任务级软上限', model_finished_without_useful_action:'模型建议结束，Runtime 未发现值得继续的任务相关检查', model_call_limit:'模型调用硬预算耗尽', tool_call_limit:'工具调用硬预算耗尽', total_token_limit:'累计 Token 硬预算耗尽', permission_blocked:'权限或范围阻断', no_new_actions:'没有新的合法动作'}
  const runStatusLabel = summary.task_completion_status === 'completed' || summary.task_completion_status === 'completed_with_gaps'
    ? `任务${taskStatusLabels[summary.task_completion_status]} · 根因状态单独显示`
    : run.status === 'supported' ? '证据核验通过' : run.status === 'unresolved' ? '尚未建立根因' : run.status
  return <div className="run-view"><div className="run-status-line"><div><span className={`status-pulse ${run.status}`}/><strong>{runStatusLabel}</strong><small>运行 {run.id.slice(0,8)} · 图版本 {run.graph_snapshot_id?.slice(0,8)}</small></div>{badge(run.status)}</div>
    {run.execution_status && <section className="business-block" aria-live="polite"><strong>执行状态：{({queued:'排队中',running:'调查中',waiting_input:'等待确认',completed:'已结束',cancelled:'已取消',timed_out:'已超时',interrupted:'执行中断',failed:'因错误停止'} as Record<string,string>)[run.execution_status] || run.execution_status}</strong><p>诊断结果：{({root_cause_supported:'✓ 根因已定位',partial:'△ 已发现部分异常，完整根因未解决',unresolved:'？证据不足，根因未解决',no_fault_observed:'✓ 已执行的授权检查中未观察到故障'} as Record<string,string>)[run.diagnosis_status] || '尚未判定'}</p>
      <small>调查编号 {run.id} · 任务结束不等于根因确认。</small>
      {['queued','running'].includes(run.execution_status) && <button className="btn" onClick={() => void api(`/runs/${run.id}/cancel`,{method:'POST',body:'{}'}).catch(e=>setCancelError(e.message))}>取消本次调查</button>}
      {cancelError && <p className="alert error">{cancelError}</p>}
      {run.diagnosis_result?.required_gaps?.length > 0 && <details open><summary>必须补齐的证据（{run.diagnosis_result.required_gaps.length} 项）</summary>{run.diagnosis_result.required_gaps.map((g:Row,i:number)=><p key={i}>{g.requirement_id} · {g.rule_id}</p>)}</details>}
      {run.diagnosis_result?.model_unknowns?.length > 0 && <details><summary>模型提出的未知项</summary>{run.diagnosis_result.model_unknowns.map((s:string,i:number)=><p key={i}>{s}</p>)}</details>}
      {run.clarification?.question_id && run.execution_status==='waiting_input' && <RuntimeQuestion key={run.clarification.question_id+':'+run.clarification.version} question={run.clarification} onResume={onResume} onScope={onScope}/>}
    </section>}
    <div className="target-binding"><strong>本次调查的服务器目标</strong>{run.target_binding_status === 'unknown' ? <span>历史运行：来源未知，未保存目标绑定</span> : <span>{run.runtime_target?.name || '目标名称未保存'} · {run.runtime_target?.host || '地址未知'}{run.runtime_target?.port ? `:${run.runtime_target.port}` : ''} · {run.runtime_target?.execution_mode || '执行方式未知'} · {run.target_id}</span>}</div>
    {summary.task_objective && <div className="business-block task-progress"><strong>{taskLabels[summary.task_objective.task_type] || '调查任务'}：{taskStatusLabels[summary.task_completion_status] || taskStatusLabels.in_progress}</strong><p>{findingLabel || '根因尚未确认'} · 停止原因：{stopReasonLabels[summary.stop_reason] || summary.stop_reason || '—'}</p><p>检查进度：{summary.completed_checks?.length || 0} 项完成 · {summary.unavailable_checks?.length || 0} 项不可用 · {summary.remaining_checks?.length || 0} 项未完成{summary.progress_summary ? ` · 连续无进展 ${summary.progress_summary.consecutive_no_progress_rounds || 0} 轮` : ''}</p>{summary.unavailable_checks?.length > 0 && <small>不可用：{summary.unavailable_checks.map((check:Row) => check.check_type).join('、')}</small>}{summary.remaining_checks?.length > 0 && <small>待检查：{summary.remaining_checks.map((check:Row) => check.check_type).join('、')}</small>}{summary.task_objective.scope_omission_count > 0 && <small>本轮目标服务超过 20 个容量上限，未纳入 {summary.task_objective.scope_omission_count} 个服务；结果保留为范围缺口。</small>}{summary.task_objective.task_type === 'health_check' && <small>“范围内未观察到异常”只描述已执行的只读检查，不代表业务 HTTP 一定可用。</small>}</div>}
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
    <div className="diagnosis-summary"><div className="summary-label"><span>诊断摘要</span><span>{run.evidence?.length || 0} 条当前证据</span></div><p>{summary.model_error?.message || summary.summary || '本次运行没有可展示的摘要。'}</p>{summary.next_action && <div className="next-action"><span>建议的下一步</span><p>{summary.next_action}</p></div>}<div className="gate-note"><b>证据核验：</b>{gate.rationale || '无支持结论。'}　当前冲突：{gate.open_conflict_count ?? 0}</div>{findingLabel && <div className="gate-note"><b>Runtime 判定：</b>{findingLabel}{summary.runtime_outcome?.direct_faults?.length > 0 && <span> · {summary.runtime_outcome.direct_faults.map((fault: Row) => `${fault.service || '服务'} ${fault.state}`).join('、')}</span>}</div>}</div>
    <div className="business-block"><p className="muted">Agent 阶段：{summary.stage === 'stopped' ? '已停止' : summary.stage === 'completed' ? '已完成' : summary.stage === 'awaiting_review' ? '等待范围审核' : summary.stage||run.status} · 模型：{summary.model_status === 'failed' ? '调用失败' : summary.model_status === 'offline' ? '离线' : summary.model_status || '历史记录未提供'} {summary.decision_protocol && `· 决策协议 ${summary.decision_protocol}`} {summary.round&&`· 第 ${summary.round} 轮`} · 数据来源：{summary.connector_types?.join('、')||'查看下方工具审计'}</p><ModelError error={summary.model_error}/>{(summary.runtime_decision_audit || []).filter((audit: Row) => audit.rejected_hypothesis_count > 0 || audit.runtime_action_override).map((audit: Row, index: number) => <div className="alert" key={index}>{audit.rejected_hypothesis_count > 0 && <>局部推理假设已隔离：{audit.rejected_hypothesis_count} 项；{audit.rejected_hypotheses?.map((item: Row) => `${item.path}（${item.type}）`).join('、')}</>}{audit.runtime_action_override && '结束建议被 Runtime 覆盖；为完成未核实的授权检查，继续执行了当前优先级最高的只读候选。'}</div>)}{summary.decision_audit?.rejected_relation_count > 0 && <div className="alert">局部证据关系已隔离：拒绝 {summary.decision_audit.rejected_relation_count} 条；{summary.decision_audit.validation_errors?.map((e:Row) => `${e.path}（${e.type}）`).join('、')}{summary.decision_audit.consecutive_error_rounds >= 2 ? '；连续两轮异常，已停止后续工具调用' : ''}</div>}{summary.unresolved_reason&&!summary.model_error&&<p className="alert error">未解决原因：{summary.unresolved_reason}</p>}{budget&&<p>输入 {budget.input_tokens} · 输出 {budget.output_tokens} Tokens · 模型 {budget.calls}/{budget.limits.max_calls} 次 · 工具 {budget.tool_calls}/{budget.limits.max_tools} 次 · 读取 {budget.retrieved_bytes} bytes {budget.stop_reason&&`· 停止：${budget.stop_reason}`}</p>}{budget?.usage_records.map((u:Row)=><ModelAttempt key={u.id} usage={u}/>)}<details><summary>每次实际执行的工具与策略结果</summary>{run.tool_calls?.map((c:Row)=><div className="business-row" key={c.id}><strong>{c.tool_id}</strong>{badge(c.status)}<small>{fmtDate(c.started_at)} · {c.policy_version} · {c.error_code||c.id}</small>{c.actual_target_id && <small>实际目标：{c.actual_target?.name || '名称未知'} · {c.actual_target?.host || '地址未知'} · {c.actual_target?.execution_mode || '执行方式未知'} · {c.actual_target_id}</small>}{c.diagnostic?.error_code && <small>{c.diagnostic.message} · 阶段 {c.diagnostic.failure_stage} · 行 {c.diagnostic.output_bytes} 字节 · 异常 {c.diagnostic.invalid_item_count} 项</small>}</div>)}</details></div>
    <div className="run-section-title"><h3>假设与证据</h3><span>{run.hypotheses?.length || 0} HYPOTHESES</span></div>
    {!!summary.hypothesis_history?.length&&<details className="business-block"><summary>查看每轮假设与反证</summary>{summary.hypothesis_history.map((h:Row)=><div className="business-row" key={h.hypothesis_id+'-'+h.round}><strong>第 {h.round} 轮 · {h.failure_mode}</strong>{badge(h.status)}<p>{h.prediction}</p><small>支持 {h.support_evidence?.length||0} · 反证 {h.counter_evidence?.length||0}{h.missing_evidence?.length?` · 尚缺：${h.missing_evidence.join('、')}`:''}</small></div>)}</details>}
    {!!summary.scope_requests?.length&&<div className="alert">需要人工确认额外范围：{summary.scope_requests.map((r:Row)=>`${r.service}（${r.reason}）`).join('；')}</div>}
    <div className="hypothesis-list">{(run.hypotheses || []).map((h: Row) => <article className="hypothesis" key={h.id}><div className="hypothesis-head"><div><span className="hypothesis-num">H</span><strong>{h.failure_mode}</strong></div>{badge(h.status)}</div><p>{h.falsifiable_prediction}</p><div className="causal-chain">{(JSON.parse(h.causal_chain_json || '[]') as string[]).map((x, i) => <React.Fragment key={i}><span>{x}</span>{i < JSON.parse(h.causal_chain_json || '[]').length-1 && <b>→</b>}</React.Fragment>)}</div><div className="hypothesis-links">{h.evidence_links?.map((e:Row)=><p key={e.evidence_id+e.stance}><b>{e.stance==='support'?'支持':'反证'}</b> · {e.evidence_id.slice(0,12)} · {e.explanation}</p>)}</div></article>)}</div>
    <div className="run-section-title"><h3>逐轮诊断</h3><span>{run.diagnostic_steps?.length || 0} STEPS</span></div>
    <div className="iteration-list">{(run.diagnostic_steps || []).map((step: Row) => { const action = step.action || {}; const executed = action.executed_action; const proposed = action.proposed_action; const actionLabel = executed ? `${executed.tool_id} · ${executed.service} (${executed.role})` : proposed && step.review_outcome === 'supported' ? '证据门已通过，未执行后续动作' : proposed && action.policy_outcome === 'rejected' ? '建议检查被策略拒绝' : proposed ? '建议的检查未执行' : '本轮未提出额外检查'; return <article className="iteration-card" key={step.id}><div><strong>第 {step.sequence_no} 轮 · {badge(step.review_outcome)}</strong><small>置信分 {Number(step.score_before).toFixed(2)} → {Number(step.score_after).toFixed(2)}</small></div><p>{actionLabel}</p>{proposed?.rationale && <small>{proposed.rationale}</small>}<small>{action.evidence_ids?.length || 0} 条用于复核的证据{action.tool_status ? ` · 工具 ${action.tool_status}` : ''}</small></article> })}</div>
    {summary.pattern_matches && <><div className="run-section-title"><h3>可迁移检查建议</h3><span>历史经验不能替代本次证据</span></div><div className="pattern-match-list">{summary.pattern_matches.length === 0 ? <p className="muted">没有适用于当前业务、版本和角色的已审核经验。</p> : summary.pattern_matches.map((m: Row) => <article className="pattern-match" key={m.match_id}><div><strong>{m.pattern.pattern_key} · v{m.pattern.version}</strong><span>{badge(m.decision)}　匹配分 {Number(m.score).toFixed(2)}</span><p>角色绑定：{(m.binding?.roles || []).join(' · ')}{m.executed_check_count == null ? '' : ` · 本次按经验建议采集 ${m.executed_check_count} 项`}</p><div className="match-checks">{(m.pattern.checks || []).map((c: Row, i: number) => <span key={i}>{i+1}. {c.tool_id}{c.target_role ? ` · ${c.target_role}` : ''}</span>)}</div></div>{m.decision === 'suggested' && <div className="match-actions"><div><small>检查已在当前目标验证？</small><button className="btn small" disabled={m.validation_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {validation:'validated'})}>是</button><button className="btn small" disabled={m.validation_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {validation:'failed'})}>否</button></div><div><small>对本次调查有帮助？</small><button className="btn small" disabled={m.usefulness_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {usefulness:'useful'})}>有帮助</button><button className="btn small" disabled={m.usefulness_outcome !== 'pending'} onClick={() => onMatchOutcome(m.match_id, {usefulness:'not_useful'})}>没有</button></div><span>复核：{m.validation_outcome} · 有用性：{m.usefulness_outcome}</span></div>}</article>)}</div></>}
    <div className="run-section-title"><h3>审计时间线</h3><span>{run.events?.length || 0} EVENTS</span></div><div className="timeline">{(run.events || []).map((event: Row) => <div className="timeline-item" key={event.id}><i/><div><strong>{event.event_type}</strong><small>{fmtDate(event.created_at)} · {event.actor}</small></div></div>)}{(run.evidence || []).map((ev: Row) => <div className="timeline-item evidence-item" key={ev.id}><i/><div><strong>{ev.tool_id} · {ev.freshness_state}</strong><small>{fmtDate(ev.observed_at)} · {ev.content_hash.slice(0,12)}{ev.truncated ? ' · truncated' : ''}</small>{ev.payload.text && <pre>{ev.payload.text.slice(-1000)}</pre>}{ev.payload.containers && <pre>{ev.payload.containers.map((x: Row) => `${x.service || x.name}: ${x.state}`).join('\n')}</pre>}</div></div>)}</div>
    {run.status === 'supported' && <form className="repair-form" onSubmit={onRepair}><div className="run-section-title"><h3>记录人工确认与修复</h3><span>创建待审核经验</span></div><label>修复记录<textarea name="repair" required minLength={5} rows={2} placeholder="说明人工执行的修复内容；Agent 不会执行写操作"/></label><label>验证结果<textarea name="verification" required minLength={5} rows={2} placeholder="说明恢复后的验证请求、时间与观察结果"/></label><label>只读健康检查 URL（须在 ALLOWED_HEALTH_URLS 中）<input name="verification_url" required defaultValue={verificationDefault}/></label><label className="check-label"><input type="checkbox" name="confirmed" required/> 我已复核诊断结论、人工修复和后续验证</label><button className="btn primary">验证恢复结果并保存待审核经验</button></form>}
  </div>
}
function Funnel({ metrics }: {metrics: Row}) {
  const values = [["检索匹配", metrics.patterns_retrieved || 0], ["达到建议门槛", metrics.patterns_suggested || 0], ["验证通过", metrics.patterns_validated || 0], ["被确认有用", metrics.patterns_useful || 0]] as [string, number][]
  const max = Math.max(1, ...values.map(x => x[1]))
  return <div className="funnel">{values.map(([name, value], i) => <div className="funnel-row" key={name}><span>{name}</span><div className="funnel-track"><i style={{width: `${Math.max(value ? 12 : 3, value/max*100)}%`, opacity: 1-i*.15}}/></div><b>{value}</b></div>)}<div className="funnel-rate"><span>建议通过率</span><strong>{metrics.pattern_suggestion_rate == null ? '—' : `${Math.round(metrics.pattern_suggestion_rate*100)}%`}</strong></div></div>
}
function Definition({ name, text }: {name:string;text:string}) { return <div className="definition"><strong>{name}</strong><p>{text}</p></div> }

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>)
