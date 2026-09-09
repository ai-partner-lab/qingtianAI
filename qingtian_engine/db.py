from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    parent_id TEXT REFERENCES tasks(id),
    source_request_id TEXT,
    title TEXT NOT NULL,
    short_summary TEXT NOT NULL DEFAULT '',
    scope_summary TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 2,
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    environment TEXT NOT NULL DEFAULT 'local',
    repository TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    worker_type TEXT NOT NULL DEFAULT 'cli',
    owner_session TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'gpt-5.6-sol',
    reasoning TEXT NOT NULL DEFAULT 'high',
    speed TEXT NOT NULL DEFAULT 'standard',
    authorization_policy TEXT NOT NULL DEFAULT 'normal',
    state TEXT NOT NULL DEFAULT 'INBOX',
    action_owner_kind TEXT NOT NULL DEFAULT 'none',
    action_owner TEXT NOT NULL DEFAULT '',
    action_text TEXT NOT NULL DEFAULT '',
    action_due TEXT,
    action_sensitive INTEGER NOT NULL DEFAULT 0,
    execution_mode TEXT NOT NULL DEFAULT 'managed',
    heartbeat_at TEXT,
    evidence_profile TEXT NOT NULL DEFAULT 'auto',
    blocking_reason TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL DEFAULT '',
    requires_deploy INTEGER NOT NULL DEFAULT 0,
    next_check_at TEXT,
    imported_from TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(parent_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_state_updated ON tasks(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_session, updated_at DESC);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'blocks',
    PRIMARY KEY(task_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    producer TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_task_time ON events(task_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, kind, value)
);

CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    adapter TEXT NOT NULL,
    command_summary TEXT NOT NULL,
    pid INTEGER,
    process_group INTEGER,
    session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'QUEUED',
    exit_code INTEGER,
    result_hash TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    retry_of TEXT REFERENCES runs(id),
    failure_kind TEXT NOT NULL DEFAULT '',
    failure_stage TEXT NOT NULL DEFAULT '',
    failure_type TEXT NOT NULL DEFAULT '',
    failure_trace_hash TEXT NOT NULL DEFAULT '',
    debug_line_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(task_id, attempt)
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, attempt DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_task
ON runs(task_id) WHERE status IN ('QUEUED', 'RUNNING');

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    scope_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'IDLE',
    model TEXT NOT NULL DEFAULT 'gpt-5.6-sol',
    reasoning TEXT NOT NULL DEFAULT 'high',
    last_seen_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS authorization_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    policy TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    fingerprint TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    imported_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback_cursors (
    consumer TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intakes (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    intent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RECEIVED',
    planner_adapter TEXT NOT NULL DEFAULT 'deterministic',
    draft_json TEXT NOT NULL DEFAULT '{}',
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    secret_warning INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intakes_created ON intakes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intakes_status ON intakes(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS intake_attachments (
    id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES intakes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    local_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(intake_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_intake_attachments_intake
ON intake_attachments(intake_id, created_at ASC);

CREATE TABLE IF NOT EXISTS intake_messages (
    id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES intakes(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intake_messages_intake
ON intake_messages(intake_id, created_at ASC);

-- Qingtian 2.0 additive orchestration layer. These tables deliberately avoid
-- changing the existing task/run contract so a 1.x process can still read the
-- same database during a rolling local migration.
CREATE TABLE IF NOT EXISTS orchestrator_leases (
    lease_key TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL DEFAULT 1,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS reconciliation_checkpoints (
    name TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL DEFAULT '',
    fencing_token INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'IDLE',
    summary_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_contracts (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'MISSING',
    policy TEXT NOT NULL DEFAULT 'verified-value',
    evidence_id INTEGER REFERENCES evidence(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_evidence_contract_status
ON evidence_contracts(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS dead_letters (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dead_letters_status
ON dead_letters(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS executor_plugins (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    min_reasoning TEXT NOT NULL DEFAULT 'high',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    config_json TEXT NOT NULL DEFAULT '{}',
    health TEXT NOT NULL DEFAULT 'UNKNOWN',
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            for name in (
                "failure_kind",
                "failure_stage",
                "failure_type",
                "failure_trace_hash",
                "debug_line_count",
            ):
                if name not in run_columns:
                    definition = (
                        "INTEGER NOT NULL DEFAULT 0"
                        if name == "debug_line_count"
                        else "TEXT NOT NULL DEFAULT ''"
                    )
                    connection.execute(
                        "ALTER TABLE runs ADD COLUMN {} {}".format(name, definition)
                    )
            task_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "execution_mode" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'managed'"
                )
            if "heartbeat_at" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT")
            if "evidence_profile" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN evidence_profile "
                    "TEXT NOT NULL DEFAULT 'auto'"
                )
            action_columns = {
                "action_owner_kind": "TEXT NOT NULL DEFAULT 'none'",
                "action_owner": "TEXT NOT NULL DEFAULT ''",
                "action_text": "TEXT NOT NULL DEFAULT ''",
                "action_due": "TEXT",
                "action_sensitive": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in action_columns.items():
                if name not in task_columns:
                    connection.execute(
                        "ALTER TABLE tasks ADD COLUMN {} {}".format(name, definition)
                    )
            # Older databases predate the explicit non-execution policies.
            # Preserve their history while making imported and analysis-only
            # records fail closed before any scheduler/retry can dispatch them.
            connection.execute(
                """
                UPDATE tasks SET authorization_policy='reference-only'
                WHERE imported_from!='' AND authorization_policy='normal'
                """
            )
            connection.execute(
                """
                UPDATE tasks SET authorization_policy='analysis-only'
                WHERE authorization_policy='normal'
                  AND source_request_id!=''
                  AND EXISTS (
                      SELECT 1 FROM intakes
                      WHERE intakes.id=tasks.source_request_id
                        AND intakes.intent='analyze'
                  )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
            )
            connection.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, tuple(params))
            return cursor.rowcount

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def add_event(
        self,
        task_id: str,
        event_type: str,
        producer: str,
        summary: str,
        dedupe_key: str,
        payload: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[str] = None,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, task_id, event_type, producer, summary,
                    payload_json, dedupe_key, occurred_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    task_id,
                    event_type,
                    producer,
                    summary,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    dedupe_key,
                    occurred_at or utc_now(),
                ),
            )
            return cursor.rowcount == 1
