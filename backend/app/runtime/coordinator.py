"""API-owned leases and atomic lifecycle transitions for ab_v1.

Workers never import this module or receive a Database. A terminal outcome is
durable before reclamation, but capacity is released only after process reaping.
"""
from __future__ import annotations

import json
import secrets
import threading
import time

from app.common import dumps, now, uid
from app.context.budget import interrupt_usage
from app.tools.registry import sanitize_payload

ACTIVE = {'queued', 'running', 'waiting_input'}
TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'interrupted'}
DIAGNOSES = {'root_cause_supported', 'direct_cause_supported', 'partial', 'unresolved', 'no_fault_observed'}
COMPLETIONS = {'root_cause_supported', 'direct_cause_supported', 'no_fault_observed'}


def unresolved_result():
    return {'schema_version': 'ab-result-1', 'diagnosis_status': 'unresolved',
            'completion_satisfied': False, 'verified_segments': [], 'required_gaps': []}


class RunCoordinator:
    def __init__(self, database, *, clock=time.monotonic, max_workers=3, stagnation_seconds=180, operation_seconds=90):
        self.db, self.clock, self.max_workers = database, clock, max_workers
        self.stagnation_seconds = stagnation_seconds
        self.operation_seconds = operation_seconds
        self._signals: dict[str, threading.Event] = {}
        self._progress: dict[str, dict] = {}
        self._operations: dict[str, dict] = {}
        self._lock = threading.RLock()

    def _observe_progress(self, tx, row, stamp):
        # Only persisted backend assessments can certify progress, never worker prose or evidence counts.
        result = json.loads(row['result_json'])
        summary = json.loads(tx.execute('SELECT summary_json FROM diagnosis_runs WHERE id=?', (row['run_id'],)).fetchone()[0])
        milestones = {('diagnosis', result.get('diagnosis_status', 'unresolved'))}
        for segment in result.get('verified_segments', []):
            if segment.get('status') in {'supported', 'refuted'}:
                milestones.add(('segment', segment.get('rule_id'), segment['status'],
                                dumps(segment.get('participants', {}))))
        for fault in result.get('direct_faults', []):
            milestones.add(('fault', fault.get('kind'), fault.get('target_node_id'), dumps(fault.get('value'))))
        if summary.get('scope_validation', {}).get('stage') == 'verified':
            milestones.add(('scope', 'verified'))
        gaps = {dumps({key: gap.get(key) for key in ('rule_id', 'target_ids', 'requirement_id')})
                for gap in result.get('required_gaps', [])}
        previous = self._progress.get(row['run_id'])
        if previous is None or previous['epoch'] != row['epoch']:
            self._progress[row['run_id']] = {'epoch': row['epoch'], 'seen': milestones, 'gaps': gaps}
            return
        milestones.update(('resolved_gap', gap) for gap in previous['gaps'] - gaps)
        if milestones - previous['seen']:
            tx.execute('UPDATE run_executions SET progress_at=? WHERE run_id=?', (stamp, row['run_id']))
            row['progress_at'] = stamp
        previous['seen'].update(milestones)
        previous['gaps'] = gaps

    def _timeout_reason(self, row, stamp):
        if row['execution_status'] == 'queued' and stamp - row['queued_since'] >= 30:
            return 'queue_timeout'
        if row['execution_status'] == 'running':
            if stamp - row['heartbeat_at'] >= 15:
                return 'worker_liveness_timeout'
            operation = self._operations.get(row['run_id'])
            if operation and operation['epoch'] == row['epoch'] and stamp >= operation['deadline']:
                return 'operation_timeout'
            if stamp - row['progress_at'] >= self.stagnation_seconds:
                return 'step_stagnation_timeout'
        return None

    def _expire(self, tx, row, stamp):
        reason = self._timeout_reason(row, stamp)
        if row['execution_status'] == 'running' and reason in {None, 'step_stagnation_timeout'}:
            self._observe_progress(tx, row, stamp)
            reason = self._timeout_reason(row, stamp)
        if reason:
            result = (self._operations[row['run_id']]['result'] if reason == 'operation_timeout'
                      else json.loads(row['result_json']))
            self._finish(tx, row, 'timed_out', result, reason, stamp)
        return bool(reason)

    def cancellation(self, run_id):
        with self._lock:
            return self._signals.setdefault(run_id, threading.Event())

    def _row(self, tx, run_id):
        row = tx.execute('SELECT * FROM run_executions WHERE run_id=?', (run_id,)).fetchone()
        if not row:
            raise KeyError('此调查没有 ab_v1 执行记录')
        return dict(row)

    def _elapsed(self, row, stamp):
        return row['elapsed_seconds'] + (max(0, stamp - row['segment_started'])
               if row['segment_started'] is not None else 0)

    def attach_process(self, run_id, lease_token, owner):
        with self._lock, self.db.transaction() as tx:
            row = self._row(tx, run_id)
            if row['execution_status'] != 'running' or not row['accepting'] or row['lease_token'] != lease_token:
                raise InterruptedError('调查租约已撤销')
            owners = json.loads(row['process_owners_json'])
            owners.append(owner)
            tx.execute('UPDATE run_executions SET process_owners_json=? WHERE run_id=?', (dumps(owners), run_id))

    def _event(self, tx, run_id, kind, payload):
        run = tx.execute('SELECT i.system_id FROM diagnosis_runs r JOIN incidents i ON i.id=r.incident_id WHERE r.id=?', (run_id,)).fetchone()
        sequence = tx.execute('SELECT COALESCE(MAX(sequence_no),0)+1 FROM events WHERE run_id=?', (run_id,)).fetchone()[0]
        tx.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (uid(), run['system_id'], run_id, sequence, 'run', run_id, kind, 'runtime', dumps(payload), now()))

    def register(self, run_id, job_id, *, max_seconds=600):
        with self._lock, self.db.transaction() as tx:
            self.register_in_transaction(tx,run_id,job_id,max_seconds=max_seconds)
        return self.public(run_id)

    def register_in_transaction(self, tx, run_id, job_id, *, max_seconds=600):
        if not 0 < max_seconds <= 600:
            raise ValueError('调查总时间上限必须在 0 到 600 秒之间')
        old = tx.execute('SELECT 1 FROM run_executions WHERE run_id=?', (run_id,)).fetchone()
        if not old:
            run = tx.execute('SELECT model_config_json FROM diagnosis_runs WHERE id=?', (run_id,)).fetchone()
            if not run or json.loads(run['model_config_json']).get('decision_protocol') != 'ab_v1':
                raise ValueError('协调器仅接受已冻结的 ab_v1 调查')
            tx.execute("INSERT INTO run_executions(run_id,job_id,execution_status,queued_since,max_seconds,result_json) VALUES(?,?,'queued',?,?,?)",
                       (run_id, job_id, self.clock(), max_seconds, dumps(unresolved_result())))
            tx.execute("UPDATE diagnosis_runs SET status='queued' WHERE id=?", (run_id,))
            tx.execute("UPDATE jobs SET status='queued',result_json=? WHERE id=?", (dumps({'run_id': run_id}), job_id))
            self._event(tx, run_id, 'run_queued', {'queue_timeout_seconds': 30, 'max_seconds': None,
                                                'budget_mode': 'accounting_only'})

    def claim(self, run_id):
        with self._lock, self.db.transaction() as tx:
            row, stamp = self._row(tx, run_id), self.clock()
            if row['execution_status'] != 'queued':
                return None
            if stamp - row['queued_since'] >= 30:
                self._finish(tx, row, 'timed_out', unresolved_result(), 'queue_timeout', stamp)
                return None
            if tx.execute('SELECT COUNT(*) FROM run_executions WHERE worker_alive=1').fetchone()[0] >= self.max_workers:
                return None
            token = secrets.token_urlsafe(32)
            tx.execute("UPDATE run_executions SET lease_token=?,epoch=epoch+1,last_sequence=0,execution_status='running',accepting=1,worker_alive=1,segment_started=?,heartbeat_at=?,progress_at=?,last_seen_wall=? WHERE run_id=?",
                       (token, stamp, stamp, stamp, time.time(), run_id))
            tx.execute("UPDATE diagnosis_runs SET status='running',ended_at=NULL WHERE id=?", (run_id,))
            tx.execute("UPDATE jobs SET status='running',ended_at=NULL WHERE id=?", (row['job_id'],))
            self.cancellation(run_id).clear()
            self._observe_progress(tx, {**row, 'epoch': row['epoch'] + 1}, stamp)
            self._event(tx, run_id, 'run_claimed', {'epoch': row['epoch'] + 1})
            return {'run_id': run_id, 'lease_token': token, 'remaining_seconds': None,
                    'epoch': row['epoch'] + 1}

    def accept(self, message):
        if (not isinstance(message, dict) or set(message) != {'run_id', 'lease_token', 'sequence', 'type', 'payload'}
                or type(message['sequence']) is not int or message['sequence'] < 1
                or message['type'] not in {'heartbeat', 'progress', 'checkpoint'}
                or not isinstance(message['payload'], dict) or len(dumps(message).encode()) > 1024 * 1024):
            return False
        with self._lock, self.db.transaction() as tx:
            try:
                row = self._row(tx, message['run_id'])
            except KeyError:
                return False
            if (row['execution_status'] != 'running' or not row['accepting']
                    or row['lease_token'] != message['lease_token'] or row['last_sequence'] + 1 != message['sequence']):
                return False
            stamp = self.clock()
            if self._expire(tx, row, stamp):
                return False
            tx.execute('UPDATE run_executions SET last_sequence=?,heartbeat_at=?,elapsed_seconds=?,segment_started=?,last_seen_wall=? WHERE run_id=?',
                       (message['sequence'], stamp, self._elapsed(row, stamp), stamp, time.time(), row['run_id']))
            if message['type'] != 'heartbeat':
                self._event(tx, row['run_id'], 'worker_' + message['type'], sanitize_payload(message['payload']))
            if message['type'] == 'progress' and message['payload'] == {'operation': 'advance'}:
                self._operations[row['run_id']] = {'epoch': row['epoch'], 'deadline': stamp + self.operation_seconds,
                                                   'check_id': f"step:{message['sequence']}",
                                                   'result': json.loads(row['result_json'])}
            return True

    def _close_pending(self, tx, run_id, reason):
        stamp = now()
        tx.execute("UPDATE runtime_observations SET status='interrupted',error=? WHERE status='pending' AND id IN (SELECT request_id FROM tool_calls WHERE run_id=?)", (reason, run_id))
        tx.execute("UPDATE tool_calls SET status='interrupted',ended_at=?,error_code=? WHERE run_id=? AND status='pending'", (stamp, reason, run_id))
        for usage in tx.execute("SELECT u.* FROM model_usage_records u JOIN context_budgets b ON b.id=u.budget_id WHERE b.run_id=? AND u.status='pending'", (run_id,)).fetchall():
            interrupt_usage(tx, usage)

    def _finish(self, tx, row, execution, result, reason, stamp, clarification=None):
        from app.run_outcomes import root_supported
        elapsed = self._elapsed(row, stamp)
        result = sanitize_payload(result)
        pending_tool = tx.execute("SELECT id FROM tool_calls WHERE run_id=? AND status='pending' ORDER BY started_at DESC LIMIT 1", (row['run_id'],)).fetchone()
        pending_model = tx.execute("SELECT u.id FROM model_usage_records u JOIN context_budgets b ON b.id=u.budget_id WHERE b.run_id=? AND u.status='pending' ORDER BY u.created_at DESC LIMIT 1", (row['run_id'],)).fetchone()
        self._close_pending(tx, row['run_id'], reason)
        tx.execute('UPDATE run_executions SET execution_status=?,accepting=0,elapsed_seconds=?,segment_started=NULL,result_json=?,stop_reason=?,clarification_json=? WHERE run_id=?',
                   (execution, elapsed, dumps(result), reason, dumps(clarification or {}), row['run_id']))
        current = tx.execute('SELECT summary_json FROM diagnosis_runs WHERE id=?', (row['run_id'],)).fetchone()
        summary = json.loads(current['summary_json'])
        if execution in {'failed', 'cancelled', 'timed_out', 'interrupted'}:
            pending = pending_tool or pending_model
            summary['runtime_error'] = {
                'error_code': reason, 'failure_stage': 'tool' if pending_tool else 'model' if pending_model else 'supervision',
                'error_category': ('stagnation' if reason == 'step_stagnation_timeout' else
                                   'liveness' if reason == 'worker_liveness_timeout' else
                                   'deadline' if execution == 'timed_out' else
                                   'cancellation' if execution == 'cancelled' else 'interruption'),
                'check_id': pending['id'] if pending else self._operations.get(row['run_id'], {}).get('check_id', f"step:{row['last_sequence']}")}
            diagnostic = summary.get('execution_error')
            if isinstance(diagnostic, dict):
                summary['runtime_error'].update(sanitize_payload({key: diagnostic[key] for key in
                    ('code', 'stage', 'failure_stage', 'error_category', 'check_id', 'exception_type')
                    if isinstance(diagnostic.get(key), str) and diagnostic[key]}))
                summary['runtime_error']['error_code'] = diagnostic.get('code') or reason
            failure_stage = summary['runtime_error'].get('stage') or summary['runtime_error']['failure_stage']
            summary['runtime_error'].update(stage=failure_stage, failure_stage=failure_stage)
        summary.update(execution_status=execution, diagnosis_status=result['diagnosis_status'],
                       diagnosis_result=result, stop_reason=reason, elapsed_seconds=elapsed)
        timestamp=now()
        supported_at=timestamp if execution=='completed' and root_supported(result) else None
        tx.execute('UPDATE diagnosis_runs SET status=?,ended_at=?,summary_json=?,supported_at=COALESCE(supported_at,?) WHERE id=?',
                   (execution, None if execution == 'waiting_input' else timestamp, dumps(summary),supported_at,row['run_id']))
        tx.execute('UPDATE jobs SET status=?,ended_at=?,error=?,result_json=? WHERE id=?',
                   (execution, None if execution == 'waiting_input' else now(), reason if execution != 'completed' else None,
                    dumps({'run_id': row['run_id'], 'execution_status': execution, 'diagnosis_status': result['diagnosis_status']}), row['job_id']))
        self._event(tx, row['run_id'], 'run_' + execution, {'reason': reason, 'diagnosis_status': result['diagnosis_status']})
        self.cancellation(row['run_id']).set()

    def finalize(self, run_id, execution_status, result, *, lease_token=None, reason='completed'):
        if (execution_status not in TERMINAL or not isinstance(result, dict) or result.get('schema_version') != 'ab-result-1'
                or result.get('diagnosis_status') not in DIAGNOSES
                or type(result.get('completion_satisfied')) is not bool
                or result['completion_satisfied'] != (result['diagnosis_status'] in COMPLETIONS)):
            raise ValueError('执行终态或后端诊断结果无效')
        with self._lock, self.db.transaction() as tx:
            row = self._row(tx, run_id)
            if row['execution_status'] not in {'queued', 'running'} or (row['execution_status'] == 'running' and row['lease_token'] != lease_token):
                return False
            if self._expire(tx, row, self.clock()):
                return False
            self._finish(tx, row, execution_status, result, reason, self.clock())
            return True

    def cancel(self, run_id):
        with self._lock, self.db.transaction() as tx:
            row = self._row(tx, run_id)
            if row['execution_status'] in ACTIVE:
                result = json.loads(row['result_json'])
                self._finish(tx, row, 'cancelled', result, 'operator_cancelled', self.clock())
        return self.public(run_id)

    def wait_for_input(self, run_id, question, *, lease_token):
        if not question.get('question_id') or type(question.get('version')) is not int:
            raise ValueError('确认问题必须有身份和版本')
        with self._lock, self.db.transaction() as tx:
            row = self._row(tx, run_id)
            if row['execution_status'] != 'running' or row['lease_token'] != lease_token:
                return False
            if self._expire(tx, row, self.clock()):
                return False
            self._finish(tx, row, 'waiting_input', json.loads(row['result_json']), 'clarification_required', self.clock(), sanitize_payload(question))
            return True

    def reaped(self, run_id, lease_token):
        with self._lock, self.db.transaction() as tx:
            row = self._row(tx, run_id)
            if row['lease_token'] != lease_token or row['execution_status'] in {'queued', 'running'}:
                return False
            tx.execute('UPDATE run_executions SET worker_alive=0 WHERE run_id=?', (run_id,))
            self._progress.pop(run_id, None)
            self._operations.pop(run_id, None)
            return True

    def resume(self, run_id, job_id, *, question_id, version):
        with self._lock, self.db.transaction() as tx:
            row = self._row(tx, run_id)
            question = json.loads(row['clarification_json'])
            if (row['execution_status'] != 'waiting_input' or row['worker_alive']
                    or question.get('question_id') != question_id or question.get('version') != version):
                raise ValueError('确认回答已过期或原执行进程尚未回收')
            tx.execute("UPDATE run_executions SET execution_status='queued',job_id=?,queued_since=?,lease_token=NULL,last_sequence=0,stop_reason=NULL WHERE run_id=?", (job_id, self.clock(), run_id))
            tx.execute("UPDATE jobs SET status='queued',ended_at=NULL,error=NULL,result_json=? WHERE id=?", (dumps({'run_id': run_id}), job_id))
            tx.execute("UPDATE diagnosis_runs SET status='queued',ended_at=NULL WHERE id=?", (run_id,))
            self._event(tx, run_id, 'run_resumed', {'question_id': question_id, 'version': version})
        return self.public(run_id)

    def tick(self, monotonic_now=None):
        stamp = self.clock() if monotonic_now is None else monotonic_now
        with self._lock, self.db.transaction() as tx:
            for record in tx.execute("SELECT * FROM run_executions WHERE execution_status IN ('queued','running')").fetchall():
                row = dict(record)
                self._expire(tx, row, stamp)

    def recover(self):
        from app.runtime.processes import reap_owned_process

        with self._lock, self.db.transaction() as tx:
            rows = tx.execute("SELECT * FROM run_executions WHERE execution_status IN ('queued','running')").fetchall()
            for record in rows:
                row = dict(record)
                # Preserve the last active interval across an API or machine restart.
                if row['segment_started'] is not None and row['last_seen_wall'] is not None:
                    row['elapsed_seconds'] += max(0, time.time() - row['last_seen_wall'])
                row['segment_started'] = None
                self._finish(tx, row, 'interrupted', json.loads(row['result_json']), 'backend_restart', self.clock())
            for record in tx.execute('SELECT * FROM run_executions WHERE worker_alive=1').fetchall():
                owners = json.loads(record['process_owners_json'])
                if all(reap_owned_process(owner) for owner in owners):
                    tx.execute('UPDATE run_executions SET worker_alive=0,accepting=0 WHERE run_id=?', (record['run_id'],))
                else:
                    tx.execute("UPDATE run_executions SET accepting=0,stop_reason='process_cleanup_pending' WHERE run_id=?", (record['run_id'],))
            return len(rows)

    def public(self, run_id):
        with self.db.connect() as tx:
            row = self._row(tx, run_id)
        row['elapsed_seconds'] = self._elapsed(row, self.clock())
        row['historical_max_seconds'] = row['max_seconds']
        row['max_seconds'] = None
        row['budget_mode'] = 'accounting_only'
        row.pop('lease_token', None)
        row.pop('segment_started', None)
        row.pop('process_owners_json', None)
        result = json.loads(row['result_json'])
        row.update(diagnosis_status=result['diagnosis_status'], completion_satisfied=result['completion_satisfied'])
        return row


_COORDINATORS: dict[str, RunCoordinator] = {}
_COORDINATORS_LOCK = threading.Lock()


def get_coordinator(database):
    key = str(database.path.resolve())
    with _COORDINATORS_LOCK:
        return _COORDINATORS.setdefault(key, RunCoordinator(database))
