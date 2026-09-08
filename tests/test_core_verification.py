from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from qingtian_core.contracts import ContractValidationError
from qingtian_core.verification import run_checks


class VerificationTest(unittest.TestCase):
    def test_side_effect_free_check_produces_hash_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-verification-test-") as temp_name:
            root = Path(temp_name)
            adapter = root / "adapter.json"
            adapter.write_text(
                json.dumps(
                    {
                        "project_id": "synthetic",
                        "schema_version": 1,
                        "project_root": ".",
                        "repositories": [],
                        "checks": [
                            {
                                "id": "python-ok",
                                "category": "contract",
                                "profiles": ["smoke"],
                                "command": [sys.executable, "-c", "print('ok')"],
                                "cwd": ".",
                                "timeout_seconds": 30,
                                "side_effect": "none",
                                "network": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            receipt = run_checks(adapter, execute_trusted_adapter=True)
            self.assertEqual(receipt["result"], "passed")
            self.assertNotIn("output_tail", receipt["checks"][0])
            self.assertEqual(receipt["checks"][0]["output_bytes"], 3)
            self.assertEqual(len(receipt["checks"][0]["output_sha256"]), 64)
            self.assertEqual(len(receipt["receipt_hash"]), 64)

    def test_external_check_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-verification-test-") as temp_name:
            root = Path(temp_name)
            adapter = root / "adapter.json"
            adapter.write_text(
                json.dumps(
                    {
                        "project_id": "synthetic",
                        "schema_version": 1,
                        "project_root": ".",
                        "repositories": [],
                        "checks": [
                            {
                                "id": "deploy",
                                "category": "release",
                                "profiles": ["smoke"],
                                "command": [sys.executable, "-c", "raise SystemExit(0)"],
                                "cwd": ".",
                                "timeout_seconds": 30,
                                "side_effect": "external",
                                "network": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "never run"):
                run_checks(adapter, execute_trusted_adapter=True)

    def test_invalid_side_effect_fails_contract_before_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-verification-test-") as temp_name:
            root = Path(temp_name)
            adapter = root / "adapter.json"
            marker = root / "must-not-exist"
            adapter.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": "synthetic",
                        "project_root": ".",
                        "repositories": [],
                        "checks": [
                            {
                                "id": "invalid",
                                "category": "contract",
                                "profiles": ["smoke"],
                                "command": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; Path('must-not-exist').write_text('x')",
                                ],
                                "cwd": ".",
                                "timeout_seconds": 30,
                                "side_effect": "external-typo",
                                "network": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ContractValidationError):
                run_checks(adapter, execute_trusted_adapter=True)
            self.assertFalse(marker.exists())

    def test_missing_command_returns_a_structured_failed_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-verification-test-") as temp_name:
            root = Path(temp_name)
            adapter = root / "adapter.json"
            adapter.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": "synthetic",
                        "project_root": ".",
                        "repositories": [],
                        "checks": [
                            {
                                "id": "missing",
                                "category": "contract",
                                "profiles": ["smoke"],
                                "command": ["qingtian-command-that-does-not-exist"],
                                "cwd": ".",
                                "timeout_seconds": 30,
                                "side_effect": "none",
                                "network": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            receipt = run_checks(adapter, execute_trusted_adapter=True)
            self.assertEqual(receipt["result"], "failed")
            self.assertEqual(receipt["checks"][0]["exit_code"], 127)
            self.assertEqual(receipt["checks"][0]["failure_kind"], "execution_error")


if __name__ == "__main__":
    unittest.main()
