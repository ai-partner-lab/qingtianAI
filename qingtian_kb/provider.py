from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Any, Iterable, Iterator
from urllib.parse import quote
from uuid import uuid4

from .engine import PII_PATTERNS, SECRET_PATTERNS, _cjk_ngrams
from .models import ConfigurationError, KnowledgeError, digest_bytes, utc_now


PROVIDER_SCHEMA_VERSION = "1.0"
DATABASE_SCHEMA_VERSION = "1"
ALLOWED_MODES = {"approved", "candidate", "history"}
ALLOWED_PURPOSES = {"human-research", "agent-context", "test"}
ALLOWED_EVIDENCE = {"E0", "E1", "E2", "E3", "E4"}
ALLOWED_REVIEW = {"pending", "approved", "rejected", "not-required"}
ALLOWED_CONFLICT = {"none", "suspected", "confirmed", "resolving", "resolved"}
ALLOWED_FRESHNESS = {"current", "unknown", "stale", "review-due", "expired"}
ALLOWED_KNOWLEDGE_STATUS = {"candidate", "curated", "disputed", "deprecated"}
ALLOWED_AUTHORITY_STATES = {
    "candidate",
    "authoritative",
    "resolved",
    "conflicted-deployment-docs",
    "conflicted-release-docs",
    "conflicted-source-of-truth",
}
CONFLICTED_AUTHORITY = re.compile(r"^conflicted-[a-z0-9]+(?:-[a-z0-9]+)*$")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SAFE_KNOWLEDGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_CANONICAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SAFE_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,255}$")
SHA256_HEX = re.compile(r"^[a-fA-F0-9]{64}$")
AUTHORITATIVE_REPOSITORY_STATES = {"authoritative"}
HUMAN_REQUIRED_FIELDS = {
    "schema_version",
    "id",
    "type",
    "title",
    "knowledge_status",
    "managed_by",
    "human_lock",
    "review_status",
    "privacy_classification",
    "evidence_level",
    "claim_scope",
    "conflict_status",
    "freshness_status",
}
DATABASE_REQUIRED_COLUMNS = {
    "documents": {
        "knowledge_id",
        "title",
        "body",
        "project",
        "category",
        "evidence_level",
        "claim_scope",
        "review_status",
        "conflict_status",
        "freshness_status",
        "classification",
        "source_revision",
        "source_sha256",
        "git_state",
        "source_status",
        "historical",
        "vault_path",
        "source_locator",
        "updated_at",
    },
    "sources": {"knowledge_id", "repo_branch", "repo_head"},
}


@dataclass(frozen=True)
class ProviderRequest:
    query: str
    caller_id: str
    purpose: str
    modes: tuple[str, ...] = ("approved",)
    projects: tuple[str, ...] = ()
    top_k: int = 10
    request_id: str | None = None


class QingtianKnowledgeProvider:
    """Read-only local adapter for Qingtian; deliberately not an ACL service."""

    def __init__(self, config_path: str | Path):
        raw_config_path = Path(config_path).expanduser().absolute()
        config_payload = self._read_regular_path_no_follow(
            raw_config_path, 1024 * 1024, "provider configuration"
        )
        try:
            self.config = json.loads(config_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("provider configuration is missing or invalid") from exc
        if not isinstance(self.config, dict) or self.config.get("schema_version") != 1:
            raise ConfigurationError("unsupported provider configuration schema")

        self.config_path = raw_config_path.resolve(strict=True)
        base = self.config_path.parent
        self.knowledge_root = base.parent.resolve(strict=True)
        marker = self.knowledge_root / ".qingtian-knowledge-root"
        marker_payload = self._read_regular_path_no_follow(
            marker, 128, "knowledge boundary marker"
        )
        if marker_payload.decode("utf-8", errors="replace").strip() != (
            "qingtian-knowledge-root-v1"
        ):
            raise ConfigurationError("provider is outside a Qingtian knowledge boundary")

        self.vault = self._resolve_configured_path(base, self.config.get("vault_root"), "vault")
        self.state_path = self._resolve_configured_path(
            base, self.config.get("state_db"), "state database"
        )
        if self.vault.parent != self.knowledge_root or not self.vault.is_dir():
            raise ConfigurationError("provider vault must be a direct knowledge-package child")
        if self.state_path.parent.parent != self.knowledge_root or not self.state_path.is_file():
            raise ConfigurationError(
                "provider state must be in a dedicated package directory"
            )
        self._reject_path_symlinks(self.vault)
        self._reject_path_symlinks(self.state_path)

        max_note_bytes = self.config.get("max_text_bytes", 1024 * 1024)
        if isinstance(max_note_bytes, bool) or not isinstance(max_note_bytes, int):
            raise ConfigurationError("provider maximum note size is invalid")
        self.max_note_bytes = min(max(max_note_bytes, 1), 1024 * 1024)
        self.repo_authority = self._validate_repository_authority(
            self.config.get("repositories", [])
        )

        before = os.stat(self.state_path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise KnowledgeError("provider read-only index is unavailable")
        if self._has_uncheckpointed_wal():
            raise KnowledgeError("provider index has uncheckpointed state")
        uri = "file:" + quote(str(self.state_path), safe="/") + "?mode=ro&immutable=1"
        try:
            self.db = sqlite3.connect(uri, uri=True, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only = ON")
            self.db.execute("PRAGMA trusted_schema = OFF")
            query_only = self.db.execute("PRAGMA query_only").fetchone()
            after = os.stat(self.state_path, follow_symlinks=False)
            if (
                query_only is None
                or int(query_only[0]) != 1
                or self._stat_signature(before) != self._stat_signature(after)
                or self._has_uncheckpointed_wal()
            ):
                raise KnowledgeError("provider could not establish a stable read-only index")
            self._validate_database_contract()
            self._index_signature = self._stat_signature(after)
        except (OSError, sqlite3.Error, ValueError) as exc:
            if hasattr(self, "db"):
                self.db.close()
            raise KnowledgeError("cannot open the knowledge index read-only") from exc
        except KnowledgeError:
            if hasattr(self, "db"):
                self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> QingtianKnowledgeProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def query(self, request: ProviderRequest) -> dict[str, Any]:
        self._assert_index_stable()
        normalized = self._normalize_request(request)
        warnings: Counter[str] = Counter()
        try:
            sqlite_hits = self._query_sqlite(normalized, warnings)
        except sqlite3.Error as exc:
            raise KnowledgeError("knowledge query failed") from exc
        vault_hits = self._query_human_vault(normalized, warnings)
        combined = self._deduplicate_hits(sqlite_hits + vault_hits, warnings)
        combined = [item for item in combined if item.get("_returnable", False)]
        combined.sort(
            key=lambda item: (
                float(item.get("_score", 0)),
                str(item.get("knowledge_id", "")),
            ),
            reverse=True,
        )
        selected = combined[: normalized.top_k]
        for item in selected:
            for internal in ("_score", "_raw_id", "_raw_hash", "_returnable"):
                item.pop(internal, None)
        self._assert_index_stable()
        return {
            "schema_version": PROVIDER_SCHEMA_VERSION,
            "request_id": normalized.request_id,
            "generated_at": utc_now(),
            "caller_id": normalized.caller_id,
            "purpose": normalized.purpose,
            "retrieval_modes": list(normalized.modes),
            "result_count": len(selected),
            "results": selected,
            "metadata_warnings": [
                {"code": code, "count": count}
                for code, count in sorted(warnings.items())
                if count > 0
            ],
            "acl_enforced": False,
            "production_integrated": False,
            "classification_filter_only": True,
            "query_persisted": False,
            "query_echoed": False,
            "query_privacy_scope": (
                "The provider itself does not persist or echo query text. Shell, terminal, "
                "stdin producer, operating-system telemetry, crash capture, and process memory "
                "are outside this guarantee."
            ),
            "policy_notice": (
                "Local read-only context provider only. Caller and purpose are audit labels, "
                "not authentication. Canonical refs are configuration declarations and have "
                "not been verified against a remote. Every result remains bounded by its "
                "provenance and claim scope."
            ),
        }

    def _normalize_request(self, request: ProviderRequest) -> ProviderRequest:
        if not isinstance(request.query, str):
            raise ConfigurationError("provider query is invalid")
        query = request.query.strip()
        if not query or len(request.query) > 4096:
            raise ConfigurationError("provider query must contain 1 to 4096 characters")
        if self._contains_sensitive(query):
            raise ConfigurationError("provider request contains sensitive-shaped content")
        if not isinstance(request.caller_id, str) or not self._safe_identifier(
            request.caller_id
        ):
            raise ConfigurationError("caller_id must be a non-sensitive stable identifier")
        if request.purpose not in ALLOWED_PURPOSES:
            raise ConfigurationError("unknown provider purpose")
        if not isinstance(request.modes, (tuple, list)) or not all(
            isinstance(mode, str) for mode in request.modes
        ):
            raise ConfigurationError("retrieval modes are invalid")
        modes = tuple(request.modes)
        if not modes or any(mode not in ALLOWED_MODES for mode in modes):
            raise ConfigurationError("retrieval modes must be approved/candidate/history")
        if len(set(modes)) != len(modes):
            raise ConfigurationError("retrieval modes must be unique")
        if not isinstance(request.projects, (tuple, list)) or not all(
            isinstance(project, str) for project in request.projects
        ):
            raise ConfigurationError("project filters are invalid")
        projects = tuple(request.projects)
        if len(set(projects)) != len(projects):
            raise ConfigurationError("project filters must be unique")
        if any(not self._safe_identifier(project) for project in projects):
            raise ConfigurationError("project filters must be non-sensitive stable identifiers")
        if isinstance(request.top_k, bool) or not isinstance(request.top_k, int):
            raise ConfigurationError("top_k must be an integer between 1 and 50")
        if not 1 <= request.top_k <= 50:
            raise ConfigurationError("top_k must be between 1 and 50")
        request_id = (
            "kq-" + uuid4().hex
            if request.request_id is None
            else request.request_id
        )
        if not isinstance(request_id, str) or not self._safe_identifier(request_id):
            raise ConfigurationError("request_id must be a non-sensitive stable identifier")
        return ProviderRequest(
            query=query,
            caller_id=request.caller_id,
            purpose=request.purpose,
            modes=modes,
            projects=projects,
            top_k=request.top_k,
            request_id=request_id,
        )

    def _query_sqlite(
        self, request: ProviderRequest, warnings: Counter[str]
    ) -> list[dict[str, Any]]:
        clauses = [
            "d.source_status='active'",
            "d.classification IN ('P0-public','P1-internal')",
        ]
        sql = (
            "SELECT d.knowledge_id, d.title, d.body, d.project, d.category, "
            "d.evidence_level, d.claim_scope, d.review_status, d.conflict_status, "
            "d.freshness_status, d.classification, d.source_revision, d.source_sha256, "
            "d.git_state, d.source_status, d.historical, d.vault_path, d.source_locator, "
            "d.updated_at, s.repo_branch, s.repo_head "
            "FROM documents d LEFT JOIN sources s ON s.knowledge_id=d.knowledge_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY d.updated_at DESC"
        )
        rows = self.db.execute(sql).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            required_text_columns = (
                "knowledge_id",
                "title",
                "body",
                "project",
                "category",
                "evidence_level",
                "claim_scope",
                "review_status",
                "conflict_status",
                "freshness_status",
                "classification",
                "git_state",
                "source_status",
                "vault_path",
                "source_locator",
                "updated_at",
            )
            optional_text_columns = (
                "source_revision",
                "source_sha256",
                "repo_branch",
                "repo_head",
            )
            if any(not isinstance(row[key], str) for key in required_text_columns) or any(
                row[key] is not None and not isinstance(row[key], str)
                for key in optional_text_columns
            ):
                warnings["sqlite-governance-invalid"] += 1
                continue
            project = str(row["project"])
            authority_config = self.repo_authority.get(project)
            authority_known = authority_config is not None
            if authority_config is None:
                warnings["repository-authority-unknown"] += 1
                authority_config = {"authority_state": "unknown", "canonical_ref": None}
            authority_state = authority_config["authority_state"]
            authority_conflicted = self._authority_is_conflicted(authority_state)
            if authority_conflicted:
                warnings["repository-authority-conflicted"] += 1

            evidence = str(row["evidence_level"])
            review = str(row["review_status"])
            freshness = str(row["freshness_status"])
            conflict_status = str(row["conflict_status"])
            claim_scope = str(row["claim_scope"])
            raw_id = str(row["knowledge_id"])
            category = str(row["category"])
            historical = bool(row["historical"]) or evidence == "E1" or (
                "historical" in claim_scope.lower()
            )
            project_selected = not request.projects or project in request.projects
            if (
                evidence not in ALLOWED_EVIDENCE
                or evidence == "E0"
                or review not in ALLOWED_REVIEW
                or freshness not in ALLOWED_FRESHNESS
                or conflict_status not in ALLOWED_CONFLICT
                or not SAFE_KNOWLEDGE_ID.fullmatch(raw_id)
                or not self._safe_metadata(claim_scope)
                or not self._safe_metadata(category)
            ):
                warnings["sqlite-governance-invalid"] += 1
                continue
            authority = self._eligible_sqlite_authority(
                review,
                freshness,
                evidence,
                historical,
                authority_state,
                request.modes,
            )

            title = str(row["title"])
            body = str(row["body"])
            vault_path = str(row["vault_path"])
            source_locator = str(row["source_locator"])
            source_revision = row["source_revision"]
            source_sha256 = row["source_sha256"]
            if (
                not title.strip()
                or len(title) > 300
                or not source_locator.strip()
                or row["historical"] not in (0, 1)
            ):
                warnings["sqlite-governance-invalid"] += 1
                continue
            outward_metadata = [
                raw_id,
                title,
                body,
                project,
                category,
                claim_scope,
                vault_path,
                source_locator,
                str(source_revision or ""),
                str(source_sha256 or ""),
                str(row["repo_branch"] or ""),
                str(row["repo_head"] or ""),
            ]
            if (
                self._contains_sensitive(*outward_metadata)
                or not self._safe_vault_relative(vault_path)
            ):
                warnings["sqlite-sensitive-or-unsafe-metadata"] += 1
                continue
            if source_sha256 is not None and not SHA256_HEX.fullmatch(str(source_sha256)):
                warnings["sqlite-governance-invalid"] += 1
                continue
            excerpt, _line, score = self._excerpt_and_score(request.query, title, body)
            returnable = (
                authority is not None
                # E1/history is explicitly non-generative and may be useful as
                # a lead before repository authority is configured.  Current
                # candidate/approved material remains fail-closed.
                and (authority == "history" or authority_known)
                and not authority_conflicted
                and conflict_status == "none"
                and project_selected
                and score > 0
            )
            if (
                score > 0
                and authority is None
                and review == "approved"
                and authority_state not in AUTHORITATIVE_REPOSITORY_STATES
            ):
                warnings["repository-not-authoritative"] += 1
            display_authority = authority or ("history" if historical else "candidate")
            raw_hash = (
                str(source_sha256).lower()
                if source_sha256 is not None
                else digest_bytes(body.encode("utf-8"))
            )
            results.append(
                {
                    "_score": score,
                    "_raw_id": raw_id,
                    "_raw_hash": raw_hash,
                    "_returnable": returnable,
                    "title": self._redact_query_echo(title, request.query),
                    "excerpt": excerpt,
                    "source_plane": "sqlite-index",
                    "authority": display_authority,
                    "authoritative": display_authority == "approved",
                    "eligible_for_generation": display_authority == "approved",
                    "usage_constraint": self._usage_constraint(display_authority),
                    "provenance": {
                        "vault_locator": f"vault:{vault_path}",
                        "source_locators": [source_locator],
                        "source_revision": source_revision,
                        "source_sha256": source_sha256,
                        "note_sha256": None,
                        "content_sha256": raw_hash,
                        "raw_knowledge_id": raw_id,
                        "repo_branch": row["repo_branch"],
                        "repo_head": row["repo_head"],
                        "canonical_ref": authority_config["canonical_ref"],
                        "canonical_ref_remote_verified": False,
                        "authority_state": authority_state,
                        "repository_authority_state": authority_state,
                        "project": project,
                        "category": category,
                        "knowledge_status": (
                            "curated" if review == "approved" else "candidate"
                        ),
                        "evidence_level": evidence,
                        "claim_scope": claim_scope,
                        "review_status": review,
                        "reviewer": None,
                        "reviewed_at": None,
                        "review_due_at": None,
                        "conflict_status": conflict_status,
                        "freshness_status": freshness,
                        "privacy_classification": str(row["classification"]),
                        "source_status": str(row["source_status"]),
                        "historical": historical,
                        "updated_at": str(row["updated_at"]),
                    },
                }
            )
        return results

    def _query_human_vault(
        self, request: ProviderRequest, warnings: Counter[str]
    ) -> list[dict[str, Any]]:
        eligible: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for note in self._iter_human_notes(warnings):
            fields = note["fields"]
            lists = note["lists"]
            review_due_at, review_due_conflict, legacy_review_due = (
                self._resolved_review_due_at(fields)
            )
            if review_due_conflict:
                # Never guess which approval deadline controls when both spellings
                # are present with different values.
                warnings["human-review-due-conflict"] += 1
                continue
            if legacy_review_due:
                warnings["human-review-due-legacy-alias"] += 1
            if not HUMAN_REQUIRED_FIELDS.issubset(fields):
                warnings["human-governance-incomplete"] += 1
                continue
            if not self._valid_human_governance(fields):
                warnings["human-governance-invalid"] += 1
                continue
            evidence = fields["evidence_level"]
            if evidence == "E0":
                warnings["human-e0-excluded"] += 1
                continue
            project = fields.get("project") or fields.get("product") or "unscoped"
            if not self._safe_identifier(project):
                warnings["human-governance-invalid"] += 1
                continue
            project_selected = not request.projects or project in request.projects
            repository_authority = self.repo_authority.get(project)
            repository_conflicted = bool(
                repository_authority is not None
                and self._authority_is_conflicted(repository_authority["authority_state"])
            )
            if repository_conflicted:
                warnings["repository-authority-conflicted"] += 1
            historical = evidence == "E1" or (
                "historical" in fields["claim_scope"].lower()
                or fields["knowledge_status"] == "deprecated"
                or fields.get("lifecycle_status") == "tombstoned"
            )
            authority, warning_code = self._eligible_human_authority(
                fields, historical, request.modes, now
            )
            if warning_code:
                warnings[warning_code] += 1
            excerpt, relative_line, score = self._excerpt_and_score(
                request.query, fields["title"], note["body"]
            )
            source_locators = list(lists.get("source_locators", []))
            if fields.get("source_locator"):
                source_locators.append(fields["source_locator"])
            source_locators = list(dict.fromkeys(source_locators))
            source_sha256 = fields.get("source_sha256")
            if source_sha256 and not SHA256_HEX.fullmatch(source_sha256):
                warnings["human-governance-invalid"] += 1
                continue
            if self._contains_sensitive(*source_locators, str(source_sha256 or "")):
                warnings["human-sensitive-metadata"] += 1
                continue
            raw_hash = (source_sha256 or note["note_hash"]).lower()
            canonical_ref = (
                repository_authority["canonical_ref"] if repository_authority else None
            )
            repository_state = (
                repository_authority["authority_state"] if repository_authority else None
            )
            returnable = (
                authority is not None
                and not repository_conflicted
                and project_selected
                and score > 0
            )
            display_authority = authority or ("history" if historical else "candidate")
            eligible.append(
                {
                    "_score": score + (40 if display_authority == "approved" else 0),
                    "_raw_id": fields["id"],
                    "_raw_hash": raw_hash,
                    "_returnable": returnable,
                    "title": self._redact_query_echo(fields["title"], request.query),
                    "excerpt": excerpt,
                    "source_plane": "human-vault",
                    "authority": display_authority,
                    "authoritative": display_authority == "approved",
                    "eligible_for_generation": display_authority == "approved",
                    "usage_constraint": self._usage_constraint(display_authority),
                    "provenance": {
                        "vault_locator": (
                            f"vault:{note['relative']}:"
                            f"{note['body_start_line'] + relative_line - 1}"
                        ),
                        "source_locators": source_locators,
                        "source_revision": "vault-sha256:" + note["note_hash"],
                        "source_sha256": source_sha256,
                        "note_sha256": note["note_hash"],
                        "content_sha256": raw_hash,
                        "raw_knowledge_id": fields["id"],
                        "repo_branch": None,
                        "repo_head": None,
                        "canonical_ref": canonical_ref,
                        "canonical_ref_remote_verified": False,
                        "authority_state": "human-vault-governed",
                        "repository_authority_state": repository_state,
                        "project": project,
                        "category": fields["type"],
                        "knowledge_status": fields["knowledge_status"],
                        "evidence_level": evidence,
                        "claim_scope": fields["claim_scope"],
                        "review_status": fields["review_status"],
                        "reviewer": fields.get("reviewer"),
                        "reviewed_at": fields.get("reviewed_at"),
                        "review_due_at": review_due_at,
                        "conflict_status": fields["conflict_status"],
                        "freshness_status": fields["freshness_status"],
                        "privacy_classification": fields["privacy_classification"],
                        "source_status": "active",
                        "historical": historical,
                        "updated_at": fields.get("updated_at", note["mtime"]),
                    },
                }
            )
        return eligible

    @staticmethod
    def _eligible_sqlite_authority(
        review: str,
        freshness: str,
        evidence: str,
        historical: bool,
        authority_state: str,
        modes: Iterable[str],
    ) -> str | None:
        requested = set(modes)
        if evidence == "E0" or review not in {"approved", "pending"}:
            return None
        if historical or evidence == "E1":
            if "history" in requested and freshness in {"current", "unknown"}:
                return "history"
            return None
        if freshness != "current" or evidence not in {"E2", "E3", "E4"}:
            return None
        if (
            review == "approved"
            and authority_state in AUTHORITATIVE_REPOSITORY_STATES
            and "approved" in requested
        ):
            return "approved"
        if "candidate" in requested:
            return "candidate"
        return None

    @classmethod
    def _eligible_human_authority(
        cls,
        fields: dict[str, str],
        historical: bool,
        modes: Iterable[str],
        now: datetime,
    ) -> tuple[str | None, str | None]:
        if fields["conflict_status"] != "none":
            return None, "human-conflict-excluded"
        if fields["knowledge_status"] == "disputed":
            return None, "human-conflict-excluded"
        requested = set(modes)
        review = fields["review_status"]
        freshness = fields["freshness_status"]
        evidence = fields["evidence_level"]
        if historical or evidence == "E1":
            if (
                "history" in requested
                and freshness in {"current", "unknown"}
                and review in {"approved", "pending"}
            ):
                return "history", None
            return None, None
        if freshness != "current" or evidence not in {"E2", "E3", "E4"}:
            return None, None
        if review == "approved":
            approval_valid, reason = cls._approval_metadata_valid(fields, now)
            if approval_valid and fields["knowledge_status"] == "curated":
                if "approved" in requested:
                    return "approved", None
                if "candidate" in requested:
                    return "candidate", None
                # A valid approved note that was simply not requested is not a
                # governance defect. History-only callers must not receive a
                # misleading approval-incomplete warning for excluded current
                # guidance.
                return None, None
            if "candidate" in requested and fields["knowledge_status"] in {
                "candidate",
                "curated",
            }:
                return "candidate", reason or "human-approval-incomplete"
            return None, reason or "human-approval-incomplete"
        if (
            review == "pending"
            and "candidate" in requested
            and fields["knowledge_status"] in {"candidate", "curated"}
        ):
            return "candidate", None
        return None, None

    @classmethod
    def _approval_metadata_valid(
        cls, fields: dict[str, str], now: datetime
    ) -> tuple[bool, str | None]:
        reviewer = fields.get("reviewer")
        reviewed_at = cls._parse_timestamp(fields.get("reviewed_at"))
        review_due_at_value, review_due_conflict, _legacy_review_due = (
            cls._resolved_review_due_at(fields)
        )
        if review_due_conflict:
            return False, "human-review-due-conflict"
        review_due_at = cls._parse_timestamp(review_due_at_value)
        if not reviewer or not cls._safe_identifier_value(reviewer):
            return False, "human-approval-incomplete"
        if reviewed_at is None or review_due_at is None or reviewed_at > review_due_at:
            return False, "human-approval-incomplete"
        if reviewed_at > now or review_due_at <= now:
            return False, "human-approval-expired"
        return True, None

    @staticmethod
    def _resolved_review_due_at(
        fields: dict[str, str],
    ) -> tuple[str | None, bool, bool]:
        """Resolve the canonical approval deadline and its legacy alias.

        Returns ``(value, conflict, legacy_present)``.  A legacy-only note remains
        readable during migration, while contradictory spellings fail closed.
        Provider responses always expose only ``review_due_at``.
        """

        canonical_present = "review_due_at" in fields
        legacy_present = "review_due" in fields
        canonical = fields.get("review_due_at")
        legacy = fields.get("review_due")
        conflict = canonical_present and legacy_present and canonical != legacy
        if conflict:
            return None, True, True
        return (canonical if canonical_present else legacy), False, legacy_present

    def _deduplicate_hits(
        self, hits: list[dict[str, Any]], warnings: Counter[str]
    ) -> list[dict[str, Any]]:
        by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for hit in hits:
            by_id[str(hit["_raw_id"])].append(hit)

        conflicted_ids: set[str] = set()
        conflicted_hashes: set[str] = set()
        for raw_id, group in by_id.items():
            hashes = {str(hit["_raw_hash"]) for hit in group}
            if len(hashes) > 1:
                conflicted_ids.add(raw_id)
                conflicted_hashes.update(hashes)
                warnings["duplicate-id-content-conflict"] += 1

        safe_hits = [
            hit
            for hit in hits
            if hit["_raw_id"] not in conflicted_ids
            and hit["_raw_hash"] not in conflicted_hashes
        ]
        by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for hit in safe_hits:
            by_hash[str(hit["_raw_hash"])].append(hit)

        deduplicated: list[dict[str, Any]] = []
        for raw_hash, group in by_hash.items():
            raw_ids = {str(hit["_raw_id"]) for hit in group}
            chosen = max(group, key=self._dedupe_preference)
            if len(group) > 1:
                warnings["duplicate-content-deduplicated"] += len(group) - 1
            if len(raw_ids) == 1:
                chosen["knowledge_id"] = "kb:" + next(iter(raw_ids))
            else:
                chosen["knowledge_id"] = "kb-sha256:" + raw_hash[:32]
            deduplicated.append(chosen)
        return deduplicated

    @staticmethod
    def _dedupe_preference(item: dict[str, Any]) -> tuple[int, int, int, float, str]:
        authority_rank = {"approved": 3, "candidate": 2, "history": 1}
        return (
            1 if item.get("_returnable") else 0,
            authority_rank.get(str(item.get("authority")), 0),
            1 if item.get("source_plane") == "human-vault" else 0,
            float(item.get("_score", 0)),
            str(item.get("_raw_id", "")),
        )

    @staticmethod
    def _usage_constraint(authority: str) -> str:
        if authority == "candidate":
            return (
                "Review lead only; not eligible for generation, product facts, or release decisions."
            )
        if authority == "history":
            return (
                "Historical lead only; does not prove current behavior, deployment, or acceptance."
            )
        return (
            "Approved only within the cited claim scope; deployment/acceptance still require "
            "matching evidence."
        )

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        query_key = query.casefold()
        values = [*re.split(r"\s+", query), *_cjk_ngrams(query)]
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.casefold()
            if not value or key == query_key or key in seen:
                continue
            seen.add(key)
            unique.append(value)
        return unique[:31]

    @classmethod
    def _excerpt_and_score(cls, query: str, title: str, body: str) -> tuple[str, int, float]:
        lowered_query = query.casefold()
        title_lower = title.casefold()
        body_lower = body.casefold()
        terms = [term.casefold() for term in cls._query_terms(query)]
        score = 0.0
        if lowered_query in title_lower:
            score += 240
        if lowered_query in body_lower:
            score += 120
        for term in terms:
            if term in title_lower:
                score += 24 + min(len(term), 12)
            score += min(body_lower.count(term), 5) * (4 + min(len(term), 8))
        offsets = [body_lower.find(term) for term in [lowered_query, *terms]]
        offsets = [offset for offset in offsets if offset >= 0]
        offset = min(offsets) if offsets else 0
        start = max(0, offset - 100)
        excerpt = body[start : offset + 300].replace("\n", " ").strip()
        excerpt = cls._redact_query_echo(excerpt, query)
        line = body[:offset].count("\n") + 1
        return excerpt, line, score

    @staticmethod
    def _redact_query_echo(value: str, query: str) -> str:
        return re.sub(re.escape(query), "[MATCH]", value, flags=re.IGNORECASE)

    @staticmethod
    def _parse_frontmatter(
        text: str,
    ) -> tuple[dict[str, str], dict[str, list[str]], str, int] | None:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return None
        closing: int | None = None
        for index, line in enumerate(lines[1:], start=1):
            if line.rstrip("\r\n") == "---":
                closing = index
                break
        if closing is None:
            return None
        fields: dict[str, str] = {}
        lists: dict[str, list[str]] = {}
        current_list: str | None = None
        for raw_line in lines[1:closing]:
            line = raw_line.rstrip("\r\n")
            if line.startswith(("  - ", "- ")) and current_list:
                value = line.split("-", 1)[1].strip().strip('"').strip("'")
                if value:
                    lists[current_list].append(value)
                continue
            if line.startswith((" ", "\t")) or ":" not in line:
                return None
            key, raw_value = line.split(":", 1)
            key = key.strip()
            raw_value = raw_value.strip()
            if not key or key in fields or key in lists:
                return None
            if raw_value == "":
                current_list = key
                lists[key] = []
                continue
            current_list = None
            if raw_value.startswith('"'):
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError:
                    return None
                if not isinstance(value, str):
                    value = str(value).lower() if isinstance(value, bool) else str(value)
            else:
                value = raw_value.strip("'")
            fields[key] = value
        body = "".join(lines[closing + 1 :])
        body_start_line = closing + 2
        return fields, lists, body, body_start_line

    def _iter_human_notes(self, warnings: Counter[str]) -> Iterator[dict[str, Any]]:
        excluded_prefixes = (
            ".obsidian/",
            "_templates/",
            "90-Sources/",
            "95-Reviews/",
            "99-System/Generated/",
            "99-System/Receipts/",
        )
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.vault, directory_flags)
        except OSError:
            warnings["human-vault-unavailable"] += 1
            return
        stack: list[tuple[int, str]] = [(root_fd, "")]
        while stack:
            directory_fd, relative_directory = stack.pop()
            try:
                directory_path = (
                    self.vault
                    if not relative_directory
                    else self.vault.joinpath(*PurePosixPath(relative_directory).parts)
                )
                directory_stat = os.fstat(directory_fd)
                if not self._fd_path_stable(directory_path, directory_stat):
                    warnings["human-vault-path-unstable"] += 1
                    continue
                try:
                    names = sorted(os.listdir(directory_fd))
                except OSError:
                    warnings["human-vault-read-failed"] += 1
                    continue
                for name in names:
                    relative = f"{relative_directory}/{name}" if relative_directory else name
                    if relative.startswith(excluded_prefixes):
                        continue
                    try:
                        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError:
                        warnings["human-vault-entry-unstable"] += 1
                        continue
                    if stat.S_ISLNK(entry_stat.st_mode):
                        warnings["human-vault-symlink-excluded"] += 1
                        continue
                    if stat.S_ISDIR(entry_stat.st_mode):
                        try:
                            child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                            child_stat = os.fstat(child_fd)
                        except OSError:
                            warnings["human-vault-entry-unstable"] += 1
                            continue
                        if (entry_stat.st_dev, entry_stat.st_ino) != (
                            child_stat.st_dev,
                            child_stat.st_ino,
                        ):
                            os.close(child_fd)
                            warnings["human-vault-entry-unstable"] += 1
                            continue
                        stack.append((child_fd, relative))
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode) or not name.endswith(".md"):
                        continue
                    if entry_stat.st_size > self.max_note_bytes:
                        warnings["human-vault-note-too-large"] += 1
                        continue
                    try:
                        file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                    except OSError:
                        warnings["human-vault-entry-unstable"] += 1
                        continue
                    try:
                        before = os.fstat(file_fd)
                        if (entry_stat.st_dev, entry_stat.st_ino) != (
                            before.st_dev,
                            before.st_ino,
                        ):
                            warnings["human-vault-entry-unstable"] += 1
                            continue
                        chunks: list[bytes] = []
                        remaining = self.max_note_bytes + 1
                        while remaining > 0:
                            chunk = os.read(file_fd, min(65536, remaining))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        raw = b"".join(chunks)
                        after = os.fstat(file_fd)
                    finally:
                        os.close(file_fd)
                    if len(raw) > self.max_note_bytes or self._stat_signature(before) != (
                        self._stat_signature(after)
                    ):
                        warnings["human-vault-entry-unstable"] += 1
                        continue
                    path = self.vault.joinpath(*PurePosixPath(relative).parts)
                    if not self._fd_path_stable(path, after):
                        warnings["human-vault-path-unstable"] += 1
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    if self._contains_sensitive(text, relative):
                        warnings["human-sensitive-note-excluded"] += 1
                        continue
                    parsed = self._parse_frontmatter(text)
                    if parsed is None:
                        warnings["human-frontmatter-invalid"] += 1
                        continue
                    fields, lists, body, body_start_line = parsed
                    # Generated/shared notes have their own SQLite/source-plane
                    # governance.  They may live in ordinary navigation folders,
                    # but they are not malformed human approvals and must not
                    # inflate human-governance warnings.
                    if fields.get("managed_by") != "human":
                        continue
                    yield {
                        "relative": relative,
                        "fields": fields,
                        "lists": lists,
                        "body": body,
                        "body_start_line": body_start_line,
                        "note_hash": digest_bytes(raw),
                        "mtime": datetime.fromtimestamp(after.st_mtime, UTC)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                    }
            finally:
                os.close(directory_fd)

    def _valid_human_governance(self, fields: dict[str, str]) -> bool:
        if (
            fields.get("schema_version") != PROVIDER_SCHEMA_VERSION
            or fields.get("managed_by") != "human"
            or fields.get("human_lock") != "true"
            or fields.get("privacy_classification") not in {"P0-public", "P1-internal"}
            or fields.get("evidence_level") not in ALLOWED_EVIDENCE
            or fields.get("review_status") not in ALLOWED_REVIEW
            or fields.get("conflict_status") not in ALLOWED_CONFLICT
            or fields.get("freshness_status") not in ALLOWED_FRESHNESS
            or fields.get("knowledge_status") not in ALLOWED_KNOWLEDGE_STATUS
            or not SAFE_KNOWLEDGE_ID.fullmatch(fields.get("id", ""))
            or not self._safe_metadata(fields.get("type", ""))
            or not self._safe_metadata(fields.get("claim_scope", ""))
        ):
            return False
        title = fields.get("title", "")
        if not title.strip() or len(title) > 300 or self._contains_sensitive(title):
            return False
        for field in ("reviewer", "project", "product"):
            if field in fields and not self._safe_identifier(fields.get(field, "")):
                return False
        return True

    def _validate_repository_authority(
        self, repositories: Any
    ) -> dict[str, dict[str, str]]:
        if not isinstance(repositories, list):
            raise ConfigurationError("repositories must be a list")
        result: dict[str, dict[str, str]] = {}
        for item in repositories:
            if not isinstance(item, dict):
                raise ConfigurationError("repository authority configuration is invalid")
            project = item.get("project")
            authority_state = item.get("authority_state")
            canonical_ref = item.get("canonical_ref")
            if (
                not isinstance(project, str)
                or not self._safe_identifier(project)
                or project in result
            ):
                raise ConfigurationError("repository authority project is invalid")
            if (
                not isinstance(authority_state, str)
                or not authority_state
                or authority_state not in ALLOWED_AUTHORITY_STATES
                or (
                    authority_state.startswith("conflicted-")
                    and not CONFLICTED_AUTHORITY.fullmatch(authority_state)
                )
                or self._contains_sensitive(authority_state)
            ):
                raise ConfigurationError("repository authority state is invalid")
            if (
                not isinstance(canonical_ref, str)
                or not SAFE_CANONICAL_REF.fullmatch(canonical_ref)
                or ".." in canonical_ref.split("/")
                or "//" in canonical_ref
                or canonical_ref.endswith(("/", ".lock"))
                or self._contains_sensitive(canonical_ref)
            ):
                raise ConfigurationError("repository canonical reference is invalid")
            result[project] = {
                "authority_state": authority_state,
                "canonical_ref": canonical_ref,
            }
        return result

    def _validate_database_contract(self) -> None:
        try:
            rows = self.db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchall()
            if len(rows) != 1 or str(rows[0][0]) != DATABASE_SCHEMA_VERSION:
                raise KnowledgeError("unsupported knowledge index schema")
            for table, required in DATABASE_REQUIRED_COLUMNS.items():
                columns = {
                    str(column["name"])
                    for column in self.db.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not required.issubset(columns):
                    raise KnowledgeError("knowledge index schema is incomplete")
        except sqlite3.Error as exc:
            raise KnowledgeError("knowledge index schema is unavailable") from exc

    def _assert_index_stable(self) -> None:
        try:
            current = os.stat(self.state_path, follow_symlinks=False)
        except OSError as exc:
            raise KnowledgeError("provider read-only index changed") from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or self._stat_signature(current) != self._index_signature
            or self._has_uncheckpointed_wal()
        ):
            raise KnowledgeError("provider read-only index changed")

    def _has_uncheckpointed_wal(self) -> bool:
        wal_path = Path(str(self.state_path) + "-wal")
        try:
            wal_stat = os.stat(wal_path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return stat.S_ISLNK(wal_stat.st_mode) or not stat.S_ISREG(wal_stat.st_mode) or (
            wal_stat.st_size > 0
        )

    @classmethod
    def _safe_identifier(cls, value: str) -> bool:
        return bool(SAFE_IDENTIFIER.fullmatch(value)) and not cls._contains_sensitive(value)

    @classmethod
    def _safe_identifier_value(cls, value: str) -> bool:
        return cls._safe_identifier(value)

    @classmethod
    def _safe_metadata(cls, value: str) -> bool:
        return bool(value and SAFE_METADATA.fullmatch(value)) and not cls._contains_sensitive(value)

    @staticmethod
    def _authority_is_conflicted(authority_state: str) -> bool:
        return bool(CONFLICTED_AUTHORITY.fullmatch(authority_state))

    @staticmethod
    def _contains_sensitive(*values: str) -> bool:
        payload = "\n".join(value for value in values if isinstance(value, str))
        return any(regex.search(payload) for regex in SECRET_PATTERNS.values()) or any(
            regex.search(payload) for regex in PII_PATTERNS.values()
        )

    @staticmethod
    def _safe_vault_relative(value: str) -> bool:
        path = PurePosixPath(value)
        return bool(
            value
            and not path.is_absolute()
            and ".." not in path.parts
            and path.suffix == ".md"
        )

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value or not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    @staticmethod
    def _read_regular_path_no_follow(path: Path, limit: int, label: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ConfigurationError(f"{label} is missing or invalid") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ConfigurationError(f"{label} is missing or invalid")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(payload) > limit or QingtianKnowledgeProvider._stat_signature(before) != (
                QingtianKnowledgeProvider._stat_signature(after)
            ):
                raise ConfigurationError(f"{label} changed while being read")
            return payload
        finally:
            os.close(descriptor)

    def _resolve_configured_path(self, base: Path, value: Any, label: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"provider {label} path is invalid")
        relative = Path(value)
        if relative.is_absolute():
            raise ConfigurationError(f"provider {label} path must be relative")
        cursor = base
        for part in relative.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                cursor = cursor.parent
                continue
            cursor = cursor / part
            if cursor.is_symlink():
                raise ConfigurationError(f"provider {label} path contains a symlink")
        try:
            return cursor.resolve(strict=True)
        except OSError as exc:
            raise ConfigurationError(f"provider {label} path is unavailable") from exc

    def _reject_path_symlinks(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.knowledge_root)
        except ValueError as exc:
            raise ConfigurationError("provider path escaped the knowledge package") from exc
        cursor = self.knowledge_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ConfigurationError("provider path contains a symlink")

    @staticmethod
    def _fd_path_stable(path: Path, fd_stat: os.stat_result) -> bool:
        try:
            current = os.stat(path, follow_symlinks=False)
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        return (
            not path.is_symlink()
            and resolved == path
            and (current.st_dev, current.st_ino) == (fd_stat.st_dev, fd_stat.st_ino)
        )
