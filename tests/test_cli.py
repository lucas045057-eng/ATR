import json
import socket
import sqlite3
import tarfile
from pathlib import Path

import pytest
from app import __version__, cli
from app.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
    (tmp_path / "demo").mkdir()
    monkeypatch.setenv("ATR_ROOT", str(tmp_path))
    return tmp_path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def env_token(project: Path) -> str:
    line = next(
        line for line in (project / ".env").read_text(encoding="utf-8").splitlines()
        if line.startswith("COLLECTOR_TOKEN=")
    )
    return line.split("=", 1)[1]


def test_init_generates_random_token_and_directories(project, capsys):
    assert cli.main(["init"]) == 0
    token = env_token(project)
    assert len(token) >= 32
    assert "replace-this" not in token
    assert (project / "data" / "artifacts").is_dir()
    assert (project / "targets" / ".gitkeep").is_file()
    output = capsys.readouterr().out
    assert "scripts" in output
    assert "serve" in output


def test_init_does_not_overwrite_existing_env(project, capsys):
    env = project / ".env"
    original = "COLLECTOR_TOKEN=" + "x" * 40 + "\n"
    env.write_text(original, encoding="utf-8")
    assert cli.main(["init"]) == 0
    assert env.read_text(encoding="utf-8") == original
    assert "已存在" in capsys.readouterr().out


def test_doctor_warns_without_dist_and_exits_zero(project, capsys):
    result = cli.main(["doctor", "--port", str(free_port()), "--json"])
    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["checks"]}
    assert result == 0
    assert payload["ok"] is True
    assert checks["frontend/dist"]["status"] == "WARN"


def test_doctor_fails_when_port_is_busy(project, capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        result = cli.main(["doctor", "--port", str(port), "--json"])
        payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["checks"]}
    assert result == 1
    assert payload["ok"] is False
    assert checks[f"端口 {port}"]["status"] == "FAIL"


def test_backup_produces_integrity_checked_database(project, capsys):
    data = project / "data"
    data.mkdir(parents=True)
    (data / "artifacts").mkdir()
    Database(data / "runtime.sqlite3").initialize()
    destination = project / "backup"
    assert cli.main(["backup", "--out", str(destination), "--data", str(data)]) == 0
    assert (destination / "manifest.json").is_file()
    with sqlite3.connect(destination / "runtime.sqlite3") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert "备份完成" in capsys.readouterr().out


def test_backup_uses_configured_database_and_artifact_root(project, capsys):
    data = project / "custom-data"
    data.mkdir()
    database = data / "chosen.sqlite3"
    Database(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('ACTIVE_CONFIGURED_DB')")
    stale = project / "data"
    stale.mkdir()
    (stale / "runtime.sqlite3").write_bytes(b"not a database")
    artifacts = project / "other-artifacts"
    (artifacts / "nested").mkdir(parents=True)
    (artifacts / "nested" / "note.txt").write_text("artifact-content", encoding="utf-8")
    (project / ".env").write_text(
        "DATABASE_PATH=./custom-data/chosen.sqlite3\n"
        "ARTIFACT_ROOT=./other-artifacts\n"
        "REPOSITORY_ROOTS=./demo\n",
        encoding="utf-8",
    )
    destination = project / "backup"
    assert cli.main(["backup", "--out", str(destination)]) == 0
    with sqlite3.connect(destination / "runtime.sqlite3") as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "ACTIVE_CONFIGURED_DB"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["application_sha"] == __version__
    assert manifest["application_sha_kind"] == "version"
    assert [entry["path"] for entry in manifest["artifacts"]] == ["artifacts/nested/note.txt"]
    with tarfile.open(destination / "artifacts.tar") as archive:
        member = archive.extractfile("artifacts/nested/note.txt")
        assert member is not None
        assert member.read().decode("utf-8") == "artifact-content"
    assert "备份完成" in capsys.readouterr().out


def test_backup_refuses_invalid_config_without_creating_destination(project, capsys):
    (project / ".env").write_text("REPOSITORY_ROOTS=./missing-root\n", encoding="utf-8")
    destination = project / "backup-invalid"
    assert cli.main(["backup", "--out", str(destination)]) == 1
    assert not destination.exists()
    output = capsys.readouterr().out
    assert "无法解析配置" in output
    assert "REPOSITORY_ROOTS" in output


def test_backup_failure_removes_partial_destination(project, monkeypatch, capsys):
    destination = project / "backup-partial"

    def fail(*args, **kwargs):
        destination.mkdir()
        (destination / "junk").write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(cli._backup_module(), "backup", fail)
    assert cli.main(["backup", "--out", str(destination)]) == 1
    assert not destination.exists()
    assert "已清理" in capsys.readouterr().out


def test_init_failure_rolls_back_generated_files(project, monkeypatch, capsys):
    real_mkdir = Path.mkdir

    def fail_on_targets(self, *args, **kwargs):
        if self.name == "targets":
            raise OSError("disk full")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_on_targets)
    assert cli.main(["init"]) == 1
    assert not (project / ".env").exists()
    assert not (project / "data").exists()
    assert "已回滚" in capsys.readouterr().out


def write_dist(project, manifest=None):
    dist = project / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<div id=\"root\"></div>", encoding="utf-8")
    if manifest is not None:
        (dist / "build.json").write_text(json.dumps(manifest), encoding="utf-8")


def doctor_checks(project, capsys):
    result = cli.main(["doctor", "--port", str(free_port()), "--json"])
    payload = json.loads(capsys.readouterr().out)
    return result, payload, {check["name"]: check for check in payload["checks"]}


def test_doctor_warns_when_build_manifest_is_missing(project, capsys):
    write_dist(project)
    result, payload, checks = doctor_checks(project, capsys)
    assert result == 0
    assert payload["ok"] is True
    assert checks["前端指纹"]["status"] == "WARN"
    assert "build.json" in checks["前端指纹"]["detail"]


def test_doctor_warns_on_frontend_version_mismatch(project, capsys):
    write_dist(project, {"version": "0.0.1", "commit": "", "built_at": "2026-01-01T00:00:00Z"})
    result, payload, checks = doctor_checks(project, capsys)
    assert result == 0
    assert payload["ok"] is True
    assert checks["前端指纹"]["status"] == "WARN"
    assert "版本不一致" in checks["前端指纹"]["detail"]


def test_doctor_warns_on_frontend_commit_mismatch(project, capsys, monkeypatch):
    write_dist(project, {"version": __version__, "commit": "a" * 40, "built_at": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr(cli, "_application_sha", lambda root: ("b" * 40, "git"))
    result, payload, checks = doctor_checks(project, capsys)
    assert result == 0
    assert payload["ok"] is True
    assert checks["前端指纹"]["status"] == "WARN"
    assert "commit" in checks["前端指纹"]["detail"]


def test_version_prints_dist_fingerprint(project, capsys):
    write_dist(project, {"version": __version__, "commit": "a" * 40, "built_at": "2026-01-01T00:00:00Z"})
    assert cli.main(["version"]) == 0
    output = capsys.readouterr().out
    assert "前端：" in output
    assert __version__ in output
    assert "a" * 12 in output
    assert "2026-01-01T00:00:00Z" in output


def test_serve_forces_single_worker_and_passes_host_port(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    calls = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda target, **kwargs: calls.update(target=target, **kwargs))
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "8013"]) == 0
    assert calls == {"target": "app.main:app", "host": "127.0.0.1", "port": 8013, "workers": 1}
    assert "警告" in capsys.readouterr().out
