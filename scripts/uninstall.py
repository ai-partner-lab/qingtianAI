#!/usr/bin/env python3
"""Remove a scripts/install.py installation while preserving task data by default."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


MARKER = ".qingtian-install.json"


def default_prefix() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "qingtian" / "runtime"


def default_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def require_install(prefix: Path) -> dict:
    prefix = prefix.expanduser().resolve()
    unsafe = {Path("/"), Path.home().resolve(), (Path.home() / ".local").resolve(),
              (Path.home() / ".local" / "share").resolve()}
    if prefix in unsafe:
        raise RuntimeError(f"Refusing unsafe install prefix: {prefix}")
    marker = prefix / MARKER
    try:
        receipt = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise RuntimeError(f"Not a verified Qingtian installation: {prefix}") from exc
    if (receipt.get("schema_version") != 1 or receipt.get("product") != "qingtian-ai"
            or receipt.get("prefix") != str(prefix)):
        raise RuntimeError(f"Invalid Qingtian installation receipt: {marker}")
    return receipt


def uninstall(prefix: Path, bin_dir: Path, *, data_dir: Path | None = None,
              purge_data: bool = False) -> dict:
    prefix = prefix.expanduser().resolve()
    bin_dir = bin_dir.expanduser().resolve()
    receipt = require_install(prefix)
    if receipt.get("bin_dir") != str(bin_dir):
        raise RuntimeError(
            f"Bin directory does not match the installation receipt: {bin_dir}"
        )
    purge_target = None
    if purge_data:
        if data_dir is None:
            raise RuntimeError("--purge-data requires --data-dir")
        purge_target = data_dir.expanduser().resolve()
        unsafe = {Path("/"), Path.home().resolve(), (Path.home() / ".local").resolve(),
                  (Path.home() / ".local" / "share").resolve()}
        if purge_target in unsafe or not (purge_target / "control-plane.sqlite3").is_file():
            raise RuntimeError(f"Refusing to purge unverified data directory: {purge_target}")
    qingtian = prefix / "venv" / "bin" / "qingtian"
    if data_dir is not None and qingtian.is_file():
        subprocess.run(
            [str(qingtian), "--data-dir", str(data_dir.expanduser().resolve()), "stop"],
            check=False,
        )
    removed_links: list[str] = []
    for raw in receipt.get("links", []):
        link = Path(raw)
        if link.parent.resolve() != bin_dir or not link.is_symlink():
            continue
        try:
            destination = link.resolve(strict=False)
        except OSError:
            continue
        if destination.is_relative_to(prefix):
            link.unlink()
            removed_links.append(str(link))
    shutil.rmtree(prefix)
    removed_data = None
    if purge_target is not None:
        shutil.rmtree(purge_target)
        removed_data = str(purge_target)
    return {"removed_prefix": str(prefix), "removed_links": removed_links,
            "removed_data": removed_data, "data_preserved": not purge_data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=default_prefix())
    parser.add_argument("--bin-dir", type=Path, default=default_bin_dir())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--purge-data", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if args.purge_data and not args.yes:
        parser.error("--purge-data requires --yes")
    print(json.dumps(uninstall(
        args.prefix, args.bin_dir, data_dir=args.data_dir, purge_data=args.purge_data
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
