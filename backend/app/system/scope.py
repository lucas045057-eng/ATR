from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from app.business.profile import claim
from app.business.service import BusinessService
from app.common import digest, dumps, now, uid
from app.connectors.models import ToolRequest
from app.connectors.service import RuntimeService

BINDING_TTL_SECONDS = 86400


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


def valid_candidate(candidate, tool):
    if not isinstance(candidate, dict):
        return False
    if candidate.get('kind') != tool.split('.')[0]:
        return False
    if any(not isinstance(candidate.get(k), str) or not 1 <= len(candidate[k]) <= 128 for k in ('id', 'name')):
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

    def discover(self, business_id, target_id):
        profile = self.business.get(business_id)
        target = self.runtime.get(business_id, target_id)
        discovery_id = uid()
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO discovery_runs VALUES(?,?,?,'running','{}',?)",
                (discovery_id, business_id, target_id, now()),
            )
        hints = [s['name'] for s in profile['services'] if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}', s['name'])][:8]
        tools = [
            ToolRequest(tool_id=t)
            for t in ("host.info", "network.listeners")
        ]
        tools.extend(ToolRequest(tool_id=t, arguments={'name_hints': hints}) for t in ('process.list', 'service.list'))
        if target["docker_reads_enabled"]:
            tools.append(ToolRequest(tool_id="container.list", arguments={'name_hints': hints}))
        tools.extend(
            ToolRequest(tool_id="config.list", arguments={"path": p})
            for p in target["allowed_paths"][:10]
        )
        observations = [self.runtime.execute(business_id, target_id, request) for request in tools]
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
        by_id = {o["observation_id"]: o for o in observations}
        with self.db.transaction() as tx:
            existing = [dict(r) for r in tx.execute(
                "SELECT * FROM service_bindings WHERE target_id=? AND status IN ('pending','confirmed')",
                (target_id,))]
            for old in existing:
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
            for service in profile["services"]:
                for candidate, observation_id in candidates:
                    score, reasons = match_service(service, candidate)
                    if not reasons:
                        continue
                    observation = by_id[observation_id]
                    stamp = observation.get("observed_at", now())
                    expires = (datetime.fromisoformat(stamp) + timedelta(seconds=BINDING_TTL_SECONDS)).isoformat()
                    signature = candidate_digest(candidate)
                    source = {"discovery_id": discovery_id, "observation_id": observation_id,
                              "tool_id": observation['tool_id'], "connector": target['connector'],
                              "runtime_name": candidate['name']}
                    same = next((b for b in existing if b['service'] == service['name']
                                 and b['runtime_id'] == candidate['id'] and b['kind'] == candidate['kind']
                                 and b['candidate_digest'] == signature), None)
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
                    matches.append(
                        {
                            "id": binding_id,
                            "business_service": service["name"],
                            "runtime_candidate": candidate["id"],
                            "kind": candidate["kind"],
                            "match_score": score,
                            "verification_status": same['status'] if same else "pending",
                            "reasons": reasons,
                            "evidence_ids": [observation_id],
                        }
                    )
                if not any(m["business_service"] == service["name"] for m in matches):
                    unmatched.append(service["name"])
            summary = {
                "connector": target["connector"],
                "matches": sorted(matches, key=lambda m: m["match_score"], reverse=True),
                "unmatched_services": unmatched,
                "unassigned_runtime_objects": [
                    c["id"]
                    for c, _ in candidates
                    if not any(m["runtime_candidate"] == c["id"] for m in matches)
                ],
                "observations": observations,
                "errors": [o for o in observations if "payload" not in o],
                "configuration_mismatches": [],
                "collection_status": "failed" if not any('payload' in o for o in observations) else "partial" if any(collection_incomplete(o) for o in observations) else "completed",
                "binding_changes": changes,
                "collection_states": [{"tool_id": o['tool_id'], "status": o.get('status', 'succeeded'), **{k:o.get('payload', {}).get(k, False) for k in ('partial','truncated','timeout')}} for o in observations],
                "warning": "分数只用于排序，候选均需人工确认；未递归扫描目录",
            }
            tx.execute(
                "UPDATE discovery_runs SET status=?,summary_json=? WHERE id=?",
                (summary['collection_status'], dumps(summary), discovery_id),
            )
        from app.system.business_graph import fuse_business_graph

        fuse_business_graph(self.db, profile)
        return {"id": discovery_id, **summary}

    def confirm(self, business_id, binding_id, decision, reason, actor):
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
                # One approved runtime object per business service per target.
                tx.execute(
                    "UPDATE service_bindings SET status='superseded' WHERE target_id=? AND service=? AND status='confirmed'",
                    (binding["target_id"], binding["service"]),
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

    def reconcile(self, business_id, observation):
        if "payload" not in observation:
            return
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
