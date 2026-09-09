from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from qingtian_engine.config import load_policy, runtime_policy
from qingtian_engine.router import route_task
from qingtian_engine.worker_entry import build_codex_command


class RouterAndPolicyTest(unittest.TestCase):
    def test_routes_workers(self) -> None:
        self.assertEqual("infra", route_task("Deploy the container").worker_type)
        self.assertEqual("browser", route_task("Playwright visual regression").worker_type)
        self.assertEqual("qa", route_task("Run the regression tests").worker_type)
        self.assertEqual("backend", route_task("Implement the API migration").owner_session)
        self.assertEqual("mobile", route_task("Expo Android crash").owner_session)
        self.assertEqual("frontend", route_task("Responsive web component").owner_session)
        self.assertEqual(
            "release-team",
            route_task("Deploy", "role:release-team").owner_session,
        )
        self.assertEqual("coordinator", route_task("Unclassified task").owner_session)

    def test_policy_forces_model_reasoning_and_speed_window(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        daytime = runtime_policy("low", datetime(2026, 7, 27, 9, tzinfo=tz))
        nighttime = runtime_policy("xhigh", datetime(2026, 7, 27, 21, tzinfo=tz))
        self.assertEqual("gpt-5.6-sol", daytime.model)
        self.assertEqual("high", daytime.reasoning)
        self.assertTrue(daytime.enable_fast_mode)
        self.assertEqual("xhigh", nighttime.reasoning)
        self.assertFalse(nighttime.enable_fast_mode)

    def test_codex_command_uses_json_stdin_and_no_dangerous_bypass(self) -> None:
        task = {
            "reasoning": "high",
            "model": "gpt-5.6-sol",
            "worktree": "/tmp/worktree",
            "repository": "",
        }
        command = build_codex_command(task)
        joined = " ".join(command)
        self.assertIn("codex exec", joined)
        self.assertIn("--json", command)
        self.assertIn("gpt-5.6-sol", command)
        self.assertEqual("-", command[-1])
        self.assertNotIn("dangerously-bypass", joined)

if __name__ == "__main__":
    unittest.main()
