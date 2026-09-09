from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

from . import __version__
from .bundle import build_bundle, scan_tree, verify_bundle
from .contracts import validate_files
from .models import QingtianError, RunState, SessionState, TaskState, canonical_json
from .providers import EchoProvider, ModelGateway
from .store import QingtianStore, SCHEMA_VERSION
from .verification import run_checks


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def open_store(database: str) -> QingtianStore:
    store = QingtianStore(database)
    try:
        store.initialize()
    except Exception:
        store.close()
        raise
    return store


def cmd_doctor(_: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="qingtian-doctor-") as temp_name:
        database = Path(temp_name) / "doctor.db"
        with open_store(str(database)) as store:
            result = {
                "qingtian_version": __version__,
                "schema_version": SCHEMA_VERSION,
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
                "fts5": store.fts_enabled,
                "profile": "portable-single-host",
                "status": "ok",
            }
    emit(result)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        emit({"status": "initialized", "database": args.db, "fts5": store.fts_enabled})
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        task = store.create_task(
            project_id="synthetic-project",
            title="Portable Qingtian smoke task",
            objective="Prove task, session, run, evidence, checkpoint, and knowledge contracts.",
            scope=["synthetic data only", "no external side effects"],
            acceptance=["run reaches SUCCEEDED", "checkpoint hash is emitted"],
        )
        task = store.transition_task(task["task_id"], TaskState.READY, expected_revision=1)
        task = store.transition_task(
            task["task_id"], TaskState.RUNNING, expected_revision=task["revision"]
        )
        session = store.create_session(task_id=task["task_id"], host_alias="local-demo")
        session = store.transition_session(session["session_id"], SessionState.ACTIVE)
        run = store.create_run(
            task_id=task["task_id"],
            session_id=session["session_id"],
            executor="offline-echo",
            idempotency_key="demo-v1",
            request={"prompt": "hello"},
        )
        run = store.transition_run(run["run_id"], RunState.RUNNING)
        gateway = ModelGateway()
        gateway.register(EchoProvider())
        model_result = gateway.generate(
            route="demo.text",
            provider_name="offline-echo",
            model="echo-v1",
            messages=[{"role": "user", "content": "hello"}],
        )
        run = store.transition_run(run["run_id"], RunState.SUCCEEDED)
        evidence = store.add_evidence(
            subject_type="run",
            subject_id=run["run_id"],
            result="passed",
            metadata={"provider_result": gateway.as_dict(model_result)},
        )
        knowledge = store.add_knowledge(
            project_id="synthetic-project",
            scope="project",
            classification="public",
            kind="decision",
            title="Portable core smoke result",
            content="The synthetic offline run completed without an external model or credential.",
            source=f"run:{run['run_id']}",
            evidence_label="OBSERVED",
        )
        checkpoint = store.create_checkpoint(
            task_id=task["task_id"],
            snapshot={
                "run_refs": [run["run_id"]],
                "evidence_refs": [evidence["evidence_id"]],
                "knowledge_refs": [knowledge["knowledge_id"]],
                "next_step": "independent review",
            },
        )
        result = {
            "status": "ok",
            "task": task,
            "session": session,
            "run": run,
            "checkpoint": checkpoint,
            "knowledge_search": store.search_knowledge(
                project_id="synthetic-project",
                query="offline",
                allowed_scopes=["project"],
                max_classification="public",
            ),
        }
        emit(result)
    return 0


def cmd_demo_web(args: argparse.Namespace) -> int:
    from .demo_web import run_demo_web

    return run_demo_web(port=args.port, open_browser=not args.no_browser)


def cmd_demo_check(args: argparse.Namespace) -> int:
    from .capability_checks import CapabilityRunner

    runner = CapabilityRunner()
    try:
        receipt = runner.run(args.capability)
    finally:
        runner.close()
    emit(receipt)
    return {"passed": 0, "failed": 1, "blocked": 2}[receipt["status"]]


def cmd_task_create(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        emit(
            store.create_task(
                project_id=args.project,
                title=args.title,
                objective=args.objective,
                scope=args.scope,
                acceptance=args.acceptance,
            )
        )
    return 0


def cmd_task_transition(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        emit(
            store.transition_task(
                args.task_id, args.to, expected_revision=args.expected_revision
            )
        )
    return 0


def cmd_task_show(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        emit(store.export_task(args.task_id))
    return 0


def cmd_knowledge_add(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        emit(
            store.add_knowledge(
                project_id=args.project,
                scope=args.scope,
                classification=args.classification,
                kind=args.kind,
                title=args.title,
                content=args.content,
                source=args.source,
                source_revision=args.source_revision,
                evidence_label=args.evidence_label,
            )
        )
    return 0


def cmd_knowledge_search(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        emit(
            store.search_knowledge(
                project_id=args.project,
                query=args.query,
                allowed_scopes=args.allowed_scope,
                max_classification=args.max_classification,
                limit=args.limit,
            )
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    receipt = run_checks(
        args.adapter,
        profile=args.profile,
        allow_local_writes=args.allow_local_writes,
        execute_trusted_adapter=args.execute_trusted_adapter,
    )
    if args.receipt:
        receipt_path = Path(args.receipt)
        receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{receipt_path.name}.", dir=receipt_path.parent
        )
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(receipt) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, receipt_path)
            if os.name == "posix":
                receipt_path.chmod(0o600)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temporary_name).unlink(missing_ok=True)
            raise
    emit(receipt)
    return 0 if receipt["result"] == "passed" else 1


def cmd_scan(args: argparse.Namespace) -> int:
    findings = scan_tree(args.root)
    root_label = "." if args.root == "." else Path(args.root).name
    emit({"root": root_label, "status": "ok" if not findings else "failed", "findings": findings})
    return 0 if not findings else 1


def cmd_bundle(args: argparse.Namespace) -> int:
    emit(build_bundle(args.root, args.output))
    return 0


def cmd_bundle_verify(args: argparse.Namespace) -> int:
    emit(verify_bundle(args.bundle))
    return 0


def cmd_contract_validate(args: argparse.Namespace) -> int:
    emit(validate_files(args.schema, args.document))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="qingtian", description="Portable Qingtian AI control-plane core")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify the zero-dependency local runtime")
    doctor.set_defaults(func=cmd_doctor)

    contract_validate = sub.add_parser(
        "contract-validate", help="validate JSON with the schema subset supported by this release"
    )
    contract_validate.add_argument("--schema", required=True)
    contract_validate.add_argument("--document", required=True)
    contract_validate.set_defaults(func=cmd_contract_validate)

    init = sub.add_parser("init", help="initialize a local SQLite authority")
    init.add_argument("--db", required=True)
    init.set_defaults(func=cmd_init)

    demo = sub.add_parser("demo", help="run a synthetic end-to-end control-plane flow")
    demo.add_argument("--db", required=True)
    demo.set_defaults(func=cmd_demo)

    demo_web = sub.add_parser(
        "demo-web", help="open a disposable, offline guided demo on 127.0.0.1"
    )
    demo_web.add_argument("--port", type=int, default=8787, help="loopback port (0 chooses a free port)")
    demo_web.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")
    demo_web.set_defaults(func=cmd_demo_web)

    demo_check = sub.add_parser("demo-check", help="run an isolated synthetic API or browser end-to-end check")
    demo_check.add_argument("--capability", choices=["api-e2e", "browser-e2e"], default="browser-e2e")
    demo_check.set_defaults(func=cmd_demo_check)

    task_create = sub.add_parser("task-create", help="create a DRAFT task")
    task_create.add_argument("--db", required=True)
    task_create.add_argument("--project", required=True)
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--objective", required=True)
    task_create.add_argument("--scope", action="append", required=True)
    task_create.add_argument("--acceptance", action="append", required=True)
    task_create.set_defaults(func=cmd_task_create)

    task_transition = sub.add_parser("task-transition", help="CAS transition a task")
    task_transition.add_argument("--db", required=True)
    task_transition.add_argument("--task-id", required=True)
    task_transition.add_argument("--to", choices=[item.value for item in TaskState], required=True)
    task_transition.add_argument("--expected-revision", type=int, required=True)
    task_transition.set_defaults(func=cmd_task_transition)

    task_show = sub.add_parser(
        "task-show", help="export one task and Task/Session/Run-addressed evidence"
    )
    task_show.add_argument("--db", required=True)
    task_show.add_argument("--task-id", required=True)
    task_show.set_defaults(func=cmd_task_show)

    knowledge_add = sub.add_parser("knowledge-add", help="add a scoped knowledge record")
    knowledge_add.add_argument("--db", required=True)
    knowledge_add.add_argument("--project", required=True)
    knowledge_add.add_argument("--scope", required=True)
    knowledge_add.add_argument(
        "--classification", choices=["public", "internal", "confidential", "restricted"], required=True
    )
    knowledge_add.add_argument("--kind", required=True)
    knowledge_add.add_argument("--title", required=True)
    knowledge_add.add_argument("--content", required=True)
    knowledge_add.add_argument("--source", required=True)
    knowledge_add.add_argument("--source-revision")
    knowledge_add.add_argument(
        "--evidence-label",
        choices=["SOURCE", "OBSERVED", "REPORTED", "INFERRED", "MISSING"],
        required=True,
    )
    knowledge_add.set_defaults(func=cmd_knowledge_add)

    knowledge_search = sub.add_parser(
        "knowledge-search", help="search within caller-supplied project, scope, and classification bounds"
    )
    knowledge_search.add_argument("--db", required=True)
    knowledge_search.add_argument("--project", required=True)
    knowledge_search.add_argument("--query", required=True)
    knowledge_search.add_argument("--allowed-scope", action="append", required=True)
    knowledge_search.add_argument(
        "--max-classification",
        choices=["public", "internal", "confidential", "restricted"],
        default="internal",
    )
    knowledge_search.add_argument("--limit", type=int, default=10)
    knowledge_search.set_defaults(func=cmd_knowledge_search)

    verify = sub.add_parser("verify", help="run an adapter's side-effect classified checks")
    verify.add_argument("--adapter", required=True)
    verify.add_argument("--profile", default="smoke")
    verify.add_argument("--receipt")
    verify.add_argument("--allow-local-writes", action="store_true")
    verify.add_argument(
        "--execute-trusted-adapter",
        action="store_true",
        help="acknowledge that adapter commands are trusted local programs, not sandboxed code",
    )
    verify.set_defaults(func=cmd_verify)

    scan = sub.add_parser("scan", help="scan a release tree for obvious secrets and host paths")
    scan.add_argument("--root", default=".")
    scan.set_defaults(func=cmd_scan)

    bundle = sub.add_parser("bundle", help="create a manifest-verified source archive")
    bundle.add_argument("--root", default=".")
    bundle.add_argument("--output", required=True)
    bundle.set_defaults(func=cmd_bundle)

    bundle_verify = sub.add_parser("bundle-verify", help="verify archive paths and MANIFEST hashes")
    bundle_verify.add_argument("--bundle", required=True)
    bundle_verify.set_defaults(func=cmd_bundle_verify)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return int(args.func(args))
    except (
        QingtianError,
        ValueError,
        KeyError,
        OSError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        tarfile.TarError,
    ) as exc:
        emit({"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
