"""Real loopback HTTP checks for the temporary, non-executing engine tour."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, redirect_stdout
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qingtian_engine import cli, entrypoint, server
from qingtian_engine.intake import DeterministicPlannerAdapter
from qingtian_engine.server import LoopbackThreadingHTTPServer


class TourReadonlyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qingtian-tour-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "tour"
        self.external = self.root / "outside-tour"
        self.external.mkdir()
        self.projects = self.external / "projects.local.json"
        self.knowledge = self.external / "knowledge.local.json"
        # Sentinels model inherited real configuration, without any real paths
        # or credentials. Reading either one is forbidden for the tour.
        self.projects.write_text('{"sentinel":"must-not-read"}', encoding="utf-8")
        self.knowledge.write_text('{"sentinel":"must-not-read"}', encoding="utf-8")
        environment = patch.dict(os.environ, {
            "QINGTIAN_ENGINE_HOME": str(self.external / "state"),
            "QINGTIAN_WORKSPACE": str(self.external),
            "QINGTIAN_INTAKE_PLANNER": "codex",
            "QINGTIAN_RECOVERY_ENABLED": "1",
            "QINGTIAN_PROJECTS_CONFIG": str(self.projects),
            "QINGTIAN_KNOWLEDGE_CONFIG": str(self.knowledge),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.guards = []
        for target in (
            "subprocess.Popen",
            "qingtian_engine.server.CodexPlannerAdapter",
            "qingtian_engine.server.RecoveryCoordinator",
            "qingtian_engine.server.background_cycle",
            "qingtian_engine.server.default_workspace",
            "qingtian_engine.runner.load_project_config",
            "qingtian_engine.intake.load_project_config",
            "qingtian_engine.knowledge.configured_task_context",
        ):
            guard = patch(target, side_effect=AssertionError("tour must not call " + target))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.service = cli.build_service(self.data)
        cli.create_demo(self.service, self.data, reset=False)

    def snapshot(self):
        with sqlite3.connect(self.data / "control-plane.sqlite3") as connection:
            return list(connection.iterdump())

    def request(self, httpd, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            connection.request("GET" if body is None else "POST", path,
                               body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    @contextmanager
    def running_tour(self):
        bound = threading.Event()
        captured = []
        failures = []
        tick_count = []
        real_event = threading.Event
        real_open = os.open

        class FastWatchdogEvent:
            def __init__(self):
                self.event = real_event()

            def set(self):
                self.event.set()

            def wait(self, timeout):
                tick_count.append(timeout)
                return self.event.wait(0.02)

        def factory(address, handler):
            self.assertEqual(address, ("127.0.0.1", 0))
            httpd = LoopbackThreadingHTTPServer(address, handler)
            captured.append(httpd)
            bound.set()
            return httpd

        def guarded_open(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                candidate = Path(os.fsdecode(path)).resolve()
                self.assertNotIn(candidate, {self.projects, self.knowledge})
            return real_open(path, *args, **kwargs)

        def run():
            try:
                server.serve("127.0.0.1", 0, self.data, synthetic_tour=True)
            except BaseException as exc:
                failures.append(exc)
                bound.set()

        with ExitStack() as stack:
            stack.enter_context(patch.object(server, "LoopbackThreadingHTTPServer", factory))
            stack.enter_context(patch.object(server.signal, "signal"))
            stack.enter_context(patch.object(server, "threading", SimpleNamespace(
                Event=FastWatchdogEvent, Thread=threading.Thread,
            )))
            stack.enter_context(patch("os.open", side_effect=guarded_open))
            # The handlers must refuse writes before any multipart/JSON parsing.
            stack.enter_context(patch.object(server.ControlPlaneHandler, "_read_json",
                                            side_effect=AssertionError("tour parsed JSON")))
            stack.enter_context(patch.object(server.ControlPlaneHandler, "_read_multipart",
                                            side_effect=AssertionError("tour parsed multipart")))
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(bound.wait(3), "tour never bound its loopback listener")
                if failures:
                    raise failures[0]
                httpd = captured[0]
                status, _ = self.request(httpd, "/api/health")
                self.assertEqual(status, 200)
                yield httpd, tick_count
            finally:
                if captured:
                    captured[0].shutdown()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "tour server did not stop")
                if failures:
                    raise failures[0]
        self.assertFalse((self.data / "run" / "server.pid").exists())
        for guard in self.guards:
            guard.assert_not_called()

    def test_tour_health_and_real_board_identify_read_only_synthetic_state(self):
        with self.running_tour() as (httpd, _):
            before = self.snapshot()
            status, raw = self.request(httpd, "/api/health")
            health = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertTrue(health["read_only"])
            self.assertTrue(health["synthetic"])
            self.assertEqual(health["mode"], "manual")
            self.assertFalse(health["automatic_dispatch"])
            self.assertEqual(Path(health["workspace"]), self.data)
            self.assertEqual(Path(health["data_dir"]), self.data)
            handler = httpd.RequestHandlerClass
            self.assertIsNot(handler, server.ControlPlaneHandler)
            self.assertFalse(server.ControlPlaneHandler.synthetic_tour)
            self.assertIsNone(handler.coordinator)
            self.assertIsInstance(handler.intakes.planner, DeterministicPlannerAdapter)
            status, raw = self.request(httpd, "/api/dashboard")
            self.assertEqual(status, 200)
            tasks = json.loads(raw)["tasks"]
            self.assertEqual(len(tasks), 5)
            for path in ("/", "/app.js", "/styles.css", "/api/report", "/api/intakes",
                         "/api/tasks/" + tasks[0]["id"]):
                self.assertEqual(self.request(httpd, path)[0], 200, path)
            self.assertEqual(self.snapshot(), before)

    def test_all_tour_posts_are_rejected_without_parsing_or_database_changes(self):
        with self.running_tour() as (httpd, _):
            before = self.snapshot()
            task_id = self.service.db.one("SELECT id FROM tasks LIMIT 1")["id"]
            paths = ["/api/intakes", "/api/intakes/synthetic/retry", "/api/tasks", "/unknown"]
            paths += ["/api/tasks/{}/{}".format(task_id, action) for action in (
                "dispatch", "cancel", "plan", "retry", "complete-human-action",
                "remind-external", "heartbeat",
            )]
            for path in paths:
                with self.subTest(path=path):
                    status, raw = self.request(httpd, path, body=b"not valid JSON or multipart",
                                               headers={"Content-Type": "application/json"})
                    self.assertEqual(status, 403)
                    self.assertEqual(json.loads(raw)["code"], "synthetic-tour-read-only")
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.service.db.all("SELECT * FROM runs"), [])
            self.assertEqual(list((self.data / "prompts").iterdir()), [])
            self.assertFalse((self.external / "state").exists())

    def test_tour_watchdog_never_reconciles_dispatches_or_loads_external_config(self):
        with self.running_tour() as (httpd, ticks):
            before = self.snapshot()
            deadline = time.monotonic() + 2
            while len(ticks) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(ticks), 3)
            health = json.loads(self.request(httpd, "/api/health")[1])
            self.assertTrue(health["watchdog"]["ok"])
            self.assertTrue(health["watchdog"]["scheduler"]["read_only"])
            self.assertEqual(health["watchdog"]["scheduler"]["dispatch"]["claimed"], [])
            self.assertEqual(self.snapshot(), before)

    def test_tour_rejects_auto_and_nonboolean_flags_before_creating_state(self):
        for options in ({"mode": "auto", "synthetic_tour": True},
                        {"synthetic_tour": "true"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                server.serve("127.0.0.1", 0, self.root / "not-created", **options)
            self.assertFalse((self.root / "not-created").exists())

    def test_entrypoint_passes_explicit_tour_flag_and_discards_temporary_state(self):
        with patch.object(server, "serve") as serve, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entrypoint.main(["tour", "--port", "18766"]), 0)
        self.assertTrue(serve.call_args.kwargs["synthetic_tour"])
        self.assertEqual(serve.call_args.kwargs["mode"], "manual")
        self.assertFalse(serve.call_args.args[2].exists())
        self.assertIn("read-only synthetic", output.getvalue())
        self.assertFalse((self.external / "state").exists())


if __name__ == "__main__":
    unittest.main()
