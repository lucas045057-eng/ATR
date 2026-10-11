import time
from pathlib import Path

import pytest
from app.settings import get_settings
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runtime.sqlite"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("REPOSITORY_ROOTS", str(Path(__file__).resolve().parents[1] / "demo"))
    monkeypatch.setenv("OPERATOR_TOKEN", "test-operator-" + "x" * 40)
    monkeypatch.setenv("COLLECTOR_TOKEN", "test-collector-" + "y" * 40)
    monkeypatch.setenv("EXTERNAL_MODEL_ENABLED", "false")
    monkeypatch.setenv("SSH_ENABLED", "false")
    monkeypatch.setenv("MODEL_NAME", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    monkeypatch.setenv("INITIAL_INPUT_TOKENS", "8000")
    monkeypatch.setenv("INVESTIGATION_INPUT_TOKENS", "12000")
    monkeypatch.setenv("BUDGET_TOTAL_TOKENS", "100000")
    get_settings.cache_clear()
    from app.main import app

    with TestClient(app, headers={"Authorization": "Bearer test-operator-" + "x" * 40}) as instance:
        yield instance
    get_settings.cache_clear()


def wait_job(client, job_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError("后台任务未在限定时间内完成")
