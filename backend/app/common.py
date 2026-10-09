from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


def uid() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()
