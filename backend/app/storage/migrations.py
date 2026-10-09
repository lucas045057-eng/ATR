"""Additive V2.1 migration. Never drops or rewrites V1 history."""

SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_profiles(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, current_version INTEGER NOT NULL,
 system_id TEXT REFERENCES systems(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_profile_versions(
 business_id TEXT REFERENCES business_profiles(id), version INTEGER NOT NULL, profile_json TEXT NOT NULL,
 content_digest TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(business_id,version));
CREATE TABLE IF NOT EXISTS uploaded_artifacts(
 id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES business_profiles(id), filename TEXT NOT NULL,
 content_digest TEXT NOT NULL, bytes INTEGER NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
 slot TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_files(
 id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES uploaded_artifacts(id), business_id TEXT NOT NULL REFERENCES business_profiles(id),
 path TEXT NOT NULL, content_digest TEXT NOT NULL, bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
 parse_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_artifact_active ON artifact_files(business_id,active,path);
CREATE TABLE IF NOT EXISTS source_index(
 file_id TEXT PRIMARY KEY REFERENCES artifact_files(id), business_id TEXT NOT NULL REFERENCES business_profiles(id),
 module TEXT NOT NULL, symbols_json TEXT NOT NULL, search_text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS source_search USING fts5(file_id UNINDEXED,business_id UNINDEXED,search_text);
CREATE TABLE IF NOT EXISTS business_fact_claims(
 id TEXT NOT NULL, business_id TEXT NOT NULL REFERENCES business_profiles(id), version INTEGER NOT NULL,
 claim_json TEXT NOT NULL, PRIMARY KEY(business_id,version,id));
CREATE TABLE IF NOT EXISTS business_reviews(
 id TEXT PRIMARY KEY,business_id TEXT NOT NULL REFERENCES business_profiles(id),version INTEGER NOT NULL,
 subject_id TEXT NOT NULL,subject_digest TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,
 actor TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_targets(
 id TEXT PRIMARY KEY,business_id TEXT NOT NULL REFERENCES business_profiles(id),config_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS service_bindings(
 id TEXT PRIMARY KEY,business_id TEXT NOT NULL REFERENCES business_profiles(id),target_id TEXT NOT NULL REFERENCES runtime_targets(id),
 service TEXT NOT NULL,runtime_id TEXT NOT NULL,kind TEXT NOT NULL,score REAL NOT NULL,
 reasons_json TEXT NOT NULL,evidence_ids_json TEXT NOT NULL,status TEXT NOT NULL,discovery_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_runs(
 id TEXT PRIMARY KEY,business_id TEXT NOT NULL REFERENCES business_profiles(id),target_id TEXT NOT NULL REFERENCES runtime_targets(id),
 status TEXT NOT NULL,summary_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_observations(
 id TEXT PRIMARY KEY,target_id TEXT NOT NULL REFERENCES runtime_targets(id),tool_id TEXT NOT NULL,
 args_json TEXT NOT NULL,status TEXT NOT NULL,payload_json TEXT NOT NULL,content_digest TEXT NOT NULL,
 observed_at TEXT NOT NULL,bytes INTEGER NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS context_budgets(
 id TEXT PRIMARY KEY,business_id TEXT REFERENCES business_profiles(id),run_id TEXT REFERENCES diagnosis_runs(id),
 phase TEXT NOT NULL,limits_json TEXT NOT NULL,calls INTEGER NOT NULL DEFAULT 0,
 reserved_tokens INTEGER NOT NULL DEFAULT 0,input_tokens INTEGER NOT NULL DEFAULT 0,
 output_tokens INTEGER NOT NULL DEFAULT 0,retrieved_bytes INTEGER NOT NULL DEFAULT 0,
 tool_calls INTEGER NOT NULL DEFAULT 0,stop_reason TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_usage_records(
 id TEXT PRIMARY KEY,budget_id TEXT NOT NULL REFERENCES context_budgets(id),model TEXT NOT NULL,
 input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,usage_kind TEXT NOT NULL,
 cost REAL,cost_kind TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,business_id TEXT REFERENCES business_profiles(id),kind TEXT NOT NULL,
 status TEXT NOT NULL,result_json TEXT NOT NULL DEFAULT '{}',error TEXT,created_at TEXT NOT NULL,ended_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_business_job ON jobs(business_id)
 WHERE status IN ('queued','running') AND business_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_investigation_job ON jobs(kind)
 WHERE status IN ('queued','running') AND kind LIKE 'investigate:%';
CREATE TABLE IF NOT EXISTS hypothesis_evidence(
 hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id),evidence_id TEXT NOT NULL REFERENCES evidence(id),
 stance TEXT NOT NULL,explanation TEXT NOT NULL,PRIMARY KEY(hypothesis_id,evidence_id,stance));
"""


def migrate(connection):
    connection.executescript(SQL)
    connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(21,datetime('now'))")
