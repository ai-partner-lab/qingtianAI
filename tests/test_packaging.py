from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from time import monotonic
from time import sleep
import unittest
from urllib.parse import urlsplit
import venv
import zipfile


@unittest.skipUnless(os.name == "posix", "secure runtime currently requires POSIX")
class InstalledWheelTestCase(unittest.TestCase):
    maxDiff = None

    def assert_installed_real_engine(self, executable, runtime, root, environment):
        """No editable checkout: serve the installed engine and real empty SQLite."""
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        data = root / "real-engine-state"
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [str(executable), "--data-dir", str(data), "--workspace", str(runtime),
                 "start", "--foreground", "--mode", "manual", "--port", str(port)],
                cwd=runtime, env=environment, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, start_new_session=True,
            )
            def request(path):
                connection = HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return response.read()
                finally:
                    connection.close()
            try:
                deadline = monotonic() + 15
                health = None
                while monotonic() < deadline and process.poll() is None:
                    try:
                        health = json.loads(request("/api/health"))
                        break
                    except (ConnectionError, OSError):
                        sleep(0.1)
                self.assertIsNotNone(health, "installed real engine failed to start")
                self.assertEqual(health["service"], "qingtian-engine")
                self.assertEqual(health["mode"], "manual")
                self.assertEqual(Path(health["data_dir"]), data.resolve())
                snapshot = json.loads(request("/api/dashboard"))
                self.assertEqual(snapshot["tasks"], [])
                self.assertIn(b'guideButton', request("/"))
                self.assertIn(b'guideSteps', request("/app.js"))
                self.assertIn(b'guide-progress', request("/styles.css"))
                self.assertEqual(list(runtime.iterdir()), [])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

    def assert_installed_demo_web(
        self, executable: Path, runtime: Path, root: Path, environment: dict[str, str]
    ) -> None:
        """Exercise the wheel's console entry point and bundled UI over HTTP."""
        demo_temporary_root = root / "demo-private-temporary"
        demo_temporary_root.mkdir(mode=0o700)
        demo_environment = {**environment, "TMPDIR": str(demo_temporary_root)}
        original_runtime_entries = sorted(path.name for path in runtime.iterdir())
        self.assertEqual(original_runtime_entries, [])
        with tempfile.TemporaryFile() as error_output:
            process = subprocess.Popen(
                [str(executable), "demo-web", "--port", "0", "--no-browser"],
                cwd=runtime,
                env=demo_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=error_output,
            )
            try:
                self.assertIsNotNone(process.stdout)
                observed = bytearray()
                url_match = None
                deadline = monotonic() + 15
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while monotonic() < deadline:
                        ready = selector.select(timeout=min(0.5, max(0, deadline - monotonic())))
                        if ready:
                            chunk = os.read(process.stdout.fileno(), 4096)
                            if not chunk:
                                break
                            observed.extend(chunk)
                            self.assertLess(len(observed), 16384, "unexpectedly large demo startup output")
                            url_match = re.search(
                                rb"^Qingtian AI offline guided demo: (http://127\.0\.0\.1:[0-9]+)/\r?$",
                                observed,
                                re.MULTILINE,
                            )
                            if url_match:
                                break
                        if process.poll() is not None:
                            break
                self.assertIsNotNone(
                    url_match,
                    f"installed demo did not print a loopback URL within 15 seconds: {observed.decode('utf-8', errors='replace')}",
                )
                address = urlsplit(url_match.group(1).decode("ascii"))
                self.assertEqual(address.hostname, "127.0.0.1")
                self.assertIsNotNone(address.port)
                self.assertGreater(address.port, 0)

                def request(
                    method: str, path: str, payload: dict[str, object] | None = None,
                    token: str | None = None,
                    *, timeout: float = 5,
                ) -> tuple[int, bytes, dict[str, str]]:
                    connection = HTTPConnection("127.0.0.1", address.port, timeout=timeout)
                    headers = {"Origin": f"http://127.0.0.1:{address.port}"}
                    body = None
                    if payload is not None:
                        body = json.dumps(payload).encode("utf-8")
                        headers["Content-Type"] = "application/json"
                    if token is not None:
                        headers["X-Qingtian-Demo-Token"] = token
                    try:
                        connection.request(method, path, body=body, headers=headers)
                        response = connection.getresponse()
                        return response.status, response.read(), dict(response.getheaders())
                    finally:
                        connection.close()

                for route, content_type in (
                    ("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css"),
                ):
                    status, body, headers = request("GET", route)
                    self.assertEqual(status, 200, f"installed resource failed: {route}")
                    self.assertGreater(len(body), 20, f"installed resource is empty: {route}")
                    self.assertTrue(headers["Content-Type"].startswith(content_type))
                status, body, _ = request("GET", "/api/state")
                self.assertEqual(status, 200)
                snapshot = json.loads(body)
                self.assertEqual(snapshot["mode"], "offline-guided")
                self.assertEqual(snapshot["step"], 0)
                self.assertEqual(snapshot["task"]["state"], "DRAFT")
                self.assertEqual(snapshot["total_steps"], 7)
                self.assertEqual(len(list(demo_temporary_root.iterdir())), 1)

                status, body, _ = request("GET", "/api/capabilities")
                self.assertEqual(status, 200)
                catalog = json.loads(body)
                self.assertEqual(catalog["schema_version"], 1)
                self.assertEqual(len(catalog["phases"]), 7)
                self.assertEqual([phase["step"] for phase in catalog["phases"]], list(range(1, 8)))
                capabilities = {item["id"]: item for item in catalog["capabilities"]}
                self.assertEqual(len(capabilities), len(catalog["capabilities"]))
                references = []
                for phase in catalog["phases"]:
                    for identifier in phase["capability_ids"]:
                        self.assertIn(identifier, capabilities)
                        self.assertEqual(capabilities[identifier]["phase_id"], phase["id"])
                        references.append(identifier)
                self.assertCountEqual(references, capabilities)
                self.assertEqual({item["id"] for item in capabilities.values() if item["runnable"]}, {"api-e2e", "browser-e2e"})

                def guide_identity(state: dict[str, object]) -> tuple[object, ...]:
                    return (
                        state["task"]["task_id"], state["task"]["revision"],
                        state["step"], state["events"],
                    )

                original_guide = guide_identity(snapshot)

                def assert_api_receipt(receipt: dict[str, object]) -> None:
                    self.assertEqual(receipt["schema_version"], 1)
                    self.assertRegex(receipt["run_id"], r"^capability_[0-9a-f]{32}$")
                    self.assertEqual(receipt["capability_id"], "api-e2e")
                    self.assertEqual(receipt["scope"], "synthetic-local-demo")
                    self.assertEqual(receipt["status"], "passed", receipt)
                    self.assertTrue(receipt["checks"])
                    self.assertTrue(all(check["status"] == "passed" for check in receipt["checks"]))
                    self.assertTrue(all(check["id"] and check["detail"] for check in receipt["checks"]))
                    self.assertGreaterEqual(receipt["duration_ms"], 0)
                    self.assertTrue(receipt["started_at"])
                    self.assertEqual(len(receipt["task_ids"]), 1)
                    self.assertNotEqual(receipt["task_ids"][0], original_guide[0])

                status, body, _ = request(
                    "POST", "/api/capabilities/run", {"capability_id": "api-e2e"},
                    snapshot["csrf_token"], timeout=65,
                )
                self.assertEqual(status, 200)
                http_receipt = json.loads(body)
                assert_api_receipt(http_receipt)
                status, body, _ = request("GET", "/api/state")
                self.assertEqual(status, 200)
                self.assertEqual(guide_identity(json.loads(body)), original_guide)

                cli_capability = subprocess.run(
                    [str(executable), "demo-check", "--capability", "api-e2e"],
                    cwd=runtime, env=demo_environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=65, check=False,
                )
                self.assertEqual(cli_capability.returncode, 0, cli_capability.stdout + cli_capability.stderr)
                cli_receipt = json.loads(cli_capability.stdout)
                assert_api_receipt(cli_receipt)
                self.assertNotEqual(cli_receipt["run_id"], http_receipt["run_id"])
                self.assertNotEqual(cli_receipt["task_ids"], http_receipt["task_ids"])

                # This fresh venv installs only the wheel with --no-deps. The
                # optional browser extra is intentionally absent, so lack of
                # Playwright must produce a blocked receipt, never a pass.
                unavailable_browser = subprocess.run(
                    [str(executable), "demo-check", "--capability", "browser-e2e"],
                    cwd=runtime, env=demo_environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=65, check=False,
                )
                self.assertEqual(unavailable_browser.returncode, 2, unavailable_browser.stdout + unavailable_browser.stderr)
                blocked = json.loads(unavailable_browser.stdout)
                self.assertEqual(blocked["capability_id"], "browser-e2e")
                self.assertEqual(blocked["status"], "blocked")
                self.assertEqual(blocked["task_ids"], [])
                self.assertTrue(blocked["reason_code"])
                self.assertTrue(blocked["setup_commands"])
                status, body, _ = request("GET", "/api/state")
                self.assertEqual(status, 200)
                self.assertEqual(guide_identity(json.loads(body)), original_guide)
                self.assertEqual(len(list(demo_temporary_root.iterdir())), 1, "standalone capability checks left temporary state")
                self.assertEqual(sorted(path.name for path in runtime.iterdir()), original_runtime_entries)
                for expected_step in range(7):
                    status, body, _ = request(
                        "POST", "/api/step", {"expected_step": expected_step}, snapshot["csrf_token"],
                    )
                    self.assertEqual(status, 200, f"installed guide step failed: {expected_step}")
                    snapshot = json.loads(body)
                    self.assertEqual(snapshot["step"], expected_step + 1)
                    if expected_step == 3:
                        self.assertTrue(any(run["state"] == "UNKNOWN" for run in snapshot["records"]["runs"]))
                    if expected_step == 4:
                        self.assertTrue(all(run["state"] == "SUCCEEDED" for run in snapshot["records"]["runs"]))
                self.assertTrue(snapshot["done"])
                self.assertEqual(snapshot["task"]["state"], "DONE")
                self.assertEqual(len(snapshot["records"]["runs"]), 3)
                self.assertTrue(snapshot["records"]["evidence"])
                self.assertTrue(snapshot["records"]["knowledge"])
                self.assertTrue(any(
                    checkpoint["task_revision"] == snapshot["task"]["revision"]
                    for checkpoint in snapshot["records"]["checkpoints"]
                ))
                status, body, _ = request("GET", "/api/state")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["task"]["state"], "DONE")
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
                    self.fail("installed demo did not stop within 10 seconds after SIGINT")
                error_output.seek(0)
                errors = error_output.read().decode("utf-8", errors="replace")
                self.assertEqual(process.returncode, 0, f"installed demo did not shut down cleanly:\n{errors}")
                self.assertEqual(list(demo_temporary_root.iterdir()), [], "installed demo left temporary state after SIGINT")
                self.assertEqual(sorted(path.name for path in runtime.iterdir()), original_runtime_entries, "installed demo wrote into the empty runtime directory")

    def test_installed_wheel_bootstraps_and_runs_outside_source_tree(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="qingtian-wheel-") as temporary:
            root = Path(temporary)
            build_source = root / "source"
            build_source.mkdir()
            shutil.copy2(source_root / "pyproject.toml", build_source)
            for candidate in (
                "README.md",
                "LICENSE",
                "LICENSE.txt",
                "LICENSE.md",
                "NOTICE",
            ):
                source = source_root / candidate
                if source.is_file():
                    shutil.copy2(source, build_source)
            shutil.copytree(
                source_root / "qingtian_kb",
                build_source / "qingtian_kb",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            shutil.copytree(
                source_root / "qingtian_core",
                build_source / "qingtian_core",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            shutil.copytree(
                source_root / "qingtian_engine", build_source / "qingtian_engine",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )

            wheelhouse = root / "wheelhouse"
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            for key in tuple(environment):
                if key.startswith("QINGTIAN_"):
                    environment.pop(key)
            environment.update(
                {
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PIP_NO_INPUT": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-cache-dir",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheelhouse),
                    str(build_source),
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            wheels = list(wheelhouse.glob("*.whl"))
            self.assertEqual(len(wheels), 1, built.stdout + built.stderr)
            wheel = wheels[0]
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                entry_points_name = next(
                    name
                    for name in names
                    if name.endswith(".dist-info/entry_points.txt")
                )
                entry_points = archive.read(entry_points_name).decode("utf-8")
            for resource in (
                "qingtian_engine/entrypoint.py",
                "qingtian_engine/runner.py",
                "qingtian_engine/server.py",
                "qingtian_engine/resources/policy.json",
                "qingtian_engine/static/index.html",
                "qingtian_engine/static/app.js",
                "qingtian_engine/static/styles.css",
                "qingtian_core/capability_checks.py",
                "qingtian_core/resources/capabilities.json",
                "qingtian_kb/resources/__init__.py",
                "qingtian_kb/resources/home.md",
                "qingtian_kb/resources/knowledge-root-marker.txt",
                "qingtian_kb/resources/obsidian-app.json",
                "qingtian_kb/resources/sources.example.json",
                "qingtian_kb/resources/contracts/provider-request.schema.json",
                "qingtian_kb/resources/contracts/provider-response.schema.json",
                "qingtian_core/resources/schemas/checkpoint.schema.json",
                "qingtian_core/resources/schemas/evidence.schema.json",
                "qingtian_core/resources/schemas/knowledge.schema.json",
                "qingtian_core/resources/schemas/project-adapter.schema.json",
                "qingtian_core/resources/schemas/release-allowlist.schema.json",
                "qingtian_core/resources/schemas/run.schema.json",
                "qingtian_core/resources/schemas/session.schema.json",
                "qingtian_core/resources/schemas/task.schema.json",
                "qingtian_core/resources/schemas/verification-receipt.schema.json",
                "qingtian_core/resources/demo/index.html",
                "qingtian_core/resources/demo/app.css",
                "qingtian_core/resources/demo/app.js",
            ):
                self.assertIn(resource, names)
            self.assertIn("qingtian = qingtian_engine.entrypoint:main", entry_points)
            self.assertIn("qingtian-lab = qingtian_core.cli:main", entry_points)
            self.assertIn("qingtian-kb = qingtian_kb.cli:main", entry_points)
            self.assertFalse(any(name.startswith("vault/") for name in names))

            virtual_environment = root / "venv"
            venv.EnvBuilder(with_pip=True, clear=True).create(virtual_environment)
            scripts = virtual_environment / "bin"
            python = scripts / "python"
            installed = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    str(wheel),
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(
                installed.returncode, 0, installed.stdout + installed.stderr
            )

            runtime = root / "empty-runtime"
            workspace = root / "source-project"
            runtime.mkdir()
            workspace.mkdir()
            (workspace / "README.md").write_text(
                "# Portable wheel knowledge\n\nA generic installation fixture.\n",
                encoding="utf-8",
            )
            knowledge_executable = scripts / "qingtian-kb"
            core_executable = scripts / "qingtian-lab"
            engine_executable = scripts / "qingtian"

            def invoke(
                executable: Path,
                *arguments: str,
                stdin_payload: dict[str, object] | None = None,
            ) -> dict[str, object]:
                completed = subprocess.run(
                    [str(executable), *arguments],
                    cwd=runtime,
                    env=environment,
                    input=(
                        json.dumps(stdin_payload, ensure_ascii=False)
                        if stdin_payload is not None
                        else None
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"command={arguments!r}\n{completed.stdout}\n{completed.stderr}",
                )
                return json.loads(completed.stdout)

            core_doctor = invoke(core_executable, "doctor")
            self.assertEqual(core_doctor["status"], "ok")
            engine_checks = invoke(engine_executable, "selftest")
            self.assertEqual(engine_checks["status"], "passed")
            self.assertEqual(engine_checks["scope"], "synthetic-engine-selftest")
            self.assertFalse(engine_checks["model_called"])
            self.assertTrue(all(item["passed"] for item in engine_checks["checks"]))
            self.assertEqual(list(runtime.iterdir()), [])
            self.assert_installed_real_engine(engine_executable, runtime, root, environment)
            self.assert_installed_demo_web(core_executable, runtime, root, environment)
            core_database = runtime / "qingtian.sqlite3"
            core_demo = invoke(
                core_executable,
                "demo",
                "--db",
                str(core_database),
            )
            self.assertEqual(core_demo["status"], "ok")
            self.assertTrue(core_demo["knowledge_search"])
            self.assertTrue(core_database.is_file())

            initialized = invoke(
                knowledge_executable,
                "init",
                "--workspace",
                str(workspace),
                "--project",
                "wheel-project",
            )
            self.assertEqual(initialized["status"], "initialized")
            self.assertEqual(
                Path(str(initialized["config"])),
                (runtime / "config" / "sources.json").resolve(),
            )
            self.assertTrue((runtime / ".qingtian-knowledge-root").is_file())
            self.assertTrue((runtime / "vault" / "00-Home" / "Home.md").is_file())
            self.assertFalse((runtime / "config" / "sources.example.json").exists())

            doctor = invoke(knowledge_executable, "doctor")
            self.assertEqual(doctor["status"], "ok")
            self.assertFalse(doctor["state_initialized"])
            plan = invoke(knowledge_executable, "plan")
            self.assertEqual(plan["source_count"], 1)
            ingested = invoke(knowledge_executable, "ingest")
            self.assertEqual(ingested["result"], "passed")
            validated = invoke(knowledge_executable, "validate")
            self.assertEqual(validated["status"], "passed")
            provider = invoke(
                knowledge_executable,
                "provider-query",
                stdin_payload={
                    "schema_version": "1.0",
                    "query": "Portable wheel knowledge",
                    "caller_id": "packaging-test",
                    "purpose": "test",
                    "retrieval_modes": ["candidate", "history"],
                    "projects": ["wheel-project"],
                    "top_k": 5,
                    "request_id": "wheel-smoke",
                },
            )
            self.assertEqual(provider["schema_version"], "1.0")
            self.assertEqual(provider["request_id"], "wheel-smoke")
            self.assertGreater(provider["result_count"], 0)
            self.assertTrue(
                all(result["authority"] == "history" for result in provider["results"])
            )
            self.assertTrue(
                all(
                    result["eligible_for_generation"] is False
                    for result in provider["results"]
                )
            )
            self.assertFalse(provider["query_persisted"])
            self.assertFalse(provider["query_echoed"])


if __name__ == "__main__":
    unittest.main()
