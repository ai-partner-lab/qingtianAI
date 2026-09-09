from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path

from qingtian_engine.db import Database
from qingtian_engine.reporting import REPORT_STATES, daily_report, daily_report_payload
from qingtian_engine.service import ControlPlane


class ReportingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = ControlPlane(
            Database(Path(self.temp.name) / "control.sqlite3")
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_payload_keeps_markdown_and_adds_structured_rows(self) -> None:
        running = self.service.create_task(
            "P0：修复日报可读性",
            idempotency_key="report-running",
            priority=0,
            owner_session="QT-03",
            repository="/tmp/management-plane",
        )
        self.service.transition(
            running["id"],
            "RUNNING",
            force=True,
            progress=72,
        )
        self.service.add_evidence(
            running["id"], "commit", "abc123", label="UI", verified=True
        )
        waiting = self.service.create_task(
            "等待验收资源",
            idempotency_key="report-waiting",
            owner_session="QT-06",
        )
        self.service.transition(
            waiting["id"],
            "WAITING",
            force=True,
            blocking_reason="等待浏览器资源",
        )

        payload = daily_report_payload(
            self.service, datetime.now(timezone.utc).date()
        )

        self.assertEqual(set(REPORT_STATES), set(payload["summary"]["counts"]))
        self.assertEqual(2, payload["summary"]["total"])
        self.assertIn("# 擎天过去24小时日报", payload["markdown"])
        self.assertIn(payload["generated_at"], payload["markdown"])
        self.assertIn(payload["window_started_at"], payload["markdown"])
        by_id = {row["id"]: row for row in payload["rows"]}
        row = by_id[running["id"]]
        self.assertEqual("RUNNING", row["state"])
        self.assertEqual(0, row["priority"])
        self.assertEqual("QT-03", row["owner"])
        self.assertEqual(72, row["progress"])
        self.assertEqual(1, row["evidence_counts"]["commit"])
        self.assertTrue(row["evidence"]["commit"][0]["verified"])
        self.assertEqual("等待浏览器资源", by_id[waiting["id"]]["risk"])
        compatible_markdown = daily_report(
            self.service, datetime.now(timezone.utc).date()
        )
        self.assertIn("| 事项 | 主责 | 状态 |", compatible_markdown)
        self.assertIn("| 阶段指数（非完成率） |", compatible_markdown)
        self.assertNotIn("| 完成度 |", compatible_markdown)

    def test_internal_states_map_to_report_groups(self) -> None:
        planned = self.service.create_task(
            "已计划",
            idempotency_key="report-planned",
        )
        self.service.transition(planned["id"], "PLANNED")
        failed = self.service.create_task(
            "执行失败",
            idempotency_key="report-failed",
        )
        self.service.transition(failed["id"], "WAITING")
        self.service.transition(failed["id"], "QUEUED")
        self.service.transition(failed["id"], "FAILED")

        payload = daily_report_payload(
            self.service, datetime.now(timezone.utc).date()
        )
        by_id = {row["id"]: row for row in payload["rows"]}
        self.assertEqual("INBOX", by_id[planned["id"]]["state"])
        self.assertEqual("WAITING", by_id[failed["id"]]["state"])
        self.assertEqual("FAILED", by_id[failed["id"]]["source_state"])

    def test_user_paused_waiting_is_a_separate_report_group(self) -> None:
        paused = self.service.create_task(
            "用户暂停的事项",
            idempotency_key="report-paused",
            state="WAITING",
            blocking_reason="PAUSED_BY_USER: 用户明确暂停，不自动分发",
        )
        waiting = self.service.create_task(
            "普通等待事项",
            idempotency_key="report-ordinary-waiting",
            state="WAITING",
            blocking_reason="等待支付渠道回调",
            action_owner_kind="external",
            action_owner="支付渠道",
            action_text="重发 state=2 回调",
        )

        payload = daily_report_payload(
            self.service, datetime.now(timezone.utc).date()
        )
        by_id = {row["id"]: row for row in payload["rows"]}

        self.assertEqual("PAUSED", by_id[paused["id"]]["state"])
        self.assertEqual("已暂停", by_id[paused["id"]]["state_label"])
        self.assertEqual("WAITING", by_id[paused["id"]]["source_state"])
        self.assertEqual(
            "收到明确恢复指令后再重新分发",
            by_id[paused["id"]]["next_step"],
        )
        self.assertEqual("WAITING", by_id[waiting["id"]]["state"])
        self.assertEqual(1, payload["summary"]["counts"]["PAUSED"])
        self.assertEqual(1, payload["summary"]["counts"]["WAITING"])
        self.assertIn("已暂停：1", payload["markdown"])
        self.assertIn("| 用户暂停的事项 |", payload["markdown"])
        self.assertIn("| 已暂停 |", payload["markdown"])

    def test_plan_only_is_not_reported_as_dispatchable_inbox(self) -> None:
        task = self.service.create_task(
            "通话方案评审",
            idempotency_key="report-plan-only",
            state="PLANNED",
            scope_summary="Plan-only：仅方案评审，不进入 Coding/dispatch",
        )

        payload = daily_report_payload(
            self.service, datetime.now(timezone.utc).date()
        )
        selected = next(row for row in payload["rows"] if row["id"] == task["id"])

        self.assertEqual("PLAN_ONLY", selected["state"])
        self.assertEqual("仅规划", selected["state_label"])
        self.assertEqual(1, payload["summary"]["counts"]["PLAN_ONLY"])
        self.assertEqual(0, payload["summary"]["counts"]["INBOX"])

    def test_rolling_window_keeps_previous_calendar_day_done_task(self) -> None:
        now = datetime(2026, 7, 27, 17, 30, tzinfo=timezone.utc)
        recent_done = self.service.create_task(
            "北京时间昨天完成但仍在24小时内",
            idempotency_key="report-recent-done",
        )
        self.service.transition(recent_done["id"], "DONE", force=True)
        old_done = self.service.create_task(
            "超过24小时的完成项",
            idempotency_key="report-old-done",
        )
        self.service.transition(old_done["id"], "DONE", force=True)
        active = self.service.create_task(
            "跨午夜仍在执行",
            idempotency_key="report-active",
        )
        self.service.transition(active["id"], "RUNNING", force=True, progress=60)
        self.service.db.execute(
            "UPDATE tasks SET finished_at=?, updated_at=? WHERE id=?",
            (
                (now - timedelta(hours=23, minutes=30)).isoformat(
                    timespec="seconds"
                ),
                (now - timedelta(hours=23, minutes=30)).isoformat(
                    timespec="seconds"
                ),
                recent_done["id"],
            ),
        )
        self.service.db.execute(
            "UPDATE tasks SET finished_at=?, updated_at=? WHERE id=?",
            (
                (now - timedelta(hours=24, minutes=1)).isoformat(
                    timespec="seconds"
                ),
                (now - timedelta(hours=24, minutes=1)).isoformat(
                    timespec="seconds"
                ),
                old_done["id"],
            ),
        )

        payload = daily_report_payload(self.service, now=now)
        ids = {row["id"] for row in payload["rows"]}

        self.assertIn(recent_done["id"], ids)
        self.assertIn(active["id"], ids)
        self.assertNotIn(old_done["id"], ids)
        self.assertEqual(1, payload["summary"]["done"])
        self.assertEqual(
            (now - timedelta(hours=24)).isoformat(timespec="seconds"),
            payload["window_started_at"],
        )
        dashboard = self.service.dashboard_payload(now=now)
        self.assertEqual(1, dashboard["rolling_24h_summary"]["done"])
        self.assertEqual(80, dashboard["rolling_24h_summary"]["average_progress"])

    def test_static_report_ui_uses_safe_dom_rendering(self) -> None:
        static = Path(__file__).parents[2] / "qingtian_engine" / "static"
        app = (static / "app.js").read_text(encoding="utf-8")
        page = (static / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", app)
        self.assertNotIn('id="reportBody"', page)
        self.assertIn("擎天过去24小时日报", page)
        self.assertIn("过去24小时平均阶段指数（非完成率）", app)
        self.assertIn("复制 Markdown", page)
        self.assertIn("查看原始 Markdown", page)
        self.assertIn('id="reportFilterStatus"', page)
        self.assertIn('class="report-filter-status"', page)
        self.assertIn("setReportFilter", app)
        self.assertIn("reportCopyMarkdown", app)
        self.assertIn('["PAUSED", "已暂停", "paused"]', app)
        self.assertIn("row.state_label", app)
        self.assertIn("｜${stateLabel}", app)
        self.assertIn("aria-pressed", app)
        self.assertIn("slice(0, 10)", app)
        self.assertIn("｜${shortSummary}｜阶段指数 ${progress}%（非完成率）", app)
        self.assertIn('"report-stat average"', app)
        self.assertNotIn('"report-stat progress"', app)


if __name__ == "__main__":
    unittest.main()
