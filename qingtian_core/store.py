from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any, Iterator, Sequence
from urllib.parse import quote
from uuid import uuid4

from .models import (
    ConflictError,
    NotFoundError,
    RUN_TRANSITIONS,
    SESSION_TRANSITIONS,
    TASK_TRANSITIONS,
    RunState,
    SessionState,
    StorageContractError,
    TaskState,
    TransitionError,
    canonical_json,
    content_hash,
    require_transition,
    utc_now,
)


SCHEMA_VERSION = 1
SCHEMA_CONTRACT = "qingtian-sqlite-v1-20260905-a"
CLASSIFICATION_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
KNOWLEDGE_KINDS = {"source", "decision", "observation", "procedure", "incident", "constraint"}
KNOWLEDGE_REVIEW_STATES = {"unreviewed", "reviewed", "rejected", "superseded"}
EVIDENCE_SUBJECT_TYPES = {"task", "session", "run", "release", "artifact"}
EVIDENCE_RESULTS = {"passed", "failed", "blocked", "unknown", "observed"}


class QingtianStore:
    """SQLite authority for the portable, single-host Qingtian profile.

    SQLite transactions provide local CAS and crash-safe state. This class does
    not claim distributed fencing or exactly-once behavior for external tools.
    """

    def __init__(self, database: str | Path):
        self.database = ":memory:" if str(database) == ":memory:" else str(
            self._absolute_database_path(database)
        )
        connection: sqlite3.Connection | None = None
        parent_descriptor: int | None = None
        database_descriptor: int | None = None
        database_name: str | None = None
        expected_identity: tuple[int, int] | None = None
        created_here = False
        try:
            if self.database == ":memory:":
                connection = sqlite3.connect(
                    self.database, timeout=30, isolation_level=None
                )
            else:
                database_path = Path(self.database)
                parent_descriptor = self._open_secure_parent(
                    database_path, create_missing=True
                )
                database_name = database_path.name
                database_descriptor, created_here = self._pin_database_file(
                    parent_descriptor, database_name
                )
                pinned = os.fstat(database_descriptor)
                expected_identity = (pinned.st_dev, pinned.st_ino)
                if not created_here:
                    self._preflight_existing_database(
                        database_path, expected_identity=expected_identity
                    )
                uri_path = quote(str(database_path), safe="/")
                connection = self._connect_bound_database(
                    f"file:{uri_path}?mode=rw",
                    expected_identity=expected_identity,
                    uri=True,
                    timeout=30,
                    isolation_level=None,
                )
                self._require_current_path_identity(
                    database_path,
                    expected_parent=os.fstat(parent_descriptor),
                    expected_file=expected_identity,
                )
            connection.row_factory = sqlite3.Row
            self._validate_connection_contract(connection)
            connection.execute("PRAGMA foreign_keys = ON")
            if database_descriptor is not None:
                os.fchmod(database_descriptor, 0o600)
            connection.execute("PRAGMA journal_mode = WAL")
        except Exception:
            if connection is not None:
                connection.close()
            if (
                created_here
                and parent_descriptor is not None
                and database_name is not None
                and expected_identity is not None
            ):
                self._remove_created_database(
                    parent_descriptor,
                    database_name,
                    expected_identity=expected_identity,
                )
            raise
        finally:
            if database_descriptor is not None:
                os.close(database_descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)
        self.connection = connection
        self.fts_enabled = False

    @staticmethod
    def _absolute_database_path(database: str | Path) -> Path:
        raw = os.fspath(database)
        if not raw or "\x00" in raw:
            raise StorageContractError("database path must be a non-empty filesystem path")
        path = Path(os.path.abspath(raw))
        # macOS exposes these stable system aliases as symlinks. Canonicalize only
        # these OS-owned prefixes; every remaining ancestor is opened O_NOFOLLOW.
        if sys.platform == "darwin" and len(path.parts) > 1:
            aliases = {"var": ("private", "var"), "tmp": ("private", "tmp")}
            replacement = aliases.get(path.parts[1])
            if replacement is not None:
                path = Path("/", *replacement, *path.parts[2:])
        if path.name in {"", ".", ".."}:
            raise StorageContractError("database path must name a file")
        return path

    @staticmethod
    def _open_secure_parent(
        database_path: Path, *, create_missing: bool = False
    ) -> int:
        if os.name != "posix":
            raise StorageContractError(
                "secure file-backed databases require a POSIX runtime"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open("/", flags)
        try:
            for component in database_path.parts[1:-1]:
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create_missing:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError as exc:
            os.close(descriptor)
            raise StorageContractError(
                "database ancestors must be existing real directories without symbolic links"
            ) from exc
        return descriptor

    @staticmethod
    def _validate_pinned_file(metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise StorageContractError("database path must be a regular file")
        if metadata.st_nlink != 1:
            raise StorageContractError("database file must have exactly one hard link")

    @classmethod
    def _pin_database_file(cls, parent_descriptor: int, name: str) -> tuple[int, bool]:
        common_flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, os.O_RDONLY | common_flags, dir_fd=parent_descriptor)
            created = False
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise StorageContractError("database path must not be a symbolic link") from exc
            if exc.errno != errno.ENOENT:
                raise StorageContractError("unable to securely open database file") from exc
            try:
                descriptor = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | common_flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                created = True
            except OSError as create_error:
                if create_error.errno == errno.ELOOP:
                    raise StorageContractError(
                        "database path must not be a symbolic link"
                    ) from create_error
                raise StorageContractError(
                    "unable to securely create database file"
                ) from create_error
        metadata = os.fstat(descriptor)
        try:
            cls._validate_pinned_file(metadata)
        except Exception:
            os.close(descriptor)
            if created:
                cls._remove_created_database(
                    parent_descriptor,
                    name,
                    expected_identity=(metadata.st_dev, metadata.st_ino),
                )
            raise
        return descriptor, created

    @staticmethod
    def _open_fd_snapshot() -> dict[int, tuple[int, int, int]]:
        fd_directory = next(
            (Path(value) for value in ("/proc/self/fd", "/dev/fd") if Path(value).is_dir()),
            None,
        )
        if fd_directory is None:
            raise StorageContractError("runtime cannot enumerate persistent file descriptors")
        snapshot: dict[int, tuple[int, int, int]] = {}
        for value in os.listdir(fd_directory):
            if not value.isdigit():
                continue
            descriptor = int(value)
            try:
                metadata = os.fstat(descriptor)
            except OSError:
                continue
            snapshot[descriptor] = (metadata.st_dev, metadata.st_ino, metadata.st_mode)
        return snapshot

    @classmethod
    def _connect_bound_database(
        cls,
        database: str,
        *,
        expected_identity: tuple[int, int],
        **options: Any,
    ) -> sqlite3.Connection:
        before = cls._open_fd_snapshot()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database, **options)
            after = cls._open_fd_snapshot()
            matches = [
                descriptor
                for descriptor, identity in after.items()
                if before.get(descriptor) != identity
                and stat.S_ISREG(identity[2])
                and identity[:2] == expected_identity
            ]
            if not matches:
                raise StorageContractError(
                    "SQLite connection is not bound to the pinned database inode"
                )
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise

    @classmethod
    def _require_current_path_identity(
        cls,
        database_path: Path,
        *,
        expected_parent: os.stat_result,
        expected_file: tuple[int, int],
    ) -> None:
        parent_descriptor = cls._open_secure_parent(database_path)
        file_descriptor: int | None = None
        try:
            parent = os.fstat(parent_descriptor)
            if (parent.st_dev, parent.st_ino) != (
                expected_parent.st_dev,
                expected_parent.st_ino,
            ):
                raise StorageContractError(
                    "database parent changed while SQLite was connecting"
                )
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                file_descriptor = os.open(
                    database_path.name, flags, dir_fd=parent_descriptor
                )
            except OSError as exc:
                raise StorageContractError(
                    "database path disappeared or became unsafe while SQLite was connecting"
                ) from exc
            current = os.fstat(file_descriptor)
            cls._validate_pinned_file(current)
            if (current.st_dev, current.st_ino) != expected_file:
                raise StorageContractError(
                    "database path changed while SQLite was connecting"
                )
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            os.close(parent_descriptor)

    @classmethod
    def _remove_created_database(
        cls,
        parent_descriptor: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
    ) -> None:
        descriptor: int | None = None
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) == expected_identity:
                os.unlink(name, dir_fd=parent_descriptor)
        except (OSError, StorageContractError):
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _contract_metadata(connection: sqlite3.Connection) -> tuple[set[str], dict[str, str]]:
        try:
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            metadata = (
                dict(connection.execute("SELECT key, value FROM metadata").fetchall())
                if "metadata" in existing_tables
                else {}
            )
        except sqlite3.DatabaseError as exc:
            raise StorageContractError("invalid database metadata schema") from exc
        return existing_tables, metadata

    @classmethod
    def _validate_connection_contract(cls, connection: sqlite3.Connection) -> None:
        existing_tables, metadata = cls._contract_metadata(connection)
        if "metadata" in existing_tables:
            if metadata.get("schema_version") != str(SCHEMA_VERSION):
                raise StorageContractError(
                    f"unsupported database schema {metadata.get('schema_version')!r}; "
                    f"expected {SCHEMA_VERSION}"
                )
            if metadata.get("schema_contract") != SCHEMA_CONTRACT:
                raise StorageContractError(
                    "unsupported database schema contract; 0.2.0 does not migrate "
                    "pre-release or modified databases in place"
                )
        elif existing_tables:
            raise StorageContractError("refusing to initialize an unversioned non-empty database")

    @classmethod
    def _preflight_existing_database(
        cls, database_path: Path, *, expected_identity: tuple[int, int]
    ) -> None:
        uri_path = quote(str(database_path), safe="/")
        connection: sqlite3.Connection | None = None
        try:
            connection = cls._connect_bound_database(
                f"file:{uri_path}?mode=ro&immutable=1",
                expected_identity=expected_identity,
                uri=True,
                timeout=30,
                isolation_level=None,
            )
            cls._validate_connection_contract(connection)
        except StorageContractError:
            raise
        except sqlite3.DatabaseError as exc:
            raise StorageContractError("database is not a readable SQLite authority") from exc
        finally:
            if connection is not None:
                connection.close()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "QingtianStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def initialize(self) -> None:
        self._validate_connection_contract(self.connection)
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    acceptance_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_project_idx ON tasks(project_id, updated_at);
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    host_alias TEXT NOT NULL,
                    context_revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    source_checkpoint_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    input_revision INTEGER NOT NULL,
                    executor TEXT NOT NULL,
                    state TEXT NOT NULL,
                    external_operation_id TEXT,
                    idempotency_key TEXT,
                    request_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, executor, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    result TEXT NOT NULL,
                    artifact_hash TEXT,
                    metadata_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS evidence_subject_idx
                    ON evidence(subject_type, subject_id, observed_at);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    task_revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    supersedes TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge (
                    knowledge_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_revision TEXT,
                    evidence_label TEXT NOT NULL,
                    review_state TEXT NOT NULL,
                    valid_until TEXT,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS knowledge_project_idx
                    ON knowledge(project_id, scope, updated_at);
                """
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_contract', ?)",
                (SCHEMA_CONTRACT,),
            )
        try:
            self.connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts "
                "USING fts5(knowledge_id UNINDEXED, title, content, tokenize='unicode61')"
            )
            with self.transaction() as db:
                db.execute("DELETE FROM knowledge_fts")
                db.execute(
                    """INSERT INTO knowledge_fts(knowledge_id, title, content)
                    SELECT knowledge_id, title, content FROM knowledge"""
                )
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"

    @staticmethod
    def _validated_id(prefix: str, value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(
            rf"{re.escape(prefix)}_[A-Za-z0-9_-]+", value
        ) is None:
            raise ValueError(f"invalid {prefix}_id")
        return value

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for key in ("scope_json", "acceptance_json", "metadata_json", "snapshot_json"):
            if key in item:
                item[key.removesuffix("_json")] = json.loads(item.pop(key))
        return item

    @staticmethod
    def _versioned(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is not None:
            item["schema_version"] = 1
        return item

    @classmethod
    def _checkpoint_from_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            snapshot = json.loads(item.pop("snapshot_json"))
            digest = content_hash(snapshot)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StorageContractError("checkpoint snapshot is not canonical JSON") from exc
        if not isinstance(snapshot, dict):
            raise StorageContractError("checkpoint snapshot must be an object")
        if (
            not isinstance(item.get("content_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["content_hash"]) is None
            or item["content_hash"] != digest
        ):
            raise StorageContractError(
                f"checkpoint integrity check failed: {item.get('checkpoint_id', 'unknown')}"
            )
        item["snapshot"] = snapshot
        item["schema_version"] = SCHEMA_VERSION
        return item

    @classmethod
    def _require_recovery_checkpoint(
        cls,
        db: sqlite3.Connection,
        *,
        checkpoint_id: str,
        task_id: str,
        task_revision: int,
    ) -> dict[str, Any]:
        cls._validated_id("checkpoint", checkpoint_id)
        checkpoint = cls._checkpoint_from_row(
            db.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        )
        if checkpoint is None:
            raise NotFoundError(f"checkpoint not found: {checkpoint_id}")
        if checkpoint["task_id"] != task_id:
            raise ConflictError("source checkpoint belongs to a different task")
        if checkpoint["task_revision"] != task_revision:
            raise ConflictError("source checkpoint was created for a different task revision")
        superseded = db.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE supersedes = ? LIMIT 1",
            (checkpoint_id,),
        ).fetchone()
        if superseded is not None:
            raise ConflictError(
                f"source checkpoint was superseded by {superseded['checkpoint_id']}"
            )
        return checkpoint

    def create_task(
        self,
        *,
        project_id: str,
        title: str,
        objective: str,
        scope: Sequence[str],
        acceptance: Sequence[str],
        task_id: str | None = None,
    ) -> dict[str, Any]:
        if not project_id.strip() or not title.strip() or not objective.strip():
            raise ValueError("project_id, title, and objective are required")
        if not scope or not acceptance or any(not value.strip() for value in [*scope, *acceptance]):
            raise ValueError("scope and acceptance must each contain at least one item")
        task_id = self._validated_id("task", task_id or self._new_id("task"))
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO tasks(
                    task_id, project_id, title, objective, scope_json, acceptance_json,
                    state, revision, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    task_id,
                    project_id,
                    title,
                    objective,
                    canonical_json(list(scope)),
                    canonical_json(list(acceptance)),
                    TaskState.DRAFT.value,
                    now,
                    now,
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        item = self._versioned(self._decode(
            self.connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        ))
        if item is None:
            raise NotFoundError(f"task not found: {task_id}")
        return item

    def list_tasks(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            rows = self.connection.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self._versioned(self._decode(row)) for row in rows]  # type: ignore[misc]

    def transition_task(
        self,
        task_id: str,
        target: str | TaskState,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        target_state = TaskState(target)
        with self.transaction() as db:
            row = db.execute("SELECT state, revision FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"task not found: {task_id}")
            if row["revision"] != expected_revision:
                raise ConflictError(
                    f"task revision conflict: expected {expected_revision}, actual {row['revision']}"
                )
            current = TaskState(row["state"])
            require_transition(current, target_state, TASK_TRANSITIONS)
            if target_state in {TaskState.REVIEW_PENDING, TaskState.DONE}:
                unresolved = db.execute(
                    """SELECT run_id, state FROM runs
                    WHERE task_id = ? AND state IN (?, ?, ?) LIMIT 1""",
                    (
                        task_id,
                        RunState.PLANNED.value,
                        RunState.RUNNING.value,
                        RunState.UNKNOWN.value,
                    ),
                ).fetchone()
                if unresolved is not None:
                    raise TransitionError(
                        f"task has unresolved run {unresolved['run_id']} in {unresolved['state']}"
                    )
                open_session = db.execute(
                    """SELECT session_id, state FROM sessions
                    WHERE task_id = ? AND state IN (?, ?, ?) LIMIT 1""",
                    (
                        task_id,
                        SessionState.CREATED.value,
                        SessionState.ACTIVE.value,
                        SessionState.QUIESCING.value,
                    ),
                ).fetchone()
                if open_session is not None:
                    raise TransitionError(
                        f"task has open session {open_session['session_id']} in {open_session['state']}"
                    )
            result = db.execute(
                """UPDATE tasks SET state = ?, revision = revision + 1, updated_at = ?
                WHERE task_id = ? AND revision = ?""",
                (target_state.value, utc_now(), task_id, expected_revision),
            )
            if result.rowcount != 1:
                raise ConflictError("task changed during transition")
        return self.get_task(task_id)

    def create_session(
        self,
        *,
        task_id: str,
        host_alias: str,
        source_checkpoint_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not host_alias.strip():
            raise ValueError("host_alias is required")
        session_id = self._validated_id("session", session_id or self._new_id("session"))
        now = utc_now()
        with self.transaction() as db:
            task = db.execute(
                "SELECT state, revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if TaskState(task["state"]) is not TaskState.RUNNING:
                raise TransitionError("a new session requires a RUNNING task")
            prior_session = db.execute(
                "SELECT session_id FROM sessions WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
            if prior_session is not None and source_checkpoint_id is None:
                raise TransitionError(
                    "every session after the first requires a valid source checkpoint"
                )
            if source_checkpoint_id is not None:
                self._require_recovery_checkpoint(
                    db,
                    checkpoint_id=source_checkpoint_id,
                    task_id=task_id,
                    task_revision=task["revision"],
                )
                open_session = db.execute(
                    """SELECT session_id, state FROM sessions
                    WHERE task_id = ? AND state IN (?, ?, ?) LIMIT 1""",
                    (
                        task_id,
                        SessionState.CREATED.value,
                        SessionState.ACTIVE.value,
                        SessionState.QUIESCING.value,
                    ),
                ).fetchone()
                if open_session is not None:
                    raise TransitionError(
                        "checkpoint recovery requires every prior session to be quiesced; "
                        f"session {open_session['session_id']} is {open_session['state']}"
                    )
                in_flight = db.execute(
                    """SELECT run_id, state FROM runs
                    WHERE task_id = ? AND state IN (?, ?) LIMIT 1""",
                    (task_id, RunState.PLANNED.value, RunState.RUNNING.value),
                ).fetchone()
                if in_flight is not None:
                    raise TransitionError(
                        "checkpoint recovery requires every known run to reach a result; "
                        f"run {in_flight['run_id']} is {in_flight['state']}"
                    )
            db.execute(
                """INSERT INTO sessions(
                    session_id, task_id, host_alias, context_revision, state,
                    source_checkpoint_id, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    task_id,
                    host_alias,
                    task["revision"],
                    SessionState.CREATED.value,
                    source_checkpoint_id,
                    now,
                    now,
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        item = self._versioned(self._decode(
            self.connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        ))
        if item is None:
            raise NotFoundError(f"session not found: {session_id}")
        return item

    def transition_session(self, session_id: str, target: str | SessionState) -> dict[str, Any]:
        target_state = SessionState(target)
        with self.transaction() as db:
            row = db.execute(
                """SELECT s.task_id, s.context_revision, s.state, s.source_checkpoint_id,
                          t.state AS task_state, t.revision AS task_revision
                FROM sessions s
                JOIN tasks t ON t.task_id = s.task_id
                WHERE s.session_id = ?""",
                (session_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"session not found: {session_id}")
            current = SessionState(row["state"])
            require_transition(current, target_state, SESSION_TRANSITIONS)
            if target_state is SessionState.ACTIVE:
                if TaskState(row["task_state"]) is not TaskState.RUNNING:
                    raise TransitionError("activating a session requires a RUNNING task")
                if row["context_revision"] != row["task_revision"]:
                    raise ConflictError(
                        "session context revision is stale; create a new session"
                    )
                prior_session = db.execute(
                    """SELECT session_id FROM sessions
                    WHERE task_id = ? AND session_id != ? LIMIT 1""",
                    (row["task_id"], session_id),
                ).fetchone()
                if prior_session is not None and row["source_checkpoint_id"] is None:
                    raise TransitionError(
                        "every session after the first requires a valid source checkpoint"
                    )
                if row["source_checkpoint_id"] is not None:
                    self._require_recovery_checkpoint(
                        db,
                        checkpoint_id=row["source_checkpoint_id"],
                        task_id=row["task_id"],
                        task_revision=row["task_revision"],
                    )
                other_executing = db.execute(
                    """SELECT session_id FROM sessions
                    WHERE task_id = ? AND session_id != ? AND state IN (?, ?, ?) LIMIT 1""",
                    (
                        row["task_id"],
                        session_id,
                        SessionState.CREATED.value,
                        SessionState.ACTIVE.value,
                        SessionState.QUIESCING.value,
                    ),
                ).fetchone()
                if other_executing is not None:
                    raise TransitionError(
                        "task already has an active or quiescing execution session "
                        f"{other_executing['session_id']}"
                    )
            if target_state in {SessionState.CHECKPOINTED, SessionState.CLOSED}:
                in_flight = db.execute(
                    """SELECT run_id, state FROM runs
                    WHERE session_id = ? AND state IN (?, ?) LIMIT 1""",
                    (session_id, RunState.PLANNED.value, RunState.RUNNING.value),
                ).fetchone()
                if in_flight is not None:
                    raise TransitionError(
                        "session cannot be checkpointed or closed while a known run is in flight; "
                        f"run {in_flight['run_id']} is {in_flight['state']}"
                    )
            db.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE session_id = ?",
                (target_state.value, utc_now(), session_id),
            )
        return self.get_session(session_id)

    def create_run(
        self,
        *,
        task_id: str,
        session_id: str,
        executor: str,
        idempotency_key: str | None = None,
        request: Any = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if not executor.strip():
            raise ValueError("executor is required")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty when provided")
        request_hash = content_hash(request) if request is not None else None
        run_id = self._validated_id("run", run_id or self._new_id("run"))
        now = utc_now()
        with self.transaction() as db:
            task = db.execute(
                "SELECT state, revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            session = db.execute(
                "SELECT task_id, context_revision, state FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise NotFoundError(f"session not found: {session_id}")
            if session["task_id"] != task_id:
                raise ConflictError("session belongs to a different task")
            if idempotency_key:
                existing = db.execute(
                    "SELECT * FROM runs WHERE task_id = ? AND executor = ? AND idempotency_key = ?",
                    (task_id, executor, idempotency_key),
                ).fetchone()
                if existing:
                    decoded = self._versioned(self._decode(existing))
                    if decoded and decoded["request_hash"] != request_hash:
                        raise ConflictError("idempotency key already belongs to a different request")
                    return decoded  # type: ignore[return-value]
            if TaskState(task["state"]) is not TaskState.RUNNING:
                raise TransitionError("a new run requires a RUNNING task")
            if SessionState(session["state"]) is not SessionState.ACTIVE:
                raise TransitionError("a new run requires an ACTIVE session")
            if session["context_revision"] != task["revision"]:
                raise ConflictError(
                    "session context revision is stale; create a new session"
                )
            unresolved = db.execute(
                "SELECT run_id FROM runs WHERE task_id = ? AND state = ? LIMIT 1",
                (task_id, RunState.UNKNOWN.value),
            ).fetchone()
            if unresolved is not None:
                raise TransitionError(
                    "a new run is blocked until UNKNOWN external operations are reconciled; "
                    f"run {unresolved['run_id']} is UNKNOWN"
                )
            other_session_run = db.execute(
                """SELECT run_id, session_id FROM runs
                WHERE task_id = ? AND session_id != ? AND state IN (?, ?) LIMIT 1""",
                (
                    task_id,
                    session_id,
                    RunState.PLANNED.value,
                    RunState.RUNNING.value,
                ),
            ).fetchone()
            if other_session_run is not None:
                raise TransitionError(
                    "a new run cannot overlap an in-flight run from another session; "
                    f"run {other_session_run['run_id']} belongs to "
                    f"{other_session_run['session_id']}"
                )
            db.execute(
                """INSERT INTO runs(
                    run_id, task_id, session_id, input_revision, executor, state,
                    idempotency_key, request_hash, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    task_id,
                    session_id,
                    task["revision"],
                    executor,
                    RunState.PLANNED.value,
                    idempotency_key,
                    request_hash,
                    now,
                    now,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        item = self._versioned(self._decode(
            self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        ))
        if item is None:
            raise NotFoundError(f"run not found: {run_id}")
        return item

    def transition_run(
        self,
        run_id: str,
        target: str | RunState,
        *,
        external_operation_id: str | None = None,
    ) -> dict[str, Any]:
        if external_operation_id is not None and (
            not isinstance(external_operation_id, str) or not external_operation_id.strip()
        ):
            raise ValueError("external_operation_id must be a non-empty string when provided")
        target_state = RunState(target)
        with self.transaction() as db:
            row = db.execute(
                """SELECT r.task_id, r.session_id, r.state, r.external_operation_id,
                          t.state AS task_state, t.revision AS task_revision,
                          s.state AS session_state, s.context_revision
                FROM runs r
                JOIN tasks t ON t.task_id = r.task_id
                JOIN sessions s ON s.session_id = r.session_id
                WHERE r.run_id = ?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"run not found: {run_id}")
            current = RunState(row["state"])
            require_transition(current, target_state, RUN_TRANSITIONS)
            if current is RunState.PLANNED and target_state is RunState.RUNNING:
                if TaskState(row["task_state"]) is not TaskState.RUNNING:
                    raise TransitionError("starting a run requires a RUNNING task")
                if SessionState(row["session_state"]) is not SessionState.ACTIVE:
                    raise TransitionError("starting a run requires an ACTIVE session")
                if row["context_revision"] != row["task_revision"]:
                    raise ConflictError(
                        "session context revision is stale; create a new session"
                    )
                unresolved = db.execute(
                    """SELECT run_id FROM runs
                    WHERE task_id = ? AND run_id != ? AND state = ? LIMIT 1""",
                    (row["task_id"], run_id, RunState.UNKNOWN.value),
                ).fetchone()
                if unresolved is not None:
                    raise TransitionError(
                        "starting a run is blocked until UNKNOWN external operations are reconciled; "
                        f"run {unresolved['run_id']} is UNKNOWN"
                    )
                other_session_run = db.execute(
                    """SELECT run_id, session_id FROM runs
                    WHERE task_id = ? AND session_id != ? AND state IN (?, ?) LIMIT 1""",
                    (
                        row["task_id"],
                        row["session_id"],
                        RunState.PLANNED.value,
                        RunState.RUNNING.value,
                    ),
                ).fetchone()
                if other_session_run is not None:
                    raise TransitionError(
                        "starting a run cannot overlap an in-flight run from another session; "
                        f"run {other_session_run['run_id']} belongs to "
                        f"{other_session_run['session_id']}"
                    )
            current_operation = row["external_operation_id"]
            if current_operation and external_operation_id and current_operation != external_operation_id:
                raise ConflictError("external_operation_id is immutable once recorded")
            effective_operation = external_operation_id or current_operation
            if target_state is RunState.UNKNOWN and not effective_operation:
                raise TransitionError("UNKNOWN requires an external_operation_id for reconciliation")
            db.execute(
                """UPDATE runs SET state = ?,
                    external_operation_id = COALESCE(?, external_operation_id), updated_at = ?
                WHERE run_id = ?""",
                (target_state.value, external_operation_id, utc_now(), run_id),
            )
        return self.get_run(run_id)

    def add_evidence(
        self,
        *,
        subject_type: str,
        subject_id: str,
        result: str,
        metadata: dict[str, Any] | None = None,
        artifact_hash: str | None = None,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        if subject_type not in EVIDENCE_SUBJECT_TYPES:
            raise ValueError(f"unsupported evidence subject_type: {subject_type}")
        if result not in EVIDENCE_RESULTS:
            raise ValueError(f"unsupported evidence result: {result}")
        subject_id = self._validated_id(subject_type, subject_id)
        if artifact_hash is not None and (
            not isinstance(artifact_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact_hash) is None
        ):
            raise ValueError("artifact_hash must be a lowercase SHA-256 when provided")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("evidence metadata must be an object")
        evidence_id = self._validated_id(
            "evidence", evidence_id or self._new_id("evidence")
        )
        with self.transaction() as db:
            subject_table = {
                "task": "tasks",
                "session": "sessions",
                "run": "runs",
            }.get(subject_type)
            if subject_table is not None:
                identifier_column = f"{subject_type}_id"
                exists = db.execute(
                    f"SELECT 1 FROM {subject_table} WHERE {identifier_column} = ?",
                    (subject_id,),
                ).fetchone()
                if exists is None:
                    raise NotFoundError(f"{subject_type} not found: {subject_id}")
            db.execute(
                """INSERT INTO evidence(
                    evidence_id, subject_type, subject_id, result, artifact_hash,
                    metadata_json, observed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence_id,
                    subject_type,
                    subject_id,
                    result,
                    artifact_hash,
                    canonical_json(metadata or {}),
                    utc_now(),
                ),
            )
        return self._versioned(self._decode(
            self.connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        ))  # type: ignore[return-value]

    def create_checkpoint(
        self,
        *,
        task_id: str,
        snapshot: dict[str, Any],
        supersedes: str | None = None,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValueError("checkpoint snapshot must be an object")
        checkpoint_id = self._validated_id(
            "checkpoint", checkpoint_id or self._new_id("checkpoint")
        )
        encoded = canonical_json(snapshot)
        digest = content_hash(snapshot)
        with self.transaction() as db:
            task = db.execute(
                "SELECT revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if supersedes:
                self._validated_id("checkpoint", supersedes)
                previous = self._checkpoint_from_row(
                    db.execute(
                        "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (supersedes,)
                    ).fetchone()
                )
                if previous is None:
                    raise NotFoundError(f"checkpoint not found: {supersedes}")
                if previous["task_id"] != task_id:
                    raise ConflictError("superseded checkpoint belongs to a different task")
                already_superseded = db.execute(
                    "SELECT checkpoint_id FROM checkpoints WHERE supersedes = ? LIMIT 1",
                    (supersedes,),
                ).fetchone()
                if already_superseded is not None:
                    raise ConflictError(
                        f"checkpoint was already superseded by {already_superseded['checkpoint_id']}"
                    )
            db.execute(
                """INSERT INTO checkpoints(
                    checkpoint_id, task_id, task_revision, snapshot_json,
                    content_hash, supersedes, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id,
                    task_id,
                    task["revision"],
                    encoded,
                    digest,
                    supersedes,
                    utc_now(),
                ),
            )
        return self.get_checkpoint(checkpoint_id)

    def get_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        self._validated_id("checkpoint", checkpoint_id)
        item = self._checkpoint_from_row(
            self.connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        )
        if item is None:
            raise NotFoundError(f"checkpoint not found: {checkpoint_id}")
        return item

    def add_knowledge(
        self,
        *,
        project_id: str,
        scope: str,
        classification: str,
        kind: str,
        title: str,
        content: str,
        source: str,
        evidence_label: str,
        source_revision: str | None = None,
        review_state: str = "unreviewed",
        valid_until: str | None = None,
        knowledge_id: str | None = None,
    ) -> dict[str, Any]:
        if classification not in CLASSIFICATION_RANK:
            raise ValueError(f"unsupported classification: {classification}")
        if kind not in KNOWLEDGE_KINDS:
            raise ValueError(f"unsupported knowledge kind: {kind}")
        if evidence_label not in {"SOURCE", "OBSERVED", "REPORTED", "INFERRED", "MISSING"}:
            raise ValueError(f"unsupported evidence label: {evidence_label}")
        if review_state not in KNOWLEDGE_REVIEW_STATES:
            raise ValueError(f"unsupported review_state: {review_state}")
        if any(not value.strip() for value in (project_id, scope, title, content, source)):
            raise ValueError("project_id, scope, title, content, and source are required")
        if source_revision is not None and not isinstance(source_revision, str):
            raise ValueError("source_revision must be a string when provided")
        if valid_until is not None:
            if not isinstance(valid_until, str):
                raise ValueError("valid_until must be an offset-aware date-time when provided")
            try:
                parsed_valid_until = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    "valid_until must be an offset-aware date-time when provided"
                ) from exc
            if parsed_valid_until.tzinfo is None:
                raise ValueError("valid_until must be an offset-aware date-time when provided")
        knowledge_id = self._validated_id(
            "knowledge", knowledge_id or self._new_id("knowledge")
        )
        now = utc_now()
        digest = content_hash(
            {
                "project_id": project_id,
                "scope": scope,
                "classification": classification,
                "kind": kind,
                "title": title,
                "content": content,
                "source": source,
                "source_revision": source_revision,
            }
        )
        with self.transaction() as db:
            db.execute(
                """INSERT INTO knowledge(
                    knowledge_id, project_id, scope, classification, kind, title,
                    content, source, source_revision, evidence_label, review_state,
                    valid_until, content_hash, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    knowledge_id,
                    project_id,
                    scope,
                    classification,
                    kind,
                    title,
                    content,
                    source,
                    source_revision,
                    evidence_label,
                    review_state,
                    valid_until,
                    digest,
                    now,
                    now,
                ),
            )
            if self.fts_enabled:
                db.execute(
                    "INSERT INTO knowledge_fts(knowledge_id, title, content) VALUES(?, ?, ?)",
                    (knowledge_id, title, content),
                )
        return self.get_knowledge(knowledge_id)

    def get_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        item = self._versioned(self._decode(
            self.connection.execute(
                "SELECT * FROM knowledge WHERE knowledge_id = ?", (knowledge_id,)
            ).fetchone()
        ))
        if item is None:
            raise NotFoundError(f"knowledge not found: {knowledge_id}")
        return item

    def search_knowledge(
        self,
        *,
        project_id: str,
        query: str,
        allowed_scopes: Sequence[str],
        max_classification: str = "internal",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if max_classification not in CLASSIFICATION_RANK:
            raise ValueError(f"unsupported classification: {max_classification}")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not allowed_scopes:
            return []
        allowed_classifications = [
            name
            for name, rank in CLASSIFICATION_RANK.items()
            if rank <= CLASSIFICATION_RANK[max_classification]
        ]
        params: list[Any] = [project_id, *allowed_scopes, *allowed_classifications]
        placeholders = ",".join("?" for _ in allowed_scopes)
        classification_placeholders = ",".join("?" for _ in allowed_classifications)
        if query.strip() and self.fts_enabled:
            sql = f"""
                SELECT k.* FROM knowledge k
                JOIN knowledge_fts f ON f.knowledge_id = k.knowledge_id
                WHERE k.project_id = ? AND k.scope IN ({placeholders})
                  AND k.classification IN ({classification_placeholders})
                  AND knowledge_fts MATCH ?
                ORDER BY bm25(knowledge_fts), k.updated_at DESC LIMIT ?
            """
            params.extend([query, limit])
        else:
            sql = f"""
                SELECT * FROM knowledge
                WHERE project_id = ? AND scope IN ({placeholders})
                  AND classification IN ({classification_placeholders})
                  AND (title LIKE ? OR content LIKE ?)
                ORDER BY updated_at DESC LIMIT ?
            """
            like = f"%{query}%"
            params.extend([like, like, limit])
        try:
            rows = self.connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Invalid FTS query syntax must not widen the result set.
            return []
        return [
            self._versioned(self._decode(row))  # type: ignore[misc]
            for row in rows
        ]

    def forget_knowledge(self, knowledge_id: str) -> None:
        with self.transaction() as db:
            result = db.execute("DELETE FROM knowledge WHERE knowledge_id = ?", (knowledge_id,))
            if result.rowcount != 1:
                raise NotFoundError(f"knowledge not found: {knowledge_id}")
            if self.fts_enabled:
                db.execute("DELETE FROM knowledge_fts WHERE knowledge_id = ?", (knowledge_id,))

    def export_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        sessions = [
            self._versioned(self._decode(row))
            for row in self.connection.execute(
                "SELECT * FROM sessions WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        ]
        runs = [
            self._versioned(self._decode(row))
            for row in self.connection.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        ]
        checkpoints = [
            self._checkpoint_from_row(row)
            for row in self.connection.execute(
                "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        ]
        session_ids = [item["session_id"] for item in sessions if item]
        run_ids = [item["run_id"] for item in runs if item]
        clauses = ["(subject_type = 'task' AND subject_id = ?)"]
        params: list[Any] = [task_id]
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            clauses.append(f"(subject_type = 'session' AND subject_id IN ({placeholders}))")
            params.extend(session_ids)
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            clauses.append(f"(subject_type = 'run' AND subject_id IN ({placeholders}))")
            params.extend(run_ids)
        evidence = [
            self._versioned(self._decode(row))
            for row in self.connection.execute(
                f"SELECT * FROM evidence WHERE {' OR '.join(clauses)} ORDER BY observed_at",
                params,
            ).fetchall()
        ]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": utc_now(),
            "task": task,
            "sessions": sessions,
            "runs": runs,
            "checkpoints": checkpoints,
            "evidence": evidence,
        }
        payload["content_hash"] = content_hash(payload)
        return payload
