"""Business data is fused into existing SQLite graph snapshots, not a new graph store."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.common import digest, dumps, now, uid
from app.system.graph import node


def fuse_business_graph(database, profile):
    from app.system.endpoint_mapping import configuration_path, latest_configuration
    from app.system.scope import binding_is_current
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
        for observed in tx.execute(
            "SELECT t.id,t.config_json,o.id AS observation_id,o.observed_at,o.tool_id,o.payload_json "
            "FROM runtime_targets t JOIN runtime_observations o ON o.target_id=t.id "
            "WHERE t.business_id=? AND o.status='succeeded' AND o.id=(SELECT id FROM runtime_observations "
            "WHERE target_id=t.id AND status='succeeded' ORDER BY observed_at DESC,id DESC LIMIT 1)",
            (business_id,),
        ):
            config = json.loads(observed["config_json"])
            if config.get("execution_mode") != "standard_ssh_readonly":
                continue
            payload = json.loads(observed["payload_json"])
            add("host", observed["id"], "host", {
                "address": config["host"], "source_type": "Runtime", "execution_mode": "standard_ssh_readonly",
                "observation_id": observed["observation_id"], "observed_at": observed["observed_at"],
                "identity": payload.get("identity", {}), "server_enforced_readonly": False,
            })
        bindings = tx.execute(
            "SELECT b.*,t.config_json FROM service_bindings b JOIN runtime_targets t ON t.id=b.target_id WHERE b.business_id=? AND b.status='confirmed'",
            (business_id,),
        ).fetchall()
        for binding in bindings:
            if not binding_is_current(binding):
                continue
            key = f"service:{binding['service']}"
            if key not in nodes:
                source = json.loads(binding['source_json'] or '{}')
                if not source.get('runtime_scope_confirmation'):
                    continue
                add('service', binding['service'], source.get('role', 'unknown'),
                    {'provenance': source, 'business_semantics': 'unknown', 'runtime_scope_only': True})
            config = json.loads(binding["config_json"])
            binding_refs=[{'schema_version':'observation-ref-1','observation_id':identifier,'source_type':'Runtime',
                           'target_id':binding['target_id'],'binding_id':binding['id'],'graph_snapshot_id':sid}
                          for identifier in json.loads(binding['evidence_ids_json'])]
            host = nodes.get(f"host:{binding['target_id']}") or add(
                "host",
                binding["target_id"],
                "host",
                {"address": config["host"], "source_type": "Human Verified", "source_refs":binding_refs},
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
        binding_sources={binding['id']:[{'schema_version':'observation-ref-1','observation_id':identifier,
                         'source_type':'Runtime','target_id':binding['target_id'],'binding_id':binding['id'],'graph_snapshot_id':sid}
                         for identifier in json.loads(binding['evidence_ids_json'])] for binding in bindings}
        current_bindings={binding['id']:binding for binding in bindings if binding_is_current(binding)}
        endpoint_mappings={}
        for fact in profile['facts']:
            value=fact.get('object')
            if fact['predicate']!='verified_endpoint' or fact['verification_status']!='confirmed' or not isinstance(value,dict):
                continue
            identifiers=value.get('binding_ids',[])
            if len(identifiers)!=2 or any(identifier not in current_bindings or current_bindings[identifier]['target_id']!=value.get('target_id') for identifier in identifiers):
                continue
            observed=tx.execute("SELECT * FROM runtime_observations WHERE id=? AND target_id=? AND tool_id='config.read' AND status='succeeded'",(value.get('observation_id'),value.get('target_id'))).fetchone()
            if not observed or observed['content_digest']!=fact['source_digest']:
                continue
            config_path=configuration_path(observed)
            latest=latest_configuration(tx,observed['target_id'],config_path)
            expires=datetime.fromisoformat(value['expires_at'])
            if not latest or latest['id']!=observed['id'] or expires.tzinfo is None or expires<=datetime.now(timezone.utc):
                continue
            endpoint_mappings.setdefault(value['dependency_fact_id'],[]).append({
                'endpoint_id':value['endpoint_id'],'target_id':value['target_id'],'binding_ids':identifiers,
                'source_refs':fact['source_refs'],'expires_at':value['expires_at'],'mapping_fact_id':fact['id'],
                'observation_id':value['observation_id'],'configuration_digest':fact['source_digest'],'configuration_path':config_path})
        for source, target, relation, certainty, fact_id in edges:
            mappings=endpoint_mappings.get(fact_id,[])
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
                            "source_refs":binding_sources.get(fact_id,[]) or facts.get(fact_id,{}).get('source_refs',[]),
                            "endpoint_mappings":mappings,
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
