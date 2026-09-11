"""Targeted synthetic inverse regressions. No sockets, processes or live data."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import platform
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qingtian_engine.db import Database, SCHEMA
from qingtian_engine.operations_clarity import (
    ASSURANCE, COMPLETION_ASSURANCE, OperationsError, OperationsClarityService,
    ReviewedRecordAdapter, initialize_operations_schema, native_basis, project_operations,
)
from qingtian_engine.releases import initialize_release_schema
from qingtian_engine.operations import operator_status
from qingtian_engine.server import ControlPlaneHandler
from qingtian_engine.service import ControlPlane

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(tempfile.gettempdir())
WHEN = "2026-09-01T01:00:00+00:00"
REVIEWED = "2026-09-01T01:01:00+00:00"
RECEIVED = "2026-09-01T01:02:00+00:00"
ADMITTED = "2026-09-01T02:00:00+00:00"


def database_rows(db, names=None):
    with db.connect() as connection:
        if names is None:
            names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                for name in names}


class SyntheticCase(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "subprocess.Popen", "os.kill"):
            guard = patch(target, side_effect=AssertionError("synthetic suite forbids " + target))
            guard.start()
            self.addCleanup(guard.stop)
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-operations-")
        self.addCleanup(self.directory.cleanup)
        self.db = Database(Path(self.directory.name) / "synthetic.sqlite3")
        self.service = ControlPlane(self.db)
        self.clarity = self.service.operations_clarity

    def seed(self, task_id="synthetic-task-1", **overrides):
        values = dict(id=task_id, idempotency_key=task_id, title="Synthetic task", state="WAITING",
                      evidence_profile="artifact", created_at=WHEN, updated_at=WHEN,
                      blocking_reason="Synthetic native reason", action_text="Synthetic native next action")
        values.update(overrides)
        self.db.execute("INSERT INTO tasks(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", values.values())
        return task_id

    def raw(self, task_id="synthetic-task-1"):
        return self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def revision(self, task_id="synthetic-task-1"):
        return self.clarity.get_task(task_id)["revision"]

    def correction(self, task_id="synthetic-task-1", key="synthetic-correction-1", **overrides):
        body = {"idempotency_key": key, "expected_revision": self.revision(task_id),
                "changes": {"reason": "Synthetic corrected reason", "next_action": "Synthetic corrected next step"},
                "correction_reason": "Synthetic independently declared receipt",
                "actor": {"id": "synthetic-reviewer", "origin": "declared_business_review"},
                "source": {"ref": "synthetic:receipt-1", "sha256": "a" * 64,
                           "observed_at": WHEN, "reviewed_at": REVIEWED, "reviewer": "synthetic-reviewer"}}
        body.update(overrides)
        return body

    def record(self, task_id="synthetic-task-1", **overrides):
        version = self.db.one("SELECT * FROM operations_task_versions WHERE task_id=?", (task_id,))
        result = {"task_id": task_id, "record_id": "synthetic-record-" + task_id,
                  "received_at": RECEIVED, "observed_at": WHEN,
                  "source": {"origin": "declared_business_review", "ref": "synthetic:" + task_id,
                             "sha256": "b" * 64, "reviewer": "synthetic-reviewer", "reviewed_at": REVIEWED},
                  "basis_revision": version["basis_revision"], "native_basis": native_basis(self.raw(task_id))}
        result.update(overrides)
        return result

    def reviewed(self, records):
        return OperationsClarityService(self.db.connect, ReviewedRecordAdapter(records, admitted_at=ADMITTED))

    def blocker(self, channel="synthetic-task-1", **overrides):
        value = {"text": "Synthetic scoped blocker", "blocker_key": "synthetic-blocker",
                 "scope": {"project_id": "synthetic-project", "environment": "synthetic-env",
                           "target": "synthetic-target", "authorization_scope": "synthetic-grant"},
                 "decision": "synthetic-required-decision", "channel": {"kind": "task", "task_id": channel}}
        value.update(overrides)
        return value

    def evidence(self, task_id="synthetic-task-1", kind="artifact", value="synthetic:evidence", verified=1):
        self.db.execute("INSERT INTO evidence(task_id,kind,value,verified,created_at) VALUES(?,?,?,?,?)",
                        (task_id, kind, value, verified, WHEN))

    def error(self, code, status, call):
        with self.assertRaises(OperationsError) as caught:
            call()
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.assertEqual(caught.exception.payload()["assurance"], ASSURANCE)

    def report(self, task_id="synthetic-task-1", key="synthetic-report-1", revision=None):
        return self.service.report_human_action(task_id, expected_revision=self.revision(task_id) if revision is None else revision,
                                                  idempotency_key=key)

    def codes(self, task):
        return {w["code"] for w in task["warnings"]}


class ProjectionTests(SyntheticCase):
    def test_known_pause_policy_defers_reviewed_urgent_task_and_prevents_group(self):
        paused = self.seed(blocking_reason="PAUSED_BY_USER: synthetic explicit pause")
        active = self.seed("synthetic-active-scope")
        records = [self.record(task_id, classification="external_blocked", blocker=self.blocker()) for task_id in (paused, active)]
        before = database_rows(self.db)
        result = project_operations(self.clarity.snapshot(), records, admission_time=ADMITTED)
        item = next(t for t in result["tasks"] if t["id"] == paused)
        self.assertEqual((item["state"], item["category"], item["urgent"]), ("WAITING", "deferred", False))
        self.assertEqual(item["classification_source"]["origin"], "existing_pause_policy")
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["counts"]["deferred"], 1)
        self.assertEqual(result["counts"]["external_blocked"], 1)
        self.assertEqual(database_rows(self.db), before)

    def test_empty_resolution_blocker_key_never_establishes_supersession(self):
        task_id = self.seed()
        for key in ("", "   "):
            old = self.record(classification="external_blocked", blocker=self.blocker(blocker_key=key))
            new = self.record(record_id="synthetic-unbound-resolution", classification="internal", blocker=self.blocker(blocker_key=key))
            new["resolution"] = {"task_id": task_id, "blocker_key": key, "scope": old["blocker"]["scope"],
                                 "supersedes": {"record_id": old["record_id"], "field": "blocker", "value": old["blocker"]["text"],
                                                "sha256": hashlib.sha256(old["blocker"]["text"].encode()).hexdigest(),
                                                "source_ref": old["source"]["ref"], "observed_at": WHEN},
                                 "evidence": {"ref": "synthetic:unbound-receipt", "sha256": "c" * 64, "observed_at": WHEN}}
            item = self.reviewed([old, new]).get_task(task_id)
            self.assertEqual(item["category"], "unclassified")
            self.assertIn("resolution_pending_cross_check", self.codes(item))
            self.assertNotIn("old_explanation_needs_review", self.codes(item))

    def test_default_empty_adapter_is_read_only_and_never_dashboard(self):
        self.seed(owner_session="synthetic-role-not-session", action_owner_kind="user")
        before = database_rows(self.db)
        with patch.object(self.service, "dashboard_payload", side_effect=AssertionError("dashboard forbidden")):
            result = self.clarity.list_tasks()
        self.assertEqual(database_rows(self.db), before)
        self.assertEqual(result["source_status"], "adapter_not_configured")
        self.assertEqual(result["published"], {"source": "/api/release-batches", "count": None})
        task = result["tasks"][0]
        self.assertEqual(task["category"], "unclassified")
        self.assertFalse(task["urgent"])
        self.assertEqual(task["owner"], {"value": None, "source": None})
        self.assertIsNone(task["meaningful_progress"]["value"])
        self.assertIsNone(task["blocker"]["source"]["observed_at"])
        self.assertEqual(task["legacy"]["blocking_reason"], "Synthetic native reason")
        for field in ("completed", "blocker", "next_action", "owner", "meaningful_progress"):
            self.assertEqual(set(task[field]), {"value", "source"})

    def test_six_categories_are_reviewed_and_history_not_published(self):
        records = []
        for category in ("executing", "internal", "pending_release", "user_action", "external_blocked", "deferred"):
            task_id = self.seed("synthetic-" + category)
            records.append(self.record(task_id, classification=category))
        self.seed("synthetic-done", state="DONE")
        self.seed("synthetic-canceled", state="CANCELED")
        result = self.reviewed(records).list_tasks()
        for record in records:
            item = next(t for t in result["tasks"] if t["id"] == record["task_id"])
            self.assertEqual(item["category"], record["classification"])
        self.assertEqual(result["counts"]["history"], 2)
        self.assertIsNone(result["published"]["count"])

    def test_paused_and_deferred_cannot_become_urgent(self):
        task_id = self.seed(state="PAUSED")
        result = self.reviewed([self.record(task_id, classification="user_action")]).get_task(task_id)
        self.assertEqual((result["category"], result["urgent"]), ("deferred", False))
        self.db.execute("UPDATE tasks SET state='WAITING' WHERE id=?", (task_id,))
        result = self.reviewed([self.record(task_id, classification="deferred")]).get_task(task_id)
        self.assertFalse(result["urgent"])

    def test_all_five_reviewed_fields_have_real_declared_sources(self):
        task_id = self.seed()
        record = self.record(classification="internal", completed="Synthetic finished artifact", blocker=self.blocker(),
                             next_action="Synthetic explicit next action", owner={"id": "synthetic-owner", "name": "Synthetic owner"},
                             meaningful_progress={"kind": "artifact_created", "ref": "synthetic:artifact", "occurred_at": WHEN})
        task = self.reviewed([record]).get_task(task_id)
        for key in ("completed", "blocker", "next_action", "owner", "meaningful_progress"):
            self.assertIsNotNone(task[key]["value"])
            self.assertEqual(task[key]["source"]["record_id"], record["record_id"])
        self.assertEqual(task["category"], "internal")
        self.assertFalse(task["urgent"])

    def test_malformed_future_missing_time_or_verified_only_are_not_trusted(self):
        task_id = self.seed()
        valid = self.record(classification="executing")
        mutations = [dict(valid, source={"verified": True}), dict(valid, observed_at="2999-01-01T00:00:00Z"),
                     dict(valid, observed_at="2026-09-01"), dict(valid, owner=["synthetic-role"]),
                     dict(valid, classification="internal approval"), dict(valid, arbitrary_import={"state": "DONE"}),
                     {k: v for k, v in valid.items() if k != "received_at"}]
        for record in mutations:
            with self.subTest(record=record):
                item = self.reviewed([record]).get_task(task_id)
                self.assertEqual(item["category"], "unclassified")
                self.assertIn("invalid_source", self.codes(item))

    def test_fixed_admission_never_promotes_known_future_and_adapter_copies_input(self):
        task_id = self.seed()
        record = self.record(classification="executing", received_at="2999-01-01T00:00:00Z")
        adapter = self.reviewed([record])
        first = adapter.get_task(task_id)
        record["received_at"] = RECEIVED
        with patch("qingtian_engine.operations_clarity.datetime") as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = datetime(3000, 1, 1, tzinfo=timezone.utc)
            second = adapter.get_task(task_id)
        self.assertEqual(first, second)
        self.assertIn("invalid_source", self.codes(second))
        self.assertEqual(project_operations(self.clarity.snapshot(), [self.record(classification="executing")])["tasks"][0]["category"], "unclassified")

    def test_heartbeats_polling_and_update_prose_never_mean_progress(self):
        task_id = self.seed(heartbeat_at=REVIEWED, updated_at=RECEIVED)
        for kind in ("heartbeat", "poll", "updated_at", "event_prose"):
            task = self.reviewed([self.record(meaningful_progress={"kind": kind, "ref": "synthetic:event", "occurred_at": WHEN})]).get_task(task_id)
            self.assertIsNone(task["meaningful_progress"]["value"])

    def test_source_basis_changes_and_aba_invalidate_records(self):
        task_id = self.seed()
        service = self.reviewed([self.record(classification="executing", blocker=self.blocker())])
        for sql in ("UPDATE tasks SET state='VERIFYING'", "UPDATE tasks SET state='WAITING'"):
            self.db.execute(sql)
            item = service.get_task(task_id)
            self.assertEqual(item["category"], "unclassified")
            self.assertEqual(item["blocker"]["value"], "Synthetic native reason")
            self.assertIn("stale_source", self.codes(item))

    def test_ambiguous_records_and_duplicate_identity_fail_closed(self):
        task_id = self.seed()
        first = self.record(classification="executing")
        for second in (dict(first, record_id="synthetic-another", classification="user_action"), dict(first, classification="deferred")):
            task = self.reviewed([first, second]).get_task(task_id)
            self.assertEqual(task["category"], "unclassified")
            self.assertTrue(self.codes(task) & {"source_conflict", "invalid_source"})

    def test_scoped_groups_require_exact_scope_channel_decision(self):
        one, two = self.seed(), self.seed("synthetic-task-2")
        first = self.record(one, classification="external_blocked", blocker=self.blocker())
        second = self.record(two, classification="external_blocked", blocker=self.blocker())
        result = self.reviewed([first, second]).list_tasks()
        self.assertEqual(result["groups"][0]["task_ids"], [one, two])
        for key in ("project_id", "environment", "target", "authorization_scope"):
            altered = copy.deepcopy(second)
            altered["blocker"]["scope"][key] = "synthetic-other"
            self.assertEqual(self.reviewed([first, altered]).list_tasks()["groups"], [])
        for key, value in (("decision", "synthetic-conflict"), ("channel", {"kind": "task", "task_id": two})):
            altered = copy.deepcopy(second)
            altered["blocker"][key] = value
            result = self.reviewed([first, altered]).list_tasks()
            self.assertEqual(result["groups"], [])
            self.assertIn("group_conflict", self.codes(result["tasks"][0]))

    def test_incomplete_scope_or_unknown_channel_is_separate(self):
        task_id = self.seed()
        for blocker in ({"text": "Synthetic missing scope"}, self.blocker(channel="synthetic-absent"),
                        self.blocker(scope={"project_id": "synthetic-project"})):
            result = self.reviewed([self.record(classification="external_blocked", blocker=blocker)]).list_tasks()
            self.assertEqual(result["groups"], [])
            self.assertEqual(result["tasks"][0]["category"], "external_blocked")
            self.assertIn("ungrouped_scope", self.codes(result["tasks"][0]))

    def test_valid_lineage_preserves_cancellation_and_is_task_only(self):
        old, new = self.seed(state="CANCELED", owner_session="synthetic-role"), self.seed("synthetic-task-2")
        self.evidence(old, "successor", new, 0)
        self.evidence(new, "predecessor", old, 0)
        before = database_rows(self.db)
        task = self.clarity.get_task(old)
        self.assertEqual(task["state"], "CANCELED")
        self.assertEqual(task["lineage"]["status"], "valid")
        self.assertEqual(task["lineage"]["successor_task_id"], new)
        self.assertEqual(task["lineage"]["reason"], "已由新任务接续")
        self.assertEqual(database_rows(self.db), before)

    def test_invalid_lineage_missing_self_ambiguous_and_cycle(self):
        old, new, third = self.seed(state="CANCELED"), self.seed("synthetic-task-2"), self.seed("synthetic-task-3")
        scenarios = [[(old, "successor", "synthetic-missing")], [(old, "successor", old)], [(old, "successor", new)],
                     [(old, "successor", new), (old, "successor", third), (new, "predecessor", old)],
                     [(old, "successor", new), (new, "predecessor", old), (new, "successor", old), (old, "predecessor", new)]]
        for edges in scenarios:
            self.db.execute("DELETE FROM evidence")
            for task_id, kind, value in edges:
                self.evidence(task_id, kind, value)
            lineage = self.clarity.get_task(old)["lineage"]
            self.assertNotEqual(lineage["status"], "valid")
            self.assertIsNone(lineage["successor_task_id"])

    def test_exact_resolution_warns_and_retains_raw_old_provenance(self):
        task_id = self.seed()
        old = self.record(classification="external_blocked", blocker=self.blocker())
        new = self.record(record_id="synthetic-new-record", classification="internal", blocker=self.blocker(text="Synthetic new explanation"))
        new["resolution"] = {"task_id": task_id, "blocker_key": old["blocker"]["blocker_key"], "scope": old["blocker"]["scope"],
                             "supersedes": {"record_id": old["record_id"], "field": "blocker", "value": old["blocker"]["text"],
                                            "sha256": hashlib.sha256(old["blocker"]["text"].encode()).hexdigest(),
                                            "source_ref": old["source"]["ref"], "observed_at": WHEN},
                             "evidence": {"ref": "synthetic:new-evidence", "sha256": "c" * 64, "observed_at": WHEN}}
        task = self.reviewed([old, new]).get_task(task_id)
        self.assertEqual(task["category"], "internal")
        self.assertIn("old_explanation_needs_review", self.codes(task))
        self.assertEqual(task["legacy"]["blocking_reason"], "Synthetic native reason")
        new["resolution"]["supersedes"]["sha256"] = "d" * 64
        task = self.reviewed([old, new]).get_task(task_id)
        self.assertIn("resolution_pending_cross_check", self.codes(task))
        self.assertEqual(task["category"], "unclassified")


class CorrectionTests(SyntheticCase):
    def test_preview_is_exact_read_only_and_write_preserves_all_native_tables(self):
        task_id = self.seed(action_owner_kind="user", action_sensitive=1)
        body = self.correction()
        before = database_rows(self.db)
        preview = self.clarity.preview_correction(task_id, body)
        self.assertEqual(database_rows(self.db), before)
        self.assertEqual(set(preview), {"task", "correction", "reused", "preview"})
        self.assertEqual(preview["task"]["revision"], 1)
        self.assertEqual(preview["correction"]["native_baseline"]["native"], native_basis(self.raw()))
        self.assertIsNone(preview["correction"]["id"])
        self.assertEqual(preview["correction"]["old_values"]["reason"], "Synthetic native reason")
        result = self.clarity.apply_correction(task_id, body)
        self.assertEqual(result["task"]["revision"], 2)
        self.assertEqual(result["task"]["blocker"]["value"], body["changes"]["reason"])
        legacy = [name for name in before if not name.startswith("operations_")]
        self.assertEqual(database_rows(self.db, legacy), {k: before[k] for k in legacy})
        self.assertEqual(self.raw()["action_sensitive"], 1)
        self.assertEqual(result["task"]["category"], preview["task"]["category"])

    def test_empty_override_does_not_unlock_or_remove_native_blocker(self):
        task_id = self.seed(action_owner_kind="user", action_sensitive=1)
        self.clarity.apply_correction(task_id, self.correction())
        result = self.clarity.apply_correction(task_id, self.correction(key="synthetic-clear", changes={"reason": "", "next_action": ""}))
        self.assertEqual(result["task"]["blocker"]["value"], "Synthetic native reason")
        self.assertEqual(result["task"]["next_action"]["value"], "Synthetic native next action")
        self.assertFalse(result["task"]["completion_basis"]["eligible"])
        self.assertEqual(self.raw()["action_sensitive"], 1)

    def test_partial_update_preserves_each_field_original_source(self):
        task_id = self.seed()
        first = self.clarity.apply_correction(task_id, self.correction(changes={"reason": "Synthetic corrected blocker"}))
        second = self.clarity.apply_correction(task_id, self.correction(key="synthetic-next", changes={"next_action": "Synthetic next"}))
        self.assertEqual(second["task"]["blocker"]["source"]["correction_id"], first["correction"]["id"])
        self.assertEqual(second["task"]["next_action"]["source"]["correction_id"], second["correction"]["id"])

    def test_exact_retry_before_cas_is_immutable_and_changed_payload_is_409(self):
        task_id = self.seed()
        body = self.correction()
        first = self.clarity.apply_correction(task_id, body)
        self.db.execute("UPDATE tasks SET heartbeat_at=?", (RECEIVED,))
        before = database_rows(self.db)
        again = self.clarity.apply_correction(task_id, body)
        self.assertTrue(again["reused"])
        self.assertEqual(again["correction"], first["correction"])
        self.assertEqual(database_rows(self.db), before)
        altered = {**body, "correction_reason": "Synthetic altered payload"}
        self.error("idempotency", 409, lambda: self.clarity.apply_correction(task_id, altered))
        self.assertEqual(database_rows(self.db), before)

    def test_native_changes_state_reason_action_owner_permission_all_invalidate(self):
        columns = {"state": "VERIFYING", "blocking_reason": "Synthetic new blocker", "action_text": "Synthetic new next",
                   "action_owner": "synthetic-new-owner", "action_owner_kind": "external", "action_sensitive": 1,
                   "authorization_policy": "synthetic-policy", "environment": "synthetic-other", "repository": "synthetic-repo"}
        for index, (column, value) in enumerate(columns.items()):
            task_id = self.seed("synthetic-change-" + str(index))
            self.clarity.apply_correction(task_id, self.correction(task_id))
            self.db.execute("UPDATE tasks SET " + column + "=? WHERE id=?", (value, task_id))
            task = self.clarity.get_task(task_id)
            self.assertIn("stale_correction", self.codes(task))
            self.assertEqual(task["blocker"]["value"], self.raw(task_id)["blocking_reason"])
            self.assertEqual(task["next_action"]["value"], self.raw(task_id)["action_text"])

    def test_monotonic_noop_aba_recreate_recursive_triggers_on_and_off(self):
        for recursive in (0, 1):
            task_id = self.seed("synthetic-recursive-" + str(recursive))
            self.clarity.apply_correction(task_id, self.correction(task_id))
            with self.db.connect() as connection:
                connection.execute("PRAGMA recursive_triggers=" + str(recursive))
                connection.execute("UPDATE tasks SET state=state WHERE id=?", (task_id,))
            self.assertEqual(self.revision(task_id), 3)
            self.assertNotIn("stale_correction", self.codes(self.clarity.get_task(task_id)))
            with self.db.connect() as connection:
                connection.execute("PRAGMA recursive_triggers=" + str(recursive))
                connection.execute("UPDATE tasks SET state='VERIFYING' WHERE id=?", (task_id,))
                connection.execute("UPDATE tasks SET state='WAITING' WHERE id=?", (task_id,))
            self.assertEqual(self.revision(task_id), 5)
            self.assertIn("stale_correction", self.codes(self.clarity.get_task(task_id)))
            old = self.raw(task_id)
            with self.db.connect() as connection:
                connection.execute("PRAGMA recursive_triggers=" + str(recursive))
                connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                connection.execute("INSERT INTO tasks(" + ",".join(old) + ") VALUES(" + ",".join("?" for _ in old) + ")", tuple(old.values()))
            self.assertEqual(self.revision(task_id), 7)
            self.assertIn("stale_correction", self.codes(self.clarity.get_task(task_id)))
            self.assertEqual(len(self.clarity.get_task(task_id)["corrections"]), 1)

    def test_heartbeat_updates_advance_cas_without_progress_or_rebinding(self):
        task_id = self.seed()
        self.clarity.apply_correction(task_id, self.correction())
        self.db.execute("UPDATE tasks SET heartbeat_at=?,updated_at=?", (REVIEWED, RECEIVED))
        task = self.clarity.get_task(task_id)
        self.assertEqual(task["revision"], 3)
        self.assertEqual(task["blocker"]["value"], "Synthetic corrected reason")
        self.assertIsNone(task["meaningful_progress"]["value"])
        self.assertNotIn("stale_correction", self.codes(task))

    def test_missing_bookkeeping_reads_never_heal_and_posts_fail_closed(self):
        task_id = self.seed()
        body = self.correction()
        self.db.execute("DELETE FROM operations_task_versions WHERE task_id=?", (task_id,))
        before = database_rows(self.db)
        self.assertIsNone(self.clarity.get_task(task_id)["revision"])
        self.error("stale", 409, lambda: self.clarity.apply_correction(task_id, body))
        self.assertEqual(database_rows(self.db), before)
        self.db.initialize()
        self.assertIsNone(self.clarity.get_task(task_id)["revision"])

    def test_bad_fields_types_and_future_corrections_do_not_write(self):
        task_id = self.seed()
        body = self.correction()
        variations = [{**body, "state": "DONE"}, {**body, "changes": {"owner": "synthetic-other"}},
                      {**body, "changes": {}}, {**body, "expected_revision": True}, {**body, "changes": {"reason": "x" * 4001}},
                      {**body, "source": {**body["source"], "verified": True}},
                      {**body, "source": {**body["source"], "observed_at": "2999-01-01T00:00:00Z"}},
                      {**body, "source": {**body["source"], "reviewed_at": ""}}]
        before = database_rows(self.db)
        for candidate in variations:
            self.error("invalid", 400, lambda: self.clarity.apply_correction(task_id, candidate))
        self.assertEqual(database_rows(self.db), before)

    def test_immutable_audit_including_replace_under_both_recursive_settings(self):
        task_id = self.seed()
        self.clarity.apply_correction(task_id, self.correction())
        before = database_rows(self.db)
        for recursive in (0, 1):
            for sql in ("UPDATE operations_corrections SET correction_reason='Synthetic tamper'", "DELETE FROM operations_corrections",
                        "INSERT OR REPLACE INTO operations_corrections SELECT * FROM operations_corrections"):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.connect() as connection:
                        connection.execute("PRAGMA recursive_triggers=" + str(recursive))
                        connection.execute(sql)
        self.assertEqual(database_rows(self.db), before)

    def test_correction_projection_failure_rolls_back_audit_and_revision(self):
        task_id = self.seed()
        self.db.execute("CREATE TRIGGER synthetic_projection_failure BEFORE INSERT ON operations_description_projection BEGIN SELECT RAISE(ABORT,'synthetic injected failure'); END")
        before = database_rows(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            self.clarity.apply_correction(task_id, self.correction())
        self.assertEqual(database_rows(self.db), before)

    def test_correction_concurrency_one_winner_and_exact_retry_single_receipt(self):
        task_id = self.seed()
        first, second = self.correction(), self.correction(key="synthetic-race-2")
        barrier = threading.Barrier(2)
        def apply(body):
            barrier.wait(timeout=5)
            try:
                return self.clarity.apply_correction(task_id, body)
            except OperationsError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, [first, second]))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(results.count("stale"), 1)
        winner = first if isinstance(results[0], dict) else second
        with ThreadPoolExecutor(max_workers=3) as pool:
            retries = list(pool.map(lambda _: self.clarity.apply_correction(task_id, winner), range(3)))
        self.assertTrue(all(r["reused"] for r in retries))
        self.assertEqual(len(self.clarity.get_task(task_id)["corrections"]), 1)


class CompletionSafetyTests(SyntheticCase):
    def test_protected_states_keep_live_observation_and_operator_conflict(self):
        for state in ("PAUSED", "CANCELED", "DONE", "PLAN_ONLY", "WAITING"):
            task_id = self.seed("synthetic-protected-live-" + state, state=state,
                                blocking_reason="PAUSED_BY_USER: synthetic pause" if state == "WAITING" else "Synthetic protected state")
            native = self.raw(task_id)
            for status in ("QUEUED", "RUNNING"):
                run = {"id": "synthetic-observed-run", "status": status, "pid": 12345}
                before = database_rows(self.db)
                with patch.object(self.service, "_process_alive", return_value=True):
                    resolution = self.service.derive_task_state(native, run)
                self.assertEqual((resolution["state"], resolution["stored_state"]), (state, state))
                self.assertFalse(resolution["syncing"])
                self.assertTrue(resolution["live_run"])
                self.assertTrue(resolution["process_alive"])
                projected = {**native, "state_resolution": resolution, "stored_state": state}
                status_view = operator_status(projected, run, "manual")
                self.assertEqual(status_view["code"], "LIVE_STATE_CONFLICT")
                self.assertTrue(status_view["needs_attention"])
                self.assertEqual(database_rows(self.db), before)

    def test_protected_live_observation_does_not_trigger_metadata_reconciliation(self):
        task_id = self.seed(state="CANCELED", execution_mode="external", heartbeat_at=WHEN,
                            blocking_reason="STALE_EXECUTION: synthetic protected blocker")
        self.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,pid,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        ("synthetic-conflicting-live-run", task_id, 1, "synthetic", "synthetic-none", "RUNNING", 12345, WHEN))
        before = database_rows(self.db)
        with patch.object(self.service, "_process_alive", return_value=True):
            resolution = self.service.derive_task_state(self.raw())
            self.assertTrue(resolution["live_run"])
            counts = self.service.reconcile_derived_states()
        self.assertEqual(counts["synchronized"], 0)
        self.assertEqual(database_rows(self.db), before)

    def test_existing_explicit_waiting_pause_blocks_report_without_prose_approval(self):
        task_id = self.seed(action_owner_kind="user", blocking_reason="PAUSED_BY_USER: synthetic explicit pause")
        before = database_rows(self.db)
        self.error("stale", 409, lambda: self.report(task_id))
        self.assertEqual(database_rows(self.db), before)

    def test_existing_explicit_waiting_pause_blocks_done_and_run_projection(self):
        task_id = self.seed(blocking_reason="PAUSED_BY_USER: synthetic explicit pause")
        self.evidence()
        before = database_rows(self.db)
        self.error("stale", 409, lambda: self.service.transition(task_id, "DONE", force=True))
        projection = self.service.derive_task_state(self.raw(), {"id": "synthetic-run", "status": "DONE", "finished_at": RECEIVED})
        self.assertEqual(projection["state"], "WAITING")
        self.assertEqual(database_rows(self.db), before)

    def test_sensitive_report_inverse_preserves_every_row(self):
        task_id = self.seed(action_owner_kind="user", action_sensitive=1)
        self.evidence()
        before = database_rows(self.db)
        self.error("forbidden", 403, lambda: self.report(task_id))
        self.assertEqual(database_rows(self.db), before)

    def test_ordinary_report_requires_version_and_preserves_native_reason(self):
        task_id = self.seed(action_owner_kind="user", action_owner="synthetic-owner")
        before = database_rows(self.db)
        self.error("invalid", 400, lambda: self.service.report_human_action(task_id))
        self.assertEqual(database_rows(self.db), before)
        result = self.report(task_id)
        self.assertEqual((result["state"], result["action_owner_kind"], result["revision"]), ("VERIFYING", "none", 2))
        self.assertEqual(result["blocking_reason"], "Synthetic native reason")
        self.assertEqual(result["report"]["original_action"]["action_owner"], "synthetic-owner")
        self.assertEqual(result["report"]["assurance"], COMPLETION_ASSURANCE)
        self.assertEqual(result["completion_basis"]["missing"], ["artifact"])

    def test_report_exact_retry_before_cas_and_changed_payload_rejected(self):
        task_id = self.seed(action_owner_kind="user")
        revision = self.revision()
        first = self.report(revision=revision)
        self.db.execute("UPDATE tasks SET heartbeat_at=?", (RECEIVED,))
        before = database_rows(self.db)
        retry = self.report(revision=revision)
        self.assertEqual(retry["report"], first["report"])
        self.assertTrue(retry["reused"])
        self.error("idempotency", 409, lambda: self.report(revision=self.revision()))
        self.assertEqual(database_rows(self.db), before)

    def test_report_noop_aba_and_correction_share_one_revision(self):
        task_id = self.seed(action_owner_kind="user")
        original = self.revision()
        self.db.execute("UPDATE tasks SET action_text=action_text")
        self.error("stale", 409, lambda: self.report(revision=original))
        revision = self.revision()
        self.db.execute("UPDATE tasks SET action_text='Synthetic changed action'")
        self.db.execute("UPDATE tasks SET action_text='Synthetic native next action'")
        self.error("stale", 409, lambda: self.report(revision=revision))
        correction = self.clarity.apply_correction(task_id, self.correction())
        receipt = self.report()
        self.assertEqual(receipt["revision"], correction["task"]["revision"] + 1)
        self.assertIn("stale_correction", self.codes(self.clarity.get_task(task_id)))

    def test_report_rejects_terminal_pause_external_or_active_run(self):
        for index, state in enumerate(("PAUSED", "PLAN_ONLY", "CANCELED", "DONE", "FAILED", "RUNNING", "QUEUED")):
            task_id = self.seed("synthetic-state-" + str(index), state=state, action_owner_kind="user")
            before = database_rows(self.db)
            self.error("stale", 409, lambda: self.report(task_id))
            self.assertEqual(database_rows(self.db), before)
        task_id = self.seed(action_owner_kind="external")
        self.error("stale", 409, lambda: self.report(task_id))
        self.db.execute("UPDATE tasks SET action_owner_kind='user' WHERE id=?", (task_id,))
        self.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        ("synthetic-active-run", task_id, 1, "synthetic", "synthetic-none", "RUNNING", WHEN))
        self.error("stale", 409, lambda: self.report(task_id))

    def test_report_false_or_exception_event_rolls_back_everything(self):
        task_id = self.seed(action_owner_kind="user")
        for failure in (False, RuntimeError("synthetic event failure")):
            before = database_rows(self.db)
            options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with patch.object(self.db, "add_event", **options):
                with self.assertRaises((OperationsError, RuntimeError)):
                    self.report(task_id)
            self.assertEqual(database_rows(self.db), before)

    def test_report_receipts_immutable_with_recursive_on_and_off(self):
        self.seed(action_owner_kind="user")
        self.report()
        before = database_rows(self.db)
        for setting in (0, 1):
            for sql in ("UPDATE operations_human_action_reports SET received_at='Synthetic changed'", "DELETE FROM operations_human_action_reports",
                        "INSERT OR REPLACE INTO operations_human_action_reports SELECT * FROM operations_human_action_reports"):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.connect() as connection:
                        connection.execute("PRAGMA recursive_triggers=" + str(setting))
                        connection.execute(sql)
        self.assertEqual(database_rows(self.db), before)

    def test_report_vs_correction_concurrency_has_one_shared_cas_winner(self):
        task_id = self.seed(action_owner_kind="user")
        body = self.correction()
        barrier = threading.Barrier(2)
        def attempt(kind):
            barrier.wait(timeout=5)
            try:
                return self.report(revision=1) if kind == "report" else self.clarity.apply_correction(task_id, body)
            except OperationsError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["report", "correction"]))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(results.count("stale"), 1)
        self.assertEqual(self.revision(), 2)

    def test_all_profiles_overlay_explicit_deploy_but_never_infer_from_old_facts(self):
        profiles = {"legacy": ["migration_metadata"], "browser": ["browser"], "qa": ["test"],
                    "code": ["commit", "test"], "artifact": ["artifact"], "auto": ["artifact"]}
        for profile, base in profiles.items():
            for deploy in (0, 1):
                task_id = self.seed("synthetic-profile-" + profile + str(deploy), evidence_profile=profile, requires_deploy=deploy,
                                    risk="Synthetic risk text does not imply deployment", action_owner_kind="agent")
                self.evidence(task_id, "deploy", "synthetic:historical-deploy", 0)
                self.evidence(task_id, "smoke", "synthetic:historical-smoke", 0)
                expected = base + (["deploy", "smoke"] if deploy else [])
                self.assertEqual(self.service.required_evidence(task_id), expected)
                self.assertEqual(self.clarity.get_task(task_id)["completion_basis"]["required"], expected)
                for kind in base:
                    self.evidence(task_id, kind, "synthetic:" + kind)
                self.assertEqual(self.service.completion_eligibility(task_id)["eligible"], not bool(deploy))

    def test_legitimate_local_auto_artifact_and_code_completion_remain_available(self):
        artifact_id = self.seed(evidence_profile="auto", repository=self.directory.name, state="VERIFYING")
        self.assertEqual(self.service.required_evidence(artifact_id), ["artifact"])
        self.assertEqual(self.service.reconcile_state_progression()["completed"], 0)
        self.evidence(artifact_id)
        self.assertEqual(self.service.reconcile_state_progression()["completed"], 1)
        self.assertEqual(self.raw(artifact_id)["state"], "DONE")
        self.assertIsNotNone(self.service.get_task(artifact_id)["completion_basis"]["last_completion_event"])
        code_id = self.seed("synthetic-local-code", evidence_profile="code", state="VERIFYING", action_owner_kind="agent",
                            risk="Synthetic historical risk", action_text="Synthetic agent progress")
        self.evidence(code_id, "commit", "synthetic:commit")
        self.evidence(code_id, "test", "synthetic:test")
        self.assertEqual(self.service.transition(code_id, "DONE")["state"], "DONE")
        self.assertEqual(self.raw(code_id)["action_text"], "Synthetic agent progress")

    def test_current_user_external_sensitive_and_dependency_block_actual_done_paths(self):
        for index, override in enumerate(({"action_owner_kind": "user"}, {"action_owner_kind": "external"},
                                          {"action_sensitive": 1}, {"action_owner_kind": "none"})):
            task_id = self.seed("synthetic-gate-" + str(index), state="VERIFYING", **override)
            self.evidence(task_id)
            if index == 3:
                dependency = self.seed("synthetic-required-dependency")
                self.service.add_dependency(task_id, dependency)
            before = database_rows(self.db)
            for force in (False, True):
                self.error("stale", 409, lambda: self.service.transition(task_id, "DONE", force=force))
            resolution = self.service.derive_task_state(self.raw(task_id), {"id": "synthetic-run", "status": "DONE", "finished_at": RECEIVED})
            self.assertNotEqual(resolution["state"], "DONE")
            self.assertEqual(self.service.reconcile_state_progression()["completed"], 0)
            self.assertEqual(database_rows(self.db), before)
            self.db.execute("UPDATE tasks SET state='WAITING',blocking_reason='EVIDENCE_COLLECTION_REQUIRED: synthetic' WHERE id=?", (task_id,))
            self.assertEqual(self.service.reconcile_state_progression()["evidence_completed"], 0)
            self.assertNotEqual(self.raw(task_id)["state"], "DONE")

    def test_derived_persistence_and_evidence_waiter_allow_only_current_gate(self):
        task_id = self.seed(state="RUNNING", action_owner_kind="external")
        self.evidence()
        self.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,finished_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        ("synthetic-terminal-run", task_id, 1, "synthetic", "synthetic-none", "DONE", RECEIVED, WHEN))
        self.service.reconcile_derived_states()
        self.assertNotEqual(self.raw()["state"], "DONE")
        self.assertEqual(self.raw()["action_owner_kind"], "external")
        self.db.execute("UPDATE tasks SET state='WAITING',action_owner_kind='none',action_text='',blocking_reason='EVIDENCE_COLLECTION_REQUIRED: synthetic'")
        self.assertEqual(self.service.reconcile_state_progression()["evidence_completed"], 1)
        self.assertEqual(self.raw()["state"], "DONE")

    def test_done_event_false_or_exception_rolls_back_state_revision_and_events(self):
        task_id = self.seed(state="VERIFYING")
        self.evidence()
        for failure in (False, RuntimeError("synthetic completion event failure")):
            before = database_rows(self.db)
            options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": False}
            with patch.object(self.db, "add_event", **options):
                with self.assertRaises((OperationsError, RuntimeError)):
                    self.service.transition(task_id, "DONE", force=True)
            self.assertEqual(database_rows(self.db), before)

    def test_completion_rechecks_after_preliminary_projection_changes(self):
        task_id = self.seed(state="VERIFYING")
        self.evidence()
        preliminary = self.service.completion_eligibility(task_id)
        self.assertTrue(preliminary["eligible"])
        self.db.execute("UPDATE tasks SET action_owner_kind='external',action_sensitive=1 WHERE id=?", (task_id,))
        before = database_rows(self.db)
        self.error("stale", 409, lambda: self.service.transition(task_id, "DONE", force=True))
        self.assertEqual(database_rows(self.db), before)


class HandlerTests(SyntheticCase):
    def request(self, method, path, data=None, headers=None, raw=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else b"")
        fields = [("Host", "127.0.0.1:39091"), ("Connection", "close"), ("Content-Type", "application/json"),
                  ("Content-Length", str(len(body)))]
        for key, value in (headers or {}).items():
            fields = [(k, v) for k, v in fields if k.lower() != key.lower()]
            if value is not None:
                fields.append((key, value))
        wire = (method + " " + path + " HTTP/1.1\r\n" + "\r\n".join(k + ": " + v for k, v in fields) + "\r\n\r\n").encode() + body
        reads = []
        class Input(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)
        class ByteConnection:
            def settimeout(self, timeout):
                self.timeout = timeout
            def __init__(self):
                self.output = io.BytesIO()
            def makefile(self, mode, buffering=-1):
                return Input(wire)
            def sendall(self, value):
                self.output.write(value)
        connection = ByteConnection()
        handler = type("SyntheticOperationsHandler", (ControlPlaneHandler,), {"service": self.service, "coordinator": None})
        handler(connection, ("127.0.0.1", 39092), SimpleNamespace(server_address=("127.0.0.1", 39091)))
        response_headers, response_body = connection.output.getvalue().split(b"\r\n\r\n", 1)
        status = int(response_headers.split(b" ", 2)[1])
        result = json.loads(response_body)
        print("SYNTHETIC_HANDLER " + json.dumps({"method": method, "path": path, "status": status, "code": result.get("code"),
                                                "body_reads": reads, "socket": False}, sort_keys=True))
        return status, result, reads

    def test_real_handler_list_detail_preview_create_retry_stale_and_missing(self):
        task_id = self.seed()
        path = "/api/operations-clarity/tasks/" + task_id
        self.assertEqual(self.request("GET", "/api/operations-clarity")[0], 200)
        self.assertEqual(self.request("GET", path)[1]["id"], task_id)
        body = self.correction()
        before = database_rows(self.db)
        self.assertTrue(self.request("POST", path + "/corrections/preview", body)[1]["preview"])
        self.assertEqual(database_rows(self.db), before)
        self.assertEqual(self.request("POST", path + "/corrections", body)[0], 201)
        self.assertTrue(self.request("POST", path + "/corrections", body)[1]["reused"])
        status, error, _ = self.request("POST", path + "/corrections", {**body, "idempotency_key": "synthetic-stale"})
        self.assertEqual((status, error["code"]), (409, "stale"))
        self.assertEqual(self.request("GET", "/api/operations-clarity/tasks/synthetic-missing")[0], 404)
        self.assertEqual(self.request("POST", "/api/operations-clarity/import", {})[0], 404)

    def test_real_handler_host_origin_and_pre_read_frame_checks(self):
        task_id = self.seed()
        path = "/api/operations-clarity/tasks/" + task_id + "/corrections"
        before = database_rows(self.db)
        for headers, expected in [({"Host": "synthetic.evil:39091"}, 403), ({"Host": "127.0.0.1:1"}, 403),
                                  ({"Origin": "https://synthetic.evil"}, 403), ({"Origin": "null"}, 403),
                                  ({"Content-Length": "65537"}, 400), ({"Content-Length": "-1"}, 400),
                                  ({"Content-Length": None}, 400), ({"Transfer-Encoding": "chunked"}, 400)]:
            status, result, reads = self.request("POST", path, self.correction(), headers=headers)
            self.assertEqual(status, expected)
            self.assertEqual(reads, [])
            self.assertEqual(result["assurance"], ASSURANCE)
        self.assertEqual(database_rows(self.db), before)

    def test_real_handler_bad_bodies_unknown_fields_and_navigation_fail_closed(self):
        task_id = self.seed()
        path = "/api/operations-clarity/tasks/" + task_id + "/corrections"
        before = database_rows(self.db)
        for raw in (b"{", b"[]", b"null", b"\xff", b'{"expected_revision":1,"expected_revision":2}', b'{"value":NaN}', b" " * 65537):
            self.assertEqual(self.request("POST", path, raw=raw)[0], 400)
        self.assertEqual(self.request("POST", path, {**self.correction(), "state": "DONE"})[0], 400)
        self.assertEqual(self.request("GET", "/api/operations-clarity/tasks/synthetic%2Fescape")[0], 400)
        self.assertEqual(database_rows(self.db), before)

    def test_same_origin_legacy_complete_missing_version_is_400(self):
        task_id = self.seed(action_owner_kind="user")
        before = database_rows(self.db)
        status, result, _ = self.request("POST", "/api/tasks/" + task_id + "/complete-human-action", {},
                                         headers={"Origin": "http://127.0.0.1:39091"})
        self.assertEqual((status, result["code"]), (400, "invalid"))
        self.assertIn("刷新", result["error"])
        self.assertEqual(database_rows(self.db), before)

    def test_complete_real_handler_sensitive_stale_and_ordinary_receipt(self):
        task_id = self.seed(action_owner_kind="user", action_sensitive=1)
        path = "/api/tasks/" + task_id + "/complete-human-action"
        body = {"expected_revision": self.revision(), "idempotency_key": "synthetic-http-report"}
        before = database_rows(self.db)
        self.assertEqual(self.request("POST", path, body)[0], 403)
        self.assertEqual(database_rows(self.db), before)
        self.db.execute("UPDATE tasks SET action_sensitive=0")
        self.assertEqual(self.request("POST", path, body)[0], 409)
        body["expected_revision"] = self.revision()
        status, result, _ = self.request("POST", path, body)
        self.assertEqual((status, result["state"]), (200, "VERIFYING"))
        self.assertTrue(self.request("POST", path, body)[1]["reused"])
        self.assertEqual(self.request("POST", path, {**body, "expected_revision": result["revision"]})[1]["code"], "idempotency")


class MigrationTests(SyntheticCase):
    def old_database(self):
        old = Database(Path(self.directory.name) / "synthetic-old.sqlite3")
        with old.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute("INSERT INTO meta VALUES('schema_version','8')")
            connection.execute("INSERT INTO meta VALUES('synthetic-custom','synthetic-preserve')")
            initialize_release_schema(connection)
            connection.execute("INSERT INTO tasks(id,idempotency_key,title,state,created_at,updated_at) VALUES('synthetic-old','synthetic-old','Synthetic old task','DONE',?,?)", (WHEN, WHEN))
            connection.execute("INSERT INTO evidence(task_id,kind,value,verified,created_at) VALUES('synthetic-old','test','synthetic:old-test',1,?)", (WHEN,))
            connection.execute("INSERT INTO events(event_id,task_id,event_type,producer,summary,dedupe_key,occurred_at) VALUES('synthetic-event','synthetic-old','synthetic','synthetic','Synthetic preserved event','synthetic-event',?)", (WHEN,))
            connection.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) VALUES('synthetic-run','synthetic-old',1,'synthetic','synthetic-none','DONE',?)", (WHEN,))
            connection.execute("INSERT INTO sessions(id,name,worker_type,last_seen_at) VALUES('synthetic-session-row','Synthetic row only','synthetic',?)", (WHEN,))
            connection.execute("INSERT INTO authorization_audit(task_id,action,policy,decision,reason,occurred_at) VALUES('synthetic-old','synthetic','synthetic','denied','Synthetic preserved decision',?)", (WHEN,))
        return old

    def test_additive_migration_preserves_all_old_tables_meta_and_idempotency(self):
        old = self.old_database()
        before = database_rows(old)
        with old.connect() as connection:
            initialize_operations_schema(connection)
            initialize_operations_schema(connection)
        self.assertEqual(database_rows(old, list(before)), before)
        version = old.one("SELECT * FROM operations_task_versions WHERE task_id='synthetic-old'")
        self.assertEqual((version["revision"], version["basis_revision"]), (1, 1))
        print("SYNTHETIC_MIGRATION " + json.dumps({"legacy_tables": len(before), "legacy_rows": sum(len(v) for v in before.values()),
                                                  "legacy_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                                                  "meta_preserved": True, "live_database": False}, sort_keys=True))

    def test_migration_failure_rolls_back_all_additive_objects(self):
        old = self.old_database()
        before = database_rows(old)
        with old.connect() as connection:
            schema_before = list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
            class FailMigration:
                def execute(self, sql, *args):
                    if "CREATE TRIGGER IF NOT EXISTS operations_native_update" in sql:
                        raise sqlite3.OperationalError("synthetic migration failure")
                    return connection.execute(sql, *args)
            with self.assertRaises(sqlite3.OperationalError):
                initialize_operations_schema(FailMigration())
            self.assertEqual(list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name")), schema_before)
        self.assertEqual(database_rows(old), before)


if __name__ == "__main__":
    print("SYNTHETIC_ENVIRONMENT " + json.dumps({"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                                                "socket.socket": "hard blocked", "subprocess.Popen": "hard blocked",
                                                "os.kill": "hard blocked", "data": "new synthetic temporary SQLite only",
                                                "listening_services": False, "live_acceptance": False}, sort_keys=True))
    unittest.main(verbosity=2)
