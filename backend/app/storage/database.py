from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.storage.migrations import migrate

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS systems (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, source_path TEXT NOT NULL, repo_revision TEXT NOT NULL,
  compose_file TEXT NOT NULL, compose_project TEXT NOT NULL, created_at TEXT NOT NULL,
  authorized_scope_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS graph_snapshots (
  id TEXT PRIMARY KEY, system_id TEXT NOT NULL REFERENCES systems(id), version INTEGER NOT NULL,
  repo_revision TEXT NOT NULL, deployment_digest TEXT NOT NULL, observed_from TEXT NOT NULL,
  observed_to TEXT NOT NULL, coverage_json TEXT NOT NULL, status TEXT NOT NULL,
  UNIQUE(system_id, version), UNIQUE(id, system_id)
);
CREATE TABLE IF NOT EXISTS graph_nodes (
  id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES graph_snapshots(id),
  canonical_key TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
  attributes_json TEXT NOT NULL, UNIQUE(snapshot_id, canonical_key), UNIQUE(id, snapshot_id)
);
CREATE TABLE IF NOT EXISTS graph_edges (
  id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES graph_snapshots(id),
  from_node_id TEXT NOT NULL, to_node_id TEXT NOT NULL, relation TEXT NOT NULL,
  certainty TEXT NOT NULL, attributes_json TEXT NOT NULL,
  FOREIGN KEY(from_node_id, snapshot_id) REFERENCES graph_nodes(id, snapshot_id),
  FOREIGN KEY(to_node_id, snapshot_id) REFERENCES graph_nodes(id, snapshot_id)
);
CREATE TABLE IF NOT EXISTS fact_claims (
  id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES graph_snapshots(id),
  subject_node_id TEXT REFERENCES graph_nodes(id), property TEXT NOT NULL, scope TEXT NOT NULL,
  value_json TEXT NOT NULL, truth_type TEXT NOT NULL, source_locator TEXT NOT NULL,
  source_revision TEXT NOT NULL, observed_at TEXT NOT NULL, extractor_version TEXT NOT NULL,
  confidence_class TEXT NOT NULL, CHECK(truth_type IN ('static','deployment','runtime'))
);
CREATE INDEX IF NOT EXISTS ix_claim_subject_property ON fact_claims(snapshot_id,subject_node_id,property,scope);
CREATE TABLE IF NOT EXISTS graph_conflicts (
  id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES graph_snapshots(id),
  subject_key TEXT NOT NULL, property TEXT NOT NULL, scope TEXT NOT NULL,
  claim_ids_json TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
  resolution_review_id TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, system_id TEXT NOT NULL REFERENCES systems(id), symptom_json TEXT NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnosis_runs (
  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES incidents(id),
  graph_snapshot_id TEXT NOT NULL REFERENCES graph_snapshots(id), mode TEXT NOT NULL,
  model_config_json TEXT NOT NULL, budget_json TEXT NOT NULL, started_at TEXT NOT NULL,
  supported_at TEXT, ended_at TEXT, status TEXT NOT NULL, final_hypothesis_id TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS hypotheses (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES diagnosis_runs(id),
  target_node_id TEXT REFERENCES graph_nodes(id), failure_mode TEXT NOT NULL,
  causal_chain_json TEXT NOT NULL, falsifiable_prediction TEXT NOT NULL,
  status TEXT NOT NULL, score REAL NOT NULL CHECK(score >= 0 AND score <= 1)
);
CREATE TABLE IF NOT EXISTS diagnostic_steps (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES diagnosis_runs(id), sequence_no INTEGER NOT NULL,
  hypothesis_id TEXT REFERENCES hypotheses(id), action_json TEXT NOT NULL,
  score_before REAL NOT NULL, score_after REAL NOT NULL, review_outcome TEXT NOT NULL,
  rationale TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY, run_id TEXT REFERENCES diagnosis_runs(id), snapshot_id TEXT REFERENCES graph_snapshots(id),
  step_id TEXT REFERENCES diagnostic_steps(id), purpose TEXT NOT NULL, tool_id TEXT NOT NULL,
  adapter_version TEXT NOT NULL, args_redacted_json TEXT NOT NULL, risk TEXT NOT NULL,
  policy_version TEXT NOT NULL, status TEXT NOT NULL, request_id TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, duration_ms INTEGER, error_code TEXT
);
CREATE INDEX IF NOT EXISTS ix_tool_call_run ON tool_calls(run_id,purpose,started_at);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, tool_call_id TEXT NOT NULL REFERENCES tool_calls(id),
  target_node_id TEXT REFERENCES graph_nodes(id), observed_at TEXT NOT NULL,
  event_from TEXT, event_to TEXT, payload_redacted_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0, freshness_state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_evidence (
  step_id TEXT NOT NULL REFERENCES diagnostic_steps(id), evidence_id TEXT NOT NULL REFERENCES evidence(id),
  stance TEXT NOT NULL CHECK(stance IN ('support','refute','neutral')),
  excerpt_locator TEXT NOT NULL DEFAULT '', PRIMARY KEY(step_id,evidence_id,stance)
);
CREATE TABLE IF NOT EXISTS experiences (
  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES incidents(id),
  run_id TEXT NOT NULL REFERENCES diagnosis_runs(id), graph_snapshot_id TEXT NOT NULL REFERENCES graph_snapshots(id),
  status TEXT NOT NULL, root_cause_json TEXT NOT NULL, repair_record_json TEXT NOT NULL,
  verification_call_ids_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_experience_run ON experiences(run_id);
CREATE TABLE IF NOT EXISTS pattern_versions (
  id TEXT PRIMARY KEY, pattern_key TEXT NOT NULL, version INTEGER NOT NULL,
  source_experience_id TEXT NOT NULL REFERENCES experiences(id), status TEXT NOT NULL,
  roles_json TEXT NOT NULL, topology_json TEXT NOT NULL, symptoms_json TEXT NOT NULL,
  checks_json TEXT NOT NULL, exclusions_json TEXT NOT NULL, validity_json TEXT NOT NULL,
  schema_version INTEGER NOT NULL, content_digest TEXT NOT NULL,
  UNIQUE(pattern_key,version), UNIQUE(id,content_digest)
);
CREATE TABLE IF NOT EXISTS pattern_matches (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES diagnosis_runs(id),
  pattern_version_id TEXT NOT NULL REFERENCES pattern_versions(id), score REAL NOT NULL,
  rank INTEGER NOT NULL, binding_json TEXT NOT NULL, gate_result TEXT NOT NULL,
  decision TEXT NOT NULL, validation_outcome TEXT NOT NULL, usefulness_outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, subject_type TEXT NOT NULL,
  experience_id TEXT REFERENCES experiences(id), pattern_version_id TEXT REFERENCES pattern_versions(id),
  conflict_id TEXT REFERENCES graph_conflicts(id), subject_digest TEXT NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
  CHECK((experience_id IS NOT NULL) + (pattern_version_id IS NOT NULL) + (conflict_id IS NOT NULL) = 1)
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, system_id TEXT NOT NULL REFERENCES systems(id), run_id TEXT REFERENCES diagnosis_runs(id),
  sequence_no INTEGER NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_redacted_json TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(run_id,sequence_no)
);
CREATE TABLE IF NOT EXISTS evaluation_cases (
  id TEXT PRIMARY KEY, case_key TEXT NOT NULL, system_fixture_ref TEXT NOT NULL,
  fixture_digest TEXT NOT NULL, fault_class TEXT NOT NULL, ground_truth_json TEXT NOT NULL,
  protocol_version TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_evaluation_case_key ON evaluation_cases(case_key);
CREATE TABLE IF NOT EXISTS evaluation_trials (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES evaluation_cases(id),
  run_id TEXT NOT NULL REFERENCES diagnosis_runs(id), pair_key TEXT NOT NULL,
  arm TEXT NOT NULL, repetition INTEGER NOT NULL, reset_digest TEXT NOT NULL,
  metrics_json TEXT NOT NULL, verdict TEXT NOT NULL, started_at TEXT NOT NULL,
  UNIQUE(pair_key,arm,repetition)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE name='systems'").fetchone()
            migrated = connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
            if legacy and not migrated:
                backup_path = self.path.with_name(self.path.name + ".pre-v21.bak")
                if not backup_path.exists():
                    backup = sqlite3.connect(backup_path)
                    try:
                        connection.backup(backup)
                    finally:
                        backup.close()
            if migrated and not connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=22"
            ).fetchone():
                backup_path = self.path.with_name(self.path.name + ".pre-v22.bak")
                if not backup_path.exists():
                    backup = sqlite3.connect(backup_path)
                    try:
                        connection.backup(backup)
                    finally:
                        backup.close()
            connection.executescript(SCHEMA)
            migrate(connection)
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
