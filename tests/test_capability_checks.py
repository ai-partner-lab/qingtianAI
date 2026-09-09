from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qingtian_core.capability_checks import CapabilityBusy, CapabilityRunner, EXPECTED_CHECK_IDS, SETUP_COMMANDS, _receipt, _valid_receipt, run_capability
from qingtian_core.cli import main, parser


class FixedCapabilityTest(unittest.TestCase):
    def test_api_check_uses_fresh_http_tasks_and_real_completion(self) -> None:
        first = run_capability("api-e2e")
        second = run_capability("api-e2e")
        for result in (first, second):
            self.assertTrue(_valid_receipt(result, "api-e2e"))
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["scope"], "synthetic-local-demo")
            self.assertEqual(len(result["task_ids"]), 1)
            self.assertTrue(all(check["status"] == "passed" for check in result["checks"]))
            ids = {check["id"] for check in result["checks"]}
            self.assertTrue({"api.unknown", "api.reconcile", "api.done", "api.contracts", "api.stale_step"} <= ids)
            self.assertEqual(ids, EXPECTED_CHECK_IDS["api-e2e"])
        self.assertNotEqual(first["task_ids"], second["task_ids"])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_success_requires_every_fixed_assertion_once(self) -> None:
        self.assertEqual(len(EXPECTED_CHECK_IDS["api-e2e"]), 16)
        self.assertEqual(len(EXPECTED_CHECK_IDS["browser-e2e"]), 38)
        for viewport in ("desktop", "mobile"):
            for check in ("leaders", "tree", "readonly"):
                self.assertIn(f"browser.{viewport}.mindmap.{check}", EXPECTED_CHECK_IDS["browser-e2e"])
        for ids in ([], ["api.fresh_task"], [*EXPECTED_CHECK_IDS["api-e2e"], "api.fresh_task"]):
            def incomplete_check(receipt):
                receipt["checks"] = [{"id": check_id, "status": "passed", "detail": "Synthetic assertion."} for check_id in ids]

            with self.subTest(ids=ids), patch("qingtian_core.capability_checks._api_check", side_effect=incomplete_check):
                receipt = run_capability("api-e2e")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["reason_code"], "check_failed")
            self.assertTrue(any(check["status"] == "failed" for check in receipt["checks"]))

    def test_unknown_capabilities_are_rejected_before_execution(self) -> None:
        for value in (None, "", "shell", "http://example.invalid", {"id": "api-e2e"}):
            with self.assertRaises(ValueError):
                run_capability(value)

    def test_receipt_status_must_agree_with_nonempty_check_results(self) -> None:
        for status, checks, expected in (
            ("passed", [], False), ("passed", ["passed"], True),
            ("passed", ["passed", "failed"], False),
            ("passed", ["blocked"], False),
            ("blocked", ["passed"], False), ("blocked", ["blocked"], True),
            ("blocked", ["passed", "blocked"], True),
            ("blocked", ["failed", "blocked"], False),
            ("failed", [], False), ("failed", ["passed"], False),
            ("failed", ["failed"], True),
            ("failed", ["passed", "failed"], True),
        ):
            with self.subTest(status=status, checks=checks):
                receipt = _receipt("api-e2e")
                receipt["status"] = status
                receipt["checks"] = [{"id": "test", "status": check, "detail": "Synthetic assertion."} for check in checks]
                self.assertEqual(_valid_receipt(receipt, "api-e2e"), expected)

    def test_missing_playwright_is_blocked_without_installing_or_creating_task(self) -> None:
        with patch("qingtian_core.capability_checks._load_playwright", side_effect=ModuleNotFoundError), patch("qingtian_core.capability_checks._synthetic_server") as server:
            result = run_capability("browser-e2e")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "playwright_missing")
        self.assertEqual(result["setup_commands"], SETUP_COMMANDS)
        self.assertEqual(result["task_ids"], [])
        server.assert_not_called()

    def test_missing_browsers_are_blocked_and_playwright_stops(self) -> None:
        calls = []
        stopped = []

        def cannot_launch(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("synthetic unavailable browser path")

        runtime = SimpleNamespace(chromium=SimpleNamespace(launch=cannot_launch), stop=lambda: stopped.append(True))
        module = SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: runtime))
        with patch("qingtian_core.capability_checks._load_playwright", return_value=module):
            result = run_capability("browser-e2e")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "browser_unavailable")
        self.assertEqual([call.get("channel") for call in calls], [None, "chrome"])
        self.assertEqual(stopped, [True])
        self.assertNotIn("synthetic unavailable", json.dumps(result))

    def test_exception_receipt_is_sanitized_and_private_server_cleans_up(self) -> None:
        from qingtian_core.demo_web import GuidedDemo

        parents = []
        original = GuidedDemo.reset

        def reset(demo):
            result = original(demo)
            parents.append(demo.database_path.parent)
            return result

        with patch.object(GuidedDemo, "reset", reset), patch("qingtian_core.capability_checks._request", side_effect=RuntimeError("synthetic-private-detail")):
            result = run_capability("api-e2e")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason_code"], "capability_execution_error")
        self.assertTrue(parents)
        self.assertTrue(all(not path.exists() for path in parents))
        self.assertNotIn("synthetic-private-detail", json.dumps(result))
        self.assertTrue(all(str(path) not in json.dumps(result) for path in parents))

    def test_cli_dispatch_default_and_result_exit_status(self) -> None:
        self.assertEqual(parser().parse_args(["demo-check"]).capability, "browser-e2e")
        for status, exit_code in (("passed", 0), ("failed", 1), ("blocked", 2)):
            value = _receipt("api-e2e")
            value["status"] = status
            output = io.StringIO()
            with patch.object(CapabilityRunner, "run", return_value=value) as run, patch.object(CapabilityRunner, "close") as close, redirect_stdout(output):
                self.assertEqual(main(["demo-check", "--capability", "api-e2e"]), exit_code)
            run.assert_called_once_with("api-e2e")
            close.assert_called_once()
            self.assertEqual(json.loads(output.getvalue())["status"], status)


class IsolatedRunnerTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Dedicated process-group cleanup is POSIX-specific")
    def test_owned_grandchildren_ignoring_sigterm_are_stopped_after_leader_exit(self) -> None:
        original_popen = subprocess.Popen
        for parent_exits in (False, True):
            with self.subTest(parent_exits=parent_exits):
                parents = []
                grandchildren = []
                child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
                parent_code = (
                    "import subprocess,sys,time; "
                    f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE,text=True); "
                    "child.stdout.readline(); print(child.pid,file=sys.stderr,flush=True); "
                    + ("sys.exit(0)" if parent_exits else "time.sleep(30)")
                )

                def process_tree(_command, **kwargs):
                    kwargs["stderr"] = subprocess.PIPE
                    parent = original_popen([sys.executable, "-c", parent_code], **kwargs)
                    parents.append(parent)
                    grandchildren.append(int(parent.stderr.readline()))
                    parent.stderr.close()
                    return parent

                runner = CapabilityRunner()
                try:
                    with patch("qingtian_core.capability_checks.subprocess.Popen", side_effect=process_tree), patch("qingtian_core.capability_checks.PROCESS_TIMEOUT_SECONDS", 0.1):
                        result = runner.run("api-e2e")
                    self.assertEqual(result["status"], "failed")
                    self.assertTrue(all(parent.poll() is not None for parent in parents))
                    self.assertEqual(runner._processes, set())
                    for pid in grandchildren:
                        deadline = monotonic() + 3
                        while monotonic() < deadline:
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                break
                            # An orphan zombie is already stopped; only its OS
                            # parent can reap it. Never claim wait() reaped it.
                            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
                            if not state or state.startswith("Z"):
                                break
                            sleep(0.02)
                        else:
                            self.fail("owned SIGTERM-ignoring grandchild remained running")
                finally:
                    runner.close()
                    for pid in grandchildren:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_real_subprocess_succeeds_without_writing_to_callers_directory(self) -> None:
        runner = CapabilityRunner()
        try:
            result = runner.run("api-e2e")
            self.assertEqual(result["status"], "passed")
            self.assertTrue(_valid_receipt(result, "api-e2e"))
            self.assertEqual(runner._processes, set())
        finally:
            runner.close()

    def test_busy_and_close_terminate_owned_child_before_temp_cleanup(self) -> None:
        runner = CapabilityRunner()
        spawned = Event()
        children = []
        directories = []
        original_popen = subprocess.Popen

        def slow_child(command, **kwargs):
            self.assertEqual(command[-2:], ["--capability", "api-e2e"])
            self.assertEqual(kwargs["env"]["TMPDIR"], kwargs["cwd"])
            child = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            directories.append(Path(kwargs["cwd"]))
            spawned.set()
            return child

        results = []
        with patch("qingtian_core.capability_checks.subprocess.Popen", side_effect=slow_child):
            thread = Thread(target=lambda: results.append(runner.run("api-e2e")))
            thread.start()
            self.assertTrue(spawned.wait(3))
            with self.assertRaises(CapabilityBusy):
                runner.run("api-e2e")
            started = monotonic()
            runner.close()
            thread.join(5)
            self.assertLess(monotonic() - started, 5)
            self.assertFalse(thread.is_alive())
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertEqual(results[0]["status"], "failed")
        with self.assertRaises(CapabilityBusy):
            runner.run("api-e2e")

    def test_timeout_is_failed_and_owned_process_is_reaped(self) -> None:
        original_popen = subprocess.Popen
        children = []

        def slow_child(_command, **kwargs):
            child = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child

        runner = CapabilityRunner()
        try:
            with patch("qingtian_core.capability_checks.subprocess.Popen", side_effect=slow_child), patch("qingtian_core.capability_checks.PROCESS_TIMEOUT_SECONDS", 0.05):
                result = runner.run("api-e2e")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason_code"], "capability_timeout")
            self.assertGreater(result["duration_ms"], 0)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertEqual(runner._processes, set())
        finally:
            runner.close()
