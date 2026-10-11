import json

import pytest
from app.business.static import inspect_file
from app.settings import get_settings
from app.storage.database import Database
from test_business import onboard

PNG = b"\x89PNG\r\n\x1a\nsynthetic-no-business-facts"


@pytest.mark.parametrize("path,optional", [("assets/icons/home.png", True), ("favicon.png", True), ("app-icon.png", True),
    ("assets/decorative-dot.png", True), ("architecture.png", False), ("icons/deployment-topology.png", False), ("unknown.png", False)])
def test_only_explicit_resources_are_optional(tmp_path, path, optional):
    file = tmp_path / "image.png"
    file.write_bytes(PNG)
    r = inspect_file(file, path, 1000)
    assert (r["parse_state"] == "optional_static_attachment") == optional
    assert not r["services"] and not r.get("manual_text")


def test_icons_do_not_add_pending_tasks_or_facts_and_undo_is_versioned(client):
    profile = onboard(client, {"icon.png": PNG, "favicon.png": PNG, "architecture.png": PNG})
    assert profile["coverage"]["optional_attachments"] == 2 and profile["coverage"]["pending_files"] == 1
    assert not profile["facts"]
    ref = next(r for r in profile["source_references"] if r["path"] == "icon.png")
    base = f"/api/v1/businesses/{profile['business_id']}"
    before_hashes = {r["id"]: r["content_digest"] for r in profile["source_references"]}
    response = client.post(base + "/files/media-batch-review", json={"file_ids": [ref["id"]], "action": "restore", "expected_version": profile["profile_version"]})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert next(r for r in restored["source_references"] if r["id"] == ref["id"])["parse_state"] == "pending_manual"
    assert restored["profile_version"] == profile["profile_version"] + 1 and restored["coverage"]["pending_files"] == 2
    assert {r["id"]: r["content_digest"] for r in restored["source_references"]} == before_hashes
    old = client.get(base + f"?version={profile['profile_version']}").json()
    assert old["coverage"]["optional_attachments"] == 2
    rejected = client.post(base + "/files/media-batch-review", json={"file_ids": [ref["id"]], "action": "optional", "expected_version": profile["profile_version"]})
    assert rejected.status_code == 422
    manual = client.post(base + f"/files/{ref['id']}/media-review", json={"text": "人工说明图标与业务界面的对应关系", "expected_version": restored["profile_version"]}).json()
    assert any(f["source_type"] == "Documented" for f in manual["facts"])
    changed = client.post(base + "/files/media-batch-review", json={"file_ids": [ref["id"]], "action": "optional", "expected_version": manual["profile_version"]}).json()
    assert any(f["object"] == "人工说明图标与业务界面的对应关系" for f in changed["facts"])
    undo = client.post(base + "/files/media-batch-review", json={"file_ids": [ref["id"]], "action": "restore", "expected_version": changed["profile_version"]}).json()
    assert next(r for r in undo["source_references"] if r["id"] == ref["id"])["parse_state"] == "complete"
    with Database(get_settings().database_path).connect() as c:
        parsed = json.loads(c.execute("SELECT parse_json FROM artifact_files WHERE id=?", (ref["id"],)).fetchone()[0])
        assert parsed["manual_text"] == "人工说明图标与业务界面的对应关系" and len(parsed["optional_history"]) >= 2
        assert c.execute("SELECT count(*) FROM business_reviews WHERE subject_id='media_batch'").fetchone()[0] == 3


def test_historical_pending_icons_batch_preserves_diagrams_and_atomic_denial(client):
    profile = onboard(client, {"icon.png": PNG, "favicon.png": PNG, "architecture.png": PNG})
    db = Database(get_settings().database_path)
    with db.transaction() as c:
        for row in c.execute("SELECT * FROM artifact_files").fetchall():
            parsed = json.loads(row["parse_json"])
            parsed.update(parse_state="pending_manual")
            parsed.pop("optional_previous", None)
            c.execute("UPDATE artifact_files SET parse_json=? WHERE id=?", (json.dumps(parsed), row["id"]))
    from app.business.service import BusinessService
    profile = BusinessService(db, get_settings()).rebuild(profile["business_id"])
    icons = [r["id"] for r in profile["source_references"] if r["path"] != "architecture.png"]
    diagram = next(r["id"] for r in profile["source_references"] if r["path"] == "architecture.png")
    base = f"/api/v1/businesses/{profile['business_id']}"
    invalid = client.post(base + "/files/media-batch-review", json={"file_ids": [*icons, diagram], "action": "optional", "expected_version": profile["profile_version"]})
    assert invalid.status_code == 422
    assert client.get(base).json()["coverage"]["pending_files"] == 3
    result = client.post(base + "/files/media-batch-review", json={"file_ids": icons, "action": "optional", "expected_version": profile["profile_version"]}).json()
    assert result["coverage"]["pending_files"] == 1 and not result["facts"]
    assert next(r for r in result["source_references"] if r["id"] == diagram)["parse_state"] == "pending_manual"
