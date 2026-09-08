from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

import qingtian_kb.cli as cli_module
from qingtian_kb.cli import main, parser
from qingtian_kb.engine import KnowledgeEngine


class KnowledgeCliBootstrapTestCase(unittest.TestCase):
    def test_diagnostic_commands_are_physically_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-readonly-cli-") as temporary:
            root = Path(temporary)
            package = root / "knowledge"
            config_dir = package / "config"
            workspace = root / "workspace"
            config_dir.mkdir(parents=True)
            workspace.mkdir()
            (workspace / "README.md").write_text(
                "# Read-only diagnostic fixture\n", encoding="utf-8"
            )
            config = config_dir / "sources.json"

            def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
                output = StringIO()
                with redirect_stdout(output):
                    result = main(["--config", str(config), *arguments])
                return result, json.loads(output.getvalue())

            self.assertEqual(
                invoke(
                    "init",
                    "--workspace",
                    str(workspace),
                    "--project",
                    "readonly-fixture",
                )[0],
                0,
            )
            state_dir = package / ".state"
            self.assertFalse(state_dir.exists())
            doctor_status, doctor = invoke("doctor")
            self.assertEqual(doctor_status, 0)
            self.assertFalse(doctor["state_initialized"])
            self.assertEqual(invoke("plan")[0], 0)
            self.assertFalse(
                state_dir.exists(),
                "doctor/plan must not bootstrap local state as a side effect",
            )

            engine = KnowledgeEngine(config)
            try:
                self.assertEqual(engine.ingest()["result"], "passed")
            finally:
                engine.close()
            state = state_dir / "qingtian-kb.sqlite"

            def snapshot() -> dict[str, tuple[str, int, int, int]]:
                result: dict[str, tuple[str, int, int, int]] = {}
                for path in package.rglob("*"):
                    if not path.is_file() or path.is_symlink():
                        continue
                    info = path.stat(follow_symlinks=False)
                    result[path.relative_to(package).as_posix()] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        stat.S_IMODE(info.st_mode),
                    )
                return result

            os.chmod(state, 0o400)
            os.chmod(state_dir, 0o500)
            try:
                before = snapshot()
                for command in (
                    ("doctor",),
                    ("plan",),
                    ("validate",),
                    ("stats",),
                    ("search", "diagnostic"),
                ):
                    status, _response = invoke(*command)
                    self.assertEqual(status, 0, command)
                after = snapshot()
                self.assertEqual(after, before)
                self.assertEqual(
                    [path.name for path in state_dir.iterdir() if path.name.startswith(state.name + "-")],
                    [],
                )

                reader = KnowledgeEngine(config, index_mode="read")
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.index.db.execute(
                            "INSERT INTO metadata(key,value) VALUES('forbidden','write')"
                        )
                finally:
                    reader.close()
                self.assertEqual(snapshot(), before)
            finally:
                os.chmod(state_dir, 0o700)
                os.chmod(state, 0o600)

    def test_init_creates_private_runnable_skeleton_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-public-init-") as temporary:
            root = Path(temporary)
            package = root / "knowledge"
            config_dir = package / "config"
            workspace = root / "workspace"
            config_dir.mkdir(parents=True)
            workspace.mkdir()
            (workspace / "README.md").write_text("# Example project\n", encoding="utf-8")
            config = config_dir / "sources.json"
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "--config",
                        str(config),
                        "init",
                        "--workspace",
                        str(workspace),
                        "--project",
                        "example-project",
                    ]
                )
            self.assertEqual(result, 0)
            response = json.loads(output.getvalue())
            self.assertEqual(response["status"], "initialized")
            self.assertEqual(response["classification"], "P1-internal-local")
            self.assertEqual(response["config"], str(config.resolve()))
            self.assertEqual(response["vault"], str((package / "vault").resolve()))
            self.assertTrue(
                all(command.startswith("qingtian-kb ") for command in response["next_steps"])
            )
            self.assertTrue(
                all(not command.startswith("./") for command in response["next_steps"])
            )
            self.assertEqual(
                (package / ".qingtian-knowledge-root")
                .read_text(encoding="utf-8")
                .strip(),
                "qingtian-knowledge-root-v1",
            )
            payload = json.loads(config.read_text(encoding="utf-8"))
            self.assertFalse(Path(payload["workspace_root"]).is_absolute())
            self.assertEqual(payload["source_sets"][0]["project"], "example-project")
            self.assertTrue((package / "vault" / "00-Home" / "Home.md").is_file())
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((package / "vault").stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((package / "vault" / "00-Home" / "Home.md").stat().st_mode),
                0o600,
            )
            engine = KnowledgeEngine(config)
            try:
                self.assertEqual(engine.workspace, workspace.resolve())
                self.assertEqual(engine.validate_vault()["status"], "passed")
            finally:
                engine.close()

            original = config.read_bytes()
            output = StringIO()
            with redirect_stdout(output):
                second = main(
                    [
                        "--config",
                        str(config),
                        "init",
                        "--workspace",
                        str(workspace),
                    ]
                )
            self.assertEqual(second, 2)
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(json.loads(output.getvalue())["status"], "error")

    def test_config_precedence_is_explicit_then_environment_then_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-config-default-") as temporary:
            root = Path(temporary)
            environment_config = root / "environment" / "sources.json"
            explicit_config = root / "explicit" / "sources.json"
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(
                    os.environ,
                    {"QINGTIAN_CONFIG": str(environment_config)},
                    clear=False,
                ):
                    self.assertEqual(
                        Path(parser().parse_args(["doctor"]).config),
                        environment_config,
                    )
                    self.assertEqual(
                        Path(
                            parser()
                            .parse_args(
                                ["--config", str(explicit_config), "doctor"]
                            )
                            .config
                        ),
                        explicit_config,
                    )
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("QINGTIAN_CONFIG", None)
                    self.assertEqual(
                        Path(parser().parse_args(["doctor"]).config).resolve(),
                        (root / "config" / "sources.json").resolve(),
                    )
            finally:
                os.chdir(original_cwd)

    def test_init_ignores_environment_destination_but_honors_explicit_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-init-destination-") as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            workspace = root / "workspace"
            environment_root = root / "environment-root"
            for directory in (runtime, workspace, environment_root):
                directory.mkdir()
            environment_config = environment_root / "config" / "sources.json"
            original_cwd = Path.cwd()
            try:
                os.chdir(runtime)
                with patch.dict(
                    os.environ,
                    {"QINGTIAN_CONFIG": str(environment_config)},
                    clear=False,
                ):
                    output = StringIO()
                    with redirect_stdout(output):
                        status = main(
                            [
                                "init",
                                "--workspace",
                                str(workspace),
                                "--project",
                                "environment-isolation",
                            ]
                        )
                self.assertEqual(status, 0, output.getvalue())
                response = json.loads(output.getvalue())
                self.assertEqual(
                    Path(response["config"]),
                    (runtime / "config" / "sources.json").resolve(),
                )
                self.assertFalse(environment_config.exists())
                self.assertFalse((environment_root / ".qingtian-knowledge-root").exists())
            finally:
                os.chdir(original_cwd)

    def test_init_rejects_noncanonical_explicit_config_before_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-init-config-shape-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            for config in (
                root / "custom" / "sources.json",
                root / "config" / "renamed.json",
            ):
                with self.subTest(config=config):
                    output = StringIO()
                    with redirect_stdout(output):
                        status = main(
                            [
                                "--config",
                                str(config),
                                "init",
                                "--workspace",
                                str(workspace),
                            ]
                        )
                    self.assertEqual(status, 2)
                    self.assertFalse((root / ".qingtian-knowledge-root").exists())
                    self.assertFalse(config.exists())

    def test_init_preflights_workspace_and_resources_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-init-preflight-") as temporary:
            root = Path(temporary)
            config = root / "config" / "sources.json"
            output = StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "--config",
                        str(config),
                        "init",
                        "--workspace",
                        str(root / "missing-workspace"),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertEqual(list(root.iterdir()), [])

            workspace = root / "workspace"
            workspace.mkdir()
            original_reader = cli_module._read_bootstrap_resource

            def invalid_resource(name: str) -> str:
                if name == "obsidian-app.json":
                    return "[]"
                return original_reader(name)

            output = StringIO()
            with patch.object(
                cli_module,
                "_read_bootstrap_resource",
                side_effect=invalid_resource,
            ), redirect_stdout(output):
                status = main(
                    [
                        "--config",
                        str(config),
                        "init",
                        "--workspace",
                        str(workspace),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertEqual(list(root.iterdir()), [workspace])

    def test_init_rolls_back_created_entries_and_permissions_on_write_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-init-rollback-") as temporary:
            root = Path(temporary)
            config_dir = root / "config"
            workspace = root / "workspace"
            config_dir.mkdir(mode=0o755)
            workspace.mkdir()
            config = config_dir / "sources.json"
            initial_mode = stat.S_IMODE(config_dir.stat().st_mode)
            original_writer = cli_module._write_new_private_file_at

            def fail_on_config(
                parent_fd: int, name: str, content: str
            ) -> tuple[int, int]:
                if name == "sources.json":
                    raise OSError("synthetic final-write failure")
                return original_writer(parent_fd, name, content)

            output = StringIO()
            with patch.object(
                cli_module,
                "_write_new_private_file_at",
                side_effect=fail_on_config,
            ), redirect_stdout(output):
                status = main(
                    [
                        "--config",
                        str(config),
                        "init",
                        "--workspace",
                        str(workspace),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertFalse((root / ".qingtian-knowledge-root").exists())
            self.assertFalse((root / "vault").exists())
            self.assertFalse(config.exists())
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), initial_mode)

    def test_init_rejects_linked_private_files_without_mutating_outside_inode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-init-linked-files-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            cases = {
                "marker": Path(".qingtian-knowledge-root"),
                "home": Path("vault/00-Home/Home.md"),
                "obsidian": Path("vault/.obsidian/app.json"),
            }
            for link_kind in ("symlink", "hardlink"):
                for label, relative in cases.items():
                    with self.subTest(link_kind=link_kind, target=label):
                        package = root / f"package-{link_kind}-{label}"
                        config = package / "config" / "sources.json"
                        target = package / relative
                        target.parent.mkdir(parents=True)
                        outside = root / f"outside-{link_kind}-{label}.txt"
                        outside.write_text(
                            "qingtian-knowledge-root-v1\n"
                            if label == "marker"
                            else "synthetic outside content\n",
                            encoding="utf-8",
                        )
                        os.chmod(outside, 0o644)
                        try:
                            if link_kind == "symlink":
                                target.symlink_to(outside)
                            else:
                                os.link(outside, target)
                        except (OSError, NotImplementedError) as exc:
                            self.skipTest(f"{link_kind} unavailable: {exc}")
                        before = outside.read_bytes()
                        before_mode = stat.S_IMODE(outside.stat().st_mode)
                        output = StringIO()
                        with redirect_stdout(output):
                            status = main(
                                [
                                    "--config",
                                    str(config),
                                    "init",
                                    "--workspace",
                                    str(workspace),
                                ]
                            )
                        self.assertEqual(status, 2)
                        self.assertEqual(outside.read_bytes(), before)
                        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), before_mode)
                        self.assertFalse(config.exists())
                        if label != "marker":
                            self.assertFalse(
                                (package / ".qingtian-knowledge-root").exists()
                            )

    def test_init_rejects_symlinked_private_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-public-symlink-") as temporary:
            root = Path(temporary)
            package = root / "knowledge"
            config_dir = package / "config"
            workspace = root / "workspace"
            outside = root / "outside"
            config_dir.mkdir(parents=True)
            workspace.mkdir()
            outside.mkdir()
            (package / "vault").mkdir()
            (package / "vault" / "00-Home").symlink_to(outside, target_is_directory=True)

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "--config",
                        str(config_dir / "sources.json"),
                        "init",
                        "--workspace",
                        str(workspace),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertFalse((config_dir / "sources.json").exists())
            self.assertFalse((outside / "Home.md").exists())
            self.assertFalse((package / ".qingtian-knowledge-root").exists())

    def test_init_root_replacement_fails_closed_at_every_identity_checkpoint(self) -> None:
        stages = (
            "root-opened",
            "preflight-complete",
            "marker",
            "config-dir",
            "vault-dir",
            "home-dir",
            "obsidian-dir",
            "home-file",
            "app-file",
            "config-file",
            "before-commit",
        )
        with tempfile.TemporaryDirectory(prefix="qingtian-init-root-race-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            for target_stage in stages:
                with self.subTest(stage=target_stage):
                    package = root / f"knowledge-{target_stage}"
                    moved = root / f"moved-{target_stage}"
                    outside = root / f"outside-{target_stage}"
                    package.mkdir()
                    outside.mkdir()
                    sentinel = outside / "sentinel.txt"
                    sentinel.write_text("outside remains untouched\n", encoding="utf-8")
                    before = sentinel.read_bytes()
                    original_checkpoint = cli_module._init_stage_checkpoint
                    swapped = False

                    def replace_root(
                        stage: str,
                        root_path: Path,
                        root_fd: int,
                        root_signature: tuple[int, int],
                        bindings: tuple[
                            tuple[int, str, tuple[int, int], bool], ...
                        ],
                    ) -> None:
                        nonlocal swapped
                        if stage == target_stage and not swapped:
                            package.rename(moved)
                            package.symlink_to(outside, target_is_directory=True)
                            swapped = True
                        original_checkpoint(
                            stage,
                            root_path,
                            root_fd,
                            root_signature,
                            bindings,
                        )

                    output = StringIO()
                    with patch.object(
                        cli_module,
                        "_init_stage_checkpoint",
                        side_effect=replace_root,
                    ), redirect_stdout(output):
                        status = main(
                            [
                                "--config",
                                str(package / "config" / "sources.json"),
                                "init",
                                "--workspace",
                                str(workspace),
                            ]
                        )

                    self.assertTrue(swapped)
                    self.assertEqual(status, 2)
                    response = json.loads(output.getvalue())
                    self.assertEqual(response["status"], "error")
                    self.assertNotEqual(response["status"], "initialized")
                    self.assertEqual(sentinel.read_bytes(), before)
                    self.assertEqual(
                        [path.name for path in outside.iterdir()], ["sentinel.txt"]
                    )
                    self.assertEqual(list(moved.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
