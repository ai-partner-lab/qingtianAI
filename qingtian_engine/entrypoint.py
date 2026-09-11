"""Default product entry point: the real engine, not the legacy lab demo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import webbrowser


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "legacy":
        from qingtian_core.cli import main as legacy_main
        print("Legacy laboratory: separate schemas and synthetic demos, not the default engine.", file=sys.stderr)
        return legacy_main(args[1:])
    if args == ["--version"]:
        from . import __version__
        print(__version__)
        return 0
    if args and args[0] in {"bundle", "bundle-verify", "scan"}:
        from qingtian_core.cli import main as tools_main
        return tools_main(args)
    commands = {"quickstart", "tour", "selftest", "knowledge", "manager-entry", "capabilities"}
    # Skip only known global options. A task title or prompt value must never
    # accidentally become a top-level command.
    cursor = 0
    while cursor < len(args) and args[cursor] in {"--data-dir", "--workspace"}:
        cursor += 2
    index = cursor if cursor < len(args) and args[cursor] in commands else None
    if index is None:
        from .cli import main as engine_main
        if not args or args == ["--help"] or args == ["-h"]:
            print("Qingtian AI — real local task engine\n"
                  "Start empty: qingtian quickstart --open\n"
                  "Guided synthetic tour on the same engine: qingtian tour --open\n"
                  "Credential-free engine checks: qingtian selftest\n"
                  "Connect your own Knowledge Hub: qingtian knowledge --help\n"
                  "Read-only manager setup guide: qingtian manager-entry guide\n"
                  "Manage the Codex manager entry: qingtian manager-entry inspect / init / sync / status\n"
                  "Source bundle: qingtian bundle / bundle-verify\n"
                  "Old 0.4 laboratory only: qingtian legacy ... / qingtian-lab ...\n")
            return engine_main(["--help"])
        return engine_main(args)
    command = args[index]
    if command == "capabilities":
        from .capability_setup import main as capability_main
        return capability_main(args[:index] + args[index + 1:])
    if command == "manager-entry":
        from .manager_entry import main as manager_main
        return manager_main(args[:index] + args[index + 1:])
    if command == "knowledge":
        from .knowledge_setup import main as knowledge_main
        return knowledge_main(args[:index] + args[index + 1:])
    parser = argparse.ArgumentParser(prog="qingtian " + command)
    if command == "selftest":
        parser.add_argument("--output", type=Path)
        options = parser.parse_args(args[:index] + args[index + 1:])
        from .selftest import run_selftest
        result = run_selftest()
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if options.output:
            options.output.parent.mkdir(parents=True, exist_ok=True)
            options.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if result["status"] == "passed" else 1
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true")
    if command == "quickstart":
        from .config import default_data_dir, default_workspace
        parser.add_argument("--data-dir", type=Path, default=default_data_dir())
        parser.add_argument("--workspace", type=Path, default=default_workspace())
        parser.add_argument("--skip-manager-entry", action="store_true",
                            help="Start only the dashboard; do not initialize a Codex manager thread")
        parser.add_argument("--manager-entry", action="store_true",
                            help="Explicitly initialize the native Codex manager thread (optional integration)")
        options = parser.parse_args(args[:index] + args[index + 1:])
        from .cli import build_service, start_server
        data = options.data_dir.expanduser().resolve()
        if not 1 <= options.port <= 65535:
            parser.error("port must be between 1 and 65535")
        build_service(data)
        if options.manager_entry and options.skip_manager_entry:
            parser.error("--manager-entry and --skip-manager-entry conflict")
        if options.manager_entry:
            from .manager_entry import initialize_from_environment
            entry = initialize_from_environment(data, options.workspace)
            print(json.dumps({"manager_entry": entry}, ensure_ascii=False), flush=True)
        return start_server(data, options.port, False, options.open,
                            mode="manual", workspace=options.workspace.expanduser().resolve())
    options = parser.parse_args(args[:index] + args[index + 1:])
    if not 1 <= options.port <= 65535:
        parser.error("tour port must be between 1 and 65535")
    from .cli import build_service, create_demo
    from .server import serve
    # The tour shares engine code but never the user's real data directory.
    with tempfile.TemporaryDirectory(prefix="qingtian-tour-") as temporary:
        data = Path(temporary)
        service = build_service(data)
        create_demo(service, data, reset=False)
        url = "http://127.0.0.1:{}".format(options.port)
        print("Qingtian engine tour (read-only synthetic fixtures, no model calls): " + url, flush=True)
        print("Press Ctrl+C to stop. This temporary tour does not touch your active task database.", flush=True)
        if options.open:
            # Wait until this tour actually owns the listener before opening it.
            from threading import Thread
            from .cli import _health_payload, _matches_instance
            from time import sleep
            def open_when_ready():
                for _ in range(50):
                    health = _health_payload(options.port)
                    if health and _matches_instance(health, data.resolve(), data.resolve(), "manual"):
                        webbrowser.open(url)
                        return
                    sleep(0.1)
            Thread(target=open_when_ready, daemon=True).start()
        try:
            serve("127.0.0.1", options.port, data, mode="manual", workspace=data,
                  synthetic_tour=True)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
