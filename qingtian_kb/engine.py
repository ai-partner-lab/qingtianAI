from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from fnmatch import fnmatch
from hashlib import sha1, sha256
from importlib.metadata import PackageNotFoundError, version as package_version
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import threading
from typing import Any, Iterable, Iterator
from urllib.parse import quote
from uuid import uuid4

from .models import (
    ConfigurationError,
    HumanEditConflict,
    KnowledgeError,
    SourceChangedDuringIngest,
    SourceRecord,
    digest_bytes,
    digest_file,
    strict_json,
    utc_now,
)


SCHEMA_VERSION = 1
RAW_PRIVACY_COMPACTION_KEY = "raw_privacy_compaction_v1"
try:
    PDF_EXTRACTOR_FINGERPRINT = "pypdf/" + package_version("pypdf")
except PackageNotFoundError:
    PDF_EXTRACTOR_FINGERPRINT = "pdf-unavailable"
MANAGED_MARKER = "<!-- qingtian:managed-source -->"
HUMAN_KNOWLEDGE_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
HUMAN_METADATA_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,255}$")
HUMAN_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "aws-resource-identifier": re.compile(r"\barn:aws(?:-[a-z]+)?:[A-Za-z0-9_./:=+@-]+"),
    "aws-account-endpoint": re.compile(r"\b\d{12}\.dkr\.ecr\.[A-Za-z0-9-]+\.amazonaws\.com\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{24,}\b"),
    "github-fine-grained-token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "gitlab-token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "openai-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "stripe-live-key": re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}\b"),
    "google-api-key": re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "bearer-token": re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]{16,}"),
    "password-url": re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"),
    "cookie-secret": re.compile(r"(?i)\b(?:session|cookie|refresh_token)\s*[=:]\s*[A-Za-z0-9._~+/-]{24,}"),
    "generic-secret-assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret(?:_key)?|password)"
        r"\s*[=:]\s*[\"']?[A-Za-z0-9._~+/-]{16,}"
    ),
}
PII_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    # Do not reinterpret an eleven-digit slice inside a hexadecimal/object ID as
    # a phone number.  Real phone-shaped values still match next to punctuation,
    # whitespace and common assignment separators.
    "phone": re.compile(r"(?<![0-9A-Za-z])(?:\+?86[- ]?)?1[3-9]\d{9}(?![0-9A-Za-z])"),
    # Local macOS account names are personal machine metadata.  Keep documented
    # placeholders and Apple's shared home outside this high-confidence match.
    "macos-user-path": re.compile(
        r"(?i)(?<![A-Za-z0-9])/(?:Users)/"
        r"(?!(?:Shared|Guest|user|username|account|example|your[-_]?name)"
        r"(?=/|[\s`'\"\])},.;:]|$))"
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}(?=/|[\s`'\"\])},.;:]|$)"
    ),
    # English @-handle assignments and Chinese role assignments are treated as
    # identity-bearing.  Generic responsibility prose and pending placeholders
    # deliberately do not match.
    "role-linked-handle": re.compile(
        r"(?im)(?:"
        r"\b(?:handlers?|reporters?|assignees?|owners?|authors?|reviewers?|"
        r"maintainers?|operators?|creators?)\b\s*[:=]\s*@"
        r"[A-Za-z0-9][A-Za-z0-9_.-]{1,38}"
        r"|(?:处理人|提出人|负责人|审核人|维护人|经办人)"
        r"(?:\s*[:=：]\s*(?!(?:待定|暂无|未知|未分配|产品|测试|研发|团队|"
        r"pending|unknown|none|team|product|qa|dev|test|api|web|ios|android|"
        r"backend|frontend)(?:\s|$))"
        r"(?:@?[A-Za-z][A-Za-z0-9_.-]{1,38}|[\u3400-\u4dbf\u4e00-\u9fff]{2,8})"
        r"|\s+@?[A-Za-z][A-Za-z0-9_.-]{1,38})"
        r")"
    ),
    # Workflow phrases that bind a named account to review/assignment activity.
    # Bare handles and generic workflow words remain allowed on their own.
    "workflow-linked-handle": re.compile(
        r"(?im)(?:"
        r"(?:待验证|(?<!提)交回|指派(?:给)?|分配给)(?:\s*[/：:=]\s*|\s+)"
        r"(?!(?:待定|暂无|未知|产品|测试|研发|团队|pending|unknown|none|team|"
        r"product|qa|dev|test|api|web|ios|android|backend|frontend)(?:\s|$))"
        r"(?:@?[A-Za-z][A-Za-z0-9_.-]{1,38}|[\u3400-\u4dbf\u4e00-\u9fff]{2,8})"
        r"|(?:创建人|处理人)(?:\s*[/、]\s*(?:创建人|处理人))*\s*"
        r"(?:均)?(?:为|是)\s*"
        r"(?!(?:待定|暂无|未知|产品|测试|研发|团队|pending|unknown|none|team|"
        r"product|qa|dev|test|api|web|ios|android|backend|frontend)(?:\s|$))"
        r"(?:@?[A-Za-z][A-Za-z0-9_.-]{1,38}|[\u3400-\u4dbf\u4e00-\u9fff]{2,8})"
        r")"
    ),
    # A populated Markdown table with two or more identity-bearing role columns
    # is high-confidence PII in both English and Chinese.  A generic
    # Role/Responsibility matrix has at most one such column and stays allowed.
    "role-handle-table": re.compile(
        r"(?im)^\|(?=(?:[^\n]*?(?:"
        r"\b(?:handlers?|reporters?|assignees?|owners?|authors?|reviewers?|"
        r"maintainers?|operators?|creators?)\b"
        r"|处理人|提出人|负责人|审核人|维护人|经办人)){2})"
        r"[^\n]*\|\s*\n"
        r"\|(?:\s*:?-{3,}:?\s*\|){2,}\s*\n"
        r"\|(?=[^\n]*\S)[^\n]*\|"
    ),
    # A UUID alone is normal run/request/job metadata.  It becomes identifying
    # only when explicitly paired with a hostname, or parenthesized immediately
    # after a hostname-like token.  Common request/job labels are excluded.
    "hostname-uuid-pair": re.compile(
        r"(?ix)(?:"
        r"\b(?:host(?:name)?|machine(?:[-_ ]?name)?|computer(?:[-_ ]?name)?|"
        r"主机(?:名|名称)?|机器(?:名|名称)?)\s*[:=：]\s*"
        r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}\s*(?:[,;|/]\s*|\s+)"
        r"(?:uuid|host[-_ ]?id|machine[-_ ]?id|主机标识|机器标识)?\s*[:=：]?\s*"
        r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
        r"|(?<![A-Za-z0-9_])`?"
        r"(?!(?:request|job|run|trace|task|order|session|command|请求|任务|作业)"
        r"(?:[-_.]|\b))"
        r"(?=[A-Za-z0-9._-]{2,63}`?\b)(?=[A-Za-z0-9._-]*[A-Za-z])"
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?`?\s*\(\s*`?"
        r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}`?\s*\)"
        r")"
    ),
}
SCANNER_POLICY_FINGERPRINT = sha256(
    strict_json(
        {
            "secrets": {
                name: [regex.pattern, regex.flags]
                for name, regex in sorted(SECRET_PATTERNS.items())
            },
            "pii": {
                name: [regex.pattern, regex.flags]
                for name, regex in sorted(PII_PATTERNS.items())
            },
        }
    ).encode("utf-8")
).hexdigest()[:16]
EXTRACTOR_VERSION = (
    "qingtian-kb/0.2.0;"
    + PDF_EXTRACTOR_FINGERPRINT
    + ";scanner/"
    + SCANNER_POLICY_FINGERPRINT
    # Rendering policy participates in the source transform fingerprint so a
    # renderer-only safety change regenerates otherwise unchanged mirrors.
    + ";renderer/inert-source-evidence-v2"
)
CLASSIFICATION_RANK = {
    "P0-public": 0,
    "P1-internal": 1,
    "P2-confidential": 2,
    "P3-restricted": 3,
}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv"}
VAULT_JSON_ALLOWLIST = {
    ".obsidian/app.json",
    ".obsidian/appearance.json",
    ".obsidian/core-plugins.json",
    ".obsidian/templates.json",
    ".obsidian/workspace.json",
    "99-System/Managed-Outputs-Manifest.json",
}
ALLOWED_GENERATED_STATUSES = {
    "active",
    "conflict",
    "quarantined",
    "resolved",
    "stale",
    "unavailable",
    "unstable",
}
TEST_TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".vue",
    ".java",
    ".kt",
    ".swift",
    ".go",
    ".rs",
    ".sh",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
}
BUG_WORDS = re.compile(
    r"(?i)(?:\bfix(?:ed|es)?\b|\bbug\b|\bdefect\b|\bregression\b|\bhotfix\b|"
    r"\brepair(?:ed)?\b|修复|缺陷|故障|回归|崩溃|异常)"
)
ALLOWED_AUTHORITY_STATES = {
    "candidate",
    "authoritative",
    "resolved",
    "conflicted-deployment-docs",
    "conflicted-release-docs",
    "conflicted-source-of-truth",
}
SAFE_CANONICAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SQLITE_OPEN_LOCK = threading.RLock()


def _open_fd_snapshot() -> dict[int, tuple[int, int, int]]:
    """Return open descriptor identities or fail closed.

    Python's sqlite3 wrapper does not expose ``SQLITE_OPEN_NOFOLLOW``.  We use
    this POSIX descriptor inventory while opening the connection to prove that
    SQLite retained a descriptor for the exact database inode pre-opened with
    ``openat(O_NOFOLLOW)``.  Platforms without an inspectable descriptor table
    cannot safely support the writable index.
    """

    descriptor_root = next(
        (Path(candidate) for candidate in ("/dev/fd", "/proc/self/fd") if Path(candidate).is_dir()),
        None,
    )
    if descriptor_root is None:
        raise KnowledgeError("cannot verify SQLite database descriptor on this platform")
    identities: dict[int, tuple[int, int, int]] = {}
    try:
        names = os.listdir(descriptor_root)
    except OSError as exc:
        raise KnowledgeError("cannot inspect process descriptors safely") from exc
    for name in names:
        try:
            descriptor = int(name)
            identity = os.fstat(descriptor)
        except (ValueError, OSError):
            continue
        identities[descriptor] = (
            identity.st_dev,
            identity.st_ino,
            stat.S_IFMT(identity.st_mode),
        )
    return identities


def _redact_secret_values(value: str) -> tuple[str, tuple[str, ...]]:
    """Redact high-confidence secret shapes without ever returning the match."""
    kinds: list[str] = []
    redacted = value
    for name, regex in SECRET_PATTERNS.items():
        if regex.search(redacted):
            kinds.append(name)
            redacted = regex.sub(f"[REDACTED:{name}]", redacted)
    return redacted, tuple(sorted(set(kinds)))


def _cjk_ngrams(value: str) -> list[str]:
    chunks = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", value)
    grams: list[str] = []
    for chunk in chunks:
        if len(chunk) < 2:
            grams.append(chunk)
            continue
        grams.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return grams


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not slug:
        slug = sha256(value.encode("utf-8")).hexdigest()[:16]
    return slug[:160]


def _portable_workspace_path(source_set: str, relative_path: str) -> str:
    """Return an account-free state locator; never persist an operational path."""

    relative = PurePosixPath(relative_path)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise KnowledgeError("source relative path cannot be persisted safely")
    return f"[workspace]/{_safe_slug(source_set)}/{relative.as_posix()}"


def _safe_yaml(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _run(command: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env={
            **{
                key: os.environ[key]
                for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT")
                if key in os.environ
            },
            # Prevent read-only Git inspection from opportunistically refreshing indexes.
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError("unsupported sources.json schema")
    return value


def _frontmatter_fields(text: str) -> dict[str, str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}
    fields: dict[str, str] = {}
    for line in text.split("\n---\n", 1)[0].splitlines()[1:]:
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _managed_identity_matches(
    knowledge_id: str, status: str, fields: dict[str, str]
) -> bool:
    note_id = fields.get("id")
    note_type = fields.get("type")
    if knowledge_id.startswith("source-active-"):
        return (
            status == "active"
            and note_type == "source"
            and note_id == knowledge_id.removeprefix("source-active-")
        )
    if knowledge_id.startswith("source-quarantine-"):
        source_id = knowledge_id.removeprefix("source-quarantine-")
        if status == "quarantined":
            return note_type == "quarantine-record" and note_id == "quarantine-" + source_id
        if status == "resolved":
            return (
                note_type == "quarantine-resolution"
                and note_id == "quarantine-resolution-" + source_id
            )
        return False
    if knowledge_id.startswith("conflict-record-"):
        suffix = knowledge_id.removeprefix("conflict-record-")
        return status == "conflict" and note_type == "conflict" and note_id == "conflict-" + suffix
    return note_id == knowledge_id


class KnowledgeIndex:
    def __init__(
        self,
        path: Path,
        *,
        state_dir_fd: int,
        state_dir_identity: tuple[int, int],
        read_only: bool = False,
    ):
        self.path = path
        self._state_dir_fd = os.dup(state_dir_fd)
        self._state_dir_identity = state_dir_identity
        self._database_fd = -1
        self.db: sqlite3.Connection | None = None
        self._read_only = read_only
        self._read_only_signature: tuple[int, int, int, int, int] | None = None
        try:
            flags = (
                os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
            ) | getattr(os, "O_NOFOLLOW", 0)
            try:
                self._database_fd = os.open(
                    path.name,
                    flags,
                    0o600,
                    dir_fd=self._state_dir_fd,
                )
            except OSError as exc:
                raise KnowledgeError("state database is unsafe or unavailable") from exc
            opened = os.fstat(self._database_fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise KnowledgeError("state database must be a private regular file")
            database_identity = (opened.st_dev, opened.st_ino)
            existed = opened.st_size > 0
            if read_only and not existed:
                raise KnowledgeError("read-only state database is uninitialized")
            if existed:
                self._preflight_existing_database(self._database_fd, opened)
            if os.name == "posix" and not read_only:
                os.fchmod(self._database_fd, 0o600)
            self._assert_path_binding(database_identity)

            database_uri = "file:" + quote(str(path.absolute()), safe="/")
            database_uri += "?mode=ro&immutable=1" if read_only else "?mode=rw"
            with _SQLITE_OPEN_LOCK:
                before_fds = _open_fd_snapshot()
                self.db = sqlite3.connect(database_uri, timeout=30, uri=True)
                after_fds = _open_fd_snapshot()
            sqlite_descriptors = [
                descriptor
                for descriptor, identity in after_fds.items()
                if before_fds.get(descriptor) != identity
                and identity
                == (database_identity[0], database_identity[1], stat.S_IFREG)
            ]
            if not sqlite_descriptors:
                raise KnowledgeError("SQLite opened an unverified state database")
            self._assert_path_binding(database_identity)
            self.db.row_factory = sqlite3.Row
            if read_only:
                self.db.execute("PRAGMA query_only = ON")
                self.db.execute("PRAGMA trusted_schema = OFF")
                self._assert_connected_snapshot_readable(database_identity)
                self._assert_connected_schema_supported()
                fts = self.db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='documents_fts'"
                ).fetchone()
                self.fts_enabled = fts is not None
                current = os.fstat(self._database_fd)
                self._read_only_signature = self._stat_signature(current)
                self.assert_read_only_unchanged()
                return
            if existed:
                # Validate the schema visible to the *actual* SQLite connection
                # before any write-state PRAGMA, DDL, migration, or FTS rebuild.
                self._assert_connected_snapshot_readable(database_identity)
                self.db.execute("PRAGMA query_only = ON")
                self._assert_connected_schema_supported()
                self.db.execute("PRAGMA query_only = OFF")
                self._assert_connected_snapshot_readable(database_identity)
            journal_mode = self.db.execute("PRAGMA journal_mode = MEMORY").fetchone()
            self._assert_path_binding(database_identity)
            if journal_mode is None or str(journal_mode[0]).lower() != "memory":
                raise KnowledgeError("SQLite state journal mode is not memory-only")
            self.db.execute("PRAGMA temp_store = MEMORY")
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA secure_delete = ON")
            self.fts_enabled = False
            # BEGIN IMMEDIATE acquires SQLite's native writer reservation without
            # changing database pages.  Revalidate after acquiring it so a direct
            # SQLite writer racing the first connected check cannot make us run
            # migrations or rebuild FTS against a future schema.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_path_binding(database_identity)
                if existed:
                    self._assert_connected_snapshot_readable(database_identity)
                    self._assert_connected_schema_supported()
                self.initialize()
                # A database created by this version starts with no legacy free
                # pages.  Existing databases deliberately receive the marker
                # only after a one-time VACUUM in scrub_legacy_source_paths().
                if not existed:
                    self.db.execute(
                        "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                        (RAW_PRIVACY_COMPACTION_KEY, "complete"),
                    )
                if existed:
                    self._assert_connected_schema_supported()
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        except Exception:
            if self.db is not None:
                self.db.close()
            if self._database_fd >= 0:
                os.close(self._database_fd)
                self._database_fd = -1
            os.close(self._state_dir_fd)
            raise

    @staticmethod
    def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def assert_read_only_unchanged(self) -> None:
        if not self._read_only or self._read_only_signature is None:
            return
        identity = self._read_only_signature[:2]
        self._assert_path_binding(identity)
        current = os.fstat(self._database_fd)
        if self._stat_signature(current) != self._read_only_signature:
            raise KnowledgeError("read-only state database changed during inspection")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(
                    self.path.name + suffix,
                    dir_fd=self._state_dir_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise KnowledgeError(
                    "read-only state sidecar cannot be inspected safely"
                ) from exc
            raise KnowledgeError("read-only state database gained a sidecar")

    def _assert_path_binding(self, database_identity: tuple[int, int]) -> None:
        try:
            parent = os.stat(self.path.parent, follow_symlinks=False)
            database = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise KnowledgeError("state database path changed during secure open") from exc
        if (
            not stat.S_ISDIR(parent.st_mode)
            or (parent.st_dev, parent.st_ino) != self._state_dir_identity
            or not stat.S_ISREG(database.st_mode)
            or database.st_nlink != 1
            or (database.st_dev, database.st_ino) != database_identity
        ):
            raise KnowledgeError("state database path changed during secure open")

    def _preflight_existing_database(
        self,
        descriptor: int,
        before: os.stat_result,
    ) -> None:
        """Reject an unknown schema using only bytes from the pinned database inode."""

        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(
                    self.path.name + suffix,
                    dir_fd=self._state_dir_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise KnowledgeError("state database sidecar cannot be inspected safely") from exc
            raise KnowledgeError("state database has an uncheckpointed sidecar")

        if before.st_size > 1024 * 1024 * 1024:
            raise KnowledgeError("state database is too large to validate safely")
        try:
            payload = os.pread(descriptor, before.st_size, 0)
        except OSError as exc:
            raise KnowledgeError("state database cannot be inspected safely") from exc
        after = os.fstat(descriptor)
        if (
            len(payload) != before.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise KnowledgeError("state database changed during schema validation")
        if (
            len(payload) < 20
            or payload[:16] != b"SQLite format 3\x00"
            or payload[18:20] != b"\x01\x01"
        ):
            raise KnowledgeError(
                "state database must be recovered to sidecar-free rollback format"
            )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(":memory:")
            connection.deserialize(payload)
            connection.execute("PRAGMA query_only = ON")
            tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            rows = (
                connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchall()
                if tables is not None and tables[0] == 1
                else []
            )
            if tables is None or tables[0] != 1 or len(rows) != 1:
                raise KnowledgeError("unsupported state database schema")
            if rows[0][0] != str(SCHEMA_VERSION):
                raise KnowledgeError("unsupported state database schema")
        except (sqlite3.DatabaseError, AttributeError) as exc:
            raise KnowledgeError("unsupported state database schema") from exc
        finally:
            if connection is not None:
                connection.close()

    def _assert_connected_snapshot_readable(
        self, database_identity: tuple[int, int]
    ) -> None:
        """Prove a rollback-format, sidecar-free image before a SQLite read."""

        self._assert_path_binding(database_identity)
        opened = os.fstat(self._database_fd)
        header = os.pread(self._database_fd, 20, 0)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != database_identity
            or len(header) < 20
            or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x01\x01"
        ):
            raise KnowledgeError("state database changed before schema validation")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(
                    self.path.name + suffix,
                    dir_fd=self._state_dir_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise KnowledgeError(
                    "state database sidecar cannot be inspected safely"
                ) from exc
            raise KnowledgeError("state database changed before schema validation")

    def _assert_connected_schema_supported(self) -> None:
        assert self.db is not None
        try:
            tables = self.db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            rows = (
                self.db.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchall()
                if tables is not None and tables[0] == 1
                else []
            )
        except sqlite3.DatabaseError as exc:
            raise KnowledgeError("unsupported state database schema") from exc
        if tables is None or tables[0] != 1 or len(rows) != 1:
            raise KnowledgeError("unsupported state database schema")
        if rows[0][0] != str(SCHEMA_VERSION):
            raise KnowledgeError("unsupported state database schema")

    def initialize(self) -> None:
        # ``executescript`` implicitly commits an existing transaction.  Execute
        # the fixed schema statements individually so the constructor's native
        # SQLite writer reservation remains held through every migration and FTS
        # rebuild.
        schema = """
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingestion_runs (
              run_id TEXT PRIMARY KEY,
              started_at TEXT NOT NULL,
              completed_at TEXT,
              config_hash TEXT NOT NULL,
              result TEXT NOT NULL,
              receipt_json TEXT
            );
            CREATE TABLE IF NOT EXISTS sources (
              knowledge_id TEXT PRIMARY KEY,
              source_set TEXT NOT NULL,
              project TEXT NOT NULL,
              category TEXT NOT NULL,
              classification TEXT NOT NULL,
              evidence_level TEXT NOT NULL,
              claim_scope TEXT NOT NULL,
              source_path TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              source_hash TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              modified_at TEXT NOT NULL,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              repo_head TEXT,
              repo_branch TEXT,
              repo_blob TEXT,
              git_state TEXT NOT NULL,
              pii TEXT NOT NULL,
              secret_kinds_json TEXT NOT NULL,
              extractor_version TEXT NOT NULL DEFAULT 'unknown',
              vault_path TEXT,
              output_hash TEXT,
              status TEXT NOT NULL,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              last_seen_run TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sources_project_idx ON sources(project, category, status);
            CREATE TABLE IF NOT EXISTS documents (
              knowledge_id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              project TEXT NOT NULL,
              category TEXT NOT NULL,
              evidence_level TEXT NOT NULL,
              claim_scope TEXT NOT NULL,
              review_status TEXT NOT NULL,
              conflict_status TEXT NOT NULL,
              freshness_status TEXT NOT NULL,
              classification TEXT NOT NULL DEFAULT 'P1-internal',
              source_revision TEXT,
              source_sha256 TEXT,
              git_state TEXT NOT NULL DEFAULT 'generated',
              source_status TEXT NOT NULL DEFAULT 'active',
              historical INTEGER NOT NULL DEFAULT 0,
              vault_path TEXT NOT NULL,
              source_locator TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS test_files (
              project TEXT NOT NULL,
              path TEXT NOT NULL,
              capability TEXT NOT NULL,
              case_count INTEGER NOT NULL,
              source_hash TEXT NOT NULL,
              last_seen_run TEXT NOT NULL,
              PRIMARY KEY(project, path)
            );
            CREATE TABLE IF NOT EXISTS git_commits (
              project TEXT NOT NULL,
              commit_sha TEXT NOT NULL,
              committed_at TEXT NOT NULL,
              subject TEXT NOT NULL,
              is_fix INTEGER NOT NULL,
              last_seen_run TEXT NOT NULL,
              PRIMARY KEY(project, commit_sha)
            );
            CREATE TABLE IF NOT EXISTS generated_outputs (
              knowledge_id TEXT PRIMARY KEY,
              vault_path TEXT NOT NULL,
              output_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              last_seen_run TEXT NOT NULL
            );
            """
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                self.db.execute(statement)
        # Forward-compatible local migrations for databases created by earlier previews.
        self._ensure_column("sources", "repo_branch", "TEXT")
        self._ensure_column("sources", "repo_blob", "TEXT")
        self._ensure_column(
            "sources", "extractor_version", "TEXT NOT NULL DEFAULT 'unknown'"
        )
        self._ensure_column(
            "documents", "classification", "TEXT NOT NULL DEFAULT 'P1-internal'"
        )
        self._ensure_column("documents", "source_revision", "TEXT")
        self._ensure_column("documents", "source_sha256", "TEXT")
        self._ensure_column(
            "documents", "git_state", "TEXT NOT NULL DEFAULT 'generated'"
        )
        self._ensure_column(
            "documents", "source_status", "TEXT NOT NULL DEFAULT 'active'"
        )
        self._ensure_column("documents", "historical", "INTEGER NOT NULL DEFAULT 0")
        self.db.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(SCHEMA_VERSION),),
        )
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts "
                "USING fts5(knowledge_id UNINDEXED, title, body, tokenize='unicode61')"
            )
            # FTS is a disposable derivative. Rebuild it from the canonical documents table
            # so restoring/copying the SQLite file cannot silently lose search results.
            self.db.execute("DELETE FROM documents_fts")
            self.db.execute(
                "INSERT INTO documents_fts(knowledge_id, title, body) "
                "SELECT knowledge_id, title, body FROM documents"
            )
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            row["name"] for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._database_fd >= 0:
            os.close(self._database_fd)
            self._database_fd = -1
        if self._state_dir_fd >= 0:
            os.close(self._state_dir_fd)
            self._state_dir_fd = -1

    def start_run(self, run_id: str, config_hash: str) -> None:
        self.db.execute(
            "INSERT INTO ingestion_runs(run_id, started_at, config_hash, result) VALUES(?, ?, ?, ?)",
            (run_id, utc_now(), config_hash, "running"),
        )
        self.db.commit()

    def finish_run(self, run_id: str, receipt: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE ingestion_runs SET completed_at = ?, result = ?, receipt_json = ? WHERE run_id = ?",
            (utc_now(), receipt["result"], strict_json(receipt), run_id),
        )
        self.db.commit()

    def previous_source(self, knowledge_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sources WHERE knowledge_id = ?", (knowledge_id,)
        ).fetchone()

    def scrub_legacy_source_paths(self) -> int:
        """Remove host-account paths and compact legacy raw pages once."""

        try:
            # Hold a SQLite writer reservation from the snapshot through the
            # update so an outside connection cannot reintroduce a legacy path
            # between the scan and commit.
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT knowledge_id, source_set, relative_path, classification, pii, "
                "secret_kinds_json, status, source_path FROM sources"
            ).fetchall()
            updates: list[tuple[str, str]] = []
            for row in rows:
                restricted = (
                    str(row["status"]).startswith("quarantined")
                    or row["classification"] in {"P2-confidential", "P3-restricted"}
                    or row["pii"] != "none"
                    or row["secret_kinds_json"] not in {"", "[]"}
                )
                if restricted:
                    expected = "[withheld]"
                else:
                    try:
                        expected = _portable_workspace_path(
                            str(row["source_set"]), str(row["relative_path"])
                        )
                    except KnowledgeError:
                        # Do not retain an unsafe operational locator while the
                        # subsequent discovery/validation decides the row's fate.
                        expected = "[withheld]"
                if str(row["source_path"]) != expected:
                    updates.append((expected, str(row["knowledge_id"])))
            if updates:
                self.db.executemany(
                    "UPDATE sources SET source_path=? WHERE knowledge_id=?", updates
                )
            compacted = self.db.execute(
                "SELECT 1 FROM metadata WHERE key=?",
                (RAW_PRIVACY_COMPACTION_KEY,),
            ).fetchone()
            self.db.commit()
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.rollback()
            raise KnowledgeError("legacy source locator scrub failed") from exc
        if compacted is None:
            try:
                # secure_delete protects future row/FTS churn.  VACUUM is the
                # one-time migration that removes sensitive bytes stranded in
                # free pages by versions predating that setting.
                database_before = os.fstat(self._database_fd)
                database_identity = (database_before.st_dev, database_before.st_ino)
                self.db.execute("VACUUM")
                self._assert_path_binding(database_identity)
                database_after = os.fstat(self._database_fd)
                if (database_after.st_dev, database_after.st_ino) != database_identity:
                    raise KnowledgeError("state database changed during privacy compaction")
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    (RAW_PRIVACY_COMPACTION_KEY, "complete"),
                )
                self.db.commit()
            except (sqlite3.Error, OSError) as exc:
                if self.db.in_transaction:
                    self.db.rollback()
                raise KnowledgeError("legacy state privacy compaction failed") from exc
        return len(updates)

    def upsert_source(
        self,
        record: SourceRecord,
        *,
        run_id: str,
        vault_path: str | None,
        output_hash: str | None,
        status: str,
    ) -> None:
        now = utc_now()
        restricted = status.startswith("quarantined")
        if record.quarantined and not restricted:
            raise KnowledgeError("restricted source cannot be persisted as active")
        persisted_source_path = (
            "[withheld]"
            if restricted
            else _portable_workspace_path(record.source_set, record.relative_path)
        )
        if not restricted and (
            any(regex.search(persisted_source_path) for regex in SECRET_PATTERNS.values())
            or any(regex.search(persisted_source_path) for regex in PII_PATTERNS.values())
        ):
            raise KnowledgeError("active source locator contains sensitive metadata")
        restricted_stub = f"95-Reviews/Quarantine/{record.knowledge_id}.md"
        persisted_vault_path = (
            vault_path
            if not restricted
            else vault_path if vault_path == restricted_stub else None
        )
        self.db.execute(
            """
            INSERT INTO sources(
              knowledge_id, source_set, project, category, classification, evidence_level,
              claim_scope, source_path, relative_path, source_hash, size_bytes, modified_at,
              title, kind, repo_head, repo_branch, repo_blob, git_state, pii,
              secret_kinds_json, extractor_version, vault_path,
              output_hash, status, first_seen_at, last_seen_at, last_seen_run
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(knowledge_id) DO UPDATE SET
              source_set=excluded.source_set, project=excluded.project, category=excluded.category,
              classification=excluded.classification, evidence_level=excluded.evidence_level,
              claim_scope=excluded.claim_scope, source_path=excluded.source_path,
              relative_path=excluded.relative_path, source_hash=excluded.source_hash,
              size_bytes=excluded.size_bytes, modified_at=excluded.modified_at,
              title=excluded.title, kind=excluded.kind, repo_head=excluded.repo_head,
              repo_branch=excluded.repo_branch, repo_blob=excluded.repo_blob,
              git_state=excluded.git_state, pii=excluded.pii,
              secret_kinds_json=excluded.secret_kinds_json,
              extractor_version=excluded.extractor_version, vault_path=excluded.vault_path,
              output_hash=excluded.output_hash, status=excluded.status,
              last_seen_at=excluded.last_seen_at, last_seen_run=excluded.last_seen_run
            """,
            (
                record.knowledge_id,
                record.source_set,
                record.project,
                record.category,
                record.classification,
                record.evidence_level,
                record.claim_scope,
                persisted_source_path,
                "[withheld]" if restricted else record.relative_path,
                record.source_hash,
                record.size_bytes,
                record.modified_at,
                f"Restricted source {record.knowledge_id}" if restricted else record.title,
                record.kind,
                record.repo_head,
                None if restricted else record.repo_branch,
                record.repo_blob,
                record.git_state,
                record.pii,
                strict_json(list(record.secret_kinds)),
                record.transform_fingerprint or EXTRACTOR_VERSION,
                persisted_vault_path,
                output_hash,
                status,
                now,
                now,
                run_id,
            ),
        )
        self.db.commit()

    def upsert_document(
        self,
        *,
        knowledge_id: str,
        title: str,
        body: str,
        project: str,
        category: str,
        evidence_level: str,
        claim_scope: str,
        review_status: str,
        conflict_status: str,
        freshness_status: str,
        classification: str = "P1-internal",
        source_revision: str | None = None,
        source_sha256: str | None = None,
        git_state: str = "generated",
        source_status: str = "active",
        historical: bool = False,
        vault_path: str,
        source_locator: str,
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO documents(
              knowledge_id, title, body, project, category, evidence_level, claim_scope,
              review_status, conflict_status, freshness_status, classification, source_revision,
              source_sha256, git_state, source_status, historical, vault_path, source_locator,
              updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                knowledge_id,
                title,
                body,
                project,
                category,
                evidence_level,
                claim_scope,
                review_status,
                conflict_status,
                freshness_status,
                classification,
                source_revision,
                source_sha256,
                git_state,
                source_status,
                int(historical),
                vault_path,
                source_locator,
                utc_now(),
            ),
        )
        if self.fts_enabled:
            self.db.execute("DELETE FROM documents_fts WHERE knowledge_id = ?", (knowledge_id,))
            self.db.execute(
                "INSERT INTO documents_fts(knowledge_id, title, body) VALUES(?, ?, ?)",
                (knowledge_id, title, body),
            )
        self.db.commit()

    def remove_document(self, knowledge_id: str) -> None:
        self.db.execute("DELETE FROM documents WHERE knowledge_id = ?", (knowledge_id,))
        if self.fts_enabled:
            self.db.execute("DELETE FROM documents_fts WHERE knowledge_id = ?", (knowledge_id,))
        self.db.commit()

    def set_document_state(
        self,
        knowledge_id: str,
        *,
        source_status: str,
        conflict_status: str | None = None,
        freshness_status: str | None = None,
    ) -> None:
        assignments = ["source_status=?", "updated_at=?"]
        values: list[Any] = [source_status, utc_now()]
        if conflict_status is not None:
            assignments.append("conflict_status=?")
            values.append(conflict_status)
        if freshness_status is not None:
            assignments.append("freshness_status=?")
            values.append(freshness_status)
        values.append(knowledge_id)
        self.db.execute(
            "UPDATE documents SET " + ", ".join(assignments) + " WHERE knowledge_id=?",
            values,
        )
        self.db.commit()

    def previous_generated(self, knowledge_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM generated_outputs WHERE knowledge_id = ?", (knowledge_id,)
        ).fetchone()

    def remove_generated(self, knowledge_id: str) -> None:
        self.db.execute("DELETE FROM generated_outputs WHERE knowledge_id = ?", (knowledge_id,))
        self.db.commit()

    def upsert_generated(
        self,
        knowledge_id: str,
        vault_path: str,
        output_hash: str,
        run_id: str,
        status: str = "active",
        *,
        commit: bool = True,
    ) -> None:
        self.db.execute(
            """INSERT INTO generated_outputs(
              knowledge_id, vault_path, output_hash, status, last_seen_at, last_seen_run
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(knowledge_id) DO UPDATE SET
              vault_path=excluded.vault_path, output_hash=excluded.output_hash,
              status=excluded.status, last_seen_at=excluded.last_seen_at,
              last_seen_run=excluded.last_seen_run""",
            (knowledge_id, vault_path, output_hash, status, utc_now(), run_id),
        )
        if commit:
            self.db.commit()

    def mark_missing(self, run_id: str) -> list[sqlite3.Row]:
        missing = self.db.execute(
            "SELECT * FROM sources WHERE last_seen_run != ? AND status != 'missing'",
            (run_id,),
        ).fetchall()
        self.db.execute(
            "UPDATE sources SET status='missing' WHERE last_seen_run != ? AND status != 'missing'",
            (run_id,),
        )
        for row in missing:
            self.db.execute(
                "UPDATE documents SET freshness_status='stale', source_status='missing', "
                "updated_at=? WHERE knowledge_id=?",
                (utc_now(), row["knowledge_id"]),
            )
        self.db.commit()
        return missing

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        projects: Iterable[str] = (),
        review_statuses: Iterable[str] = ("approved",),
        freshness: Iterable[str] = ("current",),
        classification_ceiling: str = "P1-internal",
        include_historical: bool = False,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 200))
        if classification_ceiling not in CLASSIFICATION_RANK:
            raise ConfigurationError(f"unknown classification ceiling: {classification_ceiling}")
        project_values = tuple(dict.fromkeys(projects))
        review_values = tuple(dict.fromkeys(review_statuses))
        freshness_values = tuple(dict.fromkeys(freshness))
        clauses = ["source_status = 'active'", "conflict_status = 'none'"]
        parameters: list[Any] = []
        allowed_classes = [
            value
            for value, rank in CLASSIFICATION_RANK.items()
            if rank <= CLASSIFICATION_RANK[classification_ceiling]
        ]
        clauses.append("classification IN (" + ",".join("?" for _ in allowed_classes) + ")")
        parameters.extend(allowed_classes)
        if not include_historical:
            clauses.append("historical = 0")
        if project_values:
            clauses.append("project IN (" + ",".join("?" for _ in project_values) + ")")
            parameters.extend(project_values)
        if review_values:
            clauses.append("review_status IN (" + ",".join("?" for _ in review_values) + ")")
            parameters.extend(review_values)
        if freshness_values:
            clauses.append("freshness_status IN (" + ",".join("?" for _ in freshness_values) + ")")
            parameters.extend(freshness_values)
        filter_sql = " AND ".join(clauses)
        rows: list[sqlite3.Row] = []
        candidate_limit = min(max(limit * 10, 50), 500)
        if self.fts_enabled:
            terms = [term for term in re.split(r"\s+", query) if term]
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            try:
                rows = self.db.execute(
                    """SELECT d.*, bm25(documents_fts) AS rank
                    FROM documents_fts JOIN documents d USING(knowledge_id)
                    WHERE documents_fts MATCH ? AND """
                    + filter_sql
                    + " ORDER BY rank LIMIT ?",
                    (match, *parameters, candidate_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        # unicode61 does not reliably segment unspaced Chinese. Add bounded
        # substring/bigram candidates and merge them with FTS results.
        fallback_terms = list(dict.fromkeys([query, *re.split(r"\s+", query), *_cjk_ngrams(query)]))
        fallback_terms = [term for term in fallback_terms if term][:32]
        if fallback_terms:
            like_sql = " OR ".join("title LIKE ? OR body LIKE ?" for _ in fallback_terms)
            like_params: list[Any] = []
            for term in fallback_terms:
                like_params.extend((f"%{term}%", f"%{term}%"))
            fallback_rows = self.db.execute(
                "SELECT *, 0 AS rank FROM documents WHERE ("
                + like_sql
                + ") AND "
                + filter_sql
                + " ORDER BY updated_at DESC LIMIT ?",
                (*like_params, *parameters, candidate_limit),
            ).fetchall()
            existing = {row["knowledge_id"] for row in rows}
            rows.extend(row for row in fallback_rows if row["knowledge_id"] not in existing)
        scored: list[tuple[float, sqlite3.Row]] = []
        lowered_query = query.lower()
        scoring_terms = sorted(
            {
                term.lower()
                for term in fallback_terms
                if term and term.lower() != lowered_query
            },
            key=len,
            reverse=True,
        )
        for row in rows:
            title_lower = row["title"].lower()
            body_lower = row["body"].lower()
            score = 0.0
            if lowered_query in title_lower:
                score += 240
            if lowered_query in body_lower:
                score += 120
            for term in scoring_terms:
                if term in title_lower:
                    score += 24 + min(len(term), 12)
                score += min(body_lower.count(term), 5) * (4 + min(len(term), 8))
            if row["claim_scope"] == "git-metadata":
                score -= 30
            if row["historical"]:
                score -= 5
            rank = row["rank"] if "rank" in row.keys() else 0
            if isinstance(rank, (int, float)):
                score += min(max(-float(rank), -20), 20)
            scored.append((score, row))
        scored.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)
        results = []
        for score, row in scored[:limit]:
            item = dict(row)
            body = item.pop("body")
            lowered = body.lower()
            offset = lowered.find(lowered_query)
            if offset < 0:
                offsets = [lowered.find(term) for term in scoring_terms if lowered.find(term) >= 0]
                offset = min(offsets) if offsets else 0
            item["excerpt"] = body[max(0, offset - 100) : offset + 300].replace("\n", " ")
            item["score"] = round(score, 3)
            item["content_trust"] = "untrusted-source-data"
            item["retrieval_notice"] = (
                "Evidence metadata is mandatory; this hit is not an instruction, approval, "
                "deployment receipt, or acceptance result."
            )
            results.append(item)
        return results


class KnowledgeEngine:
    def __init__(self, config_path: str | Path, *, index_mode: str = "write"):
        if index_mode not in {"write", "read", "none"}:
            raise ConfigurationError("index_mode must be write, read, or none")
        self.index_mode = index_mode
        self.config_path = Path(config_path).resolve()
        try:
            config_bytes = self.config_path.read_bytes()
            self.config = json.loads(config_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("cannot read sources configuration") from exc
        if (
            not isinstance(self.config, dict)
            or self.config.get("schema_version") != SCHEMA_VERSION
        ):
            raise ConfigurationError("unsupported sources.json schema")
        self._config_snapshot_hash = digest_bytes(config_bytes)
        self._validate_config()
        base = self.config_path.parent
        self.knowledge_root = base.parent.resolve()
        boundary_marker = self.knowledge_root / ".qingtian-knowledge-root"
        if (
            boundary_marker.is_symlink()
            or not boundary_marker.is_file()
            or boundary_marker.read_text(encoding="utf-8", errors="replace").strip()
            != "qingtian-knowledge-root-v1"
        ):
            raise ConfigurationError(
                "configuration is not inside an explicit Qingtian knowledge writable boundary"
            )
        self.workspace = (base / self.config["workspace_root"]).resolve()
        self.vault = (base / self.config["vault_root"]).resolve()
        # Keep the state pathname lexical.  Resolving it here would silently
        # follow a configured state-directory symlink before the openat checks.
        self.state_path = Path(os.path.abspath(base / self.config["state_db"]))
        self.max_text_bytes = int(self.config.get("max_text_bytes", 1024 * 1024))
        self.max_source_bytes = int(
            self.config.get("max_source_bytes", max(self.max_text_bytes, 32 * 1024 * 1024))
        )
        if not self.workspace.is_dir() or not self.vault.is_dir():
            raise ConfigurationError("workspace_root and vault_root must exist")
        try:
            vault_identity = os.stat(self.vault, follow_symlinks=False)
        except OSError as exc:
            raise ConfigurationError("vault_root cannot be inspected safely") from exc
        if not stat.S_ISDIR(vault_identity.st_mode):
            raise ConfigurationError("vault_root must be a real directory")
        # Every generated write is anchored back to this exact directory object.
        # Merely resolving the pathname once is insufficient: a writable parent
        # can otherwise be renamed or replaced between validation and commit.
        self._vault_identity = (vault_identity.st_dev, vault_identity.st_ino)
        try:
            self.vault.relative_to(self.knowledge_root)
            self.state_path.relative_to(self.knowledge_root)
        except ValueError as exc:
            raise ConfigurationError(
                "vault_root and state_db must stay inside the knowledge package"
            ) from exc
        if self.vault == self.workspace:
            raise ConfigurationError("vault_root must not be the source workspace")
        # Keep both writable surfaces in dedicated top-level directories.  A
        # configurable vault/state path must never point at the package root,
        # source configuration, Python package, tests, or at each other.
        if self.vault.parent != self.knowledge_root:
            raise ConfigurationError("vault_root must be a direct child of the knowledge package")
        if self.state_path.parent.parent != self.knowledge_root:
            raise ConfigurationError("state_db must be inside a dedicated top-level state directory")
        if self.state_path.parent == self.vault:
            raise ConfigurationError("state_db must not be stored inside the Obsidian vault")
        if self.state_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise ConfigurationError("state_db must use a .db, .sqlite, or .sqlite3 suffix")
        protected_names = {"config", "qingtian_kb", "tests"}
        if self.vault.name in protected_names or self.state_path.parent.name in protected_names:
            raise ConfigurationError("vault_root/state_db overlap a protected package directory")
        for collection in ("repositories", "test_roots"):
            for item in self.config.get(collection, []):
                root = (self.workspace / item["root"]).resolve()
                for writable in (self.vault, self.state_path):
                    try:
                        writable.relative_to(root)
                    except ValueError:
                        continue
                    raise ConfigurationError(
                        f"vault/state must not be located inside configured {collection} roots"
                    )
        self.excluded_parts = set(self.config.get("global_excluded_parts", []))
        self.hard_excluded_globs = tuple(self.config.get("hard_excluded_globs", []))
        self._knowledge_root_fd = -1
        self._state_dir_fd = -1
        self._read_lock_fd = -1
        self.index: KnowledgeIndex | None = None
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
                raise ConfigurationError("secure state directories are unsupported")
            root_before = os.lstat(self.knowledge_root)
            self._knowledge_root_fd = os.open(self.knowledge_root, directory_flags)
            root_opened = os.fstat(self._knowledge_root_fd)
            root_after = os.lstat(self.knowledge_root)
            root_identity = (root_opened.st_dev, root_opened.st_ino)
            if (
                not stat.S_ISDIR(root_opened.st_mode)
                or (root_before.st_dev, root_before.st_ino) != root_identity
                or (root_after.st_dev, root_after.st_ino) != root_identity
            ):
                raise ConfigurationError("knowledge root changed during secure state setup")
            if index_mode != "none":
                if index_mode == "write":
                    try:
                        os.mkdir(
                            self.state_path.parent.name,
                            0o700,
                            dir_fd=self._knowledge_root_fd,
                        )
                    except FileExistsError:
                        pass
                try:
                    self._state_dir_fd = os.open(
                        self.state_path.parent.name,
                        directory_flags,
                        dir_fd=self._knowledge_root_fd,
                    )
                except OSError as exc:
                    raise ConfigurationError(
                        "state directory is unsafe or unavailable"
                    ) from exc
                state_identity_stat = os.fstat(self._state_dir_fd)
                state_path_stat = os.stat(
                    self.state_path.parent, follow_symlinks=False
                )
                state_identity = (
                    state_identity_stat.st_dev,
                    state_identity_stat.st_ino,
                )
                if (
                    not stat.S_ISDIR(state_identity_stat.st_mode)
                    or (state_path_stat.st_dev, state_path_stat.st_ino)
                    != state_identity
                ):
                    raise ConfigurationError("state directory changed during secure setup")
                if stat.S_IMODE(state_identity_stat.st_mode) & 0o022:
                    raise ConfigurationError(
                        "state directory must not be group/world writable"
                    )
                if index_mode == "read":
                    # Hold a shared lock for the complete inspection lifetime.
                    # It blocks Qingtian ingestion's exclusive lock while the
                    # immutable SQLite snapshot is being queried, without ever
                    # creating a lock file or changing the state database.
                    import fcntl

                    self._read_lock_fd = self._open_vault_root()
                    try:
                        fcntl.flock(
                            self._read_lock_fd,
                            fcntl.LOCK_SH | fcntl.LOCK_NB,
                        )
                    except BlockingIOError as exc:
                        raise KnowledgeError(
                            "another knowledge write is running for this Vault"
                        ) from exc
                    self._verify_vault_fd_chain(self._read_lock_fd, [])
                    self.index = KnowledgeIndex(
                        self.state_path,
                        state_dir_fd=self._state_dir_fd,
                        state_dir_identity=state_identity,
                        read_only=True,
                    )
                else:
                    # Constructor DDL/FTS maintenance and ingestion share one
                    # stable namespace lock: the pinned Vault-root inode.
                    with self._exclusive_vault_operation_lock("initialization"):
                        self.index = KnowledgeIndex(
                            self.state_path,
                            state_dir_fd=self._state_dir_fd,
                            state_dir_identity=state_identity,
                        )
                    if os.name == "posix":
                        os.fchmod(self._state_dir_fd, 0o700)
            self.repo_context = self._repository_contexts()
        except Exception:
            if self.index is not None:
                self.index.close()
            if self._read_lock_fd >= 0:
                try:
                    import fcntl

                    fcntl.flock(self._read_lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._read_lock_fd)
                    self._read_lock_fd = -1
            if self._state_dir_fd >= 0:
                os.close(self._state_dir_fd)
                self._state_dir_fd = -1
            if self._knowledge_root_fd >= 0:
                os.close(self._knowledge_root_fd)
                self._knowledge_root_fd = -1
            raise

    def _assert_config_unchanged(self) -> None:
        """Bind one engine instance and every receipt to one parsed byte snapshot."""
        try:
            current = digest_file(self.config_path)
        except OSError as exc:
            raise ConfigurationError("sources configuration became unavailable") from exc
        if current != self._config_snapshot_hash:
            raise ConfigurationError("sources configuration changed during this operation")

    def _validate_config(self) -> None:
        required_paths = ("workspace_root", "vault_root", "state_db")
        for key in required_paths:
            if not isinstance(self.config.get(key), str) or not self.config[key].strip():
                raise ConfigurationError(f"{key} must be a non-empty path string")
        max_bytes = self.config.get("max_text_bytes", 1024 * 1024)
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= 16 * 1024 * 1024:
            raise ConfigurationError("max_text_bytes must be between 1 and 16777216")
        max_source_bytes = self.config.get("max_source_bytes", max(max_bytes, 32 * 1024 * 1024))
        if (
            not isinstance(max_source_bytes, int)
            or max_source_bytes < max_bytes
            or max_source_bytes > 256 * 1024 * 1024
        ):
            raise ConfigurationError(
                "max_source_bytes must be at least max_text_bytes and at most 268435456"
            )
        excluded_globs = self.config.get("hard_excluded_globs", [])
        if not isinstance(excluded_globs, list) or not all(
            isinstance(pattern, str) and pattern for pattern in excluded_globs
        ):
            raise ConfigurationError("hard_excluded_globs must be a list of patterns")

        def reject_sensitive_metadata(label: str, value: str) -> None:
            if any(regex.search(value) for regex in SECRET_PATTERNS.values()) or any(
                regex.search(value) for regex in PII_PATTERNS.values()
            ):
                raise ConfigurationError(f"{label} contains secret/PII-shaped metadata")

        source_ids: set[str] = set()
        allowed_evidence = {"E1", "E2"}
        source_sets = self.config.get("source_sets", [])
        if not isinstance(source_sets, list):
            raise ConfigurationError("source_sets must be a list")
        for item in source_sets:
            if not isinstance(item, dict):
                raise ConfigurationError("source_sets entries must be objects")
            source_id = item.get("id")
            if not isinstance(source_id, str) or not source_id or source_id in source_ids:
                raise ConfigurationError("source_sets ids must be unique non-empty strings")
            source_ids.add(source_id)
            reject_sensitive_metadata("source set id", source_id)
            if item.get("classification") not in CLASSIFICATION_RANK:
                raise ConfigurationError(f"invalid classification for source set {source_id}")
            if item.get("evidence_level") not in allowed_evidence:
                raise ConfigurationError(f"invalid evidence level for source set {source_id}")
            for key in ("root", "project", "category", "claim_scope"):
                if not isinstance(item.get(key), str) or not item[key]:
                    raise ConfigurationError(f"source set {source_id} has invalid {key}")
                reject_sensitive_metadata(f"source set {source_id} {key}", item[key])
            includes = item.get("includes")
            if not isinstance(includes, list) or not all(
                isinstance(pattern, str) and pattern for pattern in includes
            ):
                raise ConfigurationError(f"source set {source_id} has invalid includes")

        for collection in ("repositories", "test_roots"):
            seen_projects: set[str] = set()
            entries = self.config.get(collection, [])
            if not isinstance(entries, list):
                raise ConfigurationError(f"{collection} must be a list")
            for item in entries:
                if not isinstance(item, dict):
                    raise ConfigurationError(f"{collection} entries must be objects")
                project = item.get("project")
                root = item.get("root")
                if not isinstance(project, str) or not project or project in seen_projects:
                    raise ConfigurationError(f"{collection} projects must be unique")
                if not isinstance(root, str) or not root:
                    raise ConfigurationError(f"{collection} root must be a non-empty string")
                reject_sensitive_metadata(f"{collection} project", project)
                reject_sensitive_metadata(f"{collection} root", root)
                seen_projects.add(project)
                if collection == "test_roots":
                    includes = item.get("includes")
                    if not isinstance(includes, list) or not all(
                        isinstance(pattern, str) and pattern for pattern in includes
                    ):
                        raise ConfigurationError(f"test root {project} has invalid includes")
                else:
                    canonical_ref = item.get("canonical_ref")
                    authority_state = item.get("authority_state")
                    if (
                        not isinstance(canonical_ref, str)
                        or not SAFE_CANONICAL_REF.fullmatch(canonical_ref)
                        or ".." in canonical_ref.split("/")
                        or "//" in canonical_ref
                        or canonical_ref.endswith(("/", ".lock"))
                    ):
                        raise ConfigurationError(
                            f"repository {project} has an invalid canonical_ref"
                        )
                    reject_sensitive_metadata(
                        f"repository {project} canonical_ref", canonical_ref
                    )
                    if authority_state not in ALLOWED_AUTHORITY_STATES:
                        raise ConfigurationError(
                            f"repository {project} has an invalid authority_state"
                        )

    def close(self) -> None:
        unchanged_error: BaseException | None = None
        if self.index is not None:
            try:
                self.index.assert_read_only_unchanged()
            except BaseException as exc:
                unchanged_error = exc
            finally:
                self.index.close()
                self.index = None
        if self._read_lock_fd >= 0:
            try:
                import fcntl

                fcntl.flock(self._read_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._read_lock_fd)
                self._read_lock_fd = -1
        if self._state_dir_fd >= 0:
            os.close(self._state_dir_fd)
            self._state_dir_fd = -1
        if self._knowledge_root_fd >= 0:
            os.close(self._knowledge_root_fd)
            self._knowledge_root_fd = -1
        if unchanged_error is not None:
            raise unchanged_error

    @contextmanager
    def _exclusive_vault_operation_lock(self, operation: str) -> Iterator[int]:
        """Serialize writers on the stable Vault inode, not a replaceable name."""

        vault_lock_fd = self._open_vault_root()
        import fcntl

        try:
            try:
                fcntl.flock(vault_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise KnowledgeError(
                    f"another knowledge {operation} is already running for this Vault"
                ) from exc
            self._verify_vault_fd_chain(vault_lock_fd, [])
            yield vault_lock_fd
        finally:
            try:
                fcntl.flock(vault_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(vault_lock_fd)

    @contextmanager
    def _exclusive_ingest_lock(self) -> Iterator[None]:
        # The Vault root inode is the stable mutual-exclusion authority.  A
        # state-directory or ingest.lock namespace replacement therefore cannot
        # create a second independent ingestion lock for the same Vault.
        import fcntl

        with self._exclusive_vault_operation_lock("ingestion") as vault_lock_fd:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                anchored_parent = os.fstat(self._state_dir_fd)
                named_parent = os.stat(self.state_path.parent, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(named_parent.st_mode)
                    or (named_parent.st_dev, named_parent.st_ino)
                    != (anchored_parent.st_dev, anchored_parent.st_ino)
                ):
                    raise KnowledgeError("knowledge ingestion lock parent changed")
                descriptor = os.open(
                    "ingest.lock",
                    flags,
                    0o600,
                    dir_fd=self._state_dir_fd,
                )
            except OSError as exc:
                raise KnowledgeError(
                    "knowledge ingestion lock is unsafe or unavailable"
                ) from exc
            try:
                lock_stat = os.fstat(descriptor)
                named_parent = os.stat(self.state_path.parent, follow_symlinks=False)
                if (
                    not stat.S_ISREG(lock_stat.st_mode)
                    or lock_stat.st_nlink != 1
                    or not stat.S_ISDIR(named_parent.st_mode)
                    or (named_parent.st_dev, named_parent.st_ino)
                    != (anchored_parent.st_dev, anchored_parent.st_ino)
                ):
                    raise KnowledgeError("knowledge ingestion lock is not a regular file")
                if os.name == "posix":
                    os.fchmod(descriptor, 0o600)
            except Exception:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "r+b") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise KnowledgeError(
                        "another knowledge ingestion is already running"
                    ) from exc
                try:
                    self._verify_vault_fd_chain(vault_lock_fd, [])
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _repository_contexts(self) -> dict[str, dict[str, Any]]:
        contexts: dict[str, dict[str, Any]] = {}
        for item in self.config.get("repositories", []):
            project = item["project"]
            root = (self.workspace / item["root"]).resolve()
            try:
                root.relative_to(self.workspace)
            except ValueError as exc:
                raise ConfigurationError(f"repository root escaped workspace: {item['root']}") from exc
            if not root.is_dir():
                contexts[project] = {
                    "root": root,
                    "head": None,
                    "branch": None,
                    "status": {},
                    "tracked": set(),
                    "shallow": False,
                    "canonical_ref": item.get("canonical_ref"),
                    "authority_state": item.get("authority_state", "candidate"),
                }
                continue
            head_result = _run(["git", "rev-parse", "HEAD"], cwd=root)
            if head_result.returncode != 0:
                contexts[project] = {
                    "root": root,
                    "head": None,
                    "branch": None,
                    "status": {},
                    "tracked": set(),
                    "shallow": False,
                    "canonical_ref": item.get("canonical_ref"),
                    "authority_state": item.get("authority_state", "candidate"),
                }
                continue
            branch_result = _run(["git", "branch", "--show-current"], cwd=root)
            tracked_result = _run(["git", "ls-files", "-z"], cwd=root, timeout=120)
            tracked = set(tracked_result.stdout.split("\0")) if tracked_result.returncode == 0 else set()
            tracked.discard("")
            status_result = _run(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root
            )
            status: dict[str, str] = {}
            if status_result.returncode == 0:
                entries = status_result.stdout.split("\0")
                index = 0
                while index < len(entries):
                    entry = entries[index]
                    if len(entry) >= 4:
                        state, path = entry[:2], entry[3:]
                        status[path] = state
                        if state[0] in {"R", "C"} or state[1] in {"R", "C"}:
                            index += 1
                            if index < len(entries) and entries[index]:
                                status[entries[index]] = state
                    index += 1
            shallow_result = _run(["git", "rev-parse", "--is-shallow-repository"], cwd=root)
            branch_name = branch_result.stdout.strip() or None
            if branch_name and (
                any(regex.search(branch_name) for regex in SECRET_PATTERNS.values())
                or any(regex.search(branch_name) for regex in PII_PATTERNS.values())
            ):
                raise ConfigurationError(
                    f"repository {project} branch contains secret/PII-shaped metadata"
                )
            contexts[project] = {
                "root": root,
                "head": head_result.stdout.strip(),
                "branch": branch_name,
                "status": status,
                "tracked": tracked,
                "shallow": shallow_result.stdout.strip() == "true",
                "canonical_ref": item.get("canonical_ref"),
                "authority_state": item.get("authority_state", "candidate"),
            }
        return contexts

    def _excluded(self, relative: Path) -> bool:
        return any(part in self.excluded_parts for part in relative.parts)

    def _hard_excluded(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        lowered_name = resolved.name.lower()
        if resolved.suffix.lower() in {
            ".key",
            ".pem",
            ".p12",
            ".pfx",
            ".keystore",
            ".db",
            ".sqlite",
            ".sqlite3",
            ".har",
            ".trace",
            ".rej",
        } or lowered_name == ".env" or lowered_name.startswith(".env."):
            return True
        try:
            resolved.relative_to(self.knowledge_root)
            return True
        except ValueError:
            pass
        try:
            relative = resolved.relative_to(self.workspace).as_posix()
        except ValueError:
            return True
        return any(fnmatch(relative, pattern) for pattern in self.hard_excluded_globs)

    def _repo_for_path(self, path: Path) -> dict[str, Any] | None:
        matches: list[dict[str, Any]] = []
        for context in self.repo_context.values():
            try:
                path.relative_to(context["root"])
            except ValueError:
                continue
            matches.append(context)
        return max(matches, key=lambda item: len(item["root"].parts), default=None)

    def _refresh_source_git_metadata(self, record: SourceRecord) -> SourceRecord:
        repo = self._repo_for_path(record.source_path)
        if not repo or not repo.get("head"):
            return record
        relative = record.source_path.relative_to(repo["root"]).as_posix()
        head = _run(["git", "rev-parse", "HEAD"], cwd=repo["root"])
        if head.returncode != 0 or head.stdout.strip() != record.repo_head:
            raise SourceChangedDuringIngest(record.relative_path)
        status_result = _run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", relative],
            cwd=repo["root"],
        )
        if status_result.returncode != 0:
            return replace(record, git_state="git-status-unavailable")
        entry = status_result.stdout.split("\0", 1)[0]
        if entry.startswith("??"):
            state = "untracked"
        elif len(entry) >= 2:
            state = "worktree:" + entry[:2].replace(" ", "_")
        elif relative in repo["tracked"]:
            state = "tracked-clean"
        else:
            state = "ignored-or-untracked"
        repo_blob = record.repo_blob
        if state == "tracked-clean":
            worktree_blob = _run(["git", "hash-object", "--", relative], cwd=repo["root"])
            if worktree_blob.returncode != 0 or not repo_blob:
                state = "git-blob-unavailable"
            elif worktree_blob.stdout.strip() != repo_blob:
                state = "worktree:content-differs-from-head"
        branch = _run(["git", "branch", "--show-current"], cwd=repo["root"])
        refreshed_branch = (
            branch.stdout.strip() or None if branch.returncode == 0 else record.repo_branch
        )
        if refreshed_branch and (
            any(regex.search(refreshed_branch) for regex in SECRET_PATTERNS.values())
            or any(regex.search(refreshed_branch) for regex in PII_PATTERNS.values())
        ):
            raise ConfigurationError("repository branch contains secret/PII-shaped metadata")
        return replace(
            record,
            git_state=state,
            repo_branch=refreshed_branch,
        )

    def _discover_pattern(self, root: Path, pattern: str) -> Iterator[Path]:
        for path in root.glob(pattern):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            cursor = root
            traverses_symlink = False
            for part in relative.parts[:-1]:
                cursor = cursor / part
                if cursor.is_symlink():
                    traverses_symlink = True
                    break
            if (
                self._excluded(relative)
                or self._hard_excluded(path)
                or traverses_symlink
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            try:
                path.resolve(strict=True).relative_to(root)
            except (FileNotFoundError, ValueError):
                continue
            yield path

    def _read_source_file(
        self, root: Path, path: Path, *, retain_limit: int
    ) -> tuple[bytes, str, int, str]:
        """Read one regular allowlisted file through no-follow dirfds.

        Parent components and the final file are opened once without following
        symlinks.  The full file is hashed as a stream, while only a configured
        bounded prefix is retained in memory.
        """
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise SourceChangedDuringIngest("source escaped allowlisted root") from exc
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise SourceChangedDuringIngest("source path is unsafe")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        file_descriptor: int | None = None
        try:
            current = os.open(root, directory_flags)
            descriptors.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                descriptors.append(current)
            file_descriptor = os.open(relative.parts[-1], file_flags, dir_fd=current)
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise SourceChangedDuringIngest("allowlisted source is not a regular file")
            if before.st_size > self.max_source_bytes:
                raise KnowledgeError(
                    "allowlisted source exceeds configured max_source_bytes"
                )
            hasher = sha256()
            retained = bytearray()
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                remaining = max(0, retain_limit - len(retained))
                if remaining:
                    retained.extend(chunk[:remaining])
            after = os.fstat(file_descriptor)
            current_entry = os.stat(
                relative.parts[-1], dir_fd=current, follow_symlinks=False
            )
            fingerprint = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
            )
            if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(
                current_entry
            ):
                raise SourceChangedDuringIngest("allowlisted source changed during read")
            modified = datetime.fromtimestamp(before.st_mtime, UTC).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
            return bytes(retained), hasher.hexdigest(), before.st_size, modified
        except OSError as exc:
            raise SourceChangedDuringIngest("allowlisted source became unsafe or unavailable") from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def discover_sources(self) -> list[SourceRecord]:
        discovered: dict[str, SourceRecord] = {}
        for source_set in self.config.get("source_sets", []):
            root = (self.workspace / source_set["root"]).resolve()
            try:
                root.relative_to(self.workspace)
            except ValueError as exc:
                raise ConfigurationError(
                    f"source root escaped workspace: {source_set['root']}"
                ) from exc
            if not root.is_dir():
                continue
            for pattern in source_set.get("includes", []):
                for path in self._discover_pattern(root, pattern):
                    relative = path.relative_to(root).as_posix()
                    locator = f"{source_set['id']}:{relative}"
                    knowledge_id = "src-" + sha256(locator.encode("utf-8")).hexdigest()[:20]
                    preview, source_hash, source_size, source_modified = self._read_source_file(
                        root, path, retain_limit=self.max_text_bytes
                    )
                    suffix = path.suffix.lower()
                    kind = "pdf" if suffix == ".pdf" else "text"
                    title = self._title(path, preview)
                    category = self._category(source_set["category"], relative)
                    repo = self._repo_for_path(path)
                    git_state = "outside-git"
                    repo_head = None
                    repo_branch = None
                    repo_blob = None
                    if repo:
                        repo_head = repo["head"]
                        repo_branch = repo["branch"]
                        try:
                            repo_relative = path.relative_to(repo["root"]).as_posix()
                        except ValueError:
                            repo_relative = ""
                        raw_state = repo["status"].get(repo_relative)
                        if raw_state == "??":
                            git_state = "untracked"
                        elif raw_state:
                            git_state = "worktree:" + raw_state.replace(" ", "_")
                        elif repo_relative in repo["tracked"]:
                            git_state = "tracked-clean"
                        else:
                            git_state = "ignored-or-untracked"
                        if repo_head and repo_relative in repo["tracked"]:
                            blob_result = _run(
                                ["git", "rev-parse", f"HEAD:{repo_relative}"], cwd=repo["root"]
                            )
                            if blob_result.returncode == 0:
                                repo_blob = blob_result.stdout.strip()
                    scan_text = (
                        ""
                        if suffix == ".pdf"
                        else preview.decode("utf-8", errors="replace")
                    )
                    # Persisted metadata is part of the privacy boundary too: a
                    # secret/PII-shaped filename, H1, locator, or source label
                    # must take the same opaque quarantine path as body content.
                    scan_payload = "\n".join(
                        (
                            scan_text,
                            relative,
                            locator,
                            title,
                            source_set["id"],
                            source_set["project"],
                            category,
                        )
                    )
                    secret_kinds = tuple(
                        name for name, regex in SECRET_PATTERNS.items() if regex.search(scan_payload)
                    )
                    pii = (
                        "possible"
                        if any(regex.search(scan_payload) for regex in PII_PATTERNS.values())
                        else "none"
                    )
                    record = SourceRecord(
                        knowledge_id=knowledge_id,
                        source_set=source_set["id"],
                        project=source_set["project"],
                        category=category,
                        classification=source_set["classification"],
                        evidence_level=source_set["evidence_level"],
                        claim_scope=source_set["claim_scope"],
                        source_path=path,
                        relative_path=relative,
                        source_hash=source_hash,
                        size_bytes=source_size,
                        modified_at=source_modified,
                        title=title,
                        kind=kind,
                        repo_head=repo_head,
                        repo_branch=repo_branch,
                        repo_blob=repo_blob,
                        git_state=git_state,
                        pii=pii,
                        secret_kinds=secret_kinds,
                    )
                    record = replace(
                        record,
                        transform_fingerprint=self._transform_fingerprint(record),
                    )
                    discovered[knowledge_id] = record
        return sorted(discovered.values(), key=lambda item: (item.project, item.relative_path))

    def _transform_fingerprint(self, record: SourceRecord) -> str:
        """Fingerprint every input that can change a rendered/indexed source."""
        payload = {
            "pipeline": EXTRACTOR_VERSION,
            "max_text_bytes": self.max_text_bytes,
            "max_source_bytes": self.max_source_bytes,
            "source_set": record.source_set,
            "project": record.project,
            "category": record.category,
            "classification": record.classification,
            "evidence_level": record.evidence_level,
            "claim_scope": record.claim_scope,
            "title": record.title,
            "kind": record.kind,
        }
        return "transform/" + digest_bytes(strict_json(payload).encode("utf-8"))

    @staticmethod
    def _title(path: Path, preview: bytes) -> str:
        if path.suffix.lower() == ".md":
            for line in preview.decode("utf-8", errors="replace").splitlines()[:80]:
                if line.startswith("# "):
                    return line[2:].strip() or path.stem
        return path.stem.replace("_", " ").replace("-", " ").strip()

    @staticmethod
    def _category(default: str, relative: str) -> str:
        lowered = relative.lower()
        if BUG_WORDS.search(relative):
            return "bugs-and-fixes"
        if any(word in lowered for word in ("test", "e2e", "accept", "qa", "测试", "验收")):
            return "testing"
        if any(word in lowered for word in ("deploy", "ops", "runbook", "release", "迁移", "发布")):
            return "operations"
        if any(word in lowered for word in ("architecture", "design", "adr", "架构", "方案")):
            return "architecture"
        return default

    @staticmethod
    def _effective_evidence(record: SourceRecord) -> tuple[str, str, str]:
        """Return evidence, claim scope and freshness without promoting worktree drafts."""
        if record.git_state in {"tracked-clean", "outside-git"}:
            evidence = record.evidence_level
            scope = record.claim_scope
        else:
            evidence = "E1"
            scope = "working-tree-observation"
        freshness = "current" if evidence == "E2" and record.git_state == "tracked-clean" else "unknown"
        return evidence, scope, freshness

    @staticmethod
    def _source_revision(record: SourceRecord) -> str:
        if record.git_state == "tracked-clean" and record.repo_head:
            blob = f":blob:{record.repo_blob}" if record.repo_blob else ""
            return f"git:{record.repo_head}{blob}"
        if record.repo_head:
            return f"WORKTREE:{record.repo_head}:sha256:{record.source_hash}"
        return f"sha256:{record.source_hash}"

    def _assert_source_unchanged(self, record: SourceRecord) -> None:
        root = (self.workspace / next(
            item["root"]
            for item in self.config.get("source_sets", [])
            if item["id"] == record.source_set
        )).resolve()
        _, source_hash, size_bytes, modified_at = self._read_source_file(
            root, record.source_path, retain_limit=0
        )
        if (
            size_bytes != record.size_bytes
            or modified_at != record.modified_at
            or source_hash != record.source_hash
        ):
            raise SourceChangedDuringIngest(record.relative_path)

    def plan(self) -> dict[str, Any]:
        self._assert_config_unchanged()
        records = self.discover_sources()
        self._assert_config_unchanged()
        return {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "vault": str(self.vault),
            "source_count": len(records),
            "by_project": dict(sorted(Counter(item.project for item in records).items())),
            "by_category": dict(sorted(Counter(item.category for item in records).items())),
            "by_kind": dict(sorted(Counter(item.kind for item in records).items())),
            "quarantine_candidates": sum(item.quarantined for item in records),
            "possible_pii": sum(item.pii == "possible" for item in records),
            "working_tree_candidates": sum(
                item.git_state not in {"tracked-clean", "outside-git"} for item in records
            ),
            "outside_git": sum(item.git_state == "outside-git" for item in records),
        }

    def _extract_body(self, record: SourceRecord) -> tuple[str, dict[str, Any]]:
        root = (self.workspace / next(
            item["root"]
            for item in self.config.get("source_sets", [])
            if item["id"] == record.source_set
        )).resolve()
        if record.kind == "pdf":
            raw, source_hash, size_bytes, modified_at = self._read_source_file(
                root, record.source_path, retain_limit=self.max_source_bytes
            )
            if (
                source_hash != record.source_hash
                or size_bytes != record.size_bytes
                or modified_at != record.modified_at
            ):
                raise SourceChangedDuringIngest(record.relative_path)
            return self._extract_pdf(raw)
        raw, source_hash, size_bytes, modified_at = self._read_source_file(
            root, record.source_path, retain_limit=self.max_text_bytes
        )
        if (
            source_hash != record.source_hash
            or size_bytes != record.size_bytes
            or modified_at != record.modified_at
        ):
            raise SourceChangedDuringIngest(record.relative_path)
        truncated = record.size_bytes > self.max_text_bytes
        text = raw.decode("utf-8", errors="replace")
        if record.source_path.suffix.lower() == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True)
            except json.JSONDecodeError:
                pass
        return text, {"truncated": truncated, "extractor": "utf-8"}

    def _extract_pdf(self, raw: bytes) -> tuple[str, dict[str, Any]]:
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(BytesIO(raw))
            pages: list[str] = []
            total = 0
            for index, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                encoded = text.encode("utf-8")
                remaining = self.max_text_bytes - total
                if remaining <= 0:
                    break
                pages.append(f"\n\n## PDF page {index + 1}\n\n" + encoded[:remaining].decode("utf-8", errors="ignore"))
                total += min(len(encoded), remaining)
            return "".join(pages).strip(), {
                "truncated": total >= self.max_text_bytes,
                "extractor": "pypdf",
                "pages": len(reader.pages),
            }
        except Exception as exc:
            return "", {
                "truncated": False,
                "extractor": "unavailable",
                "error": type(exc).__name__,
            }

    def _vault_relative(self, record: SourceRecord) -> Path:
        relative = PurePosixPath(record.relative_path)
        safe_parts = [part.replace(":", "-") for part in relative.parts]
        leaf = safe_parts[-1]
        if not leaf.lower().endswith(".md"):
            leaf += ".md"
        safe_parts[-1] = leaf
        return Path("90-Sources") / record.source_set / Path(*safe_parts)

    def _render_source(self, record: SourceRecord, body: str, extraction: dict[str, Any], run_id: str) -> str:
        evidence_level, claim_scope, freshness = self._effective_evidence(record)
        acceptance = (
            "lead-only"
            if evidence_level == "E1"
            or record.category in {"testing", "bugs-and-fixes", "historical-evidence"}
            else "not-applicable"
        )
        warning = (
            "> [!warning] Historical lead only\n"
            "> Historical sources and previously captured results are investigative leads only; "
            "they do not prove current behavior.\n"
            "> Acceptance requires evidence produced by the current verification run in the "
            "target environment.\n\n"
            if acceptance == "lead-only"
            else "> [!note] Evidence boundary\n> 本页证明来源内容，不自动证明产品要求、部署状态或运行行为。\n\n"
        )
        source_revision = self._source_revision(record)
        frontmatter = [
            "---",
            'schema_version: "1.0"',
            f"id: {_safe_yaml(record.knowledge_id)}",
            'type: "source"',
            f"title: {_safe_yaml(record.title)}",
            'knowledge_status: "candidate"',
            'lifecycle_status: "active"',
            f"project: {_safe_yaml(record.project)}",
            f"category: {_safe_yaml(record.category)}",
            f"evidence_level: {_safe_yaml(evidence_level)}",
            f"claim_scope: {_safe_yaml(claim_scope)}",
            'managed_by: "qingtian"',
            "human_lock: false",
            'review_status: "pending"',
            'conflict_status: "none"',
            f"freshness_status: {_safe_yaml(freshness)}",
            f"privacy_classification: {_safe_yaml(record.classification)}",
            f"pii: {_safe_yaml(record.pii)}",
            f"acceptance_evidence: {_safe_yaml(acceptance)}",
            f"source_locator: {_safe_yaml('workspace:' + record.source_set + '/' + record.relative_path)}",
            f"source_revision: {_safe_yaml(source_revision)}",
            f"source_sha256: {_safe_yaml(record.source_hash)}",
            f"source_modified_at: {_safe_yaml(record.modified_at)}",
            f"captured_at: {_safe_yaml(utc_now())}",
            f"git_state: {_safe_yaml(record.git_state)}",
            f"repo_branch: {_safe_yaml(record.repo_branch)}",
            f"repo_head: {_safe_yaml(record.repo_head)}",
            f"repo_blob: {_safe_yaml(record.repo_blob)}",
            f"ingestion_run_id: {_safe_yaml(run_id)}",
            f"extractor: {_safe_yaml(extraction.get('extractor', 'unknown'))}",
            f"truncated: {str(bool(extraction.get('truncated'))).lower()}",
            "---",
            MANAGED_MARKER,
            "",
            f"# {record.title}",
            "",
            warning.rstrip(),
            "",
            "## Provenance",
            "",
            f"- Source set: `{record.source_set}`",
            f"- Relative path: `{record.relative_path}`",
            f"- SHA-256: `{record.source_hash}`",
            f"- Git state: `{record.git_state}`",
            f"- Extractor: `{extraction.get('extractor', 'unknown')}`",
        ]
        if "pages" in extraction:
            frontmatter.append(f"- PDF pages: `{extraction['pages']}`")
        if extraction.get("error"):
            frontmatter.append(f"- Extraction unavailable: `{extraction['error']}`")
        frontmatter.extend(["", "## Source content", ""])
        if body:
            suffix = PurePosixPath(record.relative_path).suffix.lower()
            # Every imported body, including Markdown, is evidence rather than
            # Vault navigation.  A dynamic CommonMark fence prevents untrusted
            # relative links, wiki links, tags, embeds, and source frontmatter
            # from creating phantom Obsidian nodes while preserving the exact
            # searchable text in both the note and SQLite index.
            longest_ticks = max(
                (len(match.group(0)) for match in re.finditer(r"`+", body)),
                default=0,
            )
            fence = "`" * max(3, longest_ticks + 1)
            language = {
                ".md": "markdown",
                ".json": "json",
                ".yaml": "yaml",
                ".yml": "yaml",
                ".toml": "toml",
                ".csv": "csv",
                ".tsv": "tsv",
            }.get(suffix, "text")
            frontmatter.extend(
                [
                    "> Rendered as inert source evidence; source syntax is not Vault navigation.",
                    "",
                    f"{fence}{language}\n{body}\n{fence}",
                ]
            )
        else:
            frontmatter.append("_No searchable body was imported. Use the provenance record to review the source._")
        return "\n".join(frontmatter).rstrip() + "\n"

    def _render_quarantine(self, record: SourceRecord, _run_id: str) -> str:
        risks = list(record.secret_kinds)
        if record.pii != "none":
            risks.append("possible-pii")
        if record.classification in {"P2-confidential", "P3-restricted"}:
            risks.append(record.classification)
        risks = sorted(set(risks)) or ["policy-restricted"]
        return (
            "---\n"
            'schema_version: "1.0"\n'
            f"id: {_safe_yaml('quarantine-' + record.knowledge_id)}\n"
            'type: "quarantine-record"\n'
            f"title: {_safe_yaml('Quarantined source ' + record.knowledge_id)}\n"
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "pending"\n'
            'privacy_classification: "P2-confidential"\n'
            f"source_id: {_safe_yaml(record.knowledge_id)}\n"
            f"source_sha256: {_safe_yaml(record.source_hash)}\n"
            f"risk_kinds: {_safe_yaml(risks)}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            f"# Quarantined source `{record.knowledge_id}`\n\n"
            "正文未进入 Vault 或检索索引。这里只保留来源 hash 与风险类别；不得粘贴命中值。\n"
        )

    def _resolve_quarantine_record(
        self,
        record: SourceRecord,
        run_id: str,
        quarantine_generated: sqlite3.Row | None,
    ) -> None:
        """Replace an obsolete quarantine claim with an explicit lineage resolution."""
        if quarantine_generated is None or quarantine_generated["status"] == "resolved":
            return
        relative = Path("95-Reviews") / "Quarantine" / f"{record.knowledge_id}.md"
        content = (
            "---\n"
            'schema_version: "1.0"\n'
            f"id: {_safe_yaml('quarantine-resolution-' + record.knowledge_id)}\n"
            'type: "quarantine-resolution"\n'
            f"title: {_safe_yaml('Resolved quarantine ' + record.knowledge_id)}\n"
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "pending"\n'
            'conflict_status: "resolved"\n'
            'freshness_status: "current"\n'
            'privacy_classification: "P1-internal"\n'
            f"source_id: {_safe_yaml(record.knowledge_id)}\n"
            f"source_sha256: {_safe_yaml(record.source_hash)}\n"
            f"resolved_in_run: {_safe_yaml(run_id)}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            "# Quarantine resolved\n\n"
            "This lineage record no longer asserts that the current source is quarantined. "
            "The current classification and scanner result permit a separate active mirror; "
            "human review is still pending.\n"
        )
        vault_path, output_hash = self._write_managed(
            relative,
            content,
            previous_hash=str(quarantine_generated["output_hash"]),
            run_id=run_id,
            conflict_id="quarantine-resolution-" + record.knowledge_id,
        )
        self.index.upsert_generated(
            "source-quarantine-" + record.knowledge_id,
            vault_path,
            output_hash,
            run_id,
            "resolved",
        )

    def _retire_active_mirror_for_restriction(
        self,
        record: SourceRecord,
        previous: sqlite3.Row | None,
        active_generated: sqlite3.Row | None,
        run_id: str,
    ) -> None:
        previous_path = str(previous["vault_path"]) if previous and previous["vault_path"] else None
        generated_path = str(active_generated["vault_path"]) if active_generated else None
        previous_status = str(previous["status"]) if previous else None
        if not active_generated and (previous is None or previous_status == "quarantined"):
            # A newly discovered restricted source, or an already quarantined one,
            # has no active Vault mirror to retire.
            return
        if (
            previous_status == "active"
            and previous_path
            and generated_path
            and previous_path != generated_path
        ):
            self._write_restriction_conflict(record, run_id, "baseline-path-mismatch")
        relative = Path(generated_path or previous_path or self._vault_relative(record).as_posix())
        if not relative.as_posix().startswith("90-Sources/"):
            self._write_restriction_conflict(record, run_id, "unsafe-prior-mirror-path")
        self._safe_vault_target(relative, create_parent=False)
        flags = (
            os.O_RDONLY
            | self._required_open_flag("O_NOFOLLOW")
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            with self._anchored_vault_parent(relative, create_parent=False) as (
                parent_fd,
                _chain,
            ):
                descriptor = os.open(relative.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            self.index.remove_generated(f"source-active-{record.knowledge_id}")
            return
        except (OSError, KnowledgeError):
            self._write_restriction_conflict(record, run_id, "prior-mirror-open-failed")
        if not active_generated:
            os.close(descriptor)
            self._write_restriction_conflict(record, run_id, "missing-active-mirror-baseline")
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                self._write_restriction_conflict(
                    record, run_id, "prior-mirror-is-not-a-regular-file"
                )
            hasher = sha256()
            managed_marker_seen = False
            marker_bytes = MANAGED_MARKER.encode("utf-8")
            overlap = b""
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                candidate = overlap + chunk
                if marker_bytes in candidate:
                    managed_marker_seen = True
                overlap = candidate[-len(marker_bytes) :]
            after = os.fstat(descriptor)
            current_hash = hasher.hexdigest()
        finally:
            os.close(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or not managed_marker_seen
        ):
            self._write_restriction_conflict(
                record,
                run_id,
                "prior-mirror-changed-or-unmanaged-during-verification",
                current_hash=current_hash,
                baseline_hash=str(active_generated["output_hash"]),
            )
        if current_hash != active_generated["output_hash"]:
            self._write_restriction_conflict(
                record,
                run_id,
                "prior-mirror-changed-since-baseline",
                current_hash=current_hash,
                baseline_hash=str(active_generated["output_hash"]),
            )
        try:
            with self._anchored_vault_parent(relative, create_parent=False) as (
                parent_fd,
                _chain,
            ):
                current = os.stat(
                    relative.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
        except (OSError, KnowledgeError):
            self._write_restriction_conflict(record, run_id, "prior-mirror-disappeared")
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            self._write_restriction_conflict(record, run_id, "prior-mirror-replaced-before-retire")
        # This removes only a hash-verified Qingtian duplicate inside 90-Sources.
        # The original business source remains read-only and untouched.  Keeping a
        # stub at the old path would itself retain a newly sensitive filename.
        self._anchored_vault_unlink(
            relative,
            expected_identity=(after.st_dev, after.st_ino),
        )
        self.index.remove_generated(f"source-active-{record.knowledge_id}")

    def _write_restriction_conflict(
        self,
        record: SourceRecord,
        run_id: str,
        reason: str,
        *,
        current_hash: str | None = None,
        baseline_hash: str | None = None,
    ) -> None:
        """Record an opaque fail-closed privacy transition without leaking its path/body."""
        relative = (
            Path("95-Reviews")
            / "Conflicts"
            / f"restricted-{_safe_slug(record.knowledge_id)}-{_safe_slug(run_id)}.md"
        )
        path = self._safe_vault_target(relative, create_parent=True)
        body = (
            "---\n"
            'schema_version: "1.0"\n'
            f"id: {_safe_yaml('restricted-conflict-' + record.knowledge_id + '-' + _safe_slug(run_id))}\n"
            'type: "privacy-transition-conflict"\n'
            f"title: {_safe_yaml('Restricted transition incomplete ' + record.knowledge_id)}\n"
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "pending"\n'
            'conflict_status: "confirmed"\n'
            'freshness_status: "stale"\n'
            'privacy_classification: "P2-confidential"\n'
            f"source_id: {_safe_yaml(record.knowledge_id)}\n"
            f"reason: {_safe_yaml(reason)}\n"
            f"baseline_output_sha256: {_safe_yaml(baseline_hash)}\n"
            f"current_output_sha256: {_safe_yaml(current_hash)}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            "# Restricted transition incomplete\n\n"
            "Retrieval was disabled, but Qingtian could not prove that a prior mirror was safely "
            "retired. The ingestion is partial and requires an authorized human privacy review. "
            "No source path, title, body, or matched value is recorded here.\n"
        )
        self._atomic_write(path, body)
        output_hash = digest_bytes(body.encode("utf-8"))
        # Once the transition is unsafe, do not copy the prior pathname into
        # the portable manifest.  The opaque conflict keeps only hashes; an
        # authorized human must locate/retire any residual file locally.
        self.index.remove_generated("source-active-" + record.knowledge_id)
        self.index.upsert_generated(
            "restricted-conflict-" + record.knowledge_id + "-" + _safe_slug(run_id),
            relative.as_posix(),
            output_hash,
            run_id,
            "conflict",
        )
        raise HumanEditConflict(record.knowledge_id)

    def _write_managed(
        self,
        relative: Path,
        content: str,
        *,
        previous_hash: str | None,
        run_id: str,
        conflict_id: str,
        allow_recreate: bool = False,
    ) -> tuple[str, str]:
        target = self._safe_vault_target(relative, create_parent=True)
        desired_hash = digest_bytes(content.encode("utf-8"))
        conflict_reason: str | None = None
        existing_hash: str | None = None
        if target.exists():
            existing = target.read_bytes()
            existing_hash = digest_bytes(existing)
            if existing_hash == desired_hash:
                return relative.as_posix(), existing_hash
            if MANAGED_MARKER not in existing.decode("utf-8", errors="replace"):
                conflict_reason = "target-is-not-machine-managed"
            elif previous_hash is None:
                conflict_reason = "missing-last-output-baseline"
            elif existing_hash != previous_hash:
                conflict_reason = "target-changed-since-last-output"
        elif previous_hash is not None and not allow_recreate:
            conflict_reason = "target-was-deleted-or-moved"
        if conflict_reason:
            safe_conflict_id = _safe_slug(conflict_id)
            safe_run_id = _safe_slug(run_id)
            conflict_relative = (
                Path("95-Reviews")
                / "Conflicts"
                / f"{safe_conflict_id}-{safe_run_id}.md"
            )
            conflict_path = self._safe_vault_target(conflict_relative, create_parent=True)
            conflict = (
                "---\n"
                'schema_version: "1.0"\n'
                f"id: {_safe_yaml('conflict-' + safe_conflict_id + '-' + safe_run_id)}\n"
                'type: "conflict"\n'
                f"title: {_safe_yaml('Human edit conflict: ' + relative.as_posix())}\n"
                'managed_by: "qingtian"\n'
                "human_lock: false\n"
                'review_status: "pending"\n'
                'conflict_status: "confirmed"\n'
                'privacy_classification: "P1-internal"\n'
                f"target_path: {_safe_yaml(relative.as_posix())}\n"
                f"reason: {_safe_yaml(conflict_reason)}\n"
                f"baseline_output_sha256: {_safe_yaml(previous_hash)}\n"
                f"current_output_sha256: {_safe_yaml(existing_hash)}\n"
                f"proposed_output_sha256: {_safe_yaml(desired_hash)}\n"
                "---\n"
                f"{MANAGED_MARKER}\n\n"
                "# Human edit conflict\n\n"
                f"Qingtian refused to overwrite `{relative.as_posix()}`. Reason: "
                f"`{conflict_reason}`. Review and resolve manually. No source body or diff was copied.\n"
            )
            self._atomic_write(conflict_path, conflict)
            self.index.upsert_generated(
                "conflict-record-" + safe_conflict_id + "-" + safe_run_id,
                conflict_relative.as_posix(),
                digest_bytes(conflict.encode("utf-8")),
                run_id,
                "conflict",
            )
            raise HumanEditConflict(relative.as_posix())
        self._atomic_write(target, content)
        return relative.as_posix(), desired_hash

    @staticmethod
    def _validate_vault_relative(relative: Path) -> None:
        if (
            not relative.parts
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise KnowledgeError("generated vault path is not a safe relative path")

    @staticmethod
    def _required_open_flag(name: str) -> int:
        value = getattr(os, name, None)
        if not isinstance(value, int) or value == 0:
            raise KnowledgeError(f"secure Vault writes require {name}")
        return value

    def _directory_open_flags(self) -> int:
        return (
            os.O_RDONLY
            | self._required_open_flag("O_DIRECTORY")
            | self._required_open_flag("O_NOFOLLOW")
            | getattr(os, "O_CLOEXEC", 0)
        )

    def _open_vault_root(self) -> int:
        flags = self._directory_open_flags()
        descriptor: int | None = None
        try:
            descriptor = os.open(self.vault, flags)
            opened = os.fstat(descriptor)
            current = os.stat(self.vault, follow_symlinks=False)
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise KnowledgeError("vault root changed or contains a symlink") from exc
        assert descriptor is not None
        opened_identity = (opened.st_dev, opened.st_ino)
        current_identity = (current.st_dev, current.st_ino)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or opened_identity != self._vault_identity
            or current_identity != self._vault_identity
        ):
            os.close(descriptor)
            raise KnowledgeError("vault root identity changed")
        return descriptor

    def _verify_vault_fd_chain(
        self,
        root_fd: int,
        chain: list[tuple[str, int, int]],
    ) -> None:
        """Prove that every open directory fd is still attached below this Vault."""
        try:
            opened_root = os.fstat(root_fd)
            current_root = os.stat(self.vault, follow_symlinks=False)
        except OSError as exc:
            raise KnowledgeError("vault directory chain became unavailable") from exc
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or not stat.S_ISDIR(current_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino) != self._vault_identity
            or (current_root.st_dev, current_root.st_ino) != self._vault_identity
        ):
            raise KnowledgeError("vault root identity changed during write")
        for name, parent_fd, child_fd in chain:
            try:
                entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(child_fd)
            except OSError as exc:
                raise KnowledgeError("vault parent directory changed during write") from exc
            if (
                not stat.S_ISDIR(entry.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise KnowledgeError("vault parent directory changed during write")

    @contextmanager
    def _anchored_vault_parent(
        self,
        relative: Path,
        *,
        create_parent: bool,
    ) -> Iterator[tuple[int, list[tuple[str, int, int]]]]:
        """Open a target parent without ever following a path component symlink."""
        self._validate_vault_relative(relative)
        descriptors: list[int] = []
        chain: list[tuple[str, int, int]] = []
        flags = self._directory_open_flags()
        try:
            root_fd = self._open_vault_root()
            descriptors.append(root_fd)
            current_fd = root_fd
            for part in relative.parts[:-1]:
                if create_parent:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise KnowledgeError(
                            "cannot create generated vault parent safely"
                        ) from exc
                child_fd: int | None = None
                try:
                    child_fd = os.open(part, flags, dir_fd=current_fd)
                    entry = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                    opened = os.fstat(child_fd)
                except OSError as exc:
                    if child_fd is not None:
                        try:
                            os.close(child_fd)
                        except OSError:
                            pass
                    raise KnowledgeError(
                        "generated vault path contains a symlink or invalid directory"
                    ) from exc
                assert child_fd is not None
                if (
                    not stat.S_ISDIR(entry.st_mode)
                    or not stat.S_ISDIR(opened.st_mode)
                    or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    os.close(child_fd)
                    raise KnowledgeError(
                        "generated vault path contains a symlink or replaced directory"
                    )
                descriptors.append(child_fd)
                chain.append((part, current_fd, child_fd))
                current_fd = child_fd
            self._verify_vault_fd_chain(root_fd, chain)
            yield current_fd, chain
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _safe_vault_target(self, relative: Path, *, create_parent: bool) -> Path:
        """Return a lexical Vault path after safely preparing writable parents."""
        self._validate_vault_relative(relative)
        lexical_target = self.vault.joinpath(*relative.parts)
        if create_parent:
            with self._anchored_vault_parent(relative, create_parent=True) as (
                parent_fd,
                _chain,
            ):
                try:
                    target_state = os.stat(
                        relative.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    target_state = None
                except OSError as exc:
                    raise KnowledgeError("generated vault target cannot be inspected") from exc
                if target_state is not None and stat.S_ISLNK(target_state.st_mode):
                    raise KnowledgeError("generated vault path contains a symlink")
        else:
            # Read-side callers still receive a lexical path.  Never resolve a
            # potentially replaced component into a location outside the Vault.
            cursor = self.vault
            for part in relative.parts:
                cursor = cursor / part
                try:
                    state = os.lstat(cursor)
                except FileNotFoundError:
                    break
                except OSError as exc:
                    raise KnowledgeError("generated vault path cannot be inspected") from exc
                if stat.S_ISLNK(state.st_mode):
                    raise KnowledgeError("generated vault path contains a symlink")
        return lexical_target

    @staticmethod
    def _restore_staged_entry(parent_fd: int, staging_name: str, target_name: str) -> bool:
        """Restore a regular staged inode without overwriting a concurrent target."""
        try:
            os.link(
                staging_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.unlink(staging_name, dir_fd=parent_fd)
        return True

    def _anchored_vault_unlink(
        self,
        relative: Path,
        *,
        expected_identity: tuple[int, int],
    ) -> None:
        """Delete one verified regular file without pathname traversal races."""
        self._validate_vault_relative(relative)
        staging_name = f".qingtian-{uuid4().hex}.delete-stage"
        with self._anchored_vault_parent(relative, create_parent=False) as (
            parent_fd,
            chain,
        ):
            root_fd = chain[0][1] if chain else parent_fd
            staging_created = False
            preserve_staging = False
            try:
                try:
                    before = os.stat(
                        relative.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise KnowledgeError("generated vault delete target disappeared") from exc
                if (
                    not stat.S_ISREG(before.st_mode)
                    or (before.st_dev, before.st_ino) != expected_identity
                ):
                    raise KnowledgeError(
                        "generated vault delete target is a symlink or changed inode"
                    )

                # Move first, then verify the inode that was actually removed
                # from the public target name. This closes stat(target)->unlink
                # against a concurrent ordinary-file replacement.
                self._verify_vault_fd_chain(root_fd, chain)
                os.replace(
                    relative.name,
                    staging_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                staging_created = True
                staged = os.stat(
                    staging_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(staged.st_mode)
                    or (staged.st_dev, staged.st_ino) != expected_identity
                ):
                    preserve_staging = True
                    if not self._restore_staged_entry(
                        parent_fd, staging_name, relative.name
                    ):
                        # Preserve both concurrent entries. The hidden staging
                        # inode is intentionally retained for human recovery.
                        raise KnowledgeError(
                            "generated vault delete target changed; recovery staging retained"
                        )
                    staging_created = False
                    preserve_staging = False
                    raise KnowledgeError("generated vault delete target changed during delete")

                try:
                    self._verify_vault_fd_chain(root_fd, chain)
                except KnowledgeError as exc:
                    if not self._restore_staged_entry(
                        parent_fd, staging_name, relative.name
                    ):
                        preserve_staging = True
                        raise KnowledgeError(
                            "vault parent changed during delete; recovery staging retained"
                        ) from exc
                    staging_created = False
                    os.fsync(parent_fd)
                    raise KnowledgeError(
                        "vault parent directory changed during delete; deletion rolled back"
                    ) from exc

                # Only the inode proven above is unlinked. Any new file created
                # meanwhile at the public target name is left untouched.
                os.unlink(staging_name, dir_fd=parent_fd)
                staging_created = False
                os.fsync(parent_fd)
            finally:
                if staging_created and not preserve_staging:
                    try:
                        if not self._restore_staged_entry(
                            parent_fd, staging_name, relative.name
                        ):
                            os.unlink(staging_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except FileExistsError:
                        # Never overwrite a concurrent replacement just to hide
                        # a recovery entry.
                        pass

    def _atomic_write(self, path: Path, content: str) -> None:
        """Atomically write below the Vault using only anchored *at operations."""
        try:
            relative = path.relative_to(self.vault)
        except ValueError:
            # macOS commonly exposes the same temporary directory as both
            # /var/... and /private/var/.... Locate the lexical ancestor whose
            # *directory inode* is the trusted Vault root; do not resolve the
            # target or any Vault-relative component. The anchored traversal
            # below will therefore still reject nested symlinks.
            alias_relative: Path | None = None
            candidate = path.parent
            while True:
                try:
                    candidate_state = os.stat(candidate, follow_symlinks=False)
                except OSError:
                    candidate_state = None
                if candidate_state is not None and (
                    candidate_state.st_dev,
                    candidate_state.st_ino,
                ) == self._vault_identity:
                    try:
                        alias_relative = path.relative_to(candidate)
                    except ValueError:
                        alias_relative = None
                    break
                if candidate == candidate.parent:
                    break
                candidate = candidate.parent
            if alias_relative is None:
                raise KnowledgeError("generated vault path escaped the vault")
            relative = alias_relative
        self._validate_vault_relative(relative)
        data = content.encode("utf-8")
        target_name = relative.name
        temporary_name = f".qingtian-{uuid4().hex}.tmp"
        staging_name = f".qingtian-{uuid4().hex}.write-stage"
        rollback_name = f".qingtian-{uuid4().hex}.write-rollback"

        with self._anchored_vault_parent(relative, create_parent=True) as (
            parent_fd,
            chain,
        ):
            root_fd = chain[0][1] if chain else parent_fd
            temporary_created = False
            staging_created = False
            preserve_staging = False
            descriptor: int | None = None
            try:
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | self._required_open_flag("O_NOFOLLOW")
                    | getattr(os, "O_CLOEXEC", 0)
                )
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=parent_fd,
                )
                temporary_created = True
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise KnowledgeError("generated vault temporary write stalled")
                    view = view[written:]
                os.fsync(descriptor)
                temporary_state = os.fstat(descriptor)
                os.close(descriptor)
                descriptor = None

                self._verify_vault_fd_chain(root_fd, chain)
                try:
                    before = os.stat(
                        target_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    before = None
                if before is not None:
                    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                        raise KnowledgeError(
                            "generated vault target is a symlink or non-regular file"
                        )
                    # Move the actual target entry to staging, then inspect what
                    # renameat captured. A replacement in the stat->rename gap
                    # is restored and never overwritten.
                    os.replace(
                        target_name,
                        staging_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    staging_created = True
                    staged = os.stat(
                        staging_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(staged.st_mode)
                        or (before.st_dev, before.st_ino)
                        != (staged.st_dev, staged.st_ino)
                    ):
                        preserve_staging = True
                        if not self._restore_staged_entry(
                            parent_fd, staging_name, target_name
                        ):
                            raise KnowledgeError(
                                "generated vault target changed; recovery staging retained"
                            )
                        staging_created = False
                        preserve_staging = False
                        raise KnowledgeError("generated vault target changed during write")
                    try:
                        self._verify_vault_fd_chain(root_fd, chain)
                    except KnowledgeError as exc:
                        if not self._restore_staged_entry(
                            parent_fd, staging_name, target_name
                        ):
                            preserve_staging = True
                            raise KnowledgeError(
                                "vault parent changed during write; recovery staging retained"
                            ) from exc
                        staging_created = False
                        raise KnowledgeError(
                            "vault parent directory changed during commit; write rolled back"
                        ) from exc

                # Hard-link publication has no-replace semantics. A concurrent
                # creator wins without being overwritten, unlike os.replace.
                try:
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    if staging_created:
                        os.unlink(staging_name, dir_fd=parent_fd)
                        staging_created = False
                    raise KnowledgeError(
                        "generated vault target changed before no-replace publish"
                    ) from exc

                published = os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(published.st_mode)
                    or (published.st_dev, published.st_ino)
                    != (temporary_state.st_dev, temporary_state.st_ino)
                ):
                    if staging_created:
                        os.unlink(staging_name, dir_fd=parent_fd)
                        staging_created = False
                    raise KnowledgeError("generated vault target changed after publish")

                try:
                    self._verify_vault_fd_chain(root_fd, chain)
                except KnowledgeError as exc:
                    # Move whatever currently occupies the public name aside,
                    # then inspect it before removal. This rollback is itself
                    # inode-conditional and cannot delete a concurrent file.
                    try:
                        os.replace(
                            target_name,
                            rollback_name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                    except FileNotFoundError:
                        rolled_back = None
                    else:
                        rolled_back = os.stat(
                            rollback_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    if rolled_back is not None and (
                        rolled_back.st_dev,
                        rolled_back.st_ino,
                    ) == (temporary_state.st_dev, temporary_state.st_ino):
                        os.unlink(rollback_name, dir_fd=parent_fd)
                        if staging_created:
                            if not self._restore_staged_entry(
                                parent_fd, staging_name, target_name
                            ):
                                preserve_staging = True
                                raise KnowledgeError(
                                    "vault parent changed; recovery staging retained"
                                ) from exc
                            staging_created = False
                    elif rolled_back is not None:
                        if not self._restore_staged_entry(
                            parent_fd, rollback_name, target_name
                        ):
                            raise KnowledgeError(
                                "concurrent write recovery entry retained"
                            ) from exc
                        if staging_created:
                            os.unlink(staging_name, dir_fd=parent_fd)
                            staging_created = False
                    os.fsync(parent_fd)
                    raise KnowledgeError(
                        "vault parent directory changed during commit; write rolled back"
                    ) from exc

                os.unlink(temporary_name, dir_fd=parent_fd)
                temporary_created = False
                if staging_created:
                    os.unlink(staging_name, dir_fd=parent_fd)
                    staging_created = False
                os.fsync(parent_fd)
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                if temporary_created:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                if staging_created and not preserve_staging:
                    try:
                        if not self._restore_staged_entry(
                            parent_fd, staging_name, target_name
                        ):
                            os.unlink(staging_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass

    def ingest(self) -> dict[str, Any]:
        with self._exclusive_ingest_lock():
            return self._ingest_locked()

    def _ingest_locked(self) -> dict[str, Any]:
        self._assert_config_unchanged()
        # Preview builds persisted absolute operational paths in portable state.
        # Scrub them before discovery or any early-returning baseline check so a
        # partial/conflicted ingestion cannot keep a local account name behind.
        self.index.scrub_legacy_source_paths()
        managed_count = self.index.db.execute(
            "SELECT COUNT(*) FROM generated_outputs"
        ).fetchone()[0]
        portable_manifest = self._safe_vault_target(
            Path("99-System") / "Managed-Outputs-Manifest.json", create_parent=False
        )
        if portable_manifest.is_file():
            self._assert_managed_baseline_matches_manifest()
        elif managed_count:
            raise KnowledgeError(
                "managed outputs exist without a portable manifest; recovery is required"
            )
        records = self.discover_sources()
        self._assert_config_unchanged()
        config_hash = self._config_snapshot_hash
        run_id = (
            "ing-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            + "-"
            + config_hash[:8]
            + "-"
            + uuid4().hex[:8]
        )
        self.index.start_run(run_id, config_hash)
        try:
            return self._execute_ingestion(records, config_hash, run_id)
        except Exception as exc:
            failure = {
                "schema_version": 1,
                "run_id": run_id,
                "result": "failed",
                "completed_at": utc_now(),
                "config_sha256": config_hash,
                "source_count": len(records),
                "counts": {"fatal_errors": 1},
                "errors": [{"error": type(exc).__name__}],
            }
            failure["receipt_sha256"] = digest_bytes(
                strict_json(failure).encode("utf-8")
            )
            try:
                self._write_receipt(failure)
            finally:
                self.index.finish_run(run_id, failure)
            if isinstance(exc, KnowledgeError):
                raise
            raise KnowledgeError(f"ingestion failed: {type(exc).__name__}") from exc

    def _execute_ingestion(
        self,
        records: list[SourceRecord],
        config_hash: str,
        run_id: str,
    ) -> dict[str, Any]:
        counts = Counter()
        errors: list[dict[str, str]] = []
        for source_set in self.config.get("source_sets", []):
            source_root = (self.workspace / source_set["root"]).resolve()
            if not source_root.is_dir():
                counts["source_roots_unavailable"] += 1
                errors.append(
                    {
                        "source": str(source_set["id"]),
                        "error": "source-root-unavailable",
                    }
                )
        for record in records:
            self._assert_config_unchanged()
            previous = self.index.previous_source(record.knowledge_id)
            active_key = f"source-active-{record.knowledge_id}"
            quarantine_key = f"source-quarantine-{record.knowledge_id}"
            active_generated = self.index.previous_generated(active_key)
            quarantine_generated = self.index.previous_generated(quarantine_key)
            restricted_source = record.quarantined
            try:
                # Once a source is known to be restricted, disable retrieval and
                # retire any prior mirror before operations that may fail due to
                # concurrent source changes.  Isolation failure is partial/fail-closed.
                if record.quarantined:
                    self.index.remove_document(record.knowledge_id)
                    self._retire_active_mirror_for_restriction(
                        record, previous, active_generated, run_id
                    )
                record = self._refresh_source_git_metadata(record)
                self._assert_source_unchanged(record)
                if record.quarantined:
                    relative = Path("95-Reviews") / "Quarantine" / f"{record.knowledge_id}.md"
                    content = self._render_quarantine(record, run_id)
                    vault_path, output_hash = self._write_managed(
                        relative,
                        content,
                        previous_hash=(
                            quarantine_generated["output_hash"] if quarantine_generated else None
                        ),
                        run_id=run_id,
                        conflict_id=record.knowledge_id,
                    )
                    self.index.upsert_generated(
                        quarantine_key, vault_path, output_hash, run_id, "quarantined"
                    )
                    self._assert_source_unchanged(record)
                    self.index.upsert_source(
                        record,
                        run_id=run_id,
                        vault_path=vault_path,
                        output_hash=output_hash,
                        status="quarantined",
                    )
                    if previous is not None and previous["status"] == "missing":
                        self._resolve_tombstone(record, run_id)
                    counts["quarantined"] += 1
                    continue
                target_relative = self._vault_relative(record)
                target = self.vault / target_relative
                evidence_level, claim_scope, freshness = self._effective_evidence(record)
                previous_output = active_generated["output_hash"] if active_generated else None
                if previous is not None and previous["status"] == "active":
                    self._resolve_quarantine_record(record, run_id, quarantine_generated)
                    # Keep the current quarantine lineage baseline.  A source
                    # can look safe in its raw container yet become restricted
                    # only after extraction (notably PDFs).  If an optional
                    # extractor disappears and later returns, the same source
                    # may legitimately move quarantined -> resolved ->
                    # quarantined.  Reloading prevents the final transition
                    # from being misreported as a human edit merely because a
                    # resolved lineage note already exists at the target path.
                    quarantine_generated = self.index.previous_generated(quarantine_key)
                if (
                    previous
                    and previous["status"] == "active"
                    and previous["source_hash"] == record.source_hash
                    and previous["classification"] == record.classification
                    and previous["evidence_level"] == record.evidence_level
                    and previous["claim_scope"] == record.claim_scope
                    and previous["git_state"] == record.git_state
                    and previous["repo_head"] == record.repo_head
                    and previous["repo_branch"] == record.repo_branch
                    and previous["repo_blob"] == record.repo_blob
                    and previous["project"] == record.project
                    and previous["category"] == record.category
                    and previous["title"] == record.title
                    and previous["kind"] == record.kind
                    and previous["extractor_version"] == record.transform_fingerprint
                    and previous_output
                    and target.is_file()
                    and digest_file(target) == previous_output
                ):
                    self.index.upsert_source(
                        record,
                        run_id=run_id,
                        vault_path=previous["vault_path"],
                        output_hash=previous_output,
                        status="active",
                    )
                    self.index.set_document_state(
                        record.knowledge_id,
                        source_status="active",
                        conflict_status="none",
                        freshness_status=freshness,
                    )
                    counts["unchanged"] += 1
                    continue
                body, extraction = self._extract_body(record)
                self._assert_source_unchanged(record)
                record = self._refresh_source_git_metadata(record)
                evidence_level, claim_scope, freshness = self._effective_evidence(record)
                # PDF text is scanned after extraction; matches never enter the Vault.
                extracted_secrets = [
                    name for name, regex in SECRET_PATTERNS.items() if regex.search(body)
                ]
                extracted_pii = any(regex.search(body) for regex in PII_PATTERNS.values())
                if extracted_secrets or extracted_pii:
                    restricted_source = True
                    quarantined_record = SourceRecord(
                        **{
                            **record.__dict__,
                            "secret_kinds": tuple(sorted(set(extracted_secrets))),
                            "pii": "possible" if extracted_pii else record.pii,
                        }
                    )
                    self.index.remove_document(record.knowledge_id)
                    self._retire_active_mirror_for_restriction(
                        quarantined_record, previous, active_generated, run_id
                    )
                    relative = Path("95-Reviews") / "Quarantine" / f"{record.knowledge_id}.md"
                    content = self._render_quarantine(quarantined_record, run_id)
                    vault_path, output_hash = self._write_managed(
                        relative,
                        content,
                        previous_hash=(
                            quarantine_generated["output_hash"] if quarantine_generated else None
                        ),
                        run_id=run_id,
                        conflict_id=record.knowledge_id,
                    )
                    self.index.upsert_generated(
                        quarantine_key, vault_path, output_hash, run_id, "quarantined"
                    )
                    self._assert_source_unchanged(record)
                    self.index.upsert_source(
                        quarantined_record,
                        run_id=run_id,
                        vault_path=vault_path,
                        output_hash=output_hash,
                        status="quarantined",
                    )
                    if previous is not None and previous["status"] == "missing":
                        self._resolve_tombstone(quarantined_record, run_id)
                    counts["quarantined"] += 1
                    continue
                self._resolve_quarantine_record(record, run_id, quarantine_generated)
                content = self._render_source(record, body, extraction, run_id)
                vault_path, output_hash = self._write_managed(
                    target_relative,
                    content,
                    previous_hash=previous_output,
                    run_id=run_id,
                    conflict_id=record.knowledge_id,
                )
                self.index.upsert_generated(active_key, vault_path, output_hash, run_id)
                self.index.upsert_source(
                    record,
                    run_id=run_id,
                    vault_path=vault_path,
                    output_hash=output_hash,
                    status="active",
                )
                self.index.upsert_document(
                    knowledge_id=record.knowledge_id,
                    title=record.title,
                    body=body,
                    project=record.project,
                    category=record.category,
                    evidence_level=evidence_level,
                    claim_scope=claim_scope,
                    review_status="pending",
                    conflict_status="none",
                    freshness_status=freshness,
                    classification=record.classification,
                    source_revision=self._source_revision(record),
                    source_sha256=record.source_hash,
                    git_state=record.git_state,
                    source_status="active",
                    historical=(
                        evidence_level == "E1"
                        or "historical" in claim_scope
                        or record.category == "historical-evidence"
                    ),
                    vault_path=vault_path,
                    source_locator=f"workspace:{record.source_set}/{record.relative_path}",
                )
                if previous is not None and previous["status"] == "missing":
                    self._resolve_tombstone(record, run_id)
                counts["created" if previous is None else "updated"] += 1
            except SourceChangedDuringIngest:
                counts["unstable"] += 1
                errors.append(
                    {
                        "source": record.knowledge_id if restricted_source else record.relative_path,
                        "error": "source-changed-during-ingest",
                    }
                )
                self.index.upsert_source(
                    record,
                    run_id=run_id,
                    vault_path=previous["vault_path"] if previous else None,
                    output_hash=previous["output_hash"] if previous else None,
                    status="quarantined-unstable" if restricted_source else "unstable",
                )
                self.index.set_document_state(
                    record.knowledge_id,
                    source_status="unstable",
                    freshness_status="stale",
                )
            except HumanEditConflict as exc:
                counts["conflicts"] += 1
                errors.append(
                    {
                        "source": record.knowledge_id if restricted_source else record.relative_path,
                        "error": type(exc).__name__,
                    }
                )
                self.index.upsert_source(
                    record,
                    run_id=run_id,
                    vault_path=previous["vault_path"] if previous else None,
                    output_hash=previous["output_hash"] if previous else None,
                    status="quarantined-conflict" if restricted_source else "conflict",
                )
                self.index.set_document_state(
                    record.knowledge_id,
                    source_status="conflict",
                    conflict_status="confirmed",
                    freshness_status="stale",
                )
            except Exception as exc:
                counts["errors"] += 1
                errors.append(
                    {
                        "source": record.knowledge_id if restricted_source else record.relative_path,
                        "error": type(exc).__name__,
                    }
                )
                try:
                    self.index.upsert_source(
                        record,
                        run_id=run_id,
                        vault_path=previous["vault_path"] if previous else None,
                        output_hash=previous["output_hash"] if previous else None,
                        status="quarantined-error" if restricted_source else "error",
                    )
                    self.index.set_document_state(
                        record.knowledge_id,
                        source_status="error",
                        freshness_status="stale",
                    )
                except Exception:
                    pass
        self._assert_config_unchanged()
        counts["restricted_missing_reconciled"] += self._reconcile_restricted_missing_sources(
            run_id
        )
        missing_rows = self.index.mark_missing(run_id)
        counts["missing"] = len(missing_rows)
        for row in missing_rows:
            try:
                self._write_tombstone(row, run_id)
            except HumanEditConflict:
                counts["conflicts"] += 1
        test_summary = self.ingest_test_inventory(run_id)
        git_summary = self.ingest_git_history(run_id)
        counts["generated_conflicts"] += sum(
            item.get("status") == "conflict" for item in test_summary.values()
        )
        counts["generated_conflicts"] += sum(
            item.get("status") == "conflict" for item in git_summary.values()
        )
        generated_unavailable = sum(
            item.get("status") == "unavailable"
            for item in (*test_summary.values(), *git_summary.values())
        )
        generated_unstable = sum(
            item.get("status") in {"partial", "unstable"}
            for item in (*test_summary.values(), *git_summary.values())
        )
        if generated_unavailable:
            counts["generated_unavailable"] += generated_unavailable
        if generated_unstable:
            counts["generated_unstable"] += generated_unstable
        counts["generated_conflicts"] += self.generate_indexes(run_id)
        self._assert_config_unchanged()
        managed_manifest = self.write_managed_manifest(run_id)
        manifest = [
            {
                "id": record.knowledge_id,
                "sha256": record.source_hash,
                "git_state": record.git_state,
                "repo_head": record.repo_head,
            }
            for record in records
        ]
        receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "result": (
                "passed"
                if not (
                    counts["errors"]
                    or counts["conflicts"]
                    or counts["generated_conflicts"]
                    or counts["generated_unavailable"]
                    or counts["generated_unstable"]
                    or counts["source_roots_unavailable"]
                    or counts["unstable"]
                )
                else "partial"
            ),
            "completed_at": utc_now(),
            "config_sha256": config_hash,
            "source_count": len(records),
            "source_manifest_sha256": digest_bytes(strict_json(manifest).encode("utf-8")),
            "managed_outputs_manifest_sha256": managed_manifest["manifest_sha256"],
            "counts": dict(sorted(counts.items())),
            "test_inventory": test_summary,
            "git_history": git_summary,
            "errors": errors[:100],
        }
        receipt["receipt_sha256"] = digest_bytes(strict_json(receipt).encode("utf-8"))
        self._write_receipt(receipt)
        self.index.finish_run(run_id, receipt)
        return receipt

    def _reconcile_restricted_missing_sources(self, run_id: str) -> int:
        """Retire old mirrors even when a newly restricted source is no longer readable."""
        policies = {
            item["id"]: item
            for item in self.config.get("source_sets", [])
            if item.get("classification") in {"P2-confidential", "P3-restricted"}
        }
        if not policies:
            return 0
        placeholders = ",".join("?" for _ in policies)
        rows = self.index.db.execute(
            "SELECT * FROM sources WHERE source_set IN ("
            + placeholders
            + ") AND last_seen_run != ?",
            (*policies.keys(), run_id),
        ).fetchall()
        reconciled = 0
        for row in rows:
            policy = policies[str(row["source_set"])]
            try:
                prior_secret_kinds = tuple(json.loads(row["secret_kinds_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                prior_secret_kinds = ()
            record = SourceRecord(
                knowledge_id=str(row["knowledge_id"]),
                source_set=str(row["source_set"]),
                project=str(policy["project"]),
                category=str(policy["category"]),
                classification=str(policy["classification"]),
                evidence_level=str(policy["evidence_level"]),
                claim_scope=str(policy["claim_scope"]),
                source_path=Path(str(row["source_path"])),
                relative_path=str(row["relative_path"]),
                source_hash=str(row["source_hash"]),
                size_bytes=int(row["size_bytes"]),
                modified_at=str(row["modified_at"]),
                title=str(row["title"]),
                kind=str(row["kind"]),
                repo_head=row["repo_head"],
                repo_branch=None,
                repo_blob=row["repo_blob"],
                git_state=str(row["git_state"]),
                pii=str(row["pii"]),
                secret_kinds=prior_secret_kinds or ("policy-restricted",),
                transform_fingerprint=str(row["extractor_version"]),
            )
            active = self.index.previous_generated("source-active-" + record.knowledge_id)
            self.index.remove_document(record.knowledge_id)
            self._retire_active_mirror_for_restriction(record, row, active, run_id)
            generated_id = "source-quarantine-" + record.knowledge_id
            previous_quarantine = self.index.previous_generated(generated_id)
            relative = Path("95-Reviews") / "Quarantine" / f"{record.knowledge_id}.md"
            content = self._render_quarantine(record, run_id)
            vault_path, output_hash = self._write_managed(
                relative,
                content,
                previous_hash=(
                    previous_quarantine["output_hash"] if previous_quarantine else None
                ),
                run_id=run_id,
                conflict_id=record.knowledge_id,
            )
            self.index.upsert_generated(
                generated_id, vault_path, output_hash, run_id, "quarantined"
            )
            # Preserve the old observation run so mark_missing still records that
            # the source was not observed now, while immediately withholding all
            # newly restricted metadata from portable state.
            self.index.upsert_source(
                record,
                run_id=str(row["last_seen_run"]),
                vault_path=vault_path,
                output_hash=output_hash,
                status="quarantined",
            )
            reconciled += 1
        return reconciled

    def write_managed_manifest(self, run_id: str) -> dict[str, Any]:
        rows = self.index.db.execute(
            """SELECT knowledge_id, vault_path, output_hash, status
            FROM generated_outputs ORDER BY knowledge_id"""
        ).fetchall()
        outputs = [dict(row) for row in rows]
        manifest = {
            "schema_version": 1,
            "generated_at": utc_now(),
            "run_id": run_id,
            "outputs": outputs,
            "scope": "machine-managed-baselines-only",
        }
        manifest["manifest_sha256"] = digest_bytes(
            strict_json(outputs).encode("utf-8")
        )
        path = self._safe_vault_target(
            Path("99-System") / "Managed-Outputs-Manifest.json", create_parent=True
        )
        self._atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return manifest

    def _read_managed_manifest(self) -> dict[str, Any]:
        path = self._safe_vault_target(
            Path("99-System") / "Managed-Outputs-Manifest.json", create_parent=False
        )
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeError("managed outputs manifest is missing or invalid") from exc
        outputs = manifest.get("outputs")
        if not isinstance(outputs, list) or manifest.get("schema_version") != 1:
            raise KnowledgeError("unsupported managed outputs manifest")
        expected = digest_bytes(strict_json(outputs).encode("utf-8"))
        if expected != manifest.get("manifest_sha256"):
            raise KnowledgeError("managed outputs manifest checksum mismatch")
        return manifest

    def _assert_managed_baseline_matches_manifest(self) -> None:
        manifest = self._read_managed_manifest()
        rows = self.index.db.execute(
            "SELECT knowledge_id, vault_path, output_hash, status "
            "FROM generated_outputs ORDER BY knowledge_id"
        ).fetchall()
        state_outputs = [dict(row) for row in rows]
        manifest_outputs = manifest["outputs"]
        if strict_json(state_outputs) != strict_json(manifest_outputs):
            raise KnowledgeError(
                "managed baseline does not match the portable manifest; restore from an empty "
                "state before ingest"
            )

    def restore_managed_baseline(self) -> dict[str, Any]:
        """Explicitly restore output hashes after copying a Vault without local state."""
        with self._exclusive_ingest_lock():
            return self._restore_managed_baseline_locked()

    def _restore_managed_baseline_locked(self) -> dict[str, Any]:
        existing = self.index.db.execute("SELECT COUNT(*) FROM generated_outputs").fetchone()[0]
        if existing:
            raise KnowledgeError("restore-baseline requires an empty generated-output state")
        manifest = self._read_managed_manifest()
        outputs = manifest["outputs"]
        restored = 0
        rejected: list[dict[str, str]] = []
        accepted: list[tuple[str, str, str, str]] = []
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        restore_run = "restore-" + uuid4().hex[:12]
        for item in outputs:
            if not isinstance(item, dict):
                rejected.append({"reason": "invalid-entry"})
                continue
            knowledge_id = str(item.get("knowledge_id", ""))
            relative = Path(str(item.get("vault_path", "")))
            status = str(item.get("status", ""))
            if not knowledge_id or relative.is_absolute() or ".." in relative.parts:
                rejected.append({"knowledge_id": knowledge_id, "reason": "unsafe-path"})
                continue
            if knowledge_id in seen_ids:
                rejected.append({"knowledge_id": knowledge_id, "reason": "duplicate-id"})
                continue
            if relative.as_posix() in seen_paths:
                rejected.append({"knowledge_id": knowledge_id, "reason": "duplicate-path"})
                continue
            seen_ids.add(knowledge_id)
            seen_paths.add(relative.as_posix())
            if status not in ALLOWED_GENERATED_STATUSES:
                rejected.append({"knowledge_id": knowledge_id, "reason": "invalid-status"})
                continue
            try:
                target = self._safe_vault_target(relative, create_parent=False)
            except KnowledgeError:
                rejected.append({"knowledge_id": knowledge_id, "reason": "unsafe-or-symlink-path"})
                continue
            if (
                target.suffix.lower() != ".md"
                or target.is_symlink()
                or not target.is_file()
                or digest_file(target) != item.get("output_hash")
            ):
                rejected.append({"knowledge_id": knowledge_id, "reason": "hash-mismatch-or-missing"})
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
            if MANAGED_MARKER not in text:
                rejected.append({"knowledge_id": knowledge_id, "reason": "managed-marker-missing"})
                continue
            fields = _frontmatter_fields(text)
            if (
                fields.get("managed_by") != "qingtian"
                or not _managed_identity_matches(knowledge_id, status, fields)
            ):
                rejected.append({"knowledge_id": knowledge_id, "reason": "identity-mismatch"})
                continue
            accepted.append(
                (
                    knowledge_id,
                    relative.as_posix(),
                    str(item["output_hash"]),
                    status,
                )
            )
        if not rejected:
            try:
                for knowledge_id, vault_path, output_hash, status in accepted:
                    self.index.upsert_generated(
                        knowledge_id,
                        vault_path,
                        output_hash,
                        restore_run,
                        status,
                        commit=False,
                    )
                self.index.db.commit()
                restored_state = [
                    dict(row)
                    for row in self.index.db.execute(
                        "SELECT knowledge_id, vault_path, output_hash, status "
                        "FROM generated_outputs ORDER BY knowledge_id"
                    ).fetchall()
                ]
                expected_state = [
                    {
                        "knowledge_id": knowledge_id,
                        "vault_path": vault_path,
                        "output_hash": output_hash,
                        "status": status,
                    }
                    for knowledge_id, vault_path, output_hash, status in sorted(accepted)
                ]
                if strict_json(restored_state) != strict_json(expected_state):
                    raise KnowledgeError("restored baseline does not match manifest")
                restored = len(accepted)
            except Exception:
                self.index.db.rollback()
                self.index.db.execute("DELETE FROM generated_outputs")
                self.index.db.commit()
                raise
        return {
            "status": "restored" if not rejected else "partial",
            "restored": restored,
            "rejected": rejected,
            "next_step": "run ingest to rebuild sources, documents and FTS",
        }

    def _write_tombstone(self, source: sqlite3.Row, run_id: str) -> None:
        knowledge_id = str(source["knowledge_id"])
        generated_id = f"tombstone-{knowledge_id}"
        relative = Path("95-Reviews") / "Tombstones" / f"{_safe_slug(knowledge_id)}.md"
        content = (
            "---\n"
            'schema_version: "1.0"\n'
            f"id: {_safe_yaml(generated_id)}\n"
            'type: "tombstone"\n'
            f"title: {_safe_yaml('Missing source: ' + str(source['title']))}\n"
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "pending"\n'
            'conflict_status: "suspected"\n'
            'freshness_status: "stale"\n'
            'privacy_classification: "P1-internal"\n'
            f"source_id: {_safe_yaml(knowledge_id)}\n"
            f"source_sha256: {_safe_yaml(source['source_hash'])}\n"
            f"last_seen_run: {_safe_yaml(source['last_seen_run'])}\n"
            f"missing_in_run: {_safe_yaml(run_id)}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            "# Missing source\n\n"
            "The allowlisted source was not observed in this ingestion. The prior knowledge "
            "mirror was retained, marked stale in the index, and excluded from default search. "
            "This does not prove the source was intentionally deleted.\n"
        )
        previous = self.index.previous_generated(generated_id)
        vault_path, output_hash = self._write_managed(
            relative,
            content,
            previous_hash=previous["output_hash"] if previous else None,
            run_id=run_id,
            conflict_id=generated_id,
        )
        self.index.upsert_generated(generated_id, vault_path, output_hash, run_id, "missing")

    def _resolve_tombstone(self, record: SourceRecord, run_id: str) -> None:
        generated_id = f"tombstone-{record.knowledge_id}"
        previous = self.index.previous_generated(generated_id)
        if not previous:
            return
        relative = Path(previous["vault_path"])
        content = (
            "---\n"
            'schema_version: "1.0"\n'
            f"id: {_safe_yaml(generated_id)}\n"
            'type: "tombstone"\n'
            f"title: {_safe_yaml('Resolved missing source ' + record.knowledge_id)}\n"
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "pending"\n'
            'conflict_status: "resolved"\n'
            'freshness_status: "current"\n'
            'privacy_classification: "P1-internal"\n'
            f"source_id: {_safe_yaml(record.knowledge_id)}\n"
            f"source_sha256: {_safe_yaml(record.source_hash)}\n"
            f"reappeared_in_run: {_safe_yaml(run_id)}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            "# Source reappeared\n\n"
            "The allowlisted source was observed again. This resolves the missing-source "
            "condition only; it does not approve the source's claims.\n"
        )
        vault_path, output_hash = self._write_managed(
            relative,
            content,
            previous_hash=previous["output_hash"],
            run_id=run_id,
            conflict_id=generated_id,
        )
        self.index.upsert_generated(generated_id, vault_path, output_hash, run_id, "resolved")

    def _write_receipt(self, receipt: dict[str, Any]) -> None:
        relative = Path("99-System") / "Receipts" / f"{receipt['run_id']}.md"
        path = self._safe_vault_target(relative, create_parent=True)
        body = (
            "---\n"
            'schema_version: "1.0"\n'
            f"id: {_safe_yaml('receipt-' + receipt['run_id'])}\n"
            'type: "ingestion-receipt"\n'
            f"title: {_safe_yaml('Ingestion ' + receipt['run_id'])}\n"
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "not-required"\n'
            'privacy_classification: "P1-internal"\n'
            f"receipt_sha256: {_safe_yaml(receipt['receipt_sha256'])}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            f"# Ingestion {receipt['run_id']}\n\n"
            "```json\n"
            + json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n```\n"
        )
        self._atomic_write(path, body)

    @staticmethod
    def _test_capability(path: str, content: str) -> str:
        sample = (path + "\n" + content[:20000]).lower()
        rules = [
            ("visual", ("visual", "screenshot", "snapshot", "pixelmatch")),
            ("e2e", ("e2e", "playwright", "cypress", "browser")),
            ("mobile", ("android", "ios", "react-native", "detox", "maestro", "mobile")),
            ("accessibility", ("a11y", "accessibility", "axe-core")),
            (
                "performance",
                (
                    "performance",
                    "benchmark",
                    "load test",
                    "k6",
                    "latency",
                    "coldstart",
                    "cold-start",
                    "startup time",
                ),
            ),
            ("security", ("security", "permission", "authorization", "csrf", "xss")),
            ("contract", ("contract", "schema", "openapi", "compatibility")),
            ("migration", ("migration", "alembic", "drizzle", "database upgrade")),
            ("integration", ("integration", "postgres", "redis", "s3", "database")),
            ("smoke", ("smoke", "healthcheck", "doctor")),
        ]
        for capability, needles in rules:
            if any(needle in sample for needle in needles):
                return capability
        return "unit"

    @staticmethod
    def _case_count(path: Path, content: str) -> int:
        suffix = path.suffix.lower()
        if suffix == ".py":
            return len(re.findall(r"(?m)^\s*(?:async\s+)?def\s+test_[A-Za-z0-9_]+", content))
        if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue"}:
            return len(re.findall(r"\b(?:test|it)\s*\(\s*[\"'`]", content))
        if suffix in {".java", ".kt"}:
            return len(re.findall(r"(?m)^\s*@Test\b", content))
        return 0

    def _write_unavailable_snapshot(
        self,
        *,
        knowledge_id: str,
        relative: Path,
        title: str,
        project: str,
        category: str,
        reason: str,
        run_id: str,
        status: str,
    ) -> bool:
        """Replace a formerly current generated snapshot with a stale status record."""
        content = (
            "---\n"
            'schema_version: "1.0"\n'
            f"id: {_safe_yaml(knowledge_id)}\n"
            'type: "snapshot-status"\n'
            f"title: {_safe_yaml(title)}\n"
            f"project: {_safe_yaml(project)}\n"
            'knowledge_status: "candidate"\n'
            'evidence_level: "E1"\n'
            'claim_scope: "unavailable-snapshot"\n'
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "pending"\n'
            'conflict_status: "none"\n'
            'freshness_status: "stale"\n'
            'privacy_classification: "P1-internal"\n'
            f"snapshot_status: {_safe_yaml(status)}\n"
            f"reason: {_safe_yaml(reason)}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            f"# {title}\n\n"
            "The configured source could not produce a stable current snapshot in this run. "
            "Prior rows are excluded from current aggregate indexes. This status is not a "
            "test result, deployment receipt, or proof that the underlying data was deleted.\n"
        )
        previous = self.index.previous_generated(knowledge_id)
        try:
            vault_path, output_hash = self._write_managed(
                relative,
                content,
                previous_hash=previous["output_hash"] if previous else None,
                run_id=run_id,
                conflict_id=knowledge_id,
            )
            self.index.upsert_generated(
                knowledge_id, vault_path, output_hash, run_id, status
            )
            self.index.upsert_document(
                knowledge_id=knowledge_id,
                title=title,
                body=reason,
                project=project,
                category=category,
                evidence_level="E1",
                claim_scope="unavailable-snapshot",
                review_status="pending",
                conflict_status="none",
                freshness_status="stale",
                classification="P1-internal",
                source_revision=f"unavailable:{run_id}",
                source_sha256=digest_bytes(reason.encode("utf-8")),
                git_state=status,
                source_status=status,
                historical=True,
                vault_path=vault_path,
                source_locator="configured-local-source",
            )
            return True
        except HumanEditConflict:
            self.index.set_document_state(
                knowledge_id,
                source_status="conflict",
                conflict_status="confirmed",
                freshness_status="stale",
            )
            return False

    def ingest_test_inventory(self, run_id: str) -> dict[str, Any]:
        summaries: dict[str, dict[str, Any]] = {}
        for spec in self.config.get("test_roots", []):
            project = spec["project"]
            root = (self.workspace / spec["root"]).resolve()
            try:
                root.relative_to(self.workspace)
            except ValueError as exc:
                raise ConfigurationError(f"test root escaped workspace: {spec['root']}") from exc
            if not root.is_dir():
                knowledge_id = f"test-inventory-{project}"
                self.index.db.execute("DELETE FROM test_files WHERE project = ?", (project,))
                self.index.db.commit()
                written = self._write_unavailable_snapshot(
                    knowledge_id=knowledge_id,
                    relative=Path("04-Testing") / "Inventories" / f"{project}.md",
                    title=f"{project} test capability inventory unavailable",
                    project=project,
                    category="testing",
                    reason="test-root-unavailable",
                    run_id=run_id,
                    status="unavailable",
                )
                summaries[project] = {
                    "status": "unavailable" if written else "conflict",
                    "files": 0,
                }
                continue
            files: dict[str, Path] = {}
            for pattern in spec.get("includes", []):
                for path in self._discover_pattern(root, pattern):
                    if path.suffix.lower() not in TEST_TEXT_SUFFIXES:
                        continue
                    files[path.relative_to(root).as_posix()] = path
            capabilities: Counter[str] = Counter()
            cases: Counter[str] = Counter()
            rows: list[tuple[str, str, int]] = []
            manifest_rows: list[tuple[str, str, str, int]] = []
            secret_quarantined = 0
            unstable = 0
            dirty_paths: set[str] = set()
            repo = self.repo_context.get(project)
            tree_blobs: dict[str, str] = {}
            object_format = "sha1"
            scan_head: str | None = None
            if repo and repo.get("head") and repo.get("root") == root:
                head_result = _run(["git", "rev-parse", "HEAD"], cwd=root)
                format_result = _run(["git", "rev-parse", "--show-object-format"], cwd=root)
                tree_result = _run(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=root, timeout=120)
                if head_result.returncode == 0:
                    scan_head = head_result.stdout.strip()
                if format_result.returncode == 0 and format_result.stdout.strip() in {"sha1", "sha256"}:
                    object_format = format_result.stdout.strip()
                if tree_result.returncode == 0:
                    for entry in tree_result.stdout.split("\0"):
                        if "\t" not in entry:
                            continue
                        metadata, repo_relative = entry.split("\t", 1)
                        fields = metadata.split()
                        if len(fields) >= 3 and fields[1] == "blob":
                            tree_blobs[repo_relative] = fields[2]
            for relative, path in sorted(files.items()):
                try:
                    raw, source_hash, _source_size, _source_modified = self._read_source_file(
                        root, path, retain_limit=self.max_source_bytes
                    )
                except KnowledgeError:
                    unstable += 1
                    continue
                content = raw[: self.max_text_bytes].decode("utf-8", errors="replace")
                metadata_payload = "\n".join((project, relative))
                if (
                    any(regex.search(content) for regex in SECRET_PATTERNS.values())
                    or any(regex.search(metadata_payload) for regex in SECRET_PATTERNS.values())
                    or any(regex.search(metadata_payload) for regex in PII_PATTERNS.values())
                ):
                    secret_quarantined += 1
                    continue
                if not repo or not scan_head or not tree_blobs:
                    dirty_paths.add(relative)
                else:
                    repo_relative = path.relative_to(repo["root"]).as_posix()
                    expected_blob = tree_blobs.get(repo_relative)
                    header = b"blob " + str(len(raw)).encode("ascii") + b"\0"
                    actual_blob = (
                        sha256(header + raw).hexdigest()
                        if object_format == "sha256"
                        else sha1(header + raw).hexdigest()
                    )
                    if expected_blob is None or actual_blob != expected_blob:
                        dirty_paths.add(relative)
                capability = self._test_capability(relative, content)
                count = self._case_count(path, content)
                capabilities[capability] += 1
                cases[capability] += count
                rows.append((relative, capability, count))
                manifest_rows.append((relative, source_hash, capability, count))
                self.index.db.execute(
                    """INSERT OR REPLACE INTO test_files(
                    project, path, capability, case_count, source_hash, last_seen_run
                    ) VALUES(?, ?, ?, ?, ?, ?)""",
                    (project, relative, capability, count, source_hash, run_id),
                )
            # Re-check HEAD and working-tree status after all reads.  Combined
            # with raw-vs-HEAD blob comparison above, this avoids promoting a
            # file changed after the engine's constructor snapshot to E2.
            if repo and scan_head:
                end_head = _run(["git", "rev-parse", "HEAD"], cwd=root)
                end_status = _run(
                    ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                    cwd=root,
                    timeout=120,
                )
                if end_head.returncode != 0 or end_head.stdout.strip() != scan_head:
                    dirty_paths.update(path for path, _, _ in rows)
                if end_status.returncode != 0:
                    dirty_paths.update(path for path, _, _ in rows)
                else:
                    entries = end_status.stdout.split("\0")
                    index = 0
                    changed_repo_paths: set[str] = set()
                    while index < len(entries):
                        entry = entries[index]
                        if len(entry) >= 4:
                            state, changed_path = entry[:2], entry[3:]
                            changed_repo_paths.add(changed_path)
                            if state[0] in {"R", "C"} or state[1] in {"R", "C"}:
                                index += 1
                                if index < len(entries) and entries[index]:
                                    changed_repo_paths.add(entries[index])
                        index += 1
                    for relative, _, _ in rows:
                        repo_relative = (root / relative).relative_to(repo["root"]).as_posix()
                        if repo_relative in changed_repo_paths:
                            dirty_paths.add(relative)
            dirty_test_files = len(dirty_paths)
            verification_unavailable = not (
                repo and scan_head and tree_blobs
            )
            degraded = bool(dirty_test_files or unstable or verification_unavailable)
            self.index.db.execute(
                "DELETE FROM test_files WHERE project = ? AND last_seen_run != ?", (project, run_id)
            )
            self.index.db.commit()
            note_path = Path("04-Testing") / "Inventories" / f"{project}.md"
            content = self._render_test_inventory(
                project,
                rows,
                capabilities,
                cases,
                dirty_test_files=dirty_test_files,
                secret_quarantined=secret_quarantined,
                unstable=unstable,
                verification_unavailable=verification_unavailable,
            )
            previous_id = f"test-inventory-{project}"
            previous = self.index.previous_generated(previous_id)
            previous_hash = previous["output_hash"] if previous else None
            try:
                vault_path, output_hash = self._write_managed(
                    note_path,
                    content,
                    previous_hash=previous_hash,
                    run_id=run_id,
                    conflict_id=previous_id,
                )
                self.index.upsert_generated(previous_id, vault_path, output_hash, run_id)
                inventory_hash = digest_bytes(strict_json(manifest_rows).encode("utf-8"))
                context = self.repo_context.get(project, {})
                verified_head = scan_head or context.get("head")
                evidence_level = "E1" if degraded else "E2"
                self.index.upsert_document(
                    knowledge_id=previous_id,
                    title=f"{project} test capability inventory",
                    body="\n".join(f"{path} {capability}" for path, capability, _ in rows),
                    project=project,
                    category="testing",
                    evidence_level=evidence_level,
                    claim_scope=(
                        "working-tree-test-definition-inventory"
                        if dirty_test_files
                        else "unverified-test-definition-inventory"
                        if verification_unavailable or unstable
                        else "test-definition-inventory"
                    ),
                    review_status="pending",
                    conflict_status="none",
                    freshness_status="unknown" if degraded else "current",
                    classification="P1-internal",
                    source_revision=(
                        f"WORKTREE:{verified_head}:manifest:{inventory_hash}"
                        if dirty_test_files
                        else f"git:{verified_head}:manifest:{inventory_hash}"
                    ),
                    source_sha256=inventory_hash,
                    git_state=(
                        "mixed-working-tree"
                        if dirty_test_files
                        else "verification-unavailable"
                        if verification_unavailable
                        else "unstable-read"
                        if unstable
                        else "tracked-clean"
                    ),
                    source_status=(
                        "partial"
                        if unstable
                        else "unavailable"
                        if not rows and verification_unavailable
                        else "active"
                    ),
                    historical=evidence_level == "E1",
                    vault_path=vault_path,
                    source_locator=f"workspace:{spec['root']}",
                )
            except HumanEditConflict:
                output_hash = None
                self.index.set_document_state(
                    previous_id,
                    source_status="conflict",
                    conflict_status="confirmed",
                    freshness_status="stale",
                )
            summaries[project] = {
                "status": (
                    "conflict"
                    if output_hash is None
                    else "partial"
                    if unstable
                    else "unavailable"
                    if not rows and verification_unavailable
                    else "indexed"
                ),
                "files": len(rows),
                "cases_detected": sum(cases.values()),
                "dirty_or_untracked_files": dirty_test_files,
                "secret_quarantined_files": secret_quarantined,
                "unstable_files": unstable,
                "capabilities": dict(sorted(capabilities.items())),
                "note": note_path.as_posix(),
                "output_hash": output_hash,
            }
        return summaries

    def _render_test_inventory(
        self,
        project: str,
        rows: list[tuple[str, str, int]],
        capabilities: Counter[str],
        cases: Counter[str],
        *,
        dirty_test_files: int,
        secret_quarantined: int,
        unstable: int,
        verification_unavailable: bool,
    ) -> str:
        degraded = bool(dirty_test_files or unstable or verification_unavailable)
        evidence_level = "E1" if degraded else "E2"
        claim_scope = (
            "working-tree-test-definition-inventory"
            if dirty_test_files
            else "unverified-test-definition-inventory"
            if verification_unavailable or unstable
            else "test-definition-inventory"
        )
        freshness = "unknown" if degraded else "current"
        lines = [
            "---",
            'schema_version: "1.0"',
            f"id: {_safe_yaml('test-inventory-' + project)}",
            'type: "test-inventory"',
            f"title: {_safe_yaml(project + ' test capability inventory')}",
            f"project: {_safe_yaml(project)}",
            'knowledge_status: "candidate"',
            f"evidence_level: {_safe_yaml(evidence_level)}",
            f"claim_scope: {_safe_yaml(claim_scope)}",
            'managed_by: "qingtian"',
            "human_lock: false",
            'review_status: "pending"',
            'conflict_status: "none"',
            f"freshness_status: {_safe_yaml(freshness)}",
            'privacy_classification: "P1-internal"',
            "---",
            MANAGED_MARKER,
            "",
            f"# {project} test capability inventory",
            "",
            "> [!warning] Definition inventory, not a test result",
            "> 这里只证明测试文件/用例定义存在，不证明执行、通过、覆盖有效或当前环境已验收。",
            "",
            f"- Dirty/untracked test definitions: **{dirty_test_files}**",
            f"- Secret-bearing files omitted: **{secret_quarantined}**",
            f"- Unstable files omitted: **{unstable}**",
            f"- Git verification unavailable: **{verification_unavailable}**",
            "",
            "## Capability summary",
            "",
            "| Capability | Files | Detected cases |",
            "|---|---:|---:|",
        ]
        for capability in sorted(capabilities):
            lines.append(f"| {capability} | {capabilities[capability]} | {cases[capability]} |")
        lines.extend(["", "## Files", "", "| Capability | Cases | Path |", "|---|---:|---|"])
        for path, capability, count in rows:
            lines.append(f"| {capability} | {count} | `{path}` |")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _git_ref_snapshot(root: Path) -> dict[str, Any] | None:
        head = _run(["git", "rev-parse", "HEAD"], cwd=root)
        branch = _run(["git", "branch", "--show-current"], cwd=root)
        refs = _run(
            ["git", "for-each-ref", "--format=%(refname)%09%(objectname)"],
            cwd=root,
            timeout=120,
        )
        status = _run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            timeout=120,
        )
        if any(result.returncode != 0 for result in (head, branch, refs, status)):
            return None
        branch_name = branch.stdout.strip() or None
        if branch_name and (
            any(regex.search(branch_name) for regex in SECRET_PATTERNS.values())
            or any(regex.search(branch_name) for regex in PII_PATTERNS.values())
        ):
            return None
        ref_lines = sorted(line for line in refs.stdout.splitlines() if line)
        status_entries = sorted(entry for entry in status.stdout.split("\0") if entry)
        payload = {
            "head": head.stdout.strip(),
            "branch": branch_name,
            "refs": ref_lines,
            "status": status_entries,
        }
        return {
            "head": payload["head"],
            "branch": branch_name,
            "dirty_count": len(status_entries),
            "ref_count": len(ref_lines),
            "fingerprint": digest_bytes(strict_json(payload).encode("utf-8")),
        }

    def ingest_git_history(self, run_id: str) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for spec in self.config.get("repositories", []):
            project = spec["project"]
            root = (self.workspace / spec["root"]).resolve()
            try:
                root.relative_to(self.workspace)
            except ValueError as exc:
                raise ConfigurationError(f"repository root escaped workspace: {spec['root']}") from exc
            before_snapshot = self._git_ref_snapshot(root) if root.is_dir() else None
            if before_snapshot is None:
                generated_id = f"git-history-{project}"
                self.index.db.execute("DELETE FROM git_commits WHERE project = ?", (project,))
                self.index.db.commit()
                written = self._write_unavailable_snapshot(
                    knowledge_id=generated_id,
                    relative=Path("06-Releases") / "Git-History" / f"{project}.md",
                    title=f"{project} Git history unavailable",
                    project=project,
                    category="releases-and-fixes",
                    reason="git-snapshot-failed",
                    run_id=run_id,
                    status="unavailable",
                )
                summary[project] = {
                    "status": "unavailable" if written else "conflict",
                    "error": "git-snapshot-failed",
                }
                continue
            log = _run(
                ["git", "log", "--all", "--date=iso-strict", "--pretty=format:%H%x1f%aI%x1f%s%x1e"],
                cwd=root,
                timeout=120,
            )
            if log.returncode != 0:
                generated_id = f"git-history-{project}"
                self.index.db.execute("DELETE FROM git_commits WHERE project = ?", (project,))
                self.index.db.commit()
                written = self._write_unavailable_snapshot(
                    knowledge_id=generated_id,
                    relative=Path("06-Releases") / "Git-History" / f"{project}.md",
                    title=f"{project} Git history unavailable",
                    project=project,
                    category="releases-and-fixes",
                    reason="git-log-failed",
                    run_id=run_id,
                    status="unavailable",
                )
                summary[project] = {
                    "status": "unavailable" if written else "conflict",
                    "error": "git-log-failed",
                }
                continue
            commits: list[tuple[str, str, str, bool]] = []
            redacted_subjects = 0
            for entry in log.stdout.split("\x1e"):
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.split("\x1f", 2)
                if len(parts) != 3:
                    continue
                commit_sha, committed_at, subject = parts
                subject, secret_kinds = _redact_secret_values(subject)
                pii_found = False
                for pii_regex in PII_PATTERNS.values():
                    if pii_regex.search(subject):
                        pii_found = True
                        subject = pii_regex.sub("[REDACTED:pii]", subject)
                if secret_kinds or pii_found:
                    redacted_subjects += 1
                is_fix = BUG_WORDS.search(subject) is not None
                commits.append((commit_sha, committed_at, subject.replace("\n", " "), is_fix))
            after_snapshot = self._git_ref_snapshot(root)
            if (
                after_snapshot is None
                or after_snapshot["fingerprint"] != before_snapshot["fingerprint"]
            ):
                generated_id = f"git-history-{project}"
                self.index.db.execute("DELETE FROM git_commits WHERE project = ?", (project,))
                self.index.db.commit()
                written = self._write_unavailable_snapshot(
                    knowledge_id=generated_id,
                    relative=Path("06-Releases") / "Git-History" / f"{project}.md",
                    title=f"{project} Git history unstable",
                    project=project,
                    category="releases-and-fixes",
                    reason="git-changed-during-scan",
                    run_id=run_id,
                    status="unstable",
                )
                summary[project] = {
                    "status": "unstable" if written else "conflict",
                    "error": "git-changed-during-scan",
                }
                continue
            for commit_sha, committed_at, subject, is_fix in commits:
                self.index.db.execute(
                    """INSERT OR REPLACE INTO git_commits(
                    project, commit_sha, committed_at, subject, is_fix, last_seen_run
                    ) VALUES(?, ?, ?, ?, ?, ?)""",
                    (project, commit_sha, committed_at, subject, int(is_fix), run_id),
                )
            self.index.db.execute(
                "DELETE FROM git_commits WHERE project = ? AND last_seen_run != ?", (project, run_id)
            )
            self.index.db.commit()
            note_path = Path("06-Releases") / "Git-History" / f"{project}.md"
            note = self._render_git_history(
                project,
                root,
                commits,
                redacted_subjects=redacted_subjects,
                snapshot=before_snapshot,
            )
            generated_id = f"git-history-{project}"
            previous = self.index.previous_generated(generated_id)
            had_conflict = False
            try:
                vault_path, output_hash = self._write_managed(
                    note_path,
                    note,
                    previous_hash=previous["output_hash"] if previous else None,
                    run_id=run_id,
                    conflict_id=generated_id,
                )
                self.index.upsert_generated(generated_id, vault_path, output_hash, run_id)
                body = "\n".join(f"{sha} {date} {subject}" for sha, date, subject, _ in commits)
                body_hash = digest_bytes(body.encode("utf-8"))
                self.index.upsert_document(
                    knowledge_id=generated_id,
                    title=f"{project} Git history",
                    body=body,
                    project=project,
                    category="releases-and-fixes",
                    evidence_level="E2",
                    claim_scope="git-metadata",
                    review_status="pending",
                    conflict_status="none",
                    freshness_status="current",
                    classification="P1-internal",
                    source_revision=f"git-local-refs:{before_snapshot['fingerprint']}",
                    source_sha256=body_hash,
                    git_state=(
                        "repository-dirty"
                        if before_snapshot["dirty_count"]
                        else "tracked-clean"
                    ),
                    source_status="active",
                    historical=True,
                    vault_path=vault_path,
                    source_locator=f"git:{spec['root']}",
                )
            except HumanEditConflict:
                had_conflict = True
                self.index.set_document_state(
                    generated_id,
                    source_status="conflict",
                    conflict_status="confirmed",
                    freshness_status="stale",
                )
            summary[project] = {
                "status": "conflict" if had_conflict else "indexed",
                "commits": len(commits),
                "fix_commits": sum(item[3] for item in commits),
                "redacted_subjects": redacted_subjects,
                "head": before_snapshot["head"],
                "local_refs_sha256": before_snapshot["fingerprint"],
                "ref_count": before_snapshot["ref_count"],
                "shallow": bool(self.repo_context.get(project, {}).get("shallow")),
            }
        return summary

    def _render_git_history(
        self,
        project: str,
        root: Path,
        commits: list[tuple[str, str, str, bool]],
        *,
        redacted_subjects: int,
        snapshot: dict[str, Any],
    ) -> str:
        context = self.repo_context.get(project, {})
        dirty = int(snapshot["dirty_count"])
        lines = [
            "---",
            'schema_version: "1.0"',
            f"id: {_safe_yaml('git-history-' + project)}",
            'type: "git-history"',
            f"title: {_safe_yaml(project + ' Git history')}",
            f"project: {_safe_yaml(project)}",
            'knowledge_status: "candidate"',
            'evidence_level: "E2"',
            'claim_scope: "git-metadata"',
            'managed_by: "qingtian"',
            "human_lock: false",
            'review_status: "pending"',
            'conflict_status: "none"',
            'freshness_status: "current"',
            'privacy_classification: "P1-internal"',
            f"repo_head: {_safe_yaml(snapshot.get('head'))}",
            f"repo_branch: {_safe_yaml(snapshot.get('branch'))}",
            f"canonical_ref: {_safe_yaml(context.get('canonical_ref'))}",
            f"authority_state: {_safe_yaml(context.get('authority_state'))}",
            f"local_refs_sha256: {_safe_yaml(snapshot.get('fingerprint'))}",
            f"local_ref_count: {int(snapshot.get('ref_count', 0))}",
            f"shallow_repository: {str(bool(context.get('shallow'))).lower()}",
            f"dirty_path_count: {dirty}",
            "---",
            MANAGED_MARKER,
            "",
            f"# {project} Git history",
            "",
            "> [!warning] Git metadata is not product acceptance",
            "> Commit subjects are historical engineering evidence. They do not prove deployment, behavior, or regression success.",
            "",
            f"- Commits indexed: **{len(commits)}**",
            f"- Fix-like subjects: **{sum(item[3] for item in commits)}**",
            f"- Current dirty/untracked paths: **{dirty}**",
            f"- Subjects redacted for secret/PII shapes: **{redacted_subjects}**",
            f"- Shallow repository: **{bool(context.get('shallow'))}**",
            "- Scope: local reachable refs only; this is not guaranteed to be complete remote history.",
            "",
            "## Fix-like history",
            "",
            "| Date | Commit | Subject |",
            "|---|---|---|",
        ]
        for sha, date, subject, is_fix in commits:
            if is_fix:
                safe_subject = subject.replace("|", "\\|")
                lines.append(f"| {date} | `{sha[:12]}` | {safe_subject} |")
        lines.extend(["", "## Locally reachable commit metadata", "", "| Date | Commit | Subject |", "|---|---|---|"])
        for sha, date, subject, _ in commits:
            safe_subject = subject.replace("|", "\\|")
            lines.append(f"| {date} | `{sha}` | {safe_subject} |")
        return "\n".join(lines).rstrip() + "\n"

    def generate_indexes(self, run_id: str) -> int:
        conflicts = 0
        source_rows = self.index.db.execute(
            """SELECT project, category, status, COUNT(*) AS count
            FROM sources GROUP BY project, category, status ORDER BY project, category, status"""
        ).fetchall()
        test_rows = self.index.db.execute(
            """SELECT project, capability, COUNT(*) AS files, SUM(case_count) AS cases
            FROM test_files WHERE last_seen_run = ?
            GROUP BY project, capability ORDER BY project, capability""",
            (run_id,),
        ).fetchall()
        active_source_rows = self.index.db.execute(
            """SELECT source_set, vault_path
            FROM sources
            WHERE status = 'active' AND vault_path IS NOT NULL AND pii = 'none'
              AND classification IN ('P0-public', 'P1-internal')
            ORDER BY source_set, vault_path"""
        ).fetchall()
        git_rows = self.index.db.execute(
            """SELECT project, COUNT(*) AS commits, SUM(is_fix) AS fixes
            FROM git_commits WHERE last_seen_run = ?
            GROUP BY project ORDER BY project""",
            (run_id,),
        ).fetchall()
        source_catalog = self._simple_index_note(
                "generated-source-catalog",
                "Source Catalog",
                ["Project", "Category", "Status", "Count"],
                [[row["project"], row["category"], row["status"], row["count"]] for row in source_rows],
            )
        source_catalog += self._active_source_link_section(active_source_rows)
        generated = {
            Path("99-System/Generated/Source-Catalog.md"): source_catalog,
            Path("99-System/Generated/Test-Capabilities.md"): self._simple_index_note(
                "generated-test-capabilities",
                "Test Capabilities",
                ["Project", "Capability", "Files", "Detected cases"],
                [
                    [
                        f"[[04-Testing/Inventories/{row['project']}|{row['project']}]]",
                        row["capability"],
                        row["files"],
                        row["cases"] or 0,
                    ]
                    for row in test_rows
                ],
            ),
            Path("99-System/Generated/Git-History.md"): self._simple_index_note(
                "generated-git-history",
                "Git History",
                ["Project", "Commits", "Fix-like subjects"],
                [
                    [
                        f"[[06-Releases/Git-History/{row['project']}|{row['project']}]]",
                        row["commits"],
                        row["fixes"] or 0,
                    ]
                    for row in git_rows
                ],
            ),
        }
        for relative, content in generated.items():
            generated_id = {
                "Source-Catalog.md": "generated-source-catalog",
                "Test-Capabilities.md": "generated-test-capabilities",
                "Git-History.md": "generated-git-history",
            }[relative.name]
            previous = self.index.previous_generated(generated_id)
            try:
                vault_path, output_hash = self._write_managed(
                    relative,
                    content,
                    previous_hash=previous["output_hash"] if previous else None,
                    run_id=run_id,
                    conflict_id=generated_id,
                    allow_recreate=True,
                )
                self.index.upsert_generated(
                    generated_id, vault_path, output_hash, run_id
                )
            except HumanEditConflict:
                conflicts += 1
                continue
        latest_relative = Path("00-Home") / "Ingestion-Status.md"
        body = (
            "---\n"
            'schema_version: "1.0"\n'
            'id: "moc-ingestion-status"\n'
            'type: "moc"\n'
            'title: "Ingestion Status"\n'
            'managed_by: "qingtian"\n'
            "human_lock: false\n"
            'review_status: "not-required"\n'
            'privacy_classification: "P1-internal"\n'
            f"last_ingestion_run: {_safe_yaml(run_id)}\n"
            "---\n"
            f"{MANAGED_MARKER}\n\n"
            "# Ingestion Status\n\n"
            f"Latest run: [[{run_id}]]\n\n"
            "- [[Source-Catalog]]\n- [[Test-Capabilities]]\n- [[Git-History]]\n"
        )
        generated_id = "moc-ingestion-status"
        previous = self.index.previous_generated(generated_id)
        try:
            vault_path, output_hash = self._write_managed(
                latest_relative,
                body,
                previous_hash=previous["output_hash"] if previous else None,
                run_id=run_id,
                conflict_id=generated_id,
                allow_recreate=True,
            )
            self.index.upsert_generated(generated_id, vault_path, output_hash, run_id)
        except HumanEditConflict:
            conflicts += 1
        return conflicts

    @staticmethod
    def _active_source_link_section(rows: Iterable[sqlite3.Row]) -> str:
        """Give every safe active source mirror a navigable Obsidian in-link."""

        grouped: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            source_set = str(row["source_set"])
            vault_path = str(row["vault_path"])
            target = vault_path[:-3] if vault_path.lower().endswith(".md") else vault_path
            pure = PurePosixPath(target)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not target.startswith("90-Sources/")
                or any(character in target for character in "#|[]\\\n\r")
            ):
                continue
            grouped[source_set].append(target)
        lines = [
            "",
            "## Active source mirrors",
            "",
            "Only current P0/P1 mirrors with no detected PII are linked here. "
            "Restricted, quarantined, conflicted and missing sources remain opaque.",
        ]
        for source_set in sorted(grouped):
            safe_heading = source_set.replace("`", "'").replace("\n", " ").replace("\r", " ")
            lines.extend(["", f"### `{safe_heading}`", ""])
            lines.extend(f"- [[{target}]]" for target in grouped[source_set])
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _simple_index_note(
        knowledge_id: str,
        title: str,
        headers: list[str],
        rows: list[list[Any]],
    ) -> str:
        lines = [
            "---",
            'schema_version: "1.0"',
            f"id: {_safe_yaml(knowledge_id)}",
            'type: "generated-index"',
            f"title: {_safe_yaml(title)}",
            'managed_by: "qingtian"',
            "human_lock: false",
            'review_status: "not-required"',
            'privacy_classification: "P1-internal"',
            "---",
            MANAGED_MARKER,
            "",
            f"# {title}",
            "",
            "| " + " | ".join(headers) + " |",
            "|" + "|".join("---" for _ in headers) + "|",
        ]
        for row in rows:
            lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |")
        return "\n".join(lines).rstrip() + "\n"

    def _validate_index_privacy(self) -> list[dict[str, str]]:
        """Fail closed if any persisted search/state surface contains PII."""

        errors: list[dict[str, str]] = []

        def contains_sensitive(values: Iterable[Any]) -> bool:
            payload = "\n".join(str(value) for value in values if value is not None)
            return any(regex.search(payload) for regex in SECRET_PATTERNS.values()) or any(
                regex.search(payload) for regex in PII_PATTERNS.values()
            )

        try:
            source_rows = self.index.db.execute(
                "SELECT knowledge_id, source_set, project, category, classification, "
                "evidence_level, claim_scope, source_path, relative_path, title, kind, "
                "repo_branch, git_state, pii, secret_kinds_json, vault_path, status "
                "FROM sources"
            ).fetchall()
            for row in source_rows:
                restricted = (
                    str(row["status"]).startswith("quarantined")
                    or row["classification"] in {"P2-confidential", "P3-restricted"}
                    or row["pii"] != "none"
                    or row["secret_kinds_json"] not in {"", "[]"}
                )
                persisted_path = str(row["source_path"])
                if restricted:
                    if persisted_path != "[withheld]":
                        errors.append(
                            {
                                "path": "state:sources",
                                "error": "restricted-source-path-not-withheld",
                            }
                        )
                else:
                    try:
                        expected_path = _portable_workspace_path(
                            str(row["source_set"]), str(row["relative_path"])
                        )
                    except KnowledgeError:
                        expected_path = None
                    if expected_path is None or persisted_path != expected_path:
                        errors.append(
                            {
                                "path": "state:sources",
                                "error": "active-source-path-not-portable",
                            }
                        )
                if contains_sensitive(row):
                    errors.append(
                        {
                            "path": "state:sources",
                            "error": "sensitive-source-metadata",
                        }
                    )

            privacy_tables = {
                "documents": (
                    "title",
                    "body",
                    "project",
                    "category",
                    "claim_scope",
                    "vault_path",
                    "source_locator",
                    "source_revision",
                ),
                "test_files": ("project", "path", "capability"),
                "git_commits": ("project", "subject"),
                "generated_outputs": ("vault_path", "status"),
                "ingestion_runs": ("receipt_json",),
            }
            for table, columns in privacy_tables.items():
                rows = self.index.db.execute(
                    f"SELECT {', '.join(columns)} FROM {table}"
                ).fetchall()
                if any(contains_sensitive(row) for row in rows):
                    errors.append(
                        {
                            "path": f"state:{table}",
                            "error": "sensitive-index-content",
                        }
                    )

            has_fts = self.index.db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='documents_fts'"
            ).fetchone()
            if has_fts is not None:
                fts_rows = self.index.db.execute(
                    "SELECT title, body FROM documents_fts"
                ).fetchall()
                if any(contains_sensitive(row) for row in fts_rows):
                    errors.append(
                        {
                            "path": "state:documents_fts",
                            "error": "sensitive-fts-content",
                        }
                    )
        except sqlite3.Error:
            return [{"path": "state:index", "error": "privacy-state-unreadable"}]
        try:
            before = os.fstat(self.index._database_fd)
            if before.st_size > 1024 * 1024 * 1024:
                raise OSError("state database exceeds privacy scan limit")
            raw = os.pread(self.index._database_fd, before.st_size, 0)
            after = os.fstat(self.index._database_fd)
            signature = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if len(raw) != before.st_size or signature(before) != signature(after):
                raise OSError("state database changed during privacy scan")
            raw_text = raw.decode("utf-8", errors="ignore")
            # SQLite record framing can place an all-digit hash/identifier slice
            # between binary separators and make it look like a phone number even
            # though no logical value contains one.  Phone detection therefore
            # stays on decoded logical columns; raw-page validation covers the
            # unambiguous path/identity patterns and every configured secret.
            raw_pii_patterns = (
                regex for name, regex in PII_PATTERNS.items() if name != "phone"
            )
            if any(regex.search(raw_text) for regex in SECRET_PATTERNS.values()) or any(
                regex.search(raw_text) for regex in raw_pii_patterns
            ):
                errors.append(
                    {
                        "path": "state:index-raw-bytes",
                        "error": "sensitive-raw-state-content",
                    }
                )
        except OSError:
            errors.append({"path": "state:index", "error": "privacy-state-unreadable"})
        return errors

    def validate_vault(self) -> dict[str, Any]:
        required = {"schema_version", "id", "type", "title", "managed_by", "privacy_classification"}
        enums = {
            "managed_by": {"human", "shared", "qingtian"},
            "privacy_classification": {
                "P0-public",
                "P1-internal",
                "P2-confidential",
                "P3-restricted",
            },
            "review_status": {"pending", "approved", "rejected", "not-required"},
            "evidence_level": {"E0", "E1", "E2", "E3", "E4"},
            "freshness_status": {"current", "unknown", "stale", "review-due", "expired"},
            "conflict_status": {"none", "suspected", "confirmed", "resolving", "resolved"},
            "knowledge_status": {"candidate", "curated", "disputed", "deprecated"},
        }
        ids: dict[str, str] = {}
        errors = self._validate_index_privacy()
        warnings: list[dict[str, str]] = []
        baseline_by_path: dict[str, dict[str, str]] = {}
        baseline_ids: set[str] = set()
        manifest_path = self._safe_vault_target(
            Path("99-System") / "Managed-Outputs-Manifest.json", create_parent=False
        )
        state_outputs = [
            dict(row)
            for row in self.index.db.execute(
                "SELECT knowledge_id, vault_path, output_hash, status "
                "FROM generated_outputs ORDER BY knowledge_id"
            ).fetchall()
        ]
        manifest_outputs: list[dict[str, Any]] | None = None
        if manifest_path.is_file():
            try:
                manifest_outputs = self._read_managed_manifest()["outputs"]
            except KnowledgeError:
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-invalid",
                    }
                )
            else:
                if strict_json(state_outputs) != strict_json(manifest_outputs):
                    errors.append(
                        {
                            "path": "99-System/Managed-Outputs-Manifest.json",
                            "error": "managed-manifest-state-mismatch",
                        }
                    )
        else:
            manifest_outputs = state_outputs
            if state_outputs:
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-missing-with-local-state",
                    }
                )
        for item in manifest_outputs or []:
            if not isinstance(item, dict):
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-entry-invalid",
                    }
                )
                continue
            relative = item.get("vault_path")
            knowledge_id = item.get("knowledge_id")
            output_hash = item.get("output_hash")
            if (
                not isinstance(relative, str)
                or not relative
                or not isinstance(knowledge_id, str)
                or not knowledge_id
                or not isinstance(output_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", output_hash)
            ):
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-entry-invalid",
                    }
                )
                continue
            if relative in baseline_by_path:
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-duplicate-path",
                    }
                )
                continue
            if knowledge_id in baseline_ids:
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-duplicate-id",
                    }
                )
                continue
            status = str(item.get("status", ""))
            if status not in ALLOWED_GENERATED_STATUSES:
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-invalid-status",
                    }
                )
                continue
            manifest_metadata = relative + "\n" + knowledge_id
            if any(regex.search(manifest_metadata) for regex in SECRET_PATTERNS.values()) or any(
                regex.search(manifest_metadata) for regex in PII_PATTERNS.values()
            ):
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-sensitive-metadata",
                    }
                )
                continue
            try:
                self._safe_vault_target(Path(relative), create_parent=False)
            except KnowledgeError:
                errors.append(
                    {
                        "path": "99-System/Managed-Outputs-Manifest.json",
                        "error": "managed-manifest-unsafe-path",
                    }
                )
                continue
            baseline_by_path[relative] = {
                "knowledge_id": knowledge_id,
                "output_hash": output_hash,
                "status": status,
            }
            baseline_ids.add(knowledge_id)

        # Validate the manifest in the reverse direction too.  A deleted or
        # replaced managed note must fail even when no file remains for rglob to
        # discover below.
        for relative, baseline in baseline_by_path.items():
            try:
                target = self._safe_vault_target(Path(relative), create_parent=False)
            except KnowledgeError:
                continue
            if (
                target.suffix.lower() != ".md"
                or target.is_symlink()
                or not target.is_file()
            ):
                errors.append({"path": relative, "error": "managed-output-missing-or-unsafe"})
                continue
            raw = target.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            if digest_bytes(raw) != baseline["output_hash"]:
                errors.append({"path": relative, "error": "managed-baseline-hash-mismatch"})
            if MANAGED_MARKER not in text:
                errors.append({"path": relative, "error": "managed-marker-missing"})
            if not _managed_identity_matches(
                baseline["knowledge_id"], baseline["status"], _frontmatter_fields(text)
            ):
                errors.append({"path": relative, "error": "managed-identity-mismatch"})

        def validate_receipt(relative: str, fields: dict[str, str], text: str) -> None:
            if not relative.startswith("99-System/Receipts/"):
                errors.append({"path": relative, "error": "receipt-outside-receipts-directory"})
                return
            run_id = Path(relative).stem
            match = re.search(r"\n```json\n(?P<payload>\{.*\})\n```\s*$", text, re.S)
            if not match:
                errors.append({"path": relative, "error": "receipt-json-missing"})
                return
            try:
                payload = json.loads(match.group("payload"))
            except json.JSONDecodeError:
                errors.append({"path": relative, "error": "receipt-json-invalid"})
                return
            if not isinstance(payload, dict) or payload.get("run_id") != run_id:
                errors.append({"path": relative, "error": "receipt-run-id-mismatch"})
                return
            claimed = payload.get("receipt_sha256")
            checksum_input = dict(payload)
            checksum_input.pop("receipt_sha256", None)
            expected = digest_bytes(strict_json(checksum_input).encode("utf-8"))
            if (
                claimed != expected
                or fields.get("receipt_sha256") != expected
                or fields.get("id") != "receipt-" + run_id
            ):
                errors.append({"path": relative, "error": "receipt-checksum-or-id-mismatch"})

        def validate_restricted_opaque(
            relative: str, fields: dict[str, str]
        ) -> None:
            note_type = fields.get("type")
            if note_type == "quarantine-record":
                source_id = fields.get("source_id", "")
                expected_path = f"95-Reviews/Quarantine/{source_id}.md"
                allowed_fields = {
                    "schema_version",
                    "id",
                    "type",
                    "title",
                    "managed_by",
                    "human_lock",
                    "review_status",
                    "privacy_classification",
                    "source_id",
                    "source_sha256",
                    "risk_kinds",
                }
                valid = (
                    relative == expected_path
                    and bool(re.fullmatch(r"src-[0-9a-f]{20}", source_id))
                    and fields.get("id") == "quarantine-" + source_id
                    and set(fields) == allowed_fields
                )
            elif note_type == "privacy-transition-conflict":
                source_id = fields.get("source_id", "")
                allowed_fields = {
                    "schema_version",
                    "id",
                    "type",
                    "title",
                    "managed_by",
                    "human_lock",
                    "review_status",
                    "conflict_status",
                    "freshness_status",
                    "privacy_classification",
                    "source_id",
                    "reason",
                    "baseline_output_sha256",
                    "current_output_sha256",
                }
                valid = (
                    Path(relative).parent == Path("95-Reviews/Conflicts")
                    and Path(relative).name.startswith("restricted-")
                    and Path(relative).suffix == ".md"
                    and bool(re.fullmatch(r"src-[0-9a-f]{20}", source_id))
                    and fields.get("id", "").startswith(
                        "restricted-conflict-" + source_id + "-"
                    )
                    and set(fields) == allowed_fields
                )
            else:
                valid = False
            if not valid:
                errors.append({"path": relative, "error": "invalid-restricted-opaque-schema"})

        all_vault_files = sorted(
            path
            for path in self.vault.rglob("*")
            if path.is_file() or path.is_symlink()
        )
        for path in all_vault_files:
            relative = path.relative_to(self.vault).as_posix()
            if path.suffix.lower() == ".md":
                continue
            if path.is_symlink():
                errors.append({"path": relative, "error": "symlinked-vault-file"})
                continue
            try:
                scan_bytes = path.read_bytes()[: self.max_text_bytes]
            except OSError:
                scan_bytes = b""
            scan_text = scan_bytes.decode("utf-8", errors="replace")
            for kind, regex in SECRET_PATTERNS.items():
                if regex.search(scan_text):
                    errors.append({"path": relative, "error": "secret-pattern:" + kind})
            if any(regex.search(scan_text) for regex in PII_PATTERNS.values()):
                errors.append({"path": relative, "error": "possible-pii"})
            if relative not in VAULT_JSON_ALLOWLIST:
                errors.append({"path": relative, "error": "unsupported-vault-file-type"})
                continue
            try:
                raw = path.read_bytes()
                json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                errors.append({"path": relative, "error": "invalid-allowlisted-json"})
                continue
            text = raw.decode("utf-8")
            # For allowlisted JSON scan the complete parsed payload, not only the
            # bounded preflight prefix above.
            if len(raw) > len(scan_bytes):
                for kind, regex in SECRET_PATTERNS.items():
                    if regex.search(text):
                        errors.append({"path": relative, "error": "secret-pattern:" + kind})
                if any(regex.search(text) for regex in PII_PATTERNS.values()):
                    errors.append({"path": relative, "error": "possible-pii"})

        files = [path for path in all_vault_files if path.suffix.lower() == ".md"]
        for path in files:
            relative = path.relative_to(self.vault).as_posix()
            cursor = self.vault
            unsafe_symlink = False
            for part in path.relative_to(self.vault).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    unsafe_symlink = True
                    break
            if unsafe_symlink:
                errors.append({"path": relative, "error": "symlinked-vault-note"})
                continue
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            if not text.startswith("---\n") or "\n---\n" not in text[4:]:
                errors.append({"path": relative, "error": "missing-frontmatter"})
                continue
            fields = _frontmatter_fields(text)
            missing = sorted(required - set(fields))
            if missing:
                errors.append({"path": relative, "error": "missing-fields:" + ",".join(missing)})
            if fields.get("schema_version") != "1.0":
                errors.append({"path": relative, "error": "unsupported-note-schema-version"})
            if not relative.startswith("_templates/"):
                for field in ("id", "type", "title"):
                    if not fields.get(field, "").strip():
                        errors.append({"path": relative, "error": f"empty-required-field:{field}"})
            if fields.get("managed_by") == "human":
                human_required = {
                    "human_lock",
                    "review_status",
                    "knowledge_status",
                    "evidence_level",
                    "claim_scope",
                    "conflict_status",
                    "freshness_status",
                }
                human_missing = sorted(human_required - set(fields))
                if human_missing:
                    errors.append(
                        {
                            "path": relative,
                            "error": "missing-human-governance-fields:" + ",".join(human_missing),
                        }
                    )
                if fields.get("human_lock") != "true":
                    errors.append({"path": relative, "error": "human-note-must-be-locked"})
                if not relative.startswith("_templates/"):
                    human_id = fields.get("id", "")
                    note_type = fields.get("type", "")
                    title = fields.get("title", "")
                    claim_scope = fields.get("claim_scope", "")
                    if not HUMAN_KNOWLEDGE_ID_PATTERN.fullmatch(human_id):
                        errors.append(
                            {"path": relative, "error": "invalid-human-governance:id"}
                        )
                    for field, value in (
                        ("type", note_type),
                        ("claim_scope", claim_scope),
                    ):
                        if not HUMAN_METADATA_PATTERN.fullmatch(value):
                            errors.append(
                                {
                                    "path": relative,
                                    "error": f"invalid-human-governance:{field}",
                                }
                            )
                    if not title.strip() or len(title) > 300:
                        errors.append(
                            {"path": relative, "error": "invalid-human-governance:title"}
                        )
                    for field in ("reviewer", "project", "product"):
                        if field in fields and not HUMAN_IDENTIFIER_PATTERN.fullmatch(
                            fields.get(field, "")
                        ):
                            errors.append(
                                {
                                    "path": relative,
                                    "error": f"invalid-human-governance:{field}",
                                }
                            )
                canonical_review_due = fields.get("review_due_at")
                legacy_review_due = fields.get("review_due")
                canonical_review_due_present = "review_due_at" in fields
                legacy_review_due_present = "review_due" in fields
                if legacy_review_due_present:
                    warnings.append(
                        {
                            "path": relative,
                            "warning": "deprecated-approval-field:review_due",
                        }
                    )
                review_due_conflict = (
                    canonical_review_due_present
                    and legacy_review_due_present
                    and canonical_review_due != legacy_review_due
                )
                if review_due_conflict:
                    errors.append(
                        {
                            "path": relative,
                            "error": "conflicting-approval-deadline-fields",
                        }
                    )
                effective_review_due = (
                    canonical_review_due
                    if canonical_review_due_present
                    else legacy_review_due
                )
                if fields.get("review_status") == "approved":
                    approval_missing = sorted(
                        {"reviewer", "reviewed_at"} - set(fields)
                    )
                    if not canonical_review_due_present and not legacy_review_due_present:
                        approval_missing.append("review_due_at")
                    if approval_missing:
                        errors.append(
                            {
                                "path": relative,
                                "error": "missing-approval-fields:" + ",".join(approval_missing),
                            }
                        )
                    if fields.get("knowledge_status") != "curated":
                        errors.append(
                            {"path": relative, "error": "approved-note-must-be-curated"}
                        )
                    parsed_dates: dict[str, datetime] = {}
                    for field, value in (
                        ("reviewed_at", fields.get("reviewed_at")),
                        ("review_due_at", effective_review_due),
                    ):
                        if not value:
                            continue
                        try:
                            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                            if parsed.tzinfo is None:
                                raise ValueError("timezone required")
                            parsed_dates[field] = parsed.astimezone(UTC)
                            if (
                                field == "review_due_at"
                                and parsed_dates[field] <= datetime.now(UTC)
                                and fields.get("freshness_status") == "current"
                            ):
                                errors.append(
                                    {
                                        "path": relative,
                                        "error": "approved-review-expired-but-current",
                                    }
                                )
                        except ValueError:
                            errors.append(
                                {"path": relative, "error": f"invalid-approval-date:{field}"}
                            )
                    reviewed_at = parsed_dates.get("reviewed_at")
                    review_due_at = parsed_dates.get("review_due_at")
                    if (
                        reviewed_at is not None
                        and fields.get("freshness_status") == "current"
                        and reviewed_at > datetime.now(UTC)
                    ):
                        errors.append(
                            {"path": relative, "error": "approved-review-is-in-future"}
                        )
                    if (
                        reviewed_at is not None
                        and review_due_at is not None
                        and reviewed_at > review_due_at
                    ):
                        errors.append(
                            {"path": relative, "error": "approved-review-after-deadline"}
                        )
            for field, allowed in enums.items():
                value = fields.get(field)
                if value is not None and value not in allowed:
                    errors.append(
                        {
                            "path": relative,
                            "error": f"invalid-enum:{field}:{value}",
                        }
                    )
            knowledge_id = fields.get("id")
            if knowledge_id:
                if knowledge_id in ids:
                    errors.append({"path": relative, "error": "duplicate-id:" + knowledge_id})
                ids[knowledge_id] = relative
            for kind, regex in SECRET_PATTERNS.items():
                if regex.search(text):
                    errors.append({"path": relative, "error": "secret-pattern:" + kind})
            if any(regex.search(text) for regex in PII_PATTERNS.values()):
                errors.append({"path": relative, "error": "possible-pii"})
            classification = fields.get("privacy_classification")
            if classification in {"P2-confidential", "P3-restricted"}:
                if fields.get("managed_by") != "qingtian":
                    errors.append(
                        {"path": relative, "error": "restricted-content-in-ordinary-vault-note"}
                    )
                else:
                    validate_restricted_opaque(relative, fields)
            if fields.get("managed_by") == "qingtian":
                if MANAGED_MARKER not in text:
                    errors.append({"path": relative, "error": "managed-marker-missing"})
                if fields.get("type") == "ingestion-receipt":
                    validate_receipt(relative, fields, text)
                else:
                    baseline = baseline_by_path.get(relative)
                    if baseline is None:
                        errors.append({"path": relative, "error": "managed-baseline-missing"})
                    else:
                        if digest_bytes(raw) != baseline["output_hash"]:
                            errors.append(
                                {"path": relative, "error": "managed-baseline-hash-mismatch"}
                            )
                        if not _managed_identity_matches(
                            baseline["knowledge_id"], baseline["status"], fields
                        ):
                            errors.append(
                                {"path": relative, "error": "managed-identity-mismatch"}
                            )
        return {
            "schema_version": 1,
            "status": "passed" if not errors else "failed",
            "markdown_files": len(files),
            "vault_files": len(all_vault_files),
            "unique_ids": len(ids),
            "errors": errors,
            "warnings": warnings,
        }

    def stats(self) -> dict[str, Any]:
        source = self.index.db.execute(
            "SELECT status, COUNT(*) AS count FROM sources GROUP BY status ORDER BY status"
        ).fetchall()
        return {
            "schema_version": 1,
            "sources": {row["status"]: row["count"] for row in source},
            "documents": self.index.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "test_files": self.index.db.execute("SELECT COUNT(*) FROM test_files").fetchone()[0],
            "test_cases_detected": self.index.db.execute(
                "SELECT COALESCE(SUM(case_count), 0) FROM test_files"
            ).fetchone()[0],
            "git_commits": self.index.db.execute("SELECT COUNT(*) FROM git_commits").fetchone()[0],
            "git_fix_commits": self.index.db.execute(
                "SELECT COALESCE(SUM(is_fix), 0) FROM git_commits"
            ).fetchone()[0],
            "fts5": self.index.fts_enabled,
            "vault": str(self.vault),
            "state_db": str(self.state_path),
        }
