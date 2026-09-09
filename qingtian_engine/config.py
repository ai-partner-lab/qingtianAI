from __future__ import annotations

import json
import os
from importlib.resources import files
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path.cwd()
DEFAULT_PORT = 8766


def default_data_dir() -> Path:
    """Resolve writable user state independently of an installed package."""
    override = os.environ.get("QINGTIAN_ENGINE_HOME")
    if override is not None:
        if not override.strip():
            raise ValueError("QINGTIAN_ENGINE_HOME must not be empty")
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "qingtian" / "engine"


def default_workspace() -> Path:
    return Path(os.environ.get("QINGTIAN_WORKSPACE", str(Path.cwd()))).expanduser()


DEFAULT_DATA_DIR = default_data_dir()
DEFAULT_POLICY_PATH = PACKAGE_DIR / "resources" / "policy.json"


def load_policy(path: Optional[Path] = None) -> Dict[str, Any]:
    resource = Path(path) if path is not None else files("qingtian_engine").joinpath("resources/policy.json")
    with resource.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class RuntimePolicy:
    model: str
    reasoning: str
    speed: str
    enable_fast_mode: bool


def runtime_policy(
    requested_reasoning: str = "high",
    now: Optional[datetime] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> RuntimePolicy:
    policy = policy or load_policy()
    timezone = ZoneInfo(policy["daytime"]["timezone"])
    local_now = now.astimezone(timezone) if now else datetime.now(timezone)
    start = int(policy["daytime"]["start_hour"])
    end = int(policy["daytime"]["end_hour"])
    daytime = start <= local_now.hour < end

    ranks = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "ultra": 5}
    minimum = str(policy["minimum_reasoning"])
    requested = requested_reasoning if requested_reasoning in ranks else minimum
    reasoning = requested if ranks[requested] >= ranks[minimum] else minimum
    fast = bool(policy["daytime"]["enable_fast_mode"]) if daytime else bool(
        policy["nighttime"]["enable_fast_mode"]
    )
    return RuntimePolicy(
        model=str(policy["model"]),
        reasoning=reasoning,
        speed="fast" if fast else "standard",
        enable_fast_mode=fast,
    )


def ensure_data_dirs(data_dir: Optional[Path] = None) -> Dict[str, Path]:
    data_dir = Path(data_dir if data_dir is not None else default_data_dir()).expanduser().resolve()
    paths = {
        "root": data_dir,
        "db": data_dir / "control-plane.sqlite3",
        "run": data_dir / "run",
        "evidence": data_dir / "evidence",
        "prompts": data_dir / "prompts",
        "worktrees": data_dir / "worktrees",
        "reports": data_dir / "reports",
        "intake": data_dir / "intake",
        "config": data_dir / "config",
    }
    for key, path in paths.items():
        if key != "db":
            path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
        os.chmod(paths["prompts"], 0o700)
        os.chmod(paths["intake"], 0o700)
    except OSError:
        pass
    return paths
