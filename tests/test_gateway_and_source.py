from pathlib import Path

import pytest
from app.system import source_parser

from scripts import ssh_gateway


def test_legacy_scan_prunes_ignored_directories_before_entering(tmp_path, monkeypatch):
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "unused.js").write_text("throw Error('unused')")
    (tmp_path / "main.py").write_text("import os\n")
    original = source_parser.os.scandir
    visited = []

    def guarded(path):
        visited.append(Path(path))
        assert Path(path) != ignored
        return original(path)

    monkeypatch.setattr(source_parser.os, "scandir", guarded)
    rows, stats = source_parser.inspect_source(tmp_path, 100, 10000)
    assert [r["path"] for r in rows] == ["main.py"]
    assert stats["traversal_limit_hit"] is False
    assert ignored not in visited


def test_gateway_checks_server_policy_and_keeps_container_stderr(monkeypatch):
    calls = []

    def fake_command(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0, "[stderr]\nstartup failed"

    monkeypatch.setattr(ssh_gateway, "bounded_command", fake_command)
    cid = "a" * 64
    policy = {"docker_reads_enabled": True, "containers": [cid], "services": ["catalog-api"]}
    response = ssh_gateway.execute(policy, "container.logs", {"container_id": cid, "tail": 20})
    assert "startup failed" in response["text"]
    assert calls == [
        (["/usr/bin/docker", "logs", "--timestamps", "--tail", "20", cid], {"include_stderr": True})
    ]
    for tool, args in [
        ("shell", {"command": "echo bad"}),
        ("service.logs", {"name": "catalog-api;echo bad"}),
        ("service.status", {"name": "unrelated"}),
        ("container.logs", {"container_id": "b" * 64}),
        ("host.info", {"command": "echo bad"}),
    ]:
        with pytest.raises(PermissionError):
            ssh_gateway.execute(policy, tool, args)
    assert len(calls) == 1


def test_gateway_rejects_other_ssh_entry_before_reading_policy(monkeypatch):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "unexpected")
    monkeypatch.setattr("sys.argv", ["ssh_gateway.py", "--policy", "absent-policy.json"])
    with pytest.raises(PermissionError, match="Fixed SSH entry"):
        ssh_gateway.main()
