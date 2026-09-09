# Security policy

Qingtian AI executes local workflows, reads explicitly selected files, and persists
control and knowledge state. Path handling, state transitions, idempotency,
verification commands, bundle construction, non-destructive note updates, quarantine,
provenance, and retrieval authority are security-relevant behavior.

## Supported versions

Security fixes are applied to the latest released minor version. Older versions may
not receive patches. Before a formal release exists, pin and review the exact default-
branch commit you evaluate.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/ai-partner-lab/qingtianAI/security/advisories/new)
when available. Do not publish an unpatched vulnerability in an issue, discussion,
pull request, fixture, receipt, or log.

Include the affected version or commit, platform and Python version, a minimal
synthetic reproduction, expected and actual boundary, impact, and any known workaround.
Do not attach credentials, private knowledge, customer material, personal data,
database copies, provider payloads, or unrestricted source archives. Redact local
paths and identifiers that are not essential to reproduction.

## Reference-runtime boundary

This repository is not a hardened multi-tenant service. In particular:

- The default engine is loopback-only, with Host/Origin checks but no user login
  or RBAC. Local programs able to reach the port are in the trust boundary.
- Actual Workers require a registered Git target and use Codex workspace-write;
  a worktree is not a container. Local shell tools still need careful authority.
- Explicit model planning reads the authorized startup workspace, not the project
  execution registry. Read-only planning can still disclose data to the provider.
- Synthetic tour is read-only and cannot dispatch; ordinary manual mode is writable
  and allows explicit new work. Analysis/reference authorization must not be
  upgraded by switching to auto mode.

- SQLite and Markdown files are not encrypted storage.
- Knowledge provider labels do not authenticate or authorize callers.
- Verification adapter commands are trusted local programs and are not sandboxed.
- The legacy lab echo provider is a development adapter, not the real engine's
  Codex execution path.
- Pattern scanning and release scanning are not complete DLP or secret detection.
- A successful local check does not establish deployment or policy approval.

Network exposure requires authenticated workload identity, role and purpose
authorization, transport security, project and classification enforcement, request
limits, audit retention, deletion propagation, provider governance, and incident
controls.

## High-value reports

- invalid lifecycle transitions or compare-and-swap bypass;
- duplicate side effects despite an idempotency key, or unsafe handling of `UNKNOWN`;
- command execution without the documented trust acknowledgement;
- source-root, archive, bundle, or symlink escape;
- unintended file reads or writes;
- silent overwrite of a human-managed or modified note;
- sensitive content copied into quarantine metadata, diagnostics, or receipts;
- provider scope, authority, privacy, or classification bypass;
- contract validation accepting a security-significant invalid document;
- secret leakage, malicious package content, or dependency/build compromise.

## If private data is committed

Stop distribution, revoke or rotate exposed credentials, preserve an incident record,
and remove the material from the complete Git history with an appropriate rewrite
procedure. Deleting only the newest copy is insufficient. Coordinate downstream clone,
cache, artifact, and mirror cleanup before resuming a release.
