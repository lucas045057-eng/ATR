"""One timestamp and source contract for log facts, verification and progress."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

LOG_TIMESTAMP = re.compile(
    r"^\s*(?P<base>\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)"
    r"(?:\.(?P<fraction>\d+))?(?P<offset>Z|[+-]\d\d:?\d\d)?(?=\s|$)\s*"
)
VOLATILE = {"observed_at", "collected_at", "request_id", "call_id", "observation_id", "content_hash"}


def parse_time(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    offset = re.search(r"([+-])(\d\d):?(\d\d)$", value)
    if offset and (int(offset[2]) > 23 or int(offset[3]) > 59):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def parse_log_time(line: str) -> datetime | None:
    match = LOG_TIMESTAMP.match(line)
    if not match or not match.group("offset"):
        return None
    fraction = (match.group("fraction") or "")[:6].ljust(6, "0")
    offset = match.group("offset").replace("Z", "+00:00")
    return parse_time(f"{match.group('base')}.{fraction}{offset}")


@dataclass(frozen=True)
class NormalizedLogEvent:
    raw_line: str
    message: str
    original_timezone: str | None
    event_time_utc: datetime | None
    observed_on: str | None
    startup_cycle: str | None
    parse_state: str
    truncated: bool


def normalize_log_events(text: str, metadata: dict) -> list[NormalizedLogEvent]:
    events = []
    previous = None
    for raw in text.splitlines():
        match = LOG_TIMESTAMP.match(raw)
        stamp = parse_log_time(raw)
        offset = match.group("offset") if match else None
        state = "parsed" if stamp else "invalid" if offset else "unknown_timezone" if match else "untimed"
        if not match and raw.startswith((" ", "\t")) and previous and previous.event_time_utc:
            stamp, offset, state = previous.event_time_utc, previous.original_timezone, "continuation"
        event = NormalizedLogEvent(
            raw_line=raw, message=LOG_TIMESTAMP.sub("", raw), original_timezone=offset,
            event_time_utc=stamp, observed_on=metadata.get("observed_on", metadata.get("target_node_id")),
            startup_cycle=metadata.get("startup_cycle"), parse_state=state,
            truncated=bool(metadata.get("truncated", False)),
        )
        events.append(event)
        previous = event
    return events


def event_fingerprint(event: NormalizedLogEvent) -> str:
    value = [event.observed_on, event.startup_cycle, " ".join(event.message.split())]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def semantic_payload(value):
    if isinstance(value, dict):
        return {key: semantic_payload(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, list):
        return [semantic_payload(item) for item in value]
    return value
