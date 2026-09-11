"""Controlled real SQLite completion transactions, synthetic data and no sockets.

Only the gate observation wrapper and thread rendezvous are instrumentation.
The real gate, BEGIN IMMEDIATE, native UPDATE, triggers, event and commit run.
No elapsed-time/sleep assertion is used to establish ordering or exclusion.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

import qingtian_engine.service as service_module
from qingtian_engine.db import Database
from qingtian_engine.operations_clarity import OperationsError, COMPLETION_ASSURANCE
from qingtian_engine.service import ControlPlane

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(tempfile.gettempdir())
TASK = "synthetic-race-task"
DEPENDENCY = "synthetic-race-dependency"
WHEN = "2026-09-01T01:00:00+00:00"
WAIT_SECONDS = 5
FORBIDDEN_ATTEMPTS = []
TRACE_RESULTS = []


def deny(name):
    def blocked(*args, **kwargs):
        FORBIDDEN_ATTEMPTS.append(name)
        raise AssertionError("forbidden synthetic race operation: " + name)
    return blocked


def all_rows(connection):
    names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
            for name in names}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class CompletionTransactionRaceTests(unittest.TestCase):
    def setUp(self):
        self.guard = ExitStack()
        self.addCleanup(self.guard.close)
        for target in ("socket.socket", "subprocess.Popen", "os.kill"):
            self.guard.enter_context(patch(target, side_effect=deny(target)))
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-transaction-")
        self.addCleanup(self.directory.cleanup)
        self.db = Database(Path(self.directory.name) / "synthetic.sqlite3")
        self.service = ControlPlane(self.db)
        self.trace = []
        self.trace_lock = threading.Lock()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO tasks(id,idempotency_key,title,state,evidence_profile,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (TASK, TASK, "Synthetic transaction task", "VERIFYING", "artifact", WHEN, WHEN))
            connection.execute("INSERT INTO tasks(id,idempotency_key,title,state,evidence_profile,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (DEPENDENCY, DEPENDENCY, "Synthetic required dependency", "WAITING", "artifact", WHEN, WHEN))
            connection.execute("INSERT INTO evidence(task_id,kind,value,verified,created_at) VALUES(?,?,?,?,?)",
                               (TASK, "artifact", "synthetic:transaction-artifact", 1, WHEN))
        self.mark("fixture_created", state="VERIFYING", eligible=self.service.completion_eligibility(TASK)["eligible"])

    def tearDown(self):
        item = {"test": self.id(), "events": self.trace, "synthetic": True}
        TRACE_RESULTS.append(item)
        print("SYNTHETIC_TRANSACTION_TRACE " + json.dumps(item, sort_keys=True))

    def mark(self, name, **details):
        with self.trace_lock:
            self.trace.append({"order": len(self.trace) + 1, "event": name,
                               "thread": threading.current_thread().name, **details})

    def wait(self, event):
        self.assertTrue(event.wait(WAIT_SECONDS), "controlled rendezvous timed out")

    def rows(self):
        with self.db.connect() as connection:
            connection.execute("BEGIN")
            return all_rows(connection)

    def native(self):
        return self.db.one("SELECT * FROM tasks WHERE id=?", (TASK,))

    def mutate(self, connection, kind):
        if kind == "user_action":
            connection.execute("UPDATE tasks SET action_owner_kind='user',action_owner='synthetic-reviewer',"
                               "action_text='Synthetic newly pending user action' WHERE id=?", (TASK,))
        elif kind == "required_dependency":
            connection.execute("INSERT INTO task_dependencies(task_id,depends_on_id,relation) VALUES(?,?,'blocks')",
                               (TASK, DEPENDENCY))
        elif kind == "evidence_withdrawal":
            connection.execute("DELETE FROM evidence WHERE task_id=? AND kind='artifact'", (TASK,))
        else:
            raise AssertionError("unknown synthetic mutation")

    def assert_mutation_visible(self, kind):
        gate = self.service.completion_eligibility(TASK)
        self.assertFalse(gate["eligible"])
        if kind == "user_action":
            self.assertEqual(gate["unresolved_actions"][0]["kind"], "user")
        elif kind == "required_dependency":
            self.assertEqual(gate["unresolved_dependencies"][0]["depends_on_id"], DEPENDENCY)
        else:
            self.assertEqual(gate["missing"], ["artifact"])

    def _writer_contends_after_gate(self, kind):
        before = self.rows()
        at_gate = threading.Barrier(2, timeout=WAIT_SECONDS)
        first_attempt_finished = threading.Event()
        allow_post_completion_write = threading.Event()
        actual_gate = service_module.completion_basis
        observations = []

        def competing_writer():
            connection = sqlite3.connect(str(self.db.path), timeout=0)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=0")
            try:
                at_gate.wait()
                self.mark("competing_write_attempted", mutation=kind)
                try:
                    self.mutate(connection, kind)
                    connection.commit()
                except sqlite3.OperationalError as error:
                    connection.rollback()
                    observations.append({"sqlite_errorcode": error.sqlite_errorcode,
                                         "sqlite_errorname": error.sqlite_errorname})
                    self.assertEqual(error.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                    # This is a real attempted native write, not a mocked lock.
                    self.assertEqual(all_rows(connection), before)
                    self.mark("competing_write_rejected_by_sqlite_lock", mutation=kind,
                              sqlite_errorcode=error.sqlite_errorcode, durable_rows_unchanged=True)
                else:
                    observations.append({"unexpected_commit_inside_gate": True})
                    self.fail("competing writer committed between final gate read and native DONE write")
                finally:
                    first_attempt_finished.set()
                self.wait(allow_post_completion_write)
                self.assertEqual(connection.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone()[0], "DONE")
                self.mark("later_writer_observed_committed_done", mutation=kind)
                self.mutate(connection, kind)
                connection.commit()
                self.mark("later_writer_committed", mutation=kind)
            finally:
                first_attempt_finished.set()
                connection.close()

        def gate_rendezvous(task, snapshot, required=None):
            result = actual_gate(task, snapshot, required)
            # _task_detail also calls this helper after writing DONE. Only the
            # real _complete_task gate for the still-VERIFYING row is paused.
            if task["id"] == TASK and task["state"] == "VERIFYING":
                self.assertTrue(result["eligible"])
                self.mark("final_gate_read_inside_begin_immediate", mutation=kind, eligible=True,
                          revision=next(v["revision"] for v in snapshot["versions"] if v["task_id"] == TASK))
                at_gate.wait()
                self.wait(first_attempt_finished)
                self.assertEqual(observations, [{"sqlite_errorcode": sqlite3.SQLITE_BUSY, "sqlite_errorname": "SQLITE_BUSY"}])
                self.assertEqual(self.rows(), before)
                self.mark("final_gate_released_for_done_write", mutation=kind)
            return result

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="synthetic-competing-writer") as executor:
            future = executor.submit(competing_writer)
            try:
                with patch.object(service_module, "completion_basis", side_effect=gate_rendezvous):
                    result = self.service.transition(TASK, "DONE", force=True,
                                                     dedupe_key="synthetic-race-completion-" + kind)
                # Returning from the actual service call means its context
                # manager committed; confirm through an independent connection.
                self.assertEqual(self.native()["state"], "DONE")
                committed_gate = self.service.completion_eligibility(TASK)
                self.assertTrue(committed_gate["eligible"])
                self.assertEqual(result["state"], "DONE")
                event = committed_gate["last_completion_event"]
                self.assertIsNotNone(event)
                self.assertTrue(json.loads(event["payload_json"])["completion_basis"]["eligible"])
                self.mark("done_transaction_committed_and_independently_read", mutation=kind,
                          revision=result["revision"], eligible=True, event_id=event["event_id"])
            finally:
                allow_post_completion_write.set()
            future.result(timeout=WAIT_SECONDS)
        self.assert_mutation_visible(kind)
        self.assertEqual(self.native()["state"], "DONE")
        names = [item["event"] for item in self.trace]
        for earlier, later in (
            ("final_gate_read_inside_begin_immediate", "competing_write_attempted"),
            ("competing_write_rejected_by_sqlite_lock", "final_gate_released_for_done_write"),
            ("final_gate_released_for_done_write", "done_transaction_committed_and_independently_read"),
            ("done_transaction_committed_and_independently_read", "later_writer_committed"),
        ):
            self.assertLess(names.index(earlier), names.index(later))
        self.mark("linearization_proved", order_description="DONE commit precedes separately released later native mutation",
                  later_pending_data_may_exist=True, no_retroactive_gate_claim=True)

    def test_user_action_writer_cannot_commit_between_final_gate_and_done(self):
        self._writer_contends_after_gate("user_action")

    def test_required_dependency_writer_cannot_commit_between_final_gate_and_done(self):
        self._writer_contends_after_gate("required_dependency")

    def test_evidence_withdrawal_writer_cannot_commit_between_final_gate_and_done(self):
        self._writer_contends_after_gate("evidence_withdrawal")

    def _writer_commits_before_final_transaction(self, kind):
        preliminary = self.service.completion_eligibility(TASK)
        self.assertTrue(preliminary["eligible"])
        self.mark("preliminary_eligible_read", mutation=kind)
        start_writer = threading.Barrier(2, timeout=WAIT_SECONDS)
        committed = threading.Event()
        def writer():
            try:
                start_writer.wait()
                with self.db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self.mutate(connection, kind)
                self.mark("pending_mutation_committed_before_final", mutation=kind)
            finally:
                committed.set()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="synthetic-earlier-writer") as executor:
            future = executor.submit(writer)
            start_writer.wait()
            self.wait(committed)
            future.result(timeout=WAIT_SECONDS)
        before_final = self.rows()
        self.assert_mutation_visible(kind)
        with self.assertRaises(OperationsError) as raised:
            self.service.transition(TASK, "DONE", force=True, dedupe_key="synthetic-preliminary-" + kind)
        self.assertEqual((raised.exception.code, raised.exception.status), ("stale", 409))
        self.assertEqual(self.native()["state"], "VERIFYING")
        self.assertEqual(self.rows(), before_final)
        self.assertIsNone(self.service.completion_eligibility(TASK)["last_completion_event"])
        self.mark("final_done_rejected_without_partial_writes", mutation=kind, status=409,
                  durable_sha256=digest(before_final), linearization="pending mutation committed before final transaction")

    def test_preliminary_eligible_then_committed_user_action_rejects_final_done(self):
        self._writer_commits_before_final_transaction("user_action")

    def test_preliminary_eligible_then_committed_dependency_rejects_final_done(self):
        self._writer_commits_before_final_transaction("required_dependency")

    def test_preliminary_eligible_then_committed_evidence_withdrawal_rejects_final_done(self):
        self._writer_commits_before_final_transaction("evidence_withdrawal")

    def _audit_failure(self, failure):
        with self.db.connect() as connection:
            connection.execute("CREATE TRIGGER synthetic_done_audit_failure BEFORE INSERT ON events "
                               "WHEN NEW.event_type='task.state_changed' AND json_extract(NEW.payload_json,'$.state')='DONE' "
                               "BEGIN SELECT RAISE(" + failure + "); END")
        before = self.rows()
        expected = OperationsError if failure == "IGNORE" else sqlite3.IntegrityError
        with self.assertRaises(expected):
            self.service.transition(TASK, "DONE", force=True, dedupe_key="synthetic-audit-" + failure)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.native()["state"], "VERIFYING")
        self.assertIsNone(self.service.completion_eligibility(TASK)["last_completion_event"])
        self.mark("real_sqlite_audit_failure_rolled_back_every_table", failure=failure,
                  durable_sha256=digest(before), native_revision_and_state_unchanged=True)

    def test_real_audit_insert_ignore_false_rolls_back_done_and_revision(self):
        self._audit_failure("IGNORE")

    def test_real_audit_insert_abort_exception_rolls_back_done_and_revision(self):
        self._audit_failure("ABORT,'synthetic audit failure'")

    def test_legitimate_positive_commits_done_with_recorded_gate(self):
        old_revision = self.service.get_task(TASK)["revision"]
        result = self.service.transition(TASK, "DONE", dedupe_key="synthetic-positive-done")
        self.assertEqual((result["state"], result["revision"]), ("DONE", old_revision + 1))
        basis = self.service.completion_eligibility(TASK)
        self.assertTrue(basis["eligible"])
        self.assertEqual(basis["assurance"], COMPLETION_ASSURANCE)
        event = basis["last_completion_event"]
        self.assertIsNotNone(event)
        recorded = json.loads(event["payload_json"])["completion_basis"]
        self.assertEqual(recorded["required"], ["artifact"])
        self.assertTrue(recorded["eligible"])
        self.assertIsNone(recorded["last_completion_event"])
        self.assertEqual(self.db.all("SELECT * FROM runs"), [])
        self.assertEqual(self.db.all("SELECT * FROM authorization_audit"), [])
        self.mark("legitimate_done_committed", revision=result["revision"], event_id=event["event_id"],
                  no_run_or_authorization_created=True)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.append({"test": test.id(), "outcome": "pass"})

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.outcomes.append({"test": test.id(), "outcome": "failure", "traceback": self._exc_info_to_string(error, test)})

    def addError(self, test, error):
        super().addError(test, error)
        self.outcomes.append({"test": test.id(), "outcome": "error", "traceback": self._exc_info_to_string(error, test)})


if __name__ == "__main__":
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(CompletionTransactionRaceTests))
    summary = {"synthetic": True, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
               "tests": result.testsRun, "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
               "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
               "exit_status": 0 if result.wasSuccessful() else 1,
               "elapsed_seconds": round(time.monotonic() - started, 3), "forbidden_attempts": FORBIDDEN_ATTEMPTS,
               "service_module": service_module.__file__, "outcomes": result.outcomes, "traces": TRACE_RESULTS,
               "synchronization": "thread barriers/events and real SQLITE_BUSY; no sleep-based ordering",
               "limits": ["synthetic temporary SQLite only", "no HTTP/browser/live/native session/deployment",
                          "later native mutation after DONE commit is explicitly allowed and observed",
                          "not a production load or arbitrary interleaving proof"]}
    with (EVIDENCE / "race-01-results.json").open("x", encoding="utf-8") as output:
        json.dump(summary, output, ensure_ascii=True, indent=2)
        output.write("\n")
    print("SYNTHETIC_TRANSACTION_SUMMARY " + json.dumps({key: value for key, value in summary.items() if key not in {"outcomes", "traces"}}, sort_keys=True))
    raise SystemExit(summary["exit_status"])
