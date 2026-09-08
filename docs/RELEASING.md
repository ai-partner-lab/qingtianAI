# Release checklist

Only maintainers should publish a release. Run the release from a clean checkout whose
complete history has been reviewed for private data.

1. Confirm the version in `pyproject.toml`, package modules, and `CHANGELOG.md` agrees.
2. Run `python -m unittest discover -s tests -v` on all supported Python versions.
3. Run `python -m build` and inspect both the wheel and source distribution.
4. Install the wheel into a fresh environment outside the checkout and smoke-test
   `qingtian` and `qingtian-kb`.
5. Confirm control-plane schemas and provider schemas are present in the wheel.
6. Confirm docs, schemas, examples, launchers, license files, tests, and CI are present
   in the source distribution.
7. Build and verify the release-allowlist bundle, extract it into a clean temporary
   directory, install its development dependencies, and run its complete `tests/`
   suite from the extracted copy.
8. Confirm `release-allowlist.json` names every `tests/test_*.py` file and does not name
   a checkout-root `.qingtian-knowledge-root`; only the packaged marker template under
   `qingtian_kb/resources/` belongs in distributions.
9. Reject generated Vaults, local source registries, state databases, receipts, caches,
   source archives, credentials, private paths, or organization-specific material.
10. Run independent secret, personal-data, dependency, and license review.
11. Tag the reviewed commit, publish immutable artifacts, and record their hashes.
12. Verify the public artifacts from an unauthenticated environment and test rollback.

The built-in scanner and CI boundary check are useful guardrails but are not complete
data-loss-prevention or supply-chain review.
