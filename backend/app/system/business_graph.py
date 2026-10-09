"""Business data is fused into existing SQLite graph snapshots, not a new graph store."""

from __future__ import annotations

import json

from app.common import digest, dumps, now, uid
from app.system.graph import node


def fuse_business_graph(database, profile):
    business_id = profile["business_id"]
    with database.transaction() as tx:
        business = tx.execute(
            "SELECT * FROM business_profiles WHERE id=?", (business_id,)
        ).fetchone()
        system_id = business["system_id"] or uid()
        if not business["system_id"]:
            tx.execute(
                "INSERT INTO systems VALUES(?,?,?,?,?,?,?,?)",
                (
                    system_id,
                    profile["name"],
                    f"business://{business_id}",
                    f"profile-v{profile['profile_version']}",
                    "",
                    "",
                    now(),
                    dumps({"business_id": business_id}),
                ),
            )
            tx.execute(
                "UPDATE business_profiles SET system_id=? WHERE id=?", (system_id, business_id)
            )
        sid = uid()
        nodes = {}
        edges = []
        facts = {f["id"]: f for f in profile["facts"]}

        def add(kind, name, role, attributes):
            item = node(kind, name, role, attributes, sid)
            nodes[item["canonical_key"]] = item
            return item

        for service in profile["services"]:
            add(
                "service",
                service["name"],
                service["role"],
                {**service, "provenance": facts.get(service["fact_id"])},
            )
        for module in profile["modules"]:
            add(
                "module",
                module["name"],
                "module",
                {**module, "provenance": facts.get(module["fact_id"])},
            )
        for config in profile["configurations"]:
            add(
                "configuration",
                config["path"],
                "configuration",
                {**config, "provenance": facts.get(config["fact_id"])},
            )
        for store in profile["datastores"]:
            add(
                "datastore",
                store["name"],
                "datastore",
                {**store, "provenance": facts.get(store["fact_id"])},
            )
        for interface in profile["interfaces"]:
            name = interface.get("path") or str(interface.get("port"))
            kind = "api" if "path" in interface else "network_endpoint"
            subject = (
                f"module:{interface['module']}"
                if interface.get("module")
                else f"service:{interface.get('service')}"
            )
            endpoint = add(
                kind,
                f"{subject}:{name}",
                kind,
                {**interface, "provenance": facts.get(interface["fact_id"])},
            )
            if subject in nodes:
                edges.append(
                    (subject, endpoint["canonical_key"], "exposes", "static", interface["fact_id"])
                )
        for relation in profile["dependencies"]:
            fact = facts.get(relation["fact_id"], {})
            if fact.get("verification_status") in {"rejected", "stale"}:
                continue
            if relation["from"] in nodes and relation["to"] in nodes:
                edges.append(
                    (
                        relation["from"],
                        relation["to"],
                        relation["relation"],
                        relation["certainty"],
                        relation["fact_id"],
                    )
                )
        bindings = tx.execute(
            "SELECT b.*,t.config_json FROM service_bindings b JOIN runtime_targets t ON t.id=b.target_id WHERE b.business_id=? AND b.status='confirmed'",
            (business_id,),
        ).fetchall()
        for binding in bindings:
            from app.system.scope import binding_is_current
            if not binding_is_current(binding):
                continue
            key = f"service:{binding['service']}"
            if key not in nodes:
                continue
            config = json.loads(binding["config_json"])
            host = add(
                "host",
                binding["target_id"],
                "host",
                {"address": config["host"], "source_type": "Human Verified"},
            )
            runtime_kind = {
                "container": "container",
                "service": "service",
                "process": "process",
            }.get(binding["kind"], "process")
            runtime = add(
                runtime_kind,
                f"runtime:{binding['runtime_id']}",
                "runtime",
                {
                    "binding_id": binding["id"],
                    "evidence_ids": json.loads(binding["evidence_ids_json"]),
                    "source_type": "Runtime",
                    "observed_at": binding['observed_at'],
                    "expires_at": binding['expires_at'],
                    "freshness_status": binding['freshness_status'],
                },
            )
            edges.extend(
                [
                    (
                        key,
                        runtime["canonical_key"],
                        "observed_as",
                        "human_confirmed",
                        binding["id"],
                    ),
                    (
                        runtime["canonical_key"],
                        host["canonical_key"],
                        "runs_on",
                        "human_confirmed",
                        binding["id"],
                    ),
                ]
            )
        semantic = digest(
            {
                "nodes": sorted((key, n["role"]) for key, n in nodes.items()),
                "edges": sorted((a, b, r, c) for a, b, r, c, _ in edges),
            }
        )
        previous = tx.execute(
            "SELECT * FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1",
            (system_id,),
        ).fetchone()
        version = previous["version"] + 1 if previous else 1
        if previous:
            tx.execute(
                "UPDATE graph_snapshots SET status='superseded' WHERE system_id=? AND status='current'",
                (system_id,),
            )
            # Preserve Trusted review; matching rechecks role/topology compatibility on every run.
        tx.execute(
            "INSERT INTO graph_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            (
                sid,
                system_id,
                version,
                f"profile-v{profile['profile_version']}",
                semantic,
                now(),
                now(),
                dumps(
                    {
                        **profile["coverage"],
                        "source_digest": digest([(r["path"], r["content_digest"]) for r in tx.execute(
                            "SELECT path,content_digest FROM artifact_files WHERE business_id=? AND active=1 ORDER BY path,content_digest", (business_id,)
                        )]),
                        "business_id": business_id,
                        "profile_version": profile["profile_version"],
                        "semantic_digest": semantic,
                    }
                ),
                "current",
            ),
        )
        tx.execute(
            "UPDATE systems SET repo_revision=?,authorized_scope_json=? WHERE id=?",
            (
                f"profile-v{profile['profile_version']}",
                dumps({"business_id": business_id, "services": [b["service"] for b in bindings]}),
                system_id,
            ),
        )
        for n in nodes.values():
            tx.execute(
                "INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?)",
                (
                    n["id"],
                    sid,
                    n["canonical_key"],
                    n["kind"],
                    n["name"],
                    n["role"],
                    dumps(n["attributes"]),
                ),
            )
        for source, target, relation, certainty, fact_id in edges:
            tx.execute(
                "INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?)",
                (
                    uid(),
                    sid,
                    nodes[source]["id"],
                    nodes[target]["id"],
                    relation,
                    certainty,
                    dumps(
                        {
                            "fact_id": fact_id,
                            "provenance": facts.get(fact_id),
                            "verification_status": facts.get(fact_id, {}).get(
                                "verification_status", "confirmed"
                            ),
                        }
                    ),
                ),
            )
        for fact in profile["facts"]:
            subject = nodes.get(fact["subject"])
            if subject and fact["verification_status"] != "stale":
                truth = (
                    "runtime"
                    if fact["source_type"] == "Runtime"
                    else "deployment"
                    if fact["source_type"] == "Documented"
                    else "static"
                )
                tx.execute(
                    "INSERT INTO fact_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        sid,
                        subject["id"],
                        fact["predicate"],
                        fact["subject"],
                        dumps({"object": fact["object"], "provenance": fact}),
                        truth,
                        fact["source_locator"],
                        fact["source_digest"],
                        fact["observed_at"],
                        "business-v2.1",
                        fact["verification_status"],
                    ),
                )
        for conflict in profile["conflicts"]:
            tx.execute(
                "INSERT INTO graph_conflicts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    sid,
                    conflict["subject"],
                    conflict["predicate"],
                    "business",
                    dumps(conflict["fact_ids"]),
                    "high",
                    conflict["status"],
                    None,
                ),
            )
    return {"system_id": system_id, "snapshot_id": sid, "version": version}
