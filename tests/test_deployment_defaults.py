import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from app import __version__
from app.settings import Settings, get_settings
from app.storage.database import Database
from app.tools.registry import CollectorClient, ToolRegistry
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_TOKEN = "replace-this-with-another-random-secret-at-least-32-characters"


def test_health_reports_diagnostics(client, monkeypatch):
    monkeypatch.setenv("ATR_BUILD", "test-build-123")
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ready"
    assert body["service"] == "adaptive-troubleshooting-runtime"
    assert body["version"] == __version__
    assert body["build"] == "test-build-123"
    assert body["decision_protocol"] == "legacy_v22"
    assert body["data_root"]
    assert isinstance(body["ui"], bool)


def test_health_degrades_without_leaking_configuration(client, monkeypatch):
    import app.api.health as health_module

    monkeypatch.setenv("ATR_BUILD", "test-build-123")

    def broken_settings():
        raise ValueError("Configured repository root does not exist: C:\\private\\path")

    monkeypatch.setattr(health_module, "get_settings", broken_settings)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["config_error"] == "ValueError"
    assert body["service"] == "adaptive-troubleshooting-runtime"
    assert body["version"] == __version__
    assert body["build"] == "test-build-123"
    assert "data_root" not in body
    assert "ui" not in body
    assert "private" not in response.text


def test_lifespan_rejects_invalid_configuration_with_actionable_message(monkeypatch):
    import app.main as main_module

    def broken_settings():
        raise ValueError("Configured repository root does not exist: C:\\private\\path")

    monkeypatch.setattr(main_module, "get_settings", broken_settings)

    async def start():
        async with main_module.lifespan(main_module.app):
            pass

    with pytest.raises(RuntimeError, match="ATR 配置无效"):
        asyncio.run(start())


def test_collector_refuses_short_or_placeholder_token(monkeypatch):
    from collector.app import main as collector

    async def start():
        async with collector.lifespan(collector.app):
            pass

    for token in ("short", PLACEHOLDER_TOKEN):
        monkeypatch.setattr(collector, "TOKEN", token)
        with pytest.raises(RuntimeError, match="COLLECTOR_TOKEN"):
            asyncio.run(start())
    monkeypatch.setattr(collector, "TOKEN", "x" * 40)
    asyncio.run(start())


def test_collector_placeholder_token_request_gets_401(monkeypatch):
    from collector.app import main as collector

    monkeypatch.setattr(collector, "TOKEN", PLACEHOLDER_TOKEN)
    response = TestClient(collector.app).get(
        "/internal/v1/projects/demo/containers",
        headers={"Authorization": f"Bearer {PLACEHOLDER_TOKEN}"},
    )
    assert response.status_code == 401


def test_missing_collector_token_only_fails_for_collector_tools(tmp_path):
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        settings = Settings(_env_file=None, collector_token="", allowed_health_urls=url)
        client = CollectorClient(settings)
        with pytest.raises(ValueError, match="COLLECTOR_TOKEN"):
            client.get("/internal/v1/projects/demo/containers")

        database = Database(tmp_path / "runtime.sqlite")
        database.initialize()
        with database.transaction() as tx:
            tx.execute("INSERT INTO systems(id,name,source_path,repo_revision,compose_file,compose_project,created_at,authorized_scope_json) VALUES('s','S','/tmp','r','compose.yaml','p','2026-01-01','{}')")
            tx.execute("INSERT INTO graph_snapshots(id,system_id,version,repo_revision,deployment_digest,observed_from,observed_to,coverage_json,status) VALUES('snap','s',1,'r','d','x','y','{}','complete')")
            tx.execute("INSERT INTO incidents(id,system_id,symptom_json,window_start,status,created_at) VALUES('i','s','{}','2026-01-01','open','2026-01-01')")
            tx.execute("INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status,summary_json) VALUES('run','i','snap','normal','{}','{}','2026-01-01','running','{}')")

        registry = ToolRegistry(database, settings)
        result = registry.execute("run", "snap", "project", "health.get", {"url": url})
        assert result["payload"]["status_code"] == 200

        with pytest.raises(HTTPException) as exc:
            registry.execute("run", "snap", "demo", "compose.status", {})
        assert exc.value.status_code == 409
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_missing_collector_token_does_not_block_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COLLECTOR_TOKEN", raising=False)
    monkeypatch.setenv("REPOSITORY_ROOTS", str(ROOT / "demo"))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.collector_token.get_secret_value() == ""
    finally:
        get_settings.cache_clear()
