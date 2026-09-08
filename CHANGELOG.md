# Changelog

All notable changes to Qingtian AI are documented here. The project follows semantic
versioning once public releases are tagged.

## 0.2.0 - 2026-09-08

### Added

- Portable control-plane runtime for Task, Session, Run, Evidence, Checkpoint, scoped
  Knowledge, provider routing, verification receipts, contract validation, and
  manifest-verified release bundles.
- Evidence-bound Knowledge Hub with allowlisted incremental ingestion, an
  Obsidian-compatible Vault, a rebuildable SQLite FTS index, validation, and a
  read-only JSON provider contract.
- Installed `qingtian` and `qingtian-kb` commands in one data-free distribution.
- Packaged JSON schemas, synthetic examples, adoption and architecture guidance, and
  cross-platform CI and artifact checks.

### Security

- Explicit separation between reusable source and adopter-owned databases, source
  registries, Vaults, receipts, credentials, and project content.
- Safe-by-default path, state, authority, side-effect, and release boundaries.

This release contains no migrated knowledge content or product-specific configuration.
