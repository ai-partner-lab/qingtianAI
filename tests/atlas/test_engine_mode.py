"""Synthetic engine mode checks: no real HTTP server, subprocess or Codex."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from qingtian_engine import cli, server
from qingtian_engine.coordinator import RecoveryCoordinator
from qingtian_engine.db import Database
from qingtian_engine.intake import IntakeService
from qingtian_engine.runner import RunManager
from qingtian_engine.runtime_mode import background_cycle
from qingtian_engine.service import ControlPlane


class EngineModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {
            "QINGTIAN_ENGINE_HOME": str(self.root / "default-state"),
            "QINGTIAN_KNOWLEDGE_CONFIG": str(self.root / "no-knowledge.json"),
            "QINGTIAN_PROJECTS_CONFIG": str(self.root / "no-projects.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.service = ControlPlane(Database(self.root / "fixture.sqlite3"))
        self.manager = RunManager(self.service, self.root)
        self.spawn_guard = patch(
            "subprocess.Popen", side_effect=AssertionError("real subprocess forbidden")
        )
        self.spawn_guard.start()
        self.addCleanup(self.spawn_guard.stop)
        for name in ("service", "manager", "intakes", "watchdog_health", "coordinator", "engine_mode", "workspace"):
            if name in server.ControlPlaneHandler.__dict__:
                previous = getattr(server.ControlPlaneHandler, name)
                self.addCleanup(setattr, server.ControlPlaneHandler, name, previous)
            else:
                self.addCleanup(lambda field=name: (
                    delattr(server.ControlPlaneHandler, field)
                    if field in server.ControlPlaneHandler.__dict__ else None
                ))

    def handler(self, path, payload=None, mode="manual"):
        handler = object.__new__(server.ControlPlaneHandler)
        handler.path = path
        handler.headers = {"Host": "127.0.0.1:18766"}
        handler.server = Mock(server_address=("127.0.0.1", 18766))
        handler.service = self.service
        handler.manager = self.manager
        handler.intakes = IntakeService(self.service, self.root)
        handler.engine_mode = mode
        handler.workspace = str(self.root / "workspace")
        handler.coordinator = None
        handler.watchdog_health = {
            "_last_success_monotonic": time.monotonic(), "consecutive_errors": 0
        }
        handler._read_json = Mock(return_value=payload or {})
        handler._json = Mock()
        return handler

    def test_manual_background_does_not_claim_p0_or_p1(self):
        for priority in (0, 1):
            self.service.create_task(
                "Synthetic pending task", priority=priority, state="PLANNED",
                evidence_profile="artifact", worker_type="cli",
            )
        coordinator = RecoveryCoordinator(self.service, self.manager)
        before = self.service.db.all("SELECT id,state FROM tasks ORDER BY id")
        with patch.object(coordinator, "tick") as tick, patch.object(
            self.manager, "scheduler_tick"
        ) as scheduler, patch.object(self.manager, "dispatch") as dispatch:
            for _ in range(3):
                result = background_cycle(self.manager, coordinator, "manual")
            tick.assert_not_called()
            scheduler.assert_not_called()
            dispatch.assert_not_called()
        self.assertEqual(before, self.service.db.all("SELECT id,state FROM tasks ORDER BY id"))
        self.assertEqual([], self.service.db.all("SELECT * FROM runs"))
        self.assertEqual([], result["dispatch"]["claimed"])
        self.assertFalse(result["automatic_dispatch"])

    def test_manual_stale_worker_reconciliation_does_not_recover_or_backfill(self):
        task = self.service.create_task("Synthetic stale queue", evidence_profile="artifact")
        self.service.transition(task["id"], "QUEUED", force=True)
        self.service.db.execute(
            "INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) "
            "VALUES('synthetic-stale',?,1,'cli','fixture','QUEUED','2000-01-01T00:00:00+00:00')",
            (task["id"],),
        )
        with patch.object(self.manager, "recover_infrastructure_failure") as recover, patch.object(
            self.manager, "dispatch_verification_backfill"
        ) as backfill, patch.object(self.manager, "dispatch") as dispatch:
            background_cycle(self.manager, None, "manual")
            background_cycle(self.manager, None, "manual")
            recover.assert_not_called()
            backfill.assert_not_called()
            dispatch.assert_not_called()
        self.assertEqual(1, len(self.service.db.all("SELECT * FROM runs")))
        self.assertEqual("WAITING", self.service.get_task(task["id"])["state"])

    def test_auto_explicitly_uses_coordinator(self):
        manager, coordinator = Mock(), Mock()
        coordinator.tick.return_value = {"fixture": True}
        self.assertEqual({"fixture": True}, background_cycle(manager, coordinator, "auto"))
        coordinator.tick.assert_called_once_with(max_new=1, max_active=3)
        manager.scheduler_tick.assert_not_called()

    def test_auto_without_coordinator_keeps_legacy_scheduler(self):
        manager = Mock()
        background_cycle(manager, None, "auto")
        manager.scheduler_tick.assert_called_once_with(max_new=1, max_active=3)

    def test_invalid_mode_fails_before_initializing_data(self):
        target = self.root / "not-created"
        with self.assertRaises(ValueError):
            server.serve("127.0.0.1", 0, target, mode="implicit-auto")
        self.assertFalse(target.exists())

    def test_startup_and_watchdog_both_use_default_manual_mode(self):
        # Exercise serve's real startup and watchdog bodies without binding a
        # socket, spawning a thread/process, or calling an actual coordinator.
        fake_server = Mock()
        fake_event = Mock()
        fake_event.wait.side_effect = [False, True]
        coordinator = Mock()

        def make_thread(*, target, **_kwargs):
            thread = Mock()
            thread.start.side_effect = target
            return thread

        with patch.object(server, "LoopbackThreadingHTTPServer", return_value=fake_server), patch.object(
            server.threading, "Event", return_value=fake_event
        ), patch.object(server.threading, "Thread", side_effect=make_thread), patch.object(
            server.signal, "signal"
        ), patch.object(server, "RecoveryCoordinator", return_value=coordinator), patch.dict(
            os.environ, {"QINGTIAN_RECOVERY_ENABLED": "1", "QINGTIAN_ENGINE_MODE": "auto"}
        ), patch.object(server, "background_cycle", wraps=background_cycle) as cycle:
            server.serve("127.0.0.1", 0, self.root / "server-fixture", workspace=self.root)
        self.assertEqual(2, cycle.call_count)
        self.assertTrue(all(call.args[2] == "manual" for call in cycle.call_args_list))
        coordinator.tick.assert_not_called()
        self.assertEqual("manual", server.ControlPlaneHandler.engine_mode)
        self.assertFalse((self.root / "server-fixture" / "run" / "server.pid").exists())
        fake_server.server_close.assert_called_once()

    def test_health_identifies_writable_manual_runtime(self):
        handler = self.handler("/api/health")
        handler.do_GET()
        status, payload = handler._json.call_args.args
        self.assertEqual(200, status)
        self.assertEqual("manual", payload["mode"])
        self.assertFalse(payload["automatic_dispatch"])
        self.assertFalse(payload["read_only"])
        self.assertEqual(str(self.root), payload["data_dir"])
        self.assertEqual(str(self.root / "workspace"), payload["workspace"])

    def test_startup_explicit_auto_enables_both_scheduler_cycles(self):
        fake_event = Mock()
        fake_event.wait.side_effect = [False, True]
        coordinator = Mock()
        coordinator.tick.return_value = {"dispatch": {"claimed": []}}

        def make_thread(*, target, **_kwargs):
            thread = Mock()
            thread.start.side_effect = target
            return thread

        with patch.object(server, "LoopbackThreadingHTTPServer"), patch.object(
            server.threading, "Event", return_value=fake_event
        ), patch.object(server.threading, "Thread", side_effect=make_thread), patch.object(
            server.signal, "signal"
        ), patch.object(server, "RecoveryCoordinator", return_value=coordinator), patch.dict(
            os.environ, {"QINGTIAN_RECOVERY_ENABLED": "1"}
        ):
            server.serve("127.0.0.1", 0, self.root / "auto-fixture", mode="auto")
        self.assertEqual(2, coordinator.tick.call_count)
        self.assertTrue(all(call.kwargs == {"max_new": 1, "max_active": 3}
                            for call in coordinator.tick.call_args_list))

    def test_health_identifies_auto_runtime(self):
        handler = self.handler("/api/health", mode="auto")
        handler.do_GET()
        self.assertTrue(handler._json.call_args.args[1]["automatic_dispatch"])

    def test_loopback_hostnames_and_ipv6_host_are_accepted(self):
        for host in ("127.0.0.1:18766", "localhost:18766", "[::1]:18766"):
            handler = self.handler("/api/health")
            handler.headers = {"Host": host}
            handler.do_GET()
            self.assertEqual(200, handler._json.call_args.args[0])

    def test_rebinding_host_and_wrong_port_are_rejected_before_mutation(self):
        for host in ("attacker.invalid:18766", "127.0.0.1:80", "", "localhost:bad",
                     "attacker@127.0.0.1:18766", "127.0.0.1:18766/path"):
            handler = self.handler("/api/tasks", {"title": "Must not create"})
            handler.headers = {"Host": host, "Origin": "http://" + host}
            handler.do_POST()
            self.assertEqual(403, handler._json.call_args.args[0])
            handler._read_json.assert_not_called()
        self.assertEqual([], self.service.list_tasks())

    def test_cross_origin_is_rejected_but_no_origin_local_client_works(self):
        handler = self.handler("/api/tasks", {"title": "Must not create"})
        handler.headers["Origin"] = "https://attacker.invalid"
        handler.do_POST()
        self.assertEqual(403, handler._json.call_args.args[0])
        handler._read_json.assert_not_called()
        handler = self.handler("/api/tasks", {"title": "Synthetic local client"})
        handler.do_POST()
        self.assertEqual(201, handler._json.call_args.args[0])

    def test_port_collision_precedes_any_automatic_scheduler_tick(self):
        coordinator = Mock()
        with patch.object(server, "LoopbackThreadingHTTPServer", side_effect=OSError("fixture port busy")), patch.object(
            server, "RecoveryCoordinator", return_value=coordinator
        ), patch.dict(os.environ, {"QINGTIAN_RECOVERY_ENABLED": "1"}), self.assertRaises(OSError):
            server.serve("127.0.0.1", 0, self.root / "occupied-port", mode="auto")
        coordinator.tick.assert_not_called()

    def test_explicit_api_dispatch_is_available_in_manual_mode(self):
        task = self.service.create_task("Synthetic explicit task")
        handler = self.handler(
            "/api/tasks/{}/dispatch".format(task["id"]),
            {"instruction": "Only synthetic fixture", "resume": False},
        )
        captured = []

        def fake_dispatch(task_id, prompt, resume=False):
            captured.append((task_id, prompt, prompt.read_text(), resume))
            return {"id": "synthetic-receipt"}

        with patch.object(self.manager, "dispatch", side_effect=fake_dispatch):
            handler.do_POST()
        self.assertEqual(200, handler._json.call_args.args[0])
        self.assertEqual("synthetic-receipt", handler._json.call_args.args[1]["run"]["id"])
        self.assertEqual((task["id"], "Only synthetic fixture", False),
                         (captured[0][0], captured[0][2], captured[0][3]))
        self.assertFalse(captured[0][1].exists())

    def test_api_dispatch_requires_instruction_and_boolean_resume(self):
        for payload in ({}, {"instruction": " "}, {"instruction": 3},
                        {"instruction": "fixture", "resume": "false"}):
            handler = self.handler("/api/tasks/no-task/dispatch", payload)
            with patch.object(self.manager, "dispatch") as dispatch:
                handler.do_POST()
                dispatch.assert_not_called()
            self.assertEqual(400, handler._json.call_args.args[0])

    def test_deployment_flag_rejects_non_boolean_before_task_creation(self):
        for invalid in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=invalid):
                handler = self.handler("/api/tasks", {"title": "Synthetic invalid flag", "requires_deploy": invalid})
                handler.do_POST()
                self.assertEqual(400, handler._json.call_args.args[0])
                self.assertEqual([], self.service.list_tasks())

    def test_plain_api_task_creation_does_not_dispatch(self):
        handler = self.handler("/api/tasks", {"title": "Synthetic new task", "priority": 0})
        with patch.object(self.manager, "dispatch") as dispatch:
            handler.do_POST()
            dispatch.assert_not_called()
        self.assertEqual(201, handler._json.call_args.args[0])

    def test_explicit_api_auto_start_is_preserved_but_requires_boolean(self):
        for auto_start, expected_status, expected_calls in ((True, 201, 1), ("true", 400, 0)):
            handler = self.handler("/api/tasks", {
                "title": "Synthetic immediate task", "auto_start": auto_start,
                "instruction": "Only synthetic fixture",
            })
            with patch.object(self.manager, "dispatch") as dispatch:
                handler.do_POST()
                self.assertEqual(expected_calls, dispatch.call_count)
            self.assertEqual(expected_status, handler._json.call_args.args[0])

    def test_missing_intake_intent_is_analysis_only_in_manual_mode(self):
        handler = self.handler("/api/intakes")
        handler._read_multipart = Mock(return_value=(
            {"text": "分析测试方案", "idempotency_key": "synthetic-missing-intent"}, [], None
        ))
        with patch.object(self.manager, "dispatch") as dispatch:
            handler.do_POST()
            dispatch.assert_not_called()
        self.assertEqual(201, handler._json.call_args.args[0])
        self.assertEqual([], self.service.db.all("SELECT * FROM runs"))

    def test_explicit_intake_implement_keeps_dispatch_contract(self):
        handler = self.handler("/api/intakes")
        handler._read_multipart = Mock(return_value=(
            {"text": "修复 Profile 登录弹窗", "intent": "implement",
             "idempotency_key": "synthetic-explicit-intent"}, [], None
        ))
        with patch.object(self.manager, "dispatch", return_value={"id": "synthetic"}) as dispatch:
            handler.do_POST()
            dispatch.assert_called_once()
        self.assertEqual(201, handler._json.call_args.args[0])

    def test_init_never_imports_workspace_ledgers(self):
        target = self.root / "fresh-init"
        with patch.object(cli, "import_defaults") as importer, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, cli.main([
                "--data-dir", str(target), "--workspace", str(self.root), "init"
            ]))
        importer.assert_not_called()
        self.assertEqual({}, json.loads(out.getvalue())["imported"])
        self.assertEqual([], Database(target / "control-plane.sqlite3").all("SELECT * FROM tasks"))

    def test_bootstrap_import_requires_explicit_flag(self):
        for explicit in (False, True):
            with patch.object(cli, "import_defaults", return_value={}) as importer, patch.object(
                cli, "_which", return_value="synthetic-tool"
            ), redirect_stdout(io.StringIO()):
                args = ["--data-dir", str(self.root / "bootstrap"), "--workspace", str(self.root), "bootstrap"]
                if explicit:
                    args.append("--import-ledgers")
                self.assertEqual(0, cli.main(args))
                self.assertEqual(int(explicit), importer.call_count)

    def test_import_without_targets_is_rejected_before_database_creation(self):
        target = self.root / "not-created-import"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.main(["--data-dir", str(target), "import"])
        self.assertEqual(2, error.exception.code)
        self.assertFalse(target.exists())

    def test_import_defaults_requires_explicit_flag(self):
        with patch.object(cli, "import_defaults", return_value={}) as importer, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(["--data-dir", str(self.root / "import"), "import", "--defaults"]))
        importer.assert_called_once()

    def test_cli_start_defaults_manual_and_propagates_explicit_auto(self):
        for options, mode in (([], "manual"), (["--mode", "auto"], "auto")):
            with patch.object(cli, "start_server", return_value=0) as start:
                self.assertEqual(0, cli.main([
                    "--data-dir", str(self.root), "--workspace", str(self.root), "start", *options
                ]))
                self.assertEqual(mode, start.call_args.args[4])
                self.assertEqual(self.root, start.call_args.args[5])

    def test_start_server_forwards_mode_and_identity_to_child(self):
        with patch.object(cli, "_health_payload", return_value=None), patch.object(
            cli.subprocess, "call", return_value=0
        ) as child:
            self.assertEqual(0, cli.start_server(self.root, 18766, True, False, "manual", self.root))
        command = child.call_args.args[0]
        self.assertEqual("manual", command[command.index("--mode") + 1])
        self.assertEqual(str(self.root), command[command.index("--data-dir") + 1])
        self.assertEqual(str(self.root), command[command.index("--workspace") + 1])

    def test_existing_engine_mode_or_data_identity_mismatch_never_spawns(self):
        valid = {"ok": True, "service": "qingtian-engine", "mode": "manual",
                 "data_dir": str(self.root), "workspace": str(self.root)}
        for change in ({"mode": "auto"}, {"data_dir": str(self.root / "other")},
                       {"workspace": str(self.root / "other")}, {"mode": None},
                       {"service": "unrelated"}):
            with patch.object(cli, "_health_payload", return_value={**valid, **change}), patch.object(
                cli.subprocess, "call"
            ) as child, redirect_stderr(io.StringIO()):
                self.assertEqual(2, cli.start_server(self.root, 18766, True, False, "manual", self.root))
                child.assert_not_called()

    def test_matching_existing_engine_is_reused_without_spawn(self):
        payload = {"ok": True, "service": "qingtian-engine", "mode": "manual",
                   "data_dir": str(self.root), "workspace": str(self.root)}
        with patch.object(cli, "_health_payload", return_value=payload), patch.object(
            cli.subprocess, "call"
        ) as child, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.start_server(self.root, 18766, True, False, "manual", self.root))
            child.assert_not_called()

    def test_unknown_lock_owner_identity_is_explicitly_unverified(self):
        with patch.object(cli, "_health_payload", return_value=None), patch.object(
            cli, "active_instance_pid", return_value=123
        ), redirect_stderr(io.StringIO()) as warning, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.start_server(self.root, 18766, True, False, "manual", self.root))
        self.assertIn("unverified", warning.getvalue())
        self.assertIn("no mode change", warning.getvalue())

    def test_cli_explicit_dispatch_remains_available(self):
        prompt = self.root / "synthetic-prompt.txt"
        prompt.write_text("synthetic test only")
        with patch.object(cli.RunManager, "dispatch", return_value={"id": "synthetic"}) as dispatch, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main([
                "--data-dir", str(self.root / "explicit-cli"), "dispatch", "synthetic-task",
                "--prompt-file", str(prompt),
            ]))
        dispatch.assert_called_once_with("synthetic-task", prompt, False, False)

    def test_demo_is_synthetic_and_does_not_use_runner(self):
        with patch.object(cli, "RunManager") as manager:
            result = cli.create_demo(self.service, self.root, reset=True)
        manager.assert_not_called()
        self.assertTrue(result["synthetic"])
        self.assertFalse(result["paid_api_called"])
        self.assertEqual([], self.service.db.all("SELECT * FROM runs"))
        done = self.service.list_tasks(["DONE"])
        self.assertEqual(1, len(done))
        evidence = self.service.db.all("SELECT * FROM evidence WHERE task_id=?", (done[0]["id"],))
        self.assertIn("synthetic", evidence[0]["value"])


if __name__ == "__main__":
    unittest.main()
