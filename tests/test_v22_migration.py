import json
import sqlite3

from app.storage.database import SCHEMA, Database
from app.storage.migrations import SQL


def test_v21_binding_upgrade_keeps_history_and_requires_fresh_verification(tmp_path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA + SQL)
    connection.execute("INSERT INTO schema_migrations VALUES(21,'2026-10-08')")
    connection.execute(
        "INSERT INTO business_profiles VALUES('b','Original business','kept',1,NULL,'2026-10-08')"
    )
    connection.execute("INSERT INTO runtime_targets VALUES('t','b','{}','2026-10-08')")
    connection.execute(
        "INSERT INTO discovery_runs VALUES('d','b','t','completed','{}','2026-10-08')"
    )
    connection.execute(
        "INSERT INTO service_bindings VALUES('bind','b','t','api','api.service','service',1,'[]','[]','confirmed','d')"
    )
    connection.execute("INSERT INTO jobs(id,business_id,kind,status,created_at) VALUES('old','b','parse','completed','2026-10-08')")
    connection.commit()
    connection.close()
    database = Database(path)
    database.initialize()
    database.initialize()  # idempotent upgrade must not duplicate or change the old confirmation.
    with database.transaction() as tx:
        binding = dict(tx.execute("SELECT * FROM service_bindings WHERE id='bind'").fetchone())
        assert binding['status'] == 'confirmed'
        assert binding['freshness_status'] == 'unverified'
        assert binding['observed_at'] is None
        assert json.loads(binding['source_json']) == {}
        assert tx.execute("SELECT status FROM jobs WHERE id='old'").fetchone()[0] == 'completed'
        assert tx.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=22").fetchone()[0] == 1
    backup_path = path.with_name(path.name + '.pre-v22.bak')
    assert backup_path.exists()
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("SELECT status FROM service_bindings WHERE id='bind'").fetchone()[0] == 'confirmed'
        assert 'freshness_status' not in {r[1] for r in backup.execute('PRAGMA table_info(service_bindings)')}
    finally:
        backup.close()
