from __future__ import annotations

import base64
from contextlib import redirect_stdout
from hashlib import sha256
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from scripts import repository_boundary as boundary


ROOT = Path(__file__).resolve().parents[1]
WORD = "r" + "18"


def actual_ci_program() -> str:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    match = re.search(r"          python - <<'PY'\n(.*?)\n          PY", workflow, re.S)
    if match is None:
        raise AssertionError("repository-boundary CI entry point not found")
    return textwrap.dedent(match[1])


class RepositoryBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = [
            (ROOT / "docs/diagrams" / name).read_text(encoding="utf-8")
            for name in ("engine-overview.svg", "engine-lifecycle.svg")
        ]
        cls.payloads = [payload for document in cls.documents for payload in re.findall(
            r"data:font/woff2;base64,([A-Za-z0-9+/=]+)", document
        )]
        cls.payload = next(value for value in cls.payloads if WORD in value.casefold())
        cls.program = compile(actual_ci_program(), "ci-repository-boundary", "exec")

    def svg(self, extra="", payload=None):
        return (
            '<svg xmlns="http://www.w3.org/2000/svg"><defs><style>'
            '@font-face { font-family: Xiaolai; src: url(data:font/woff2;base64,'
            + (self.payload if payload is None else payload)
            + '); }</style></defs>' + extra + '</svg>'
        )

    def run_ci(self, files):
        """Execute the unchanged Python body of the actual CI boundary step."""
        with tempfile.TemporaryDirectory(prefix="qingtian-ci-boundary-") as temporary:
            previous = Path.cwd()
            directory = Path(temporary)
            for name, content in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            try:
                os.chdir(directory)
                with patch.object(subprocess, "check_output", return_value=(
                    "\0".join(files) + "\0"
                ).encode()) as git, redirect_stdout(io.StringIO()) as output:
                    exec(self.program, {"__name__": "__main__"})
                    git.assert_called_once_with(["git", "ls-files", "-z"])
                    return output.getvalue()
            finally:
                os.chdir(previous)

    def test_all_reviewed_font_bytes_are_present_and_valid(self):
        self.assertEqual(len(self.payloads), 26)
        self.assertEqual({sha256(base64.b64decode(value)).hexdigest() for value in self.payloads},
                         boundary.REVIEWED_FONT_SHA256)
        self.assertTrue(all(boundary.reviewed_font(value) for value in self.payloads))

    def test_actual_diagrams_pass_complete_ci_boundary(self):
        files = {"docs/diagram-{}.svg".format(index): document
                 for index, document in enumerate(self.documents)}
        self.assertIn("2 tracked paths", self.run_ci(files))

    def test_valid_font_omits_only_base64_not_font_rule_or_visible_text(self):
        source = self.svg("<text>Visible generic architecture</text>")
        semantic = boundary.semantic_text("diagram.svg", source)
        self.assertIn(WORD, source.casefold())
        self.assertNotIn(WORD, semantic.casefold())
        self.assertIn("font-family: Xiaolai", semantic)
        self.assertIn("Visible generic architecture", semantic)
        self.assertIn("1 tracked paths", self.run_ci({"diagram.svg": source}))

    def test_visible_text_attributes_comments_and_xml_entities_remain_protected(self):
        for extra in ("<text>" + WORD + "</text>", "<!-- " + WORD + " -->",
                      '<g aria-label="' + WORD + '"/>',
                      "<text>r&#49;8</text>", '<g aria-label="r&#49;8"/>'):
            with self.subTest(extra=extra), self.assertRaisesRegex(SystemExit, "business-term:diagram.svg"):
                self.run_ci({"diagram.svg": self.svg(extra)})

    def test_developer_paths_are_checked_in_visible_text_and_decoded_attributes(self):
        host_path = "/" + "Users" + "/" + "tab" + "/"
        for extra in ("<text>" + host_path + "</text>",
                      '<g data-path="' + host_path.replace("/", "&#47;") + '"/>'):
            with self.subTest(extra=extra), self.assertRaisesRegex(SystemExit, "developer-path:diagram.svg"):
                self.run_ci({"diagram.svg": self.svg(extra)})

    def test_unvalidated_changed_truncated_and_forged_font_bytes_are_not_masked(self):
        data = base64.b64decode(self.payload)
        forged = bytearray(data)
        forged[14] = 1  # nonzero WOFF2 reserved header field
        changed = bytearray(data)
        changed[-1] ^= 1
        invalid = [self.payload + "!" + WORD, self.payload[:-4] + WORD,
                   base64.b64encode(data[:-1]).decode(),
                   base64.b64encode(bytes(forged)).decode(),
                   base64.b64encode(bytes(changed)).decode()]
        for payload in invalid:
            with self.subTest(length=len(payload)):
                self.assertFalse(boundary.reviewed_font(payload))
                self.assertIn(WORD, payload.casefold())
                self.assertIn(payload, boundary.semantic_text("diagram.svg", self.svg(payload=payload)))
                with self.assertRaisesRegex(SystemExit, "business-term:diagram.svg"):
                    self.run_ci({"diagram.svg": self.svg(payload=payload)})

    def test_font_header_length_and_private_data_requirements_are_enforced(self):
        original = base64.b64decode(self.payload)
        for offset in (8, 14, 28, 32, 36, 40, 44):
            mutated = bytearray(original)
            mutated[offset] ^= 1
            self.assertFalse(boundary.reviewed_font(base64.b64encode(mutated).decode()))
        self.assertFalse(boundary.reviewed_font(base64.b64encode(b"not a font").decode()))

    def test_non_svg_wrong_namespace_malformed_xml_and_entities_fail_closed(self):
        original = self.svg()
        values = [original.replace("http://www.w3.org/2000/svg", "urn:not-svg"),
                  original[:-6], '<!DOCTYPE svg [<!ENTITY value "fixture">]>' + original]
        for source in values:
            self.assertEqual(boundary.semantic_text("diagram.svg", source), source)
            with self.assertRaisesRegex(SystemExit, "business-term:diagram.svg"):
                self.run_ci({"diagram.svg": source})
        self.assertEqual(boundary.semantic_text("notes.md", original), original)

    def test_identical_payload_in_visible_text_is_not_hidden_with_font(self):
        source = self.svg("<text>" + self.payload + "</text>")
        self.assertIn(self.payload, boundary.semantic_text("diagram.svg", source))
        with self.assertRaisesRegex(SystemExit, "business-term:diagram.svg"):
            self.run_ci({"diagram.svg": source})

    def test_css_comments_extra_properties_and_xml_comments_are_not_font_context(self):
        original = self.svg()
        for source in (
            original.replace("<style>", "<style>/*\n").replace("</style>", "\n*/</style>"),
            original.replace("; }", "; extra-property: fixture; }"),
            original.replace("<style>", "<!-- <style>").replace("</style>", "</style> -->"),
        ):
            self.assertIn(self.payload, boundary.semantic_text("diagram.svg", source))
            with self.assertRaisesRegex(SystemExit, "business-term:diagram.svg"):
                self.run_ci({"diagram.svg": source})

    def test_secret_scan_uses_complete_raw_text_even_if_semantic_projection_is_empty(self):
        fake_secret = "gh" + "p_" + "a" * 32
        source = self.svg("<metadata>" + fake_secret + "</metadata>")
        with patch.object(boundary, "semantic_text", return_value=""), self.assertRaisesRegex(
            SystemExit, "github-token:diagram.svg"
        ):
            self.run_ci({"diagram.svg": source})

    def test_secret_pattern_inside_unvalidated_data_uri_is_not_bypassed(self):
        fake_secret = "AK" + "IA" + "A" * 16
        payload = self.payload + fake_secret
        with self.assertRaisesRegex(SystemExit, "aws-access-key:diagram.svg"):
            # An explicit delimiter gives the raw pattern its word boundary.
            self.run_ci({"diagram.svg": self.svg(payload=payload + ";" + fake_secret)})

    def test_private_path_and_plain_text_gates_still_execute(self):
        for files, expected in (({".data/state.txt": "fixture"}, r"\.data/state.txt"),
                                ({"README.md": WORD}, "business-term:README.md")):
            with self.subTest(files=list(files)), self.assertRaisesRegex(SystemExit, expected):
                self.run_ci(files)

    def test_helper_is_in_source_distribution_contract_and_release_allowlist(self):
        import json
        paths = json.loads((ROOT / "release-allowlist.json").read_text())["paths"]
        self.assertIn("scripts/repository_boundary.py", paths)
        self.assertIn("tests/test_repository_boundary.py", paths)
        self.assertIn("recursive-include scripts *.py *.sh", (ROOT / "MANIFEST.in").read_text())
        self.assertIn('"scripts/repository_boundary.py",',
                      (ROOT / ".github/workflows/ci.yml").read_text())


if __name__ == "__main__":
    unittest.main()
