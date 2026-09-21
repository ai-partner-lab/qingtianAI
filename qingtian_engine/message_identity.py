"""Prepare display-labelled messages; no transport, authority or receipt claims.

Only display labels are normalized. The caller remains responsible for reviewing
the message body and private audit metadata before sharing either with a host.
"""
from __future__ import annotations

import re
import unicodedata


UNKNOWN_ROLE = "未知角色"
UNKNOWN_SESSION = "未知会话"
MAX_LABEL = 80
MAX_BODY = 20000
_HEADER = re.compile(r"^【([^【】\r\n|｜]+)[|｜]([^【】\r\n|｜]+)】")

MESSAGE_IDENTITY_INSTRUCTIONS = (
    " For every cross-task message, put a human-readable source label on the first "
    "line in the form 【角色｜来源会话名】, for example 【运维｜部署核验】. "
    "Use the actual source task's current display name and role when known; use "
    "未知角色 or 未知会话 when unavailable, never infer an identity from the target "
    "or a private workspace/account. Do not duplicate an existing matching prefix; "
    "resolve conflicting prefixes before sending. Keep source task ID, source "
    "reference, handoff ID and manifest hash in separate audit metadata. Display "
    "names are not authenticated identity or permission. Review the body for "
    "private paths and accounts before sharing. This only controls message text, "
    "not the host's native chrome. Preparing a body, sending, transport receipt, "
    "recipient acceptance, execution start and completion are separate facts. "
    "Use an actually available authorized host transport; if unavailable, report "
    "that the message is prepared but unsent, and never fabricate delivery."
)


def _display_label(value: str | None, fallback: str) -> tuple[str, str]:
    if value is None or value == "":
        return fallback, "unavailable"
    if not isinstance(value, str):
        raise ValueError("display labels must be strings or null")
    # Reject rather than strip controls: stripping can conceal a forged label.
    if any(unicodedata.category(char).startswith("C") for char in value):
        return fallback, "rejected"
    clean = unicodedata.normalize("NFKC", value).strip()
    if not clean:
        return fallback, "unavailable"
    # Labels are names, not paths, URLs, handles, emails or header delimiters.
    # This is bounded label hygiene, not a general body/PII redaction engine.
    if len(clean) > MAX_LABEL or any(char in clean for char in "/\\@~:|【】"):
        return fallback, "rejected"
    return clean, "provided"


def _audit_value(value: str | None) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 2000
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise ValueError("audit references must be nonempty bounded strings or null")
    # Preserve opaque correlation values; never infer authority or a receipt.
    return value


def prepare_message(
    body: str, *, source_role: str | None = None,
    source_session_name: str | None = None, source_task_id: str | None = None,
    handoff_id: str | None = None, manifest_hash: str | None = None,
    source_ref: str | None = None,
) -> dict:
    """Return a JSON-ready, unsent envelope with an idempotent first-line label.

    Conflicting pre-existing labels raise ValueError without changing the input.
    Audit references are caller attestations, kept verbatim outside the body;
    no task lookup, disk access, native API call or lifecycle mutation occurs.
    """
    # Allow one already-formatted prefix even at the maximum content length.
    if (not isinstance(body, str) or not body.strip()
            or len(body) > MAX_BODY + 2 * MAX_LABEL + 4):
        raise ValueError("body must be a nonempty string of at most 20000 characters")
    if "\x00" in body:
        raise ValueError("body must not contain a null character")
    role, role_state = _display_label(source_role, UNKNOWN_ROLE)
    name, name_state = _display_label(source_session_name, UNKNOWN_SESSION)
    prefix = f"【{role}｜{name}】"
    remainder = body
    while match := _HEADER.match(remainder):
        prior_role, prior_role_state = _display_label(match[1], UNKNOWN_ROLE)
        prior_name, prior_name_state = _display_label(match[2], UNKNOWN_SESSION)
        if (prior_role_state == "rejected" or prior_name_state == "rejected"
                or (prior_role, prior_name) != (role, name)):
            raise ValueError("existing source prefix conflicts with supplied display identity")
        remainder = remainder[match.end():].lstrip("\r\n")
    if not remainder.strip():
        raise ValueError("message must contain a body after its source prefix")
    if len(remainder) > MAX_BODY:
        raise ValueError("body content must be at most 20000 characters")
    return {
        "schema_version": 1,
        "status": "prepared",
        "body": prefix + "\n" + remainder,
        "source": {
            "role": role, "session_name": name,
            "role_state": role_state, "session_name_state": name_state,
            "identity_assurance": "display_only_caller_attestation",
        },
        "audit": {
            "source_task_id": _audit_value(source_task_id),
            "source_ref": _audit_value(source_ref),
            "handoff_id": _audit_value(handoff_id),
            "manifest_hash": _audit_value(manifest_hash),
        },
        "native_delivery": False,
        "recipient_accepted": False,
        "execution_started": False,
        "completion_granted": False,
    }
