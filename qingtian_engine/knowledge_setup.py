"""Explicit local Knowledge Hub wiring; never ingest or query during setup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile

from .config import default_data_dir
from .knowledge import KnowledgeProviderError, knowledge_config_path


def _read(path: Path) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"schema_version": 1, "enabled": False}
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            raise ValueError("configuration must be a bounded regular JSON file")
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("invalid knowledge configuration")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qingtian knowledge")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    sub = parser.add_subparsers(dest="action", required=True)
    configure = sub.add_parser("configure", help="Connect your own initialized Knowledge Hub (no ingestion)")
    configure.add_argument("--root", required=True, type=Path)
    sub.add_parser("status", help="Read configuration only; does not query or authenticate")
    sub.add_parser("disable", help="Disable retrieval; retain the Knowledge Hub and its data")
    args = parser.parse_args(argv)
    try:
        path = knowledge_config_path(data_dir=args.data_dir.expanduser().resolve())
        if args.action == "status":
            value = _read(path)
            # No knowledge content, credentials or query results are read here.
            print(json.dumps({"config": str(path), "configured": path.is_file(),
                              "enabled": value["enabled"], "provider": value.get("provider"),
                              "home": value.get("home"), "queried": False}, indent=2))
            return 0
        value = {"schema_version": 1, "enabled": False}
        if args.action == "configure":
            home = args.root.expanduser().resolve()
            if not home.is_dir() or not (home / ".qingtian-knowledge-root").is_file():
                parser.error("--root must be an initialized Knowledge Hub; run qingtian-kb init there first")
            value.update(enabled=True, provider="builtin-module", home=str(home))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() and not path.is_file():
            raise ValueError("configuration target must be a regular file")
        descriptor, temporary = tempfile.mkstemp(prefix=".knowledge-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(json.dumps({"config": str(path), "enabled": value["enabled"],
                          "provider": value.get("provider"), "queried": False}, indent=2))
        return 0
    except (OSError, ValueError, KnowledgeProviderError) as exc:
        parser.exit(2, "Knowledge setup failed: {}\n".format(exc))
