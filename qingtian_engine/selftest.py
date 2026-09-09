"""Synthetic checks of the same state and evidence engine used by real work."""
from __future__ import annotations

from pathlib import Path
import tempfile

from .cli import build_service
from .runtime_mode import background_cycle
from .runner import RunManager


def run_selftest():
    checks = []
    def check(identifier, condition):
        checks.append({"id": identifier, "passed": bool(condition)})
        if not condition:
            raise AssertionError(identifier)
    with tempfile.TemporaryDirectory(prefix="qingtian-selftest-") as temporary:
        root = Path(temporary)
        service = build_service(root)
        check("fresh-empty-database", not service.list_tasks())
        task = service.create_task("Synthetic artifact", idempotency_key="selftest-once",
                                   owner_session="QT-00", worker_type="manager", evidence_profile="artifact")
        same = service.create_task("Synthetic artifact", idempotency_key="selftest-once")
        check("idempotent-task-creation", same["id"] == task["id"])
        service.transition(task["id"], "VERIFYING", force=True)
        service.add_evidence(task["id"], "artifact", "synthetic unverified result")
        service.reconcile_state_progression()
        check("unverified-is-not-done", service.get_task(task["id"])["state"] == "VERIFYING")
        service.add_evidence(task["id"], "artifact", "synthetic independently checked result", verified=True)
        service.reconcile_state_progression()
        check("verified-evidence-closes-task", service.get_task(task["id"])["state"] == "DONE")
        pending = service.create_task("Synthetic pending P0", priority=0, state="PLANNED",
                                      evidence_profile="artifact", owner_session="QT-00")
        manager = RunManager(service, root)
        cycle = background_cycle(manager, None, "manual")
        check("manual-never-claims", cycle["dispatch"]["claimed"] == []
              and not service.db.all("SELECT id FROM runs")
              and service.get_task(pending["id"])["state"] == "PLANNED")
        reopened = build_service(root)
        check("durable-reopen", len(reopened.list_tasks()) == 2
              and reopened.get_task(task["id"])["state"] == "DONE")
        check("sqlite-integrity", service.db.one("PRAGMA integrity_check")["integrity_check"] == "ok")
        dashboard = service.dashboard_payload()
        check("real-dashboard-projection", len(dashboard["tasks"]) == 2)
        return {"status": "passed", "scope": "synthetic-engine-selftest",
                "model_called": False, "business_acceptance": False,
                "checks": checks, "task_count": 2, "legacy_tasks_imported": 0}
