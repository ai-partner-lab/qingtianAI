"""Synthetic, isolated lifecycle transactions. No paid models or live task writes."""
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from qingtian_engine.db import Database
    from qingtian_engine.service import ControlPlane
    from qingtian_engine.lifecycle import LifecycleError
    from qingtian_engine.runner import RunManager
except ModuleNotFoundError:
    from xhcontrol.db import Database
    from xhcontrol.service import ControlPlane
    from xhcontrol.lifecycle import LifecycleError
    from xhcontrol.runner import RunManager


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qingtian-lifecycle-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "db.sqlite3")
        self.service = ControlPlane(self.db)
        self.task = self.service.create_task("Synthetic lifecycle test", idempotency_key="fixture", state="VERIFYING", owner_session="manager", evidence_profile="artifact")
        self.task_id = self.task["id"]
        self.sequence = 0

    def now(self, seconds=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def payload(self, **fields):
        self.sequence += 1
        return {"expected_revision": self.service.lifecycle.snapshot(self.task_id)["revision"], "idempotency_key": "request-" + str(self.sequence), "actor": "manager", "source_ref": "thread:synthetic-turn", **fields}

    def apply(self, action, **fields):
        return self.service.lifecycle.apply(self.task_id, action, self.payload(**fields))

    def dump(self):
        with self.db.connect() as connection:
            return "\n".join(connection.iterdump())

    def register(self, **fields):
        return self.apply("external-register", **{"executor": "executor", "model": "gpt-5.6-sol", "reasoning": "high", "speed": "standard", "source_thread": "thread-1", "source_turn": "turn-1", "last_activity_at": self.now(-20), "activity_ref": "event:actual-1", **fields})

    def offer(self, **fields):
        return self.apply("handoff-offer", **{"recipient": "receiver", "deadline": self.now(3600), "stage": "release", "next_action": "Review sealed package", "artifacts": [{"ref": "artifact:sealed-package", "sha256": "a" * 64}], **fields})

    def test_amend_audits_fields_and_preserves_runs_state_and_authority(self):
        before = self.service.get_task(self.task_id)
        result = self.apply("amend", changes={"requires_deploy": True, "environment": "dev", "stage": "deploy"})
        current = self.service.get_task(self.task_id)
        self.assertEqual(before["state"], current["state"])
        self.assertEqual(before["authorization_policy"], current["authorization_policy"])
        self.assertEqual(before["runs"], current["runs"])
        self.assertEqual("deploy", result["lifecycle"]["stage"])
        event = self.db.one("SELECT * FROM events WHERE event_type='lifecycle.amend'")
        audit = json.loads(event["payload_json"])
        self.assertEqual(0, audit["result"]["before"]["requires_deploy"])
        self.assertIs(True, audit["result"]["after"]["requires_deploy"])
        self.assertEqual("thread:synthetic-turn", audit["source_ref"])

    def test_strict_invalid_input_has_zero_side_effects(self):
        for changes in ({"requires_deploy": "true"}, {"requires_deploy": 1}, {"environment": []}, {"stage": {}}, {"repository": "/tmp/forbidden"}, {"authorization_policy": "normal"}, {"action_text": "x" * 501}, {"action_text": "Bearer abcdefghijklmnop"}, {"action_due": "tomorrow"}):
            p = self.payload(changes=changes)
            before = self.dump()
            with self.assertRaises(LifecycleError):
                self.service.lifecycle.apply(self.task_id, "amend", p)
            self.assertEqual(before, self.dump())
        before = self.dump()
        p = self.payload(changes={"stage": "review"}, source_ref="../secrets")
        with self.assertRaises(LifecycleError):
            self.service.lifecycle.apply(self.task_id, "amend", p)
        self.assertEqual(before, self.dump())

    def test_exact_replay_and_key_conflict(self):
        p = self.payload(changes={"requires_deploy": True})
        result = self.service.lifecycle.apply(self.task_id, "amend", p)
        before = self.dump()
        replay = self.service.lifecycle.apply(self.task_id, "amend", p)
        self.assertTrue(replay["reused"])
        self.assertEqual(result["revision"], replay["revision"])
        self.assertEqual(before, self.dump())
        with self.assertRaisesRegex(LifecycleError, "different payload"):
            self.service.lifecycle.apply(self.task_id, "amend", {**p, "changes": {"environment": "dev"}})
        self.assertEqual(before, self.dump())
        stored = self.db.one("SELECT request_json FROM lifecycle_requests")
        self.assertEqual(64, len(stored["request_json"]))

    def test_concurrent_revision_has_one_winner(self):
        barrier = threading.Barrier(2)
        p = self.payload(changes={"environment": "dev"})
        def race(key):
            barrier.wait()
            try:
                return self.service.lifecycle.apply(self.task_id, "amend", {**p, "idempotency_key": key})["revision"]
            except LifecycleError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(race, ("race-a", "race-b")))
        self.assertEqual(1, outcomes.count("stale_revision"))
        self.assertEqual(1, len(self.db.all("SELECT * FROM lifecycle_requests")))

    def test_native_task_update_invalidates_revision(self):
        p = self.payload(changes={"stage": "review"})
        self.db.execute("UPDATE tasks SET owner_session='new-owner' WHERE id=?", (self.task_id,))
        before = self.dump()
        with self.assertRaisesRegex(LifecycleError, "stale lifecycle revision"):
            self.service.lifecycle.apply(self.task_id, "amend", p)
        self.assertEqual(before, self.dump())

    def test_audit_abort_and_ignore_roll_back_every_table(self):
        for mode in ("ABORT,'synthetic audit failure'", "IGNORE"):
            self.db.execute("CREATE TRIGGER fail_lifecycle_audit BEFORE INSERT ON events WHEN NEW.event_type LIKE 'lifecycle.%' BEGIN SELECT RAISE(" + mode + "); END")
            before = self.dump()
            with self.assertRaises((LifecycleError, sqlite3.IntegrityError)):
                self.apply("amend", changes={"requires_deploy": True, "stage": "deploy"})
            self.assertEqual(before, self.dump())
            self.db.execute("DROP TRIGGER fail_lifecycle_audit")

    def test_paused_terminal_analysis_and_imported_reject_new_work(self):
        cases = [("state", "PAUSED"), ("state", "CANCELED"), ("state", "DONE"), ("authorization_policy", "analysis-only"), ("imported_from", "historical-archive")]
        for name, value in cases:
            self.db.execute("UPDATE tasks SET " + name + "=? WHERE id=?", (value, self.task_id))
            before = self.dump()
            with self.assertRaises(LifecycleError):
                self.apply("amend", changes={"requires_deploy": True})
            with self.assertRaises(LifecycleError):
                self.register()
            self.assertEqual(before, self.dump())
            self.db.execute("UPDATE tasks SET state='VERIFYING',authorization_policy='normal',imported_from='' WHERE id=?", (self.task_id,))

    def test_deploy_upgrade_requires_deploy_smoke_for_all_profiles_even_force(self):
        for profile, kinds in (("artifact", ["artifact"]), ("browser", ["browser"]), ("qa", ["test"]), ("code", ["commit", "test"]), ("legacy", ["migration_metadata"])):
            self.db.execute("UPDATE tasks SET evidence_profile=? WHERE id=?", (profile, self.task_id))
            for kind in kinds:
                self.service.add_evidence(self.task_id, kind, "synthetic-" + kind, verified=True)
            self.apply("amend", changes={"requires_deploy": True})
            self.assertIn("deploy", self.service.required_evidence(self.task_id))
            self.assertIn("smoke", self.service.required_evidence(self.task_id))
            before = self.dump()
            with self.assertRaises(ValueError):
                self.service.transition(self.task_id, "DONE", force=True)
            self.assertEqual(before, self.dump())
        self.service.add_evidence(self.task_id, "deploy", "synthetic-deploy", verified=True)
        self.service.add_evidence(self.task_id, "smoke", "synthetic-smoke", verified=True)
        self.assertEqual("DONE", self.service.transition(self.task_id, "DONE")["state"])

    def test_deploy_contract_cannot_be_weakened(self):
        self.apply("amend", changes={"requires_deploy": True})
        before = self.dump()
        with self.assertRaisesRegex(LifecycleError, "cannot be weakened"):
            self.apply("amend", changes={"requires_deploy": False})
        self.assertEqual(before, self.dump())

    def test_managed_run_prevents_external_registration_and_identity_amendment(self):
        self.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) VALUES('managed',?,1,'cli','synthetic','RUNNING',?)", (self.task_id, self.now()))
        before = self.dump()
        with self.assertRaises(LifecycleError):
            self.register()
        with self.assertRaises(LifecycleError):
            self.apply("amend", changes={"owner_session": "other"})
        with self.assertRaises(LifecycleError):
            self.apply("amend", changes={"environment": "dev"})
        self.assertEqual(before, self.dump())

    def test_external_registration_prevents_managed_dispatch_without_side_effects(self):
        self.register()
        manager = RunManager(self.service, Path(self.temp.name))
        before = self.dump()
        with patch("subprocess.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(RuntimeError, "LIFECYCLE"):
                manager.dispatch(self.task_id, Path(self.temp.name) / "not-read.txt")
        self.assertEqual(before, self.dump())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at) VALUES('racing',?,1,'cli','synthetic','QUEUED',?)", (self.task_id, self.now()))
        self.assertEqual([], self.service.get_task(self.task_id)["runs"])

    def test_actual_models_preserved_and_polling_evidence_does_not_heartbeat(self):
        self.register()
        self.register(executor="reviewer", model="gpt-6-astra", reasoning="xhigh", speed="unknown", source_thread="thread-2")
        before = self.service.lifecycle.snapshot(self.task_id)
        self.service.add_evidence(self.task_id, "artifact", "synthetic-note", verified=False)
        self.service.dashboard_payload()
        self.service.get_task(self.task_id)
        after = self.service.lifecycle.snapshot(self.task_id)
        self.assertEqual(before, after)
        self.assertIsNone(self.service.get_task(self.task_id)["heartbeat_at"])
        self.assertEqual({("gpt-5.6-sol", "high"), ("gpt-6-astra", "xhigh")}, {(e["model"], e["reasoning"]) for e in after["external_executions"]})

    def test_external_finish_is_idempotent_and_not_done(self):
        execution = self.register()["result"]["execution_id"]
        p = self.payload(actor="executor", execution_id=execution, status="finished", finished_at=self.now(-1), artifacts=[{"ref": "artifact:result", "sha256": "b" * 64}])
        result = self.service.lifecycle.apply(self.task_id, "external-finish", p)
        before = self.dump()
        self.assertFalse(result["result"]["completion_granted"])
        self.assertTrue(self.service.lifecycle.apply(self.task_id, "external-finish", p)["reused"])
        self.assertEqual(before, self.dump())
        self.assertEqual("VERIFYING", self.service.get_task(self.task_id)["state"])
        self.assertEqual([], self.service.get_task(self.task_id)["runs"])

    def test_activity_requires_new_actual_source_and_identity(self):
        execution = self.register()["result"]["execution_id"]
        before = self.dump()
        with self.assertRaises(LifecycleError):
            self.apply("external-activity", execution_id=execution, last_activity_at=self.now(-2), activity_ref="event:actual-2")
        with self.assertRaises(LifecycleError):
            self.apply("external-activity", actor="executor", execution_id=execution, last_activity_at=self.now(-2), activity_ref="event:actual-1")
        self.assertEqual(before, self.dump())
        self.apply("external-activity", actor="executor", execution_id=execution, last_activity_at=self.now(-2), activity_ref="event:actual-2")
        self.assertIsNone(self.service.get_task(self.task_id)["heartbeat_at"])

    def test_handoff_offer_delivery_acceptance_and_execution_are_distinct(self):
        offer = self.offer()
        self.assertEqual("awaiting_acceptance", offer["lifecycle"]["status"])
        self.assertFalse(offer["result"]["accepted"])
        self.assertEqual([], offer["lifecycle"]["external_executions"])
        h = offer["result"]
        accepted = self.apply("handoff-accept", actor="receiver", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"])
        self.assertEqual("accepted", accepted["lifecycle"]["status"])
        self.assertFalse(accepted["result"]["execution_started"])
        running = self.register(executor="receiver")
        self.assertEqual("execution_active", running["lifecycle"]["status"])
        self.assertEqual("accepted", running["lifecycle"]["handoffs"][0]["status"])

    def test_handoff_rejection_requires_missing_items_and_recipient(self):
        h = self.offer()["result"]
        before = self.dump()
        for fields in ({"actor": "impostor", "reason": "Missing tests", "missing_items": ["tests"]}, {"actor": "receiver", "reason": "Missing tests", "missing_items": []}):
            with self.assertRaises(LifecycleError):
                self.apply("handoff-reject", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"], **fields)
        self.assertEqual(before, self.dump())
        rejected = self.apply("handoff-reject", actor="receiver", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"], reason="Missing tests", missing_items=["tests"])
        self.assertEqual("rejected", rejected["lifecycle"]["status"])
        self.assertEqual("manager", rejected["lifecycle"]["next_action"]["owner"])

    def test_handoff_late_stale_and_paused_ack_rejected(self):
        h = self.offer()["result"]
        self.db.execute("UPDATE lifecycle_handoffs SET deadline=? WHERE id=?", (self.now(-1), h["handoff_id"]))
        before = self.dump()
        with self.assertRaisesRegex(LifecycleError, "deadline expired"):
            self.apply("handoff-accept", actor="receiver", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"])
        self.assertEqual(before, self.dump())
        self.db.execute("UPDATE lifecycle_handoffs SET deadline=? WHERE id=?", (self.now(60), h["handoff_id"]))
        self.db.execute("UPDATE tasks SET state='PAUSED' WHERE id=?", (self.task_id,))
        before = self.dump()
        with self.assertRaises(LifecycleError):
            self.apply("handoff-accept", actor="receiver", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"])
        self.assertEqual(before, self.dump())

    def test_restart_preserves_unacked_and_outbox_reclaim_rejects_stale_token(self):
        offer = self.offer()
        notice = offer["lifecycle"]["outbox"][0]["id"]
        p = self.payload(actor="bridge", outbox_id=notice, lease_seconds=30)
        claim = self.service.lifecycle.apply(self.task_id, "outbox-claim", p)["result"]
        self.service = ControlPlane(Database(self.db.path))
        self.assertEqual("claimed", self.service.lifecycle.snapshot(self.task_id)["outbox"][0]["status"])
        self.assertTrue(self.service.lifecycle.apply(self.task_id, "outbox-claim", p)["reused"])
        self.db.execute("UPDATE lifecycle_outbox SET lease_until=? WHERE id=?", (self.now(-1), notice))
        before = self.dump()
        with self.assertRaisesRegex(LifecycleError, "stale"):
            self.apply("outbox-ack", actor="bridge", outbox_id=notice, claim_token=claim["claim_token"], delivery_ref="host:delivery-1")
        self.assertEqual(before, self.dump())
        renewed = self.apply("outbox-claim", actor="bridge", outbox_id=notice, lease_seconds=30)["result"]
        with self.assertRaises(LifecycleError):
            self.apply("outbox-ack", actor="bridge", outbox_id=notice, claim_token=claim["claim_token"], delivery_ref="host:delivery-1")
        p = self.payload(actor="bridge", outbox_id=notice, claim_token=renewed["claim_token"], delivery_ref="host:delivery-2")
        ack = self.service.lifecycle.apply(self.task_id, "outbox-ack", p)
        self.assertTrue(ack["result"]["delivered"])
        self.assertFalse(ack["result"]["recipient_accepted"])
        self.assertEqual("offered", ack["lifecycle"]["handoffs"][0]["status"])
        self.assertTrue(self.service.lifecycle.apply(self.task_id, "outbox-ack", p)["reused"])
        events = self.db.all("SELECT payload_json FROM events WHERE event_type LIKE 'lifecycle.%'")
        self.assertNotIn(renewed["claim_token"], json.dumps(events))

    def test_manual_reconcile_lost_and_overdue_notify_once_never_resume(self):
        execution = self.register(last_activity_at=self.now(-1800))["result"]["execution_id"]
        h = self.offer()["result"]["handoff_id"]
        self.db.execute("UPDATE lifecycle_handoffs SET deadline=? WHERE id=?", (self.now(-1), h))
        self.db.execute("UPDATE tasks SET state='PAUSED' WHERE id=?", (self.task_id,))
        before_activity = self.service.lifecycle.snapshot(self.task_id)["external_executions"][0]["last_activity_at"]
        with patch("subprocess.Popen", side_effect=AssertionError("must not dispatch")):
            first = self.apply("reconcile")
            self.service = ControlPlane(Database(self.db.path))
            self.apply("reconcile")
        self.assertEqual([execution], first["result"]["lost_executions"])
        self.assertEqual("PAUSED", self.service.get_task(self.task_id)["state"])
        snap = self.service.lifecycle.snapshot(self.task_id)
        self.assertEqual(before_activity, snap["external_executions"][0]["last_activity_at"])
        self.assertEqual(1, sum(n["kind"] == "execution.lost" for n in snap["outbox"]))
        self.assertEqual(1, sum(n["kind"] == "handoff.overdue" for n in snap["outbox"]))
        self.assertEqual([], self.service.get_task(self.task_id)["runs"])

    def test_unresolved_handoff_and_external_prevent_done_in_service_and_sql(self):
        self.service.add_evidence(self.task_id, "artifact", "synthetic-result", verified=True)
        h = self.offer()["result"]
        with self.assertRaises(ValueError):
            self.service.transition(self.task_id, "DONE", force=True)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE tasks SET state='DONE' WHERE id=?", (self.task_id,))
        self.apply("handoff-resolve", handoff_id=h["handoff_id"], resolution="withdrawn", resolution_ref="thread:withdraw-authority")
        execution = self.register()["result"]["execution_id"]
        with self.assertRaises(ValueError):
            self.service.transition(self.task_id, "DONE", force=True)
        self.apply("external-finish", actor="executor", execution_id=execution, status="finished", finished_at=self.now(-1), artifacts=[])
        self.assertEqual("VERIFYING", self.service.get_task(self.task_id)["state"])
        self.assertEqual("DONE", self.service.transition(self.task_id, "DONE")["state"])

    def test_normal_manual_watchdog_detects_stall_and_restart_is_noop(self):
        import importlib
        module = importlib.import_module(ControlPlane.__module__.rsplit(".", 1)[0] + ".runtime_mode")
        self.register(last_activity_at=self.now(-1800))
        handoff = self.offer()["result"]["handoff_id"]
        self.db.execute("UPDATE lifecycle_handoffs SET deadline=? WHERE id=?", (self.now(-1), handoff))
        self.db.execute("UPDATE tasks SET state='PAUSED' WHERE id=?", (self.task_id,))
        native = self.db.one("SELECT * FROM tasks WHERE id=?", (self.task_id,))
        with patch("subprocess.Popen", side_effect=AssertionError("watchdog must not launch")):
            result = module.background_cycle(RunManager(self.service, Path(self.temp.name)), None, "manual")
            self.assertEqual(1, result["reconcile"]["lifecycle_lost_executions"])
            self.assertEqual(1, result["reconcile"]["lifecycle_overdue_handoffs"])
            self.assertEqual([], result["dispatch"]["claimed"])
            self.service = ControlPlane(Database(self.db.path))
            before = self.dump()
            second = module.background_cycle(RunManager(self.service, Path(self.temp.name)), None, "manual")
        self.assertEqual(0, second["reconcile"]["lifecycle_tasks_reconciled"])
        self.assertEqual(before, self.dump())
        self.assertEqual(native, self.db.one("SELECT * FROM tasks WHERE id=?", (self.task_id,)))
        self.assertEqual([], self.service.get_task(self.task_id)["runs"])

    def test_accepted_unstarted_deadline_notifies_recipient_once(self):
        h = self.offer()["result"]
        self.apply("handoff-accept", actor="receiver", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"])
        # An unrelated execution by the same recipient is not proof of a start.
        self.register(executor="receiver")
        self.db.execute("UPDATE lifecycle_handoffs SET deadline=? WHERE id=?", (self.now(-1), h["handoff_id"]))
        result = self.service.lifecycle.reconcile_due()
        self.assertEqual(1, result["start_overdue_handoffs"])
        snap = self.service.lifecycle.snapshot(self.task_id)
        self.assertTrue(snap["handoffs"][0]["start_overdue"])
        self.assertFalse(snap["handoffs"][0]["execution_started"])
        notices = [n for n in snap["outbox"] if n["kind"] == "handoff.start_overdue"]
        self.assertEqual(["receiver"], [n["recipient"] for n in notices])
        self.service = ControlPlane(Database(self.db.path))
        before = self.dump()
        self.service.lifecycle.reconcile_due()
        self.assertEqual(before, self.dump())

    def test_explicit_binding_proves_start_even_after_execution_finishes(self):
        h = self.offer()["result"]
        self.apply("handoff-accept", actor="receiver", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"])
        before = self.dump()
        with self.assertRaises(LifecycleError):
            self.register(executor="wrong", handoff_id=h["handoff_id"], last_activity_at=self.now())
        self.assertEqual(before, self.dump())
        execution = self.register(executor="receiver", handoff_id=h["handoff_id"], last_activity_at=self.now())["result"]["execution_id"]
        self.apply("external-finish", actor="receiver", execution_id=execution, status="finished", finished_at=self.now(), artifacts=[])
        self.db.execute("UPDATE lifecycle_handoffs SET deadline=? WHERE id=?", (self.now(-1), h["handoff_id"]))
        self.assertEqual(0, self.service.lifecycle.reconcile_due()["start_overdue_handoffs"])
        snap = self.service.lifecycle.snapshot(self.task_id)
        self.assertTrue(snap["handoffs"][0]["execution_started"])
        self.assertEqual([execution], snap["handoffs"][0]["execution_ids"])
        self.assertFalse(snap["handoffs"][0]["start_overdue"])
        self.assertTrue(snap["completion_blockers"])

    def test_legacy_heartbeat_without_model_never_resumes_protected_tasks(self):
        for values in ({"state": "PAUSED"}, {"state": "CANCELED"}, {"state": "FAILED"}, {"state": "WAITING", "blocking_reason": "用户暂停"}, {"authorization_policy": "analysis-only"}, {"imported_from": "retired-archive"}):
            self.db.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), self.task_id))
            before = self.dump()
            with self.assertRaises(ValueError):
                self.service.heartbeat_task(self.task_id)
            self.assertEqual(before, self.dump())
            self.db.execute("UPDATE tasks SET state='VERIFYING',blocking_reason='',authorization_policy='normal',imported_from='' WHERE id=?", (self.task_id,))

    def test_legacy_heartbeat_audit_failure_is_atomic(self):
        self.db.execute("CREATE TRIGGER fail_heartbeat BEFORE INSERT ON events WHEN NEW.event_type='task.external_heartbeat' BEGIN SELECT RAISE(IGNORE); END")
        before = self.dump()
        with self.assertRaises(LifecycleError):
            self.service.heartbeat_task(self.task_id)
        self.assertEqual(before, self.dump())

    def test_delivered_loss_then_recovery_gets_new_notice_on_next_loss_episode(self):
        execution = self.register(last_activity_at=self.now(-3600))["result"]["execution_id"]
        self.assertEqual(1, self.service.lifecycle.reconcile_due()["lost_executions"])
        first = self.service.lifecycle.snapshot(self.task_id)["outbox"][0]
        claim = self.apply("outbox-claim", actor="bridge", outbox_id=first["id"], lease_seconds=30)["result"]
        self.apply("outbox-ack", actor="bridge", outbox_id=first["id"], claim_token=claim["claim_token"], delivery_ref="host:first-loss-delivered")

        # Restart within the same loss episode cannot recreate delivered work.
        self.service = ControlPlane(Database(self.db.path))
        before = self.dump()
        self.assertEqual(0, self.service.lifecycle.reconcile_due()["tasks_reconciled"])
        self.assertEqual(before, self.dump())

        recovered_at = self.now(-1)
        recovered = self.apply("external-activity", actor="executor", execution_id=execution, last_activity_at=recovered_at, activity_ref="event:recovered-actual-activity")
        self.assertEqual("active", recovered["lifecycle"]["external_executions"][0]["status"])
        future = datetime.now(timezone.utc) + timedelta(minutes=16)

        class NextEpisodeClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future.astimezone(tz) if tz is not None else future.replace(tzinfo=None)

        lifecycle_module = ControlPlane.__module__.rsplit(".", 1)[0] + ".lifecycle"
        with patch(lifecycle_module + ".datetime", NextEpisodeClock):
            self.assertEqual(1, self.service.lifecycle.reconcile_due()["lost_executions"])
            self.service = ControlPlane(Database(self.db.path))
            before = self.dump()
            self.assertEqual(0, self.service.lifecycle.reconcile_due()["tasks_reconciled"])
            self.assertEqual(before, self.dump())
            snap = self.service.lifecycle.snapshot(self.task_id)

        notices = [notice for notice in snap["outbox"] if notice["kind"] == "execution.lost"]
        self.assertEqual(2, len(notices))
        old_notice = next(notice for notice in notices if notice["id"] == first["id"])
        new_notice = next(notice for notice in notices if notice["id"] != first["id"])
        self.assertEqual("delivered", old_notice["status"])
        self.assertEqual("pending", new_notice["status"])
        self.assertNotEqual(old_notice["dedupe_key"], new_notice["dedupe_key"])
        self.assertEqual(recovered_at, snap["external_executions"][0]["last_activity_at"])
        self.assertEqual([], self.service.get_task(self.task_id)["runs"])

    def test_lost_execution_owns_next_action_not_later_healthy_executor(self):
        self.register(executor="lost-first", source_thread="thread-older", last_activity_at=self.now(-1800))
        result = self.register(executor="healthy-last", source_thread="thread-newer", last_activity_at=self.now(-1))
        self.assertEqual("execution_lost", result["lifecycle"]["status"])
        self.assertEqual("lost-first", result["lifecycle"]["next_action"]["owner"])
        self.assertEqual("active", result["lifecycle"]["external_executions"][-1]["display_status"])
        self.assertEqual("healthy-last", result["lifecycle"]["external_executions"][-1]["executor"])

    def test_structured_activity_supersedes_only_its_legacy_heartbeat_watchdog(self):
        import importlib
        runtime_mode = importlib.import_module(ControlPlane.__module__.rsplit(".", 1)[0] + ".runtime_mode")
        expired = self.now(-1800)
        self.db.execute("UPDATE tasks SET state='RUNNING',execution_mode='external',heartbeat_at=?,updated_at=? WHERE id=?", (expired, expired, self.task_id))
        self.register(last_activity_at=self.now(-1))
        native = self.db.one("SELECT * FROM tasks WHERE id=?", (self.task_id,))
        legacy = self.service.create_task("Synthetic old protocol task", idempotency_key="old-protocol", state="RUNNING", evidence_profile="artifact")
        self.db.execute("UPDATE tasks SET execution_mode='external',heartbeat_at=?,updated_at=? WHERE id=?", (expired, expired, legacy["id"]))
        with patch("subprocess.Popen", side_effect=AssertionError("manual cycle must not launch")):
            result = runtime_mode.background_cycle(RunManager(self.service, Path(self.temp.name)), None, "manual")
        self.assertEqual(native, self.db.one("SELECT * FROM tasks WHERE id=?", (self.task_id,)))
        self.assertEqual("execution_active", self.service.lifecycle.snapshot(self.task_id)["status"])
        self.assertEqual(1, result["reconcile"]["external_stale"])
        self.assertEqual("WAITING", self.service.get_task(legacy["id"])["state"])
        self.assertTrue(self.service.get_task(legacy["id"])["blocking_reason"].startswith("STALE_EXECUTION:"))
        projected = next(t for t in self.service.dashboard_payload()["tasks"] if t["id"] == self.task_id)
        self.assertEqual("RUNNING", projected["state"])
        self.assertNotIn(projected["runtime_status"]["code"], {"RECOVERY_REQUIRED", "EXTERNAL_STALE"})
        self.assertEqual([], result["dispatch"]["claimed"])

    def test_finished_execution_and_resolved_handoff_cannot_return_to_legacy_heartbeat(self):
        execution = self.register()["result"]["execution_id"]
        h = self.offer()["result"]
        self.apply("handoff-accept", actor="receiver", handoff_id=h["handoff_id"], manifest_sha256=h["manifest_sha256"])
        self.apply("external-finish", actor="executor", execution_id=execution, status="finished", finished_at=self.now(-1), artifacts=[])
        self.apply("handoff-resolve", handoff_id=h["handoff_id"], resolution="completed", resolution_ref="thread:reviewed-handoff-close")
        lifecycle = self.service.lifecycle.snapshot(self.task_id)
        self.assertEqual([], lifecycle["completion_blockers"])
        self.assertEqual("finished", lifecycle["external_executions"][0]["status"])
        self.assertEqual("resolved", lifecycle["handoffs"][0]["status"])
        before = self.dump()
        with self.assertRaisesRegex(LifecycleError, "lifecycle-owned"):
            self.service.heartbeat_task(self.task_id)
        self.assertEqual(before, self.dump())


if __name__ == "__main__":
    unittest.main()
