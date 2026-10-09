"""Linux process boundary fixtures; real gateway parsing, bounds and paging on Windows."""

import json
from types import SimpleNamespace

import pytest
from app.connectors.mock import MockConnector
from app.connectors.models import ListArgs, RuntimeTargetCreate
from app.connectors.policy import check
from pydantic import ValidationError

from scripts import ssh_gateway


class NativeStreams:
    """Replace Linux pipe selection only; exercise the production streaming collector."""

    def __init__(self, monkeypatch, text, *, stall=False, code=0, stderr=b""):
        self.text = text.encode()
        self.stderr = stderr
        self.stall, self.code = stall, code
        self.calls, self.processes = [], []
        self.clock = 0.0

        def popen(argv, **kwargs):
            self.calls.append((argv, kwargs))
            buffers = {1: bytearray(self.text), 2: bytearray(self.stderr)}
            process = SimpleNamespace(buffers=buffers, killed=False, bytes_read=0)
            process.stdout = SimpleNamespace(fileno=lambda: 1, close=lambda: None)
            process.stderr = SimpleNamespace(fileno=lambda: 2, close=lambda: None)
            process.poll = lambda: None if stall or any(buffers.values()) else code
            process.kill = lambda: setattr(process, "killed", True)
            process.wait = lambda **kwargs: code
            self.processes.append(process)
            return process

        fixture = self

        class PipeSelector:
            def __init__(self):
                self.keys = {}

            def register(self, pipe, events):
                self.keys[pipe.fileno()] = SimpleNamespace(fileobj=pipe)

            def unregister(self, pipe):
                self.keys.pop(pipe.fileno())

            def get_map(self):
                return self.keys

            def select(self, timeout):
                return [
                    (key, 1)
                    for fd, key in self.keys.items()
                    if fd == 2 or fixture.processes[-1].buffers[fd] or not stall
                ]

            def close(self):
                self.keys.clear()

        def read(fd, size):
            process = self.processes[-1]
            data = bytes(process.buffers[fd][:size])
            del process.buffers[fd][:size]
            process.bytes_read += len(data)
            return data

        def monotonic():
            self.clock += 1.0 if stall else 0.00001
            return self.clock

        monkeypatch.setattr(ssh_gateway.subprocess, "Popen", popen)
        monkeypatch.setattr(ssh_gateway.selectors, "DefaultSelector", PipeSelector)
        monkeypatch.setattr(ssh_gateway.os, "read", read)
        monkeypatch.setattr(ssh_gateway.time, "monotonic", monotonic)


@pytest.mark.parametrize(
    "tool,field",
    [
        ("service.list", "services"),
        ("process.list", "processes"),
        ("container.list", "containers"),
    ],
)
def test_large_inventory_keeps_known_business_after_650_unrelated_rows(monkeypatch, tool, field):
    if tool == "service.list":
        lines = [
            f"noise-{i:04d}.service loaded active running {'unrelated ' * 10}" for i in range(650)
        ]
        lines.append("catalog-api.service loaded failed failed Catalog application")
        target_id = "catalog-api.service"
    elif tool == "process.list":
        lines = [f"{i + 10000} noise-{'x' * 80}-{i:04d}" for i in range(650)]
        lines.append("90000 catalog-api")
        target_id = "90000"
    else:
        lines = [
            json.dumps(
                {
                    "ID": f"{i + 1:064x}",
                    "Names": f"noise-{i}",
                    "Image": "unrelated:" + "x" * 100,
                    "State": "running",
                }
            )
            for i in range(650)
        ]
        lines.append(
            json.dumps(
                {
                    "ID": "a" * 64,
                    "Names": "catalog-api",
                    "Image": "catalog:current",
                    "State": "exited",
                }
            )
        )
        target_id = "a" * 64
    text = "\n".join(lines) + "\n"
    assert len(text.encode()) > 48000
    native = NativeStreams(monkeypatch, text)
    policy = {"services": ["catalog-api"], "containers": ["a" * 64], "docker_reads_enabled": True}
    payload = ssh_gateway.execute(policy, tool, {"limit": 5, "name_hints": ["catalog-api"]})
    assert payload[field][0]["id"] == target_id
    assert len(payload[field]) <= 5
    assert len(json.dumps(payload).encode()) < 48000
    assert payload["truncated"] and payload["partial"] and not payload["timeout"]
    assert payload["filtered"] is True
    assert payload["name_hints"] == ["catalog-api"]
    assert payload["scanned"] == 651
    assert len(native.calls) == 1
    argv, options = native.calls[0]
    assert options["shell"] is False
    if tool == "service.list":
        assert argv[-2:] == ["--", "*catalog-api*"]
    elif tool == "process.list":
        assert argv[0] == "/usr/bin/pgrep" and argv[-2] == "--"
    else:
        assert "--filter" in argv and any(part.startswith("name=") for part in argv)


def test_inventory_pages_are_stable_without_raw_order_dependence(monkeypatch):
    native = NativeStreams(
        monkeypatch,
        "\n".join(f"service-{i:03d}.service loaded active running" for i in reversed(range(7)))
        + "\n",
    )
    first = ssh_gateway.execute({}, "service.list", {"limit": 3})
    second = ssh_gateway.execute({}, "service.list", {"limit": 3, "cursor": first["next_cursor"]})
    third = ssh_gateway.execute({}, "service.list", {"limit": 3, "cursor": second["next_cursor"]})
    assert [item["id"] for item in first["services"]] == [
        "service-000.service",
        "service-001.service",
        "service-002.service",
    ]
    assert [item["id"] for item in second["services"]] == [
        "service-003.service",
        "service-004.service",
        "service-005.service",
    ]
    assert [item["id"] for item in third["services"]] == ["service-006.service"]
    assert third["next_cursor"] is None and third["partial"] is False
    assert len(native.calls) == 3


def test_unfiltered_discovery_prioritizes_server_authorized_identity(monkeypatch):
    NativeStreams(
        monkeypatch,
        "\n".join(
            [f"noise-{i:03d}.service loaded active running" for i in range(650)]
            + ["catalog-api.service loaded failed failed"]
        )
        + "\n",
    )
    result = ssh_gateway.execute({"services": ["catalog-api"]}, "service.list", {"limit": 1})
    assert result["services"][0]["id"] == "catalog-api.service"
    assert result["filtered"] is False and result["truncated"] is True


def test_inventory_scan_stops_before_unbounded_output(monkeypatch):
    native = NativeStreams(
        monkeypatch,
        "\n".join(f"noise-{i:05d}.service loaded active running" for i in range(10000)) + "\n",
    )
    result = ssh_gateway.execute({}, "service.list", {"limit": 2})
    assert result["scanned"] <= 2048
    assert result["limit_reason"] == "scan_limit"
    assert result["partial"] and result["truncated"]
    assert native.processes[0].killed
    assert native.processes[0].bytes_read <= 1024 * 1024


def test_inventory_timeout_keeps_partial_observation(monkeypatch):
    native = NativeStreams(monkeypatch, "catalog-api.service loaded failed failed\n", stall=True)
    result = ssh_gateway.execute({}, "service.list", {"name_hints": ["catalog-api"]})
    assert result["services"][0]["name"] == "catalog-api"
    assert result["timeout"] and result["partial"] and result["truncated"]
    assert result["limit_reason"] == "timeout"
    assert native.processes[0].killed


def test_inventory_child_that_does_not_exit_cannot_block_cleanup(monkeypatch):
    native = NativeStreams(monkeypatch, "catalog-api.service loaded failed failed\n")
    original_popen = ssh_gateway.subprocess.Popen

    def delayed_exit(argv, **kwargs):
        process = original_popen(argv, **kwargs)
        process.poll = lambda: None

        def wait(**options):
            if "timeout" not in options:
                raise AssertionError("Unbounded wait after killing a native discovery process")
            raise ssh_gateway.subprocess.TimeoutExpired(argv, options["timeout"])

        process.wait = wait
        return process

    monkeypatch.setattr(ssh_gateway.subprocess, "Popen", delayed_exit)
    result = ssh_gateway.execute({}, "service.list", {})
    assert result["timeout"] and result["partial"]
    assert native.processes[0].killed


def test_inventory_rejects_command_failure_instead_of_claiming_empty_complete(monkeypatch):
    NativeStreams(monkeypatch, "", code=1, stderr=b"Permission denied\n")
    with pytest.raises(PermissionError):
        ssh_gateway.execute({}, "service.list", {})


def test_pgrep_no_match_is_complete_empty_result(monkeypatch):
    NativeStreams(monkeypatch, "", code=1)
    result = ssh_gateway.execute({}, "process.list", {"name_hints": ["catalog-api"]})
    assert result["processes"] == [] and result["partial"] is False
    assert result["timeout"] is False


def test_oversized_inventory_record_has_explicit_incomplete_status(monkeypatch):
    native = NativeStreams(monkeypatch, "x" * (2 * 1024 * 1024))
    result = ssh_gateway.execute({}, "service.list", {})
    assert result["services"] == []
    assert result["partial"] and result["truncated"]
    assert result["limit_reason"] == "record_limit"
    assert native.processes[0].bytes_read <= 8192


def test_malformed_inventory_record_does_not_invent_complete_inventory(monkeypatch):
    NativeStreams(
        monkeypatch,
        '{"ID": "not-an-id"}\n'
        + json.dumps({"ID": "a" * 64, "Names": "catalog-api", "Image": "catalog:current"})
        + "\n",
    )
    result = ssh_gateway.execute({"docker_reads_enabled": True}, "container.list", {})
    assert result["containers"][0]["id"] == "a" * 64
    assert result["partial"] and result["invalid_records"] == 1


def test_list_schema_accepts_only_bounded_hints_and_cursor():
    config = RuntimeTargetCreate(name="isolated").model_dump()
    args = check(
        config,
        "service.list",
        {"limit": 3, "name_hints": ["catalog-api"], "cursor": "0:catalog-api.service"},
    )
    assert args["name_hints"] == ["catalog-api"] and args["limit"] == 3


@pytest.mark.parametrize(
    "args",
    [
        {"name_hints": ["catalog-api;touch-bad"]},
        {"name_hints": ["*catalog*"]},
        {"name_hints": ["-bad"]},
        {"name_hints": ["x"] * 9},
        {"name_hints": "catalog-api"},
        {"cursor": "1:service;bad"},
        {"cursor": "2:service"},
        {"limit": True},
    ],
)
def test_gateway_and_client_reject_unbounded_or_injected_list_parameters(args):
    with pytest.raises(ValidationError):
        ListArgs.model_validate(args)
    with pytest.raises((ValueError, PermissionError)):
        ssh_gateway.execute({}, "service.list", args)


def test_discovery_hints_do_not_authorize_deep_reads(monkeypatch):
    native = NativeStreams(monkeypatch, "catalog-api.service loaded failed failed\n")
    result = ssh_gateway.execute({}, "service.list", {"name_hints": ["catalog-api"]})
    assert result["services"][0]["name"] == "catalog-api"
    with pytest.raises(PermissionError):
        ssh_gateway.execute({}, "service.logs", {"name": "catalog-api"})
    assert len(native.calls) == 1


def test_mock_hints_obey_bounds_keep_unrelated_metadata_and_paginate():
    connector = MockConnector(RuntimeTargetCreate(name="isolated").model_dump())
    first = connector.read("service.list", {"limit": 1, "name_hints": ["unrelated-backup"]})
    assert [row["name"] for row in first["services"]] == ["unrelated-backup"]
    assert first["truncated"] and first["filtered"] is False
    second = connector.read(
        "service.list",
        {"limit": 1, "name_hints": ["unrelated-backup"], "cursor": first["next_cursor"]},
    )
    assert [row["name"] for row in second["services"]] == ["catalog-api"]
    assert second["partial"] is False
