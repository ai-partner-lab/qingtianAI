from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from qingtian_engine.db import Database
from qingtian_engine.project_config import register_project
from qingtian_engine.service import ControlPlane
from qingtian_engine.runner import RunManager
from qingtian_engine.worker_entry import (
    _event_metadata,
    build_codex_command,
    run_worker,
    task_evidence_path,
)
from qingtian_engine.worktrees import prepare_worktree


class WorktreeAndWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = ControlPlane(Database(self.root / "control.sqlite3"))
        self.repo_temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.repo_temp.name)
        subprocess.run(
            ["git", "init", "-b", "dev", str(self.repo)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"],
            check=True,
        )
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
        roles = (
            "coordinator",
            "backend",
            "frontend",
            "mobile",
            "qa",
            "browser-qa",
            "security",
            "infrastructure",
            "documentation",
        )
        for data_dir in (
            self.root,
            self.root / "relative-data",
            self.root / "day",
            self.root / "night",
        ):
            register_project(
                "sample", self.repo, "dev", roles, data_dir=data_dir
            )
        # Worker protocol fixtures must not read the developer's real KB or
        # let its provider subprocess consume the fake Codex process. The
        # knowledge handoff itself is covered by test_worker_knowledge.
        knowledge = patch("qingtian_engine.worker_entry.configured_task_context", return_value=None)
        knowledge.start()
        self.addCleanup(knowledge.stop)
        runner_worktree = patch(
            "qingtian_engine.runner.prepare_worktree",
            side_effect=self._prepare_runner_worktree,
        )
        runner_worktree.start()
        self.addCleanup(runner_worktree.stop)

    def tearDown(self) -> None:
        self.repo_temp.cleanup()
        self.temp.cleanup()

    def _attach_fixture_worktree(self, task) -> Path:
        worktree = self.root / "worktrees" / task["id"]
        worktree.mkdir(parents=True)
        self.service.db.execute(
            "UPDATE tasks SET worktree=? WHERE id=?",
            (str(worktree), task["id"]),
        )
        return worktree

    @staticmethod
    def _prepare_runner_worktree(service, task_id, data_dir, dry_run=False):
        task = service.get_task(task_id)
        worktree = Path(data_dir).resolve() / "worktrees" / task_id
        worktree.mkdir(parents=True, exist_ok=True)
        branch = task["branch"] or "qingtian/{}-task".format(task_id)
        service.db.execute(
            "UPDATE tasks SET worktree=?, branch=? WHERE id=?",
            (str(worktree), branch, task_id),
        )
        return {
            "worktree": str(worktree),
            "branch": branch,
            "base": task["base_branch"],
        }

    def test_independent_worktree_is_created(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "dev", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True,
        )
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
        task = self.service.create_task(
            "隔离开发",
            idempotency_key="worktree",
            repository=str(repo),
            base_branch="dev",
        )
        before = self.service.get_task(task["id"])
        before_events = self.service.db.one(
            "SELECT COUNT(*) AS count FROM events WHERE task_id=?", (task["id"],)
        )["count"]
        planned = prepare_worktree(self.service, task["id"], self.root, dry_run=True)
        self.assertFalse(Path(planned["worktree"]).exists())
        self.assertEqual(before, self.service.get_task(task["id"]))
        self.assertEqual(
            before_events,
            self.service.db.one(
                "SELECT COUNT(*) AS count FROM events WHERE task_id=?", (task["id"],)
            )["count"],
        )
        result = prepare_worktree(self.service, task["id"], self.root)
        self.assertTrue(Path(result["worktree"]).exists())
        self.assertTrue(result["branch"].startswith("qingtian/"))
        self.assertEqual("dev", result["base"])

    def test_prepare_worktree_repairs_only_the_expected_phantom_path(self) -> None:
        task = self.service.create_task(
            "Recover old dry-run state",
            idempotency_key="phantom-worktree",
            repository=str(self.repo),
            base_branch="dev",
        )
        expected = self.root / "worktrees" / task["id"]
        self.service.db.execute(
            "UPDATE tasks SET worktree=?, branch=? WHERE id=?",
            (str(expected), "qingtian/{}-recovered".format(task["id"]), task["id"]),
        )
        result = prepare_worktree(self.service, task["id"], self.root)
        self.assertEqual(expected.resolve(), Path(result["worktree"]).resolve())
        self.assertTrue(expected.is_dir())

        outside = self.root / "outside-worktree"
        second = self.service.create_task(
            "Reject external worktree",
            idempotency_key="external-worktree",
            repository=str(self.repo),
            base_branch="dev",
        )
        self.service.db.execute(
            "UPDATE tasks SET worktree=? WHERE id=?",
            (str(outside), second["id"]),
        )
        with self.assertRaisesRegex(RuntimeError, "outside the engine boundary"):
            prepare_worktree(self.service, second["id"], self.root)

    def test_json_event_only_keeps_safe_metadata(self) -> None:
        event = {
            "type": "thread.started",
            "thread_id": "019f-example",
            "prompt": "password=should-never-persist",
            "item": {"type": "agent_message", "text": "secret final output"},
        }
        normalized = _event_metadata(event)
        self.assertEqual("thread.started", normalized["type"])
        self.assertEqual("019f-example", normalized["metadata"]["thread_id"])
        self.assertNotIn("prompt", normalized["metadata"])
        self.assertNotIn("text", normalized["metadata"])
        self.assertIn("sha256", normalized["metadata"])

    def test_non_object_json_event_is_ignored(self) -> None:
        self.assertIsNone(_event_metadata([]))
        self.assertIsNone(_event_metadata(None))
        self.assertIsNone(_event_metadata("text"))

    def test_bad_json_event_does_not_kill_worker(self) -> None:
        task = self.service.create_task(
            "事件隔离回归",
            idempotency_key="event-isolation",
            worker_type="cli",
            repository=str(self.repo),
            base_branch="dev",
        )
        self._attach_fixture_worktree(task)
        run_id = "run-event-isolation"
        self.service.db.execute(
            """
            INSERT INTO runs(id, task_id, attempt, adapter, command_summary, status, created_at)
            VALUES(?, ?, 1, 'cli', 'test', 'QUEUED', datetime('now'))
            """,
            (run_id, task["id"]),
        )
        prompt = self.root / "prompt.txt"
        prompt.write_text("test", encoding="utf-8")

        class Sink:
            def write(self, _value):
                return None

            def close(self):
                return None

        class FakeProcess:
            stdin = Sink()
            stdout = iter(
                [
                    "[]\n",
                    "{not-json}\n",
                    json.dumps(
                        {"type": "thread.started", "thread_id": "session-safe"}
                    )
                    + "\n",
                ]
            )

            def wait(self):
                return 0

        args = argparse.Namespace(
            db=str(self.service.db.path),
            data_dir=str(self.root),
            task=task["id"],
            run=run_id,
            prompt_file=str(prompt),
            resume=False,
        )
        with patch("qingtian_engine.worker_entry.subprocess.Popen", return_value=FakeProcess()):
            self.assertEqual(0, run_worker(args))

        stored = self.service.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        self.assertEqual("DONE", stored["status"])
        self.assertEqual("session-safe", stored["session_id"])
        self.assertEqual(2, stored["debug_line_count"])
        skipped = self.service.db.all(
            "SELECT payload_json FROM events WHERE task_id=? AND event_type='codex.event_skipped'",
            (task["id"],),
        )
        self.assertEqual([], skipped)

    def test_failed_worker_ends_in_waiting_recovery_state(self) -> None:
        task = self.service.create_task(
            "失败执行进入恢复",
            idempotency_key="worker-failure-waiting",
            worker_type="cli",
            repository=str(self.repo),
            base_branch="dev",
        )
        self._attach_fixture_worktree(task)
        run_id = "run-failure-waiting"
        self.service.db.execute(
            """
            INSERT INTO runs(id, task_id, attempt, adapter, command_summary, status, created_at)
            VALUES(?, ?, 1, 'cli', 'test', 'QUEUED', datetime('now'))
            """,
            (run_id, task["id"]),
        )
        prompt = self.root / "failure-prompt.txt"
        prompt.write_text("test", encoding="utf-8")

        class Sink:
            def write(self, _value):
                return None

            def close(self):
                return None

        class FailedProcess:
            stdin = Sink()
            stdout = iter([])

            def wait(self):
                return 2

        args = argparse.Namespace(
            db=str(self.service.db.path),
            data_dir=str(self.root),
            task=task["id"],
            run=run_id,
            prompt_file=str(prompt),
            resume=False,
        )
        with patch(
            "qingtian_engine.worker_entry.subprocess.Popen", return_value=FailedProcess()
        ):
            self.assertEqual(2, run_worker(args))

        stored_run = self.service.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        stored_task = self.service.get_task(task["id"])
        self.assertEqual("FAILED", stored_run["status"])
        self.assertEqual("WAITING", stored_task["state"])
        self.assertIn("安全重试", stored_task["blocking_reason"])

    def test_worker_evidence_spool_is_inside_writable_task_workspace(self) -> None:
        task = self.service.create_task(
            "证据路径回归",
            idempotency_key="evidence-workspace",
            repository=str(self.repo),
            base_branch="dev",
            worker_type="cli",
            evidence_profile="code",
        )
        workspace = self._attach_fixture_worktree(task)
        task = self.service.get_task(task["id"])
        run_id = "run-evidence-workspace"
        self.service.db.execute(
            """
            INSERT INTO runs(id, task_id, attempt, adapter, command_summary, status, created_at)
            VALUES(?, ?, 1, 'cli', 'test', 'QUEUED', datetime('now'))
            """,
            (run_id, task["id"]),
        )
        self.service.db.add_event(
            task["id"],
            "verification.backfill_queued",
            "test",
            "验证证据回填",
            "evidence-workspace-backfill",
            {"attempt": 1, "state": "QUEUED"},
        )
        prompt = self.root / "evidence-prompt.txt"
        prompt.write_text("test", encoding="utf-8")
        evidence_path = task_evidence_path(task, run_id)

        class Sink:
            def write(self, _value):
                return None

            def close(self):
                return None

        class FakeProcess:
            stdin = Sink()
            stdout = iter([])

            def wait(self):
                evidence_path.write_text(
                    json.dumps({"commit": "abc123", "test": "1 passed"}),
                    encoding="utf-8",
                )
                return 0

        args = argparse.Namespace(
            db=str(self.service.db.path),
            data_dir=str(self.root),
            task=task["id"],
            run=run_id,
            prompt_file=str(prompt),
            resume=False,
        )
        with patch("qingtian_engine.worker_entry.subprocess.Popen", return_value=FakeProcess()):
            self.assertEqual(0, run_worker(args))

        self.assertEqual(workspace.resolve(), evidence_path.parent)
        self.assertFalse(evidence_path.exists())
        completed = self.service.get_task(task["id"])
        self.assertEqual("DONE", completed["state"])
        self.assertEqual(
            {"commit", "test"},
            {item["kind"] for item in completed["evidence"] if item["verified"]},
        )

    def test_resume_command_uses_captured_session(self) -> None:
        task = {
            "reasoning": "xhigh",
            "model": "gpt-5.6-sol",
            "worktree": str(self.root),
            "repository": "",
        }
        command = build_codex_command(task, "019f-session", resume=True)
        self.assertEqual(["codex", "exec", "resume"], command[:3])
        self.assertIn("019f-session", command)
        self.assertEqual("-", command[-1])

    def test_codex_command_refuses_repository_free_execution(self) -> None:
        task = {
            "reasoning": "high",
            "model": "gpt-5.6-sol",
            "worktree": "",
            "repository": "",
        }
        with self.assertRaisesRegex(RuntimeError, "registered project"):
            build_codex_command(task)

    def test_registered_scope_is_the_only_worker_cwd(self) -> None:
        relative_scope = Path("packages") / "component"
        (self.repo / relative_scope).mkdir(parents=True)
        register_project(
            "sample",
            self.repo,
            "dev",
            ["coordinator"],
            relative_scope.as_posix(),
            data_dir=self.root,
        )
        task = self.service.create_task(
            "Scoped check",
            idempotency_key="scoped-worker",
            repository=str(self.repo),
            base_branch="dev",
            worker_type="cli",
        )
        worktree = self._attach_fixture_worktree(task)
        scoped_worktree = worktree / relative_scope
        scoped_worktree.mkdir(parents=True)
        run_id = "run-scoped-worker"
        self.service.db.execute(
            """
            INSERT INTO runs(id, task_id, attempt, adapter, command_summary, status, created_at)
            VALUES(?, ?, 1, 'cli', 'test', 'QUEUED', datetime('now'))
            """,
            (run_id, task["id"]),
        )
        prompt = self.root / "scope-prompt.txt"
        prompt.write_text("inspect the registered scope", encoding="utf-8")

        class Sink:
            def write(self, _value):
                return None

            def close(self):
                return None

        class FakeProcess:
            stdin = Sink()
            stdout = iter([])

            def wait(self):
                return 0

        args = argparse.Namespace(
            db=str(self.service.db.path),
            data_dir=str(self.root),
            task=task["id"],
            run=run_id,
            prompt_file=str(prompt),
            resume=False,
        )
        with patch(
            "qingtian_engine.worker_entry.subprocess.Popen",
            return_value=FakeProcess(),
        ) as spawn:
            self.assertEqual(0, run_worker(args))
        command = spawn.call_args.args[0]
        self.assertEqual(str(scoped_worktree.resolve()), spawn.call_args.kwargs["cwd"])
        self.assertEqual(
            str(scoped_worktree.resolve()), command[command.index("-C") + 1]
        )

    def test_dispatch_resolves_control_paths_before_worker_changes_cwd(self) -> None:
        task = self.service.create_task(
            "绝对控制路径", idempotency_key="absolute-control-paths"
        )
        prompt = self.root / "absolute-path-prompt.txt"
        prompt.write_text("test", encoding="utf-8")
        relative_data = Path(os.path.relpath(self.root / "relative-data", Path.cwd()))

        class Spawned:
            pid = 43209

        manager = RunManager(self.service, relative_data)
        with patch("qingtian_engine.runner.subprocess.Popen", return_value=Spawned()) as spawn:
            manager.dispatch(task["id"], prompt)
        command = spawn.call_args.args[0]
        db_path = Path(command[command.index("--db") + 1])
        data_path = Path(command[command.index("--data-dir") + 1])
        self.assertTrue(manager.data_dir.is_absolute())
        self.assertTrue(db_path.is_absolute())
        self.assertTrue(data_path.is_absolute())

    def test_dry_run_resolves_repository_without_mutating_the_task(self) -> None:
        task = self.service.create_task(
            "Frontend visual verification",
            idempotency_key="repository-inference-target",
            worker_type="qa",
            owner_session="frontend",
            state="PLANNED",
        )
        prompt = self.root / "inferred-repository.txt"
        prompt.write_text("verify", encoding="utf-8")

        before_task = self.service.get_task(task["id"])
        before_events = self.service.db.one(
            "SELECT COUNT(*) AS count FROM events WHERE task_id=?", (task["id"],)
        )["count"]
        plan = RunManager(self.service, self.root).dispatch(
            task["id"], prompt, dry_run=True
        )

        self.assertTrue(plan["dry_run"])
        self.assertEqual(str(self.repo.resolve()), plan["repository"])
        self.assertEqual("dev", plan["base_branch"])
        self.assertTrue(plan["would_create_worktree"])
        self.assertEqual(before_task, self.service.get_task(task["id"]))
        self.assertEqual(
            before_events,
            self.service.db.one(
                "SELECT COUNT(*) AS count FROM events WHERE task_id=?", (task["id"],)
            )["count"],
        )
        self.assertEqual(
            0,
            self.service.db.one(
                "SELECT COUNT(*) AS count FROM runs WHERE task_id=?", (task["id"],)
            )["count"],
        )
        self.assertEqual(
            0,
            self.service.db.one(
                "SELECT COUNT(*) AS count FROM evidence WHERE task_id=?", (task["id"],)
            )["count"],
        )
        self.assertEqual([], list((self.root / "prompts").glob("*.txt")))

    def test_dispatch_blocks_unroutable_qa_before_creating_run(self) -> None:
        task = self.service.create_task(
            "纯视觉验收",
            idempotency_key="repository-inference-blocked",
            worker_type="qa",
            owner_session="qa",
            state="PLANNED",
        )
        prompt = self.root / "missing-repository.txt"
        prompt.write_text("verify", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "CONFIGURATION: repository"):
            RunManager(self.service, self.root / "unconfigured").dispatch(
                task["id"], prompt, dry_run=True
            )

        blocked = self.service.get_task(task["id"])
        self.assertEqual("PLANNED", blocked["state"])
        self.assertEqual("", blocked["blocking_reason"])
        self.assertEqual(
            0,
            self.service.db.one(
                "SELECT COUNT(*) AS count FROM runs WHERE task_id=?",
                (task["id"],),
            )["count"],
        )

    def test_production_dispatch_is_hard_blocked(self) -> None:
        prompt = self.root / "prompt.md"
        prompt.write_text("do not run", encoding="utf-8")
        task = self.service.create_task(
            "生产发布", idempotency_key="prod", environment="pro"
        )
        with self.assertRaisesRegex(RuntimeError, "不自动执行"):
            RunManager(self.service, self.root).dispatch(task["id"], prompt, dry_run=True)
        self.assertEqual("INBOX", self.service.get_task(task["id"])["state"])
        self.assertEqual(
            0,
            self.service.db.one(
                "SELECT COUNT(*) AS count FROM authorization_audit WHERE task_id=?",
                (task["id"],),
            )["count"],
        )

    def test_recover_marks_legacy_exit_70_as_infrastructure_failure(self) -> None:
        task = self.service.create_task("恢复执行器", idempotency_key="recover")
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, session_id,
                status, exit_code, created_at
            ) VALUES('failed-run', ?, 1, 'cli', 'test', 'session-1', 'FAILED', 70, datetime('now'))
            """,
            (task["id"],),
        )
        self.service.db.add_event(
            task["id"],
            "task.state_changed",
            "worker-guard",
            "后台执行器异常：AttributeError",
            "legacy-worker-error",
            {"state": "FAILED"},
        )
        self.service.transition(task["id"], "FAILED", force=True)
        prompt = self.root / "recover.txt"
        prompt.write_text("continue", encoding="utf-8")

        class Spawned:
            pid = 43210

        manager = RunManager(self.service, self.root)
        with patch("qingtian_engine.runner.subprocess.Popen", return_value=Spawned()):
            recovered = manager.recover_infrastructure_failure(
                task["id"], prompt, resume=True
            )

        failed = self.service.db.one("SELECT * FROM runs WHERE id='failed-run'")
        self.assertEqual("INFRASTRUCTURE", failed["failure_kind"])
        self.assertEqual("AttributeError", failed["failure_type"])
        self.assertTrue(failed["failure_trace_hash"])
        self.assertEqual("failed-run", recovered["retry_of"])
        recovery = self.service.db.one(
            "SELECT summary FROM events WHERE task_id=? AND event_type='run.recovered'",
            (task["id"],),
        )
        self.assertEqual("执行器已恢复并重试", recovery["summary"])

    def test_verification_debt_has_bounded_recoverable_retries(self) -> None:
        task = self.service.create_task("证据回填", idempotency_key="backfill")
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, session_id,
                status, exit_code, created_at
            ) VALUES('done-run', ?, 1, 'cli', 'test', 'session-2', 'DONE', 0, datetime('now'))
            """,
            (task["id"],),
        )
        self.service.transition(task["id"], "VERIFYING", force=True)

        class Spawned:
            pid = 43211

        manager = RunManager(self.service, self.root)
        with patch("qingtian_engine.runner.subprocess.Popen", return_value=Spawned()):
            first = manager.dispatch_verification_backfill(
                task["id"], cooldown_seconds=0
            )
            self.assertEqual("done-run", first["retry_of"])
            self.service.db.execute(
                "UPDATE runs SET status='DONE', finished_at=datetime('now') WHERE id=?",
                (first["id"],),
            )
            self.service.transition(task["id"], "VERIFYING", force=True)
            second = manager.dispatch_verification_backfill(
                task["id"], cooldown_seconds=0
            )
            self.service.db.execute(
                "UPDATE runs SET status='DONE', finished_at=datetime('now') WHERE id=?",
                (second["id"],),
            )
            self.service.transition(task["id"], "VERIFYING", force=True)
            third = manager.dispatch_verification_backfill(
                task["id"], cooldown_seconds=0
            )
            self.service.db.execute(
                "UPDATE runs SET status='DONE', finished_at=datetime('now') WHERE id=?",
                (third["id"],),
            )
            self.service.transition(task["id"], "VERIFYING", force=True)
            with self.assertRaisesRegex(RuntimeError, "retry limit"):
                manager.dispatch_verification_backfill(
                    task["id"], cooldown_seconds=0
                )
        self.assertEqual(
            3,
            self.service.db.one(
                "SELECT COUNT(*) count FROM events "
                "WHERE task_id=? AND event_type='verification.backfill_queued'",
                (task["id"],),
            )["count"],
        )

    def test_verification_reconcile_skips_undispatchable_head_fairly(self) -> None:
        blocked = self.service.create_task(
            "队首不可派发",
            idempotency_key="backfill-fair-blocked",
            priority=0,
        )
        self.service.transition(blocked["id"], "VERIFYING", force=True)
        self.service.db.add_event(
            blocked["id"],
            "verification.internal_qa_hold",
            "test",
            "等待内部 QA",
            "fair-blocked-qa",
            {"state": "VERIFYING"},
        )
        ready = self.service.create_task(
            "后续可派发",
            idempotency_key="backfill-fair-ready",
            priority=1,
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status, created_at
            ) VALUES('fair-ready-done', ?, 1, 'cli', 'test', 'DONE', datetime('now'))
            """,
            (ready["id"],),
        )
        self.service.transition(ready["id"], "VERIFYING", force=True)

        class Spawned:
            pid = 43212

        manager = RunManager(self.service, self.root)
        with patch("qingtian_engine.runner.subprocess.Popen", return_value=Spawned()):
            result = manager.reconcile_verification_debt(
                max_new=1, max_active=1, cooldown_seconds=0
            )
        self.assertEqual([ready["id"]], [item["task_id"] for item in result["queued"]])
        self.assertEqual(blocked["id"], result["skipped"][0]["task_id"])

    def test_verification_reconcile_ignores_waiting_qa_markers(self) -> None:
        task = self.service.create_task(
            "等待真实设备的内部 QA",
            idempotency_key="waiting-internal-qa",
        )
        self.service.db.add_event(
            task["id"],
            "verification.internal_qa_hold",
            "test",
            "等待内部 QA",
            "waiting-internal-qa-marker",
            {"state": "WAITING"},
        )
        self.service.transition(
            task["id"],
            "WAITING",
            blocking_reason="WAITING_INDEPENDENT_QA: 缺真实设备",
            force=True,
        )

        result = RunManager(self.service, self.root).reconcile_verification_debt(
            max_new=1, max_active=1, cooldown_seconds=0
        )

        self.assertEqual([], result["queued"])
        self.assertEqual([], result["skipped"])
        self.assertEqual(0, result["active"])

    def test_reconcile_moves_only_stale_external_and_delegated_to_recovery(
        self,
    ) -> None:
        fresh = self.service.create_task(
            "真实外部运行", idempotency_key="fresh-external", state="RUNNING"
        )
        self.service.heartbeat_task(fresh["id"])
        stale = self.service.create_task(
            "失联委派任务",
            idempotency_key="stale-delegated",
            state="RUNNING",
            action_owner_kind="agent",
            action_owner="subtask",
            action_text="旧执行状态",
        )
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        self.service.db.execute(
            """
            UPDATE tasks
            SET execution_mode='delegated', heartbeat_at=?, updated_at=?
            WHERE id=?
            """,
            (old, old, stale["id"]),
        )
        waiting = self.service.create_task(
            "真实外部等待",
            idempotency_key="preserve-external-waiting",
            state="WAITING",
            action_owner_kind="external",
            action_owner="渠道",
            action_text="等待回调",
        )
        result = RunManager(self.service, self.root).reconcile()
        self.assertEqual(1, result["delegated_stale"])
        self.assertEqual("RUNNING", self.service.get_task(fresh["id"])["state"])
        recovered = self.service.get_task(stale["id"])
        self.assertEqual("WAITING", recovered["state"])
        self.assertTrue(recovered["blocking_reason"].startswith("STALE_EXECUTION:"))
        self.assertEqual("WAITING", self.service.get_task(waiting["id"])["state"])
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == stale["id"]
        )
        self.assertEqual("RECOVERY_REQUIRED", selected["runtime_status"]["code"])
        self.assertIn("失联", selected["display_action"])
        self.service.heartbeat_task(
            stale["id"], execution_mode="delegated"
        )
        self.assertEqual("RUNNING", self.service.get_task(stale["id"])["state"])

    def test_live_managed_run_overrides_stale_external_wait_and_repairs_task(
        self,
    ) -> None:
        task = self.service.create_task(
            "存活执行器优先",
            idempotency_key="live-run-over-stale-external",
            state="WAITING",
            blocking_reason="STALE_EXECUTION:external heartbeat expired",
            action_owner_kind="external",
            action_owner="旧外部任务",
            action_text="等待心跳",
        )
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        self.service.db.execute(
            """
            UPDATE tasks SET execution_mode='external', heartbeat_at=?,
                updated_at=? WHERE id=?
            """,
            (old, old, task["id"]),
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status, pid,
                started_at, created_at
            ) VALUES('live-state-sync', ?, 1, 'cli', 'test', 'RUNNING', ?,
                datetime('now'), datetime('now'))
            """,
            (task["id"], os.getpid()),
        )

        projected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("RUNNING", projected["state"])
        self.assertEqual("WAITING", projected["stored_state"])
        self.assertEqual("STATE_SYNCING", projected["runtime_status"]["code"])
        self.assertEqual("执行中（状态同步中）", projected["display_action"])
        self.assertEqual("none", projected["human_action"]["owner_kind"])

        manager = RunManager(self.service, self.root)
        result = manager.reconcile()
        repaired = self.service.get_task(task["id"])
        self.assertEqual("RUNNING", repaired["state"])
        self.assertEqual("managed", repaired["execution_mode"])
        self.assertIsNone(repaired["heartbeat_at"])
        self.assertEqual("", repaired["blocking_reason"])
        self.assertEqual(1, result["live_run_restored"])
        self.assertEqual(0, manager.reconcile()["state_synchronized"])

    def test_terminal_run_reconciliation_closes_parent_and_unlocks_dependency(
        self,
    ) -> None:
        parent = self.service.create_task(
            "已结束的上游执行",
            idempotency_key="terminal-parent",
            state="WAITING",
            evidence_profile="artifact",
        )
        self.service.add_evidence(
            parent["id"], "artifact", "verified result", verified=True
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                exit_code, started_at, finished_at, created_at
            ) VALUES('terminal-parent-run', ?, 1, 'cli', 'test', 'DONE', 0,
                datetime('now'), datetime('now'), datetime('now'))
            """,
            (parent["id"],),
        )
        child = self.service.create_task(
            "等待上游完成",
            idempotency_key="terminal-child",
            state="WAITING",
            blocking_reason="依赖未完成：已结束的上游执行",
        )
        self.service.add_dependency(child["id"], parent["id"])

        result = RunManager(self.service, self.root).reconcile()

        self.assertEqual("DONE", self.service.get_task(parent["id"])["state"])
        self.assertEqual("PLANNED", self.service.get_task(child["id"])["state"])
        self.assertEqual(1, result["run_completed"])
        self.assertEqual(1, result["dependencies_ready"])

    def test_done_run_without_evidence_reliably_enters_verifying(self) -> None:
        task = self.service.create_task(
            "执行结束等待证据",
            idempotency_key="terminal-verifying",
            state="RUNNING",
            evidence_profile="artifact",
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                exit_code, started_at, finished_at, created_at
            ) VALUES('terminal-verifying-run', ?, 1, 'cli', 'test', 'DONE', 0,
                datetime('now'), datetime('now'), datetime('now'))
            """,
            (task["id"],),
        )

        RunManager(self.service, self.root).reconcile()

        selected = self.service.get_task(task["id"])
        self.assertEqual("VERIFYING", selected["state"])
        self.assertEqual(["artifact"], self.service.missing_completion_evidence(task["id"]))

    def test_recovery_returns_single_active_attempt(self) -> None:
        task = self.service.create_task(
            "单一恢复尝试", idempotency_key="single-active-recovery"
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status, pid,
                created_at
            ) VALUES('active-recovery', ?, 1, 'cli', 'test', 'RUNNING', 123,
                datetime('now'))
            """,
            (task["id"],),
        )
        prompt = self.root / "single-active.txt"
        prompt.write_text("continue", encoding="utf-8")
        run = RunManager(self.service, self.root).recover_infrastructure_failure(
            task["id"], prompt
        )
        self.assertEqual("active-recovery", run["id"])
        self.assertEqual(
            1,
            self.service.db.one(
                "SELECT COUNT(*) AS count FROM runs WHERE task_id=?",
                (task["id"],),
            )["count"],
        )

    def test_scheduler_claims_runnable_p0_fifo_and_preserves_runtime_policy(
        self,
    ) -> None:
        from qingtian_engine.config import runtime_policy

        # Explicit UTC instants correspond to 12:00 and 00:00 Asia/Shanghai.
        # Exercise the actual policy function, not a permissive speed assertion.
        for label, fixed_now, expected_speed, expected_fast in (
            ("day", datetime(2026, 1, 1, 4, tzinfo=timezone.utc), "fast", True),
            ("night", datetime(2026, 1, 1, 16, tzinfo=timezone.utc), "standard", False),
        ):
            with self.subTest(period=label):
                service = ControlPlane(Database(self.root / (label + ".sqlite3")))
                first = service.create_task(
                    "最早 P0", idempotency_key="scheduler-fifo-first", priority=0,
                    state="PLANNED", reasoning="high",
                )
                second = service.create_task(
                    "稍后 P0", idempotency_key="scheduler-fifo-second", priority=0,
                    state="PLANNED", reasoning="high",
                )
                service.db.execute(
                    "UPDATE tasks SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                    (first["id"],),
                )
                service.db.execute(
                    "UPDATE tasks SET created_at='2026-01-02T00:00:00+00:00' WHERE id=?",
                    (second["id"],),
                )
                expected_policy = runtime_policy("high", now=fixed_now)
                self.assertEqual("gpt-5.6-sol", expected_policy.model)
                self.assertEqual("high", expected_policy.reasoning)
                self.assertEqual(expected_speed, expected_policy.speed)
                self.assertIs(expected_fast, expected_policy.enable_fast_mode)

                class Spawned:
                    pid = os.getpid()

                manager = RunManager(service, self.root / label)
                with patch("qingtian_engine.runner.subprocess.Popen", return_value=Spawned()), patch(
                    "qingtian_engine.runner.runtime_policy",
                    side_effect=lambda requested: runtime_policy(requested, now=fixed_now),
                ) as policy_call:
                    result = manager.reconcile_dispatch_queue(max_new=1, max_active=1)

                policy_call.assert_called_once_with("high")
                self.assertEqual([first["id"]], [item["task_id"] for item in result["claimed"]])
                selected = service.get_task(first["id"])
                self.assertEqual("gpt-5.6-sol", selected["model"])
                self.assertEqual("high", selected["reasoning"])
                self.assertEqual(expected_speed, selected["speed"])
                self.assertEqual("QUEUED", selected["state"])
                self.assertEqual("PLANNED", service.get_task(second["id"])["state"])

    def test_scheduler_never_claims_paused_external_sensitive_or_ops(self) -> None:
        safe = self.service.create_task(
            "可自动领取",
            idempotency_key="scheduler-only-safe",
            priority=1,
            state="PLANNED",
        )
        paused = self.service.create_task(
            "用户确认暂缓",
            idempotency_key="scheduler-skip-paused",
            priority=0,
            state="PLANNED",
        )
        external = self.service.create_task(
            "等待渠道",
            idempotency_key="scheduler-skip-external",
            priority=0,
            state="PLANNED",
            action_owner_kind="external",
            action_owner="渠道",
            action_text="等待外部回调",
        )
        sensitive = self.service.create_task(
            "敏感授权",
            idempotency_key="scheduler-skip-sensitive",
            priority=0,
            state="PLANNED",
            action_owner_kind="agent",
            action_owner="security",
            action_text="需要授权",
            action_sensitive=True,
        )
        ops = self.service.create_task(
            "OPS 上线门禁",
            idempotency_key="scheduler-skip-ops",
            priority=0,
            state="PLANNED",
        )
        plan_only = self.service.create_task(
            "仅做方案评审",
            idempotency_key="scheduler-skip-plan-only",
            priority=0,
            state="PLANNED",
            scope_summary="Plan-only：只分析，不进入自动执行",
        )

        class Spawned:
            pid = os.getpid()

        with patch("qingtian_engine.runner.subprocess.Popen", return_value=Spawned()):
            result = RunManager(
                self.service, self.root
            ).reconcile_dispatch_queue(max_new=2, max_active=2)

        self.assertEqual([safe["id"]], [item["task_id"] for item in result["claimed"]])
        for task in (paused, external, sensitive, ops, plan_only):
            self.assertEqual("PLANNED", self.service.get_task(task["id"])["state"])

    def test_scheduler_moves_exhausted_backfill_to_auditable_evidence_wait(
        self,
    ) -> None:
        task = self.service.create_task(
            "证据回填耗尽",
            idempotency_key="scheduler-exhausted-backfill",
            priority=0,
            state="VERIFYING",
            evidence_profile="artifact",
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                exit_code, started_at, finished_at, created_at
            ) VALUES('exhausted-done', ?, 1, 'cli', 'test', 'DONE', 0,
                datetime('now'), datetime('now'), datetime('now'))
            """,
            (task["id"],),
        )
        for sequence in range(1, 4):
            self.service.db.add_event(
                task["id"],
                "verification.backfill_queued",
                "test",
                "backfill",
                "exhausted-backfill-{}".format(sequence),
                {"attempt": sequence, "backfill_sequence": sequence},
            )
        self.service.db.add_event(
            task["id"],
            "verification.internal_qa_hold",
            "test",
            "internal qa",
            "exhausted-independent-qa",
            {},
        )

        result = RunManager(
            self.service, self.root
        )._mark_exhausted_verification()

        self.assertEqual([task["id"]], result["moved"])
        selected = self.service.get_task(task["id"])
        self.assertEqual("WAITING", selected["state"])
        self.assertEqual("agent", selected["action_owner_kind"])
        self.assertTrue(
            selected["blocking_reason"].startswith(
                "EVIDENCE_COLLECTION_REQUIRED:"
            )
        )
        self.assertEqual(
            3,
            self.service.db.one(
                "SELECT COUNT(*) count FROM events "
                "WHERE task_id=? AND event_type='verification.backfill_queued'",
                (task["id"],),
            )["count"],
        )

    def test_evidence_collection_wait_closes_when_real_evidence_arrives(
        self,
    ) -> None:
        task = self.service.create_task(
            "真实证据闭环",
            idempotency_key="evidence-collection-close",
            state="WAITING",
            evidence_profile="artifact",
            blocking_reason="EVIDENCE_COLLECTION_REQUIRED: artifact",
            action_owner_kind="agent",
            action_owner="qa",
            action_text="补真实证据",
        )
        self.service.add_evidence(
            task["id"], "artifact", "real artifact", verified=True
        )

        result = self.service.reconcile_state_progression()

        self.assertEqual(1, result["evidence_completed"])
        self.assertEqual("DONE", self.service.get_task(task["id"])["state"])

    def test_reconcile_releases_stale_queued_run_without_pid(self) -> None:
        task = self.service.create_task(
            "陈旧排队执行",
            idempotency_key="stale-queued-without-pid",
            state="QUEUED",
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                created_at
            ) VALUES('stale-queued-run', ?, 1, 'cli', 'test', 'QUEUED',
                '2026-01-01T00:00:00+00:00')
            """,
            (task["id"],),
        )

        result = RunManager(self.service, self.root).reconcile()

        self.assertEqual(1, result["stale"])
        run = self.service.db.one(
            "SELECT * FROM runs WHERE id='stale-queued-run'"
        )
        self.assertEqual("FAILED", run["status"])
        self.assertEqual("queue_watchdog", run["failure_stage"])
        self.assertEqual("WAITING", self.service.get_task(task["id"])["state"])

    def test_infrastructure_recovery_respects_global_active_cap(self) -> None:
        active = self.service.create_task(
            "已有活跃执行",
            idempotency_key="recovery-cap-active",
            state="RUNNING",
        )
        waiting = self.service.create_task(
            "等待基础设施恢复",
            idempotency_key="recovery-cap-waiting",
            state="WAITING",
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                pid, created_at
            ) VALUES('global-active-run', ?, 1, 'cli', 'test', 'RUNNING',
                ?, datetime('now'))
            """,
            (active["id"], os.getpid()),
        )
        self.service.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                exit_code, failure_kind, failure_stage, created_at, finished_at
            ) VALUES('recoverable-failure', ?, 1, 'cli', 'test', 'FAILED',
                70, 'INFRASTRUCTURE', 'worker', datetime('now'), datetime('now'))
            """,
            (waiting["id"],),
        )

        result = RunManager(
            self.service, self.root
        )._recover_safe_infrastructure_waiters(max_new=1, max_active=1)

        self.assertEqual([], result["recovered"])
        self.assertEqual(1, result["active"])
        self.assertEqual("WAITING", self.service.get_task(waiting["id"])["state"])


if __name__ == "__main__":
    unittest.main()
