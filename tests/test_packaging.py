from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile


@unittest.skipUnless(os.name == "posix", "secure runtime currently requires POSIX")
class InstalledWheelTestCase(unittest.TestCase):
    maxDiff = None

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

            wheelhouse = root / "wheelhouse"
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("QINGTIAN_CONFIG", None)
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
            ):
                self.assertIn(resource, names)
            self.assertIn("qingtian = qingtian_core.cli:main", entry_points)
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
            core_executable = scripts / "qingtian"

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
