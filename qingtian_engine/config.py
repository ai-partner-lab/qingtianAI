from __future__ import annotations

import json
import os
import re
from importlib.resources import files
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path.cwd()
DEFAULT_PORT = 8766
EXECUTION_MODEL = "gpt-5.6-sol"
EXECUTION_REASONING = "high"
EXECUTION_MODELS = frozenset({"gpt-5.6-sol", "gpt-6-astra"})
EXECUTION_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
EXECUTION_SPEEDS = frozenset({"standard", "fast"})


def default_data_dir() -> Path:
    """Resolve writable user state independently of an installed package."""
    override = os.environ.get("QINGTIAN_ENGINE_HOME")
    if override is not None:
        if not override.strip():
            raise ValueError("QINGTIAN_ENGINE_HOME must not be empty")
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "qingtian" / "engine"


def require_execution_model(model: str, reasoning: str) -> None:
    if not isinstance(model, str) or not isinstance(reasoning, str) or model not in EXECUTION_MODELS or reasoning not in EXECUTION_EFFORTS:
        raise ValueError(
            "MODEL_POLICY: choose explicit gpt-5.6-sol or gpt-6-astra and a supported "
            "low/medium/high/xhigh/max/ultra effort; no alias, family-wide permission or fallback"
        )


def default_workspace() -> Path:
    return Path(os.environ.get("QINGTIAN_WORKSPACE", str(Path.cwd()))).expanduser()


DEFAULT_DATA_DIR = default_data_dir()
DEFAULT_POLICY_PATH = PACKAGE_DIR / "resources" / "policy.json"


def load_policy(path: Optional[Path] = None) -> Dict[str, Any]:
    if path is None and "QINGTIAN_POLICY_PATH" in os.environ:
        override = os.environ["QINGTIAN_POLICY_PATH"]
        if not override.strip():
            raise ValueError("QINGTIAN_POLICY_PATH must not be empty")
        path = Path(override).expanduser()
    resource = Path(path) if path is not None else files("qingtian_engine").joinpath("resources/policy.json")
    with resource.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class RuntimePolicy:
    model: str
    reasoning: str
    speed: str
    enable_fast_mode: bool


REASONING_RANKS = {name: index for index, name in enumerate(
    ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
)}


def validate_model(model: Any) -> str:
    # Model availability is provider/account dependent. Preserve valid IDs and
    # let Codex reject unavailable models; never replace them with a fallback.
    if not isinstance(model, str) or model not in EXECUTION_MODELS:
        raise ValueError("MODEL_POLICY: choose gpt-5.6-sol or gpt-6-astra; no fallback")
    return model


def validate_reasoning(reasoning: Any) -> str:
    if not isinstance(reasoning, str) or reasoning not in EXECUTION_EFFORTS:
        raise ValueError("invalid reasoning effort")
    return reasoning


def validate_speed(speed: Any) -> str:
    if not isinstance(speed, str) or speed not in {"fast", "standard"}:
        raise ValueError("invalid execution speed")
    return speed


def runtime_policy(
    requested_reasoning: Optional[str] = None,
    now: Optional[datetime] = None,
    policy: Optional[Dict[str, Any]] = None,
    *,
    requested_model: Optional[str] = None,
    requested_speed: Optional[str] = None,
    explicit_reasoning: bool = False,
    pinned: bool = False,
    role: str = "executor",
) -> RuntimePolicy:
    policy = load_policy() if policy is None else policy
    if policy.get("schema_version") != 2 or role not in policy.get("defaults", {}):
        raise ValueError("MODEL_POLICY: role-based policy v2 required; explicitly migrate the old fixed policy")
    defaults = policy["defaults"][role]
    if pinned:
        require_execution_model(requested_model, requested_reasoning)
        if requested_speed is None:
            raise ValueError("MODEL_PINNING: explicit immutable speed is required")
    model = os.environ.get("QINGTIAN_MODEL", defaults["model"]) if requested_model is None else requested_model
    reasoning = (os.environ.get("QINGTIAN_REASONING", defaults["reasoning"]) if requested_reasoning is None
                 else requested_reasoning)
    speed = os.environ.get("QINGTIAN_SPEED", defaults["speed"]) if requested_speed is None else requested_speed
    require_execution_model(model, reasoning)
    if not isinstance(speed, str) or speed not in EXECUTION_SPEEDS:
        raise ValueError("MODEL_POLICY: speed must be standard or fast, separately from reasoning")
    # now remains accepted for caller compatibility, but never changes a pinned
    # choice or silently swaps speed at a day/night boundary.
    return RuntimePolicy(model=model, reasoning=reasoning, speed=speed, enable_fast_mode=speed == "fast")


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
