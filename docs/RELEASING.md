# Release checklist

Only maintainers should publish a release. Run the release from a clean checkout whose
complete history has been reviewed for private data.

The current source version is **0.6.0rc2**, an unpublished prerelease candidate,
not stable 0.6.0. Its final combined-source regression and artifact acceptance
remain pending. Actual provider execution and native Codex first-use/effective
role workflow acceptance are separate unfinished gates, not implied by synthetic
fixtures, valid configuration or the version string. Keep prior 0.5.0 receipts
and the historical video unchanged; do not relabel them as rc2 passes.

1. Confirm the version in `pyproject.toml`, package modules, and `CHANGELOG.md` agrees.
2. Run `python -m unittest discover -s tests -v` on all supported Python versions.
3. Run `python -m build` and inspect both the wheel and source distribution.
4. Install the wheel into a fresh environment outside the checkout and smoke-test
   `qingtian` and `qingtian-kb`.
5. Confirm control-plane schemas and provider schemas are present in the wheel.
6. Confirm docs, schemas, examples, launchers, license files, tests, and CI are present
   in the source distribution.
   Check Markdown navigation in the extracted distribution, including adoption,
   operations, FAQ, migration, terminology, and both canonical engine Excalidraw
   scenes with their SVG/PNG exports and font diagnostics.
   Run `python -m unittest discover -s tests -p test_distribution_contract.py -v`
   with the `dev` extra installed. This builds a separate source archive and bundle,
   checks file bytes and local Markdown targets after extraction, and tests example
   syntax and archive-safe provenance without recording or starting the engine.
   The PEP 517 backend in `scripts/build_sdist.py` delegates to setuptools and
   normalizes tar.gz owner IDs/names,
   timestamps and nonstructural PAX metadata, plus gzip filename/time fields.
   Audit **every header, including directories**, not just file contents. Preserve
   source bytes, normal metadata and executable modes. Use the registered backend
   through `python -m build`; directly calling setuptools' unwrapped sdist hook
   bypasses this project contract and is not an accepted release build.
   CI runs `assert_public_archive_headers` from the distribution contract test on
   the actual wheel/sdist/bundle: wheel comments/extra fields and identifying tar
   metadata are not accepted. Header privacy is not established by content hashes.
7. Build and verify the release-allowlist bundle, extract it into a clean temporary
   directory, install its development dependencies, and run its complete `tests/`
   suite from the extracted copy.
8. Confirm `release-allowlist.json` names every Python file recursively under `tests/`
   (including `tests/atlas/`) and does not name
   a checkout-root `.qingtian-knowledge-root`; only the packaged marker template under
   `qingtian_kb/resources/` belongs in distributions.
   Every new public documentation page must also be explicitly allowlisted. The
   source distribution's recursive Markdown rule does not add files to the
   separate allowlist bundle; check navigation inside both artifacts.
9. Reject generated Vaults, local source registries, state databases, receipts, caches,
   source archives, credentials, private paths, capability manifests/drafts, or adopter-specific material.
10. Run independent secret, personal-data, dependency, and license review.
11. Tag the reviewed commit, publish immutable artifacts, and record their hashes.
12. Verify the public artifacts from an unauthenticated environment and test rollback.

The built-in scanner and CI boundary check are useful guardrails but are not complete
data-loss-prevention or supply-chain review.

The engine's `/api/release-batches` records append-only, reviewed declarations of
deployment, enablement and acceptance facts. Those records are evidence receipts,
not deployment actions or independent verification. A task/run becoming `DONE`
does not create or prove a release. Maintainers must still perform this checklist,
approve the exact frozen source and publish through an explicitly authorized release
process; the release API cannot satisfy or bypass these gates.

## Artifact contract

| Artifact | Included | Intentionally excluded |
|---|---|---|
| Runtime wheel | Engine/laboratory/Knowledge Hub Python modules, policy, schemas, static UI and packaged KB templates; package license metadata. | Repository docs, diagrams, showcase source, videos and presentation decks. |
| Python source distribution | Every reviewed `release-allowlist.json` path, including docs, tests, CI, examples and the assets below; normal build metadata may be additional. | Runtime data, private output, unreviewed showcase files and unapproved media. |
| Independent allowlist bundle | Exactly the allowlisted paths plus its generated `MANIFEST.sha256`; executable launchers are preserved. | All non-allowlisted files, including Python build metadata and private state. |

`examples/showcase/` has exactly eight source files: `README.md`, `scenario.json`,
`engine_bridge.py`, `record.cjs`, `verify_recording.py`, `render.cjs`, `qa_media.cjs`,
and `check_playback.cjs`. Run the documented commands from an extracted archive's
root just as from a checkout; Git metadata is optional and an unavailable source
commit is explicitly `null`. Example dependencies remain optional authoring tools,
not engine runtime dependencies. Syntax/provenance checks do not certify a new
recording or a browser journey.

The exact editorial asset exception is `docs/assets/showcase/poster.png`,
`actual-board-public.png`, and `qingtian-showcase-short.mp4`. The approved video is
6,549,535 bytes with SHA-256
`ce7885bcbfc6de9ba0e06dc4802d7f81a04385fe16e2dfbfd5335218d3ff3faf`.
Do not replace it with a freshly rendered file or include a full/raw recording,
database, private audit, presentation archive, or a user's private screenshot.
The historical [SSE omission disclosure](VIDEO-DEMO.md#必须一起展示的-sse-限制)
remains part of that recording's evidence even after a future engine fix.

Keep native Excalidraw scenes and their same-scene SVG/PNG exports. The engine
SVGs already embed the required Excalifont/Xiaolai subsets; distribute their
[font notices and OFL text](diagrams/FONT-LICENSES.md) alongside the diagnostics.
Do not claim the archive includes a complete offline font/editor installation.

The optional PDF extra requires `pypdf>=6.18.0,<7`; no PDF dependency is added to
the core runtime. The upstream [CID width advisory](https://github.com/py-pdf/pypdf/security/advisories/GHSA-fwg2-594c-jp42)
and [whitespace parser advisory](https://github.com/py-pdf/pypdf/security/advisories/GHSA-fc8x-2rww-xw9m)
identify versions below 6.15.0 as affected; the higher floor uses the version
actually checked for this candidate. Build/dev setuptools requires `>=78.1.1`,
excluding the older [PackageIndex traversal range](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf).
These are bounded minimum-version decisions, not an exploit test, unrestricted
PDF trust policy or complete transitive dependency/security certification.

## Candidate freeze and evidence

Before the final regression, freeze all owners' source files together and record a
reviewed commit (or pre-commit snapshot identity), every allowlisted file's SHA-256,
the allowlist hash, Python/build-tool versions, OS/architecture, and the exact
commands and exit codes. Rebuild and repeat affected checks if any candidate input
changes. A prior green CI run, older test count, demo receipt or package version
string does not certify a later dirty snapshot.

The configured CI target is macOS and Linux on Python 3.11–3.14. Report actual
executed combinations separately from configured, failed, skipped and not-run
ones. Native Windows is not currently a claimed supported/accepted platform.
Keep current-engine browser acceptance distinct from the legacy
`qingtian-lab demo-check --capability browser-e2e` job and synthetic DOM/protocol
unit tests. Missing browser prerequisites must not become an accepted browser pass.
Credential-free fixtures do not prove real model availability, desktop entry
visibility, effective manager role rules or production acceptance. A locally
reviewed capability manifest proves only a current installation advertisement,
not account permission, quota or served tier.

The dedicated `current-engine-browser` CI job uses Ubuntu, Python 3.12 and
`playwright==1.55.0`; install its matching Chromium with
`python -m playwright install --with-deps chromium`. From the repository root run
`python -m tests.atlas.test_dashboard_journey --browser-e2e "$RUNNER_TEMP/qingtian-engine-browser"`.
No external Node installation is required by this Python browser driver; separate
DOM unit checks use the CI Node runtime. The job has a ten-minute budget, treats
missing dependencies and cleanup failures as failures, validates receipt hashes,
and always attempts to upload the dedicated receipt directory (including raw
HTTP/SSE, screenshots, summary, ledger and logs). A hard runner termination cannot
guarantee receipt generation or upload. Retain the separately named legacy job;
neither job's configured existence is a claim that the current candidate passed.

After source freeze, the release evidence must include fresh full tests, selftest,
current-engine browser results, wheel/sdist/bundle contents and hashes, a clean
outside-checkout wheel install/smoke, and a complete test run from the independently
extracted bundle. Also inspect the artifacts themselves for private data and
third-party notices; source-only scanning is insufficient. Do not publish if any
applicable gate is blocked or requires a maintainer decision.

The preserved showcase video is historical evidence from an older real-engine run
using synthetic tasks. Keep its media bytes and disclosures unchanged. It does not
claim that the rc2 candidate's browser journey, paid models, capability manifest or
native manager workflow has been newly verified.

Only after maintainers approve that evidence: commit the exact reviewed source,
verify successful CI on that commit, create the selected version tag, and publish
immutable matching artifacts and checksums. Never silently overwrite a tag or
replace a published asset. Download and verify the public bytes independently;
state backup/rollback results honestly. This checklist does not itself authorize
a commit, push, tag, release, service restart or deployment.
