"""Local fake HTTP only: model failure must not affect the API process."""

import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from app.context.budget import ContextBudgetManager
from app.context.model import call_json
from app.settings import Settings, get_settings
from app.storage.database import Database
from conftest import wait_job
from pydantic import SecretStr
from test_business import onboard


@pytest.fixture
def fake_model(tmp_path):
    state = {"status": 200, "invalid": False, "bad_schema": False, "delay": 0, "requests": 0}
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state['wire'], state['path'] = request, self.path
            state["requests"] += 1
            received.set()
            time.sleep(state["delay"])
            content = "invalid json" if state["invalid"] else json.dumps({"summary": "Catalog mock understanding", "inferences": [], "business_flows": [], "unknowns": []})
            if state["bad_schema"]:
                content = json.dumps({"summary": [], "inferences": [], "business_flows": None})
            if state.get("reply"):
                content = json.dumps(state["reply"](json.loads(request["messages"][1]["content"]), state["requests"]))
            body = {"error": state.get("error", {"message": "Bearer synthetic-secret NEVER-ECHO-REQUEST", "type": "fixture"})} if state["status"] != 200 else {
                "id": "local-fixture", "object": "chat.completion", "created": 1, "model": "gpt-6.1-sol",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
            }
            data = json.dumps(body).encode()
            if state['status'] == 200 and 'usage' in state:
                if state['usage'] == 'omit':
                    body.pop('usage')
                else:
                    body['usage'] = state['usage']
                data = json.dumps(body).encode()
            if state['status'] == 200 and 'choices' in state:
                body['choices'] = state['choices']
                data = json.dumps(body).encode()
            if 'envelope' in state:
                data = json.dumps(state['envelope']).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("x-request-id", "req-local-safe1234")
            self.end_headers()
            time.sleep(state.get('body_delay', 0))
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = Database(tmp_path / "models.sqlite")
    db.initialize()
    settings = Settings(_env_file=None, external_model_enabled=True, model_name="gpt-6.1-sol", model_api_key=SecretStr("synthetic-secret"), model_base_url=f"http://127.0.0.1:{server.server_port}/v1", model_timeout_seconds=2)
    try:
        yield db, settings, state, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_first_request_does_not_install_litellm_filters_in_host(fake_model):
    db, settings, state, _ = fake_model
    loggers = [logging.getLogger(name) for name in ("uvicorn.access", "uvicorn.error", "asyncio", "httpx")]
    before = [list(logger.filters) for logger in loggers]
    manager = ContextBudgetManager(db, settings)
    assert call_json(db, settings, manager.create("initial"), "bounded", {})["summary"] == "Catalog mock understanding"
    assert [list(logger.filters) for logger in loggers] == before
    assert state["requests"] == 1


def test_concurrent_first_requests_are_isolated_and_settled(fake_model):
    db, settings, state, _ = fake_model
    manager = ContextBudgetManager(db, settings)
    bids = [manager.create("initial") for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(call_json, db, settings, bid, "bounded", {}) for bid in bids]
        assert all(f.result(timeout=20)["summary"] == "Catalog mock understanding" for f in futures)
    assert state["requests"] == 3
    for bid in bids:
        usage = manager.get(bid)
        assert usage["reserved_tokens"] == 0 and usage["calls"] == 1
        assert usage["usage_records"][0]["usage_kind"] == "actual"


@pytest.mark.parametrize("status,code", [(400, "provider_bad_request"), (401, "provider_auth_error"), (429, "provider_rate_limit"), (500, "provider_unavailable"), (504, "provider_unavailable")])
def test_provider_errors_are_classified_without_leaks(fake_model, status, code, caplog):
    db, settings, state, _ = fake_model
    state["status"] = status
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("initial")
    with pytest.raises(Exception) as caught:
        call_json(db, settings, bid, "bounded", {})
    assert caught.value.code == code
    assert caught.value.diagnostic["status_code"] == status
    assert "synthetic-secret" not in str(caught.value) + caplog.text
    assert "NEVER-ECHO-REQUEST" not in str(caught.value) + caplog.text
    usage = manager.get(bid)
    assert usage["reserved_tokens"] == 0 and usage["calls"] == state["requests"] == 1
    assert usage["usage_records"][0]["status"] == "failed"
    assert usage["usage_records"][0]["usage_kind"] == "estimate"


def test_invalid_json_retains_actual_usage_and_releases_reservation(fake_model):
    db, settings, state, _ = fake_model
    state["invalid"] = True
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("initial")
    with pytest.raises(Exception) as caught:
        call_json(db, settings, bid, "bounded", {})
    assert caught.value.code == "model_invalid_json"
    result = manager.get(bid)
    assert result["reserved_tokens"] == 0
    assert result["usage_records"][0]["status"] == "failed"
    assert result["usage_records"][0]["usage_kind"] == "actual"
    assert (result["input_tokens"], result["output_tokens"]) == (11, 5)


@pytest.mark.parametrize("exception,code", [(ImportError, "model_import_error"), (RuntimeError, "model_internal_error")])
def test_internal_errors_are_safe_and_settled(fake_model, monkeypatch, exception, code):
    db, settings, _, _ = fake_model

    def fail(**_):
        raise exception("synthetic-secret SECRET-REQUEST-BODY")

    monkeypatch.setattr("app.context.model._completion", fail)
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("initial")
    with pytest.raises(Exception) as caught:
        call_json(db, settings, bid, "bounded", {})
    assert caught.value.code == code
    assert caught.value.diagnostic["exception_type"] == exception.__name__
    assert "synthetic-secret" not in str(caught.value)
    assert manager.get(bid)["reserved_tokens"] == 0


def configure_client(settings):
    active = get_settings()
    active.external_model_enabled = True
    active.model_name = settings.model_name
    active.model_api_key = settings.model_api_key
    active.model_base_url = settings.model_base_url
    active.model_timeout_seconds = settings.model_timeout_seconds


def test_worker_import_failure_does_not_poison_next_request(fake_model, tmp_path, monkeypatch):
    db, settings, state, _ = fake_model
    shadow = tmp_path / "litellm.py"
    shadow.write_text("raise ImportError('synthetic-secret SECRET-REQUEST-BODY')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    manager = ContextBudgetManager(db, settings)
    with pytest.raises(Exception) as caught:
        call_json(db, settings, manager.create("initial"), "bounded", {})
    assert caught.value.code == "model_import_error"
    assert caught.value.diagnostic["stage"] == "initialize"
    assert caught.value.diagnostic["frames"]
    assert "synthetic-secret" not in str(caught.value)
    assert state["requests"] == 0
    shadow.unlink()
    assert call_json(db, settings, manager.create("initial"), "bounded", {})["summary"]
    assert state["requests"] == 1


def test_hung_import_is_killed_at_outer_deadline_and_budget_is_released(fake_model, tmp_path, monkeypatch):
    db, settings, state, _ = fake_model
    (tmp_path / "litellm.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    children = []
    original = subprocess.Popen

    def record_child(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", record_child)
    manager = ContextBudgetManager(db, settings)
    bid = manager.create("initial")
    started = time.monotonic()
    with pytest.raises(Exception) as caught:
        call_json(db, settings, bid, "bounded", {})
    assert caught.value.code == "model_timeout" and caught.value.diagnostic["stage"] == "initialize"
    assert time.monotonic() - started < settings.model_timeout_seconds + 13
    assert len(children) == 1 and children[0].poll() is not None
    assert state["requests"] == 0
    budget = manager.get(bid)
    assert budget["reserved_tokens"] == 0 and budget["calls"] == 1
    assert budget["usage_records"][0]["status"] == "failed"


def test_timeout_preserves_health_business_api_and_allows_next_task(client, fake_model):
    _, settings, state, received = fake_model
    profile = onboard(client)
    base = f"/api/v1/businesses/{profile['business_id']}"
    configure_client(settings)
    state["delay"] = 4
    task = client.post(base + "/analyze").json()
    assert received.wait(12), "Fake local provider did not receive request"
    start = time.monotonic()
    assert client.get("/api/v1/health").status_code == 200
    assert client.get(base).status_code == 200
    assert time.monotonic() - start < 1
    duplicate = client.post(base + "/analyze")
    assert duplicate.status_code == 409
    failed = wait_job(client, task["job_id"], timeout=15)
    assert failed["status"] == "failed"
    assert failed["result"]["model_status"] == "model_error:model_timeout"
    assert failed["result"]["model_error"]["exception_type"]
    assert client.get(base).json()["model_status"] == "model_error:model_timeout"
    budget = client.get(base + "/budgets").json()[0]
    assert budget["calls"] == 1 and budget["reserved_tokens"] == 0
    assert budget["usage_records"][0]["status"] == "failed"
    assert budget["usage_records"][0]["usage_kind"] == "estimate"
    state["delay"] = 0
    success = wait_job(client, client.post(base + "/analyze").json()["job_id"], timeout=15)
    assert success["status"] == "completed" and success["result"]["model_status"] == "completed"
    assert client.get(base).json()["model_error"] is None
    budget = client.get(base + "/budgets").json()[0]
    assert budget["calls"] == 2 and budget["reserved_tokens"] == 0
    assert [r["status"] for r in budget["usage_records"]] == ["failed", "succeeded"]


def test_budget_edit_is_read_only_and_analysis_still_requests_provider(client, fake_model):
    _, settings, state, _ = fake_model
    profile = onboard(client)
    base = f"/api/v1/businesses/{profile['business_id']}"
    configure_client(settings)
    budget = client.get(base + "/budgets").json()[0]
    response = client.put(base + "/budgets/" + budget["id"], json={"max_input_tokens": 1})
    assert response.status_code == 409
    job = wait_job(client, client.post(base + "/analyze").json()["job_id"])
    assert job["status"] == "completed" and job["result"]["model_status"] == "completed"
    budget = client.get(base + "/budgets").json()[0]
    assert budget["calls"] == state["requests"] == 1 and budget["reserved_tokens"] == 0
    assert budget["usage_records"][0]["status"] == "succeeded"


def test_wrong_profile_schema_marks_actual_usage_failed_and_allows_recovery(client, fake_model):
    _, settings, state, _ = fake_model
    profile = onboard(client)
    base = f"/api/v1/businesses/{profile['business_id']}"
    configure_client(settings)
    state["bad_schema"] = True
    job = wait_job(client, client.post(base + "/analyze").json()["job_id"], timeout=15)
    assert job["status"] == "failed" and job["result"]["model_status"] == "model_error:model_schema_error"
    assert job["result"]["model_error"]["stage"] == "validate"
    budget = client.get(base + "/budgets").json()[0]
    assert budget["calls"] == 1 and budget["reserved_tokens"] == 0
    record = budget["usage_records"][0]
    assert record["status"] == "failed" and record["usage_kind"] == "actual"
    assert (record["input_tokens"], record["output_tokens"]) == (11, 5)
    state["bad_schema"] = False
    success = wait_job(client, client.post(base + "/analyze").json()["job_id"], timeout=15)
    assert success["status"] == "completed" and success["result"]["model_error"] is None
    assert state["requests"] == 2
