"""Fixed, isolated capability checks for the synthetic local demo.

No caller-supplied command, URL, path, workspace, or provider is accepted. Browser
checks use an optional Playwright installation and never install prerequisites.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from http.client import HTTPConnection
import importlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from threading import Lock, RLock, Thread
from time import monotonic
from typing import Any, Iterator
from urllib.parse import urlsplit
import uuid

from .models import utc_now


CAPABILITY_IDS = ("api-e2e", "browser-e2e")
# This is the closed set of successful assertions, shared with the public
# capability work-tree contract. Errors/blocked prerequisites use separate
# diagnostic checks and never count as a successful capability run.
_FINAL_CHECK_SUFFIXES = ("done", "runs", "evidence", "checkpoint", "contracts")
EXPECTED_CHECK_IDS = {
    "api-e2e": frozenset({
        "api.fresh_task", "api.stale_step", "api.unknown", "api.reconcile",
        *(f"api.step.{step}" for step in range(1, 8)),
        *("api." + suffix for suffix in _FINAL_CHECK_SUFFIXES),
    }),
    "browser-e2e": frozenset({
        "browser.reload", "browser.mobile.layout", "browser.javascript", "browser.network",
        *(f"browser.{viewport}.{suffix}" for viewport in ("desktop", "mobile") for suffix in ("fresh_task", "unknown", *_FINAL_CHECK_SUFFIXES)),
        *(f"browser.{viewport}.step.{step}" for viewport in ("desktop", "mobile") for step in range(1, 8)),
        *(f"browser.{viewport}.mindmap.{suffix}" for viewport in ("desktop", "mobile") for suffix in ("leaders", "tree", "readonly")),
    }),
}
PROCESS_TIMEOUT_SECONDS = 55
BROWSER_DEADLINE_SECONDS = 45
SETUP_COMMANDS = ["python -m pip install playwright", "python -m playwright install chromium"]


class CapabilityBusy(RuntimeError):
    """A fixed check is already running, or its owning server is closing."""


class CheckFailed(RuntimeError):
    pass


class CheckBlocked(RuntimeError):
    def __init__(self, reason_code: str, detail: str, setup_commands: list[str]):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail
        self.setup_commands = setup_commands


def _check_id(capability_id: str) -> None:
    if not isinstance(capability_id, str) or capability_id not in CAPABILITY_IDS:
        raise ValueError("unsupported capability_id")


def _receipt(capability_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": "capability_" + uuid.uuid4().hex,
        "capability_id": capability_id,
        "status": "failed",
        "scope": "synthetic-local-demo",
        "started_at": utc_now(),
        "duration_ms": 0,
        "checks": [],
        "setup_commands": [],
        "task_ids": [],
    }


def _require(receipt: dict[str, Any], condition: bool, check_id: str, detail: str) -> None:
    receipt["checks"].append({"id": check_id, "status": "passed" if condition else "failed", "detail": detail})
    if not condition:
        raise CheckFailed(check_id)


@contextmanager
def _synthetic_server() -> Iterator[Any]:
    from .demo_web import DemoHTTPServer

    server = DemoHTTPServer(0)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request(server: Any, method: str, route: str, payload: dict[str, Any] | None = None, token: str | None = None) -> tuple[int, dict[str, Any]]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Origin": server.url}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if token is not None:
        headers["X-Qingtian-Demo-Token"] = token
    try:
        connection.request(method, route, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read(1024 * 1024))
        return response.status, result
    finally:
        connection.close()


def _state(server: Any) -> dict[str, Any]:
    status, state = _request(server, "GET", "/api/state")
    if status != 200:
        raise CheckFailed("state_endpoint")
    return state


def _check_finished(receipt: dict[str, Any], state: dict[str, Any], prefix: str) -> None:
    from .contracts import bundled_schema, validate

    records = state["records"]
    _require(receipt, state["done"] is True and state["step"] == 7 and state["task"]["state"] == "DONE", prefix + ".done", "The seven-step synthetic task reached DONE.")
    _require(receipt, len(records["runs"]) == 3 and all(run["state"] == "SUCCEEDED" for run in records["runs"]), prefix + ".runs", "All three persisted synthetic runs succeeded after reconciliation.")
    passed = {item["subject_id"] for item in records["evidence"] if item["subject_type"] == "run" and item["result"] == "passed"}
    _require(receipt, all(run["run_id"] in passed for run in records["runs"]), prefix + ".evidence", "Every run has matching passed Evidence.")
    _require(receipt, any(checkpoint["task_revision"] == state["task"]["revision"] for checkpoint in records["checkpoints"]), prefix + ".checkpoint", "A final checkpoint matches the current Task revision.")
    validate(state["task"], bundled_schema("task"))
    for collection, schema in (("sessions", "session"), ("runs", "run"), ("evidence", "evidence"), ("checkpoints", "checkpoint"), ("knowledge", "knowledge")):
        for record in records[collection]:
            validate(record, bundled_schema(schema))
    _require(receipt, True, prefix + ".contracts", "Persisted records satisfy the bundled core contracts and integrity checks.")


def _api_check(receipt: dict[str, Any]) -> None:
    with _synthetic_server() as server:
        state = _state(server)
        receipt["task_ids"].append(state["task"]["task_id"])
        _require(receipt, state["step"] == 0 and state["task"]["state"] == "DRAFT", "api.fresh_task", "A new isolated synthetic Task starts at DRAFT.")
        token = state["csrf_token"]
        for expected in range(7):
            status, state = _request(server, "POST", "/api/step", {"expected_step": expected}, token)
            _require(receipt, status == 200 and state.get("step") == expected + 1, f"api.step.{expected + 1}", "The fixed step advanced exactly once through the real HTTP endpoint.")
            if expected == 0:
                status, _ = _request(server, "POST", "/api/step", {"expected_step": 0}, token)
                _require(receipt, status == 409 and _state(server)["step"] == 1, "api.stale_step", "A duplicate expected_step was rejected without another transition.")
            if expected == 3:
                _require(receipt, any(run["state"] == "UNKNOWN" for run in state["records"]["runs"]), "api.unknown", "Acknowledgement loss is preserved as an actual UNKNOWN Run.")
            if expected == 4:
                _require(receipt, all(run["state"] == "SUCCEEDED" for run in state["records"]["runs"]) and len(state["records"]["sessions"]) == 2, "api.reconcile", "The new Session reconciled UNKNOWN before the verification Run completed.")
        _check_finished(receipt, state, "api")


def _load_playwright() -> Any:
    return importlib.import_module("playwright.sync_api")


def _browser_check(receipt: dict[str, Any]) -> None:
    try:
        playwright = _load_playwright()
    except (ImportError, ModuleNotFoundError):
        raise CheckBlocked("playwright_missing", "The optional Python Playwright package is not installed.", list(SETUP_COMMANDS)) from None
    deadline = monotonic() + BROWSER_DEADLINE_SECONDS

    def timeout() -> int:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CheckFailed("browser_deadline")
        return max(1, min(5000, int(remaining * 1000)))

    try:
        manager = playwright.sync_playwright()
        runtime = manager.start()
    except Exception:
        raise CheckBlocked("playwright_runtime_unavailable", "The local Playwright runtime could not start.", list(SETUP_COMMANDS)) from None
    browser = None
    try:
        # Both are standard Playwright launch targets. No machine-specific
        # executable path, existing profile, or remote browser is accepted.
        for channel in (None, "chrome"):
            try:
                arguments: dict[str, Any] = {"headless": True, "timeout": timeout()}
                if channel:
                    arguments["channel"] = channel
                browser = runtime.chromium.launch(**arguments)
                receipt["browser"] = {"name": "chromium" if channel is None else "chrome", "version": browser.version}
                break
            except Exception:
                continue
        if browser is None:
            raise CheckBlocked("browser_unavailable", "Neither the Playwright Chromium browser nor an installed Chrome could launch.", ["python -m playwright install chromium"])
        with _synthetic_server() as server:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block", reduced_motion="reduce")
            unexpected_requests: list[str] = []
            page_errors: list[str] = []
            mutation_requests: list[str] = []

            def route_request(route: Any) -> None:
                target = urlsplit(route.request.url)
                if target.scheme == "http" and target.hostname == "127.0.0.1" and target.port == server.server_port:
                    if route.request.method not in {"GET", "HEAD"}:
                        mutation_requests.append("application-mutation-request")
                    route.continue_()
                else:
                    unexpected_requests.append("non-loopback-application-request")
                    route.abort()

            context.route("**/*", route_request)
            if hasattr(context, "route_web_socket"):
                def block_websocket(websocket: Any) -> None:
                    unexpected_requests.append("unexpected-application-websocket")
                    websocket.close()
                context.route_web_socket("**/*", block_websocket)
            page = context.new_page()
            page.on("pageerror", lambda _error: page_errors.append("uncaught-javascript-error"))
            page.set_default_timeout(5000)

            def check_mindmap(label: str) -> None:
                before = _state(server)
                mutations_before = len(mutation_requests)
                status, catalog = _request(server, "GET", "/api/capabilities")
                if status != 200:
                    raise CheckFailed("mindmap_catalog")
                phases = catalog["phases"]
                for phase in phases:
                    button = page.locator(f'#step-phase-{phase["step"]}')
                    button.click(timeout=timeout())
                    playwright.expect(button).to_have_attribute("aria-expanded", "true", timeout=timeout())
                    playwright.expect(button).to_have_attribute("aria-pressed", "true", timeout=timeout())
                    playwright.expect(page.locator("#phase-leader-name")).to_have_text(phase["leader"]["name"], timeout=timeout())
                    playwright.expect(page.locator("#phase-leader-mission")).to_have_text(phase["leader"]["mission"], timeout=timeout())
                    for capability_id in phase["capability_ids"]:
                        playwright.expect(page.locator(f'[data-agent-id="{capability_id}"]')).to_be_visible(timeout=timeout())
                _require(receipt, len(phases) == 7, f"browser.{label}.mindmap.leaders", "All seven Leader branches can be selected and reveal their Agent nodes.")
                page.locator("#step-phase-5").click(timeout=timeout())
                agent_toggle = page.locator('[data-agent-toggle="browser-e2e"]')
                if agent_toggle.get_attribute("aria-expanded") != "true":
                    agent_toggle.click(timeout=timeout())
                playwright.expect(agent_toggle).to_have_attribute("aria-expanded", "true", timeout=timeout())
                playwright.expect(page.locator('[data-agent-detail="browser-e2e"]')).to_be_visible(timeout=timeout())
                agent = next(item["agent"] for item in catalog["capabilities"] if item["id"] == "browser-e2e")
                branch = next(item for item in agent["work_items"] if item["children"])
                expanded = 0

                def expand_work(node: dict[str, Any]) -> None:
                    nonlocal expanded
                    container = page.locator(f'[data-work-node="{node["id"]}"]')
                    playwright.expect(container).to_be_visible(timeout=timeout())
                    toggle = page.locator(f'[data-work-toggle="{node["id"]}"]')
                    if toggle.get_attribute("aria-expanded") != "true":
                        toggle.click(timeout=timeout())
                    playwright.expect(toggle).to_have_attribute("aria-expanded", "true", timeout=timeout())
                    detail = page.locator(f'[data-work-detail="{node["id"]}"]')
                    playwright.expect(detail).to_be_visible(timeout=timeout())
                    playwright.expect(detail).to_contain_text(node["description"], timeout=timeout())
                    if node["check_ids"]:
                        badges = detail.locator(".work-check-badge")
                        playwright.expect(badges).to_have_count(len(node["check_ids"]), timeout=timeout())
                        for index, check_id in enumerate(node["check_ids"]):
                            playwright.expect(badges.nth(index)).to_contain_text(check_id, timeout=timeout())
                            playwright.expect(badges.nth(index)).to_have_attribute("data-tone", "unrun", timeout=timeout())
                    expanded += 1
                    for child in node["children"]:
                        expand_work(child)

                expand_work(branch)
                _require(receipt, expanded > 0, f"browser.{label}.mindmap.tree", "The browser Agent and recursive work details expand with linked assertions still marked unrun, without executing the capability.")
                _require(receipt, _state(server) == before and len(mutation_requests) == mutations_before, f"browser.{label}.mindmap.readonly", "Leader, Agent and work-node navigation left the entire main Task snapshot unchanged and sent no mutation request.")

            try:
                page.goto(server.url + "/", wait_until="load", timeout=timeout())
                playwright.expect(page.locator("#step-value")).to_have_text("0", timeout=timeout())
                playwright.expect(page.locator("#next-step")).to_be_enabled(timeout=timeout())
                initial = _state(server)
                receipt["task_ids"].append(initial["task"]["task_id"])
                _require(receipt, initial["step"] == 0 and initial["task"]["state"] == "DRAFT", "browser.desktop.fresh_task", "The desktop browser opened a new isolated synthetic task.")
                for label in ("desktop", "mobile"):
                    if label == "mobile":
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.locator("#reset-button").click(timeout=timeout())
                        page.locator("#reset-button").click(timeout=timeout())
                        playwright.expect(page.locator("#step-value")).to_have_text("0", timeout=timeout())
                        fresh = _state(server)
                        _require(receipt, fresh["task"]["task_id"] not in receipt["task_ids"] and fresh["step"] == 0, "browser.mobile.fresh_task", "The 390-pixel viewport starts its own fresh task via the reset button.")
                        receipt["task_ids"].append(fresh["task"]["task_id"])
                    check_mindmap(label)
                    for expected in range(7):
                        page.locator("#next-step").click(timeout=timeout())
                        playwright.expect(page.locator("#step-value")).to_have_text(str(expected + 1), timeout=timeout())
                        state = _state(server)
                        _require(receipt, state["step"] == expected + 1, f"browser.{label}.step.{expected + 1}", "A real browser click advanced the matching persisted HTTP state.")
                        if expected == 3:
                            _require(receipt, any(run["state"] == "UNKNOWN" for run in state["records"]["runs"]), f"browser.{label}.unknown", "The browser reached the real UNKNOWN handoff checkpoint.")
                        if label == "desktop" and expected == 2:
                            task_id = state["task"]["task_id"]
                            page.reload(wait_until="load", timeout=timeout())
                            playwright.expect(page.locator("#step-value")).to_have_text("3", timeout=timeout())
                            _require(receipt, _state(server)["task"]["task_id"] == task_id, "browser.reload", "Reload restored the same task and its current step without replaying work.")
                    _check_finished(receipt, state, "browser." + label)
                    playwright.expect(page.locator("#done-banner")).to_be_visible(timeout=timeout())
                    if label == "mobile":
                        fits = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                        _require(receipt, fits, "browser.mobile.layout", "The 390-pixel page has no horizontal document overflow.")
                _require(receipt, not page_errors, "browser.javascript", "No uncaught JavaScript errors were observed during both rounds.")
                _require(receipt, not unexpected_requests, "browser.network", "No application request left the isolated loopback origin; external requests were blocked.")
            finally:
                context.close()
    finally:
        if browser is not None:
            browser.close()
        runtime.stop()


def run_capability(capability_id: str) -> dict[str, Any]:
    """Run one fixed check in this process; public callers use CapabilityRunner."""
    _check_id(capability_id)
    receipt = _receipt(capability_id)
    started = monotonic()
    try:
        (_api_check if capability_id == "api-e2e" else _browser_check)(receipt)
        ids = [check["id"] for check in receipt["checks"]]
        if len(ids) != len(set(ids)) or set(ids) != EXPECTED_CHECK_IDS[capability_id] or any(check["status"] != "passed" for check in receipt["checks"]):
            raise CheckFailed("incomplete_fixed_check_set")
        receipt["status"] = "passed"
    except CheckBlocked as error:
        receipt["status"] = "blocked"
        receipt["reason_code"] = error.reason_code
        receipt["setup_commands"] = error.setup_commands
        receipt["checks"].append({"id": "environment", "status": "blocked", "detail": error.detail})
    except CheckFailed:
        receipt["reason_code"] = "check_failed"
        if not any(check["status"] == "failed" for check in receipt["checks"]):
            receipt["checks"].append({"id": "execution", "status": "failed", "detail": "A fixed end-to-end assertion did not complete successfully."})
    except KeyboardInterrupt:
        receipt["reason_code"] = "capability_cancelled"
        receipt["checks"].append({"id": "execution", "status": "failed", "detail": "The isolated check was cancelled."})
    except Exception:
        receipt["reason_code"] = "capability_execution_error"
        receipt["checks"].append({"id": "execution", "status": "failed", "detail": "The fixed check encountered an execution error; no internal exception text is exposed."})
    receipt["duration_ms"] = max(0, int((monotonic() - started) * 1000))
    return receipt


def _valid_receipt(value: Any, capability_id: str) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("capability_id") != capability_id:
        return False
    if value.get("scope") != "synthetic-local-demo" or value.get("status") not in {"passed", "failed", "blocked"}:
        return False
    required = {"schema_version", "run_id", "capability_id", "status", "scope", "started_at", "duration_ms", "checks", "setup_commands", "task_ids"}
    if not required <= set(value) or set(value) - required - {"reason_code", "browser"}:
        return False
    if "reason_code" in value and (not isinstance(value["reason_code"], str) or not re.fullmatch(r"[a-z_]{1,80}", value["reason_code"])):
        return False
    if not isinstance(value.get("run_id"), str) or not re.fullmatch(r"capability_[0-9a-f]{32}", value["run_id"]):
        return False
    if not isinstance(value.get("started_at"), str) or type(value.get("duration_ms")) is not int or value["duration_ms"] < 0:
        return False
    if not isinstance(value.get("checks"), list) or len(value["checks"]) > 100:
        return False
    for check in value["checks"]:
        if not isinstance(check, dict) or set(check) != {"id", "status", "detail"} or check.get("status") not in {"passed", "failed", "blocked"}:
            return False
        if any(not isinstance(check.get(name), str) or len(check[name]) > 2000 for name in ("id", "detail")):
            return False
    statuses = {check["status"] for check in value["checks"]}
    if value["status"] == "passed" and statuses != {"passed"}:
        return False
    if value["status"] == "blocked" and ("blocked" not in statuses or "failed" in statuses):
        return False
    if value["status"] == "failed" and "failed" not in statuses:
        return False
    if not isinstance(value.get("task_ids"), list) or any(not isinstance(item, str) or not re.fullmatch(r"task_[A-Za-z0-9_-]+", item) for item in value["task_ids"]):
        return False
    if not isinstance(value.get("setup_commands"), list) or any(item not in SETUP_COMMANDS for item in value["setup_commands"]):
        return False
    if "browser" in value and (not isinstance(value["browser"], dict) or set(value["browser"]) != {"name", "version"} or any(not isinstance(item, str) for item in value["browser"].values())):
        return False
    return True


class CapabilityRunner:
    """One bounded subprocess at a time; ownership is scoped to one demo server."""

    def __init__(self) -> None:
        self._run_lock = Lock()
        self._process_lock = RLock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._closed = False

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        # Only processes registered immediately after our start_new_session
        # launch own a group here. Serialize cleanup and remove ownership once
        # finished: repeated close/finally paths must never signal a reused PID.
        with self._process_lock:
            if process not in self._processes:
                return
            try:
                if os.name == "posix":
                    # The leader may already have exited while its browser or
                    # driver is still alive in the dedicated process group.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                if os.name == "posix":
                    # Always signal remaining group members, even when wait()
                    # successfully reaped the leader. Grandchildren are reaped
                    # by their OS parent, not by this Python process.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            except (ProcessLookupError, OSError):
                pass
            finally:
                self._processes.discard(process)

    def close(self) -> None:
        with self._process_lock:
            self._closed = True
            processes = tuple(self._processes)
        for process in processes:
            self._terminate(process)

    def run(self, capability_id: str) -> dict[str, Any]:
        _check_id(capability_id)
        if not self._run_lock.acquire(blocking=False):
            raise CapabilityBusy("capability_busy")
        receipt = _receipt(capability_id)
        started = monotonic()
        process = None
        try:
            with tempfile.TemporaryDirectory(prefix="qingtian-capability-") as temporary, tempfile.TemporaryFile() as output, ExitStack() as cleanup:
                environment = {name: os.environ[name] for name in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME", "DISPLAY", "WAYLAND_DISPLAY", "LANG", "LC_ALL") if name in os.environ}
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                # All child demo databases and browser profiles belong to this
                # disposable root, including after a forced timeout or shutdown.
                environment.update({"TMPDIR": temporary, "TEMP": temporary, "TMP": temporary})
                with self._process_lock:
                    if self._closed:
                        raise CapabilityBusy("capability_busy")
                    process = subprocess.Popen(
                        [sys.executable, "-B", "-m", "qingtian_core.capability_checks", "--capability", capability_id],
                        cwd=temporary, env=environment, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.DEVNULL, start_new_session=os.name == "posix",
                    )
                    self._processes.add(process)
                    cleanup.callback(self._terminate, process)
                try:
                    process.wait(timeout=PROCESS_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    self._terminate(process)
                    receipt["reason_code"] = "capability_timeout"
                    receipt["checks"].append({"id": "execution", "status": "failed", "detail": "The fixed check exceeded its 55-second execution limit."})
                    return receipt
                output.seek(0)
                encoded = output.read(65537)
                try:
                    result = json.loads(encoded) if len(encoded) <= 65536 else None
                except (ValueError, UnicodeError):
                    result = None
                if process.returncode != 0 or not _valid_receipt(result, capability_id):
                    receipt["reason_code"] = "invalid_runner_receipt"
                    receipt["checks"].append({"id": "execution", "status": "failed", "detail": "The isolated runner did not return a valid bounded receipt."})
                    return receipt
                return result
        except CapabilityBusy:
            raise
        except Exception:
            receipt["reason_code"] = "runner_start_failed"
            receipt["checks"].append({"id": "execution", "status": "failed", "detail": "The isolated fixed runner could not execute."})
            return receipt
        finally:
            if process is not None:
                self._terminate(process)
                with self._process_lock:
                    self._processes.discard(process)
            receipt["duration_ms"] = max(0, int((monotonic() - started) * 1000))
            self._run_lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a fixed synthetic capability check")
    parser.add_argument("--capability", choices=CAPABILITY_IDS, default="browser-e2e")
    args = parser.parse_args(argv)
    if hasattr(signal, "SIGTERM"):
        def cancelled(_signal: int, _frame: Any) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, cancelled)
    print(json.dumps(run_capability(args.capability), ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
