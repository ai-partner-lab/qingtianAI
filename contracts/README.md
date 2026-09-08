# Knowledge provider contract

The request and response schemas define the local process boundary between an AI
integration and `qingtian_kb.provider.QingtianKnowledgeProvider`. Call
`qingtian-kb provider-query` with a JSON request on standard input and consume one
JSON response on standard output.

The schemas are also installed in `qingtian_kb.resources.contracts` for consumers
using `importlib.resources`. Files in this directory and their packaged copies must
remain byte-identical.

## Security defaults

- `approved` is the only implicit retrieval mode. `candidate` and `history` require an
  explicit request.
- Conflict, stale, missing, P2/P3, and disputed-authority material is excluded.
- The provider opens SQLite read-only and rejects unsafe WAL or changing-index states.
  Human Vault notes are read through bounded, no-follow, stability-checked paths.
- Query text is accepted on standard input, not a positional process argument, and is
  neither echoed nor persisted by the provider. This does not cover the caller,
  terminal capture, operating-system telemetry, crash capture, or process memory.
- Secret and personal-data patterns are guardrails, not complete DLP. Encoded,
  fragmented, image-only, encrypted, or novel formats require independent controls.
- E1 evidence is history-only and E0 is excluded. Approved human notes require the
  documented review, freshness, conflict, classification, and provenance metadata.
- Repository authority and canonical refs are configuration declarations. Remote
  verification is not implied. For repository-derived `sqlite-index` material,
  unknown authority fails closed in `approved` and `candidate`; an explicit `history`
  request may return an E1 unknown-authority lead only with `authoritative=false` and
  `eligible_for_generation=false`. Conflicting authoritative state remains excluded.
  Human Vault notes follow their independent human-governance gates.
- Cross-plane duplicates are reconciled by raw knowledge ID and content hash.
- `caller_id` and `purpose` are labels, not authenticated identity or authorization.
- A response is bounded context with provenance, not proof of deployment, product
  behavior, release, or acceptance.

## Examples

Default approved retrieval:

```bash
qingtian-kb provider-query <<'JSON'
{"schema_version":"1.0","query":"retention policy","caller_id":"local-agent","purpose":"agent-context"}
JSON
```

Investigation-only historical retrieval:

```bash
qingtian-kb provider-query <<'JSON'
{"schema_version":"1.0","query":"migration decision","caller_id":"local-research","purpose":"human-research","retrieval_modes":["history"]}
JSON
```

For an interactive raw-text producer, keep non-sensitive metadata in flags:

```bash
printf '%s\n' 'knowledge privacy' \
  | qingtian-kb provider-query --caller-id local-agent --purpose agent-context
```

See [Public contracts](../docs/CONTRACTS.md) for compatibility guidance.
