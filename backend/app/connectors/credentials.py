"""Process-local SSH passwords. Never serialize entries or pass them to model workers."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import SecretStr

TTL_SECONDS = 1800


class CredentialUnavailable(PermissionError):
    code = "ssh_credential_unavailable"

    def __init__(self):
        super().__init__("SSH 密码会话未提供、已过期或已清除；请重新输入密码（后端重启也会失效）")


def binding(config, owner):
    return (owner, config["id"], config["host"], config["port"],
            config["username"], config["host_public_key"])


@dataclass(repr=False)
class Entry:
    scope: tuple
    password: SecretStr
    deadline: float
    expires_at: str


class SessionCredentials:
    def __init__(self):
        self._entries: dict[str, Entry] = {}
        self._lock = threading.RLock()

    def purge(self):
        with self._lock:
            for ref, entry in list(self._entries.items()):
                if time.monotonic() >= entry.deadline:
                    del self._entries[ref]

    def put(self, config, owner, password: SecretStr):
        scope = binding(config, owner)
        with self._lock:
            self.purge()
            for ref, entry in list(self._entries.items()):
                if entry.scope == scope:
                    del self._entries[ref]
            if len(self._entries) >= 100:
                raise PermissionError("SSH 会话凭据数量已达上限，请清除不再使用的会话")
            ref = "session-" + secrets.token_hex(24)
            self._entries[ref] = Entry(scope, password, time.monotonic() + TTL_SECONDS,
                (datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)).isoformat())
            return ref

    def resolve(self, config, owner):
        with self._lock:
            self.purge()
            entry = self._entries.get(config.get("credential_ref", ""))
            if not entry or entry.scope != binding(config, owner):
                raise CredentialUnavailable()
            return entry.password

    def status(self, config, owner):
        with self._lock:
            try:
                self.resolve(config, owner)
            except CredentialUnavailable:
                return {"state": "required", "expires_at": None}
            return {"state": "ready", "expires_at": self._entries[config["credential_ref"]].expires_at}

    def grant(self, config, owner):
        with self._lock:
            password = self.resolve(config, owner)
            entry = self._entries[config["credential_ref"]]
            return {"password": password.get_secret_value(), "deadline": entry.deadline}

    def revoke(self, config, owner):
        with self._lock:
            entry = self._entries.get(config.get("credential_ref", ""))
            if entry and entry.scope == binding(config, owner):
                del self._entries[config["credential_ref"]]

    def clear_target(self, config, owner):
        # Explicit clearing also covers a concurrent refresh after the caller read an old reference.
        scope = binding(config, owner)
        with self._lock:
            for ref, entry in list(self._entries.items()):
                if entry.scope == scope:
                    del self._entries[ref]

    def clear(self):
        with self._lock:
            self._entries.clear()


SSH_SESSIONS = SessionCredentials()
