from __future__ import annotations

import io
import os
import socket
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from qingtian_engine import cli
from qingtian_engine.runtime import (
    InstanceLock,
    active_instance_pid,
    cleanup_runtime_files_if_idle,
    publish_server_pid,
    read_pid,
    remove_server_pid,
)


class RuntimeFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name) / "run"
        self.run_dir.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_lock_identifies_only_the_current_owner_and_clears_on_release(self) -> None:
        lock_path = self.run_dir / "instance.lock"
        lock = InstanceLock(lock_path)
        lock.acquire()
        self.assertEqual(os.getpid(), active_instance_pid(self.run_dir))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            InstanceLock(lock_path).acquire()

        lock.release()
        self.assertIsNone(active_instance_pid(self.run_dir))
        self.assertEqual("", lock_path.read_text(encoding="utf-8"))

    def test_pid_publication_and_idle_cleanup_are_conditional(self) -> None:
        publish_server_pid(self.run_dir, 12345)
        self.assertEqual(12345, read_pid(self.run_dir / "server.pid"))

        remove_server_pid(self.run_dir, expected_pid=54321)
        self.assertEqual(12345, read_pid(self.run_dir / "server.pid"))
        remove_server_pid(self.run_dir, expected_pid=12345)
        self.assertFalse((self.run_dir / "server.pid").exists())

        publish_server_pid(self.run_dir, 999999)
        (self.run_dir / "instance.lock").write_text("999999", encoding="utf-8")
        self.assertTrue(cleanup_runtime_files_if_idle(self.run_dir))
        self.assertFalse((self.run_dir / "server.pid").exists())
        self.assertEqual(
            "", (self.run_dir / "instance.lock").read_text(encoding="utf-8")
        )


class ServerLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.environment = mock.patch.dict(os.environ, {
            "QINGTIAN_ENGINE_HOME": str(self.data_dir),
            "QINGTIAN_KNOWLEDGE_CONFIG": str(Path(self.temp.name) / "no-knowledge.json"),
            "QINGTIAN_PROJECTS_CONFIG": str(Path(self.temp.name) / "no-projects.json"),
            "QINGTIAN_INTAKE_PLANNER": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.children = []
        real_popen = subprocess.Popen

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.children.append(process)
            return process

        self.popen_patch = mock.patch.object(
            cli.subprocess, "Popen", side_effect=tracked_popen
        )
        self.popen_patch.start()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])

    def tearDown(self) -> None:
        if active_instance_pid(self.data_dir / "run") is not None:
            cli.stop_server(self.data_dir)
        for process in self.children:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)
        self.popen_patch.stop()
        self.temp.cleanup()

    def test_concurrent_start_is_idempotent_when_http_probe_is_unavailable(self) -> None:
        with mock.patch.object(cli, "_health_payload", return_value=None):
            with redirect_stdout(io.StringIO()):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(
                        pool.map(
                            lambda _index: cli.start_server(
                                self.data_dir, self.port, False, False
                            ),
                            range(2),
                        )
                    )

        self.assertEqual([0, 0], results)
        active_pid = active_instance_pid(self.data_dir / "run")
        self.assertIsNotNone(active_pid)
        self.assertEqual(
            active_pid, read_pid(self.data_dir / "run" / "server.pid")
        )

        with mock.patch.object(cli, "_health_payload", return_value=None):
            with mock.patch.object(cli.subprocess, "Popen") as popen:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        0,
                        cli.start_server(self.data_dir, self.port, False, False),
                    )
                popen.assert_not_called()

            output = io.StringIO()
            with redirect_stdout(output):
                status = cli.main(
                    [
                        "--data-dir",
                        str(self.data_dir),
                        "status",
                        "--port",
                        str(self.port),
                    ]
                )
        self.assertEqual(0, status)
        self.assertEqual("running", output.getvalue().strip())

    def test_stop_uses_lock_owner_not_an_incorrect_pid_file(self) -> None:
        with mock.patch.object(cli, "_health_payload", return_value=None):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0, cli.start_server(self.data_dir, self.port, False, False)
                )
        active_pid = active_instance_pid(self.data_dir / "run")
        self.assertIsNotNone(active_pid)
        (self.data_dir / "run" / "server.pid").write_text(
            "999999", encoding="utf-8"
        )

        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.stop_server(self.data_dir))

        self.assertIsNone(active_instance_pid(self.data_dir / "run"))
        self.assertFalse((self.data_dir / "run" / "server.pid").exists())
        self.assertEqual(
            "",
            (self.data_dir / "run" / "instance.lock").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
