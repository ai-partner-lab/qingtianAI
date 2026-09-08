# Qingtian AI

[![CI](https://github.com/ai-partner-lab/qingtianAI/actions/workflows/ci.yml/badge.svg)](https://github.com/ai-partner-lab/qingtianAI/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Qingtian AI is a portable, standard-library-first foundation for long-running AI
work. It combines a durable control plane with a local, evidence-bound knowledge
pipeline while keeping every adopter's product rules, prompts, credentials, and
knowledge content outside this repository.

The distribution is deliberately generic and data-free. Clone it, run the offline
demo, and then add your own provider and project adapters at explicit boundaries.

## Two independent layers

| Layer | Command | What it provides |
|---|---|---|
| Control plane | `qingtian` | Task, session, and run state machines; optimistic concurrency; idempotency; evidence and checkpoints; scoped knowledge records; provider routing; verification receipts; contract validation; and release bundles |
| Knowledge Hub | `qingtian-kb` | Allowlisted file ingestion; provenance and evidence metadata; a human-readable Obsidian-compatible Vault; a rebuildable SQLite FTS index; validation; and a read-only retrieval contract |

Both layers ship in the same Python distribution. They are designed to compose, but
they do not silently share a database or copy data between one another. An adopter
chooses the identity, authorization, retention, and audit policy at the integration
boundary. See [Architecture](docs/ARCHITECTURE.md) for the trust model.

## What is not included

- product-specific workflows, catalogs, moderation rules, prompts, or UI;
- model credentials or a hosted provider account;
- customer, conversation, payment, incident, or production data;
- a generated Vault, source registry, index, receipt store, or local database;
- a hardened multi-tenant service, identity provider, or deployment controller.

## Requirements

- Python 3.11 or newer;
- Linux or macOS;
- WSL2 on Windows (native Windows is not currently in the test matrix);
- Obsidian only if you want its editing experience; Markdown workflows work without it.

## Install

From a checkout:

```bash
git clone https://github.com/ai-partner-lab/qingtianAI.git
cd qingtianAI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[pdf,dev]'
```

Or install a built wheel:

```bash
python -m pip install './dist/qingtian_ai-0.2.0-py3-none-any.whl[pdf]'
```

The optional `pdf` extra enables PDF text extraction. The control plane and the
Knowledge Hub's Markdown, text, JSON, YAML, TOML, CSV, and repository-metadata paths
have no runtime dependency outside the Python standard library.

## Control-plane quick start

The demo is offline, uses synthetic data, and does not require a model credential:

```bash
qingtian doctor
qingtian init --db /tmp/qingtian-demo/control.db
qingtian demo --db /tmp/qingtian-demo/control.db
```

The control plane persists Tasks, Sessions, Runs, Evidence, Checkpoints, and scoped
knowledge in SQLite. Task transitions use an expected revision; Session and Run
transitions are serialized by the local store and checked against their lifecycle.
Repeated external work should use idempotency keys, and uncertain side effects must
remain `UNKNOWN` until reconciled.

A model adapter returns `ProviderResponse(output=..., reported_model=...)`.
`reported_model` must name the model that actually handled the request, after any
provider-side aliasing or fallback. If the adapter cannot determine that model, it
must report `None` (JSON `null`); the gateway keeps `requested_model` separate and
does not guess an effective model.

Inspect all commands:

```bash
qingtian --help
qingtian task-create --help
qingtian verify --help
```

Project adapters can declare verification checks. Command checks are trusted local
programs, not a sandbox; execution requires an explicit acknowledgement. Schemas in
[`schemas/`](schemas/) define the portable records, and synthetic examples live in
[`examples/`](examples/).

Build and verify a manifest-hashed source bundle from the repository's exact release
allowlist:

```bash
qingtian bundle --root . --output release/qingtianAI.tar.gz
qingtian bundle-verify --bundle release/qingtianAI.tar.gz
```

Only regular files named in `release-allowlist.json` enter the bundle. The builder
also applies path, mode, archive, and obvious secret/host-path checks. This source
bundle includes code, contracts, examples, operational documentation, and the complete
`tests/test_*.py` suite, so a recipient can verify the transferred source independently
after checking and extracting the archive.

## Knowledge Hub quick start

Keep the generated knowledge workspace outside this source checkout. From a new,
empty, private directory, point initialization at a project you are authorized to
read:

```bash
mkdir -p /path/to/private-knowledge-home
cd /path/to/private-knowledge-home
qingtian-kb init --workspace /absolute/path/to/your-project --project example-project
qingtian-kb doctor
qingtian-kb plan
qingtian-kb ingest
qingtian-kb validate
```

Initialization creates a local `.qingtian-knowledge-root`, `config/sources.json`, and
`vault/`. The first ingestion creates the local `.state/` index and writes an
ingestion receipt into the Vault. Review the generated allowlist before ingestion.
Later commands resolve configuration in this order: `--config PATH`,
`QINGTIAN_CONFIG`, then `./config/sources.json`.

Search locally:

```bash
qingtian-kb search "architecture decision" --limit 10
```

Or use the read-only JSON provider over standard input:

```bash
qingtian-kb provider-query <<'JSON'
{"schema_version":"1.0","query":"architecture decision","caller_id":"local-agent","purpose":"agent-context","retrieval_modes":["history"],"top_k":10}
JSON
```

The request and response schemas are in [`contracts/`](contracts/) and are also
available from the installed package with `importlib.resources` under
`qingtian_kb.resources.contracts`.

For repository-derived `sqlite-index` material, unknown repository authority fails
closed in `approved` and `candidate` retrieval. An explicitly requested `history`
query may still return an E1 item whose repository authority is unknown as an
investigation lead; that result is non-authoritative and always carries
`eligible_for_generation=false`. It must not be promoted into generation context or
treated as current product truth. Human Vault notes use their separate review,
freshness, conflict, classification, and provenance gates.

## Data boundary

This Git repository contains reusable code, schemas, examples, tests, and guidance
only. Never commit or publish an adopter's generated knowledge workspace here,
including:

- `vault/` or copied source content;
- `config/sources.json` or machine-specific paths;
- `.state/`, SQLite files, ingestion receipts, caches, logs, or source archives;
- credentials, tokens, private conversations, personal data, or operational exports.

The ignore rules and CI checks are guardrails, not a substitute for reviewing the
complete diff and history. Store team knowledge in a separate private repository or
an access-controlled storage system. Deleting a leaked file in a later commit does
not remove it from Git history.

## Build and test

```bash
python -m unittest discover -s tests -v
python -m build
qingtian bundle --root . --output release/qingtianAI.tar.gz
qingtian bundle-verify --bundle release/qingtianAI.tar.gz
```

CI tests Python 3.11–3.14 on Linux and macOS, checks the repository boundary, audits
both distribution formats, smoke-tests both installed CLIs outside the source tree,
and extracts the release-allowlist bundle to run its complete included test suite.

Read [Adoption](docs/ADOPTION.md) before connecting real projects or providers,
[Contributing](CONTRIBUTING.md) before submitting changes, and [Security](SECURITY.md)
for private vulnerability reporting.

## License

Licensed under the [Apache License 2.0](LICENSE).
