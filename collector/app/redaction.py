from __future__ import annotations

import re


_AUTH = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization|proxy-authorization)\s*[:=]\s*['\"]?)"
    r"(?:(?:bearer|basic)\s+)?(?P<value>[^'\"\s,;]+)(?P<quote>['\"]?)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/-]+=*")
_SECRET_PAIR = re.compile(
    r"(?i)(?P<key>[\"']?(?:password|passwd|pwd|token|secret|api[_-]?key|"
    r"client[_-]?secret|access[_-]?token|refresh[_-]?token|private[_-]?key|"
    r"cookie|set-cookie|x-api-key|auth|(?:aws[_-]?)?secret[_-]?access[_-]?key|"
    r"aws[_-]?session[_-]?token)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\"'\s,;}{]+)(?P=quote)"
)
_URI_PASSWORD = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@/\s]+(@)")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL
)


def redact_text(value: str) -> str:
    value = _PRIVATE_KEY_BLOCK.sub("[REDACTED_PRIVATE_KEY]", value)
    value = _AUTH.sub(lambda match: match.group("prefix") + "[REDACTED]" + match.group("quote"), value)
    value = _BEARER.sub(r"\1 [REDACTED]", value)
    value = _SECRET_PAIR.sub(
        lambda match: match.group("key") + match.group("quote") + "[REDACTED]" + match.group("quote"),
        value,
    )
    value = _URI_PASSWORD.sub(r"\1[REDACTED]\2", value)
    value = _JWT.sub("[REDACTED_JWT]", value)
    return _AWS_ACCESS_KEY.sub("[REDACTED_AWS_KEY]", value)


def safe_text(value: str, limit: int) -> tuple[str, bool]:
    redacted = redact_text(value)
    clipped = len(redacted) > limit
    return redacted[-limit:], clipped
