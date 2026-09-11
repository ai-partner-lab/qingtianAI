"""Recorded-source operations projection; no execution or native description writes."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from .transactions import transaction_scope

ASSURANCE = "recorded_sources_not_independent_verification"
COMPLETION_ASSURANCE = "task_evidence_gate_not_authorization_deployment_or_functional_acceptance"
CATEGORIES = {
    "executing": "正在执行", "internal": "内部处理", "pending_release": "待发布",
    "user_action": "需要你处理", "external_blocked": "外部阻塞", "deferred": "暂缓处理",
    "unclassified": "待核实分类", "history": "历史记录",
}
SCOPE_KEYS = {"project_id", "environment", "target", "authorization_scope"}
# Public human-action CAS owns action_revision separately. Its native trigger
# also advances on heartbeat writes, which must not invalidate completion facts.
VOLATILE = {"updated_at", "heartbeat_at", "action_revision"}
TRIGGERS = {
    "operations_native_insert", "operations_native_update", "operations_native_delete",
    "operations_correction_no_update", "operations_correction_no_delete",
    "operations_correction_no_replace",
    "operations_report_no_update", "operations_report_no_delete", "operations_report_no_replace",
}


class OperationsError(ValueError):
    def __init__(self, message, code="invalid", status=400):
        super().__init__(message)
        self.code, self.status = code, status

    def payload(self):
        return {"error": str(self), "code": self.code, "assurance": ASSURANCE}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _text(value, limit=4000, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise OperationsError("invalid bounded text")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise OperationsError("invalid identifier")
    return value


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise OperationsError("invalid object fields")
    return value


def _positive(value):
    if type(value) is not int or value < 1 or value >= 2 ** 63:
        raise OperationsError("invalid revision")
    return value


def _time(value):
    _text(value, 80)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise OperationsError("invalid timezone-aware observation time") from exc


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise OperationsError("invalid SHA256")
    return value


def native_basis(task):
    return {key: value for key, value in task.items() if key not in VOLATILE}


def initialize_operations_schema(connection):
    """Add only operations tables/triggers, under an atomic savepoint."""
    connection.execute("SAVEPOINT operations_migration")
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS operations_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        version = connection.execute("SELECT value FROM operations_meta WHERE key='schema_version'").fetchone()
        if version and version[0] != "1":
            raise sqlite3.DatabaseError("unsupported operations schema")
        connection.execute("""CREATE TABLE IF NOT EXISTS operations_task_versions (
            task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
            basis_revision INTEGER NOT NULL CHECK(basis_revision>0 AND basis_revision<=revision),
            present INTEGER NOT NULL CHECK(present IN (0,1)), native_basis_json TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS operations_description_projection (
            task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, basis_revision INTEGER NOT NULL,
            native_basis_json TEXT NOT NULL, overrides_json TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS operations_corrections (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL, received_at TEXT NOT NULL, payload_json TEXT NOT NULL,
            old_values_json TEXT NOT NULL, new_values_json TEXT NOT NULL,
            actor_json TEXT NOT NULL, source_json TEXT NOT NULL, correction_reason TEXT NOT NULL,
            native_baseline_json TEXT NOT NULL,
            UNIQUE(task_id,idempotency_key), UNIQUE(task_id,revision))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS operations_human_action_reports (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL, received_at TEXT NOT NULL,
            payload_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
            UNIQUE(task_id,idempotency_key), UNIQUE(task_id,revision))""")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(tasks)") if row[1] not in VOLATILE]
        if not {"id", "state", "blocking_reason", "action_text"} <= set(columns):
            raise sqlite3.DatabaseError("native tasks schema unavailable")
        # Column names originate only in the native schema, never a request.
        def basis_sql(prefix):
            return "json_object(" + ",".join("'" + c.replace("'", "''") + "'," + prefix + '."' + c.replace('"', '""') + '"' for c in columns) + ")"

        changed = " OR ".join('NEW."' + c + '" IS NOT OLD."' + c + '"' for c in columns)
        new_basis = basis_sql("NEW")
        upsert = """ON CONFLICT(task_id) DO UPDATE SET
            revision=operations_task_versions.revision+1,
            basis_revision=operations_task_versions.revision+1,
            present=1,native_basis_json=excluded.native_basis_json"""
        statements = [
            "CREATE TRIGGER IF NOT EXISTS operations_native_insert AFTER INSERT ON tasks BEGIN "
            "INSERT INTO operations_task_versions VALUES(NEW.id,1,1,1," + new_basis + ") " + upsert + "; END",
            "CREATE TRIGGER IF NOT EXISTS operations_native_delete AFTER DELETE ON tasks BEGIN "
            "UPDATE operations_task_versions SET revision=revision+1,basis_revision=revision+1,present=0 WHERE task_id=OLD.id; END",
            "CREATE TRIGGER IF NOT EXISTS operations_native_update AFTER UPDATE OF " +
            ",".join('"' + row[1] + '"' for row in connection.execute("PRAGMA table_info(tasks)") if row[1] != "action_revision") + " ON tasks BEGIN "
            "UPDATE operations_task_versions SET revision=revision+1,basis_revision=revision+1,present=0 "
            "WHERE task_id=OLD.id AND OLD.id IS NOT NEW.id; "
            "INSERT INTO operations_task_versions SELECT NEW.id,1,1,1," + new_basis +
            " WHERE OLD.id IS NOT NEW.id " + upsert + "; "
            "UPDATE operations_task_versions SET revision=revision+1,basis_revision=CASE WHEN " + changed +
            " THEN revision+1 ELSE basis_revision END,native_basis_json=" + new_basis +
            ",present=1 WHERE task_id=NEW.id AND OLD.id IS NEW.id; END",
            "CREATE TRIGGER IF NOT EXISTS operations_correction_no_update BEFORE UPDATE ON operations_corrections "
            "BEGIN SELECT RAISE(ABORT,'immutable operations correction'); END",
            "CREATE TRIGGER IF NOT EXISTS operations_correction_no_delete BEFORE DELETE ON operations_corrections "
            "BEGIN SELECT RAISE(ABORT,'immutable operations correction'); END",
            "CREATE TRIGGER IF NOT EXISTS operations_correction_no_replace BEFORE INSERT ON operations_corrections "
            "WHEN EXISTS(SELECT 1 FROM operations_corrections WHERE id=NEW.id OR "
            "(task_id=NEW.task_id AND (idempotency_key=NEW.idempotency_key OR revision=NEW.revision))) "
            "BEGIN SELECT RAISE(ABORT,'immutable operations correction identity'); END",
        ]
        for statement in statements:
            connection.execute(statement)
        for verb in ("UPDATE", "DELETE"):
            connection.execute("CREATE TRIGGER IF NOT EXISTS operations_report_no_" + verb.lower() +
                               " BEFORE " + verb + " ON operations_human_action_reports "
                               "BEGIN SELECT RAISE(ABORT,'immutable ordinary action report'); END")
        connection.execute("CREATE TRIGGER IF NOT EXISTS operations_report_no_replace BEFORE INSERT ON operations_human_action_reports "
                           "WHEN EXISTS(SELECT 1 FROM operations_human_action_reports WHERE id=NEW.id OR "
                           "(task_id=NEW.task_id AND (idempotency_key=NEW.idempotency_key OR revision=NEW.revision))) "
                           "BEGIN SELECT RAISE(ABORT,'immutable ordinary action report identity'); END")
        if version is None:
            connection.execute("INSERT INTO operations_task_versions SELECT tasks.id,1,1,1," + basis_sql("tasks") +
                               " FROM tasks WHERE true ON CONFLICT(task_id) DO NOTHING")
            connection.execute("INSERT INTO operations_meta VALUES('schema_version','1')")
        connection.execute("RELEASE operations_migration")
    except Exception:
        connection.execute("ROLLBACK TO operations_migration")
        connection.execute("RELEASE operations_migration")
        raise


def _snapshot(connection):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for name, table in {
        "tasks": "tasks", "evidence": "evidence", "runs": "runs", "dependencies": "task_dependencies",
        "events": "events", "versions": "operations_task_versions", "corrections": "operations_corrections",
        "descriptions": "operations_description_projection", "operations_meta": "operations_meta",
        "reports": "operations_human_action_reports",
    }.items():
        if table not in tables:
            result[name] = []
            continue
        cursor = connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid')
        columns = [c[0] for c in cursor.description]
        result[name] = [dict(zip(columns, row)) for row in cursor]
    result["trigger_names"] = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
    return result


def completion_basis(task, snapshot, required=None):
    """Evidence categories and current structured constraints, not acceptance."""
    profile = str(task.get("evidence_profile") or "auto").lower()
    if required is None:
        effective = profile
        if effective == "auto":
            worker = str(task.get("worker_type") or "").lower()
            effective = worker if worker in {"browser", "qa"} else ("code" if task.get("repository") else "artifact")
        required = list({"legacy": ["migration_metadata"], "browser": ["browser"], "qa": ["test"],
                         "code": ["commit", "test"]}.get(effective, ["artifact"]))
        if task.get("requires_deploy"):
            required.extend(["deploy", "smoke"])
    present = sorted({row["kind"] for row in snapshot.get("evidence", [])
                      if row.get("task_id") == task["id"] and row.get("verified") == 1 and str(row.get("value") or "").strip()})
    missing = [kind for kind in required if kind not in present]
    actions = []
    if task.get("action_owner_kind") in {"user", "external"} or task.get("action_sensitive"):
        actions.append({"kind": task.get("action_owner_kind"), "owner": task.get("action_owner"),
                        "text": task.get("action_text"), "due": task.get("action_due"),
                        "sensitive": bool(task.get("action_sensitive")), "source": "native_action_columns"})
    if task.get("state") in {"PAUSED", "PLAN_ONLY", "CANCELED", "FAILED"} or task["id"] in snapshot.get("paused_task_ids", []):
        actions.append({"kind": "protected_task_state", "state": task.get("state"), "source": "tasks.state"})
    for run in snapshot.get("runs", []):
        if run.get("task_id") == task["id"] and run.get("status") in {"QUEUED", "RUNNING"}:
            actions.append({"kind": "active_run", "run_id": run["id"], "status": run["status"], "source": "runs.status"})
    tasks = {row["id"]: row for row in snapshot.get("tasks", [])}
    dependencies = []
    for dependency in snapshot.get("dependencies", []):
        if dependency.get("task_id") != task["id"]:
            continue
        target = tasks.get(dependency.get("depends_on_id"))
        # The inherited stored relation contract treats every dependency row as
        # required (the creator writes 'blocks'); no new optional type is guessed.
        if target is None or target.get("state") != "DONE":
            dependencies.append({**copy.deepcopy(dependency), "state": target.get("state") if target else None,
                                 "source": "task_dependencies"})
    last = None
    for event in sorted(snapshot.get("events", []), key=lambda e: e.get("id", 0)):
        if event.get("task_id") != task["id"] or event.get("event_type") != "task.state_changed":
            continue
        try:
            payload = json.loads(event.get("payload_json") or "{}")
            if isinstance(payload, dict) and payload.get("state") == "DONE":
                last = copy.deepcopy(event)
        except (ValueError, TypeError):
            pass
    return {"profile": profile, "required": list(required), "present": present, "missing": missing,
            "unresolved_actions": actions, "unresolved_dependencies": dependencies, "last_completion_event": last,
            "eligible": not (missing or actions or dependencies), "assurance": COMPLETION_ASSURANCE}


class ReviewedRecordAdapter:
    def __init__(self, records, *, admitted_at):
        _time(admitted_at)
        if not isinstance(records, (list, tuple)) or len(records) > 10000:
            raise OperationsError("invalid bounded record collection")
        self._records = copy.deepcopy(records)
        self.admitted_at = admitted_at

    def records(self):
        return copy.deepcopy(self._records)


def _scope(scope, complete=False):
    _shape(scope, SCOPE_KEYS if complete else (), () if complete else SCOPE_KEYS)
    for value in scope.values():
        _text(value, 256, empty=not complete)
    return scope


def _record(record, admission_time):
    required = {"task_id", "record_id", "received_at", "observed_at", "source", "basis_revision", "native_basis"}
    optional = {"classification", "completed", "blocker", "next_action", "owner", "meaningful_progress", "resolution"}
    _shape(record, required, optional)
    _id(record["task_id"])
    _id(record["record_id"])
    _positive(record["basis_revision"])
    if not isinstance(record["native_basis"], dict):
        raise OperationsError("missing exact native basis")
    source = _shape(record["source"], {"origin", "ref", "sha256", "reviewer", "reviewed_at"})
    _text(source["origin"], 128)
    _text(source["ref"], 1024)
    _text(source["reviewer"], 128)
    _sha(source["sha256"])
    observed, reviewed, received = _time(record["observed_at"]), _time(source["reviewed_at"]), _time(record["received_at"])
    if not observed <= reviewed <= received <= _time(admission_time):
        raise OperationsError("record exceeds fixed source/time boundary")
    if "classification" in record and record["classification"] not in set(CATEGORIES) - {"unclassified", "history"}:
        raise OperationsError("invalid reviewed classification")
    for key in ("completed", "next_action"):
        if key in record:
            _text(record[key])
    if "owner" in record:
        _shape(record["owner"], {"id", "name"})
        _id(record["owner"]["id"])
        _text(record["owner"]["name"], 256)
    if "blocker" in record:
        blocker = _shape(record["blocker"], {"text"}, {"blocker_key", "scope", "decision", "channel"})
        _text(blocker["text"])
        for key in ("blocker_key", "decision"):
            if key in blocker:
                _text(blocker[key], 256, empty=True)
        if "scope" in blocker:
            _scope(blocker["scope"])
        if "channel" in blocker:
            _shape(blocker["channel"], {"kind", "task_id"})
            if blocker["channel"]["kind"] != "task":
                raise OperationsError("only task navigation is allowed")
            _id(blocker["channel"]["task_id"])
    if "meaningful_progress" in record:
        progress = _shape(record["meaningful_progress"], {"kind", "ref", "occurred_at"})
        if progress["kind"] not in {"artifact_created", "test_completed", "review_completed", "decision_recorded", "work_completed"}:
            raise OperationsError("not an explicit meaningful progress kind")
        _text(progress["ref"], 1024)
        if _time(progress["occurred_at"]) > observed:
            raise OperationsError("progress exceeds record observation boundary")
    # Resolution incompleteness remains a visible pending cross-check, rather
    # than turning an unsupported claim into an automatic explanation change.
    if "resolution" in record and not isinstance(record["resolution"], dict):
        raise OperationsError("invalid resolution object")
    return record


def _warning(code, message, *sources):
    return {"code": code, "message": message, "sources": list(sources)}


def _source(record):
    return {**copy.deepcopy(record["source"]), "record_id": record["record_id"],
            "received_at": record["received_at"], "observed_at": record["observed_at"], "assurance": ASSURANCE}


def _field(value=None, source=None):
    return {"value": value if value != "" else None, "source": source}


def _native_source(task, field):
    return {"origin": "native_task", "ref": "tasks/" + str(task["id"]) + "/" + field,
            "observed_at": None, "assurance": ASSURANCE}


def _version_for(task, snapshot):
    candidates = [v for v in snapshot.get("versions", []) if v["task_id"] == task["id"]]
    if len(candidates) != 1:
        return None
    value = candidates[0]
    try:
        if value["present"] != 1 or _positive(value["basis_revision"]) > _positive(value["revision"]):
            return None
        if json.loads(value["native_basis_json"]) != native_basis(task):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    return value


def _resolution_match(record, records):
    try:
        resolution = _shape(record["resolution"], {"task_id", "blocker_key", "scope", "supersedes", "evidence"})
        _text(resolution["blocker_key"], 256)
        previous = _shape(resolution["supersedes"], {"record_id", "field", "value", "sha256", "source_ref", "observed_at"})
        evidence = _shape(resolution["evidence"], {"ref", "sha256", "observed_at"})
        _scope(resolution["scope"], complete=True)
        _text(evidence["ref"], 1024)
        _sha(evidence["sha256"])
        if _time(evidence["observed_at"]) > _time(record["observed_at"]):
            return None
        old = records.get(previous["record_id"])
        if not old or old["task_id"] != record["task_id"] or resolution["task_id"] != record["task_id"]:
            return None
        if previous["field"] not in {"blocker", "next_action"}:
            return None
        value = old.get("blocker", {}).get("text") if previous["field"] == "blocker" else old.get("next_action")
        if (not isinstance(value, str) or previous["value"] != value or
                _sha(previous["sha256"]) != hashlib.sha256(value.encode()).hexdigest() or
                previous["source_ref"] != old["source"]["ref"] or
                _time(previous["observed_at"]) != _time(old["observed_at"])):
            return None
        for candidate in (old, record):
            blocker = candidate.get("blocker", {})
            if blocker.get("blocker_key") != resolution["blocker_key"] or blocker.get("scope") != resolution["scope"]:
                return None
        if _time(evidence["observed_at"]) < _time(old["observed_at"]):
            return None
        return old
    except (KeyError, ValueError, TypeError):
        return None


def _lineage(task, tasks, evidence):
    task_id = task["id"]
    edges, incoming, sources = {}, {}, {}
    for item in evidence:
        kind = item.get("kind")
        if kind in {"successor", "predecessor"}:
            target = edges if kind == "successor" else incoming
            target.setdefault(item.get("task_id"), set()).add(item.get("value"))
            sources.setdefault(item.get("task_id"), []).append(copy.deepcopy(item))
    result = {"status": "none", "successor_task_id": None, "reason": "无已确认接续", "source": None}
    if not edges.get(task_id) and not incoming.get(task_id):
        return result
    result["source"] = {"origin": "native_evidence", "records": sources.get(task_id, []), "assurance": ASSURANCE}
    seen, current, first = set(), task_id, None
    while True:
        if current in seen:
            result.update(status="invalid", reason="接续存在循环")
            return result
        seen.add(current)
        successors, predecessors = edges.get(current, set()), incoming.get(current, set())
        if len(successors) > 1 or len(predecessors) > 1:
            result.update(status="ambiguous", reason="接续记录不唯一")
            return result
        if not successors:
            break
        target = next(iter(successors))
        if target == current:
            result.update(status="invalid", reason="任务不能接续自身")
            return result
        try:
            _id(target)
        except OperationsError:
            result.update(status="invalid", reason="接续目标标识无效")
            return result
        if target not in tasks:
            result.update(status="invalid", reason="接续目标不可用")
            return result
        if incoming.get(target, set()) != {current}:
            result.update(status="ambiguous" if len(incoming.get(target, set())) > 1 else "invalid", reason="缺少唯一相互对应的接续证据")
            return result
        if first is None:
            first = target
            result["source"]["records"] += sources.get(target, [])
        current = target
    if first is not None:
        result.update(status="valid", successor_task_id=first,
                      reason="已由新任务接续" if task.get("state") == "CANCELED" else "已有确认的接续任务")
    return result


def _audit(row):
    result = {k: row[k] for k in ("id", "task_id", "revision", "idempotency_key", "received_at", "correction_reason")}
    for key in ("old_values", "new_values", "actor", "source", "native_baseline"):
        result[key] = json.loads(row[key + "_json"])
    return result


def project_operations(snapshot, records=(), *, admission_time=None):
    """Pure, conservative projection. Never consults wall-clock or runtime state."""
    tasks = {t["id"]: t for t in snapshot.get("tasks", [])}
    candidates, invalid, by_id = {}, {}, {}
    for record in records:
        task_id = record.get("task_id") if isinstance(record, dict) else None
        if not isinstance(task_id, str) or task_id not in tasks:
            continue
        try:
            _record(record, admission_time)
            if record["record_id"] in by_id and by_id[record["record_id"]] != record:
                other = by_id[record["record_id"]]
                invalid.setdefault(other["task_id"], []).append(_warning("source_conflict", "记录标识对应多个来源", other))
                raise OperationsError("conflicting record identity")
            by_id[record["record_id"]] = record
            candidates.setdefault(task_id, {})[record["record_id"]] = record
        except (ValueError, TypeError, KeyError):
            invalid.setdefault(task_id, []).append(_warning("invalid_source", "来源或时间不完整，待核对", copy.deepcopy(record)))
    projected, grouping = [], []
    for task_id, task in tasks.items():
        version = _version_for(task, snapshot)
        warnings = list(invalid.get(task_id, []))
        if version is None:
            warnings.append(_warning("bookkeeping_missing", "版本记录缺失或损坏，暂不可更正"))
        all_records = candidates.get(task_id, {})
        superseded = set()
        for candidate in all_records.values():
            if "resolution" in candidate:
                old = _resolution_match(candidate, all_records)
                if old is not None and old["record_id"] != candidate["record_id"]:
                    superseded.add(old["record_id"])
                    warnings.append(_warning("old_explanation_needs_review", "旧说明需重新核对；原始事实已保留", copy.deepcopy(old), copy.deepcopy(candidate)))
                else:
                    warnings.append(_warning("resolution_pending_cross_check", "解除证据不完整，待交叉核对", copy.deepcopy(candidate)))
        heads = [r for key, r in all_records.items() if key not in superseded]
        record = heads[0] if len(heads) == 1 and not invalid.get(task_id) else None
        if all_records and len(heads) != 1:
            warnings.append(_warning("source_conflict", "当前记录冲突或接续不明确，待核对", *copy.deepcopy(list(all_records.values()))))
        if record and (version is None or record["native_basis"] != native_basis(task) or record["basis_revision"] != version["basis_revision"]):
            warnings.append(_warning("stale_source", "来源对应旧原始事实，需重新核对", copy.deepcopy(record)))
            record = None
        category = record.get("classification", "unclassified") if record else "unclassified"
        classification_source = _source(record) if record and "classification" in record else None
        if task.get("state") in {"PAUSED", "PLAN_ONLY"}:
            category, classification_source = "deferred", _native_source(task, "state")
        elif task_id in snapshot.get("paused_task_ids", []):
            category = "deferred"
            classification_source = {"origin": "existing_pause_policy", "ref": "qingtian_engine.service.is_paused_by_user",
                                     "task_id": task_id, "observed_at": None, "assurance": ASSURANCE}
        elif task.get("state") in {"DONE", "CANCELED"} and category == "unclassified":
            category, classification_source = "history", _native_source(task, "state")
        item = {
            "id": task_id, "title": task.get("title", ""), "state": task.get("state"),
            "revision": version["revision"] if version else None,
            "category": category, "category_label": CATEGORIES[category],
            "urgent": category in {"user_action", "external_blocked"} and task.get("state") not in {"DONE", "CANCELED", "PAUSED", "PLAN_ONLY"},
            "classification_source": classification_source,
            "completed": _field(),
            "blocker": _field(task.get("blocking_reason") or None, _native_source(task, "blocking_reason")),
            "next_action": _field(task.get("action_text") or None, _native_source(task, "action_text")),
            "owner": _field(), "meaningful_progress": _field(),
            "lineage": _lineage(task, tasks, snapshot.get("evidence", [])), "warnings": warnings,
            "legacy": {key: task.get(key) for key in ("blocking_reason", "action_text", "created_at", "updated_at", "heartbeat_at")},
            "corrections": [_audit(r) for r in snapshot.get("corrections", []) if r["task_id"] == task_id],
            "completion_basis": completion_basis(task, snapshot, snapshot.get("required_by_task", {}).get(task_id)),
        }
        item["legacy"].update(blocking_reason_at=None, action_text_at=None,
                              evidence=[copy.deepcopy(e) for e in snapshot.get("evidence", []) if e.get("task_id") == task_id],
                              events=[copy.deepcopy(e) for e in snapshot.get("events", []) if e.get("task_id") == task_id])
        if record:
            source = _source(record)
            for field in ("completed", "next_action"):
                if field in record:
                    item[field] = _field(record[field], copy.deepcopy(source))
            if "blocker" in record:
                item["blocker"] = _field(record["blocker"]["text"], copy.deepcopy(source))
            if "owner" in record:
                item["owner"] = _field(record["owner"]["name"], {**copy.deepcopy(source), "owner_id": record["owner"]["id"]})
            if "meaningful_progress" in record:
                item["meaningful_progress"] = _field(record["meaningful_progress"]["occurred_at"], {**copy.deepcopy(source), **record["meaningful_progress"]})
        description = next((d for d in snapshot.get("descriptions", []) if d["task_id"] == task_id), None)
        if description:
            if not version or description["basis_revision"] != version["basis_revision"] or json.loads(description["native_basis_json"]) != native_basis(task):
                warnings.append(_warning("stale_correction", "更正需重新核对；已显示新原始事实", *item["corrections"][-1:]))
                item["blocker"] = _field(task.get("blocking_reason") or None, _native_source(task, "blocking_reason"))
                item["next_action"] = _field(task.get("action_text") or None, _native_source(task, "action_text"))
            else:
                history = {a["id"]: a for a in item["corrections"]}
                for name, override in json.loads(description["overrides_json"]).items():
                    correction = history.get(override["correction_id"])
                    if correction:
                        item["blocker" if name == "reason" else "next_action"] = _field(override["value"], {
                            **copy.deepcopy(correction["source"]), "origin": "declared_display_correction", "correction_id": correction["id"],
                            "actor": correction["actor"], "received_at": correction["received_at"], "assurance": ASSURANCE,
                        })
        projected.append(item)
        if record and category == "external_blocked":
            blocker = record.get("blocker", {})
            try:
                _text(blocker.get("blocker_key"), 256)
                _scope(blocker.get("scope"), complete=True)
                _text(blocker.get("decision"), 256)
                channel = blocker.get("channel", {})
                if channel.get("kind") != "task" or _id(channel.get("task_id")) not in tasks:
                    raise OperationsError("missing task channel")
                grouping.append((item, blocker))
            except OperationsError:
                warnings.append(_warning("ungrouped_scope", "阻塞范围或处理入口不完整，单独显示", _source(record)))
    buckets = {}
    for item, blocker in grouping:
        key = _json([blocker["blocker_key"], blocker["scope"]])
        buckets.setdefault(key, []).append((item, blocker))
    groups = []
    for key, entries in buckets.items():
        handles = {_json([b["decision"], b["channel"]]) for _, b in entries}
        if len(handles) != 1:
            for item, _ in entries:
                item["warnings"].append(_warning("group_conflict", "相同范围的决策或处理入口冲突，单独显示"))
        elif len(entries) > 1:
            blocker = entries[0][1]
            groups.append({"id": "scope-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                           "blocker_key": blocker["blocker_key"], "scope": copy.deepcopy(blocker["scope"]),
                           "decision": blocker["decision"], "channel": copy.deepcopy(blocker["channel"]),
                           "task_ids": sorted(i["id"] for i, _ in entries)})
    return {"schema_version": 1, "assurance": ASSURANCE,
            "source_status": "reviewed_adapter" if admission_time is not None else "adapter_not_configured",
            "tasks": projected, "groups": groups,
            "counts": {category: sum(t["category"] == category for t in projected) for category in CATEGORIES},
            "published": {"source": "/api/release-batches", "count": None}}


class OperationsClarityService:
    def __init__(self, connect, record_adapter=None, required_evidence=None, paused_predicate=None):
        if record_adapter is not None and not isinstance(record_adapter, ReviewedRecordAdapter):
            raise OperationsError("an explicit reviewed record adapter is required")
        self.connect, self.record_adapter = connect, record_adapter
        self.required_evidence = required_evidence
        self.paused_predicate = paused_predicate

    def snapshot_from(self, connection):
        snapshot = _snapshot(connection)
        if self.required_evidence:
            snapshot["required_by_task"] = {t["id"]: self.required_evidence(t) for t in snapshot["tasks"]}
        if self.paused_predicate:
            snapshot["paused_task_ids"] = [t["id"] for t in snapshot["tasks"] if self.paused_predicate(t)]
        return snapshot

    def snapshot(self):
        with self.connect() as connection, transaction_scope(connection):
            return self.snapshot_from(connection)

    def _project(self, snapshot):
        adapter = self.record_adapter
        return project_operations(snapshot, adapter.records() if adapter else (),
                                  admission_time=adapter.admitted_at if adapter else None)

    def list_tasks(self):
        return self._project(self.snapshot())

    def get_task(self, task_id):
        _id(task_id)
        return self._find(self.list_tasks(), task_id)

    @staticmethod
    def _find(projected, task_id):
        for task in projected["tasks"]:
            if task["id"] == task_id:
                return task
        raise OperationsError("task not found", "missing", 404)

    @staticmethod
    def _body(payload):
        _shape(payload, {"idempotency_key", "expected_revision", "changes", "correction_reason", "actor", "source"})
        _id(payload["idempotency_key"])
        _positive(payload["expected_revision"])
        changes = _shape(payload["changes"], (), {"reason", "next_action"})
        if not changes:
            raise OperationsError("one or both description fields required")
        for value in changes.values():
            _text(value, empty=True)
        _text(payload["correction_reason"], 2000)
        actor = _shape(payload["actor"], {"id", "origin"})
        _text(actor["id"], 128)
        _text(actor["origin"], 128)
        source = _shape(payload["source"], {"ref", "sha256", "observed_at", "reviewed_at", "reviewer"})
        _text(source["ref"], 1024)
        _text(source["reviewer"], 128)
        _sha(source["sha256"])
        _time(source["observed_at"])
        _time(source["reviewed_at"])
        return _json(payload)

    @staticmethod
    def _bookkeeping(snapshot, task):
        version = _version_for(task, snapshot)
        meta = {v["key"]: v["value"] for v in snapshot.get("operations_meta", [])}
        if not version or meta.get("schema_version") != "1" or not TRIGGERS <= set(snapshot.get("trigger_names", [])):
            raise OperationsError("task bookkeeping missing or damaged", "stale", 409)
        audits = [r for r in snapshot["corrections"] if r["task_id"] == task["id"]]
        description = next((r for r in snapshot["descriptions"] if r["task_id"] == task["id"]), None)
        if (audits and (not description or max(r["revision"] for r in audits) != description["revision"])) or (description and not audits):
            raise OperationsError("description bookkeeping damaged", "stale", 409)
        if audits and max(r["revision"] for r in audits) > version["revision"]:
            raise OperationsError("revision is behind audit", "stale", 409)
        reports = [r for r in snapshot.get("reports", []) if r["task_id"] == task["id"]]
        if reports and max(r["revision"] for r in reports) > version["revision"]:
            raise OperationsError("revision is behind action report", "stale", 409)
        return version

    def preview_correction(self, task_id, payload):
        return self._correction(task_id, payload, preview=True)

    def apply_correction(self, task_id, payload):
        return self._correction(task_id, payload, preview=False)

    def _correction(self, task_id, payload, *, preview):
        _id(task_id)
        canonical = self._body(payload)
        with self.connect() as connection, transaction_scope(connection, write=not preview):
            snapshot = self.snapshot_from(connection)
            native = next((t for t in snapshot["tasks"] if t["id"] == task_id), None)
            if native is None:
                raise OperationsError("task not found", "missing", 404)
            existing = next((r for r in snapshot["corrections"] if r["task_id"] == task_id and r["idempotency_key"] == payload["idempotency_key"]), None)
            if existing:
                if existing["payload_json"] != canonical:
                    raise OperationsError("idempotency key already has different content", "idempotency", 409)
                # Exact retries precede CAS and never refresh received/review time.
                result = {"task": self._find(self._project(snapshot), task_id), "correction": _audit(existing), "reused": True}
                if preview:
                    result["preview"] = True
                return result
            version = self._bookkeeping(snapshot, native)
            if payload["expected_revision"] != version["revision"]:
                raise OperationsError("task revision changed; review again", "stale", 409)
            received = datetime.now(timezone.utc).isoformat()
            if not _time(payload["source"]["observed_at"]) <= _time(payload["source"]["reviewed_at"]) <= _time(received):
                raise OperationsError("correction exceeds fixed received time")
            task = self._find(self._project(snapshot), task_id)
            correction = {
                "id": None if preview else str(uuid.uuid4()), "task_id": task_id,
                "revision": version["revision"] + 1, "idempotency_key": payload["idempotency_key"], "received_at": received,
                "old_values": {key: task["blocker" if key == "reason" else "next_action"]["value"] for key in payload["changes"]},
                "new_values": copy.deepcopy(payload["changes"]), "actor": copy.deepcopy(payload["actor"]),
                "source": copy.deepcopy(payload["source"]), "correction_reason": payload["correction_reason"],
                "native_baseline": {"revision": version["revision"], "basis_revision": version["basis_revision"], "native": native_basis(native)},
            }
            if preview:
                return {"task": task, "correction": correction, "reused": False, "preview": True}
            changed = connection.execute("UPDATE operations_task_versions SET revision=revision+1 WHERE task_id=? AND revision=? AND present=1",
                                         (task_id, version["revision"]))
            if changed.rowcount != 1:
                raise OperationsError("task revision changed", "stale", 409)
            connection.execute("""INSERT INTO operations_corrections (
                id,task_id,revision,idempotency_key,received_at,payload_json,old_values_json,new_values_json,
                actor_json,source_json,correction_reason,native_baseline_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (correction["id"], task_id, correction["revision"], correction["idempotency_key"], received, canonical,
                 _json(correction["old_values"]), _json(correction["new_values"]), _json(correction["actor"]),
                 _json(correction["source"]), correction["correction_reason"], _json(correction["native_baseline"])))
            old_projection = next((p for p in snapshot["descriptions"] if p["task_id"] == task_id), None)
            overrides = json.loads(old_projection["overrides_json"]) if old_projection and old_projection["basis_revision"] == version["basis_revision"] else {}
            for key, value in payload["changes"].items():
                if value:
                    overrides[key] = {"value": value, "correction_id": correction["id"]}
                else:
                    overrides.pop(key, None)
            connection.execute("""INSERT INTO operations_description_projection VALUES(?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,basis_revision=excluded.basis_revision,
                native_basis_json=excluded.native_basis_json,overrides_json=excluded.overrides_json""",
                (task_id, correction["revision"], version["basis_revision"], _json(native_basis(native)), _json(overrides)))
            result = self._find(self._project(self.snapshot_from(connection)), task_id)
            return {"task": result, "correction": correction, "reused": False}
