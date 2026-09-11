"""Isolated manager onboarding tests: no credentials, model calls or business state."""
from contextlib import redirect_stdout
from copy import deepcopy
import http.client
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from qingtian_engine import manager_entry as entry
from qingtian_engine.entrypoint import main


class FakeServer:
    def __init__(self, workspace):
        self.workspace = str(workspace)
        self.threads = {}
        self.calls = []
        self.failures = {}
        self.pin_field = True
        self.apply_pin = True
        self.pages = None
        self.lost_create = False

    def __call__(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def add(self, thread_id, *, name=entry.MANAGER_NAME, cwd=None, pinned=False):
        self.threads[thread_id] = {
            "id": thread_id, "name": name, "cwd": cwd or self.workspace,
            "ephemeral": False, "isPinned": pinned,
        }
        return self.threads[thread_id]

    def call(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method in self.failures:
            raise self.failures[method]
        if method == "thread/list":
            if self.pages is not None:
                return deepcopy(self.pages[params["cursor"]])
            return {"data": [deepcopy(t) for t in self.threads.values()
                             if t["name"] == entry.MANAGER_NAME], "nextCursor": None}
        if method == "thread/start":
            thread = self.add("new-" + str(len(self.threads)), name=None)
            if self.lost_create:
                raise entry.EntryError("timeout", method=method)
            return {"thread": deepcopy(thread)}
        thread = self.threads.get(params["threadId"])
        if thread is None:
            raise entry.EntryError("rpc_error", method=method, rpc_code=-32000)
        if method == "thread/name/set":
            thread["name"] = params["name"]
            return {}
        if method == "thread/metadata/update":
            assert params["isPinned"] is True
            if self.apply_pin:
                thread["isPinned"] = True
            # A successful write response alone is deliberately not evidence.
            return {"thread": {**deepcopy(thread), "isPinned": True}}
        if method == "thread/read":
            assert params["includeTurns"] is False
            result = deepcopy(thread)
            if not self.pin_field:
                result.pop("isPinned", None)
            return {"thread": result}
        raise AssertionError("Unexpected/model operation: " + method)

    def count(self, method):
        return sum(item[0] == method for item in self.calls)


class ManagerEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.remote = FakeServer(self.workspace)

    def initialize(self, **kwargs):
        return entry.initialize_entry(self.data, self.workspace, client_factory=self.remote, **kwargs)

    def test_status_is_read_only_and_never_spawns(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no process")):
            self.assertEqual(entry.read_status(self.data)["status"], "not_initialized")
        self.assertFalse(self.data.exists())

    def test_create_rename_pin_readback_and_repeat_are_idempotent(self):
        first = self.initialize()
        second = self.initialize()
        self.assertEqual(first["status"], "partial")
        self.assertTrue(first["metadata_ready"])
        self.assertFalse(first["workflow_ready"])
        self.assertEqual(first["error_code"], "role_unverified")
        self.assertEqual(first["role_configuration"]["source"], "thread/start")
        self.assertFalse(first["role_configuration"]["verified"])
        self.assertEqual(first["role_configuration"], second["role_configuration"])
        self.assertEqual(first["thread_id"], second["thread_id"])
        self.assertEqual(first["pinned_at"], second["pinned_at"])
        for method in ("thread/start", "thread/name/set", "thread/metadata/update"):
            self.assertEqual(self.remote.count(method), 1)
        self.assertNotIn("identity", first)
        self.assertTrue(first["available"])
        self.assertIs(first["pinned"], True)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(entry._state_path(self.data).stat().st_mode), 0o600)

    def test_paginated_reuse_enforces_exact_title_and_workspace(self):
        wanted = self.remote.add("wanted")
        outside = self.remote.add("outside", cwd=str(self.root / "other"))
        similar = self.remote.add("similar", name=entry.MANAGER_NAME + " old")
        self.remote.pages = {
            None: {"data": [outside, similar], "nextCursor": "page2"},
            "page2": {"data": [wanted], "nextCursor": None},
        }
        result = self.initialize()
        self.assertEqual(result["thread_id"], "wanted")
        self.assertEqual(self.remote.count("thread/start"), 0)
        self.assertFalse(self.remote.threads["outside"]["isPinned"])

    def test_multiple_matches_require_explicit_selection(self):
        self.remote.add("a")
        self.remote.add("b")
        result = self.initialize()
        self.assertEqual(result["error_code"], "ambiguous")
        self.assertEqual(set(result["candidate_ids"]), {"a", "b"})
        self.assertEqual(self.remote.count("thread/metadata/update"), 0)
        selected = self.initialize(thread_id="b")
        self.assertEqual(selected["status"], "partial")
        self.assertTrue(selected["metadata_ready"])
        self.assertEqual(selected["role_configuration"]["source"], "unknown")
        self.assertFalse(self.remote.threads["a"]["isPinned"])
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_lost_create_response_never_retries_creation(self):
        self.remote.lost_create = True
        first = self.initialize()
        self.assertTrue(first["creation_pending"])
        second = self.initialize()
        self.assertEqual(second["error_code"], "creation_uncertain")
        self.assertEqual(self.remote.count("thread/start"), 1)
        recovered = self.initialize(thread_id="new-0")
        self.assertEqual(recovered["status"], "partial")
        self.assertTrue(recovered["metadata_ready"])
        self.assertEqual(recovered["role_configuration"]["source"], "unknown")
        self.assertFalse(recovered["creation_pending"])

    def test_rename_failure_keeps_real_id_for_retry(self):
        self.remote.failures["thread/name/set"] = entry.EntryError("rpc_error", method="thread/name/set")
        first = self.initialize()
        self.assertEqual(first["thread_id"], "new-0")
        self.remote.failures.clear()
        self.assertTrue(self.initialize()["metadata_ready"])
        self.assertEqual(self.remote.count("thread/start"), 1)

    def test_successful_write_without_pin_readback_is_not_success(self):
        self.remote.apply_pin = False
        result = self.initialize()
        self.assertEqual(result["status"], "partial")
        self.assertIs(result["pinned"], False)
        self.assertEqual(result["error_code"], "pin_not_applied")

    def test_old_version_ignoring_unknown_pin_field_is_explicit(self):
        self.remote.pin_field = False
        result = self.initialize()
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["pinned"])
        self.assertEqual(result["error_code"], "pin_unverified")
        self.assertTrue(result["available"])

    def test_unsupported_pin_does_not_lose_thread_or_invent_pin(self):
        self.remote.failures["thread/metadata/update"] = entry.EntryError(
            "unsupported", method="thread/metadata/update", rpc_code=-32601)
        result = self.initialize()
        self.assertEqual(result["status"], "partial")
        self.assertIs(result["pin_supported"], False)
        self.assertEqual(result["thread_id"], "new-0")
        self.assertIs(result["pinned"], False)

    def test_ignored_pin_field_rejected_as_empty_patch_remains_unverified(self):
        self.remote.pin_field = False
        self.remote.failures["thread/metadata/update"] = entry.EntryError(
            "rpc_error", method="thread/metadata/update", rpc_code=-32600)
        result = self.initialize()
        self.assertEqual(result["error_code"], "pin_unverified")
        self.assertEqual(result["rpc_code"], -32600)
        self.assertIsNone(result["pinned"])
        self.assertTrue(result["available"])

    def test_explicit_rebinding_has_new_binding_and_pin_timestamps(self):
        first = self.initialize()
        self.remote.add("selected")
        second = self.initialize(thread_id="selected")
        self.assertEqual(second["status"], "partial")
        self.assertTrue(second["metadata_ready"])
        self.assertEqual(second["role_configuration"]["source"], "unknown")
        self.assertNotEqual(first["bound_at"], second["bound_at"])
        self.assertNotEqual(first["pinned_at"], second["pinned_at"])

    def test_invalid_environment_is_visible_and_recoverable(self):
        with patch.dict(os.environ, {"QINGTIAN_CODEX_TRANSPORT": "invalid"}):
            result = entry.initialize_from_environment(self.data, self.workspace)
        self.assertEqual(result["error_code"], "invalid_config")
        self.assertEqual(entry.read_status(self.data)["error_code"], "invalid_config")
        self.assertTrue(self.initialize()["metadata_ready"])

    def test_failed_resync_keeps_old_observation_time_but_not_live_flags(self):
        first = self.initialize()
        self.remote.failures["thread/read"] = entry.EntryError("connection_closed", method="thread/read")
        second = self.initialize()
        self.assertFalse(second["available"])
        self.assertIsNone(second["pinned"])
        self.assertTrue(second["stale"])
        self.assertEqual(first["last_synced_at"], second["last_synced_at"])
        self.assertEqual(self.remote.count("thread/start"), 1)

    def test_missing_bound_thread_does_not_create_a_replacement(self):
        self.initialize()
        self.remote.threads.clear()
        self.assertEqual(self.initialize()["error_code"], "rpc_error")
        self.assertEqual(self.remote.count("thread/start"), 1)

    def test_incomplete_listing_never_creates(self):
        self.remote.pages = {None: {"data": [], "nextCursor": "repeat"},
                             "repeat": {"data": [], "nextCursor": "repeat"}}
        self.assertEqual(self.initialize()["error_code"], "scan_incomplete")
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_lock_contention_never_contacts_remote(self):
        with entry._lock(self.data):
            result = self.initialize()
        self.assertEqual(result["error_code"], "busy")
        self.assertEqual(self.remote.calls, [])

    def test_workspace_or_transport_change_never_rebinds(self):
        self.initialize()
        before = entry._state_path(self.data).read_bytes()
        other = self.root / "other"
        other.mkdir()
        result = entry.initialize_entry(self.data, other, client_factory=Mock(side_effect=AssertionError("no remote")))
        self.assertEqual(result["error_code"], "scope_mismatch")
        self.assertEqual(self.initialize(command=["other-codex", "app-server"])["error_code"], "scope_mismatch")
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_role_of_existing_pinned_thread_is_unknown_without_mutations(self):
        self.remote.add("existing", pinned=True)
        for kwargs in ({}, {"thread_id": "existing"}):
            result = self.initialize(**kwargs)
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["metadata_ready"])
            self.assertFalse(result["workflow_ready"])
            self.assertEqual(result["role_configuration"], entry._unknown_role())
        self.assertEqual({method for method, _ in self.remote.calls}, {"thread/list", "thread/read"})

    def test_legacy_ready_snapshot_is_not_role_verification(self):
        self.initialize()
        state = entry._load(self.data)
        state["status"] = "ready"
        state.pop("role_configuration")
        state.pop("metadata_ready")
        state.pop("workflow_ready")
        entry._save(self.data, state)
        before = entry._state_path(self.data).read_bytes()
        result = entry.read_status(self.data, workspace=self.workspace)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error_code"], "role_unverified")
        self.assertTrue(result["metadata_ready"])
        self.assertFalse(result["role_configuration"]["verified"])
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_readonly_workspace_home_and_transport_scope_mismatch(self):
        self.initialize()
        before = entry._state_path(self.data).read_bytes()
        cases = [({"workspace": self.root / "other"}, {}),
                 ({"workspace": self.workspace}, {"CODEX_HOME": str(self.root / "other-home")}),
                 ({"workspace": self.workspace}, {"QINGTIAN_CODEX_TRANSPORT": "proxy"})]
        with patch("subprocess.Popen", side_effect=AssertionError("read-only")):
            for kwargs, environment in cases:
                with self.subTest(environment=environment, workspace=str(kwargs["workspace"])):
                    with patch.dict(os.environ, environment):
                        result = entry.read_status(self.data, **kwargs)
                    self.assertEqual(result["error_code"], "scope_mismatch")
                    self.assertFalse(result["available"])
                    self.assertFalse(result["metadata_ready"])
                    self.assertFalse(result["scope_valid"])
                    self.assertFalse(result["workflow_ready"])
                    self.assertTrue(result["stale"])
                    self.assertIsNone(result["pinned"])
                    self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_status_cli_checks_explicit_workspace_and_transport_without_writes(self):
        self.initialize()
        before = entry._state_path(self.data).read_bytes()
        with patch("subprocess.Popen", side_effect=AssertionError("status")):
            for options in (["--workspace", str(self.root / "other")],
                            ["--workspace", str(self.workspace), "--transport", "proxy"]):
                with redirect_stdout(StringIO()) as output:
                    result = main(["manager-entry", "status", "--data-dir", str(self.data), *options])
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(output.getvalue())["error_code"], "scope_mismatch")
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_invalid_new_transport_does_not_rewrite_existing_binding(self):
        self.initialize()
        before = entry._state_path(self.data).read_bytes()
        with patch.dict(os.environ, {"QINGTIAN_CODEX_TRANSPORT": "invalid"}):
            with patch("subprocess.Popen", side_effect=AssertionError("invalid config")):
                result = entry.initialize_from_environment(self.data, self.workspace)
                projected = entry.read_status(self.data, workspace=self.workspace)
        self.assertEqual(result["error_code"], "invalid_config")
        self.assertEqual(projected["error_code"], "invalid_config")
        self.assertFalse(projected["available"])
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_role_instructions_export_is_read_only(self):
        with patch("subprocess.Popen", side_effect=AssertionError("instructions")):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["manager-entry", "instructions", "--data-dir", str(self.data)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["role_instructions"], entry.MANAGER_INSTRUCTIONS)
        self.assertFalse(result["role_configuration"]["verified"])
        self.assertFalse(self.data.exists())

    def test_corrupt_state_is_preserved_and_blocks_creation(self):
        path = entry._state_path(self.data)
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        result = self.initialize()
        self.assertEqual(result["error_code"], "invalid_state")
        self.assertEqual(path.read_text(), "{broken")
        self.assertEqual(self.remote.calls, [])

    def test_state_write_failure_prevents_create(self):
        with patch.object(entry, "_save", side_effect=OSError("private path / secret")):
            result = self.initialize()
        self.assertEqual(result["error_code"], "io_error")
        self.assertEqual(self.remote.count("thread/start"), 0)
        self.assertNotIn("secret", json.dumps(result))

    def test_sync_does_not_create_an_unbound_entry(self):
        self.assertEqual(self.initialize(allow_create=False)["error_code"], "not_found")
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_quickstart_survives_unavailable_entry_and_skip_has_no_remote_call(self):
        with patch("qingtian_engine.cli.build_service"), patch("qingtian_engine.cli.start_server", return_value=0) as start:
            with patch.object(entry, "initialize_from_environment", return_value={"status": "unavailable"}) as initialize:
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(["quickstart", "--manager-entry", "--data-dir", str(self.data), "--workspace", str(self.workspace)]), 0)
                initialize.assert_called_once()
                start.assert_called_once()
            with patch.object(entry, "initialize_from_environment", side_effect=AssertionError("skip")):
                self.assertEqual(main(["quickstart", "--skip-manager-entry", "--data-dir", str(self.data)]), 0)
                self.assertEqual(main(["quickstart", "--data-dir", str(self.data)]), 0)

    def test_tour_does_not_initialize_manager(self):
        with patch("qingtian_engine.cli.build_service"), patch("qingtian_engine.cli.create_demo"), patch("qingtian_engine.server.serve"):
            with patch.object(entry, "initialize_from_environment", side_effect=AssertionError("tour")):
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(["tour"]), 0)

    def test_http_health_and_dashboard_only_read_snapshot(self):
        from qingtian_engine.db import Database
        from qingtian_engine.service import ControlPlane
        from qingtian_engine.server import ControlPlaneHandler, LoopbackThreadingHTTPServer
        self.initialize()
        service = ControlPlane(Database(self.data / "control-plane.sqlite3"), manager_entry_workspace=self.workspace)
        handler = type("IsolatedEntryHandler", (ControlPlaneHandler,), {
            "service": service, "manager": Mock(data_dir=self.data), "coordinator": None,
            "watchdog_health": {"_last_success_monotonic": time.monotonic()},
            "workspace": str(self.workspace), "synthetic_tour": False,
        })
        with patch("subprocess.Popen", side_effect=AssertionError("GET must not spawn")):
            server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for path in ("/api/health", "/api/dashboard"):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read())
                    connection.close()
                    self.assertEqual(payload["manager_entry"]["thread_id"], "new-0")
                    self.assertEqual(payload["manager_entry"]["observation"], "last_sync")
                self.assertEqual(service.list_tasks(), [])
                before = entry._state_path(self.data).read_bytes()
                service.manager_entry_workspace = self.root / "different-workspace"
                for path in ("/api/health", "/api/dashboard"):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    connection.close()
                    self.assertEqual(payload["manager_entry"]["error_code"], "scope_mismatch")
                    self.assertFalse(payload["manager_entry"]["available"])
                    self.assertTrue(payload["manager_entry"]["stale"])
                self.assertEqual(entry._state_path(self.data).read_bytes(), before)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


class SectionServer(FakeServer):
    """Model 0.153.4's no-preview discovery gap without a model or database."""
    server_version = "0.153.4"

    def __init__(self, workspace):
        super().__init__(workspace)
        self.pin_field = False
        self.sections = [{"id": entry.BUILTIN_PINNED_SECTION_ID, "name": "Pinned"}]
        self.section_pages = None
        self.view_pages = {}
        self.apply_section_move = True
        self.expose_section = True

    def add(self, thread_id, *, section=None, preview="", **kwargs):
        thread = super().add(thread_id, **kwargs)
        thread.pop("isPinned")
        thread.update(section=deepcopy(section), preview=preview)
        return thread

    def call(self, method, params):
        if method in {"threadSection/list", "thread/list", "thread/section/move"}:
            self.calls.append((method, deepcopy(params)))
            if method in self.failures:
                raise self.failures[method]
            if method == "threadSection/list":
                if self.section_pages is not None:
                    return deepcopy(self.section_pages[params["cursor"]])
                return {"data": deepcopy(self.sections), "nextCursor": None}
            if method == "thread/list":
                view = params.get("sectionId", "all")
                if view in self.view_pages:
                    return deepcopy(self.view_pages[view][params["cursor"]])
                values = []
                for thread in self.threads.values():
                    section_id = (thread.get("section") or {}).get("id")
                    if view == "all" or view is None:
                        if not thread.get("preview") or (view is None and section_id is not None):
                            continue
                    elif section_id != view:
                        continue
                    values.append(deepcopy(thread))
                return {"data": values, "nextCursor": None}
            if self.apply_section_move:
                self.threads[params["threadId"]]["section"] = deepcopy(next(
                    s for s in self.sections if s["id"] == params["sectionId"]))
            return {}
        result = super().call(method, params)
        if method == "thread/read" and not self.expose_section:
            result["thread"].pop("section", None)
        return result


class SectionCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.remote = SectionServer(self.workspace)
        self.builtin = deepcopy(self.remote.sections[0])

    def initialize(self, **kwargs):
        return entry.initialize_entry(self.data, self.workspace, client_factory=self.remote, **kwargs)

    def test_bound_section_pin_readback_and_repeat_do_not_reorder_or_create(self):
        self.remote.add("selected")
        first = self.initialize(thread_id="selected")
        second = self.initialize()
        for result in (first, second):
            self.assertTrue(result["metadata_ready"])
            self.assertTrue(result["pinned"])
            self.assertEqual(result["pin_evidence_source"], "builtin_section")
            self.assertEqual(result["pin_builtin_section_id"], self.builtin["id"])
            self.assertEqual(result["pin_backend_version"], "0.153.4")
            self.assertEqual(result["error_code"], "role_unverified")
            self.assertFalse(result["workflow_ready"])
            self.assertEqual(result["role_configuration"], entry._unknown_role())
        self.assertEqual(self.remote.count("thread/section/move"), 1)
        self.assertEqual(self.remote.count("thread/metadata/update"), 0)
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_fresh_binding_discovers_pinned_record_and_deduplicates_views(self):
        for preview in ("", "visible preview"):
            with self.subTest(preview=preview):
                self.data = self.root / ("hidden" if not preview else "visible")
                self.remote.add("existing", section=self.builtin, preview=preview)
                result = self.initialize()
                self.assertEqual(result["thread_id"], "existing")
                self.assertTrue(result["metadata_ready"])
        self.assertEqual(self.remote.count("thread/start"), 0)
        self.assertEqual(self.remote.count("thread/section/move"), 0)
        lists = [params for method, params in self.remote.calls if method == "thread/list"]
        self.assertTrue(any("sectionId" not in p for p in lists))
        self.assertTrue(any("sectionId" in p and p["sectionId"] is None for p in lists))
        self.assertTrue(any(p.get("sectionId") == self.builtin["id"] for p in lists))
        self.assertTrue(all(p["modelProviders"] == [] for p in lists))

    def test_catalog_and_pinned_pages_are_exhausted_before_reuse(self):
        custom = {"id": "fixture-custom", "name": "Personal"}
        self.remote.section_pages = {
            None: {"data": [custom], "nextCursor": "sections2"},
            "sections2": {"data": [self.builtin], "nextCursor": None},
        }
        wanted = self.remote.add("wanted", section=self.builtin)
        self.remote.view_pages[self.builtin["id"]] = {
            None: {"data": [], "nextCursor": "pinned2"},
            "pinned2": {"data": [wanted], "nextCursor": None},
        }
        result = self.initialize()
        self.assertEqual(result["thread_id"], "wanted")
        self.assertTrue(result["metadata_ready"])
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_custom_sections_are_scanned_without_treating_their_name_as_pinning(self):
        custom = {"id": "fixture-custom", "name": "Pinned"}
        self.remote.sections.append(custom)
        self.remote.add("wanted", section=custom)
        result = self.initialize()
        self.assertTrue(result["metadata_ready"])
        move = next(p for m, p in self.remote.calls if m == "thread/section/move")
        self.assertEqual(move, {"threadId": "wanted", "sectionId": self.builtin["id"]})
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_ambiguity_across_sections_never_creates_or_moves(self):
        custom = {"id": "fixture-custom", "name": "Personal"}
        self.remote.sections.append(custom)
        self.remote.add("first", section=self.builtin)
        self.remote.add("second", section=custom)
        result = self.initialize()
        self.assertEqual(result["error_code"], "ambiguous")
        self.assertEqual(set(result["candidate_ids"]), {"first", "second"})
        self.assertEqual(self.remote.count("thread/start"), 0)
        self.assertEqual(self.remote.count("thread/section/move"), 0)

    def test_incomplete_pinned_scan_blocks_reuse_and_creation(self):
        wanted = self.remote.add("wanted", section=self.builtin)
        self.remote.view_pages[self.builtin["id"]] = {
            None: {"data": [wanted], "nextCursor": "repeat"},
            "repeat": {"data": [], "nextCursor": "repeat"},
        }
        self.assertEqual(self.initialize()["error_code"], "scan_incomplete")
        self.assertEqual(self.remote.count("thread/start"), 0)
        self.assertEqual(self.remote.count("thread/section/move"), 0)

    def test_catalog_failure_and_repeated_cursor_never_create(self):
        self.remote.section_pages = {None: {"data": [], "nextCursor": "repeat"},
                                     "repeat": {"data": [], "nextCursor": "repeat"}}
        self.assertEqual(self.initialize()["error_code"], "scan_incomplete")
        self.remote.section_pages = {None: {"data": [{"id": "", "name": "Pinned"}]}}
        self.assertEqual(self.initialize()["error_code"], "protocol_error")
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_empty_or_invisible_unsectioned_results_cannot_authorize_create(self):
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                if hidden:
                    self.remote.add("invisible-no-turn")
                result = self.initialize()
                self.assertEqual(result["error_code"], "scan_incomplete")
                self.assertFalse(result["creation_pending"])
                self.assertEqual(self.remote.count("thread/start"), 0)

    def test_visibility_gap_preserves_creation_pending_journal(self):
        with entry._lock(self.data):
            state = entry._empty()
            state.update(identity=entry._identity(self.workspace.resolve(), [os.environ.get("QINGTIAN_CODEX_BIN", "codex"), "app-server"]),
                         creation_pending=True)
            entry._save(self.data, state)
        result = self.initialize()
        self.assertTrue(result["creation_pending"])
        self.assertEqual(result["error_code"], "scan_incomplete")
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_unknown_backend_never_uses_local_launcher_version_or_section_fallback(self):
        self.remote.add("selected", section=self.builtin)
        self.remote.failures["thread/metadata/update"] = entry.EntryError(
            "rpc_error", method="thread/metadata/update", rpc_code=-32600)
        for version in (None, "0.153.5", "0.153.4-custom"):
            with self.subTest(version=version):
                self.remote.server_version = version
                result = self.initialize(thread_id="selected", command=["codex-0.153.4", "app-server", "proxy"])
                self.assertIsNone(result["pinned"])
                self.assertIsNone(result["pin_evidence_source"])
                self.assertEqual(result["error_code"], "pin_unverified")
        self.assertEqual(self.remote.count("threadSection/list"), 0)
        self.assertEqual(self.remote.count("thread/section/move"), 0)

    def test_missing_builtin_identity_cannot_be_replaced_by_custom_pinned_name(self):
        self.remote.sections = [{"id": "fixture-custom", "name": "Pinned"}]
        self.remote.add("selected", section=self.builtin)
        result = self.initialize(thread_id="selected")
        self.assertEqual(result["error_code"], "pin_unverified")
        self.assertIsNone(result["pinned"])
        self.assertIsNone(result["pin_builtin_section_id"])
        self.assertEqual(self.remote.count("thread/section/move"), 0)

    def test_is_pinned_capability_keeps_original_path_even_on_identified_version(self):
        thread = self.remote.add("selected")
        thread["isPinned"] = False
        self.remote.pin_field = True
        self.remote.sections = []
        result = self.initialize(thread_id="selected")
        self.assertTrue(result["metadata_ready"])
        self.assertEqual(result["pin_evidence_source"], "isPinned")
        self.assertIsNone(result["pin_builtin_section_id"])
        self.assertEqual(self.remote.count("thread/metadata/update"), 1)
        self.assertEqual(self.remote.count("threadSection/list"), 0)
        self.assertEqual(self.remote.count("thread/section/move"), 0)

    def test_metadata_failures_do_not_trigger_section_writes(self):
        self.remote.add("selected")["isPinned"] = False
        self.remote.pin_field = True
        for code, rpc_code in (("timeout", None), ("rpc_error", -32000), ("unsupported", -32601)):
            with self.subTest(code=code):
                self.remote.failures["thread/metadata/update"] = entry.EntryError(
                    code, method="thread/metadata/update", rpc_code=rpc_code)
                result = self.initialize(thread_id="selected")
                self.assertEqual(result["error_code"], code)
                self.assertFalse(result["metadata_ready"])
        self.assertEqual(self.remote.count("thread/section/move"), 0)
        self.assertEqual(self.remote.count("threadSection/list"), 0)

    def test_section_write_failures_preserve_errors_and_do_not_try_metadata(self):
        self.remote.add("selected")
        for code, rpc_code in (("timeout", None), ("rpc_error", -32000), ("unsupported", -32601)):
            with self.subTest(code=code):
                self.remote.failures["thread/section/move"] = entry.EntryError(
                    code, method="thread/section/move", rpc_code=rpc_code)
                result = self.initialize(thread_id="selected")
                self.assertEqual(result["error_code"], code)
                self.assertIsNone(result["pinned"])
                self.assertIsNone(result["pin_evidence_source"])
                self.assertEqual(result["thread_id"], "selected")
        self.assertEqual(self.remote.count("thread/metadata/update"), 0)
        self.assertEqual(self.remote.count("thread/start"), 0)

    def test_missing_or_unchanged_section_readback_is_not_a_success(self):
        self.remote.add("selected")
        self.remote.apply_section_move = False
        result = self.initialize(thread_id="selected")
        self.assertIs(result["pinned"], False)
        self.assertEqual(result["error_code"], "pin_not_applied")
        self.remote.expose_section = False
        result = self.initialize()
        self.assertIsNone(result["pinned"])
        self.assertIsNone(result["pin_evidence_source"])
        self.assertEqual(result["error_code"], "pin_unverified")

    def test_section_pin_readonly_scope_mismatch_clears_evidence_not_binding(self):
        self.remote.add("selected")
        self.initialize(thread_id="selected")
        before = entry._state_path(self.data).read_bytes()
        with patch("subprocess.Popen", side_effect=AssertionError("read-only")):
            result = entry.read_status(self.data, workspace=self.root / "other")
        self.assertEqual(result["error_code"], "scope_mismatch")
        for key in ("pinned", "pin_evidence_source", "pin_builtin_section_id", "pin_backend_version"):
            self.assertIsNone(result[key])
        self.assertFalse(result["metadata_ready"])
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_outside_workspace_binding_is_never_moved(self):
        self.remote.add("outside", cwd=str(self.root / "other"))
        self.assertEqual(self.initialize(thread_id="outside")["error_code"], "scope_mismatch")
        self.assertEqual(self.remote.count("thread/section/move"), 0)
        self.assertEqual(self.remote.count("threadSection/list"), 0)

    def test_current_stdio_client_integrates_section_pin_and_fresh_binding_discovery(self):
        self.remote.add("fixture-existing")
        state_path, audit_path = self.root / "peer-state.json", self.root / "peer-audit.json"
        state_path.write_text(json.dumps(self.remote.threads), encoding="utf-8")
        peer = r'''
import json,pathlib,sys
sys.path.insert(0,sys.argv[3])
from tests.atlas.test_manager_entry import SectionServer
state,audit=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2])
remote=SectionServer(pathlib.Path.cwd())
remote.threads=json.loads(state.read_text())
calls=json.loads(audit.read_text()) if audit.exists() else []
allowed={'initialize','thread/read','thread/list','threadSection/list','thread/section/move'}
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request: continue
 method=request['method']
 assert method in allowed, 'No create, model, metadata fallback, or resume'
 calls.append(method)
 result={'userAgent':'qingtian_manager_entry/0.153.4 (fake)'} if method=='initialize' else remote.call(method,request['params'])
 state.write_text(json.dumps(remote.threads))
 audit.write_text(json.dumps(calls))
 print(json.dumps({'id':request['id'],'result':result}),flush=True)
'''
        command = [sys.executable, "-u", "-c", peer, str(state_path), str(audit_path),
                   str(Path(__file__).resolve().parents[2])]
        first = entry.initialize_entry(self.data, self.workspace, command=command,
                                       thread_id="fixture-existing")
        second_data = self.root / "fresh-binding"
        second = entry.initialize_entry(second_data, self.workspace, command=command)
        third = entry.initialize_entry(second_data, self.workspace, command=command)
        for result in (first, second, third):
            self.assertEqual(result["thread_id"], "fixture-existing")
            self.assertEqual(result["pin_evidence_source"], "builtin_section")
            self.assertEqual(result["pin_backend_version"], "0.153.4")
            self.assertTrue(result["metadata_ready"])
            self.assertEqual(result["error_code"], "role_unverified")
            self.assertFalse(result["workflow_ready"])
        calls = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(calls.count("initialize"), 3)
        self.assertEqual(calls.count("thread/section/move"), 1)
        self.assertNotIn("thread/start", calls)
        self.assertNotIn("thread/metadata/update", calls)


class OnboardingGuideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace with spaces"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.command = ["fixture-codex", "app-server"]
        self.remote = SectionServer(self.workspace)

    def inspect(self, **kwargs):
        return entry.inspect_entry(self.data, self.workspace, command=self.command,
                                   client_factory=self.remote, **kwargs)

    def guide(self, **kwargs):
        return entry.read_status(self.data, workspace=self.workspace,
                                 command=kwargs.get("command", self.command))["onboarding"]

    def save_pending(self):
        with entry._lock(self.data):
            state = entry._empty()
            state.update(identity=entry._identity(self.workspace.resolve(), self.command),
                         creation_pending=True, error_code="scan_incomplete")
            entry._save(self.data, state)

    def test_local_guide_has_no_process_or_directory_side_effects(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no process")):
            guide = self.guide()
        self.assertTrue(guide["read_only"])
        self.assertFalse(guide["can_create"])
        self.assertEqual(guide["reason"], "not_initialized")
        self.assertFalse(self.data.exists())
        self.assertIn("dedicated_entry", [s["id"] for s in guide["action_steps"]])
        self.assertTrue(all(s["requires_user_action"] for s in guide["action_steps"]))

    def test_guide_commands_preserve_scope_proxy_and_shell_quoting(self):
        command = ["/tmp/a codex;$HOME", "app-server", "proxy", "--sock", "/tmp/my socket"]
        steps = self.guide(command=command)["action_steps"]
        for step in steps:
            if not step["argv"]:
                self.assertIsNone(step["command"])
                continue
            argv = step["argv"]
            self.assertEqual(shlex.split(step["command"]), argv)
            self.assertIn(str(self.workspace.resolve()), argv)
            self.assertIn(str(self.data.resolve()), argv)
            self.assertIn("--codex-bin=" + command[0], argv)
            self.assertIn("--socket=" + command[4], argv)
            self.assertEqual(argv[argv.index("--transport") + 1], "proxy")
            self.assertNotIn("init", argv)

    def test_internal_script_transport_is_not_exposed_or_replaced(self):
        guide = self.guide(command=[sys.executable, "-c", "PRIVATE_SCRIPT"])
        self.assertNotIn("PRIVATE_SCRIPT", json.dumps(guide))
        self.assertEqual([s["id"] for s in guide["action_steps"]], ["reconcile_scope"])

    def test_pending_creation_guide_never_suggests_a_new_entry(self):
        self.save_pending()
        before = entry._state_path(self.data).read_bytes()
        guide = self.guide()
        ids = [s["id"] for s in guide["action_steps"]]
        self.assertIn("recover_pending", ids)
        self.assertNotIn("dedicated_entry", ids)
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_visibility_gap_inspection_is_readonly_and_cannot_authorize_create(self):
        self.remote.add("hidden-zero-turn")
        result = self.inspect()
        self.assertEqual(result["inspection"]["error_code"], "scan_incomplete")
        self.assertFalse(result["inspection"]["can_create"])
        self.assertFalse(result["inspection"]["can_bind"])
        self.assertFalse(self.data.exists())
        self.assertNotIn("bind", [s["id"] for s in result["onboarding"]["action_steps"]])
        self.assertTrue(all(m in {"thread/list", "threadSection/list"} for m, _ in self.remote.calls))

    def test_explicit_hidden_id_checks_identity_without_renaming_or_pinning(self):
        self.remote.add("selected", name="User selected task")
        before = deepcopy(self.remote.threads)
        result = self.inspect(thread_id="selected")
        self.assertTrue(result["inspection"]["can_bind"])
        self.assertEqual(result["inspection"]["server_version"], "0.153.4")
        self.assertEqual(result["inspection"]["thread_id"], "selected")
        self.assertFalse(result["metadata_ready"])
        self.assertFalse(result["workflow_ready"])
        self.assertFalse(result["role_configuration"]["verified"])
        self.assertIsNone(result["thread_id"], "Inspection must not pretend to bind")
        self.assertEqual(self.remote.threads, before)
        self.assertEqual(self.remote.calls, [("thread/read", {"threadId": "selected", "includeTurns": False})])
        self.assertFalse(self.data.exists())

    def test_pending_inspection_preserves_journal_and_saved_binding_bytes(self):
        self.save_pending()
        before = entry._state_path(self.data).read_bytes()
        self.remote.add("earlier-result")
        result = self.inspect(thread_id="earlier-result")
        self.assertTrue(result["inspection"]["can_bind"])
        self.assertTrue(result["creation_pending"])
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_scope_mismatch_blocks_inspection_and_write_guidance(self):
        self.save_pending()
        before = entry._state_path(self.data).read_bytes()
        with patch("subprocess.Popen", side_effect=AssertionError("no connection")):
            result = entry.inspect_entry(self.data, self.root, command=self.command)
        self.assertEqual(result["inspection"]["error_code"], "scope_mismatch")
        self.assertEqual([s["id"] for s in result["onboarding"]["action_steps"]], ["reconcile_scope"])
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)

    def test_corrupt_state_blocks_inspection_without_rewriting_it(self):
        path = entry._state_path(self.data)
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        result = self.inspect(thread_id="anything")
        self.assertEqual(result["inspection"]["error_code"], "invalid_state")
        self.assertFalse(self.remote.calls)
        self.assertEqual(path.read_text(), "{broken")

    def test_outside_workspace_or_archived_id_cannot_be_bound_by_inspection(self):
        self.remote.add("outside", cwd=str(self.root))
        self.remote.add("archived")["archived"] = True
        for selected, error in (("outside", "scope_mismatch"), ("archived", "not_found")):
            with self.subTest(selected=selected):
                result = self.inspect(thread_id=selected)
                self.assertEqual(result["inspection"]["error_code"], error)
                self.assertFalse(result["inspection"]["can_bind"])
        self.assertFalse(self.data.exists())
        self.assertTrue(all(m == "thread/read" for m, _ in self.remote.calls))

    def test_unknown_empty_discovery_is_not_creation_authorization(self):
        self.remote = FakeServer(self.workspace)
        result = self.inspect()
        self.assertEqual(result["inspection"]["error_code"], "not_found")
        self.assertFalse(result["inspection"]["can_create"])
        self.assertEqual(self.remote.count("thread/start"), 0)
        self.assertFalse(self.data.exists())

    def test_ambiguous_discovery_requires_selection_without_mutations(self):
        for selected in ("one", "two"):
            self.remote.add(selected, section=self.remote.sections[0])
        result = self.inspect()
        self.assertEqual(result["inspection"]["error_code"], "ambiguous")
        self.assertEqual(set(result["inspection"]["candidate_ids"]), {"one", "two"})
        self.assertFalse(result["inspection"]["can_bind"])
        self.assertFalse(self.data.exists())
        self.assertTrue(all(m in {"thread/list", "threadSection/list"} for m, _ in self.remote.calls))

    def test_protocol_failures_do_not_expose_messages_or_mutate(self):
        for code in ("timeout", "rpc_error", "protocol_error"):
            self.remote.failures["thread/read"] = entry.EntryError(code, method="thread/read")
            result = self.inspect(thread_id="selected")
            self.assertEqual(result["inspection"]["error_code"], code)
            self.assertFalse(result["inspection"]["can_bind"])
        self.assertFalse(self.data.exists())
        self.assertTrue(all(m == "thread/read" for m, _ in self.remote.calls))

    def test_invalid_timeout_never_contacts_remote(self):
        for timeout in (0, 61, float("nan")):
            self.assertEqual(self.inspect(timeout=timeout)["inspection"]["error_code"], "invalid_config")
        self.assertFalse(self.remote.calls)
        self.assertFalse(self.data.exists())

    def test_cli_guide_routing_and_explicit_id_do_not_contact_codex(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no process")):
            with redirect_stdout(StringIO()) as output:
                code = main(["--workspace", str(self.workspace), "--data-dir", str(self.data),
                             "manager-entry", "guide", "--thread-id", "selected"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        inspect = next(s for s in result["onboarding"]["action_steps"] if s["id"] == "inspect")
        self.assertIn("--thread-id=selected", inspect["argv"])
        self.assertFalse(self.data.exists())

    def test_cli_inspect_exit_is_identity_check_not_workflow_acceptance(self):
        self.remote.add("selected")
        with patch.object(entry, "AppServerClient", self.remote):
            for selected, expected in (("selected", 0), ("missing", 2)):
                with redirect_stdout(StringIO()) as output:
                    code = main(["manager-entry", "inspect", "--workspace", str(self.workspace),
                                 "--data-dir", str(self.data), "--thread-id", selected])
                self.assertEqual(code, expected)
                self.assertFalse(json.loads(output.getvalue())["workflow_ready"])
        self.assertFalse(self.data.exists())

    def test_cli_invalid_transport_is_readonly_and_has_no_runnable_steps(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no process")):
            for action in ("guide", "inspect"):
                with redirect_stdout(StringIO()) as output:
                    code = main(["manager-entry", action, "--data-dir", str(self.data),
                                 "--transport", "stdio", "--socket", "/tmp/fixture.sock"])
                result = json.loads(output.getvalue())
                self.assertEqual(code, 0 if action == "guide" else 2)
                self.assertEqual(result["error_code"], "invalid_config")
                self.assertTrue(all(s["argv"] is None for s in result["onboarding"]["action_steps"]))
        self.assertFalse(self.data.exists())

    def test_fake_inspect_then_explicit_bind_and_repeat_stay_deduplicated(self):
        self.remote.add("selected")
        checked = self.inspect(thread_id="selected")
        bind = next(s for s in checked["onboarding"]["action_steps"] if s["id"] == "bind")
        self.assertEqual(bind["kind"], "metadata_write")
        self.assertIn("--thread-id=selected", bind["argv"])
        first = entry.initialize_entry(self.data, self.workspace, command=self.command,
            thread_id="selected", allow_create=False, client_factory=self.remote)
        before = entry._state_path(self.data).read_bytes()
        self.assertTrue(self.inspect()["inspection"]["can_bind"])
        self.assertEqual(entry._state_path(self.data).read_bytes(), before)
        second = entry.initialize_entry(self.data, self.workspace, command=self.command,
            allow_create=False, client_factory=self.remote)
        for result in (first, second):
            self.assertTrue(result["metadata_ready"])
            self.assertFalse(result["workflow_ready"])
            self.assertEqual(result["error_code"], "role_unverified")
        self.assertEqual(self.remote.count("thread/start"), 0)
        self.assertEqual(self.remote.count("thread/section/move"), 1)


class ProtocolTests(unittest.TestCase):
    def test_backend_version_comes_from_actual_initialize_response(self):
        for agent, expected in (("qingtian_manager_entry/0.153.4 (Mac OS)", "0.153.4"),
                                ("qingtian_manager_entry/0.999.0 (Linux)", "0.999.0"),
                                ("other/0.153.4", None),
                                ("qingtian_manager_entry/0.153.4-custom", None),
                                (None, None)):
            with self.subTest(agent=agent):
                self.assertEqual(entry._backend_version({"userAgent": agent}), expected)
        peer = "import json,sys\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'id':r['id'],'result':{'userAgent':'qingtian_manager_entry/0.153.4 (fixture)'}}),flush=True)\nfor line in sys.stdin: pass\n"
        with entry.AppServerClient([sys.executable, "-u", "-c", peer], cwd=Path.cwd(), timeout=2) as client:
            self.assertEqual(client.server_version, "0.153.4")

    @unittest.skipUnless(os.name == "posix", "POSIX process-group lifetime test")
    def test_inherited_stdout_cleanup_is_bounded_and_leaks_no_live_resources(self):
        peer = r'''
import json,pathlib,subprocess,sys,time
r=json.loads(sys.stdin.readline())
child=subprocess.Popen([sys.executable,'-c',
    'import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)',
    sys.argv[1]],stdin=subprocess.DEVNULL)
while not pathlib.Path(sys.argv[1]).exists():
    time.sleep(.005)
print(json.dumps({'id':r['id'],'result':{}}),flush=True)
for line in sys.stdin:
    if json.loads(line).get('method')=='read':
        break
'''
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "child.pid"
            sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            try:
                client = entry.AppServerClient([sys.executable, "-u", "-c", peer, str(pid_path)], cwd=Path.cwd(), timeout=2)
                # Startup is not the operation under test. The peer acknowledges
                # initialize only after its SIGTERM-ignoring descendant is ready.
                # Keep the read + cleanup budget strict, independently of CI's
                # Python startup latency; unexpected initialize errors must fail.
                with client:
                    read_fd = client.process.stdout.fileno()
                    write_fd = client.process.stdin.fileno()
                    child_pid = int(pid_path.read_text())
                    started = time.monotonic()
                    client.deadline = started + .1
                    with self.assertRaises(entry.EntryError) as error:
                        client.call("read", {})
                self.assertEqual(error.exception.code, "timeout")
                self.assertEqual(error.exception.method, "read")
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertFalse(client.reader.is_alive())
                self.assertIsNotNone(client.process.poll())
                self.assertTrue(client.process.stdout.closed)
                self.assertTrue(client.process.stdin.closed)
                for fd in (read_fd, write_fd):
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                for _ in range(20):
                    state = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True, timeout=1).stdout.strip()
                    if not state or state.startswith("Z"):
                        break
                    time.sleep(.01)
                self.assertTrue(not state or state.startswith("Z"), "transport descendant remained live")
                self.assertIsNone(sentinel.poll(), "unrelated process was terminated")
            finally:
                sentinel.kill()
                sentinel.wait(timeout=2)

    def test_initialize_timeout_cleans_up_before_context_body(self):
        peer = "import time; time.sleep(30)"
        client = entry.AppServerClient([sys.executable, "-u", "-c", peer], cwd=Path.cwd(), timeout=.1)
        entered = False
        started = time.monotonic()
        with self.assertRaises(entry.EntryError) as error:
            with client:
                entered = True
        self.assertFalse(entered)
        self.assertEqual(error.exception.code, "timeout")
        self.assertEqual(error.exception.method, "initialize")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(client.reader.is_alive())
        self.assertIsNotNone(client.process.poll())
        self.assertTrue(client.process.stdout.closed)
        self.assertTrue(client.process.stdin.closed)

    def test_unread_stdin_respects_deadline_and_reader_stops(self):
        peer = "import json,sys,time\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'id':r['id'],'result':{}}),flush=True)\ntime.sleep(30)\n"
        client = entry.AppServerClient([sys.executable, "-u", "-c", peer], cwd=Path.cwd(), timeout=.15)
        started = time.monotonic()
        with self.assertRaises(entry.EntryError) as error:
            with client:
                client.call("read", {"data": "x" * entry.MAX_MESSAGE})
        self.assertEqual(error.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(client.reader.is_alive())
        self.assertIsNotNone(client.process.poll())

    def test_real_stdio_handshake_interleaving_id_matching_and_redaction(self):
        script = r'''
import json, sys
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialized":
        initialized = True
        continue
    if "id" not in request or not method:
        continue
    if method != "initialize" and not initialized:
        raise SystemExit(9)
    print(json.dumps({"method":"notice","params":{"text":"secret model text"}}), flush=True)
    print(json.dumps({"id":9000,"result":{"wrong":True}}), flush=True)
    print(json.dumps({"id":"approval","method":"item/commandExecution/requestApproval","params":{}}), flush=True)
    answer = {"id":request["id"], "result":{"ok":True}}
    if method == "fail":
        answer = {"id":request["id"], "error":{"code":-32601,"message":"SECRET /private/path"}}
    print(json.dumps(answer), flush=True)
'''
        with entry.AppServerClient([sys.executable, "-u", "-c", script], cwd=Path.cwd(), timeout=2) as client:
            self.assertEqual(client.call("read", {}), {"ok": True})
            with self.assertRaises(entry.EntryError) as error:
                client.call("fail", {})
            self.assertEqual(error.exception.code, "unsupported")
            self.assertNotIn("SECRET", str(error.exception))

    def test_timeout_kills_child_and_bounds_whole_session(self):
        script = "import json,sys,time\nfor line in sys.stdin:\n r=json.loads(line)\n if 'id' in r:\n  time.sleep(.08)\n  print(json.dumps({'id':r['id'],'result':{}}),flush=True)\n"
        started = time.monotonic()
        client = entry.AppServerClient([sys.executable, "-u", "-c", script], cwd=Path.cwd(), timeout=.12)
        with self.assertRaises(entry.EntryError) as error:
            with client:
                client.call("read", {})
        self.assertEqual(error.exception.code, "timeout")
        self.assertIsNotNone(client.process.poll())
        self.assertLess(time.monotonic() - started, 3)

    def test_missing_executable_is_structured(self):
        with self.assertRaises(entry.EntryError) as error:
            with entry.AppServerClient(["/does-not-exist/qingtian-codex"], cwd=Path.cwd()):
                pass
        self.assertEqual(error.exception.code, "codex_missing")


if __name__ == "__main__":
    unittest.main()
