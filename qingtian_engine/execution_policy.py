"""Fail-closed execution authorization shared by schedulers and workers."""
from __future__ import annotations

from typing import Any, Mapping, Optional


NON_EXECUTABLE_POLICIES = frozenset({"analysis-only", "reference-only"})
LEGACY_PLAN_ONLY_MARKERS = (
    "plan-only",
    "plan only",
    "仅方案评审",
    "只分析",
    "不进入 coding",
    "不进入coding",
    "不进入自动执行",
    "go/no-go 后另拆",
)


def _normalized_policy(value: Any) -> str:
    return str(value or "normal").strip().lower().replace("_", "-")


def execution_forbidden(
    task: Mapping[str, Any], *, intake_intent: Optional[str] = None
) -> bool:
    """Return whether a task is a reference/analysis record, never executable.

    ``intake_intent`` is a compatibility input for records created before the
    explicit authorization policy was persisted on every intake task.
    """

    if _normalized_policy(task.get("authorization_policy")) in NON_EXECUTABLE_POLICIES:
        return True
    if str(task.get("imported_from") or "").strip():
        return True
    if str(intake_intent or "").strip().lower() == "analyze":
        return True

    searchable = " ".join(
        str(task.get(key) or "").strip().lower()
        for key in ("title", "scope_summary", "blocking_reason", "action_text")
    )
    if any(marker in searchable for marker in LEGACY_PLAN_ONLY_MARKERS):
        return True

    # Preserve the legacy, deliberately conservative plan-only markers while
    # new records move to the explicit authorization_policy field.
    from .service import is_plan_only

    return is_plan_only(dict(task))


def execution_forbidden_in_database(db: Any, task: Mapping[str, Any]) -> bool:
    """Apply the persisted policy plus the pre-policy intake compatibility fact."""

    if execution_forbidden(task):
        return True
    source_request_id = str(task.get("source_request_id") or "").strip()
    if not source_request_id:
        return False
    intake = db.one("SELECT intent FROM intakes WHERE id=?", (source_request_id,))
    return execution_forbidden(
        task,
        intake_intent=str(intake.get("intent") or "") if intake else None,
    )
