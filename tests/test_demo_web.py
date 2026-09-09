from __future__ import annotations

from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import patch

from qingtian_core.cli import main, parser
from qingtian_core.capability_checks import _receipt
from qingtian_core.contracts import bundled_schema, validate
from qingtian_core.demo_web import DemoConflict, DemoExecutionError, DemoHTTPServer, DemoRequestHandler, DemoResetRequired, GuidedDemo, run_demo_web
from qingtian_core.models import TransitionError
from qingtian_core.store import QingtianStore


class GuidedDemoTest(unittest.TestCase):
    def test_failed_provider_retains_actual_state_and_requires_explicit_reset(self) -> None:
        with GuidedDemo() as demo:
            demo.advance(0)
            demo.advance(1)
            with patch("qingtian_core.demo_web.EchoProvider.generate", side_effect=RuntimeError("synthetic-provider-failure")) as provider:
                with self.assertRaises(DemoExecutionError):
                    demo.advance(2)
                failed = demo.snapshot()
                self.assertTrue(failed["requires_reset"])
                self.assertFalse(failed["done"])
                self.assertEqual(failed["step"], 2)
                self.assertEqual(failed["task"]["state"], "RUNNING")
                self.assertEqual(failed["records"]["runs"][0]["state"], "RUNNING")
                self.assertEqual(failed["error"]["code"], "guide_step_failed")
                self.assertNotIn("synthetic-provider-failure", json.dumps(failed))
                with self.assertRaises(DemoResetRequired):
                    demo.advance(2)
                provider.assert_called_once()
            reset = demo.reset()
            self.assertFalse(reset["requires_reset"])
            self.assertIsNone(reset["error"])
            self.assertFalse(reset["records"]["runs"])
            self.assertEqual(reset["task"]["state"], "DRAFT")

    def test_seven_steps_use_core_contracts_and_finish_with_bound_evidence(self) -> None:
        with GuidedDemo() as demo:
            initial = demo.snapshot()
            self.assertEqual(initial["mode"], "offline-guided")
            self.assertEqual(initial["step"], 0)
            self.assertEqual(initial["task"]["state"], "DRAFT")
            self.assertEqual(initial["total_steps"], 7)
            self.assertEqual(initial["guide"]["role"], "planner")
            snapshots = [demo.advance(index) for index in range(7)]
            self.assertEqual([item["active_role"] for item in snapshots], ["planner", "keeper", "builder", "builder", "tester", "reviewer", "archivist"])
            self.assertEqual([item["task"]["state"] for item in snapshots], ["RUNNING"] * 5 + ["REVIEW_PENDING", "DONE"])
            result = snapshots[-1]
            self.assertTrue(result["done"])
            self.assertEqual(result["step"], 7)
            self.assertEqual(len(result["events"]), 7)
            records = result["records"]
            self.assertEqual(len(records["sessions"]), 2)
            self.assertTrue(all(item["state"] == "CLOSED" for item in records["sessions"]))
            self.assertEqual(len(records["runs"]), 3)
            self.assertTrue(all(item["state"] == "SUCCEEDED" for item in records["runs"]))
            self.assertEqual(len(records["knowledge"]), 2)
            self.assertEqual(len(records["checkpoints"]), 3)
            with QingtianStore(demo.database_path) as store:
                checkpoint = store.get_checkpoint(demo.checkpoint_id)
            self.assertEqual(checkpoint["task_revision"], result["task"]["revision"])
            self.assertFalse(checkpoint["snapshot"]["unknown_run_refs"])
            self.assertEqual(set(checkpoint["snapshot"]["knowledge_refs"]), {item["knowledge_id"] for item in records["knowledge"]})
            provider_evidence = next(item for item in records["evidence"] if "provider_result" in item["metadata"])
            provider = provider_evidence["metadata"]["provider_result"]
            self.assertEqual(provider["effective_model"], "echo-v1")
            self.assertEqual(provider["output"]["provider"], "offline-echo")
            validate(result["task"], bundled_schema("task"))
            for collection, schema in (("sessions", "session"), ("runs", "run"), ("evidence", "evidence"), ("checkpoints", "checkpoint"), ("knowledge", "knowledge")):
                for record in records[collection]:
                    validate(record, bundled_schema(schema))

    def test_unknown_handoff_blocks_new_runs_until_reconciled(self) -> None:
        with GuidedDemo() as demo:
            for index in range(4):
                result = demo.advance(index)
            self.assertEqual(result["events"][-1]["state"], "UNKNOWN")
            unknown = next(run for run in result["records"]["runs"] if run["state"] == "UNKNOWN")
            self.assertTrue(unknown["external_operation_id"].startswith("synthetic-operation-"))
            self.assertEqual(result["records"]["sessions"][0]["state"], "CLOSED")
            self.assertIn(unknown["run_id"], result["records"]["checkpoints"][0]["snapshot"]["unknown_run_refs"])
            original_new_run = demo._new_run
            observations: list[str] = []

            def checked_new_run(store: QingtianStore, executor: str, key: str, request: dict[str, object]) -> dict[str, object]:
                if executor == "synthetic-verifier":
                    observations.append(store.get_run(unknown["run_id"])["state"])
                return original_new_run(store, executor, key, request)

            # Reuse the normal restored session creation and attempt an extra
            # run before the actual reconciliation in the real store.
            original_transition = QingtianStore.transition_run

            def checked_transition(store: QingtianStore, run_id: str, target: object, **kwargs: object) -> dict[str, object]:
                if run_id == unknown["run_id"] and str(target) == "SUCCEEDED":
                    with self.assertRaises(TransitionError):
                        store.create_run(task_id=demo.task_id, session_id=demo.session_id, executor="synthetic-probe", idempotency_key="must-not-execute", request={"probe": True})
                return original_transition(store, run_id, target, **kwargs)

            with patch.object(demo, "_new_run", side_effect=checked_new_run), patch.object(QingtianStore, "transition_run", checked_transition):
                reconciled = demo.advance(4)
            self.assertEqual(observations, ["SUCCEEDED"])
            self.assertEqual(len(reconciled["records"]["runs"]), 3)
            self.assertFalse(any(run["state"] == "UNKNOWN" for run in reconciled["records"]["runs"]))
            self.assertEqual(len(reconciled["records"]["sessions"]), 2)

    def test_stale_duplicate_and_invalid_steps_do_not_mutate_state(self) -> None:
        with GuidedDemo() as demo:
            demo.advance(0)
            for expected in (0, 3):
                with self.assertRaises(DemoConflict):
                    demo.advance(expected)
            for expected in (True, None, "1", 1.0, -1):
                with self.assertRaises(ValueError):
                    demo.advance(expected)
            unchanged = demo.snapshot()
            self.assertEqual(unchanged["step"], 1)
            self.assertEqual(len(unchanged["records"]["sessions"]), 1)
            self.assertEqual(len(unchanged["events"]), 1)
            for index in range(1, 7):
                demo.advance(index)
            with self.assertRaises(DemoConflict):
                demo.advance(7)

    def test_reset_rotates_task_token_and_private_database_then_cleans_up(self) -> None:
        demo = GuidedDemo()
        try:
            old = demo.snapshot()
            old_path = demo.database_path
            self.assertEqual(old_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(old_path.parent.stat().st_mode & 0o777, 0o700)
            demo.advance(0)
            fresh = demo.reset()
            new_path = demo.database_path
            self.assertNotEqual(old_path, new_path)
            self.assertFalse(old_path.parent.exists())
            self.assertNotEqual(fresh["csrf_token"], old["csrf_token"])
            self.assertNotEqual(fresh["task"]["task_id"], old["task"]["task_id"])
            self.assertEqual(fresh["step"], 0)
            self.assertEqual(fresh["events"], [])
            self.assertTrue(all(not value for value in fresh["records"].values()))
            self.assertNotIn(str(new_path), json.dumps(fresh))
        finally:
            demo.close()
        self.assertFalse(new_path.parent.exists())


class DemoHTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = DemoHTTPServer(0)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def request(self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def post(self, path: str, value: object, *, token: str | None = None, extra: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
        headers = {"Content-Type": "application/json", "X-Qingtian-Demo-Token": token or self.server.demo.csrf_token}
        headers.update(extra or {})
        status, body, _ = self.request("POST", path, body=json.dumps(value).encode(), headers=headers)
        return status, json.loads(body)

    def test_api_state_step_conflict_reset_and_browser_headers(self) -> None:
        status, body, headers = self.request("GET", "/api/state")
        initial = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(initial["step"], 0)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, result = self.post("/api/step", {"expected_step": 0}, extra={"Origin": self.server.url})
        self.assertEqual(status, 200)
        self.assertEqual(result["step"], 1)
        self.assertEqual(self.post("/api/step", {"expected_step": 0})[0], 409)
        status, reset = self.post("/api/reset", {})
        self.assertEqual(status, 200)
        self.assertEqual(reset["step"], 0)
        self.assertNotEqual(reset["task"]["task_id"], initial["task"]["task_id"])
        self.assertEqual(self.post("/api/step", {"expected_step": 0}, token=initial["csrf_token"])[0], 403)

    def test_capability_catalog_is_packaged_read_only_and_boundary_checked(self) -> None:
        before = self.server.demo.snapshot()
        status, body, headers = self.request("GET", "/api/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        catalog = json.loads(body)
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(len(catalog["phases"]), 7)
        self.assertEqual(self.request("GET", "/api/capabilities", headers={"Host": "example.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/capabilities", headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/capabilities?path=unused")[0], 404)
        self.assertEqual(self.server.demo.snapshot(), before)

    def test_capability_run_is_fixed_isolated_and_does_not_change_parent_guide(self) -> None:
        self.post("/api/step", {"expected_step": 0})
        before = self.server.demo.snapshot()
        status, result = self.post("/api/capabilities/run", {"capability_id": "api-e2e"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scope"], "synthetic-local-demo")
        self.assertEqual(len(result["task_ids"]), 1)
        self.assertNotIn(before["task"]["task_id"], result["task_ids"])
        self.assertEqual(self.server.demo.snapshot(), before)
        self.assertFalse(self.server.capabilities._processes)

    def test_nested_api_check_does_not_require_reverse_dns_in_its_child(self) -> None:
        before = self.server.demo.snapshot()
        real_popen = subprocess.Popen
        probe = (
            "import runpy, socket, sys\n"
            "def forbidden(*args, **kwargs):\n"
            "    raise AssertionError('loopback startup attempted reverse DNS')\n"
            "socket.getfqdn = forbidden\n"
            "sys.argv = ['qingtian_core.capability_checks', '--capability', 'api-e2e']\n"
            "runpy.run_module('qingtian_core.capability_checks', run_name='__main__')\n"
        )

        def guarded_child(command, *args, **kwargs):
            self.assertEqual(command, [sys.executable, "-B", "-m",
                                      "qingtian_core.capability_checks", "--capability", "api-e2e"])
            return real_popen([sys.executable, "-B", "-c", probe], *args, **kwargs)

        with patch("qingtian_core.capability_checks.subprocess.Popen", side_effect=guarded_child) as child:
            status, result = self.post("/api/capabilities/run", {"capability_id": "api-e2e"})
        child.assert_called_once()
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(len(result["checks"]), 16)
        self.assertEqual(len(result["task_ids"]), 1)
        self.assertEqual(self.server.demo.snapshot(), before)
        self.assertFalse(self.server.capabilities._processes)

    def test_capability_run_rejects_parameter_injection_and_invalid_boundaries(self) -> None:
        before = self.server.demo.snapshot()
        with patch.object(self.server.capabilities, "run") as run:
            for body in (None, [], {}, {"capability_id": "shell"}, {"capability_id": 1}, {"capability_id": "api-e2e", "url": "http://example.invalid"}, {"capability_id": "api-e2e", "command": "unused"}, {"capability_id": "api-e2e", "path": "unused"}):
                self.assertEqual(self.post("/api/capabilities/run", body)[0], 400)
            for headers in ({"Origin": "https://example.invalid"}, {"Host": "example.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(self.post("/api/capabilities/run", {"capability_id": "api-e2e"}, extra=headers)[0], 403)
            self.assertEqual(self.post("/api/capabilities/run", {"capability_id": "api-e2e"}, token="wrong-token")[0], 403)
            headers = {"Content-Type": "application/json", "X-Qingtian-Demo-Token": before["csrf_token"]}
            self.assertEqual(self.request("POST", "/api/capabilities/run", body=b'{"capability_id":"api-e2e","capability_id":"browser-e2e"}', headers=headers)[0], 400)
            self.assertEqual(self.request("POST", "/api/capabilities/run", body=b"x" * 1025, headers=headers)[0], 413)
            self.assertEqual(self.request("POST", "/api/capabilities/run", body=b"{}", headers={**headers, "Content-Type": "text/plain"})[0], 415)
            run.assert_not_called()
        self.assertEqual(self.server.demo.snapshot(), before)

    def test_capability_busy_does_not_block_main_guide_and_errors_do_not_poison_it(self) -> None:
        self.server.capabilities._run_lock.acquire()
        try:
            status, result = self.post("/api/capabilities/run", {"capability_id": "api-e2e"})
            self.assertEqual((status, result), (409, {"error": "capability_busy"}))
            self.assertEqual(self.post("/api/step", {"expected_step": 0})[0], 200)
        finally:
            self.server.capabilities._run_lock.release()
        entered = Event()
        release = Event()
        results = []

        def delayed_run(capability_id):
            entered.set()
            self.assertTrue(release.wait(3))
            receipt = _receipt(capability_id)
            receipt.update(status="blocked", reason_code="playwright_missing", checks=[{"id": "environment", "status": "blocked", "detail": "Synthetic blocked check."}])
            return receipt

        with patch.object(self.server.capabilities, "run", side_effect=delayed_run):
            thread = Thread(target=lambda: results.append(self.post("/api/capabilities/run", {"capability_id": "browser-e2e"})))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                started = monotonic()
                self.assertEqual(self.post("/api/step", {"expected_step": 1})[0], 200)
                self.assertLess(monotonic() - started, 1)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0][0], 200)
        self.assertEqual(results[0][1]["status"], "blocked")
        before = self.server.demo.snapshot()
        with patch.object(self.server.capabilities, "run", side_effect=RuntimeError("synthetic-private-detail")):
            status, result = self.post("/api/capabilities/run", {"capability_id": "api-e2e"})
        self.assertEqual(status, 500)
        self.assertEqual(result, {"error": "capability_request_failed"})
        self.assertEqual(self.server.demo.snapshot(), before)

    def test_failed_step_returns_structured_error_and_reset_recovers(self) -> None:
        self.post("/api/step", {"expected_step": 0})
        self.post("/api/step", {"expected_step": 1})
        with patch("qingtian_core.demo_web.EchoProvider.generate", side_effect=RuntimeError("synthetic-internal-detail")) as provider:
            status, error = self.post("/api/step", {"expected_step": 2})
            self.assertEqual(status, 500)
            self.assertEqual(error["error"], "guide_step_failed")
            self.assertTrue(error["requires_reset"])
            self.assertEqual(error["state"]["records"]["runs"][0]["state"], "RUNNING")
            self.assertNotIn("synthetic-internal-detail", json.dumps(error))
            status, blocked = self.post("/api/step", {"expected_step": 2})
            self.assertEqual(status, 409)
            self.assertEqual(blocked["error"], "reset_required")
            provider.assert_called_once()
        status, body, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["requires_reset"])
        status, reset = self.post("/api/reset", {})
        self.assertEqual(status, 200)
        self.assertFalse(reset["requires_reset"])
        self.assertEqual(self.post("/api/step", {"expected_step": 0})[0], 200)

    def test_idle_browser_connection_does_not_block_state_requests(self) -> None:
        idle = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        try:
            started = monotonic()
            status, body, _ = self.request("GET", "/api/state")
            elapsed = monotonic() - started
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["step"], 0)
            self.assertLess(elapsed, 1.5, "idle browser connection blocked a normal request")
        finally:
            idle.close()

    def test_reset_rechecks_token_after_a_delayed_request_body(self) -> None:
        old_token = self.server.demo.csrf_token
        checked = Event()
        original_token_valid = DemoRequestHandler._token_valid

        def observed_token_check(handler: DemoRequestHandler) -> bool:
            valid = original_token_valid(handler)
            if handler.path == "/api/step" and valid:
                checked.set()
            return valid

        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        body = b'{"expected_step":0}'
        try:
            with patch.object(DemoRequestHandler, "_token_valid", observed_token_check):
                connection.putrequest("POST", "/api/step")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("X-Qingtian-Demo-Token", old_token)
                connection.endheaders()
                self.assertTrue(checked.wait(timeout=2), "delayed request did not pass the early token check")
                status, reset = self.post("/api/reset", {})
                self.assertEqual(status, 200)
                self.assertNotEqual(reset["csrf_token"], old_token)
                connection.send(body)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertEqual(json.loads(response.read())["error"], "invalid_token")
            self.assertEqual(self.server.demo.snapshot()["step"], 0)
            self.assertFalse(self.server.demo.snapshot()["events"])
        finally:
            connection.close()

    def test_capability_request_rechecks_token_after_delayed_body(self) -> None:
        old_token = self.server.demo.csrf_token
        checked = Event()
        original_token_valid = DemoRequestHandler._token_valid

        def observed_token_check(handler: DemoRequestHandler) -> bool:
            valid = original_token_valid(handler)
            if handler.path == "/api/capabilities/run" and valid:
                checked.set()
            return valid

        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        body = b'{"capability_id":"api-e2e"}'
        try:
            with patch.object(DemoRequestHandler, "_token_valid", observed_token_check), patch.object(self.server.capabilities, "run") as run:
                connection.putrequest("POST", "/api/capabilities/run")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("X-Qingtian-Demo-Token", old_token)
                connection.endheaders()
                self.assertTrue(checked.wait(timeout=2))
                self.assertEqual(self.post("/api/reset", {})[0], 200)
                fresh = self.server.demo.snapshot()
                connection.send(body)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertEqual(json.loads(response.read())["error"], "invalid_token")
                run.assert_not_called()
            self.assertEqual(self.server.demo.snapshot(), fresh)
        finally:
            connection.close()

    def test_server_close_terminates_capability_child_before_joining_http_worker(self) -> None:
        original_popen = subprocess.Popen
        spawned = Event()
        children = []
        directories = []

        def slow_child(_command, **kwargs):
            process = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(process)
            directories.append(Path(kwargs["cwd"]))
            spawned.set()
            return process

        results = []

        def request_capability():
            try:
                results.append(self.post("/api/capabilities/run", {"capability_id": "api-e2e"}))
            except OSError:
                # Closing the server deliberately closes accepted sockets.
                results.append("connection-closed")

        with patch("qingtian_core.capability_checks.subprocess.Popen", side_effect=slow_child), patch.object(self.server, "handle_error") as handle_error:
            client = Thread(target=request_capability)
            client.start()
            self.assertTrue(spawned.wait(2))
            started = monotonic()
            self.server.shutdown()
            self.thread.join(2)
            self.server.server_close()
            client.join(3)
            self.assertLess(monotonic() - started, 4)
            self.assertFalse(client.is_alive())
            handle_error.assert_not_called()
        self.assertTrue(results)
        self.assertTrue(all(process.poll() is not None for process in children))
        self.assertTrue(all(not directory.exists() for directory in directories))
        self.assertFalse(any(worker.is_alive() for worker in self.server._threads))

    def test_server_close_unblocks_idle_workers_before_cleaning_data(self) -> None:
        idle = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        self.request("GET", "/api/state")
        database_parent = self.server.demo.database_path.parent
        self.server.shutdown()
        self.thread.join(timeout=3)
        try:
            started = monotonic()
            self.server.server_close()
            self.assertLess(monotonic() - started, 1.5)
            self.assertFalse(database_parent.exists())
            self.assertFalse(any(thread.is_alive() for thread in self.server._threads))
        finally:
            idle.close()

    def test_host_origin_fetch_site_and_token_boundaries_reject_mutations(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for host in ("example.invalid", "example.invalid:" + str(self.server.server_port), "127.0.0.1:1", "localhost:" + str(self.server.server_port)):
            self.assertEqual(self.request("GET", "/api/state", headers={"Host": host})[0], 403)
        for origin in ("https://example.invalid", "null", "http://localhost:" + str(self.server.server_port)):
            self.assertEqual(self.post("/api/step", {"expected_step": 0}, extra={"Origin": origin})[0], 403)
        for fetch_site in ("cross-site", "same-site"):
            self.assertEqual(self.request("GET", "/api/state", headers={"Sec-Fetch-Site": fetch_site})[0], 403)
        self.assertEqual(self.post("/api/step", {"expected_step": 0}, token="incorrect-token")[0], 403)
        self.assertEqual(self.post("/api/step", {"expected_step": 0}, token="\u00e9")[0], 403)
        self.assertEqual(self.request("POST", "/api/step", body=b'{"expected_step":0}', headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.server.demo.snapshot()["step"], 0)

    def test_request_shape_size_and_routes_are_bounded(self) -> None:
        for value in ([], None, {"expected_step": True}, {"expected_step": 0, "command": "unused"}, {"expected_step": "0"}):
            self.assertEqual(self.post("/api/step", value)[0], 400)
        token_headers = {"Content-Type": "application/json", "X-Qingtian-Demo-Token": self.server.demo.csrf_token}
        self.assertEqual(self.request("POST", "/api/step", body=b'{"expected_step":0,"expected_step":0}', headers=token_headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/step", body=b"x" * 1025, headers=token_headers)[0], 413)
        self.assertEqual(self.request("POST", "/api/step", body=b"{}", headers={**token_headers, "Content-Length": "9" * 5000})[0], 413)
        self.assertEqual(self.request("POST", "/api/step", body=b"{}", headers={**token_headers, "Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request("POST", "/api/step", body=b"{}", headers={**token_headers, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post("/api/reset", {"workspace": "unused"})[0], 400)
        self.assertEqual(self.post("/api/command", {})[0], 404)
        self.assertEqual(self.request("OPTIONS", "/api/step")[0], 405)
        for path in ("/../README.md", "/%2e%2e/README.md", "/api/state?token=unused", "/control.db", "/resources/demo/index.html"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.server.demo.snapshot()["step"], 0)

    def test_duplicate_authority_and_token_headers_are_rejected(self) -> None:
        for header, value in (("Host", f"127.0.0.1:{self.server.server_port}"), ("Origin", self.server.url), ("X-Qingtian-Demo-Token", self.server.demo.csrf_token)):
            connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            try:
                connection.putrequest("POST", "/api/step", skip_host=True)
                headers = {"Host": f"127.0.0.1:{self.server.server_port}", "Origin": self.server.url, "X-Qingtian-Demo-Token": self.server.demo.csrf_token, "Content-Type": "application/json", "Content-Length": "19"}
                for name, item in headers.items():
                    connection.putheader(name, item)
                connection.putheader(header, value)
                connection.endheaders(b'{"expected_step":0}')
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()

    def test_static_server_only_serves_packaged_resource_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-static-test-") as temporary:
            root = Path(temporary)
            (root / "demo").mkdir()
            for name, payload in (("index.html", "<title>Synthetic demo</title>"), ("app.css", "body { color: black; }"), ("app.js", "'use strict';")):
                (root / "demo" / name).write_text(payload, encoding="utf-8")
            with patch("qingtian_core.demo_web.files", return_value=root):
                for route, mime in (("/", "text/html"), ("/index.html", "text/html"), ("/app.css", "text/css"), ("/app.js", "text/javascript")):
                    status, body, headers = self.request("GET", route)
                    self.assertEqual(status, 200)
                    self.assertTrue(body)
                    self.assertTrue(headers["Content-Type"].startswith(mime))
                self.assertEqual(self.request("HEAD", "/")[1], b"")


class DemoCliTest(unittest.TestCase):
    def test_numeric_loopback_bind_never_performs_reverse_dns(self) -> None:
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected reverse DNS")) as resolver:
            with DemoHTTPServer(0) as server:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertEqual(server.server_address, ("127.0.0.1", server.server_port))
                self.assertGreater(server.server_port, 0)
                self.assertEqual(server.url, "http://127.0.0.1:{}".format(server.server_port))
        resolver.assert_not_called()

    def test_cli_help_and_dispatch(self) -> None:
        arguments = parser().parse_args(["demo-web", "--port", "0", "--no-browser"])
        self.assertEqual(arguments.port, 0)
        self.assertTrue(arguments.no_browser)
        with patch("qingtian_core.demo_web.run_demo_web", return_value=0) as run:
            self.assertEqual(main(["demo-web", "--port", "0", "--no-browser"]), 0)
            run.assert_called_once_with(port=0, open_browser=False)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit_context:
            main(["demo-web", "--help"])
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn("--no-browser", output.getvalue())
        for invalid in (-1, 65536, True):
            with self.assertRaises(ValueError):
                DemoHTTPServer(invalid)

    def test_keyboard_interrupt_cleans_temporary_storage_and_opens_browser_by_default(self) -> None:
        paths: list[Path] = []

        def interrupt(server: DemoHTTPServer, **_kwargs: object) -> None:
            paths.append(server.demo.database_path.parent)
            self.assertTrue(paths[-1].is_dir())
            raise KeyboardInterrupt

        output = io.StringIO()
        with patch.object(DemoHTTPServer, "serve_forever", interrupt), patch("qingtian_core.demo_web.webbrowser.open", return_value=True) as browser, redirect_stdout(output):
            self.assertEqual(run_demo_web(port=0), 0)
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        browser.assert_called_once()
        self.assertIn("http://127.0.0.1:", output.getvalue())
        self.assertNotIn(str(paths[0]), output.getvalue())


if __name__ == "__main__":
    unittest.main()
