import importlib.util
from datetime import datetime, timezone

import pytest
from app.agent.decision_runtime import _parse_log_time, _parse_time
from app.agent.task_stop_policy import _fingerprint
from app.agent.workflow import _parse_log_timestamp, _parse_timestamp


@pytest.mark.parametrize("offset,hour", [("Z", 9), ("+08:00", 17), ("+0800", 17),
                                         ("+0000", 9), ("-0400", 5)])
def test_all_consumers_use_same_aware_log_timestamp(offset, hour):
    line = f"2026-10-13T{hour:02}:00:00.123456789{offset} startup failed"
    expected = datetime(2026, 10, 13, 9, 0, 0, 123456, tzinfo=timezone.utc)
    assert _parse_log_timestamp(line) == _parse_log_time(line) == expected


@pytest.mark.parametrize("value", ["2026-10-13T09:00:00", "2026-99-13T09:00:00Z", None])
def test_unknown_or_invalid_time_cannot_become_utc(value):
    assert _parse_timestamp(value) is None
    assert _parse_time(value) is None


def test_normalizer_preserves_source_cycle_raw_text_and_continuation():
    assert importlib.util.find_spec("app.agent.log_events") is not None
    from app.agent.log_events import normalize_log_events
    text = "2026-10-13T17:00:00+0800 startup failed\n  File app.py, line 5\nuntimed independent event"
    events = normalize_log_events(text, {"target_node_id": "app", "startup_cycle": "boot-2", "truncated": True})
    assert events[0].raw_line == text.splitlines()[0]
    assert events[0].original_timezone == "+0800"
    assert events[0].observed_on == "app"
    assert events[0].startup_cycle == "boot-2"
    assert events[0].truncated is True
    assert events[1].event_time_utc == events[0].event_time_utc
    assert events[1].parse_state == "continuation"
    assert events[2].event_time_utc is None


def test_log_fingerprint_ignores_acquisition_ids_and_timestamp_rewrites():
    common = {"target_node_id": "app", "tool_id": "service.logs"}
    first = {**common, "payload": {"text": "2026-10-13T17:00:00+0800 startup failed", "request_id": "a"}}
    repeated = {**common, "payload": {"text": "2026-10-13T09:00:02Z startup failed", "request_id": "b"}}
    assert _fingerprint(first) == _fingerprint(repeated)


def test_non_log_fingerprint_ignores_volatile_acquisition_metadata():
    first = {"target_node_id": "app", "tool_id": "health.get", "payload": {"status_code": 502, "request_id": "a", "observed_at": "first"}}
    repeated = {**first, "payload": {"status_code": 502, "request_id": "b", "observed_at": "second"}}
    assert _fingerprint(first) == _fingerprint(repeated)


def test_invalid_offset_minutes_do_not_roll_into_valid_time():
    assert _parse_time("2026-10-13T09:00:00+01:99") is None
    assert _parse_log_time("2026-10-13T09:00:00+0160 startup failed") is None


def test_reused_process_identity_is_a_semantic_change():
    first = {"target_node_id": "app", "tool_id": "process.status", "payload": {"state": "running", "pid": 4, "start_time": "boot-1"}}
    replaced = {**first, "payload": {"state": "running", "pid": 4, "start_time": "boot-2"}}
    assert _fingerprint(first) != _fingerprint(replaced)
