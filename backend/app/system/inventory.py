from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.settings import Settings
from app.storage.database import Database
from app.system.compose_parser import parse_compose
from app.system.conflicts import compose_conflicts
from app.system.graph import build_graph, json_text
from app.system.source_parser import inspect_source


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SystemOnboarding:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    def create_snapshot(self, source_key: str, display_name: str, compose_file: str) -> dict[str, Any]:
        root_candidates = [root / source_key for root in self.settings.repository_root_paths()]
        source_root = next((p.resolve() for p in root_candidates if p.exists()), None)
        if source_root is None or not source_root.is_dir():
            raise ValueError("Source repository must already exist under an authorized repository root")
        if not any(source_root.is_relative_to(root) for root in self.settings.repository_root_paths()):
            raise ValueError("Repository path escapes the configured read-only roots")
        compose_path = (source_root / compose_file).resolve()
        if not compose_path.is_relative_to(source_root) or not compose_path.is_file():
            raise ValueError("Compose file must be a file below the repository root")
        spec, deployment_digest = parse_compose(compose_path)
        source_rows, coverage = inspect_source(source_root, self.settings.max_source_files, self.settings.max_source_bytes)
        repo_revision = self._revision(source_root, source_rows)
        system_id = str(uuid.uuid4())
        snapshot_id = str(uuid.uuid4())
        version = 1
        connection = self.db.connect()
        try:
            old = connection.execute("SELECT id FROM systems WHERE source_path=?", (str(source_root),)).fetchone()
            if old:
                system_id = old["id"]
                version = connection.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM graph_snapshots WHERE system_id=?", (system_id,)).fetchone()["v"]
            services = spec["services"]
            compose_project = str(spec.get("name") or f"atr-{system_id[:8]}")[:63]
            nodes, edges = build_graph(snapshot_id, services, spec.get("networks") or {})
            source_digest = hashlib.sha256("".join(f"{r['path']}:{r['content_sha256']}" for r in source_rows).encode()).hexdigest()
            coverage.update({"compose_services": len(services), "source_files_supported": len(source_rows),
                             "source_digest": source_digest, "runtime_observation": "not_collected"})
            timestamp = now()
            with self.db.transaction() as tx:
                previous = tx.execute("SELECT id FROM graph_snapshots WHERE system_id=? AND status='current'", (system_id,)).fetchall()
                for old_snapshot in previous:
                    old_snapshot_id = old_snapshot["id"]
                    tx.execute("UPDATE graph_snapshots SET status='superseded',observed_to=? WHERE id=?", (timestamp, old_snapshot_id))
                    old_patterns = tx.execute("SELECT id,validity_json,status FROM pattern_versions WHERE status IN ('candidate','trusted','stale')").fetchall()
                    for pattern in old_patterns:
                        if json.loads(pattern["validity_json"]).get("snapshot_id") != old_snapshot_id or pattern["status"] == "stale":
                            continue
                        tx.execute("UPDATE pattern_versions SET status='stale' WHERE id=?", (pattern["id"],))
                        tx.execute("INSERT INTO events(id,system_id,run_id,sequence_no,entity_type,entity_id,event_type,actor,payload_redacted_json,created_at) VALUES(?,?,NULL,?,?,?,?,?,?,?)",
                                   (str(uuid.uuid4()), system_id, 0, "pattern", pattern["id"], "pattern_stale",
                                    "system", json_text({"previous_snapshot_id": old_snapshot_id}), timestamp))
                if old:
                    tx.execute("UPDATE systems SET name=?,repo_revision=?,compose_file=?,compose_project=? WHERE id=?",
                               (display_name, repo_revision, compose_file, compose_project, system_id))
                else:
                    tx.execute("INSERT INTO systems(id,name,source_path,repo_revision,compose_file,compose_project,created_at,authorized_scope_json) VALUES(?,?,?,?,?,?,?,?)",
                               (system_id, display_name, str(source_root), repo_revision, compose_file, compose_project, timestamp,
                                json_text({"services": sorted(services)})))
                tx.execute("INSERT INTO graph_snapshots(id,system_id,version,repo_revision,deployment_digest,observed_from,observed_to,coverage_json,status) VALUES(?,?,?,?,?,?,?,?,?)",
                           (snapshot_id, system_id, version, repo_revision, deployment_digest, timestamp, timestamp, json_text(coverage), "current"))
                for item in nodes:
                    tx.execute("INSERT INTO graph_nodes(id,snapshot_id,canonical_key,kind,name,role,attributes_json) VALUES(?,?,?,?,?,?,?)",
                               (item["id"], snapshot_id, item["canonical_key"], item["kind"], item["name"], item["role"], json_text(item["attributes"])))
                for edge in edges:
                    tx.execute("INSERT INTO graph_edges(id,snapshot_id,from_node_id,to_node_id,relation,certainty,attributes_json) VALUES(?,?,?,?,?,?,?)",
                               (edge["id"], snapshot_id, edge["from_node_id"], edge["to_node_id"], edge["relation"], edge["certainty"], json_text(edge["attributes"])))
                by_name = {n["name"]: n for n in nodes if n["kind"] == "service"}
                for service_name, config in services.items():
                    subject = by_name[service_name]["id"]
                    self._claim(tx, snapshot_id, subject, "service_role", f"service:{service_name}", by_name[service_name]["role"], "compose.yaml", repo_revision)
                    for port in (config or {}).get("ports", []) or []:
                        container, published = self._port_pair(port)
                        if container:
                            self._claim(tx, snapshot_id, subject, "listen_port", f"container:{service_name}", container,
                                        f"{compose_file}:services.{service_name}.ports", repo_revision)
                        if published:
                            self._claim(tx, snapshot_id, subject, "published_port", "host", published,
                                        f"{compose_file}:services.{service_name}.ports", repo_revision)
                    if (config or {}).get("healthcheck"):
                        self._claim(tx, snapshot_id, subject, "healthcheck_declared", f"service:{service_name}", True,
                                    f"{compose_file}:services.{service_name}.healthcheck", repo_revision)
                app_node = next((n for n in nodes if n["role"] == "application"), nodes[0])
                for row in source_rows:
                    if row.get("routes") or row.get("imports") or row.get("environment_keys"):
                        self._claim(tx, snapshot_id, app_node["id"], "source_summary", f"source:{row['path']}", row,
                                    row["path"], repo_revision, truth_type="static")
                for conflict in compose_conflicts(services):
                    tx.execute("INSERT INTO graph_conflicts(id,snapshot_id,subject_key,property,scope,claim_ids_json,severity,status) VALUES(?,?,?,?,?,?,?,?)",
                               (str(uuid.uuid4()), snapshot_id, conflict["subject_key"], conflict["property"], conflict["scope"],
                                json_text(conflict["claims"]), conflict["severity"], conflict["status"]))
            return {"system_id": system_id, "snapshot_id": snapshot_id, "version": version, "repo_revision": repo_revision,
                    "deployment_digest": deployment_digest, "compose_project": compose_project, "coverage": coverage,
                    "node_count": len(nodes), "edge_count": len(edges)}
        finally:
            connection.close()

    @staticmethod
    def _port_pair(port: Any) -> tuple[int | None, int | None]:
        if isinstance(port, int):
            return port, None
        parts = str(port).split(":")
        try:
            return int(parts[-1].split("/")[0]), int(parts[-2]) if len(parts) > 1 else None
        except (TypeError, ValueError):
            return None, None

    @staticmethod
    def _claim(tx: Any, snapshot: str, node: str, prop: str, scope: str, value: Any,
               locator: str, revision: str, truth_type: str = "deployment") -> None:
        tx.execute("INSERT INTO fact_claims(id,snapshot_id,subject_node_id,property,scope,value_json,truth_type,source_locator,source_revision,observed_at,extractor_version,confidence_class) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (str(uuid.uuid4()), snapshot, node, prop, scope, json_text(value), truth_type, locator,
                    revision, now(), "inventory-v1", "parsed"))

    @staticmethod
    def _revision(root: Path, source_rows: list[dict[str, Any]]) -> str:
        try:
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                    check=True, capture_output=True, text=True, timeout=3)
            return result.stdout.strip()[:64]
        except (OSError, subprocess.SubprocessError):
            digest = hashlib.sha256("".join(f"{row['path']}:{row['content_sha256']}" for row in source_rows).encode()).hexdigest()
            return "unversioned-" + digest[:16]
