import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.api.v1 import pattern_matches
from app.common import dumps, now, uid
from app.settings import Settings
from app.storage.database import SCHEMA, Database
from app.system.inventory import SystemOnboarding

from collector.app import main as collector
from collector.app.main import processes


def seed(db, snapshot_id, system_id):
    incident, run, experience = uid(), uid(), uid()
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO incidents VALUES(?,?,?, ?,NULL,'open',?)",
            (incident, system_id, dumps({"text": "startup failure"}), now(), now()),
        )
        tx.execute(
            "INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status) VALUES(?,?,?,'normal','{}','{}',?,'supported')",
            (run, incident, snapshot_id, now()),
        )
        tx.execute(
            "INSERT INTO experiences VALUES(?,?,?,?,'verified','{}','{}','[]',?)",
            (experience, incident, run, snapshot_id, now()),
        )
        ids = []
        for role, terms in [("application", ["startup", "failure"]), ("datastore", ["other"])]:
            pid = uid()
            ids.append(pid)
            tx.execute(
                "INSERT INTO pattern_versions VALUES(?,?,1,?,'trusted',?,'[]',?,?,'[]',?,1,?)",
                (
                    pid,
                    role,
                    experience,
                    dumps([role]),
                    dumps(terms),
                    dumps(
                        [{"tool_id": "container.logs", "target_role": role, "purpose": "diagnosis"}]
                    ),
                    dumps({"snapshot_id": snapshot_id}),
                    uid(),
                ),
            )
    return run, ids


def test_pattern_state_does_not_leak_across_loop_and_equivalent_graph(tmp_path):
    db = Database(tmp_path / "runtime.sqlite")
    db.initialize()
    root = Path(__file__).resolve().parents[1] / "demo"
    onboarding = SystemOnboarding(db, Settings(repository_roots=str(root)))
    result = onboarding.create_snapshot("system-a", "A", "compose.yaml")
    run, ids = seed(db, result["snapshot_id"], result["system_id"])
    with db.transaction() as tx:
        matches = pattern_matches(
            tx,
            run,
            result["snapshot_id"],
            "startup failure",
            {"application", "datastore"},
            set(),
            {("container.logs", "application")},
            False,
            {"application": ["app"], "datastore": ["db"]},
        )
        assert matches[0]["decision"] == "suggested"
        assert matches[0]["binding"]["roles"] == ["application"]
        assert matches[1]["binding"]["roles"] == ["datastore"]
    onboarding.create_snapshot("system-a", "A", "compose.yaml")
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT status FROM pattern_versions WHERE id=?", (ids[0],)).fetchone()[0]
            == "trusted"
        )
    finally:
        conn.close()


def test_docker_titles_and_process_rows(monkeypatch):
    monkeypatch.setattr(collector, "TOKEN", "x" * 40)
    container = SimpleNamespace(
        id="a" * 64,
        top=lambda ps_args: {
            "Titles": ["USER", "PID", "%CPU", "COMMAND"],
            "Processes": [["root", "101", "1.2", "python app.py"]],
        },
    )
    monkeypatch.setattr(collector, "resolve_container", lambda p, c: container)
    result = processes("test", "a" * 64, "Bearer " + "x" * 40)
    assert result["processes"] == [
        {"USER": "root", "PID": "101", "%CPU": "1.2", "COMMAND": "python app.py"}
    ]


def test_additive_migration_backup_and_original_history(tmp_path):
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO systems VALUES('legacy','Original','/demo','old','compose.yaml','old','2026-01-01','{}')"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.initialize()
    db.initialize()
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT name FROM systems WHERE id='legacy'").fetchone()[0] == "Original"
        )
        assert conn.execute("SELECT version FROM schema_migrations").fetchone()[0] == 21
        assert path.with_name(path.name + ".pre-v21.bak").is_file()
    finally:
        conn.close()


def test_format_only_compose_update_keeps_trusted_pattern(tmp_path):
    project = tmp_path / "repositories" / "example"
    project.mkdir(parents=True)
    compose = project / "compose.yaml"
    compose.write_text("services:\n  api: {build: ., depends_on: [db]}\n  db: {image: mysql:8.4}\n")
    db = Database(tmp_path / "runtime.sqlite")
    db.initialize()
    onboarding = SystemOnboarding(db, Settings(repository_roots=str(project.parent)))
    first = onboarding.create_snapshot("example", "Example", "compose.yaml")
    _, ids = seed(db, first["snapshot_id"], first["system_id"])
    compose.write_text(compose.read_text() + "\n# formatting-only change\n")
    second = onboarding.create_snapshot("example", "Example", "compose.yaml")
    assert second["deployment_digest"] != first["deployment_digest"]
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT status FROM pattern_versions WHERE id=?", (ids[0],)).fetchone()[0]
            == "trusted"
        )
    finally:
        conn.close()
