from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

from .config import DEFAULT_PORT, default_data_dir, default_workspace, ensure_data_dirs, load_policy
from .db import Database
from .importers import import_defaults, import_governance, import_tasks_markdown
from .reporting import daily_report, write_daily_report
from .runner import RunManager
from .runtime import (
    active_instance_pid,
    cleanup_runtime_files_if_idle,
    read_pid,
)
from .service import ControlPlane
from .runtime_mode import ENGINE_MODES, validate_mode


DEFAULT_WORKSPACE = default_workspace()


def build_service(data_dir: Path) -> ControlPlane:
    paths = ensure_data_dirs(data_dir)
    return ControlPlane(Database(paths["db"]))


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def start_server(
    data_dir: Path, port: int, foreground: bool, open_browser: bool,
    mode: str = "manual", workspace: Optional[Path] = None,
) -> int:
    validate_mode(mode)
    data_dir = Path(data_dir).expanduser().resolve()
    workspace = Path(workspace or default_workspace()).expanduser().resolve()
    paths = ensure_data_dirs(data_dir)
    active_pid = active_instance_pid(paths["run"])
    running = _health_payload(port)
    if running is not None or active_pid is not None:
        if running is not None and not _matches_instance(running, data_dir, workspace, mode):
            print(
                "Refusing to use this port: running engine mode/data_dir/workspace "
                "does not match the requested instance. Inspect /api/health; "
                "choose another port or explicitly stop the correct instance.",
                file=sys.stderr,
            )
            return 2
        if running is None:
            print("Warning: lock owner is running but its mode/identity is unverified; "
                  "no mode change was applied.", file=sys.stderr)
        print("Control plane already running: http://127.0.0.1:{}".format(port))
        if open_browser:
            webbrowser.open("http://127.0.0.1:{}".format(port))
        return 0
    command = [
        sys.executable,
        "-m",
        "qingtian_engine.server",
        "--port",
        str(port),
        "--data-dir",
        str(data_dir),
        "--mode",
        mode,
        "--workspace",
        str(workspace),
    ]
    if foreground:
        return subprocess.call(command)
    log_path = paths["run"] / "server.log"
    log_handle = log_path.open("ab")
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    for _ in range(150):
        health = _health_payload(port)
        if health is not None and not _matches_instance(health, data_dir, workspace, mode):
            # Stop only the child created by this invocation, never an unrelated
            # engine found through the requested port or a historical PID file.
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
            print("Control plane identity mismatch after start; inspect /api/health", file=sys.stderr)
            return 2
        if health is not None and health.get("ok"):
            url = "http://127.0.0.1:{}".format(port)
            print("Control plane started: {} (pid {})".format(url, process.pid))
            if open_browser:
                webbrowser.open(url)
            return 0
        active_pid = active_instance_pid(paths["run"])
        published_pid = read_pid(paths["run"] / "server.pid")
        if active_pid is not None and published_pid == active_pid:
            if process.poll() is None and active_pid != process.pid:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=3)
            url = "http://127.0.0.1:{}".format(port)
            label = "started" if active_pid == process.pid else "already running"
            if health is None:
                print("Warning: process lock is ready but HTTP mode/identity is unverified; "
                      "inspect /api/health before dispatching.", file=sys.stderr)
            print("Control plane {}: {} (pid {})".format(label, url, active_pid))
            if open_browser:
                webbrowser.open(url)
            return 0
        if process.poll() is not None:
            active_pid = active_instance_pid(paths["run"])
            if active_pid is not None:
                print(
                    "Control plane already running: http://127.0.0.1:{} (pid {})".format(
                        port, active_pid
                    )
                )
                return 0
            break
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    print("Control plane failed to start; see {}".format(log_path), file=sys.stderr)
    return 1


def _health_payload(port: int) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:{}/api/health".format(port), timeout=0.3
        ) as response:
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except (ValueError, OSError):
            return None
    except (json.JSONDecodeError, urllib.error.URLError, TimeoutError):
        return None


def _matches_instance(payload: Dict[str, Any], data_dir: Path, workspace: Path, mode: str) -> bool:
    return (
        payload.get("service") == "qingtian-engine"
        and payload.get("mode") == mode
        and isinstance(payload.get("data_dir"), str)
        and Path(payload["data_dir"]).expanduser().resolve() == data_dir
        and isinstance(payload.get("workspace"), str)
        and Path(payload["workspace"]).expanduser().resolve() == workspace
    )


def _health(port: int) -> bool:
    payload = _health_payload(port)
    return bool(payload and payload.get("ok"))


def stop_server(data_dir: Path) -> int:
    paths = ensure_data_dirs(data_dir)
    pid = active_instance_pid(paths["run"])
    if pid is None:
        cleanup_runtime_files_if_idle(paths["run"])
        print("Control plane is not running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if active_instance_pid(paths["run"]) != pid:
                break
            time.sleep(0.1)
        else:
            print(
                "Control plane did not stop within 5 seconds (pid {})".format(pid),
                file=sys.stderr,
            )
            return 1
    except ProcessLookupError:
        pass
    cleanup_runtime_files_if_idle(paths["run"])
    print("Control plane stopped")
    return 0


def create_demo(service: ControlPlane, data_dir: Path, reset: bool) -> Dict[str, Any]:
    if reset:
        with service.db.connect() as connection:
            connection.execute("DELETE FROM tasks WHERE imported_from='demo'")
    created = []
    specs = [
        ("合成示例 · 待分配：整理模块文档", "INBOX", "cli", ""),
        ("合成示例 · 执行中：优化样例组件", "RUNNING", "cli", ""),
        ("合成示例 · 等待中：外部测试环境", "WAITING", "infra", "等待接入方准备测试环境"),
        ("合成示例 · 验收中：浏览器交互", "VERIFYING", "browser", ""),
    ]
    for index, (title, state, worker, blocker) in enumerate(specs):
        task = service.create_task(
            title,
            idempotency_key="demo-card-{}".format(index),
            scope_summary="合成状态 fixture，仅用于只读 tour 展示；不是实际执行记录",
            worker_type=worker,
            owner_session="QT-{:02d}".format(index + 1),
            imported_from="demo",
            state="INBOX",
        )
        if state != "INBOX":
            service.transition(
                task["id"],
                state,
                producer="demo",
                summary="演示状态 {}".format(state),
                dedupe_key="demo-state:{}:{}".format(task["id"], state),
                blocking_reason=blocker,
                force=True,
            )
        created.append(task["id"])

    done_task = service.create_task(
        "已完成：控制面全状态闭环",
        idempotency_key="demo-full-lifecycle",
        scope_summary="synthetic fixture：状态与证据演示，不启动 Worker 或调用模型",
        imported_from="demo",
        evidence_profile="artifact",
        worker_type="cli",
    )
    if done_task["state"] != "DONE":
        service.add_evidence(
            done_task["id"], "artifact", "synthetic demo fixture; not execution evidence",
            verified=True,
        )
        service.transition(done_task["id"], "VERIFYING", producer="demo", force=True)
        service.transition(done_task["id"], "DONE", producer="demo")
    created.append(done_task["id"])
    return {"created_or_reused": created, "paid_api_called": False, "synthetic": True}


def configure_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qingtian", description="Qingtian — local AI task execution engine"
    )
    parser.add_argument("--data-dir", default=str(default_data_dir()))
    parser.add_argument("--workspace", default=str(default_workspace()))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize an empty local SQLite database; never import ledgers")
    sub.add_parser("doctor", help="Read-only local diagnostics; no service, model or credentials required")
    bootstrap = sub.add_parser(
        "bootstrap", help="Initialize a teammate-local instance and import optional ledgers"
    )
    bootstrap.add_argument("--start", action="store_true")
    bootstrap.add_argument("--port", type=int, default=DEFAULT_PORT)
    bootstrap.add_argument("--mode", choices=ENGINE_MODES, default="manual")
    bootstrap.add_argument(
        "--import-ledgers", action="store_true",
        help="Explicitly import TASKS/governance from --workspace (may contain old work)",
    )
    start = sub.add_parser("start", help="Start dashboard in background")
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    start.add_argument("--foreground", action="store_true")
    start.add_argument("--open", action="store_true")
    start.add_argument(
        "--mode", choices=ENGINE_MODES, default="manual",
        help="manual: explicit dispatch only; auto: enable background claim/recovery",
    )
    sub.add_parser("stop")
    status = sub.add_parser("status")
    status.add_argument("--port", type=int, default=DEFAULT_PORT)

    project = sub.add_parser("project", help="Explicit local repository registry")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    register = project_sub.add_parser("register", help="Register an authorized Git repository")
    register.add_argument("name")
    register.add_argument("--repo", required=True, type=Path)
    register.add_argument("--base", required=True, help="Explicit non-protected branch/ref")
    register.add_argument("--role", action="append", default=[])
    register.add_argument("--scope")
    project_sub.add_parser("list")
    remove = project_sub.add_parser("remove", help="Remove registration, not repository files")
    remove.add_argument("name")

    importer = sub.add_parser("import", help="Explicit ledger import; does not start workers")
    importer.add_argument("--tasks", type=Path)
    importer.add_argument("--governance", type=Path)
    importer.add_argument("--force", action="store_true")
    importer.add_argument("--defaults", action="store_true", help="Scan --workspace default ledgers")

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    add = task_sub.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--scope", default="")
    add.add_argument("--priority", type=int, default=2)
    add.add_argument("--environment", default="local")
    repository = add.add_mutually_exclusive_group()
    repository.add_argument("--repo", default="", help="Exact path of an already registered repository")
    repository.add_argument("--project", default="", help="Registered project name")
    add.add_argument("--base", default="")
    add.add_argument("--worker", default="auto")
    add.add_argument("--owner", default="")
    add.add_argument("--reasoning", default="auto")
    add.add_argument("--model")
    add.add_argument("--speed", choices=("standard", "fast"))
    add.add_argument("--deploy", action="store_true")
    listing = task_sub.add_parser("list")
    listing.add_argument("--state", action="append")
    show = task_sub.add_parser("show")
    show.add_argument("task_id")
    transition = task_sub.add_parser("transition")
    transition.add_argument("task_id")
    transition.add_argument("state")
    transition.add_argument("--summary", default="")
    transition.add_argument("--blocker", default="")
    transition.add_argument("--force", action="store_true")
    evidence = task_sub.add_parser("evidence")
    evidence.add_argument("task_id")
    evidence.add_argument("kind")
    evidence.add_argument("value")
    evidence.add_argument("--verified", action="store_true")
    dependency = task_sub.add_parser("depend")
    dependency.add_argument("task_id")
    dependency.add_argument("depends_on_id")
    reroute = task_sub.add_parser("reroute")
    reroute.add_argument("task_id", nargs="?")
    reroute.add_argument("--imported", action="store_true")

    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("task_id")
    dispatch.add_argument("--prompt-file", type=Path, required=True)
    dispatch.add_argument("--resume", action="store_true")
    dispatch.add_argument("--dry-run", action="store_true")
    recover = sub.add_parser(
        "recover", help="Retry the latest infrastructure-failed run"
    )
    recover.add_argument("task_id")
    recover.add_argument("--fresh", action="store_true")
    heartbeat = sub.add_parser(
        "heartbeat", help="Mark or refresh an externally executed task"
    )
    heartbeat.add_argument("task_id")
    heartbeat.add_argument("--model")
    heartbeat.add_argument("--reasoning")
    heartbeat.add_argument("--speed", choices=("standard", "fast"))
    heartbeat.add_argument(
        "--mode", choices=("external", "delegated"), default="external"
    )
    cancel = sub.add_parser("cancel")
    cancel.add_argument("task_id")
    sub.add_parser("reconcile")
    feedback = sub.add_parser(
        "feedback", help="Return only state changes not yet delivered to a manager"
    )
    feedback.add_argument("--consumer", default="QT-00")
    feedback.add_argument("--limit", type=int, default=30)
    feedback.add_argument("--peek", action="store_true")
    feedback.add_argument("--catch-up", action="store_true")

    report = sub.add_parser("report")
    report.add_argument("--date")
    report.add_argument("--out", type=Path)
    demo = sub.add_parser("demo")
    demo.add_argument("--reset", action="store_true")
    demo.add_argument("--start", action="store_true")
    demo.add_argument("--port", type=int, default=DEFAULT_PORT)
    demo.add_argument("--mode", choices=ENGINE_MODES, default="manual")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = configure_parser()
    args = parser.parse_args(argv)
    if args.command == "import":
        if not (args.defaults or args.tasks or args.governance):
            parser.error("import requires --tasks, --governance or explicit --defaults")
        if args.defaults and (args.tasks or args.governance):
            parser.error("--defaults cannot be combined with --tasks or --governance")
    data_dir = Path(args.data_dir).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    if args.command == "doctor":
        result = doctor(data_dir, workspace)
        _json(result)
        return 0 if result["dashboard_ready"] else 1
    if args.command == "project":
        from .project_config import ProjectConfigError, load_project_config, register_project, remove_project
        try:
            if args.project_command == "register":
                result = register_project(args.name, args.repo, args.base, args.role, args.scope, data_dir=data_dir)
            elif args.project_command == "remove":
                result = remove_project(args.name, data_dir=data_dir)
            else:
                result = load_project_config(data_dir=data_dir)
            _json(result.to_dict())
            return 0
        except ProjectConfigError as exc:
            _json({"error": str(exc), "code": "invalid-project-registration"})
            return 2
    # Keep configured providers/registry aligned with this invocation's state,
    # including explicit --data-dir. Doctor and project list remain read-only.
    os.environ["QINGTIAN_ENGINE_HOME"] = str(data_dir)
    if args.command == "start":
        return start_server(data_dir, args.port, args.foreground, args.open, args.mode, workspace)
    if args.command == "stop":
        return stop_server(data_dir)
    if args.command == "status":
        paths = ensure_data_dirs(data_dir)
        ok = _health(args.port) or active_instance_pid(paths["run"]) is not None
        print("running" if ok else "stopped")
        return 0 if ok else 1

    service = build_service(data_dir)
    if args.command == "init":
        _json(
            {
                "initialized": True,
                "imported": {},
                "data_dir": str(data_dir),
                "workspace": str(workspace),
                "next": "qingtian start --mode manual --open",
            }
        )
    elif args.command == "bootstrap":
        checks = {
            "python": sys.version.split()[0],
            "codex": bool(_which("codex")),
            "git": bool(_which("git")),
            "workspace": str(workspace),
            "workspace_exists": workspace.exists(),
        }
        imported = (
            import_defaults(service, workspace)
            if args.import_ledgers and workspace.exists() else {}
        )
        feedback_cursor = service.initialize_feedback_cursor("QT-00")
        _json(
            {
                "mode": "single-user-local",
                "checks": checks,
                "imported": imported,
                "feedback": feedback_cursor,
                "data_dir": str(data_dir),
                "next": "qingtian start --mode manual --open",
            }
        )
        if args.start:
            return start_server(data_dir, args.port, False, False, args.mode, workspace)
    elif args.command == "import":
        results: Dict[str, Any] = {}
        if args.tasks:
            results["tasks"] = import_tasks_markdown(service, args.tasks, args.force)
        if args.governance:
            results["governance"] = import_governance(service, args.governance)
        if args.defaults:
            results = import_defaults(service, workspace)
        _json(results)
    elif args.command == "task":
        if args.task_command == "add":
            _json(
                service.create_task(
                    args.title,
                    scope_summary=args.scope,
                    priority=args.priority,
                    environment=args.environment,
                    repository=args.project or args.repo,
                    base_branch=args.base,
                    worker_type=args.worker,
                    owner_session=args.owner,
                    reasoning=args.reasoning,
                    model=args.model,
                    speed=args.speed,
                    requires_deploy=args.deploy,
                )
            )
        elif args.task_command == "list":
            _json(service.list_tasks(args.state))
        elif args.task_command == "show":
            _json(service.get_task(args.task_id))
        elif args.task_command == "transition":
            _json(
                service.transition(
                    args.task_id,
                    args.state.upper(),
                    summary=args.summary,
                    blocking_reason=args.blocker,
                    force=args.force,
                )
            )
        elif args.task_command == "evidence":
            _json(
                {
                    "inserted": service.add_evidence(
                        args.task_id, args.kind, args.value, verified=args.verified
                    )
                }
            )
        elif args.task_command == "depend":
            service.add_dependency(args.task_id, args.depends_on_id)
            _json({"ok": True})
        elif args.task_command == "reroute":
            if args.imported:
                _json({"rerouted": service.reroute_imported()})
            elif args.task_id:
                _json(service.reroute(args.task_id))
            else:
                raise SystemExit("task reroute requires TASK_ID or --imported")
    elif args.command == "dispatch":
        _json(
            RunManager(service, data_dir).dispatch(
                args.task_id, args.prompt_file, args.resume, args.dry_run
            )
        )
    elif args.command == "recover":
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False
        ) as handle:
            handle.write(
                "执行器基础设施已恢复。继续既有计划，只执行未完成部分；"
                "先核对当前 worktree 与最新基线，再 Coding、相关测试和最小冒烟。"
            )
            prompt_path = Path(handle.name)
        try:
            _json(
                RunManager(service, data_dir).recover_infrastructure_failure(
                    args.task_id, prompt_path, resume=not args.fresh
                )
            )
        finally:
            try:
                prompt_path.unlink()
            except OSError:
                pass
    elif args.command == "heartbeat":
        _json(
            service.heartbeat_task(
                args.task_id, execution_mode=args.mode, model=args.model,
                reasoning=args.reasoning, speed=args.speed,
            )
        )
    elif args.command == "cancel":
        _json(RunManager(service, data_dir).cancel(args.task_id))
    elif args.command == "reconcile":
        _json(RunManager(service, data_dir).reconcile())
    elif args.command == "feedback":
        if args.catch_up:
            _json(service.initialize_feedback_cursor(args.consumer, force=True))
        else:
            _json(
                service.feedback_changes(
                    consumer=args.consumer, limit=args.limit, advance=not args.peek
                )
            )
    elif args.command == "report":
        report_date = date.fromisoformat(args.date) if args.date else None
        if args.out:
            print(write_daily_report(service, args.out, report_date))
        else:
            print(daily_report(service, report_date))
    elif args.command == "demo":
        _json(create_demo(service, data_dir, args.reset))
        if args.start:
            return start_server(data_dir, args.port, False, False, args.mode, workspace)
    return 0


def _which(command: str) -> Optional[str]:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def doctor(data_dir: Path, workspace: Path) -> Dict[str, Any]:
    """Inspect paths and optional tools only; do not initialize state or probe auth."""
    policy_error = ""
    try:
        policy = load_policy()
    except (OSError, ValueError) as exc:
        policy = {}
        policy_error = type(exc).__name__
    supported_python = (3, 11) <= sys.version_info[:2] <= (3, 14)
    supported_platform = os.name == "posix"
    codex, git = bool(_which("codex")), bool(_which("git"))
    dashboard_ready = supported_python and supported_platform and not policy_error
    return {
        "schema_version": 1, "read_only": True,
        "python": sys.version.split()[0], "supported_python": supported_python,
        "platform": sys.platform, "supported_platform": supported_platform,
        "data_dir": str(data_dir), "data_dir_exists": data_dir.exists(),
        "workspace": str(workspace), "workspace_exists": workspace.is_dir(),
        "default_mode": "manual", "default_port": DEFAULT_PORT,
        "policy_loaded": not policy_error, "policy_error": policy_error,
        "model": policy.get("model"),
        "optional_tools": {"codex": codex, "git": git},
        "credentials_checked": False, "credentials_required_to_start": False,
        "dashboard_ready": dashboard_ready,
        "execution_tools_present": codex and git,
        "note": "Dispatch requires separately configured model access and an explicitly registered project. "
                "Runtime process isolation currently supports macOS/Linux (POSIX).",
    }


if __name__ == "__main__":
    raise SystemExit(main())
