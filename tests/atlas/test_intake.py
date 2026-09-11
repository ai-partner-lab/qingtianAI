from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.atlas.capability_fixture import advertised_capabilities
from qingtian_engine.db import Database
from qingtian_engine.intake import (
    CodexPlannerAdapter,
    DeterministicPlannerAdapter,
    IntakeError,
    IntakeService,
    memory_upload,
)
from qingtian_engine.project_config import register_project
from qingtian_engine.runner import RunManager
from qingtian_engine.service import ControlPlane


PNG = b"\x89PNG\r\n\x1a\n" + b"safe-local-test" * 4


class IntakeServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        capability = advertised_capabilities()
        capability.start()
        self.addCleanup(capability.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = ControlPlane(Database(self.root / "control.sqlite3"))
        self.intakes = IntakeService(self.control, self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_advanced_deployment_requires_real_boolean(self):
        for invalid in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(IntakeError, "boolean"):
                    self.intakes.create_intake("Synthetic flag check", intent="analyze", uploads=[], idempotency_key="invalid-flag",
                        advanced={"requires_deploy": invalid})
                self.assertEqual([], self.control.list_tasks())
        self.assertEqual({}, IntakeService._clean_advanced({"requires_deploy": False}))
        self.assertEqual({"requires_deploy": True}, IntakeService._clean_advanced({"requires_deploy": True}))

    def test_text_attachment_planner_policy_and_persistence(self) -> None:
        result = self.intakes.create_intake(
            "Fix the responsive frontend modal loop",
            "analyze",
            [memory_upload("../../screen.png", "image/png", PNG)],
            "request-1",
        )
        self.assertEqual("ROUTED", result["status"])
        self.assertEqual("frontend", result["draft"]["owner_session"])
        self.assertEqual("gpt-5.6-sol", result["draft"]["model"])
        self.assertEqual("screen.png", result["attachments"][0]["name"])
        self.assertNotIn("local_path", result["attachments"][0])
        self.assertEqual(result["id"], result["tasks"][0]["parent_id"] or result["id"])
        self.assertEqual(
            result["id"],
            self.control.get_task(result["task_id"])["source_request_id"],
        )

        internal = self.intakes.get_intake(result["id"], include_internal=True)
        stored = self.root / internal["attachments"][0]["local_path"]
        self.assertEqual(0o600, stored.stat().st_mode & 0o777)
        self.assertEqual(0o700, stored.parent.stat().st_mode & 0o777)
        reloaded = IntakeService(self.control, self.root).get_intake(result["id"])
        self.assertEqual(result["task_id"], reloaded["task_id"])
        self.assertEqual(3, len(reloaded["messages"]))

    def test_idempotency_and_sha_deduplication(self) -> None:
        first = self.intakes.create_intake(
            "分析截图",
            "analyze",
            [
                memory_upload("one.png", "image/png", PNG),
                memory_upload("two.png", "image/png", PNG),
            ],
            "duplicate-key",
        )
        second = self.intakes.create_intake(
            "完全不同的文字也不能重复创建",
            "analyze",
            [],
            "duplicate-key",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["reused"])
        self.assertEqual(1, len(second["attachments"]))
        self.assertEqual(
            1,
            self.control.db.one("SELECT COUNT(*) AS count FROM intakes")["count"],
        )
        self.assertEqual(
            1,
            self.control.db.one(
                "SELECT COUNT(*) AS count FROM tasks WHERE source_request_id=?",
                (first["id"],),
            )["count"],
        )

    def test_secret_is_warned_and_redacted(self) -> None:
        result = self.intakes.create_intake(
            "排查 token=abcdefghijklmnopqrstuvwxyz123456",
            "analyze",
            [],
            "secret-key",
        )
        self.assertTrue(result["secret_warning"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result["text"])
        self.assertIn("[REDACTED]", result["text"])

    def test_cross_domain_complex_task_creates_parent_and_children(self) -> None:
        result = self.intakes.create_intake(
            "Update the frontend component and backend API across the full stack",
            "analyze",
            [],
            "complex-key",
        )
        self.assertEqual("coordinator", result["draft"]["owner_session"])
        self.assertGreaterEqual(len(result["tasks"]), 3)
        parent_id = result["task_id"]
        children = [row for row in result["tasks"] if row["id"] != parent_id]
        self.assertTrue(all(row["parent_id"] == parent_id for row in children))
        self.assertIn("backend", {row["owner_session"] for row in children})
        self.assertIn("frontend", {row["owner_session"] for row in children})

    def test_intent_controls_dispatch(self) -> None:
        dispatched = []

        def dispatch(task_id: str, prompt: str) -> None:
            dispatched.append((task_id, prompt))

        self.intakes.create_intake(
            "分析 API",
            "analyze",
            [],
            "analysis-only",
            dispatcher=dispatch,
        )
        self.assertEqual([], dispatched)
        result = self.intakes.create_intake(
            "实现 API 修复",
            "implement",
            [],
            "implement",
            dispatcher=dispatch,
        )
        self.assertEqual(result["task_id"], dispatched[0][0])

    def test_unsafe_environment_needs_input_without_task(self) -> None:
        result = self.intakes.create_intake(
            "发布到生产",
            "implement_and_deploy_dev",
            [],
            "unsafe",
            advanced={"environment": "pro"},
        )
        self.assertEqual("NEEDS_INPUT", result["status"])
        self.assertEqual([], result["tasks"])

    def test_analysis_intent_survives_reopen_without_prompt_markers(self) -> None:
        for priority in (0, 1):
            with self.subTest(priority=priority):
                result = self.intakes.create_intake(
                    "P{} Fix a frontend component".format(priority),
                    "analyze", [], "analysis-p{}".format(priority),
                    advanced={"scope_summary": "bounded component detail " * 40},
                )
                reopened = ControlPlane(Database(self.root / "control.sqlite3"))
                task = reopened.get_task(result["task_id"])
                self.assertEqual("analysis-only", task["authorization_policy"])
                self.assertNotIn("只分析", task["title"] + task["scope_summary"])
                self.assertFalse(RunManager._eligible_for_managed_dispatch(task))
                manager = RunManager(reopened, self.root)
                with patch.object(manager, "dispatch", side_effect=AssertionError("analysis must not dispatch")):
                    self.assertEqual([], manager.reconcile_dispatch_queue()["claimed"])
                self.assertEqual([], reopened.db.all("SELECT id FROM runs"))

    def test_reopen_migrates_pre_policy_analysis_intake(self) -> None:
        result = self.intakes.create_intake(
            "Inspect a component",
            "analyze",
            [],
            "legacy-analysis-policy",
        )
        self.control.db.execute(
            "UPDATE tasks SET authorization_policy='normal' WHERE id=?",
            (result["task_id"],),
        )
        reopened = ControlPlane(Database(self.root / "control.sqlite3"))
        task = reopened.get_task(result["task_id"])
        self.assertEqual("analysis-only", task["authorization_policy"])
        self.assertFalse(RunManager._eligible_for_managed_dispatch(task))

    def test_analysis_split_marks_parent_and_every_child(self) -> None:
        result = self.intakes.create_intake(
            "P0 Update frontend and backend API across the full stack",
            "analyze", [], "analysis-split", dispatcher=lambda *_: self.fail("analysis dispatch"),
        )
        self.assertGreaterEqual(len(result["tasks"]), 3)
        for item in result["tasks"]:
            task = self.control.get_task(item["id"])
            self.assertEqual("analysis-only", task["authorization_policy"])
            self.assertFalse(RunManager._eligible_for_managed_dispatch(task))

    def test_analysis_retry_never_dispatches_a_retained_draft(self) -> None:
        result = self.intakes.create_intake(
            "P0 Fix a frontend component", "analyze", [], "analysis-retry",
        )
        self.control.db.execute("UPDATE intakes SET status='FAILED' WHERE id=?", (result["id"],))
        retried = self.intakes.retry_intake(
            result["id"], dispatcher=lambda *_: self.fail("analysis retry dispatch"),
        )
        self.assertEqual("ROUTED", retried["status"])
        self.assertEqual(result["task_id"], retried["task_id"])
        self.assertEqual("analysis-only", self.control.get_task(result["task_id"])["authorization_policy"])

    def test_implementation_keeps_execution_authorization(self) -> None:
        dispatched = []
        result = self.intakes.create_intake(
            "P0 Fix a frontend component", "implement", [], "authorized-implementation",
            dispatcher=lambda task_id, _prompt: dispatched.append(task_id),
        )
        task = self.control.get_task(result["task_id"])
        self.assertEqual("normal", task["authorization_policy"])
        self.assertEqual([task["id"]], dispatched)
        self.assertTrue(RunManager._eligible_for_managed_dispatch(task))

    def test_invalid_attachment_is_rejected_and_key_can_be_reused(self) -> None:
        with self.assertRaisesRegex(IntakeError, "不支持"):
            self.intakes.create_intake(
                "运行附件",
                "analyze",
                [memory_upload("../../run.sh", "application/x-sh", b"#!/bin/sh")],
                "bad-upload",
            )
        self.assertEqual(
            0,
            self.control.db.one("SELECT COUNT(*) AS count FROM intakes")["count"],
        )
        valid = self.intakes.create_intake(
            "改成安全附件",
            "analyze",
            [memory_upload("safe.png", "image/png", PNG)],
            "bad-upload",
        )
        self.assertEqual("ROUTED", valid["status"])

    def test_attachment_integrity_is_checked_on_read(self) -> None:
        result = self.intakes.create_intake(
            "读取附件",
            "analyze",
            [memory_upload("safe.png", "image/png", PNG)],
            "read",
        )
        attachment = result["attachments"][0]
        metadata, body = self.intakes.read_attachment(result["id"], attachment["id"])
        self.assertEqual(PNG, body)
        self.assertEqual("image/png", metadata["mime"])
        internal = self.intakes.get_intake(result["id"], include_internal=True)
        path = self.root / internal["attachments"][0]["local_path"]
        path.write_bytes(PNG + b"tampered")
        with self.assertRaisesRegex(IntakeError, "状态异常"):
            self.intakes.read_attachment(result["id"], attachment["id"])

    def test_real_planner_interface_is_explicit_and_xhigh(self) -> None:
        adapter = CodexPlannerAdapter(self.root)
        self.assertEqual("codex-exec", adapter.name)
        planned = {"title": "synthetic"}
        output = json.dumps(
            {"item": {"type": "agent_message", "text": json.dumps(planned)}}
        )
        attachment_path = str(self.root / "private-attachment.txt")
        with patch(
            "qingtian_engine.intake.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""),
        ) as run:
            self.assertEqual(
                planned,
                adapter.plan(
                    "plan a synthetic task",
                    "analyze",
                    [
                        {
                            "id": "attachment-1",
                            "name": "request.txt",
                            "mime": "text/plain",
                            "size": 10,
                            "sha256": "a" * 64,
                            "local_path": attachment_path,
                        }
                    ],
                    {"planner_model": "gpt-6-astra", "planner_reasoning": "xhigh", "planner_speed": "standard"},
                ),
            )
        command = run.call_args.args[0]
        self.assertEqual("gpt-6-astra", command[command.index("-m") + 1])
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn("--ephemeral", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertNotIn(attachment_path, run.call_args.kwargs["input"])

    def test_repository_must_be_registered_and_receipt_uses_project_name(self) -> None:
        outside = tempfile.TemporaryDirectory(prefix="qingtian-project-")
        self.addCleanup(outside.cleanup)
        repository = Path(outside.name)
        (repository / ".git").mkdir()
        register_project(
            "sample", repository, "dev", ["frontend"], data_dir=self.root
        )

        result = self.intakes.create_intake(
            "Analyze the frontend component",
            "analyze",
            [],
            "registered-project",
            advanced={"repository": "sample"},
        )
        self.assertEqual("sample", result["draft"]["repository"])
        self.assertEqual("dev", result["draft"]["base_branch"])
        self.assertNotIn(str(repository), json.dumps(result))

        unregistered = Path(outside.name).parent / "unregistered-project"
        unregistered.mkdir(exist_ok=True)
        self.addCleanup(lambda: unregistered.rmdir() if unregistered.exists() else None)
        blocked = self.intakes.create_intake(
            "Analyze another frontend component",
            "analyze",
            [],
            "unregistered-project",
            advanced={"repository": str(unregistered)},
        )
        self.assertEqual("NEEDS_INPUT", blocked["status"])
        self.assertEqual([], blocked["tasks"])

    def test_static_composer_contract(self) -> None:
        static = Path(__file__).parents[2] / "qingtian_engine" / "static"
        page = (static / "index.html").read_text(encoding="utf-8")
        app = (static / "app.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="brand-lockup"', page)
        self.assertNotIn('class="brand-mark"', page)
        self.assertIn(".brand-lockup {", styles)
        self.assertIn('id="intakeForm"', page)
        self.assertIn('id="intakeDialog"', page)
        self.assertNotIn('id="intakeTimeline"', page)
        self.assertNotIn("把事情直接告诉擎天", page)
        self.assertNotIn("先收件 · 后路由", page)
        self.assertIn('id="attachmentInput"', page)
        self.assertIn('id="advancedSettings"', page)
        self.assertIn("高级设置", page)
        self.assertIn('value="analyze"', page)
        self.assertIn('value="implement"', page)
        self.assertIn('value="implement_and_deploy_dev"', page)
        self.assertIn('"paste"', app)
        self.assertIn('"dragover"', app)
        self.assertIn('"drop"', app)
        self.assertIn("pendingFiles", app)
        self.assertNotIn("loadIntakes", app)
        self.assertNotIn("routedReceipt", app)
        self.assertNotIn("renderSending", app)
        self.assertNotIn("innerHTML", app)
        self.assertIn("new EventSource", app)
        self.assertIn("lastEventId=", app)
        self.assertIn('"visibilitychange"', app)
        self.assertIn("30000", app)
        self.assertNotIn("setInterval(refresh, 3000)", app)
        self.assertIn('class="state-badge"', page)
        self.assertIn('id="interventionPanel"', page)
        self.assertIn('id="systemDetailsDialog"', page)
        self.assertIn('id="mobileActionBar"', page)
        self.assertIn('class="action-summary"', page)
        self.assertIn("renderInterventions", app)
        self.assertIn("complete-human-action", app)
        self.assertIn("remind-external", app)
        self.assertNotIn("HUMAN ACTION", page)
        self.assertIn(".layout.has-user-actions", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertIn("@keyframes badge-shimmer", styles)


if __name__ == "__main__":
    unittest.main()
