"""Durable, manual lifecycle bookkeeping; never dispatches or grants execution.

actor/source_ref are attestations from the trusted local caller, not authenticated
Codex identities. The host bridge must authenticate callers and deliver outbox
messages. A delivery ACK is explicitly not a recipient's acceptance receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


class LifecycleError(ValueError):
    def __init__(self, message, code="invalid", status=400):
        super().__init__(message)
        self.code, self.status = code, status

    def payload(self):
        return {"error": str(self), "code": self.code}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _text(value, limit=500, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or any(ord(c) < 32 for c in value):
        raise LifecycleError("invalid bounded text")
    from .redaction import contains_secret
    if contains_secret(value):
        raise LifecycleError("secret-bearing text or URL query must be replaced with a safe reference")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", value):
        raise LifecycleError("invalid identifier")
    _text(value, 128)
    return value


def _choice(value, options):
    return isinstance(value, str) and value in options


def _ref(value):
    _text(value, 1000)
    if "\\" in value or any(p in {".", ".."} for p in value.split("/")):
        raise LifecycleError("invalid reference path")
    return value


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise LifecycleError("invalid object fields")
    return value


def _time(value):
    _text(value, 80)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise LifecycleError("invalid timezone-aware timestamp") from exc


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise LifecycleError("invalid SHA256")
    return value


def _artifacts(value, required=False):
    if not isinstance(value, list) or len(value) > 50 or (required and not value):
        raise LifecycleError("invalid artifacts")
    for item in value:
        _shape(item, {"ref", "sha256"})
        _ref(item["ref"])
        _sha(item["sha256"])
    if len({item["ref"] for item in value}) != len(value):
        raise LifecycleError("duplicate artifact reference")
    return value


STAGES = {"unassigned", "planning", "implementation", "review", "qa", "handoff", "release", "deploy", "smoke", "closure"}
ACTIONS = {"amend", "external-register", "external-activity", "external-finish", "handoff-offer", "handoff-accept", "handoff-reject", "handoff-resolve", "outbox-claim", "outbox-ack", "reconcile"}
COMMON = {"expected_revision", "idempotency_key", "actor", "source_ref"}


@contextmanager
def _transaction(connection):
    """Borrowed connections retain outer ownership, including worker transactions."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
        yield
        return
    savepoint = "lifecycle_" + uuid.uuid4().hex
    connection.execute("SAVEPOINT " + savepoint)
    try:
        yield
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK TO " + savepoint)
            connection.execute("RELEASE " + savepoint)
        raise
    else:
        connection.execute("RELEASE " + savepoint)


def initialize_lifecycle_schema(connection):
    """Additive and restart-safe; no task/run history rewrite or imported DB copy."""
    statements = [
        """CREATE TABLE IF NOT EXISTS lifecycle_versions (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0), stage TEXT NOT NULL DEFAULT 'unassigned')""",
        """CREATE TABLE IF NOT EXISTS lifecycle_requests (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(task_id,idempotency_key))""",
        """CREATE TABLE IF NOT EXISTS lifecycle_external_executions (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            executor TEXT NOT NULL, model TEXT NOT NULL, reasoning TEXT NOT NULL, speed TEXT NOT NULL,
            source_thread TEXT NOT NULL, source_turn TEXT NOT NULL, status TEXT NOT NULL,
            last_activity_at TEXT NOT NULL, activity_ref TEXT NOT NULL, finished_at TEXT,
            artifacts_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
            UNIQUE(task_id,executor,source_thread,source_turn))""",
        """CREATE TABLE IF NOT EXISTS lifecycle_handoffs (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            sender TEXT NOT NULL, recipient TEXT NOT NULL, deadline TEXT NOT NULL, stage TEXT NOT NULL,
            next_action TEXT NOT NULL, artifacts_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'offered', accepted_at TEXT, rejected_at TEXT,
            rejection_reason TEXT NOT NULL DEFAULT '', missing_items_json TEXT NOT NULL DEFAULT '[]',
            resolved_at TEXT, resolution_ref TEXT NOT NULL DEFAULT '', resolution TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS lifecycle_outbox (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            handoff_id TEXT REFERENCES lifecycle_handoffs(id), recipient TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL, claimed_by TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '',
            lease_until TEXT, delivered_at TEXT, delivery_ref TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            dedupe_key TEXT, UNIQUE(handoff_id,kind,recipient))""",
        "CREATE INDEX IF NOT EXISTS lifecycle_external_task ON lifecycle_external_executions(task_id,status)",
        "CREATE INDEX IF NOT EXISTS lifecycle_handoff_task ON lifecycle_handoffs(task_id,status)",
        "CREATE INDEX IF NOT EXISTS lifecycle_outbox_task ON lifecycle_outbox(task_id,status)",
        "INSERT OR IGNORE INTO lifecycle_versions(task_id) SELECT id FROM tasks",
        """CREATE TRIGGER IF NOT EXISTS lifecycle_task_insert AFTER INSERT ON tasks BEGIN
            INSERT OR IGNORE INTO lifecycle_versions(task_id) VALUES(NEW.id); END""",
        """CREATE TRIGGER IF NOT EXISTS lifecycle_task_update AFTER UPDATE ON tasks BEGIN
            UPDATE lifecycle_versions SET revision=revision+1 WHERE task_id=NEW.id; END""",
        """CREATE TRIGGER IF NOT EXISTS lifecycle_completion_guard BEFORE UPDATE OF state ON tasks
            WHEN NEW.state='DONE' AND OLD.state!='DONE' BEGIN
            SELECT CASE WHEN EXISTS(SELECT 1 FROM lifecycle_handoffs WHERE task_id=NEW.id AND status!='resolved')
                THEN RAISE(ABORT,'unresolved lifecycle handoff') END;
            SELECT CASE WHEN EXISTS(SELECT 1 FROM lifecycle_external_executions WHERE task_id=NEW.id AND status IN ('active','lost'))
                THEN RAISE(ABORT,'unfinished external execution') END;
            SELECT CASE WHEN NEW.requires_deploy=1 AND (
                NOT EXISTS(SELECT 1 FROM evidence WHERE task_id=NEW.id AND kind='deploy' AND verified=1) OR
                NOT EXISTS(SELECT 1 FROM evidence WHERE task_id=NEW.id AND kind='smoke' AND verified=1))
                THEN RAISE(ABORT,'missing deployment completion evidence') END; END""",
        """CREATE TRIGGER IF NOT EXISTS lifecycle_managed_enqueue_guard BEFORE INSERT ON runs
            WHEN NEW.status IN ('QUEUED','RUNNING') BEGIN
            SELECT CASE WHEN EXISTS(SELECT 1 FROM lifecycle_external_executions WHERE task_id=NEW.task_id AND status IN ('active','lost'))
                OR EXISTS(SELECT 1 FROM lifecycle_handoffs WHERE task_id=NEW.task_id AND status!='resolved')
                THEN RAISE(ABORT,'lifecycle responsibility is unresolved; managed dispatch blocked') END; END""",
    ]
    with _transaction(connection):
        connection.execute("CREATE TABLE IF NOT EXISTS lifecycle_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        version = connection.execute("SELECT value FROM lifecycle_meta WHERE key='schema_version'").fetchone()
        if version and version[0] != "1":
            raise LifecycleError("unsupported lifecycle schema")
        for statement in statements:
            connection.execute(statement)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(lifecycle_outbox)")}
        if "dedupe_key" not in columns:
            connection.execute("ALTER TABLE lifecycle_outbox ADD COLUMN dedupe_key TEXT")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_outbox_dedupe ON lifecycle_outbox(dedupe_key)")
        external_columns = {row[1] for row in connection.execute("PRAGMA table_info(lifecycle_external_executions)")}
        if "handoff_id" not in external_columns:
            connection.execute("ALTER TABLE lifecycle_external_executions ADD COLUMN handoff_id TEXT REFERENCES lifecycle_handoffs(id)")
        connection.execute("INSERT OR IGNORE INTO lifecycle_meta(key,value) VALUES('schema_version','1')")


class LifecycleService:
    def __init__(self, db):
        self.db = db

    def snapshot(self, task_id, connection=None, now=None):
        if connection is None:
            with self.db.connect() as owned:
                if not owned.in_transaction:
                    owned.execute("BEGIN")
                return self.snapshot(task_id, owned, now)
        task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task_row is None:
            raise LifecycleError("task not found", "missing", 404)
        task = dict(task_row)
        version = dict(connection.execute("SELECT * FROM lifecycle_versions WHERE task_id=?", (task_id,)).fetchone())
        now = now or datetime.now(timezone.utc)
        external = [dict(row) for row in connection.execute("SELECT * FROM lifecycle_external_executions WHERE task_id=? ORDER BY created_at,id", (task_id,))]
        handoffs = [dict(row) for row in connection.execute("SELECT * FROM lifecycle_handoffs WHERE task_id=? ORDER BY created_at,id", (task_id,))]
        outbox = [dict(row) for row in connection.execute("SELECT * FROM lifecycle_outbox WHERE task_id=? ORDER BY created_at,id", (task_id,))]
        for item in external:
            item["artifacts"] = json.loads(item.pop("artifacts_json"))
            item["overdue"] = item["status"] in {"active", "lost"} and _time(item["last_activity_at"]) + timedelta(minutes=15) < now
            item["display_status"] = "lost" if item["overdue"] else item["status"]
        for item in handoffs:
            item["artifacts"] = json.loads(item.pop("artifacts_json"))
            item["missing_items"] = json.loads(item.pop("missing_items_json"))
            item["execution_ids"] = [e["id"] for e in external if e.get("handoff_id") == item["id"]]
            item["execution_started"] = bool(item["execution_ids"])
            item["start_overdue"] = item["status"] == "accepted" and not item["execution_started"] and _time(item["deadline"]) < now
            item["overdue"] = (item["status"] == "offered" and _time(item["deadline"]) < now) or item["start_overdue"]
        for item in outbox:
            # Claim tokens are returned only to the claiming write caller.
            item.pop("claim_token", None)
        unresolved = [h for h in handoffs if h["status"] != "resolved"]
        unfinished = [e for e in external if e["status"] in {"active", "lost"}]
        status = "idle"
        action = {"owner_kind": task.get("action_owner_kind", "none"), "owner": task.get("action_owner", ""), "text": task.get("action_text", ""), "due": task.get("action_due")}
        if unfinished:
            lost = [e for e in unfinished if e["display_status"] == "lost"]
            status = "execution_lost" if lost else "execution_active"
            responsible = min(lost, key=lambda e: (_time(e["last_activity_at"]), e["id"])) if lost else unfinished[-1]
            action = {"owner_kind": "agent", "owner": responsible["executor"], "text": "确认外部执行活动并登记实际结果" if status == "execution_lost" else "完成已登记执行并交付产物", "due": None}
        if unresolved:
            h = unresolved[-1]
            if not unfinished:
                status = {"offered": "awaiting_acceptance", "accepted": "accepted", "rejected": "rejected"}[h["status"]]
                action = {"owner_kind": "agent", "owner": h["sender"] if h["status"] == "rejected" else h["recipient"], "text": "补齐拒收缺项：" + h["rejection_reason"] if h["status"] == "rejected" else h["next_action"], "due": h["deadline"]}
        blockers = ["unresolved_handoff:" + h["id"] for h in unresolved] + ["unfinished_external_execution:" + e["id"] for e in unfinished]
        return {"revision": version["revision"], "stage": version["stage"], "status": status, "next_action": action,
                "external_executions": external, "handoffs": handoffs, "outbox": outbox, "completion_blockers": blockers,
                "notification_bridge": {"mode": "manual", "native_delivery": False, "identity_assurance": "trusted_local_caller_attestation"}}

    def _executable(self, connection, task):
        from .execution_policy import execution_forbidden
        from .service import is_paused_by_user
        intake = connection.execute("SELECT intent FROM intakes WHERE id=?", (task.get("source_request_id", ""),)).fetchone()
        if is_paused_by_user(task):
            raise LifecycleError("user-paused task cannot register execution or change lifecycle ownership", "forbidden", 409)
        if task["state"] in {"DONE", "CANCELED", "FAILED", "PAUSED"} or is_paused_by_user(task) or execution_forbidden(task, intake_intent=intake[0] if intake else None):
            raise LifecycleError("terminal, paused, imported or analysis-only task is not mutable", "forbidden", 409)

    def _no_managed_run(self, connection, task_id):
        if connection.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('QUEUED','RUNNING')", (task_id,)).fetchone():
            raise LifecycleError("active managed run must retain its execution identity", "conflict", 409)

    def _record(self, connection, task_id, action, payload, before, result, now):
        from .redaction import redact_text
        def safe(value):
            if isinstance(value, str):
                return redact_text(value, max_chars=1000)
            if isinstance(value, dict):
                return {k: safe(v) for k, v in value.items() if k != "claim_token"}
            if isinstance(value, list):
                return [safe(v) for v in value]
            return value
        audit = safe({"source_ref": payload["source_ref"], "actor": payload["actor"], "before_revision": before["revision"], "after_revision": result["revision"], "result": result["result"]})
        inserted = connection.execute("INSERT INTO events(event_id,task_id,event_type,producer,summary,payload_json,dedupe_key,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), task_id, "lifecycle." + action, payload["actor"], "Lifecycle " + action,
                            _json(audit), "lifecycle:" + task_id + ":" + payload["idempotency_key"], now))
        if inserted.rowcount != 1:
            raise LifecycleError("audit event was not recorded", "audit_failed", 409)

    def _notify(self, connection, task_id, handoff_id, recipient, kind, now, subject_id=None):
        dedupe = _json([task_id, subject_id or handoff_id, recipient, kind])
        connection.execute("INSERT OR IGNORE INTO lifecycle_outbox(id,task_id,handoff_id,recipient,kind,available_at,created_at,dedupe_key) VALUES(?,?,?,?,?,?,?,?)",
                           ("notice-" + uuid.uuid4().hex, task_id, handoff_id, recipient, kind, now, now, dedupe))
        if not connection.execute("SELECT 1 FROM lifecycle_outbox WHERE dedupe_key=?", (dedupe,)).fetchone():
            raise LifecycleError("notification outbox was not recorded", "audit_failed", 409)

    def apply(self, task_id, action, payload):
        _id(task_id)
        if not _choice(action, ACTIONS):
            raise LifecycleError("unknown lifecycle action", "missing", 404)
        if not isinstance(payload, dict) or not COMMON <= set(payload):
            raise LifecycleError("expected_revision, idempotency_key, actor and source_ref are required")
        revision = payload["expected_revision"]
        if type(revision) is not int or not 0 <= revision < 2 ** 63:
            raise LifecycleError("invalid expected_revision")
        _id(payload["idempotency_key"])
        _id(payload["actor"])
        _ref(payload["source_ref"])
        encoded = _json({"action": action, "payload": payload})
        if len(encoded.encode("utf-8")) > 65536:
            raise LifecycleError("lifecycle request exceeds 64 KiB")
        request_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.db.connect() as connection, _transaction(connection):
            prior = connection.execute("SELECT * FROM lifecycle_requests WHERE task_id=? AND idempotency_key=?", (task_id, payload["idempotency_key"])).fetchone()
            if prior:
                if prior["request_json"] != request_hash:
                    raise LifecycleError("idempotency key reused with different payload", "idempotency_conflict", 409)
                return {**json.loads(prior["result_json"]), "reused": True}
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise LifecycleError("task not found", "missing", 404)
            task = dict(row)
            current = connection.execute("SELECT revision FROM lifecycle_versions WHERE task_id=?", (task_id,)).fetchone()[0]
            if current != revision:
                raise LifecycleError("stale lifecycle revision; refresh before retry", "stale_revision", 409)
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            before = self.snapshot(task_id, connection, now_dt)
            handler = getattr(self, "_" + action.replace("-", "_"))
            result = handler(connection, task, payload, now_dt)
            connection.execute("UPDATE lifecycle_versions SET revision=revision+1 WHERE task_id=? AND revision=?", (task_id, revision))
            after = self.snapshot(task_id, connection, now_dt)
            response = {"task_id": task_id, "action": action, "revision": after["revision"], "reused": False, "result": result, "lifecycle": after}
            self._record(connection, task_id, action, payload, before, response, now)
            inserted = connection.execute("INSERT INTO lifecycle_requests(task_id,idempotency_key,request_json,result_json,created_at) VALUES(?,?,?,?,?)",
                               (task_id, payload["idempotency_key"], request_hash, _json(response), now))
            if inserted.rowcount != 1:
                raise LifecycleError("idempotency record was not recorded", "audit_failed", 409)
            return response

    def _amend(self, connection, task, p, now):
        _shape(p, COMMON | {"changes"})
        self._executable(connection, task)
        allowed = {"environment", "requires_deploy", "owner_session", "action_owner_kind", "action_owner", "action_text", "action_due", "stage"}
        changes = _shape(p["changes"], set(), allowed)
        old_stage = connection.execute("SELECT stage FROM lifecycle_versions WHERE task_id=?", (task["id"],)).fetchone()[0]
        if not changes:
            raise LifecycleError("empty amendment")
        if set(changes) & {"environment", "owner_session"}:
            self._no_managed_run(connection, task["id"])
        if "environment" in changes and not _choice(changes["environment"], {"local", "dev", "test", "staging", "prod", "production"}):
            raise LifecycleError("invalid environment")
        if "requires_deploy" in changes:
            if type(changes["requires_deploy"]) is not bool:
                raise LifecycleError("requires_deploy must be a JSON boolean")
            if task["requires_deploy"] and not changes["requires_deploy"]:
                raise LifecycleError("deployment acceptance requirements cannot be weakened", "forbidden", 409)
        for name in ("owner_session", "action_owner"):
            if name in changes:
                _text(changes[name], 120, empty=True)
        if "action_text" in changes:
            _text(changes["action_text"], 500, empty=True)
        if "action_due" in changes and changes["action_due"] is not None:
            _time(changes["action_due"])
        if "action_owner_kind" in changes and not _choice(changes["action_owner_kind"], {"none", "user", "external", "agent"}):
            raise LifecycleError("invalid action owner kind")
        if "stage" in changes and not _choice(changes["stage"], STAGES):
            raise LifecycleError("invalid stage")
        merged = {**task, **changes}
        if merged["action_owner_kind"] != "none" and (not merged["action_owner"] or not merged["action_text"]):
            raise LifecycleError("action owner and text required")
        if merged["action_owner_kind"] == "none" and any(merged.get(k) for k in ("action_owner", "action_text", "action_due")):
            raise LifecycleError("none action cannot carry owner, text or due")
        native = {k: v for k, v in changes.items() if k != "stage"}
        if native:
            native["updated_at"] = now.isoformat()
            connection.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in native) + " WHERE id=?", (*native.values(), task["id"]))
        if "stage" in changes:
            connection.execute("UPDATE lifecycle_versions SET stage=? WHERE task_id=?", (changes["stage"], task["id"]))
        return {"before": {k: old_stage if k == "stage" else task.get(k) for k in changes}, "after": changes, "authorization": "source_reference_recorded_not_permission_grant"}

    def _external_register(self, connection, task, p, now):
        _shape(p, COMMON | {"executor", "model", "reasoning", "speed", "source_thread", "source_turn", "last_activity_at", "activity_ref"}, {"artifacts", "handoff_id"})
        self._executable(connection, task)
        self._no_managed_run(connection, task["id"])
        for field in ("executor", "source_thread", "source_turn"):
            _id(p[field])
        if not _choice(p["model"], {"gpt-5.6-sol", "gpt-6-astra"}) or not _choice(p["reasoning"], {"low", "medium", "high", "xhigh", "max", "ultra"}) or not _choice(p["speed"], {"standard", "fast", "priority", "unknown"}):
            raise LifecycleError("actual execution must use Sol or Astra with a supported effort and explicit speed")
        observed = _time(p["last_activity_at"])
        if observed > now:
            raise LifecycleError("activity timestamp cannot be in the future")
        _ref(p["activity_ref"])
        artifacts = _artifacts(p.get("artifacts", []))
        if "handoff_id" in p:
            h = self._handoff(connection, task, p)
            if h["status"] != "accepted" or h["recipient"] != p["executor"]:
                raise LifecycleError("execution must bind to an accepted handoff for its recipient", "identity_mismatch", 409)
            if observed < _time(h["accepted_at"]):
                raise LifecycleError("bound execution activity cannot predate acceptance")
        if connection.execute("SELECT 1 FROM lifecycle_external_executions WHERE task_id=? AND executor=? AND source_thread=? AND source_turn=?", (task["id"], p["executor"], p["source_thread"], p["source_turn"])).fetchone():
            raise LifecycleError("execution already registered; reuse the original idempotency key", "conflict", 409)
        execution_id = "external-" + uuid.uuid4().hex
        connection.execute("INSERT INTO lifecycle_external_executions(id,task_id,executor,model,reasoning,speed,source_thread,source_turn,status,last_activity_at,activity_ref,artifacts_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (execution_id, task["id"], p["executor"], p["model"], p["reasoning"], p["speed"], p["source_thread"], p["source_turn"], "active", observed.isoformat(), p["activity_ref"], _json(artifacts), now.isoformat()))
        if "handoff_id" in p:
            connection.execute("UPDATE lifecycle_external_executions SET handoff_id=? WHERE id=?", (p["handoff_id"], execution_id))
        return {"execution_id": execution_id, "handoff_id": p.get("handoff_id"), "executor": p["executor"], "model": p["model"], "reasoning": p["reasoning"], "speed": p["speed"], "source_thread": p["source_thread"], "source_turn": p["source_turn"], "last_activity_at": observed.isoformat(), "task_state_changed": False}

    def _execution(self, connection, task, execution_id):
        _id(execution_id)
        row = connection.execute("SELECT * FROM lifecycle_external_executions WHERE task_id=? AND id=?", (task["id"], execution_id)).fetchone()
        if not row:
            raise LifecycleError("external execution not found", "missing", 404)
        if row["status"] not in {"active", "lost"}:
            raise LifecycleError("execution already finished", "conflict", 409)
        return dict(row)

    def _external_activity(self, connection, task, p, now):
        _shape(p, COMMON | {"execution_id", "last_activity_at", "activity_ref"})
        self._executable(connection, task)
        record = self._execution(connection, task, p["execution_id"])
        if p["actor"] != record["executor"]:
            raise LifecycleError("activity actor must match executor", "identity_mismatch", 403)
        observed = _time(p["last_activity_at"])
        _ref(p["activity_ref"])
        if observed <= _time(record["last_activity_at"]) or observed > now or p["activity_ref"] == record["activity_ref"]:
            raise LifecycleError("activity must be new, monotonic and not future-dated")
        connection.execute("UPDATE lifecycle_external_executions SET status='active',last_activity_at=?,activity_ref=? WHERE id=?", (observed.isoformat(), p["activity_ref"], record["id"]))
        return {"execution_id": record["id"], "before_last_activity_at": record["last_activity_at"], "last_activity_at": observed.isoformat(), "activity_ref": p["activity_ref"], "task_state_changed": False}

    def _external_finish(self, connection, task, p, now):
        _shape(p, COMMON | {"execution_id", "status", "finished_at", "artifacts"})
        record = self._execution(connection, task, p["execution_id"])
        if p["actor"] not in {record["executor"], task["owner_session"]}:
            raise LifecycleError("finish actor must match executor or task owner", "identity_mismatch", 403)
        if not _choice(p["status"], {"finished", "failed", "canceled"}):
            raise LifecycleError("invalid external terminal status")
        finished = _time(p["finished_at"])
        if finished < _time(record["last_activity_at"]) or finished > now:
            raise LifecycleError("invalid execution finish time")
        _artifacts(p["artifacts"])
        artifacts = json.loads(record["artifacts_json"])
        for item in p["artifacts"]:
            if item not in artifacts:
                artifacts.append(item)
        connection.execute("UPDATE lifecycle_external_executions SET status=?,finished_at=?,artifacts_json=? WHERE id=?", (p["status"], finished.isoformat(), _json(artifacts), record["id"]))
        return {"execution_id": record["id"], "status": p["status"], "finished_at": finished.isoformat(), "artifacts": artifacts, "task_state_changed": False, "completion_granted": False}

    def _handoff_offer(self, connection, task, p, now):
        _shape(p, COMMON | {"recipient", "deadline", "stage", "next_action", "artifacts"})
        self._executable(connection, task)
        _id(p["recipient"])
        if p["recipient"] == p["actor"]:
            raise LifecycleError("sender and recipient must differ")
        deadline = _time(p["deadline"])
        if deadline <= now:
            raise LifecycleError("handoff deadline must be in the future")
        if not _choice(p["stage"], STAGES):
            raise LifecycleError("invalid stage")
        _text(p["next_action"], 500)
        artifacts = _artifacts(p["artifacts"], required=True)
        handoff_id = "handoff-" + uuid.uuid4().hex
        manifest = hashlib.sha256(_json(artifacts).encode("utf-8")).hexdigest()
        connection.execute("INSERT INTO lifecycle_handoffs(id,task_id,sender,recipient,deadline,stage,next_action,artifacts_json,manifest_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (handoff_id, task["id"], p["actor"], p["recipient"], deadline.isoformat(), p["stage"], p["next_action"], _json(artifacts), manifest, now.isoformat()))
        self._notify(connection, task["id"], handoff_id, p["recipient"], "handoff.offered", now.isoformat())
        return {"handoff_id": handoff_id, "manifest_sha256": manifest, "accepted": False, "native_delivery": False}

    def _handoff(self, connection, task, p):
        _id(p["handoff_id"])
        row = connection.execute("SELECT * FROM lifecycle_handoffs WHERE task_id=? AND id=?", (task["id"], p["handoff_id"])).fetchone()
        if row is None:
            raise LifecycleError("handoff not found", "missing", 404)
        return dict(row)

    def _receipt(self, connection, task, p, now, accepted):
        _shape(p, COMMON | {"handoff_id", "manifest_sha256"} | (set() if accepted else {"reason", "missing_items"}))
        h = self._handoff(connection, task, p)
        if accepted:
            self._executable(connection, task)
        if p["actor"] != h["recipient"]:
            raise LifecycleError("receipt actor must match handoff recipient", "identity_mismatch", 403)
        if h["status"] != "offered" or _time(h["deadline"]) <= now:
            raise LifecycleError("handoff already answered or receipt deadline expired; sender must resolve/reoffer", "stale_receipt", 409)
        if _sha(p["manifest_sha256"]) != h["manifest_sha256"]:
            raise LifecycleError("handoff manifest does not match", "manifest_mismatch", 409)
        reason, missing = "", []
        if not accepted:
            reason = _text(p["reason"], 500)
            missing = p["missing_items"]
            if not isinstance(missing, list) or not 1 <= len(missing) <= 50:
                raise LifecycleError("rejection requires missing_items")
            for item in missing:
                _text(item, 300)
        status = "accepted" if accepted else "rejected"
        connection.execute("UPDATE lifecycle_handoffs SET status=?," + ("accepted_at" if accepted else "rejected_at") + "=?,rejection_reason=?,missing_items_json=? WHERE id=?",
                           (status, now.isoformat(), reason, _json(missing), h["id"]))
        self._notify(connection, task["id"], h["id"], h["sender"], "handoff." + status, now.isoformat())
        return {"handoff_id": h["id"], "status": status, "execution_started": False, "completion_granted": False}

    def _handoff_accept(self, connection, task, p, now):
        return self._receipt(connection, task, p, now, True)

    def _handoff_reject(self, connection, task, p, now):
        return self._receipt(connection, task, p, now, False)

    def _handoff_resolve(self, connection, task, p, now):
        _shape(p, COMMON | {"handoff_id", "resolution", "resolution_ref"})
        h = self._handoff(connection, task, p)
        if p["actor"] != h["sender"]:
            raise LifecycleError("only the sender may resolve a handoff", "identity_mismatch", 403)
        if h["status"] == "resolved":
            raise LifecycleError("handoff already resolved", "conflict", 409)
        if not _choice(p["resolution"], {"completed", "withdrawn", "superseded"}):
            raise LifecycleError("invalid handoff resolution")
        if p["resolution"] == "completed" and h["status"] != "accepted":
            raise LifecycleError("unaccepted handoff cannot be resolved as completed")
        _ref(p["resolution_ref"])
        connection.execute("UPDATE lifecycle_handoffs SET status='resolved',resolved_at=?,resolution_ref=?,resolution=? WHERE id=?", (now.isoformat(), p["resolution_ref"], p["resolution"], h["id"]))
        self._notify(connection, task["id"], h["id"], h["recipient"], "handoff.resolved", now.isoformat())
        return {"handoff_id": h["id"], "status": "resolved", "completion_granted": False}

    def _outbox_claim(self, connection, task, p, now):
        _shape(p, COMMON | {"outbox_id", "lease_seconds"})
        _id(p["outbox_id"])
        if type(p["lease_seconds"]) is not int or not 1 <= p["lease_seconds"] <= 300:
            raise LifecycleError("lease_seconds must be an integer in 1..300")
        item = connection.execute("SELECT * FROM lifecycle_outbox WHERE task_id=? AND id=?", (task["id"], p["outbox_id"])).fetchone()
        if not item:
            raise LifecycleError("notification not found", "missing", 404)
        if item["status"] == "delivered" or (item["status"] == "claimed" and _time(item["lease_until"]) > now):
            raise LifecycleError("notification delivered or lease still held", "conflict", 409)
        token = uuid.uuid4().hex
        lease = (now + timedelta(seconds=p["lease_seconds"])).isoformat()
        connection.execute("UPDATE lifecycle_outbox SET status='claimed',claimed_by=?,claim_token=?,lease_until=?,attempts=attempts+1 WHERE id=?", (p["actor"], token, lease, item["id"]))
        return {"outbox_id": item["id"], "claim_token": token, "lease_until": lease, "recipient": item["recipient"], "kind": item["kind"], "handoff_id": item["handoff_id"], "native_delivery": False}

    def _outbox_ack(self, connection, task, p, now):
        _shape(p, COMMON | {"outbox_id", "claim_token", "delivery_ref"})
        _id(p["outbox_id"])
        _id(p["claim_token"])
        _ref(p["delivery_ref"])
        item = connection.execute("SELECT * FROM lifecycle_outbox WHERE task_id=? AND id=?", (task["id"], p["outbox_id"])).fetchone()
        if not item:
            raise LifecycleError("notification not found", "missing", 404)
        if item["status"] != "claimed" or p["actor"] != item["claimed_by"] or p["claim_token"] != item["claim_token"] or _time(item["lease_until"]) <= now:
            raise LifecycleError("stale or mismatched delivery lease", "stale_ack", 409)
        connection.execute("UPDATE lifecycle_outbox SET status='delivered',delivered_at=?,delivery_ref=? WHERE id=?", (now.isoformat(), p["delivery_ref"], item["id"]))
        return {"outbox_id": item["id"], "delivered": True, "recipient_accepted": False, "native_delivery": False}

    def _reconcile(self, connection, task, p, now):
        _shape(p, COMMON)
        stale = connection.execute("SELECT * FROM lifecycle_external_executions WHERE task_id=? AND status='active'", (task["id"],)).fetchall()
        lost = []
        for item in stale:
            if _time(item["last_activity_at"]) + timedelta(minutes=15) < now:
                connection.execute("UPDATE lifecycle_external_executions SET status='lost' WHERE id=?", (item["id"],))
                lost.append(item["id"])
                self._notify(connection, task["id"], None, task["owner_session"] or item["executor"], "execution.lost", now.isoformat(), subject_id=[item["id"], item["last_activity_at"]])
        overdue = []
        start_overdue = []
        for h in connection.execute("SELECT * FROM lifecycle_handoffs WHERE task_id=? AND status='offered'", (task["id"],)):
            if _time(h["deadline"]) < now:
                overdue.append(h["id"])
                self._notify(connection, task["id"], h["id"], h["sender"], "handoff.overdue", now.isoformat())
        for h in connection.execute("SELECT h.* FROM lifecycle_handoffs h WHERE h.task_id=? AND h.status='accepted' AND NOT EXISTS(SELECT 1 FROM lifecycle_external_executions e WHERE e.handoff_id=h.id)", (task["id"],)):
            if _time(h["deadline"]) < now:
                start_overdue.append(h["id"])
                self._notify(connection, task["id"], h["id"], h["recipient"], "handoff.start_overdue", now.isoformat())
        return {"lost_executions": lost, "overdue_handoffs": overdue, "start_overdue_handoffs": start_overdue, "dispatches": 0, "resumed_tasks": 0, "native_delivery": False}

    def reconcile_due(self):
        """Watchdog housekeeping only for newly registered lifecycle subjects.

        A race with a user revision defers to the next tick. No unchanged poll
        produces an audit, updates activity, renews a lease, or launches work.
        """
        now = datetime.now(timezone.utc)
        subjects = {}
        with self.db.connect() as connection:
            connection.execute("BEGIN")
            for item in connection.execute("SELECT * FROM lifecycle_external_executions WHERE status='active'"):
                if _time(item["last_activity_at"]) + timedelta(minutes=15) < now:
                    subjects.setdefault(item["task_id"], []).append([item["id"], item["last_activity_at"]])
            for item in connection.execute("SELECT h.* FROM lifecycle_handoffs h WHERE h.status='offered' AND NOT EXISTS(SELECT 1 FROM lifecycle_outbox o WHERE o.handoff_id=h.id AND o.kind='handoff.overdue' AND o.recipient=h.sender)"):
                if _time(item["deadline"]) < now:
                    subjects.setdefault(item["task_id"], []).append([item["id"], item["deadline"]])
            for item in connection.execute("SELECT h.* FROM lifecycle_handoffs h WHERE h.status='accepted' AND NOT EXISTS(SELECT 1 FROM lifecycle_external_executions e WHERE e.handoff_id=h.id) AND NOT EXISTS(SELECT 1 FROM lifecycle_outbox o WHERE o.handoff_id=h.id AND o.kind='handoff.start_overdue' AND o.recipient=h.recipient)"):
                if _time(item["deadline"]) < now:
                    subjects.setdefault(item["task_id"], []).append([item["id"], "accepted", item["deadline"]])
        result = {"tasks_reconciled": 0, "lost_executions": 0, "overdue_handoffs": 0, "start_overdue_handoffs": 0, "deferred_races": 0, "dispatches": 0}
        for task_id, due in subjects.items():
            revision = self.snapshot(task_id)["revision"]
            key = "watchdog-" + hashlib.sha256(_json(sorted(due)).encode("utf-8")).hexdigest()
            try:
                applied = self.apply(task_id, "reconcile", {"expected_revision": revision, "idempotency_key": key, "actor": "lifecycle-watchdog", "source_ref": "engine:non-executing-housekeeping",})
            except LifecycleError as exc:
                if exc.code in {"stale_revision", "idempotency_conflict"}:
                    result["deferred_races"] += 1
                    continue
                raise
            result["tasks_reconciled"] += 1
            result["lost_executions"] += len(applied["result"]["lost_executions"])
            result["overdue_handoffs"] += len(applied["result"]["overdue_handoffs"])
            result["start_overdue_handoffs"] += len(applied["result"]["start_overdue_handoffs"])
        return result

    def record_legacy_heartbeat(self, task_id, producer, execution_mode, model, reasoning, speed, process_alive):
        """Keep the legacy explicit heartbeat, with an atomic fail-closed guard.

        New lifecycle-owned work must use its source-backed activity endpoint.
        This does not synthesize an external execution or a Worker Run.
        """
        _text(producer, 120)
        with self.db.connect() as connection, _transaction(connection):
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("task not found")
            task = dict(row)
            self._executable(connection, task)
            lifecycle = self.snapshot(task_id, connection)
            if lifecycle["completion_blockers"] or lifecycle["external_executions"]:
                raise LifecycleError("use the lifecycle execution activity endpoint for lifecycle-owned work", "conflict", 409)
            active = connection.execute("SELECT * FROM runs WHERE task_id=? AND status IN ('QUEUED','RUNNING') ORDER BY attempt DESC LIMIT 1", (task_id,)).fetchone()
            if model is not None and active:
                raise LifecycleError("active managed run cannot be relabelled by an external heartbeat", "conflict", 409)
            alive = bool(active and process_alive(active["pid"]))
            if active and not alive:
                raise LifecycleError("managed run must be reconciled before external takeover", "conflict", 409)
            now = datetime.now(timezone.utc).isoformat()
            mode = "managed" if alive else execution_mode
            updates = {"execution_mode": mode, "heartbeat_at": None if alive else now, "blocking_reason": "", "updated_at": now, "state": "RUNNING"}
            if task["state"] != "RUNNING":
                updates["progress"] = 55
                updates["started_at"] = task["started_at"] or now
            if model is not None:
                updates.update(model=model, reasoning=reasoning, speed=task["speed"] if speed is None else speed)
            connection.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in updates) + " WHERE id=?", (*updates.values(), task_id))
            def event(kind, summary, payload):
                inserted = connection.execute("INSERT INTO events(event_id,task_id,event_type,producer,summary,payload_json,dedupe_key,occurred_at) VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), task_id, kind, producer, summary, _json(payload), kind + ":" + task_id + ":" + uuid.uuid4().hex, now))
                if inserted.rowcount != 1:
                    raise LifecycleError("heartbeat audit was not recorded", "audit_failed", 409)
            if model is not None:
                event("task.execution_model_registered", "已登记本次外部执行配置；历史 Run 保持原记录", {"previous_model": task["model"], "previous_reasoning": task["reasoning"], "model": model, "reasoning": reasoning, "speed": speed, "source": "external-owner", "assurance": "declared_execution_parameters_not_independent_host_verification"})
            if task["state"] != "RUNNING":
                event("task.state_changed", "显式执行心跳已登记", {"state": "RUNNING"})
            event("task.external_heartbeat", "外部执行心跳已更新", {"state": "RUNNING", "execution_mode": mode})


def handle_lifecycle_http(handler, method):
    """Shared route, host/origin validation stays with the existing server."""
    from urllib.parse import unquote, urlparse
    parts = urlparse(handler.path).path.strip("/").split("/")
    if len(parts) < 4 or parts[:2] != ["api", "tasks"] or parts[3] != "lifecycle":
        return False
    try:
        if method == "GET" and len(parts) == 4:
            result = handler.service.lifecycle.snapshot(unquote(parts[2]))
        elif method == "POST" and len(parts) == 5:
            length = handler.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]+", length) or not 0 < int(length) <= 65536 or handler.headers.get("Transfer-Encoding"):
                handler.close_connection = True
                raise LifecycleError("lifecycle JSON body must be at most 64 KiB")
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate JSON key")
                    value[key] = item
                return value
            try:
                payload = json.loads(handler.rfile.read(int(length)), object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            except (ValueError, UnicodeError, RecursionError) as exc:
                handler.close_connection = True
                raise LifecycleError("invalid lifecycle JSON") from exc
            result = handler.service.lifecycle.apply(unquote(parts[2]), parts[4], payload)
        else:
            raise LifecycleError("lifecycle endpoint not found", "missing", 404)
        handler._json(200, result)
    except LifecycleError as exc:
        handler._json(exc.status, exc.payload())
    return True
