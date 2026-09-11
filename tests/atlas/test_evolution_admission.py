"""Synthetic persistence and BytesIO handler checks; never HTTP/browser acceptance."""
import io
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qingtian_engine.admission import AdmissionError, AdmissionService, initialize_admission_schema
from qingtian_engine.db import Database
from qingtian_engine.server import ControlPlaneHandler
from qingtian_engine.service import ControlPlane

ROOT = Path(__file__).resolve().parents[2]


class ForbiddenSideEffect(BaseException):
    """Production catch(Exception) cannot hide a forbidden test operation."""


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "subprocess.Popen", "os.kill"):
            guard = patch(target, side_effect=ForbiddenSideEffect("forbidden in synthetic suite: " + target))
            guard.start()
            self.addCleanup(guard.stop)
        directory = tempfile.TemporaryDirectory(prefix="synthetic-admission-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = Database(self.root / "test.sqlite3")
        self.service = ControlPlane(self.db)
        self.admission = AdmissionService(self.db)
        self.seed("task-a")
        self.calls = 0

    def seed(self, task_id, **fields):
        values = {"id": task_id, "idempotency_key": task_id, "title": "Synthetic new task", "state": "PLANNED",
                  "repository": "/synthetic/project", "base_branch": "dev", "evidence_profile": "artifact", "created_at": "2026-09-10T01:00:00Z", "updated_at": "2026-09-10T01:00:00Z"}
        values.update(fields)
        self.db.execute("INSERT INTO tasks (" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))

    def request(self, key="key-1", task_id="task-a", **fields):
        value = {"instruction": "Execute the agreed synthetic scope", "resume": False, "expected_revision": self.admission.get(task_id)["revision"], "idempotency_key": key}
        value.update(fields)
        return value

    def queue(self, task_id, instruction, resume):
        self.calls += 1
        self.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) VALUES(?,?,1,'synthetic','synthetic','QUEUED','2026-09-10T01:00:00Z')", ("run-" + task_id, task_id))
        self.db.execute("UPDATE tasks SET state='QUEUED' WHERE id=?", (task_id,))
        return {"id": "run-" + task_id}

    def no_dispatch(self, *args):
        raise ForbiddenSideEffect("dispatcher must not be called")

    def test_saved_is_not_dispatch(self):
        result = self.admission.get("task-a")
        self.assertEqual(result["current"]["state"], "saved")
        self.assertIsNone(result["receipt"])
        self.assertEqual(self.db.all("SELECT * FROM runs"), [])

    def test_queued_binds_real_run_not_person_or_session(self):
        result = self.admission.dispatch("task-a", self.request(), self.queue)
        self.assertEqual(result["receipt"]["state"], "queued")
        self.assertEqual(result["current"]["run_id"], "run-task-a")
        self.assertIsNone(result["current"]["session_id"])
        self.assertIsNone(result["current"]["executor"]["host_id"])
        self.assertEqual(len(result["history"]), 2)

    def test_duplicate_lost_reply_keeps_original_payload_revision_time(self):
        request = self.request()
        first = self.admission.dispatch("task-a", request, self.queue)
        self.db.execute("UPDATE tasks SET heartbeat_at='later' WHERE id='task-a'")
        retry = self.admission.dispatch("task-a", request, self.no_dispatch)
        self.assertEqual(first["receipt"], retry["receipt"])
        self.assertEqual(first["history"], retry["history"])
        self.assertTrue(retry["reused"])
        self.assertFalse(retry["basis_stale"])
        self.assertEqual(self.calls, 1)

    def test_conflicting_key_is_409_without_partial_write(self):
        request = self.request()
        self.admission.dispatch("task-a", request, self.queue)
        before = self.db.all("SELECT * FROM admission_requests")
        with self.assertRaises(AdmissionError) as caught:
            self.admission.dispatch("task-a", dict(request, instruction="Different scope"), self.no_dispatch)
        self.assertEqual(caught.exception.code, "idempotency")
        self.assertEqual(self.db.all("SELECT * FROM admission_requests"), before)

    def test_changed_key_compatible_intent_coalesces_existing_owned_run(self):
        first = self.admission.dispatch("task-a", self.request(), self.queue)
        result = self.admission.dispatch("task-a", self.request("second"), self.no_dispatch)
        self.assertEqual(result["receipt"]["state"], "coalesced")
        self.assertEqual(result["receipt"]["run_id"], first["receipt"]["run_id"])

    def test_run_progress_preserves_admission_and_only_known_session(self):
        self.admission.dispatch("task-a", self.request(), self.queue)
        self.db.execute("UPDATE tasks SET state='RUNNING',progress=25 WHERE id='task-a'")
        self.db.execute("UPDATE runs SET status='RUNNING',session_id='real-captured-session' WHERE task_id='task-a'")
        current = self.admission.get("task-a")
        self.assertEqual(current["current"]["state"], "queued")
        self.assertEqual(current["current"]["run_status"], "RUNNING")
        self.assertFalse(current["basis_stale"])
        self.assertEqual(current["current"]["session_id"], "real-captured-session")
        result = self.admission.dispatch("task-a", self.request("second"), self.no_dispatch)
        self.assertEqual(result["receipt"]["state"], "coalesced")

    def test_different_intent_does_not_coalesce(self):
        self.admission.dispatch("task-a", self.request(), self.queue)
        result = self.admission.dispatch("task-a", self.request("second", instruction="Changed deliverable"), self.no_dispatch)
        self.assertEqual(result["receipt"]["reason_code"], "run_binding_ambiguous")

    def test_changed_scope_does_not_coalesce(self):
        self.admission.dispatch("task-a", self.request(), self.queue)
        self.db.execute("UPDATE tasks SET scope_summary='new scope' WHERE id='task-a'")
        result = self.admission.dispatch("task-a", self.request("second"), self.no_dispatch)
        self.assertEqual(result["receipt"]["state"], "uncertain")

    def test_changed_execution_target_never_coalesces_old_run(self):
        for field in ("model", "reasoning", "speed", "worker_type", "owner_session", "branch", "worktree"):
            with self.subTest(field=field):
                task_id = "target-" + field
                self.seed(task_id)
                accepted = self.admission.dispatch(task_id, self.request(task_id=task_id), self.queue)
                before = self.db.one("SELECT * FROM admission_run_targets WHERE run_id=?", ("run-" + task_id,))
                self.db.execute("UPDATE tasks SET " + field + "=? WHERE id=?", ("changed-" + field, task_id))
                current = self.admission.get(task_id)
                self.assertTrue(current["basis_stale"])
                self.assertEqual(current["current"]["reason_code"], "receipt_basis_stale")
                result = self.admission.dispatch(task_id, self.request("new-key", task_id=task_id), self.no_dispatch)
                self.assertEqual(result["receipt"]["reason_code"], "run_binding_ambiguous")
                self.assertNotEqual(result["receipt"]["execution_target_hash"], accepted["receipt"]["execution_target_hash"])
                self.assertEqual(self.db.one("SELECT * FROM admission_run_targets WHERE run_id=?", ("run-" + task_id,)), before)

    def test_late_execution_target_changes_do_not_confirm_admission(self):
        for field in ("model", "reasoning", "speed", "worker_type", "owner_session", "branch", "worktree"):
            with self.subTest(field=field):
                task_id = "late-target-" + field
                self.seed(task_id)
                def late(*args):
                    result = self.queue(*args)
                    self.db.execute("UPDATE tasks SET " + field + "=? WHERE id=?", ("changed-" + field, task_id))
                    return result
                result = self.admission.dispatch(task_id, self.request(task_id=task_id), late)
                self.assertEqual(result["receipt"]["reason_code"], "task_changed_during_dispatch")
                self.assertIsNone(result["receipt"]["run_id"])

    def test_normal_preparation_records_actual_target_at_queue_insert(self):
        def prepare_then_queue(*args):
            self.db.execute("UPDATE tasks SET model='prepared-model',reasoning='high',speed='standard',branch='xh/prepared',worktree='/synthetic/prepared' WHERE id='task-a'")
            return self.queue(*args)
        result = self.admission.dispatch("task-a", self.request(), prepare_then_queue)
        self.assertEqual(result["receipt"]["state"], "queued")
        target = self.db.one("SELECT * FROM admission_run_targets WHERE run_id='run-task-a'")
        self.assertEqual(json.loads(target["target_json"])["worktree"], "/synthetic/prepared")
        result = self.admission.dispatch("task-a", self.request("new-key"), self.no_dispatch)
        self.assertEqual(result["receipt"]["state"], "coalesced")

    def test_migration_does_not_backfill_legacy_run_targets(self):
        self.db.execute("DROP TRIGGER admission_capture_run_target")
        self.queue("task-a", "", False)
        with self.db.connect() as connection:
            initialize_admission_schema(connection)
        self.assertIsNone(self.db.one("SELECT * FROM admission_run_targets WHERE run_id='run-task-a'"))
        result = self.admission.dispatch("task-a", self.request(), self.no_dispatch)
        self.assertEqual(result["receipt"]["reason_code"], "run_binding_ambiguous")

    def test_unowned_active_run_is_not_coalesced(self):
        self.queue("task-a", "", False)
        result = self.admission.dispatch("task-a", self.request(), self.no_dispatch)
        self.assertEqual(result["receipt"]["reason_code"], "run_binding_ambiguous")

    def test_save_success_dispatch_failure_is_uncertain_and_task_preserved(self):
        def fail(*args):
            raise RuntimeError("synthetic failure before known ack")
        result = self.admission.dispatch("task-a", self.request(), fail)
        self.assertEqual(result["receipt"]["reason_code"], "dispatch_result_unknown")
        self.assertEqual(self.db.one("SELECT state FROM tasks WHERE id='task-a'")["state"], "PLANNED")
        self.assertEqual(len(result["history"]), 2)

    def test_side_effect_then_lost_ack_never_redispatched(self):
        request = self.request()
        def lost(*args):
            self.queue(*args)
            raise TimeoutError("lost acknowledgement")
        first = self.admission.dispatch("task-a", request, lost)
        retry = self.admission.dispatch("task-a", request, self.no_dispatch)
        self.assertEqual(first["receipt"], retry["receipt"])
        self.assertEqual(retry["receipt"]["state"], "uncertain")
        second = self.admission.dispatch("task-a", self.request("second"), self.no_dispatch)
        self.assertEqual(second["receipt"]["reason_code"], "dispatch_already_unconfirmed")
        self.assertEqual(self.calls, 1)

    def test_crash_leaves_pending_without_reexecution(self):
        request = self.request()
        def crash(*args):
            raise SystemExit("synthetic abrupt termination")
        with self.assertRaises(SystemExit):
            self.admission.dispatch("task-a", request, crash)
        result = self.admission.dispatch("task-a", request, self.no_dispatch)
        self.assertEqual(result["receipt"]["state"], "pending")
        self.assertFalse(result["dispatch_allowed"])

    def test_confirmation_commit_failure_leaves_original_pending(self):
        request = self.request()
        self.db.execute("CREATE TRIGGER synthetic_fail_confirmation BEFORE INSERT ON admission_receipts WHEN NEW.state='queued' BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
        with self.assertRaises(Exception):
            self.admission.dispatch("task-a", request, self.queue)
        result = self.admission.dispatch("task-a", request, self.no_dispatch)
        self.assertEqual(result["receipt"]["state"], "pending")
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(self.calls, 1)

    def test_message_success_is_not_acceptance(self):
        result = self.admission.dispatch("task-a", self.request(), lambda *args: {"message_id": "sent", "success": True})
        self.assertEqual(result["receipt"]["reason_code"], "external_ack_not_integrated")
        self.assertIsNone(result["current"]["run_id"])

    def test_other_task_run_cannot_bind(self):
        self.seed("task-b")
        self.queue("task-b", "", False)
        result = self.admission.dispatch("task-a", self.request(), lambda *args: {"id": "run-task-b"})
        self.assertEqual(result["receipt"]["state"], "uncertain")
        self.assertIsNone(result["receipt"]["run_id"])

    def test_pause_cancel_sensitive_and_reference_never_start(self):
        cases = [{"state": "PAUSED"}, {"state": "CANCELED"}, {"state": "DONE"}, {"action_sensitive": 1}, {"authorization_policy": "analysis-only"}, {"imported_from": "archive"}]
        for index, fields in enumerate(cases):
            task_id = "protected-" + str(index)
            self.seed(task_id, **fields)
            before = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            result = self.admission.dispatch(task_id, self.request(task_id=task_id), self.no_dispatch)
            self.assertEqual(result["receipt"]["state"], "rejected")
            self.assertEqual(self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)), before)

    def test_current_user_external_actions_defer(self):
        for kind in ("user", "external"):
            self.seed(kind, action_owner_kind=kind, action_text="Pending decision")
            result = self.admission.dispatch(kind, self.request(task_id=kind), self.no_dispatch)
            self.assertEqual(result["receipt"]["reason_code"], kind + "_action_pending")

    def test_dependency_resolution_invalidates_old_reason_without_resume(self):
        self.seed("dep")
        self.db.execute("INSERT INTO task_dependencies VALUES('task-a','dep','blocks')")
        original = self.admission.dispatch("task-a", self.request(), self.no_dispatch)
        self.assertEqual(original["receipt"]["reason_code"], "dependency_unresolved")
        self.db.execute("UPDATE tasks SET state='DONE' WHERE id='dep'")
        result = self.admission.get("task-a")
        self.assertEqual(result["current"]["reason_code"], "receipt_basis_stale")
        self.assertEqual(result["history"], original["history"])
        self.assertEqual(self.db.all("SELECT * FROM runs"), [])

    def test_late_result_after_pause_cancel_never_accepted_or_resumed(self):
        for state in ("PAUSED", "CANCELED"):
            task_id = "late-" + state
            self.seed(task_id)
            def late(*args):
                result = self.queue(*args)
                self.db.execute("UPDATE tasks SET state=? WHERE id=?", (state, task_id))
                return result
            result = self.admission.dispatch(task_id, self.request(task_id=task_id), late)
            self.assertEqual(result["receipt"]["reason_code"], "task_changed_during_dispatch")
            self.assertEqual(self.db.one("SELECT state FROM tasks WHERE id=?", (task_id,))["state"], state)
            self.assertEqual(result["current"]["state"], "rejected")

    def test_stale_revision_and_missing_version_fail_closed(self):
        request = self.request()
        self.db.execute("UPDATE tasks SET title=title WHERE id='task-a'")
        with self.assertRaises(AdmissionError) as caught:
            self.admission.dispatch("task-a", request, self.no_dispatch)
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual(self.db.all("SELECT * FROM admission_requests"), [])
        current_request = self.request()
        self.db.execute("DELETE FROM operations_task_versions WHERE task_id='task-a'")
        self.assertIsNone(self.admission.get("task-a")["revision"])
        with self.assertRaises(AdmissionError):
            self.admission.dispatch("task-a", current_request, self.no_dispatch)

    def test_scope_changes_during_dispatch_and_old_run_return_are_uncertain(self):
        def changed(*args):
            result = self.queue(*args)
            self.db.execute("UPDATE tasks SET scope_summary='different target' WHERE id='task-a'")
            return result
        result = self.admission.dispatch("task-a", self.request(), changed)
        self.assertEqual(result["receipt"]["reason_code"], "task_changed_during_dispatch")
        self.seed("old-run-task")
        self.queue("old-run-task", "", False)
        self.db.execute("UPDATE runs SET status='DONE' WHERE task_id='old-run-task'")
        self.db.execute("UPDATE tasks SET state='PLANNED' WHERE id='old-run-task'")
        result = self.admission.dispatch("old-run-task", self.request(task_id="old-run-task"), lambda *args: {"id": "run-old-run-task"})
        self.assertEqual(result["receipt"]["reason_code"], "run_binding_ambiguous")

    def test_same_key_concurrent_receipt_returns_pending_only_once(self):
        entered, release = threading.Event(), threading.Event()
        request = self.request()
        results = []
        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.queue(*args)
        thread = threading.Thread(target=lambda: results.append(self.admission.dispatch("task-a", request, blocked)))
        thread.start()
        self.assertTrue(entered.wait(5))
        pending = self.admission.dispatch("task-a", request, self.no_dispatch)
        self.assertEqual(pending["receipt"]["state"], "pending")
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["receipt"]["state"], "queued")
        self.assertEqual(self.calls, 1)

    def test_migration_read_and_immutable_history(self):
        self.admission.dispatch("task-a", self.request(), self.queue)
        with self.db.connect() as connection:
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            before = {name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '"')] for name in names}
            initialize_admission_schema(connection)
            initialize_admission_schema(connection)
            after = {name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '"')] for name in names}
        self.assertEqual(before, after)
        self.admission.get("task-a")
        with self.db.connect() as connection:
            self.assertEqual(before, {name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '"')] for name in names})
            with self.assertRaises(Exception):
                connection.execute("UPDATE admission_receipts SET state='queued'")
            receipt = connection.execute("SELECT * FROM admission_receipts LIMIT 1").fetchone()
            with self.assertRaises(Exception):
                connection.execute("INSERT OR REPLACE INTO admission_receipts VALUES(?,?,?,?,?,?)", tuple(receipt))
            target = connection.execute("SELECT * FROM admission_run_targets LIMIT 1").fetchone()
            with self.assertRaises(Exception):
                connection.execute("UPDATE admission_run_targets SET target_json='{}'")
            with self.assertRaises(Exception):
                connection.execute("INSERT OR REPLACE INTO admission_run_targets VALUES(?,?,?)", tuple(target))


class HandlerTests(AdmissionTests):
    # Only this one method is added; inherited service scenarios are separately
    # selected by the documented command, not counted twice.
    def exchange(self, method, path, payload=None, headers=None, raw_body=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        if raw_body is not None:
            body = raw_body
        supplied_headers = {"Host": "127.0.0.1:12345", "Content-Length": str(len(body)), "Connection": "close"}
        supplied_headers.update(headers or {})
        handler = ControlPlaneHandler.__new__(ControlPlaneHandler)
        handler.service = self.service
        handler.manager = SimpleNamespace(paths={"prompts": self.root}, dispatch=lambda task_id, prompt, resume=False: self.queue(task_id, prompt.read_text(), resume))
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 12345))
        handler.client_address = ("synthetic", 0)
        handler.connection = SimpleNamespace()
        handler.rfile = io.BytesIO((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in supplied_headers.items()) + "\r\n").encode() + body)
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        handler.handle_one_request()
        raw = handler.wfile.getvalue()
        headers, value = raw.split(b"\r\n\r\n", 1)
        print("SYNTHETIC_ADMISSION_HANDLER", method, path, int(headers.split(b" ")[1]), "listening_socket=false")
        return int(headers.split(b" ")[1]), json.loads(value)

    def test_buffered_dispatch_security_and_framing_without_side_effect(self):
        request = self.request()
        for headers, status in [({"Host": "evil.example:12345"}, 403), ({"Origin": "https://evil.example"}, 403), ({"Content-Length": "65537"}, 400), ({"Transfer-Encoding": "chunked"}, 400), ({"Content-Length": "-1"}, 400)]:
            self.assertEqual(self.exchange("POST", "/api/tasks/task-a/dispatch", request, headers=headers)[0], status)
        self.assertEqual(self.exchange("POST", "/api/tasks/task-a/dispatch", raw_body=b'{"instruction":"first","instruction":"second"}')[0], 400)
        self.assertEqual(self.db.all("SELECT * FROM admission_requests"), [])
        self.assertEqual(self.calls, 0)

    def test_actual_buffered_handler_dispatch_get_and_legacy_rejection(self):
        status, result = self.exchange("POST", "/api/tasks/task-a/dispatch", {"instruction": "old client"})
        self.assertEqual(status, 400)
        request = self.request()
        status, result = self.exchange("POST", "/api/tasks/task-a/dispatch", request)
        self.assertEqual(status, 200)
        self.assertEqual(result["receipt"]["state"], "queued")
        status, retry = self.exchange("POST", "/api/tasks/task-a/dispatch", request)
        self.assertEqual(retry["receipt"], result["receipt"])
        self.assertTrue(retry["reused"])
        status, current = self.exchange("GET", "/api/tasks/task-a/admission")
        self.assertEqual(status, 200)
        self.assertEqual(current["current"]["run_id"], "run-task-a")
        status, detail = self.exchange("GET", "/api/tasks/task-a")
        self.assertEqual(detail["admission"]["receipt"], result["receipt"])


if __name__ == "__main__":
    unittest.main()
