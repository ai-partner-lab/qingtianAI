"""Local capability evidence checks, not remote/account authentication.

OS identity, effective user and current CLI/cache are independent of the
installed manifest. A trusted local operator must review/install that file.
Same-user/root tampering and cloned OS identities are outside this trust model.
No credentials, sessions, network, model request or automatic installation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import uuid

from .config import default_data_dir, require_execution_model


MAX_EVIDENCE_AGE = timedelta(hours=24)
FIELDS = {"schema_version", "adapter", "host_identity", "uid", "codex_home",
          "cli_path", "cli_version", "binary_sha256", "source_kind", "source_ref",
          "source_sha256", "observed_at", "expires_at", "models"}
RFC3339 = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"


def _error(message):
    return ValueError("MODEL_CAPABILITY: " + message)


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(RFC3339, value) or value.endswith("-00:00"):
        raise _error("explicit RFC3339 timestamp with timezone required")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise _error("invalid capability timestamp") from exc


def _utcnow():
    return datetime.now(timezone.utc)


def _json(raw):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _error("duplicate JSON field in capability evidence")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique_object)


def _host_identity():
    """OS-install identifier, deliberately not hostname/model or a secret."""
    try:
        system = platform.system()
        if system == "Darwin":
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-a", "-r", "-d", "1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, check=True, timeout=5,
            )
            rows = plistlib.loads(result.stdout)
            raw = str(uuid.UUID(rows[0]["IOPlatformUUID"]))
            kind = "macos-ioplatformuuid-sha256"
        elif system == "Linux":
            raw = Path("/etc/machine-id").read_text(encoding="ascii").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", raw) or int(raw, 16) == 0:
                raise ValueError("invalid machine-id")
            kind = "linux-machine-id-sha256"
        else:
            raise ValueError("unsupported OS identity source")
        if not raw or raw == str(uuid.UUID(int=0)):
            raise ValueError("empty OS identity")
        digest = hashlib.sha256((kind + ":" + raw).encode("ascii")).hexdigest()
        return {"kind": kind, "sha256": digest}
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError,
            plistlib.InvalidFileException) as exc:
        raise _error("cannot independently verify local OS-install identity") from exc


def _codex_home():
    # Same environment used by the child CLI; no credential/config/session read.
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve(strict=True)


def _cli_identity():
    executable = shutil.which("codex")
    if not executable:
        raise _error("target CLI is unavailable")
    path = Path(executable).resolve(strict=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # Bounded, read-only --version only. Never invoke exec/app-server/model/list.
    result = subprocess.run([str(path), "--version"], capture_output=True,
                            text=True, check=True, timeout=5)
    match = re.fullmatch(r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)", result.stdout.strip())
    if not match:
        raise _error("unrecognized actual CLI version")
    return str(path), match.group(1), digest


def _models_from_catalog(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise _error("unrecognized local model catalog")
    models = {}
    for item in catalog["models"]:
        if not isinstance(item, dict):
            raise _error("malformed local model catalog entry")
        slug = item.get("slug")
        if slug not in ("gpt-5.6-sol", "gpt-6-astra"):
            continue
        if slug in models:
            raise _error("ambiguous duplicate catalog model")
        levels = item.get("supported_reasoning_levels")
        tiers = item.get("service_tiers")
        if (not isinstance(levels, list) or not levels or not isinstance(tiers, list)
                or any(not isinstance(level, dict) or not isinstance(level.get("effort"), str) for level in levels)
                or any(not isinstance(tier, dict) or not isinstance(tier.get("id"), str) for tier in tiers)):
            raise _error("catalog does not explicitly advertise reasoning/service tiers")
        models[slug] = {"reasoning": [level["effort"] for level in levels],
                        "speed": ["standard"] + (["fast"] if any(t["id"] == "priority" for t in tiers) else [])}
    return models


def _validate_models(models, advertised):
    if not isinstance(models, dict) or not models:
        raise _error("nonempty reviewed model capabilities required")
    for name, entry in models.items():
        if not isinstance(entry, dict) or set(entry) != {"reasoning", "speed"} or name not in advertised:
            raise _error("reviewed model is not in the actual local catalog")
        for field in ("reasoning", "speed"):
            values = entry[field]
            if (not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values)
                    or len(set(values)) != len(values) or not set(values) <= set(advertised[name][field])):
                raise _error("reviewed capabilities exceed or misrepresent the local catalog")
        for effort in entry["reasoning"]:
            require_execution_model(name, effort)


def load_codex_capabilities():
    """Validate schema-v2 reviewed local-cache evidence on every use.

    Only the current Codex home's models_cache.json is an accepted source.
    This is local advertisement, not proof of account access or served tier.
    """
    path = Path(os.environ.get("QINGTIAN_CODEX_CAPABILITIES", default_data_dir() / "config/codex-capabilities.json"))
    if not path.is_file():
        raise _error("no reviewed codex-cli capability manifest; run qingtian capabilities status and follow docs/CODEX-CAPABILITIES.md")
    try:
        manifest = _json(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or set(manifest) != FIELDS
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 2
                or manifest["adapter"] != "codex-cli"
                or manifest["source_kind"] != "codex-local-model-cache-v1"):
            raise _error("unsupported or incomplete capability evidence schema/source/adapter")
        observed, expires, now = _timestamp(manifest["observed_at"]), _timestamp(manifest["expires_at"]), _utcnow()
        if not observed <= now < expires or not timedelta(0) < expires - observed <= MAX_EVIDENCE_AGE:
            raise _error("capability evidence is future, expired or exceeds the 24-hour lifetime")
        if manifest["host_identity"] != _host_identity():
            raise _error("evidence belongs to a different OS-install identity")
        codex_home = _codex_home()
        if (type(manifest["uid"]) is not int or manifest["uid"] != os.geteuid()
                or manifest["codex_home"] != str(codex_home)):
            raise _error("evidence belongs to a different effective user/Codex home")
        cli_path, version, binary_sha256 = _cli_identity()
        if (manifest["cli_path"], manifest["cli_version"], manifest["binary_sha256"]) != (cli_path, version, binary_sha256):
            raise _error("actual CLI path/version/bytes do not match reviewed evidence")
        catalog_path = codex_home / "models_cache.json"
        if manifest["source_ref"] != str(catalog_path) or catalog_path.is_symlink():
            raise _error("source is not the current fixed local model-cache path")
        raw = catalog_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["source_sha256"]:
            raise _error("actual local catalog has changed; explicit evidence review required")
        catalog = _json(raw)
        if (not isinstance(catalog, dict) or catalog.get("client_version") != version
                or _timestamp(catalog.get("fetched_at")) != observed):
            raise _error("catalog version/observation time does not match reviewed evidence")
        _validate_models(manifest["models"], _models_from_catalog(catalog))
        return manifest
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("MODEL_CAPABILITY:"):
            raise
        raise _error("local capability evidence cannot be verified") from exc
