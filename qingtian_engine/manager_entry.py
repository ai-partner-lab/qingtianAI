"""Initialize a real Codex manager thread without starting a model turn.

Only explicit onboarding/sync commands contact app-server. HTTP readers consume
the last atomic snapshot; this module never edits Codex's database or sidebar.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import signal
import subprocess
import tempfile
import threading
import time

from . import __version__


MANAGER_NAME = "\u64ce\u5929\u5927\u7ba1\u5bb6"
MANAGER_INSTRUCTIONS = (
    "You are Qingtian's manager entry. Coordinate only the user's newly authorized work. "
    "Keep user communication responsive, delegate substantial execution to suitable "
    "authorized workers, avoid duplicate owners, review actual evidence, and report "
    "results and limitations concisely. Preserve existing work and applicable project "
    "policies. Do not resume old tasks or expand deployment permissions without "
    "authorization. Do not claim background execution or completed acceptance without evidence."
)
STATE_NAME = "manager-entry.json"
MAX_MESSAGE = 2 * 1024 * 1024
# CLI 0.153.4's compiled migration and isolated protocol readback establish
# this protected built-in identity. A display name or launcher version cannot.
BUILTIN_PINNED_SECTION_ID = "01984de2-8f74-7c91-a3b2-5c5e937cf318"
SECTION_PIN_VERSIONS = frozenset({"0.153.4"})
MESSAGES = {
    "not_initialized": "Manager entry has not been initialized.",
    "initializing": "Manager entry synchronization is in progress.",
    "ready": "The manager thread and persisted pin state were verified.",
    "role_unverified": "Thread metadata is verified; manager role instructions still require explicit setup and acceptance.",
    "codex_missing": "Codex executable is unavailable. Install Codex or set QINGTIAN_CODEX_BIN.",
    "connection_closed": "App-server connection closed. Check Codex login and the configured transport/socket.",
    "timeout": "App-server did not respond before the deadline.",
    "protocol_error": "App-server returned an invalid protocol response.",
    "unsupported": "This app-server version does not support the requested operation.",
    "rpc_error": "App-server rejected the operation. Check Codex setup and retry synchronization.",
    "pin_unverified": "The thread exists, but app-server did not expose verified native pin evidence.",
    "pin_not_applied": "App-server readback reports that the thread is not pinned.",
    "busy": "Another manager-entry operation is in progress.",
    "invalid_state": "The local binding is unreadable. Preserve it and reconcile the existing thread before retrying.",
    "scope_mismatch": "The binding belongs to another workspace or Codex configuration. Use its original configuration or a separate data directory.",
    "ambiguous": "Multiple matching manager threads exist. Select the intended one with --thread-id.",
    "creation_uncertain": "An earlier create may have succeeded. Reconcile its thread ID with --thread-id; automatic creation is paused.",
    "not_found": "No active manager entry was found. Run manager-entry init to create one.",
    "scan_incomplete": "Thread discovery could not establish a complete result. No thread was created; reconcile an existing ID explicitly.",
    "io_error": "Local manager-entry state or process I/O is unavailable.",
    "invalid_config": "Invalid manager-entry configuration. Check transport, socket and timeout.",
    "cleanup_unavailable": "This platform cannot provide bounded transport pipe/process cleanup.",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _backend_version(result):
    """Use the connected server's handshake, never the local proxy executable."""
    agent = result.get("userAgent")
    if not isinstance(agent, str) or not agent.startswith("qingtian_manager_entry/"):
        return None
    version = agent.partition("/")[2].partition(" ")[0]
    parts = version.split(".")
    return version if len(version) <= 24 and len(parts) == 3 and all(
        part.isascii() and part.isdigit() for part in parts) else None


class EntryError(Exception):
    def __init__(self, code, *, method=None, rpc_code=None):
        super().__init__(MESSAGES[code])
        self.code, self.method, self.rpc_code = code, method, rpc_code


class AppServerClient:
    """Bounded, ID-correlated newline JSON-RPC over a child stdio transport."""

    def __init__(self, command, *, cwd, timeout=15, env=None):
        self.command, self.cwd, self.timeout, self.env = command, cwd, timeout, env
        self.process = None
        self.reader = None
        self.job = None
        self.incoming = queue.Queue(maxsize=64)
        self.stopped = threading.Event()
        self.next_id = 0
        self.server_version = None

    def __enter__(self):
        self.deadline = time.monotonic() + self.timeout
        try:
            self.process = subprocess.Popen(
                self.command, cwd=str(self.cwd), env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as exc:
            raise EntryError("codex_missing") from exc
        except OSError as exc:
            raise EntryError("io_error") from exc
        try:
            if os.name == "nt":
                self._assign_windows_job()
            try:
                os.set_blocking(self.process.stdout.fileno(), False)
                os.set_blocking(self.process.stdin.fileno(), False)
            except (OSError, AttributeError) as exc:
                raise EntryError("cleanup_unavailable") from exc
            self.reader = threading.Thread(target=self._read, name="qingtian-entry-reader", daemon=True)
            self.reader.start()
            initialized = self.call("initialize", {"clientInfo": {
                "name": "qingtian_manager_entry", "title": "Qingtian manager entry",
                "version": __version__,
            }, "capabilities": {"experimentalApi": True}})
            self.server_version = _backend_version(initialized)
            self._send({"method": "initialized", "params": {}})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _read(self):
        pending = bytearray()
        while not self.stopped.is_set():
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    self._enqueue(EntryError("protocol_error" if pending else "connection_closed"))
                    return
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    if len(line) > MAX_MESSAGE:
                        raise ValueError("oversized message")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("invalid message")
                    if "id" in message:
                        self._enqueue(message)
                if len(pending) > MAX_MESSAGE:
                    raise ValueError("oversized message")
            except BlockingIOError:
                self.stopped.wait(0.01)
            except (OSError, ValueError):
                self._enqueue(EntryError("protocol_error"))
                return

    def _enqueue(self, message):
        while not self.stopped.is_set():
            try:
                self.incoming.put(message, timeout=0.05)
                return
            except queue.Full:
                continue

    def _send(self, message):
        body = memoryview((json.dumps(message) + "\n").encode("utf-8"))
        while body:
            if time.monotonic() >= self.deadline:
                raise EntryError("timeout", method=message.get("method"))
            try:
                written = os.write(self.process.stdin.fileno(), body)
                body = body[written:]
            except BlockingIOError:
                self.stopped.wait(0.01)
            except OSError as exc:
                raise EntryError("connection_closed") from exc

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = self.deadline
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EntryError("timeout", method=method)
            try:
                message = self.incoming.get(timeout=remaining)
            except queue.Empty as exc:
                raise EntryError("timeout", method=method) from exc
            if isinstance(message, EntryError):
                raise EntryError(message.code, method=method)
            if "method" in message:
                # Onboarding cannot approve tools, permissions, or start work.
                self._send({"id": message["id"], "error": {
                    "code": -32601, "message": "Unsupported by manager-entry client",
                }})
                continue
            if type(message.get("id")) is not int or message["id"] != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if not isinstance(error, dict):
                    raise EntryError("protocol_error", method=method)
                code = error.get("code")
                raise EntryError(
                    "unsupported" if code in (-32601, -32602) else "rpc_error",
                    method=method, rpc_code=code if type(code) is int else None,
                )
            if not isinstance(message.get("result"), dict):
                raise EntryError("protocol_error", method=method)
            return message["result"]

    def _assign_windows_job(self):
        # Only this newly launched transport is assigned, before any RPC. A
        # proxy's already-running server is not part of this job/process group.
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            getattr(api, name).argtypes, getattr(api, name).restype = args, result
        job = api.CreateJobObjectW(None, None)
        process = api.OpenProcess(0x0101, False, self.process.pid)
        assigned = bool(job and process and api.AssignProcessToJobObject(job, process))
        if process:
            api.CloseHandle(process)
        if not assigned:
            if job:
                api.CloseHandle(job)
            raise EntryError("cleanup_unavailable")
        self.job = (api, job)

    def __exit__(self, *_args):
        self.stopped.set()
        if self.process is None:
            return
        cleanup_error = None
        try:
            if self.reader is not None:
                self.reader.join(timeout=0.2)
            self.process.stdin.close()  # Raw FileIO: no BufferedReader lock.
            # Allow EOF shutdown, but do not reap the group leader until all
            # signals are sent: its PID must not be recycled for another group.
            time.sleep(0.1)
            if os.name == "posix":
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(self.process.pid, sig)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # Some macOS sandboxes return EPERM for a group that
                        # disappeared after TERM. Reap our child and require
                        # pipe EOF before treating that case as cleaned up.
                        self.process.kill()
                        try:
                            self.process.wait(timeout=0.3)
                        except subprocess.TimeoutExpired:
                            cleanup_error = EntryError("cleanup_unavailable")
                        try:
                            for _ in range(MAX_MESSAGE // 65536 + 1):
                                if not os.read(self.process.stdout.fileno(), 65536):
                                    break
                            else:
                                cleanup_error = EntryError("cleanup_unavailable")
                        except (BlockingIOError, OSError):
                            cleanup_error = EntryError("cleanup_unavailable")
                        break
                    if sig == signal.SIGTERM:
                        time.sleep(0.1)
            elif self.job:
                api, job = self.job
                if not api.TerminateJobObject(job, 1):
                    cleanup_error = EntryError("cleanup_unavailable")
            else:
                self.process.kill()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    cleanup_error = EntryError("cleanup_unavailable")
        finally:
            if self.reader is not None:
                self.reader.join(timeout=0.5)
            self.process.stdout.close()
            if self.job:
                api, job = self.job
                api.CloseHandle(job)
                self.job = None
        if cleanup_error:
            raise cleanup_error


def _unknown_role():
    return {"source": "unknown", "status": "unverified", "verified": False,
            "instruction_sha256": None, "submitted_at": None,
            "verification_method": "not_available"}


def _empty():
    return {
        "schema_version": 1, "name": MANAGER_NAME, "status": "not_initialized",
        "thread_id": None, "available": False, "supported": None,
        "pinned": None, "pin_supported": None, "pinned_at": None,
        "pin_evidence_source": None, "pin_builtin_section_id": None,
        "pin_backend_version": None,
        "bound_at": None, "last_attempt_at": None, "last_synced_at": None,
        "error_code": "not_initialized", "message": MESSAGES["not_initialized"],
        "creation_pending": False,
        "metadata_ready": False, "workflow_ready": False,
        "role_configuration": _unknown_role(),
    }


def _state_path(data_dir):
    return Path(data_dir) / "config" / STATE_NAME


def _load(data_dir):
    try:
        with _state_path(data_dir).open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as exc:
        raise EntryError("invalid_state") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or (value.get("identity") is not None and not isinstance(value["identity"], str))
            or (not value.get("identity") and (value.get("thread_id") or value.get("creation_pending")))
            or not isinstance(value.get("creation_pending"), bool)
            or (value.get("thread_id") is not None and not isinstance(value["thread_id"], str))):
        raise EntryError("invalid_state")
    return {**_empty(), **value}


def _save(data_dir, state):
    target = _state_path(data_dir)
    fd, name = tempfile.mkstemp(prefix=".manager-entry-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _failure(state, error):
    state.update(status="partial" if state.get("available") else "unavailable",
                 error_code=error.code, message=MESSAGES[error.code],
                 failed_method=error.method, rpc_code=error.rpc_code)
    if error.code in {"ambiguous", "creation_uncertain", "scope_mismatch", "invalid_state"}:
        state["status"] = "needs_attention"
    elif error.code == "unsupported" and not state.get("available"):
        state.update(status="unsupported", supported=False)
    return state


def _record_configuration_error(data_dir, error):
    try:
        with _lock(data_dir):
            state = _load(data_dir)
            if state.get("identity"):
                # An invalid new configuration cannot authorize updates to an
                # existing binding whose scope could not even be validated.
                state.update(available=False, pinned=None, metadata_ready=False,
                             pin_evidence_source=None, pin_builtin_section_id=None,
                             pin_backend_version=None,
                             workflow_ready=False, scope_valid=False, stale=True)
                return _failure(state, error)
            state.update(last_attempt_at=_now(), available=False, pinned=None)
            _save(data_dir, _failure(state, error))
            return read_status(data_dir)
    except EntryError as exc:
        return _failure(_empty(), exc)
    except OSError:
        return _failure(_empty(), EntryError("io_error"))


def _identity(workspace, command):
    home = str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve())
    return hashlib.sha256(json.dumps([str(workspace), home, command]).encode()).hexdigest()


def read_status(data_dir, *, workspace=None, command=None):
    """Read-only last observation, never an assertion of live connectivity."""
    try:
        state = _load(data_dir)
    except EntryError as exc:
        state = _failure(_empty(), exc)
    from .config import default_workspace
    expected_workspace = Path(workspace if workspace is not None else default_workspace()).expanduser().resolve()
    state["requested_workspace"] = str(expected_workspace)
    state["scope_valid"] = None
    expected_command = None
    try:
        expected_command = command if command is not None else configured_command()
        if state.get("identity"):
            state["scope_valid"] = state["identity"] == _identity(expected_workspace, expected_command)
            if not state["scope_valid"]:
                state.update(available=False, pinned=None, metadata_ready=False,
                             pin_evidence_source=None, pin_builtin_section_id=None,
                             pin_backend_version=None)
                _failure(state, EntryError("scope_mismatch"))
    except EntryError as exc:
        state.update(available=False, pinned=None, metadata_ready=False, scope_valid=False,
                     pin_evidence_source=None, pin_builtin_section_id=None,
                     pin_backend_version=None)
        _failure(state, exc)
    # thread/read cannot verify instructions. Even older ready snapshots only
    # proved metadata. Do not promote local provenance into role verification.
    role = state.get("role_configuration")
    if not isinstance(role, dict):
        role = _unknown_role()
    state["role_configuration"] = {**role, "verified": False, "verification_method": "not_available"}
    state["workflow_ready"] = False
    if state["status"] == "ready":
        state["metadata_ready"] = state.get("pinned") is True and state.get("available") is True
        _failure(state, EntryError("role_unverified"))
    result = {key: value for key, value in state.items() if key != "identity"}
    result["observation"] = "last_sync"
    try:
        stamp = datetime.fromisoformat(state["last_synced_at"])
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        result["stale"] = age > 300 or state["status"] != "ready"
    except (TypeError, ValueError):
        result["stale"] = True
    result["onboarding"] = _onboarding(result, data_dir, expected_workspace, expected_command)
    return result


def _onboarding(state, data_dir, workspace, command, *, selected_id=None, inspection=None):
    """Instructions only: never treat displayed commands as authorization."""
    reason = (inspection or {}).get("error_code") or state.get("error_code")
    steps = []

    def step(key, title, detail, kind="manual", argv=None):
        steps.append({"id": key, "title": title, "detail": detail, "kind": kind,
                      "argv": argv, "command": shlex.join(argv) if argv else None,
                      "shell": "posix" if argv else None, "requires_user_action": True})

    result = {"schema_version": 1, "read_only": True, "reason": reason,
              "workspace": str(workspace), "data_dir": str(Path(data_dir).expanduser().resolve()),
              "can_create": False, "action_steps": steps}
    # Arbitrary internal/test transports cannot be translated into CLI flags.
    # Do not expose their embedded scripts or guess a different backend.
    ordinary = (isinstance(command, list) and len(command) == 2
                and command[1] == "app-server")
    proxy = (isinstance(command, list) and len(command) in (3, 5)
             and command[1:3] == ["app-server", "proxy"]
             and (len(command) == 3 or command[3] == "--sock"))
    if (state.get("scope_valid") is False
            or reason in {"scope_mismatch", "invalid_state", "invalid_config"}
            or not (ordinary or proxy)
            or not all(isinstance(value, str) and value for value in command)):
        step("reconcile_scope", "Restore the original binding configuration",
             "Use the original workspace, data directory, CODEX_HOME and transport. Preserve the binding and any creation_pending receipt. Do not delete it or create a replacement to bypass this error.")
        return result
    options = ["--workspace", str(workspace), "--data-dir", result["data_dir"],
               "--codex-bin=" + command[0], "--transport", "proxy" if proxy else "stdio"]
    if proxy and len(command) == 5:
        options.append("--socket=" + command[4])

    def cli(action, thread_id=None):
        return ["qingtian", "manager-entry", action, *options] + (
            ["--thread-id=" + thread_id] if thread_id else [])

    step("status", "Read the saved binding", "Local snapshot only, not live acceptance. Run these commands with the same CODEX_HOME and environment as the dashboard.",
         "local_read", cli("status"))
    thread_id = selected_id or state.get("thread_id")
    if state.get("creation_pending"):
        step("recover_pending", "Recover the earlier creation result",
             "A previous request may have created an entry. Find its exact returned ID in your client's task records; do not create another task, delete the receipt or use a fresh data directory to retry.")
    elif not thread_id:
        step("select_existing", "Choose one existing manager task",
             "In your Codex client, select the intended task in this workspace and obtain its exact ID. Resolve multiple candidates explicitly. An empty or incomplete CLI list does not prove that no task exists.")
        step("dedicated_entry", "If no suitable task exists, obtain explicit creation approval",
             "First reconcile existing tasks with their owner. Only after explicit approval, use your client's supported flow to create exactly one dedicated task in this workspace and retain its returned ID. If that flow starts a model turn, obtain separate permission first. This guide neither authorizes nor performs creation; never use it to bypass incomplete discovery.")
    thread_id = thread_id or "YOUR_EXISTING_THREAD_ID"
    step("inspect", "Check the selected ID without changing it",
         "Replace the ID placeholder with your explicit selection. Reads thread identity and workspace without binding, renaming, pinning or starting/resuming a turn. Continue only when inspection.can_bind is true.",
         "read_only", cli("inspect", thread_id))
    if inspection is None or inspection.get("can_bind") is True:
        step("bind", "Explicitly bind the checked task",
             "Only after a successful inspect and your approval: sync reads the ID again, saves the binding, and may rename it to the manager title and pin it. It never creates a replacement or starts work. Exit 2 is expected while role/workflow acceptance remains incomplete.",
             "metadata_write", cli("sync", thread_id))
    step("instructions", "Review the proposed manager role rules",
         "Exports suggested rules only. Merge them through your client's supported configuration with explicit authorization, preserving applicable project rules; this command does not install them.",
         "local_read", cli("instructions"))
    step("manual_acceptance", "Keep role and workflow acceptance separate",
         "Actual effective instructions, desktop placement and a fresh coordination journey require separate evidence and permission. A model's self-report or a manual checkbox cannot verify them. workflow_ready remains false.")
    return result


def inspect_entry(data_dir, workspace, *, command=None, timeout=15,
                  thread_id=None, client_factory=None):
    """Explicit protocol reads only; no binding writes, lock, or mutation RPC."""
    workspace = Path(workspace).expanduser().resolve()
    result = read_status(data_dir, workspace=workspace, command=command)
    inspection = {"read_only": True, "status": "blocked", "error_code": None,
                  "message": None, "thread_id": None, "candidate_ids": [],
                  "server_version": None, "can_bind": False, "can_create": False,
                  "observation": "current_connection"}
    result["inspection"] = inspection
    try:
        if result.get("scope_valid") is False or result["error_code"] in {
                "invalid_state", "invalid_config", "scope_mismatch"}:
            raise EntryError(result["error_code"])
        command = command if command is not None else configured_command()
        if not workspace.is_dir() or not 0 < timeout <= 60:
            raise EntryError("invalid_config")
        factory = client_factory or AppServerClient
        with factory(command, cwd=workspace, timeout=timeout) as client:
            inspection["server_version"] = getattr(client, "server_version", None)
            selected_id = thread_id or result.get("thread_id")
            if not selected_id:
                matches = _find_threads(client, workspace)
                inspection["candidate_ids"] = [item["id"] for item in matches]
                if len(matches) > 1:
                    raise EntryError("ambiguous")
                if not matches:
                    raise EntryError("creation_uncertain" if result.get("creation_pending") else "not_found")
                selected_id = matches[0]["id"]
            thread = _thread(client.call("thread/read", {
                "threadId": selected_id, "includeTurns": False,
            }), workspace, selected_id)
            if thread.get("archived") is True:
                raise EntryError("not_found", method="thread/read")
        inspection.update(status="candidate_verified", thread_id=thread["id"],
                          candidate_ids=[thread["id"]], can_bind=True,
                          message="Selected ID and workspace were read back. No metadata, binding or role configuration was changed.")
    except EntryError as exc:
        inspection.update(error_code=exc.code, message=MESSAGES[exc.code],
                          failed_method=exc.method, rpc_code=exc.rpc_code)
    except OSError:
        inspection.update(error_code="io_error", message=MESSAGES["io_error"])
    result["onboarding"] = _onboarding(
        result, data_dir, workspace, command,
        selected_id=inspection["thread_id"] or thread_id, inspection=inspection)
    return result


@contextmanager
def _lock(data_dir):
    parent = _state_path(data_dir).parent
    parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(parent / "manager-entry.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            lock = lambda: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock()
        except OSError as exc:
            raise EntryError("busy") from exc
        yield
    finally:
        os.close(fd)


def _thread(result, workspace, expected_id=None):
    thread = result.get("thread")
    if (not isinstance(thread, dict) or not isinstance(thread.get("id"), str)
            or not thread["id"] or (expected_id and thread["id"] != expected_id)):
        raise EntryError("protocol_error")
    if not isinstance(thread.get("cwd"), str) or Path(thread["cwd"]).resolve() != workspace:
        raise EntryError("scope_mismatch")
    if thread.get("ephemeral") is True:
        raise EntryError("protocol_error")
    return thread


def _section_inventory(client):
    """Only the identified backend has a proven built-in section contract."""
    if getattr(client, "server_version", None) not in SECTION_PIN_VERSIONS:
        return None
    sections, cursor, seen = {}, None, set()
    for _ in range(100):
        result = client.call("threadSection/list", {"limit": 100, "cursor": cursor})
        if not isinstance(result.get("data"), list):
            raise EntryError("protocol_error", method="threadSection/list")
        for section in result["data"]:
            if (not isinstance(section, dict) or not isinstance(section.get("id"), str)
                    or not section["id"] or not isinstance(section.get("name"), str)):
                raise EntryError("protocol_error", method="threadSection/list")
            sections[section["id"]] = section
        cursor = result.get("nextCursor")
        if cursor is None:
            if BUILTIN_PINNED_SECTION_ID not in sections:
                raise EntryError("pin_unverified", method="threadSection/list")
            return sections
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise EntryError("scan_incomplete", method="threadSection/list")


def _find_threads(client, workspace):
    sections = _section_inventory(client)
    # Omitted means all sections in the schema, not unsectioned. Nevertheless,
    # 0.153.4 omits fresh no-turn records from that view. Query every registered
    # section explicitly, including the built-in, to recover pinned entries.
    views = [{}] if sections is None else [{}, {"sectionId": None}] + [
        {"sectionId": section_id} for section_id in sections]
    matches, pages = {}, 0
    for view in views:
        cursor, seen = None, set()
        while True:
            if pages >= 100:
                raise EntryError("scan_incomplete", method="thread/list")
            pages += 1
            result = client.call("thread/list", {
                "cwd": str(workspace), "searchTerm": MANAGER_NAME,
                "archived": False, "limit": 100, "cursor": cursor,
                "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"],
                "modelProviders": [], "useStateDbOnly": True, **view,
            })
            if not isinstance(result.get("data"), list):
                raise EntryError("protocol_error", method="thread/list")
            for candidate in result["data"]:
                if not isinstance(candidate, dict):
                    raise EntryError("protocol_error", method="thread/list")
                if candidate.get("name") == MANAGER_NAME:
                    # A backend may ignore filters: enforce exact local scope too.
                    if (isinstance(candidate.get("cwd"), str)
                            and Path(candidate["cwd"]).resolve() == workspace
                            and candidate.get("ephemeral") is not True
                            and candidate.get("archived") is not True):
                        thread = _thread({"thread": candidate}, workspace)
                        matches[thread["id"]] = thread
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise EntryError("scan_incomplete", method="thread/list")
            seen.add(cursor)
    if sections is not None and not matches:
        # Empty no-turn, unsectioned threads remain invisible even to an explicit
        # null query. Exhausted pages cannot prove absence on this backend.
        raise EntryError("scan_incomplete", method="thread/list")
    return list(matches.values())


def initialize_entry(data_dir, workspace, *, command=None, timeout=15,
                     thread_id=None, allow_create=True, client_factory=AppServerClient):
    """Create/reuse/pin only this binding; never resume or dispatch any turn."""
    workspace = Path(workspace).expanduser().resolve()
    command = command or [os.environ.get("QINGTIAN_CODEX_BIN", "codex"), "app-server"]
    identity = _identity(workspace, command)
    try:
        with _lock(data_dir):
            state = _load(data_dir)
            if state.get("identity", identity) != identity:
                return read_status(data_dir, workspace=workspace, command=command)
            state.update(identity=identity, workspace=str(workspace), last_attempt_at=_now(),
                         available=False, pinned=None, pin_supported=None,
                         pin_evidence_source=None, pin_builtin_section_id=None,
                         pin_backend_version=None,
                         metadata_ready=False, workflow_ready=False,
                         status="initializing", error_code=None,
                         message=MESSAGES["initializing"], failed_method=None, rpc_code=None)
            try:
                if not workspace.is_dir() or not 0 < timeout <= 60:
                    raise EntryError("invalid_config")
                with client_factory(command, cwd=workspace, timeout=timeout) as client:
                    bound_id = thread_id or state.get("thread_id")
                    if bound_id:
                        thread = _thread(client.call("thread/read", {
                            "threadId": bound_id, "includeTurns": False,
                        }), workspace, bound_id)
                    else:
                        matches = _find_threads(client, workspace)
                        if len(matches) > 1:
                            state["candidate_ids"] = [item["id"] for item in matches]
                            raise EntryError("ambiguous")
                        if matches:
                            thread = _thread(client.call("thread/read", {
                                "threadId": matches[0]["id"], "includeTurns": False,
                            }), workspace, matches[0]["id"])
                        else:
                            if state.get("creation_pending"):
                                raise EntryError("creation_uncertain")
                            if not allow_create:
                                raise EntryError("not_found")
                            # Journal intent before the request. A lost response
                            # must never become permission to create again.
                            state["creation_pending"] = True
                            _save(data_dir, state)
                            try:
                                result = client.call("thread/start", {
                                    "cwd": str(workspace), "ephemeral": False,
                                    "developerInstructions": MANAGER_INSTRUCTIONS,
                                })
                            except EntryError as exc:
                                if exc.code == "unsupported":
                                    state["creation_pending"] = False
                                raise
                            raw_thread = result.get("thread", {})
                            if isinstance(raw_thread, dict) and isinstance(raw_thread.get("id"), str) and raw_thread["id"]:
                                state.update(thread_id=raw_thread["id"], creation_pending=False, bound_at=_now())
                                state["role_configuration"] = {
                                    **_unknown_role(), "source": "thread/start",
                                    "status": "submitted_at_creation", "submitted_at": _now(),
                                    "instruction_sha256": hashlib.sha256(MANAGER_INSTRUCTIONS.encode()).hexdigest(),
                                }
                                _save(data_dir, state)
                            thread = _thread(result, workspace)
                    if state.get("thread_id") != thread["id"]:
                        state.update(bound_at=None, pinned_at=None)
                        state["role_configuration"] = _unknown_role()
                    state.update(thread_id=thread["id"], creation_pending=False,
                                 bound_at=state.get("bound_at") or _now(), supported=True)
                    state.pop("candidate_ids", None)
                    _save(data_dir, state)
                    if thread.get("name") != MANAGER_NAME:
                        client.call("thread/name/set", {"threadId": thread["id"], "name": MANAGER_NAME})
                    pin_error = None
                    backend_version = getattr(client, "server_version", None)
                    section_pin = (backend_version in SECTION_PIN_VERSIONS
                                   and "isPinned" not in thread)
                    if section_pin:
                        try:
                            # This is a version-identified capability path, not
                            # a retry after permission, timeout, or metadata errors.
                            _section_inventory(client)
                            section = thread.get("section")
                            if not isinstance(section, dict) or section.get("id") != BUILTIN_PINNED_SECTION_ID:
                                client.call("thread/section/move", {
                                    "threadId": thread["id"], "sectionId": BUILTIN_PINNED_SECTION_ID,
                                })
                        except EntryError as exc:
                            pin_error = exc
                    elif thread.get("isPinned") is not True:
                        try:
                            client.call("thread/metadata/update", {"threadId": thread["id"], "isPinned": True})
                        except EntryError as exc:
                            pin_error = exc
                    verified = _thread(client.call("thread/read", {
                        "threadId": thread["id"], "includeTurns": False,
                    }), workspace, thread["id"])
                    if verified.get("name") != MANAGER_NAME:
                        raise EntryError("protocol_error", method="thread/name/set")
                    pinned = verified.get("isPinned")
                    pinned = pinned if type(pinned) is bool else None
                    evidence_source = "isPinned" if pinned is not None else None
                    if section_pin and pin_error is None and "isPinned" not in verified:
                        section = verified.get("section")
                        if "section" in verified and section is None:
                            pinned = False
                        elif isinstance(section, dict) and isinstance(section.get("id"), str) and section["id"]:
                            pinned = section["id"] == BUILTIN_PINNED_SECTION_ID
                        if pinned is not None:
                            evidence_source = "builtin_section"
                    state.update(available=True, pinned=pinned, last_synced_at=_now(),
                                 pin_evidence_source=evidence_source,
                                 pin_builtin_section_id=BUILTIN_PINNED_SECTION_ID if evidence_source == "builtin_section" else None,
                                 pin_backend_version=backend_version if evidence_source else None,
                                 pin_supported=True if pinned is not None else None)
                    if pinned is True:
                        state.update(metadata_ready=True, pinned_at=state.get("pinned_at") or _now())
                        _failure(state, EntryError("role_unverified"))
                    else:
                        if pinned is False:
                            state["pinned_at"] = None
                        if pin_error and pin_error.code == "unsupported":
                            state["pin_supported"] = False
                        elif (not section_pin and pinned is None and pin_error
                              and pin_error.code == "rpc_error" and pin_error.rpc_code == -32600):
                            # Older versions can ignore isPinned and reject the
                            # now-empty patch. Missing readback stays unknown.
                            pin_error = EntryError("pin_unverified", method="thread/metadata/update",
                                                   rpc_code=pin_error.rpc_code)
                        raise pin_error or EntryError("pin_not_applied" if pinned is False else "pin_unverified",
                                                      method="thread/section/move" if section_pin else "thread/metadata/update")
            except EntryError as exc:
                _failure(state, exc)
            _save(data_dir, state)
            return read_status(data_dir, workspace=workspace, command=command)
    except EntryError as exc:
        return _failure(_empty(), exc)
    except OSError:
        return _failure(_empty(), EntryError("io_error"))


def configured_command(*, transport=None, socket=None, codex_bin=None):
    transport = transport or os.environ.get("QINGTIAN_CODEX_TRANSPORT", "stdio")
    socket = socket or os.environ.get("QINGTIAN_CODEX_SOCKET")
    command = [codex_bin or os.environ.get("QINGTIAN_CODEX_BIN", "codex"), "app-server"]
    if transport not in {"stdio", "proxy"} or (socket and transport != "proxy"):
        raise EntryError("invalid_config")
    if transport == "proxy":
        command.append("proxy")
        if socket:
            command.extend(["--sock", str(Path(socket).expanduser())])
    return command


def initialize_from_environment(data_dir, workspace):
    try:
        return initialize_entry(data_dir, workspace, command=configured_command())
    except EntryError as exc:
        return _record_configuration_error(data_dir, exc)


def main(argv=None):
    from .config import default_data_dir, default_workspace
    parser = argparse.ArgumentParser(prog="qingtian manager-entry")
    parser.add_argument("action", choices=("init", "sync", "status", "guide", "inspect", "instructions"))
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--workspace", type=Path, default=default_workspace())
    parser.add_argument("--thread-id", help="Explicitly bind a verified existing thread in this workspace")
    parser.add_argument("--transport", choices=("stdio", "proxy"))
    parser.add_argument("--socket")
    parser.add_argument("--codex-bin")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)
    if args.action in {"status", "guide", "inspect"}:
        try:
            command = configured_command(transport=args.transport, socket=args.socket, codex_bin=args.codex_bin)
            if args.action == "inspect":
                result = inspect_entry(args.data_dir, args.workspace, command=command,
                                       timeout=args.timeout, thread_id=args.thread_id)
            else:
                result = read_status(args.data_dir.expanduser().resolve(), workspace=args.workspace, command=command)
                if args.action == "guide":
                    result["onboarding"] = _onboarding(result, args.data_dir,
                        args.workspace.expanduser().resolve(), command, selected_id=args.thread_id)
        except EntryError as exc:
            result = _failure(_empty(), exc)
            result["onboarding"] = _onboarding(result, args.data_dir,
                args.workspace.expanduser().resolve(), None)
    elif args.action == "instructions":
        result = {"role_instructions": MANAGER_INSTRUCTIONS, "role_configuration": _unknown_role(),
                  "instruction_sha256": hashlib.sha256(MANAGER_INSTRUCTIONS.encode()).hexdigest()}
    else:
        try:
            result = initialize_entry(
                args.data_dir.expanduser().resolve(), args.workspace,
                command=configured_command(transport=args.transport, socket=args.socket, codex_bin=args.codex_bin),
                timeout=args.timeout, thread_id=args.thread_id, allow_create=args.action == "init",
            )
        except EntryError as exc:
            result = _record_configuration_error(args.data_dir.expanduser().resolve(), exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "inspect":
        return 0 if result.get("inspection", {}).get("can_bind") is True else 2
    return 0 if args.action in {"status", "guide", "instructions"} or result.get("workflow_ready") is True else 2
