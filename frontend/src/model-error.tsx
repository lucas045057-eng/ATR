type Row = Record<string, any>

const stages: Record<string, string> = {
  initialize: '客户端初始化', request: '供应商请求', parse: 'JSON 解析',
  validate: '结构与引用校验', worker: '隔离进程等待', budget: '预算检查',
}

const budgetReasons: Record<string, string> = {
  request_input_limit: '单次输入预检查上限', model_context_window: '模型上下文容量',
  total_token_limit: '调查累计 Token 预算', model_call_limit: '模型调用次数',
  tool_call_limit: '工具调用次数', retrieval_limit: '资料读取量',
  execution_time_limit: '调查执行时间', provider_usage_exceeded_reservation: '供应商用量超过预留预算',
  target_candidate_limit: '本轮服务节点候选容量',
}

const validationTypes: Record<string, string> = {
  missing: '缺少必填字段', extra_forbidden: '存在不允许的额外字段',
  string_type: '必须是字符串', float_type: '必须是数值', int_type: '必须是整数',
  bool_type: '必须是布尔值', list_type: '必须是数组', dict_type: '必须是对象',
  literal_error: '不在允许的枚举值内', greater_than_equal: '低于允许下限',
  less_than_equal: '超过允许上限', too_short: '未达到最小长度', too_long: '超过最大长度',
  unknown_target: '不在本轮允许引用的服务节点集合中', target_role_mismatch: '角色与目标节点不匹配',
  unknown_evidence: '证据编号不在当前证据中', unknown_prediction_target: '预测目标不在本轮允许引用的服务节点集合中',
  prediction_tool_not_allowed: '预测工具不在白名单中', invalid_prediction: '预测的工具、字段、比较方式或取值组合无效',
  action_not_allowed: '动作不在本轮授权候选中', finite_number: '必须是有限数值',
  counterevidence_target_mismatch: '反证必须属于该假设的目标节点；依赖故障请单独建立假设',
}

function ValidationIssues({ error }: { error: Row }) {
  if (error.code !== 'model_schema_error' && error.error_category !== 'model_schema_error') return null
  if (!error.validation_errors?.length) return <p>此记录未保存字段级诊断，无法确认具体失败字段。</p>
  return <div data-testid="validation-fields">
    <p>{error.validation_kind === 'decision' ? '模型决策校验失败' : '格式校验失败'}；已停止后续工具调用。</p>
    <ul>{error.validation_errors.map((issue: Row, index: number) => <li key={index}>
      <code>{issue.path}</code>：{validationTypes[issue.type] || '校验失败'}
      {' '}（{issue.type} · {stages[issue.stage] || '结构与引用校验'}）
    </li>)}</ul>
    <p>仅显示安全字段路径和错误类型，未记录模型响应内容。</p>
  </div>
}

export function ModelError({ error }: { error?: Row | null }) {
  if (!error) return null
  return <div className="alert error model-error" role="alert" data-testid="model-error">
    <strong>模型调用失败：{error.message}</strong>
    <p>模型：{error.model || '历史记录未提供'} · 阶段：{stages[error.stage] || error.stage || '未知'}
      {' '}· 原始异常：{error.exception_type || '未知'}
      {error.status_code != null && ` · HTTP ${error.status_code}`}</p>
    <ValidationIssues error={error} />
    {error.code === 'model_budget_exceeded' && error.budget_reason && <p data-testid="budget-error-details">
      触发限制：{budgetReasons[error.budget_reason] || '预算预检查'}。
      {error.estimated_input_tokens != null && <> 本地输入估算 {error.estimated_input_tokens}（UTF-8 字节加固定开销）。</>}
      {error.input_limit != null && <> 单次输入上限 {error.input_limit}。</>}
      {error.budget_reason === 'target_candidate_limit' && <>
        {' '}服务节点 {error.candidate_count} 个，上限 {error.candidate_limit} 个；候选未被截断，模型请求未发送。请由操作者审核调查范围。
      </>}
    </p>}
    <p>已有证据和假设历史已保留；未执行额外未授权工具；未自动重试。</p>
    <p>可由操作员手动重新调查，可能产生额外费用。</p>
    <small>内部尝试编号：{error.request_attempt_id || '未发起请求'}
      {' '}· 供应商请求编号：{error.provider_request_id || '未提供'}
      {error.elapsed_ms != null && ` · 耗时 ${error.elapsed_ms} ms`}</small>
  </div>
}

export function ModelAttempt({ usage }: { usage: Row }) {
  const audit = usage.diagnostic || {}
  return <details className="muted">
    <summary>{usage.model} · {usage.status === 'failed' ? '调用失败' : usage.status === 'interrupted' ? '已中断' : usage.status}
      {' '}· {usage.usage_kind === 'actual' ? '供应商实际 usage（不代表实付费用）' : 'Token 估算'}
      {' '}· {usage.input_tokens}+{usage.output_tokens} Tokens
      {' '}· 费用 {usage.cost == null ? '未知' : `估算 $${usage.cost.toFixed(6)}`}</summary>
    {audit.request_attempt_id ? <p>内部尝试编号 {audit.request_attempt_id} · 重试 {audit.retry_count}
      {' '}· {audit.started_at} → {audit.ended_at || (usage.status === 'pending' ? '进行中' : '实际结束时间未知')}
      {' '}· 总耗时 {audit.elapsed_ms ?? '未知'} ms
      {' '}· 进程开销 {audit.process_overhead_ms ?? '未知'} ms
      {' '}· 初始化 {audit.worker_initialize_ms ?? '未知'} ms
      {' '}· 网络请求 {audit.network_ms ?? '未知'} ms
      {' '}· 解析/校验 {audit.parse_ms ?? '未知'} ms</p> : <p>历史记录没有调用诊断信息。</p>}
    <ValidationIssues error={audit} />
  </details>
}
