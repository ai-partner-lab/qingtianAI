from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shlex
import unittest

from qingtian_core.bundle import SECRET_PATTERNS
from qingtian_core.capability_checks import CAPABILITY_IDS, EXPECTED_CHECK_IDS


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "qingtian_core" / "resources" / "capabilities.json"


class CapabilityCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    def assert_text_list(self, value: object) -> None:
        self.assertIsInstance(value, list)
        self.assertTrue(value)
        self.assertTrue(all(isinstance(item, str) and item.strip() for item in value))

    def test_seven_phases_and_capabilities_have_complete_unique_references(self) -> None:
        self.assertEqual(set(self.catalog), {"schema_version", "phases", "capabilities"})
        self.assertIs(type(self.catalog["schema_version"]), int)
        self.assertEqual(self.catalog["schema_version"], 1)
        phases = self.catalog["phases"]
        capabilities = self.catalog["capabilities"]
        self.assertEqual(len(phases), 7)
        self.assertEqual([phase["step"] for phase in phases], list(range(1, 8)))
        phase_ids = [phase["id"] for phase in phases]
        capability_ids = [item["id"] for item in capabilities]
        self.assertEqual(len(phase_ids), len(set(phase_ids)))
        self.assertEqual(len(capability_ids), len(set(capability_ids)))
        by_id = {item["id"]: item for item in capabilities}
        referenced = []
        for phase in phases:
            self.assertEqual(set(phase), {"id", "step", "title", "summary", "inputs", "outputs", "prerequisites", "capability_ids", "leader"})
            self.assertRegex(phase["id"], r"^[a-z][a-z0-9-]*$")
            for field in ("title", "summary"):
                self.assertIsInstance(phase[field], str)
                self.assertTrue(phase[field].strip())
            for field in ("inputs", "outputs", "prerequisites", "capability_ids"):
                self.assert_text_list(phase[field])
            self.assertIsInstance(phase["leader"], dict)
            self.assertEqual(set(phase["leader"]), {"name", "mission"})
            for value in phase["leader"].values():
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip())
            self.assertEqual(len(phase["capability_ids"]), len(set(phase["capability_ids"])))
            for capability_id in phase["capability_ids"]:
                self.assertIn(capability_id, by_id)
                self.assertEqual(by_id[capability_id]["phase_id"], phase["id"])
                referenced.append(capability_id)
        self.assertCountEqual(referenced, capability_ids)

    def test_capabilities_expose_requirements_boundaries_and_honest_run_availability(self) -> None:
        required = {"id", "phase_id", "title", "category", "availability", "runnable", "description", "prerequisites", "outputs", "boundary", "command", "agent"}
        runnable = set()
        planned = []
        for capability in self.catalog["capabilities"]:
            with self.subTest(capability=capability.get("id")):
                self.assertEqual(set(capability), required)
                self.assertRegex(capability["id"], r"^[a-z][a-z0-9-]*$")
                self.assertIs(type(capability["runnable"]), bool)
                self.assertIn(capability["availability"], {"bundled", "bundled-optional", "adapter-required", "planned"})
                for field in ("phase_id", "title", "category", "description", "boundary"):
                    self.assertIsInstance(capability[field], str)
                    self.assertTrue(capability[field].strip())
                for field in ("prerequisites", "outputs"):
                    self.assert_text_list(capability[field])
                self.assertIsInstance(capability["command"], str)
                if capability["availability"] == "planned":
                    planned.append(capability["id"])
                    self.assertFalse(capability["runnable"])
                    self.assertEqual(capability["command"], "")
                else:
                    command = shlex.split(capability["command"])
                    self.assertTrue(command)
                    self.assertIn(command[0], {"qingtian", "qingtian-kb"})
                    self.assertFalse(any(item in {";", "&&", "|"} for item in command))
                if capability["runnable"]:
                    runnable.add(capability["id"])
                    self.assertIn(capability["availability"], {"bundled", "bundled-optional"})
                    self.assertEqual(capability["phase_id"], "verify")
                    self.assertEqual(shlex.split(capability["command"]), ["qingtian", "demo-check", "--capability", capability["id"]])
        self.assertEqual(runnable, {"api-e2e", "browser-e2e"})
        self.assertTrue(planned, "future integrations must remain distinguishable from bundled capabilities")

    def test_recursive_work_trees_are_complete_bounded_and_globally_unique(self) -> None:
        used_ids = {item["id"] for collection in ("phases", "capabilities") for item in self.catalog[collection]}
        total_nodes = 0

        def walk(node: object, depth: int, mapped_checks: list[str]) -> None:
            nonlocal total_nodes
            self.assertLessEqual(depth, 4, "work-tree nesting exceeds the four-level public contract")
            self.assertIsInstance(node, dict)
            self.assertEqual(set(node), {"id", "title", "kind", "description", "check_ids", "children"})
            self.assertIsInstance(node["id"], str)
            self.assertRegex(node["id"], r"^[a-z][a-z0-9-]*$")
            self.assertNotIn(node["id"], used_ids, "node identifiers must be unique across the full catalog")
            used_ids.add(node["id"])
            total_nodes += 1
            self.assertIn(node["kind"], {"action", "check", "artifact"})
            for field in ("title", "description"):
                self.assertIsInstance(node[field], str)
                self.assertTrue(node[field].strip())
            self.assertIsInstance(node["check_ids"], list)
            self.assertIsInstance(node["children"], list)
            self.assertTrue(all(isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9._-]*", item) for item in node["check_ids"]))
            if node["check_ids"]:
                self.assertFalse(node["children"], "only leaf work nodes can bind real assertion IDs")
                self.assertEqual(node["kind"], "check", "real assertions must be clearly identified as checks")
                mapped_checks.extend(node["check_ids"])
            for child in node["children"]:
                walk(child, depth + 1, mapped_checks)

        for capability in self.catalog["capabilities"]:
            with self.subTest(capability=capability["id"]):
                agent = capability["agent"]
                self.assertIsInstance(agent, dict)
                self.assertEqual(set(agent), {"name", "mission", "work_items"})
                for field in ("name", "mission"):
                    self.assertIsInstance(agent[field], str)
                    self.assertTrue(agent[field].strip())
                self.assertIsInstance(agent["work_items"], list)
                self.assertGreaterEqual(len(agent["work_items"]), 2)
                self.assertTrue(any(item["children"] for item in agent["work_items"]), "each Agent needs an expandable work branch")
                checks: list[str] = []
                for node in agent["work_items"]:
                    walk(node, 1, checks)
                self.assertEqual(len(checks), len(set(checks)), "a real check may only map to one work leaf")
                if capability["id"] in CAPABILITY_IDS:
                    self.assertTrue(capability["runnable"])
                    self.assertEqual(set(checks), EXPECTED_CHECK_IDS[capability["id"]], "work leaves must exhaust the real runner's successful assertions")
                else:
                    self.assertEqual(checks, [], "descriptive and planned Agents cannot claim runnable assertion mappings")
        self.assertGreater(total_nodes, len(self.catalog["capabilities"]) * 2)

    def test_packaged_resources_share_the_existing_public_boundary_policy(self) -> None:
        # Reuse CI's maintained business-residue list without duplicating private
        # identifiers in this test source. Only literal string concatenation is
        # accepted; no workflow code or arbitrary Python is executed.
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        match = re.search(r"\bbusiness_terms\s*=\s*(\[.*?\])", workflow, re.DOTALL)
        self.assertIsNotNone(match, "CI must expose its public-repository residue policy")
        expression = ast.parse(match.group(1), mode="eval").body

        def literal_string(node: ast.AST) -> str:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return literal_string(node.left) + literal_string(node.right)
            raise AssertionError("business residue policy must use literal strings only")

        self.assertIsInstance(expression, ast.List)
        business_terms = [literal_string(node).casefold() for node in expression.elts]
        self.assertTrue(business_terms)
        checked = 0
        for package in ("qingtian_core", "qingtian_kb"):
            for resource in sorted((ROOT / package / "resources").rglob("*")):
                if "__pycache__" in resource.parts or not resource.is_file():
                    continue
                self.assertFalse(resource.is_symlink())
                content = resource.read_text(encoding="utf-8")
                relative = resource.relative_to(ROOT).as_posix()
                checked += 1
                with self.subTest(resource=relative):
                    self.assertFalse(any(term in content.casefold() for term in business_terms), f"business residue in resource {relative}")
                    for label, pattern in SECRET_PATTERNS.items():
                        self.assertIsNone(pattern.search(content), f"public boundary rule {label} matched resource {relative}")
        self.assertGreater(checked, 10)


if __name__ == "__main__":
    unittest.main()
