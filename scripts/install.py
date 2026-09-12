#!/usr/bin/env python3
"""Install Qingtian into an isolated user-owned virtual environment.

The installer never imports an existing task database. ``--start`` is explicit,
and creating/reusing the Codex manager entry requires ``--manager-entry``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("qingtian", "qingtian-kb", "qingtian-lab")
MARKER = ".qingtian-install.json"


def default_prefix() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "qingtian" / "runtime"


def default_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def install(prefix: Path, bin_dir: Path, *, run_selftest: bool = True) -> dict:
    prefix = prefix.expanduser().resolve()
    bin_dir = bin_dir.expanduser().resolve()
    venv = prefix / "venv"
    prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
    run([sys.executable, "-m", "venv", str(venv)])
    python = venv / "bin" / "python"
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "setuptools>=78.1.1"])
    run([str(python), "-m", "pip", "install", str(ROOT)])
    if run_selftest:
        run([str(venv / "bin" / "qingtian"), "selftest"])

    bin_dir.mkdir(parents=True, exist_ok=True)
    # Check every public command before creating the first link.  A conflict on
    # a later name must not leave a partial installation on PATH.
    for name in COMMANDS:
        source = venv / "bin" / name
        target = bin_dir / name
        if target.is_symlink() and target.resolve() == source.resolve():
            continue
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"Refusing to replace existing command: {target}")
    links: list[str] = []
    for name in COMMANDS:
        source = venv / "bin" / name
        target = bin_dir / name
        if target.is_symlink() and target.resolve() == source.resolve():
            links.append(str(target))
            continue
        target.symlink_to(source)
        links.append(str(target))
    version = subprocess.run(
        [str(venv / "bin" / "qingtian"), "--version"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    receipt = {
        "schema_version": 1,
        "product": "qingtian-ai",
        "version": version,
        "prefix": str(prefix),
        "bin_dir": str(bin_dir),
        "links": links,
    }
    (prefix / MARKER).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(prefix / MARKER, 0o600)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=default_prefix())
    parser.add_argument("--bin-dir", type=Path, default=default_bin_dir())
    parser.add_argument("--skip-selftest", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--manager-entry", action="store_true")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    if args.manager_entry and not args.start:
        parser.error("--manager-entry requires --start")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    receipt = install(args.prefix, args.bin_dir, run_selftest=not args.skip_selftest)
    print(json.dumps({"installed": receipt}, ensure_ascii=False, indent=2))
    if args.start:
        executable = args.prefix.expanduser().resolve() / "venv" / "bin" / "qingtian"
        data_dir = (args.data_dir or Path(os.environ.get(
            "QINGTIAN_ENGINE_HOME", Path.home() / ".local" / "share" / "qingtian" / "engine"
        ))).expanduser().resolve()
        command = [
            str(executable), "quickstart", "--data-dir", str(data_dir),
            "--workspace", str(args.workspace.expanduser().resolve()),
            "--port", str(args.port),
        ]
        if args.manager_entry:
            command.append("--manager-entry")
        if args.open:
            command.append("--open")
        run(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
