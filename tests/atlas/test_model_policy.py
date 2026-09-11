from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qingtian_engine.config import load_policy, runtime_policy
from tests.atlas.capability_fixture import advertised_capabilities
from qingtian_engine.db import Database, SCHEMA
from qingtian_engine.intake import CodexPlannerAdapter, DeterministicPlannerAdapter, IntakeError, IntakeService
from qingtian_engine.runner import RunManager
from qingtian_engine.service import ControlPlane
from qingtian_engine.worker_entry import build_codex_command, run_worker


class ModelPolicyTest(unittest.TestCase):
    def setUp(self):
        capability = advertised_capabilities()
        capability.start()
        self.addCleanup(capability.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="qingtian-model-policy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = ControlPlane(Database(self.root / "control.sqlite3"))
        self.env = patch.dict(os.environ, {"QINGTIAN_MODEL": "gpt-6-astra", "QINGTIAN_REASONING": "xhigh"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def assert_selection(self, selection, model="gpt-6-astra", effort="xhigh"):
        self.assertEqual(model, selection["model"])
        self.assertEqual(effort, selection["reasoning"])

    def test_configuration_drives_task_session_dashboard_and_reroute(self):
        task = self.service.create_task("Synthetic model consistency")
        self.assert_selection(task)
        self.service.upsert_session("synthetic", "test", "test", "cli", "test", "fixture", model="gpt-6-astra", reasoning="xhigh")
        self.assert_selection(self.service.db.one("SELECT * FROM sessions WHERE id='synthetic'"))
        self.assert_selection(self.service.dashboard_payload()["policy"])
        with patch.dict(os.environ, {"QINGTIAN_MODEL": "gpt-5.6-sol", "QINGTIAN_REASONING": "high"}):
            self.assert_selection(self.service.reroute(task["id"]))

    def test_explicit_selection_beats_environment_and_has_no_silent_clamping(self):
        task = self.service.create_task("Explicit task", model="gpt-5.6-sol", reasoning="high")
        self.assert_selection(task, "gpt-5.6-sol", "high")
        low = self.service.create_task("Explicit low task", model="gpt-6-astra", reasoning="low")
        self.assertEqual("low", low["reasoning"])
        for model in ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"):
            with self.assertRaisesRegex(ValueError, "MODEL_POLICY"):
                self.service.create_task("Below floor", model=model)
        policy = load_policy()
        policy["minimum_reasoning"] = "low"
        selected = runtime_policy("low", policy=policy, explicit_reasoning=True)
        self.assertEqual("low", selected.reasoning)

    def test_invalid_environment_and_explicit_settings_fail_closed(self):
        for settings in ({"QINGTIAN_MODEL": ""}, {"QINGTIAN_MODEL": "bad model --flag"}, {"QINGTIAN_REASONING": "invalid"}):
            with self.subTest(settings=settings), patch.dict(os.environ, settings):
                with self.assertRaises(ValueError):
                    runtime_policy()
        for model, effort in (("", "high"), ("ok", "bogus"), (None, 5)):
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                self.service.create_task("Invalid choice", model=model, reasoning=effort)
        self.assertEqual([], self.service.list_tasks())

    def test_policy_path_is_explicit_and_missing_path_does_not_fallback(self):
        custom = load_policy()
        custom["minimum_reasoning"] = "medium"
        path = self.root / "policy.json"
        path.write_text(json.dumps(custom))
        with patch.dict(os.environ, {"QINGTIAN_POLICY_PATH": str(path)}):
            self.assertEqual("medium", load_policy()["minimum_reasoning"])
        with patch.dict(os.environ, {"QINGTIAN_POLICY_PATH": str(self.root / "missing.json")}):
            with self.assertRaises(FileNotFoundError):
                load_policy()

    def test_existing_run_migration_preserves_history_without_inventing_models(self):
        path = self.root / "legacy.sqlite3"
        legacy_schema = SCHEMA.replace(
            "    model TEXT NOT NULL DEFAULT '',\n    reasoning TEXT NOT NULL DEFAULT '',\n    speed TEXT NOT NULL DEFAULT '',\n", ""
        )
        with sqlite3.connect(path) as connection:
            connection.executescript(legacy_schema)
            connection.execute("INSERT INTO tasks(id,idempotency_key,title,created_at,updated_at) VALUES('old','old','Legacy','old','old')")
            connection.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,session_id,created_at,status) VALUES('run-old','old',1,'cli','historical summary','historical-session','old','DONE')")
        connection.close()
        database = Database(path)
        database.initialize()
        run = database.one("SELECT * FROM runs WHERE id='run-old'")
        self.assertEqual("historical summary", run["command_summary"])
        self.assertEqual("historical-session", run["session_id"])
        self.assertEqual("DONE", run["status"])
        self.assertEqual(("", "", ""), (run["model"], run["reasoning"], run["speed"]))

    def test_intake_and_split_children_keep_explicit_selection(self):
        intake = IntakeService(self.service, self.root)
        for index, text in enumerate(("Implement a frontend modal", "同时实现前端页面以及后端 API migration")):
            result = intake.create_intake(text, "analyze", [], "synthetic-" + str(index),
                advanced={"model": "gpt-6-astra", "reasoning": "xhigh"})
            self.assertEqual("ROUTED", result["status"])
            self.assert_selection(result["draft"])
            for task in result["tasks"]:
                self.assert_selection(task)
            if index:
                self.assertGreater(len(result["tasks"]), 1)
        with self.assertRaises(IntakeError):
            intake.create_intake("Invalid model setting", "analyze", [], "invalid", advanced={"reasoning": "invalid"})

    def test_real_planner_command_capture_uses_same_selection(self):
        draft = DeterministicPlannerAdapter().plan("Synthetic", "analyze", [], {})
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(draft)}})
        with patch("qingtian_engine.intake.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="")) as process:
            CodexPlannerAdapter(self.root).plan("Synthetic", "analyze", [], {})
        command = process.call_args.args[0]
        self.assertEqual("gpt-6-astra", command[command.index("-m") + 1])
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn("read-only", command)
        self.assertEqual(1, process.call_count)

    def test_unavailable_planner_is_failure_not_model_fallback(self):
        with patch("qingtian_engine.intake.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="model is unavailable")) as process:
            with self.assertRaisesRegex(IntakeError, "unavailable"):
                CodexPlannerAdapter(self.root).plan("Synthetic", "analyze", [], {})
        self.assertEqual(1, process.call_count)
        self.assertIn("gpt-6-astra", process.call_args.args[0])

    def test_worker_and_resume_command_ignore_changed_global_defaults(self):
        task = self.service.create_task("Pinned worker command")
        task.update(worktree=str(self.root), speed="standard")
        with patch.dict(os.environ, {"QINGTIAN_MODEL": "gpt-5.6-sol", "QINGTIAN_REASONING": "high"}):
            for resume in (False, True):
                command = build_codex_command(task, "synthetic-session", resume=resume)
                self.assertEqual("gpt-6-astra", command[command.index("-m") + 1])
                self.assertIn('model_reasoning_effort="xhigh"', command)
                self.assertIn('service_tier="default"', command)
                self.assertNotIn("fast_mode", command)
                self.assertEqual(resume, "resume" in command)
                if resume:
                    self.assertEqual(["synthetic-session", "-"], command[-2:])

    def dispatch_fixture(self):
        task = self.service.create_task("Pinned dispatched task", state="PLANNED")
        prompt = self.root / "input.txt"
        prompt.write_text("Synthetic, no real model invocation")
        manager = RunManager(self.service, self.root)
        with patch.object(manager, "_resolve_dispatch_repository", side_effect=lambda task, **kwargs: task), patch("qingtian_engine.runner.subprocess.Popen", return_value=SimpleNamespace(pid=os.getpid())):
            with patch.dict(os.environ, {"QINGTIAN_MODEL": "gpt-5.6-sol", "QINGTIAN_REASONING": "high"}):
                dry = manager.dispatch(task["id"], prompt, dry_run=True)
                self.assert_selection(dry)
                run = manager.dispatch(task["id"], prompt)
        return task, run, manager

    def test_dispatch_freezes_run_settings_and_resume_uses_same_choice(self):
        task, run, manager = self.dispatch_fixture()
        self.assert_selection(run)
        self.assertIn("gpt-6-astra", run["command_summary"])
        self.assertIn("reasoning=xhigh", run["command_summary"])
        self.service.db.execute("UPDATE runs SET status='FAILED', session_id='synthetic-session' WHERE id=?", (run["id"],))
        self.service.db.execute("UPDATE tasks SET model='gpt-5.6-sol', reasoning='high' WHERE id=?", (task["id"],))
        with patch.object(manager, "_resolve_dispatch_repository", side_effect=lambda task, **kwargs: task), patch("qingtian_engine.runner.subprocess.Popen", return_value=SimpleNamespace(pid=os.getpid())), patch.dict(os.environ, {"QINGTIAN_MODEL": "gpt-5.6-sol", "QINGTIAN_REASONING": "high"}):
            with self.assertRaisesRegex(ValueError, "MODEL_PINNING"):
                manager.dispatch(task["id"], self.root / "input.txt", resume=True)
            self.service.db.execute("UPDATE tasks SET model=?, reasoning=? WHERE id=?", (run["model"], run["reasoning"], task["id"]))
            resumed = manager.dispatch(task["id"], self.root / "input.txt", resume=True)
        self.assert_selection(resumed)
        self.assertEqual("synthetic-session", resumed["session_id"])

    def test_worker_process_uses_run_snapshot_and_fails_without_fallback(self):
        task, run, manager = self.dispatch_fixture()
        self.service.db.execute("UPDATE tasks SET model='gpt-5.6-sol', reasoning='high' WHERE id=?", (task["id"],))
        prompt = self.root / "worker-input.txt"
        prompt.write_text("Only a synthetic failure fixture")
        stdin = io.StringIO()
        captured_prompt = []
        stdin.close = lambda: captured_prompt.append(stdin.getvalue())
        fake = SimpleNamespace(stdin=stdin, stdout=iter([]), wait=lambda: 1)
        args = argparse.Namespace(db=str(self.service.db.path), data_dir=str(self.root), task=task["id"], run=run["id"], prompt_file=str(prompt), resume=False)
        with patch("qingtian_engine.worker_entry._validate_registered_workspace", return_value=self.root), patch("qingtian_engine.worker_entry.knowledge_prompt", return_value="No knowledge fixture"), patch("qingtian_engine.worker_entry.subprocess.Popen", return_value=fake) as process:
            with self.assertRaisesRegex(ValueError, "MODEL_PINNING"):
                run_worker(args)
            process.assert_not_called()
            self.service.db.execute("UPDATE tasks SET model=?, reasoning=? WHERE id=?", (run["model"], run["reasoning"], task["id"]))
            self.assertEqual(1, run_worker(args))
        self.assertEqual(1, process.call_count)
        command = process.call_args.args[0]
        self.assertIn("gpt-6-astra", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn("model=gpt-6-astra，reasoning=xhigh", captured_prompt[0])
        persisted = self.service.db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual("FAILED", persisted["status"])
        self.assert_selection(persisted)

    def test_all_seven_execution_fields_are_checked_before_historical_resume(self):
        from qingtian_engine.execution_parameters import require_same_execution_target
        changes = {"model": "gpt-5.6-sol", "reasoning": "low", "speed": "fast",
                   "worker_type": "qa", "owner_session": "different-owner", "branch": "different-branch",
                   "worktree": "/synthetic/different-worktree"}
        task, run, manager = self.dispatch_fixture()
        native = self.service.get_task(task["id"])
        snapshot = self.service.db.one("SELECT * FROM admission_run_targets WHERE run_id=?", (run["id"],))
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = dict(native, **{field: value})
                with self.assertRaisesRegex(ValueError, "seven-field"):
                    require_same_execution_target(self.service.db, changed, run)
        require_same_execution_target(self.service.db, native, run)
        self.assertEqual(snapshot, self.service.db.one("SELECT * FROM admission_run_targets WHERE run_id=?", (run["id"],)))

    def test_legacy_session_observation_does_not_invent_a_model_tuple(self):
        self.service.upsert_session("unknown", "Synthetic", "test", "cli", "test", "fixture")
        observed = self.service.db.one("SELECT * FROM sessions WHERE id='unknown'")
        self.assertEqual(("", ""), (observed["model"], observed["reasoning"]))


if __name__ == "__main__":
    unittest.main()
