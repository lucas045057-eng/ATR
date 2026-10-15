from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from app.business.profile import claim
from app.business.service import BusinessService
from app.common import digest, dumps, now, uid
from app.connectors.diagnostics import TERMINAL_CODES
from app.connectors.models import ToolRequest
from app.connectors.service import RuntimeService
from app.connectors.unit_names import valid_service_name
from app.context.budget import BudgetExceeded, ContextBudgetManager

RUNTIME_IDENTITY_TTL_SECONDS = 86400
# Compatibility for old clarification answers; this is not an authorization expiry.
BINDING_TTL_SECONDS = RUNTIME_IDENTITY_TTL_SECONDS


def binding_is_current(binding, at=None):
    entry = dict(binding)
    try:
        observed = datetime.fromisoformat(entry.get("observed_at") or "")
        expires = datetime.fromisoformat(entry.get("expires_at") or "")
        instant = at or datetime.now(timezone.utc)
        return (entry.get("status") == "confirmed"
                and entry.get("freshness_status") == "confirmed"
                and observed.tzinfo is not None and expires.tzinfo is not None
                and observed <= instant < expires)
    except (ValueError, TypeError):
        return False


def candidate_digest(candidate):
    identity = {key: candidate.get(key) for key in ("id", "kind", "name", "image")}
    for key in ("ports", "environment_keys"):
        identity[key] = sorted(set(candidate.get(key) or []), key=str)
    return digest(identity)


INVENTORY_FIELDS = {'service.list': 'services', 'process.list': 'processes', 'container.list': 'containers'}


def discovery_requests(profile, target, include_configs=False):
    hints = [s['name'] for s in profile['services'] if valid_service_name(s['name'])][:8]
    tools = [ToolRequest(tool_id=t) for t in ("host.info", "host.memory", "host.disk", "network.listeners")]
    tools.append(ToolRequest(tool_id="service.list", arguments={"name_hints": hints}))
    if target["docker_reads_enabled"]:
        tools.append(ToolRequest(tool_id="container.list", arguments={"name_hints": hints}))
    tools.append(ToolRequest(tool_id="process.list", arguments={"name_hints": hints}))
    if include_configs:
        tools.extend(ToolRequest(tool_id="config.list", arguments={"path": p}) for p in target["allowed_paths"][:10])
    return tools


def valid_candidate(candidate, tool):
    if not isinstance(candidate, dict):
        return False
    if candidate.get('kind') != tool.split('.')[0]:
        return False
    limit = 255 if candidate.get('kind') == 'service' else 128
    if any(not isinstance(candidate.get(k), str) or not 1 <= len(candidate[k]) <= limit for k in ('id', 'name')):
        return False
    if candidate.get('kind') == 'service' and not valid_service_name(candidate['id']):
        return False
    if not isinstance(candidate.get('image', ''), str):
        return False
    ports, keys = candidate.get('ports', []), candidate.get('environment_keys', [])
    return (isinstance(ports, list) and len(ports) <= 100 and all(type(p) is int and 1 <= p <= 65535 for p in ports)
            and isinstance(keys, list) and len(keys) <= 100 and all(isinstance(k, str) and len(k) <= 128 for k in keys))


def collection_incomplete(observation):
    payload = observation.get('payload')
    if not isinstance(payload, dict) or any(payload.get(k, False) for k in ('partial', 'truncated', 'timeout')):
        return True
    field = INVENTORY_FIELDS.get(observation['tool_id'])
    return bool(field and (not isinstance(payload.get(field), list) or any(
        not valid_candidate(c, observation['tool_id'])
        for c in payload[field])))


def match_service(service, candidate):
    reasons = []
    score = 0.0

    def normalize(value):
        return re.sub(r"[^a-z0-9]", "", str(value).lower().removesuffix(".service"))

    name = normalize(service["name"])
    runtime_name = normalize(candidate.get("name", ""))
    if name and name == runtime_name:
        score += 0.65
        reasons.append("服务名称与运行对象名称一致")
    elif name and name in runtime_name:
        score += 0.4
        reasons.append("运行对象名称包含资料中的服务名")
    if service.get("container_name") and service["container_name"] == candidate.get("name"):
        score += 0.65
        reasons.append("显式 container_name 匹配")
    image = service.get("image", "").split(":")[0]
    if image and image == candidate.get("image", "").split(":")[0]:
        score += 0.3
        reasons.append("部署镜像名称一致")
    ports = set()
    for p in service.get("ports", []):
        try:
            ports.add(int(str(p).split(":")[-1].split("/")[0]))
        except ValueError:
            pass
    if ports & set(candidate.get("ports", [])):
        score += 0.2
        reasons.append("声明端口与运行元数据有交集")
    if set(service.get("environment_keys", [])) & set(candidate.get("environment_keys", [])):
        score += 0.1
        reasons.append("环境变量键名一致；未读取变量值")
    return round(min(score, 1.0), 3), reasons


class ScopeResolver:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings
        self.runtime = RuntimeService(database, settings)
        self.business = BusinessService(database, settings)

    def discover(self, business_id, target_id, observations=None, *, verification_run_id=None, allowed_grants=None):
        from app.system.authorization import authorization, authorized
        from app.system.authorization import matches as matches_rule
        profile = self.business.get(business_id)
        target = self.runtime.get(business_id, target_id)
        discovery_id = uid()
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO discovery_runs VALUES(?,?,?,'running','{}',?)",
                (discovery_id, business_id, target_id, now()),
            )
        hints = [s['name'] for s in profile['services'] if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}', s['name'])][:8]
        if observations is None:
            observations = []
            manager = ContextBudgetManager(self.db, self.settings)
            budget_id = manager.create("discovery", business_id)
            for request in discovery_requests(profile, target, include_configs=True):
                started = time.monotonic()
                try:
                    manager.consume_tool(budget_id)
                    observation = self.runtime.execute(business_id, target_id, request, timeout_cap=manager.remaining_seconds(budget_id))
                    observations.append(observation)
                    manager.retrieval(budget_id, max(observation["bytes"], observation.get("payload", {}).get("output_bytes", 0)))
                except BudgetExceeded:
                    observations.append({"tool_id": request.tool_id, "status": "failed", "error_code": "global_budget_exhausted",
                                         "message": "发现预算已用尽", "observation_id": None})
                    break
                finally:
                    manager.record_time(budget_id, time.monotonic() - started)
                if observation.get("error_code") in TERMINAL_CODES:
                    break
        candidates = []
        for observation in observations:
            if "payload" not in observation:
                continue
            field = INVENTORY_FIELDS.get(observation['tool_id'])
            if field:
                items = observation['payload'].get(field)
                if not isinstance(items, list):
                    continue
                candidates.extend(
                    (item, observation["observation_id"])
                    for item in items[:100]
                    if valid_candidate(item, observation['tool_id'])
                )
        matches, unmatched, changes = [], [], []
        by_tool = {o["tool_id"]: o for o in observations}
        by_id = {o["observation_id"]: o for o in observations if o.get("observation_id")}
        with self.db.transaction() as tx:
            existing = [dict(r) for r in tx.execute(
                "SELECT * FROM service_bindings WHERE business_id=? AND target_id=? AND status IN ('pending','confirmed','rejected')",
                (business_id, target_id))]
            grants = [b for b in existing if authorized(b, target)
                      and (allowed_grants is None or allowed_grants.get(authorization(b)['grant_id']) == digest(authorization(b)))]
            ambiguous = {b['id'] for b in grants if len({(c['kind'], c['id']) for c, _ in candidates
                         if matches_rule(authorization(b), c)}) > 1}
            for identifier in ambiguous:
                tx.execute("UPDATE service_bindings SET freshness_status='stale' WHERE id=?", (identifier,))
                changes.append({'binding_id': identifier, 'reason': 'identity_conflict', 'review_required': True})
            grants = [b for b in grants if b['id'] not in ambiguous]
            for old in existing:
                if old['status'] == 'rejected':
                    continue
                source = by_tool.get(f"{old['kind']}.list", {})
                payload = source.get("payload")
                field = {"service": "services", "process": "processes", "container": "containers"}.get(old["kind"])
                complete = not collection_incomplete(source) if source else False
                if complete and payload.get('filtered'):
                    covered_hints = payload.get('name_hints', hints)
                    old_source = json.loads(old['source_json'])
                    query_name = old['runtime_id'] if old['kind'] == 'service' else old_source.get('runtime_name', '')
                    complete = bool(query_name) and any(isinstance(h, str) and
                        (h.casefold() in query_name.casefold() if old['kind'] == 'process' else h in query_name)
                        for h in covered_hints)
                entries = (payload or {}).get(field) if field else []
                observed_ids = {str(c.get("id")) for c in entries if isinstance(c, dict)} if isinstance(entries, list) else set()
                observed_old = next((c for c, _ in candidates if c['id'] == old['runtime_id'] and c['kind'] == old['kind']), None)
                if observed_old and old['candidate_digest'] and candidate_digest(observed_old) != old['candidate_digest']:
                    tx.execute("UPDATE service_bindings SET freshness_status='stale' WHERE id=?", (old['id'],))
                    changes.append({"binding_id": old['id'], "reason": "identity_changed", "review_required": True})
                if complete and old['runtime_id'] not in observed_ids:
                    tx.execute("UPDATE service_bindings SET freshness_status='stale' WHERE id=?", (old['id'],))
                    changes.append({"binding_id": old['id'], "reason": "absent_in_complete_snapshot", "review_required": True})
            services = list(profile['services'])
            for grant in grants:
                if not any(s['name'] == grant['service'] for s in services):
                    services.append({'name': grant['service'], 'role': json.loads(grant['source_json']).get('role', 'unknown')})
            for service in services:
                if allowed_grants is not None and not any(b['service'] == service['name'] for b in grants):
                    continue
                for candidate, observation_id in candidates:
                    owners = [b for b in grants if b['kind'] == candidate['kind'] and matches_rule(authorization(b), candidate)]
                    approved = next((b for b in owners if b['service'] == service['name']), None) if len(owners) == 1 else None
                    score, reasons = match_service(service, candidate)
                    if not reasons and not approved:
                        continue
                    observation = by_id[observation_id]
                    stamp = observation.get("observed_at", now())
                    expires = (datetime.fromisoformat(stamp) + timedelta(seconds=BINDING_TTL_SECONDS)).isoformat()
                    signature = candidate_digest(candidate)
                    if any(b['status'] == 'rejected' and b['service'] == service['name'] and b['kind'] == candidate['kind']
                           and b['runtime_id'] == candidate['id'] and b['candidate_digest'] == signature for b in existing):
                        continue
                    source = {"discovery_id": discovery_id, "observation_id": observation_id,
                              "tool_id": observation['tool_id'], "connector": target['connector'],
                              "runtime_name": candidate['name'], 'candidate': candidate}
                    same = next((b for b in existing if b['service'] == service['name']
                                 and b['runtime_id'] == candidate['id'] and b['kind'] == candidate['kind']
                                 and b['candidate_digest'] == signature
                                 and (b['status'] == 'pending' or approved and b['id'] == approved['id'])), None)
                    if approved:
                        source = {**json.loads(approved['source_json']), **source, 'identity_verified_at': stamp,
                                  'identity_verification_run_id': verification_run_id,
                                  'authorization_grant_id': authorization(approved)['grant_id']}
                    binding_id = same['id'] if same else uid()
                    if same:
                        tx.execute("UPDATE service_bindings SET observed_at=?,expires_at=?,source_json=?,evidence_ids_json=?,discovery_id=?,freshness_status=? WHERE id=?",
                                   (stamp, expires, dumps(source), dumps([observation_id]), discovery_id,
                                    'confirmed' if same['status'] == 'confirmed' else 'unverified', binding_id))
                    else:
                        for old in existing:
                            if old['service'] == service['name'] and old['kind'] == candidate['kind'] and old['runtime_id'] == candidate['id']:
                                tx.execute("UPDATE service_bindings SET freshness_status='stale' WHERE id=?", (old['id'],))
                                changes.append({"binding_id": old['id'], "reason": "identity_changed", "review_required": True})
                        tx.execute(
                        "INSERT INTO service_bindings(id,business_id,target_id,service,runtime_id,kind,score,reasons_json,evidence_ids_json,status,discovery_id,freshness_status,observed_at,expires_at,source_json,candidate_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            binding_id,
                            business_id,
                            target_id,
                            service["name"],
                            candidate["id"],
                            candidate["kind"],
                            score,
                            dumps(reasons),
                            dumps([observation_id]),
                            "pending",
                            discovery_id,
                            "unverified", stamp, expires, dumps(source), signature,
                        ),
                        )
                    if approved:
                        tx.execute("UPDATE service_bindings SET status='superseded' WHERE business_id=? AND target_id=? AND service=? AND status='confirmed' AND id<>?",
                                   (business_id, target_id, service['name'], binding_id))
                        tx.execute("UPDATE service_bindings SET status='confirmed',freshness_status='confirmed',authorization_json=?,verified_run_id=COALESCE(?,verified_run_id) WHERE id=?",
                                   (approved['authorization_json'], verification_run_id, binding_id))
                        if approved['id'] != binding_id:
                            tx.execute('INSERT INTO business_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                                (uid(), business_id, profile['profile_version'], binding_id, digest(source), 'identity_verified',
                                 '仅复用原授权选择规则验证新实例，未新增业务授权', 'runtime', stamp))
                    matches.append(
                        {
                            "id": binding_id,
                            "business_service": service["name"],
                            "runtime_candidate": candidate["id"],
                            "kind": candidate["kind"],
                            "match_score": score,
                            "verification_status": 'confirmed' if approved else same['status'] if same else "pending",
                            "reasons": reasons,
                            "evidence_ids": [observation_id],
                        }
                    )
                if not any(m["business_service"] == service["name"] for m in matches):
                    unmatched.append(service["name"])
            objects = []
            for candidate, observation_id in candidates:
                rejected = any(b['status'] == 'rejected' and b['runtime_id'] == candidate['id'] and b['kind'] == candidate['kind']
                               and b['candidate_digest'] == candidate_digest(candidate) for b in existing)
                related = [m for m in matches if m['runtime_candidate'] == candidate['id'] and m['kind'] == candidate['kind']]
                classification = ('unrelated' if rejected else 'confirmed' if any(m['verification_status'] == 'confirmed' for m in related)
                                  else 'insufficient_evidence' if candidate['kind'] == 'process' or related else 'unmatched')
                objects.append({**candidate, 'observation_id': observation_id,
                                'evidence_id': by_id[observation_id].get('evidence_id', observation_id), 'classification': classification})
            objects.sort(key=lambda c: ({'service': 0, 'container': 1, 'process': 2}[c['kind']], c['name'], c['id']))
            groups = {}
            for item in objects:
                if item['kind'] == 'process':
                    group = groups.setdefault((item['name'], item.get('state', 'unknown')), {'name': item['name'], 'state': item.get('state', 'unknown'), 'processes': []})
                    group['processes'].append({'pid': item.get('pid', item['id']), 'classification': item['classification'],
                                               'evidence_id': item['evidence_id'], 'observation_id': item['observation_id']})
            summary = {
                "connector": target["connector"],
                "matches": sorted(matches, key=lambda m: m["match_score"], reverse=True),
                "unmatched_services": unmatched,
                "unassigned_runtime_objects": [
                    c["id"]
                    for c, _ in candidates
                    if not any(m["runtime_candidate"] == c["id"] for m in matches)
                ],
                "runtime_objects": objects,
                "process_groups": [{**g, 'count': len(g['processes'])} for g in groups.values()],
                "observations": observations,
                "errors": [o for o in observations if "payload" not in o],
                "incomplete_tools": [o["diagnostic"] for o in observations if o.get("diagnostic", {}).get("partial")],
                "configuration_mismatches": [],
                "collection_status": "failed" if not any('payload' in o for o in observations) else "partial" if any(collection_incomplete(o) for o in observations) else "completed",
                "binding_changes": changes,
                "collection_states": [{"tool_id": o['tool_id'], "status": o.get('status', 'succeeded'), **{k:o.get('payload', {}).get(k, False) for k in ('partial','truncated','timeout')}} for o in observations],
                "warning": "原长期授权对象自动核验；未知对象仍需独立审批；未递归扫描目录",
            }
            tx.execute(
                "UPDATE discovery_runs SET status=?,summary_json=? WHERE id=?",
                (summary['collection_status'], dumps(summary), discovery_id),
            )
        from app.system.business_graph import fuse_business_graph

        fuse_business_graph(self.db, profile)
        return {"id": discovery_id, **summary}

    def propose(self, business_id, proposal, candidate, discovery_id, profile_version):
        stamp = candidate["observed_at"]
        expires = (datetime.fromisoformat(stamp) + timedelta(seconds=BINDING_TTL_SECONDS)).isoformat()
        signature = candidate_digest(candidate)
        with self.db.transaction() as tx:
            previous = tx.execute("SELECT * FROM service_bindings WHERE business_id=? AND target_id=? AND service=? AND runtime_id=? AND kind=? AND candidate_digest=? ORDER BY observed_at DESC LIMIT 1",
                (business_id, candidate["target_id"], proposal["service"], candidate["id"], candidate["kind"], signature)).fetchone()
            if previous and previous["status"] in {"rejected", "confirmed"}:
                return {"binding_id": previous["id"], "state": previous["status"], **proposal}
            source = {"discovery_id": discovery_id, "observation_id": candidate["observation_id"],
                      "tool_id": candidate["kind"] + ".list", "runtime_name": candidate["name"],
                      "profile_version": profile_version, "mapping_state": "proposed", "ai_proposal": proposal}
            if previous:
                binding_id = previous["id"]
                tx.execute("UPDATE service_bindings SET source_json=?,reasons_json=? WHERE id=?", (dumps(source), dumps(proposal["reasons"]), binding_id))
            else:
                binding_id = uid()
                tx.execute("INSERT INTO service_bindings(id,business_id,target_id,service,runtime_id,kind,score,reasons_json,evidence_ids_json,status,discovery_id,freshness_status,observed_at,expires_at,source_json,candidate_digest) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,'unverified',?,?,?,?)",
                    (binding_id, business_id, candidate["target_id"], proposal["service"], candidate["id"], candidate["kind"], 0,
                     dumps(proposal["reasons"]), dumps([candidate["observation_id"]]), discovery_id, stamp, expires, dumps(source), signature))
        return {"binding_id": binding_id, "state": "awaiting_review", "runtime_candidate": candidate["id"], "target_id": candidate["target_id"], **proposal}

    def confirm(self, business_id, binding_id, decision, reason, actor, identity_mode='exact_instance'):
        from app.system.authorization import create_grant
        with self.db.transaction() as tx:
            binding = tx.execute(
                "SELECT * FROM service_bindings WHERE id=? AND business_id=?",
                (binding_id, business_id),
            ).fetchone()
            if not binding or binding["status"] != "pending":
                raise ValueError("范围候选不存在或已处理")
            if decision == "confirm":
                if binding['freshness_status'] == 'stale' or not binding['expires_at'] or datetime.fromisoformat(binding['expires_at']) <= datetime.now(timezone.utc):
                    raise ValueError("候选观测已过期，需要重新发现并审核")
                source = json.loads(binding['source_json'])
                candidate = source.get('candidate')
                if not candidate:
                    observed = tx.execute('SELECT payload_json FROM runtime_observations WHERE id=? AND target_id=?',
                                          (source.get('observation_id'), binding['target_id'])).fetchone()
                    field = INVENTORY_FIELDS.get(binding['kind'] + '.list')
                    candidate = next((row for row in json.loads(observed[0]).get(field, []) if row.get('id') == binding['runtime_id']), None) if observed else None
                if not candidate or candidate_digest(candidate) != binding['candidate_digest']:
                    raise ValueError('原候选的完整身份依据缺失，请重新发现')
                target = self.runtime.get(business_id, binding['target_id'])
                self.observed_candidate(tx, target, source.get('observation_id'), binding['runtime_id'], binding['kind'],
                                        require_signature=identity_mode == 'stable_object')
                grant = create_grant(binding_id, target, candidate, actor, identity_mode)
                tx.execute('UPDATE service_bindings SET authorization_json=? WHERE id=?', (dumps(grant), binding_id))
                # One approved runtime object per business service per target.
                tx.execute(
                    "UPDATE service_bindings SET status='superseded' WHERE business_id=? AND target_id=? AND service=? AND status='confirmed'",
                    (business_id, binding["target_id"], binding["service"]),
                )
            tx.execute(
                "UPDATE service_bindings SET status=?,freshness_status=? WHERE id=?",
                ("confirmed" if decision == "confirm" else "rejected", "confirmed" if decision == 'confirm' else 'unverified', binding_id),
            )
            current = tx.execute(
                "SELECT current_version FROM business_profiles WHERE id=?", (business_id,)
            ).fetchone()[0]
            tx.execute(
                "INSERT INTO business_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    business_id,
                    current,
                    binding_id,
                    digest(dict(binding)),
                    decision,
                    reason,
                    actor,
                    now(),
                ),
            )
        from app.system.business_graph import fuse_business_graph

        return fuse_business_graph(self.db, self.business.get(business_id))

    def observed_candidate(self, tx, target, observation_id, runtime_id, kind, *, require_signature=True):
        from app.agent.discovery import authorization_digest
        row = tx.execute('SELECT * FROM runtime_observations WHERE id=? AND target_id=? AND tool_id=? AND status=\'succeeded\'',
                         (observation_id, target['id'], kind + '.list')).fetchone()
        if not row:
            raise ValueError('缺少当前目标的成功发现依据，请重新发现')
        stamp = datetime.fromisoformat(row['observed_at'])
        instant = datetime.now(timezone.utc)
        if stamp.tzinfo is None or not stamp <= instant < stamp + timedelta(seconds=RUNTIME_IDENTITY_TTL_SECONDS):
            raise ValueError('发现依据已过期，请重新发现后授权')
        signature = json.loads(row['diagnostics_json']).get('target_authorization_signature')
        if (signature or require_signature) and signature != authorization_digest(target):
            raise ValueError('发现时的目标授权已变化或身份依据不足，请重新发现')
        candidate = next((c for c in json.loads(row['payload_json']).get(INVENTORY_FIELDS[kind + '.list'], [])
                          if c.get('id') == runtime_id and valid_candidate(c, kind + '.list')), None)
        if not candidate:
            raise ValueError('只能授权实际发现的运行对象')
        if kind == 'container' and not re.fullmatch(r'[a-f0-9]{12,64}', runtime_id):
            raise ValueError('容器缺少精确实例身份')
        if kind == 'process' and not re.fullmatch(r'[1-9][0-9]*:[a-f0-9-]{36}:[0-9]{1,20}', runtime_id):
            raise ValueError('进程缺少 PID、主机启动和进程启动身份')
        return candidate, stamp

    def authorize_object(self, business_id, target_id, request, actor):
        from app.system.authorization import identity_rule
        target = self.runtime.get(business_id, target_id)
        profile = self.business.get(business_id)
        configured = next((s for s in profile['services'] if s['name'] == request.service), None)
        if configured and request.role != 'unknown' and request.role != configured.get('role', 'unknown'):
            raise ValueError('运行对象授权不能覆盖已经审核的业务作用')
        binding_id, discovery_id = uid(), uid()
        with self.db.transaction() as tx:
            candidate, stamp = self.observed_candidate(tx, target, request.observation_id, request.runtime_id, request.kind)
            identity_rule(candidate, request.identity_mode)
            source = {'observation_id': request.observation_id, 'runtime_name': candidate['name'], 'candidate': candidate,
                      'runtime_scope_confirmation': True, 'role': (configured or {}).get('role', request.role)}
            tx.execute("INSERT INTO discovery_runs VALUES(?,?,?,'completed',?,?)",
                       (discovery_id, business_id, target_id, dumps({'source':'operator_inventory_authorization'}), now()))
            tx.execute("INSERT INTO service_bindings(id,business_id,target_id,service,runtime_id,kind,score,reasons_json,evidence_ids_json,status,discovery_id,freshness_status,observed_at,expires_at,source_json,candidate_digest) VALUES(?,?,?,?,?,?,0,?,?,'pending',?,'unverified',?,?,?,?)",
                       (binding_id, business_id, target_id, request.service, request.runtime_id, request.kind,
                        dumps(['操作者独立授权实际发现对象']), dumps([request.observation_id]), discovery_id,
                        stamp.isoformat(), (stamp + timedelta(seconds=RUNTIME_IDENTITY_TTL_SECONDS)).isoformat(),
                        dumps(source), candidate_digest(candidate)))
        self.confirm(business_id, binding_id, 'confirm', request.reason, actor, request.identity_mode)
        return {'binding_id': binding_id, 'authorization_status': 'confirmed'}

    def approve_rule(self, business_id, binding_id, request, actor):
        from app.system.authorization import authorized, create_grant
        with self.db.transaction() as tx:
            binding = tx.execute('SELECT * FROM service_bindings WHERE id=? AND business_id=?', (binding_id, business_id)).fetchone()
            if not binding:
                raise ValueError('业务对象授权不存在')
            target = self.runtime.get(business_id, binding['target_id'])
            if not authorized(binding, target) or not binding_is_current(binding):
                raise ValueError('请先重新发现并核验当前实例，再审核身份规则')
            source = json.loads(binding['source_json'])
            candidate, _ = self.observed_candidate(tx, target, source.get('observation_id'), binding['runtime_id'], binding['kind'])
            grant = create_grant(binding_id, target, candidate, actor, request.identity_mode)
            tx.execute('UPDATE service_bindings SET authorization_json=?,verified_run_id=NULL WHERE id=?', (dumps(grant), binding_id))
            version = tx.execute('SELECT current_version FROM business_profiles WHERE id=?', (business_id,)).fetchone()[0]
            tx.execute('INSERT INTO business_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                       (uid(), business_id, version, binding_id, digest(grant), 'approve_rule', request.reason, actor, now()))
        return {'binding_id': binding_id, 'authorization_status': 'confirmed'}

    def revoke(self, business_id, binding_id, reason, actor):
        from app.system.authorization import authorization
        with self.db.transaction() as tx:
            binding = tx.execute('SELECT * FROM service_bindings WHERE id=? AND business_id=?', (binding_id, business_id)).fetchone()
            if not binding or not authorization(binding).get('grant_id'):
                raise ValueError('长期授权记录不存在')
            identifier = authorization(binding)['grant_id']
            for row in tx.execute('SELECT * FROM service_bindings WHERE business_id=?', (business_id,)).fetchall():
                grant = authorization(row)
                if grant.get('grant_id') == identifier:
                    grant.update(revoked_at=now(), revoked_by=actor, revocation_reason=reason)
                    tx.execute("UPDATE service_bindings SET authorization_json=?,status='revoked',freshness_status='unverified' WHERE id=?", (dumps(grant), row['id']))
            version = tx.execute('SELECT current_version FROM business_profiles WHERE id=?', (business_id,)).fetchone()[0]
            tx.execute('INSERT INTO business_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                       (uid(), business_id, version, binding_id, digest(authorization(binding)), 'revoke', reason, actor, now()))
        return {'binding_id': binding_id, 'authorization_status': 'revoked'}

    def reconcile(self, business_id, observation):
        if "payload" not in observation:
            return
        if observation["payload"].get("execution_mode") == "standard_ssh_readonly" or observation.get('tool_id')=='config.read':
            from app.system.business_graph import fuse_business_graph
            fuse_business_graph(self.db, self.business.get(business_id))
        text = observation["payload"].get("text", "")
        runtime_store = (
            "PostgreSQL"
            if re.search(r"(?i)DB_ENGINE\s*=\s*postgres(?:ql)?", text)
            else "MySQL"
            if re.search(r"(?i)DB_ENGINE\s*=\s*mysql", text)
            else None
        )
        if not runtime_store:
            return
        profile = self.business._profile(business_id)
        runtime_fact = claim(
            f"business:{business_id}",
            "datastore_engine",
            runtime_store,
            "Runtime",
            f"observation:{observation['observation_id']}",
            digest(observation["payload"]),
        )
        profile.facts.append(runtime_fact)
        static_names = {x["name"] for x in profile.datastores}
        if len(static_names) == 1 and runtime_store not in static_names:
            static_fact_ids = [x["fact_id"] for x in profile.datastores]
            profile.conflicts.append(
                {
                    "id": uid(),
                    "subject": f"business:{business_id}",
                    "predicate": "datastore_engine",
                    "fact_ids": [*static_fact_ids, runtime_fact.id],
                    "status": "open",
                    "verification": "核对已授权配置与当前服务连接元数据，再由人工确认部署版本差异",
                }
            )
        profile.review_status = "pending"
        return self.business.save(profile, profile.profile_version)
