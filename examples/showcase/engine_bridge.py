"""Private rehearsal service bridge. Uses public engine methods, never SQL writes.

All tasks and signals are synthetic. Fixture evidence proves only that our tiny
local JSON contract passed. It does NOT prove model execution or business QA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_source_commit(root=ROOT):
    """Archives have no Git identity; never borrow an enclosing checkout's HEAD."""
    root = Path(root).resolve()
    if not (root / ".git").exists():
        return None
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "HEAD"], cwd=root,
            env=environment, capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.strip().splitlines()
    if len(lines) != 2 or Path(lines[0]).resolve() != root:
        return None
    commit = lines[1]
    if len(commit) not in (40, 64) or any(c not in "0123456789abcdef" for c in commit):
        return None
    return commit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Read-only provenance must work from both checkouts and unpacked archives.
    # The per-file digests, not a possibly dirty checkout's HEAD, identify inputs.
    source = {str(p.relative_to(ROOT)): sha(p) for p in sorted((ROOT / "qingtian_engine").rglob("*"))
              if p.is_file() and p.suffix in {".py", ".js", ".css", ".html", ".json"}}
    source_commit = read_source_commit()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # A fresh disposable identity on every run; never point at an existing DB.
    data = Path(tempfile.mkdtemp(prefix="qingtian-showcase-", dir="/tmp"))
    os.environ.update(QINGTIAN_ENGINE_HOME=str(data), QINGTIAN_WORKSPACE=str(data),
                      QINGTIAN_RECOVERY_ENABLED="0", QINGTIAN_INTAKE_PLANNER="")
    from qingtian_engine.db import Database
    from qingtian_engine.service import ControlPlane
    service = ControlPlane(Database(data / "control-plane.sqlite3"), manager_entry_workspace=data)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port in {8765, 8766}:
        raise RuntimeError("reserved port")
    server_log = (output / "server.log").open("w")
    server = subprocess.Popen([sys.executable, "-m", "qingtian_engine.server", "--host", "127.0.0.1",
                               "--port", str(port), "--data-dir", str(data), "--workspace", str(data),
                               "--mode", "manual"], cwd=ROOT, env=os.environ.copy(),
                              stdout=server_log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    start = time.monotonic()
    aliases = {}
    history = []
    assertions = []
    fixture_records = []
    scenario = json.loads((Path(__file__).with_name("scenario.json")).read_text())

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return json.load(response)

    for _ in range(100):
        try:
            health = get("/api/health")
            break
        except OSError:
            if server.poll() is not None:
                raise RuntimeError("showcase server exited")
            time.sleep(.1)
    else:
        raise RuntimeError("showcase server unavailable")

    def emit(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    def task_id(alias):
        return aliases[alias]

    def transition(alias, state, summary, blocking=""):
        return service.transition(task_id(alias), state, producer="showcase-local-fixture",
                                  summary=summary, blocking_reason=blocking)

    def action(alias, kind, owner, text):
        return service.set_human_action(task_id(alias), kind, owner, text,
                                         producer="showcase-local-fixture")

    def fixture(alias, valid):
        # Actual filesystem generation and a deliberately tiny local validator.
        artifact_dir = data / "fixture-artifacts"
        artifact_dir.mkdir(exist_ok=True)
        version = sum(r["alias"] == alias for r in fixture_records) + 1
        path = artifact_dir / f"{alias}-v{version}.json"
        payload = {"synthetic": True, "task": alias, "entries": list(range(10 if valid else 8)),
                   "producer": "deterministic-local-fixture", "business_acceptance": False}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        readback = json.loads(path.read_text())
        passed = (readback["synthetic"] is True and readback["task"] == alias
                  and readback["entries"] == list(range(10)) and readback["business_acceptance"] is False)
        assert passed == valid
        digest = sha(path)
        record = {"alias": alias, "path": str(path), "sha256": digest, "passed": passed,
                  "contract": "10 deterministic entries; synthetic=true; business_acceptance=false"}
        fixture_records.append(record)
        service.add_evidence(task_id(alias), "artifact", f"{path.name} · sha256:{digest}",
                             label="本地合成 JSON 校验通过" if passed else "本地合成 JSON 校验失败：8/10 项",
                             verified=passed)
        record["evidence_event_cursor"] = service.event_cursor()
        # Preserve evidence content outside the disposable DB as a recording artifact.
        (output / "fixtures").mkdir(exist_ok=True)
        (output / "fixtures" / path.name).write_bytes(path.read_bytes())
        return record

    def snapshot(command, result=None):
        dashboard = get("/api/dashboard")
        assert len({t["id"] for t in dashboard["tasks"]}) == len(aliases)
        for task in dashboard["tasks"]:
            raw = get("/api/tasks/" + task["id"])
            assert raw["state"] == task["stored_state"], (raw["state"], task["stored_state"])
            assert not task["state_resolution"]["syncing"], task
        entry = {"elapsed": round(time.monotonic() - start, 3), "command": command,
                 "result": result, "cursor": service.event_cursor(),
                 "states": {a: {"raw": next(t for t in dashboard["tasks"] if t["id"] == i)["state"],
                                  "display": next(t for t in dashboard["tasks"] if t["id"] == i)["display_state"]}
                            for a, i in aliases.items()},
                 "counts": {s: len(ts) for s, ts in dashboard["columns"].items()}}
        history.append(entry)
        with (output / "timeline.jsonl").open("a") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    emit({"ready": True, "base": base, "data_dir": str(data), "server_pid": server.pid,
          "source_commit": source_commit})
    try:
        for line in sys.stdin:
            request = json.loads(line)
            command = request["op"]
            targets = request.get("tasks", [])
            result = None
            try:
                if command == "create":
                    for alias in targets:
                        spec = next(t for t in scenario["tasks"] if t["id"] == alias)
                        task = service.create_task(
                            f"{alias} · {spec['title']}", idempotency_key=f"showcase:{alias}",
                            scope_summary="合成演示需求；由本地 fixture 生成 10 项清单并核对 JSON。无模型、无外网、非生产执行；不构成业务验收。",
                            worker_type="local-fixture", owner_session="本地校验器（合成演示）",
                            reasoning="xhigh", environment="isolated-local-showcase", evidence_profile="artifact",
                            authorization_policy="analysis-only" if alias in {"SC29", "SC30"} else "normal")
                        aliases[alias] = task["id"]
                elif command == "plan":
                    for alias in targets:
                        transition(alias, "PLANNED", "需求已整理，进入计划；尚未执行")
                elif command == "start":
                    for alias in targets:
                        state = service.get_task(task_id(alias))["state"]
                        if state == "INBOX":
                            transition(alias, "PLANNED", "演示编排：明确进入计划")
                            state = "PLANNED"
                        if state in {"PLANNED", "FAILED"}:
                            transition(alias, "QUEUED", "本地 fixture 排队；不调用模型")
                        if service.get_task(task_id(alias))["state"] != "RUNNING":
                            transition(alias, "RUNNING", "本地 fixture 开始处理合成清单")
                        service.heartbeat_task(task_id(alias), producer="showcase-local-fixture", execution_mode="external")
                        action(alias, "agent", "本地校验器（演示）", "本地 fixture 正在处理 · 无模型调用")
                elif command == "wait":
                    for alias in targets:
                        kind = request["kind"]
                        settings = {
                            "external": ("external", "资料提供方（模拟）", "请补齐样例字段说明；未收到则保持等待", "等待外部合成资料"),
                            "user": ("user", "需求发起人（模拟）", "请选择样例方案 A 或 B；只通过大管家确认", "等待用户决策信号（合成）"),
                            "recovery": ("agent", "本地校验器（演示）", "执行中断模拟：保持断点，收到明确授权才续接", "执行中断：仅模拟额度/环境停止信号，未消费真实额度"),
                            "pause": ("agent", "需求发起人（模拟）", "用户明确暂停；只有收到明确恢复指令才继续", "PAUSED_BY_USER: 用户明确暂停（合成信号）"),
                        }
                        owner_kind, owner, text, blocking = settings[kind]
                        action(alias, owner_kind, owner, text)
                        transition(alias, "WAITING", text, blocking)
                elif command == "dependency":
                    for alias in targets:
                        service.add_dependency(task_id(alias), task_id(request["upstream"]))
                        transition(alias, "WAITING", "前置清单完成后再处理，当前不盲跑", "依赖：" + request["upstream"] + " 的清单验收")
                elif command == "authorize":
                    for alias in targets:
                        service.record_authorization(task_id(alias), "resume-local-fixture", "normal", "allow",
                                                     "合成明确恢复信号；仅授权本地 JSON 校验，无真实额度恢复或业务部署")
                        action(alias, "none", "", "")
                elif command == "verify":
                    for alias in targets:
                        action(alias, "agent", "本地校验器（演示）", "验收门等待 verified artifact；业务验收不在本片范围")
                        transition(alias, "VERIFYING", "本地 fixture 处理结束，进入证据校验")
                elif command == "gate_reject":
                    result = []
                    for alias in targets:
                        try:
                            transition(alias, "DONE", "此调用必须被真实证据门拒绝")
                        except ValueError as error:
                            assert "missing evidence" in str(error)
                            assertions.append({"alias": alias, "gate_rejected": True, "error": str(error)})
                            result.append(assertions[-1])
                        else:
                            raise AssertionError("missing evidence incorrectly passed DONE gate")
                elif command == "fixture":
                    result = [fixture(alias, request.get("valid", True)) for alias in targets]
                elif command == "repair":
                    for alias in targets:
                        transition(alias, "RUNNING", "校验失败：8/10 项；退回本地 fixture 补齐后重新送验")
                        service.heartbeat_task(task_id(alias), producer="showcase-local-fixture", execution_mode="external")
                        action(alias, "agent", "本地校验器（演示）", "返修：补齐 2 项缺失清单，再提交校验")
                elif command == "reconcile":
                    result = service.reconcile_state_progression()
                elif command == "cancel":
                    for alias in targets:
                        transition(alias, "CANCELED", "合成用户决定取消重复/过期需求；保留历史，不再调度")
                elif command == "snapshot":
                    pass
                elif command == "finish":
                    final = get("/api/dashboard")
                    for spec in scenario["tasks"]:
                        actual = next(t for t in final["tasks"] if t["id"] == task_id(spec["id"]))
                        assert actual["display_state"] == spec["final"], (spec, actual["display_state"])
                    assert len(final["tasks"]) == 32
                    assert all(final["columns"][s] for s in ("INBOX", "RUNNING", "WAITING", "PAUSED", "PLAN_ONLY", "VERIFYING", "DONE", "CANCELED"))
                    assert not service.db.all("SELECT id FROM runs"), "real worker run was unexpectedly created"
                    assert all(not t["imported_from"] for t in final["tasks"])
                    details = {a: get("/api/tasks/" + i) for a, i in aliases.items()}
                    # Check the entire canonical raw-state trajectory, not just
                    # the final screenshot. Display-only categories stay honest.
                    base_route = ["INBOX", "PLANNED", "QUEUED", "RUNNING"]
                    routes = {}
                    for spec in scenario["tasks"]:
                        a = spec["id"]
                        n = int(a[2:])
                        if n in {7, 8, 29, 30}:
                            expected = ["INBOX"]
                        elif n in {31, 32}:
                            expected = ["INBOX", "CANCELED"]
                        elif n == 9:
                            expected = ["INBOX", "PLANNED", "WAITING", "PLANNED", "QUEUED", "RUNNING", "WAITING", "RUNNING", "VERIFYING", "DONE"]
                        elif n in {10, 11, 12, 14, 15, 19, 20, 21, 23}:
                            expected = base_route + ["WAITING"]
                        elif n in {18, 22}:
                            expected = base_route + ["WAITING", "RUNNING"]
                        elif n in {13, 17}:
                            expected = base_route + ["WAITING", "RUNNING", "VERIFYING", "DONE"]
                        elif n == 16:
                            expected = base_route + ["WAITING", "RUNNING", "VERIFYING"]
                        elif n in {24, 27}:
                            expected = base_route + ["VERIFYING", "RUNNING", "VERIFYING"] + (["DONE"] if n == 24 else [])
                        else:
                            expected = base_route + ["VERIFYING"] + (["DONE"] if spec["final"] == "DONE" else [])
                        events = service.db.all("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id(a),))
                        actual = [json.loads(e["payload_json"])["state"] for e in events
                                  if e["event_type"] in {"task.created", "task.state_changed"}]
                        assert actual == expected, (a, expected, actual)
                        for event in events:
                            if event["event_type"] == "task.state_changed" and json.loads(event["payload_json"])["state"] == "DONE":
                                assert any(f["alias"] == a and f["passed"] and f["evidence_event_cursor"] < event["id"] for f in fixture_records)
                        routes[a] = {"expected_raw": expected, "actual_raw": actual, "passed": True}
                    (output / "state-trajectories.json").write_text(json.dumps(routes, ensure_ascii=False, indent=2))
                    all_events = service.db.all("SELECT * FROM events ORDER BY id")
                    (output / "service-events.json").write_text(json.dumps(all_events, ensure_ascii=False, indent=2))
                    (output / "final-dashboard.json").write_text(json.dumps(final, ensure_ascii=False, indent=2))
                    (output / "task-details.json").write_text(json.dumps(details, ensure_ascii=False, indent=2))
                    audit = service.db.all("SELECT * FROM authorization_audit")
                    source_after = {p: sha(ROOT / p) for p in source}
                    manifest = {"disclosure": scenario["disclosure"], "business_acceptance": False,
                                "source_commit": source_commit, "source_sha256": source,
                                "source_unchanged_during_recording": source_after == source,
                                "source_changes_during_recording": [p for p in source if source[p] != source_after[p]],
                                "server_pid": server.pid, "port": port, "data_dir": str(data), "engine_mode": "manual",
                                "model_calls": 0, "managed_worker_runs": 0, "production_tasks": 0,
                                "tasks": aliases, "final_counts": {s: len(v) for s, v in final["columns"].items()},
                                "state_trajectories_checked": len(routes), "completion_causality_checked": True,
                                "gate_assertions": assertions, "fixtures": fixture_records,
                                "authorization_audit": audit, "event_count": len(all_events), "event_cursor": service.event_cursor(),
                                "elapsed_seconds": round(time.monotonic() - start, 3)}
                    (output / "verification.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
                    result = manifest
                else:
                    raise ValueError("unknown operation: " + command)
                entry = snapshot(request, result)
                emit({"ok": True, "entry": entry, "aliases": aliases})
                if command == "finish":
                    break
            except Exception as error:
                emit({"ok": False, "error": str(error), "type": type(error).__name__, "request": request})
    finally:
        # Stop ONLY the process started above. Keep its temporary DB for audit.
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server_log.close()


if __name__ == "__main__":
    main()
