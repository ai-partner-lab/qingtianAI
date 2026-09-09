from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlparse

from .config import DEFAULT_PORT, default_data_dir, default_workspace, ensure_data_dirs
from .coordinator import RecoveryCoordinator
from .db import Database, utc_now
from .intake import (
    AttachmentUpload,
    CodexPlannerAdapter,
    IntakeError,
    IntakeService,
)
from .reporting import daily_report_payload
from .runner import RunManager
from .runtime import InstanceLock, publish_server_pid, remove_server_pid
from .runtime_mode import ENGINE_MODES, background_cycle, validate_mode
from .multipart import MAX_MULTIPART_BYTES, MultipartError, MultipartForm, parse_multipart, read_body
from .service import ControlPlane


STATIC_DIR = Path(__file__).resolve().parent / "static"


class ControlPlaneHandler(BaseHTTPRequestHandler):
    service: ControlPlane
    manager: RunManager
    intakes: IntakeService
    watchdog_health: Dict[str, Any] = {}
    coordinator: Optional[RecoveryCoordinator] = None
    engine_mode = "manual"
    workspace = ""
    synthetic_tour = False
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid persisting request paths or user input in access logs.
        return

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' blob: data:",
        )
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.exists() or STATIC_DIR not in path.resolve().parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self' blob: data:",
        )
        self.end_headers()
        self.wfile.write(body)

    def _write_sse(
        self, event: str, event_id: int, payload: Optional[Dict[str, Any]] = None
    ) -> None:
        lines = ["id: {}".format(max(0, int(event_id))), "event: {}".format(event)]
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            lines.append("data: {}".format(data.replace("\n", "\\n")))
        self.wfile.write(("\n".join(lines) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _stream_events(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        raw_cursor = query.get(
            "lastEventId", [self.headers.get("Last-Event-ID", "0")]
        )[0]
        try:
            cursor = max(0, int(raw_cursor or 0))
        except (TypeError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid event cursor"})
            return

        # SSE owns this HTTP connection until the client disconnects. Prevent
        # BaseHTTPRequestHandler from trying to parse a follow-up request on a
        # socket that a browser/curl has already reset after the stream closes.
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        try:
            self.wfile.write(b"retry: 3000\n\n")
            requested_cursor = cursor
            initial = self.service.realtime_payload(after=cursor)
            if self.coordinator is not None:
                initial.setdefault("dashboard", {})["qingtian_v2"] = (
                    self.coordinator.status()
                )
            cursor = int(initial["version"])
            initial["reset"] = cursor < requested_cursor
            self._write_sse("snapshot", cursor, initial)
            last_heartbeat = time.monotonic()
            while True:
                time.sleep(0.75)
                latest = self.service.event_cursor()
                if latest > cursor:
                    payload = self.service.realtime_payload(after=cursor)
                    if self.coordinator is not None:
                        payload.setdefault("dashboard", {})["qingtian_v2"] = (
                            self.coordinator.status()
                        )
                    cursor = int(payload["version"])
                    self._write_sse("dashboard", cursor, payload)
                    last_heartbeat = time.monotonic()
                elif time.monotonic() - last_heartbeat >= 15:
                    self._write_sse("heartbeat", cursor, {"version": cursor})
                    last_heartbeat = time.monotonic()
        except Exception:
            # Client disconnects and test teardown can race the next SQLite poll;
            # one stream must never leak a handler traceback or affect the server.
            return

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body(64 * 1024)
        if not raw:
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _read_body(self, maximum: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise IntakeError("Transfer-Encoding is not supported")
        if hasattr(self.headers, "get_all") and len(self.headers.get_all("Content-Length", [])) > 1:
            self.close_connection = True
            raise IntakeError("Duplicate Content-Length")
        try:
            return read_body(self.rfile, self.headers.get("Content-Length", "0"), maximum)
        except (MultipartError, TimeoutError) as exc:
            self.close_connection = True
            raise IntakeError(str(exc), status=getattr(exc, "status", 400)) from exc

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in {"http://" + host, "https://" + host}

    def _valid_host(self) -> bool:
        """Reject DNS-rebinding hostnames even when Origin agrees with Host."""
        raw_host = self.headers.get("Host", "")
        try:
            parsed = urlparse("http://" + raw_host)
            expected_port = int(self.server.server_address[1])
            return (
                parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.username is None and parsed.password is None
                and not parsed.path and not parsed.query and not parsed.fragment
                and (parsed.port or 80) == expected_port
            )
        except (ValueError, TypeError, AttributeError):
            return False

    def _read_multipart(
        self,
    ) -> tuple[Dict[str, str], list[AttachmentUpload], MultipartForm]:
        content_type = self.headers.get("Content-Type", "")
        try:
            form = parse_multipart(content_type, self._read_body(MAX_MULTIPART_BYTES))
        except MultipartError as exc:
            raise IntakeError(str(exc), status=exc.status) from exc
        uploads = [AttachmentUpload(name=item.filename, mime=item.content_type, file=item.stream)
                   for item in form.files]
        return form.fields, uploads, form

    def _dispatch_intake(self, task_id: str, prompt_text: str) -> Dict[str, Any]:
        prompt = self.manager.paths["prompts"] / "intake-{}-{}.txt".format(
            task_id, os.urandom(5).hex()
        )
        prompt.write_text(prompt_text, encoding="utf-8")
        os.chmod(prompt, 0o600)
        try:
            return self.manager.dispatch(task_id, prompt)
        finally:
            try:
                prompt.unlink()
            except OSError:
                pass

    def _attachment(self, intake_id: str, attachment_id: str) -> None:
        try:
            metadata, body = self.intakes.read_attachment(intake_id, attachment_id)
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "attachment not found"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", metadata["mime"])
        self.send_header("Content-Length", str(len(body)))
        disposition = "inline" if metadata["mime"].startswith("image/") else "attachment"
        self.send_header(
            "Content-Disposition",
            "{}; filename*=UTF-8''{}".format(
                disposition, quote(metadata["name"], safe="")
            ),
        )
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._valid_host():
            self._json(HTTPStatus.FORBIDDEN, {"error": "loopback Host and matching port required"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self._file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
        elif parsed.path == "/styles.css":
            self._file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
        elif parsed.path == "/api/health":
            watchdog = dict(self.watchdog_health)
            last_success_monotonic = float(
                watchdog.pop("_last_success_monotonic", 0.0) or 0.0
            )
            age_seconds = (
                max(0, int(time.monotonic() - last_success_monotonic))
                if last_success_monotonic
                else None
            )
            watchdog["age_seconds"] = age_seconds
            watchdog["ok"] = bool(
                age_seconds is not None
                and age_seconds <= 45
                and int(watchdog.get("consecutive_errors", 0)) == 0
            )
            self._json(
                HTTPStatus.OK if watchdog["ok"] else HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": bool(watchdog["ok"]),
                    "service": "qingtian-engine",
                    "pid": os.getpid(),
                    "mode": self.engine_mode,
                    "automatic_dispatch": self.engine_mode == "auto",
                    "read_only": self.synthetic_tour,
                    "synthetic": self.synthetic_tour,
                    "data_dir": str(self.manager.data_dir),
                    "workspace": self.workspace,
                    "watchdog": watchdog,
                },
            )
        elif parsed.path == "/api/dashboard":
            payload = self.service.dashboard_payload()
            if self.coordinator is not None:
                payload["qingtian_v2"] = self.coordinator.status()
            self._json(HTTPStatus.OK, payload)
        elif parsed.path == "/api/v2/orchestrator":
            if self.coordinator is None:
                self._json(HTTPStatus.NOT_FOUND, {"enabled": False})
            else:
                self._json(HTTPStatus.OK, self.coordinator.status())
        elif parsed.path == "/api/events/stream":
            self._stream_events(parsed)
        elif parsed.path == "/api/report":
            self._json(HTTPStatus.OK, daily_report_payload(self.service))
        elif parsed.path == "/api/intakes":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["30"])[0])
            self._json(
                HTTPStatus.OK,
                {"intakes": self.intakes.list_intakes(limit=limit)},
            )
        elif parsed.path.startswith("/api/intakes/"):
            parts = parsed.path.strip("/").split("/")
            try:
                if len(parts) == 3:
                    self._json(HTTPStatus.OK, self.intakes.get_intake(parts[2]))
                elif len(parts) == 5 and parts[3] == "attachments":
                    self._attachment(parts[2], parts[4])
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "intake not found"})
            except IntakeError as exc:
                self._json(exc.status, {"error": str(exc)})
        elif parsed.path.startswith("/api/tasks/"):
            task_id = parsed.path.split("/")[-1]
            try:
                self._json(HTTPStatus.OK, self.service.get_task(task_id))
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.synthetic_tour:
            # Reject before parsing or buffering input. An unread body must not
            # become a follow-up request on this HTTP/1.1 connection.
            self.close_connection = True
            self._json(HTTPStatus.FORBIDDEN, {
                "error": "The synthetic tour is read-only; use quickstart for new tasks.",
                "code": "synthetic-tour-read-only",
                "read_only": True,
                "synthetic": True,
            })
            return
        if not self._valid_host():
            self._json(HTTPStatus.FORBIDDEN, {"error": "loopback Host and matching port required"})
            return
        if not self._same_origin():
            self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin mutation rejected"})
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/intakes":
                fields, uploads, _form = self._read_multipart()
                try:
                    advanced = json.loads(fields.get("advanced", "{}"))
                except json.JSONDecodeError as exc:
                    if _form is not None:
                        _form.close()
                    raise IntakeError("高级设置 JSON 无效") from exc
                try:
                    result = self.intakes.create_intake(
                        text=fields.get("text", ""),
                        # Missing intent is not permission to execute in manual mode.
                        intent=fields.get(
                            "intent", "analyze" if self.engine_mode == "manual" else "implement"
                        ),
                        uploads=uploads,
                        idempotency_key=fields.get(
                            "idempotency_key",
                            self.headers.get("Idempotency-Key", ""),
                        ),
                        advanced=advanced,
                        dispatcher=self._dispatch_intake,
                    )
                finally:
                    if _form is not None:
                        _form.close()
                status = HTTPStatus.OK if result.get("reused") else HTTPStatus.CREATED
                self._json(status, result)
                return
            if parsed.path.startswith("/api/intakes/") and parsed.path.endswith("/retry"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 4:
                    raise IntakeError("invalid intake action")
                result = self.intakes.retry_intake(
                    parts[2], dispatcher=self._dispatch_intake
                )
                self._json(HTTPStatus.OK, result)
                return
            payload = self._read_json()
            if parsed.path == "/api/tasks":
                instruction = str(payload.pop("instruction", ""))
                auto_start = payload.pop("auto_start", False)
                if not isinstance(auto_start, bool):
                    raise ValueError("auto_start must be a boolean")
                requires_deploy = payload.get("requires_deploy", False)
                if not isinstance(requires_deploy, bool):
                    raise ValueError("requires_deploy must be a boolean")
                task = self.service.create_task(
                    title=str(payload.get("title", "")),
                    scope_summary=str(payload.get("scope_summary", "")),
                    priority=int(payload.get("priority", 2)),
                    environment=str(payload.get("environment", "local")),
                    repository=str(payload.get("repository", "")),
                    base_branch=str(payload.get("base_branch", "")),
                    worker_type=str(payload.get("worker_type", "auto")),
                    requires_deploy=requires_deploy,
                )
                if auto_start and instruction:
                    prompt = self.manager.paths["prompts"] / "web-{}.txt".format(task["id"])
                    prompt.write_text(instruction, encoding="utf-8")
                    os.chmod(prompt, 0o600)
                    try:
                        self.manager.dispatch(task["id"], prompt)
                    finally:
                        try:
                            prompt.unlink()
                        except OSError:
                            pass
                    task = self.service.get_task(task["id"])
                self._json(HTTPStatus.CREATED, task)
                return
            if parsed.path.startswith("/api/tasks/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("invalid task action")
                task_id, action = parts[2], parts[3]
                if action == "dispatch":
                    instruction = payload.get("instruction", "")
                    if not isinstance(instruction, str) or not instruction.strip():
                        raise ValueError("dispatch requires a nonempty instruction")
                    resume = payload.get("resume", False)
                    if not isinstance(resume, bool):
                        raise ValueError("resume must be a boolean")
                    prompt = self.manager.paths["prompts"] / "dispatch-{}-{}.txt".format(
                        task_id, os.urandom(5).hex()
                    )
                    prompt.write_text(instruction, encoding="utf-8")
                    os.chmod(prompt, 0o600)
                    try:
                        run = self.manager.dispatch(task_id, prompt, resume=resume)
                    finally:
                        prompt.unlink(missing_ok=True)
                    result = {"task": self.service.get_task(task_id), "run": run}
                elif action == "cancel":
                    result = self.manager.cancel(task_id)
                elif action == "plan":
                    result = self.service.transition(
                        task_id,
                        "PLANNED",
                        producer="dashboard",
                        summary="看板确认进入计划",
                        dedupe_key="dashboard-plan:{}".format(task_id),
                        force=True,
                    )
                elif action == "retry":
                    run = self.service.db.one(
                        "SELECT session_id FROM runs WHERE task_id=? AND session_id != '' "
                        "ORDER BY attempt DESC LIMIT 1",
                        (task_id,),
                    )
                    if not run:
                        raise ValueError("no Codex session id available for retry")
                    prompt = self.manager.paths["prompts"] / "retry-{}.txt".format(task_id)
                    prompt.write_text(
                        "继续当前任务；先检查已有进展与失败原因，只执行未完成部分。",
                        encoding="utf-8",
                    )
                    os.chmod(prompt, 0o600)
                    try:
                        self.manager.dispatch(task_id, prompt, resume=True)
                    finally:
                        try:
                            prompt.unlink()
                        except OSError:
                            pass
                    result = self.service.get_task(task_id)
                elif action == "complete-human-action":
                    result = self.service.complete_human_action(task_id)
                elif action == "remind-external":
                    result = self.service.remind_human_action(task_id)
                elif action == "heartbeat":
                    result = self.service.heartbeat_task(
                        task_id,
                        producer="local-api",
                        execution_mode=str(payload.get("mode", "external")),
                    )
                else:
                    raise ValueError("unsupported action")
                self._json(HTTPStatus.OK, result)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except IntakeError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, RuntimeError, KeyError, FileNotFoundError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def serve(
    host: str, port: int, data_dir: Path, mode: str = "manual",
    workspace: Optional[Path] = None,
    *, synthetic_tour: bool = False,
) -> None:
    validate_mode(mode)
    if type(synthetic_tour) is not bool:
        raise ValueError("synthetic_tour must be a boolean")
    if synthetic_tour and mode != "manual":
        raise ValueError("synthetic tour requires manual mode")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("MVP only binds to loopback")
    data_dir = Path(data_dir).expanduser().resolve()
    workspace = (
        data_dir if synthetic_tour
        else Path(workspace or default_workspace()).expanduser().resolve()
    )
    os.environ["QINGTIAN_ENGINE_HOME"] = str(data_dir)
    paths = ensure_data_dirs(data_dir)
    lock = InstanceLock(paths["run"] / "instance.lock")
    lock.acquire()
    server: Optional[ThreadingHTTPServer] = None
    watchdog_stop = threading.Event()
    watchdog_thread: Optional[threading.Thread] = None
    published_pid = False
    try:
        # Exclusive ownership makes it safe to remove a PID left by a process
        # that died before it could run normal shutdown cleanup.
        remove_server_pid(paths["run"])
        db = Database(paths["db"])
        service = ControlPlane(db)
        manager = RunManager(service, data_dir)
        qingtian_v2_enabled = not synthetic_tour and os.environ.get("QINGTIAN_RECOVERY_ENABLED", "1").lower() not in {
            "0", "false", "off", "no"
        }
        coordinator = RecoveryCoordinator(service, manager) if qingtian_v2_enabled else None
        planner = (
            CodexPlannerAdapter(cwd=workspace)
            if not synthetic_tour and os.environ.get("QINGTIAN_INTAKE_PLANNER", "").lower() == "codex"
            else None
        )
        intakes = IntakeService(service, data_dir, planner=planner)
        # Tour identity is per server, so another in-process manual instance
        # cannot accidentally turn its mutation guard off (or vice versa).
        handler_class = (
            type("SyntheticTourHandler", (ControlPlaneHandler,), {})
            if synthetic_tour else ControlPlaneHandler
        )

        def _cycle() -> Dict[str, Any]:
            if synthetic_tour:
                # No recovery, reconciliation, configuration lookup or worker
                # dispatch: the fixtures remain unchanged throughout the tour.
                return {
                    "mode": "manual", "automatic_dispatch": False,
                    "read_only": True, "synthetic": True,
                    "reconcile": {}, "dispatch": {"claimed": []},
                    "verification": {"claimed": []},
                }
            return background_cycle(manager, coordinator, mode)

        # Binding must succeed before an explicitly automatic startup can claim
        # work; a port collision is not permission to launch background workers.
        server = ThreadingHTTPServer((host, port), handler_class)
        scheduler_result = _cycle()
        watchdog_health: Dict[str, Any] = {
            "last_success_at": utc_now(),
            "_last_success_monotonic": time.monotonic(),
            "consecutive_errors": 0,
            "last_error_type": "",
            # A clean restart can briefly observe the previous process's
            # unexpired coordinator lease. STANDBY is healthy serving state,
            # not a startup failure; the watchdog will acquire the lease on a
            # later tick without disrupting independently owned workers.
            "reconcile": scheduler_result.get("reconcile", {}),
            "verification": scheduler_result.get("verification", {}),
            "scheduler": scheduler_result,
        }
        handler_class.service = service
        handler_class.manager = manager
        handler_class.intakes = intakes
        handler_class.watchdog_health = watchdog_health
        handler_class.coordinator = coordinator
        handler_class.engine_mode = mode
        handler_class.workspace = str(workspace)
        handler_class.synthetic_tour = synthetic_tour
        publish_server_pid(paths["run"], os.getpid())
        published_pid = True

        def _watchdog_loop() -> None:
            while not watchdog_stop.wait(15):
                try:
                    scheduler = _cycle()
                    handler_class.watchdog_health = {
                        "last_success_at": utc_now(),
                        "_last_success_monotonic": time.monotonic(),
                        "consecutive_errors": 0,
                        "last_error_type": "",
                        "reconcile": scheduler.get("reconcile", {}),
                        "verification": scheduler.get("verification", {}),
                        "scheduler": scheduler,
                    }
                except Exception as exc:
                    # The watchdog must never take down intake/dashboard serving.
                    previous = dict(handler_class.watchdog_health)
                    previous["consecutive_errors"] = (
                        int(previous.get("consecutive_errors", 0)) + 1
                    )
                    previous["last_error_type"] = type(exc).__name__
                    handler_class.watchdog_health = previous
                    continue

        watchdog_thread = threading.Thread(
            target=_watchdog_loop, name="qt-runtime-watchdog", daemon=True
        )
        watchdog_thread.start()

        def _stop(_signum: int, _frame: Any) -> None:
            watchdog_stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, _stop)
        server.serve_forever(poll_interval=0.5)
    finally:
        watchdog_stop.set()
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=1)
        if server is not None:
            server.server_close()
        if published_pid:
            remove_server_pid(paths["run"], os.getpid())
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--data-dir", default=str(default_data_dir()))
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--mode", choices=ENGINE_MODES, default="manual")
    args = parser.parse_args()
    try:
        serve(args.host, args.port, Path(args.data_dir), args.mode, args.workspace)
        return 0
    except Exception as exc:
        print("control-plane server error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
