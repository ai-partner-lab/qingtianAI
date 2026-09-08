from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
import json
import math
from typing import Any


class QingtianError(RuntimeError):
    """Base error for public Qingtian operations."""


class ConflictError(QingtianError):
    """The caller used a stale revision or conflicting idempotency key."""


class NotFoundError(QingtianError):
    """A requested entity does not exist in the selected project scope."""


class TransitionError(QingtianError):
    """A requested state transition is outside the versioned contract."""


class StorageContractError(QingtianError):
    """The on-disk database is not compatible with this runtime contract."""


class TaskState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    REVIEW_PENDING = "REVIEW_PENDING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class SessionState(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    QUIESCING = "QUIESCING"
    CHECKPOINTED = "CHECKPOINTED"
    CLOSED = "CLOSED"


class RunState(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"


TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DRAFT: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {TaskState.BLOCKED, TaskState.REVIEW_PENDING, TaskState.CANCELLED}
    ),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.REVIEW_PENDING: frozenset(
        {TaskState.DONE, TaskState.READY, TaskState.CANCELLED}
    ),
    TaskState.DONE: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

SESSION_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset({SessionState.ACTIVE, SessionState.CLOSED}),
    SessionState.ACTIVE: frozenset({SessionState.QUIESCING}),
    SessionState.QUIESCING: frozenset({SessionState.CHECKPOINTED}),
    SessionState.CHECKPOINTED: frozenset({SessionState.CLOSED}),
    SessionState.CLOSED: frozenset(),
}

RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PLANNED: frozenset({RunState.RUNNING, RunState.INTERRUPTED}),
    RunState.RUNNING: frozenset(
        {RunState.SUCCEEDED, RunState.FAILED, RunState.INTERRUPTED, RunState.UNKNOWN}
    ),
    # UNKNOWN is observational, not a retry signal. Reconciliation may resolve it.
    RunState.UNKNOWN: frozenset({RunState.SUCCEEDED, RunState.FAILED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.INTERRUPTED: frozenset(),
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_strict_json(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            require_strict_json(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            require_strict_json(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} contains a non-JSON value of type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    require_strict_json(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_transition(current: StrEnum, target: StrEnum, table: dict[Any, frozenset[Any]]) -> None:
    if target not in table[current]:
        raise TransitionError(f"illegal transition: {current.value} -> {target.value}")


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    requested_model: str
    reported_model: str | None
    status: str
    latency_ms: int
    usage: dict[str, int | float | str | None] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    """Explicit provider output and its provider-reported effective model."""

    output: Any
    reported_model: str | None


@dataclass(frozen=True)
class ProviderResult:
    requested_model: str
    resolved_route: str
    effective_model: str | None
    output: Any
    attempts: tuple[ProviderAttempt, ...]
    fallback_reason: str | None = None
    cost: dict[str, int | float | str | None] = field(default_factory=dict)
