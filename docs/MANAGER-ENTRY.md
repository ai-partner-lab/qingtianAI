# Qingtian manager entry

The open-source engine can create or reuse a real Codex thread named
`擎天大管家入口`, set its name, request persisted pinning and read the result back.
It does not start a model turn, resume old conversations, dispatch tasks or
modify Codex's database directly. Pinning requires a subsequent `thread/read`
with `isPinned: true`, or verified membership of an identified backend's genuine
built-in pinned section as described below. A successful write alone is not proof.
Metadata success is not manager-workflow acceptance. The current protocol does
not read back effective per-thread role instructions; the overall status remains
`partial` even when naming and pinning are verified.

## Start and inspect

```sh
qingtian quickstart --workspace /path/to/workspace --data-dir /path/to/engine --manager-entry --open
qingtian manager-entry status --workspace /path/to/workspace --data-dir /path/to/engine
qingtian manager-entry guide --workspace /path/to/workspace --data-dir /path/to/engine
qingtian manager-entry inspect --workspace /path/to/workspace --data-dir /path/to/engine --thread-id YOUR_EXISTING_THREAD_ID
qingtian manager-entry sync --workspace /path/to/workspace --data-dir /path/to/engine
qingtian manager-entry instructions
```

`quickstart` is dashboard-only by default and does not contact Codex. Add
`--manager-entry` to opt in to manager initialization before starting the
manual-mode dashboard. Failure is printed as structured JSON and does not prevent
the dashboard from starting. `--skip-manager-entry` remains accepted for older
dashboard-only invocations and cannot be combined with `--manager-entry`.
`tour`, `selftest`, ordinary `start`, and HTTP GET requests never initialize an
entry. Existing task and business states are unchanged by onboarding.

`manager-entry init` is the dedicated, explicit first-use creation action. It may
create exactly one entry after writing a durable creation intent, including on
the identified section-compatible CLI version whose empty no-turn listing is
incomplete. A lost response never causes a blind retry. Read-only inspection,
ordinary library calls and automatic recovery remain fail-closed. `sync` only
reuses a matching entry or verifies the existing binding; it never creates a replacement.
`status` and `guide` only read local state and validate the requested scope; they
do not create directories. `guide` also emits one-time setup steps, not commands
to execute automatically. `instructions` exports the suggested role rules without
contacting Codex. These local read-only commands exit zero. `inspect` makes only
read RPCs and exits zero when the selected ID and workspace are verified, or two
when blocked. Its exit zero is not metadata, role or workflow acceptance.
Init/sync exit two while
role setup or other acceptance remains unverified; metadata success alone does
not yield a successful complete-onboarding exit code.

**CLI 0.153.4 first-time boundary:** zero-turn unsectioned threads can be hidden
from its lists, so `inspect` and ordinary discovery report `scan_incomplete`.
Only an explicit `manager-entry init` (or `quickstart --manager-entry`) may cross
that empty-list boundary once, after journaling creation intent. Reuse the same
data directory thereafter. If an existing task is selected instead, inspect it
and bind it with `sync --thread-id`. Never delete a pending binding or switch data
directories merely to create another entry.

### First-user journey without bypassing discovery

Start with `qingtian manager-entry guide` using the dashboard's workspace, data
directory and transport. Its JSON includes copyable commands with these explicit
arguments. Use the same `CODEX_HOME` and environment as the dashboard. No model
default, credentials or socket discovery is added by the guide.

1. Read the saved status. If scope or local state is invalid, restore the original configuration and preserve the binding; the guide withholds runnable setup commands.
2. In your own Codex client, choose the intended existing task in the same workspace and obtain its exact ID. With `creation_pending`, recover the earlier request's result instead of creating another task. Multiple candidates require an explicit selection.
3. Run `qingtian manager-entry inspect --workspace /path/to/workspace --data-dir /path/to/engine --thread-id YOUR_EXISTING_THREAD_ID`. Check `inspection.status: candidate_verified`, `inspection.thread_id`, and `inspection.can_bind: true`. This reads the exact ID with `includeTurns: false`, rejects workspace mismatches and archived records, and does not save a binding, rename, pin, resume or start work.
4. Only after checking the ID and approving the metadata changes, run the displayed `sync --thread-id` command. Sync re-reads the ID, validates the binding scope and may rename/pin the selected task. The inspection does not grant permission or replace sync's checks. Exit two remains expected while role/workflow acceptance is incomplete.
5. Export `manager-entry instructions`, explicitly review/merge the proposed role rules through your client's supported configuration, and obtain separate permission for any ensuing model turn. Keep actual role, desktop and fresh coordination acceptance evidence separate.

If no suitable task exists and you approve creating one, run the displayed
`manager-entry init` command (or `quickstart --manager-entry`) once with the
intended persistent data directory. It creates an idle task without starting a
model turn. There is deliberately no force-create flag: the dedicated command
records intent before creation and refuses to retry after an uncertain response.
Never discard a pending receipt or switch data directories merely to retry.

`inspect` without an ID reads the existing scoped binding, or performs the same
bounded, paginated discovery as initialization. It never authorizes creation,
even when a backend returns no matches. In particular, CLI 0.153.4 still returns
`inspection.error_code: scan_incomplete` for an empty/invisible no-turn scan.
Neither inspect nor guide changes that guard. Protocol reads launch the selected
transport; the Codex server may maintain its own runtime files, but this client
sends no mutation RPC and writes no engine binding or creation receipt.

### Read-only onboarding JSON contract

Every normal `manager_entry` snapshot from `read_status`, including HTTP
snapshots, and the `guide` CLI result includes `onboarding`:

- `schema_version: 1`, `read_only: true`, `reason`, canonical `workspace` and `data_dir`, and `can_create: false`.
- `action_steps`: ordered objects with `id`, `title`, `detail`, `kind`, `argv`, `command`, `shell` and `requires_user_action: true`.
- `kind` is `local_read`, `read_only`, `metadata_write`, or `manual`. Only the future explicit bind command is `metadata_write`; generating/displaying it performs no write.
- `argv` is a token array; `command` is a POSIX-shell-quoted rendering and `shell` is `posix`. Manual steps use null for all three. Windows consumers should display the token array for their supported shell, not assume POSIX quoting works there; native Windows acceptance remains outstanding.

Render steps as instructions or copy targets, never automatic dispatch/approval
buttons. Respect conditional text: a bind command in a local guide is not proof
that its placeholder/selected ID has passed inspection. Scope/config/state errors
produce manual reconciliation only. Arbitrary internal transport commands that
cannot be expressed safely as supported CLI options are not exposed or replaced
with guessed commands.

`inspect` additionally returns `inspection` with `read_only: true`, `status`,
`error_code`, bounded `message`, `thread_id`, `candidate_ids`, `server_version`,
`can_bind`, `can_create: false`, and `observation: current_connection`. Errors may
include `failed_method` and `rpc_code`. The enclosing saved `thread_id`, pin and
role fields remain the last-sync snapshot; do not confuse them with this fresh
identity check. `can_bind` describes only that identity check, not approval to
write. Failed inspections suppress bind instructions; no inspection marks a role
verified or changes `workflow_ready` from false.

## Transport

The default is the documented `codex app-server` stdio transport. It uses the
adopter's Codex installation and Codex home. The client sends `initialize`, waits
for its response, sends `initialized`, and correlates subsequent responses by ID.
It neither invokes `codex exec` nor starts `turn/start`.
The handshake opts into experimental APIs, but a successful request still needs
pin readback before it can count as a verified pin.

To connect through an installed CLI's proxy to an already-running app-server:

```sh
qingtian manager-entry init --workspace /path/to/workspace --data-dir /path/to/engine \
  --transport proxy --socket /path/to/app-server-control.sock
```

`--codex-bin` selects the executable; `--timeout` bounds the connection's complete
RPC session, default 15 seconds, maximum 60. For quickstart, use
`QINGTIAN_CODEX_BIN`, `QINGTIAN_CODEX_TRANSPORT` (`stdio` or `proxy`), and optional
`QINGTIAN_CODEX_SOCKET`. A socket requires proxy mode. No socket is guessed or
read from private desktop management-plane files. Proxy support and socket
availability depend on the installed Codex version.

Pipe reads and writes are nonblocking. Cleanup adds a bounded grace period and
only targets the transport this invocation launched: a separate POSIX process
group, or an assigned Windows Job. The proxy's pre-existing shared app-server is
outside that boundary. An inherited stdout descriptor cannot hold a buffered
reader lock and extend cleanup indefinitely. Platforms that cannot establish
nonblocking pipe/process cleanup report `cleanup_unavailable`. The inherited-FD
and process-group regression was verified on POSIX; the Windows Job path still
requires native Windows acceptance.

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server)
documents `thread/start`, `thread/list`, `thread/name/set`, `thread/read`, and
`thread/metadata/update` with `isPinned`. Installed versions can lag those docs:
some accept the metadata request but ignore unknown pin fields. Missing pin
readback is `pin_unverified`, not a successful pin. Moving into a named custom
section is not treated as native pinning.

An earlier isolated check confirmed creation and naming but lacked native pin
evidence. The final 0.6.0 path was then exercised against Codex Desktop 0.153.4:
the connected server identity was read from the real handshake, the same thread
was moved into the protected built-in pinned section, and `thread/read` returned
that membership. The result was `pinned=true` with
`pin_evidence_source=builtin_section`. This verifies metadata on that host only;
effective role rules still cannot be read back and remain `role_unverified`.

### Version-scoped native section compatibility

CLI 0.153.4 exposes a protected built-in pinned section with identity
`01984de2-8f74-7c91-a3b2-5c5e937cf318`. Its generated protocol, compiled migration
and isolated cross-connection readback establish this identity. The compatibility
path requires the **connected server's** `initialize.userAgent` version, absence
of `isPinned` in the identified thread, and the exact built-in ID returned by a
fully paginated `threadSection/list`. A local executable's version does not
identify a proxy's backend. Unknown versions or missing identities cannot opt
into this path; a section merely named `Pinned` is not a substitute.

The adapter moves the already scope-checked bound ID with `thread/section/move`
and verifies that ID's section through `thread/read`. The CLI destination is the
built-in UUID, not the desktop host tool's literal `pinned` alias. Existing
membership is read back without moving/reordering it again. The `isPinned` path
remains authoritative when that field is present. A metadata permission error,
network/transport failure, timeout, or generic RPC error never triggers a blind
section-write retry. No custom section is created.

The generated `ThreadListParams` contract says omitted `sectionId` includes all
sections, explicit `null` selects unsectioned threads, and an ID selects that
section. However, isolated CLI 0.153.4 observations show a fresh zero-turn record
missing from ordinary and null views even after naming; a specific pinned-section
query returns it after pinning. Title search, all-provider selection and source
filter changes did not resolve the ordinary-list gap. A rollout existed, and a
scan-and-repair listing still omitted it. Binary query fragments include an
empty-preview exclusion, but its exact conditional application is not established
by that static evidence alone. This is **not** evidence that omission defaults to
the unsectioned view. The original combined fixture failure remains a failure;
the narrower persisted section-membership evidence does not overwrite it.

Discovery scans the ordinary view, explicit null view, and every
registered section (including the proven built-in), follows pagination with a
shared safety budget and deduplicates exact IDs before selecting a match. A
catalog/listing failure or ambiguity cannot create a replacement. On this
identified backend, even an exhausted empty scan cannot establish absence of an
invisible unsectioned zero-turn entry, so read-only discovery reports
`scan_incomplete`. The explicit first-use action is separately authorized to
create once because it journals intent before the request and then binds that
returned ID. It does not make ordinary-list discoverability complete. Use an
explicitly selected existing ID to reconcile known entries; do not delete
binding/creation-intent records or create a duplicate to bypass the guard.

## Binding and recovery

The local binding is `/path/to/engine/config/manager-entry.json`, written by
atomic replacement with private file permissions. An OS file lock serializes
initialization for that data directory. The binding fingerprint includes the
canonical workspace, Codex home and transport command. A changed workspace or
transport is `scope_mismatch`; use the original configuration or a separate data
directory. Do not delete a binding merely to retry a failed operation.
Every CLI/HTTP read also compares the current workspace, Codex home and transport
with this fingerprint. A mismatch projects `scope_mismatch`, `available: false`,
`metadata_ready: false`, `pinned: null`, and `stale: true`, without changing the
old binding or making an RPC call. Use the same transport configuration for
onboarding, status and the dashboard runtime.

An unbound initialization searches all supported interactive/app-server source
kinds, follows pages, and checks exact title and workspace. Multiple matches
require an explicit choice. A retrieved thread ID is saved before rename or pin;
later retries target the same thread. Independent data directories do not share
the lock, so concurrent first-time onboarding of the same workspace should use
one chosen data directory.

Before creation, an intent marker is saved. If the connection fails after the
request and before its response, a subsequent run can recover a uniquely named
entry, but will not issue a second creation request for an unnamed/unknown
result. Inspect your Codex task list and bind the intended existing ID:

```sh
qingtian manager-entry sync --workspace /path/to/workspace --data-dir /path/to/engine \
  --thread-id YOUR_EXISTING_THREAD_ID
```

The selected thread must belong to the same workspace. This command can rename
it to `擎天大管家入口` and pin it; it never resumes it. A binding created by
0.6.0rc2 under the legacy `擎天大管家` title is reused and renamed, not duplicated.
Deleted or unreadable bound threads, incomplete listings, corrupt state and ambiguous matches do not cause
automatic replacements. Resolve the binding explicitly. Existing Codex login
and access errors are reported without copying server messages or credentials.

## Role setup and manual acceptance

`role_configuration` is separate from thread metadata. A newly created thread
records source `thread/start`, status `submitted_at_creation`, a SHA-256 of the
canonical rules, and submission time. This proves what this client submitted,
not what an existing or future turn actually loaded. `verified` remains false.
A reused same-name thread, explicit binding, or legacy snapshot has unknown role
provenance unless this binding already contains a matching creation receipt.
Changing the selected thread resets that provenance.

The installed schema permits `developerInstructions` in `thread/start` and
`thread/resume`, but `thread/read` has no corresponding readback field.
`config/read` resolves workspace/disk settings, not one identified thread's
effective instructions. The integration therefore does not resume an existing
thread, overwrite its instructions, edit global config or run a turn to obtain
a success result. An empty metadata/read response cannot verify a manager role.

To finish an adopter-controlled setup:

1. Run `manager-entry status` with the correct workspace, data directory and transport. Confirm the intended thread ID before changing any instructions.
2. Run `manager-entry instructions` to inspect the canonical manager rules. Review and explicitly merge them through your own client's supported configuration, preserving existing applicable rules. Alternatively, use a dedicated manager workspace's `AGENTS.md`; do not replace global or shared-workspace instructions blindly.
3. If your client only supports user messages, the user must explicitly choose to send the rules and authorize the ensuing turn. This initializer does not perform that action.
4. Check actual desktop visibility and persisted pinning, then explicitly authorize a small fresh coordination task to evaluate delegation, responsiveness and evidence-based reporting. Preserve that separate acceptance evidence.

This version has no protocol-backed role readback, so it does not convert a
manual confirmation into `verified: true`. `workflow_ready` remains false and
overall status stays partial. A fully automatic, desktop-visible, pinned and
role-verified manager workflow is not claimed complete.

## Observable state and limits

`/api/health`, `/api/dashboard` and the dashboard's system-details panel expose
`manager_entry`: thread ID, status, availability at last sync, pin readback,
binding/attempt/sync/pin timestamps, and a bounded error code/message. HTTP
readers do not contact Codex. Open system details or refresh the dashboard after
a CLI sync; entry rendering is independent of the task event cursor.

`metadata_ready` means the last readback verified both name and pin in the
current scope. `role_configuration.verified` independently describes role
verification. `partial` means the thread was read successfully but role or
pinning setup remains incomplete. Old `ready` snapshots are projected as partial
with `role_unverified`; they are not silently treated as workflow acceptance. `unsupported`
or `unavailable` describes failed protocol/connection setup; `needs_attention`
describes ambiguous or unsafe-to-repeat binding changes. Missing pin evidence is
`null`, never an invented `false` or `true`. `observation: last_sync` and `stale`
make clear this is not a live connectivity monitor. Five-minute-old snapshots
and unsuccessful syncs are stale; the prior successful timestamp is retained.

`pin_evidence_source` distinguishes `isPinned` from `builtin_section`;
`pin_builtin_section_id` and `pin_backend_version` identify section evidence.
Missing evidence remains null, and scope-invalid read-only projections clear
these fields without rewriting the binding. A native section readback proves
metadata storage, not desktop sidebar appearance, effective role instructions,
or `workflow_ready`. Those acceptance boundaries remain separate.

The engine watchdog's HTTP health code remains independent of optional entry
setup. The feature does not prove desktop sidebar visibility, account login,
execution-model identity or business acceptance. No API response, server stderr,
prompt content, token or raw conversation history is persisted in the binding.

## Isolated checks

```sh
python3 -m unittest tests.atlas.test_manager_entry tests.test_engine_entrypoint tests.atlas.test_server_intake
```

Tests cover create/reuse/pin/readback, retries after lost responses and rename
failure, missing/ignored pin support, pagination, ambiguity, scope changes, file
locking, corrupt state, read-only HTTP/status paths, quickstart fallback, tour
isolation, stdio handshake, interleaved responses, redaction and bounded timeout.
They also cover inherited stdout, blocked stdin, no live reader/transport-child
or descriptor leaks, unrelated-process survival, unknown reused roles, old ready
snapshots, and read-only workspace, home, and transport invalidation with unchanged
binding bytes.
Section compatibility cases also cover actual-handshake version selection,
native versus same-named custom identities, cross-view deduplication, section
and thread pagination, incomplete/ambiguous/empty discovery, original metadata
capability precedence, error paths without alternate writes, and section
readback that is missing or unchanged. No missing-readback assertion is removed.
They use temporary directories and fake protocol peers, without credentials or
model calls. Real desktop placement still requires adopter-side verification
with that installation's supported app-server and sidebar.
