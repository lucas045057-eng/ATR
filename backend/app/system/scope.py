from __future__ import annotations

import re

from app.business.profile import claim
from app.business.service import BusinessService
from app.common import digest, dumps, now, uid
from app.connectors.models import ToolRequest
from app.connectors.service import RuntimeService


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
        tools = [
            ToolRequest(tool_id=t)
            for t in ("host.info", "process.list", "network.listeners", "service.list")
        ]
        if target["docker_reads_enabled"]:
            tools.append(ToolRequest(tool_id="container.list"))
        tools.extend(
            ToolRequest(tool_id="config.list", arguments={"path": p})
            for p in target["allowed_paths"][:10]
        )
        observations = [self.runtime.execute(business_id, target_id, request) for request in tools]
        candidates = []
        for observation in observations:
            if "payload" not in observation:
                continue
            for field in ("services", "containers", "processes"):
                candidates.extend(
                    (item, observation["observation_id"])
                    for item in observation["payload"].get(field, [])[:100]
                )
        matches = []
        unmatched = []
        with self.db.transaction() as tx:
            tx.execute("UPDATE service_bindings SET status='stale' WHERE target_id=?", (target_id,))
            for service in profile["services"]:
                for candidate, observation_id in candidates:
                    score, reasons = match_service(service, candidate)
                    if not reasons:
                        continue
                    binding_id = uid()
                    tx.execute(
                        "INSERT INTO service_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                        ),
                    )
                    matches.append(
                        {
                            "id": binding_id,
                            "business_service": service["name"],
                            "runtime_candidate": candidate["id"],
                            "kind": candidate["kind"],
                            "match_score": score,
                            "verification_status": "pending",
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
                "warning": "分数只用于排序，候选均需人工确认；未递归扫描目录",
            }
            tx.execute(
                "UPDATE discovery_runs SET status=?,summary_json=? WHERE id=?",
                ("partial" if summary["errors"] else "completed", dumps(summary), discovery_id),
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
                # One approved runtime object per business service per target.
                tx.execute(
                    "UPDATE service_bindings SET status='superseded' WHERE target_id=? AND service=? AND status='confirmed'",
                    (binding["target_id"], binding["service"]),
                )
            tx.execute(
                "UPDATE service_bindings SET status=? WHERE id=?",
                ("confirmed" if decision == "confirm" else "rejected", binding_id),
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
