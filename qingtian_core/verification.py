from __future__ import annotations

import json
from hashlib import sha256
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic
from typing import Any

from .contracts import bundled_schema, validate
from .models import content_hash, utc_now


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def run_checks(
    adapter_path: str | Path,
    *,
    profile: str = "smoke",
    allow_local_writes: bool = False,
    execute_trusted_adapter: bool = False,
) -> dict[str, Any]:
    adapter_file = Path(adapter_path).resolve()
    adapter = json.loads(adapter_file.read_text(encoding="utf-8"))
    validate(adapter, bundled_schema("project-adapter"))
    if not execute_trusted_adapter:
        raise ValueError(
            "project adapters execute local programs; pass explicit trusted-adapter acknowledgement"
        )
    project_root = (adapter_file.parent / adapter.get("project_root", ".")).resolve()
    if not project_root.is_dir():
        raise ValueError(f"project_root is not a directory: {project_root}")
    selected = []
    for check in adapter.get("checks", []):
        if profile in check.get("profiles", []):
            selected.append(check)
    if not selected:
        raise ValueError(f"no checks selected for profile: {profile}")
    all_ids = [check["id"] for check in adapter["checks"]]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("project adapter check ids must be unique")

    results: list[dict[str, Any]] = []
    inherited_names = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT", "COMSPEC", "PATHEXT")
    child_environment = {
        name: os.environ[name] for name in inherited_names if name in os.environ
    }
    if "PYTHONPATH" in os.environ:
        child_environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for check in selected:
        command = check.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
            raise ValueError(f"check {check.get('id')} must use a non-empty argv array")
        side_effect = check.get("side_effect", "none")
        if side_effect == "external":
            raise ValueError(f"external side-effect check is never run by the portable verifier: {check['id']}")
        if side_effect == "local" and not allow_local_writes:
            raise ValueError(f"local-write check requires --allow-local-writes: {check['id']}")
        cwd = (project_root / check.get("cwd", ".")).resolve()
        if not _inside(project_root, cwd) or not cwd.is_dir():
            raise ValueError(f"check cwd escapes project_root: {check['id']}")
        timeout = int(check.get("timeout_seconds", 60))
        started = monotonic()
        timed_out = False
        with tempfile.TemporaryFile() as output_file:
            try:
                process = subprocess.run(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    env=child_environment,
                    timeout=timeout,
                    check=False,
                )
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124
            except OSError:
                exit_code = 127
                execution_error = True
            else:
                execution_error = False
            output_file.flush()
            output_file.seek(0, 2)
            output_size = output_file.tell()
            output_file.seek(0)
            output_digest = sha256()
            while chunk := output_file.read(1024 * 1024):
                output_digest.update(chunk)
            output_file.seek(max(0, output_size - 8000))
            output_tail = output_file.read()
        result = {
            "id": check["id"],
            "category": check.get("category", "unspecified"),
            "status": "passed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "duration_ms": round((monotonic() - started) * 1000),
            "output_bytes": output_size,
            "output_sha256": output_digest.hexdigest(),
        }
        if timed_out:
            result["failure_kind"] = "timeout"
        elif execution_error:
            result["failure_kind"] = "execution_error"
        if check.get("include_output", False):
            result["output_tail"] = output_tail.decode("utf-8", errors="replace")
        results.append(result)
        if exit_code != 0:
            break
    receipt = {
        "receipt_version": 1,
        "adapter_id": adapter.get("project_id", "unknown"),
        "adapter_hash": content_hash(adapter),
        "profile": profile,
        "observed_at": utc_now(),
        "runtime": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "os": platform.system() or "unknown",
        },
        "execution_policy": {
            "adapter_trusted": True,
            "shell": False,
            "environment": "minimal",
            "filesystem_sandbox": False,
            "network_sandbox": False,
        },
        "result": "passed" if results and all(item["status"] == "passed" for item in results) else "failed",
        "checks": results,
    }
    receipt["receipt_hash"] = content_hash(receipt)
    validate(receipt, bundled_schema("verification-receipt"))
    return receipt
