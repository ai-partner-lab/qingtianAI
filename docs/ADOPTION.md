# Adoption guide

This guide starts with synthetic offline behavior and adds private project data only
after the storage and authority boundaries are understood.

## 1. Evaluate the distribution

Use Linux or macOS with Python 3.11 or newer. Windows users should use WSL2. Create an
isolated environment and install the project:

```bash
git clone https://github.com/ai-partner-lab/qingtianAI.git
cd qingtianAI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[pdf,dev]'
python -m unittest discover -s tests -v
```

Run the synthetic control-plane flow before adding any provider or project adapter:

```bash
qingtian doctor
qingtian demo --db /tmp/qingtian-evaluation/control.db
```

The demo is offline and should emit a `RUNNING` Task, an `ACTIVE` Session, a
`SUCCEEDED` Run, a checkpoint that references recorded Evidence, and a scoped
knowledge search result. It demonstrates durable primitives rather than closing the
Task lifecycle on an adopter's behalf.

## 2. Choose private storage boundaries

Use separate access-controlled locations for:

- the control-plane database;
- each Knowledge Hub data root;
- provider credentials and configuration;
- logs, receipts, backups, and exported release artifacts.

Do not place generated knowledge data inside this source checkout. This repository's
ignore rules cover common local paths but do not protect data written under an
unexpected name or committed in earlier history.

## 3. Model project work

Define a stable, non-sensitive project identifier and map your lifecycle into the
portable Task, Session, and Run states. Preserve these rules:

- create one bounded Task for one objective and acceptance set;
- use the Task compare-and-swap revision for competing task writers;
- attach execution context to a Session;
- use a unique idempotency key for each logically repeatable Run;
- keep uncertain side effects `UNKNOWN` until an authoritative reconciliation;
- attach Evidence to the exact Task, Session, or Run it supports;
- checkpoint references and next steps before transferring ownership or machines.

Do not encode credentials, full conversations, or unbounded provider payloads in
control records.

## 4. Add a provider adapter

Start from the offline echo adapter and implement the provider interface behind the
gateway. Before enabling real traffic, define:

- secret loading and rotation;
- allowed model routes and data classifications;
- request size, timeout, retry, concurrency, and budget limits;
- idempotency and reconciliation behavior;
- provider retention and training settings;
- privacy-safe request and response evidence;
- exact model lineage: set `ProviderResponse.reported_model` to the model that actually
  handled the request, or `None` (JSON `null`) when the provider cannot determine it;
- a disable switch and rollback procedure.

Test failure, timeout, acknowledgement loss, malformed response, and retry paths. A
transport failure must not be treated as proof that the remote action did not happen.

## 5. Add a project adapter and verification

Copy the synthetic adapter example and define checks with explicit side-effect classes.
Keep read-only checks as the default. Only enable local writes for checks that are
reviewed and reversible. `--execute-trusted-adapter` acknowledges that declared
commands can execute with the current user's permissions; it is not a sandbox.

Store the resulting receipt in an access-controlled location and validate it against
the published schema before using it as a release input.

## 6. Initialize a Knowledge Hub

Create a new empty private directory and point it at a project workspace you are
authorized to read:

```bash
mkdir -p /path/to/private-knowledge-home
cd /path/to/private-knowledge-home
qingtian-kb init --workspace /absolute/path/to/project --project example-project
```

Initialization creates `.qingtian-knowledge-root`, `config/sources.json`, and
`vault/`. The first ingestion creates the local `.state/` index and writes an
ingestion receipt into the Vault. Review `config/sources.json` before ingestion.
Start with the smallest useful source root, narrow include patterns, hard exclusions,
practical size limits, and explicit evidence and privacy metadata.

Never aim discovery at a home directory, filesystem root, credential directory,
dependency tree, production export, unrestricted shared drive, or unrelated worktree.
The bundled example is structural guidance, not authorization to read a real source.

## 7. Plan, ingest, and validate

```bash
qingtian-kb doctor
qingtian-kb plan
qingtian-kb ingest
qingtian-kb validate
qingtian-kb stats
```

Review the proposed read set before ingestion. Afterward, inspect conflicts, stale
items, quarantine metadata, review state, source coverage, and the ingestion receipt.
A zero exit status means the local operation completed its checks; it does not approve
candidate content or prove an external release.

On macOS, `qingtian-kb open-vault` can open the generated Vault in Obsidian. On Linux
or WSL2, open the private `vault/` directory with your chosen Markdown tool.

## 8. Compose the layers explicitly

Use the Knowledge Hub provider contract over standard input and treat each result
according to its authority and usage constraint. An adapter may:

1. authenticate and authorize the caller outside the reference provider;
2. request a bounded project, retrieval mode, and result count;
3. supply returned excerpts to one control-plane Run;
4. record result identifiers, source/content hashes, mode, and constraints as Evidence;
5. require human review before promoting a retrieved claim into scoped control-plane
   knowledge.

Repository-derived `sqlite-index` material with unknown repository authority must
return no result in `approved` or `candidate` mode. An explicit `history` request may
return an E1 unknown-authority item only as a non-authoritative investigation lead
with `eligible_for_generation=false`; keep it out of generation context and
current-product claims. Apply the separate review, freshness, conflict,
classification, and provenance gates to Human Vault notes.

Do not connect the two SQLite databases directly, infer authority from relevance, or
copy a private Vault wholesale into run metadata.

## 9. Prepare production operations

Before production traffic, complete the identity, authorization, encryption, auditing,
retention, deletion, backup, restore, capacity, observability, migration, incident,
and emergency-disable controls in [Architecture](ARCHITECTURE.md). Run restore and
side-effect reconciliation exercises, not just happy-path tests.

## 10. Upgrade and transfer safely

1. Pin and review the exact release or commit.
2. Back up the control database and the Knowledge Hub recovery set separately.
3. Review schema, lifecycle, and migration changes.
4. Test against disposable synthetic storage.
5. Run `doctor`, verification, ingestion planning, and validation.
6. Confirm rollback and `UNKNOWN` reconciliation paths.
7. Transfer private data only through an approved encrypted channel.

The Knowledge Hub's search database is rebuildable. Restore the reviewed Vault,
managed baseline, and matching source registry together; verify hashes and rebuild the
index before enabling retrieval.
