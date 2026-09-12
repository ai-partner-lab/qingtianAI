# Knowledge source configuration

`sources.example.json` is an empty-safe template for a private Knowledge Hub source
registry. `qingtian-kb init` copies and customizes the packaged template in a newly
created private data root.

`policy.example.json` is the public role-based execution-policy template. Copy it
outside the source checkout, review every value, then point
`QINGTIAN_POLICY_PATH` at that private copy. It intentionally matches the packaged
default and grants no provider capability or execution authorization by itself.

Do not turn this file into a real registry. Machine-specific roots, internal project
identifiers, and source selections belong in the generated `config/sources.json`,
which is intentionally ignored and excluded from distributions.
