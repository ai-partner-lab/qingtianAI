"""Explicit local evidence preparation. Never activates an execution provider."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from . import codex_capabilities as cc
from .config import EXECUTION_EFFORTS


def prepare_draft(output: Path):
    output = output.expanduser().absolute()
    active = Path(os.environ.get("QINGTIAN_CODEX_CAPABILITIES", cc.default_data_dir() / "config/codex-capabilities.json"))
    if output.resolve() == active.expanduser().resolve():
        raise ValueError("Choose a separate draft path; preparation must not activate evidence")
    if output.exists() or output.is_symlink():
        raise ValueError("Draft output already exists; no overwrite is permitted")
    # Only explicit prepare reads identity/catalog. No authentication, sessions,
    # app-server, remote request, exec or automatic cache refresh is performed.
    home = cc._codex_home()
    cli, version, binary = cc._cli_identity()
    source = home / "models_cache.json"
    if source.is_symlink():
        raise ValueError("The current fixed model catalog must not be a symlink")
    raw = source.read_bytes()
    catalog = cc._json(raw)
    if not isinstance(catalog, dict):
        raise ValueError("The local catalog must be a JSON object")
    observed = cc._timestamp(catalog.get("fetched_at"))
    expires = observed + cc.MAX_EVIDENCE_AGE
    if catalog.get("client_version") != version or not observed <= cc._utcnow() < expires:
        raise ValueError("Current catalog is stale or belongs to another CLI; refresh it explicitly in Codex first")
    advertised = cc._models_from_catalog(catalog)
    models = {name: {"reasoning": [level for level in value["reasoning"] if level in EXECUTION_EFFORTS],
                     "speed": value["speed"]} for name, value in advertised.items()}
    cc._validate_models(models, advertised)
    draft = {"schema_version": 2, "adapter": "codex-cli", "host_identity": cc._host_identity(),
             "uid": os.geteuid(), "codex_home": str(home), "cli_path": cli, "cli_version": version,
             "binary_sha256": binary, "source_kind": "codex-local-model-cache-v1", "source_ref": str(source),
             "source_sha256": hashlib.sha256(raw).hexdigest(), "observed_at": observed.isoformat(),
             "expires_at": expires.isoformat(), "models": models}
    # Owner-only and exclusive. Parent directory must already be chosen by the
    # operator; no directories, environment or active configuration are changed.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(draft, stream, indent=2)
        stream.write("\n")
    return {"status": "draft_only", "enabled": False, "models": sorted(models),
            "expires_at": draft["expires_at"],
            "next_action": "Review the private draft and local catalog manually; only then select its private path with QINGTIAN_CODEX_CAPABILITIES. Never commit or share it."}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="qingtian capabilities", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read-only validation of explicitly reviewed local evidence")
    prepare = sub.add_parser("prepare", help="Write a private, inactive draft for manual review; no credentials/model calls")
    prepare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_draft(args.output)
        else:
            manifest = cc.load_codex_capabilities()
            result = {"status": "reviewed_local_advertisement", "models": manifest["models"],
                      "expires_at": manifest["expires_at"], "account_entitlement_verified": False,
                      "served_model_or_tier_verified": False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "unavailable", "error": str(exc),
                          "next_action": "Read docs/CODEX-CAPABILITIES.md; explicitly prepare and review fresh local evidence. No model fallback or automatic refresh."}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
