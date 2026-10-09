import io
import json
import sys
import tarfile
from types import SimpleNamespace

import pytest
from app.business.ingestion import UnsafeArtifact, unpack
from app.settings import get_settings
from app.tools.redaction import redact_text
from conftest import wait_job
from pydantic import SecretStr
from test_business import onboard
from test_ingestion import LIMITS


@pytest.mark.parametrize("extension,mode", [(".tar", "w"), (".tar.gz", "w:gz"), (".tgz", "w:gz")])
def test_tar_and_gzip_with_normal_dot_root(tmp_path, extension, mode):
    source = tmp_path / ("source" + extension)
    with tarfile.open(source, mode) as t:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        t.addfile(root)
        entry = tarfile.TarInfo("./src/app.py")
        content = b"raise RuntimeError('never execute')"
        entry.size = len(content)
        t.addfile(entry, io.BytesIO(content))
    assert unpack(source, tmp_path / "files", source.name, LIMITS)[0]["path"] == "src/app.py"


@pytest.mark.parametrize(
    "text",
    [
        'password="private value with spaces"',
        "DB_PASSWORD=private value with spaces",
        '{"api_key": "private value with spaces"}',
        "Authorization: Bearer sensitive-token",
        "postgres://user:secret@store/app",
    ],
)
def test_redaction_preserves_json_and_masks_spaces(text):
    redacted = redact_text(text)
    assert "private value" not in redacted
    assert "sensitive-token" not in redacted
    if text.startswith("{"):
        assert json.loads(redacted)["api_key"] == "[REDACTED]"


def test_private_key_is_not_stored_or_echoed_on_target_validation(client):
    profile = onboard(client)
    private = "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/targets",
        json={"name": "invalid", "host_public_key": private},
    )
    assert response.status_code == 422
    assert "private-material" not in response.text
    assert client.get(f"/api/v1/businesses/{profile['business_id']}/targets").json() == []


def test_api_budget_stop_retains_static_and_resume(client, monkeypatch):
    profile = onboard(client)
    base = f"/api/v1/businesses/{profile['business_id']}"
    settings = get_settings()
    settings.external_model_enabled = True
    settings.model_name = "unit-test-provider"
    settings.model_api_key = SecretStr("unit-test-key")
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        data = {
            "summary": "Catalog service",
            "inferences": [],
            "business_flows": [],
            "unknowns": ["运行版本仍需验证"],
        }
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))],
            usage=SimpleNamespace(prompt_tokens=400, completion_tokens=100),
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    budget = client.get(base + "/budgets").json()[0]
    limits = {**budget["limits"], "max_input_tokens": 10}
    assert client.put(base + f"/budgets/{budget['id']}", json=limits).status_code == 200
    job = client.post(base + "/analyze").json()
    assert wait_job(client, job["job_id"])["status"] == "completed"
    latest = client.get(base).json()
    assert latest["model_status"].startswith("budget_exceeded")
    assert latest["services"] == profile["services"]
    assert not calls
    client.put(base + f"/budgets/{budget['id']}", json={**limits, "max_input_tokens": 8000})
    job = client.post(base + "/analyze").json()
    assert wait_job(client, job["job_id"])["status"] == "completed"
    assert client.get(base).json()["model_status"] == "completed"
    assert len(calls) == 1
    assert "def record" not in calls[0]["messages"][1]["content"]


def test_file_count_depth_and_declared_upload_limit(client, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("x")
    with pytest.raises(UnsafeArtifact):
        unpack(source, tmp_path / "deep", "a/b/c/file.txt", {**LIMITS, "depth": 2})
    profile = onboard(client)
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/artifacts?filename=big.zip",
        content=b"x",
        headers={"Content-Length": str(100000000)},
    )
    assert response.status_code == 413


def test_code_access_is_scoped_to_business(client):
    profile = onboard(client)
    another = client.post("/api/v1/businesses", json={"name": "other"}).json()
    file_id = next(r["id"] for r in profile["source_references"] if r["media_type"] == "code")
    assert (
        client.post(
            f"/api/v1/businesses/{another['business_id']}/code/context", json={"file_id": file_id}
        ).status_code
        == 404
    )
