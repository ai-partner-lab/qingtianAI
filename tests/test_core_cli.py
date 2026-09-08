from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from qingtian_core.cli import main


class CliTest(unittest.TestCase):
    def call(self, *args: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(list(args))
        return status, json.loads(output.getvalue())

    def test_doctor_and_demo(self) -> None:
        status, doctor = self.call("doctor")
        self.assertEqual(status, 0)
        self.assertEqual(doctor["status"], "ok")
        with tempfile.TemporaryDirectory(prefix="qingtian-cli-test-") as temp_name:
            database = str(Path(temp_name) / "demo.db")
            status, demo = self.call("demo", "--db", database)
            self.assertEqual(status, 0)
            self.assertEqual(demo["run"]["state"], "SUCCEEDED")
            self.assertTrue(demo["knowledge_search"])

    def test_verify_creates_the_receipt_parent_directory(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="qingtian-cli-test-") as temp_name:
            receipt = Path(temp_name) / "nested" / "receipt.json"
            status, result = self.call(
                "verify",
                "--adapter",
                str(root / "examples" / "project.adapter.json"),
                "--profile",
                "smoke",
                "--execute-trusted-adapter",
                "--receipt",
                str(receipt),
            )
            self.assertEqual(status, 0)
            self.assertEqual(result["result"], "passed")
            self.assertTrue(receipt.is_file())
            if os.name == "posix":
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)

    def test_legacy_database_error_is_structured(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-cli-test-") as temp_name:
            database = Path(temp_name) / "legacy.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value) VALUES('schema_version', '1');
                """
            )
            connection.close()
            status, result = self.call("init", "--db", str(database))
            self.assertEqual(status, 2)
            self.assertEqual(result["error"], "StorageContractError")


if __name__ == "__main__":
    unittest.main()
