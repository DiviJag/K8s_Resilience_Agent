"""Secret and PII redaction (HLD §11).

Applied before evidence reaches the LLM and before it is written to the golden set. Both
backends call this, so the logic is written and tested once, before any cluster exists.
"""

import re
from typing import Any

REDACTED = "***REDACTED***"

_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "access_key_id",
    "secret_access_key",
    "private_key",
    "credential",
    "credentials",
    "authorization",
    "auth_token",
    "client_secret",
    "session_token",
    "dockerconfigjson",
    ".dockerconfigjson",
}

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{8,}=*", re.I), f"Bearer {REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), REDACTED),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), REDACTED),
    # `=` assignments are the classic leak shape (DB_PASSWORD=..., api_key=...) and are
    # redacted wherever they appear, including mid-line in a log.
    (
        re.compile(
            r"(?i)([A-Za-z0-9_.\-]*"
            r"(?:password|passwd|pwd|token|secret|api[_-]?key|credential)"
            r"[A-Za-z0-9_.\-]*)"
            r"(\s*=\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s,;\"']+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    # `:` assignments are only treated as secrets at the start of a line (optionally inside
    # quotes or a structure), where config lives. Mid-sentence they are prose, and redacting
    # them destroys evidence: a registry error reading "failed to fetch anonymous token:
    # unexpected status ... 403 Forbidden" is exactly what identifies an auth failure.
    (
        re.compile(
            r"(?im)^([\s\-\"'{,]*[A-Za-z0-9_.\-]*"
            r"(?:password|passwd|pwd|token|secret|api[_-]?key|credential)"
            r"[A-Za-z0-9_.\-]*)"
            r"(\s*:\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s,;\"']+)"
        ),
        rf"\1\2{REDACTED}",
    ),
]

# Long opaque blobs are usually encoded secrets, but image digests and resource UIDs are
# equally long - leaving pure hex alone keeps that evidence intact for diagnosis.
_B64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")


def _keep_hex(match: re.Match[str]) -> str:
    return match.group(0) if _HEX.match(match.group(0)) else REDACTED


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _B64_BLOB.sub(_keep_hex, text)


def redact(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        result: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS and not isinstance(
                value, (dict, list)
            ):
                result[key] = REDACTED
            else:
                result[key] = redact(value)
        return result
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact(item) for item in obj)
    return obj
