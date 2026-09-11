"""Portable, append-only registration of manually reviewed release evidence.

Only a context-managed SQLite connection is required. No execution, filesystem,
network, task lifecycle, configuration, or independent verification adapters exist.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

ASSURANCE = "recorded_evidence_review_not_independent_reverification"
ENVIRONMENTS = {"dev", "test", "prod"}
STATUSES = {
    "deployment": {"deployed", "failed", "rolled_back", "unknown"},
    "enablement": {"enabled", "disabled", "unknown"},
    "acceptance": {"passed", "failed", "not_tested"},
}
HEALTH = {
    "argo_sync": "Synced", "argo_health": "Healthy",
    "argo_operation": "Succeeded", "rollout": "Healthy", "readiness": "passed",
}
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS release_schema_version (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL CHECK(schema_version=1),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0))""",
    """INSERT OR IGNORE INTO release_schema_version
        (singleton,schema_version,revision) VALUES(1,1,0)""",
    """CREATE TABLE IF NOT EXISTS release_batches (
        id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
        payload_hash TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
        environment TEXT NOT NULL CHECK(environment IN ('dev','test','prod')),
        owner TEXT NOT NULL, recorded_at TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>=1))""",
    """CREATE TABLE IF NOT EXISTS release_items (
        batch_id TEXT NOT NULL REFERENCES release_batches(id),
        item_key TEXT NOT NULL, ordinal INTEGER NOT NULL,
        feature_key TEXT NOT NULL, title TEXT NOT NULL, component TEXT NOT NULL,
        artifact_json TEXT NOT NULL, PRIMARY KEY(batch_id,item_key))""",
    """CREATE TABLE IF NOT EXISTS release_item_tasks (
        batch_id TEXT NOT NULL, item_key TEXT NOT NULL, task_id TEXT NOT NULL,
        PRIMARY KEY(batch_id,item_key,task_id),
        FOREIGN KEY(batch_id,item_key) REFERENCES release_items(batch_id,item_key))""",
    """CREATE INDEX IF NOT EXISTS idx_release_task_mapping
        ON release_item_tasks(task_id,batch_id)""",
    """CREATE TABLE IF NOT EXISTS release_receipts (
        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES release_batches(id),
        revision INTEGER NOT NULL, idempotency_key TEXT NOT NULL,
        payload_hash TEXT NOT NULL, received_at TEXT NOT NULL, items_json TEXT NOT NULL,
        UNIQUE(batch_id,revision), UNIQUE(batch_id,idempotency_key))""",
    """CREATE TRIGGER IF NOT EXISTS release_receipts_no_update
        BEFORE UPDATE ON release_receipts BEGIN
        SELECT RAISE(ABORT,'release receipts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS release_receipts_no_delete
        BEFORE DELETE ON release_receipts BEGIN
        SELECT RAISE(ABORT,'release receipts are append-only'); END""",
)


class ReleaseError(ValueError):
    def __init__(self, message: str, code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status

    def payload(self) -> dict:
        return {"error": str(self), "code": self.code, "assurance": ASSURANCE}


def initialize_release_schema(connection: Any) -> None:
    """Add only release-owned tables/indexes/triggers; never rewrite legacy rows."""
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ReleaseError("payload must be finite JSON") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _rows(connection: Any, sql: str, args: tuple = ()) -> list:
    cursor = connection.execute(sql, args)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ReleaseError(label + " must be an object")
    return value


def _keys(value: dict, allowed: set, label: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise ReleaseError(label + " has unsupported fields: " + ", ".join(sorted(unexpected)))


def _text(value: Any, label: str, limit: int = 512, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ReleaseError(label + " must be bounded text without control characters")
    value = value.strip()
    if not value and not empty:
        raise ReleaseError(label + " is required")
    return value


def _facts(value: Any) -> dict:
    value = _object(value, "facts")
    _keys(value, set(STATUSES), "facts")
    result = {}
    for dimension, raw in value.items():
        fact = _object(raw, "facts." + dimension)
        _keys(fact, {"status", "observed_at", "source", "review", "proof", "verified"}, dimension)
        if not isinstance(fact.get("status"), str) or fact["status"] not in STATUSES[dimension]:
            raise ReleaseError("invalid " + dimension + " status")
        for key in ("source", "review", "proof"):
            if key in fact:
                _object(fact[key], dimension + "." + key)
        if "observed_at" in fact and not isinstance(fact["observed_at"], str):
            raise ReleaseError(dimension + ".observed_at must be text")
        # Detach caller-owned containers, retaining untrusted review/proof fields
        # for audit. A publisher's `verified` flag has no assessment semantics.
        result[dimension] = json.loads(_json(fact))
    return result


def _bounded_payload(payload: Any) -> dict:
    payload = _object(payload, "payload")
    try:
        size = len(_json(payload).encode("utf-8"))
    except UnicodeError as exc:
        raise ReleaseError("payload contains invalid Unicode") from exc
    if size > 64 * 1024:
        raise ReleaseError("release JSON body must be at most 64 KiB")
    return payload


def _create_payload(payload: Any) -> dict:
    payload = _bounded_payload(payload)
    _keys(payload, {"idempotency_key", "name", "version", "environment", "owner", "items"}, "payload")
    result = {key: _text(payload.get(key), key, 200)
              for key in ("idempotency_key", "name", "version", "environment", "owner")}
    if result["environment"] not in ENVIRONMENTS:
        raise ReleaseError("environment must be dev, test, or prod")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ReleaseError("items must contain 1 to 100 entries")
    result["items"], seen = [], set()
    for raw in items:
        item = _object(raw, "item")
        _keys(item, {"item_key", "feature_key", "title", "component", "artifact", "task_ids", "facts"}, "item")
        entry = {key: _text(item.get(key), key, 300)
                 for key in ("item_key", "feature_key", "title", "component")}
        if entry["item_key"] in seen:
            raise ReleaseError("duplicate item_key")
        seen.add(entry["item_key"])
        artifact = _object(item.get("artifact"), "artifact")
        _keys(artifact, {"source_revision", "digest", "ops_revision"}, "artifact")
        entry["artifact"] = {key: _text(artifact.get(key, ""), "artifact." + key, 512, empty=True)
                             for key in ("source_revision", "digest", "ops_revision")}
        tasks = item.get("task_ids")
        if not isinstance(tasks, list) or len(tasks) > 100:
            raise ReleaseError("task_ids must be a list of at most 100 IDs")
        entry["task_ids"] = sorted({_text(task, "task_id", 200) for task in tasks})
        entry["facts"] = _facts(item.get("facts", {}))
        result["items"].append(entry)
    return result


def _append_payload(payload: Any) -> dict:
    payload = _bounded_payload(payload)
    _keys(payload, {"idempotency_key", "expected_revision", "items"}, "payload")
    revision = payload.get("expected_revision")
    if type(revision) is not int or revision < 1:
        raise ReleaseError("expected_revision must be a positive integer")
    result = {"idempotency_key": _text(payload.get("idempotency_key"), "idempotency_key", 200),
              "expected_revision": revision, "items": []}
    if not isinstance(payload.get("items"), list) or not 1 <= len(payload["items"]) <= 100:
        raise ReleaseError("items must contain 1 to 100 entries")
    seen = set()
    for raw in payload["items"]:
        item = _object(raw, "item")
        _keys(item, {"item_key", "facts"}, "receipt item")
        key = _text(item.get("item_key"), "item_key", 300)
        if key in seen:
            raise ReleaseError("duplicate item_key")
        seen.add(key)
        facts = _facts(item.get("facts"))
        if not facts:
            raise ReleaseError("receipt item facts must not be empty")
        result["items"].append({"item_key": key, "facts": facts})
    # Receipt deltas and task IDs are sets; their order is not idempotency data.
    result["items"].sort(key=lambda item: item["item_key"])
    return result


def _time(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            return None
        return instant.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _assessment(dimension: str, fact: dict, item: dict, environment: str,
                assessed_at: Optional[str]) -> dict:
    default = "not_tested" if dimension == "acceptance" else "unknown"
    status = fact.get("status", default)
    reasons = []
    if status == default:
        return {"state": default, "reasons": [dimension + "." + default], "assurance": ASSURANCE}

    def require(ok: bool, reason: str) -> None:
        if not ok:
            reasons.append(reason)

    # Registered facts keep their original receipt's temporal boundary. Reads,
    # exact retries and later receipts for other dimensions cannot mature a
    # future-dated declaration into a confirmed historical/current fact.
    boundary = _time(assessed_at)
    require(boundary is not None, "assessment_time.missing_or_invalid")
    observed = _time(fact.get("observed_at"))
    require(observed is not None, "observed_at.missing_or_invalid")
    if observed is not None and boundary is not None:
        require(observed <= boundary, "observed_at.in_future")
    source, review, proof = (fact.get(key, {}) for key in ("source", "review", "proof"))
    for key in ("kind", "ref"):
        require(_present(source.get(key)), "source." + key + ".missing")
    require(isinstance(source.get("sha256"), str) and bool(SHA256.fullmatch(source["sha256"])),
            "source.sha256.missing_or_invalid")
    for key in ("reviewer", "method"):
        require(_present(review.get(key)), "review." + key + ".missing")
    checked = _time(review.get("checked_at"))
    require(checked is not None, "review.checked_at.missing_or_invalid")
    if checked is not None:
        if boundary is not None:
            require(checked <= boundary, "review.checked_at.in_future")
        if observed is not None:
            require(checked >= observed, "review.checked_at.before_observation")
    for key, value in (("environment", environment), ("component", item["component"])):
        require(proof.get(key) == value, "proof." + key + ".missing_or_mismatch")

    if dimension == "deployment" and status == "deployed":
        require(proof.get("profile") == "cluster_release_v1", "proof.profile.unsupported_or_missing")
        for proof_key, artifact_key in (("source_revision", "source_revision"),
                                        ("artifact_digest", "digest"), ("ops_revision", "ops_revision")):
            value = item["artifact"][artifact_key]
            require(_present(value) and proof.get(proof_key) == value,
                    "proof." + proof_key + ".missing_or_mismatch")
        for key in ("release_ref", "terminal_ref"):
            require(_present(proof.get(key)), "proof." + key + ".missing")
        for key, value in (("release_result", "success"), ("terminal_state", "Success")):
            require(proof.get(key) == value, "proof." + key + ".not_success")
        require(proof.get("terminal_proven") is True, "proof.terminal_proven.not_true")

        def health(value: Any, prefix: str) -> None:
            if not isinstance(value, dict):
                reasons.append(prefix + ".missing")
                return
            for key, expected in HEALTH.items():
                require(value.get(key) == expected, prefix + "." + key + ".not_healthy")

        health(proof.get("health"), "proof.health")
        applicability = proof.get("siblings_applicable")
        require(type(applicability) is bool, "proof.siblings_applicable.missing_or_invalid")
        siblings = proof.get("siblings")
        if applicability is False:
            require(siblings == [], "proof.siblings.nonempty_or_missing_when_not_applicable")
        elif applicability is True:
            require(isinstance(siblings, list) and bool(siblings), "proof.siblings.missing_mapping")
            seen = set()
            for index, sibling in enumerate(siblings if isinstance(siblings, list) else []):
                prefix = "proof.siblings." + str(index)
                if not isinstance(sibling, dict):
                    reasons.append(prefix + ".invalid_mapping")
                    continue
                component = sibling.get("component")
                valid = _present(component) and component != item["component"] and component not in seen
                require(valid, prefix + ".component.missing_duplicate_or_self")
                if _present(component):
                    seen.add(component)
                require(sibling.get("environment") == environment, prefix + ".environment.mismatch")
                for key in ("source_revision", "artifact_digest", "ops_revision"):
                    require(_present(sibling.get(key)), prefix + "." + key + ".missing")
                health(sibling.get("health"), prefix + ".health")
    elif dimension == "deployment" and status == "rolled_back":
        for key in ("reason", "rollback_ref"):
            require(_present(proof.get(key)), "proof." + key + ".missing")
    elif dimension == "deployment" and status == "failed":
        require(_present(proof.get("reason")), "proof.reason.missing")
        require(_present(proof.get("release_ref")), "proof.release_ref.missing")
        require(proof.get("release_result") == "failed", "proof.release_result.not_failed")
    else:
        require(proof.get("feature_key") == item["feature_key"], "proof.feature_key.missing_or_mismatch")
        if dimension == "enablement":
            require(type(proof.get("enabled")) is bool and proof["enabled"] == (status == "enabled"),
                    "proof.enabled.missing_or_mismatch")
        else:
            require(proof.get("result") == status, "proof.result.missing_or_mismatch")
            for key in ("round_id", "method"):
                require(_present(proof.get(key)), "proof." + key + ".missing")
            if proof.get("method") == "fresh_character_e2e":
                require(_present(proof.get("new_character_id")), "proof.new_character_id.missing")
    state = "confirmed" if dimension == "deployment" and status == "deployed" else status
    return {"state": "reported" if reasons else state, "reasons": reasons, "assurance": ASSURANCE}


def _project(batch: dict, items: list, history: list,
             preview_at: Optional[str] = None) -> dict:
    items = json.loads(_json(items))
    by_key = {item["item_key"]: item for item in items}
    released = []
    # Only the explicit preview caller may provide a current-time fallback.
    # Damaged or legacy persisted facts must never borrow batch/read time.
    default_assessed_at = preview_at
    fact_received_at = {item["item_key"]: {} for item in items}
    for item in items:
        item.setdefault("facts", {})
    for receipt in history:
        for delta in receipt["items"]:
            by_key[delta["item_key"]]["facts"].update(delta["facts"])
            for dimension in delta["facts"]:
                fact_received_at[delta["item_key"]][dimension] = receipt.get("received_at")
            deployment = delta["facts"].get("deployment", {})
            if _assessment("deployment", deployment, by_key[delta["item_key"]],
                           batch["environment"], receipt.get("received_at"))["state"] == "confirmed":
                released.append(_time(deployment["observed_at"]))
    counts = {key: 0 for key in ("total", "deployed", "enabled", "accepted", "rolled_back", "unverified")}
    for item in items:
        item["assessment"] = {dimension: _assessment(dimension, item["facts"].get(dimension, {}),
                                                    item, batch["environment"],
                                                    fact_received_at[item["item_key"]].get(dimension, default_assessed_at))
                              for dimension in STATUSES}
        item["assurance"] = ASSURANCE
        deployment = item["assessment"]["deployment"]["state"]
        counts["total"] += 1
        counts["deployed"] += deployment == "confirmed"
        counts["enabled"] += item["assessment"]["enablement"]["state"] == "enabled"
        counts["accepted"] += item["assessment"]["acceptance"]["state"] == "passed"
        counts["rolled_back"] += deployment == "rolled_back"
        counts["unverified"] += deployment in {"unknown", "reported"}
        if deployment == "confirmed":
            released.append(_time(item["facts"]["deployment"]["observed_at"]))
    if counts["deployed"] == counts["total"]:
        status = "released"
    elif counts["deployed"]:
        status = "partial"
    elif counts["rolled_back"] == counts["total"]:
        status = "rolled_back"
    else:
        status = "unverified"
    return {**batch, "schema_version": 1, "status": status, "state": status,
            "released_at": max(released).isoformat() if released else None,
            "counts": counts, "items": items, "history": history, "assurance": ASSURANCE}


class ReleaseService:
    def __init__(self, connect: Callable):
        self.connect = connect

    def _tasks_exist(self, connection: Any, items: list) -> None:
        ids = sorted({task for item in items for task in item["task_ids"]})
        if not ids:
            return
        if not _rows(connection, "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"):
            raise ReleaseError("task mapping requires an existing tasks table", "missing", 404)
        for task_id in ids:
            if not _rows(connection, "SELECT id FROM tasks WHERE id=?", (task_id,)):
                raise ReleaseError("task not found: " + task_id, "missing", 404)

    def _batch(self, connection: Any, batch_id: str) -> dict:
        rows = _rows(connection, "SELECT id,name,version,environment,owner,recorded_at,revision "
                     "FROM release_batches WHERE id=?", (batch_id,))
        if not rows:
            raise ReleaseError("release batch not found", "missing", 404)
        items = _rows(connection, "SELECT item_key,feature_key,title,component,artifact_json "
                      "FROM release_items WHERE batch_id=? ORDER BY ordinal", (batch_id,))
        mappings = _rows(connection, "SELECT item_key,task_id FROM release_item_tasks "
                         "WHERE batch_id=? ORDER BY task_id", (batch_id,))
        for item in items:
            item["artifact"] = json.loads(item.pop("artifact_json"))
            item["task_ids"] = [row["task_id"] for row in mappings if row["item_key"] == item["item_key"]]
        history = _rows(connection, "SELECT id,revision,idempotency_key,received_at,items_json "
                        "FROM release_receipts WHERE batch_id=? ORDER BY revision", (batch_id,))
        for receipt in history:
            receipt["items"] = json.loads(receipt.pop("items_json"))
            receipt["assurance"] = ASSURANCE
        return _project(rows[0], items, history)

    def get_batch(self, batch_id: str) -> dict:
        batch_id = _text(batch_id, "batch_id", 200)
        with self.connect() as connection:
            connection.execute("BEGIN")
            return self._batch(connection, batch_id)

    def list_batches(self, environment: Any = None, task_id: Any = None) -> dict:
        if environment is not None and (not isinstance(environment, str) or environment not in ENVIRONMENTS):
            raise ReleaseError("environment must be dev, test, or prod")
        if task_id is not None:
            task_id = _text(task_id, "task_id", 200)
        with self.connect() as connection:
            connection.execute("BEGIN")
            revision = _rows(connection, "SELECT revision FROM release_schema_version WHERE singleton=1")[0]["revision"]
            clauses, args = [], []
            if environment is not None:
                clauses.append("environment=?")
                args.append(environment)
            if task_id is not None:
                clauses.append("EXISTS (SELECT 1 FROM release_item_tasks m WHERE m.batch_id=b.id AND m.task_id=?)")
                args.append(task_id)
            rows = _rows(connection, "SELECT id FROM release_batches b" +
                         (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(args))
            batches = [self._batch(connection, row["id"]) for row in rows]
            batches.sort(key=lambda batch: (batch["released_at"] or batch["recorded_at"], batch["recorded_at"], batch["id"]), reverse=True)
            return {"schema_version": 1, "revision": revision, "batches": batches,
                    "total": len(batches), "assurance": ASSURANCE}

    def preview(self, payload: dict) -> dict:
        data = _create_payload(payload)
        with self.connect() as connection:
            connection.execute("BEGIN")
            self._tasks_exist(connection, data["items"])
        batch = {key: data[key] for key in ("name", "version", "environment", "owner")}
        return {**_project({**batch, "id": None, "recorded_at": None, "revision": 0},
                           data["items"], [], preview_at=_now()),
                "preview": True}

    def _record(self, connection: Any, batch_id: str, revision: int, data: dict, received_at: str) -> None:
        deltas = [{"item_key": item["item_key"], "facts": item["facts"]} for item in data["items"]]
        connection.execute("INSERT INTO release_receipts "
                           "(id,batch_id,revision,idempotency_key,payload_hash,received_at,items_json) "
                           "VALUES(?,?,?,?,?,?,?)", ("rr-" + uuid.uuid4().hex, batch_id, revision,
                           data["idempotency_key"], _hash(data), received_at, _json(deltas)))
        connection.execute("UPDATE release_schema_version SET revision=revision+1 WHERE singleton=1")

    def create(self, payload: dict) -> dict:
        data = _create_payload(payload)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = _rows(connection, "SELECT id,payload_hash FROM release_batches WHERE idempotency_key=?",
                             (data["idempotency_key"],))
            if existing:
                if existing[0]["payload_hash"] != _hash(data):
                    raise ReleaseError("idempotency key belongs to a different payload", "idempotency", 409)
                return {**self._batch(connection, existing[0]["id"]), "reused": True}
            self._tasks_exist(connection, data["items"])
            batch_id, recorded_at = "rb-" + uuid.uuid4().hex, _now()
            connection.execute("INSERT INTO release_batches "
                               "(id,idempotency_key,payload_hash,name,version,environment,owner,recorded_at,revision) "
                               "VALUES(?,?,?,?,?,?,?,?,1)", (batch_id, data["idempotency_key"], _hash(data),
                               data["name"], data["version"], data["environment"], data["owner"], recorded_at))
            for ordinal, item in enumerate(data["items"]):
                connection.execute("INSERT INTO release_items "
                                   "(batch_id,item_key,ordinal,feature_key,title,component,artifact_json) "
                                   "VALUES(?,?,?,?,?,?,?)", (batch_id, item["item_key"], ordinal, item["feature_key"],
                                   item["title"], item["component"], _json(item["artifact"])))
                for task_id in item["task_ids"]:
                    connection.execute("INSERT INTO release_item_tasks(batch_id,item_key,task_id) VALUES(?,?,?)",
                                       (batch_id, item["item_key"], task_id))
            self._record(connection, batch_id, 1, data, recorded_at)
            return {**self._batch(connection, batch_id), "reused": False}

    def append_receipts(self, batch_id: str, payload: dict) -> dict:
        batch_id, data = _text(batch_id, "batch_id", 200), _append_payload(payload)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._batch(connection, batch_id)
            existing = _rows(connection, "SELECT payload_hash FROM release_receipts WHERE batch_id=? AND idempotency_key=?",
                             (batch_id, data["idempotency_key"]))
            # Exact retries remain successful after the batch advances. A new
            # payload (including a changed expected_revision) is a conflict.
            if existing:
                if existing[0]["payload_hash"] != _hash(data):
                    raise ReleaseError("idempotency key belongs to a different receipt", "idempotency", 409)
                return {**batch, "reused": True}
            if batch["revision"] != data["expected_revision"]:
                raise ReleaseError("stale release batch revision", "stale", 409)
            keys = {item["item_key"] for item in batch["items"]}
            for item in data["items"]:
                if item["item_key"] not in keys:
                    raise ReleaseError("release item not found: " + item["item_key"], "missing", 404)
            changed = connection.execute("UPDATE release_batches SET revision=revision+1 WHERE id=? AND revision=?",
                                         (batch_id, data["expected_revision"])).rowcount
            if changed != 1:
                raise ReleaseError("stale release batch revision", "stale", 409)
            self._record(connection, batch_id, batch["revision"] + 1, data, _now())
            return {**self._batch(connection, batch_id), "reused": False}
