import io
import zipfile

from conftest import wait_job


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, value in files.items():
            z.writestr(name, value)
    return buffer.getvalue()


SAMPLE = {
    "src/api.py": "from src.store import read\n@app.get('/record')\ndef record(): return read()",
    "src/store.py": "import pymysql\ndef read(): return None",
    "web/app.js": "import express from 'express'; app.get('/health', fn)",
    "compose.yaml": "services:\n  catalog-api: {build: ., ports: ['8000:8000'], depends_on: [store]}\n  store: {image: mysql:8.4}",
    "README.md": "# Catalog\nAn isolated test project.",
}


def onboard(client, files=SAMPLE):
    response = client.post(
        "/api/v1/businesses", json={"name": "Catalog", "description": "只读商品目录"}
    )
    assert response.status_code == 201, response.text
    business = response.json()
    uploaded = client.post(
        f"/api/v1/businesses/{business['business_id']}/artifacts?filename=project.zip",
        content=archive(files),
    )
    assert uploaded.status_code == 202, uploaded.text
    job = wait_job(client, uploaded.json()["job_id"])
    assert job["status"] == "completed", job
    return client.get(f"/api/v1/businesses/{business['business_id']}").json()


def test_empty_profile_auth_and_no_fabrication(client):
    result = client.post("/api/v1/businesses", json={"name": "Empty"}).json()
    assert result["services"] == []
    assert result["coverage"]["knowledge_level"] == "insufficient"
    assert client.get("/api/v1/businesses", headers={"Authorization": ""}).status_code == 401


def test_upload_search_review_and_incremental(client):
    profile = onboard(client)
    business_id = profile["business_id"]
    base = f"/api/v1/businesses/{business_id}"
    assert len(profile["services"]) == 2
    assert len(profile["modules"]) == 3
    results = client.post(base + "/code/search", json={"keyword": "record"}).json()
    assert results
    context = client.post(
        base + "/code/context",
        json={"file_id": results[0]["file_id"], "symbol": "record", "max_lines": 2},
    ).json()
    assert "def record" in context["text"]
    relation = next(d for d in profile["dependencies"] if d["relation"] == "depends_on")
    reviewed = client.post(
        base + "/review",
        json={
            "expected_version": profile["profile_version"],
            "subject_id": relation["fact_id"],
            "decision": "approve",
            "reason": "人工核对 Compose 中的依赖声明",
        },
    ).json()
    assert (
        next(f for f in reviewed["facts"] if f["id"] == relation["fact_id"])["verification_status"]
        == "confirmed"
    )
    assert (
        client.post(
            base + "/review",
            json={"expected_version": 1, "decision": "approve", "reason": "已过期的审核尝试"},
        ).status_code
        == 422
    )
    changed = {**SAMPLE, "src/store.py": "import pymysql\ndef read(): return 1"}
    changed.pop("web/app.js")
    uploaded = client.post(
        base + "/artifacts?filename=new.zip&mode=replace&slot=source", content=archive(changed)
    ).json()
    job = wait_job(client, uploaded["job_id"])
    assert job["status"] == "completed", job
    assert job["result"]["parse_results_reused"] == 3
    latest = client.get(base).json()
    assert "src/store.py" in latest["changes"]["modified"]
    assert "web/app.js" in latest["changes"]["deleted"]
    assert (
        next(f for f in latest["facts"] if f["id"] == relation["fact_id"])["verification_status"]
        == "confirmed"
    )
    assert any(f["verification_status"] == "stale" for f in latest["facts"])
    assert client.get(base + "/versions").json()[0]["version"] == latest["profile_version"]
    history = client.post(
        base + "/code/context",
        json={"file_id": results[0]["file_id"], "profile_version": profile["profile_version"]},
    )
    assert history.status_code == 200


def test_large_irrelevant_set_and_offline_status(client):
    files = {
        **SAMPLE,
        **{f"node_modules/package/file{i}.js": "raise_error('do not execute')" for i in range(250)},
    }
    profile = onboard(client, files)
    assert len(profile["modules"]) == 3
    assert profile["coverage"]["files"] == 255
    job_id = client.post(f"/api/v1/businesses/{profile['business_id']}/analyze").json()["job_id"]
    assert wait_job(client, job_id)["status"] == "completed"
    latest = client.get(f"/api/v1/businesses/{profile['business_id']}").json()
    assert latest["model_status"] == "pending_model_configuration"
    budgets = client.get(f"/api/v1/businesses/{profile['business_id']}/budgets").json()
    assert budgets[0]["calls"] == 0


def test_malicious_upload_quarantined_without_profile_loss(client):
    profile = onboard(client)
    base = f"/api/v1/businesses/{profile['business_id']}"
    upload = client.post(
        base + "/artifacts?filename=evil.zip", content=archive({"../outside.py": "bad"})
    ).json()
    assert wait_job(client, upload["job_id"])["status"] == "failed"
    latest = client.get(base).json()
    assert latest["profile_version"] == profile["profile_version"]
    assert latest["services"] == profile["services"]


def test_damaged_media_can_receive_explicit_manual_annotation(client):
    profile = onboard(client, {"architecture.png": b"\x89PNG\r\n\x1a\nplaceholder"})
    ref = profile["source_references"][0]
    assert ref["parse_state"] == "media_local_failed"
    result = client.post(
        f"/api/v1/businesses/{profile['business_id']}/files/{ref['id']}/media-review",
        json={
            "text": "人工核对图中服务之间的部署结构",
            "expected_version": profile["profile_version"],
        },
    ).json()
    assert any(f["source_type"] == "Documented" for f in result["facts"])
    assert result["services"] == []
