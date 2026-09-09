from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qingtian_engine.db import Database
from qingtian_engine.service import ControlPlane, WAITING_CATEGORY_LABELS


class WaitingTaxonomyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = ControlPlane(
            Database(Path(self.temp.name) / "control.sqlite3")
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_waiting_categories_and_summary_share_one_projection(self) -> None:
        fixtures = (
            (
                "paused",
                "PAUSED_BY_USER: 用户明确暂停，不自动分发",
                "user",
                "收到恢复指令后再继续",
            ),
            ("external", "等待支付渠道回调", "external", "等待回调"),
            ("user", "等待配置", "user", "请补充 redirect URI"),
            (
                "internal_qa",
                "WAITING_INDEPENDENT_QA: 缺真实设备",
                "agent",
                "等待内部 QA",
            ),
            ("internal_release", "待部署到 dev", "agent", "准备发布"),
            ("dependency", "依赖上游任务完成", "agent", "等待依赖"),
            (
                "execution_recovery",
                "STALE_EXECUTION: worker lost",
                "external",
                "等待安全恢复",
            ),
        )
        task_ids = {}
        for index, (key, reason, owner_kind, action_text) in enumerate(fixtures):
            task = self.service.create_task(
                "等待分类 {}".format(key),
                idempotency_key="waiting-taxonomy-{}".format(index),
                state="WAITING",
                blocking_reason=reason,
                action_owner_kind=owner_kind,
                action_text=action_text,
            )
            task_ids[key] = task["id"]

        payload = self.service.dashboard_payload()
        by_id = {task["id"]: task for task in payload["tasks"]}
        for key, task_id in task_ids.items():
            self.assertEqual(key, by_id[task_id]["waiting_category"]["key"])
            self.assertEqual(
                WAITING_CATEGORY_LABELS[key],
                by_id[task_id]["waiting_category"]["label"],
            )
            self.assertEqual(
                0 if key == "paused" else 1,
                payload["waiting_summary"][key],
            )
        paused = by_id[task_ids["paused"]]
        self.assertEqual("WAITING", paused["state"])
        self.assertEqual("PAUSED", paused["display_state"])
        self.assertEqual("none", paused["human_action"]["owner_kind"])
        self.assertEqual(1, payload["paused_count"])
        self.assertEqual(1, len(payload["columns"]["PAUSED"]))
        self.assertNotIn(
            task_ids["paused"],
            {task["id"] for task in payload["columns"]["WAITING"]},
        )

    def test_unknown_wait_is_internal_not_external(self) -> None:
        task = self.service.create_task(
            "普通内部暂停",
            idempotency_key="waiting-taxonomy-internal",
            state="WAITING",
            blocking_reason="等待排期",
        )
        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        self.assertEqual("internal", selected["waiting_category"]["key"])
        self.assertEqual("内部等待", selected["waiting_category"]["label"])

    def test_canceled_paused_and_plan_only_are_honest_non_action_states(self) -> None:
        canceled = self.service.create_task(
            "已取消的旧事项",
            idempotency_key="honest-state-canceled",
            state="CANCELED",
            action_owner_kind="user",
            action_owner="项目负责人",
            action_text="旧动作不应继续展示",
        )
        paused = self.service.create_task(
            "用户暂停事项",
            idempotency_key="honest-state-paused",
            state="PAUSED",
            action_owner_kind="user",
            action_owner="项目负责人",
            action_text="恢复后再处理",
        )
        plan_only = self.service.create_task(
            "通话方案评审",
            idempotency_key="honest-state-plan-only",
            state="PLANNED",
            scope_summary="Plan-only：仅方案评审，不进入 Coding/dispatch",
            action_owner_kind="user",
            action_owner="项目负责人",
            action_text="Go/No-Go 后另拆实施任务",
        )

        payload = self.service.dashboard_payload()
        by_id = {task["id"]: task for task in payload["tasks"]}

        self.assertEqual("CANCELED", by_id[canceled["id"]]["display_state"])
        self.assertEqual("PAUSED", by_id[paused["id"]]["display_state"])
        self.assertEqual("PLAN_ONLY", by_id[plan_only["id"]]["display_state"])
        self.assertNotIn(
            canceled["id"],
            {task["id"] for task in payload["columns"]["WAITING"]},
        )
        self.assertEqual(0, payload["action_summary"]["user"])
        self.assertEqual(0, sum(payload["waiting_summary"].values()))
        self.assertEqual(1, payload["paused_count"])
        self.assertEqual(1, payload["plan_only_count"])
        self.assertEqual(1, payload["canceled_count"])
        self.assertEqual("none", by_id[paused["id"]]["human_action"]["owner_kind"])
        self.assertEqual(
            "none", by_id[plan_only["id"]]["human_action"]["owner_kind"]
        )

    def test_stale_delegated_exposes_non_automatic_recovery_entry(self) -> None:
        task = self.service.create_task(
            "失联委派执行",
            idempotency_key="honest-state-stale-delegated",
            state="WAITING",
            blocking_reason="STALE_EXECUTION:delegated heartbeat expired",
        )
        self.service.db.execute(
            "UPDATE tasks SET execution_mode='delegated' WHERE id=?",
            (task["id"],),
        )

        selected = next(
            item
            for item in self.service.dashboard_payload()["tasks"]
            if item["id"] == task["id"]
        )
        recovery = selected["runtime_status"]["recovery"]
        self.assertEqual("RECOVERY_REQUIRED", selected["runtime_status"]["code"])
        self.assertEqual("委派执行已失联", selected["runtime_status"]["label"])
        self.assertTrue(recovery["required"])
        self.assertFalse(recovery["automatic"])
        self.assertEqual("delegated", recovery["mode"])
        self.assertEqual(
            "/api/tasks/{}/heartbeat".format(task["id"]),
            recovery["heartbeat_endpoint"],
        )
        self.assertEqual("WAITING", self.service.get_task(task["id"])["state"])

    def test_dashboard_uses_waiting_taxonomy_in_all_ui_surfaces(self) -> None:
        app_js = (
            Path(__file__).parents[2] / "qingtian_engine/static/app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('["WAITING", "等待中"]', app_js)
        self.assertIn('["PAUSED", "已暂停"]', app_js)
        self.assertIn('["PLAN_ONLY", "仅规划"]', app_js)
        self.assertIn('["CANCELED", "已取消"]', app_js)
        self.assertIn("waitingBreakdown(data)", app_js)
        self.assertIn("waitingAndPausedBreakdown(data)", app_js)
        self.assertIn("taskDisplayState(task)", app_js)
        self.assertIn("task.waiting_category.label", app_js)
        self.assertIn("用户明确暂停 · 不参与调度", app_js)
        self.assertIn("恢复入口（不会自动运行）", app_js)
        self.assertNotIn('["WAITING", "等待外部"]', app_js)

    def test_dashboard_realtime_transport_coalesces_fallback_and_tracks_heartbeat(
        self,
    ) -> None:
        app_js = (
            Path(__file__).parents[2] / "qingtian_engine/static/app.js"
        ).read_text(encoding="utf-8")
        server_py = (
            Path(__file__).parents[2] / "qingtian_engine/server.py"
        ).read_text(encoding="utf-8")
        self.assertIn("if (refreshPromise) return refreshPromise", app_js)
        self.assertIn('addEventListener("heartbeat"', app_js)
        self.assertIn('self._write_sse("heartbeat"', server_py)
        self.assertIn("}, 30000)", app_js)


if __name__ == "__main__":
    unittest.main()
