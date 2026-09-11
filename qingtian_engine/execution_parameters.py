"""Exact execution choices, host capability checks, and immutable run bindings."""
from __future__ import annotations

import json

from .config import (RuntimePolicy, EXECUTION_SPEEDS,
                     require_execution_model)
from .codex_capabilities import load_codex_capabilities
from .admission import TARGET_FIELDS


PARAMETERS = ("model", "reasoning", "speed")


def explicit_parameters(value):
    require_execution_model(value.get("model"), value.get("reasoning"))
    speed = value.get("speed")
    if not isinstance(speed, str) or speed not in EXECUTION_SPEEDS:
        raise ValueError("MODEL_POLICY: explicit standard/fast speed is required")
    return RuntimePolicy(value["model"], value["reasoning"], speed, speed == "fast")


def _checked_manifest(policy):
    # Production callers cannot bypass host verification by passing a raw dict.
    explicit_parameters({key: getattr(policy, key) for key in PARAMETERS})
    if policy.enable_fast_mode != (policy.speed == "fast"):
        raise ValueError("MODEL_POLICY: speed and fast flag disagree")
    manifest = load_codex_capabilities()
    models = manifest.get("models") if isinstance(manifest, dict) else None
    model = models.get(policy.model) if isinstance(models, dict) else None
    if (not isinstance(manifest, dict) or manifest.get("adapter") != "codex-cli" or not isinstance(model, dict)
            or policy.reasoning not in model.get("reasoning", [])
            or policy.speed not in model.get("speed", [])):
        raise ValueError("MODEL_CAPABILITY: codex-cli does not advertise the exact requested model/effort/speed; no fallback")
    return manifest


def require_codex_capability(policy):
    _checked_manifest(policy)
    return policy


def _model_arguments(policy):
    # Official CLI config contract: Fast is service_tier=priority. Never use a
    # stale fast_mode feature flag; standard is explicit so user config cannot
    # silently opt an existing run into a different speed.
    return ["-m", policy.model, "-c", 'model_reasoning_effort="' + policy.reasoning + '"',
            "-c", 'service_tier="' + ("priority" if policy.enable_fast_mode else "default") + '"']


def codex_model_arguments(policy):
    _checked_manifest(policy)
    return _model_arguments(policy)


def codex_command_prefix(policy, *subcommands):
    manifest = _checked_manifest(policy)
    # Execute the verified absolute file, not a later PATH lookup of "codex".
    return [manifest["cli_path"], *subcommands, *_model_arguments(policy)]


def pinned_run_parameters(db, run):
    # P1 captures these native fields at the real run INSERT and makes the row
    # immutable. Reuse that authority; do not backfill old runs from current tasks
    # or parse display command_summary as an execution receipt.
    stored = db.one("SELECT task_id,target_json FROM admission_run_targets WHERE run_id=?", (run["id"],))
    if not stored or stored["task_id"] != run["task_id"]:
        raise ValueError("MODEL_PINNING: historical run has no immutable execution parameters; cannot safely resume")
    target = json.loads(stored["target_json"])
    if not isinstance(target, dict) or set(target) != set(TARGET_FIELDS) or any(not isinstance(target[key], str) for key in TARGET_FIELDS):
        raise ValueError("MODEL_PINNING: immutable execution target is incomplete; cannot safely resume")
    pinned = explicit_parameters(target)
    # The public schema already records the actual run tuple. Never let a later
    # mutable-row edit override or contradict the insert-time immutable target.
    if any(run.get(key) not in (None, "", getattr(pinned, key)) for key in PARAMETERS):
        raise ValueError("MODEL_PINNING: run parameters disagree with immutable execution target")
    return pinned


def require_same_execution_target(db, task, run):
    stored = db.one("SELECT task_id,target_json FROM admission_run_targets WHERE run_id=?", (run["id"],))
    if not stored or stored["task_id"] != task["id"]:
        raise ValueError("MODEL_PINNING: historical run has no immutable execution target")
    target = json.loads(stored["target_json"])
    if not isinstance(target, dict) or set(target) != set(TARGET_FIELDS) or any(task.get(key) != target[key] for key in TARGET_FIELDS):
        raise ValueError("MODEL_PINNING: current task differs from immutable seven-field execution target")


def require_same_parameters(task, pinned):
    if any(task.get(key) != getattr(pinned, key) for key in PARAMETERS):
        raise ValueError("MODEL_PINNING: current task differs from pinned run; do not change historical resume parameters")


def execution_policy_prompt(policy):
    return ("本次固定执行参数：model={model}，reasoning={reasoning}，speed={speed}。"
            "模型、推理和速度分别设置，禁止静默替换。下派执行器按任务风险明确选择 "
            "gpt-5.6-sol 或 gpt-6-astra 及目标宿主支持的推理强度；模型下限为 Sol，"
            "Terra/Luna/Spark/旧模型与未核验默认均不允许。速度不强制继承；能力不支持时明确报告，"
            "不改登记冒充执行。历史已固定运行保持原参数，不因政策变化恢复旧任务。"
            ).format(model=policy.model, reasoning=policy.reasoning, speed=policy.speed)
