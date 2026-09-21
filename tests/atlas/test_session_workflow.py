import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qingtian_engine import cli
from qingtian_engine.session_workflow import (
    configured_executable_present,
    session_workflow_profile,
)


class SessionWorkflowProfileTests(unittest.TestCase):
    def test_codex_presence_uses_default_path_when_override_is_absent(self):
        calls = []
        with patch.dict(os.environ, {}, clear=True):
            present = configured_executable_present(
                "codex", "QINGTIAN_CODEX_BIN",
                resolver=lambda name: calls.append(name) or "/tools/codex",
            )
        self.assertTrue(present)
        self.assertEqual(calls, ["codex"])

    def test_codex_presence_accepts_custom_absolute_executable_without_path_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "custom codex"
            executable.write_text("fixture", encoding="utf-8")
            executable.chmod(0o700)
            with patch.dict(os.environ, {"QINGTIAN_CODEX_BIN": str(executable)}):
                present = configured_executable_present(
                    "codex", "QINGTIAN_CODEX_BIN",
                    resolver=lambda name: self.fail("absolute override must not search PATH"),
                )
        self.assertTrue(present)

    def test_codex_presence_accepts_custom_path_name(self):
        calls = []
        with patch.dict(os.environ, {"QINGTIAN_CODEX_BIN": "custom-codex"}):
            present = configured_executable_present(
                "codex", "QINGTIAN_CODEX_BIN",
                resolver=lambda name: calls.append(name) or "/tools/custom-codex",
            )
        self.assertTrue(present)
        self.assertEqual(calls, ["custom-codex"])

    def test_invalid_explicit_codex_does_not_fall_back_to_default_path(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"QINGTIAN_CODEX_BIN": str(Path(folder) / "missing")}
        ):
            present = configured_executable_present(
                "codex", "QINGTIAN_CODEX_BIN",
                resolver=lambda name: self.fail("invalid override must not fall back"),
            )
        self.assertFalse(present)

    def test_empty_explicit_codex_is_invalid_and_does_not_fall_back(self):
        with patch.dict(os.environ, {"QINGTIAN_CODEX_BIN": ""}):
            present = configured_executable_present(
                "codex", "QINGTIAN_CODEX_BIN",
                resolver=lambda name: self.fail("empty override must not fall back"),
            )
        self.assertFalse(present)

    def test_profile_is_read_only_and_reports_missing_host_capabilities(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = session_workflow_profile(
                data_dir=root / "state", workspace=root / "missing-workspace",
                codex_present=False, git_present=False,
            )
        self.assertTrue(result["read_only"])
        self.assertEqual(result["default_mode"], "manual")
        self.assertFalse(result["notification_bridge"]["native_delivery"])
        self.assertFalse(result["native_delivery_ready"])
        self.assertIn("codex_cli_missing", result["blockers"])
        self.assertFalse(result["portable_configuration"]["values_included"])

    def test_manual_recording_does_not_imply_delivery_or_role_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = session_workflow_profile(
                data_dir=root / "state", workspace=root,
                codex_present=True, git_present=True,
            )
        self.assertTrue(result["manual_recording_ready"])
        self.assertFalse(result["native_delivery_ready"])
        self.assertFalse(result["capabilities"]["manager_entry"]["workflow_ready"])
        self.assertIn("handoff_accepted", result["separate_facts"])
        self.assertIn("execution_started", result["separate_facts"])

    def test_cli_status_is_read_only(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"QINGTIAN_CODEX_BIN": ""}
        ), patch(
            "qingtian_engine.cli._which", return_value=None
        ), patch("sys.stdout", new_callable=io.StringIO) as output:
            rc = cli.main([
                "--data-dir", str(Path(folder) / "state"), "--workspace", folder,
                "session-workflow", "status",
            ])
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["read_only"])
        self.assertFalse(result["host"]["codex_cli_present"])

    def test_doctor_and_status_share_absolute_codex_override_without_disclosing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "private-codex"
            executable.write_text("fixture", encoding="utf-8")
            executable.chmod(0o700)
            environment = {"QINGTIAN_CODEX_BIN": str(executable)}
            with patch.dict(os.environ, environment), patch(
                "qingtian_engine.cli._which",
                side_effect=lambda name: "/usr/bin/git" if name == "git" else None,
            ), patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(["--workspace", folder, "doctor"]), 0)
                doctor = json.loads(output.getvalue())
                output.seek(0)
                output.truncate()
                self.assertEqual(cli.main(["--workspace", folder, "session-workflow", "status"]), 0)
                status = json.loads(output.getvalue())
        self.assertTrue(doctor["optional_tools"]["codex"])
        self.assertTrue(status["host"]["codex_cli_present"])
        self.assertNotIn(str(executable), json.dumps(status))

    def test_cli_prepare_calls_identity_helper_without_delivery(self):
        prepared = {
            "schema_version": 1, "status": "prepared",
            "body": "【Manager｜Task A】\nReview this", "source": {}, "audit": {},
            "native_delivery": False,
        }
        with patch(
            "qingtian_engine.message_identity.prepare_message", return_value=prepared
        ) as helper, patch("sys.stdout", new_callable=io.StringIO) as output:
            rc = cli.main([
                "session-workflow", "prepare", "--body", "Review this",
                "--source-role", "Manager", "--source-session-name", "Task A",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "prepared")
        self.assertFalse(json.loads(output.getvalue())["native_delivery"])
        helper.assert_called_once()

    def test_cli_prepare_rejects_identity_conflict(self):
        with patch(
            "qingtian_engine.message_identity.prepare_message",
            side_effect=ValueError("conflicting source prefix"),
        ), patch("sys.stdout", new_callable=io.StringIO) as output:
            rc = cli.main(["session-workflow", "prepare", "--body", "bad"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "invalid-message")

    def test_body_file_accepts_maximum_prepared_envelope_idempotently(self):
        from qingtian_engine.message_identity import MAX_BODY, prepare_message
        prepared = prepare_message(
            "x" * MAX_BODY, source_role="Manager", source_session_name="Task A"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "message.txt"
            path.write_text(prepared["body"], encoding="utf-8")
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                rc = cli.main([
                    "session-workflow", "prepare", "--body-file", str(path),
                    "--source-role", "Manager", "--source-session-name", "Task A",
                ])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["body"], prepared["body"])

    def test_body_file_error_does_not_echo_private_path(self):
        private_path = "/private/account/workspace/missing-message.txt"
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            rc = cli.main([
                "session-workflow", "prepare", "--body-file", private_path,
            ])
        self.assertEqual(rc, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error"], "message body file is unavailable")
        self.assertNotIn(private_path, output.getvalue())


if __name__ == "__main__":
    unittest.main()
