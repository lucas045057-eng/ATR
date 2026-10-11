from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from app.business.media import optional_image
from app.business.models import BusinessProfile, Fact
from app.common import digest, now


def claim(
    subject, predicate, obj, source_type, locator, source_digest, status="pending", confidence=1.0
):
    key = digest([subject, predicate, obj, source_type, locator, source_digest])[:32]
    return Fact(
        id=key,
        subject=subject,
        predicate=predicate,
        object=obj,
        source_type=source_type,
        source_locator=locator,
        source_digest=source_digest,
        observed_at=now(),
        confidence=confidence,
        verification_status=status,
    )


def build_static(business, files, previous):
    profile = BusinessProfile(
        business_id=business["id"],
        name=business["name"],
        description=business["description"],
        profile_version=business["current_version"] + 1,
    )
    service_by_name = {}
    module_by_name = {}
    files_by_id = {row["id"]: row for row in files}
    stacks = set()
    prior = {fact.id: fact for fact in previous.facts} if previous else {}

    def add(subject, pred, obj, row, source_type="Static"):
        fact = claim(
            subject,
            pred,
            obj,
            source_type,
            f"artifact:{row.get('slot', 'source')}:{row['path']}",
            row["content_digest"],
        )
        if fact.id in prior:
            fact = prior[fact.id].model_copy(deep=True)
        profile.facts.append(fact)
        return fact.id

    for row in files:
        info = row["analysis"]
        ref = {key: row[key] for key in ("id", "path", "content_digest", "bytes", "media_type")}
        ref["parse_state"] = info.get("parse_state")
        if row["media_type"] == "image":
            ref.update(media_role=info.get("media_role"), optional_suggested=optional_image(row["path"]),
                       can_restore=info.get("parse_state") == "optional_static_attachment" and bool(info.get("optional_previous")))
        profile.source_references.append(ref)
        if info.get("manual_text"):
            add(f"document:{row['path']}", "describes", info["manual_text"], row, "Documented")
        if info.get("language"):
            stacks.add(info["language"])
        if info.get("parse_state") not in {"complete", "partial_parser_error"}:
            if info.get("parse_state") not in {"excluded_irrelevant", "unsupported", "optional_static_attachment"}:
                profile.unknowns.append(f"资料 {row['path']}：{info.get('parse_state')}")
            continue
        for candidate in info.get("services", []):
            fact_id = add(f"service:{candidate['name']}", "declares_service", candidate, row)
            if (
                candidate["name"] in service_by_name
                and service_by_name[candidate["name"]]["role"] != candidate["role"]
            ):
                profile.conflicts.append(
                    {
                        "id": digest([candidate["name"], "role"]),
                        "subject": f"service:{candidate['name']}",
                        "predicate": "role",
                        "fact_ids": [fact_id, service_by_name[candidate["name"]]["fact_id"]],
                        "status": "open",
                        "verification": "核对部署来源和当前服务配置",
                    }
                )
            service_by_name[candidate["name"]] = {
                **candidate,
                "fact_id": fact_id,
                "source_type": "Static",
                "verification_status": prior.get(fact_id, profile.facts[-1]).verification_status,
            }
            config_id = add(
                f"service:{candidate['name']}", "configured_by", f"configuration:{row['path']}", row
            )
            profile.dependencies.append(
                {
                    "from": f"service:{candidate['name']}",
                    "to": f"configuration:{row['path']}",
                    "relation": "configured_by",
                    "fact_id": config_id,
                    "certainty": "declared",
                }
            )
            for dependency in candidate["dependencies"]:
                relation_id = add(
                    f"service:{candidate['name']}", "depends_on", f"service:{dependency}", row
                )
                profile.dependencies.append(
                    {
                        "from": f"service:{candidate['name']}",
                        "to": f"service:{dependency}",
                        "relation": "depends_on",
                        "fact_id": relation_id,
                        "certainty": "declared",
                    }
                )
            if candidate.get("healthcheck_declared"):
                health_id = add(f"service:{candidate['name']}", "healthcheck_declared", True, row)
                profile.observability.append(
                    {"service": candidate["name"], "type": "healthcheck", "fact_id": health_id}
                )
            for port in candidate.get("ports", []):
                port_id = add(f"service:{candidate['name']}", "exposes", str(port), row)
                profile.interfaces.append(
                    {"service": candidate["name"], "port": port, "fact_id": port_id}
                )
        if row["media_type"] == "code":
            module = str(PurePosixPath(row["path"]).with_suffix("")).replace("/", ".")
            fact_id = add(f"module:{module}", "defined_in", row["path"], row)
            item = {
                "name": module,
                "file_id": row["id"],
                "imports": info.get("imports", []),
                "environment_keys": info.get("environment_keys", []),
                "fact_id": fact_id,
                "responsibility": "unknown",
            }
            profile.modules.append(item)
            module_by_name[module] = item
            stacks.update(info.get("imports", [])[:50])
            for route in info.get("routes", []):
                route_id = add(f"module:{module}", "exposes", route, row)
                profile.interfaces.append(
                    {**route, "module": module, "file_id": row["id"], "fact_id": route_id}
                )
        if row["media_type"] == "configuration":
            config_id = add(f"configuration:{row['path']}", "keys", info.get("keys", []), row)
            profile.configurations.append(
                {
                    "path": row["path"],
                    "file_id": row["id"],
                    "keys": info.get("keys", []),
                    "fact_id": config_id,
                }
            )
        for store in info.get("datastores", []):
            store_id = add(
                f"configuration:{row['path']}"
                if row["media_type"] == "configuration"
                else f"source:{row['path']}",
                "references_datastore",
                store,
                row,
            )
            profile.datastores.append(
                {
                    "name": store,
                    "file_id": row["id"],
                    "fact_id": store_id,
                    "verification_status": "pending",
                }
            )
        if info.get("headings"):
            add(f"document:{row['path']}", "describes", info["headings"], row, "Documented")
        if row["media_type"] == "code":
            module_name = str(PurePosixPath(row["path"]).with_suffix("")).replace("/", ".")
            for call in info.get("client_calls", [])[:20]:
                store = {
                    "pymysql": "MySQL",
                    "psycopg": "PostgreSQL",
                    "psycopg2": "PostgreSQL",
                    "redis": "Redis",
                }.get(call["client"])
                if store:
                    fact_id = add(f"module:{module_name}", "connects_to", f"datastore:{store}", row)
                    profile.dependencies.append(
                        {
                            "from": f"module:{module_name}",
                            "to": f"datastore:{store}",
                            "relation": "connects_to",
                            "fact_id": fact_id,
                            "certainty": "static_call",
                        }
                    )
    aliases = {}
    for name in module_by_name:
        aliases.setdefault(name.split(".")[-1], []).append(name)
    for module in profile.modules:
        for imported in module["imports"]:
            if len(profile.dependencies) >= 2000:
                profile.unknowns.append(
                    "模块依赖骨架达到 2000 条上限；完整导入关系仍保留在源码索引中"
                )
                break
            candidates = aliases.get(imported.split(".")[-1], [])
            relative = (
                module["name"].rsplit(".", 1)[0] + imported
                if imported.startswith(".")
                else imported
            )
            match = (
                relative
                if relative in module_by_name
                else candidates[0]
                if len(candidates) == 1
                else None
            )
            if match:
                row = files_by_id[module["file_id"]]
                fact_id = add(f"module:{module['name']}", "calls", f"module:{match}", row)
                profile.dependencies.append(
                    {
                        "from": f"module:{module['name']}",
                        "to": f"module:{match}",
                        "relation": "calls",
                        "fact_id": fact_id,
                        "certainty": "static_import",
                    }
                )
    profile.services = list(service_by_name.values())
    profile.technology_stack = sorted(stacks)[:200]
    active_digests = {r["content_digest"] for r in files}
    current_ids = {f.id for f in profile.facts}
    for old in prior.values():
        if old.id in current_ids:
            continue
        retained = old.model_copy(deep=True)
        if old.source_type == "Runtime" or (
            old.source_type in {"Inferred", "Human Verified"}
            and old.source_digest in active_digests
        ):
            profile.facts.append(retained)
        else:
            retained.verification_status = "stale"
            profile.facts.append(retained)
    # Human decisions survive only while their source digest remains applicable.
    for service in profile.services:
        role_facts = [
            f
            for f in profile.facts
            if f.subject == f"service:{service['name']}"
            and f.predicate == "role"
            and f.source_type == "Human Verified"
            and f.verification_status == "confirmed"
        ]
        if role_facts:
            service["role"] = role_facts[-1].object
    for fact in profile.facts:
        if fact.source_type == "Inferred" and fact.verification_status not in {"rejected", "stale"}:
            if fact.predicate in {"depends_on", "calls"} and not any(
                d["fact_id"] == fact.id for d in profile.dependencies
            ):
                profile.dependencies.append(
                    {
                        "from": fact.subject,
                        "to": fact.object,
                        "relation": fact.predicate,
                        "fact_id": fact.id,
                        "certainty": "inferred",
                    }
                )
            elif fact.predicate == "responsibility":
                for module in profile.modules:
                    if f"module:{module['name']}" == fact.subject:
                        module["responsibility"] = fact.object
    if previous:
        for conflict in previous.conflicts:
            if not any(c["id"] == conflict["id"] for c in profile.conflicts):
                profile.conflicts.append(dict(conflict))
    rejected_ids = {f.id for f in profile.facts if f.verification_status == "rejected"}
    profile.dependencies = [r for r in profile.dependencies if r["fact_id"] not in rejected_ids]
    profile.unknowns.extend(
        [
            "静态资料不证明当前运行版本",
            "尚未完成运行环境映射与实时验证",
            "模块的业务职责和端到端流程需要模型或人工核验",
        ]
    )
    if not files:
        profile.unknowns.insert(0, "没有业务资料，认知覆盖不足")
    if not profile.services:
        profile.unknowns.insert(0, "没有可核验的部署服务声明，不生成猜测服务")
    profile.unknowns = profile.unknowns[:100]
    profile.coverage = {
        "files": len(files),
        "types": dict(Counter(r["media_type"] for r in files)),
        "tree_summary": [r["path"] for r in files[:100]],
        "static_parsed": sum(r["analysis"].get("parse_state") == "complete" for r in files),
        "pending_files": sum(
            r["analysis"].get("parse_state")
            not in {"complete", "excluded_irrelevant", "unsupported", "optional_static_attachment"}
            for r in files
        ),
        "optional_attachments": sum(r["analysis"].get("parse_state") == "optional_static_attachment" for r in files),
        "services_declared": len(profile.services),
        "runtime_verified": any(
            f.source_type == "Runtime" and f.verification_status == "confirmed"
            for f in profile.facts
        ),
        "knowledge_level": "skeleton" if files else "insufficient",
    }
    return profile
