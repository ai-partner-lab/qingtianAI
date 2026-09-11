"""Fresh synthetic SQLite: real worker helpers, no model/process/network."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qingtian_engine.db import Database
from qingtian_engine.operations_clarity import OperationsError
from qingtian_engine.service import ControlPlane
from qingtian_engine.worker_entry import _TransactionDatabase, _claim_run, _finish_run


TASK = "synthetic-task"
RUN = "synthetic-run"
WHEN = "2001-01-01T00:00:00Z"
FORBIDDEN_ATTEMPTS = []


def deny(name):
    def forbidden(*args, **kwargs):
        FORBIDDEN_ATTEMPTS.append(name)
        raise AssertionError("forbidden test side effect: " + name)
    return forbidden


def rows(connection):
    names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
            for name in names}


class WorkerTransactionOwnership(unittest.TestCase):
    def setUp(self):
        self.guard = ExitStack()
        self.addCleanup(self.guard.close)
        for target in ("socket.socket", "subprocess.Popen", "os.kill"):
            self.guard.enter_context(patch(target, side_effect=deny(target)))
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-p0-transaction-")
        self.addCleanup(self.directory.cleanup)
        self.db = Database(Path(self.directory.name) / "synthetic.sqlite3")
        self.service = ControlPlane(self.db)
        self.evidence_path = Path(self.directory.name) / "absent-spool.json"
        self.guard_start = len(FORBIDDEN_ATTEMPTS)

    def tearDown(self):
        self.assertEqual(FORBIDDEN_ATTEMPTS[self.guard_start:], [])

    def seed(self, state="RUNNING", evidence=True):
        # P1 keeps immutable run-target tombstones after task deletion. Each
        # matrix scenario needs a fresh DB, not reuse of the same run identity.
        self.seed_number = getattr(self, "seed_number", 0) + 1
        self.db = Database(Path(self.directory.name) / ("scenario-" + str(self.seed_number) + ".sqlite3"))
        self.service = ControlPlane(self.db)
        self.db.execute("""INSERT INTO tasks(id,idempotency_key,title,state,evidence_profile,created_at,updated_at)
            VALUES(?,?,?,?,'artifact',?,?)""", (TASK, TASK, "Synthetic task", state, WHEN, WHEN))
        if state in {"RUNNING", "QUEUED"}:
            self.db.execute("""INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,pid,created_at)
                VALUES(?,?,1,'synthetic','no process executed',?,?,?)""",
                (RUN, TASK, state, os.getpid() if state == "RUNNING" else None, WHEN))
        if evidence:
            self.db.execute("INSERT INTO evidence(task_id,kind,value,verified,created_at) VALUES(?,'artifact','synthetic:artifact',1,?)", (TASK, WHEN))

    def snapshot(self):
        with self.db.connect() as connection:
            return rows(connection)

    def finish(self):
        run = self.db.one("SELECT * FROM runs WHERE id=?", (RUN,))
        return _finish_run(self.db, TASK, run, 0, "synthetic-hash", "", self.evidence_path)

    def state(self):
        return self.db.one("SELECT * FROM tasks WHERE id=?", (TASK,))

    def audit_trigger(self, behavior):
        with self.db.connect() as connection:
            connection.execute("""CREATE TRIGGER synthetic_done_audit_failure BEFORE INSERT ON events
                WHEN NEW.event_type='task.state_changed' AND json_extract(NEW.payload_json,'$.state')='DONE'
                BEGIN SELECT RAISE(""" + behavior + "); END")

    def correction(self, service):
        return {"expected_revision": service.get_task(TASK)["revision"], "idempotency_key": "synthetic-correction",
                "changes": {"reason": "Synthetic display correction"}, "correction_reason": "Synthetic review",
                "actor": {"id": "synthetic-reviewer", "origin": "declared_review"},
                "source": {"ref": "synthetic:review", "sha256": "a" * 64,
                           "observed_at": WHEN, "reviewed_at": WHEN, "reviewer": "synthetic-reviewer"}}

    def test_nested_reads_observe_uncommitted_state_and_do_not_commit(self):
        self.seed("VERIFYING")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "outer failure"):
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE tasks SET action_owner_kind='user', action_text='synthetic pending' WHERE id=?", (TASK,))
                txservice = ControlPlane(_TransactionDatabase(self.db, connection))
                read_before = rows(connection)
                self.assertEqual(txservice.get_task(TASK)["action_owner_kind"], "user")
                self.assertFalse(txservice.completion_eligibility(TASK)["eligible"])
                self.assertFalse(txservice.completion_eligibility(TASK, connection)["eligible"])
                self.assertEqual(txservice.operations_clarity.snapshot()["tasks"][0]["action_owner_kind"], "user")
                self.assertEqual(rows(connection), read_before)
                self.assertTrue(connection.in_transaction)
                self.assertEqual(self.state()["action_owner_kind"], "none")
                raise RuntimeError("outer failure")
        self.assertEqual(self.snapshot(), before)

    def test_nested_done_outer_failure_rolls_back_state_version_and_audit(self):
        self.seed("VERIFYING")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "outer failure"):
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                txservice = ControlPlane(_TransactionDatabase(self.db, connection))
                result = txservice.transition(TASK, "DONE")
                self.assertEqual(result["state"], "DONE")
                self.assertTrue(result["completion_basis"]["last_completion_event"])
                self.assertTrue(connection.in_transaction)
                self.assertEqual(self.snapshot(), before)
                raise RuntimeError("outer failure")
        self.assertEqual(self.snapshot(), before)

    def test_completion_audit_false_and_abort_rollback_worker_run_too(self):
        for behavior in ("IGNORE", "ABORT, 'synthetic audit failure'", "ROLLBACK, 'synthetic audit rollback'"):
            with self.subTest(behavior=behavior):
                self.seed()
                self.audit_trigger(behavior)
                before = self.snapshot()
                with self.assertRaises((OperationsError, sqlite3.IntegrityError)):
                    self.finish()
                self.assertEqual(self.snapshot(), before)
                with self.db.connect() as connection:
                    connection.execute("DROP TRIGGER synthetic_done_audit_failure")
                    connection.execute("DELETE FROM tasks WHERE id=?", (TASK,))

    def test_nested_audit_failure_is_atomic_even_if_caller_handles_exception(self):
        self.seed("VERIFYING")
        for behavior in ("IGNORE", "ABORT, 'synthetic audit failure'"):
            with self.subTest(behavior=behavior):
                self.audit_trigger(behavior)
                with self.db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("UPDATE tasks SET title='outer caller work' WHERE id=?", (TASK,))
                    before_operation = rows(connection)
                    txservice = ControlPlane(_TransactionDatabase(self.db, connection))
                    with self.assertRaises((OperationsError, sqlite3.IntegrityError)):
                        txservice.transition(TASK, "DONE")
                    self.assertTrue(connection.in_transaction)
                    self.assertEqual(rows(connection), before_operation)
                    connection.execute("UPDATE tasks SET title='outer caller continued' WHERE id=?", (TASK,))
                self.assertEqual(self.state()["state"], "VERIFYING")
                self.assertEqual(self.state()["title"], "outer caller continued")
                self.db.execute("DROP TRIGGER synthetic_done_audit_failure")

    def test_pending_action_success_is_not_task_done_or_infrastructure_failure(self):
        for kind, sensitive, text in (("user", 0, "pending"), ("external", 0, "pending"),
                                      ("user", 1, "pending approval"), ("none", 1, "")):
            with self.subTest(kind=kind, sensitive=sensitive):
                self.seed()
                self.db.execute("UPDATE tasks SET action_owner_kind=?,action_owner='synthetic-owner',action_text=?,action_sensitive=? WHERE id=?", (kind, text, sensitive, TASK))
                action = {k: self.state()[k] for k in ("action_owner_kind", "action_owner", "action_text", "action_sensitive")}
                self.assertTrue(self.finish())
                self.assertEqual(self.state()["state"], "VERIFYING")
                self.assertIsNone(self.state()["finished_at"])
                self.assertEqual({k: self.state()[k] for k in action}, action)
                run = self.db.one("SELECT * FROM runs WHERE id=?", (RUN,))
                self.assertEqual((run["status"], run["exit_code"], run["failure_kind"]), ("DONE", 0, ""))
                self.assertEqual(self.service.completion_eligibility(TASK)["last_completion_event"], None)
                self.db.execute("DELETE FROM tasks WHERE id=?", (TASK,))

    def test_pending_dependency_success_preserves_dependency(self):
        self.seed()
        self.db.execute("INSERT INTO tasks(id,idempotency_key,title,state,created_at,updated_at) VALUES('synthetic-dependency','synthetic-dependency','dependency','WAITING',?,?)", (WHEN, WHEN))
        self.service.add_dependency(TASK, "synthetic-dependency")
        self.assertTrue(self.finish())
        self.assertEqual(self.state()["state"], "VERIFYING")
        self.assertEqual(len(self.service.completion_eligibility(TASK)["unresolved_dependencies"]), 1)
        self.assertEqual(self.db.one("SELECT status FROM runs WHERE id=?", (RUN,))["status"], "DONE")

    def test_missing_evidence_success_waits_then_can_complete(self):
        self.seed(evidence=False)
        self.assertTrue(self.finish())
        self.assertEqual(self.state()["state"], "VERIFYING")
        self.assertEqual(self.service.completion_eligibility(TASK)["missing"], ["artifact"])
        self.service.add_evidence(TASK, "artifact", "synthetic:artifact", verified=True)
        self.assertEqual(self.service.transition(TASK, "DONE")["state"], "DONE")

    def test_claim_transition_audit_exception_rolls_back_all_rows(self):
        self.seed("QUEUED")
        self.db.execute("""CREATE TRIGGER synthetic_claim_audit_failure BEFORE INSERT ON events
            BEGIN SELECT RAISE(ABORT,'synthetic claim audit failure'); END""")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            _claim_run(self.db, TASK, RUN, os.getpid(), os.getpid())
        self.assertEqual(self.snapshot(), before)

    def test_worker_outer_failure_rolls_back_everything_and_retains_spool(self):
        class FailBeforeCommit(Database):
            @contextmanager
            def connect(inner):
                with super().connect() as connection:
                    yield connection
                    if connection.in_transaction and connection.total_changes:
                        raise RuntimeError("synthetic outer commit failure")

        for state in ("QUEUED", "RUNNING"):
            with self.subTest(state=state):
                self.seed(state)
                failing_db = FailBeforeCommit(self.db.path)
                # This is a newly generated synthetic worker spool, not a live asset.
                self.evidence_path.write_text(json.dumps({"artifact": "synthetic:spooled-artifact"}))
                before = self.snapshot()
                with self.assertRaisesRegex(RuntimeError, "synthetic outer commit failure"):
                    if state == "QUEUED":
                        _claim_run(failing_db, TASK, RUN, os.getpid(), os.getpid())
                    else:
                        run = self.db.one("SELECT * FROM runs WHERE id=?", (RUN,))
                        _finish_run(failing_db, TASK, run, 0, "synthetic-hash", "", self.evidence_path)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(self.evidence_path.exists())
                self.db.execute("DELETE FROM tasks WHERE id=?", (TASK,))

    def test_protected_claim_and_finish_do_not_change_any_rows(self):
        for state, reason in (("PAUSED", ""), ("CANCELED", ""), ("DONE", ""), ("FAILED", ""),
                              ("PLAN_ONLY", ""), ("WAITING", "paused_by_user: synthetic pause")):
            with self.subTest(state=state):
                self.seed()
                self.db.execute("UPDATE tasks SET state=?,blocking_reason=? WHERE id=?", (state, reason, TASK))
                before = self.snapshot()
                self.assertFalse(self.finish())
                self.assertEqual(self.snapshot(), before)
                self.db.execute("UPDATE runs SET status='QUEUED',pid=NULL WHERE id=?", (RUN,))
                before_claim = self.snapshot()
                self.assertIsNone(_claim_run(self.db, TASK, RUN, os.getpid(), os.getpid()))
                self.assertEqual(self.snapshot(), before_claim)
                self.db.execute("DELETE FROM tasks WHERE id=?", (TASK,))

    def test_late_superseded_or_wrong_pid_results_do_not_change_rows(self):
        self.seed()
        self.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) VALUES('synthetic-newer',?,2,'synthetic','not executed','DONE',?)", (TASK, WHEN))
        before = self.snapshot()
        self.assertFalse(self.finish())
        self.assertEqual(self.snapshot(), before)
        self.db.execute("DELETE FROM runs WHERE id='synthetic-newer'")
        self.db.execute("UPDATE runs SET pid=? WHERE id=?", (os.getpid() + 1, RUN))
        before_wrong = self.snapshot()
        self.assertFalse(self.finish())
        self.assertEqual(self.snapshot(), before_wrong)

    def test_duplicate_successful_result_does_not_add_event_or_change_receipt(self):
        self.seed()
        run = self.db.one("SELECT * FROM runs WHERE id=?", (RUN,))
        self.assertTrue(self.finish())
        before_retry = self.snapshot()
        self.assertFalse(_finish_run(self.db, TASK, run, 0, "synthetic-late-hash", "", self.evidence_path))
        self.assertEqual(self.snapshot(), before_retry)

    def test_pause_between_preflight_and_write_lock_is_rechecked(self):
        self.seed()
        class PauseBeforeLock:
            def exists(inner):
                self.db.execute("UPDATE tasks SET state='PAUSED' WHERE id=?", (TASK,))
                return False
        self.evidence_path = PauseBeforeLock()
        self.assertFalse(self.finish())
        self.assertEqual(self.state()["state"], "PAUSED")
        self.assertEqual(self.db.one("SELECT status FROM runs WHERE id=?", (RUN,))["status"], "RUNNING")
        self.assertEqual(self.db.all("SELECT * FROM events WHERE task_id=?", (TASK,)), [])

    def test_report_and_correction_obey_outer_rollback(self):
        self.seed("WAITING")
        self.db.execute("UPDATE tasks SET action_owner_kind='user',action_text='ordinary task' WHERE id=?", (TASK,))
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "outer rollback"):
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                txservice = ControlPlane(_TransactionDatabase(self.db, connection))
                payload = self.correction(txservice)
                preview = txservice.operations_clarity.preview_correction(TASK, payload)
                self.assertTrue(preview["preview"])
                correction = txservice.operations_clarity.apply_correction(TASK, payload)
                retry = txservice.operations_clarity.apply_correction(TASK, payload)
                self.assertTrue(retry["reused"])
                self.assertEqual(retry["correction"], correction["correction"])
                revision = txservice.get_task(TASK)["revision"]
                report = txservice.report_human_action(TASK, revision, "synthetic-report")
                retry = txservice.report_human_action(TASK, revision, "synthetic-report")
                self.assertTrue(retry["reused"])
                self.assertEqual(retry["report"], report["report"])
                self.assertEqual(self.snapshot(), before)
                raise RuntimeError("outer rollback")
        self.assertEqual(self.snapshot(), before)

    def test_nested_sensitive_report_and_stale_cas_remain_rejected(self):
        self.seed("WAITING")
        self.db.execute("UPDATE tasks SET action_owner_kind='user',action_text='sensitive approval',action_sensitive=1 WHERE id=?", (TASK,))
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            txservice = ControlPlane(_TransactionDatabase(self.db, connection))
            revision = txservice.get_task(TASK)["revision"]
            before = rows(connection)
            with self.assertRaises(OperationsError) as sensitive:
                txservice.report_human_action(TASK, revision, "synthetic-sensitive")
            self.assertEqual(sensitive.exception.status, 403)
            with self.assertRaises(OperationsError) as stale:
                txservice.report_human_action(TASK, revision + 1, "synthetic-stale")
            self.assertEqual(stale.exception.status, 409)
            self.assertEqual(rows(connection), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
