from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


class KnowledgeError(RuntimeError):
    """Expected user-facing Knowledge Hub error."""


class ConfigurationError(KnowledgeError):
    """The source or vault configuration is invalid."""


class HumanEditConflict(KnowledgeError):
    """A generated file was edited after the last Qingtian write."""


class SourceChangedDuringIngest(KnowledgeError):
    """A source changed while it was being read and was not ingested."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def strict_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def digest_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def digest_file(path: Path) -> str:
    checksum = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


@dataclass(frozen=True)
class SourceRecord:
    knowledge_id: str
    source_set: str
    project: str
    category: str
    classification: str
    evidence_level: str
    claim_scope: str
    source_path: Path
    relative_path: str
    source_hash: str
    size_bytes: int
    modified_at: str
    title: str
    kind: str
    repo_head: str | None
    repo_branch: str | None
    repo_blob: str | None
    git_state: str
    pii: str
    secret_kinds: tuple[str, ...]
    transform_fingerprint: str = ""

    @property
    def quarantined(self) -> bool:
        return (
            bool(self.secret_kinds)
            or self.classification in {"P2-confidential", "P3-restricted"}
            or self.pii != "none"
        )
