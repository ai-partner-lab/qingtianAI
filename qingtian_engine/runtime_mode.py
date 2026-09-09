"""Explicit scheduling policy for a local, writable engine (not read-only)."""
from __future__ import annotations

from typing import Any, Dict


ENGINE_MODES = ("manual", "auto")


def validate_mode(mode: str) -> str:
    if mode not in ENGINE_MODES:
        raise ValueError("engine mode must be manual or auto")
    return mode


def background_cycle(manager: Any, coordinator: Any, mode: str) -> Dict[str, Any]:
    """Manual mode may reconcile evidence/state, but never claims or recovers work.

    In particular, do not replace reconcile() with scheduler_tick(), or call
    verification/infrastructure recovery helpers here: those can spawn workers.
    """
    validate_mode(mode)
    if mode == "auto":
        return (
            coordinator.tick(max_new=1, max_active=3)
            if coordinator is not None
            else manager.scheduler_tick(max_new=1, max_active=3)
        )
    return {
        "mode": "manual",
        "automatic_dispatch": False,
        "reconcile": manager.reconcile(),
        "dispatch": {"claimed": []},
        "verification": {"claimed": []},
    }
