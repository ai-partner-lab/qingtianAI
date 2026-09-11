from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from qingtian_engine.db import Database
from qingtian_engine.server import ControlPlaneHandler, LoopbackThreadingHTTPServer
from qingtian_engine.service import ActionConflict, ControlPlane


class HumanActionCASTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qingtian-action-cas-")
        self.addCleanup(self.temp.cleanup)
        self.service = ControlPlane(Database(Path(self.temp.name) / "control.sqlite3"))
        self.task = self.service.create_task("Synthetic external action", state="WAITING")
        self.task = self.service.set_human_action(self.task["id"], "user", "test user", "Set the new redirect URI")

    def test_current_version_atomically_records_report_only(self):
        original = self.task
        with patch("subprocess.Popen", side_effect=AssertionError("completion must not dispatch")):
            result = self.service.complete_human_action(original["id"], original["action_version"])
        self.assertEqual("VERIFYING", result["state"])
        self.assertEqual("none", result["action_owner_kind"])
        self.assertEqual(original["authorization_policy"], result["authorization_policy"])
        self.assertEqual(original["requires_deploy"], result["requires_deploy"])
        self.assertEqual([], result["runs"])
        self.assertEqual([], result["evidence"])
        self.assertNotEqual(original["action_version"], result["action_version"])
        self.assertEqual(1, sum(event["event_type"] == "task.human_action_completed" for event in result["events"]))

    def test_stale_and_repeated_reports_preserve_current_action(self):
        original = self.task
        changed = self.service.set_human_action(original["id"], "user", "new owner", "Different URI", due="tomorrow")
        with self.assertRaises(ActionConflict):
            self.service.complete_human_action(original["id"], original["action_version"])
        self.assertEqual(changed, self.service.get_task(original["id"]))
        self.service.complete_human_action(changed["id"], changed["action_version"])
        with self.assertRaises(ActionConflict):
            self.service.complete_human_action(changed["id"], changed["action_version"])

    def test_failure_rolls_back_action_state_and_events(self):
        original = self.service.get_task(self.task["id"])
        with patch.object(self.service.db, "add_event", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.service.complete_human_action(original["id"], original["action_version"])
        self.assertEqual(original, self.service.get_task(original["id"]))

    def test_two_concurrent_reports_have_one_winner(self):
        barrier = threading.Barrier(3)
        outcomes = []
        def report():
            barrier.wait()
            try:
                self.service.complete_human_action(self.task["id"], self.task["action_version"])
                outcomes.append("ok")
            except ActionConflict:
                outcomes.append("conflict")
        threads = [threading.Thread(target=report) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(["conflict", "ok"], sorted(outcomes))

    def test_sensitive_paused_terminal_and_analysis_tasks_are_not_authorized(self):
        cases = [dict(action_sensitive=1), dict(state="PAUSED"), dict(state="DONE"),
                 dict(state="CANCELED"), dict(authorization_policy="analysis-only"),
                 dict(imported_from="legacy")]
        for index, settings in enumerate(cases):
            with self.subTest(settings=settings):
                task = self.service.create_task("Guard " + str(index), state="WAITING", action_owner_kind="user", action_text="guard")
                self.service.db.execute("UPDATE tasks SET " + ",".join(key + "=?" for key in settings) + " WHERE id=?", [*settings.values(), task["id"]])
                original = self.service.get_task(task["id"])
                with self.assertRaises(ValueError):
                    self.service.complete_human_action(task["id"], original["action_version"])
                self.assertEqual(original, self.service.get_task(task["id"]))

    def test_legacy_without_version_is_atomic_but_not_a_stale_read_contract(self):
        result = self.service.complete_human_action(self.task["id"])
        self.assertEqual("VERIFYING", result["state"])
        self.assertEqual([], result["runs"])

    def test_real_http_stale_version_returns_409_and_fresh_version_succeeds(self):
        handler = type("IsolatedActionHandler", (ControlPlaneHandler,), {"service": self.service, "coordinator": None})
        server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        def request(method, path, payload=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            try:
                connection.request(method, path, body=json.dumps(payload) if payload is not None else None,
                                   headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        path = "/api/tasks/" + self.task["id"]
        status, detail = request("GET", path)
        self.assertEqual(200, status)
        stale = detail["action_version"]
        changed = self.service.set_human_action(self.task["id"], "user", text="New requested URI")
        status, error = request("POST", path + "/complete-human-action", {"expected_action_version": stale})
        self.assertEqual(409, status)
        self.assertIn("changed", error["error"])
        self.assertEqual(changed, self.service.get_task(self.task["id"]))
        status, result = request("POST", path + "/complete-human-action", {"expected_action_version": changed["action_version"]})
        self.assertEqual(200, status)
        self.assertEqual("VERIFYING", result["state"])
        self.assertEqual([], result["runs"])


if __name__ == "__main__":
    unittest.main()
