from __future__ import annotations

import unittest

from scripts.live_selfcheck import selected_execution_matches


class LiveSelfcheckPolicyTest(unittest.TestCase):
    def test_observed_session_must_match_persisted_allowed_tuple(self):
        for model, reasoning in (
            ("gpt-6-astra", "high"),
            ("gpt-5.6-sol", "medium"),
            ("gpt-5.6-sol", "high"),
        ):
            run = {
                "session_id": "1" * 36,
                "model": model,
                "reasoning": reasoning,
                "speed": "standard",
            }
            actual = {
                "session_id": run["session_id"],
                "model": model,
                "reasoning": reasoning,
            }
            self.assertTrue(selected_execution_matches(run, actual))
            self.assertFalse(
                selected_execution_matches(run, {**actual, "reasoning": "xhigh"})
            )

    def test_below_floor_or_unsupported_values_never_count_as_verified(self):
        for model, reasoning in (
            ("gpt-5.5", "high"),
            ("gpt-5.6-terra", "high"),
            ("gpt-5.3-codex-spark", "high"),
            ("gpt-5.6-sol", "low"),
            ("gpt-6-astra", "max"),
        ):
            value = {
                "session_id": "2" * 36,
                "model": model,
                "reasoning": reasoning,
                "speed": "standard",
            }
            self.assertFalse(selected_execution_matches(value, value))

    def test_invalid_persisted_speed_is_not_verified(self):
        value = {
            "session_id": "3" * 36,
            "model": "gpt-5.6-sol",
            "reasoning": "high",
            "speed": "turbo",
        }
        self.assertFalse(selected_execution_matches(value, value))


if __name__ == "__main__":
    unittest.main()
