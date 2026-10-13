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
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\r\n,;}{]+)"
)
_URI_PASSWORD = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@/\s]+(@)")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL
)
_CPP_RAW_LITERAL = re.compile(r'(?:u8|u|U|L)?R"(?P<raw_delimiter>[^\s()\\]{0,16})\([\s\S]*?(?:\)(?P=raw_delimiter)"|\Z)')
_SOURCE_LITERAL = re.compile(_CPP_RAW_LITERAL.pattern+r'''|(?P<triple>"{3}|'{3})(?:\\.|(?!(?P=triple))[\s\S])*?(?:(?P=triple)|\Z)|(?P<single>["'`])(?:\\.|(?!(?P=single))[\s\S])*?(?:(?P=single)|\Z)''')
_SOURCE_TOKEN = re.compile(r'(?P<comment>\#(?:\\\r?\n|[^\r\n])*|//(?:\\\r?\n|[^\r\n])*|/\*[\s\S]*?(?:\*/|\Z))|'+_SOURCE_LITERAL.pattern)
_CREDENTIAL_CONTEXT = re.compile(r'(?i)password|passwd|token|secret|api.?key|authorization|credentials?|hmac|compare.?digest|\bauth\b')


def redact_condition(value: str) -> str:
    # Conditional literals can be credential arguments rather than assignments.
    return redact_text(_SOURCE_LITERAL.sub(lambda match:'"[REDACTED_LITERAL]"'+'\n'*match[0].count('\n'),value))


def redact_source_text(value: str) -> str:
    def literal(match):
        if match.groupdict().get('comment') is not None:
            # '#' also introduces C/C++ directives; sensitive quoted macro values stay hidden.
            return redact_condition(match[0]) if _CREDENTIAL_CONTEXT.search(match[0]) else redact_text(match[0])
        prefix=value[value.rfind('\n',0,match.start())+1:match.start()]
        groups=match.groupdict()
        delimiter=(')'+groups['raw_delimiter']+'"') if groups.get('raw_delimiter') is not None else groups.get('triple') or groups.get('single')
        incomplete=len(match[0])==len(delimiter) or not match[0].endswith(delimiter)
        return '"[REDACTED_LITERAL]"'+'\n'*match[0].count('\n') if incomplete or _CREDENTIAL_CONTEXT.search(prefix) else match[0]
    # Raw literal bodies can cross a directive's physical newline even in partial source.
    value=_CPP_RAW_LITERAL.sub(literal,value)
    value=_SOURCE_TOKEN.sub(literal,value)
    value=_PRIVATE_KEY_BLOCK.sub(lambda match:'[REDACTED_PRIVATE_KEY]'+'\n'*match[0].count('\n'),value)
    return redact_text(value)


def redact_text(value: str) -> str:
    value = _PRIVATE_KEY_BLOCK.sub("[REDACTED_PRIVATE_KEY]", value)
    value = _AUTH.sub(lambda match: match.group("prefix") + "[REDACTED]" + match.group("quote"), value)
    value = _BEARER.sub(r"\1 [REDACTED]", value)
    value = _SECRET_PAIR.sub(
        lambda match: match.group("key") + (match.group("value")[0] + "[REDACTED]" + match.group("value")[0] if match.group("value").startswith(('"', "'")) else "[REDACTED]"),
        value,
    )
    value = _URI_PASSWORD.sub(r"\1[REDACTED]\2", value)
    value = _JWT.sub("[REDACTED_JWT]", value)
    return _AWS_ACCESS_KEY.sub("[REDACTED_AWS_KEY]", value)
