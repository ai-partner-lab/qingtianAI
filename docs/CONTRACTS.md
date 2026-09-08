# Public contracts

Qingtian AI publishes JSON schemas so integrations can validate boundaries without
depending on Python internals.

## Control-plane schemas

The files in [`../schemas/`](../schemas/) define Task, Session, Run, Evidence,
Checkpoint, scoped Knowledge, project adapter, verification receipt, and release
allowlist records. Synthetic examples are in [`../examples/`](../examples/).

Use the bundled validator for the supported schema subset:

```bash
qingtian contract-validate \
  --schema schemas/task.schema.json \
  --document examples/task.json
```

Schema `$id`, `schema_version`, state names, required fields, and enum values are public
compatibility surfaces. Additive optional fields are normally backward-compatible;
removing fields, tightening constraints, or changing semantics requires a documented
version change and migration guidance.

The Python model-provider boundary also has a lineage invariant: every adapter must
construct `ProviderResponse` with `reported_model` set to the model that actually
handled the request. If that model is unknown, the value is `None` (JSON `null`), and
`ProviderResult.effective_model` remains unknown rather than inheriting the requested
model.

## Knowledge provider schemas

[`../contracts/provider-request.schema.json`](../contracts/provider-request.schema.json)
and [`../contracts/provider-response.schema.json`](../contracts/provider-response.schema.json)
define the local read-only retrieval interface. The same files are included in wheels:

```python
from importlib.resources import files

contracts = files("qingtian_kb.resources.contracts")
request_schema = contracts.joinpath("provider-request.schema.json").read_text(encoding="utf-8")
response_schema = contracts.joinpath("provider-response.schema.json").read_text(encoding="utf-8")
```

`caller_id` and `purpose` are descriptive labels. They do not authenticate a caller.
The response's authority, provenance, usage constraints, and policy flags must travel
with any excerpt supplied to another system.

For repository-derived `sqlite-index` material, unknown repository authority is
excluded from `approved` and `candidate`. An explicit `history` request may expose an
E1 unknown-authority record only as a non-authoritative lead with
`eligible_for_generation=false`; conflicting authority remains excluded. Human Vault
notes use their independent governance metadata rather than a required repository
entry.

## Compatibility discipline

- Pin a released version or exact commit in production.
- Validate both requests and responses at process or service boundaries.
- Preserve unknown fields only when the relevant schema permits them.
- Fail closed on unknown lifecycle states or higher data classifications.
- Record the schema version and content hash with durable evidence.
- Test old readers against new writers and new readers against retained fixtures.

The schemas describe data shape and selected invariants. They do not provide identity,
authorization, transport security, storage encryption, or policy approval.
