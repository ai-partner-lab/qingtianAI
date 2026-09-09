"""Loopback-only, synthetic guided demonstration backed by the real core store.

The named roles are a fixed teaching sequence, not autonomous agents. No remote
provider, arbitrary command, product workspace, or Knowledge Hub Vault is opened.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
import secrets
import socket
import tempfile
from threading import RLock
from typing import Any, Iterator
import webbrowser

from .models import RunState, SessionState, TaskState, content_hash
from .capability_checks import CAPABILITY_IDS, CapabilityBusy, CapabilityRunner
from .providers import EchoProvider, ModelGateway
from .store import QingtianStore


ROLES = (
    {"id": "planner", "name": "规划师", "title": "Planner", "description": "定义目标、范围和验收标准"},
    {"id": "keeper", "name": "知识官", "title": "Knowledge Keeper", "description": "准备带来源的合成知识"},
    {"id": "builder", "name": "执行者", "title": "Builder", "description": "调用离线模型并记录真实执行状态"},
    {"id": "tester", "name": "测试员", "title": "Tester", "description": "先核对未知结果，再执行验证"},
    {"id": "reviewer", "name": "审查者", "title": "Reviewer", "description": "检查证据和任务完成条件"},
    {"id": "archivist", "name": "归档员", "title": "Archivist", "description": "保存结论、检查点并完成任务"},
)

GUIDE = (
    {"role": "planner", "title": "1 · 制定计划", "description": "创建一项合成任务，明确验收条件，再启用第一个执行会话。", "next_label": "让规划师开始"},
    {"role": "keeper", "title": "2 · 准备知识", "description": "把演示要求保存为有来源的控制层知识记录。这里不会读取实际 Knowledge Hub 或私人资料。", "next_label": "让知识官准备上下文"},
    {"role": "builder", "title": "3 · 执行任务", "description": "通过真正的 ModelGateway 调用离线 EchoProvider，记录 Run 和执行证据。", "next_label": "让执行者运行"},
    {"role": "builder", "title": "4 · 演示回执丢失", "description": "模拟本地操作已经完成但回执丢失。将 Run 标记 UNKNOWN，保存检查点并交接，演示不会发送外部请求。", "next_label": "注入合成故障"},
    {"role": "tester", "title": "5 · 核对后继续", "description": "测试员从检查点接手，先核对合成本地操作记录并解决 UNKNOWN，再建立新的验证 Run。", "next_label": "让测试员核对"},
    {"role": "reviewer", "title": "6 · 审查证据", "description": "检查全部 Run 已成功、证据齐全，关闭执行会话并把任务转入 REVIEW_PENDING。", "next_label": "让审查者复核"},
    {"role": "archivist", "title": "7 · 归档完成", "description": "记录可复用结论，按合法状态转换完成任务，并保存与最终修订匹配的检查点。", "next_label": "让归档员完成任务"},
)


class DemoConflict(ValueError):
    """The browser supplied an old step or the guide already finished."""


class DemoExecutionError(RuntimeError):
    """A step partially failed; its actual records are retained for inspection."""


class DemoResetRequired(RuntimeError):
    """No more actions may run after a failed step until an explicit reset."""


class GuidedDemo:
    """One disposable walkthrough; every snapshot reads actual persisted records."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._closed = False
        self.reset()

    @property
    def database_path(self) -> Path:
        if self._closed or self._temporary is None:
            raise RuntimeError("demo is closed")
        return Path(self._temporary.name) / "control.db"

    @contextmanager
    def _store(self) -> Iterator[QingtianStore]:
        with QingtianStore(self.database_path) as store:
            store.initialize()
            yield store

    def close(self) -> None:
        with self._lock:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            self._closed = True

    def __enter__(self) -> GuidedDemo:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("demo is closed")
            fresh = tempfile.TemporaryDirectory(prefix="qingtian-guided-demo-")
            previous = self._temporary
            self._temporary = fresh
            try:
                with self._store() as store:
                    task = store.create_task(
                        project_id="synthetic-guided-demo",
                        title="合成任务：完成一次可追溯的 AI 工作交接",
                        objective="离线演示规划、知识、执行、UNKNOWN 核对、评审与归档。",
                        scope=["synthetic data only", "offline fixed guide", "no external side effects"],
                        acceptance=["all runs succeed", "UNKNOWN is reconciled before new work", "evidence and final checkpoint exist"],
                    )
            except Exception:
                self._temporary = previous
                fresh.cleanup()
                raise
            self.task_id = task["task_id"]
            self.csrf_token = secrets.token_urlsafe(32)
            self.step_number = 0
            self.requires_reset = False
            self.error: dict[str, str] | None = None
            self.active_role = "planner"
            self.events: list[dict[str, str]] = []
            self.knowledge_ids: list[str] = []
            self.session_id: str | None = None
            self.checkpoint_id: str | None = None
            self.unknown_run_id: str | None = None
            self._simulated_operations: dict[str, dict[str, Any]] = {}
            if previous is not None:
                previous.cleanup()
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock, self._store() as store:
            return self._snapshot(store)

    def _snapshot(self, store: QingtianStore) -> dict[str, Any]:
        exported = store.export_task(self.task_id)
        done = self.step_number == len(GUIDE)
        guide = dict(GUIDE[self.step_number]) if not done else {
            "role": "archivist", "title": "演示完成", "description": "Task 已完成，执行证据、知识记录与最终检查点均保存在本次临时数据库。关闭服务后演示数据会清理。", "next_label": "已完成",
        }
        if self.requires_reset:
            guide = {"role": self.active_role, "title": "演示需要重置", "description": "步骤未完成，已保留实际内核状态。为避免重复执行，请重置开始新一轮。", "next_label": "请重置演示"}
        return {
            "mode": "offline-guided",
            "step": self.step_number,
            "total_steps": len(GUIDE),
            "csrf_token": self.csrf_token,
            "active_role": self.active_role,
            "task": exported["task"],
            "guide": guide,
            "events": [dict(event) for event in self.events],
            "records": {
                "sessions": exported["sessions"],
                "runs": exported["runs"],
                "evidence": exported["evidence"],
                "checkpoints": exported["checkpoints"],
                "knowledge": [store.get_knowledge(item) for item in self.knowledge_ids],
            },
            "roles": [dict(role) for role in ROLES],
            "done": done,
            "requires_reset": self.requires_reset,
            "error": dict(self.error) if self.error else None,
        }

    def record_failure(self) -> None:
        """Mark only the guide as failed; never invent a core result or retry."""
        with self._lock:
            if self.requires_reset:
                return
            self.requires_reset = True
            self.active_role = GUIDE[min(self.step_number, len(GUIDE) - 1)]["role"]
            self.error = {
                "code": "guide_step_failed",
                "message": "演示步骤未完成，实际状态已保留；请重置开始新一轮。",
            }
            self.events.append({
                "id": f"event-error-{self.step_number}", "role": self.active_role,
                "title": "步骤中断，需要重置", "detail": self.error["message"], "state": "ERROR",
            })

    def advance(self, expected_step: int) -> dict[str, Any]:
        with self._lock:
            if type(expected_step) is not int or expected_step < 0:
                raise ValueError("expected_step must be a non-negative integer")
            if self.requires_reset:
                raise DemoResetRequired("Reset the failed guide before starting any more work.")
            if expected_step != self.step_number or self.step_number >= len(GUIDE):
                raise DemoConflict("The guide changed; refresh the current state before continuing.")
            action = (
                self._plan, self._prepare_knowledge, self._execute,
                self._lose_acknowledgement, self._reconcile, self._review, self._archive,
            )[self.step_number]
            try:
                with self._store() as store:
                    role, title, detail, state = action(store)
                    self.active_role = role
                    self.step_number += 1
                    self.events.append({
                        "id": f"event-{self.step_number}", "role": role,
                        "title": title, "detail": detail, "state": state,
                    })
                    return self._snapshot(store)
            except Exception as exc:
                self.record_failure()
                raise DemoExecutionError("The step failed; reset is required.") from exc

    def _transition_task(self, store: QingtianStore, target: TaskState) -> dict[str, Any]:
        task = store.get_task(self.task_id)
        return store.transition_task(self.task_id, target, expected_revision=task["revision"])

    def _plan(self, store: QingtianStore) -> tuple[str, str, str, str]:
        self._transition_task(store, TaskState.READY)
        self._transition_task(store, TaskState.RUNNING)
        session = store.create_session(task_id=self.task_id, host_alias="synthetic-builder")
        self.session_id = session["session_id"]
        store.transition_session(self.session_id, SessionState.ACTIVE)
        store.add_evidence(subject_type="task", subject_id=self.task_id, result="observed", metadata={"role": "planner", "synthetic": True, "observation": "Scope and acceptance are declared; initial session is active."})
        return "planner", "目标已确定", "Task 经 DRAFT → READY → RUNNING，首个 Session 已 ACTIVE。", "RUNNING"

    def _prepare_knowledge(self, store: QingtianStore) -> tuple[str, str, str, str]:
        knowledge = store.add_knowledge(
            project_id="synthetic-guided-demo", scope="project", classification="public",
            kind="procedure", title="离线演示的合成执行规则",
            content="Use only the offline echo provider. Reconcile an UNKNOWN operation before starting another run.",
            source=f"task:{self.task_id}", evidence_label="SOURCE", review_state="reviewed",
        )
        self.knowledge_ids.append(knowledge["knowledge_id"])
        store.add_evidence(subject_type="task", subject_id=self.task_id, result="observed", artifact_hash=knowledge["content_hash"], metadata={"role": "keeper", "synthetic": True, "knowledge_id": knowledge["knowledge_id"], "source_plane": "control-plane-scoped-knowledge"})
        return "keeper", "上下文已准备", "合成规则已保存为 scoped Knowledge；没有读取私人 Vault。", "OBSERVED"

    def _new_run(self, store: QingtianStore, executor: str, key: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.session_id is None:
            raise RuntimeError("guide has no active session")
        run = store.create_run(task_id=self.task_id, session_id=self.session_id, executor=executor, idempotency_key=key, request=request)
        return store.transition_run(run["run_id"], RunState.RUNNING)

    def _execute(self, store: QingtianStore) -> tuple[str, str, str, str]:
        run = self._new_run(store, "offline-echo", "guide-build-v1", {"prompt": "Synthetic handoff artifact"})
        gateway = ModelGateway()
        gateway.register(EchoProvider())
        result = gateway.generate(route="demo.offline", provider_name="offline-echo", model="echo-v1", messages=[{"role": "user", "content": "Synthetic handoff artifact"}])
        store.transition_run(run["run_id"], RunState.SUCCEEDED)
        store.add_evidence(subject_type="run", subject_id=run["run_id"], result="passed", artifact_hash=content_hash(result.output), metadata={"role": "builder", "synthetic": True, "provider_result": gateway.as_dict(result)})
        return "builder", "离线执行成功", "真实 EchoProvider 返回合成结果；Run 已 SUCCEEDED，Evidence 保存模型来源和输出哈希。", "SUCCEEDED"

    def _checkpoint(self, store: QingtianStore, next_step: str) -> dict[str, Any]:
        exported = store.export_task(self.task_id)
        checkpoint = store.create_checkpoint(
            task_id=self.task_id,
            snapshot={
                "synthetic": True, "next_step": next_step,
                "run_refs": [run["run_id"] for run in exported["runs"]],
                "evidence_refs": [item["evidence_id"] for item in exported["evidence"]],
                "knowledge_refs": list(self.knowledge_ids),
                "unknown_run_refs": [run["run_id"] for run in exported["runs"] if run["state"] == "UNKNOWN"],
            },
            supersedes=self.checkpoint_id,
        )
        self.checkpoint_id = checkpoint["checkpoint_id"]
        return checkpoint

    def _close_session(self, store: QingtianStore) -> None:
        if self.session_id is None:
            raise RuntimeError("guide has no active session")
        store.transition_session(self.session_id, SessionState.QUIESCING)
        store.transition_session(self.session_id, SessionState.CHECKPOINTED)
        store.transition_session(self.session_id, SessionState.CLOSED)

    def _lose_acknowledgement(self, store: QingtianStore) -> tuple[str, str, str, str]:
        run = self._new_run(store, "synthetic-local-operation", "guide-receipt-loss-v1", {"operation": "record-synthetic-artifact"})
        self.unknown_run_id = run["run_id"]
        operation_id = "synthetic-operation-" + run["run_id"]
        self._simulated_operations[operation_id] = {"completed": True, "artifact": "synthetic-artifact", "synthetic": True}
        store.transition_run(run["run_id"], RunState.UNKNOWN, external_operation_id=operation_id)
        store.add_evidence(subject_type="run", subject_id=run["run_id"], result="unknown", metadata={"role": "builder", "synthetic": True, "simulation": "Local fixture completed; acknowledgement intentionally withheld. No external action was performed."})
        self._checkpoint(store, "Reconcile the synthetic operation before creating any new run.")
        self._close_session(store)
        return "builder", "回执丢失，暂停新执行", "合成故障使 Run 进入 UNKNOWN；已保存检查点并关闭旧 Session，等待核对。", "UNKNOWN"

    def _reconcile(self, store: QingtianStore) -> tuple[str, str, str, str]:
        if self.unknown_run_id is None or self.checkpoint_id is None:
            raise RuntimeError("guide has no operation to reconcile")
        session = store.create_session(task_id=self.task_id, host_alias="synthetic-tester", source_checkpoint_id=self.checkpoint_id)
        self.session_id = session["session_id"]
        store.transition_session(self.session_id, SessionState.ACTIVE)
        run = store.get_run(self.unknown_run_id)
        operation = self._simulated_operations.get(run["external_operation_id"])
        if not operation or operation.get("completed") is not True:
            raise RuntimeError("synthetic operation has not been reconciled")
        store.transition_run(run["run_id"], RunState.SUCCEEDED)
        store.add_evidence(subject_type="run", subject_id=run["run_id"], result="passed", artifact_hash=content_hash(operation), metadata={"role": "tester", "synthetic": True, "reconciliation_source": "demo-local-operation-fixture", "external_operation_id": run["external_operation_id"]})
        verification = self._new_run(store, "synthetic-verifier", "guide-verify-v1", {"checks": ["all_prior_runs_succeeded", "knowledge_present"]})
        previous_runs = [item for item in store.export_task(self.task_id)["runs"] if item["run_id"] != verification["run_id"]]
        if not previous_runs or any(item["state"] != "SUCCEEDED" for item in previous_runs) or not self.knowledge_ids:
            raise RuntimeError("guide verification did not pass")
        store.transition_run(verification["run_id"], RunState.SUCCEEDED)
        store.add_evidence(subject_type="run", subject_id=verification["run_id"], result="passed", metadata={"role": "tester", "synthetic": True, "checks": ["all_prior_runs_succeeded", "knowledge_present"], "source_checkpoint_id": self.checkpoint_id})
        return "tester", "先核对，再执行验证", "新 Session 从检查点接手；UNKNOWN 已核对为 SUCCEEDED，此后才建立验证 Run。", "SUCCEEDED"

    def _review(self, store: QingtianStore) -> tuple[str, str, str, str]:
        exported = store.export_task(self.task_id)
        if len(exported["runs"]) != 3 or any(item["state"] != "SUCCEEDED" for item in exported["runs"]):
            raise RuntimeError("review requires three successful synthetic runs")
        passed_ids = {item["subject_id"] for item in exported["evidence"] if item["subject_type"] == "run" and item["result"] == "passed"}
        if any(item["run_id"] not in passed_ids for item in exported["runs"]):
            raise RuntimeError("review requires matching run evidence")
        self._close_session(store)
        self._transition_task(store, TaskState.REVIEW_PENDING)
        store.add_evidence(subject_type="task", subject_id=self.task_id, result="passed", metadata={"role": "reviewer", "synthetic": True, "checks": ["all_runs_succeeded", "run_evidence_present", "execution_sessions_closed"], "scope": "This fixed offline guide only; not a product acceptance or deployment."})
        self._checkpoint(store, "Archive the reviewed synthetic result.")
        return "reviewer", "证据复核完成", "全部 Run 有成功证据且会话已关闭；Task 已 REVIEW_PENDING。", "REVIEW_PENDING"

    def _archive(self, store: QingtianStore) -> tuple[str, str, str, str]:
        knowledge = store.add_knowledge(
            project_id="synthetic-guided-demo", scope="project", classification="public", kind="observation",
            title="合成演示的交接结论",
            content="The offline guide preserved an UNKNOWN run across a checkpoint handoff and reconciled it before starting a verification run.",
            source=f"task:{self.task_id}", evidence_label="OBSERVED", review_state="reviewed",
        )
        self.knowledge_ids.append(knowledge["knowledge_id"])
        self._transition_task(store, TaskState.DONE)
        self._checkpoint(store, "Completed. Start a new disposable demo to repeat the guide.")
        return "archivist", "任务已完成并归档", "Task 已 DONE；最终修订对应的 Checkpoint 和 scoped Knowledge 已保存。", "DONE"


class DemoHTTPServer(ThreadingHTTPServer):
    """Loopback server with serialized state changes and independent connections."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, port: int = 8787):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self._connections: set[socket.socket] = set()
        self._connections_lock = RLock()
        self.capabilities = CapabilityRunner()
        self.demo = GuidedDemo()
        try:
            super().__init__(("127.0.0.1", port), DemoRequestHandler)
        except Exception:
            self.capabilities.close()
            self.demo.close()
            raise

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def get_request(self) -> tuple[Any, Any]:
        connection, address = super().get_request()
        connection.settimeout(5)
        with self._connections_lock:
            self._connections.add(connection)
        return connection, address

    def close_request(self, request: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(request)
        super().close_request(request)

    def server_close(self) -> None:
        try:
            # Reap owned capability subprocesses and their browsers before
            # joining HTTP workers or deleting this server's guide data.
            self.capabilities.close()
            # End idle/preconnected and slow-reading requests before joining
            # non-daemon workers. No worker may outlive the private demo data.
            with self._connections_lock:
                connections = tuple(self._connections)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            super().server_close()
        finally:
            self.demo.close()


class DemoRequestHandler(BaseHTTPRequestHandler):
    server: DemoHTTPServer
    server_version = "QingtianDemo"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    MAX_BODY = 1024
    STATIC = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/app.css": ("app.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    }

    def log_message(self, _format: str, *args: Any) -> None:
        # Do not print URLs, tokens, request bodies, or browser metadata.
        pass

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        self.close_connection = True
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # A browser may disconnect or server_close may deliberately close
            # its socket while an isolated capability result is finishing.
            pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, code: str) -> None:
        self._json(status, {"error": code})

    def _failed_guide(self, status: int, code: str) -> None:
        with self.server.demo._lock:
            if self.command == "POST" and not self._token_valid():
                self._error(403, "invalid_token")
                return
            self.server.demo.record_failure()
            try:
                state = self.server.demo.snapshot()
            except Exception:
                # A storage failure can also prevent reading records. Expose no
                # invented state or exception text, but keep reset available.
                state = None
        self._json(status, {"error": code, "requires_reset": True, "state": state})

    def _token_valid(self) -> bool:
        tokens = self.headers.get_all("X-Qingtian-Demo-Token", [])
        return len(tokens) == 1 and secrets.compare_digest(tokens[0].encode("utf-8"), self.server.demo.csrf_token.encode("ascii"))

    def _check_request_boundary(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        if hosts != [f"127.0.0.1:{self.server.server_port}"]:
            self._error(403, "invalid_host")
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.url]:
            self._error(403, "invalid_origin")
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            self._error(403, "cross_site_request")
            return False
        return True

    def do_GET(self) -> None:
        if not self._check_request_boundary():
            return
        if self.path == "/api/capabilities":
            try:
                catalog = json.loads(files("qingtian_core.resources").joinpath("capabilities.json").read_text(encoding="utf-8"))
                if not isinstance(catalog, dict):
                    raise ValueError("catalog object required")
            except (OSError, ValueError, UnicodeError):
                self._error(500, "capability_catalog_unavailable")
                return
            self._json(200, catalog)
            return
        if self.path == "/api/state":
            try:
                snapshot = self.server.demo.snapshot()
            except Exception:
                self._failed_guide(500, "guide_state_unavailable")
                return
            self._json(200, snapshot)
            return
        target = self.STATIC.get(self.path)
        if target is None:
            self._error(404, "not_found")
            return
        name, content_type = target
        try:
            payload = files("qingtian_core.resources").joinpath("demo", name).read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            self._error(404, "static_resource_missing")
            return
        self._send(200, payload, content_type)

    def do_HEAD(self) -> None:
        self.do_GET()

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def do_POST(self) -> None:
        if not self._check_request_boundary():
            return
        if self.path not in {"/api/step", "/api/reset", "/api/capabilities/run"}:
            self._error(404, "not_found")
            return
        if not self._token_valid():
            self._error(403, "invalid_token")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(400, "unsupported_transfer_encoding")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(411, "content_length_required")
            return
        if len(lengths[0]) > 4:
            self._error(413, "request_too_large")
            return
        length = int(lengths[0])
        if length > self.MAX_BODY:
            self._error(413, "request_too_large")
            return
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._error(415, "json_required")
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=self._unique_object)
            if not isinstance(body, dict):
                raise ValueError("object required")
            if self.path == "/api/capabilities/run":
                if set(body) != {"capability_id"} or not isinstance(body["capability_id"], str) or body["capability_id"] not in CAPABILITY_IDS:
                    raise ValueError("supported capability_id required")
                with self.server.demo._lock:
                    if not self._token_valid():
                        self._error(403, "invalid_token")
                        return
                # Independent checks must not hold the main guide lock. A
                # separate nonblocking runner lock admits one bounded child.
                result = self.server.capabilities.run(body["capability_id"])
                self._json(200, result)
                return
            # Read bounded bodies outside the state lock so a slow browser
            # cannot stop all clients. Revalidate the token under the same lock
            # as reset/advance; reset may have rotated it during the body read.
            with self.server.demo._lock:
                if not self._token_valid():
                    self._error(403, "invalid_token")
                    return
                if self.path == "/api/reset":
                    if body:
                        raise ValueError("reset body must be empty")
                    result = self.server.demo.reset()
                else:
                    if set(body) != {"expected_step"}:
                        raise ValueError("expected_step required")
                    result = self.server.demo.advance(body["expected_step"])
        except CapabilityBusy:
            self._error(409, "capability_busy")
            return
        except DemoExecutionError:
            self._failed_guide(500, "guide_step_failed")
            return
        except DemoResetRequired:
            self._failed_guide(409, "reset_required")
            return
        except DemoConflict:
            self._error(409, "stale_step")
            return
        except (ValueError, UnicodeError, TimeoutError):
            self._error(400, "invalid_request")
            return
        except Exception:
            if self.path == "/api/capabilities/run":
                self._error(500, "capability_request_failed")
                return
            self._failed_guide(500, "guide_request_failed")
            return
        self._json(200, result)

    def do_OPTIONS(self) -> None:
        if self._check_request_boundary():
            self._error(405, "method_not_allowed")


def run_demo_web(*, port: int = 8787, open_browser: bool = True) -> int:
    with DemoHTTPServer(port) as server:
        print(f"Qingtian AI offline guided demo: {server.url}/", flush=True)
        print("Synthetic temporary data only. Press Ctrl+C to stop and clean up.", flush=True)
        if open_browser:
            try:
                webbrowser.open(server.url + "/", new=2)
            except webbrowser.Error:
                print("Open the loopback URL above in your browser.", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    return 0
