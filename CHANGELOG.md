# Changelog

All notable changes to Qingtian AI are documented here. The project follows semantic
versioning once public releases are tagged.

## 0.5.0 - 2026-09-09

- Rebuild the default product from the working local task engine, including real
  Codex Workers, per-task worktrees, SQLite task/run/event/evidence authority,
  dependencies, recovery coordination, intake attachments, SSE and feedback.
- Default `qingtian` now starts the real engine. Add empty manual quickstart,
  credential-free selftest, an isolated same-engine tour, local project registry
  and explicit Knowledge Hub configuration. No historical runtime data included.
- Keep prior synthetic teaching work under explicit `qingtian-lab` / `legacy`;
  its schemas and database are not the default engine's contracts.
- Package engine UI/policy resources and remove the Python cgi dependency.
- Document actual capabilities, adopter-owned integrations and limitations with
  editable hand-drawn Excalidraw architecture diagrams.
- Expand installation, public-boundary and multi-version regression checks.
- Replace task-card fixed stage percentages with stage, elapsed runtime and actual
  recent Worker activity; explicitly label report indices as non-completion rates.
- Start loopback engine and laboratory HTTP listeners without reverse DNS; keep
  startup deadlines and cover unavailable DNS in direct and child-process tests.

Commands shown in older entries below describe their historical version; use the
`qingtian-lab` entry point for those laboratory commands on 0.5.

## 0.4.0 - 2026-09-08 (local development)

- Clarify that illustrated AI responsibilities are not human users or autonomous
  model connections.
- Expandable responsibility mind map: Qingtian, phase Leaders, specialist Agents,
  recursive work nodes and receipt-linked checks; exploration does not mutate the
  guide task. The capability catalog keeps availability explicit.
- Independent API E2E and optional Playwright browser E2E via fixed UI actions and
  `qingtian demo-check`, using isolated temporary tasks and structured receipts.
- Capability extraction roadmap separating reusable mechanisms from private
  project mappings, data, baselines, credentials and deployment environments.

## 0.3.0 - 2026-09-08 (local development)

### Added

- One-command, loopback-only `qingtian demo-web` experience with six animated roles
  and a seven-step guide backed by real control-plane records and offline echo.
- Inspectable evidence, an explicitly simulated acknowledgement-loss scenario,
  UNKNOWN reconciliation, checkpoint handoff, review, and final Task completion.
- Packaged static UI assets with no CDN, JavaScript build, or runtime dependency.
- Demo adoption guidance and a Chinese product introduction with editable
  hand-drawn Excalidraw architecture sources.

### Boundaries

- The guided scenario is deterministic and synthetic; it is not a production
  multi-agent scheduler, real provider integration, or project Knowledge Hub demo.
- The web server is local-only and uses temporary storage and mutation tokens.
  It is not an authenticated multi-user hosting service.

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
