"""Run one isolated real-Worker check from a Qingtian source checkout.

This is separate from the credential-free ``qingtian selftest`` command and
requires the explicit ``--run`` flag. It consumes one real Codex invocation,
uses a new synthetic Git repository and never imports an existing task store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def actual_execution_configuration(session_id: str) -> dict[str, str]:
    """Read only model metadata for one captured session; never copy raw logs."""
    if re.fullmatch(r"[0-9a-f-]{36}", session_id or "") is None:
        return {}
    sessions = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    for source in sessions.glob("*/*/*/*" + session_id + ".jsonl"):
        with source.open(encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if event.get("type") == "turn_context":
                    payload = event.get("payload", {})
                    return {
                        "model": payload.get("model"),
                        "reasoning": payload.get("effort"),
                        "source": str(source),
                        "session_id": session_id,
                    }
    return {}


def selected_execution_matches(run: dict, actual: dict) -> bool:
    """Compare the observed session with the persisted run tuple exactly."""
    from qingtian_engine.config import EXECUTION_SPEEDS, require_execution_model

    try:
        require_execution_model(run.get("model"), run.get("reasoning"))
    except ValueError:
        return False
    return bool(
        run.get("speed") in EXECUTION_SPEEDS
        and run.get("session_id")
        and actual.get("session_id") == run.get("session_id")
        and actual.get("model") == run.get("model")
        and actual.get("reasoning") == run.get("reasoning")
    )


def verify_result(root, service, task, run, marker, initial_head):
    repo = root / "fixture-repository"
    worktree = Path(task["worktree"])
    if not worktree.resolve().is_relative_to(root.resolve()):
        raise ValueError("selfcheck worktree escaped its own fixture")
    artifact = service.db.one(
        "SELECT value FROM evidence WHERE task_id=? AND kind='artifact' AND value=?",
        (task["id"], marker),
    )
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    current_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    actual = actual_execution_configuration(run["session_id"])
    model_reasoning_verified = selected_execution_matches(run, actual)
    verified = bool(
        model_reasoning_verified
        and run["status"] == "DONE"
        and run["exit_code"] == 0
        and artifact
        and (worktree / "README.md").read_text(encoding="utf-8") == marker + "\n"
        and current_head == initial_head
        and status == ""
    )
    if verified:
        service.add_evidence(
            task["id"],
            "artifact",
            "Independent selfcheck verifier: " + marker,
            verified=True,
        )
        service.reconcile_state_progression()
    receipt = {
        "task_id": task["id"],
        "run_id": run["id"],
        "run_status": run["status"],
        "exit_code": run["exit_code"],
        "session_captured": bool(run["session_id"]),
        "legacy_tasks": 0,
        "existing_task_store_imported": False,
        "fixture_repository": str(repo),
        "worktree": str(worktree),
        "fixture_head": initial_head,
        "verified": verified,
        "artifact_value_sha256": hashlib.sha256(marker.encode()).hexdigest(),
        "verification_source": "persisted evidence + clean fixture worktree + marker match",
        "selected_execution": {
            "model": run["model"],
            "reasoning": run["reasoning"],
            "speed": run["speed"],
        },
        "actual_execution": actual,
        "model_reasoning_verified": model_reasoning_verified,
        "command_summary": run["command_summary"],
        "task_state": service.get_task(task["id"])["state"],
    }
    receipt["event_types"] = [
        row["event_type"]
        for row in service.db.all(
            "SELECT event_type FROM events WHERE task_id=? ORDER BY id", (task["id"],)
        )
    ]
    (root / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)
    return 0 if verified else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--run", action="store_true")
    action.add_argument(
        "--verify-existing",
        type=Path,
        help="Recheck one fixture created by this script; never dispatch",
    )
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(tempfile.gettempdir()) / "qingtian-worker-selfchecks",
        help="Parent for isolated fixtures; defaults to the operating-system temp root",
    )
    parser.add_argument("--model")
    parser.add_argument("--reasoning")
    parser.add_argument("--speed")
    args = parser.parse_args()
    parent = args.output_root.expanduser().resolve()
    if args.verify_existing:
        from qingtian_engine.db import Database
        from qingtian_engine.service import ControlPlane

        root = args.verify_existing.expanduser().resolve()
        if root.parent != parent or not root.name.startswith("worker-"):
            raise ValueError("Only this script's isolated selfcheck directory is accepted")
        previous = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        service = ControlPlane(Database(root / "runtime/control-plane.sqlite3"))
        task = service.get_task(previous["task_id"])
        run = service.db.one(
            "SELECT * FROM runs WHERE id=? AND task_id=?",
            (previous["run_id"], task["id"]),
        )
        repo = root / "fixture-repository"
        if task["repository"] != str(repo) or task["imported_from"]:
            raise ValueError("Not an isolated new selfcheck task")
        initial_head = previous.get("fixture_head") or subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "dev"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        marker = (repo / "README.md").read_text(encoding="utf-8").strip()
        return verify_result(root, service, task, run, marker, initial_head)

    from qingtian_engine.config import runtime_policy
    from qingtian_engine.db import Database
    from qingtian_engine.runner import RunManager
    from qingtian_engine.service import ControlPlane

    policy = runtime_policy(
        args.reasoning,
        requested_model=args.model,
        requested_speed=args.speed,
        role="executor",
    )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix="worker-", dir=parent)).resolve()
    repo = root / "fixture-repository"
    repo.mkdir()
    (root / "git-template").mkdir()
    projects = root / "projects.json"
    inherited_pythonpath = os.environ.get("PYTHONPATH")
    os.environ.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_KEY_1": "commit.gpgsign",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "init.templateDir",
            "GIT_CONFIG_VALUE_2": str(root / "git-template"),
            "QINGTIAN_PROJECTS_CONFIG": str(projects),
            "QINGTIAN_WORKSPACE": str(root),
            "PYTHONPATH": str(ROOT)
            + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
        }
    )

    def git(*words: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *words],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    git("init", "-b", "dev")
    git("config", "user.name", "Qingtian Engine Selfcheck")
    git("config", "user.email", "selfcheck@example.invalid")
    marker = "QINGTIAN_ENGINE_SELF_CHECK_OK:" + uuid4().hex[:12]
    (repo / "README.md").write_text(marker + "\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "Initialize isolated engine fixture")
    initial_head = git("rev-parse", "HEAD")
    projects.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": {
                    "selfcheck": {
                        "repository": str(repo),
                        "base_branch": "dev",
                        "roles": ["selfcheck"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runtime = root / "runtime"
    service = ControlPlane(Database(runtime / "control-plane.sqlite3"))
    task = service.create_task(
        "Qingtian engine isolated selfcheck",
        owner_session="selfcheck",
        worker_type="cli",
        scope_summary="Read one synthetic marker and return artifact evidence",
        repository=str(repo),
        base_branch="dev",
        environment="local",
        evidence_profile="artifact",
        model=policy.model,
        reasoning=policy.reasoning,
        speed=policy.speed,
    )
    prompt = root / "instruction.txt"
    prompt.write_text(
        "Perform only this isolated engine selfcheck. Read the single line in README.md "
        "and write it unchanged as the artifact string value in the evidence JSON file "
        "specified by the wrapper instructions. Do not edit files, commit, push, deploy, "
        "read other repositories or task stores, or use network tools.",
        encoding="utf-8",
    )
    os.chmod(prompt, 0o600)
    manager = RunManager(service, runtime)
    run = manager.dispatch(task["id"], prompt)
    print(
        json.dumps(
            {
                "stage": "dispatched",
                "task_id": task["id"],
                "run_id": run["id"],
                "evidence_root": str(root),
                "legacy_tasks": 0,
                "selected_execution": {
                    "model": run["model"],
                    "reasoning": run["reasoning"],
                    "speed": run["speed"],
                },
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        run = service.db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        if run["status"] in {"DONE", "FAILED", "CANCELED"}:
            break
        time.sleep(1)
    else:
        manager.cancel(task["id"])
        raise RuntimeError("Isolated selfcheck timed out; only its own Worker was canceled")
    task = service.get_task(task["id"])
    return verify_result(root, service, task, run, marker, initial_head)


if __name__ == "__main__":
    raise SystemExit(main())
