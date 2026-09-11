from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qingtian_engine.db import Database
from qingtian_engine.service import ControlPlane


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "control.sqlite3")
        self.service = ControlPlane(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_task_and_event_idempotency(self) -> None:
        first = self.service.create_task("同一个任务", idempotency_key="same")
        second = self.service.create_task("不同标题也不重复", idempotency_key="same")
        self.assertEqual(first["id"], second["id"])
        inserted = self.db.add_event(
            first["id"], "test", "unit", "once", "same-event"
        )
        duplicate = self.db.add_event(
            first["id"], "test", "unit", "twice", "same-event"
        )
        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(
            3,
            self.db.one(
                "SELECT COUNT(*) AS count FROM events WHERE task_id=?", (first["id"],)
            )["count"],
        )

    def test_observed_deploy_or_smoke_does_not_expand_code_only_contract(self):
        task = self.service.create_task(
            "Synthetic code-only task", evidence_profile="code", requires_deploy=False,
        )
        self.service.transition(task["id"], "VERIFYING", force=True)
        self.service.add_evidence(task["id"], "commit", "independently checked commit", verified=True)
        self.service.add_evidence(task["id"], "test", "independently run test", verified=True)
        self.service.add_evidence(task["id"], "deploy", "Not run: not authorized", verified=False)
        self.service.add_evidence(task["id"], "smoke", "local-only check", verified=False)
        self.assertEqual(["commit", "test"], self.service.required_evidence(task["id"]))
        self.assertEqual([], self.service.missing_completion_evidence(task["id"]))
        self.service.reconcile_state_progression()
        self.assertEqual("DONE", self.service.get_task(task["id"])["state"])
        self.assertEqual(0, self.db.one("SELECT COUNT(*) AS n FROM runs")["n"])

    def test_explicit_deployment_still_requires_verified_deploy_and_smoke(self):
        task = self.service.create_task(
            "Synthetic authorized deployment", evidence_profile="code", requires_deploy=True,
        )
        self.service.transition(task["id"], "VERIFYING", force=True)
        for kind in ("commit", "test"):
            self.service.add_evidence(task["id"], kind, "independently checked", verified=True)
        for kind in ("deploy", "smoke"):
            self.service.add_evidence(task["id"], kind, "not run", verified=False)
        self.assertEqual(["deploy", "smoke"], self.service.missing_completion_evidence(task["id"]))
        self.service.reconcile_state_progression()
        self.assertEqual("VERIFYING", self.service.get_task(task["id"])["state"])

    def test_deployment_requirement_applies_to_every_evidence_profile(self):
        for profile, base in (("code", ["commit", "test"]), ("qa", ["test"]),
                              ("browser", ["browser"]), ("artifact", ["artifact"]),
                              ("legacy", ["migration_metadata"])):
            with self.subTest(profile=profile):
                task = self.service.create_task("Synthetic deploy " + profile,
                    evidence_profile=profile, requires_deploy=True)
                self.service.transition(task["id"], "VERIFYING", force=True)
                for kind in base:
                    self.service.add_evidence(task["id"], kind, "independently verified fixture", verified=True)
                self.assertEqual(base + ["deploy", "smoke"], self.service.required_evidence(task["id"]))
                self.assertEqual(["deploy", "smoke"], self.service.missing_completion_evidence(task["id"]))
                with self.assertRaisesRegex(ValueError, "deploy, smoke"):
                    self.service.transition(task["id"], "DONE")

    def test_done_requires_evidence(self) -> None:
        task = self.service.create_task(
            "代码任务",
            idempotency_key="code",
            repository="/tmp/example",
            worker_type="cli",
        )
        for state in ("PLANNED", "QUEUED", "RUNNING", "VERIFYING"):
            self.service.transition(task["id"], state)
        with self.assertRaisesRegex(ValueError, "commit, test"):
            self.service.transition(task["id"], "DONE")
        self.service.add_evidence(
            task["id"], "commit", "abc123", verified=True
        )
        self.service.add_evidence(
            task["id"], "test", "4 passed", verified=True
        )
        done = self.service.transition(task["id"], "DONE")
        self.assertEqual("DONE", done["state"])
        self.assertEqual(100, done["progress"])

    def test_auto_profile_uses_artifact_for_existing_non_git_repository(self) -> None:
        local_artifact = Path(self.temp.name) / "management-plane"
        local_artifact.mkdir()
        task = self.service.create_task(
            "本地控制面产物",
            idempotency_key="local-non-git-artifact",
            repository=str(local_artifact),
            worker_type="cli",
        )
        self.assertEqual(["artifact"], self.service.required_evidence(task["id"]))
        self.service.add_evidence(
            task["id"],
            "artifact",
            "source hashes and local smoke report",
            verified=True,
        )
        self.service.transition(task["id"], "VERIFYING", force=True)
        done = self.service.transition(task["id"], "DONE")
        self.assertEqual("DONE", done["state"])

        explicit_code = self.service.create_task(
            "非 Git 路径上的显式代码任务",
            idempotency_key="explicit-code-non-git",
            repository=str(local_artifact),
            evidence_profile="code",
        )
        self.assertEqual(
            ["commit", "test"],
            self.service.required_evidence(explicit_code["id"]),
        )

    def test_unverified_evidence_never_passes_completion_gate(self) -> None:
        task = self.service.create_task(
            "部署冒烟必须验真",
            idempotency_key="verified-evidence-only",
            repository="/tmp/example",
            requires_deploy=True,
        )
        for kind in ("commit", "test", "deploy"):
            self.service.add_evidence(
                task["id"], kind, "{} ok".format(kind), verified=True
            )
        self.service.add_evidence(
            task["id"], "smoke", "PARTIAL/BLOCKED", verified=False
        )
        self.assertEqual(
            ["smoke"], self.service.missing_completion_evidence(task["id"])
        )
        self.service.transition(task["id"], "VERIFYING", force=True)
        with self.assertRaisesRegex(ValueError, "smoke"):
            self.service.transition(task["id"], "DONE")

    def test_dependency_blocks_dispatch_readiness(self) -> None:
        parent = self.service.create_task("依赖", idempotency_key="parent")
        child = self.service.create_task("下游", idempotency_key="child")
        self.service.add_dependency(child["id"], parent["id"])
        self.assertEqual(1, len(self.service.unresolved_dependencies(child["id"])))
        self.service.add_evidence(parent["id"], "artifact", "report", verified=True)
        self.service.transition(parent["id"], "DONE", force=True)
        self.assertEqual([], self.service.unresolved_dependencies(child["id"]))

    def test_reconcile_advances_satisfied_completion_gate(self) -> None:
        task = self.service.create_task(
            "延迟证据自动收口",
            idempotency_key="delayed-evidence-completion",
            evidence_profile="artifact",
        )
        self.service.transition(task["id"], "VERIFYING", force=True)
        self.service.add_evidence(
            task["id"], "artifact", "verified local artifact", verified=True
        )

        result = self.service.reconcile_state_progression()

        self.assertEqual({"completed": 1, "dependencies_ready": 0, "evidence_completed": 0}, result)
        self.assertEqual("DONE", self.service.get_task(task["id"])["state"])

    def test_reconcile_releases_resolved_dependency_waiter(self) -> None:
        parent = self.service.create_task(
            "已完成依赖",
            idempotency_key="resolved-dependency-parent",
            evidence_profile="artifact",
        )
        child = self.service.create_task(
            "等待依赖后继续",
            idempotency_key="resolved-dependency-child",
        )
        self.service.add_dependency(child["id"], parent["id"])
        self.service.transition(
            child["id"],
            "WAITING",
            blocking_reason="依赖未完成：已完成依赖",
            force=True,
        )
        self.service.add_evidence(
            parent["id"], "artifact", "parent verified", verified=True
        )
        self.service.transition(parent["id"], "DONE", force=True)

        result = self.service.reconcile_state_progression()

        self.assertEqual({"completed": 0, "dependencies_ready": 1, "evidence_completed": 0}, result)
        released = self.service.get_task(child["id"])
        self.assertEqual("PLANNED", released["state"])
        self.assertEqual("", released["blocking_reason"])

    def test_reconcile_does_not_release_waiter_with_other_owner(self) -> None:
        parent = self.service.create_task(
            "完成但仍需外部动作",
            idempotency_key="external-wait-parent",
            evidence_profile="artifact",
        )
        child = self.service.create_task(
            "仍需人工处理",
            idempotency_key="external-wait-child",
        )
        self.service.add_dependency(child["id"], parent["id"])
        self.service.transition(
            child["id"],
            "WAITING",
            blocking_reason="依赖已完成，但仍需外部授权",
            force=True,
        )
        self.service.set_human_action(
            child["id"], "external", owner="外部管理员", text="授权"
        )
        self.service.add_evidence(
            parent["id"], "artifact", "parent verified", verified=True
        )
        self.service.transition(parent["id"], "DONE", force=True)

        result = self.service.reconcile_state_progression()

        self.assertEqual({"completed": 0, "dependencies_ready": 0, "evidence_completed": 0}, result)
        self.assertEqual("WAITING", self.service.get_task(child["id"])["state"])

    def test_sensitive_values_are_redacted(self) -> None:
        task = self.service.create_task(
            "排查 token=abcdefghijklmnopqrstuvwxyz1234567890 联系 a@example.com",
            idempotency_key="secret",
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", task["title"])
        self.assertNotIn("a@example.com", task["title"])
        self.assertIn("[REDACTED", task["title"])

    def test_feedback_cursor_prevents_duplicate_manager_updates(self) -> None:
        task = self.service.create_task("反馈游标", idempotency_key="feedback")
        first = self.service.feedback_changes("QT-00")
        second = self.service.feedback_changes("QT-00")
        self.assertGreaterEqual(len(first["changes"]), 2)
        self.assertEqual([], second["changes"])
        self.service.transition(task["id"], "PLANNED")
        third = self.service.feedback_changes("QT-00")
        self.assertEqual(1, len(third["changes"]))

    def test_bootstrap_feedback_cursor_skips_import_history(self) -> None:
        self.service.create_task("历史任务", idempotency_key="history")
        cursor = self.service.initialize_feedback_cursor("manager")
        self.assertGreater(cursor["cursor"], 0)
        self.assertEqual([], self.service.feedback_changes("manager")["changes"])

    def test_dashboard_exposes_realtime_version_and_verification_debt(self) -> None:
        task = self.service.create_task(
            "待回填证据",
            idempotency_key="verification-debt",
            repository="/tmp/repo",
        )
        self.db.execute(
            """
            INSERT INTO runs(id, task_id, attempt, adapter, command_summary, status, created_at)
            VALUES('done-run', ?, 1, 'cli', 'test', 'DONE', datetime('now'))
            """,
            (task["id"],),
        )
        self.service.transition(task["id"], "VERIFYING", force=True)
        payload = self.service.dashboard_payload()
        selected = next(item for item in payload["tasks"] if item["id"] == task["id"])
        self.assertGreater(payload["version"], 0)
        self.assertEqual("BACKFILL_REQUIRED", selected["runtime_status"]["code"])
        self.assertEqual(["commit", "test"], selected["runtime_status"]["missing_evidence"])

    def test_external_heartbeat_prevents_false_no_run_alarm(self) -> None:
        task = self.service.create_task(
            "Figma 外部执行", idempotency_key="external-heartbeat"
        )
        self.service.transition(task["id"], "RUNNING", force=True)
        before = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("NO_RUN", before["runtime_status"]["code"])
        self.service.heartbeat_task(task["id"])
        after = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("EXTERNAL", after["runtime_status"]["code"])
        self.assertIn("心跳正常", after["runtime_status"]["label"])

    def test_delegated_heartbeat_registers_direct_codex_run_without_local_row(
        self,
    ) -> None:
        task = self.service.create_task(
            "直接委派 Codex",
            idempotency_key="direct-delegated-heartbeat",
            state="INBOX",
        )

        registered = self.service.heartbeat_task(
            task["id"], execution_mode="delegated"
        )

        self.assertEqual("RUNNING", registered["state"])
        self.assertEqual("delegated", registered["execution_mode"])
        self.assertEqual([], registered["runs"])
        projected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("DELEGATED_AGENT", projected["runtime_status"]["code"])
        self.assertIn("子任务执行中", projected["display_action"])

    def test_new_delegated_heartbeat_supersedes_older_terminal_local_run(
        self,
    ) -> None:
        task = self.service.create_task(
            "旧 run 后重新直接委派",
            idempotency_key="delegated-after-old-terminal",
        )
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                exit_code, finished_at, created_at
            ) VALUES('old-terminal-run', ?, 1, 'cli', 'old', 'DONE', 0,
                '2026-07-28T00:00:00+00:00', '2026-07-28T00:00:00+00:00')
            """,
            (task["id"],),
        )

        self.service.heartbeat_task(task["id"], execution_mode="delegated")

        projected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("RUNNING", projected["state"])
        self.assertEqual("DELEGATED_AGENT", projected["runtime_status"]["code"])

    def test_stale_delegated_heartbeat_after_older_done_run_is_recovered(
        self,
    ) -> None:
        task = self.service.create_task(
            "旧 run 后失联的直接委派",
            idempotency_key="stale-delegated-after-old-terminal",
            state="RUNNING",
        )
        old_run = datetime.now(timezone.utc) - timedelta(hours=3)
        stale_heartbeat = datetime.now(timezone.utc) - timedelta(hours=2)
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                exit_code, finished_at, created_at
            ) VALUES('old-done-before-stale-heartbeat', ?, 1, 'cli', 'old',
                'DONE', 0, ?, ?)
            """,
            (
                task["id"],
                old_run.isoformat(timespec="seconds"),
                old_run.isoformat(timespec="seconds"),
            ),
        )
        self.db.execute(
            """
            UPDATE tasks SET execution_mode='delegated', heartbeat_at=?,
                updated_at=? WHERE id=?
            """,
            (
                stale_heartbeat.isoformat(timespec="seconds"),
                stale_heartbeat.isoformat(timespec="seconds"),
                task["id"],
            ),
        )

        result = self.service.reconcile_derived_states()

        self.assertEqual(1, result["delegated_stale"])
        recovered = self.service.get_task(task["id"])
        self.assertEqual("WAITING", recovered["state"])
        self.assertTrue(recovered["blocking_reason"].startswith("STALE_EXECUTION:"))

    def test_fresh_delegated_heartbeat_after_older_done_run_is_not_recovered(
        self,
    ) -> None:
        task = self.service.create_task(
            "旧 run 后仍存活的直接委派",
            idempotency_key="fresh-delegated-after-old-terminal",
        )
        old_run = datetime.now(timezone.utc) - timedelta(hours=2)
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                exit_code, finished_at, created_at
            ) VALUES('old-done-before-fresh-heartbeat', ?, 1, 'cli', 'old',
                'DONE', 0, ?, ?)
            """,
            (
                task["id"],
                old_run.isoformat(timespec="seconds"),
                old_run.isoformat(timespec="seconds"),
            ),
        )
        self.service.heartbeat_task(task["id"], execution_mode="delegated")

        result = self.service.reconcile_derived_states()

        self.assertEqual(0, result["delegated_stale"])
        self.assertEqual("RUNNING", self.service.get_task(task["id"])["state"])

    def test_running_external_precedes_old_internal_qa_marker(self) -> None:
        task = self.service.create_task(
            "H5 RN 仍在真实外部执行",
            idempotency_key="external-precedes-qa",
            repository="/tmp/repo",
        )
        self.db.add_event(
            task["id"],
            "verification.internal_qa_hold",
            "control-plane",
            "旧 QA 标记",
            "old-qa-marker",
            {"state": "VERIFYING"},
        )
        self.service.transition(task["id"], "RUNNING", force=True)
        self.service.heartbeat_task(task["id"])
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("EXTERNAL", selected["runtime_status"]["code"])
        self.assertEqual(
            "外部 Codex 任务执行中 · 心跳正常", selected["display_action"]
        )

    def test_imported_legacy_profile_uses_verified_evidence_without_faking_artifact(
        self,
    ) -> None:
        task = self.service.create_task(
            "Profile 登录修复",
            idempotency_key="legacy-profile",
            imported_from="/tmp/TASKS.md",
            state="VERIFYING",
            worker_type="cli",
            requires_deploy=True,  # deployment is a task requirement, not inferred from a note
        )
        for kind in ("commit", "test", "deploy"):
            self.service.add_evidence(
                task["id"], kind, "{} verified".format(kind), verified=True
            )
        self.service.add_evidence(
            task["id"], "smoke", "PARTIAL/BLOCKED", verified=False
        )
        result = self.service.reconcile_evidence_profiles()
        self.assertEqual({"code": 1, "legacy": 0}, result)
        corrected = self.service.get_task(task["id"])
        self.assertEqual("code", corrected["evidence_profile"])
        self.assertEqual(
            ["smoke"], self.service.missing_completion_evidence(task["id"])
        )
        self.assertFalse(
            self.db.one(
                "SELECT id FROM evidence WHERE task_id=? AND kind='artifact'",
                (task["id"],),
            )
        )

    def test_imported_legacy_without_safe_signal_enters_metadata_review(self) -> None:
        task = self.service.create_task(
            "旧手工迁移代码任务",
            idempotency_key="legacy-metadata-review",
            imported_from="/tmp/TASKS.md",
            state="VERIFYING",
        )
        self.service.reconcile_evidence_profiles()
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("legacy", selected["evidence_profile"])
        self.assertEqual("MIGRATION_REVIEW", selected["runtime_status"]["code"])
        self.assertEqual(
            ["migration_metadata"],
            selected["runtime_status"]["missing_evidence"],
        )

    def test_internal_qa_hold_remains_user_independent_after_backfill(self) -> None:
        task = self.service.create_task(
            "H5 RN 内部验收",
            idempotency_key="internal-qa-hold",
            repository="/tmp/repo",
        )
        self.db.add_event(
            task["id"],
            "verification.internal_qa_hold",
            "control-plane",
            "无需用户补信息，等待 QT-06 内部验收",
            "internal-qa-hold",
            {"state": "VERIFYING"},
        )
        self.db.execute(
            """
            INSERT INTO runs(id, task_id, attempt, adapter, command_summary, status, created_at)
            VALUES('qa-done', ?, 1, 'cli', 'test', 'DONE', datetime('now'))
            """,
            (task["id"],),
        )
        self.service.transition(task["id"], "VERIFYING", force=True)
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("INDEPENDENT_QA", selected["runtime_status"]["code"])
        self.assertEqual(
            "无需你提供 · 等待 QT-06 内部验收",
            selected["runtime_status"]["label"],
        )
        self.assertIn("已补跑", selected["runtime_status"]["action"])

    def test_recovery_status_persists_after_successful_retry(self) -> None:
        task = self.service.create_task(
            "执行器恢复状态",
            idempotency_key="persistent-recovery-status",
        )
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                failure_kind, created_at
            ) VALUES('infra-failed', ?, 1, 'cli', 'test', 'FAILED',
                'INFRASTRUCTURE', datetime('now'))
            """,
            (task["id"],),
        )
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                retry_of, created_at
            ) VALUES('recovered-done', ?, 2, 'cli', 'test', 'DONE',
                'infra-failed', datetime('now'))
            """,
            (task["id"],),
        )
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("执行器已恢复并重试", selected["recovery_status"])

    def test_auto_closure_never_asks_user_for_evidence(self) -> None:
        task = self.service.create_task(
            "Admin 自动收口",
            idempotency_key="auto-closure-status",
            repository="/tmp/repo",
        )
        self.db.add_event(
            task["id"],
            "verification.auto_closure",
            "control-plane",
            "执行产物已保留 · 正在自动收口",
            "auto-closure-status",
            {"state": "VERIFYING"},
        )
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                pid, created_at
            ) VALUES('closure-live', ?, 1, 'cli', 'test', 'RUNNING',
                1, datetime('now'))
            """,
            (task["id"],),
        )
        self.service.transition(task["id"], "RUNNING", force=True)
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("AUTO_CLOSURE", selected["runtime_status"]["code"])
        self.assertEqual(
            "执行产物已保留 · 正在自动收口",
            selected["runtime_status"]["label"],
        )
        self.assertNotIn("用户", selected["runtime_status"]["action"])

    def test_agent_owned_running_task_uses_status_sentence_on_card(self) -> None:
        sentence = "正在核对通用服务响应，并准备对应回归测试"
        task = self.service.create_task(
            "检查通用服务响应的回退处理",
            idempotency_key="agent-status-card",
            priority=0,
            owner_session="QT-04+QT-01+QT-06",
            worker_type="CLI",
            state="RUNNING",
            action_owner_kind="agent",
            action_owner="QT-04+QT-01+QT-06",
            action_text=sentence,
        )
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual(sentence, selected["display_action"])
        self.assertFalse(selected["human_action"]["requires_user"])
        self.assertEqual("agent", selected["human_action"]["owner_kind"])

        self.db.execute(
            "UPDATE tasks SET execution_mode='delegated' WHERE id=?",
            (task["id"],),
        )
        delegated = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("DELEGATED_AGENT", delegated["runtime_status"]["code"])

    def test_auto_closure_failure_exposes_exact_stage(self) -> None:
        task = self.service.create_task(
            "Admin 收口失败",
            idempotency_key="auto-closure-failure-stage",
            repository="/tmp/repo",
        )
        self.db.add_event(
            task["id"],
            "verification.auto_closure",
            "control-plane",
            "执行产物已保留 · 正在自动收口",
            "auto-closure-failure-stage",
            {"state": "VERIFYING"},
        )
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status,
                failure_stage, failure_type, created_at
            ) VALUES('closure-failed', ?, 1, 'cli', 'test', 'FAILED',
                'git_push', 'PermissionDenied', datetime('now'))
            """,
            (task["id"],),
        )
        self.service.transition(task["id"], "FAILED", force=True)
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("FAILED_STAGE", selected["runtime_status"]["code"])
        self.assertIn("git_push/PermissionDenied", selected["display_action"])

    def test_human_actions_are_first_class_and_sorted_user_first(self) -> None:
        external = self.service.create_task(
            "等待支付渠道", idempotency_key="human-action-external"
        )
        user = self.service.create_task(
            "Google OAuth 配置", idempotency_key="human-action-user"
        )
        self.service.set_human_action(
            external["id"],
            "external",
            owner="支付渠道",
            text="重发 state=2 回调",
        )
        self.service.set_human_action(
            user["id"],
            "user",
            owner="你 / Google Cloud 管理员",
            text="新增 redirect URI",
        )
        payload = self.service.dashboard_payload()
        self.assertEqual({"user": 1, "external": 1}, payload["action_summary"])
        self.assertEqual(user["id"], payload["tasks"][0]["id"])
        selected = next(item for item in payload["tasks"] if item["id"] == user["id"])
        self.assertTrue(selected["human_action"]["requires_user"])
        self.assertEqual("你需要：新增 redirect URI", selected["display_action"])

    def test_watchdog_accepts_legacy_naive_event_timestamps(self) -> None:
        task = self.service.create_task(
            "旧时间戳", idempotency_key="legacy-time"
        )
        self.db.execute(
            """
            INSERT INTO runs(
                id, task_id, attempt, adapter, command_summary, status, pid, created_at
            ) VALUES('legacy-live', ?, 1, 'cli', 'test', 'RUNNING', ?, datetime('now'))
            """,
            (task["id"], 1),
        )
        self.service.transition(task["id"], "RUNNING", force=True)
        self.db.add_event(
            task["id"],
            "legacy.event",
            "legacy",
            "naive time",
            "legacy-naive-time",
            {"state": "RUNNING"},
            occurred_at="2020-01-01 00:00:00",
        )
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("EVENT_STALE", selected["runtime_status"]["code"])


if __name__ == "__main__":
    unittest.main()
