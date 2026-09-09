"""Explicit local project registry for repository-backed Qingtian tasks.

The registry lives in the runtime data directory, never in the installed source
tree. A missing registry is an empty allowlist: repository execution must not
fall back to a remembered task path, a basename guess, or the current working
directory.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from uuid import uuid4

from .config import default_data_dir


PROJECT_CONFIG_ENV = "QINGTIAN_PROJECTS_CONFIG"
PROJECT_CONFIG_NAME = "projects.local.json"
PROTECTED_BASE_BRANCHES = {"main", "master", "origin/main", "origin/master"}
NAME_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
ROLE_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
BASE_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
MAX_CONFIG_BYTES = 256 * 1024
SOURCE_ROOT = Path(__file__).resolve().parent.parent


class ProjectConfigError(ValueError):
    """Stable configuration failure without leaking repository contents."""


def _data_root(data_dir: Optional[Path]) -> Path:
    return Path(data_dir or default_data_dir()).expanduser().resolve()


def project_config_path(
    *, data_dir: Optional[Path] = None, path: Optional[Path] = None
) -> Path:
    if path is not None:
        configured = Path(path).expanduser()
    else:
        override = os.environ.get(PROJECT_CONFIG_ENV)
        if override is not None:
            if not override.strip():
                raise ProjectConfigError("project config override is empty")
            configured = Path(override).expanduser()
        else:
            configured = _data_root(data_dir) / "config" / PROJECT_CONFIG_NAME
    if not configured.is_absolute():
        raise ProjectConfigError("project config path must be absolute")
    # Preserve the final path component so _read_document can reject a symlink
    # with O_NOFOLLOW instead of silently following it during normalization.
    return Path(os.path.abspath(configured))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _read_document(path: Path) -> Optional[dict[str, Any]]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ProjectConfigError("cannot read project config") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CONFIG_BYTES:
            raise ProjectConfigError("project config must be a bounded regular file")
        raw = os.read(descriptor, MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ProjectConfigError("cannot read project config") from None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ProjectConfigError("project config is too large")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ProjectConfigError("project config is not valid JSON") from None
    if not isinstance(value, dict):
        raise ProjectConfigError("project config must be an object")
    return value


def _safe_slug(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ProjectConfigError("{} must be a lowercase slug".format(label))
    return value


def _safe_base_branch(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError("{} must be a non-empty Git ref".format(label))
    branch = value.strip()
    if (
        BASE_BRANCH_PATTERN.fullmatch(branch) is None
        or ".." in branch
        or "//" in branch
        or branch.endswith(("/", ".", ".lock"))
        or branch.lower() in PROTECTED_BASE_BRANCHES
    ):
        raise ProjectConfigError("{} is not an allowed base branch".format(label))
    return branch


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _validate_repository(path_value: Any, data_root: Path, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise ProjectConfigError("{} must be an absolute Git repository".format(label))
    untrusted = Path(path_value).expanduser()
    if not untrusted.is_absolute():
        raise ProjectConfigError("{} must be an absolute Git repository".format(label))
    repository = untrusted.resolve()
    lowered_parts = {part.lower() for part in repository.parts}
    if "site-packages" in lowered_parts or "dist-packages" in lowered_parts:
        raise ProjectConfigError("{} cannot be inside an installed package".format(label))
    if _inside(repository, data_root):
        raise ProjectConfigError("{} cannot be inside the engine data directory".format(label))
    if _inside(repository, SOURCE_ROOT):
        raise ProjectConfigError("{} cannot be the Qingtian source tree".format(label))
    if not repository.is_dir() or not (repository / ".git").exists():
        raise ProjectConfigError("{} is not a Git repository".format(label))
    return repository


def _validate_scope(repository: Path, value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError("{} must be a non-empty relative path".format(label))
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProjectConfigError("{} must stay inside the repository".format(label))
    resolved = (repository / relative).resolve()
    if not _inside(resolved, repository) or not resolved.exists():
        raise ProjectConfigError("{} does not resolve inside the repository".format(label))
    return relative.as_posix()


@dataclass(frozen=True)
class ProjectRepository:
    name: str
    repository: Path
    base_branch: str
    roles: tuple[str, ...]
    scope: Optional[str] = None

    @property
    def scope_path(self) -> Optional[Path]:
        return self.repository / self.scope if self.scope else None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "repository": str(self.repository),
            "base_branch": self.base_branch,
            "roles": list(self.roles),
        }
        if self.scope is not None:
            value["scope"] = self.scope
        return value


@dataclass(frozen=True)
class ProjectConfig:
    source_path: Path
    data_root: Path
    projects: Dict[str, ProjectRepository]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "projects": {
                name: project.to_dict()
                for name, project in sorted(self.projects.items())
            },
        }

    def targets_for_roles(self, roles: Iterable[str]) -> list[ProjectRepository]:
        requested = {str(role).lower() for role in roles}
        return [
            project
            for project in self.projects.values()
            if requested.intersection(project.roles)
        ]

    def resolve_reference(self, reference: str) -> Optional[ProjectRepository]:
        if reference in self.projects:
            return self.projects[reference]
        candidate = Path(reference).expanduser()
        if not candidate.is_absolute():
            return None
        resolved = candidate.resolve()
        matches = [
            project
            for project in self.projects.values()
            if project.repository == resolved
        ]
        return matches[0] if len(matches) == 1 else None


def _parse_config(
    document: dict[str, Any], *, source_path: Path, data_root: Path
) -> ProjectConfig:
    if set(document) != {"schema_version", "projects"}:
        raise ProjectConfigError("project config fields are invalid")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ProjectConfigError("schema_version must be 1")
    raw_projects = document.get("projects")
    if not isinstance(raw_projects, dict):
        raise ProjectConfigError("projects must be an object")

    projects: Dict[str, ProjectRepository] = {}
    repository_branches: Dict[Path, str] = {}
    for raw_name, raw_project in raw_projects.items():
        name = _safe_slug(raw_name, "project name", NAME_PATTERN)
        if (
            not isinstance(raw_project, dict)
            or not {"repository", "base_branch", "roles"}.issubset(raw_project)
            or set(raw_project) - {"repository", "base_branch", "roles", "scope"}
        ):
            raise ProjectConfigError("project {} fields are invalid".format(name))
        repository = _validate_repository(
            raw_project["repository"], data_root, "project {} repository".format(name)
        )
        base_branch = _safe_base_branch(
            raw_project["base_branch"], "project {} base_branch".format(name)
        )
        raw_roles = raw_project["roles"]
        if not isinstance(raw_roles, list) or len(raw_roles) != len(set(raw_roles)):
            raise ProjectConfigError("project {} roles must be a unique list".format(name))
        roles = tuple(
            _safe_slug(role, "project {} role".format(name), ROLE_PATTERN)
            for role in raw_roles
        )
        scope = _validate_scope(
            repository, raw_project.get("scope"), "project {} scope".format(name)
        )
        previous = repository_branches.get(repository)
        if previous is not None and previous != base_branch:
            raise ProjectConfigError(
                "one repository cannot use conflicting base branches"
            )
        repository_branches[repository] = base_branch
        projects[name] = ProjectRepository(
            name=name,
            repository=repository,
            base_branch=base_branch,
            roles=roles,
            scope=scope,
        )
    return ProjectConfig(source_path=source_path, data_root=data_root, projects=projects)


def load_project_config(
    *, data_dir: Optional[Path] = None, path: Optional[Path] = None
) -> ProjectConfig:
    data_root = _data_root(data_dir)
    source_path = project_config_path(data_dir=data_root, path=path)
    document = _read_document(source_path)
    if document is None:
        return ProjectConfig(source_path=source_path, data_root=data_root, projects={})
    return _parse_config(document, source_path=source_path, data_root=data_root)


@contextmanager
def _config_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name("." + path.name + ".lock")
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        raise ProjectConfigError("cannot lock project config") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ProjectConfigError("project config lock must be a regular file")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8", closefd=False) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
    finally:
        os.close(descriptor)


def _write_config_locked(config: ProjectConfig) -> None:
    path = config.source_path
    temporary = path.with_name(".{}.{}.tmp".format(path.name, uuid4().hex))
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        payload = (
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_locked_config(source_path: Path, data_root: Path) -> ProjectConfig:
    document = _read_document(source_path)
    if document is None:
        return ProjectConfig(
            source_path=source_path, data_root=data_root, projects={}
        )
    return _parse_config(document, source_path=source_path, data_root=data_root)


def register_project(
    name: str,
    repository: Path,
    base_branch: str,
    roles: Iterable[str],
    scope: Optional[str] = None,
    *,
    data_dir: Optional[Path] = None,
) -> ProjectConfig:
    data_root = _data_root(data_dir)
    source_path = project_config_path(data_dir=data_root)
    with _config_lock(source_path):
        config = _load_locked_config(source_path, data_root)
        document = config.to_dict()
        document["projects"][name] = {
            "repository": str(Path(repository).expanduser().resolve()),
            "base_branch": base_branch,
            "roles": list(roles),
            **({"scope": scope} if scope is not None else {}),
        }
        updated = _parse_config(
            document, source_path=config.source_path, data_root=config.data_root
        )
        _write_config_locked(updated)
        return updated


def remove_project(name: str, *, data_dir: Optional[Path] = None) -> ProjectConfig:
    data_root = _data_root(data_dir)
    source_path = project_config_path(data_dir=data_root)
    with _config_lock(source_path):
        config = _load_locked_config(source_path, data_root)
        if name not in config.projects:
            raise ProjectConfigError("project is not registered")
        document = config.to_dict()
        del document["projects"][name]
        updated = _parse_config(
            document, source_path=config.source_path, data_root=config.data_root
        )
        _write_config_locked(updated)
        return updated
