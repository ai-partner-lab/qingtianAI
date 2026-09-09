from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .db import Database, utc_now
from .redaction import redact_text
from .runner import RunManager
from .service import ControlPlane


def _utc(value: Optional[str] = None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


class RecoveryCoordinator:
    """Durable, fenced coordinator layered over the 1.x scheduler.

    The coordinator never performs a provider-side action itself. It serializes
    reconciliation, synchronizes evidence contracts and records exhausted
    failures so restart recovery remains safe and inspectable.
    """

    LEASE_KEY = "qingtian-2-recovery"

    def __init__(
        self,
        service: ControlPlane,
        manager: RunManager,
        holder_id: Optional[str] = None,
        lease_seconds: int = 45,
    ) -> None:
        self.service = service
        self.manager = manager
        self.db: Database = service.db
        self.holder_id = holder_id or "{}:{}:{}".format(
            socket.gethostname(), os.getpid(), uuid.uuid4().hex[:8]
        )
        self.lease_seconds = max(15, int(lease_seconds))
        self.db.initialize()
        self._register_builtin_plugins()

    def _register_builtin_plugins(self) -> None:
        now = utc_now()
        plugins = (
            ("codex", "executor", 1, "gpt-5.6-sol", "high", ["coding", "review", "verify"], "READY"),
            ("claude-audit", "auditor", 0, "", "high", ["adversarial-review"], "DISABLED"),
            ("grok-audit", "auditor", 0, "", "high", ["adversarial-review"], "DISABLED"),
        )
        with self.db.connect() as connection:
            for name, kind, enabled, model, reasoning, capabilities, health in plugins:
                connection.execute(
                    """
                    INSERT INTO executor_plugins(
                        name, kind, enabled, model, min_reasoning,
                        capabilities_json, config_json, health, updated_at
                    ) VALUES(?,?,?,?,?,?, '{}',?,?)
                    ON CONFLICT(name) DO UPDATE SET
                        kind=excluded.kind,
                        capabilities_json=excluded.capabilities_json,
                        updated_at=excluded.updated_at
                    """,
                    (name, kind, enabled, model, reasoning, json.dumps(capabilities), health, now),
                )

    def acquire_lease(self) -> Optional[int]:
        now = _utc()
        expires = now + timedelta(seconds=self.lease_seconds)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM orchestrator_leases WHERE lease_key=?",
                (self.LEASE_KEY,),
            ).fetchone()
            if row and row["holder_id"] != self.holder_id and _utc(row["expires_at"]) > now:
                return None
            token = int(row["fencing_token"]) + 1 if row and row["holder_id"] != self.holder_id else int(row["fencing_token"] if row else 1)
            connection.execute(
                """
                INSERT INTO orchestrator_leases(
                    lease_key, holder_id, fencing_token, acquired_at,
                    heartbeat_at, expires_at, metadata_json
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(lease_key) DO UPDATE SET
                    holder_id=excluded.holder_id,
                    fencing_token=excluded.fencing_token,
                    acquired_at=excluded.acquired_at,
                    heartbeat_at=excluded.heartbeat_at,
                    expires_at=excluded.expires_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    self.LEASE_KEY,
                    self.holder_id,
                    token,
                    now.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                    expires.isoformat(timespec="seconds"),
                    json.dumps({"pid": os.getpid()}, sort_keys=True),
                ),
            )
            return token

    def sync_evidence_contracts(self) -> Dict[str, int]:
        counts = {"contracts": 0, "satisfied": 0, "missing": 0}
        tasks = self.db.all("SELECT id FROM tasks WHERE state NOT IN ('CANCELED')")
        now = utc_now()
        with self.db.connect() as connection:
            for task in tasks:
                task_id = str(task["id"])
                required = set(self.service.required_evidence(task_id))
                evidence = connection.execute(
                    "SELECT id, kind, verified FROM evidence WHERE task_id=? ORDER BY id DESC",
                    (task_id,),
                ).fetchall()
                by_kind = {}
                for item in evidence:
                    by_kind.setdefault(str(item["kind"]), item)
                for kind in required:
                    item = by_kind.get(kind)
                    status = "SATISFIED" if item and int(item["verified"]) else "MISSING"
                    connection.execute(
                        """
                        INSERT INTO evidence_contracts(
                            task_id, kind, required, status, policy, evidence_id, updated_at
                        ) VALUES(?,?,1,?,'verified-value',?,?)
                        ON CONFLICT(task_id,kind) DO UPDATE SET
                            required=1, status=excluded.status,
                            evidence_id=excluded.evidence_id, updated_at=excluded.updated_at
                        """,
                        (task_id, kind, status, item["id"] if item else None, now),
                    )
                    counts["contracts"] += 1
                    counts["satisfied" if status == "SATISFIED" else "missing"] += 1
                if required:
                    placeholders = ",".join("?" for _ in required)
                    connection.execute(
                        "UPDATE evidence_contracts SET required=0, updated_at=? "
                        "WHERE task_id=? AND kind NOT IN ({})".format(placeholders),
                        (now, task_id, *sorted(required)),
                    )
                else:
                    connection.execute(
                        "UPDATE evidence_contracts SET required=0, updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
        return counts

    def capture_dead_letters(self, max_attempts: int = 3) -> Dict[str, int]:
        created = 0
        exhausted = self.db.all(
            """
            SELECT r.* FROM runs r
            JOIN (SELECT task_id, MAX(attempt) attempt FROM runs GROUP BY task_id) x
              ON x.task_id=r.task_id AND x.attempt=r.attempt
            WHERE r.status='FAILED' AND r.attempt>=?
            """,
            (max(1, int(max_attempts)),),
        )
        for run in exhausted:
            reason = redact_text(
                "{}:{}".format(run.get("failure_kind") or "FAILED", run.get("failure_type") or "unknown")
            )[:240]
            created += int(
                self.db.execute(
                    """
                    INSERT OR IGNORE INTO dead_letters(
                        id, task_id, run_id, category, reason, attempts,
                        status, payload_json, dedupe_key, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,'OPEN','{}',?,?,?)
                    """,
                    (
                        str(uuid.uuid4()), run["task_id"], run["id"],
                        run.get("failure_kind") or "EXECUTION", reason,
                        int(run.get("attempt") or 0), "run:{}".format(run["id"]),
                        utc_now(), utc_now(),
                    ),
                ) > 0
            )
        return {"created": created, "open": int((self.db.one("SELECT COUNT(*) count FROM dead_letters WHERE status='OPEN'") or {"count": 0})["count"])}

    def tick(self, max_new: int = 1, max_active: int = 3) -> Dict[str, Any]:
        token = self.acquire_lease()
        if token is None:
            return {"status": "STANDBY", "holder_id": self.holder_id}
        started = utc_now()
        self.db.execute(
            """
            INSERT INTO reconciliation_checkpoints(
                name, holder_id, fencing_token, status, summary_json,
                started_at, updated_at
            ) VALUES('recovery',?,?, 'RUNNING','{}',?,?)
            ON CONFLICT(name) DO UPDATE SET
                holder_id=excluded.holder_id, fencing_token=excluded.fencing_token,
                status='RUNNING', started_at=excluded.started_at,
                updated_at=excluded.updated_at
            """,
            (self.holder_id, token, started, started),
        )
        try:
            scheduler = self.manager.scheduler_tick(max_new=max_new, max_active=max_active)
            contracts = self.sync_evidence_contracts()
            dead_letters = self.capture_dead_letters()
            summary = {
                "status": "READY",
                "fencing_token": token,
                "scheduler": scheduler,
                "reconcile": scheduler.get("reconcile", {}),
                "verification": scheduler.get("verification", {}),
                "evidence": contracts,
                "dead_letters": dead_letters,
            }
            self.db.execute(
                "UPDATE reconciliation_checkpoints SET status='READY', summary_json=?, finished_at=?, updated_at=? WHERE name='recovery'",
                (json.dumps(summary, ensure_ascii=False, sort_keys=True), utc_now(), utc_now()),
            )
            return summary
        except Exception as exc:
            failure = {"status": "DEGRADED", "error": type(exc).__name__, "fencing_token": token}
            self.db.execute(
                "UPDATE reconciliation_checkpoints SET status='DEGRADED', summary_json=?, finished_at=?, updated_at=? WHERE name='recovery'",
                (json.dumps(failure, sort_keys=True), utc_now(), utc_now()),
            )
            raise

    def status(self) -> Dict[str, Any]:
        checkpoint = self.db.one("SELECT * FROM reconciliation_checkpoints WHERE name='recovery'") or {}
        try:
            summary = json.loads(str(checkpoint.get("summary_json") or "{}"))
        except json.JSONDecodeError:
            summary = {}
        plugins = self.db.all("SELECT name,kind,enabled,model,min_reasoning,health FROM executor_plugins ORDER BY name")
        evidence = self.db.one(
            "SELECT COUNT(*) total, SUM(CASE WHEN status='MISSING' AND required=1 THEN 1 ELSE 0 END) missing FROM evidence_contracts WHERE required=1"
        ) or {"total": 0, "missing": 0}
        dead = self.db.one("SELECT COUNT(*) count FROM dead_letters WHERE status='OPEN'") or {"count": 0}
        return {
            "enabled": True,
            "status": checkpoint.get("status") or "STARTING",
            "holder_id": checkpoint.get("holder_id") or "",
            "fencing_token": int(checkpoint.get("fencing_token") or 0),
            "updated_at": checkpoint.get("updated_at"),
            "summary": summary,
            "evidence": {"total": int(evidence.get("total") or 0), "missing": int(evidence.get("missing") or 0)},
            "dead_letters": {"open": int(dead.get("count") or 0)},
            "plugins": plugins,
        }
