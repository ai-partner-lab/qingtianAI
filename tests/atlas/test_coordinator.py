from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qingtian_engine.coordinator import RecoveryCoordinator
from qingtian_engine.db import Database
from qingtian_engine.service import ControlPlane


class _Manager:
    def scheduler_tick(self, max_new: int = 1, max_active: int = 3):
        return {
            "reconcile": {"stale": 0},
            "verification": {"started": []},
            "dispatch": {"claimed": []},
        }


class CoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "control.sqlite3")
        self.service = ControlPlane(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_lease_is_exclusive_and_fenced_after_expiry(self) -> None:
        first = RecoveryCoordinator(self.service, _Manager(), holder_id="first")
        second = RecoveryCoordinator(self.service, _Manager(), holder_id="second")
        self.assertEqual(1, first.acquire_lease())
        self.assertIsNone(second.acquire_lease())
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.db.execute(
            "UPDATE orchestrator_leases SET expires_at=? WHERE lease_key=?",
            (expired, RecoveryCoordinator.LEASE_KEY),
        )
        self.assertEqual(2, second.acquire_lease())

    def test_evidence_contract_projection_and_plugin_defaults(self) -> None:
        task = self.service.create_task(
            "代码变更", idempotency_key="contract", repository="/tmp/repo",
            evidence_profile="code",
        )
        coordinator = RecoveryCoordinator(self.service, _Manager())
        first = coordinator.sync_evidence_contracts()
        self.assertEqual(2, first["missing"])
        self.service.add_evidence(task["id"], "commit", "abc123", verified=True)
        second = coordinator.sync_evidence_contracts()
        self.assertEqual(1, second["satisfied"])
        status = coordinator.status()
        codex = next(item for item in status["plugins"] if item["name"] == "codex")
        self.assertEqual("gpt-5.6-sol", codex["model"])
        self.assertEqual("high", codex["min_reasoning"])
        self.assertTrue(codex["enabled"])

    def test_tick_persists_checkpoint(self) -> None:
        coordinator = RecoveryCoordinator(self.service, _Manager(), holder_id="unit")
        result = coordinator.tick()
        self.assertEqual("READY", result["status"])
        status = coordinator.status()
        self.assertEqual("READY", status["status"])
        self.assertEqual(1, status["fencing_token"])


if __name__ == "__main__":
    unittest.main()
