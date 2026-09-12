from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from qingtian_engine.entrypoint import main
from qingtian_engine.selftest import run_selftest


class EngineEntryPointTests(unittest.TestCase):
    def test_no_model_selftest(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no subprocess in selftest")):
            result = run_selftest()
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["model_called"])
        self.assertFalse(result["business_acceptance"])
        self.assertEqual(result["legacy_tasks_imported"], 0)
        self.assertEqual(len(result["checks"]), 11)
        self.assertEqual(
            {
                "role-policy-default-manager",
                "role-policy-default-executor",
                "role-policy-default-planner",
            },
            {item["id"] for item in result["checks"] if item["id"].startswith("role-policy-")},
        )

    def test_command_name_inside_title_does_not_hijack_parser(self):
        args = ["task", "add", "--title", "tour"]
        with patch("qingtian_engine.cli.main", return_value=7) as engine:
            self.assertEqual(main(args), 7)
            engine.assert_called_once_with(args)

    def test_knowledge_setup_explicit_private_and_no_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, kb = root / "engine", root / "kb"
            kb.mkdir()
            (kb / ".qingtian-knowledge-root").write_text("fixture", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("subprocess.Popen", side_effect=AssertionError("no query")):
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(["--data-dir", str(data), "knowledge", "status"]), 0)
                self.assertFalse(json.loads(output.getvalue())["enabled"])
                self.assertFalse(data.exists())
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(["knowledge", "--data-dir", str(data), "configure", "--root", str(kb)]), 0)
                config = data / "config" / "knowledge.local.json"
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                self.assertEqual(json.loads(config.read_text())["provider"], "builtin-module")
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(["knowledge", "--data-dir", str(data), "disable"]), 0)
                self.assertEqual(json.loads(config.read_text()), {"schema_version": 1, "enabled": False})
                self.assertTrue(kb.is_dir())
                self.assertFalse((data / "control-plane.sqlite3").exists())

    def test_quickstart_rejects_bad_port_before_initialization(self):
        with patch("qingtian_engine.cli.build_service", side_effect=AssertionError("no initialization")):
            with self.assertRaises(SystemExit) as error:
                main(["quickstart", "--port", "0"])
            self.assertEqual(error.exception.code, 2)
