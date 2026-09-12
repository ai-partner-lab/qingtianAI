from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"qingtian_{name}_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class InstallationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "install"
        self.bin_dir = self.root / "bin"
        self.uninstaller = load_script("uninstall")

    def fixture(self):
        executable = self.prefix / "venv" / "bin" / "qingtian"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture", encoding="utf-8")
        self.bin_dir.mkdir()
        link = self.bin_dir / "qingtian"
        link.symlink_to(executable)
        receipt = {
            "schema_version": 1, "product": "qingtian-ai",
            "prefix": str(self.prefix.resolve()), "bin_dir": str(self.bin_dir.resolve()),
            "links": [str(link)], "version": "fixture",
        }
        (self.prefix / self.uninstaller.MARKER).write_text(json.dumps(receipt), encoding="utf-8")
        return link

    def test_uninstall_removes_only_receipted_prefix_and_link(self):
        link = self.fixture()
        private_data = self.root / "data"
        private_data.mkdir()
        (private_data / "keep.txt").write_text("keep", encoding="utf-8")
        with patch("subprocess.run"):
            result = self.uninstaller.uninstall(
                self.prefix, self.bin_dir, data_dir=private_data, purge_data=False
            )
        self.assertFalse(self.prefix.exists())
        self.assertFalse(link.exists())
        self.assertTrue((private_data / "keep.txt").is_file())
        self.assertTrue(result["data_preserved"])

    def test_uninstall_refuses_unreceipted_or_broad_prefix(self):
        with self.assertRaisesRegex(RuntimeError, "verified"):
            self.uninstaller.uninstall(self.prefix, self.bin_dir)
        with patch.object(Path, "home", return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                self.uninstaller.require_install(self.root)

    def test_uninstall_refuses_a_different_bin_directory(self):
        self.fixture()
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            self.uninstaller.uninstall(self.prefix, self.root / "other-bin")
        self.assertTrue(self.prefix.is_dir())

    def test_purge_rejects_unknown_data_directory(self):
        self.fixture()
        unknown = self.root / "unknown"
        unknown.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unverified data"):
            self.uninstaller.uninstall(
                self.prefix, self.bin_dir, data_dir=unknown, purge_data=True
            )
        self.assertTrue(self.prefix.is_dir())


if __name__ == "__main__":
    unittest.main()
