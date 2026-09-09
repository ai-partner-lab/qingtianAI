> Legacy laboratory archive: these documents describe the separate pre-0.5 runtime. Use `qingtian-lab` for the commands below; they are not the default engine or its acceptance evidence. See [current engine](../ENGINE-ARCHITECTURE.md).

# AI collaboration and capability demo

## Start in one command

Once Qingtian AI is installed, run:

```bash
qingtian demo-web
```

The browser opens the local URL. If port 8787 is busy or automatic opening is not
available, choose a port and open the printed URL:

```bash
qingtian demo-web --port 8788 --no-browser
```

This is an offline, synthetic experience. Installation needs a package source, but
running the demo needs no model credential, account, CDN, npm build, Docker, or
external API. Python 3.11+ and a modern browser are sufficient. Use WSL2 on Windows;
open the printed loopback URL in your host browser if automatic opening is unavailable.

## The guided experience

Click Next once per step. The active role, task state, event log, and records panel
are linked to the server response. The animation illustrates returned observations;
it does not decide whether an operation succeeded.

| Step | Role | What happens |
| --- | --- | --- |
| 1 | Planner | Establish scope and acceptance, activate a session, and start a Task. |
| 2 | Knowledge keeper | Add a synthetic scoped control-plane knowledge record. |
| 3 | Builder | Execute the real bundled EchoProvider and record Run evidence. |
| 4 | Builder | Inject a local acknowledgement-loss scenario, enter UNKNOWN, and checkpoint. |
| 5 | Tester | Recover from the checkpoint, reconcile the synthetic operation, and verify. |
| 6 | Reviewer | Record review evidence and move the Task to REVIEW_PENDING. |
| 7 | Archivist | Preserve completion references and finish the Task as DONE. |

The six characters are visual roles in a deterministic teaching orchestrator. They
are not six autonomous model connections. The knowledge step demonstrates the
control plane's small scoped records, not a full Knowledge Hub ingestion or RAG run.
The injected loss concerns a synthetic local operation, not a live external action.

## Inspect rather than just watch

The responsibility map is hierarchical: Qingtian → seven phase Leaders → specialist
Agents → concrete work items → checks and artifacts. Click a Leader to reveal its
Agents; click an Agent and then its work nodes to keep expanding. Each Agent owns
one capability in the catalog. This is an inspectable responsibility configuration,
not proof that a matching autonomous process or model is connected.

Selection and expansion are independent of actual progress: selecting phase 5
does not execute phases 1–4, change a Task revision, or manufacture completed steps.
You can inspect earlier or later branches without running the guide.

Under the testing Leader, expand the **API E2E** or **Browser E2E** Agent and run
that check independently.
The check creates its own temporary local service and fresh synthetic task, then
returns a separate receipt. It does not advance or reset the visible guide.
The browser check operates the actual UI with Playwright; the API check uses HTTP
and does not claim browser coverage. Other cards clearly state whether they are
available through a CLI/adapter or still planned; they do not expose fake run buttons.
Receipt checks are linked to work nodes by their stable check IDs. Unexecuted or
unmapped nodes do not inherit a green success state from a parent. Results describe
the latest independent receipt retained in this page, not durable production jobs.

The guide's fifth step is specifically UNKNOWN reconciliation and handoff. Its
small synthetic verification Run is not itself the independent browser E2E suite.
See [Capabilities](CAPABILITIES.md) for setup, result semantics and module boundaries.

Open the records panel to compare Task revisions, Session states, Run idempotency
keys and states, Evidence subjects, and Checkpoint hashes. Before UNKNOWN is
reconciled, the core refuses a new execution attempt. Reset begins a fresh demo
round; it never deletes an adopter's project data.

Reduced-motion preferences are respected. Buttons remain usable with a keyboard,
and the guide is usable on a small screen. A lost connection is shown as an error,
not as a fabricated success. Stale or concurrent step requests are rejected and the
UI refreshes the authoritative state.

## Local-only boundary

The command binds only to 127.0.0.1, stores synthetic records in a temporary private
directory, and cleans that directory up on normal shutdown. It does not load a real
Vault, read project files, execute user-supplied commands, or call cloud services.
Host and Origin checks and a per-demo mutation token protect the local action
endpoints. The token is not an enterprise identity or authorization system.

Do not expose this teaching server through a public reverse proxy or treat it as a
production control panel. It has no user accounts, tenancy, durable remote jobs, or
service-level availability promise. A killed process may leave an operating-system
temporary directory; its data is synthetic only.

## From demo to a real project

Keep the visible lifecycle, idempotency, reconciliation, and evidence invariants.
Replace the scenario-specific orchestrator with an adopter-owned integration,
implement a real model provider, and add authentication, permissions, retention,
encrypted storage, budgets, monitoring, and recovery. Follow [Adoption](ADOPTION.md)
before introducing private data or external actions.
