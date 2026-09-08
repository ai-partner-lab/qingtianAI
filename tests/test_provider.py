from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qingtian_kb.cli import main
from qingtian_kb.engine import KnowledgeEngine
from qingtian_kb.models import ConfigurationError, KnowledgeError
from qingtian_kb.provider import ProviderRequest, QingtianKnowledgeProvider


class QingtianProviderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="qingtian-provider-test-")
        self.knowledge = Path(self.temporary.name) / "knowledge"
        self.config_dir = self.knowledge / "config"
        self.vault = self.knowledge / "vault"
        self.state_dir = self.knowledge / "state"
        self.state = self.state_dir / "index.sqlite3"
        for directory in (self.config_dir, self.vault, self.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        (self.knowledge / ".qingtian-knowledge-root").write_text(
            "qingtian-knowledge-root-v1\n", encoding="utf-8"
        )
        self.config_path = self.config_dir / "sources.json"
        self.repositories = [
            {
                "project": "sample",
                "root": "sample",
                "canonical_ref": "origin/main",
                "authority_state": "authoritative",
            },
            {
                "project": "candidate-project",
                "root": "candidate",
                "canonical_ref": "main",
                "authority_state": "candidate",
            },
            {
                "project": "conflicted-project",
                "root": "conflicted",
                "canonical_ref": "origin/dev",
                "authority_state": "conflicted-source-of-truth",
            },
        ]
        self.write_config()
        self.create_database()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, repositories: object | None = None) -> None:
        payload = {
            "schema_version": 1,
            "workspace_root": "../..",
            "vault_root": "../vault",
            "state_db": "../state/index.sqlite3",
            "max_text_bytes": 128 * 1024,
            "source_sets": [],
            "repositories": self.repositories if repositories is None else repositories,
            "test_roots": [],
        }
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")

    def create_database(self) -> None:
        connection = sqlite3.connect(self.state)
        try:
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value) VALUES('schema_version', '1');
                CREATE TABLE sources (
                  knowledge_id TEXT PRIMARY KEY,
                  repo_branch TEXT,
                  repo_head TEXT
                );
                CREATE TABLE documents (
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
                  classification TEXT NOT NULL,
                  source_revision TEXT,
                  source_sha256 TEXT,
                  git_state TEXT NOT NULL,
                  source_status TEXT NOT NULL,
                  historical INTEGER NOT NULL,
                  vault_path TEXT NOT NULL,
                  source_locator TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    def insert_document(
        self,
        knowledge_id: str,
        *,
        body: str = "A searchable needle appears here.",
        title: str = "Provider note",
        project: str = "sample",
        evidence: str = "E2",
        review: str = "approved",
        conflict: str = "none",
        freshness: str = "current",
        classification: str = "P1-internal",
        historical: bool = False,
        source_hash: str | None = None,
    ) -> str:
        digest = source_hash or hashlib.sha256(body.encode("utf-8")).hexdigest()
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "INSERT INTO sources(knowledge_id, repo_branch, repo_head) VALUES(?,?,?)",
                (knowledge_id, "main", "a" * 40),
            )
            connection.execute(
                """
                INSERT INTO documents(
                  knowledge_id,title,body,project,category,evidence_level,claim_scope,
                  review_status,conflict_status,freshness_status,classification,
                  source_revision,source_sha256,git_state,source_status,historical,
                  vault_path,source_locator,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    knowledge_id,
                    title,
                    body,
                    project,
                    "product",
                    evidence,
                    "historical-document" if historical or evidence == "E1" else "product-fact",
                    review,
                    conflict,
                    freshness,
                    classification,
                    "git:" + "a" * 40,
                    digest,
                    "clean",
                    "active",
                    int(historical),
                    f"90-Sources/{knowledge_id}.md",
                    f"workspace:docs/{knowledge_id}.md",
                    "2026-09-07T00:00:00Z",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return digest

    def write_human_note(
        self,
        note_id: str,
        *,
        body: str = "Human searchable needle is on this line.\n",
        filename: str | None = None,
        evidence: str = "E2",
        review: str = "approved",
        knowledge_status: str = "curated",
        freshness: str = "current",
        conflict: str = "none",
        reviewer: str | None = "qa.owner",
        reviewed_at: str | None = "2025-01-01T00:00:00Z",
        review_due_at: str | None = "2099-01-01T00:00:00Z",
        legacy_review_due: str | None = None,
        project: str = "sample",
        source_hash: str | None = None,
    ) -> Path:
        directory = self.vault / "01-Product"
        directory.mkdir(parents=True, exist_ok=True)
        fields = [
            "---",
            'schema_version: "1.0"',
            f'id: "{note_id}"',
            'type: "product-fact"',
            'title: "Human Provider Note"',
            f'knowledge_status: "{knowledge_status}"',
            'managed_by: "human"',
            "human_lock: true",
            f'review_status: "{review}"',
            f'privacy_classification: "P1-internal"',
            f'evidence_level: "{evidence}"',
            (
                'claim_scope: "historical-document"'
                if evidence == "E1"
                else 'claim_scope: "product-fact"'
            ),
            f'conflict_status: "{conflict}"',
            f'freshness_status: "{freshness}"',
            f'project: "{project}"',
        ]
        if reviewer is not None:
            fields.append(f'reviewer: "{reviewer}"')
        if reviewed_at is not None:
            fields.append(f'reviewed_at: "{reviewed_at}"')
        if review_due_at is not None:
            fields.append(f'review_due_at: "{review_due_at}"')
        if legacy_review_due is not None:
            fields.append(f'review_due: "{legacy_review_due}"')
        if source_hash is not None:
            fields.append(f'source_sha256: "{source_hash}"')
        fields.extend(["---", ""])
        path = directory / (filename or f"{note_id}.md")
        path.write_text("\n".join(fields) + body, encoding="utf-8")
        return path

    @staticmethod
    def request(query: str, *modes: str) -> ProviderRequest:
        return ProviderRequest(
            query=query,
            caller_id="test.runner",
            purpose="test",
            modes=tuple(modes) or ("approved",),
            request_id="request-1",
        )

    def filesystem_snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.knowledge).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in self.knowledge.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def test_sqlite_is_read_only_and_modes_fail_closed(self) -> None:
        self.insert_document("approved-note", body="approved needle")
        self.insert_document("candidate-note", body="candidate needle", review="pending")
        self.insert_document(
            "history-note", body="history needle", evidence="E1", historical=True
        )
        self.insert_document(
            "private-note", body="private needle", classification="P2-confidential"
        )
        self.insert_document("conflict-note", body="conflict needle", conflict="confirmed")
        self.insert_document("unknown-authority", body="unknown needle", project="unknown")
        self.insert_document(
            "unknown-history",
            body="unknown history needle",
            project="unknown-history-project",
            evidence="E1",
            historical=True,
        )
        self.insert_document(
            "repo-candidate", body="repository candidate needle", project="candidate-project"
        )
        before = self.filesystem_snapshot()

        with QingtianKnowledgeProvider(self.config_path) as provider:
            approved = provider.query(self.request("needle"))
            with self.assertRaises(sqlite3.OperationalError):
                provider.db.execute(
                    "INSERT INTO metadata(key,value) VALUES('write-attempt','blocked')"
                )
            candidate = provider.query(self.request("needle", "candidate"))
            history = provider.query(self.request("needle", "history"))

        self.assertEqual(["kb:approved-note"], [r["knowledge_id"] for r in approved["results"]])
        self.assertEqual(
            {"kb:candidate-note", "kb:repo-candidate", "kb:approved-note"},
            {r["knowledge_id"] for r in candidate["results"]},
        )
        self.assertEqual(
            {"kb:history-note", "kb:unknown-history"},
            {r["knowledge_id"] for r in history["results"]},
        )
        unknown_history = next(
            result
            for result in history["results"]
            if result["knowledge_id"] == "kb:unknown-history"
        )
        self.assertEqual("history", unknown_history["authority"])
        self.assertFalse(unknown_history["authoritative"])
        self.assertFalse(unknown_history["eligible_for_generation"])
        self.assertEqual(
            "unknown",
            unknown_history["provenance"]["repository_authority_state"],
        )
        self.assertNotIn(
            "kb:unknown-authority",
            {result["knowledge_id"] for result in approved["results"]},
        )
        self.assertNotIn(
            "kb:unknown-authority",
            {result["knowledge_id"] for result in candidate["results"]},
        )
        self.assertEqual(
            "vault:90-Sources/approved-note.md",
            approved["results"][0]["provenance"]["vault_locator"],
        )
        self.assertFalse(approved["acl_enforced"])
        self.assertFalse(approved["production_integrated"])
        self.assertFalse(
            approved["results"][0]["provenance"]["canonical_ref_remote_verified"]
        )
        codes = {warning["code"] for warning in approved["metadata_warnings"]}
        self.assertIn("repository-authority-unknown", codes)
        self.assertEqual(before, self.filesystem_snapshot())
        self.assertFalse(Path(str(self.state) + "-wal").exists())
        self.assertFalse(Path(str(self.state) + "-shm").exists())

    def test_cli_reads_json_request_from_stdin_without_query_echo_or_persistence(self) -> None:
        query = "unique provider phrase"
        self.insert_document("cli-note", body=f"prefix {query} suffix")
        payload = {
            "schema_version": "1.0",
            "query": query,
            "caller_id": "cli.test",
            "purpose": "test",
            "request_id": "cli-request",
        }
        output = io.StringIO()
        before = self.filesystem_snapshot()
        original_stdin = os.sys.stdin
        try:
            os.sys.stdin = io.StringIO(json.dumps(payload))
            with redirect_stdout(output):
                status = main(["--config", str(self.config_path), "provider-query"])
        finally:
            os.sys.stdin = original_stdin
        rendered = output.getvalue()
        response = json.loads(rendered)
        self.assertEqual(0, status)
        self.assertNotIn(query, rendered)
        self.assertIn("[MATCH]", response["results"][0]["excerpt"])
        self.assertFalse(response["query_echoed"])
        self.assertFalse(response["query_persisted"])
        self.assertIn("operating-system", response["query_privacy_scope"])
        self.assertEqual(before, self.filesystem_snapshot())

    def test_cli_raw_stdin_and_sensitive_metadata_errors_do_not_echo_values(self) -> None:
        self.insert_document("raw-note")
        output = io.StringIO()
        # Assemble the synthetic PAT shape at runtime so repository scanners and
        # GitHub push protection do not mistake a test fixture for a credential.
        sensitive_caller = "github" + "_pat_" + "123456789012345678901234"
        original_stdin = os.sys.stdin
        try:
            os.sys.stdin = io.StringIO("needle\n")
            with redirect_stdout(output):
                status = main(
                    [
                        "--config",
                        str(self.config_path),
                        "provider-query",
                        "--caller-id",
                        sensitive_caller,
                        "--purpose",
                        "test",
                    ]
                )
        finally:
            os.sys.stdin = original_stdin
        self.assertEqual(2, status)
        self.assertNotIn("github_pat_", output.getvalue())

        stderr = io.StringIO()
        leaked_positional = "query-that-must-not-be-echoed"
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(
                [
                    "--config",
                    str(self.config_path),
                    "provider-query",
                    leaked_positional,
                ]
            )
        self.assertNotIn(leaked_positional, stderr.getvalue())

    def test_request_collections_and_explicit_null_follow_json_contract(self) -> None:
        self.insert_document("contract-note")
        base_payload: dict[str, object] = {
            "schema_version": "1.0",
            "query": "needle",
            "caller_id": "contract.test",
            "purpose": "test",
        }

        def invoke(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
            output = io.StringIO()
            original_stdin = os.sys.stdin
            try:
                os.sys.stdin = io.StringIO(json.dumps(payload))
                with redirect_stdout(output):
                    status = main(
                        ["--config", str(self.config_path), "provider-query"]
                    )
            finally:
                os.sys.stdin = original_stdin
            return status, json.loads(output.getvalue())

        invalid_payloads = (
            {**base_payload, "retrieval_modes": []},
            {
                **base_payload,
                "retrieval_modes": ["approved", "approved"],
            },
            {**base_payload, "projects": ["sample", "sample"]},
            {**base_payload, "request_id": None},
            {**base_payload, "request_id": ""},
            {**base_payload, "query": "   "},
            {**base_payload, "query": " " + ("x" * 4096)},
            {**base_payload, "top_k": 51},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, response = invoke(payload)
                self.assertEqual(2, status)
                self.assertEqual("error", response["status"])

        status, accepted = invoke({**base_payload, "projects": []})
        self.assertEqual(0, status)
        self.assertEqual(1, accepted["result_count"])
        self.assertIsInstance(accepted["request_id"], str)

        with QingtianKnowledgeProvider(self.config_path) as provider:
            invalid_requests = (
                ProviderRequest("needle", "contract.test", "test", modes=()),
                ProviderRequest(
                    "needle",
                    "contract.test",
                    "test",
                    modes=("approved", "approved"),
                ),
                ProviderRequest(
                    "needle",
                    "contract.test",
                    "test",
                    projects=("sample", "sample"),
                ),
                ProviderRequest(
                    "needle", "contract.test", "test", request_id=""
                ),
            )
            for request in invalid_requests:
                with self.subTest(request=request):
                    with self.assertRaises(ConfigurationError):
                        provider.query(request)

    def test_request_identifiers_are_secret_and_pii_scanned(self) -> None:
        self.insert_document("request-note")
        with QingtianKnowledgeProvider(self.config_path) as provider:
            for request in (
                ProviderRequest("needle", "ghp_" + "A" * 24, "test"),
                ProviderRequest(
                    "needle", "test.runner", "test", request_id="ghp_" + "B" * 24
                ),
                ProviderRequest(
                    "needle", "test.runner", "test", projects=("13800138000",)
                ),
            ):
                with self.subTest(request=request):
                    with self.assertRaises(ConfigurationError) as raised:
                        provider.query(request)
                    self.assertNotIn("ghp_", str(raised.exception))
                    self.assertNotIn("13800138000", str(raised.exception))

    def test_repository_authority_and_database_contract_are_strict(self) -> None:
        invalid_repositories = [
            [{"project": "sample", "root": "x", "canonical_ref": "main"}],
            [
                {
                    "project": "sample",
                    "root": "x",
                    "canonical_ref": "main",
                    "authority_state": "unknown",
                }
            ],
            [
                {
                    "project": "sample",
                    "root": "x",
                    "canonical_ref": "ghp_" + "A" * 24,
                    "authority_state": "authoritative",
                }
            ],
        ]
        for repositories in invalid_repositories:
            with self.subTest(repositories=repositories):
                self.write_config(repositories)
                with self.assertRaises(ConfigurationError):
                    QingtianKnowledgeProvider(self.config_path)

        self.write_config()
        connection = sqlite3.connect(self.state)
        connection.execute("UPDATE metadata SET value='99' WHERE key='schema_version'")
        connection.commit()
        connection.close()
        with self.assertRaises(KnowledgeError):
            QingtianKnowledgeProvider(self.config_path)

        connection = sqlite3.connect(self.state)
        connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        connection.execute("DROP TABLE sources")
        connection.execute("CREATE TABLE sources(knowledge_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        with self.assertRaises(KnowledgeError):
            QingtianKnowledgeProvider(self.config_path)

    def test_uncheckpointed_wal_is_rejected_without_auxiliary_writes(self) -> None:
        wal_path = Path(str(self.state) + "-wal")
        wal_path.write_bytes(b"uncheckpointed")
        before = self.filesystem_snapshot()
        with self.assertRaises(KnowledgeError):
            QingtianKnowledgeProvider(self.config_path)
        self.assertEqual(before, self.filesystem_snapshot())
        self.assertFalse(Path(str(self.state) + "-shm").exists())

    def test_human_approval_expiry_evidence_matrix_and_real_line_locator(self) -> None:
        approved_path = self.write_human_note(
            "human-approved",
            body="\n\ncontext\nHuman special needle appears here.\n",
        )
        self.write_human_note(
            "human-expired",
            body="Human special needle expired.\n",
            review_due_at="2020-01-01T00:00:00Z",
        )
        self.write_human_note(
            "human-incomplete",
            body="Human special needle incomplete.\n",
            reviewer=None,
        )
        self.write_human_note(
            "human-history",
            body="Human special needle history.\n",
            evidence="E1",
            knowledge_status="candidate",
            review="pending",
            reviewer=None,
            reviewed_at=None,
            review_due_at=None,
        )
        self.write_human_note(
            "human-e0",
            body="Human special needle guess.\n",
            evidence="E0",
            knowledge_status="candidate",
            review="pending",
            reviewer=None,
            reviewed_at=None,
            review_due_at=None,
        )

        with QingtianKnowledgeProvider(self.config_path) as provider:
            approved = provider.query(self.request("special needle"))
            candidate = provider.query(self.request("special needle", "candidate"))
            history = provider.query(self.request("special needle", "history"))

        self.assertEqual(["kb:human-approved"], [r["knowledge_id"] for r in approved["results"]])
        self.assertTrue(approved["results"][0]["authoritative"])
        self.assertTrue(approved["results"][0]["eligible_for_generation"])
        locator = approved["results"][0]["provenance"]["vault_locator"]
        line_number = int(locator.rsplit(":", 1)[1])
        self.assertIn("special needle", approved_path.read_text(encoding="utf-8").splitlines()[line_number - 1])
        self.assertEqual(
            {"kb:human-approved", "kb:human-expired", "kb:human-incomplete"},
            {r["knowledge_id"] for r in candidate["results"]},
        )
        self.assertTrue(all(not r["authoritative"] for r in candidate["results"]))
        self.assertEqual(["kb:human-history"], [r["knowledge_id"] for r in history["results"]])
        self.assertFalse(history["results"][0]["eligible_for_generation"])
        self.assertNotIn(
            "kb:human-e0",
            {
                result["knowledge_id"]
                for response in (approved, candidate, history)
                for result in response["results"]
            },
        )

    def test_history_only_does_not_warn_for_valid_unrequested_approved_note(self) -> None:
        self.write_human_note(
            "human-approved-current",
            body="Shared history-filter needle in current guidance.\n",
        )
        self.write_human_note(
            "human-history-only",
            body="Shared history-filter needle in historical evidence.\n",
            evidence="E1",
            knowledge_status="candidate",
            review="pending",
            reviewer=None,
            reviewed_at=None,
            review_due_at=None,
        )

        with QingtianKnowledgeProvider(self.config_path) as provider:
            response = provider.query(self.request("history-filter needle", "history"))

        self.assertEqual(
            ["kb:human-history-only"],
            [result["knowledge_id"] for result in response["results"]],
        )
        warning_codes = {
            warning["code"] for warning in response["metadata_warnings"]
        }
        self.assertNotIn("human-approval-incomplete", warning_codes)

    def test_generated_notes_outside_generated_folder_are_not_human_warnings(self) -> None:
        directory = self.vault / "04-Testing" / "Inventories"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sample.md").write_text(
            """---
schema_version: "1.0"
id: "inventory-sample"
type: "test-inventory"
title: "Generated inventory"
managed_by: "qingtian"
human_lock: false
review_status: "not-required"
privacy_classification: "P1-internal"
---

Generated inventory needle.
""",
            encoding="utf-8",
        )
        with QingtianKnowledgeProvider(self.config_path) as provider:
            response = provider.query(self.request("inventory needle", "candidate"))

        warning_codes = {item["code"] for item in response["metadata_warnings"]}
        self.assertNotIn("human-governance-incomplete", warning_codes)
        self.assertNotIn("human-governance-invalid", warning_codes)

    def test_pii_rows_are_excluded_from_approved_candidate_and_history_modes(self) -> None:
        uuid = "37787dd0-1234-5678-90ab-1234567890ab"
        self.insert_document(
            "approved-private-path",
            body="privacyfilter /" + "Users/privateaccount/projects/app",
        )
        self.insert_document(
            "candidate-role-table",
            body=(
                "privacyfilter\n| Issue | Handler | Reporter |\n"
                "| --- | --- | --- |\n| 1 | synthetic.handler | synthetic.reporter |"
            ),
            review="pending",
        )
        self.insert_document(
            "history-host-pair",
            body=f"privacyfilter Mac `BuildMac` (`{uuid}`)",
            evidence="E1",
            historical=True,
        )
        self.insert_document(
            "safe-request-uuid",
            body=f"privacyfilter request ({uuid})",
        )

        with QingtianKnowledgeProvider(self.config_path) as provider:
            approved = provider.query(self.request("privacyfilter"))
            candidate = provider.query(self.request("privacyfilter", "candidate"))
            history = provider.query(self.request("privacyfilter", "history"))

        returned = {
            result["knowledge_id"]
            for response in (approved, candidate, history)
            for result in response["results"]
        }
        self.assertNotIn("kb:approved-private-path", returned)
        self.assertNotIn("kb:candidate-role-table", returned)
        self.assertNotIn("kb:history-host-pair", returned)
        self.assertIn("kb:safe-request-uuid", returned)
        for response in (approved, candidate, history):
            warning_codes = {
                warning["code"] for warning in response["metadata_warnings"]
            }
            self.assertIn("sqlite-sensitive-or-unsafe-metadata", warning_codes)

    def test_canonical_review_due_at_validates_then_queries_as_approved(self) -> None:
        # Replace the provider-only fixture database with the complete engine
        # schema so this exercises the actual Validator -> Provider boundary.
        self.state.unlink()
        self.write_human_note(
            "canonical-approved",
            body="Canonical deadline integration needle.\n",
            review_due_at="2099-01-01T00:00:00Z",
        )

        engine = KnowledgeEngine(self.config_path)
        try:
            validation = engine.validate_vault()
        finally:
            engine.close()

        self.assertEqual("passed", validation["status"])
        self.assertEqual([], validation["errors"])
        self.assertEqual([], validation["warnings"])
        with QingtianKnowledgeProvider(self.config_path) as provider:
            response = provider.query(self.request("integration needle"))

        self.assertEqual(["kb:canonical-approved"], [
            item["knowledge_id"] for item in response["results"]
        ])
        provenance = response["results"][0]["provenance"]
        self.assertEqual("2099-01-01T00:00:00Z", provenance["review_due_at"])
        self.assertNotIn("review_due", provenance)

    def test_pending_human_validator_and_provider_share_governance_contract(self) -> None:
        self.state.unlink()
        valid = self.write_human_note(
            "pending-valid",
            body="Pending governance contract needle.\n",
            review="pending",
            knowledge_status="candidate",
            reviewer=None,
            reviewed_at=None,
            review_due_at=None,
        )
        engine = KnowledgeEngine(self.config_path)
        try:
            valid_result = engine.validate_vault()
        finally:
            engine.close()
        self.assertEqual("passed", valid_result["status"])
        with QingtianKnowledgeProvider(self.config_path) as provider:
            accepted = provider.query(
                self.request("governance contract needle", "candidate")
            )
        self.assertEqual(
            ["kb:pending-valid"],
            [item["knowledge_id"] for item in accepted["results"]],
        )
        self.assertFalse(accepted["results"][0]["eligible_for_generation"])
        valid.unlink()

        cases: dict[str, tuple[str, str]] = {
            "missing-knowledge-status": ('knowledge_status: "candidate"\n', ""),
            "missing-evidence": ('evidence_level: "E2"\n', ""),
            "missing-claim-scope": ('claim_scope: "product-fact"\n', ""),
            "invalid-id": ('id: "invalid-invalid-id"', 'id: "invalid id"'),
            "invalid-type": ('type: "product-fact"', 'type: "bad|type"'),
            "long-title": (
                'title: "Human Provider Note"',
                'title: "' + ("x" * 301) + '"',
            ),
            "invalid-reviewer": (
                'review_status: "pending"\n',
                'review_status: "pending"\nreviewer: "bad reviewer!"\n',
            ),
        }
        invalid_paths: set[str] = set()
        for label, (old, new) in cases.items():
            note_id = "invalid-" + label
            path = self.write_human_note(
                note_id,
                filename=note_id + ".md",
                body="Pending governance contract needle.\n",
                review="pending",
                knowledge_status="candidate",
                reviewer=None,
                reviewed_at=None,
                review_due_at=None,
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn(old, text)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            invalid_paths.add(path.relative_to(self.vault).as_posix())

        engine = KnowledgeEngine(self.config_path)
        try:
            invalid_result = engine.validate_vault()
        finally:
            engine.close()
        self.assertEqual("failed", invalid_result["status"])
        self.assertTrue(
            invalid_paths.issubset(
                {item["path"] for item in invalid_result["errors"]}
            )
        )
        with QingtianKnowledgeProvider(self.config_path) as provider:
            rejected = provider.query(
                self.request("governance contract needle", "candidate")
            )
        self.assertEqual([], rejected["results"])
        warning_codes = {
            item["code"] for item in rejected["metadata_warnings"]
        }
        self.assertTrue(
            {"human-governance-incomplete", "human-governance-invalid"}
            & warning_codes
        )

    def test_legacy_review_due_alias_warns_and_conflicting_alias_fails_closed(self) -> None:
        self.state.unlink()
        self.write_human_note(
            "legacy-approved",
            body="Legacy deadline compatibility needle.\n",
            review_due_at=None,
            legacy_review_due="2099-01-01T00:00:00Z",
        )
        engine = KnowledgeEngine(self.config_path)
        try:
            legacy_validation = engine.validate_vault()
        finally:
            engine.close()

        self.assertEqual("passed", legacy_validation["status"])
        self.assertEqual(
            [{
                "path": "01-Product/legacy-approved.md",
                "warning": "deprecated-approval-field:review_due",
            }],
            legacy_validation["warnings"],
        )

        self.write_human_note(
            "conflicting-deadline",
            body="Conflicting deadline compatibility needle.\n",
            review_due_at="2099-01-01T00:00:00Z",
            legacy_review_due="2098-01-01T00:00:00Z",
        )
        engine = KnowledgeEngine(self.config_path)
        try:
            conflict_validation = engine.validate_vault()
        finally:
            engine.close()
        self.assertEqual("failed", conflict_validation["status"])
        self.assertIn(
            {
                "path": "01-Product/conflicting-deadline.md",
                "error": "conflicting-approval-deadline-fields",
            },
            conflict_validation["errors"],
        )

        with QingtianKnowledgeProvider(self.config_path) as provider:
            approved = provider.query(self.request("compatibility needle"))
            candidate = provider.query(
                self.request("compatibility needle", "candidate")
            )

        self.assertEqual(["kb:legacy-approved"], [
            item["knowledge_id"] for item in approved["results"]
        ])
        self.assertNotIn(
            "kb:conflicting-deadline",
            {item["knowledge_id"] for item in candidate["results"]},
        )
        self.assertEqual(
            "2099-01-01T00:00:00Z",
            approved["results"][0]["provenance"]["review_due_at"],
        )
        warning_codes = {
            warning["code"] for warning in approved["metadata_warnings"]
        }
        self.assertIn("human-review-due-legacy-alias", warning_codes)
        self.assertIn("human-review-due-conflict", warning_codes)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_human_vault_symlink_files_and_parent_directories_are_excluded(self) -> None:
        outside = Path(self.temporary.name) / "outside.md"
        content_path = self.write_human_note("outside-template")
        outside.write_bytes(content_path.read_bytes())
        content_path.unlink()
        (self.vault / "01-Product" / "linked.md").symlink_to(outside)
        outside_directory = Path(self.temporary.name) / "outside-directory"
        outside_directory.mkdir()
        (outside_directory / "nested.md").write_bytes(outside.read_bytes())
        (self.vault / "linked-directory").symlink_to(outside_directory, target_is_directory=True)

        with QingtianKnowledgeProvider(self.config_path) as provider:
            result = provider.query(self.request("needle"))

        self.assertEqual([], result["results"])
        codes = {warning["code"] for warning in result["metadata_warnings"]}
        self.assertIn("human-vault-symlink-excluded", codes)

    def test_human_note_path_swap_after_fd_read_fails_closed(self) -> None:
        note = self.write_human_note("swapped-note")
        replacement = note.with_suffix(".replacement")
        replacement.write_bytes(note.read_bytes())
        original_stability_check = QingtianKnowledgeProvider._fd_path_stable
        normalized_note = note.resolve()
        swapped = False

        def swap_before_path_verification(path: Path, fd_stat: os.stat_result) -> bool:
            nonlocal swapped
            if path == normalized_note and not swapped:
                os.replace(replacement, note)
                swapped = True
            return original_stability_check(path, fd_stat)

        with QingtianKnowledgeProvider(self.config_path) as provider, patch.object(
            QingtianKnowledgeProvider,
            "_fd_path_stable",
            side_effect=swap_before_path_verification,
        ):
            result = provider.query(self.request("needle"))

        self.assertTrue(swapped)
        self.assertEqual([], result["results"])
        codes = {warning["code"] for warning in result["metadata_warnings"]}
        self.assertIn("human-vault-path-unstable", codes)

    def test_cross_plane_raw_id_and_hash_deduplication_fails_closed_on_conflict(self) -> None:
        hash_a = self.insert_document("shared-id", body="dedupe needle database")
        self.write_human_note(
            "shared-id",
            body="dedupe needle human conflict\n",
            source_hash="b" * 64,
            review="pending",
            knowledge_status="candidate",
            reviewer=None,
            reviewed_at=None,
            review_due_at=None,
        )
        hash_c = self.insert_document("database-copy", body="dedupe needle shared content")
        self.write_human_note(
            "human-copy",
            body="dedupe needle human mirror\n",
            source_hash=hash_c,
        )

        with QingtianKnowledgeProvider(self.config_path) as provider:
            result = provider.query(self.request("dedupe needle"))

        self.assertNotEqual(hash_a, "b" * 64)
        self.assertEqual(1, result["result_count"])
        self.assertEqual(
            "kb-sha256:" + hash_c[:32], result["results"][0]["knowledge_id"]
        )
        codes = {warning["code"] for warning in result["metadata_warnings"]}
        self.assertIn("duplicate-id-content-conflict", codes)
        self.assertIn("duplicate-content-deduplicated", codes)
        rendered = json.dumps(result)
        self.assertNotIn("shared-id", rendered)

    def test_full_query_is_scored_once_and_redacted_from_excerpt(self) -> None:
        self.assertEqual(
            ["alpha", "beta"], QingtianKnowledgeProvider._query_terms("alpha beta")
        )
        excerpt, line, score = QingtianKnowledgeProvider._excerpt_and_score(
            "alpha beta", "alpha beta", "alpha beta"
        )
        self.assertEqual(434, score)
        self.assertEqual(1, line)
        self.assertEqual("[MATCH]", excerpt)


if __name__ == "__main__":
    unittest.main()
