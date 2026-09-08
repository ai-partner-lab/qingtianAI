from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from qingtian_kb.engine import (
    EXTRACTOR_VERSION,
    PII_PATTERNS,
    RAW_PRIVACY_COMPACTION_KEY,
    SCANNER_POLICY_FINGERPRINT,
    KnowledgeEngine,
    KnowledgeIndex,
    SECRET_PATTERNS,
)
from qingtian_kb.models import ConfigurationError, KnowledgeError


class KnowledgeEngineTestCase(unittest.TestCase):
    """Black-box contract tests for the standalone Qingtian knowledge engine."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="qingtian-kb-test-")
        self.root = Path(self._temporary.name)
        # Match the production trust boundary: the knowledge package is a
        # sibling of the read-only source workspace, never nested inside it.
        self.knowledge = self.root / "knowledge"
        self.config_dir = self.knowledge / "config"
        self.workspace = self.root / "workspace"
        self.vault = self.knowledge / "vault"
        self.state = self.knowledge / "state" / "index.sqlite3"
        self.docs = self.workspace / "docs"
        self.project = self.workspace / "project"
        for path in (self.config_dir, self.docs, self.project, self.vault):
            path.mkdir(parents=True, exist_ok=True)
        (self.knowledge / ".qingtian-knowledge-root").write_text(
            "qingtian-knowledge-root-v1\n", encoding="utf-8"
        )
        # The distributable vault ships with this human-facing section.  Keep the
        # fixture minimal while matching that supported on-disk contract.
        (self.vault / "00-Home").mkdir()
        self.config_path = self.config_dir / "sources.json"
        self.write_config()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write_config(
        self,
        *,
        source_sets: list[dict[str, object]] | None = None,
        test_roots: list[dict[str, object]] | None = None,
        repositories: list[dict[str, str]] | None = None,
        schema_version: int = 1,
    ) -> None:
        if source_sets is None:
            source_sets = [
                {
                    "id": "product-docs",
                    "root": "docs",
                    "project": "sample",
                    "category": "product",
                    "classification": "P1-internal",
                    "evidence_level": "E1",
                    "claim_scope": "historical-document",
                    "includes": ["**/*.md"],
                }
            ]
        normalized_repositories = [
            {
                "canonical_ref": "main",
                "authority_state": "candidate",
                **item,
            }
            for item in (repositories or [])
        ]
        config = {
            "schema_version": schema_version,
            "workspace_root": "../../workspace",
            "vault_root": "../vault",
            "state_db": "../state/index.sqlite3",
            "max_text_bytes": 128 * 1024,
            "source_sets": source_sets,
            "repositories": normalized_repositories,
            "test_roots": test_roots or [],
            "global_excluded_parts": [
                ".git",
                ".venv",
                "node_modules",
                "dist",
                "build",
                "coverage",
                "test-results",
                "__pycache__",
            ],
        }
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def ingest(self) -> dict[str, object]:
        engine = KnowledgeEngine(self.config_path)
        try:
            return engine.ingest()
        finally:
            engine.close()

    def search(self, query: str, **filters: object) -> list[dict[str, object]]:
        filters.setdefault("review_statuses", ("pending", "approved"))
        if filters.get("include_historical"):
            filters.setdefault("freshness", ("current", "unknown"))
        engine = KnowledgeEngine(self.config_path)
        try:
            return engine.index.search(query, **filters)
        finally:
            engine.close()

    def query_one(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row:
        connection = sqlite3.connect(self.state)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(sql, parameters).fetchone()
            self.assertIsNotNone(row, f"query returned no row: {sql}")
            return row
        finally:
            connection.close()

    def source_vault_path(self) -> Path:
        row = self.query_one(
            "SELECT vault_path FROM sources WHERE source_set = 'product-docs'"
        )
        return self.vault / row["vault_path"]

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        self.assertEqual(
            result.returncode,
            0,
            f"git {' '.join(arguments)} failed: {result.stderr}",
        )
        return result

    def test_plan_is_read_only_and_reports_classification_counts(self) -> None:
        normal = self.docs / "roadmap.md"
        normal.write_text("# 产品路线图\n\n下一阶段。\n", encoding="utf-8")
        secret = "ghp_" + "A" * 32
        (self.docs / "credentials.md").write_text(
            f"# 临时记录\n\ncredential={secret}\n", encoding="utf-8"
        )
        before = hashlib.sha256(normal.read_bytes()).hexdigest()

        engine = KnowledgeEngine(self.config_path)
        try:
            plan = engine.plan()
        finally:
            engine.close()

        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["source_count"], 2)
        self.assertEqual(plan["by_project"], {"sample": 2})
        self.assertEqual(plan["by_kind"], {"text": 2})
        self.assertEqual(plan["quarantine_candidates"], 1)
        self.assertEqual(hashlib.sha256(normal.read_bytes()).hexdigest(), before)
        self.assertEqual(list(self.vault.rglob("*.md")), [])

    def test_unsupported_configuration_schema_is_rejected(self) -> None:
        self.write_config(schema_version=99)
        with self.assertRaises(ConfigurationError):
            KnowledgeEngine(self.config_path)

    def test_future_database_schema_is_rejected_before_any_write(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', '99')"
            )
            connection.execute("CREATE TABLE future_payload(value TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO future_payload(value) VALUES('must-survive-unchanged')"
            )
            connection.commit()
        finally:
            connection.close()

        before_bytes = self.state.read_bytes()
        before_stat = self.state.stat()
        before_entries = sorted(path.name for path in self.state.parent.iterdir())

        with self.assertRaises(KnowledgeError):
            KnowledgeEngine(self.config_path)

        after_stat = self.state.stat()
        self.assertEqual(before_bytes, self.state.read_bytes())
        self.assertEqual(before_entries, sorted(path.name for path in self.state.parent.iterdir()))
        self.assertEqual(before_stat.st_ino, after_stat.st_ino)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        connection = sqlite3.connect(f"file:{self.state}?mode=ro", uri=True)
        try:
            self.assertEqual(
                "99",
                connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "must-survive-unchanged",
                connection.execute("SELECT value FROM future_payload").fetchone()[0],
            )
        finally:
            connection.close()

    def test_future_schema_race_after_preflight_is_rejected_before_engine_write(self) -> None:
        engine = KnowledgeEngine(self.config_path)
        engine.close()
        real_connect = sqlite3.connect
        attacked_snapshot: list[tuple[bytes, tuple[int, int, int], list[str]]] = []
        attacked = False

        def replace_schema_after_preflight(
            database: object, *args: object, **kwargs: object
        ) -> sqlite3.Connection:
            nonlocal attacked
            if not attacked and isinstance(database, str) and "mode=rw" in database:
                attacker = real_connect(self.state)
                try:
                    attacker.execute(
                        "UPDATE metadata SET value='99' WHERE key='schema_version'"
                    )
                    attacker.execute(
                        "CREATE TABLE future_payload(value TEXT NOT NULL)"
                    )
                    attacker.execute(
                        "INSERT INTO future_payload(value) VALUES('race-must-survive')"
                    )
                    attacker.commit()
                finally:
                    attacker.close()
                attacked = True
                state = self.state.stat()
                attacked_snapshot.append(
                    (
                        self.state.read_bytes(),
                        (state.st_ino, state.st_size, state.st_mtime_ns),
                        sorted(path.name for path in self.state.parent.iterdir()),
                    )
                )
            return real_connect(database, *args, **kwargs)

        with patch(
            "qingtian_kb.engine.sqlite3.connect",
            side_effect=replace_schema_after_preflight,
        ):
            with self.assertRaisesRegex(KnowledgeError, "unsupported state database schema"):
                KnowledgeEngine(self.config_path)

        self.assertTrue(attacked)
        before_bytes, before_stat, before_entries = attacked_snapshot[0]
        after = self.state.stat()
        self.assertEqual(before_bytes, self.state.read_bytes())
        self.assertEqual(before_stat, (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertEqual(
            before_entries,
            sorted(path.name for path in self.state.parent.iterdir()),
        )
        connection = real_connect(f"file:{self.state}?mode=ro", uri=True)
        try:
            self.assertEqual(
                "99",
                connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "race-must-survive",
                connection.execute("SELECT value FROM future_payload").fetchone()[0],
            )
        finally:
            connection.close()

    def test_future_schema_race_before_sqlite_writer_lock_is_rechecked(self) -> None:
        engine = KnowledgeEngine(self.config_path)
        engine.close()
        original_assert = KnowledgeIndex._assert_connected_schema_supported
        attacked_snapshot: list[bytes] = []
        connected_checks = 0

        def mutate_after_first_connected_check(index: KnowledgeIndex) -> None:
            nonlocal connected_checks
            original_assert(index)
            connected_checks += 1
            if connected_checks == 1:
                attacker = sqlite3.connect(self.state)
                try:
                    attacker.execute(
                        "UPDATE metadata SET value='99' WHERE key='schema_version'"
                    )
                    attacker.execute(
                        "CREATE TABLE post_connected_future(value TEXT NOT NULL)"
                    )
                    attacker.execute(
                        "INSERT INTO post_connected_future(value) VALUES('locked-race')"
                    )
                    attacker.commit()
                finally:
                    attacker.close()
                attacked_snapshot.append(self.state.read_bytes())

        with patch.object(
            KnowledgeIndex,
            "_assert_connected_schema_supported",
            new=mutate_after_first_connected_check,
        ):
            with self.assertRaisesRegex(KnowledgeError, "unsupported state database schema"):
                KnowledgeEngine(self.config_path)

        self.assertEqual(connected_checks, 1)
        self.assertEqual(attacked_snapshot, [self.state.read_bytes()])
        connection = sqlite3.connect(f"file:{self.state}?mode=ro", uri=True)
        try:
            self.assertEqual(
                "99",
                connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "locked-race",
                connection.execute(
                    "SELECT value FROM post_connected_future"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_source_root_may_not_escape_the_workspace_allowlist(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "should-not-be-read.md").write_text("private", encoding="utf-8")
        self.write_config(
            source_sets=[
                {
                    "id": "escaped",
                    "root": "../outside",
                    "project": "sample",
                    "category": "product",
                    "classification": "P1-internal",
                    "evidence_level": "E1",
                    "claim_scope": "historical-document",
                    "includes": ["**/*.md"],
                }
            ]
        )

        with self.assertRaises(ConfigurationError):
            engine = KnowledgeEngine(self.config_path)
            try:
                engine.plan()
            finally:
                engine.close()

    def test_immediate_reingest_is_idempotent_and_has_unique_receipts(self) -> None:
        (self.docs / "stable.md").write_text(
            "# Stable document\n\nunchanged body\n", encoding="utf-8"
        )

        first = self.ingest()
        note = self.source_vault_path()
        first_note_hash = hashlib.sha256(note.read_bytes()).hexdigest()
        second = self.ingest()

        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["counts"].get("created"), 1)
        self.assertEqual(second["counts"].get("unchanged"), 1)
        self.assertEqual(hashlib.sha256(note.read_bytes()).hexdigest(), first_note_hash)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM sources")["n"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM documents")["n"], 1)
        self.assertEqual(
            self.query_one("SELECT COUNT(*) AS n FROM ingestion_runs")["n"], 2
        )
        receipts = list((self.vault / "99-System" / "Receipts").glob("*.md"))
        self.assertEqual(len(receipts), 2)
        catalog = (
            self.vault / "99-System" / "Generated" / "Source-Catalog.md"
        ).read_text(encoding="utf-8")
        mirror_target = note.relative_to(self.vault).with_suffix("").as_posix()
        self.assertIn("## Active source mirrors", catalog)
        self.assertIn(f"[[{mirror_target}]]", catalog)

    def test_human_modified_source_note_is_never_overwritten(self) -> None:
        source = self.docs / "edited.md"
        source.write_text("# Original\n\nfirst source version\n", encoding="utf-8")
        self.ingest()
        note = self.source_vault_path()
        human_line = "\nHUMAN CURATION MUST SURVIVE\n"
        note.write_text(note.read_text(encoding="utf-8") + human_line, encoding="utf-8")
        human_version = note.read_bytes()
        source.write_text("# Original\n\nsecond source version\n", encoding="utf-8")

        receipt = self.ingest()

        self.assertEqual(note.read_bytes(), human_version)
        self.assertEqual(receipt["counts"].get("conflicts"), 1)
        conflicts = list((self.vault / "95-Reviews" / "Conflicts").glob("*.md"))
        self.assertEqual(len(conflicts), 1)
        self.assertIn("refused to overwrite", conflicts[0].read_text(encoding="utf-8"))

    def test_human_modified_generated_inventory_is_never_overwritten(self) -> None:
        tests = self.project / "e2e"
        tests.mkdir(parents=True)
        (tests / "first.spec.ts").write_text(
            "test('first', async () => {});\n", encoding="utf-8"
        )
        self.write_config(
            test_roots=[
                {
                    "project": "sample",
                    "root": "project",
                    "includes": ["e2e/**/*.ts"],
                }
            ]
        )
        self.ingest()
        inventory = self.vault / "04-Testing" / "Inventories" / "sample.md"
        human_line = "\nHUMAN REVIEW NOTES MUST SURVIVE\n"
        inventory.write_text(
            inventory.read_text(encoding="utf-8") + human_line, encoding="utf-8"
        )
        human_version = inventory.read_bytes()
        (tests / "second.spec.ts").write_text(
            "test('second', async () => {});\n", encoding="utf-8"
        )

        self.ingest()

        self.assertEqual(inventory.read_bytes(), human_version)
        conflicts = list((self.vault / "95-Reviews" / "Conflicts").glob("*.md"))
        self.assertTrue(
            any("test-inventory-sample" in path.name for path in conflicts),
            "a generated-note edit must produce a reviewable conflict record",
        )

    def test_secret_is_quarantined_without_body_or_value_leakage(self) -> None:
        secret = "sk-" + "NeverExposeThisCredential1234567890"
        (self.docs / "unsafe.md").write_text(
            f"# Unsafe\n\nprovider_key={secret}\nUNIQUE_SECRET_CONTEXT\n",
            encoding="utf-8",
        )

        receipt = self.ingest()

        self.assertEqual(receipt["counts"].get("quarantined"), 1)
        row = self.query_one(
            "SELECT status, secret_kinds_json, vault_path, relative_path, source_path, title "
            "FROM sources WHERE status='quarantined'"
        )
        self.assertEqual(row["status"], "quarantined")
        self.assertIn("openai-key", row["secret_kinds_json"])
        self.assertTrue(row["vault_path"].startswith("95-Reviews/Quarantine/"))
        self.assertEqual(row["relative_path"], "[withheld]")
        self.assertEqual(row["source_path"], "[withheld]")
        self.assertNotIn("Unsafe", row["title"])
        vault_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in self.vault.rglob("*.md")
        )
        self.assertNotIn(secret, vault_text)
        self.assertNotIn("UNIQUE_SECRET_CONTEXT", vault_text)
        self.assertNotIn(secret.encode("utf-8"), self.state.read_bytes())
        self.assertEqual(self.search(secret), [])
        self.assertEqual(self.search("UNIQUE_SECRET_CONTEXT"), [])

    def test_chinese_search_returns_provenance_and_evidence_scope(self) -> None:
        (self.docs / "audit-events.md").write_text(
            "# 审计事件设计\n\n事件保留策略和导出策略需要分别验证。\n",
            encoding="utf-8",
        )
        self.ingest()

        results = self.search("事件保留", include_historical=True)

        self.assertGreaterEqual(len(results), 1)
        match = next(item for item in results if item["title"] == "审计事件设计")
        self.assertEqual(match["project"], "sample")
        self.assertEqual(match["evidence_level"], "E1")
        self.assertEqual(match["claim_scope"], "historical-document")
        self.assertEqual(match["review_status"], "pending")
        self.assertEqual(match["conflict_status"], "none")
        self.assertEqual(match["freshness_status"], "unknown")
        self.assertEqual(match["source_locator"], "workspace:product-docs/audit-events.md")
        self.assertIn("事件保留", match["excerpt"])

    def test_non_markdown_source_syntax_is_fenced_from_obsidian_navigation(self) -> None:
        workflow = self.docs / "diagnose.yml"
        workflow.write_text(
            "pattern: '[[:space:]]+'\n"
            "literal_link: '[[phantom]]'\n"
            "literal_fence: '```'\n",
            encoding="utf-8",
        )
        self.write_config(
            source_sets=[
                {
                    "id": "workflow-docs",
                    "root": "docs",
                    "project": "sample",
                    "category": "operations",
                    "classification": "P1-internal",
                    "evidence_level": "E1",
                    "claim_scope": "historical-workflow-definition",
                    "includes": ["**/*.yml"],
                }
            ]
        )

        self.ingest()

        row = self.query_one(
            "SELECT vault_path FROM sources WHERE source_set = 'workflow-docs'"
        )
        rendered = (self.vault / row["vault_path"]).read_text(encoding="utf-8")
        source_section = rendered.split("## Source content", 1)[1]
        self.assertIn("Rendered as inert source evidence", source_section)
        self.assertIn("````yaml\n", source_section)
        self.assertIn("pattern: '[[:space:]]+'", source_section)
        self.assertIn("literal_link: '[[phantom]]'", source_section)
        self.assertRegex(source_section, r"\n````\s*$")

    def test_markdown_source_links_are_fenced_from_obsidian_navigation(self) -> None:
        source = self.docs / "linked.md"
        source.write_text(
            "# Imported Markdown\n\n"
            "[relative](../missing/phantom.md) and [[wiki-phantom]].\n\n"
            "```text\nembedded fence\n```\n",
            encoding="utf-8",
        )

        self.ingest()

        row = self.query_one("SELECT vault_path FROM sources WHERE title = 'Imported Markdown'")
        rendered = (self.vault / row["vault_path"]).read_text(encoding="utf-8")
        source_section = rendered.split("## Source content", 1)[1]
        self.assertIn("Rendered as inert source evidence", source_section)
        self.assertIn("````markdown\n# Imported Markdown", source_section)
        self.assertIn("[relative](../missing/phantom.md)", source_section)
        self.assertIn("[[wiki-phantom]]", source_section)
        self.assertRegex(source_section, r"\n````\s*$")

    def test_deleted_source_becomes_stale_tombstone_and_leaves_default_search(self) -> None:
        source = self.docs / "retired.md"
        source.write_text(
            "# Retired behavior\n\nTOMBSTONE_SEARCH_TERM historical note\n",
            encoding="utf-8",
        )
        self.ingest()
        source_note = self.source_vault_path()
        source.unlink()

        receipt = self.ingest()

        self.assertEqual(receipt["counts"].get("missing"), 1)
        row = self.query_one(
            "SELECT status FROM sources WHERE relative_path='retired.md'"
        )
        self.assertEqual(row["status"], "missing")
        self.assertTrue(source_note.is_file(), "provenance note should remain as a tombstone")
        document = self.query_one(
            "SELECT freshness_status, source_status FROM documents WHERE title='Retired behavior'"
        )
        self.assertEqual(document["freshness_status"], "stale")
        self.assertEqual(document["source_status"], "missing")
        self.assertEqual(
            self.search("TOMBSTONE_SEARCH_TERM"), [],
            "missing historical content must not silently appear as current knowledge",
        )
        catalog = (
            self.vault / "99-System" / "Generated" / "Source-Catalog.md"
        ).read_text(encoding="utf-8")
        self.assertIn("| sample | product | missing | 1 |", catalog)

    def test_test_inventory_records_definitions_without_claiming_execution(self) -> None:
        tests = self.project / "e2e"
        tests.mkdir(parents=True)
        (tests / "login.spec.ts").write_text(
            "import { test } from '@playwright/test';\n"
            "test('login', async () => {});\n"
            "test('logout', async () => {});\n",
            encoding="utf-8",
        )
        self.write_config(
            test_roots=[
                {
                    "project": "sample",
                    "root": "project",
                    "includes": ["e2e/**/*.ts"],
                }
            ]
        )

        receipt = self.ingest()

        inventory_summary = receipt["test_inventory"]["sample"]
        self.assertEqual(inventory_summary["files"], 1)
        self.assertEqual(inventory_summary["cases_detected"], 2)
        self.assertEqual(inventory_summary["capabilities"], {"e2e": 1})
        inventory = (
            self.vault / "04-Testing" / "Inventories" / "sample.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Definition inventory, not a test result", inventory)
        self.assertIn("不证明执行、通过、覆盖有效或当前环境已验收", inventory)
        self.assertIn('claim_scope: "working-tree-test-definition-inventory"', inventory)
        self.assertIn('evidence_level: "E1"', inventory)
        capability_index = (
            self.vault / "99-System" / "Generated" / "Test-Capabilities.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "[[04-Testing/Inventories/sample\\|sample]]", capability_index
        )
        self.assertIn('freshness_status: "unknown"', inventory)
        self.assertIn("Dirty/untracked test definitions: **1**", inventory)
        self.assertIn('knowledge_status: "candidate"', inventory)
        self.assertNotIn('test_result: "passed"', inventory)
        self.assertNotIn('acceptance_status: "passed"', inventory)
        result = next(
            item
            for item in self.search("login.spec.ts", include_historical=True)
            if item["project"] == "sample"
        )
        self.assertEqual(result["evidence_level"], "E1")
        self.assertEqual(result["claim_scope"], "working-tree-test-definition-inventory")

    def test_mjs_cases_and_native_coldstart_are_classified(self) -> None:
        self.assertEqual(
            KnowledgeEngine._case_count(
                Path("scripts/parity/visual-gate.test.mjs"),
                "test('first', () => {});\nit(`second`, () => {});\n",
            ),
            2,
        )
        self.assertEqual(
            KnowledgeEngine._test_capability(
                "deploy/test/measure-native-coldstart.sh", "#!/bin/sh\n"
            ),
            "performance",
        )

    def test_p2_and_pii_sources_are_quarantined_without_path_or_title_leakage(self) -> None:
        p2_path = self.docs / "VIP-客户张三-未公开.md"
        p2_title = "张三绝密商业合作方案"
        p2_body = "P2_ONLY_BODY_SENTINEL"
        p2_path.write_text(f"# {p2_title}\n\n{p2_body}\n", encoding="utf-8")
        pii_path = self.docs / "contact-李四-13800138000.md"
        pii_title = "李四联系方式"
        pii_email = "li.si.private@example.test"
        pii_body = "PII_ONLY_BODY_SENTINEL"
        pii_path.write_text(
            f"# {pii_title}\n\n{pii_email}\n{pii_body}\n", encoding="utf-8"
        )
        self.write_config(
            source_sets=[
                {
                    "id": "confidential-docs",
                    "root": "docs",
                    "project": "sample",
                    "category": "product",
                    "classification": "P2-confidential",
                    "evidence_level": "E1",
                    "claim_scope": "historical-document",
                    "includes": ["VIP-*.md"],
                },
                {
                    "id": "pii-docs",
                    "root": "docs",
                    "project": "sample",
                    "category": "product",
                    "classification": "P1-internal",
                    "evidence_level": "E1",
                    "claim_scope": "historical-document",
                    "includes": ["contact-*.md"],
                },
            ]
        )

        receipt = self.ingest()

        self.assertEqual(receipt["counts"].get("quarantined"), 2)
        connection = sqlite3.connect(self.state)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT knowledge_id, source_path, relative_path, title, status, pii "
                "FROM sources ORDER BY knowledge_id"
            ).fetchall()
            document_count = connection.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(document_count, 0)
        for row in rows:
            self.assertEqual(row["source_path"], "[withheld]")
            self.assertEqual(row["relative_path"], "[withheld]")
            self.assertEqual(row["title"], f"Restricted source {row['knowledge_id']}")
            self.assertEqual(row["status"], "quarantined")
        self.assertIn("possible", {row["pii"] for row in rows})

        forbidden = (
            p2_path.name,
            p2_title,
            p2_body,
            pii_path.name,
            pii_title,
            pii_email,
            "13800138000",
            pii_body,
        )
        vault_payload = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in self.vault.rglob("*")
            if path.is_file() and path.suffix in {".md", ".json"}
        )
        state_payload = b"\n".join(
            path.read_bytes() for path in self.state.parent.glob("*") if path.is_file()
        )
        for value in forbidden:
            self.assertNotIn(value, vault_payload)
            self.assertNotIn(value.encode("utf-8"), state_payload)
            self.assertEqual(
                self.search(
                    value,
                    classification_ceiling="P2-confidential",
                    include_historical=True,
                ),
                [],
            )

    def test_source_becoming_restricted_removes_prior_machine_mirror(self) -> None:
        source = self.docs / "later-sensitive-value.md"
        source.write_text("# Safe title\n\nOrdinary historical content.\n", encoding="utf-8")
        first = self.ingest()
        self.assertEqual(first["counts"].get("created"), 1)
        old_mirror = self.source_vault_path()
        self.assertTrue(old_mirror.is_file())

        with patch.dict(
            SECRET_PATTERNS,
            {"synthetic-path": re.compile(r"later-sensitive-value")},
            clear=False,
        ):
            second = self.ingest()

        self.assertEqual(second["counts"].get("quarantined"), 1)
        self.assertFalse(old_mirror.exists())
        row = self.query_one(
            "SELECT knowledge_id, status, relative_path, source_path, vault_path "
            "FROM sources"
        )
        self.assertEqual(row["status"], "quarantined")
        self.assertEqual(row["relative_path"], "[withheld]")
        self.assertEqual(row["source_path"], "[withheld]")
        self.assertEqual(
            row["vault_path"], f"95-Reviews/Quarantine/{row['knowledge_id']}.md"
        )
        visible_paths = [path.relative_to(self.vault).as_posix() for path in self.vault.rglob("*")]
        self.assertFalse(any("later-sensitive-value" in value for value in visible_paths))
        self.assertEqual(self.search("Ordinary historical content", include_historical=True), [])

    def test_default_search_requires_approved_current_nonhistorical_knowledge(self) -> None:
        (self.docs / "lead.md").write_text(
            "# Historical lead\n\nDEFAULT_GATE_SENTINEL\n", encoding="utf-8"
        )
        self.ingest()
        engine = KnowledgeEngine(self.config_path)
        try:
            self.assertEqual(engine.index.search("DEFAULT_GATE_SENTINEL"), [])
            explicit = engine.index.search(
                "DEFAULT_GATE_SENTINEL",
                review_statuses=("pending",),
                freshness=("unknown",),
                include_historical=True,
            )
        finally:
            engine.close()
        self.assertEqual(len(explicit), 1)
        self.assertEqual(explicit[0]["evidence_level"], "E1")

    def test_repository_authority_metadata_is_strictly_validated(self) -> None:
        self.write_config(
            repositories=[
                {
                    "project": "sample",
                    "root": "project",
                    "canonical_ref": "origin/dev",
                    "authority_state": "conflict",
                }
            ]
        )
        with self.assertRaisesRegex(ConfigurationError, "authority_state"):
            KnowledgeEngine(self.config_path)

        self.write_config(
            repositories=[
                {
                    "project": "sample",
                    "root": "project",
                    "canonical_ref": "../secret",
                    "authority_state": "candidate",
                }
            ]
        )
        with self.assertRaisesRegex(ConfigurationError, "canonical_ref"):
            KnowledgeEngine(self.config_path)

    def test_vault_validation_requires_machine_baseline_and_receipt_checksum(self) -> None:
        (self.docs / "validated.md").write_text(
            "# Validated\n\nVALIDATOR_SENTINEL\n", encoding="utf-8"
        )
        self.ingest()
        forged = self.vault / "00-Home" / "forged-machine.md"
        forged.write_text(
            "---\n"
            'schema_version: "1.0"\n'
            'id: "forged-machine"\n'
            'type: "moc"\n'
            'title: "Forged machine note"\n'
            'managed_by: "qingtian"\n'
            'privacy_classification: "P1-internal"\n'
            "---\n"
            "<!-- qingtian:managed-source -->\n",
            encoding="utf-8",
        )
        receipt = next((self.vault / "99-System" / "Receipts").glob("*.md"))
        receipt.write_text(
            receipt.read_text(encoding="utf-8").replace(
                '"result": "passed"', '"result": "tampered"', 1
            ),
            encoding="utf-8",
        )

        engine = KnowledgeEngine(self.config_path)
        try:
            result = engine.validate_vault()
        finally:
            engine.close()

        errors = {(item["path"], item["error"]) for item in result["errors"]}
        self.assertIn(("00-Home/forged-machine.md", "managed-baseline-missing"), errors)
        self.assertIn(
            (receipt.relative_to(self.vault).as_posix(), "receipt-checksum-or-id-mismatch"),
            errors,
        )

    def test_restore_baseline_is_all_or_nothing_on_file_mismatch(self) -> None:
        (self.docs / "restore-atomic.md").write_text(
            "# Restore atomic\n\nRESTORE_ATOMIC_SENTINEL\n", encoding="utf-8"
        )
        self.ingest()
        manifest = json.loads(
            (self.vault / "99-System" / "Managed-Outputs-Manifest.json").read_text(
                encoding="utf-8"
            )
        )
        target_entry = next(
            item for item in manifest["outputs"] if item["vault_path"].endswith("restore-atomic.md")
        )
        target = self.vault / target_entry["vault_path"]
        target.write_text(target.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
        shutil.rmtree(self.state.parent)

        engine = KnowledgeEngine(self.config_path)
        try:
            restored = engine.restore_managed_baseline()
            count = engine.index.db.execute(
                "SELECT COUNT(*) FROM generated_outputs"
            ).fetchone()[0]
        finally:
            engine.close()

        self.assertEqual(restored["status"], "partial")
        self.assertEqual(restored["restored"], 0)
        self.assertEqual(count, 0)

    def test_symlinked_generated_directory_cannot_escape_vault(self) -> None:
        (self.docs / "escape.md").write_text("# Escape\n", encoding="utf-8")
        outside = self.root / "outside-generated"
        outside.mkdir()
        try:
            os.symlink(outside, self.vault / "99-System")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaisesRegex(KnowledgeError, "symlink"):
            self.ingest()
        self.assertEqual(list(outside.rglob("*")), [])

    def test_atomic_write_rejects_static_target_symlink(self) -> None:
        parent = self.vault / "99-System"
        parent.mkdir()
        outside = self.root / "outside-write.md"
        outside.write_text("OUTSIDE_WRITE_SENTINEL\n", encoding="utf-8")
        target = parent / "Receipt.md"
        try:
            os.symlink(outside, target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        engine = KnowledgeEngine(self.config_path)
        try:
            with self.assertRaisesRegex(KnowledgeError, "symlink"):
                engine._atomic_write(target, "must not escape\n")
        finally:
            engine.close()

        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "OUTSIDE_WRITE_SENTINEL\n")

    def test_atomic_write_rolls_back_concurrent_parent_replacement(self) -> None:
        parent = self.vault / "99-System"
        parent.mkdir()
        target = parent / "Receipt.md"
        target.write_text("ORIGINAL_RECEIPT\n", encoding="utf-8")
        moved_parent = self.root / "moved-write-parent"
        outside = self.root / "outside-write-directory"
        outside.mkdir()
        attack_requested = threading.Event()
        attack_finished = threading.Event()
        attack_errors: list[BaseException] = []

        def replace_parent() -> None:
            try:
                if not attack_requested.wait(5):
                    raise AssertionError("write test never reached the commit boundary")
                os.rename(parent, moved_parent)
                os.symlink(outside, parent, target_is_directory=True)
            except BaseException as exc:  # pragma: no cover - reported in the test thread
                attack_errors.append(exc)
            finally:
                attack_finished.set()

        attacker = threading.Thread(target=replace_parent, daemon=True)
        attacker.start()
        original_replace = os.replace
        armed = True

        def replace_during_attack(
            source: object,
            destination: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal armed
            if (
                armed
                and source == target.name
                and isinstance(destination, str)
                and destination.endswith(".write-stage")
                and dst_dir_fd is not None
            ):
                armed = False
                attack_requested.set()
                if not attack_finished.wait(5):
                    raise AssertionError("concurrent write parent replacement timed out")
            original_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        engine = KnowledgeEngine(self.config_path)
        try:
            with patch("qingtian_kb.engine.os.replace", side_effect=replace_during_attack):
                with self.assertRaisesRegex(KnowledgeError, "write rolled back"):
                    engine._atomic_write(target, "NEW_RECEIPT\n")
        finally:
            engine.close()
            attacker.join(timeout=5)

        self.assertFalse(attacker.is_alive())
        self.assertEqual(attack_errors, [])
        self.assertEqual(
            (moved_parent / target.name).read_text(encoding="utf-8"),
            "ORIGINAL_RECEIPT\n",
        )
        self.assertFalse((outside / target.name).exists())
        self.assertEqual(
            [item.name for item in moved_parent.iterdir() if item.name.startswith(".qingtian-")],
            [],
        )

    def test_atomic_write_preserves_concurrent_target_replacement(self) -> None:
        parent = self.vault / "99-System"
        parent.mkdir()
        target = parent / "Receipt.md"
        target.write_text("ORIGINAL_RECEIPT\n", encoding="utf-8")
        concurrent = parent / ".human-replacement"
        original_replace = os.replace
        replaced = False

        def replace_target_before_stage(
            source: object,
            destination: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal replaced
            if (
                not replaced
                and source == target.name
                and isinstance(destination, str)
                and destination.endswith(".write-stage")
            ):
                replaced = True
                concurrent.write_text("CONCURRENT_HUMAN_WRITE\n", encoding="utf-8")
                original_replace(concurrent, target)
            original_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        engine = KnowledgeEngine(self.config_path)
        try:
            with patch("qingtian_kb.engine.os.replace", side_effect=replace_target_before_stage):
                with self.assertRaisesRegex(KnowledgeError, "target changed"):
                    engine._atomic_write(target, "NEW_RECEIPT\n")
        finally:
            engine.close()

        self.assertTrue(replaced)
        self.assertEqual(target.read_text(encoding="utf-8"), "CONCURRENT_HUMAN_WRITE\n")
        self.assertEqual(
            [item.name for item in parent.iterdir() if item.name.startswith(".qingtian-")],
            [],
        )

    def test_anchored_delete_rejects_static_target_symlink(self) -> None:
        parent = self.vault / "90-Sources"
        parent.mkdir()
        outside = self.root / "outside-delete.md"
        outside.write_text("OUTSIDE_DELETE_SENTINEL\n", encoding="utf-8")
        target = parent / "mirror.md"
        try:
            os.symlink(outside, target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        link_state = os.lstat(target)

        engine = KnowledgeEngine(self.config_path)
        try:
            with self.assertRaisesRegex(KnowledgeError, "symlink|changed inode"):
                engine._anchored_vault_unlink(
                    Path("90-Sources/mirror.md"),
                    expected_identity=(link_state.st_dev, link_state.st_ino),
                )
        finally:
            engine.close()

        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "OUTSIDE_DELETE_SENTINEL\n")

    def test_anchored_delete_rejects_static_parent_symlink(self) -> None:
        outside = self.root / "outside-delete-parent"
        outside.mkdir()
        outside_target = outside / "mirror.md"
        outside_target.write_text("OUTSIDE_PARENT_SENTINEL\n", encoding="utf-8")
        target_state = os.stat(outside_target, follow_symlinks=False)
        try:
            os.symlink(outside, self.vault / "90-Sources", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        engine = KnowledgeEngine(self.config_path)
        try:
            with self.assertRaisesRegex(KnowledgeError, "symlink"):
                engine._anchored_vault_unlink(
                    Path("90-Sources/mirror.md"),
                    expected_identity=(target_state.st_dev, target_state.st_ino),
                )
        finally:
            engine.close()

        self.assertEqual(
            outside_target.read_text(encoding="utf-8"),
            "OUTSIDE_PARENT_SENTINEL\n",
        )

    def test_anchored_delete_rolls_back_concurrent_parent_replacement(self) -> None:
        parent = self.vault / "90-Sources"
        parent.mkdir()
        target = parent / "mirror.md"
        target.write_text("ORIGINAL_MIRROR\n", encoding="utf-8")
        target_state = os.stat(target, follow_symlinks=False)
        moved_parent = self.root / "moved-delete-parent"
        outside = self.root / "outside-delete-directory"
        outside.mkdir()
        attack_requested = threading.Event()
        attack_finished = threading.Event()
        attack_errors: list[BaseException] = []

        def replace_parent() -> None:
            try:
                if not attack_requested.wait(5):
                    raise AssertionError("delete test never reached the unlink boundary")
                os.rename(parent, moved_parent)
                os.symlink(outside, parent, target_is_directory=True)
            except BaseException as exc:  # pragma: no cover - reported in the test thread
                attack_errors.append(exc)
            finally:
                attack_finished.set()

        attacker = threading.Thread(target=replace_parent, daemon=True)
        attacker.start()
        original_replace = os.replace
        armed = True

        def replace_during_attack(
            source: object,
            destination: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal armed
            if (
                armed
                and source == target.name
                and isinstance(destination, str)
                and destination.endswith(".delete-stage")
                and dst_dir_fd is not None
            ):
                armed = False
                attack_requested.set()
                if not attack_finished.wait(5):
                    raise AssertionError("concurrent delete parent replacement timed out")
            original_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        engine = KnowledgeEngine(self.config_path)
        try:
            with patch("qingtian_kb.engine.os.replace", side_effect=replace_during_attack):
                with self.assertRaisesRegex(KnowledgeError, "deletion rolled back"):
                    engine._anchored_vault_unlink(
                        Path("90-Sources/mirror.md"),
                        expected_identity=(target_state.st_dev, target_state.st_ino),
                    )
        finally:
            engine.close()
            attacker.join(timeout=5)

        self.assertFalse(attacker.is_alive())
        self.assertEqual(attack_errors, [])
        self.assertEqual(
            (moved_parent / target.name).read_text(encoding="utf-8"),
            "ORIGINAL_MIRROR\n",
        )
        self.assertFalse((outside / target.name).exists())
        self.assertEqual(
            [item.name for item in moved_parent.iterdir() if item.name.startswith(".qingtian-")],
            [],
        )

    def test_anchored_delete_preserves_concurrent_target_replacement(self) -> None:
        parent = self.vault / "90-Sources"
        parent.mkdir()
        target = parent / "mirror.md"
        target.write_text("ORIGINAL_MIRROR\n", encoding="utf-8")
        target_state = os.stat(target, follow_symlinks=False)
        concurrent = parent / ".human-replacement"
        original_replace = os.replace
        replaced = False

        def replace_target_before_stage(
            source: object,
            destination: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal replaced
            if (
                not replaced
                and source == target.name
                and isinstance(destination, str)
                and destination.endswith(".delete-stage")
            ):
                replaced = True
                concurrent.write_text("CONCURRENT_HUMAN_DELETE_GUARD\n", encoding="utf-8")
                original_replace(concurrent, target)
            original_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        engine = KnowledgeEngine(self.config_path)
        try:
            with patch("qingtian_kb.engine.os.replace", side_effect=replace_target_before_stage):
                with self.assertRaisesRegex(KnowledgeError, "target changed"):
                    engine._anchored_vault_unlink(
                        Path("90-Sources/mirror.md"),
                        expected_identity=(target_state.st_dev, target_state.st_ino),
                    )
        finally:
            engine.close()

        self.assertTrue(replaced)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "CONCURRENT_HUMAN_DELETE_GUARD\n",
        )
        self.assertEqual(
            [item.name for item in parent.iterdir() if item.name.startswith(".qingtian-")],
            [],
        )

    def test_dirty_and_untracked_git_sources_are_downgraded_to_worktree_evidence(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "Qingtian Test")
        self.git("config", "user.email", "qingtian-test@example.invalid")
        source_dir = self.project / "docs"
        source_dir.mkdir()
        dirty = source_dir / "dirty.md"
        dirty.write_text("# Dirty implementation\n\ncommitted\n", encoding="utf-8")
        self.git("add", "docs/dirty.md")
        self.git("commit", "-q", "-m", "initial implementation")
        dirty.write_text("# Dirty implementation\n\nworking tree edit\n", encoding="utf-8")
        untracked = source_dir / "untracked.md"
        untracked.write_text("# Untracked proposal\n\nlocal draft\n", encoding="utf-8")
        self.write_config(
            source_sets=[
                {
                    "id": "implementation-docs",
                    "root": "project",
                    "project": "sample",
                    "category": "architecture",
                    "classification": "P1-internal",
                    "evidence_level": "E2",
                    "claim_scope": "implementation-observation",
                    "includes": ["docs/**/*.md"],
                }
            ],
            repositories=[{"project": "sample", "root": "project"}],
        )

        self.ingest()

        connection = sqlite3.connect(self.state)
        connection.row_factory = sqlite3.Row
        try:
            documents = connection.execute(
                "SELECT title, evidence_level, claim_scope, freshness_status, historical, "
                "git_state, source_revision FROM documents"
            ).fetchall()
        finally:
            connection.close()
        source_documents = {
            row["title"]: row
            for row in documents
            if row["title"] in {"Dirty implementation", "Untracked proposal"}
        }
        self.assertEqual(set(source_documents), {"Dirty implementation", "Untracked proposal"})
        for row in source_documents.values():
            self.assertEqual(row["evidence_level"], "E1")
            self.assertEqual(row["claim_scope"], "working-tree-observation")
            self.assertEqual(row["freshness_status"], "unknown")
            self.assertEqual(row["historical"], 1)
            self.assertTrue(row["source_revision"].startswith("WORKTREE:"))
        self.assertTrue(source_documents["Dirty implementation"]["git_state"].startswith("worktree:"))
        self.assertEqual(source_documents["Untracked proposal"]["git_state"], "untracked")
        self.assertEqual(self.search("working tree edit"), [])
        self.assertEqual(self.search("local draft"), [])
        self.assertEqual(
            self.search("working tree edit", include_historical=True)[0]["evidence_level"],
            "E1",
        )
        self.assertEqual(
            self.search("local draft", include_historical=True)[0]["evidence_level"],
            "E1",
        )

    def test_historical_content_is_opt_in_for_retrieval(self) -> None:
        (self.docs / "history.md").write_text(
            "# Historical decision\n\nHISTORY_OPT_IN_SENTINEL\n", encoding="utf-8"
        )
        self.ingest()

        self.assertEqual(self.search("HISTORY_OPT_IN_SENTINEL"), [])
        results = self.search("HISTORY_OPT_IN_SENTINEL", include_historical=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["historical"], 1)
        self.assertEqual(results[0]["evidence_level"], "E1")
        self.assertIn("not an instruction", results[0]["retrieval_notice"])

    def test_restore_baseline_validates_manifest_and_rebuilds_deleted_state(self) -> None:
        (self.docs / "recoverable.md").write_text(
            "# Recoverable knowledge\n\nRESTORE_BASELINE_SENTINEL\n", encoding="utf-8"
        )
        self.ingest()
        manifest_path = self.vault / "99-System" / "Managed-Outputs-Manifest.json"
        original_manifest = manifest_path.read_text(encoding="utf-8")
        original_data = json.loads(original_manifest)
        self.assertGreater(len(original_data["outputs"]), 0)

        shutil.rmtree(self.state.parent)
        tampered = json.loads(original_manifest)
        tampered["outputs"][0]["output_hash"] = "0" * 64
        manifest_path.write_text(
            json.dumps(tampered, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        engine = KnowledgeEngine(self.config_path)
        try:
            with self.assertRaisesRegex(KnowledgeError, "checksum mismatch"):
                engine.restore_managed_baseline()
            manifest_path.write_text(original_manifest, encoding="utf-8")
            restored = engine.restore_managed_baseline()
            self.assertEqual(restored["status"], "restored")
            self.assertEqual(restored["restored"], len(original_data["outputs"]))
            self.assertEqual(restored["rejected"], [])
            self.assertEqual(engine.stats()["documents"], 0)
        finally:
            engine.close()

        rebuilt = self.ingest()

        self.assertEqual(rebuilt["source_count"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM sources")["n"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM documents")["n"], 1)
        self.assertEqual(
            self.search("RESTORE_BASELINE_SENTINEL", include_historical=True)[0]["title"],
            "Recoverable knowledge",
        )

    def test_git_subjects_are_redacted_before_vault_and_index_storage(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "Qingtian Test")
        self.git("config", "user.email", "qingtian-test@example.invalid")
        (self.project / "README.md").write_text("seed\n", encoding="utf-8")
        self.git("add", "README.md")
        token = "sk-" + "CommitHistorySecretValue123456789"
        email = "release.owner@example.test"
        phone = "13900139000"
        subject = f"fix auth {token} owner {email} phone {phone}"
        self.git("commit", "-q", "-m", subject)
        self.write_config(
            source_sets=[],
            repositories=[{"project": "sample", "root": "project"}],
        )

        receipt = self.ingest()

        summary = receipt["git_history"]["sample"]
        self.assertEqual(summary["redacted_subjects"], 1)
        self.assertEqual(summary["fix_commits"], 1)
        row = self.query_one(
            "SELECT subject, is_fix FROM git_commits WHERE project='sample'"
        )
        self.assertEqual(row["is_fix"], 1)
        self.assertIn("[REDACTED:openai-key]", row["subject"])
        self.assertGreaterEqual(row["subject"].count("[REDACTED:pii]"), 2)
        git_note = (
            self.vault / "06-Releases" / "Git-History" / "sample.md"
        ).read_text(encoding="utf-8")
        self.assertIn("[REDACTED:openai-key]", git_note)
        self.assertIn("[REDACTED:pii]", git_note)
        self.assertIn("Subjects redacted for secret/PII shapes: **1**", git_note)
        git_index = (
            self.vault / "99-System" / "Generated" / "Git-History.md"
        ).read_text(encoding="utf-8")
        self.assertIn("[[06-Releases/Git-History/sample\\|sample]]", git_index)
        forbidden = (token, email, phone)
        state_payload = b"\n".join(
            path.read_bytes() for path in self.state.parent.glob("*") if path.is_file()
        )
        for value in forbidden:
            self.assertNotIn(value, git_note)
            self.assertNotIn(value.encode("utf-8"), state_payload)

    def test_fatal_ingestion_is_finalized_as_failed_not_left_running(self) -> None:
        (self.docs / "fatal.md").write_text("# Fatal path fixture\n", encoding="utf-8")
        engine = KnowledgeEngine(self.config_path)
        try:
            with patch.object(
                engine,
                "_execute_ingestion",
                side_effect=RuntimeError("synthetic fatal failure"),
            ):
                with self.assertRaisesRegex(KnowledgeError, "RuntimeError"):
                    engine.ingest()
        finally:
            engine.close()

        connection = sqlite3.connect(self.state)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT run_id, completed_at, result, receipt_json FROM ingestion_runs"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "failed")
        self.assertIsNotNone(rows[0]["completed_at"])
        failure = json.loads(rows[0]["receipt_json"])
        self.assertEqual(failure["counts"], {"fatal_errors": 1})
        self.assertEqual(failure["errors"], [{"error": "RuntimeError"}])
        self.assertNotIn("synthetic fatal failure", rows[0]["receipt_json"])
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) AS n FROM ingestion_runs WHERE result='running'"
            )["n"],
            0,
        )
        receipt_path = self.vault / "99-System" / "Receipts" / f"{rows[0]['run_id']}.md"
        self.assertTrue(receipt_path.is_file())
        self.assertNotIn("synthetic fatal failure", receipt_path.read_text(encoding="utf-8"))

    def test_vault_validation_rejects_unknown_governance_enums(self) -> None:
        note = self.vault / "00-Home" / "invalid.md"
        note.write_text(
            "---\n"
            'schema_version: "1.0"\n'
            'id: "invalid-governance-enum"\n'
            'type: "test"\n'
            'title: "Invalid governance enum"\n'
            'managed_by: "robot"\n'
            'review_status: "maybe"\n'
            'privacy_classification: "P9-unknown"\n'
            "---\n\n# Invalid governance enum\n",
            encoding="utf-8",
        )
        engine = KnowledgeEngine(self.config_path)
        try:
            result = engine.validate_vault()
        finally:
            engine.close()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            {item["error"] for item in result["errors"]},
            {
                "invalid-enum:managed_by:robot",
                "invalid-enum:review_status:maybe",
                "invalid-enum:privacy_classification:P9-unknown",
            },
        )

    def test_pdf_pipeline_change_forces_reextract_and_retires_newly_restricted_mirror(
        self,
    ) -> None:
        pdf = self.docs / "stable.pdf"
        pdf.write_bytes(b"synthetic-pdf-container-with-stable-hash")
        unchanged_source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
        self.write_config(
            source_sets=[
                {
                    "id": "pdf-docs",
                    "root": "docs",
                    "project": "sample",
                    "category": "product",
                    "classification": "P1-internal",
                    "evidence_level": "E1",
                    "claim_scope": "historical-document",
                    "includes": ["**/*.pdf"],
                }
            ]
        )

        with patch("qingtian_kb.engine.EXTRACTOR_VERSION", "test-pdf-pipeline-v1"), patch.object(
            KnowledgeEngine,
            "_extract_pdf",
            autospec=True,
            return_value=(
                "SAFE_PDF_MIRROR_SENTINEL",
                {"extractor": "synthetic-v1", "truncated": False, "pages": 1},
            ),
        ) as first_extract:
            first = self.ingest()
        self.assertEqual(first_extract.call_count, 1)
        self.assertEqual(first["counts"].get("created"), 1)
        active_mirror = self.vault / self.query_one(
            "SELECT vault_path FROM sources WHERE source_set='pdf-docs'"
        )["vault_path"]
        self.assertIn("SAFE_PDF_MIRROR_SENTINEL", active_mirror.read_text(encoding="utf-8"))
        first_transform = self.query_one(
            "SELECT extractor_version FROM sources WHERE source_set='pdf-docs'"
        )["extractor_version"]

        synthetic_secret = "sk-" + "NewScannerFindsThisSecret123456789"
        with patch("qingtian_kb.engine.EXTRACTOR_VERSION", "test-pdf-pipeline-v2"), patch.object(
            KnowledgeEngine,
            "_extract_pdf",
            autospec=True,
            return_value=(
                f"new extraction contains {synthetic_secret}",
                {"extractor": "synthetic-v2", "truncated": False, "pages": 1},
            ),
        ) as second_extract:
            second = self.ingest()

        self.assertEqual(second_extract.call_count, 1, "same-hash PDF must be re-extracted")
        self.assertEqual(hashlib.sha256(pdf.read_bytes()).hexdigest(), unchanged_source_hash)
        self.assertEqual(second["counts"].get("quarantined"), 1)
        self.assertEqual(second["counts"].get("unchanged", 0), 0)
        source = self.query_one(
            "SELECT status, extractor_version, source_path, relative_path FROM sources "
            "WHERE source_set='pdf-docs'"
        )
        self.assertEqual(source["status"], "quarantined")
        self.assertNotEqual(source["extractor_version"], first_transform)
        self.assertEqual(source["source_path"], "[withheld]")
        self.assertEqual(source["relative_path"], "[withheld]")
        self.assertEqual(
            self.query_one("SELECT COUNT(*) AS n FROM documents")["n"],
            0,
            "newly restricted extracted text must be removed from retrieval",
        )
        self.assertFalse(
            active_mirror.exists(),
            "a newly restricted mirror must be retired without retaining its sensitive filename",
        )
        vault_payload = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in self.vault.rglob("*")
            if path.is_file() and path.suffix in {".md", ".json"}
        )
        self.assertNotIn(synthetic_secret, vault_payload)
        self.assertNotIn(synthetic_secret.encode("utf-8"), self.state.read_bytes())

    def test_extracted_pdf_can_return_to_quarantine_after_resolution(self) -> None:
        pdf = self.docs / "oscillating.pdf"
        pdf.write_bytes(b"synthetic-pdf-container")
        self.write_config(
            source_sets=[
                {
                    "id": "pdf-docs",
                    "root": "docs",
                    "project": "sample",
                    "category": "product",
                    "classification": "P1-internal",
                    "evidence_level": "E1",
                    "claim_scope": "historical-document",
                    "includes": ["**/*.pdf"],
                }
            ]
        )
        synthetic_secret = "sk-" + "ExtractorToggleSecret123456789"

        with patch("qingtian_kb.engine.EXTRACTOR_VERSION", "pdf-restricted-v1"), patch.object(
            KnowledgeEngine,
            "_extract_pdf",
            autospec=True,
            return_value=(
                synthetic_secret,
                {"extractor": "synthetic-v1", "truncated": False, "pages": 1},
            ),
        ):
            first = self.ingest()
        self.assertEqual(first["counts"].get("quarantined"), 1)

        with patch("qingtian_kb.engine.EXTRACTOR_VERSION", "pdf-safe-v2"), patch.object(
            KnowledgeEngine,
            "_extract_pdf",
            autospec=True,
            return_value=(
                "safe extracted text",
                {"extractor": "synthetic-v2", "truncated": False, "pages": 1},
            ),
        ):
            second = self.ingest()
        self.assertEqual(second["counts"].get("updated"), 1)
        source_id = self.query_one("SELECT knowledge_id FROM sources")["knowledge_id"]
        self.assertEqual(
            self.query_one(
                "SELECT status FROM generated_outputs WHERE knowledge_id=?",
                ("source-quarantine-" + source_id,),
            )["status"],
            "resolved",
        )

        with patch("qingtian_kb.engine.EXTRACTOR_VERSION", "pdf-restricted-v3"), patch.object(
            KnowledgeEngine,
            "_extract_pdf",
            autospec=True,
            return_value=(
                synthetic_secret,
                {"extractor": "synthetic-v3", "truncated": False, "pages": 1},
            ),
        ):
            third = self.ingest()

        self.assertEqual(third["result"], "passed")
        self.assertEqual(third["counts"].get("quarantined"), 1)
        self.assertEqual(third["counts"].get("conflicts", 0), 0)
        source_row = self.query_one("SELECT status, source_path FROM sources")
        self.assertEqual(source_row["status"], "quarantined")
        self.assertEqual(source_row["source_path"], "[withheld]")
        self.assertEqual(
            self.query_one(
                "SELECT status FROM generated_outputs WHERE knowledge_id=?",
                ("source-quarantine-" + source_id,),
            )["status"],
            "quarantined",
        )
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) AS n FROM generated_outputs WHERE status='conflict'"
            )["n"],
            0,
        )
        self.assertNotIn(synthetic_secret.encode("utf-8"), self.state.read_bytes())

    def test_transform_fingerprint_updates_project_category_and_max_text_bytes(self) -> None:
        source = self.docs / "neutral.md"
        source.write_text(
            "# Transformable\n\nPREFIX-AAAA-MIDDLE-BBBB-TAIL_TRANSFORM_SENTINEL\n",
            encoding="utf-8",
        )
        configuration = json.loads(self.config_path.read_text(encoding="utf-8"))
        configuration["max_text_bytes"] = 24
        configuration["source_sets"][0]["project"] = "project-a"
        configuration["source_sets"][0]["category"] = "product"
        self.config_path.write_text(
            json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.ingest()
        fingerprints = [
            self.query_one("SELECT extractor_version FROM sources")["extractor_version"]
        ]

        configuration["source_sets"][0]["category"] = "architecture"
        self.config_path.write_text(
            json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        category_receipt = self.ingest()
        fingerprints.append(
            self.query_one("SELECT extractor_version FROM sources")["extractor_version"]
        )
        self.assertEqual(category_receipt["counts"].get("updated"), 1)
        self.assertEqual(category_receipt["counts"].get("unchanged", 0), 0)
        self.assertEqual(self.query_one("SELECT category FROM documents")["category"], "architecture")
        self.assertIn('category: "architecture"', self.source_vault_path().read_text(encoding="utf-8"))

        configuration["source_sets"][0]["project"] = "project-b"
        self.config_path.write_text(
            json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        project_receipt = self.ingest()
        fingerprints.append(
            self.query_one("SELECT extractor_version FROM sources")["extractor_version"]
        )
        self.assertEqual(project_receipt["counts"].get("updated"), 1)
        self.assertEqual(project_receipt["counts"].get("unchanged", 0), 0)
        self.assertEqual(self.query_one("SELECT project FROM documents")["project"], "project-b")
        self.assertIn('project: "project-b"', self.source_vault_path().read_text(encoding="utf-8"))

        configuration["max_text_bytes"] = 4096
        self.config_path.write_text(
            json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        max_bytes_receipt = self.ingest()
        fingerprints.append(
            self.query_one("SELECT extractor_version FROM sources")["extractor_version"]
        )
        self.assertEqual(max_bytes_receipt["counts"].get("updated"), 1)
        self.assertEqual(max_bytes_receipt["counts"].get("unchanged", 0), 0)
        self.assertIn(
            "TAIL_TRANSFORM_SENTINEL",
            self.query_one("SELECT body FROM documents")["body"],
        )
        self.assertEqual(len(set(fingerprints)), 4)

    def test_unavailable_test_root_and_git_snapshot_stale_existing_documents(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "Qingtian Test")
        self.git("config", "user.email", "qingtian-test@example.invalid")
        tests = self.project / "e2e"
        tests.mkdir()
        (tests / "stable.spec.ts").write_text(
            "test('stable', async () => {});\n", encoding="utf-8"
        )
        self.git("add", "e2e/stable.spec.ts")
        self.git("commit", "-q", "-m", "add stable test definition")
        self.write_config(
            source_sets=[],
            repositories=[{"project": "sample", "root": "project"}],
            test_roots=[
                {
                    "project": "sample",
                    "root": "project",
                    "includes": ["e2e/**/*.ts"],
                }
            ],
        )
        first = self.ingest()
        self.assertEqual(first["result"], "passed")
        inventory_before = self.query_one(
            "SELECT evidence_level, freshness_status, source_status FROM documents "
            "WHERE knowledge_id='test-inventory-sample'"
        )
        self.assertEqual(tuple(inventory_before), ("E2", "current", "active"))

        self.project.rename(self.workspace / "project-offline")
        second = self.ingest()

        self.assertEqual(second["result"], "partial")
        self.assertEqual(second["test_inventory"]["sample"]["status"], "unavailable")
        self.assertEqual(second["git_history"]["sample"]["status"], "unavailable")
        self.assertEqual(second["counts"].get("generated_unavailable"), 2)
        for knowledge_id in ("test-inventory-sample", "git-history-sample"):
            row = self.query_one(
                "SELECT source_status, freshness_status FROM documents WHERE knowledge_id=?",
                (knowledge_id,),
            )
            self.assertEqual(row["source_status"], "unavailable")
            self.assertEqual(row["freshness_status"], "stale")

    def test_empty_test_root_without_repository_is_unavailable_not_e2_current(self) -> None:
        self.write_config(
            source_sets=[],
            test_roots=[
                {
                    "project": "sample",
                    "root": "project",
                    "includes": ["e2e/**/*.ts"],
                }
            ],
        )

        receipt = self.ingest()

        self.assertEqual(receipt["result"], "partial")
        self.assertEqual(receipt["test_inventory"]["sample"]["status"], "unavailable")
        self.assertEqual(receipt["counts"].get("generated_unavailable"), 1)
        row = self.query_one(
            "SELECT evidence_level, claim_scope, freshness_status, git_state, source_status "
            "FROM documents WHERE knowledge_id='test-inventory-sample'"
        )
        self.assertEqual(row["evidence_level"], "E1")
        self.assertEqual(row["claim_scope"], "unverified-test-definition-inventory")
        self.assertEqual(row["freshness_status"], "unknown")
        self.assertEqual(row["git_state"], "verification-unavailable")
        self.assertEqual(row["source_status"], "unavailable")
        inventory = (
            self.vault / "04-Testing" / "Inventories" / "sample.md"
        ).read_text(encoding="utf-8")
        self.assertIn('evidence_level: "E1"', inventory)
        self.assertIn('freshness_status: "unknown"', inventory)
        self.assertIn("Git verification unavailable: **True**", inventory)

    def test_git_snapshot_change_during_scan_keeps_prior_snapshot_stale(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "Qingtian Test")
        self.git("config", "user.email", "qingtian-test@example.invalid")
        tracked = self.project / "README.md"
        tracked.write_text("first\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "first snapshot")
        self.write_config(
            source_sets=[],
            repositories=[{"project": "sample", "root": "project"}],
        )
        self.ingest()
        prior_note = self.vault / "06-Releases" / "Git-History" / "sample.md"
        prior_note_bytes = prior_note.read_bytes()
        self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM git_commits")["n"], 1)

        tracked.write_text("second\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "second snapshot must not leak")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        before = {
            "head": head,
            "branch": "main",
            "dirty_count": 0,
            "ref_count": 1,
            "fingerprint": "snapshot-before",
        }
        after = {**before, "dirty_count": 1, "fingerprint": "snapshot-after"}
        engine = KnowledgeEngine(self.config_path)
        try:
            with patch.object(engine, "_git_ref_snapshot", side_effect=[before, after]):
                receipt = engine.ingest()
        finally:
            engine.close()

        self.assertEqual(receipt["result"], "partial")
        self.assertEqual(receipt["git_history"]["sample"]["status"], "unstable")
        self.assertEqual(receipt["counts"].get("generated_unstable"), 1)
        stale_note = prior_note.read_text(encoding="utf-8")
        self.assertNotEqual(prior_note.read_bytes(), prior_note_bytes)
        self.assertIn('type: "snapshot-status"', stale_note)
        self.assertIn('freshness_status: "stale"', stale_note)
        self.assertNotIn("second snapshot must not leak", stale_note)
        self.assertEqual(
            self.query_one("SELECT COUNT(*) AS n FROM git_commits")["n"],
            0,
            "neither unstable rows nor a stale prior snapshot may remain in active aggregates",
        )
        document = self.query_one(
            "SELECT source_status, freshness_status FROM documents "
            "WHERE knowledge_id='git-history-sample'"
        )
        self.assertEqual(document["source_status"], "unstable")
        self.assertEqual(document["freshness_status"], "stale")

    def test_symlinked_state_directory_is_rejected_without_external_write(self) -> None:
        external = self.root / "external-state-target"
        external.mkdir()
        try:
            os.symlink(external, self.state.parent)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaises(ConfigurationError):
            KnowledgeEngine(self.config_path)

        self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_state_database_is_rejected_without_touching_target(self) -> None:
        self.state.parent.mkdir(mode=0o700)
        external = self.root / "external-database-target"
        sentinel = b"EXTERNAL_DATABASE_MUST_NOT_CHANGE"
        external.write_bytes(sentinel)
        try:
            os.symlink(external, self.state)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaises(KnowledgeError):
            KnowledgeEngine(self.config_path)

        self.assertEqual(external.read_bytes(), sentinel)

    def test_state_parent_replacement_during_sqlite_open_cannot_write_external_db(self) -> None:
        external = self.root / "external-state-race-target"
        external.mkdir()
        external_database = external / self.state.name
        connection = sqlite3.connect(external_database)
        try:
            connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel(value) VALUES('unchanged')")
            connection.commit()
        finally:
            connection.close()
        external_before = external_database.read_bytes()
        detached = self.knowledge / "detached-state-race"
        real_connect = sqlite3.connect
        replaced = False

        def replace_parent(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
            nonlocal replaced
            if not replaced and isinstance(database, str) and "mode=rw" in database:
                self.state.parent.rename(detached)
                os.symlink(external, self.state.parent)
                replaced = True
            return real_connect(database, *args, **kwargs)

        try:
            with patch("qingtian_kb.engine.sqlite3.connect", side_effect=replace_parent):
                with self.assertRaisesRegex(KnowledgeError, "unverified|path changed"):
                    KnowledgeEngine(self.config_path)
        finally:
            if self.state.parent.is_symlink():
                self.state.parent.unlink()
            if detached.exists():
                detached.rename(self.state.parent)

        self.assertTrue(replaced)
        self.assertEqual(external_database.read_bytes(), external_before)

    def test_ingest_lock_parent_replacement_uses_pinned_directory(self) -> None:
        engine = KnowledgeEngine(self.config_path)
        external = self.root / "external-lock-race-target"
        external.mkdir()
        detached = self.knowledge / "detached-lock-race"
        real_open = os.open
        replaced = False

        def replace_parent(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal replaced
            if not replaced and path == "ingest.lock":
                self.state.parent.rename(detached)
                os.symlink(external, self.state.parent)
                replaced = True
            return real_open(path, flags, *args, **kwargs)

        try:
            with patch("qingtian_kb.engine.os.open", side_effect=replace_parent):
                with self.assertRaises(KnowledgeError):
                    engine.ingest()
        finally:
            engine.close()
            if self.state.parent.is_symlink():
                self.state.parent.unlink()
            if detached.exists():
                detached.rename(self.state.parent)

        self.assertTrue(replaced)
        self.assertEqual(list(external.iterdir()), [])

    def test_replaced_ingest_lock_namespace_cannot_split_vault_lock(self) -> None:
        first = KnowledgeEngine(self.config_path)
        second = KnowledgeEngine(self.config_path)
        lock_path = self.state.parent / "ingest.lock"
        displaced = self.state.parent / "ingest.lock.displaced"
        try:
            with first._exclusive_ingest_lock():
                lock_path.rename(displaced)
                lock_path.write_bytes(b"replacement-lock-namespace\n")
                before_runs = first.index.db.execute(
                    "SELECT COUNT(*) FROM ingestion_runs"
                ).fetchone()[0]
                with self.assertRaisesRegex(KnowledgeError, "running for this Vault"):
                    second.ingest()
                after_runs = first.index.db.execute(
                    "SELECT COUNT(*) FROM ingestion_runs"
                ).fetchone()[0]
                self.assertEqual(before_runs, after_runs)
                self.assertEqual(
                    lock_path.read_bytes(), b"replacement-lock-namespace\n"
                )
        finally:
            first.close()
            second.close()
            if lock_path.exists():
                lock_path.unlink()
            if displaced.exists():
                displaced.rename(lock_path)

    def test_symlinked_ingest_lock_is_rejected_without_touching_target(self) -> None:
        engine = KnowledgeEngine(self.config_path)
        external_lock = self.root / "external-lock-target"
        sentinel = b"EXTERNAL_LOCK_MUST_NOT_CHANGE"
        external_lock.write_bytes(sentinel)
        lock_path = self.state.parent / "ingest.lock"
        try:
            os.symlink(external_lock, lock_path)
        except (OSError, NotImplementedError) as exc:
            engine.close()
            self.skipTest(f"symlinks unavailable: {exc}")
        try:
            with self.assertRaisesRegex(KnowledgeError, "lock is unsafe or unavailable"):
                engine.ingest()
        finally:
            engine.close()

        self.assertEqual(external_lock.read_bytes(), sentinel)

    def test_excluded_and_symlinked_sources_are_not_discovered_or_ingested(self) -> None:
        (self.docs / "visible.md").write_text(
            "# Visible\n\nALLOWED_CONTENT\n", encoding="utf-8"
        )
        excluded = self.docs / "node_modules"
        excluded.mkdir()
        (excluded / "hidden.md").write_text(
            "# Hidden\n\nEXCLUDED_CONTENT\n", encoding="utf-8"
        )
        outside_file = self.root / "outside-file.md"
        outside_file.write_text("# Outside\n\nSYMLINK_FILE_CONTENT\n", encoding="utf-8")
        outside_dir = self.root / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "stolen.md").write_text(
            "# Stolen\n\nSYMLINK_DIRECTORY_CONTENT\n", encoding="utf-8"
        )
        try:
            os.symlink(outside_file, self.docs / "outside-link.md")
            os.symlink(self.docs / "visible.md", self.docs / "inside-link.md")
            os.symlink(outside_dir, self.docs / "linked-directory")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        engine = KnowledgeEngine(self.config_path)
        try:
            records = engine.discover_sources()
        finally:
            engine.close()
        self.assertEqual([record.relative_path for record in records], ["visible.md"])

        receipt = self.ingest()

        self.assertEqual(receipt["source_count"], 1)
        self.assertEqual(
            self.search("ALLOWED_CONTENT", include_historical=True)[0]["title"],
            "Visible",
        )
        self.assertEqual(self.search("EXCLUDED_CONTENT"), [])
        self.assertEqual(self.search("SYMLINK_FILE_CONTENT"), [])
        self.assertEqual(self.search("SYMLINK_DIRECTORY_CONTENT"), [])

    def test_phone_pattern_does_not_match_inside_hex_identifier(self) -> None:
        (self.docs / "hash.md").write_text(
            "# Hash\n\nabcdef13900139000abcdef\n", encoding="utf-8"
        )
        (self.docs / "phone.md").write_text(
            "# Phone\n\nphone=13900139000\n", encoding="utf-8"
        )

        receipt = self.ingest()

        self.assertEqual(receipt["counts"].get("quarantined"), 1)
        self.assertEqual(
            self.query_one(
                "SELECT status FROM sources WHERE relative_path='hash.md'"
            )["status"],
            "active",
        )
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) AS n FROM sources WHERE status='quarantined'"
            )["n"],
            1,
        )

    def test_high_confidence_machine_and_role_pii_patterns_avoid_known_false_positives(
        self,
    ) -> None:
        positive = {
            "macos-user-path": [
                "/" + "Users/alice/Projects/app",
                "file:///" + "Users/build.bot/archive.txt",
            ],
            "role-linked-handle": [
                "Handler: @release.owner",
                "处理人: synthetic.assignee",
                "负责人: 样例甲",
            ],
            "workflow-linked-handle": [
                "待验证/synthetic.assignee",
                "交回 synthetic.reviewer",
                "创建人/处理人均为 synthetic.assignee",
            ],
            "role-handle-table": [
                "| ID | Handler | Reporter |\n| --- | --- | --- |\n| 1 | synthetic.handler | synthetic.reporter |",
                "| 工单 | 处理人 | 提出人 |\n| --- | --- | --- |\n| 1 | 样例甲 | 样例乙 |",
            ],
            "hostname-uuid-pair": [
                "hostname: build-mac UUID: 37787dd0-1234-5678-90ab-1234567890ab",
                "Mac `SyntheticBuildHost` (`37787dd0-1234-5678-90ab-1234567890ab`)",
            ],
        }
        for pattern_name, samples in positive.items():
            for sample in samples:
                with self.subTest(pattern=pattern_name, sample=sample):
                    self.assertIsNotNone(PII_PATTERNS[pattern_name].search(sample))

        negative = [
            "/" + "Users/Shared/cache",
            "/" + "Users/<account>/repo",
            "/" + "Users/{account}/repo",
            "/" + "Users/$USER/repo",
            "/" + "Users/username/repo",
            "request (37787dd0-1234-5678-90ab-1234567890ab)",
            "job_id: 37787dd0-1234-5678-90ab-1234567890ab",
            "Architecture owner: pending",
            "负责人: product",
            "待验证需求保留",
            "待验证范围",
            "提交回 API",
            "交回 API",
            "| Role | Owner | Responsibility |\n| --- | --- | --- |\n| QA | team | test |",
            "synthetic.assignee",
        ]
        for sample in negative:
            with self.subTest(sample=sample):
                self.assertFalse(
                    any(pattern.search(sample) for pattern in PII_PATTERNS.values())
                )

    def test_scanner_fingerprint_covers_every_pii_rule(self) -> None:
        expected = hashlib.sha256(
            json.dumps(
                {
                    "secrets": {
                        name: [regex.pattern, regex.flags]
                        for name, regex in sorted(SECRET_PATTERNS.items())
                    },
                    "pii": {
                        name: [regex.pattern, regex.flags]
                        for name, regex in sorted(PII_PATTERNS.items())
                    },
                },
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        self.assertEqual(expected, SCANNER_POLICY_FINGERPRINT)
        self.assertIn(";scanner/" + expected, EXTRACTOR_VERSION)
        self.assertTrue(
            {
                "macos-user-path",
                "role-linked-handle",
                "workflow-linked-handle",
                "role-handle-table",
                "hostname-uuid-pair",
            }.issubset(PII_PATTERNS)
        )

    def test_operational_absolute_source_path_is_not_scanned_or_persisted(self) -> None:
        (self.docs / "safe.md").write_text(
            "# Safe\n\nABSOLUTE_SCAN_CONTROL\n", encoding="utf-8"
        )
        with patch.dict(
            PII_PATTERNS,
            {"synthetic-operational-path": re.compile(re.escape(str(self.docs)))},
            clear=True,
        ):
            receipt = self.ingest()

        self.assertEqual(receipt["counts"].get("created"), 1)
        row = self.query_one(
            "SELECT source_path, relative_path, status FROM sources"
        )
        self.assertEqual(row["source_path"], "[workspace]/product-docs/safe.md")
        self.assertEqual(row["relative_path"], "safe.md")
        self.assertEqual(row["status"], "active")
        state_payload = b"\n".join(
            path.read_bytes() for path in self.state.parent.glob("*") if path.is_file()
        )
        self.assertNotIn(str(self.root).encode("utf-8"), state_payload)

    def test_legacy_active_locator_is_scrubbed_before_source_discovery_failure(self) -> None:
        (self.docs / "legacy.md").write_text("# Legacy\n\nSafe body.\n", encoding="utf-8")
        self.ingest()
        leaked = "/" + "Users/privateaccount/projects/legacy.md"
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("UPDATE sources SET source_path=?", (leaked,))
            connection.commit()
        finally:
            connection.close()

        engine = KnowledgeEngine(self.config_path)
        try:
            with patch.object(
                engine, "discover_sources", side_effect=RuntimeError("stop-after-scrub")
            ):
                with self.assertRaisesRegex(RuntimeError, "stop-after-scrub"):
                    engine.ingest()
        finally:
            engine.close()

        row = self.query_one("SELECT source_path FROM sources")
        self.assertEqual(row["source_path"], "[workspace]/product-docs/legacy.md")
        self.assertNotIn(leaked.encode("utf-8"), self.state.read_bytes())

    def test_legacy_free_page_pii_is_compacted_before_source_discovery_failure(self) -> None:
        (self.docs / "safe.md").write_text("# Safe\n", encoding="utf-8")
        self.ingest()
        leaked = "/" + "Users/privateaccount/projects/free-page.md"
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("PRAGMA secure_delete = OFF")
            connection.execute(
                "DELETE FROM metadata WHERE key=?", (RAW_PRIVACY_COMPACTION_KEY,)
            )
            connection.execute("CREATE TABLE legacy_free_page(value TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO legacy_free_page(value) VALUES(?)",
                [(leaked + ("X" * 4000),) for _ in range(32)],
            )
            connection.commit()
            connection.execute("DROP TABLE legacy_free_page")
            connection.commit()
        finally:
            connection.close()
        self.assertIn(leaked.encode("utf-8"), self.state.read_bytes())

        engine = KnowledgeEngine(self.config_path)
        try:
            with patch.object(
                engine, "discover_sources", side_effect=RuntimeError("stop-after-compact")
            ):
                with self.assertRaisesRegex(RuntimeError, "stop-after-compact"):
                    engine.ingest()
        finally:
            engine.close()

        payload = self.state.read_bytes()
        self.assertNotIn(leaked.encode("utf-8"), payload)
        connection = sqlite3.connect(self.state)
        try:
            marker = connection.execute(
                "SELECT value FROM metadata WHERE key=?",
                (RAW_PRIVACY_COMPACTION_KEY,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(marker, ("complete",))

    def test_legacy_locator_scrub_is_atomic_when_one_row_conflicts(self) -> None:
        (self.docs / "one.md").write_text("# One\n", encoding="utf-8")
        (self.docs / "two.md").write_text("# Two\n", encoding="utf-8")
        self.ingest()
        account_one_path = "/" + "Users/accountone/repo/one.md"
        account_two_path = "/" + "Users/accounttwo/repo/two.md"
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "UPDATE sources SET source_path=? WHERE relative_path='one.md'",
                (account_one_path,),
            )
            connection.execute(
                "UPDATE sources SET source_path=? WHERE relative_path='two.md'",
                (account_two_path,),
            )
            connection.execute(
                "CREATE TRIGGER block_second_scrub BEFORE UPDATE OF source_path ON sources "
                "WHEN OLD.relative_path='two.md' BEGIN "
                "SELECT RAISE(ABORT, 'blocked'); END"
            )
            connection.commit()
        finally:
            connection.close()

        engine = KnowledgeEngine(self.config_path)
        try:
            with self.assertRaisesRegex(KnowledgeError, "locator scrub failed"):
                engine.ingest()
        finally:
            engine.close()

        connection = sqlite3.connect(self.state)
        try:
            paths = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT relative_path, source_path FROM sources"
                ).fetchall()
            }
            run_count = connection.execute(
                "SELECT COUNT(*) FROM ingestion_runs"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(paths["one.md"], account_one_path)
        self.assertEqual(paths["two.md"], account_two_path)
        self.assertEqual(run_count, 1)

    def test_restricted_transition_conflict_scrubs_state_and_search_planes(self) -> None:
        source = self.docs / "transition.md"
        source.write_text("# Transition\n\nPrior safe body.\n", encoding="utf-8")
        self.ingest()
        mirror = self.source_vault_path()
        mirror.write_text(
            mirror.read_text(encoding="utf-8") + "\nHuman edit retained.\n",
            encoding="utf-8",
        )
        leaked = "/" + "Users/privateaccount/projects/transition.md"
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("UPDATE sources SET source_path=?", (leaked,))
            connection.commit()
        finally:
            connection.close()
        source.write_text(
            "# Transition\n\n待验证/synthetic.assignee\nPRIVATE_TRANSITION_NEEDLE\n",
            encoding="utf-8",
        )

        receipt = self.ingest()

        self.assertEqual(receipt["result"], "partial")
        self.assertEqual(receipt["counts"].get("conflicts"), 1)
        row = self.query_one(
            "SELECT status, source_path, relative_path, title FROM sources"
        )
        self.assertEqual(row["status"], "quarantined-conflict")
        self.assertEqual(row["source_path"], "[withheld]")
        self.assertEqual(row["relative_path"], "[withheld]")
        self.assertTrue(row["title"].startswith("Restricted source src-"))
        self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM documents")["n"], 0)
        connection = sqlite3.connect(self.state)
        try:
            has_fts = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_fts'"
            ).fetchone()
            fts_count = (
                connection.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
                if has_fts
                else 0
            )
        finally:
            connection.close()
        self.assertEqual(fts_count, 0)
        self.assertEqual(
            self.search("PRIVATE_TRANSITION_NEEDLE", include_historical=True), []
        )
        state_payload = b"\n".join(
            path.read_bytes() for path in self.state.parent.glob("*") if path.is_file()
        )
        for value in (leaked, "synthetic.assignee", "PRIVATE_TRANSITION_NEEDLE"):
            self.assertNotIn(value.encode("utf-8"), state_payload)

    def test_validation_fails_closed_on_source_database_and_fts_pii(self) -> None:
        (self.docs / "validation.md").write_text(
            "# Validation\n\nSafe searchable body.\n", encoding="utf-8"
        )
        self.ingest()
        leak = "/" + "Users/privateaccount/private/DB_PRIVACY_NEEDLE"
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("UPDATE sources SET source_path=?", (leak,))
            connection.execute("UPDATE documents SET body=?", (leak,))
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_fts'"
            ).fetchone():
                connection.execute("UPDATE documents_fts SET body=?", (leak,))
            connection.commit()
        finally:
            connection.close()

        engine = KnowledgeEngine(self.config_path)
        try:
            result = engine.validate_vault()
        finally:
            engine.close()

        errors = {(item["path"], item["error"]) for item in result["errors"]}
        self.assertEqual(result["status"], "failed")
        self.assertIn(("state:sources", "active-source-path-not-portable"), errors)
        self.assertIn(("state:sources", "sensitive-source-metadata"), errors)
        self.assertIn(("state:documents", "sensitive-index-content"), errors)
        if any(path == "state:documents_fts" for path, _error in errors):
            self.assertIn(("state:documents_fts", "sensitive-fts-content"), errors)

    def test_validate_rejects_unknown_non_markdown_secret_and_deleted_baseline(self) -> None:
        (self.docs / "managed.md").write_text("# Managed\n\nbody\n", encoding="utf-8")
        self.ingest()
        managed = self.source_vault_path()
        managed.unlink()
        secret = "sk-" + "A" * 32
        (self.vault / "unexpected.bin").write_bytes(secret.encode("ascii"))

        engine = KnowledgeEngine(self.config_path)
        try:
            result = engine.validate_vault()
        finally:
            engine.close()

        errors = {(item["path"], item["error"]) for item in result["errors"]}
        self.assertIn(("unexpected.bin", "unsupported-vault-file-type"), errors)
        self.assertIn(("unexpected.bin", "secret-pattern:openai-key"), errors)
        self.assertIn(
            (managed.relative_to(self.vault).as_posix(), "managed-output-missing-or-unsafe"),
            errors,
        )

    def test_restore_rejects_duplicate_manifest_identity_atomically(self) -> None:
        (self.docs / "restore.md").write_text("# Restore\n\nbody\n", encoding="utf-8")
        self.ingest()
        manifest_path = self.vault / "99-System" / "Managed-Outputs-Manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["outputs"].append(dict(manifest["outputs"][0]))
        manifest["manifest_sha256"] = hashlib.sha256(
            json.dumps(
                manifest["outputs"],
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(self.state.parent)

        engine = KnowledgeEngine(self.config_path)
        try:
            result = engine.restore_managed_baseline()
            count = engine.index.db.execute(
                "SELECT COUNT(*) FROM generated_outputs"
            ).fetchone()[0]
        finally:
            engine.close()

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["restored"], 0)
        self.assertEqual(count, 0)
        self.assertIn("duplicate-id", {item["reason"] for item in result["rejected"]})

    def test_config_change_during_discovery_fails_before_run_start(self) -> None:
        (self.docs / "stable.md").write_text("# Stable\n", encoding="utf-8")
        engine = KnowledgeEngine(self.config_path)
        original = engine.discover_sources

        def mutate_config() -> list[object]:
            records = original()
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            data["max_text_bytes"] += 1
            self.config_path.write_text(json.dumps(data), encoding="utf-8")
            return records

        try:
            with patch.object(engine, "discover_sources", side_effect=mutate_config):
                with self.assertRaisesRegex(ConfigurationError, "changed during"):
                    engine.ingest()
            runs = engine.index.db.execute(
                "SELECT COUNT(*) FROM ingestion_runs"
            ).fetchone()[0]
        finally:
            engine.close()

        self.assertEqual(runs, 0)

    def test_oversized_source_is_rejected_before_content_enters_vault(self) -> None:
        configuration = json.loads(self.config_path.read_text(encoding="utf-8"))
        configuration["max_text_bytes"] = 16
        configuration["max_source_bytes"] = 32
        self.config_path.write_text(json.dumps(configuration), encoding="utf-8")
        (self.docs / "large.md").write_text("# Large\n" + "X" * 64, encoding="utf-8")

        engine = KnowledgeEngine(self.config_path)
        try:
            with self.assertRaisesRegex(KnowledgeError, "max_source_bytes"):
                engine.ingest()
        finally:
            engine.close()

        self.assertEqual(list((self.vault / "90-Sources").rglob("*.md")), [])

    def test_safe_reclassification_resolves_obsolete_quarantine_claim(self) -> None:
        source = self.docs / "profile.md"
        source.write_text("# Profile\n\nphone=13900139000\n", encoding="utf-8")
        first = self.ingest()
        self.assertEqual(first["counts"].get("quarantined"), 1)
        source.write_text("# Profile\n\npublic biography\n", encoding="utf-8")

        second = self.ingest()

        self.assertEqual(second["counts"].get("updated"), 1)
        source_row = self.query_one("SELECT knowledge_id, status FROM sources")
        self.assertEqual(source_row["status"], "active")
        quarantine = (
            self.vault / "95-Reviews" / "Quarantine" / f"{source_row['knowledge_id']}.md"
        ).read_text(encoding="utf-8")
        self.assertIn('type: "quarantine-resolution"', quarantine)
        self.assertIn('conflict_status: "resolved"', quarantine)
        self.assertNotIn("13900139000", quarantine)
        generated = self.query_one(
            "SELECT status FROM generated_outputs WHERE knowledge_id=?",
            ("source-quarantine-" + source_row["knowledge_id"],),
        )
        self.assertEqual(generated["status"], "resolved")

    def test_missing_source_newly_classified_p2_retires_old_mirror(self) -> None:
        source = self.docs / "retired.md"
        source.write_text("# Retired\n\nold safe mirror\n", encoding="utf-8")
        self.ingest()
        mirror = self.source_vault_path()
        self.assertTrue(mirror.is_file())
        source.unlink()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["source_sets"][0]["classification"] = "P2-confidential"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

        receipt = self.ingest()

        self.assertFalse(mirror.exists())
        self.assertEqual(receipt["counts"].get("restricted_missing_reconciled"), 1)
        row = self.query_one(
            "SELECT status, source_path, relative_path, title FROM sources"
        )
        self.assertEqual(row["status"], "missing")
        self.assertEqual(row["source_path"], "[withheld]")
        self.assertEqual(row["relative_path"], "[withheld]")
        self.assertTrue(row["title"].startswith("Restricted source src-"))
        self.assertEqual(
            self.query_one("SELECT COUNT(*) AS n FROM documents")["n"], 0
        )

    def test_sensitive_git_branch_metadata_fails_closed(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "Qingtian Test")
        self.git("config", "user.email", "qingtian-test@example.invalid")
        (self.project / "README.md").write_text("seed\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "seed")
        self.git("checkout", "-q", "-b", "feature/13900139000")
        self.write_config(
            source_sets=[],
            repositories=[{"project": "sample", "root": "project"}],
        )

        with self.assertRaisesRegex(ConfigurationError, "branch contains"):
            KnowledgeEngine(self.config_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
