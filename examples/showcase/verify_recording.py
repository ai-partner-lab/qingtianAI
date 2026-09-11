"""Read-only post-recording audit; does not start a server or change task rows."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def expected_route(alias, final):
    n = int(alias[2:])
    base = ["INBOX", "PLANNED", "QUEUED", "RUNNING"]
    if n in {7, 8, 29, 30}:
        return ["INBOX"]
    if n in {31, 32}:
        return ["INBOX", "CANCELED"]
    if n == 9:
        return ["INBOX", "PLANNED", "WAITING", "PLANNED", "QUEUED", "RUNNING", "WAITING", "RUNNING", "VERIFYING", "DONE"]
    if n in {10, 11, 12, 14, 15, 19, 20, 21, 23}:
        return base + ["WAITING"]
    if n in {18, 22}:
        return base + ["WAITING", "RUNNING"]
    if n in {13, 17}:
        return base + ["WAITING", "RUNNING", "VERIFYING", "DONE"]
    if n == 16:
        return base + ["WAITING", "RUNNING", "VERIFYING"]
    if n in {24, 27}:
        return base + ["VERIFYING", "RUNNING", "VERIFYING"] + (["DONE"] if n == 24 else [])
    return base + ["VERIFYING"] + (["DONE"] if final == "DONE" else [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    folder = args.recording.resolve()
    load = lambda name: json.loads((folder / name).read_text())
    verification = load("verification.json")
    recording = load("recording.json")
    scenario = json.loads(Path(__file__).with_name("scenario.json").read_text())
    # This is the recording's own isolated /tmp DB, opened with SQLite mode=ro.
    data = Path(verification["data_dir"])
    assert str(data).startswith(("/tmp/qingtian-showcase-", "/private/tmp/qingtian-showcase-"))
    db = sqlite3.connect(f"file:{data / 'control-plane.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    events = [dict(e) for e in db.execute("SELECT * FROM events ORDER BY id")]
    evidence = [dict(e) for e in db.execute("SELECT * FROM evidence ORDER BY id")]
    assert not list(db.execute("SELECT id FROM runs"))
    assert verification["model_calls"] == 0
    assert recording["sse_count"] > 0
    assert not recording["browser_errors"] and not recording["external_requests_blocked"]
    routes = {}
    for spec in scenario["tasks"]:
        alias = spec["id"]
        task_id = verification["tasks"][alias]
        own = [e for e in events if e["task_id"] == task_id]
        actual = [json.loads(e["payload_json"])["state"] for e in own
                  if e["event_type"] in {"task.created", "task.state_changed"}]
        expected = expected_route(alias, spec["final"])
        assert actual == expected, (alias, expected, actual)
        positive = [e for e in evidence if e["task_id"] == task_id and e["kind"] == "artifact" and e["verified"]]
        if spec["final"] == "DONE":
            assert positive
            done_event = next(e for e in own if e["event_type"] == "task.state_changed" and json.loads(e["payload_json"])["state"] == "DONE")
            matching_commands = [c for c in recording["commands"] if c["request"]["op"] == "fixture"
                                 and alias in c["request"]["tasks"] and c["request"].get("valid", True)]
            assert matching_commands
            assert matching_commands[-1]["entry"]["cursor"] < done_event["id"]
            for e in positive:
                assert e["created_at"] <= done_event["occurred_at"]
        routes[alias] = {"title": spec["title"], "expected_raw": expected, "actual_raw": actual,
                         "final_display": spec["final"], "passed": True}
    for fixture in verification["fixtures"]:
        file = folder / "fixtures" / Path(fixture["path"]).name
        assert hashlib.sha256(file.read_bytes()).hexdigest() == fixture["sha256"]
        value = json.loads(file.read_text())
        passed = value["entries"] == list(range(10)) and value["synthetic"] and not value["business_acceptance"]
        assert passed == fixture["passed"]
    assert sum(verification["final_counts"].values()) == 32
    assert all(verification["final_counts"].values())
    assert len(verification["gate_assertions"]) == 3 and all(a["gate_rejected"] for a in verification["gate_assertions"])
    (folder / "service-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
    (folder / "state-trajectories.json").write_text(json.dumps(routes, ensure_ascii=False, indent=2))
    report = {"passed": True, "tasks": 32, "display_states": 8, "service_events": len(events),
              "verified_completions": verification["final_counts"]["DONE"], "gate_rejections": 3,
              "managed_worker_runs": 0, "model_calls": 0, "business_acceptance": False,
              "sse_messages": recording["sse_count"], "whole_trajectories_checked": True,
              "evidence_before_completion_checked": True, "fixture_sha256_checked": True}
    (folder / "post-recording-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
