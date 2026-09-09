from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qingtian_engine.cli import create_demo
from qingtian_engine.db import Database
from qingtian_engine.importers import import_governance, import_tasks_markdown
from qingtian_engine.reporting import daily_report
from qingtian_engine.runner import RunManager
from qingtian_engine.service import ControlPlane


TASKS_FIXTURE = """\
# Tasks

## Active

- [ ] **P0：组件修复** - frontend
  - 只跑相关测试

## Waiting On

- [ ] **外部回调** - since now

## Done

- [x] ~~完成事项~~ (2026-07-27)
"""

GOVERNANCE_FIXTURE = """\
| coordinator | `synthetic-thread-0` | `Coordinator` | manager | Intake and planning |
| qa | `synthetic-thread-6` | `Quality` | qa | Verification only |
"""


class ImportAndDemoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = ControlPlane(Database(self.root / "control.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_import_is_idempotent(self) -> None:
        tasks = self.root / "TASKS.md"
        tasks.write_text(TASKS_FIXTURE, encoding="utf-8")
        governance = self.root / "governance.md"
        governance.write_text(GOVERNANCE_FIXTURE, encoding="utf-8")
        first = import_tasks_markdown(self.service, tasks)
        second = import_tasks_markdown(self.service, tasks)
        forced = import_tasks_markdown(self.service, tasks, force=True)
        after_force = import_tasks_markdown(self.service, tasks)
        sessions = import_governance(self.service, governance)
        self.assertEqual(3, first["imported"])
        self.assertEqual(0, second["imported"])
        self.assertEqual({"imported": 0, "skipped": 3}, forced)
        self.assertEqual({"imported": 0, "skipped": 3}, after_force)
        self.assertEqual(2, sessions["imported"])
        self.assertEqual(3, len(self.service.list_tasks()))
        persisted_sources = [
            row["source"] for row in self.service.db.all("SELECT source FROM imports")
        ]
        persisted_sources.extend(
            row["source"] for row in self.service.db.all("SELECT source FROM sessions")
        )
        persisted_sources.extend(
            row["imported_from"]
            for row in self.service.db.all("SELECT imported_from FROM tasks")
        )
        self.assertTrue(persisted_sources)
        for source in persisted_sources:
            self.assertTrue(source.startswith("markdown:"))
            self.assertNotIn(str(self.root), source)

    def test_imported_tasks_remain_reference_only_after_reopen(self) -> None:
        tasks = self.root / "TASKS.md"
        tasks.write_text(TASKS_FIXTURE, encoding="utf-8")
        import_tasks_markdown(self.service, tasks)
        # Simulate a database written before explicit reference policies were
        # introduced; reopening must migrate it fail-closed.
        self.service.db.execute(
            "UPDATE tasks SET authorization_policy='normal' WHERE imported_from!=''"
        )
        reopened = ControlPlane(Database(self.root / "control.sqlite3"))
        imported = reopened.list_tasks()
        self.assertTrue(imported)
        for task in imported:
            self.assertEqual("reference-only", task["authorization_policy"])
            self.assertFalse(RunManager._eligible_for_managed_dispatch(task))
        manager = RunManager(reopened, self.root)
        with patch.object(manager, "dispatch", side_effect=AssertionError("reference must not dispatch")):
            self.assertEqual([], manager.reconcile_dispatch_queue()["claimed"])
        self.assertEqual([], reopened.db.all("SELECT id FROM runs"))

    def test_demo_has_all_dashboard_columns_and_report(self) -> None:
        result = create_demo(self.service, self.root, reset=True)
        payload = self.service.dashboard_payload()
        self.assertFalse(result["paid_api_called"])
        for state in ("INBOX", "RUNNING", "WAITING", "VERIFYING", "DONE"):
            self.assertGreaterEqual(len(payload["columns"][state]), 1)
        report = daily_report(self.service)
        self.assertIn("10字概要", report)
        self.assertIn("Commit/Test/Deploy/Smoke", report)
        self.assertIn("控制面全状态闭环", report)


if __name__ == "__main__":
    unittest.main()
