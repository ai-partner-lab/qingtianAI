# Contributing

Thank you for improving Qingtian AI. Contributions are accepted under the
[Apache License 2.0](LICENSE) and should preserve portability, explicit authority,
durable evidence, and the repository's data-free boundary.

## Development setup

Use Linux or macOS with Python 3.11 or newer. Windows contributors should use WSL2.

```bash
git clone https://github.com/ai-partner-lab/qingtianAI.git
cd qingtianAI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[pdf,dev]'
python -m unittest discover -s tests -v
python -m build
```

Run both offline smoke paths after a behavior or packaging change:

```bash
qingtian doctor
qingtian selftest
qingtian-lab demo --db /tmp/qingtian-contributor/legacy.db
qingtian-kb --help
```

## Data-free contribution rule

Use temporary directories and unmistakably synthetic, non-identifying fixtures. Do
not add to a commit or Git history:

- generated `vault/`, `.state/`, `config/sources.json`, databases, receipts, logs,
  source archives, or local caches;
- organization, customer, product, incident, deployment, or private test material;
- credentials, tokens, private conversations, personal data, payment data, or
  production exports;
- developer-specific absolute paths, user names, host names, account names, network
  addresses, repository remotes, or infrastructure identifiers.

Ignore rules and automated scans are defense in depth, not permission to stage
sensitive material. Review every changed file and scan the complete history before a
public release.

## Design rules

- Keep side effects explicit, bounded, and evidence-producing.
- Preserve real-engine lifecycle validation, idempotency, evidence completion
  gates, explicit execution authority and manual-mode boundaries. The lab's CAS
  revisions and UNKNOWN schema belong only to the separate legacy runtime.
- Do not describe a reported or missing result as observed success.
- Keep source discovery allowlist-based and reject unsafe paths by default.
- Preserve provenance, authority, review, freshness, privacy, and conflict metadata.
- Never overwrite human-managed or human-modified knowledge silently.
- Keep provider requests bounded and provider responses provenance-aware.
- Treat project adapter commands as trusted local programs, not sandboxed input.
- Maintain documented contracts or version and test an intentional incompatibility.
- Do not introduce an implicit bridge between the control and Knowledge Hub databases.

## Tests and documentation

Add focused tests for the successful path, invalid input, boundary behavior, failure,
retry or concurrency where relevant, and non-destructive handling. Tests must not
depend on network access, credentials, the current user name, or a developer's machine.

Update public documentation and examples when behavior, state, configuration, schemas,
or platform support changes. If a schema changes, describe compatibility and migration
impact in the pull request and `CHANGELOG.md`.

## Pull requests

A pull request should include:

- the problem, intended behavior, and non-goals;
- implementation and security-boundary summary;
- tests run, operating systems, and Python versions;
- contract, migration, rollout, and rollback impact;
- confirmation that the diff and history contain no private data.

Keep commits focused and do not reformat unrelated files. All CI jobs must pass.
Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
