import re


def valid_service_name(value):
    # Literal systemd escapes are passed as one quoted argument, never decoded to shell text.
    first = r"[A-Za-z0-9_:.-]" if isinstance(value, str) and value.endswith(".service") else r"[A-Za-z0-9_:]"
    return (isinstance(value, str) and len(value) <= (255 if value.endswith(".service") else 128)
            and value != ".service"
            and value.count("@") <= 1
            and re.fullmatch(r"(?:" + first + r"|\\x[0-9A-Fa-f]{2})(?:[A-Za-z0-9_.:@-]|\\x[0-9A-Fa-f]{2})*", value) is not None)
