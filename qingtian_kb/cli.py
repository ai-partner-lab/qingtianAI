from __future__ import annotations

import argparse
from importlib import resources
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
from typing import Any

from . import __version__
from .engine import EXTRACTOR_VERSION, KnowledgeEngine
from .models import KnowledgeError
from .provider import ProviderRequest, QingtianKnowledgeProvider, SAFE_IDENTIFIER


CONFIG_ENVIRONMENT_VARIABLE = "QINGTIAN_CONFIG"
BOOTSTRAP_RESOURCE_PACKAGE = "qingtian_kb.resources"
SAFE_BOOTSTRAP_PROJECT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class NonEchoingArgumentParser(argparse.ArgumentParser):
    """Keep rejected argv values out of CLI diagnostics."""

    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


class ConfigPathAction(argparse.Action):
    """Record whether ``--config`` was supplied instead of inherited."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        setattr(namespace, self.dest, values)
        setattr(namespace, "config_explicit", True)


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def default_config_path() -> Path:
    """Resolve the per-workspace default without referring to the install tree."""

    configured = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.cwd() / "config" / "sources.json"


def _read_bootstrap_resource(name: str) -> str:
    """Read a template shipped inside the installed wheel."""

    try:
        resource = resources.files(BOOTSTRAP_RESOURCE_PACKAGE).joinpath(name)
        if not resource.is_file():
            raise FileNotFoundError(name)
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError) as exc:
        raise KnowledgeError("packaged bootstrap resources are unavailable") from exc


def with_engine(
    args: argparse.Namespace, *, index_mode: str = "write"
) -> KnowledgeEngine:
    return KnowledgeEngine(args.config, index_mode=index_mode)


def sqlite_fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe_fts USING fts5(value)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def _entry_signature(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int) or value == 0:
        raise KnowledgeError(f"secure init requires {name}")
    return value


def _directory_open_flags() -> int:
    return os.O_RDONLY | _required_open_flag("O_DIRECTORY") | _required_open_flag(
        "O_NOFOLLOW"
    )


def _assert_named_root_stable(
    root_path: Path, root_fd: int, signature: tuple[int, int]
) -> None:
    try:
        opened = os.fstat(root_fd)
        named = os.stat(root_path, follow_symlinks=False)
    except OSError as exc:
        raise KnowledgeError("knowledge root changed during init") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _entry_signature(opened) != signature
        or _entry_signature(named) != signature
    ):
        raise KnowledgeError("knowledge root changed during init")


def _assert_bound_entry_stable(
    parent_fd: int,
    name: str,
    signature: tuple[int, int],
    directory: bool,
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise KnowledgeError("private knowledge entry changed during init") from exc
    valid_type = stat.S_ISDIR(named.st_mode) if directory else stat.S_ISREG(named.st_mode)
    if (
        stat.S_ISLNK(named.st_mode)
        or not valid_type
        or _entry_signature(named) != signature
        or (not directory and named.st_nlink != 1)
    ):
        raise KnowledgeError("private knowledge entry changed during init")


def _init_stage_checkpoint(
    stage: str,
    root_path: Path,
    root_fd: int,
    root_signature: tuple[int, int],
    bindings: tuple[tuple[int, str, tuple[int, int], bool], ...],
) -> None:
    """Testable identity checkpoint for the root and every named descendant."""

    del stage
    _assert_named_root_stable(root_path, root_fd, root_signature)
    for parent_fd, name, signature, directory in bindings:
        _assert_bound_entry_stable(parent_fd, name, signature, directory)


def _open_existing_private_directory_at(
    parent_fd: int, name: str
) -> tuple[int, tuple[int, int]] | None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KnowledgeError("cannot inspect private knowledge directory") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise KnowledgeError("private knowledge directory is unsafe")
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        signature = _entry_signature(opened)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or signature != _entry_signature(before)
            or signature != _entry_signature(named_after)
        ):
            raise KnowledgeError("private knowledge directory changed during init")
        return descriptor, signature
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, OSError):
            raise KnowledgeError("cannot inspect private knowledge directory") from exc
        raise


def _inspect_private_file_at(
    parent_fd: int, name: str, *, max_bytes: int | None = None
) -> tuple[tuple[int, int], bytes] | None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KnowledgeError("cannot inspect private knowledge file") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise KnowledgeError("private knowledge file is unsafe")
    flags = os.O_RDONLY | _required_open_flag("O_NOFOLLOW")
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise KnowledgeError("cannot inspect private knowledge file") from exc
    try:
        opened = os.fstat(descriptor)
        signature = _entry_signature(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or signature != _entry_signature(before)
        ):
            raise KnowledgeError("private knowledge file changed during init")
        payload = b""
        if max_bytes is not None:
            while len(payload) <= max_bytes:
                chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload += chunk
            if len(payload) > max_bytes:
                raise KnowledgeError("private knowledge file is unexpectedly large")
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            after.st_nlink != 1
            or _entry_signature(after) != signature
            or _entry_signature(named_after) != signature
        ):
            raise KnowledgeError("private knowledge file changed during init")
        return signature, payload
    except OSError as exc:
        raise KnowledgeError("cannot inspect private knowledge file") from exc
    finally:
        os.close(descriptor)


def _entry_exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KnowledgeError("cannot inspect private knowledge entry") from exc


def _write_new_private_file_at(
    parent_fd: int, name: str, content: str
) -> tuple[int, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_open_flag("O_NOFOLLOW")
    )
    descriptor: int | None = None
    signature: tuple[int, int] | None = None
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        signature = _entry_signature(opened)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or signature != _entry_signature(named)
        ):
            raise KnowledgeError("private knowledge file changed during creation")
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            after.st_size != len(payload)
            or after.st_nlink != 1
            or _entry_signature(after) != signature
            or _entry_signature(named_after) != signature
        ):
            raise KnowledgeError("private knowledge file changed during creation")
        return signature
    except BaseException:
        if signature is None and descriptor is not None:
            try:
                signature = _entry_signature(os.fstat(descriptor))
            except OSError:
                pass
        if signature is not None:
            _remove_bound_inode(parent_fd, name, signature, directory=False)
        elif descriptor is not None:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _create_private_directory_at(
    parent_fd: int, name: str
) -> tuple[int, tuple[int, int]]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except OSError as exc:
        raise KnowledgeError("cannot create private knowledge directory") from exc
    descriptor: int | None = None
    signature: tuple[int, int] | None = None
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        signature = _entry_signature(opened)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or signature != _entry_signature(named)
        ):
            raise KnowledgeError("private knowledge directory changed during init")
        return descriptor, signature
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if signature is not None:
            _remove_bound_inode(parent_fd, name, signature, directory=True)
        else:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise KnowledgeError("cannot create private knowledge directory") from exc
        raise


def _harden_bound_entry(
    descriptor: int,
    signature: tuple[int, int],
    mode: int,
    *,
    directory: bool,
) -> int:
    before = os.fstat(descriptor)
    valid_type = stat.S_ISDIR(before.st_mode) if directory else (
        stat.S_ISREG(before.st_mode) and before.st_nlink == 1
    )
    if not valid_type or _entry_signature(before) != signature:
        raise KnowledgeError("private knowledge entry changed during init")
    previous = stat.S_IMODE(before.st_mode)
    changed = False
    try:
        os.fchmod(descriptor, mode)
        changed = True
        after = os.fstat(descriptor)
        if (
            _entry_signature(after) != signature
            or (not directory and after.st_nlink != 1)
            or stat.S_IMODE(after.st_mode) != mode
        ):
            raise KnowledgeError("private knowledge entry changed during init")
        return previous
    except BaseException as exc:
        if changed:
            try:
                current = os.fstat(descriptor)
                if (
                    _entry_signature(current) == signature
                    and (directory or current.st_nlink == 1)
                ):
                    os.fchmod(descriptor, previous)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise KnowledgeError("cannot secure private knowledge entry") from exc
        raise


def _open_and_harden_private_file_at(
    parent_fd: int,
    name: str,
    expected_signature: tuple[int, int],
    *,
    expected_content: bytes | None = None,
) -> tuple[int, int]:
    flags = os.O_RDONLY | _required_open_flag("O_NOFOLLOW")
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise KnowledgeError("cannot secure private knowledge file") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _entry_signature(opened) != expected_signature
        ):
            raise KnowledgeError("private knowledge file changed during init")
        _assert_bound_entry_stable(parent_fd, name, expected_signature, False)
        if expected_content is not None:
            payload = b""
            while len(payload) <= len(expected_content):
                chunk = os.read(
                    descriptor, min(65536, len(expected_content) + 1 - len(payload))
                )
                if not chunk:
                    break
                payload += chunk
            if payload != expected_content:
                raise KnowledgeError("knowledge boundary marker is invalid")
        previous = _harden_bound_entry(
            descriptor, expected_signature, 0o600, directory=False
        )
        _assert_bound_entry_stable(parent_fd, name, expected_signature, False)
        return descriptor, previous
    except BaseException:
        os.close(descriptor)
        raise


def _remove_bound_inode(
    parent_fd: int,
    preferred_name: str,
    signature: tuple[int, int],
    *,
    directory: bool,
) -> None:
    names = [preferred_name]
    try:
        names.extend(name for name in os.listdir(parent_fd) if name != preferred_name)
    except OSError:
        pass
    for name in names:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            valid_type = stat.S_ISDIR(current.st_mode) if directory else stat.S_ISREG(
                current.st_mode
            )
            if valid_type and _entry_signature(current) == signature:
                if directory:
                    os.rmdir(name, dir_fd=parent_fd)
                else:
                    os.unlink(name, dir_fd=parent_fd)
                return
        except OSError:
            continue


def _rollback_init_at(
    created: list[tuple[int, str, tuple[int, int], bool]],
    modes: list[tuple[int, tuple[int, int], int, bool]],
) -> None:
    for parent_fd, name, signature, directory in reversed(created):
        _remove_bound_inode(
            parent_fd, name, signature, directory=directory
        )
    for descriptor, signature, previous_mode, directory in reversed(modes):
        try:
            current = os.fstat(descriptor)
            if (
                _entry_signature(current) == signature
                and (directory or current.st_nlink == 1)
            ):
                os.fchmod(descriptor, previous_mode)
        except OSError:
            pass


def cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap a private local Vault from package-owned templates."""

    if not SAFE_BOOTSTRAP_PROJECT.fullmatch(args.project):
        raise KnowledgeError("project must be a non-sensitive identifier")
    # A mutating bootstrap never inherits its destination from the process
    # environment.  Normal read/query commands still honor QINGTIAN_CONFIG.
    if getattr(args, "config_explicit", False):
        requested_config_path = Path(os.path.abspath(Path(args.config).expanduser()))
    else:
        requested_config_path = Path.cwd() / "config" / "sources.json"
    if (
        requested_config_path.name != "sources.json"
        or requested_config_path.parent.name != "config"
    ):
        raise KnowledgeError("init --config must be <knowledge-root>/config/sources.json")
    requested_knowledge_root = requested_config_path.parent.parent
    try:
        root_state = os.lstat(requested_knowledge_root)
    except OSError as exc:
        raise KnowledgeError("knowledge root must be an existing directory") from exc
    if stat.S_ISLNK(root_state.st_mode) or not stat.S_ISDIR(root_state.st_mode):
        raise KnowledgeError("knowledge root must be a real directory")
    knowledge_root = requested_knowledge_root.resolve(strict=True)
    config_path = knowledge_root / "config" / "sources.json"
    vault_path = knowledge_root / "vault"

    root_fd: int | None = None
    held_fds: list[int] = []
    created: list[tuple[int, str, tuple[int, int], bool]] = []
    modes: list[tuple[int, tuple[int, int], int, bool]] = []
    bindings: list[tuple[int, str, tuple[int, int], bool]] = []
    try:
        root_fd = os.open(requested_knowledge_root, _directory_open_flags())
        held_fds.append(root_fd)
        opened_root = os.fstat(root_fd)
        root_signature = _entry_signature(opened_root)
        canonical_root = os.stat(knowledge_root, follow_symlinks=False)
        if (
            root_signature != _entry_signature(root_state)
            or root_signature != _entry_signature(canonical_root)
        ):
            raise KnowledgeError("knowledge root changed during init")
        _init_stage_checkpoint(
            "root-opened",
            requested_knowledge_root,
            root_fd,
            root_signature,
            tuple(bindings),
        )

        try:
            raw_workspace = Path(os.path.abspath(Path(args.workspace).expanduser()))
            workspace_state = os.lstat(raw_workspace)
            workspace = raw_workspace.resolve(strict=True)
        except OSError as exc:
            raise KnowledgeError("workspace does not exist") from exc
        if (
            stat.S_ISLNK(workspace_state.st_mode)
            or not stat.S_ISDIR(workspace_state.st_mode)
            or not workspace.is_dir()
            or workspace == knowledge_root
        ):
            raise KnowledgeError("workspace must be an existing source directory")

        # Read every package input before the first mkdir, write, or chmod.
        marker_text = _read_bootstrap_resource("knowledge-root-marker.txt")
        marker_content = marker_text.encode("utf-8")
        home_content = _read_bootstrap_resource("home.md")
        obsidian_content = _read_bootstrap_resource("obsidian-app.json")
        try:
            obsidian_payload = json.loads(obsidian_content)
        except json.JSONDecodeError as exc:
            raise KnowledgeError("packaged Obsidian configuration is invalid") from exc
        if not isinstance(obsidian_payload, dict):
            raise KnowledgeError("packaged Obsidian configuration is invalid")
        try:
            template = json.loads(_read_bootstrap_resource("sources.example.json"))
        except json.JSONDecodeError as exc:
            raise KnowledgeError("packaged example configuration is invalid") from exc
        template["workspace_root"] = os.path.relpath(workspace, config_path.parent)
        source_sets = template.get("source_sets")
        if (
            not isinstance(source_sets, list)
            or not source_sets
            or not isinstance(source_sets[0], dict)
        ):
            raise KnowledgeError("packaged example configuration has no source profile")
        source_sets[0]["project"] = args.project
        config_content = (
            json.dumps(template, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )

        def open_existing_directory(
            parent_fd: int, name: str
        ) -> tuple[int, tuple[int, int]] | None:
            value = _open_existing_private_directory_at(parent_fd, name)
            if value is not None:
                descriptor, signature = value
                held_fds.append(descriptor)
                bindings.append((parent_fd, name, signature, True))
            return value

        config_directory = open_existing_directory(root_fd, "config")
        vault_directory = open_existing_directory(root_fd, "vault")
        home_directory = (
            open_existing_directory(vault_directory[0], "00-Home")
            if vault_directory is not None
            else None
        )
        obsidian_directory = (
            open_existing_directory(vault_directory[0], ".obsidian")
            if vault_directory is not None
            else None
        )
        if config_directory is not None and _entry_exists_at(
            config_directory[0], "sources.json"
        ):
            raise KnowledgeError("local sources configuration already exists")

        marker_existing = _inspect_private_file_at(
            root_fd, ".qingtian-knowledge-root", max_bytes=128
        )
        if (
            marker_existing is not None
            and marker_existing[1].strip() != marker_content.strip()
        ):
            raise KnowledgeError("knowledge boundary marker is invalid")
        if marker_existing is not None:
            bindings.append(
                (root_fd, ".qingtian-knowledge-root", marker_existing[0], False)
            )
        home_existing = (
            _inspect_private_file_at(home_directory[0], "Home.md")
            if home_directory is not None
            else None
        )
        if home_existing is not None and home_directory is not None:
            bindings.append((home_directory[0], "Home.md", home_existing[0], False))
        app_existing = (
            _inspect_private_file_at(obsidian_directory[0], "app.json")
            if obsidian_directory is not None
            else None
        )
        if app_existing is not None and obsidian_directory is not None:
            bindings.append(
                (obsidian_directory[0], "app.json", app_existing[0], False)
            )

        _init_stage_checkpoint(
            "preflight-complete",
            requested_knowledge_root,
            root_fd,
            root_signature,
            tuple(bindings),
        )

        if marker_existing is None:
            signature = _write_new_private_file_at(
                root_fd, ".qingtian-knowledge-root", marker_text
            )
            created.append((root_fd, ".qingtian-knowledge-root", signature, False))
            bindings.append((root_fd, ".qingtian-knowledge-root", signature, False))
        else:
            descriptor, previous = _open_and_harden_private_file_at(
                root_fd,
                ".qingtian-knowledge-root",
                marker_existing[0],
                expected_content=marker_content,
            )
            held_fds.append(descriptor)
            modes.append((descriptor, marker_existing[0], previous, False))
        _init_stage_checkpoint(
            "marker",
            requested_knowledge_root,
            root_fd,
            root_signature,
            tuple(bindings),
        )

        def ensure_directory(
            existing: tuple[int, tuple[int, int]] | None,
            parent_fd: int,
            name: str,
            stage: str,
        ) -> tuple[int, tuple[int, int]]:
            if existing is None:
                descriptor, signature = _create_private_directory_at(parent_fd, name)
                held_fds.append(descriptor)
                created.append((parent_fd, name, signature, True))
                bindings.append((parent_fd, name, signature, True))
            else:
                descriptor, signature = existing
                previous = _harden_bound_entry(
                    descriptor, signature, 0o700, directory=True
                )
                modes.append((descriptor, signature, previous, True))
            _init_stage_checkpoint(
                stage,
                requested_knowledge_root,
                root_fd,
                root_signature,
                tuple(bindings),
            )
            return descriptor, signature

        config_directory = ensure_directory(
            config_directory, root_fd, "config", "config-dir"
        )
        vault_directory = ensure_directory(
            vault_directory, root_fd, "vault", "vault-dir"
        )
        home_directory = ensure_directory(
            home_directory, vault_directory[0], "00-Home", "home-dir"
        )
        obsidian_directory = ensure_directory(
            obsidian_directory, vault_directory[0], ".obsidian", "obsidian-dir"
        )

        if home_existing is None:
            signature = _write_new_private_file_at(
                home_directory[0], "Home.md", home_content
            )
            created.append((home_directory[0], "Home.md", signature, False))
            bindings.append((home_directory[0], "Home.md", signature, False))
        else:
            descriptor, previous = _open_and_harden_private_file_at(
                home_directory[0], "Home.md", home_existing[0]
            )
            held_fds.append(descriptor)
            modes.append((descriptor, home_existing[0], previous, False))
        _init_stage_checkpoint(
            "home-file",
            requested_knowledge_root,
            root_fd,
            root_signature,
            tuple(bindings),
        )

        if app_existing is None:
            signature = _write_new_private_file_at(
                obsidian_directory[0], "app.json", obsidian_content
            )
            created.append((obsidian_directory[0], "app.json", signature, False))
            bindings.append((obsidian_directory[0], "app.json", signature, False))
        else:
            descriptor, previous = _open_and_harden_private_file_at(
                obsidian_directory[0], "app.json", app_existing[0]
            )
            held_fds.append(descriptor)
            modes.append((descriptor, app_existing[0], previous, False))
        _init_stage_checkpoint(
            "app-file",
            requested_knowledge_root,
            root_fd,
            root_signature,
            tuple(bindings),
        )

        signature = _write_new_private_file_at(
            config_directory[0], "sources.json", config_content
        )
        created.append((config_directory[0], "sources.json", signature, False))
        bindings.append((config_directory[0], "sources.json", signature, False))
        _init_stage_checkpoint(
            "config-file",
            requested_knowledge_root,
            root_fd,
            root_signature,
            tuple(bindings),
        )
        _init_stage_checkpoint(
            "before-commit",
            requested_knowledge_root,
            root_fd,
            root_signature,
            tuple(bindings),
        )
    except BaseException:
        _rollback_init_at(created, modes)
        raise
    else:
        emit(
            {
                "status": "initialized",
                "classification": "P1-internal-local",
                "config": str(config_path),
                "vault": str(vault_path),
                "next_steps": [
                    shlex.join(
                        ["qingtian-kb", "--config", str(config_path), "doctor"]
                    ),
                    shlex.join(
                        ["qingtian-kb", "--config", str(config_path), "ingest"]
                    ),
                ],
            }
        )
        return 0
    finally:
        for descriptor in reversed(held_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def cmd_doctor(args: argparse.Namespace) -> int:
    engine = with_engine(args, index_mode="none")
    try:
        result = {
            "status": "ok",
            "version": __version__,
            "extractor": EXTRACTOR_VERSION,
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "fts5": sqlite_fts5_available(),
            "state_initialized": engine.state_path.is_file(),
            "workspace": str(engine.workspace),
            "vault": str(engine.vault),
            "obsidian_installed": Path("/Applications/Obsidian.app").exists(),
            "mode": "read-only-sources/non-destructive-vault",
        }
    finally:
        engine.close()
    emit(result)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    engine = with_engine(args, index_mode="none")
    try:
        result = engine.plan()
    finally:
        engine.close()
    emit(result)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    engine = with_engine(args)
    try:
        receipt = engine.ingest()
        emit(receipt)
        return 0 if receipt["result"] == "passed" else 1
    finally:
        engine.close()


def cmd_search(args: argparse.Namespace) -> int:
    engine = with_engine(args, index_mode="read")
    try:
        freshness = args.freshness or ["current"]
        review_statuses = args.review_status or ["approved"]
        result = {
            "query": args.query,
            "include_historical": args.include_historical,
            "classification_ceiling": args.classification_ceiling,
            "results": engine.index.search(
                args.query,
                args.limit,
                projects=args.project,
                review_statuses=review_statuses,
                freshness=freshness,
                classification_ceiling=args.classification_ceiling,
                include_historical=args.include_historical,
            ),
        }
    finally:
        engine.close()
    emit(result)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    engine = with_engine(args, index_mode="read")
    try:
        result = engine.validate_vault()
    finally:
        engine.close()
    emit(result)
    return 0 if result["status"] == "passed" else 1


def cmd_stats(args: argparse.Namespace) -> int:
    engine = with_engine(args, index_mode="read")
    try:
        result = engine.stats()
    finally:
        engine.close()
    emit(result)
    return 0


def cmd_provider_query(args: argparse.Namespace) -> int:
    raw_request = sys.stdin.read(65537)
    if not raw_request.strip() or len(raw_request) > 65536:
        raise KnowledgeError("provider request on stdin is missing or too large")
    stripped = raw_request.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise KnowledgeError("provider request JSON is invalid") from exc
        allowed = {
            "schema_version",
            "query",
            "caller_id",
            "purpose",
            "retrieval_modes",
            "projects",
            "top_k",
            "request_id",
        }
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise KnowledgeError("provider request JSON has an invalid shape")
        if payload.get("schema_version") != "1.0":
            raise KnowledgeError("provider request schema is unsupported")
        if any(
            value is not None
            for value in (
                args.caller_id,
                args.purpose,
                args.mode,
                args.project,
                args.top_k,
                args.request_id,
            )
        ):
            raise KnowledgeError("provider request must not mix JSON and CLI metadata")
        query = payload.get("query")
        caller_id = payload.get("caller_id")
        purpose = payload.get("purpose")
        modes = payload.get("retrieval_modes", ["approved"])
        projects = payload.get("projects", [])
        top_k = payload.get("top_k", 10)
        request_id = payload.get("request_id")
        if (
            not isinstance(query, str)
            or not isinstance(caller_id, str)
            or not isinstance(purpose, str)
            or not isinstance(modes, list)
            or not modes
            or not all(isinstance(value, str) for value in modes)
            or len(set(modes)) != len(modes)
            or not isinstance(projects, list)
            or not all(isinstance(value, str) for value in projects)
            or len(set(projects)) != len(projects)
            or isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or (
                "request_id" in payload
                and (
                    not isinstance(request_id, str)
                    or SAFE_IDENTIFIER.fullmatch(request_id) is None
                )
            )
        ):
            raise KnowledgeError("provider request JSON has an invalid shape")
    else:
        if args.caller_id is None or args.purpose is None:
            raise KnowledgeError(
                "raw stdin queries require --caller-id and --purpose metadata"
            )
        query = stripped
        caller_id = args.caller_id
        purpose = args.purpose
        modes = args.mode or ["approved"]
        projects = args.project or []
        top_k = args.top_k if args.top_k is not None else 10
        request_id = args.request_id
    request = ProviderRequest(
        query=query,
        caller_id=caller_id,
        purpose=purpose,
        modes=tuple(modes),
        projects=tuple(projects),
        top_k=top_k,
        request_id=request_id,
    )
    with QingtianKnowledgeProvider(args.config) as provider:
        emit(provider.query(request))
    return 0


def cmd_restore_baseline(args: argparse.Namespace) -> int:
    engine = with_engine(args)
    try:
        result = engine.restore_managed_baseline()
        emit(result)
        return 0 if result["status"] == "restored" else 1
    finally:
        engine.close()


def cmd_open(args: argparse.Namespace) -> int:
    engine = with_engine(args, index_mode="none")
    try:
        if sys.platform != "darwin":
            raise KnowledgeError("open-vault is currently implemented for macOS only")
        subprocess.run(
            ["open", "-a", "Obsidian", str(engine.vault)],
            stdin=subprocess.DEVNULL,
            check=True,
            env={key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ},
        )
        emit({"status": "opened", "vault": str(engine.vault)})
        return 0
    finally:
        engine.close()


def parser() -> argparse.ArgumentParser:
    root = NonEchoingArgumentParser(
        prog="qingtian-kb",
        description="Traceable, non-destructive local knowledge ingestion",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument(
        "--config",
        default=str(default_config_path()),
        action=ConfigPathAction,
        help=(
            "Path to sources.json (default: QINGTIAN_CONFIG or "
            "./config/sources.json; init ignores QINGTIAN_CONFIG unless --config is explicit)"
        ),
    )
    root.set_defaults(config_explicit=False)
    commands = root.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser(
        "init", help="Create a private local config and minimal Obsidian Vault without overwriting"
    )
    initialize.add_argument("--workspace", required=True)
    initialize.add_argument("--project", default="project-template")
    initialize.set_defaults(func=cmd_init)
    commands.add_parser("doctor").set_defaults(func=cmd_doctor)
    commands.add_parser("plan").set_defaults(func=cmd_plan)
    commands.add_parser("ingest").set_defaults(func=cmd_ingest)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--project", action="append", default=[])
    search.add_argument("--review-status", action="append", default=[])
    search.add_argument(
        "--freshness",
        action="append",
        choices=["current", "unknown", "stale", "review-due", "expired"],
        default=[],
    )
    search.add_argument(
        "--classification-ceiling",
        choices=["P0-public", "P1-internal", "P2-confidential", "P3-restricted"],
        default="P1-internal",
        help="Caller-side filter only; this is not authentication or an ACL.",
    )
    search.add_argument(
        "--include-historical",
        action="store_true",
        help="Include E1/history leads; results remain non-acceptance evidence.",
    )
    search.set_defaults(func=cmd_search)
    commands.add_parser("validate").set_defaults(func=cmd_validate)
    commands.add_parser("stats").set_defaults(func=cmd_stats)
    provider = commands.add_parser(
        "provider-query",
        help=(
            "Read-only Qingtian context contract; reads a JSON request or raw query from stdin."
        ),
    )
    provider.add_argument("--caller-id")
    provider.add_argument(
        "--purpose",
        choices=["human-research", "agent-context", "test"],
    )
    provider.add_argument(
        "--mode",
        action="append",
        choices=["approved", "candidate", "history"],
        default=None,
    )
    provider.add_argument("--project", action="append", default=None)
    provider.add_argument("--top-k", type=int, default=None)
    provider.add_argument("--request-id")
    provider.set_defaults(func=cmd_provider_query)
    commands.add_parser("restore-baseline").set_defaults(func=cmd_restore_baseline)
    commands.add_parser("open-vault").set_defaults(func=cmd_open)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return int(args.func(args))
    except (KnowledgeError, OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        emit({"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 2
