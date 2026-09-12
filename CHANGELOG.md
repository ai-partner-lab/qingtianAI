# Changelog

All notable changes to Qingtian AI are documented here. The project follows semantic
versioning once public releases are tagged.

## Unreleased — 0.6.0rc2 candidate

This is a prerelease source candidate, not a published stable release or a claim
that every integration is ready. Final whole-candidate regression, clean installs
and artifact review remain release gates; older 0.5.0 results are not relabeled.
Real provider execution and native Codex first-use/role workflow acceptance remain
unverified and must be reported separately from credential-free fixture results.

- Normalize every sdist tar owner/PAX header and gzip header through the standard
  build hook; preserve source bytes and executable modes, and check actual wheel,
  sdist and bundle headers in distribution regression/CI. Earlier candidates with
  host-identifying archive metadata are not approved for publication.
- Raise the optional PDF floor to the checked `pypdf>=6.18.0,<7` and the build/dev
  setuptools floor to `>=78.1.1`, excluding cited older affected ranges without
  adding a mandatory runtime dependency or claiming complete supply-chain review.
- Separate the historical recording's 307/294/13 SSE observation from the RC's
  scoped cursor repair, and distinguish implemented public navigation UI from
  unverified native destinations and first-use/role workflows.
- Separate atomic dashboard snapshot versions from delivered SSE cursors, honor
  Last-Event-ID before stale URL cursors, paginate backlog, and preserve reset and
  client parse/render failure recovery boundaries.
- Add version-checked ordinary human-action reports. Schema 10 persists a monotonic
  task-row revision, including same-value writes, to prevent stale ABA confirmation.
  Mutation and audit succeed or roll back together; conflicts require a fresh GET.
  Migrated revision 0 is a baseline, not reconstructed history. Success enters
  VERIFYING only and never grants sensitive-action, budget or deployment approval.
- Make new execution parameters exact and fail closed: only Sol/Astra, reasoning
  `medium`, `high`, `xhigh` or `ultra` without clamp, and independent standard/fast speed. Role
  defaults are manager Astra/ultra/fast and executor/planner Sol/high/standard;
  explicit arguments outrank environment and route/default selection. Add a matching
  public policy example, role-default selftests and a source-checkout real Worker
  selfcheck that verifies the run's persisted tuple instead of one hard-coded model.
- Require a manually reviewed, private, 24-hour schema-2 local Codex capability
  manifest for real execution. Add read-only `capabilities status` and explicit,
  non-overwriting `capabilities prepare`; neither reads credentials/conversations,
  calls a model, auto-enables the draft, or proves account/tier availability.
- Freeze seven execution-target fields on admitted new runs. Refuse resume when an
  old run lacks that immutable snapshot; preserve old columns without guessing or
  backfilling historical execution.
- Add revisioned, idempotent admission receipts to the HTTP task-dispatch route.
  Intake, retry, automatic dispatch and external-host acknowledgement remain
  separate and are not presented as the same guarantee.
- Add a transactional task-completion evidence gate that `force` cannot bypass,
  and an operations-clarity projection for actor, next action, due time and source.
  Reviewed corrections are supplemental, append-only declarations and do not
  overwrite native task facts.
- Add append-only release batches and reviewed receipts as declared release facts.
  They perform no deployment, enablement or acceptance action; task/run `DONE`
  never creates or proves a release.
- Make quickstart dashboard-only by default. Real manager-entry setup is an explicit
  `--manager-entry` opt-in; retain `--skip-manager-entry` for compatibility.
- Add an isolated current-engine browser CI job with Python 3.12, Playwright 1.55.0,
  matching Chromium and failure receipts, separate from the legacy laboratory job.
  Missing browser dependencies fail the browser job, not silently skip acceptance.
- Extend dashboard action details, narrow-screen layout and failed-render recovery,
  and expose scoped manager-entry metadata without treating submitted role rules,
  names or pin metadata as verified native collaboration readiness.

- Preserve the complete eight-file showcase example and the one reviewed short
  video in source distributions and independent allowlist bundles; keep editorial
  media and presentations out of runtime wheels. Add extracted-archive byte,
  navigation, font-notice, example syntax and provenance regression checks.
- Allow showcase source provenance to be read from archives without Git metadata:
  report an unknown commit as `null` and retain per-file source digests. This does
  not re-record or re-certify the earlier synthetic demonstration or its disclosed
  13-event SSE gap.

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
- Add operational documentation set for 0.5 (quickstart, operations, FAQ,
  migration, terminology), and keep legacy demo/0.4 content in explicit archive mode.
- Align documentation with CLI signatures, background service ownership, the
  eight synthetic selftest checks, intake idempotency and evidence deduplication;
  expand Knowledge Hub onboarding and source-bundle documentation checks.
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
