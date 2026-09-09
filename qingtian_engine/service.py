from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import runtime_policy
from .db import Database, utc_now
from .redaction import fingerprint, redact_text, safe_event_payload
from .router import route_task


BOARD_STATES = (
    "INBOX",
    "RUNNING",
    "WAITING",
    "PAUSED",
    "PLAN_ONLY",
    "VERIFYING",
    "DONE",
    "CANCELED",
)
ACTION_OWNER_KINDS = {"user", "external", "agent", "none"}
WAITING_CATEGORY_LABELS = {
    "paused": "已暂停",
    "external": "外部等待",
    "user": "需用户",
    "internal_qa": "内部 QA",
    "internal_release": "内部发布",
    "dependency": "依赖等待",
    "execution_recovery": "执行恢复",
    "internal": "内部等待",
}
EVIDENCE_PROFILES = {"auto", "artifact", "browser", "code", "legacy", "qa"}
CODE_EVIDENCE_KINDS = {"commit", "test", "deploy", "smoke"}
EXTERNAL_HEARTBEAT_TTL_SECONDS = 15 * 60
MAX_VERIFICATION_BACKFILL_ATTEMPTS = 3
STATE_PROGRESS = {
    "INBOX": 10,
    "PLANNED": 20,
    "QUEUED": 30,
    "RUNNING": 55,
    "WAITING": 55,
    "VERIFYING": 85,
    "DONE": 100,
    "FAILED": 65,
    "CANCELED": 0,
}
ALLOWED_TRANSITIONS = {
    "INBOX": {"PLANNED", "WAITING", "CANCELED"},
    "PLANNED": {"QUEUED", "WAITING", "CANCELED"},
    "QUEUED": {"RUNNING", "WAITING", "FAILED", "CANCELED"},
    "RUNNING": {"VERIFYING", "WAITING", "FAILED", "CANCELED"},
    "WAITING": {"PLANNED", "QUEUED", "RUNNING", "CANCELED"},
    "VERIFYING": {"DONE", "RUNNING", "WAITING", "FAILED", "CANCELED"},
    "FAILED": {"QUEUED", "CANCELED"},
    "CANCELED": {"PLANNED"},
    "DONE": {"PLANNED"},
}


def is_paused_by_user(task: Dict[str, Any]) -> bool:
    """Return whether a task is explicitly paused by the user."""

    state = str(task.get("state") or "").upper()
    if state == "PAUSED":
        return True
    if state != "WAITING":
        return False
    searchable = " ".join(
        str(task.get(key) or "").strip().lower()
        for key in (
            "blocking_reason",
            "action_owner",
            "action_text",
        )
    )
    return (
        searchable.startswith("paused_by_user")
        or any(
            marker in searchable
            for marker in (
                "用户明确暂停",
                "用户确认暂缓",
                "用户暂停",
                "暂缓执行",
                "擎天暂停池",
                "暂停池",
            )
        )
    )


def is_plan_only(task: Dict[str, Any]) -> bool:
    """Return whether a planned task is intentionally excluded from execution."""

    state = str(task.get("state") or "").upper()
    # Display synthetic tour fixtures in their illustrative columns. This is
    # not execution permission: runner rejects every imported_from value,
    # and the tour HTTP server rejects all mutations.
    if task.get("imported_from") == "demo":
        return False
    if str(task.get("authorization_policy") or "normal").lower() in {
        "analysis-only",
        "reference-only",
    }:
        return True
    if state in {"DONE", "CANCELED"}:
        return False
    # Preserve compatibility with plan-only records created before explicit
    # authorization policies were introduced.
    scope = str(task.get("scope_summary") or "").strip().lower()
    if scope.startswith("[plan-only]"):
        return True
    if state not in {"INBOX", "PLANNED"}:
        return False
    searchable = " ".join(
        str(task.get(key) or "").strip().lower()
        for key in (
            "title",
            "scope_summary",
            "blocking_reason",
            "action_text",
        )
    )
    return any(
        marker in searchable
        for marker in (
            "plan-only",
            "plan only",
            "仅方案评审",
            "只分析",
            "不进入 coding",
            "不进入coding",
            "不进入自动执行",
            "go/no-go 后另拆",
        )
    )
def classify_waiting(
    task: Dict[str, Any],
    runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Project one WAITING row into an honest, user-facing wait category."""

    owner_kind = str(task.get("action_owner_kind") or "none").lower()
    reason = str(task.get("blocking_reason") or "").strip().lower()
    runtime_code = str((runtime or {}).get("code") or "").upper()
    action_text = str(task.get("action_text") or "").strip().lower()
    searchable = "{} {}".format(reason, action_text)

    if is_paused_by_user(task):
        key = "paused"
    elif owner_kind == "user":
        key = "user"
    elif (
        reason.startswith("waiting_independent_qa")
        or runtime_code == "INDEPENDENT_QA"
        or any(
            marker in searchable
            for marker in ("内部 qa", "内部qa", "内部验收", "真实设备")
        )
    ):
        key = "internal_qa"
    elif (
        reason.startswith("stale_execution:")
        or runtime_code
        in {
            "RECOVERY_REQUIRED",
            "FAILED_STAGE",
            "PROCESS_LOST",
            "EVENT_STALE",
        }
        or any(
            marker in searchable
            for marker in ("执行器", "进程已丢失", "执行中断", "失联", "安全恢复")
        )
    ):
        key = "execution_recovery"
    elif owner_kind == "external":
        key = "external"
    elif any(
        marker in searchable
        for marker in (
            "内部发布",
            "等待发布",
            "等待部署",
            "待部署",
            "待上线",
            "待推送",
            "release",
            "deploy",
            "git push",
        )
    ):
        key = "internal_release"
    elif any(
        marker in searchable
        for marker in ("依赖", "前置任务", "上游任务", "下游任务")
    ):
        key = "dependency"
    elif any(
        marker in searchable
        for marker in (
            "外部",
            "第三方",
            "渠道",
            "回调",
            "供应商",
            "外部授权",
        )
    ):
        key = "external"
    else:
        key = "internal"
    return {"key": key, "label": WAITING_CATEGORY_LABELS[key]}


def compact_summary(value: str, limit: int = 10) -> str:
    normalized = re.sub(r"\s+", "", value or "")
    normalized = re.sub(r"^[Pp]\d[:：]\s*", "", normalized)
    return normalized[:limit] or "待处理"


def _safe_path(value: str) -> str:
    if not value:
        return ""
    if "\x00" in value:
        raise ValueError("path contains NUL")
    return value[:500]


def new_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return "qt-{}-{}".format(stamp, uuid.uuid4().hex[:8])


class ControlPlane:
    def __init__(self, db: Database):
        self.db = db
        self.db.initialize()

    def create_task(
        self,
        title: str,
        idempotency_key: Optional[str] = None,
        scope_summary: str = "",
        priority: int = 2,
        environment: str = "local",
        repository: str = "",
        base_branch: str = "",
        worker_type: str = "auto",
        owner_session: str = "",
        reasoning: str = "auto",
        authorization_policy: str = "normal",
        requires_deploy: bool = False,
        imported_from: str = "",
        state: str = "INBOX",
        progress: Optional[int] = None,
        blocking_reason: str = "",
        action_owner_kind: str = "none",
        action_owner: str = "",
        action_text: str = "",
        action_due: Optional[str] = None,
        action_sensitive: bool = False,
        parent_id: Optional[str] = None,
        source_request_id: str = "",
        evidence_profile: str = "auto",
    ) -> Dict[str, Any]:
        clean_title = redact_text(title, max_chars=180)
        clean_scope = redact_text(scope_summary, max_chars=500)
        if not clean_title:
            raise ValueError("title is required")
        key = idempotency_key or fingerprint(
            "|".join((clean_title, repository, environment, source_request_id))
        )
        existing = self.db.one("SELECT * FROM tasks WHERE idempotency_key=?", (key,))
        if existing:
            return existing

        route = route_task(clean_title, clean_scope, repository)
        selected_worker = route.worker_type if worker_type == "auto" else worker_type
        selected_owner = owner_session or route.owner_session
        selected_reasoning = route.reasoning if reasoning == "auto" else reasoning
        policy = runtime_policy(selected_reasoning)
        now = utc_now()
        task_id = new_task_id()
        selected_progress = STATE_PROGRESS.get(state, 0) if progress is None else progress
        selected_action_kind = str(action_owner_kind or "none").lower()
        if selected_action_kind not in ACTION_OWNER_KINDS:
            raise ValueError("invalid action owner kind")
        selected_evidence_profile = str(evidence_profile or "auto").lower()
        if selected_evidence_profile not in EVIDENCE_PROFILES:
            raise ValueError("invalid evidence profile")
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, idempotency_key, parent_id, source_request_id, title,
                    short_summary, scope_summary, priority, progress, environment,
                    repository, base_branch, worker_type, owner_session, model,
                    reasoning, speed, authorization_policy, state, blocking_reason,
                    action_owner_kind, action_owner, action_text, action_due,
                    action_sensitive, requires_deploy, evidence_profile,
                    imported_from, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    key,
                    parent_id,
                    source_request_id,
                    clean_title,
                    compact_summary(clean_title),
                    clean_scope,
                    max(0, min(3, int(priority))),
                    max(0, min(100, int(selected_progress))),
                    environment,
                    _safe_path(repository),
                    redact_text(base_branch, max_chars=100),
                    selected_worker,
                    selected_owner,
                    policy.model,
                    policy.reasoning,
                    policy.speed,
                    authorization_policy,
                    state,
                    redact_text(blocking_reason, max_chars=300),
                    selected_action_kind,
                    redact_text(action_owner, max_chars=120),
                    redact_text(action_text, max_chars=500),
                    redact_text(action_due, max_chars=80) if action_due else None,
                    int(bool(action_sensitive)),
                    int(requires_deploy),
                    selected_evidence_profile,
                    redact_text(imported_from, max_chars=300),
                    now,
                    now,
                ),
            )
        self.db.add_event(
            task_id,
            "task.created",
            "control-plane",
            "任务进入控制面",
            "task-created:{}".format(key),
            {"state": state},
        )
        self.db.add_event(
            task_id,
            "task.routed",
            "router",
            "{} → {}".format(route.reason, selected_owner),
            "task-route:{}".format(task_id),
            {"state": state},
        )
        return self.get_task(task_id)

    def set_human_action(
        self,
        task_id: str,
        owner_kind: str,
        owner: str = "",
        text: str = "",
        due: Optional[str] = None,
        sensitive: bool = False,
        producer: str = "control-plane",
    ) -> Dict[str, Any]:
        clean_kind = str(owner_kind or "none").lower()
        if clean_kind not in ACTION_OWNER_KINDS:
            raise ValueError("invalid action owner kind")
        clean_owner = redact_text(owner, max_chars=120)
        clean_text = redact_text(text, max_chars=500)
        clean_due = redact_text(due, max_chars=80) if due else None
        if clean_kind in {"user", "external", "agent"} and not clean_text:
            raise ValueError("action text is required")
        if clean_kind == "none":
            clean_owner = ""
            clean_text = ""
            clean_due = None
            sensitive = False
        now = utc_now()
        self.db.execute(
            """
            UPDATE tasks SET action_owner_kind=?, action_owner=?, action_text=?,
                action_due=?, action_sensitive=?, updated_at=?
            WHERE id=?
            """,
            (
                clean_kind,
                clean_owner,
                clean_text,
                clean_due,
                int(bool(sensitive)),
                now,
                task_id,
            ),
        )
        self.db.add_event(
            task_id,
            "task.human_action_changed",
            producer,
            "人工动作：{}".format(clean_kind),
            "human-action:{}:{}:{}".format(
                task_id, clean_kind, fingerprint("|".join((clean_owner, clean_text, clean_due or "")))
            ),
            {
                "action_owner_kind": clean_kind,
                "action_sensitive": bool(sensitive),
                "state": self.get_task(task_id)["state"],
            },
        )
        return self.get_task(task_id)

    def complete_human_action(self, task_id: str) -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task.get("action_owner_kind") != "user":
            raise ValueError("task has no user-owned action")
        self.db.add_event(
            task_id,
            "task.human_action_completed",
            "dashboard",
            "用户已完成动作，重新进入内部验证",
            "human-action-completed:{}:{}".format(
                task_id, fingerprint(task.get("action_text", ""))
            ),
            {"state": task["state"]},
        )
        self.set_human_action(task_id, "none", producer="dashboard")
        if task["state"] != "VERIFYING":
            return self.transition(
                task_id,
                "VERIFYING",
                producer="dashboard",
                summary="人工动作完成，进入内部验证",
                dedupe_key="human-action-verify:{}:{}".format(
                    task_id, fingerprint(task.get("action_text", ""))
                ),
                force=True,
            )
        return self.get_task(task_id)

    def remind_human_action(self, task_id: str) -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task.get("action_owner_kind") != "external":
            raise ValueError("task has no external-owned action")
        self.db.add_event(
            task_id,
            "task.external_reminder_recorded",
            "dashboard",
            "已记录提醒，继续等待 {}".format(
                task.get("action_owner") or "外部主责"
            ),
            "external-reminder:{}:{}".format(task_id, utc_now()[:16]),
            {
                "action_owner_kind": "external",
                "action_sensitive": bool(task.get("action_sensitive")),
                "state": task["state"],
            },
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError("task not found: {}".format(task_id))
        task["dependencies"] = self.db.all(
            """
            SELECT d.depends_on_id, d.relation, t.title, t.state
            FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_id
            WHERE d.task_id=?
            """,
            (task_id,),
        )
        task["evidence"] = self.db.all(
            "SELECT kind, value, label, verified, created_at FROM evidence "
            "WHERE task_id=? ORDER BY created_at DESC",
            (task_id,),
        )
        task["events"] = self.db.all(
            "SELECT event_type, producer, summary, payload_json, occurred_at "
            "FROM events WHERE task_id=? AND event_type!='codex.event_skipped' "
            "ORDER BY id DESC LIMIT 100",
            (task_id,),
        )
        task["runs"] = self.db.all(
            "SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC", (task_id,)
        )
        return task

    def list_tasks(
        self, states: Optional[Sequence[str]] = None, limit: int = 500
    ) -> List[Dict[str, Any]]:
        if states:
            placeholders = ",".join("?" for _ in states)
            return self.db.all(
                "SELECT * FROM tasks WHERE state IN ({}) "
                "ORDER BY priority ASC, updated_at DESC LIMIT ?".format(placeholders),
                tuple(states) + (limit,),
            )
        return self.db.all(
            "SELECT * FROM tasks ORDER BY priority ASC, updated_at DESC LIMIT ?",
            (limit,),
        )

    def transition(
        self,
        task_id: str,
        new_state: str,
        producer: str = "control-plane",
        summary: str = "",
        dedupe_key: Optional[str] = None,
        blocking_reason: str = "",
        progress: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        task = self.get_task(task_id)
        old_state = task["state"]
        if new_state == old_state:
            return task
        if not force and new_state not in ALLOWED_TRANSITIONS.get(old_state, set()):
            raise ValueError("invalid transition {} -> {}".format(old_state, new_state))
        if new_state == "DONE":
            missing = self.missing_completion_evidence(task_id)
            if missing and not force:
                raise ValueError("cannot complete; missing evidence: {}".format(", ".join(missing)))
        now = utc_now()
        values: List[Any] = [
            new_state,
            STATE_PROGRESS.get(new_state, task["progress"]) if progress is None else progress,
            redact_text(blocking_reason, max_chars=300),
            now,
        ]
        started_at_sql = ""
        finished_at_sql = ""
        human_action_sql = ""
        if new_state == "RUNNING" and not task["started_at"]:
            started_at_sql = ", started_at=?"
            values.append(now)
        if new_state in {"DONE", "FAILED", "CANCELED"}:
            finished_at_sql = ", finished_at=?"
            values.append(now)
        if new_state in {"DONE", "CANCELED"}:
            human_action_sql = (
                ", action_owner_kind='none', action_owner='', action_text='', "
                "action_due=NULL, action_sensitive=0"
            )
        values.append(task_id)
        self.db.execute(
            """
            UPDATE tasks SET state=?, progress=?, blocking_reason=?, updated_at=?
            {}{}{} WHERE id=?
            """.format(started_at_sql, finished_at_sql, human_action_sql),
            values,
        )
        event_key = dedupe_key or "transition:{}:{}:{}:{}".format(
            task_id, old_state, new_state, task["updated_at"]
        )
        self.db.add_event(
            task_id,
            "task.state_changed",
            producer,
            redact_text(summary or "{} → {}".format(old_state, new_state), max_chars=300),
            event_key,
            safe_event_payload({"state": new_state}),
        )
        return self.get_task(task_id)

    def add_dependency(self, task_id: str, depends_on_id: str) -> None:
        if task_id == depends_on_id:
            raise ValueError("task cannot depend on itself")
        with self.db.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_id) VALUES(?, ?)",
                (task_id, depends_on_id),
            )

    def unresolved_dependencies(self, task_id: str) -> List[Dict[str, Any]]:
        return self.db.all(
            """
            SELECT t.id, t.title, t.state
            FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_id
            WHERE d.task_id=? AND t.state != 'DONE'
            """,
            (task_id,),
        )

    @staticmethod
    def _process_alive(pid: Any) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError, TypeError):
            return False
        except PermissionError:
            return True
        return True

    def derive_task_state(
        self,
        task: Dict[str, Any],
        latest_run: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Resolve the task state from durable execution facts in one place.

        A live managed process is stronger evidence than task-level external
        heartbeat metadata. Terminal runs are likewise stronger than a stale
        RUNNING/QUEUED task row. Callers may project this result for reads or
        persist it from the reconciler.
        """
        run = latest_run
        if run is None:
            run = self.db.one(
                "SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                (task["id"],),
            )
        stored_state = str(task.get("state") or "INBOX").upper()
        result: Dict[str, Any] = {
            "state": stored_state,
            "stored_state": stored_state,
            "source": "task",
            "reason": "",
            "syncing": False,
            "live_run": False,
            "process_alive": False,
            "run_id": run.get("id") if run else "",
            "run_status": str(run.get("status") or "") if run else "",
        }
        if run and str(run.get("status") or "").upper() in {"QUEUED", "RUNNING"}:
            process_alive = self._process_alive(run.get("pid"))
            result["process_alive"] = process_alive
            if process_alive:
                execution_mode = str(
                    task.get("execution_mode") or "managed"
                ).lower()
                result.update(
                    {
                        "state": "RUNNING",
                        "source": "live_run",
                        "reason": "active {} run has a live worker pid".format(
                            str(run.get("status") or "").upper()
                        ),
                        "syncing": (
                            stored_state != "RUNNING"
                            or str(run.get("status") or "").upper() == "QUEUED"
                            or execution_mode != "managed"
                            or str(task.get("blocking_reason") or "").startswith(
                                "STALE_EXECUTION:"
                            )
                        ),
                        "live_run": True,
                    }
                )
                return result
            if run.get("pid") or str(run.get("status") or "").upper() == "RUNNING":
                result.update(
                    {
                        "state": "WAITING",
                        "source": "lost_run",
                        "reason": "active run worker pid is not alive",
                        "syncing": stored_state != "WAITING",
                    }
                )
                return result

        terminal_authoritative = True
        if run and str(run.get("status") or "").upper() in {
            "DONE",
            "FAILED",
            "CANCELED",
        }:
            try:
                finished_at = datetime.fromisoformat(
                    str(run.get("finished_at") or run.get("created_at"))
                )
                task_updated_at = datetime.fromisoformat(str(task.get("updated_at")))
                if finished_at.tzinfo is None:
                    finished_at = finished_at.replace(tzinfo=timezone.utc)
                if task_updated_at.tzinfo is None:
                    task_updated_at = task_updated_at.replace(tzinfo=timezone.utc)
                terminal_authoritative = finished_at >= task_updated_at
            except (TypeError, ValueError):
                terminal_authoritative = True
        if (
            terminal_authoritative
            and run
            and str(run.get("status") or "").upper() == "DONE"
        ):
            if stored_state == "WAITING" and str(
                task.get("action_owner_kind") or "none"
            ).lower() in {"user", "external"}:
                return result
            missing = self.missing_completion_evidence(task["id"])
            resolved = "VERIFYING" if missing else "DONE"
            result.update(
                {
                    "state": resolved,
                    "source": "completed_run",
                    "reason": (
                        "run finished; completion evidence is pending"
                        if missing
                        else "run finished and completion evidence is satisfied"
                    ),
                    "syncing": stored_state != resolved,
                }
            )
            return result

        if (
            terminal_authoritative
            and run
            and str(run.get("status") or "").upper() in {"FAILED", "CANCELED"}
        ):
            if stored_state not in {"DONE", "CANCELED"}:
                result.update(
                    {
                        "state": "WAITING",
                        "source": "terminal_run",
                        "reason": "run ended without a successful result",
                        "syncing": stored_state != "WAITING",
                    }
                )
            return result

        execution_mode = str(task.get("execution_mode") or "managed").lower()
        if stored_state == "RUNNING" and execution_mode in {"external", "delegated"}:
            heartbeat_at = task.get("heartbeat_at") or task.get("updated_at")
            expired = heartbeat_at is None
            if heartbeat_at:
                try:
                    seen_at = datetime.fromisoformat(str(heartbeat_at))
                    if seen_at.tzinfo is None:
                        seen_at = seen_at.replace(tzinfo=timezone.utc)
                    expired = (
                        self._utc_datetime(now) - seen_at.astimezone(timezone.utc)
                    ).total_seconds() > EXTERNAL_HEARTBEAT_TTL_SECONDS
                except ValueError:
                    expired = True
            if expired:
                result.update(
                    {
                        "state": "WAITING",
                        "source": "stale_external_heartbeat",
                        "reason": "external execution heartbeat expired",
                        "syncing": True,
                    }
                )
        return result

    def reconcile_derived_states(self) -> Dict[str, int]:
        """Persist drift detected by :meth:`derive_task_state`.

        This is intentionally the only background path that translates run and
        heartbeat facts back into the task row.
        """
        counters = {
            "synchronized": 0,
            "live_run_restored": 0,
            "run_completed": 0,
            "run_waiting": 0,
            "external_stale": 0,
            "delegated_stale": 0,
        }
        tasks = self.db.all("SELECT * FROM tasks ORDER BY updated_at ASC")
        latest_runs = {
            row["task_id"]: row
            for row in self.db.all(
                """
                SELECT run.*
                FROM runs run
                JOIN (
                    SELECT task_id, MAX(attempt) AS attempt
                    FROM runs GROUP BY task_id
                ) latest
                ON latest.task_id=run.task_id AND latest.attempt=run.attempt
                """
            )
        }
        for task in tasks:
            run = latest_runs.get(task["id"])
            resolution = self.derive_task_state(task, run)
            source = str(resolution["source"])
            target = str(resolution["state"])
            metadata_drift = bool(
                source == "live_run"
                and (
                    str(task.get("execution_mode") or "managed").lower()
                    != "managed"
                    or str(task.get("blocking_reason") or "").startswith(
                        "STALE_EXECUTION:"
                    )
                )
            )
            state_drift = target != task["state"]
            if not state_drift and not metadata_drift:
                continue

            blocking_reason = ""
            if source == "lost_run":
                blocking_reason = (
                    "后台执行器进程已丢失；等待安全恢复，不自动重放外部动作"
                )
            elif source == "terminal_run":
                blocking_reason = "最近一次执行未成功；等待安全重试"
            elif source == "stale_external_heartbeat":
                mode = str(task.get("execution_mode") or "external").lower()
                blocking_reason = (
                    "STALE_EXECUTION:{} heartbeat expired; "
                    "require a real heartbeat before RUNNING"
                ).format(mode)

            if source == "live_run":
                self.db.execute(
                    """
                    UPDATE tasks SET execution_mode='managed', heartbeat_at=NULL,
                        blocking_reason='', updated_at=?
                    WHERE id=?
                    """,
                    (utc_now(), task["id"]),
                )

            summaries = {
                "live_run": "检测到存活执行器，任务状态已同步为执行中",
                "lost_run": "执行器进程已丢失，任务进入安全恢复",
                "completed_run": (
                    "执行结束，证据门禁已通过"
                    if target == "DONE"
                    else "执行结束，进入证据校验"
                ),
                "terminal_run": "执行未成功，任务进入安全恢复",
                "stale_external_heartbeat": "外部执行心跳过期，进入恢复队列",
            }
            run_token = str(resolution.get("run_id") or task.get("heartbeat_at") or "none")
            if state_drift:
                self.transition(
                    task["id"],
                    target,
                    producer="state-reconciler",
                    summary=summaries.get(source, "任务状态已按执行事实同步"),
                    dedupe_key="derived-state:{}:{}:{}:{}".format(
                        task["id"], run_token, source, target
                    ),
                    blocking_reason=blocking_reason,
                    force=True,
                )
            else:
                self.db.add_event(
                    task["id"],
                    "task.state_synchronized",
                    "state-reconciler",
                    summaries.get(source, "任务状态元数据已同步"),
                    "derived-metadata:{}:{}:{}".format(
                        task["id"], run_token, source
                    ),
                    {
                        "state": target,
                        "source": source,
                        "run_id": resolution.get("run_id") or "",
                    },
                )
            counters["synchronized"] += 1
            if source == "live_run":
                counters["live_run_restored"] += 1
            elif source == "completed_run":
                counters["run_completed"] += 1
            elif source in {"lost_run", "terminal_run"}:
                counters["run_waiting"] += 1
            elif source == "stale_external_heartbeat":
                mode = str(task.get("execution_mode") or "external").lower()
                key = "delegated_stale" if mode == "delegated" else "external_stale"
                counters[key] += 1
        return counters

    def reconcile_state_progression(self) -> Dict[str, int]:
        """Advance only states whose existing facts make the next state certain."""
        completed = 0
        dependencies_ready = 0
        evidence_completed = 0
        verifying = self.db.all(
            "SELECT id FROM tasks WHERE state='VERIFYING' ORDER BY updated_at ASC"
        )
        for task in verifying:
            if self.missing_completion_evidence(task["id"]):
                continue
            self.transition(
                task["id"],
                "DONE",
                producer="reconciler",
                summary="证据门禁已满足，自动完成",
                dedupe_key="completion-gate:{}".format(task["id"]),
            )
            completed += 1

        evidence_waiters = self.db.all(
            """
            SELECT id FROM tasks
            WHERE state='WAITING'
              AND blocking_reason LIKE 'EVIDENCE_COLLECTION_REQUIRED:%'
            ORDER BY updated_at ASC
            """
        )
        for task in evidence_waiters:
            if self.missing_completion_evidence(task["id"]):
                continue
            self.transition(
                task["id"],
                "DONE",
                producer="reconciler",
                summary="专属证据采集已闭环，自动完成",
                dedupe_key="evidence-collection-complete:{}".format(task["id"]),
                force=True,
            )
            evidence_completed += 1

        dependency_waiters = self.db.all(
            """
            SELECT DISTINCT task.*
            FROM tasks task
            JOIN task_dependencies dependency ON dependency.task_id=task.id
            WHERE task.state='WAITING'
            ORDER BY task.updated_at ASC
            """
        )
        for task in dependency_waiters:
            if self.unresolved_dependencies(task["id"]):
                continue
            if str(task.get("action_owner_kind") or "none") != "none":
                continue
            if str(task.get("execution_mode") or "managed").lower() != "managed":
                continue
            blocker = str(task.get("blocking_reason") or "").strip()
            if blocker and not (
                blocker.startswith("依赖")
                or blocker.startswith("DEPENDENCY:")
            ):
                continue
            self.transition(
                task["id"],
                "PLANNED",
                producer="reconciler",
                summary="所有依赖已完成，自动回到待分发",
                dedupe_key="dependencies-ready:{}".format(task["id"]),
                force=True,
            )
            dependencies_ready += 1
        return {
            "completed": completed,
            "dependencies_ready": dependencies_ready,
            "evidence_completed": evidence_completed,
        }

    def add_evidence(
        self,
        task_id: str,
        kind: str,
        value: str,
        label: str = "",
        verified: bool = False,
    ) -> bool:
        clean_kind = re.sub(r"[^a-z0-9_-]", "", kind.lower())
        if not clean_kind:
            raise ValueError("invalid evidence kind")
        clean_value = redact_text(value, max_chars=500)
        now = utc_now()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO evidence(task_id, kind, value, label, verified, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    clean_kind,
                    clean_value,
                    redact_text(label, max_chars=200),
                    int(verified),
                    now,
                ),
            )
        inserted = cursor.rowcount == 1
        if inserted:
            self.db.add_event(
                task_id,
                "evidence.added",
                "control-plane",
                "新增 {} 证据".format(clean_kind),
                "evidence:{}:{}:{}".format(task_id, clean_kind, fingerprint(clean_value)),
                {"state": self.get_task(task_id)["state"]},
            )
        return inserted

    def _required_evidence_for_task(
        self, task: Dict[str, Any], existing_kinds: Optional[set] = None
    ) -> List[str]:
        profile = str(task.get("evidence_profile") or "auto").lower()
        repository = Path(str(task.get("repository") or "")).expanduser()
        auto_local_artifact = bool(
            profile == "auto"
            and task.get("repository")
            and repository.exists()
            and not (repository / ".git").exists()
        )
        if profile == "legacy":
            required = ["migration_metadata"]
        elif profile == "browser" or (
            profile == "auto" and task["worker_type"].lower() == "browser"
        ):
            required = ["browser"]
        elif profile == "qa" or (
            profile == "auto" and task["worker_type"].lower() == "qa"
        ):
            required = ["test"]
        elif profile == "code" or (
            profile == "auto"
            and task["repository"]
            and not auto_local_artifact
        ):
            required = ["commit", "test"]
        else:
            required = ["artifact"]
        # An evidence category is an observation, not authorization or a new
        # requirement. Explicit deployment requirements apply to every profile;
        # an unverified "deploy: not authorized/not run" note adds no requirement.
        if task["requires_deploy"]:
            required.extend(["deploy", "smoke"])
        return required

    def required_evidence(self, task_id: str) -> List[str]:
        task = self.get_task(task_id)
        existing_kinds = {
            row["kind"]
            for row in self.db.all(
                "SELECT DISTINCT kind FROM evidence WHERE task_id=?", (task_id,)
            )
        }
        return self._required_evidence_for_task(task, existing_kinds)

    def missing_completion_evidence(self, task_id: str) -> List[str]:
        required = self.required_evidence(task_id)
        present = {
            row["kind"]
            for row in self.db.all(
                "SELECT DISTINCT kind FROM evidence "
                "WHERE task_id=? AND verified=1",
                (task_id,),
            )
        }
        return [kind for kind in required if kind not in present]

    def reconcile_evidence_profiles(self) -> Dict[str, int]:
        """Correct only evidence policy metadata; never manufacture evidence."""
        corrected_code = 0
        marked_legacy = 0
        candidates = self.db.all(
            """
            SELECT * FROM tasks
            WHERE evidence_profile='auto' AND repository=''
                AND imported_from!=''
            ORDER BY created_at ASC
            """
        )
        for task in candidates:
            worker_type = str(task.get("worker_type") or "").lower()
            if worker_type in {"browser", "qa"}:
                continue
            verified_kinds = {
                row["kind"]
                for row in self.db.all(
                    "SELECT DISTINCT kind FROM evidence "
                    "WHERE task_id=? AND verified=1",
                    (task["id"],),
                )
            }
            profile = (
                "code"
                if verified_kinds.intersection(CODE_EVIDENCE_KINDS)
                else "legacy"
            )
            self.db.execute(
                "UPDATE tasks SET evidence_profile=?, updated_at=? WHERE id=?",
                (profile, utc_now(), task["id"]),
            )
            self.db.add_event(
                task["id"],
                "task.evidence_profile_reconciled",
                "reconciler",
                (
                    "依据既有已验证代码证据纠偏为 code profile"
                    if profile == "code"
                    else "旧迁移任务等待 evidence profile 元数据复核"
                ),
                "evidence-profile-reconciled:{}:{}".format(task["id"], profile),
                {
                    "state": task["state"],
                    "evidence_profile": profile,
                    "evidence_created": False,
                },
            )
            if profile == "code":
                corrected_code += 1
            else:
                marked_legacy += 1
        return {"code": corrected_code, "legacy": marked_legacy}

    def record_authorization(
        self,
        task_id: Optional[str],
        action: str,
        policy: str,
        decision: str,
        reason: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO authorization_audit(task_id, action, policy, decision, reason, occurred_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                redact_text(action, max_chars=100),
                redact_text(policy, max_chars=100),
                redact_text(decision, max_chars=50),
                redact_text(reason, max_chars=300),
                utc_now(),
            ),
        )

    def upsert_session(
        self,
        session_id: str,
        code: str,
        name: str,
        worker_type: str,
        scope_summary: str,
        source: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO sessions(
                id, code, name, worker_type, scope_summary, status,
                model, reasoning, last_seen_at, source
            ) VALUES(?, ?, ?, ?, ?, 'IDLE', 'gpt-5.6-sol', 'high', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                code=excluded.code, name=excluded.name, worker_type=excluded.worker_type,
                scope_summary=excluded.scope_summary, last_seen_at=excluded.last_seen_at,
                source=excluded.source
            """,
            (
                session_id,
                code,
                redact_text(name, max_chars=160),
                worker_type,
                redact_text(scope_summary, max_chars=500),
                utc_now(),
                source,
            ),
        )

    def heartbeat_task(
        self,
        task_id: str,
        producer: str = "external-manager",
        execution_mode: str = "external",
    ) -> Dict[str, Any]:
        task = self.get_task(task_id)
        clean_mode = str(execution_mode or "external").lower()
        if clean_mode not in {"external", "delegated"}:
            raise ValueError("heartbeat mode must be external or delegated")
        if task["state"] in {"DONE", "CANCELED"}:
            raise ValueError("terminal task cannot register an execution heartbeat")
        active_run = self.db.one(
            "SELECT * FROM runs WHERE task_id=? AND status IN ('QUEUED','RUNNING') "
            "ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        local_run_alive = bool(
            active_run and self._process_alive(active_run.get("pid"))
        )
        now = utc_now()
        self.db.execute(
            """
            UPDATE tasks SET execution_mode=?, heartbeat_at=?,
                blocking_reason='', updated_at=?
            WHERE id=?
            """,
            (
                "managed" if local_run_alive else clean_mode,
                None if local_run_alive else now,
                now,
                task_id,
            ),
        )
        if task["state"] != "RUNNING":
            self.transition(
                task_id,
                "RUNNING",
                producer=producer,
                summary=(
                    "检测到存活本地执行器，任务恢复执行中"
                    if local_run_alive
                    else (
                        "已登记 Codex 直接委派执行"
                        if clean_mode == "delegated"
                        else "已登记外部执行"
                    )
                ),
                dedupe_key="execution-heartbeat-resumed:{}:{}".format(
                    task_id, now
                ),
                force=True,
            )
        self.db.add_event(
            task_id,
            "task.external_heartbeat",
            producer,
            "外部执行心跳已更新",
            "external-heartbeat:{}:{}".format(task_id, now[:16]),
            {
                "state": self.get_task(task_id)["state"],
                "execution_mode": "managed" if local_run_alive else clean_mode,
            },
        )
        return self.get_task(task_id)

    def reroute(self, task_id: str) -> Dict[str, Any]:
        task = self.get_task(task_id)
        route = route_task(task["title"], task["scope_summary"], task["repository"])
        policy = runtime_policy(route.reasoning)
        self.db.execute(
            """
            UPDATE tasks SET worker_type=?, owner_session=?, model=?, reasoning=?,
                speed=?, updated_at=? WHERE id=?
            """,
            (
                route.worker_type,
                route.owner_session,
                policy.model,
                policy.reasoning,
                policy.speed,
                utc_now(),
                task_id,
            ),
        )
        self.db.add_event(
            task_id,
            "task.routed",
            "router",
            "{} → {}".format(route.reason, route.owner_session),
            "task-reroute:{}:{}:{}".format(
                task_id, route.owner_session, fingerprint(task["scope_summary"])
            ),
            {"state": task["state"]},
        )
        return self.get_task(task_id)

    def reroute_imported(self) -> int:
        rows = self.db.all("SELECT id FROM tasks WHERE imported_from != ''")
        for row in rows:
            self.reroute(row["id"])
        return len(rows)

    @staticmethod
    def _utc_datetime(value: Optional[datetime] = None) -> datetime:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def rolling_24h_tasks(
        self, now: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Return active tasks plus tasks completed in the trailing 24 hours."""
        window_end = self._utc_datetime(now)
        window_start = window_end - timedelta(hours=24)
        return self.db.all(
            """
            SELECT * FROM tasks
            WHERE state NOT IN ('DONE', 'CANCELED')
               OR (
                    state='DONE'
                    AND COALESCE(finished_at, updated_at) >= ?
               )
            ORDER BY priority ASC, updated_at DESC
            """,
            (window_start.isoformat(timespec="seconds"),),
        )

    def rolling_24h_summary(
        self, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        window_end = self._utc_datetime(now)
        window_start = window_end - timedelta(hours=24)
        tasks = self.rolling_24h_tasks(window_end)
        total = len(tasks)
        return {
            "window_started_at": window_start.isoformat(timespec="seconds"),
            "window_ended_at": window_end.isoformat(timespec="seconds"),
            "done": sum(1 for task in tasks if task["state"] == "DONE"),
            "total": total,
            "average_progress": (
                round(sum(int(task["progress"]) for task in tasks) / total)
                if total
                else 0
            ),
        }

    def dashboard_payload(
        self, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        tasks = self.list_tasks(limit=1000)
        latest_runs = {
            row["task_id"]: row
            for row in self.db.all(
                """
                SELECT run.*
                FROM runs run
                JOIN (
                    SELECT task_id, MAX(attempt) AS attempt
                    FROM runs GROUP BY task_id
                ) latest
                ON latest.task_id=run.task_id AND latest.attempt=run.attempt
                """
            )
        }
        columns: Dict[str, List[Dict[str, Any]]] = {state: [] for state in BOARD_STATES}
        for task in tasks:
            resolution = self.derive_task_state(task, latest_runs.get(task["id"]), now)
            task["stored_state"] = task["state"]
            task["state_resolution"] = resolution
            task["state"] = resolution["state"]
            if resolution["syncing"]:
                task["progress"] = STATE_PROGRESS.get(
                    resolution["state"], task["progress"]
                )
            display_state = task["state"]
            if display_state == "CANCELED":
                display_state = "CANCELED"
            elif is_paused_by_user(task):
                display_state = "PAUSED"
            elif is_plan_only(task):
                display_state = "PLAN_ONLY"
            elif display_state in {"PLANNED", "QUEUED"}:
                display_state = "INBOX"
            elif display_state == "FAILED":
                display_state = "WAITING"
            task["display_state"] = display_state
            columns.setdefault(display_state, []).append(task)
        events = self.db.all(
            """
            SELECT e.id, e.event_id, e.task_id, e.event_type, e.producer,
                e.summary, e.occurred_at, t.title
            FROM events e JOIN tasks t ON t.id=e.task_id
            WHERE e.event_type!='codex.event_skipped'
            ORDER BY e.id DESC LIMIT 60
            """
        )
        cursor = self.event_cursor()
        evidence_counts = self.db.all(
            """
            SELECT task_id, COUNT(*) AS count,
                SUM(CASE WHEN verified=1 THEN 1 ELSE 0 END) AS verified_count
            FROM evidence GROUP BY task_id
            """
        )
        counts = {row["task_id"]: row["count"] for row in evidence_counts}
        verified_counts = {
            row["task_id"]: int(row["verified_count"] or 0)
            for row in evidence_counts
        }
        recovered_runs = self.db.all(
            """
            SELECT DISTINCT later.task_id
            FROM runs failed
            JOIN runs later
                ON later.task_id=failed.task_id
                AND later.attempt > failed.attempt
            WHERE failed.failure_kind='INFRASTRUCTURE'
            """
        )
        recovering = {row["task_id"] for row in recovered_runs}
        latest_event_times = {
            row["task_id"]: row["occurred_at"]
            for row in self.db.all(
                """
                SELECT event.task_id, event.occurred_at
                FROM events event
                JOIN (
                    SELECT task_id, MAX(id) AS id FROM events GROUP BY task_id
                ) latest ON latest.id=event.id
                """
            )
        }
        latest_worker_events = {
            row["task_id"]: row
            for row in self.db.all(
                """
                SELECT event.task_id, event.event_type, event.occurred_at
                FROM events event JOIN (
                    SELECT task_id, MAX(id) AS id FROM events
                    WHERE producer='codex-json' AND event_type!='codex.event_skipped'
                    GROUP BY task_id
                ) latest ON latest.id=event.id
                """
            )
        }
        backfill_events = self.db.all(
            """
            SELECT task_id, payload_json, occurred_at
            FROM events
            WHERE event_type='verification.backfill_queued'
            ORDER BY id ASC
            """
        )
        backfill_status: Dict[str, Dict[str, Any]] = {}
        for event in backfill_events:
            status = backfill_status.setdefault(
                event["task_id"],
                {"attempts": 0, "last_at": "", "run_attempt": 0},
            )
            status["attempts"] += 1
            status["last_at"] = event["occurred_at"]
            try:
                event_payload = json.loads(event["payload_json"] or "{}")
            except (TypeError, ValueError):
                event_payload = {}
            status["run_attempt"] = int(event_payload.get("attempt") or 0)
        backfill_requested = set(backfill_status)
        independent_qa_tasks = {
            row["task_id"]
            for row in self.db.all(
                "SELECT DISTINCT task_id FROM events "
                "WHERE event_type='verification.internal_qa_hold'"
            )
        }
        auto_closure_tasks = {
            row["task_id"]
            for row in self.db.all(
                "SELECT DISTINCT task_id FROM events "
                "WHERE event_type='verification.auto_closure'"
            )
        }
        action_started_at = {
            row["task_id"]: row["occurred_at"]
            for row in self.db.all(
                """
                SELECT event.task_id, event.occurred_at
                FROM events event
                JOIN (
                    SELECT task_id, MAX(id) AS id
                    FROM events
                    WHERE event_type='task.human_action_changed'
                    GROUP BY task_id
                ) latest ON latest.id=event.id
                """
            )
        }
        all_evidence_kinds: Dict[str, set] = {}
        evidence_kinds: Dict[str, set] = {}
        for row in self.db.all("SELECT task_id, kind, verified FROM evidence"):
            all_evidence_kinds.setdefault(row["task_id"], set()).add(row["kind"])
            if int(row["verified"] or 0) == 1:
                evidence_kinds.setdefault(row["task_id"], set()).add(row["kind"])
        runtime_now = datetime.now(timezone.utc)
        for task in tasks:
            task["evidence_count"] = counts.get(task["id"], 0)
            task["verified_evidence_count"] = verified_counts.get(task["id"], 0)
            task["recovery_status"] = (
                "执行器已恢复并重试" if task["id"] in recovering else ""
            )
            required = set(
                self._required_evidence_for_task(
                    task, all_evidence_kinds.get(task["id"], set())
                )
            )
            missing = sorted(required - evidence_kinds.get(task["id"], set()))
            run = latest_runs.get(task["id"])
            worker_event = latest_worker_events.get(task["id"], {})
            task["activity"] = {
                "run_started_at": run.get("started_at") if run else None,
                "run_finished_at": run.get("finished_at") if run else None,
                "last_event_at": worker_event.get("occurred_at"),
                "last_event_type": worker_event.get("event_type"),
                "progress_kind": "state-milestone-not-completion",
                "synthetic": task.get("imported_from") == "demo",
            }
            backfill = backfill_status.get(
                task["id"], {"attempts": 0, "last_at": "", "run_attempt": 0}
            )
            runtime = {
                "code": "IDLE",
                "label": "",
                "action": "",
                "recovery": {"required": False},
                "missing_evidence": missing,
                "backfill_attempts": int(backfill["attempts"]),
                "backfill_limit": MAX_VERIFICATION_BACKFILL_ATTEMPTS,
            }
            state_resolution = task.get("state_resolution") or {}
            execution_mode = str(task.get("execution_mode") or "managed").lower()
            heartbeat_age: Optional[int] = None
            heartbeat_at = task.get("heartbeat_at") or task.get("updated_at")
            if heartbeat_at:
                try:
                    heartbeat_seen = datetime.fromisoformat(str(heartbeat_at))
                    if heartbeat_seen.tzinfo is None:
                        heartbeat_seen = heartbeat_seen.replace(tzinfo=timezone.utc)
                    heartbeat_age = max(
                        0, int((runtime_now - heartbeat_seen).total_seconds())
                    )
                except ValueError:
                    heartbeat_age = None
            runtime["heartbeat_age_seconds"] = heartbeat_age
            if state_resolution.get("live_run") and state_resolution.get("syncing"):
                runtime.update(
                    {
                        "code": "STATE_SYNCING",
                        "label": "执行中（状态同步中）",
                        "action": "已检测到存活执行器，正在校正任务状态",
                    }
                )
            elif (
                task["state"] == "RUNNING"
                and execution_mode in {"external", "delegated"}
            ):
                stale_heartbeat = (
                    heartbeat_age is None
                    or heartbeat_age > EXTERNAL_HEARTBEAT_TTL_SECONDS
                )
                runtime.update(
                    {
                        "code": (
                            "EXTERNAL_STALE"
                            if stale_heartbeat
                            else (
                                "DELEGATED_AGENT"
                                if execution_mode == "delegated"
                                else "EXTERNAL"
                            )
                        ),
                        "label": (
                            (
                                "委派执行心跳已过期"
                                if execution_mode == "delegated"
                                else "外部执行心跳已过期"
                            )
                            if stale_heartbeat
                            else (
                                "Codex 子任务执行中 · 心跳正常"
                                if execution_mode == "delegated"
                                else "外部 Codex 任务执行中 · 心跳正常"
                            )
                        ),
                        "action": (
                            "恢复入口：原执行器真实恢复后上报心跳；不会自动运行"
                            if stale_heartbeat
                            else "最近心跳 {} 秒前".format(heartbeat_age or 0)
                        ),
                        "recovery": (
                            {
                                "required": True,
                                "mode": execution_mode,
                                "automatic": False,
                                "heartbeat_endpoint": "/api/tasks/{}/heartbeat".format(
                                    task["id"]
                                ),
                                "instruction": (
                                    "先恢复原 Codex 子任务，再由该执行器提交真实心跳；"
                                    "看板不会代替执行器续命。"
                                ),
                            }
                            if stale_heartbeat
                            else {"required": False}
                        ),
                    }
                )
            elif str(task.get("blocking_reason") or "").startswith(
                "STALE_EXECUTION:"
            ):
                recovery_mode = (
                    "delegated" if execution_mode == "delegated" else "external"
                )
                runtime.update(
                    {
                        "code": "RECOVERY_REQUIRED",
                        "label": (
                            "委派执行已失联"
                            if recovery_mode == "delegated"
                            else "外部执行已失联"
                        ),
                        "action": "恢复入口：原执行器真实恢复后上报心跳；不会自动运行",
                        "recovery": {
                            "required": True,
                            "mode": recovery_mode,
                            "automatic": False,
                            "heartbeat_endpoint": "/api/tasks/{}/heartbeat".format(
                                task["id"]
                            ),
                            "instruction": (
                                "先恢复原 Codex 子任务，再由该执行器提交真实心跳；"
                                "看板不会代替执行器续命。"
                            ),
                        },
                    }
                )
            elif (
                task["id"] in auto_closure_tasks
                and task["state"] != "DONE"
                and run
                and run["status"] in {"QUEUED", "RUNNING"}
            ):
                runtime.update(
                    {
                        "code": "AUTO_CLOSURE",
                        "label": "执行产物已保留 · 正在自动收口",
                        "action": "Admin owner 正在补齐 commit/test/deploy/smoke"
                        if run and run["status"] in {"QUEUED", "RUNNING"}
                        else "收口运行已结束，等待证据校验",
                    }
                )
            elif (
                task["id"] in auto_closure_tasks
                and task["state"] != "DONE"
                and run
                and run["status"] == "FAILED"
            ):
                runtime.update(
                    {
                        "code": "FAILED_STAGE",
                        "label": "自动收口失败：{}/{}".format(
                            run.get("failure_stage") or "unknown",
                            run.get("failure_type") or "unknown",
                        ),
                        "action": "已停止重复重试，保留工作树等待安全恢复",
                    }
                )
            elif (
                str(task.get("blocking_reason", "")).startswith(
                    "WAITING_INDEPENDENT_QA"
                )
                or task["id"] in independent_qa_tasks
            ):
                runtime.update(
                    {
                        "code": "INDEPENDENT_QA",
                        "label": "无需你提供 · 等待 QT-06 内部验收",
                        "action": "内部 QA 正在补齐证据"
                        if run and run["status"] in {"QUEUED", "RUNNING"}
                        else "内部 QA 已补跑，等待验收结论",
                    }
                )
            elif task["state"] == "RUNNING":
                if not run or run["status"] not in {"QUEUED", "RUNNING"}:
                    runtime.update(
                        {
                            "code": "NO_RUN",
                            "label": "状态不一致：无活动 Run",
                            "action": "重新派发或标记外部执行",
                        }
                    )
                else:
                    process_alive = bool(
                        state_resolution.get("process_alive")
                    )
                    if not process_alive:
                        runtime.update(
                            {
                                "code": "PROCESS_LOST",
                                "label": "执行器进程已丢失",
                                "action": "安全重试",
                            }
                        )
                    else:
                        occurred_at = latest_event_times.get(task["id"])
                        stale = False
                        if occurred_at:
                            try:
                                seen_at = datetime.fromisoformat(occurred_at)
                                if seen_at.tzinfo is None:
                                    seen_at = seen_at.replace(tzinfo=timezone.utc)
                                age = runtime_now - seen_at
                                stale = age.total_seconds() > 180
                            except ValueError:
                                stale = False
                        runtime.update(
                            {
                                "code": "EVENT_STALE" if stale else "HEALTHY",
                                "label": "事件超过 3 分钟未更新"
                                if stale
                                else "执行器健康",
                                "action": "暂未收到新事件；不等于卡死，请先检查运行详情"
                                if stale
                                else "",
                            }
                        )
            elif (
                task["state"] in {"VERIFYING", "FAILED"}
                and missing
                and task["id"] in backfill_requested
                and run
                and (
                    run["status"] != "FAILED"
                    or int(backfill.get("run_attempt") or 0) == int(run["attempt"])
                )
            ):
                if run["status"] in {"QUEUED", "RUNNING"}:
                    label = "QA 证据回填中"
                    code = "BACKFILL_RUNNING"
                elif int(backfill["attempts"]) >= MAX_VERIFICATION_BACKFILL_ATTEMPTS:
                    label = "QA 回填已达上限：仍缺 {}".format("/".join(missing))
                    code = "BACKFILL_EXHAUSTED"
                else:
                    label = "QA 回填可恢复重试：仍缺 {}".format("/".join(missing))
                    code = "BACKFILL_INCOMPLETE"
                runtime.update(
                    {
                        "code": code,
                        "label": label,
                        "action": "有界重试真实 QA 证据；不伪造验收",
                    }
                )
            elif task["state"] == "VERIFYING" and missing:
                if run and run["status"] in {"QUEUED", "RUNNING"}:
                    label = "QA 证据回填中"
                    code = "BACKFILL_RUNNING"
                elif run and run["status"] == "DONE" and task["id"] not in backfill_requested:
                    label = "待自动 QA 回填：缺 {}".format("/".join(missing))
                    code = "BACKFILL_REQUIRED"
                elif task.get("evidence_profile") == "legacy":
                    label = "迁移元数据待复核（未补造证据）"
                    code = "MIGRATION_REVIEW"
                else:
                    label = "人工迁移待回填：缺 {}".format("/".join(missing))
                    code = "EVIDENCE_REQUIRED"
                runtime.update(
                    {
                        "code": code,
                        "label": label,
                        "action": (
                            "补充 repository 或明确 evidence profile；不创建假证据"
                            if code == "MIGRATION_REVIEW"
                            else "回填真实 commit/test/deploy/smoke 证据"
                        ),
                    }
                )
            task["runtime_status"] = runtime
            stored_action_kind = str(
                task.get("action_owner_kind") or "none"
            ).lower()
            paused = is_paused_by_user(task)
            plan_only = is_plan_only(task)
            terminal_canceled = task["state"] == "CANCELED"
            action_kind = (
                "none"
                if state_resolution.get("live_run")
                or paused
                or plan_only
                or terminal_canceled
                else stored_action_kind
            )
            action_owner = str(task.get("action_owner") or "").strip()
            action_text = str(task.get("action_text") or "").strip()
            if paused:
                display_action = "无需你处理 · 已暂停，等待明确恢复指令"
                action_rank = 4
            elif plan_only:
                display_action = "无需你处理 · 仅规划，不进入自动执行"
                action_rank = 4
            elif terminal_canceled:
                display_action = "无需你处理 · 已取消，不再进入等待或调度"
                action_rank = 4
            elif action_kind == "user":
                display_action = "你需要：{}".format(action_text)
                action_rank = 0
            elif runtime["code"] == "FAILED_STAGE":
                display_action = runtime["label"]
                action_rank = 1
            elif task["state"] == "FAILED":
                display_action = "执行失败：{}/{}".format(
                    run.get("failure_stage") if run else "unknown",
                    run.get("failure_type") if run else "unknown",
                )
                action_rank = 1
            elif action_kind == "external":
                display_action = "等待{}：{}".format(
                    " {}".format(action_owner) if action_owner else "外部",
                    action_text,
                )
                action_rank = 2
            elif runtime["code"] in {"EXTERNAL_STALE", "RECOVERY_REQUIRED"}:
                display_action = runtime["label"]
                action_rank = 1
            elif action_kind == "agent":
                # Internal owners use this sentence as the live card summary.
                # It stays out of both the user-action and external queues.
                display_action = action_text
                action_rank = 3
            elif runtime["code"] in {"EXTERNAL", "DELEGATED_AGENT"}:
                display_action = runtime["label"]
                action_rank = 3
            elif runtime["code"] == "STATE_SYNCING":
                display_action = "执行中（状态同步中）"
                action_rank = 3
            elif runtime["code"] == "AUTO_CLOSURE":
                display_action = "执行产物已保留 · 正在自动收口"
                action_rank = 3
            elif runtime["code"] == "INDEPENDENT_QA":
                display_action = "无需你处理 · 等待 QT-06 内部验收"
                action_rank = 3
            elif task["recovery_status"]:
                display_action = "无需你处理 · {}".format(task["recovery_status"])
                action_rank = 3
            elif runtime["code"] in {"EVENT_STALE", "PROCESS_LOST", "NO_RUN"}:
                display_action = runtime["label"]
                action_rank = 1
            elif task["state"] == "RUNNING":
                display_action = "无需你处理 · 后台执行中"
                action_rank = 3
            elif task["state"] == "VERIFYING":
                display_action = "无需你处理 · 内部验收中"
                action_rank = 3
            elif task["state"] == "DONE":
                display_action = "无需你处理 · 已完成"
                action_rank = 4
            elif task["state"] in {"PLANNED", "QUEUED", "INBOX"}:
                display_action = "无需你处理 · 待擎天分发"
                action_rank = 4
            else:
                display_action = "无需你处理 · 系统等待中"
                action_rank = 4
            task["display_action"] = display_action
            task["action_rank"] = action_rank
            task["human_action"] = {
                "owner_kind": action_kind,
                "stored_owner_kind": stored_action_kind,
                "owner": action_owner,
                "text": action_text,
                "due": task.get("action_due"),
                "sensitive": bool(task.get("action_sensitive")),
                "requires_user": action_kind == "user",
                "since": action_started_at.get(task["id"]),
            }
            if task["state"] in {"WAITING", "FAILED"}:
                task["waiting_category"] = classify_waiting(task, runtime)
        sort_key = lambda item: (
            int(item.get("action_rank", 4)),
            int(item.get("priority", 3)),
            str(item.get("action_due") or "9999"),
            str(item.get("updated_at") or ""),
        )
        tasks.sort(key=sort_key)
        for state in BOARD_STATES:
            columns[state].sort(key=sort_key)
        action_summary = {
            "user": sum(
                1
                for task in tasks
                if task["display_state"] not in {"PAUSED", "PLAN_ONLY", "CANCELED"}
                and task["human_action"]["owner_kind"] == "user"
            ),
            "external": sum(
                1
                for task in tasks
                if task["display_state"] not in {"PAUSED", "PLAN_ONLY", "CANCELED"}
                and task["human_action"]["owner_kind"] == "external"
            ),
        }
        waiting_summary = {
            key: 0 for key in WAITING_CATEGORY_LABELS
        }
        for task in columns["WAITING"]:
            category = task.get("waiting_category") or classify_waiting(task)
            task["waiting_category"] = category
            waiting_summary[category["key"]] += 1
        return {
            "generated_at": utc_now(),
            "version": cursor,
            "action_summary": action_summary,
            "waiting_summary": waiting_summary,
            "waiting_labels": WAITING_CATEGORY_LABELS,
            "paused_count": len(columns["PAUSED"]),
            "plan_only_count": len(columns["PLAN_ONLY"]),
            "canceled_count": len(columns["CANCELED"]),
            "rolling_24h_summary": self.rolling_24h_summary(now),
            "columns": columns,
            "tasks": tasks,
            "events": events,
            "sessions": self.db.all(
                "SELECT * FROM sessions ORDER BY code ASC, name ASC"
            ),
            "runs": self.db.all(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT 50"
            ),
            "policy": {
                "model": "gpt-5.6-sol",
                "minimum_reasoning": "high",
                "dev_gate": "相关测试 + 最小冒烟",
                "sensitive_storage": "无完整 prompt / 无原始 JSONL",
            },
        }

    def event_cursor(self) -> int:
        row = self.db.one("SELECT COALESCE(MAX(id), 0) AS cursor FROM events")
        return int(row["cursor"]) if row else 0

    def realtime_payload(self, after: int = 0, limit: int = 100) -> Dict[str, Any]:
        """Return a versioned dashboard snapshot plus compact change metadata.

        SQLite's autoincrement event id is the source-of-truth cursor. Clients can
        safely discard snapshots whose version is not newer than the last applied
        version, while reconnecting clients use ``after`` to learn what changed.
        """
        clean_after = max(0, int(after))
        changes = self.db.all(
            """
            SELECT e.id, e.event_id, e.task_id, e.event_type, e.producer,
                e.summary, e.occurred_at
            FROM events e
            WHERE e.id > ?
            ORDER BY e.id ASC LIMIT ?
            """,
            (clean_after, max(1, min(500, int(limit)))),
        )
        dashboard = self.dashboard_payload()
        return {
            "version": int(dashboard["version"]),
            "changes": changes,
            "dashboard": dashboard,
        }

    def feedback_changes(
        self, consumer: str = "QT-00", limit: int = 30, advance: bool = True
    ) -> Dict[str, Any]:
        clean_consumer = redact_text(consumer, max_chars=80)
        cursor = self.db.one(
            "SELECT last_event_id FROM feedback_cursors WHERE consumer=?",
            (clean_consumer,),
        )
        last_event_id = int(cursor["last_event_id"]) if cursor else 0
        events = self.db.all(
            """
            SELECT e.id, e.task_id, e.event_type, e.producer, e.summary,
                e.occurred_at, t.title, t.state, t.progress, t.owner_session
            FROM events e JOIN tasks t ON t.id=e.task_id
            WHERE e.id > ?
            ORDER BY e.id ASC LIMIT ?
            """,
            (last_event_id, max(1, min(200, int(limit)))),
        )
        new_cursor = events[-1]["id"] if events else last_event_id
        if advance and events:
            self.db.execute(
                """
                INSERT INTO feedback_cursors(consumer, last_event_id, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(consumer) DO UPDATE SET
                    last_event_id=excluded.last_event_id,
                    updated_at=excluded.updated_at
                """,
                (clean_consumer, new_cursor, utc_now()),
            )
        return {
            "consumer": clean_consumer,
            "previous_cursor": last_event_id,
            "cursor": new_cursor,
            "advanced": bool(advance and events),
            "changes": events,
        }

    def initialize_feedback_cursor(
        self, consumer: str = "QT-00", force: bool = False
    ) -> Dict[str, Any]:
        clean_consumer = redact_text(consumer, max_chars=80)
        head = self.db.one("SELECT COALESCE(MAX(id), 0) AS id FROM events")
        event_id = int(head["id"]) if head else 0
        verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
        self.db.execute(
            "{} INTO feedback_cursors(consumer, last_event_id, updated_at) "
            "VALUES(?, ?, ?)".format(verb),
            (clean_consumer, event_id, utc_now()),
        )
        cursor = self.db.one(
            "SELECT last_event_id FROM feedback_cursors WHERE consumer=?",
            (clean_consumer,),
        )
        return {
            "consumer": clean_consumer,
            "cursor": int(cursor["last_event_id"]) if cursor else event_id,
        }

    def is_verification_backfill_run(self, task_id: str, attempt: int) -> bool:
        rows = self.db.all(
            """
            SELECT payload_json FROM events
            WHERE task_id=? AND event_type='verification.backfill_queued'
            ORDER BY id DESC
            """,
            (task_id,),
        )
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if int(payload.get("attempt") or 0) == int(attempt):
                return True
        return False

    @staticmethod
    def _qa_evidence_is_positive(value: str) -> bool:
        normalized = re.sub(
            r"\b0\s+(?:failed|failures|errors)\b", "", str(value), flags=re.I
        )
        return not bool(
            re.search(
                r"\b(?:blocked|partial|failed|failure|not\s+run|skipped)\b"
                r"|阻塞|失败|未运行|未验证",
                normalized,
                flags=re.I,
            )
        )

    def import_evidence_file(
        self, task_id: str, path: Path, verified: bool = False
    ) -> int:
        if not path.exists():
            return 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("evidence file must be an object")
        allowed = {"commit", "test", "deploy", "smoke", "browser", "artifact", "risk"}
        count = 0
        for kind, value in payload.items():
            if kind not in allowed:
                continue
            if isinstance(value, (str, int, float)):
                clean_value = str(value)
                count += int(
                    self.add_evidence(
                        task_id,
                        kind,
                        clean_value,
                        verified=bool(
                            verified and self._qa_evidence_is_positive(clean_value)
                        ),
                    )
                )
            elif isinstance(value, list):
                for item in value[:20]:
                    if isinstance(item, (str, int, float)):
                        clean_value = str(item)
                        count += int(
                            self.add_evidence(
                                task_id,
                                kind,
                                clean_value,
                                verified=bool(
                                    verified
                                    and self._qa_evidence_is_positive(clean_value)
                                ),
                            )
                        )
        try:
            path.unlink()
        except OSError:
            pass
        return count
