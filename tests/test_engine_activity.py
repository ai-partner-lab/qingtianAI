from pathlib import Path
import tempfile
import unittest
from qingtian_engine.cli import build_service, create_demo


class EngineActivityTests(unittest.TestCase):
    def test_projection_exposes_actual_event_time_not_fixed_percentage(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = build_service(Path(temporary))
            task = service.create_task("Synthetic activity fixture")
            service.db.add_event(task["id"], "item.completed", "codex-json", "bounded metadata", "event-once")
            expected = service.db.one("SELECT occurred_at FROM events WHERE dedupe_key='event-once'")["occurred_at"]
            service.db.add_event(task["id"], "system.note", "system", "not worker activity", "system-once")
            activity = service.dashboard_payload()["tasks"][0]["activity"]
            self.assertEqual(activity["last_event_at"], expected)
            self.assertEqual(activity["last_event_type"], "item.completed")
            self.assertEqual(activity["progress_kind"], "state-milestone-not-completion")
            self.assertFalse(activity["synthetic"])
            self.assertIsNone(activity["run_started_at"])

    def test_synthetic_cards_explicitly_marked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = build_service(root)
            create_demo(service, root, False)
            service = build_service(root)  # migrations must not collapse tour columns
            dashboard = service.dashboard_payload()
            self.assertTrue(all(t["activity"]["synthetic"] for t in dashboard["tasks"]))
            for state in ("INBOX", "RUNNING", "WAITING", "VERIFYING", "DONE"):
                self.assertEqual(len(dashboard["columns"][state]), 1, state)

    def test_card_does_not_animate_a_stage_value_as_completion(self):
        script = (Path(__file__).parents[1] / "qingtian_engine/static/app.js").read_text()
        card = script.split("function taskCard(", 1)[1].split("function renderBoard(", 1)[0]
        self.assertNotIn("animateNumber(", card)
        self.assertNotIn("task.progress", card)
        self.assertIn("taskActivity(task)", card)
        self.assertIn("不是实际工作完成百分比", card)
