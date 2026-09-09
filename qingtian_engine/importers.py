from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from .db import Database, utc_now
from .redaction import fingerprint, redact_text
from .service import ControlPlane


TASK_RE = re.compile(r"^- \[([ xX])\]\s+(.+)$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
GOVERNANCE_ROW_RE = re.compile(
    r"^\|\s*([a-z][a-z0-9._-]{0,63})\s*\|\s*`([^`]+)`\s*\|"
    r"\s*`([^`]+)`\s*\|\s*(manager|cli|qa|browser|infra|security)\s*\|"
    r"\s*([^|]+)\|"
)


def import_tasks_markdown(
    service: ControlPlane, path: Path, force: bool = False
) -> Dict[str, int]:
    content = path.read_text(encoding="utf-8")
    source_fingerprint = fingerprint("tasks-v1|" + content)
    existing = service.db.one(
        "SELECT * FROM imports WHERE fingerprint=?", (source_fingerprint,)
    )
    if existing and not force:
        return {"imported": 0, "skipped": int(existing["imported_count"])}

    section = ""
    current: Optional[Dict[str, str]] = None
    tasks: List[Dict[str, str]] = []
    for raw_line in content.splitlines():
        section_match = SECTION_RE.match(raw_line)
        if section_match:
            if current:
                tasks.append(current)
                current = None
            section = section_match.group(1).strip()
            continue
        task_match = TASK_RE.match(raw_line)
        if task_match:
            if current:
                tasks.append(current)
            raw_task = task_match.group(2).strip()
            title = raw_task
            note = ""
            formatted = re.match(
                r"^(?:\*\*(.+?)\*\*|~~(.+?)~~)(.*)$", raw_task
            )
            if formatted:
                title = (formatted.group(1) or formatted.group(2) or "").strip()
                note = (formatted.group(3) or "").strip()
                note = re.sub(r"^\s*-\s*", "", note)
            current = {
                "checked": task_match.group(1),
                "title": title.strip("~ "),
                "note": note,
                "section": section,
                "details": "",
            }
            continue
        if current and re.match(r"^\s{2,}-\s+", raw_line):
            detail = re.sub(r"^\s{2,}-\s+", "", raw_line)
            current["details"] += ("; " if current["details"] else "") + detail
    if current:
        tasks.append(current)

    imported = 0
    skipped = 0
    for item in tasks:
        checked = item["checked"].lower() == "x"
        state = "DONE" if checked else {
            "Waiting On": "WAITING",
            "Waiting": "WAITING",
        }.get(item["section"], "INBOX")
        priority_match = re.match(r"P([0-3])[:：]", item["title"], re.I)
        priority = int(priority_match.group(1)) if priority_match else 2
        scope = redact_text(
            " ".join(filter(None, (item["note"], item["details"]))), max_chars=500
        )
        key = fingerprint(
            "{}|{}|{}".format(source_fingerprint, item["section"], item["title"])
        )
        before = service.db.one("SELECT id FROM tasks WHERE idempotency_key=?", (key,))
        task = service.create_task(
            title=item["title"],
            idempotency_key=key,
            scope_summary=scope,
            priority=priority,
            imported_from="markdown:{}".format(path.name),
            authorization_policy="reference-only",
            state=state,
            progress=100 if state == "DONE" else None,
            blocking_reason=item["note"] if state == "WAITING" else "",
        )
        if before:
            skipped += 1
        else:
            imported += 1
            if state == "DONE":
                service.add_evidence(
                    task["id"],
                    "artifact",
                    "Imported ledger historical completion",
                    verified=False,
                )
    service.db.execute(
        """
        INSERT OR REPLACE INTO imports(fingerprint, source, imported_at, imported_count)
        VALUES(?, ?, ?, ?)
        """,
        (
            source_fingerprint,
            "markdown:{}".format(path.name),
            utc_now(),
            imported + skipped,
        ),
    )
    return {"imported": imported, "skipped": skipped}


def import_governance(service: ControlPlane, path: Path) -> Dict[str, int]:
    content = path.read_text(encoding="utf-8")
    source_fingerprint = fingerprint("governance-v2|" + content)
    existing = service.db.one(
        "SELECT * FROM imports WHERE fingerprint=?", (source_fingerprint,)
    )
    if existing:
        return {"imported": 0, "skipped": int(existing["imported_count"])}
    imported = 0
    for line in content.splitlines():
        match = GOVERNANCE_ROW_RE.match(line)
        if not match:
            continue
        role, thread_id, name, worker_type, scope = match.groups()
        service.upsert_session(
            thread_id,
            role,
            name,
            worker_type,
            scope.strip(),
            "markdown:{}".format(path.name),
        )
        imported += 1
    service.db.execute(
        """
        INSERT OR REPLACE INTO imports(fingerprint, source, imported_at, imported_count)
        VALUES(?, ?, ?, ?)
        """,
        (
            source_fingerprint,
            "markdown:{}".format(path.name),
            utc_now(),
            imported,
        ),
    )
    return {"imported": imported, "skipped": 0}


def import_defaults(service: ControlPlane, workspace: Path) -> Dict[str, Dict[str, int]]:
    task_path = workspace / "TASKS.md"
    governance_path = workspace / "GOVERNANCE.md"
    results: Dict[str, Dict[str, int]] = {}
    if task_path.exists():
        results["tasks"] = import_tasks_markdown(service, task_path)
    if governance_path.exists():
        results["governance"] = import_governance(service, governance_path)
    return results
