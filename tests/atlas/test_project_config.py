from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from qingtian_engine.db import Database
from qingtian_engine.project_config import (
    PROJECT_CONFIG_ENV,
    ProjectConfigError,
    load_project_config,
    register_project,
    remove_project,
)
from qingtian_engine.runner import RunManager
from qingtian_engine.service import ControlPlane


class ProjectConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _git_repository(path: Path) -> Path:
        path.mkdir(parents=True)
        (path / ".git").mkdir()
        return path

    def _write(self, document: dict, name: str = "projects.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_missing_config_is_an_empty_runtime_registry(self) -> None:
        config = load_project_config(data_dir=self.data_dir)
        self.assertEqual({}, config.projects)
        self.assertEqual(
            (self.data_dir / "config" / "projects.local.json").resolve(),
            config.source_path,
        )
        self.assertFalse(config.source_path.exists())

    def test_register_resolve_list_and_remove_project(self) -> None:
        repository = self._git_repository(self.root / "sample-repository")
        scope = repository / "apps" / "mobile"
        scope.mkdir(parents=True)

        registered = register_project(
            "sample",
            repository,
            "dev",
            ["backend", "qa"],
            "apps/mobile",
            data_dir=self.data_dir,
        )

        self.assertEqual(repository.resolve(), registered.projects["sample"].repository)
        self.assertEqual(scope.resolve(), registered.projects["sample"].scope_path)
        self.assertEqual(
            registered.projects["sample"], registered.resolve_reference("sample")
        )
        self.assertEqual(
            registered.projects["sample"],
            registered.resolve_reference(str(repository.resolve())),
        )
        self.assertEqual(
            [registered.projects["sample"]],
            registered.targets_for_roles({"qa"}),
        )
        self.assertEqual(0o600, registered.source_path.stat().st_mode & 0o777)
        self.assertEqual(registered.to_dict(), load_project_config(data_dir=self.data_dir).to_dict())

        removed = remove_project("sample", data_dir=self.data_dir)
        self.assertEqual({}, removed.projects)

    def test_explicit_environment_override_is_absolute_and_precedes_default(self) -> None:
        repository = self._git_repository(self.root / "repository")
        override = self.root / "custom" / "projects.json"
        override.parent.mkdir()
        override.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "projects": {
                        "sample": {
                            "repository": str(repository),
                            "base_branch": "dev",
                            "roles": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {PROJECT_CONFIG_ENV: str(override)}):
            config = load_project_config(data_dir=self.data_dir)
        self.assertEqual(Path(os.path.abspath(override)), config.source_path)
        with patch.dict(os.environ, {PROJECT_CONFIG_ENV: "relative.json"}):
            with self.assertRaisesRegex(ProjectConfigError, "absolute"):
                load_project_config(data_dir=self.data_dir)

    def test_rejects_unsafe_repository_branch_scope_and_shape(self) -> None:
        repository = self._git_repository(self.root / "repository")
        bad_projects = (
            {"repository": "relative", "base_branch": "dev", "roles": []},
            {"repository": str(repository), "base_branch": "main", "roles": []},
            {"repository": str(repository), "base_branch": "--unsafe", "roles": []},
            {"repository": str(repository), "base_branch": "dev", "roles": ["Bad Role"]},
            {
                "repository": str(repository),
                "base_branch": "dev",
                "roles": [],
                "scope": "../outside",
            },
        )
        for index, project in enumerate(bad_projects):
            with self.subTest(index=index):
                path = self._write(
                    {"schema_version": 1, "projects": {"sample": project}},
                    "bad-{}.json".format(index),
                )
                with self.assertRaises(ProjectConfigError):
                    load_project_config(data_dir=self.data_dir, path=path)

        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            '{"schema_version":1,"schema_version":1,"projects":{}}',
            encoding="utf-8",
        )
        with self.assertRaises(ProjectConfigError):
            load_project_config(data_dir=self.data_dir, path=duplicate)

    def test_rejects_engine_state_source_and_installed_package_repositories(self) -> None:
        state_repo = self._git_repository(self.data_dir / "repository")
        with self.assertRaisesRegex(ProjectConfigError, "data directory"):
            register_project("state", state_repo, "dev", [], data_dir=self.data_dir)

        source_root = self.root / "engine-source"
        source_repo = self._git_repository(source_root / "nested")
        with patch("qingtian_engine.project_config.SOURCE_ROOT", source_root.resolve()):
            with self.assertRaisesRegex(ProjectConfigError, "source tree"):
                register_project("source", source_repo, "dev", [], data_dir=self.data_dir)

        installed = self._git_repository(self.root / "site-packages" / "project")
        with self.assertRaisesRegex(ProjectConfigError, "installed package"):
            register_project("installed", installed, "dev", [], data_dir=self.data_dir)

    def test_symlink_config_is_rejected(self) -> None:
        target = self._write({"schema_version": 1, "projects": {}}, "target.json")
        symlink = self.root / "symlink.json"
        symlink.symlink_to(target)
        with self.assertRaises(ProjectConfigError):
            load_project_config(data_dir=self.data_dir, path=symlink)

    def test_registration_rejects_a_symlink_lock_file(self) -> None:
        repository = self._git_repository(self.root / "repository")
        config_path = self.data_dir / "config" / "projects.local.json"
        config_path.parent.mkdir(parents=True)
        lock_target = self.root / "lock-target"
        lock_target.write_text("must-not-change", encoding="utf-8")
        config_path.with_name(".projects.local.json.lock").symlink_to(lock_target)
        with self.assertRaisesRegex(ProjectConfigError, "lock"):
            register_project(
                "sample", repository, "dev", [], data_dir=self.data_dir
            )
        self.assertEqual("must-not-change", lock_target.read_text(encoding="utf-8"))

    def test_concurrent_registration_does_not_lose_an_update(self) -> None:
        repositories = [
            self._git_repository(self.root / "repository-{}".format(index))
            for index in range(2)
        ]

        def register(index: int) -> None:
            register_project(
                "sample-{}".format(index),
                repositories[index],
                "dev",
                ["role-{}".format(index)],
                data_dir=self.data_dir,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(register, range(2)))
        self.assertEqual(
            {"sample-0", "sample-1"},
            set(load_project_config(data_dir=self.data_dir).projects),
        )

    def test_runner_resolves_registered_name_and_owner_role_without_history(self) -> None:
        repository = self._git_repository(self.root / "registered")
        config = register_project(
            "sample", repository, "dev", ["frontend"], data_dir=self.data_dir
        )
        service = ControlPlane(Database(self.root / "control.sqlite3"))
        named = service.create_task(
            "Explicit project",
            idempotency_key="named",
            repository="sample",
            owner_session="frontend",
            worker_type="cli",
        )
        routed = service.create_task(
            "Frontend component",
            idempotency_key="routed",
            owner_session="frontend",
            worker_type="cli",
        )
        manager = RunManager(service, self.data_dir)
        with patch("qingtian_engine.runner.load_project_config", return_value=config):
            named_result = manager._resolve_dispatch_repository(named)
            routed_result = manager._resolve_dispatch_repository(routed)
        self.assertEqual(str(repository.resolve()), named_result["repository"])
        self.assertEqual("dev", named_result["base_branch"])
        self.assertEqual(str(repository.resolve()), routed_result["repository"])

    def test_runner_fails_closed_and_never_reuses_a_historical_path(self) -> None:
        legacy = self._git_repository(self.root / "legacy")
        service = ControlPlane(Database(self.root / "control.sqlite3"))
        service.create_task(
            "Historical frontend task",
            idempotency_key="historical",
            repository=str(legacy),
            base_branch="dev",
            owner_session="frontend",
        )
        task = service.create_task(
            "New frontend task",
            idempotency_key="new",
            owner_session="frontend",
            worker_type="cli",
            state="PLANNED",
        )
        manager = RunManager(service, self.data_dir)
        empty = load_project_config(data_dir=self.data_dir)
        with patch("qingtian_engine.runner.load_project_config", return_value=empty):
            with self.assertRaisesRegex(RuntimeError, "project register"):
                manager._resolve_dispatch_repository(task)
        self.assertEqual("WAITING", service.get_task(task["id"])["state"])


if __name__ == "__main__":
    unittest.main()
