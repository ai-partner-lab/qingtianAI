"""Synthetic host/catalog bytes; the real loader and argv builder are exercised."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qingtian_engine import codex_capabilities as cc, execution_parameters as ep
from qingtian_engine.config import RuntimePolicy
from qingtian_engine.worker_entry import build_codex_command


NOW = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
OBSERVED = "2026-09-11T09:00:00Z"
EXPIRES = "2026-09-11T11:00:00Z"
CATALOG_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]
POLICY_EFFORTS = ["medium", "high", "xhigh", "ultra"]
UUID = "12e767f0-0790-4940-9aca-b87e5f6063ae"


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="synthetic-host-binding-"))
        self.root = Path(self.temp).resolve()
        self.binary = self.root / "fake-codex"
        self.binary.write_bytes(b"not executable; synthetic CLI bytes")
        self.catalog_file = self.root / "models_cache.json"
        self.file = self.root / "reviewed.json"
        self.catalog = {"client_version": "0.153.4", "fetched_at": OBSERVED,
            "models": [{"slug": model, "supported_reasoning_levels": [{"effort": e} for e in CATALOG_EFFORTS],
                        "service_tiers": [{"id": "priority"}]} for model in ("gpt-5.6-sol", "gpt-6-astra")]}
        self.manifest = {"schema_version": 2, "adapter": "codex-cli",
            "host_identity": {"kind": "macos-ioplatformuuid-sha256", "sha256": hashlib.sha256(("macos-ioplatformuuid-sha256:" + UUID).encode()).hexdigest()},
            "uid": os.geteuid(), "codex_home": str(self.root), "cli_path": str(self.binary),
            "cli_version": "0.153.4", "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            "source_kind": "codex-local-model-cache-v1", "source_ref": str(self.catalog_file),
            "observed_at": OBSERVED, "expires_at": EXPIRES,
            "models": {model: {"reasoning": POLICY_EFFORTS[:], "speed": ["standard", "fast"]} for model in ("gpt-5.6-sol", "gpt-6-astra")}}
        self.write_catalog()
        self.write_manifest()
        self.attempts = []
        def forbidden(*args, **kwargs):
            self.attempts.append("forbidden side effect")
            raise AssertionError("real socket/process/signal forbidden")
        for target in ("socket.socket", "subprocess.Popen", "os.kill"):
            self.stack.enter_context(patch(target, side_effect=forbidden))
        self.stack.enter_context(patch.dict(os.environ, {"QINGTIAN_CODEX_CAPABILITIES": str(self.file)}))
        self.stack.enter_context(patch.object(cc, "_utcnow", return_value=NOW))
        self.stack.enter_context(patch.object(cc, "_codex_home", return_value=self.root))
        self.stack.enter_context(patch.object(cc.platform, "system", return_value="Darwin"))
        self.stack.enter_context(patch.object(cc.shutil, "which", return_value=str(self.binary)))
        self.commands = []
        def local_read(command, **kwargs):
            self.commands.append(command)
            if command == ["/usr/sbin/ioreg", "-a", "-r", "-d", "1", "-c", "IOPlatformExpertDevice"]:
                return SimpleNamespace(stdout=plistlib.dumps([{"IOPlatformUUID": UUID}]))
            if command == [str(self.binary), "--version"]:
                return SimpleNamespace(stdout="codex-cli 0.153.4\n")
            raise AssertionError("unexpected read-only probe: " + repr(command))
        self.local_read = self.stack.enter_context(patch.object(cc.subprocess, "run", side_effect=local_read))

    def tearDown(self):
        self.assertEqual(self.attempts, [])

    def write_catalog(self):
        self.catalog_file.write_text(json.dumps(self.catalog))
        self.manifest["source_sha256"] = hashlib.sha256(self.catalog_file.read_bytes()).hexdigest()

    def write_manifest(self):
        self.file.write_text(json.dumps(self.manifest))

    def policy(self, model="gpt-5.6-sol", effort="high", speed="standard"):
        return RuntimePolicy(model, effort, speed, speed == "fast")

    def reject(self, message="MODEL_CAPABILITY"):
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, message):
            ep.codex_model_arguments(self.policy())

    def test_no_manifest_is_closed_before_any_probe(self):
        with patch.dict(os.environ, {"QINGTIAN_CODEX_CAPABILITIES": str(self.root / "absent")}):
            with self.assertRaisesRegex(ValueError, "no reviewed"):
                ep.codex_model_arguments(self.policy())
        self.assertEqual(self.commands, [])

    def test_explicit_prepare_is_private_inactive_and_reviewable(self):
        from qingtian_engine.capability_setup import prepare_draft
        original = self.file.read_bytes()
        output = self.root / "new-draft.json"
        result = prepare_draft(output)
        self.assertEqual("draft_only", result["status"])
        self.assertFalse(result["enabled"])
        self.assertEqual(0o600, output.stat().st_mode & 0o777)
        self.assertEqual(original, self.file.read_bytes())
        self.assertEqual(str(self.file), os.environ["QINGTIAN_CODEX_CAPABILITIES"])
        self.assertNotIn(str(self.root), json.dumps(result))
        with patch.dict(os.environ, {"QINGTIAN_CODEX_CAPABILITIES": str(output)}):
            reviewed = cc.load_codex_capabilities()
        self.assertEqual(set(cc.FIELDS), set(reviewed))
        self.assertEqual(self.manifest["source_sha256"], reviewed["source_sha256"])
        self.assertEqual({tuple(command) for command in self.commands}, {
            (str(self.binary), "--version"), ("/usr/sbin/ioreg", "-a", "-r", "-d", "1", "-c", "IOPlatformExpertDevice")})

    def test_prepare_never_overwrites_or_activates(self):
        from qingtian_engine.capability_setup import prepare_draft
        original = self.file.read_bytes()
        with self.assertRaisesRegex(ValueError, "separate draft"):
            prepare_draft(self.file)
        output = self.root / "existing.json"
        output.write_text("keep me")
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_draft(output)
        self.assertEqual("keep me", output.read_text())
        self.assertEqual(original, self.file.read_bytes())
        self.assertEqual([], self.commands)

    def test_prepare_stale_or_bad_catalog_leaves_no_draft(self):
        from qingtian_engine.capability_setup import prepare_draft
        output = self.root / "stale-draft.json"
        self.catalog["fetched_at"] = "2001-01-01T00:00:00Z"
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "stale"):
            prepare_draft(output)
        self.assertFalse(output.exists())

    def test_status_has_no_probe_when_unconfigured(self):
        import io
        from contextlib import redirect_stdout
        from qingtian_engine.capability_setup import main
        stream = io.StringIO()
        with patch.dict(os.environ, {"QINGTIAN_CODEX_CAPABILITIES": str(self.root / "absent")}), redirect_stdout(stream):
            self.assertEqual(1, main(["status"]))
        self.assertEqual("unavailable", json.loads(stream.getvalue())["status"])
        self.assertEqual([], self.commands)

    def test_original_qa_three_regressions_reject(self):
        # The former QA payloads and real assertRaises contract are preserved.
        old = {"schema_version": 1, "adapter": "codex-cli", "source_ref": "independent-fixture", "cli_version": "synthetic",
               "binary_sha256": self.manifest["binary_sha256"], "models": deepcopy(self.manifest["models"])}
        for fields in ({}, {"host_id": "synthetic-host", "observed_at": "2000-01-01T00:00:00Z", "expires_at": "2000-01-02T00:00:00Z"},
                       {"source_ref": "https://developers.openai.com/api/docs/models/gpt-5.6-sol", "source_kind": "api_documentation"}):
            with self.subTest(fields=fields):
                self.manifest = dict(old, **fields)
                self.reject()

    def test_each_required_field_missing_rejects(self):
        good = deepcopy(self.manifest)
        for key in cc.FIELDS:
            with self.subTest(key=key):
                self.manifest = deepcopy(good)
                del self.manifest[key]
                self.reject()

    def test_wrong_schema_and_extra_fields_reject(self):
        for version in (1, 3, True, "2", 2.0, None):
            with self.subTest(version=version):
                self.manifest["schema_version"] = version
                self.reject()
        self.manifest["schema_version"] = 2
        self.manifest["host_id"] = "cannot self-assert identity"
        self.reject()

    def test_different_host_rejects_same_binary(self):
        self.manifest["host_identity"]["sha256"] = "f" * 64
        self.reject("different OS-install")

    def test_hostname_or_machine_model_is_not_identity(self):
        self.manifest["host_identity"] = {"kind": "hostname", "sha256": "MacBook"}
        self.reject("different OS-install")

    def test_matching_host_valid_source_exact_tuple_fresh_and_resume(self):
        for model in self.manifest["models"]:
            for effort in POLICY_EFFORTS:
                for speed in ("standard", "fast"):
                    for resume in (False, True):
                        with self.subTest(model=model, effort=effort, speed=speed, resume=resume):
                            task = dict(model=model, reasoning=effort, speed=speed, worktree="/synthetic/project")
                            command = build_codex_command(task, "captured-session", resume)
                            self.assertEqual(command[0], str(self.binary))
                            self.assertEqual(command[command.index("-m") + 1], model)
                            self.assertIn('model_reasoning_effort="' + effort + '"', command)
                            self.assertIn('service_tier="' + ("priority" if speed == "fast" else "default") + '"', command)
                            self.assertEqual("resume" in command, resume)

    def test_wrong_adapter_and_nonlocal_sources_reject(self):
        self.manifest["adapter"] = "openai-api"
        self.reject()
        self.manifest["adapter"] = "codex-cli"
        for kind in ("api_documentation", "codex-help", "operator-claim", "app-server", ""):
            with self.subTest(kind=kind):
                self.manifest["source_kind"] = kind
                self.reject()

    def test_wrong_cli_binary_path_or_version_reject(self):
        good = deepcopy(self.manifest)
        for key, value in (("binary_sha256", "0" * 64), ("cli_path", "/other/codex"), ("cli_version", "0.999.0")):
            with self.subTest(key=key):
                self.manifest = dict(good, **{key: value})
                self.reject("actual CLI")

    def test_actual_binary_replaced_reject(self):
        self.binary.write_bytes(b"different actual bytes")
        self.reject("actual CLI")

    def test_missing_cli_reject(self):
        with patch.object(cc.shutil, "which", return_value=None):
            self.reject("CLI is unavailable")

    def test_unreadable_or_unsupported_identity_reject(self):
        with patch.object(cc.platform, "system", return_value="UnsupportedOS"):
            self.reject("cannot independently verify")
        with patch.object(cc.subprocess, "run", side_effect=subprocess.TimeoutExpired("ioreg", 5)):
            self.reject("cannot independently verify")
        self.local_read.side_effect = lambda *a, **k: SimpleNamespace(stdout=plistlib.dumps([{"IOPlatformUUID": "00000000-0000-0000-0000-000000000000"}]))
        self.reject("cannot independently verify")

    def test_wrong_user_or_codex_home_reject(self):
        self.manifest["uid"] = os.geteuid() + 1
        self.reject("effective user")
        self.manifest["uid"] = os.geteuid()
        self.manifest["codex_home"] = "/another/home"
        self.reject("effective user")

    def test_expired_and_future_observation_reject(self):
        for observed, expires in (("2000-01-01T00:00:00Z", "2000-01-02T00:00:00Z"),
                ("2026-09-11T10:00:00.000001Z", EXPIRES), (OBSERVED, "2026-09-11T10:00:00Z"),
                (OBSERVED, OBSERVED), (OBSERVED, "2026-09-12T09:00:00.000001Z")):
            with self.subTest(observed=observed, expires=expires):
                self.manifest.update(observed_at=observed, expires_at=expires)
                self.reject("future, expired")

    def test_invalid_and_timezone_less_timestamp_reject(self):
        for value in ("2026-09-11", "2026-09-11T09:00:00", "2026-13-11T09:00:00Z", "2026-09-11T09:00:00+25:00", "2026-09-11T09:00:00-00:00", 1, None):
            for key in ("observed_at", "expires_at"):
                with self.subTest(value=value, key=key):
                    self.manifest.update(observed_at=OBSERVED, expires_at=EXPIRES)
                    self.manifest[key] = value
                    self.reject("timestamp")

    def test_timezone_offset_is_normalized_without_changing_instant(self):
        self.manifest.update(observed_at="2026-09-11T17:00:00+08:00", expires_at="2026-09-11T19:00:00+08:00")
        self.write_manifest()
        self.assertEqual(ep.require_codex_capability(self.policy()), self.policy())

    def test_cannot_retimestamp_old_catalog_as_new(self):
        self.catalog["fetched_at"] = "2000-01-01T00:00:00Z"
        self.write_catalog()
        self.reject("observation time")

    def test_current_catalog_hash_and_cli_version_must_match(self):
        self.manifest["source_sha256"] = "0" * 64
        self.reject("catalog has changed")
        self.catalog["client_version"] = "0.999.0"
        self.write_catalog()
        self.reject("catalog version")

    def test_arbitrary_file_or_api_url_cannot_substitute_for_local_catalog(self):
        for source in ("https://developers.openai.com/api/docs/models/gpt-5.6-sol", str(self.root / "copy.json")):
            with self.subTest(source=source):
                self.manifest["source_ref"] = source
                self.reject("fixed local model-cache path")

    def test_missing_or_malformed_catalog_reject(self):
        self.catalog_file.write_text("not json")
        self.manifest["source_sha256"] = hashlib.sha256(self.catalog_file.read_bytes()).hexdigest()
        self.reject()
        self.catalog_file.unlink()
        self.reject()

    def test_catalog_no_fast_cannot_be_widened_by_manifest(self):
        self.catalog["models"][0]["service_tiers"] = []
        self.write_catalog()
        self.reject("exceed")
        self.manifest["models"]["gpt-5.6-sol"]["speed"] = ["standard"]
        self.write_manifest()
        self.assertEqual(ep.require_codex_capability(self.policy()), self.policy())
        with self.assertRaisesRegex(ValueError, "exact requested"):
            ep.codex_model_arguments(self.policy(speed="fast"))

    def test_review_can_narrow_but_never_widen_effort(self):
        self.manifest["models"]["gpt-5.6-sol"]["reasoning"] = ["high"]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "exact requested"):
            ep.codex_model_arguments(self.policy(effort="ultra"))
        self.catalog["models"][0]["supported_reasoning_levels"] = [{"effort": "max"}]
        self.write_catalog()
        self.reject("exceed")

    def test_empty_duplicate_or_string_capabilities_reject(self):
        good = deepcopy(self.manifest)
        for values in ([], "high", ["high", "high"], [None], ["unknown"]):
            with self.subTest(values=values):
                self.manifest = deepcopy(good)
                self.manifest["models"]["gpt-5.6-sol"]["reasoning"] = values
                self.reject()

    def test_raw_capability_injection_removed_from_all_production_entries(self):
        unverified = {"adapter": "codex-cli", "models": deepcopy(self.manifest["models"])}
        for function in (ep.require_codex_capability, ep.codex_model_arguments):
            with self.subTest(function=function.__name__), self.assertRaises(TypeError):
                function(self.policy(), capabilities=unverified)
        with self.assertRaises(TypeError):
            build_codex_command(dict(model="gpt-5.6-sol", reasoning="high", speed="standard", worktree="/synthetic"), capabilities=unverified)

    def test_speed_flag_cannot_override_exact_speed(self):
        with self.assertRaisesRegex(ValueError, "speed and fast flag"):
            ep.codex_model_arguments(RuntimePolicy("gpt-5.6-sol", "high", "standard", True))

    def test_duplicate_json_fields_are_ambiguous_and_rejected(self):
        raw = self.file.read_text()
        self.file.write_text(raw[:-1] + ', "adapter": "codex-cli"}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            ep.codex_model_arguments(self.policy())

    def test_linux_identity_uses_machine_id_not_hostname(self):
        raw = "f9c15f5a4992458795787a75e02b5403"
        with patch.object(cc.platform, "system", return_value="Linux"), patch.object(Path, "read_text", return_value=raw) as read:
            identity = cc._host_identity()
            self.assertEqual(identity, {"kind": "linux-machine-id-sha256", "sha256": hashlib.sha256(("linux-machine-id-sha256:" + raw).encode()).hexdigest()})
            self.assertEqual(read.call_args.kwargs, {"encoding": "ascii"})
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
