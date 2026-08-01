from __future__ import annotations

import re
from collections.abc import Mapping


SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "qr",
    "qrcode",
    "password",
    "secret",
}
PHONE_RE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
HEADER_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|access[_-]?token|password|secret)\s*[:=]\s*([^\s,;]+)"
)


def _redact_text(text: str) -> str:
    text = PHONE_RE.sub(r"\1****\2", text)
    return HEADER_RE.sub(lambda match: f"{match.group(1)}: [REDACTED]", text)


def redact(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value

