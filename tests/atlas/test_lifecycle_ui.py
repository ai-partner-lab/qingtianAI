"""CI entrypoint for the offline lifecycle JavaScript/DOM contract tests."""
from pathlib import Path
import shutil
import subprocess
import unittest


SCRIPT = Path(__file__).with_name("test_lifecycle_ui.mjs")


class LifecycleUiNodeTests(unittest.TestCase):
    def test_offline_lifecycle_dom_contract(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required for offline lifecycle DOM checks")
        result = subprocess.run(
            [node, "--test", str(SCRIPT)],
            cwd=SCRIPT.parents[2], capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("# pass 24", result.stdout)
        self.assertIn("# fail 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
