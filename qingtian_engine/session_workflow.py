"""Portable, read-only description of Qingtian's session workflow boundary.

This module reports capabilities without attempting onboarding, authentication,
delivery, dispatch, or recovery. A different machine must configure and verify
its own host integrations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def configured_executable_present(
    default_name: str,
    override_variable: str,
    *,
    resolver: Callable[[str], Optional[str]],
) -> bool:
    """Check a supported executable selection without running or exposing it.

    An explicitly present override is authoritative, including an empty or
    invalid value. Absolute paths are checked directly; simple command names
    use the supplied PATH resolver. Relative paths are not accepted.
    """
    if override_variable not in os.environ:
        return bool(resolver(default_name))
    configured = os.environ.get(override_variable, "")
    if not configured or not configured.strip():
        return False
    candidate = Path(configured)
    if candidate.is_absolute():
        return candidate.is_file() and os.access(candidate, os.X_OK)
    if "/" in configured or "\\" in configured:
        return False
    return bool(resolver(configured))


def session_workflow_profile(
    *, data_dir: Path, workspace: Path, codex_present: bool, git_present: bool,
) -> Dict[str, Any]:
    """Return a data-free host capability profile without mutating local state."""
    data_dir = Path(data_dir).expanduser().resolve()
    workspace = Path(workspace).expanduser().resolve()
    supported_platform = os.name == "posix"
    supported_python = (3, 11) <= sys.version_info[:2] <= (3, 14)
    blockers = []
    if not supported_platform:
        blockers.append("unsupported_platform")
    if not supported_python:
        blockers.append("unsupported_python")
    if not workspace.is_dir():
        blockers.append("workspace_missing")
    if not codex_present:
        blockers.append("codex_cli_missing")
    if not git_present:
        blockers.append("git_missing")
    blockers.extend(("manager_role_unverified", "native_delivery_bridge_unavailable"))

    return {
        "schema_version": 1,
        "read_only": True,
        "default_mode": "manual",
        "host": {
            "platform": sys.platform,
            "supported_platform": supported_platform,
            "python": sys.version.split()[0],
            "supported_python": supported_python,
            "workspace_exists": workspace.is_dir(),
            "data_dir_exists": data_dir.exists(),
            "codex_cli_present": bool(codex_present),
            "git_present": bool(git_present),
            "credentials_checked": False,
            "app_server_protocol_checked": False,
            "reviewed_model_capabilities_checked": False,
        },
        "capabilities": {
            "message_preparation": {"implemented": True, "delivers": False},
            "lifecycle_ledger": {"implemented": True, "native_delivery": False},
            "manager_entry": {
                "implemented": True,
                "effective_role_verified": False,
                "workflow_ready": False,
            },
        },
        "separate_facts": [
            "message_prepared",
            "transport_delivered",
            "handoff_accepted",
            "execution_started",
            "qa_passed",
            "task_completed",
            "deployment_verified",
        ],
        "notification_bridge": {
            "mode": "manual",
            "native_delivery": False,
            "identity_assurance": "trusted_local_caller_attestation",
        },
        "portable_configuration": {
            "environment_names": [
                "QINGTIAN_ENGINE_HOME",
                "QINGTIAN_CODEX_BIN",
                "QINGTIAN_CODEX_TRANSPORT",
                "QINGTIAN_CODEX_SOCKET",
                "QINGTIAN_CODEX_CAPABILITIES",
                "QINGTIAN_POLICY_PATH",
                "QINGTIAN_PROJECTS_CONFIG",
            ],
            "values_included": False,
            "project_registry": "machine_local",
            "capability_manifest": "machine_local_review_required",
            "runtime_database": "machine_local_do_not_copy",
        },
        "manual_recording_ready": bool(
            supported_platform and supported_python and workspace.is_dir()
        ),
        "native_delivery_ready": False,
        "blockers": blockers,
        "not_verified": [
            "credentials",
            "provider_model_availability_or_served_tier",
            "reviewed_capability_manifest",
            "app_server_methods",
            "desktop_visibility_or_pinning",
            "effective_manager_role",
            "native_message_delivery",
            "another_machine_end_to_end",
        ],
    }
