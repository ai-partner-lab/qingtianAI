"""Durable local dispatch receipts. No process, network or scheduling on reads.

The dispatcher is deliberately outside the database transaction. A reservation
without a confirmation is never retried automatically: external effects cannot
be rolled back by SQLite. Human/host identity is not inferred from a role label.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .db import utc_now
from .execution_policy import execution_forbidden
from .operations_clarity import native_basis, _version_for
from .service import is_paused_by_user

ASSURANCE = "local_persisted_admission_not_execution_progress_or_external_host_ack"
TARGET_FIELDS = ("model", "reasoning", "speed", "worker_type", "owner_session", "branch", "worktree")
REASONS = {
    "task_saved_no_dispatch_receipt": ("manager", "审阅范围；需要执行时显式派发", "明确执行授权和当前版本"),
    "dispatch_confirmation_pending": ("operator", "核查原请求及真实运行；不要重复启动", "原请求确认或人工核对运行记录"),
    "local_run_recorded": ("local_runtime", "查看已绑定运行的真实事件", "运行提供实质事件；心跳不算进展"),
    "active_run_reused": ("local_runtime", "查看同范围已存在运行；未重复派发", "原运行产生结果"),
    "dependency_unresolved": ("dependency_owner", "查看列出的依赖任务；本入口不代为完成", "依赖完成后刷新并重新审阅；不会自动恢复"),
    "user_action_pending": ("user", "按当前任务原处理渠道完成事项", "当前用户事项已按原渠道解决并重新审阅"),
    "external_action_pending": ("external", "联系当前登记的外部责任人", "外部事项有新事实后刷新审阅"),
    "user_paused": ("user", "保持暂停；需用户另行决定", "另行明确恢复授权；本入口不恢复暂停"),
    "task_terminal": ("manager", "查看终态历史；不重新启动", "另行创建获授权的新任务"),
    "execution_forbidden": ("manager", "仅查看；如需实施另建获授权任务", "新的独立执行授权"),
    "sensitive_approval_required": ("authorized_approver", "使用原范围的敏感审批渠道", "正式审批事实；本回执不是审批"),
    "release_scope_forbidden": ("release_owner", "转交正式发布控制面", "独立发布授权与既有门禁"),
    "dispatch_result_unknown": ("operator", "按原key核查运行/派发日志；勿更换key重启", "确认原请求是否产生真实运行；不得凭异常推定未执行"),
    "external_ack_not_integrated": ("host_owner", "核查外部宿主接单；当前未接入宿主ack", "带任务、原请求和真实运行绑定的接单证据"),
    "dispatch_already_unconfirmed": ("operator", "核查另一个未确认原请求；不要重复派发", "原请求得到明确人工核对；当前不提供自动重派"),
    "run_binding_ambiguous": ("operator", "核对既有运行与精确执行意图；不猜合并", "唯一同范围运行及其原接纳回执"),
    "task_changed_during_dispatch": ("operator", "任务约束已变化；核对可能产生的运行", "人工协调运行与新约束；不自动恢复/取消"),
    "receipt_basis_stale": ("manager", "旧等待理由已失效；刷新事实并重新审阅", "基于当前依赖/状态重新作出明确决定"),
}


class AdmissionError(ValueError):
    def __init__(self, message, code="invalid", status=400):
        super().__init__(message)
        self.code, self.status = code, status

    def payload(self):
        return {"error": str(self), "code": self.code, "assurance": ASSURANCE}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def initialize_admission_schema(connection):
    # Additive only: no legacy backfill, no task/run writes, no competing CAS.
    for sql in (
        "CREATE TABLE IF NOT EXISTS admission_schema_version (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)",
        "INSERT OR IGNORE INTO admission_schema_version VALUES(1,1)",
        """CREATE TABLE IF NOT EXISTS admission_requests (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL, intent_hash TEXT NOT NULL, source_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(task_id,idempotency_key))""",
        """CREATE TABLE IF NOT EXISTS admission_receipts (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL REFERENCES admission_requests(id),
            task_id TEXT NOT NULL, state TEXT NOT NULL, receipt_json TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS admission_receipts_task ON admission_receipts(task_id,sequence)",
        """CREATE TABLE IF NOT EXISTS admission_run_targets (
            run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, target_json TEXT NOT NULL)""",
    ):
        connection.execute(sql)
    # Capture the actual prepared target at the real queue INSERT, atomically
    # with that run. No legacy backfill or inferred target from current rows.
    target_sql = "json_object(" + ",".join("'" + key + "',t." + key for key in TARGET_FIELDS) + ")"
    connection.execute("""CREATE TRIGGER IF NOT EXISTS admission_capture_run_target
        AFTER INSERT ON runs BEGIN
        INSERT INTO admission_run_targets(run_id,task_id,target_json)
        SELECT NEW.id,NEW.task_id,""" + target_sql + " FROM tasks t WHERE t.id=NEW.task_id; END")
    connection.execute("UPDATE admission_schema_version SET version=2 WHERE singleton=1 AND version=1")
    for operation in ("UPDATE", "DELETE"):
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS admission_run_targets_no_{operation.lower()} BEFORE {operation} ON admission_run_targets BEGIN SELECT RAISE(ABORT,'immutable run target'); END")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS admission_run_targets_no_replace
        BEFORE INSERT ON admission_run_targets WHEN EXISTS(SELECT 1 FROM admission_run_targets WHERE run_id=NEW.run_id)
        BEGIN SELECT RAISE(ABORT,'immutable run target identity'); END""")
    for table in ("admission_requests", "admission_receipts"):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable admission history'); END")
        # INSERT OR REPLACE must not bypass immutable history when recursive triggers are off.
        condition = "id=NEW.id" if table == "admission_requests" else "id=NEW.id OR sequence=NEW.sequence"
        if table == "admission_requests":
            condition += " OR (task_id=NEW.task_id AND idempotency_key=NEW.idempotency_key)"
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition}) BEGIN SELECT RAISE(ABORT,'immutable admission identity'); END")


class AdmissionService:
    def __init__(self, db):
        self.db = db

    def _facts(self, connection, task_id):
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise AdmissionError("task not found", "missing", 404)
        task = dict(row)
        versions = [dict(row) for row in connection.execute("SELECT * FROM operations_task_versions WHERE task_id=?", (task_id,))]
        version = _version_for(task, {"versions": versions})
        revision = version["revision"] if version else None
        deps = [dict(row) for row in connection.execute("SELECT t.id,t.state,d.relation FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_id WHERE d.task_id=? ORDER BY t.id", (task_id,))]
        runs = [dict(row) for row in connection.execute("SELECT id,task_id,attempt,adapter,status,session_id FROM runs WHERE task_id=? ORDER BY attempt,id", (task_id,))]
        targets = {row["run_id"]: json.loads(row["target_json"]) for row in connection.execute("SELECT * FROM admission_run_targets WHERE task_id=?", (task_id,))}
        execution_target = {key: task.get(key) for key in TARGET_FIELDS}
        intake = connection.execute("SELECT intent FROM intakes WHERE id=?", (task.get("source_request_id"),)).fetchone() if task.get("source_request_id") else None
        basis = {"task": native_basis(task), "scope": {key: task.get(key) for key in ("title", "scope_summary", "repository", "base_branch", "environment", "authorization_policy", "imported_from", "execution_mode", "requires_deploy", "evidence_profile")},
                 "dependencies": deps, "runs": runs, "intake_intent": intake[0] if intake else None}
        return {"task": task, "revision": revision, "basis": digest(basis), "scope_hash": digest(basis["scope"]), "execution_target_hash": digest(execution_target), "run_targets": targets, "dependencies": deps,
                "runs": runs, "forbidden": execution_forbidden(task, intake_intent=basis["intake_intent"])}

    def _guard(self, facts):
        task = facts["task"]
        if is_paused_by_user(task):
            return "rejected", "user_paused"
        if task["state"] in {"DONE", "CANCELED", "FAILED"}:
            return "rejected", "task_terminal"
        if facts["forbidden"]:
            return "rejected", "execution_forbidden"
        if task.get("action_sensitive"):
            return "rejected", "sensitive_approval_required"
        if task.get("environment", "").lower() in {"prod", "pro", "production"} or task.get("base_branch", "").lower() in {"main", "master", "origin/main", "origin/master"}:
            return "rejected", "release_scope_forbidden"
        if task.get("action_owner_kind") in {"user", "external"}:
            return "deferred", task["action_owner_kind"] + "_action_pending"
        # Match actual RunManager dependency contract, not a more permissive guess.
        if any(item["state"] != "DONE" for item in facts["dependencies"]):
            return "deferred", "dependency_unresolved"
        return None

    def _receipts(self, connection, task_id):
        return [json.loads(row[0]) for row in connection.execute("SELECT receipt_json FROM admission_receipts WHERE task_id=? ORDER BY sequence", (task_id,))]

    def _append(self, connection, request, facts, state, reason, run=None):
        prior = connection.execute("SELECT receipt_json FROM admission_receipts WHERE request_id=? ORDER BY sequence LIMIT 1", (request["id"],)).fetchone()
        receipt_id = str(uuid.uuid4())
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM admission_receipts").fetchone()[0]
        owner, action, recovery = REASONS[reason]
        receipt = {"id": receipt_id, "sequence": sequence, "request_id": request["id"], "task_id": request["task_id"],
                   "idempotency_key": request["idempotency_key"], "source_revision": request["source_revision"],
                   "request_hash": request["payload_hash"], "intent_hash": request["intent_hash"],
                   "recorded_revision": facts["revision"], "received_at": utc_now(), "state": state, "reason_code": reason,
                   "responsibility": {"kind": owner, "declared_owner": facts["task"].get("action_owner") or None},
                   "next_action": action, "recovery_condition": recovery, "basis": facts["basis"], "scope_hash": facts["scope_hash"],
                   "execution_target_hash": facts["execution_target_hash"],
                   "run_id": run["id"] if run else None,
                   "executor": {"run_id": run["id"], "adapter": run["adapter"], "attempt": run["attempt"], "host_id": None, "person_id": None} if run else None,
                   "original_receipt_id": json.loads(prior[0])["id"] if prior else receipt_id, "assurance": ASSURANCE}
        connection.execute("INSERT INTO admission_receipts(sequence,id,request_id,task_id,state,receipt_json) VALUES(?,?,?,?,?,?)", (sequence, receipt_id, request["id"], request["task_id"], state, canonical(receipt)))
        return receipt

    def _project(self, connection, facts, receipt=None, reused=False):
        history = self._receipts(connection, facts["task"]["id"])
        latest = history[-1] if history else None
        current = dict(latest) if latest else {"state": "saved", "reason_code": "task_saved_no_dispatch_receipt", "run_id": None, "executor": None}
        stale = bool(latest and latest["basis"] != facts["basis"])
        bound = next((run for run in facts["runs"] if run["id"] == current.get("run_id")), None)
        # Starting/finishing a known run cannot revoke historical admission.
        # For accepted receipts, current protection + exact execution scope are
        # the validity boundary; status/session are separately displayed facts.
        if latest and latest["state"] in {"queued", "coalesced"}:
            target = facts["run_targets"].get(bound["id"]) if bound else None
            stale = not bound or latest["scope_hash"] != facts["scope_hash"] or latest.get("execution_target_hash") != facts["execution_target_hash"] or target is None or digest(target) != facts["execution_target_hash"]
        guard = self._guard(facts)
        if guard:
            current["state"], current["reason_code"] = guard
        elif stale:
            current.update(state="uncertain", reason_code="receipt_basis_stale")
        owner, action, recovery = REASONS[current["reason_code"]]
        current.update(responsibility={"kind": owner, "declared_owner": facts["task"].get("action_owner") or None}, next_action=action, recovery_condition=recovery,
                       reason_valid=not stale, source_revision=facts["revision"])
        current["session_id"] = bound["session_id"] or None if bound else None
        current["run_status"] = bound["status"] if bound else None
        if not bound:
            current["run_id"], current["executor"] = None, None
        unresolved = self._unconfirmed(connection, facts["task"]["id"])
        return {"schema_version": 1, "task_id": facts["task"]["id"], "revision": facts["revision"], "current": current,
                "receipt": receipt or latest, "original_receipt": next((item for item in history if item["id"] == (receipt or latest or {}).get("original_receipt_id")), None),
                "history": history, "reused": reused, "basis_stale": stale, "dependencies": facts["dependencies"],
                "dispatch_allowed": facts["revision"] is not None and not guard and not unresolved and not any(run["status"] in {"QUEUED", "RUNNING"} for run in facts["runs"]),
                "external_host_ack": "not_integrated", "assurance": ASSURANCE}

    def _unconfirmed(self, connection, task_id):
        return connection.execute("""SELECT r.id FROM admission_requests r JOIN admission_receipts a ON a.request_id=r.id
            WHERE r.task_id=? AND a.sequence=(SELECT MAX(b.sequence) FROM admission_receipts b WHERE b.request_id=r.id)
            AND a.state IN ('pending','uncertain') LIMIT 1""", (task_id,)).fetchone()

    def get(self, task_id):
        with self.db.connect() as connection:
            connection.execute("BEGIN")
            return self._project(connection, self._facts(connection, task_id))

    def dispatch(self, task_id, payload, dispatcher, *, validate_execution=None):
        required = {"instruction", "idempotency_key", "expected_revision"}
        if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"resume"}:
            raise AdmissionError("dispatch requires instruction, idempotency_key and expected_revision; refresh and review")
        normalized = dict(payload, resume=payload.get("resume", False))
        for field, limit in (("instruction", 32000), ("idempotency_key", 128)):
            if not isinstance(normalized[field], str) or not normalized[field].strip() or len(normalized[field]) > limit:
                raise AdmissionError("invalid " + field)
        if type(normalized["resume"]) is not bool or type(normalized["expected_revision"]) is not int or normalized["expected_revision"] < 1:
            raise AdmissionError("invalid resume or expected_revision")
        payload_hash = digest(normalized)
        intent_hash = digest({key: normalized[key] for key in ("instruction", "resume")})
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts = self._facts(connection, task_id)
            prior = connection.execute("SELECT * FROM admission_requests WHERE task_id=? AND idempotency_key=?", (task_id, normalized["idempotency_key"])).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash:
                    raise AdmissionError("key already used for another payload", "idempotency", 409)
                receipts = [item for item in self._receipts(connection, task_id) if item["request_id"] == prior["id"]]
                return self._project(connection, facts, receipts[-1], True)
            if facts["revision"] != normalized["expected_revision"]:
                raise AdmissionError("task version changed or is unavailable; refresh and review", "stale", 409)
            request = {"id": str(uuid.uuid4()), "task_id": task_id, "idempotency_key": normalized["idempotency_key"], "payload_hash": payload_hash,
                       "intent_hash": intent_hash, "source_revision": facts["revision"], "created_at": utc_now()}
            unconfirmed = self._unconfirmed(connection, task_id)
            connection.execute("INSERT INTO admission_requests VALUES(:id,:task_id,:idempotency_key,:payload_hash,:intent_hash,:source_revision,:created_at)", request)
            state_reason, bound = self._guard(facts), None
            active = [run for run in facts["runs"] if run["status"] in {"QUEUED", "RUNNING"}]
            if not state_reason and unconfirmed:
                state_reason = ("uncertain", "dispatch_already_unconfirmed")
            if not state_reason and active:
                compatible = connection.execute("""SELECT a.receipt_json FROM admission_receipts a JOIN admission_requests r ON a.request_id=r.id
                    WHERE a.task_id=? AND a.state IN ('queued','coalesced') AND r.intent_hash=? ORDER BY a.sequence DESC""", (task_id, intent_hash)).fetchall()
                matching = [json.loads(row[0]) for row in compatible if json.loads(row[0])["scope_hash"] == facts["scope_hash"] and json.loads(row[0]).get("execution_target_hash") == facts["execution_target_hash"] and len(active) == 1 and json.loads(row[0])["run_id"] == active[0]["id"] and active[0]["id"] in facts["run_targets"] and digest(facts["run_targets"][active[0]["id"]]) == facts["execution_target_hash"]]
                if matching:
                    state_reason, bound = ("coalesced", "active_run_reused"), active[0]
                else:
                    state_reason = ("uncertain", "run_binding_ambiguous")
            # Exact retries, protected/deferred work, and proven coalescing keep
            # their original contract. Only a NEW actual dispatch needs this
            # read-only parameter/capability preflight; failure rolls back the
            # uncommitted request and is not a false uncertain worker result.
            if state_reason is None and validate_execution is not None:
                validate_execution(facts["task"], normalized["resume"])
            state_reason = state_reason or ("pending", "dispatch_confirmation_pending")
            receipt = self._append(connection, request, facts, *state_reason, run=bound)
            if state_reason[0] != "pending":
                return self._project(connection, facts, receipt)
            initial_scope = facts["scope_hash"]
            initial_run_ids = {run["id"] for run in facts["runs"]}
        # Do not hold a SQLite transaction while existing dispatch can run a worker.
        try:
            result = dispatcher(task_id, normalized["instruction"], normalized["resume"])
            reason = "external_ack_not_integrated"
        except Exception:
            result, reason = None, "dispatch_result_unknown"
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts = self._facts(connection, task_id)
            state, bound = "uncertain", None
            if self._guard(facts) or facts["scope_hash"] != initial_scope:
                reason = "task_changed_during_dispatch"
            elif isinstance(result, dict) and result.get("id"):
                matches = [run for run in facts["runs"] if run["id"] == result["id"] and run["task_id"] == task_id]
                target = facts["run_targets"].get(result["id"])
                if target is not None and digest(target) != facts["execution_target_hash"]:
                    reason = "task_changed_during_dispatch"
                elif len(matches) == 1 and target is not None and matches[0]["id"] not in initial_run_ids and matches[0]["status"] in {"QUEUED", "RUNNING", "DONE"} and sum(run["status"] in {"QUEUED", "RUNNING"} for run in facts["runs"]) <= 1:
                    state, reason, bound = "queued", "local_run_recorded", matches[0]
                else:
                    reason = "run_binding_ambiguous"
            receipt = self._append(connection, request, facts, state, reason, bound)
            return self._project(connection, facts, receipt)
