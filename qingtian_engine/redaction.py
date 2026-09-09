from __future__ import annotations

import hashlib
import re
from typing import Any, Dict


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)"
    r"\b\s*[:=]\s*([^\s,;]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
KNOWN_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
)
URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s#]+")


def redact_text(value: str, max_chars: int = 500, redact_email: bool = True) -> str:
    text = value or ""
    text = SECRET_ASSIGN_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = KNOWN_SECRET_RE.sub("[REDACTED_KNOWN_SECRET]", text)
    text = URL_QUERY_RE.sub(r"\1?[REDACTED_QUERY]", text)
    if redact_email:
        text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def safe_event_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Allowlist only operational metadata; never persist prompts or raw model text."""
    safe: Dict[str, Any] = {}
    allowed = {
        "thread_id",
        "session_id",
        "turn_id",
        "item_type",
        "exit_code",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "duration_ms",
        "state",
        "attempt",
        "pid",
        "chars",
        "sha256",
        "stage",
        "payload_kind",
        "line_number",
        "exception_type",
        "trace_hash",
        "infrastructure_failure",
        "failed_run_id",
        "failure_kind",
    }
    for key, value in payload.items():
        if key in allowed and isinstance(value, (str, int, float, bool, type(None))):
            safe[key] = redact_text(value) if isinstance(value, str) else value
    return safe


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def contains_secret(value: str) -> bool:
    text = value or ""
    return bool(
        SECRET_ASSIGN_RE.search(text)
        or BEARER_RE.search(text)
        or KNOWN_SECRET_RE.search(text)
        or URL_QUERY_RE.search(text)
    )
