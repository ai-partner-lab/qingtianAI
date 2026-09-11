"""Synthetic, targeted release registry tests. Never starts the real scheduler."""
from __future__ import annotations

import copy
import hashlib
import http.client
import importlib.util
import io
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from qingtian_engine.db import Database
from qingtian_engine.releases import ASSURANCE, HEALTH, ReleaseError, ReleaseService, initialize_release_schema
from qingtian_engine.server import ControlPlaneHandler
from qingtian_engine.service import ControlPlane

ROOT = Path(__file__).resolve().parents[2]
WHEN = "2026-01-01T01:00:00+00:00"
CHECKED = "2026-01-01T01:01:00+00:00"


@contextmanager
def release_clock(initial):
    class Clock(datetime):
        current = datetime.fromisoformat(initial)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz or timezone.utc)

    with patch("qingtian_engine.releases.datetime", Clock):
        yield Clock


def fact(status, proof=None):
    return {
        "status": status, "observed_at": WHEN,
        "source": {"kind": "release_receipt", "ref": "synthetic://receipt/1", "sha256": "a" * 64},
        "review": {"reviewer": "synthetic-owner", "checked_at": CHECKED, "method": "owner_review"},
        "proof": proof or {},
    }


def payload(key="create-1", environment="dev", task_ids=None):
    artifact = {"source_revision": "synthetic-source-1", "digest": "sha256:" + "b" * 64,
                "ops_revision": "synthetic-ops-1"}
    proof = {
        "profile": "cluster_release_v1", "environment": environment, "component": "component-a",
        "source_revision": artifact["source_revision"], "artifact_digest": artifact["digest"],
        "ops_revision": artifact["ops_revision"], "release_ref": "synthetic://release/1",
        "release_result": "success", "terminal_ref": "synthetic://terminal/1",
        "terminal_state": "Success", "terminal_proven": True, "health": dict(HEALTH),
        "siblings_applicable": False, "siblings": [],
    }
    return {"idempotency_key": key, "name": "Synthetic batch", "version": "v-test-1",
            "environment": environment, "owner": "synthetic-owner", "items": [{
                "item_key": "item-a", "feature_key": "feature-a", "title": "Synthetic feature",
                "component": "component-a", "artifact": artifact, "task_ids": task_ids or [],
                "facts": {"deployment": fact("deployed", proof)},
            }]}


def snapshot(db, names=None):
    with db.connect() as connection:
        if names is None:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                for name in names}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix=".backend-release-test-")
        self.addCleanup(self.directory.cleanup)
        self.deny_process = patch.object(subprocess, "Popen", side_effect=AssertionError("process dispatch forbidden"))
        self.deny_process.start()
        self.addCleanup(self.deny_process.stop)
        self.db = Database(Path(self.directory.name) / "synthetic.sqlite3")
        self.service = ControlPlane(self.db)
        self.releases = self.service.releases

    def seed_task(self, task_id="synthetic-task-1", state="DONE"):
        self.db.execute("INSERT INTO tasks(id,idempotency_key,title,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (task_id, task_id, "Synthetic task", state, WHEN, WHEN))
        self.db.add_event(task_id, "synthetic", "test", "Synthetic only", task_id + "-event")
        return task_id

    def assert_error(self, code, status, call):
        with self.assertRaises(ReleaseError) as caught:
            call()
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.assertEqual(caught.exception.payload()["assurance"], ASSURANCE)

    def test_done_tasks_do_not_create_releases_or_events(self):
        self.seed_task()
        before = snapshot(self.db)
        self.assertEqual(self.releases.list_batches(), {
            "schema_version": 1, "revision": 0, "total": 0, "batches": [], "assurance": ASSURANCE})
        self.assertEqual(snapshot(self.db), before)

    def test_preview_is_read_only_and_returns_direct_projection(self):
        before = snapshot(self.db)
        result = self.releases.preview(payload())
        self.assertTrue(result["preview"])
        self.assertIsNone(result["id"])
        self.assertIsNone(result["recorded_at"])
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["history"], [])
        self.assertEqual(result["status"], "released")
        self.assertEqual(snapshot(self.db), before)

    def test_complete_registration_shape_and_observation_time(self):
        result = self.releases.create(payload())
        self.assertFalse(result["reused"])
        self.assertEqual(result["status"], "released")
        self.assertEqual(result["state"], "released")
        self.assertEqual(result["released_at"], WHEN)
        self.assertNotEqual(result["recorded_at"], WHEN)
        self.assertEqual(result["counts"], {"total": 1, "deployed": 1, "enabled": 0,
                                            "accepted": 0, "rolled_back": 0, "unverified": 0})
        history = result["history"][0]
        self.assertEqual(set(history), {"id", "revision", "idempotency_key", "received_at", "items", "assurance"})
        self.assertEqual(history["items"][0]["facts"], payload()["items"][0]["facts"])
        detail = self.releases.get_batch(result["id"])
        self.assertEqual(detail, {k: v for k, v in result.items() if k != "reused"})
        self.assertEqual(self.releases.list_batches()["batches"], [detail])

    def test_missing_proof_verified_flags_and_ci_are_not_deployment(self):
        for candidate in ({"status": "deployed", "verified": True},
                          {"status": "deployed", "verified": "true"},
                          fact("deployed")):
            with self.subTest(candidate=candidate):
                data = payload()
                data["items"][0]["facts"]["deployment"] = candidate
                result = self.releases.preview(data)
                self.assertEqual(result["counts"]["deployed"], 0)
                self.assertIsNone(result["released_at"])
                self.assertEqual(result["items"][0]["assessment"]["deployment"]["state"], "reported")
        data = payload()
        dep = data["items"][0]["facts"]["deployment"]
        dep["source"]["kind"] = "ci_build"
        dep["review"]["verified"] = True
        dep["proof"] = {"verified": True, "build": "success", "environment": "dev", "component": "component-a"}
        self.assertEqual(self.releases.preview(data)["counts"]["deployed"], 0)

    def test_dev_is_not_prod_and_environment_filters_are_exact(self):
        dev = self.releases.create(payload())
        copied = payload("copied")
        copied["environment"] = "prod"
        prod = self.releases.create(copied)
        self.assertEqual(prod["counts"]["deployed"], 0)
        self.assertEqual(prod["status"], "unverified")
        self.assertEqual([x["id"] for x in self.releases.list_batches("dev")["batches"]], [dev["id"]])
        self.assertEqual(self.releases.list_batches("prod")["batches"][0]["id"], prod["id"])
        self.assertEqual(self.releases.list_batches("test")["total"], 0)

    def test_partial_batch_counts_only_current_complete_deployments(self):
        data = payload()
        other = copy.deepcopy(data["items"][0])
        other["item_key"] = "item-b"
        other["facts"] = {}
        data["items"].append(other)
        result = self.releases.create(data)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["deployed"], 1)
        self.assertEqual(result["counts"]["unverified"], 1)

    def test_create_canonical_idempotency_conflict_and_no_writes(self):
        task_id = self.seed_task()
        data = payload(task_ids=[task_id, task_id])
        first = self.releases.create(data)
        before = snapshot(self.db)
        data["items"][0]["task_ids"] = [task_id]
        retry = self.releases.create(dict(reversed(list(data.items()))))
        self.assertTrue(retry["reused"])
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(snapshot(self.db), before)
        data["version"] = "different"
        self.assert_error("idempotency", 409, lambda: self.releases.create(data))
        self.assertEqual(snapshot(self.db), before)

    def test_many_items_many_batches_mapping_never_changes_tasks_events(self):
        one, two = self.seed_task(), self.seed_task("synthetic-task-2", "WAITING")
        before = snapshot(self.db, ["tasks", "events", "runs", "evidence", "feedback_cursors"])
        data = payload(task_ids=[one, two])
        second = copy.deepcopy(data["items"][0])
        second["item_key"] = "item-b"
        data["items"].append(second)
        first = self.releases.create(data)
        self.releases.create(payload("prod-map", "prod", [one]))
        self.assertEqual(self.releases.list_batches(task_id=one)["total"], 2)
        self.assertEqual(self.releases.list_batches(task_id=two)["total"], 1)
        self.assertEqual(self.releases.list_batches(task_id="absent")["total"], 0)
        self.assertEqual(self.releases.list_batches(task_id=one, environment="prod")["total"], 1)
        self.releases.append_receipts(first["id"], {"idempotency_key": "unknown", "expected_revision": 1,
            "items": [{"item_key": "item-a", "facts": {"deployment": {"status": "unknown"}}}]})
        self.assertEqual(snapshot(self.db, list(before)), before)

    def test_rollback_preserves_other_items_and_append_history(self):
        data = payload()
        other = copy.deepcopy(data["items"][0])
        other["item_key"] = "item-b"
        data["items"].append(other)
        first = self.releases.create(data)
        update = {"idempotency_key": "rollback", "expected_revision": 1, "items": [{"item_key": "item-a", "facts": {
            "deployment": fact("rolled_back", {"environment": "dev", "component": "component-a",
                                                  "reason": "synthetic rollback", "rollback_ref": "synthetic://rollback/1"})}}]}
        result = self.releases.append_receipts(first["id"], update)
        self.assertEqual(result["counts"]["deployed"], 1)
        self.assertEqual(result["counts"]["rolled_back"], 1)
        self.assertEqual(result["items"][1], first["items"][1])
        self.assertEqual(result["history"][0], first["history"][0])
        self.assertEqual(result["history"][1]["items"], update["items"])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(self.releases.list_batches()["revision"], 2)

    def test_incomplete_rollback_removes_old_green_without_field_inheritance(self):
        first = self.releases.create(payload())
        result = self.releases.append_receipts(first["id"], {"idempotency_key": "weak-rollback", "expected_revision": 1,
            "items": [{"item_key": "item-a", "facts": {"deployment": {"status": "rolled_back", "verified": True}}}]})
        self.assertEqual(result["counts"]["deployed"], 0)
        self.assertEqual(result["counts"]["rolled_back"], 0)
        self.assertEqual(result["released_at"], WHEN)
        current = result["items"][0]
        self.assertEqual(current["assessment"]["deployment"]["state"], "reported")
        self.assertNotIn("source", current["facts"]["deployment"])
        self.assertEqual(result["history"][0]["items"][0]["facts"]["deployment"]["status"], "deployed")

    def test_historical_release_time_survives_rollback_and_uses_latest_actual_deployment(self):
        first = self.releases.create(payload())
        deployment = copy.deepcopy(payload()["items"][0]["facts"]["deployment"])
        deployment["observed_at"] = "2026-02-01T01:00:00Z"
        deployment["review"]["checked_at"] = "2026-02-01T01:01:00Z"
        second = self.releases.append_receipts(first["id"], {"idempotency_key": "redeploy", "expected_revision": 1,
            "items": [{"item_key": "item-a", "facts": {"deployment": deployment}}]})
        rollback = fact("rolled_back", {"environment": "dev", "component": "component-a",
                                        "reason": "synthetic rollback", "rollback_ref": "synthetic://rollback/2"})
        rollback["observed_at"] = "2026-03-01T01:00:00Z"
        rollback["review"]["checked_at"] = "2026-03-01T01:01:00Z"
        third = self.releases.append_receipts(first["id"], {"idempotency_key": "later-rollback", "expected_revision": 2,
            "items": [{"item_key": "item-a", "facts": {"deployment": rollback}}]})
        self.assertEqual(third["released_at"], "2026-02-01T01:00:00+00:00")
        self.assertEqual(third["released_at"], second["released_at"])
        self.assertEqual(third["status"], "rolled_back")
        self.assertEqual(third["counts"]["deployed"], 0)
        self.assertEqual(third["counts"]["rolled_back"], 1)
        self.assertEqual(len(third["history"]), 3)

    def test_append_retry_precedes_cas_and_conflicts_do_not_write(self):
        first = self.releases.create(payload())
        update = {"idempotency_key": "receipt-2", "expected_revision": 1, "items": [
            {"item_key": "item-a", "facts": {"enablement": {"status": "unknown"}}}]}
        second = self.releases.append_receipts(first["id"], update)
        third = copy.deepcopy(update)
        third.update(idempotency_key="receipt-3", expected_revision=2)
        self.releases.append_receipts(first["id"], third)
        before = snapshot(self.db)
        retry = self.releases.append_receipts(first["id"], update)
        self.assertTrue(retry["reused"])
        self.assertEqual(retry["revision"], 3)
        self.assertEqual(len(retry["history"]), 3)
        stale = copy.deepcopy(update)
        stale["idempotency_key"] = "stale"
        self.assert_error("stale", 409, lambda: self.releases.append_receipts(first["id"], stale))
        conflict = copy.deepcopy(update)
        conflict["expected_revision"] = second["revision"]
        self.assert_error("idempotency", 409, lambda: self.releases.append_receipts(first["id"], conflict))
        self.assertEqual(snapshot(self.db), before)

    def test_enablement_and_acceptance_are_independent_and_require_own_proof(self):
        data = payload()
        facts = data["items"][0]["facts"]
        common = {"environment": "dev", "component": "component-a", "feature_key": "feature-a"}
        facts["enablement"] = fact("enabled", {**common, "enabled": True})
        facts["acceptance"] = fact("passed", {**common, "result": "passed", "round_id": "synthetic-round-1",
                                               "method": "fresh_character_e2e"})
        first = self.releases.create(data)
        self.assertEqual(first["counts"]["enabled"], 1)
        self.assertEqual(first["counts"]["accepted"], 0)
        acceptance = copy.deepcopy(facts["acceptance"])
        acceptance["proof"]["new_character_id"] = "synthetic-new-character-1"
        result = self.releases.append_receipts(first["id"], {"idempotency_key": "accept", "expected_revision": 1,
            "items": [{"item_key": "item-a", "facts": {"acceptance": acceptance}}]})
        self.assertEqual(result["counts"]["accepted"], 1)
        self.assertEqual(result["counts"]["enabled"], 1)
        facts["enablement"]["proof"]["enabled"] = "true"
        self.assertEqual(self.releases.preview(data)["counts"]["enabled"], 0)

    def test_complete_failed_disabled_and_rollback_states(self):
        data = payload()
        common = {"environment": "dev", "component": "component-a", "feature_key": "feature-a"}
        data["items"][0]["facts"] = {
            "deployment": fact("failed", {**common, "reason": "synthetic failure", "release_ref": "synthetic://failed",
                                            "release_result": "failed"}),
            "enablement": fact("disabled", {**common, "enabled": False}),
            "acceptance": fact("failed", {**common, "result": "failed", "round_id": "round-1", "method": "synthetic_test"}),
        }
        result = self.releases.preview(data)
        states = result["items"][0]["assessment"]
        self.assertEqual([states[key]["state"] for key in ("deployment", "enablement", "acceptance")],
                         ["failed", "disabled", "failed"])
        data["items"][0]["facts"]["deployment"] = fact("rolled_back", {**common, "reason": "synthetic", "rollback_ref": "synthetic://r"})
        self.assertEqual(self.releases.preview(data)["status"], "rolled_back")

    def test_proof_field_mutations_each_prevent_green(self):
        paths = [("source", "sha256"), ("source", "ref"), ("source", "kind"),
                 ("review", "reviewer"), ("review", "method"), ("review", "checked_at"),
                 ("proof", "profile"), ("proof", "environment"), ("proof", "component"),
                 ("proof", "source_revision"), ("proof", "artifact_digest"), ("proof", "ops_revision"),
                 ("proof", "release_ref"), ("proof", "release_result"), ("proof", "terminal_ref"),
                 ("proof", "terminal_state"), ("proof", "terminal_proven"), ("proof", "health"),
                 ("proof", "siblings_applicable")]
        for container, key in paths:
            with self.subTest(container=container, key=key):
                data = payload()
                del data["items"][0]["facts"]["deployment"][container][key]
                result = self.releases.preview(data)
                self.assertEqual(result["counts"]["deployed"], 0)
                self.assertTrue(result["items"][0]["assessment"]["deployment"]["reasons"])
        for key in HEALTH:
            data = payload()
            data["items"][0]["facts"]["deployment"]["proof"]["health"][key] = "green"
            self.assertEqual(self.releases.preview(data)["counts"]["deployed"], 0)

    def test_future_receipt_stays_reported_after_clock_advance_without_writes(self):
        # Cover both a future observation and a future review of an already
        # observed deployment. Passing time is not a new receipt or review.
        for index, registered_at in enumerate(("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:30+00:00")):
            with self.subTest(registered_at=registered_at), release_clock(registered_at) as clock:
                data = payload("future-clock-" + str(index))
                first = self.releases.create(data)
                self.assertEqual(first["counts"]["deployed"], 0)
                self.assertIsNone(first["released_at"])
                original_reasons = first["items"][0]["assessment"]["deployment"]["reasons"]
                before = snapshot(self.db)
                clock.current = datetime(2026, 1, 3, tzinfo=timezone.utc)
                detail = self.releases.get_batch(first["id"])
                listing = self.releases.list_batches()
                retry = self.releases.create(data)
                for result in (detail, retry, next(batch for batch in listing["batches"] if batch["id"] == first["id"])):
                    self.assertEqual(result["counts"]["deployed"], 0)
                    self.assertIsNone(result["released_at"])
                    self.assertEqual(result["items"][0]["assessment"]["deployment"]["state"], "reported")
                    self.assertEqual(result["items"][0]["assessment"]["deployment"]["reasons"], original_reasons)
                    self.assertEqual(result["revision"], first["revision"])
                    self.assertEqual(result["history"], first["history"])
                self.assertTrue(retry["reused"])
                self.assertEqual(listing["revision"], before["release_schema_version"][0][2])
                self.assertEqual(snapshot(self.db), before)
                # A new preview may assess evidence against the time of that
                # preview, but it never changes the old registered projection.
                self.assertEqual(self.releases.preview(data)["counts"]["deployed"], 1)
                self.assertEqual(snapshot(self.db), before)

    def test_receipt_time_is_per_dimension_and_explicit_correction_can_confirm(self):
        with release_clock("2026-01-01T00:00:00+00:00") as clock:
            data = payload("dimension-clock")
            first = self.releases.create(data)
            clock.current = datetime(2026, 1, 3, tzinfo=timezone.utc)
            enablement = fact("enabled", {"environment": "dev", "component": "component-a",
                                          "feature_key": "feature-a", "enabled": True})
            second = self.releases.append_receipts(first["id"], {
                "idempotency_key": "enablement-only-clock", "expected_revision": 1,
                "items": [{"item_key": "item-a", "facts": {"enablement": enablement}}],
            })
            self.assertEqual(second["counts"]["enabled"], 1)
            self.assertEqual(second["counts"]["deployed"], 0)
            self.assertIsNone(second["released_at"])
            self.assertEqual(second["items"][0]["assessment"]["deployment"]["state"], "reported")
            self.assertEqual(second["history"][0], first["history"][0])
            corrected = self.releases.append_receipts(first["id"], {
                "idempotency_key": "explicit-deployment-correction", "expected_revision": 2,
                "items": [{"item_key": "item-a", "facts": data["items"][0]["facts"]}],
            })
            self.assertEqual(corrected["counts"]["deployed"], 1)
            self.assertEqual(corrected["counts"]["enabled"], 1)
            self.assertEqual(corrected["released_at"], WHEN)
            self.assertEqual(corrected["revision"], 3)
            self.assertEqual(self.releases.list_batches()["revision"], 3)
            self.assertEqual(corrected["history"][:2], second["history"])

    def test_future_history_cannot_acquire_release_time_after_replacement(self):
        with release_clock("2026-01-01T00:00:00+00:00") as clock:
            first = self.releases.create(payload("history-clock"))
            clock.current = datetime(2026, 1, 3, tzinfo=timezone.utc)
            replaced = self.releases.append_receipts(first["id"], {
                "idempotency_key": "unknown-clock", "expected_revision": 1,
                "items": [{"item_key": "item-a", "facts": {"deployment": {"status": "unknown"}}}],
            })
            self.assertEqual(replaced["counts"]["deployed"], 0)
            self.assertIsNone(replaced["released_at"])
            self.assertEqual(replaced["history"][0], first["history"][0])
            before = snapshot(self.db)
            clock.current = datetime(2027, 1, 1, tzinfo=timezone.utc)
            self.assertIsNone(self.releases.get_batch(first["id"])["released_at"])
            self.assertEqual(snapshot(self.db), before)

    def test_missing_or_damaged_receipt_time_cannot_fall_back_to_now(self):
        from qingtian_engine.releases import _project

        first = self.releases.create(payload("damaged-projection"))
        before = snapshot(self.db)
        with release_clock("2027-01-01T00:00:00+00:00"):
            for bad_time in ("absent-key", None, "", "not-a-time", "2026-01-01"):
                with self.subTest(received_at=bad_time):
                    history = copy.deepcopy(first["history"])
                    if bad_time == "absent-key":
                        del history[0]["received_at"]
                    else:
                        history[0]["received_at"] = bad_time
                    result = _project(first, first["items"], history)
                    self.assertEqual(result["counts"]["deployed"], 0)
                    self.assertIsNone(result["released_at"])
                    assessment = result["items"][0]["assessment"]["deployment"]
                    self.assertEqual(assessment["state"], "reported")
                    self.assertIn("assessment_time.missing_or_invalid", assessment["reasons"])
            # An old snapshot with facts but no receipt history is not preview.
            no_history = _project(first, first["items"], [])
            self.assertEqual(no_history["counts"]["deployed"], 0)
            self.assertIsNone(no_history["released_at"])
        self.assertEqual(snapshot(self.db), before)

    def test_same_payload_retry_cannot_repair_damaged_receipt_time(self):
        data = payload("damaged-idempotency")
        # Inject a malformed writer timestamp into synthetic storage without
        # disabling append-only triggers or rewriting an existing receipt.
        with patch("qingtian_engine.releases._now", return_value=""):
            first = self.releases.create(data)
        self.assertEqual(first["items"][0]["assessment"]["deployment"]["state"], "reported")
        before = snapshot(self.db)
        with release_clock("2027-01-01T00:00:00+00:00"):
            retry = self.releases.create(data)
            self.assertTrue(retry["reused"])
            self.assertEqual(retry["counts"]["deployed"], 0)
            self.assertIsNone(retry["released_at"])
            self.assertEqual(retry["history"], first["history"])
            self.assertEqual(retry["revision"], 1)
            self.assertEqual(self.releases.list_batches()["revision"], 1)
            self.assertEqual(snapshot(self.db), before)
            corrected = self.releases.append_receipts(first["id"], {
                "idempotency_key": "new-explicit-correction", "expected_revision": 1,
                "items": [{"item_key": "item-a", "facts": data["items"][0]["facts"]}],
            })
            self.assertEqual(corrected["counts"]["deployed"], 1)
            self.assertEqual(corrected["revision"], 2)
            self.assertEqual(corrected["history"][0], first["history"][0])

    def test_invalid_future_and_unreviewed_observation_dates(self):
        for observed, checked in (("yesterday", CHECKED), ("2026-01-01", CHECKED),
                                  ("2999-01-01T00:00:00Z", "2999-01-01T00:01:00Z"),
                                  (WHEN, "2025-01-01T00:00:00Z")):
            with self.subTest(observed=observed, checked=checked):
                data = payload()
                data["items"][0]["facts"]["deployment"].update(observed_at=observed)
                data["items"][0]["facts"]["deployment"]["review"]["checked_at"] = checked
                self.assertEqual(self.releases.preview(data)["counts"]["deployed"], 0)

    def test_sibling_applicability_requires_mapping_and_health(self):
        data = payload()
        proof = data["items"][0]["facts"]["deployment"]["proof"]
        proof["siblings_applicable"] = True
        self.assertEqual(self.releases.preview(data)["counts"]["deployed"], 0)
        sibling = {"environment": "dev", "component": "component-b", "source_revision": "sibling-src",
                   "artifact_digest": "sha256:" + "c" * 64, "ops_revision": "sibling-ops", "health": dict(HEALTH)}
        proof["siblings"] = [sibling]
        self.assertEqual(self.releases.preview(data)["counts"]["deployed"], 1)
        for key in sibling:
            copy_data = copy.deepcopy(data)
            del copy_data["items"][0]["facts"]["deployment"]["proof"]["siblings"][0][key]
            self.assertEqual(self.releases.preview(copy_data)["counts"]["deployed"], 0)
        sibling["component"] = "component-a"
        self.assertEqual(self.releases.preview(data)["counts"]["deployed"], 0)

    def test_missing_entities_and_invalid_shapes_are_atomic(self):
        before = snapshot(self.db)
        self.assert_error("missing", 404, lambda: self.releases.get_batch("absent"))
        self.assert_error("missing", 404, lambda: self.releases.create(payload(task_ids=["absent"])))
        self.assert_error("missing", 404, lambda: self.releases.preview(payload(task_ids=["absent"])))
        for mutation in ({"items": []}, {"environment": "Dev"}, {"idempotency_key": ""}, {"auto_start": True},
                         {"items": ["invalid"]}, {"items": [dict(payload()["items"][0], task_ids="bad")]}):
            with self.subTest(mutation=mutation):
                self.assert_error("invalid", 400, lambda: self.releases.create({**payload(), **mutation}))
        self.assertEqual(snapshot(self.db), before)

    def test_unknown_item_and_cas_type_cannot_change_metadata_or_mapping(self):
        first = self.releases.create(payload())
        base = {"idempotency_key": "delta", "expected_revision": 1, "items": [
            {"item_key": "missing", "facts": {"deployment": {"status": "unknown"}}}]}
        before = snapshot(self.db)
        self.assert_error("missing", 404, lambda: self.releases.append_receipts(first["id"], base))
        self.assert_error("missing", 404, lambda: self.releases.append_receipts("missing", base))
        for revision in (True, "1", 0, -1, 1.5):
            self.assert_error("invalid", 400, lambda: self.releases.append_receipts(first["id"], {**base, "expected_revision": revision}))
        self.assert_error("invalid", 400, lambda: self.releases.append_receipts(first["id"], {**base, "owner": "other"}))
        self.assertEqual(snapshot(self.db), before)

    def test_receipt_storage_rejects_update_and_delete(self):
        self.releases.create(payload())
        before = snapshot(self.db)
        for sql in ("UPDATE release_receipts SET received_at='bad'", "DELETE FROM release_receipts"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
        self.assertEqual(snapshot(self.db), before)

    def test_concurrent_create_is_single_registration(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.releases.create(payload()), range(4)))
        self.assertEqual(sum(not result["reused"] for result in results), 1)
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(self.releases.list_batches()["revision"], 1)

    def test_concurrent_cas_has_one_winner(self):
        first = self.releases.create(payload())
        barrier = threading.Barrier(2)

        def append(key):
            barrier.wait(timeout=5)
            try:
                return self.releases.append_receipts(first["id"], {"idempotency_key": key, "expected_revision": 1,
                    "items": [{"item_key": "item-a", "facts": {"deployment": {"status": "unknown"}}}]})
            except ReleaseError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, ("race-1", "race-2")))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(results.count("stale"), 1)
        self.assertEqual(self.releases.get_batch(first["id"])["revision"], 2)
        self.assertEqual(self.releases.list_batches()["revision"], 2)

    def test_connection_only_portability_without_task_schema(self):
        location = Path(self.directory.name) / "portable.sqlite3"

        @contextmanager
        def connect():
            connection = sqlite3.connect(location)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        with connect() as connection:
            initialize_release_schema(connection)
        standalone = ReleaseService(connect)
        result = standalone.create(payload())
        self.assertEqual(result["counts"]["deployed"], 1)
        self.assert_error("missing", 404, lambda: standalone.create(payload("needs-task", task_ids=["absent"])))

    def test_schema10_migration_preserves_every_legacy_table_row(self):
        path = Path(self.directory.name) / "synthetic-schema10.sqlite3"
        old = Database(path)
        with old.connect() as connection:
            connection.executescript((Path(__file__).parent / "fixtures/schema10.sql").read_text())
            connection.execute("INSERT INTO meta VALUES('schema_version','10')")
        # All legacy tables carry representative data, including evidence
        # marked verified and task completion. No original DB is ever opened.
        statements = [
            "INSERT INTO tasks(id,idempotency_key,title,state,created_at,updated_at) VALUES('t','t','Synthetic','DONE','x','x')",
            "INSERT INTO tasks(id,idempotency_key,title,created_at,updated_at) VALUES('u','u','Synthetic','x','x')",
            "INSERT INTO task_dependencies VALUES('t','u','blocks')",
            "INSERT INTO events(event_id,task_id,event_type,producer,summary,dedupe_key,occurred_at) VALUES('e','t','done','synthetic','x','e','x')",
            "INSERT INTO evidence(task_id,kind,value,verified,created_at) VALUES('t','deploy','synthetic-only',1,'x')",
            "INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) VALUES('r','t',1,'synthetic','none','SUCCEEDED','x')",
            "INSERT INTO sessions(id,name,worker_type,last_seen_at) VALUES('s','Synthetic','synthetic','x')",
            "INSERT INTO authorization_audit(task_id,action,policy,decision,reason,occurred_at) VALUES('t','synthetic','none','denied','synthetic','x')",
            "INSERT INTO imports VALUES('i','synthetic','x',1)",
            "INSERT INTO feedback_cursors VALUES('synthetic',1,'x')",
            "INSERT INTO intakes(id,idempotency_key,text,intent,created_at,updated_at) VALUES('i','i','Synthetic','analyze','x','x')",
            "INSERT INTO intake_attachments VALUES('a','i','Synthetic','text/plain',0,'synthetic','synthetic','x')",
            "INSERT INTO intake_messages VALUES('m','i','user','text','Synthetic','{}','x')",
            "INSERT INTO orchestrator_leases(lease_key,holder_id,acquired_at,heartbeat_at,expires_at) VALUES('l','synthetic','x','x','x')",
            "INSERT INTO reconciliation_checkpoints(name,updated_at) VALUES('c','x')",
            "INSERT INTO evidence_contracts(task_id,kind,updated_at) VALUES('t','synthetic','x')",
            "INSERT INTO dead_letters(id,task_id,category,reason,dedupe_key,created_at,updated_at) VALUES('d','t','synthetic','none','d','x','x')",
            "INSERT INTO executor_plugins(name,kind,updated_at) VALUES('synthetic','disabled','x')",
        ]
        for statement in statements:
            old.execute(statement)
        before = snapshot(old)
        self.assertEqual(old.one("SELECT value FROM meta WHERE key='schema_version'")["value"], "10")
        before.pop("meta")
        upgraded = Database(path)
        upgraded.initialize()
        upgraded.initialize()
        self.assertEqual(snapshot(upgraded, list(before)), before)
        registry = ReleaseService(upgraded.connect)
        initial_release = registry.list_batches()
        self.assertEqual(initial_release["total"], 0)
        registry.create(payload(task_ids=["t"]))
        # The migration above preserves ALL inherited tables, including the
        # release domain. Explicit registration then legitimately appends a
        # release and advances its counter, but must still preserve every
        # original task-domain row. Assert both boundaries rather than claiming
        # the newly inherited release counter should stay unchanged after POST.
        release_tables = {"release_schema_version", "release_batches", "release_items",
                          "release_item_tasks", "release_receipts"}
        task_tables = [name for name in before if name not in release_tables]
        self.assertEqual(snapshot(upgraded, task_tables), {name: before[name] for name in task_tables})
        after_release = registry.list_batches()
        self.assertEqual(after_release["total"], 1)
        self.assertEqual(after_release["revision"], initial_release["revision"] + 1)
        print("MIGRATION_EVIDENCE " + json.dumps({"legacy_tables": sorted(before), "schema_version": 10,
              "row_content_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
              "original_database_accessed": False}, sort_keys=True))


class ReleaseHTTPTests(ReleaseTests):
    # Reuse setup/helpers without rerunning inherited service test methods.
    def setUp(self):
        super().setUp()
        handler = type("SyntheticReleaseHandler", (ControlPlaneHandler,), {
            "service": self.service, "coordinator": None, "engine_mode": "manual",
        })
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_port
        self.assertNotIn(self.port, (8765, 8766))
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

        def stop():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=2)
            self.assertFalse(self.thread.is_alive())

        self.addCleanup(stop)

    def request(self, method, path, data=None, headers=None, raw=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        try:
            client.request(method, path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
            response = client.getresponse()
            result = json.loads(response.read())
            print("HTTP_EVIDENCE " + json.dumps({"method": method, "path": path, "status": response.status,
                  "code": result.get("code"), "revision": result.get("revision"), "reused": result.get("reused")}, sort_keys=True))
            return response.status, result
        finally:
            client.close()

    def test_http_create_preview_list_detail_append_and_retry(self):
        status, preview = self.request("POST", "/api/release-batches/preview", payload())
        self.assertEqual(status, 200)
        self.assertTrue(preview["preview"])
        self.assertEqual(self.request("GET", "/api/release-batches")[1]["revision"], 0)
        status, batch = self.request("POST", "/api/release-batches", payload())
        self.assertEqual(status, 201)
        self.assertEqual(self.request("POST", "/api/release-batches", payload())[0], 200)
        self.assertEqual(self.request("GET", "/api/release-batches?environment=dev")[1]["total"], 1)
        self.assertEqual(self.request("GET", "/api/release-batches/" + batch["id"])[1]["counts"]["deployed"], 1)
        update = {"idempotency_key": "http-append", "expected_revision": 1,
                  "items": [{"item_key": "item-a", "facts": {"deployment": {"status": "rolled_back"}}}]}
        status, result = self.request("POST", "/api/release-batches/" + batch["id"] + "/receipts", update)
        self.assertEqual(status, 200)
        self.assertEqual(result["counts"]["deployed"], 0)
        self.assertTrue(self.request("POST", "/api/release-batches/" + batch["id"] + "/receipts", update)[1]["reused"])

    def test_http_invalid_host_and_origin_cannot_preview_create_or_append(self):
        first = self.releases.create(payload())
        before = snapshot(self.db)
        endpoints = ("/api/release-batches", "/api/release-batches/preview",
                     "/api/release-batches/" + first["id"] + "/receipts")
        for endpoint in endpoints:
            for headers in ({"Host": "evil.example:" + str(self.port)}, {"Host": "127.0.0.1:1"},
                            {"Origin": "https://evil.example"}, {"Origin": "http://127.0.0.1:1"},
                            {"Origin": "null"}):
                with self.subTest(endpoint=endpoint, headers=headers):
                    self.assertEqual(self.request("POST", endpoint, payload(), headers=headers)[0], 403)
        self.assertEqual(snapshot(self.db), before)

    def test_http_same_origin_is_allowed(self):
        self.assertEqual(self.request("POST", "/api/release-batches", payload(),
            headers={"Origin": "http://127.0.0.1:" + str(self.port)})[0], 201)

    def test_http_400_404_409_error_codes_and_no_partial_writes(self):
        first = self.releases.create(payload())
        before = snapshot(self.db)
        scenarios = [("GET", "/api/release-batches?environment=staging", None, 400, "invalid"),
                     ("GET", "/api/release-batches?environment=dev&environment=prod", None, 400, "invalid"),
                     ("GET", "/api/release-batches/missing", None, 404, "missing"),
                     ("POST", "/api/release-batches", {**payload(), "owner": "different"}, 409, "idempotency"),
                     ("POST", "/api/release-batches", payload("bad-task", task_ids=["missing"]), 404, "missing"),
                     ("POST", "/api/release-batches/" + first["id"] + "/receipts",
                      {"idempotency_key": "stale", "expected_revision": 2,
                       "items": [{"item_key": "item-a", "facts": {"deployment": {"status": "unknown"}}}]}, 409, "stale")]
        for method, path, data, wanted_status, wanted_code in scenarios:
            status, result = self.request(method, path, data)
            self.assertEqual((status, result["code"]), (wanted_status, wanted_code))
            self.assertEqual(result["assurance"], ASSURANCE)
        self.assertEqual(snapshot(self.db), before)

    def test_http_64k_limit_invalid_json_and_scalar_bodies_never_write(self):
        before = snapshot(self.db)
        bodies = (b"{" , b"[]", b"null", b"\xff", b" " * (64 * 1024 + 1),
                  json.dumps(payload()).encode() + b" " * (64 * 1024))
        for body in bodies:
            status, result = self.request("POST", "/api/release-batches", raw=body)
            self.assertEqual((status, result["code"]), (400, "invalid"))
        self.assertEqual(snapshot(self.db), before)


class ReleaseHandlerStreamTests(unittest.TestCase):
    """Actual request parser/handler with byte buffers, without a listening socket.

    These complement, and explicitly do not substitute for, real HTTP/browser
    integration when local socket binding is prohibited by the execution sandbox.
    """
    def setUp(self):
        ReleaseTests.setUp(self)
        self.port = 39091

    def request(self, method, path, data=None, headers=None, raw=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else b"")
        fields = {"Host": "127.0.0.1:" + str(self.port), "Connection": "close",
                  "Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}
        wire = (method + " " + path + " HTTP/1.1\r\n" +
                "\r\n".join(key + ": " + value for key, value in fields.items()) + "\r\n\r\n").encode() + body

        class ByteConnection:
            def settimeout(self, timeout):
                self.timeout = timeout
            def __init__(self):
                self.output = io.BytesIO()

            def makefile(self, mode, buffering=-1):
                return io.BytesIO(wire)

            def sendall(self, value):
                self.output.write(value)

        connection = ByteConnection()
        handler = type("SyntheticStreamHandler", (ControlPlaneHandler,), {
            "service": self.service, "coordinator": None, "engine_mode": "manual",
        })
        handler(connection, ("127.0.0.1", 39092), SimpleNamespace(server_address=("127.0.0.1", self.port)))
        response_headers, response_body = connection.output.getvalue().split(b"\r\n\r\n", 1)
        status = int(response_headers.split(b" ", 2)[1])
        result = json.loads(response_body)
        print("HANDLER_STREAM_EVIDENCE " + json.dumps({"method": method, "path": path, "status": status,
              "code": result.get("code"), "revision": result.get("revision"), "reused": result.get("reused"),
              "listening_socket": False}, sort_keys=True))
        return status, result


for _name, _test in list(ReleaseHTTPTests.__dict__.items()):
    if _name.startswith("test_http_"):
        setattr(ReleaseHandlerStreamTests, _name, _test)


# HTTP classes share cases, but inherited service tests should run only once.
def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(ReleaseTests))
    suite.addTests(ReleaseHTTPTests(name) for name in sorted(ReleaseHTTPTests.__dict__) if name.startswith("test_http_"))
    suite.addTests(loader.loadTestsFromTestCase(ReleaseHandlerStreamTests))
    return suite


if __name__ == "__main__":
    print("TEST_ENVIRONMENT " + json.dumps({"python": platform.python_version(), "data": "synthetic-only",
          "network": "127.0.0.1 random port, never 8765/8766", "process_dispatch": "hard blocked",
          "scheduler": "not started", "assurance": ASSURANCE}, sort_keys=True))
    unittest.main(verbosity=2)
