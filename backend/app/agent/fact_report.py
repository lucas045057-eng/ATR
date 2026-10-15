"""Deterministic observations, not model prose, describe the final ab_v1 evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent.decision_runtime import facts_from_state
from app.agent.log_events import normalize_log_events, parse_time
from app.agent.mechanisms import FAILED
from app.tools.redaction import redact_text

BASELINE_TOOLS = {'service.status', 'container.status', 'process.status',
                  'service.logs', 'container.logs', 'log.tail', 'health.get'}


def check_id(item):
    tool = item['tool_id']
    kind = 'logs' if tool == 'log.tail' else tool.rsplit('.', 1)[-1]
    if tool == 'health.get':
        endpoint = item.get('arguments', {}).get('endpoint_id') or item.get('payload', {}).get('endpoint_id')
        kind = 'health:' + str(endpoint)
    return item['target_node_id'] + ':' + kind


def evidence_quality(evidence, state, *, ssh=False):
    if not evidence:
        return 'missing', '未保存成功的取证结果'
    if evidence.get('freshness_state', 'current') != 'current':
        return 'expired', '取证记录已标记过期'
    observed = parse_time(evidence.get('observed_at'))
    window = parse_time(state['incident'].get('window_start'))
    if not observed or not window:
        return 'insufficient_for_gate', '缺少可核验的观测或事故窗口时间'
    if observed < window:
        return 'out_of_window', '观测早于本次事故窗口'
    if observed > datetime.now(timezone.utc) + timedelta(seconds=5):
        return 'insufficient_for_gate', '观测时间超前，不能作为当前证据'
    payload, tool = evidence.get('payload', {}), evidence['tool_id']
    if any(payload.get(key) for key in ('partial', 'truncated', 'timeout')):
        return 'insufficient_for_gate', '取证结果不完整、截断或超时'
    if tool not in BASELINE_TOOLS:
        return 'current', '只读资料已保存；资料本身不构成根因证明'
    if tool == 'health.get':
        if (payload.get('observation_location') != 'remote_ssh' or not payload.get('endpoint_id')
                or not (type(payload.get('status_code')) is int or state.get('investigation_goal')
                        and payload.get('reachable') is False and payload.get('failure_kind') == 'connection_refused')
                or payload.get('redirected')):
            return 'insufficient_for_gate', '缺少原目标的精确 HTTP 观测身份'
        return 'current', '原授权端点的响应事实，不能单独证明根因'
    if ssh and ((not payload.get('startup_cycle') and not (
            state.get('investigation_goal') and tool.endswith('.status') and evidence.get('runtime_identity_verified')))
            or tool in {'service.logs', 'container.logs', 'log.tail'} and not payload.get('cycle_verified')):
        return 'insufficient_for_gate', '当前启动身份尚未核验'
    if tool.endswith('.status'):
        return ('current', '当前状态观测') if isinstance(payload.get('state'), str) else (
            'insufficient_for_gate', '缺少明确的运行状态')
    text = payload.get('text')
    if not isinstance(text, str):
        return 'insufficient_for_gate', '缺少日志内容'
    events = normalize_log_events(text, {**payload, **evidence})
    if not events or any(not event.event_time_utc or event.event_time_utc > observed + timedelta(seconds=5)
                         for event in events):
        return 'insufficient_for_gate', '没有可核验的当前日志事件时间'
    started = parse_time(payload.get('started_at'))
    if not any(event.event_time_utc >= window and (not started or event.event_time_utc >= started)
               for event in events):
        return 'out_of_window', '日志事件均早于事故窗口或当前启动'
    return 'current', '仅窗口内、当前启动的日志事件参与事实核验'


def build_fact_report(state, result, history, *, model_error=None, ssh=False):
    scope = set(state['scope_node_ids'])
    names = {node['id']: node['name'] for node in state['graph']['nodes']}
    evidence_by_id = {e['evidence_id']: e for e in state['evidence'] if e.get('evidence_id')}
    facts = [fact for fact in facts_from_state(state) if fact['target_node_id'] in scope
             and fact['freshness'] == 'current' and fact['independent_observation']]
    facts = [{**fact, 'service': names.get(fact['target_node_id'])} for fact in facts]
    for fact in facts:
        if fact['kind'] in {'http_status', 'endpoint_reachability'}:
            payload = evidence_by_id[fact['evidence_id']]['payload']
            fact.update(endpoint_id=payload.get('endpoint_id'), url=payload.get('url'))
    direct = [fact for fact in facts if fact['kind'] == 'service_state' and fact['value'] in FAILED
              or fact['kind'] == 'http_status' and fact['value'] >= 400
              or fact['kind'] == 'endpoint_reachability' and fact['value'] is False
              or fact['kind'] == 'log_observation' and (fact['value'].get('failure_signal')
                                                       or fact['value'].get('mechanisms'))]
    evidence_by_call = {e['call_id']: e for e in state['evidence'] if e.get('call_id')}
    checks = []
    for action in history:
        evidence = evidence_by_call.get(action.get('call_id'))
        if not evidence and not action.get('call_id') and action.get('status') == 'succeeded':
            evidence = next((e for e in reversed(state['evidence']) if e['tool_id'] == action['tool_id']
                             and e['target_node_id'] == action['target_node_id']), None)
        quality, reason = evidence_quality(evidence, state, ssh=ssh)
        checks.append({'check_id': check_id(action), 'tool_id': action['tool_id'],
                       'target_node_id': action['target_node_id'], 'service': names.get(action['target_node_id']),
                       'call_id': action.get('call_id'), 'tool_status': action['status'],
                       'error_code': action.get('error_code'), 'evidence_status': quality, 'reason': reason,
                       'evidence_id': evidence.get('evidence_id') if evidence else None,
                       'endpoint_id': action.get('arguments', {}).get('endpoint_id'),
                       'url': evidence.get('payload', {}).get('url') if evidence else None,
                       'observed_at': evidence.get('observed_at') if evidence else None})
    attempted = {check['check_id'] for check in checks}
    for requirement in state.get('baseline_checks', []):
        if requirement not in attempted:
            node, kind = requirement.split(':', 1)
            checks.append({'check_id': requirement, 'target_node_id': node, 'service': names.get(node),
                           'tool_id': kind, 'tool_status': 'not_attempted', 'evidence_status': 'missing',
                           'call_id': None, 'evidence_id': None, 'observed_at': None,
                           'endpoint_id': kind.removeprefix('health:') if kind.startswith('health:') else None,
                           'reason': '未执行：没有当前授权候选，或执行已受预算、时间、取消约束停止'})
    counts = {status: sum(check['tool_status'] == status for check in checks)
              for status in ('succeeded', 'denied', 'failed', 'timed_out', 'not_attempted')}
    states = '、'.join(names.get(fact['target_node_id'], fact['target_node_id']) + '=' + fact['value']
                      for fact in facts if fact['kind'] == 'service_state')
    status = result['diagnosis_status']
    stopped = any(f['kind'] == 'service_state' and f['value'] in {'exited', 'inactive', 'dead'} for f in direct)
    verdict = {'root_cause_supported': ('当前调查范围内的因果主张已通过独立证据核验。' if result.get('investigation_goal') else
                                       '完整故障链已通过原有独立证据核验。'),
               'direct_cause_supported': ('已确认相关实例已停止；该实例无法提供服务。停止的触发原因尚未确认。' if stopped else
                                          '已确认相关实例处于异常状态，无法正常提供服务；触发原因尚未确认。'),
               'no_fault_observed': '已执行的当前授权检查中未观察到故障。',
               'partial': '已观察到直接异常；完整根因尚未通过证据核验。',
               'unresolved': '当前证据不足，故障原因未确认。'}[status]
    if result.get('impact_scope', {}).get('entry_impact_verified'):
        urls = '、'.join(e['url'] for e in result['impact_scope']['endpoints'])
        verdict += f' 当前端口配置确认该实例唯一直接承载入口 {urls}，实例停止足以解释该入口不可用。'
    summary = (verdict + (' 状态：' + states + '。' if states else '')
               + f"只读取证成功 {counts['succeeded']} 项、拒绝 {counts['denied']} 项、"
               f"失败或超时 {counts['failed'] + counts['timed_out']} 项；保存 {len(facts)} 条当前独立事实。"
               )
    if model_error:
        summary += ' 模型分析不可用，事实报告由 Runtime 生成，未自动追加模型请求。'
    scope_reasons = {
        'business_object_authorization_required': '当前业务没有身份已验证的对象授权；在独立待审核区域完成首次授权后再调查。',
        'authorized_identity_not_verified': '已授权对象的当前身份未通过验证；可能已消失、身份变化或发现证据不完整。',
        'business_authorization_changed': '调查期间授权被撤销或变化；已停止后续读取，迟到结果不会保存为证据。',
        'task_service_ambiguous': '任务涉及多个已授权对象，尚未设置明确的默认主服务；仅报告已执行范围。',
        'profile_changed': '调查期间业务资料版本发生变化，需按新资料重新调查。',
    }
    missing = [*[{**gap, **({'reason': scope_reasons[gap['requirement_id']]} if gap.get('requirement_id') in scope_reasons else {})}
                 for gap in result['required_gaps']], *[
        {'requirement_id': check['check_id'], 'target_ids': [check['target_node_id']],
         'tool_status': check['tool_status'], 'evidence_status': check['evidence_status'], 'reason': check['reason']}
        for check in checks if check['tool_status'] != 'succeeded' or check['evidence_status'] != 'current']]
    unknowns = [] if status in {'root_cause_supported', 'no_fault_observed'} else ['故障触发原因尚未通过独立证据确认。']
    if result.get('investigation_goal') and not result.get('impact_scope', {}).get('entry_impact_verified'):
        unknowns.append('业务入口的全部副本及承载关系尚未完整核验；影响范围限于已观测对象。')
    unknowns += ['模型提出的待核实项（非已确认原因）：' + redact_text(str(item))
                 for item in result.get('model_unknowns', [])]
    for ambiguity in result.get('critical_ambiguities', []):
        reason = '关键资料冲突尚未解除：' + redact_text(str(ambiguity.get('subject_key', ''))[:160]
                    + ' · ' + str(ambiguity.get('predicate', ''))[:160])
        unknowns.append(reason)
        missing.append({'requirement_id': 'resolve_critical_ambiguity',
                        'conflict_id': ambiguity.get('id'), 'target_ids': ambiguity.get('related_node_ids', []),
                        'reason': reason})
    next_steps = []
    if missing:
        next_steps.append('核对列出的权限、时间窗、启动身份和未采集证据；仅在明确授权后补充检查。')
    if status == 'direct_cause_supported':
        next_steps.append('直接故障定位已完成；如需追查停止的触发原因，可显式发起深入调查。')
    elif status not in {'root_cause_supported', 'no_fault_observed'}:
        next_steps.append('当前状态或 HTTP 异常不能单独证明触发原因，需补齐原 Evidence Gate 要求的独立证据。')
    if model_error:
        next_steps.append('模型恢复后由操作者另行决定是否发起新推理；本次离线流程不会自动收费重试。')
    directions = [{'direction': segment['rule_id'], 'evidence_level': 'verified_segment',
                   'evidence_ids': sorted({item['evidence_id'] for item in segment.get('supports', [])}),
                   'root_cause_verified': status == 'root_cause_supported'}
                  for segment in result.get('verified_segments', [])]
    for fact in direct:
        value = str(fact['value']) if fact['kind'] != 'log_observation' else '日志包含当前异常信号'
        directions.append({'direction': f"{fact.get('service') or fact['target_node_id']}：{value}",
                           'evidence_level': 'direct_observation', 'evidence_ids': [fact['evidence_id']],
                           'root_cause_verified': False})
    advice = []
    if direct:
        advice.append('人工可依据当前状态和日志核对受影响组件的启动、配置及依赖；涉及修改或重启时另行审批。')
    if status == 'root_cause_supported':
        advice.append('人工可按已验证故障链评估修复依赖及恢复顺序，并在批准操作后重新采集状态验证效果。')
    if not advice:
        advice.append('先补齐列出的授权或证据缺口，再决定是否需要修复；当前报告不支持直接执行修复。')
    return {'schema_version': 'ab-fact-report-1', 'summary': summary, 'diagnosis_status': status,
            'investigation_goal': result.get('investigation_goal'),
            'direct_explanations': result.get('direct_explanations', []),
            'impact_scope': result.get('impact_scope', {}),
            'trigger_cause_status': result.get('trigger_cause_status', 'unknown'),
            'confirmed_facts': facts, 'direct_anomalies': direct, 'checks': checks,
            'fault_directions': directions, 'root_cause_verified': status == 'root_cause_supported',
            'repair_advice': advice,
            'unknown_causes': list(dict.fromkeys(unknowns)),
            'missing_evidence': missing, 'next_steps': next_steps, 'model_error': model_error,
            'source': 'runtime_evidence', 'is_model_root_cause_claim': False}
