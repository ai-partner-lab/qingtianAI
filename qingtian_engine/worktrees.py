from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict

from .config import ensure_data_dirs
from .service import ControlPlane


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def prepare_worktree(
    service: ControlPlane, task_id: str, data_dir: Path, dry_run: bool = False
) -> Dict[str, str]:
    task = service.get_task(task_id)
    if not task["repository"]:
        return {"worktree": "", "branch": "", "base": ""}
    repo = Path(task["repository"]).expanduser().resolve()
    _git(repo, "rev-parse", "--git-dir")
    paths = ensure_data_dirs(data_dir)
    worktree = (paths["worktrees"] / task_id).resolve()
    if task["worktree"]:
        persisted = Path(task["worktree"]).expanduser().resolve()
        if persisted != worktree:
            raise RuntimeError("persisted worktree is outside the engine boundary")
        if persisted.exists():
            if not persisted.is_dir():
                raise RuntimeError("persisted worktree is not a directory")
            return {
                "worktree": str(persisted),
                "branch": task["branch"],
                "base": task["base_branch"],
            }
    safe_title = re.sub(r"[^a-z0-9]+", "-", task["short_summary"].lower()).strip("-")
    branch = task["branch"] or "qingtian/{}-{}".format(
        task_id, safe_title or "task"
    )
    base = task["base_branch"] or "origin/dev"
    if worktree.exists():
        raise RuntimeError("worktree path already exists: {}".format(worktree))
    if dry_run:
        return {"worktree": str(worktree), "branch": branch, "base": base}
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    service.db.execute(
        "UPDATE tasks SET worktree=?, branch=?, base_branch=?, updated_at=datetime('now') "
        "WHERE id=?",
        (str(worktree), branch, base, task_id),
    )
    service.db.add_event(
        task_id,
        "worktree.prepared",
        "worktree-manager",
        "独立 worktree 已创建",
        "worktree:{}:{}".format(task_id, branch),
        {"state": task["state"]},
    )
    return {"worktree": str(worktree), "branch": branch, "base": base}
