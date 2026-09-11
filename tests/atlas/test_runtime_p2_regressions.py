from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import io
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qingtian_engine.config import load_policy, runtime_policy
from tests.atlas.capability_fixture import advertised_capabilities
from qingtian_engine.db import Database
from qingtian_engine.importers import import_tasks_markdown
from qingtian_engine.runner import RunManager
from qingtian_engine.server import ControlPlaneHandler, LoopbackThreadingHTTPServer
from qingtian_engine.service import ActionConflict, ControlPlane
from qingtian_engine.worker_entry import run_worker


FIXED_NOW = "2026-09-10T01:00:00+00:00"

# Frozen legacy DDL, transcribed from the independently reviewed schema8 source.
# Do not derive this fixture by removing fields from the implementation SCHEMA.
LEGACY_TASKS = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
    parent_id TEXT REFERENCES tasks(id), source_request_id TEXT,
    title TEXT NOT NULL, short_summary TEXT NOT NULL DEFAULT '',
    scope_summary TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 2,
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    environment TEXT NOT NULL DEFAULT 'local', repository TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '', worker_type TEXT NOT NULL DEFAULT 'cli',
    owner_session TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT 'gpt-5.6-sol',
    reasoning TEXT NOT NULL DEFAULT 'high', speed TEXT NOT NULL DEFAULT 'standard',
    authorization_policy TEXT NOT NULL DEFAULT 'normal', state TEXT NOT NULL DEFAULT 'INBOX',
    action_owner_kind TEXT NOT NULL DEFAULT 'none', action_owner TEXT NOT NULL DEFAULT '',
    action_text TEXT NOT NULL DEFAULT '', action_due TEXT,
    action_sensitive INTEGER NOT NULL DEFAULT 0, execution_mode TEXT NOT NULL DEFAULT 'managed',
    heartbeat_at TEXT, evidence_profile TEXT NOT NULL DEFAULT 'auto',
    blocking_reason TEXT NOT NULL DEFAULT '', risk TEXT NOT NULL DEFAULT '',
    requires_deploy INTEGER NOT NULL DEFAULT 0, next_check_at TEXT,
    imported_from TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, FOREIGN KEY(parent_id) REFERENCES tasks(id)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
LEGACY_RUN_COLUMNS = """
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL, adapter TEXT NOT NULL, command_summary TEXT NOT NULL,
    pid INTEGER, process_group INTEGER, session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'QUEUED', exit_code INTEGER, result_hash TEXT NOT NULL DEFAULT '',
    started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL, retry_of TEXT REFERENCES runs(id),
    failure_kind TEXT NOT NULL DEFAULT '', failure_stage TEXT NOT NULL DEFAULT '',
    failure_type TEXT NOT NULL DEFAULT '', failure_trace_hash TEXT NOT NULL DEFAULT '',
    debug_line_count INTEGER NOT NULL DEFAULT 0
"""


class RuntimeP2RegressionTest(unittest.TestCase):
    def setUp(self):
        capability = advertised_capabilities()
        capability.start()
        self.addCleanup(capability.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="qingtian-runtime-p2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = patch("qingtian_engine.service.utc_now", return_value=FIXED_NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.spawn_guard = patch("subprocess.Popen", side_effect=AssertionError("real subprocess forbidden"))
        self.spawn_guard.start()
        self.addCleanup(self.spawn_guard.stop)
        self.service = ControlPlane(Database(self.root / "control.sqlite3"))

    def action(self, title="Synthetic monotonic action"):
        task = self.service.create_task(title, state="WAITING", model="gpt-6-astra", reasoning="xhigh")
        return self.service.set_human_action(task["id"], "user", "fixture", "Action A")

    def completed_events(self, task_id):
        return self.service.db.all("SELECT * FROM events WHERE task_id=? AND event_type='task.human_action_completed' ORDER BY id", (task_id,))

    def test_fixed_clock_aba_rejects_original_token_without_mutation(self):
        first = self.action()
        self.service.set_human_action(first["id"], "user", "fixture", "Action B")
        latest = self.service.set_human_action(first["id"], "user", "fixture", "Action A")
        self.assertEqual(first["updated_at"], latest["updated_at"])
        self.assertEqual(first["action_text"], latest["action_text"])
        self.assertGreater(latest["action_revision"], first["action_revision"])
        self.assertNotEqual(first["action_version"], latest["action_version"])
        with self.assertRaises(ActionConflict):
            self.service.complete_human_action(first["id"], first["action_version"])
        self.assertEqual(latest, self.service.get_task(first["id"]))
        self.assertEqual([], self.completed_events(first["id"]))

    def test_fixed_clock_identical_reissues_each_have_independent_audit(self):
        first = self.action()
        self.service.complete_human_action(first["id"], first["action_version"])
        self.service.transition(first["id"], "WAITING")
        second = self.service.set_human_action(first["id"], "user", "fixture", "Action A")
        self.assertEqual(first["updated_at"], second["updated_at"])
        self.assertNotEqual(first["action_version"], second["action_version"])
        self.service.complete_human_action(second["id"], second["action_version"])
        events = self.completed_events(first["id"])
        self.assertEqual(2, len(events))
        self.assertEqual(2, len({event["dedupe_key"] for event in events}))
        self.assertTrue(events[0]["dedupe_key"].endswith(first["action_version"]))
        self.assertTrue(events[1]["dedupe_key"].endswith(second["action_version"]))
        changes = self.service.db.all("SELECT * FROM events WHERE task_id=? AND event_type='task.human_action_changed'", (first["id"],))
        self.assertEqual(4, len(changes))  # two assignments plus two clears

    def test_reissuing_same_fields_and_clear_paths_advance_revision(self):
        task = self.action()
        same = self.service.set_human_action(task["id"], "user", "fixture", "Action A")
        self.assertGreater(same["action_revision"], task["action_revision"])
        cleared = self.service.set_human_action(task["id"], "none")
        self.assertGreater(cleared["action_revision"], same["action_revision"])
        self.service.set_human_action(task["id"], "user", "fixture", "Action A")
        before_terminal = self.service.get_task(task["id"])
        terminal = self.service.transition(task["id"], "CANCELED")
        self.assertEqual("none", terminal["action_owner_kind"])
        self.assertGreater(terminal["action_revision"], before_terminal["action_revision"])

    def test_legacy_sql_updates_and_recursive_trigger_setting_remain_monotonic(self):
        task = self.action()
        with self.service.db.connect() as connection:
            connection.execute("PRAGMA recursive_triggers=ON")
            for text in ("Action B", "Action A", "Action A"):
                connection.execute("UPDATE tasks SET action_text=? WHERE id=?", (text, task["id"]))
            # Older compatible SQL writers need not know about the new column.
            connection.execute("UPDATE tasks SET state='VERIFYING' WHERE id=?", (task["id"],))
            connection.execute("UPDATE tasks SET state='WAITING' WHERE id=?", (task["id"],))
        latest = self.service.get_task(task["id"])
        self.assertEqual(task["action_revision"] + 5, latest["action_revision"])
        with self.assertRaises(ActionConflict):
            self.service.complete_human_action(task["id"], task["action_version"])
        self.assertEqual(latest, self.service.get_task(task["id"]))

    def test_revision_persists_across_reopen_without_reconstructing_history(self):
        task = self.action()
        reopened = ControlPlane(Database(self.service.db.path))
        self.assertEqual(task, reopened.get_task(task["id"]))
        updated = reopened.set_human_action(task["id"], "user", "fixture", "Action A")
        self.assertGreater(updated["action_revision"], task["action_revision"])

    def test_unrelated_task_row_update_requires_reload_then_fresh_report_succeeds(self):
        task = self.action()
        self.service.db.execute("UPDATE tasks SET short_summary='New status' WHERE id=?", (task["id"],))
        updated = self.service.get_task(task["id"])
        self.assertEqual(task["action_text"], updated["action_text"])
        with self.assertRaises(ActionConflict):
            self.service.complete_human_action(task["id"], task["action_version"])
        self.assertEqual(updated, self.service.get_task(task["id"]))
        reported = self.service.complete_human_action(task["id"], updated["action_version"])
        self.assertEqual("VERIFYING", reported["state"])
        self.assertEqual(1, len(self.completed_events(task["id"])))

    def test_explicit_revision_writes_cannot_rewind_and_overflow_fails_closed(self):
        task = self.action()
        self.service.db.execute("UPDATE tasks SET action_revision=0 WHERE id=?", (task["id"],))
        advanced = self.service.get_task(task["id"])
        self.assertEqual(task["action_revision"] + 1, advanced["action_revision"])
        with self.service.db.connect() as connection:
            connection.execute("PRAGMA recursive_triggers=ON")
            connection.execute("UPDATE tasks SET action_revision=0 WHERE id=?", (task["id"],))
        self.assertEqual(advanced["action_revision"] + 1, self.service.get_task(task["id"])["action_revision"])
        self.service.db.execute("UPDATE tasks SET action_revision=? WHERE id=?", (2**63 - 1, task["id"]))
        at_limit = self.service.get_task(task["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.set_human_action(task["id"], "user", "fixture", "Action B")
        self.assertEqual(at_limit, self.service.get_task(task["id"]))

    def test_schema8_and9_migrations_preserve_every_prior_field_and_are_idempotent(self):
        for version in (8, 9):
            with self.subTest(version=version):
                path = self.root / ("schema" + str(version) + ".sqlite3")
                with closing(sqlite3.connect(path)) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.executescript(LEGACY_TASKS)
                    run_fields = LEGACY_RUN_COLUMNS
                    if version == 9:
                        run_fields += ", model TEXT NOT NULL DEFAULT '', reasoning TEXT NOT NULL DEFAULT '', speed TEXT NOT NULL DEFAULT ''"
                    connection.execute("CREATE TABLE runs (" + run_fields + ", UNIQUE(task_id, attempt))")
                    connection.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(version),))
                    connection.execute("INSERT INTO tasks(id,idempotency_key,title,state,action_owner_kind,action_text,created_at,updated_at) VALUES('legacy','legacy','Historical task','WAITING','user','Historical pending action','original-created','original-updated')")
                    connection.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,session_id,status,created_at) VALUES('old-run','legacy',1,'cli','Historical command','old-session','FAILED','original-created')")
                    if version == 9:
                        connection.execute("UPDATE runs SET model='gpt-6-astra', reasoning='xhigh', speed='standard'")
                    before_task = dict(connection.execute("SELECT * FROM tasks").fetchone())
                    before_run = dict(connection.execute("SELECT * FROM runs").fetchone())
                    connection.commit()
                database = Database(path)
                database.initialize()
                first_task = database.one("SELECT * FROM tasks")
                first_run = database.one("SELECT * FROM runs")
                database.initialize()
                self.assertEqual(first_task, database.one("SELECT * FROM tasks"))
                self.assertEqual(first_run, database.one("SELECT * FROM runs"))
                self.assertEqual(before_task, {key: first_task[key] for key in before_task})
                self.assertEqual(before_run, {key: first_run[key] for key in before_run})
                self.assertEqual(0, first_task["action_revision"])
                if version == 8:
                    self.assertEqual(("", "", ""), (first_run["model"], first_run["reasoning"], first_run["speed"]))
                self.assertEqual("11", database.one("SELECT value FROM meta WHERE key='schema_version'")["value"])
                database.execute("UPDATE tasks SET action_text=action_text WHERE id='legacy'")
                self.assertEqual(1, database.one("SELECT action_revision FROM tasks")["action_revision"])

    def test_false_audit_insert_rolls_back_assignment_and_completion(self):
        task = self.action()
        for operation in (
            lambda: self.service.set_human_action(task["id"], "user", "fixture", "Action B"),
            lambda: self.service.complete_human_action(task["id"], task["action_version"]),
        ):
            with patch.object(self.service.db, "add_event", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "audit was not inserted"):
                    operation()
            self.assertEqual(task, self.service.get_task(task["id"]))

    def test_later_false_audit_insert_rolls_back_prior_audit_inserts(self):
        task = self.action()
        original_add = self.service.db.add_event
        for failure_index in (2, 3):
            with self.subTest(failure_index=failure_index):
                calls = []
                def insert_until_failure(*args, **kwargs):
                    calls.append(args[1])
                    if len(calls) == failure_index:
                        return False
                    return original_add(*args, **kwargs)
                with patch.object(self.service.db, "add_event", side_effect=insert_until_failure):
                    with self.assertRaisesRegex(RuntimeError, "audit was not inserted"):
                        self.service.complete_human_action(task["id"], task["action_version"])
                self.assertEqual(failure_index, len(calls))
                self.assertEqual(task, self.service.get_task(task["id"]))

    def test_real_dedupe_collision_is_an_error_and_preserves_task(self):
        task = self.action()
        self.service.db.add_event(task["id"], "task.human_action_completed", "fixture", "reserved collision", "task.human_action_completed:{}:{}".format(task["id"], task["action_version"]))
        original = self.service.get_task(task["id"])
        with self.assertRaisesRegex(RuntimeError, "audit was not inserted"):
            self.service.complete_human_action(task["id"], task["action_version"])
        self.assertEqual(original, self.service.get_task(task["id"]))

    def test_modification_holds_transaction_then_old_confirmation_conflicts(self):
        task = self.action()
        modified, release, confirm_started = threading.Event(), threading.Event(), threading.Event()
        failures, outcomes = [], []
        original_add = self.service.db.add_event
        def pause_modifier(*args, **kwargs):
            result = original_add(*args, **kwargs)
            if args[2] == "modifier":
                modified.set()
                if not release.wait(3):
                    raise RuntimeError("modifier was not released")
            return result
        def modify():
            try:
                self.service.set_human_action(task["id"], "user", "fixture", "Action B", producer="modifier")
            except BaseException as exc:
                failures.append(exc)
        def confirm():
            confirm_started.set()
            try:
                self.service.complete_human_action(task["id"], task["action_version"])
                outcomes.append("unexpected success")
            except ActionConflict:
                outcomes.append("conflict")
            except BaseException as exc:
                failures.append(exc)
        with patch.object(self.service.db, "add_event", side_effect=pause_modifier):
            writer = threading.Thread(target=modify)
            writer.start()
            self.assertTrue(modified.wait(3))
            reader = threading.Thread(target=confirm)
            reader.start()
            self.assertTrue(confirm_started.wait(3))
            release.set()
            for thread in (writer, reader):
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(["conflict"], outcomes)
        latest = self.service.get_task(task["id"])
        self.assertEqual("Action B", latest["action_text"])
        self.assertEqual("WAITING", latest["state"])
        self.assertEqual([], self.completed_events(task["id"]))

    def test_confirmation_holds_transaction_then_reissue_is_not_cleared(self):
        task = self.action()
        confirming, release, writer_started = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_add = self.service.db.add_event
        def pause_confirmation(*args, **kwargs):
            result = original_add(*args, **kwargs)
            if args[1] == "task.human_action_completed":
                confirming.set()
                if not release.wait(3):
                    raise RuntimeError("confirmation was not released")
            return result
        def confirm():
            try:
                self.service.complete_human_action(task["id"], task["action_version"])
            except BaseException as exc:
                errors.append(exc)
        def modify():
            writer_started.set()
            try:
                self.service.set_human_action(task["id"], "user", "fixture", "New Action A")
            except BaseException as exc:
                errors.append(exc)
        with patch.object(self.service.db, "add_event", side_effect=pause_confirmation):
            confirmer = threading.Thread(target=confirm)
            confirmer.start()
            self.assertTrue(confirming.wait(3))
            writer = threading.Thread(target=modify)
            writer.start()
            self.assertTrue(writer_started.wait(3))
            release.set()
            for thread in (confirmer, writer):
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        latest = self.service.get_task(task["id"])
        self.assertEqual("New Action A", latest["action_text"])
        self.assertEqual("user", latest["action_owner_kind"])
        self.assertEqual(1, len(self.completed_events(task["id"])))
        with self.assertRaises(ActionConflict):
            self.service.complete_human_action(task["id"], task["action_version"])

    def test_fixed_clock_aba_returns_http_409_with_zero_mutation(self):
        task = self.action()
        self.service.set_human_action(task["id"], "user", "fixture", "Action B")
        latest = self.service.set_human_action(task["id"], "user", "fixture", "Action A")
        handler = type("IsolatedP2Handler", (ControlPlaneHandler,), {"service": self.service, "coordinator": None})
        server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        try:
            connection.request("POST", "/api/tasks/" + task["id"] + "/complete-human-action",
                               json.dumps({"expected_action_version": task["action_version"]}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(409, response.status)
            self.assertIn("changed", json.loads(response.read())["error"])
        finally:
            connection.close()
        self.assertEqual(latest, self.service.get_task(task["id"]))

    def test_import_is_idempotent_and_keeps_action_revision_baseline(self):
        path = self.root / "TASKS.md"
        path.write_text("## Waiting\n- [ ] Synthetic imported fixture\n")
        self.assertEqual({"imported": 1, "skipped": 0}, import_tasks_markdown(self.service, path))
        task = self.service.list_tasks()[0]
        self.assertEqual(0, task["action_revision"])
        self.assertEqual("reference-only", task["authorization_policy"])
        self.assertEqual({"imported": 0, "skipped": 1}, import_tasks_markdown(self.service, path, force=True))
        self.assertEqual(task, self.service.list_tasks()[0])

    def resume_fixture(self, speed):
        task = self.service.create_task("Synthetic resume " + repr(speed), state="FAILED", model="gpt-6-astra", reasoning="xhigh")
        if speed in {"standard", "fast"}:
            self.service.db.execute("UPDATE tasks SET speed=? WHERE id=?", (speed, task["id"]))
        run_id = "previous-" + task["id"]
        self.service.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,model,reasoning,speed,session_id,status,created_at) VALUES(?,?,1,'cli','historical selection','gpt-6-astra','xhigh',?,'fixture-session','FAILED',?)", (run_id, task["id"], speed, FIXED_NOW))
        return task, run_id

    def capture_worker_command(self, task_id, run_id):
        prompt = self.root / (run_id + ".txt")
        prompt.write_text("Synthetic only; no model execution")
        stdin = io.StringIO()
        stdin.close = lambda: None
        process = SimpleNamespace(stdin=stdin, stdout=iter([]), wait=lambda: 1)
        args = argparse.Namespace(db=str(self.service.db.path), data_dir=str(self.root), task=task_id, run=run_id, prompt_file=str(prompt), resume=True)
        with patch("qingtian_engine.worker_entry._validate_registered_workspace", return_value=self.root), patch("qingtian_engine.worker_entry.knowledge_prompt", return_value="No knowledge fixture"), patch("qingtian_engine.worker_entry.subprocess.Popen", return_value=process) as spawn:
            self.assertEqual(1, run_worker(args))
        self.assertEqual(1, spawn.call_count)
        return spawn.call_args.args[0]

    def test_resume_freezes_speed_both_directions_through_dry_run_new_run_and_worker(self):
        cases = (
            ("standard", datetime(2026, 9, 10, 1, tzinfo=timezone.utc), True, "fast"),
            ("fast", datetime(2026, 9, 10, 13, tzinfo=timezone.utc), False, "standard"),
        )
        for old_speed, fixed_now, configured_fast, current_speed in cases:
            with self.subTest(old_speed=old_speed):
                task, previous_id = self.resume_fixture(old_speed)
                old_run = self.service.db.one("SELECT * FROM runs WHERE id=?", (previous_id,))
                config = load_policy()
                config["defaults"]["executor"]["speed"] = current_speed
                self.assertEqual(current_speed, runtime_policy("xhigh", now=fixed_now, policy=config).speed)
                manager = RunManager(self.service, self.root)
                prompt = self.root / "resume-prompt.txt"
                prompt.write_text("Synthetic resume only")
                task_before = self.service.get_task(task["id"])
                with patch.object(manager, "_resolve_dispatch_repository", side_effect=lambda task, **kwargs: task), patch.dict(os.environ, {"QINGTIAN_MODEL": "gpt-5.6-sol", "QINGTIAN_REASONING": "high"}):
                    dry = manager.dispatch(task["id"], prompt, resume=True, dry_run=True)
                    self.assertEqual(task_before, self.service.get_task(task["id"]))
                    self.assertEqual(old_speed, dry["speed"])
                    with patch("qingtian_engine.runner.subprocess.Popen", return_value=SimpleNamespace(pid=os.getpid())):
                        resumed = manager.dispatch(task["id"], prompt, resume=True)
                self.assertEqual(("gpt-6-astra", "xhigh", old_speed), (resumed["model"], resumed["reasoning"], resumed["speed"]))
                self.assertEqual(old_run, self.service.db.one("SELECT * FROM runs WHERE id=?", (previous_id,)))
                self.assertIn("speed=" + old_speed, resumed["command_summary"])
                # Later task/default changes cannot alter the recorded run.
                self.service.db.execute("UPDATE tasks SET speed=? WHERE id=?", (current_speed, task["id"]))
                with self.assertRaisesRegex(ValueError, "MODEL_PINNING"):
                    self.capture_worker_command(task["id"], resumed["id"])
                self.service.db.execute("UPDATE tasks SET speed=? WHERE id=?", (old_speed, task["id"]))
                command = self.capture_worker_command(task["id"], resumed["id"])
                self.assertIn('service_tier="' + ("priority" if old_speed == "fast" else "default") + '"', command)
                self.assertIn("gpt-6-astra", command)
                self.assertIn('model_reasoning_effort="xhigh"', command)

    def test_legacy_missing_snapshot_refuses_resume_and_preserves_all_rows(self):
        task = self.service.create_task("Synthetic historical run", state="FAILED")
        # The insert-time snapshot trigger did not exist for historical runs.
        with self.service.db.connect() as connection:
            connection.execute("DROP TRIGGER admission_capture_run_target")
            connection.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,session_id,created_at) VALUES('legacy-run',?,1,'cli','original','FAILED','old-session',?)", (task["id"], FIXED_NOW))
        before = self.service.db.all("SELECT * FROM runs")
        manager = RunManager(self.service, self.root)
        with patch.object(manager, "_resolve_dispatch_repository", side_effect=lambda task, **kwargs: task):
            for dry in (True, False):
                with self.assertRaisesRegex(ValueError, "no immutable execution"):
                    manager.dispatch(task["id"], Path(__file__), resume=True, dry_run=dry)
        self.assertEqual(before, self.service.db.all("SELECT * FROM runs"))
        self.assertEqual([], self.service.db.all("SELECT * FROM admission_run_targets"))

    def test_invalid_nonempty_run_speed_is_rejected_without_attempt_or_model_fallback(self):
        for speed in ("turbo", " ", "0"):
            with self.subTest(speed=speed):
                task, previous_id = self.resume_fixture(speed)
                original = self.service.get_task(task["id"])
                manager = RunManager(self.service, self.root)
                with patch.object(manager, "_resolve_dispatch_repository", side_effect=lambda task, **kwargs: task):
                    for dry in (True, False):
                        with self.assertRaisesRegex(ValueError, "MODEL_PINNING"):
                            manager.dispatch(task["id"], Path(__file__), resume=True, dry_run=dry)
                self.assertEqual(original, self.service.get_task(task["id"]))


if __name__ == "__main__":
    unittest.main()
