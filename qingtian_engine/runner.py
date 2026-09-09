from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ensure_data_dirs, runtime_policy
from .db import Database, utc_now
from .project_config import (
    ProjectConfigError,
    load_project_config,
)
from .redaction import fingerprint, redact_text
from .router import matching_routes
from .service import (
    MAX_VERIFICATION_BACKFILL_ATTEMPTS,
    ControlPlane,
    is_plan_only,
)
from .worktrees import prepare_worktree


AUTO_DISPATCH_PRIORITY = {0, 1}
AUTO_DISPATCH_BLOCKED_MARKERS = (
    "用户确认暂缓",
    "用户暂停",
    "暂缓执行",
    "ops",
    "production key",
    "生产 key",
    "生产key",
    "等待回调",
    "外部回调",
)


def _execution_forbidden(task: Dict[str, Any]) -> bool:
    return (
        str(task.get("authorization_policy") or "normal").lower()
        in {"analysis-only", "reference-only"}
        or bool(str(task.get("imported_from") or "").strip())
        or is_plan_only(task)
    )


class RunManager:
    def __init__(self, service: ControlPlane, data_dir: Path):
        self.service = service
        self.db = service.db
        # Workers run with their task worktree as cwd. Resolve the management
        # data root once so DB/prompt/evidence paths never drift into that worktree.
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.paths = ensure_data_dirs(self.data_dir)

    def dispatch(
        self,
        task_id: str,
        prompt_file: Path,
        resume: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        task = self.service.get_task(task_id)
        if _execution_forbidden(task):
            raise RuntimeError(
                "AUTHORIZATION: plan-only task cannot execute; create a separately "
                "authorized implementation task"
            )
        if task["environment"].lower() in {"pro", "prod", "production"} or task[
            "base_branch"
        ].lower() in {"main", "master", "origin/main", "origin/master"}:
            reason = "MVP 不自动执行 production/main/master，需正式发布控制面"
            if not dry_run:
                self.service.record_authorization(
                    task_id,
                    "codex.exec",
                    task["authorization_policy"],
                    "denied",
                    reason,
                )
                if task["state"] != "WAITING":
                    self.service.transition(
                        task_id,
                        "WAITING",
                        producer="authorization-gate",
                        summary=reason,
                        dedupe_key="production-denied:{}".format(task_id),
                        blocking_reason=reason,
                        force=True,
                    )
            raise RuntimeError(reason)
        unresolved = self.service.unresolved_dependencies(task_id)
        if unresolved:
            reason = "依赖未完成：" + "、".join(item["title"] for item in unresolved[:5])
            if not dry_run and task["state"] != "WAITING":
                self.service.transition(
                    task_id,
                    "WAITING",
                    producer="dispatcher",
                    summary=reason,
                    dedupe_key="deps-wait:{}:{}".format(
                        task_id, fingerprint("|".join(item["id"] for item in unresolved))
                    ),
                    blocking_reason=reason,
                    force=True,
                )
            raise RuntimeError(reason)
        active = self.db.one(
            "SELECT * FROM runs WHERE task_id=? AND status IN ('QUEUED','RUNNING') "
            "ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        if active and not dry_run:
            return active
        if not prompt_file.exists():
            raise FileNotFoundError(str(prompt_file))

        task = self._resolve_dispatch_repository(task, persist=not dry_run)

        policy = runtime_policy(task["reasoning"])
        if dry_run:
            latest = self.db.one(
                "SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            )
            session_id = latest["session_id"] if resume and latest else ""
            if resume and not session_id:
                raise RuntimeError("cannot continue: no captured Codex session id")
            return {
                "schema_version": 1,
                "dry_run": True,
                "task_id": task_id,
                "repository": task["repository"],
                "base_branch": task["base_branch"],
                "worker_type": task["worker_type"],
                "model": policy.model,
                "reasoning": policy.reasoning,
                "speed": policy.speed,
                "resume": bool(resume),
                "would_create_worktree": not bool(task.get("worktree")),
                "active_run_id": active["id"] if active else None,
            }
        self.db.execute(
            "UPDATE tasks SET model=?, reasoning=?, speed=?, updated_at=? WHERE id=?",
            (policy.model, policy.reasoning, policy.speed, utc_now(), task_id),
        )
        task = self.service.get_task(task_id)

        if task["repository"]:
            # Idempotently validate the persisted worktree boundary on every
            # real dispatch. This also repairs the old dry-run bug that could
            # persist an expected path before the directory was created.
            prepare_worktree(self.service, task_id, self.data_dir)
            task = self.service.get_task(task_id)

        latest = self.db.one(
            "SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task_id,)
        )
        attempt = int(latest["attempt"]) + 1 if latest else 1
        session_id = latest["session_id"] if resume and latest else ""
        if resume and not session_id:
            raise RuntimeError("cannot continue: no captured Codex session id")

        run_id = str(uuid.uuid4())
        private_prompt = self.paths["prompts"] / "{}.txt".format(run_id)
        private_prompt.write_bytes(prompt_file.read_bytes())
        os.chmod(private_prompt, 0o600)
        command_summary = (
            "codex exec{} --json -m {} reasoning={} speed={} -C {} [prompt via stdin]"
        ).format(
            " resume" if resume else "",
            task["model"],
            task["reasoning"],
            task["speed"],
            task["worktree"] or task["repository"] or str(self.data_dir),
        )
        try:
            with self.db.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, task_id, attempt, adapter, command_summary, session_id,
                        status, created_at, retry_of
                    ) VALUES(?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        attempt,
                        task["worker_type"],
                        redact_text(command_summary, max_chars=500),
                        session_id,
                        utc_now(),
                        latest["id"] if latest else None,
                    ),
                )
        except sqlite3.IntegrityError:
            try:
                private_prompt.unlink()
            except OSError:
                pass
            active = self.db.one(
                "SELECT * FROM runs WHERE task_id=? "
                "AND status IN ('QUEUED','RUNNING') "
                "ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            )
            if active:
                return active
            raise
        if task["state"] == "INBOX":
            self.service.transition(
                task_id,
                "PLANNED",
                producer="dispatcher",
                dedupe_key="planned:{}:{}".format(task_id, attempt),
            )
        self.service.transition(
            task_id,
            "QUEUED",
            producer="dispatcher",
            summary="后台任务已入队",
            dedupe_key="queued:{}:{}".format(task_id, attempt),
            force=task["state"] not in {"PLANNED", "FAILED", "WAITING"},
        )
        self.service.record_authorization(
            task_id,
            "codex.exec",
            task["authorization_policy"],
            "allowed",
            "用户已授权正常范围执行；危险绕过参数仍禁用",
        )
        command = [
            sys.executable,
            "-m",
            "qingtian_engine.worker_entry",
            "--db",
            str(self.db.path.expanduser().resolve()),
            "--data-dir",
            str(self.data_dir),
            "--task",
            task_id,
            "--run",
            run_id,
            "--prompt-file",
            str(private_prompt),
        ]
        if resume:
            command.append("--resume")
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = source_root + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        bootstrap_log_path = self.paths["run"] / "worker-bootstrap-{}.log".format(run_id)
        bootstrap_log = bootstrap_log_path.open("ab")
        os.chmod(bootstrap_log_path, 0o600)
        try:
            process = subprocess.Popen(
                command,
                cwd=task["worktree"] or task["repository"] or str(self.data_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=bootstrap_log,
                start_new_session=True,
                env=env,
            )
        except Exception:
            try:
                private_prompt.unlink()
            except OSError:
                pass
            self.db.execute(
                """
                UPDATE runs SET status='FAILED', exit_code=70, finished_at=?,
                    failure_kind='INFRASTRUCTURE', failure_stage='process_spawn',
                    failure_type='SpawnError',
                    failure_trace_hash=?
                WHERE id=?
                """,
                (utc_now(), fingerprint("process_spawn"), run_id),
            )
            self.service.transition(
                task_id,
                "WAITING",
                producer="dispatcher",
                summary="后台执行器启动失败，进入安全恢复",
                dedupe_key="spawn-failed:{}".format(run_id),
                blocking_reason="后台执行器启动失败；等待安全重试",
                force=True,
            )
            raise
        finally:
            bootstrap_log.close()
        self.db.execute(
            "UPDATE runs SET pid=?, process_group=? WHERE id=?",
            (process.pid, process.pid, run_id),
        )
        self.db.add_event(
            task_id,
            "run.spawned",
            "dispatcher",
            "后台执行器已启动",
            "run-spawned:{}".format(run_id),
            {"pid": process.pid, "attempt": attempt, "state": "QUEUED"},
        )
        poll = getattr(process, "poll", None)
        if callable(poll):
            for _ in range(20):
                current = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
                if current and (
                    current["started_at"]
                    or current["status"] in {"RUNNING", "DONE", "FAILED"}
                ):
                    return current
                exit_code = poll()
                if exit_code is not None:
                    trace_hash = fingerprint(
                        "worker-bootstrap:{}:{}".format(run_id, exit_code)
                    )
                    self.db.execute(
                        """
                        UPDATE runs SET status='FAILED', exit_code=70, finished_at=?,
                            failure_kind='INFRASTRUCTURE',
                            failure_stage='worker_bootstrap',
                            failure_type='WorkerBootstrapExit',
                            failure_trace_hash=?
                        WHERE id=? AND status='QUEUED'
                        """,
                        (utc_now(), trace_hash, run_id),
                    )
                    self.service.transition(
                        task_id,
                        "WAITING",
                        producer="dispatcher",
                        summary="后台执行器启动后立即退出，进入安全恢复",
                        dedupe_key="bootstrap-exit:{}".format(run_id),
                        blocking_reason="后台执行器启动后立即退出；等待安全重试",
                        force=True,
                    )
                    raise RuntimeError(
                        "worker bootstrap failed; see {}".format(bootstrap_log_path)
                    )
                time.sleep(0.05)
        return self.db.one("SELECT * FROM runs WHERE id=?", (run_id,)) or {}

    @staticmethod
    def _is_git_repository(path: Path) -> bool:
        return path.is_dir() and (path / ".git").exists()

    def _block_repository_configuration(
        self,
        task: Dict[str, Any],
        detail: str,
        owner: str,
        *,
        persist: bool = True,
    ) -> None:
        reason = "CONFIGURATION: {}; 请先配置 repository/base_branch".format(detail)
        if persist:
            self.service.transition(
                task["id"],
                "WAITING",
                producer="dispatcher",
                summary="派发前仓库配置校验失败",
                dedupe_key="dispatch-repository-missing:{}:{}".format(
                    task["id"], fingerprint("{}|{}".format(owner or "unknown", detail))
                ),
                blocking_reason=reason,
                force=True,
            )
        raise RuntimeError(reason)

    def _resolve_dispatch_repository(
        self, task: Dict[str, Any], *, persist: bool = True
    ) -> Dict[str, Any]:
        """Resolve only a named or exact-path entry in the local registry."""
        owner = str(task.get("owner_session") or "").strip().lower()
        owner_roles = {owner} if owner else set()
        owner_roles.update(
            route.owner_session
            for route in matching_routes(
                str(task.get("title") or ""),
                str(task.get("scope_summary") or ""),
                "",
            )
        )
        try:
            project_config = load_project_config(data_dir=self.data_dir)
        except ProjectConfigError:
            self._block_repository_configuration(
                task,
                "本地项目注册表无效",
                owner,
                persist=persist,
            )

        if task["repository"]:
            configured_target = project_config.resolve_reference(
                str(task["repository"])
            )
            if configured_target is None:
                self._block_repository_configuration(
                    task,
                    "repository 必须是已注册项目名或其精确路径",
                    owner,
                    persist=persist,
                )
            if task["base_branch"] and task["base_branch"] != configured_target.base_branch:
                self._block_repository_configuration(
                    task,
                    "任务 base_branch 与已注册项目不一致",
                    owner,
                    persist=persist,
                )
            if not persist:
                resolved = dict(task)
                resolved["repository"] = str(configured_target.repository)
                resolved["base_branch"] = configured_target.base_branch
                return resolved
            self.db.execute(
                """
                UPDATE tasks SET repository=?, base_branch=?, updated_at=?
                WHERE id=?
                """,
                (
                    str(configured_target.repository),
                    configured_target.base_branch,
                    utc_now(),
                    task["id"],
                ),
            )
            return self.service.get_task(task["id"])

        configured_targets = project_config.targets_for_roles(owner_roles)
        unique_targets = {}
        for target in configured_targets:
            unique_targets[(target.repository, target.base_branch)] = target
        if len(unique_targets) == 1:
            selected = next(iter(unique_targets.values()))
            if not persist:
                resolved = dict(task)
                resolved["repository"] = str(selected.repository)
                resolved["base_branch"] = selected.base_branch
                return resolved
            self.db.execute(
                """
                UPDATE tasks SET repository=?, base_branch=?, updated_at=?
                WHERE id=? AND repository=''
                """,
                (
                    str(selected.repository),
                    selected.base_branch,
                    utc_now(),
                    task["id"],
                ),
            )
            self.db.add_event(
                task["id"],
                "task.repository_inferred",
                "dispatcher",
                "已按 owner role 选择已注册项目",
                "repository-inferred:{}:{}".format(task["id"], selected.name),
                {
                    "owner_session": owner,
                    "project_name": selected.name,
                    "state": task["state"],
                },
            )
            return self.service.get_task(task["id"])
        if len(unique_targets) > 1:
            detail = "owner role 同时匹配多个已注册项目，请显式选择 project"
        else:
            detail = "没有为 owner role {} 注册项目；请先运行 project register".format(
                "、".join(sorted(owner_roles)) or "unknown"
            )
        self._block_repository_configuration(
            task,
            "repository 为空且{}".format(detail),
            owner,
            persist=persist,
        )

    def recover_infrastructure_failure(
        self, task_id: str, prompt_path: Path, resume: bool = True
    ) -> Dict[str, Any]:
        if _execution_forbidden(self.service.get_task(task_id)):
            raise RuntimeError("AUTHORIZATION: plan-only task cannot be recovered")
        active = self.db.one(
            "SELECT * FROM runs WHERE task_id=? AND status IN ('QUEUED','RUNNING') "
            "ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        if active:
            return active
        lock_path = self.paths["run"] / "recovery-{}.lock".format(
            fingerprint(task_id)[:16]
        )
        lock_handle = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(
                    lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except BlockingIOError:
                active = self.db.one(
                    "SELECT * FROM runs WHERE task_id=? "
                    "AND status IN ('QUEUED','RUNNING') "
                    "ORDER BY attempt DESC LIMIT 1",
                    (task_id,),
                )
                if active:
                    return active
                raise RuntimeError("task recovery is already locked")
            return self._recover_infrastructure_failure_locked(
                task_id, prompt_path, resume=resume
            )
        finally:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()

    def _recover_infrastructure_failure_locked(
        self, task_id: str, prompt_path: Path, resume: bool = True
    ) -> Dict[str, Any]:
        latest = self.db.one(
            "SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        if not latest or latest["status"] != "FAILED":
            raise RuntimeError("latest run is not failed")
        if latest["failure_kind"] != "INFRASTRUCTURE" and int(
            latest["exit_code"] or 0
        ) != 70:
            raise RuntimeError("latest run is not an infrastructure failure")
        failures = self.db.one(
            """
            SELECT COUNT(*) AS count FROM runs
            WHERE task_id=? AND failure_kind='INFRASTRUCTURE'
            """,
            (task_id,),
        )
        failure_count = int(failures["count"]) if failures else 0
        if failure_count >= 4:
            raise RuntimeError(
                "infrastructure recovery limit reached at stage {}".format(
                    latest.get("failure_stage") or "unknown"
                )
            )
        if failure_count >= 2 and latest.get("finished_at"):
            try:
                finished_at = datetime.fromisoformat(latest["finished_at"])
                if finished_at.tzinfo is None:
                    finished_at = finished_at.replace(tzinfo=timezone.utc)
                backoff = min(60, 2 ** min(failure_count - 2, 6))
                elapsed = (datetime.now(timezone.utc) - finished_at).total_seconds()
                if elapsed < backoff:
                    raise RuntimeError(
                        "infrastructure recovery backoff: retry in {}s".format(
                            max(1, int(backoff - elapsed))
                        )
                    )
            except ValueError:
                pass
        legacy_event = self.db.one(
            """
            SELECT summary FROM events
            WHERE task_id=? AND producer='worker-guard'
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        )
        inferred_type = "WorkerError"
        if legacy_event and "：" in legacy_event["summary"]:
            candidate = legacy_event["summary"].rsplit("：", 1)[-1].strip()
            if candidate and candidate.replace("_", "").isalnum():
                inferred_type = candidate[:80]
        inferred_trace = fingerprint(
            "legacy:{}:{}:event-payload-shape".format(
                latest["id"], inferred_type
            )
        )
        self.db.execute(
            """
            UPDATE runs SET failure_kind='INFRASTRUCTURE',
                failure_stage=CASE WHEN failure_stage='' THEN 'worker_entry' ELSE failure_stage END,
                failure_type=CASE WHEN failure_type='' THEN ? ELSE failure_type END,
                failure_trace_hash=CASE WHEN failure_trace_hash='' THEN ? ELSE failure_trace_hash END
            WHERE id=?
            """,
            (inferred_type, inferred_trace, latest["id"]),
        )
        self.db.add_event(
            task_id,
            "run.recovered",
            "worker-guard",
            "执行器已恢复并重试",
            "worker-recovered:{}".format(latest["id"]),
            {
                "failed_run_id": latest["id"],
                "failure_kind": "INFRASTRUCTURE",
                "state": "QUEUED",
            },
        )
        return self.dispatch(task_id, prompt_path, resume=resume)

    def dispatch_verification_backfill(
        self,
        task_id: str,
        max_attempts: int = MAX_VERIFICATION_BACKFILL_ATTEMPTS,
        cooldown_seconds: int = 60,
    ) -> Dict[str, Any]:
        task = self.service.get_task(task_id)
        if _execution_forbidden(task):
            raise RuntimeError("AUTHORIZATION: plan-only task cannot run verification")
        if task["state"] not in {"VERIFYING", "FAILED"}:
            raise RuntimeError("task is not waiting for verification")
        missing = self.service.missing_completion_evidence(task_id)
        if not missing:
            raise RuntimeError("task has no verification debt")
        active = self.db.one(
            "SELECT * FROM runs WHERE task_id=? AND status IN ('QUEUED','RUNNING') "
            "ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        if active:
            return active
        history = self.db.all(
            """
            SELECT payload_json, occurred_at FROM events
            WHERE task_id=? AND event_type='verification.backfill_queued'
            ORDER BY id ASC
            """,
            (task_id,),
        )
        if len(history) >= max(1, int(max_attempts)):
            raise RuntimeError("verification backfill retry limit reached")
        if history and cooldown_seconds > 0:
            try:
                last_at = datetime.fromisoformat(history[-1]["occurred_at"])
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
                backoff = min(
                    15 * 60,
                    max(1, int(cooldown_seconds)) * (2 ** (len(history) - 1)),
                )
                if elapsed < backoff:
                    raise RuntimeError(
                        "verification backfill cooldown: retry in {}s".format(
                            max(1, int(backoff - elapsed))
                        )
                    )
            except ValueError:
                pass
        latest = self.db.one(
            "SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        if not latest or latest["status"] not in {"DONE", "FAILED"}:
            raise RuntimeError("latest run is not complete or recoverable")
        if latest["status"] == "FAILED":
            latest_backfill_attempt = 0
            if history:
                try:
                    latest_backfill_attempt = int(
                        json.loads(history[-1]["payload_json"] or "{}").get(
                            "attempt", 0
                        )
                    )
                except (TypeError, ValueError):
                    latest_backfill_attempt = 0
            if latest_backfill_attempt != int(latest["attempt"]):
                raise RuntimeError("latest failure was not a verification backfill")
        if task["state"] != "VERIFYING":
            self.service.transition(
                task_id,
                "VERIFYING",
                producer="watchdog",
                summary="QA 回填失败后进入有界恢复重试",
                dedupe_key="verification-backfill-recover:{}:{}".format(
                    task_id, len(history) + 1
                ),
                force=True,
            )
        prompt = self.paths["prompts"] / "backfill-{}-{}.txt".format(
            task_id, uuid.uuid4().hex[:8]
        )
        prompt.write_text(
            "这是验证证据回填，不是重新开发。检查既有 worktree/commit，只运行与改动直接相关的测试"
            "和必要最小冒烟；不得伪造证据。缺少的证据类型：{}。"
            "若无法取得真实证据，明确说明阻塞，不得把任务标为完成。".format(
                "、".join(missing)
            ),
            encoding="utf-8",
        )
        os.chmod(prompt, 0o600)
        next_run_attempt = int(latest["attempt"]) + 1
        backfill_sequence = len(history) + 1
        self.db.add_event(
            task_id,
            "verification.backfill_queued",
            "watchdog",
            "已自动派发 QA 证据回填（{}/{}）".format(
                backfill_sequence, max_attempts
            ),
            "verification-backfill:{}:{}".format(task_id, backfill_sequence),
            {
                "attempt": next_run_attempt,
                "backfill_sequence": backfill_sequence,
                "max_attempts": int(max_attempts),
                "state": "QUEUED",
            },
        )
        try:
            return self.dispatch(
                task_id, prompt, resume=bool(latest["session_id"])
            )
        finally:
            try:
                prompt.unlink()
            except OSError:
                pass

    def reconcile_verification_debt(
        self,
        max_new: int = 1,
        max_active: int = 3,
        cooldown_seconds: int = 60,
    ) -> Dict[str, Any]:
        active = self.db.one(
            "SELECT COUNT(*) AS count FROM runs WHERE status IN ('QUEUED','RUNNING')"
        )
        active_count = int(active["count"]) if active else 0
        if active_count >= max_active:
            return {"queued": [], "active": active_count, "capacity": 0}
        capacity = min(max(0, int(max_new)), max_active - active_count)
        queued = []
        skipped = []
        if capacity:
            candidates = [
                task
                for task in self.service.dashboard_payload()["tasks"]
                if task["state"] in {"VERIFYING", "FAILED"}
                and task["runtime_status"]["code"]
                in {
                    "BACKFILL_REQUIRED",
                    "BACKFILL_INCOMPLETE",
                    "INDEPENDENT_QA",
                }
            ]
            candidates.sort(
                key=lambda task: (
                    int(task["runtime_status"].get("backfill_attempts") or 0),
                    int(task.get("priority") or 0),
                    str(task.get("updated_at") or ""),
                    task["id"],
                )
            )
            for task in candidates:
                if len(queued) >= capacity:
                    break
                try:
                    run = self.dispatch_verification_backfill(
                        task["id"], cooldown_seconds=cooldown_seconds
                    )
                    queued.append({"task_id": task["id"], "run_id": run["id"]})
                except (RuntimeError, ValueError, FileNotFoundError) as exc:
                    skipped.append(
                        {"task_id": task["id"], "reason": str(exc)[:160]}
                    )
                    continue
        return {
            "queued": queued,
            "skipped": skipped,
            "active": active_count + len(queued),
            "capacity": max(0, capacity - len(queued)),
        }

    @staticmethod
    def _eligible_for_managed_dispatch(task: Dict[str, Any]) -> bool:
        if str(task.get("state") or "").upper() not in {"INBOX", "PLANNED"}:
            return False
        if int(task.get("priority") or 0) not in AUTO_DISPATCH_PRIORITY:
            return False
        if str(task.get("execution_mode") or "managed").lower() != "managed":
            return False
        if _execution_forbidden(task):
            return False
        if str(task.get("action_owner_kind") or "none").lower() in {
            "user",
            "external",
        }:
            return False
        if bool(task.get("action_sensitive")):
            return False
        if str(task.get("environment") or "").lower() in {
            "pro",
            "prod",
            "production",
        }:
            return False
        if str(task.get("base_branch") or "").lower() in {
            "main",
            "master",
            "origin/main",
            "origin/master",
        }:
            return False
        searchable = " ".join(
            str(task.get(key) or "")
            for key in (
                "title",
                "scope_summary",
                "blocking_reason",
                "action_text",
            )
        ).lower()
        return not any(marker in searchable for marker in AUTO_DISPATCH_BLOCKED_MARKERS)

    def _autodispatch_prompt(self, task: Dict[str, Any]) -> Path:
        prompt = self.paths["prompts"] / "autodispatch-{}-{}.txt".format(
            task["id"], uuid.uuid4().hex[:8]
        )
        prompt.write_text(
            "擎天已自动领取此任务。目标：{title}。\n"
            "范围：{scope}\n"
            "严格保持任务现有模型与推理配置：gpt-5.6-sol，reasoning high 或更高；"
            "夜间使用 standard。只处理本任务范围，不修改无关仓库。\n"
            "dev 只运行与改动直接相关的测试和最小部署后冒烟，不运行全量门禁。"
            "不得伪造 commit/test/deploy/smoke 证据；遇到真实外部依赖、用户授权、"
            "生产 Key、回调或高风险动作，立即转为可审计等待。".format(
                title=task["title"],
                scope=task.get("scope_summary") or task.get("short_summary") or "按任务标题执行",
            ),
            encoding="utf-8",
        )
        os.chmod(prompt, 0o600)
        return prompt

    def _mark_exhausted_verification(self) -> Dict[str, Any]:
        moved = []
        for task in self.service.dashboard_payload()["tasks"]:
            if task["state"] != "VERIFYING":
                continue
            runtime = task.get("runtime_status") or {}
            attempts = int(runtime.get("backfill_attempts") or 0)
            limit = int(
                runtime.get("backfill_limit")
                or MAX_VERIFICATION_BACKFILL_ATTEMPTS
            )
            if attempts < limit:
                continue
            missing = self.service.missing_completion_evidence(task["id"])
            reason = "EVIDENCE_COLLECTION_REQUIRED: {}".format(
                "、".join(missing) if missing else "人工复核"
            )
            self.service.set_human_action(
                task["id"],
                "agent",
                owner="qa",
                text="内部证据采集：补齐真实 {}；不再自动重跑开发".format(
                    "、".join(missing) if missing else "验收结论"
                ),
                producer="scheduler",
            )
            self.service.transition(
                task["id"],
                "WAITING",
                producer="scheduler",
                summary="自动 QA 已达上限，转入专属证据采集",
                dedupe_key="verification-exhausted:{}".format(task["id"]),
                blocking_reason=reason,
                force=True,
            )
            self.db.add_event(
                task["id"],
                "verification.evidence_collection_required",
                "scheduler",
                "证据回填已达上限，停止无限重试",
                "verification-evidence-collection:{}".format(task["id"]),
                {"missing": missing, "state": "WAITING"},
            )
            moved.append(task["id"])
        return {"moved": moved}

    def _recover_safe_infrastructure_waiters(
        self, max_new: int = 1, max_active: int = 3
    ) -> Dict[str, Any]:
        recovered = []
        skipped = []
        active_row = self.db.one(
            "SELECT COUNT(*) AS count FROM runs "
            "WHERE status IN ('QUEUED','RUNNING')"
        )
        active_count = int(active_row["count"]) if active_row else 0
        capacity = min(
            max(0, int(max_new)),
            max(0, int(max_active) - active_count),
        )
        candidates = self.service.list_tasks(states=["WAITING"], limit=500)
        candidates.sort(
            key=lambda task: (
                int(task.get("priority") or 0),
                str(task.get("created_at") or ""),
                task["id"],
            )
        )
        for task in candidates:
            if len(recovered) >= capacity:
                break
            if str(task.get("execution_mode") or "managed").lower() != "managed":
                continue
            if _execution_forbidden(task):
                continue
            if str(task.get("action_owner_kind") or "none").lower() in {
                "user",
                "external",
            }:
                continue
            if bool(task.get("action_sensitive")):
                continue
            searchable = " ".join(
                str(task.get(key) or "")
                for key in ("title", "blocking_reason", "action_text")
            ).lower()
            if any(marker in searchable for marker in AUTO_DISPATCH_BLOCKED_MARKERS):
                continue
            latest = self.db.one(
                "SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                (task["id"],),
            )
            if not latest or latest["status"] != "FAILED":
                continue
            if latest.get("failure_kind") != "INFRASTRUCTURE" and int(
                latest.get("exit_code") or 0
            ) != 70:
                continue
            prompt = self._autodispatch_prompt(task)
            try:
                run = self.recover_infrastructure_failure(
                    task["id"],
                    prompt,
                    resume=bool(latest.get("session_id")),
                )
                recovered.append({"task_id": task["id"], "run_id": run["id"]})
            except (RuntimeError, ValueError, FileNotFoundError) as exc:
                skipped.append({"task_id": task["id"], "reason": str(exc)[:160]})
            finally:
                try:
                    prompt.unlink()
                except OSError:
                    pass
        return {
            "recovered": recovered,
            "skipped": skipped,
            "active": active_count + len(recovered),
        }

    def reconcile_dispatch_queue(
        self, max_new: int = 1, max_active: int = 3
    ) -> Dict[str, Any]:
        """Claim runnable P0/P1 tasks FIFO under one scheduler lease."""
        lock_path = self.paths["run"] / "scheduler-dispatch.lock"
        lock_handle = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(
                    lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except BlockingIOError:
                return {
                    "claimed": [],
                    "skipped": [],
                    "active": 0,
                    "lease": "busy",
                }
            active_row = self.db.one(
                "SELECT COUNT(*) AS count FROM runs "
                "WHERE status IN ('QUEUED','RUNNING')"
            )
            active_count = int(active_row["count"]) if active_row else 0
            capacity = min(
                max(0, int(max_new)),
                max(0, int(max_active) - active_count),
            )
            claimed = []
            skipped = []
            candidates = [
                task
                for task in self.service.list_tasks(
                    states=["INBOX", "PLANNED"], limit=500
                )
                if self._eligible_for_managed_dispatch(task)
            ]
            candidates.sort(
                key=lambda task: (
                    int(task.get("priority") or 0),
                    str(task.get("created_at") or ""),
                    task["id"],
                )
            )
            for task in candidates:
                if len(claimed) >= capacity:
                    break
                if self.service.unresolved_dependencies(task["id"]):
                    continue
                prompt = self._autodispatch_prompt(task)
                try:
                    run = self.dispatch(task["id"], prompt)
                    claimed.append({"task_id": task["id"], "run_id": run["id"]})
                except (RuntimeError, ValueError, FileNotFoundError) as exc:
                    skipped.append(
                        {"task_id": task["id"], "reason": str(exc)[:160]}
                    )
                finally:
                    try:
                        prompt.unlink()
                    except OSError:
                        pass
            return {
                "claimed": claimed,
                "skipped": skipped,
                "active": active_count + len(claimed),
                "lease": "acquired",
            }
        finally:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()

    def scheduler_tick(
        self, max_new: int = 1, max_active: int = 3
    ) -> Dict[str, Any]:
        reconcile = self.reconcile()
        exhausted = self._mark_exhausted_verification()
        infrastructure = self._recover_safe_infrastructure_waiters(
            max_new=max_new, max_active=max_active
        )
        verification = self.reconcile_verification_debt(
            max_new=max_new, max_active=max_active
        )
        dispatch = self.reconcile_dispatch_queue(
            max_new=max_new, max_active=max_active
        )
        return {
            "reconcile": reconcile,
            "exhausted": exhausted,
            "infrastructure": infrastructure,
            "verification": verification,
            "dispatch": dispatch,
        }

    def cancel(self, task_id: str) -> Dict[str, Any]:
        run = self.db.one(
            "SELECT * FROM runs WHERE task_id=? AND status IN ('QUEUED','RUNNING') "
            "ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        if run and run["process_group"]:
            try:
                os.killpg(int(run["process_group"]), signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.db.execute(
                "UPDATE runs SET status='CANCELED', finished_at=? WHERE id=?",
                (utc_now(), run["id"]),
            )
        self.service.transition(
            task_id,
            "CANCELED",
            producer="dispatcher",
            summary="用户取消后台任务",
            dedupe_key="cancel:{}:{}".format(task_id, run["id"] if run else "none"),
            force=True,
        )
        return self.service.get_task(task_id)

    def reconcile(self) -> Dict[str, int]:
        """Mark stale wrappers lost after crashes without replaying an external action."""
        queued_without_pid = self.db.all(
            "SELECT * FROM runs WHERE status='QUEUED' AND pid IS NULL"
        )
        running = self.db.all(
            "SELECT * FROM runs WHERE status IN ('QUEUED','RUNNING') AND pid IS NOT NULL"
        )
        stale = 0
        alive = 0
        now = datetime.now(timezone.utc)
        for run in queued_without_pid:
            try:
                created_at = datetime.fromisoformat(str(run["created_at"]))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                created_at = now
            if (now - created_at).total_seconds() < 120:
                alive += 1
                continue
            stale += 1
            self.db.execute(
                """
                UPDATE runs SET status='FAILED', exit_code=70, finished_at=?,
                    failure_kind='INFRASTRUCTURE', failure_stage='queue_watchdog',
                    failure_type='MissingWorkerPid', failure_trace_hash=?
                WHERE id=?
                """,
                (utc_now(), fingerprint("queue-watchdog"), run["id"]),
            )
            self.service.transition(
                run["task_id"],
                "WAITING",
                producer="reconciler",
                summary="排队任务未启动执行器，等待安全重试",
                dedupe_key="stale-queued-run:{}".format(run["id"]),
                blocking_reason="后台执行器未成功启动；未自动重放外部动作",
                force=True,
            )
        for run in running:
            try:
                os.kill(int(run["pid"]), 0)
                alive += 1
            except ProcessLookupError:
                stale += 1
                self.db.execute(
                    """
                    UPDATE runs SET status='FAILED', exit_code=70, finished_at=?,
                        failure_kind='INFRASTRUCTURE', failure_stage='process_watchdog',
                        failure_type='ProcessLookupError', failure_trace_hash=?
                    WHERE id=?
                    """,
                    (utc_now(), fingerprint("process-watchdog"), run["id"]),
                )
                self.service.transition(
                    run["task_id"],
                    "WAITING",
                    producer="reconciler",
                    summary="执行器进程已丢失，等待安全重试",
                    dedupe_key="stale-run:{}".format(run["id"]),
                    blocking_reason="后台执行器中断；未自动重放外部动作",
                    force=True,
                )
        state_result = self.service.reconcile_derived_states()
        profile_result = self.service.reconcile_evidence_profiles()
        progression_result = self.service.reconcile_state_progression()
        return {
            "alive": alive,
            "stale": stale,
            "state_synchronized": state_result["synchronized"],
            "live_run_restored": state_result["live_run_restored"],
            "run_completed": state_result["run_completed"],
            "run_waiting": state_result["run_waiting"],
            "external_stale": state_result["external_stale"],
            "delegated_stale": state_result["delegated_stale"],
            "evidence_profiles_corrected": (
                profile_result["code"] + profile_result["legacy"]
            ),
            "completed": progression_result["completed"],
            "dependencies_ready": progression_result["dependencies_ready"],
        }
