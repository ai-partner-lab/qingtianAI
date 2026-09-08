from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
import sys
import tempfile
import unittest

from qingtian_core.contracts import (
    ContractValidationError,
    bundled_schema,
    validate,
    validate_files,
)
from qingtian_core.models import RunState, SessionState, canonical_json
from qingtian_core.store import QingtianStore
from qingtian_core.verification import run_checks


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"


def schema(name: str) -> dict[str, object]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


class ContractTest(unittest.TestCase):
    def test_repository_and_packaged_schemas_are_identical(self) -> None:
        packaged = files("qingtian_core.resources.schemas")
        repository_names = {path.name for path in SCHEMAS.glob("*.schema.json")}
        packaged_names = {
            item.name
            for item in packaged.iterdir()
            if item.name.endswith(".schema.json")
        }
        self.assertEqual(packaged_names, repository_names)
        self.assertEqual(len(repository_names), 9)
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            resource = packaged.joinpath(path.name)
            self.assertEqual(resource.read_bytes(), path.read_bytes())
            name = path.name.removesuffix(".schema.json")
            self.assertEqual(bundled_schema(name), json.loads(path.read_text(encoding="utf-8")))
        with self.assertRaisesRegex(ContractValidationError, "invalid bundled schema name"):
            bundled_schema("../task")

    def test_all_schemas_are_unique_and_use_supported_keywords(self) -> None:
        identifiers: set[str] = set()
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            contract = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(contract["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertTrue(contract["$id"].startswith("urn:qingtian-ai:schema:"))
            self.assertNotIn(contract["$id"], identifiers)
            identifiers.add(contract["$id"])
            # Validating an intentionally incomplete object exercises schema keyword parsing.
            with self.assertRaises(ContractValidationError):
                validate({}, contract)
        with self.assertRaisesRegex(ContractValidationError, "unsupported schema keywords"):
            validate(
                {},
                {"type": "object", "properties": {"optional": {"futureKeyword": True}}},
            )
        with self.assertRaisesRegex(ContractValidationError, "schema must be an object"):
            validate({}, [])  # type: ignore[arg-type]

    def test_example_task_validates_and_unknown_field_fails_closed(self) -> None:
        result = validate_files(SCHEMAS / "task.schema.json", ROOT / "examples" / "task.json")
        self.assertEqual(result["status"], "valid")
        invalid = json.loads((ROOT / "examples" / "task.json").read_text(encoding="utf-8"))
        invalid["undeclared"] = True
        with self.assertRaisesRegex(ContractValidationError, "unexpected properties"):
            validate(invalid, schema("task"))
        with self.assertRaises(ContractValidationError):
            validate(True, {"const": 1})

    def test_non_finite_numbers_are_not_portable_json(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": "雪"}), '{"a":"雪","b":1}')
        with self.assertRaisesRegex(ValueError, "non-finite"):
            canonical_json({"value": float("nan")})
        with self.assertRaisesRegex(ContractValidationError, "non-finite"):
            validate(
                {"metadata": {"value": float("inf")}},
                {"type": "object", "properties": {"metadata": {"type": "object"}}},
            )

    def test_malformed_schema_keyword_values_fail_structurally(self) -> None:
        malformed_schemas: tuple[object, ...] = (
            {"type": []},
            {"type": ["string", "string"]},
            {"enum": "not-an-array"},
            {"enum": [1, 1]},
            {"required": "name"},
            {"properties": {1: {}}},
            {"additionalProperties": 0},
            {"items": []},
            {"minItems": True},
            {"minLength": -1},
            {"minimum": "zero"},
            {"pattern": 7},
            {"pattern": "["},
            {"format": "email"},
        )
        for malformed in malformed_schemas:
            with self.subTest(schema=malformed):
                with self.assertRaises(ContractValidationError):
                    validate({}, malformed)  # type: ignore[arg-type]

    def test_date_time_format_is_strict_rfc3339(self) -> None:
        contract = {"type": "string", "format": "date-time"}
        for value in (
            "2026-09-08T12:34:56Z",
            "2026-09-08T12:34:56.123+08:00",
        ):
            validate(value, contract)
        for value in (
            "2026-09-08 12:34:56Z",
            "2026-W37-2T12:34:56Z",
            "2026-09-08T12:34:56+08:00:30",
            "2026-09-08T12:34:56",
            "2026-09-08T24:00:00Z",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ContractValidationError, "RFC 3339"):
                    validate(value, contract)

    def test_store_outputs_match_public_contracts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-contract-test-") as temp_name:
            with QingtianStore(Path(temp_name) / "state.db") as store:
                store.initialize()
                task = store.create_task(
                    project_id="contract-project",
                    title="Contract task",
                    objective="Validate stored public objects",
                    scope=["synthetic"],
                    acceptance=["all objects validate"],
                )
                ready = store.transition_task(
                    task["task_id"], "READY", expected_revision=task["revision"]
                )
                task = store.transition_task(
                    task["task_id"], "RUNNING", expected_revision=ready["revision"]
                )
                session = store.create_session(task_id=task["task_id"], host_alias="test-host")
                session = store.transition_session(session["session_id"], SessionState.ACTIVE)
                run = store.create_run(
                    task_id=task["task_id"],
                    session_id=session["session_id"],
                    executor="contract-test",
                    request={"synthetic": True},
                )
                run = store.transition_run(run["run_id"], RunState.RUNNING)
                evidence = store.add_evidence(
                    subject_type="run", subject_id=run["run_id"], result="observed"
                )
                checkpoint = store.create_checkpoint(
                    task_id=task["task_id"], snapshot={"run_refs": [run["run_id"]]}
                )
                knowledge = store.add_knowledge(
                    project_id="contract-project",
                    scope="project",
                    classification="public",
                    kind="source",
                    title="Synthetic source",
                    content="This record exists only for contract validation.",
                    source="synthetic",
                    evidence_label="SOURCE",
                )
                for name, document in (
                    ("task", task),
                    ("session", session),
                    ("run", run),
                    ("evidence", evidence),
                    ("checkpoint", checkpoint),
                    ("knowledge", knowledge),
                ):
                    validate(document, schema(name))
                mismatched_evidence = dict(evidence)
                mismatched_evidence["subject_id"] = task["task_id"]
                with self.assertRaisesRegex(
                    ContractValidationError, "does not match subject_type"
                ):
                    validate(mismatched_evidence, schema("evidence"))

    def test_adapter_and_receipt_match_public_contracts(self) -> None:
        adapter_path = ROOT / "examples" / "project.adapter.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        validate(adapter, schema("project-adapter"))
        receipt = run_checks(
            adapter_path,
            profile="smoke",
            execute_trusted_adapter=True,
        )
        validate(receipt, schema("verification-receipt"))

        tampered_receipt = json.loads(json.dumps(receipt))
        tampered_receipt["checks"][0]["duration_ms"] += 1
        with self.assertRaisesRegex(ContractValidationError, "receipt hash mismatch"):
            validate(tampered_receipt, schema("verification-receipt"))

    def test_checkpoint_schema_rejects_snapshot_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-contract-test-") as temp_name:
            with QingtianStore(Path(temp_name) / "state.db") as store:
                store.initialize()
                task = store.create_task(
                    project_id="contract-project",
                    title="Checkpoint contract",
                    objective="Verify semantic hashes",
                    scope=["synthetic"],
                    acceptance=["tampering is rejected"],
                )
                checkpoint = store.create_checkpoint(
                    task_id=task["task_id"], snapshot={"generation": 1}
                )
                validate(checkpoint, schema("checkpoint"))
                checkpoint["snapshot"]["generation"] = 2
                with self.assertRaisesRegex(
                    ContractValidationError, "snapshot hash mismatch"
                ):
                    validate(checkpoint, schema("checkpoint"))


if __name__ == "__main__":
    unittest.main()
