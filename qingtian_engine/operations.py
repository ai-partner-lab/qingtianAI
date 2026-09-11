"""Read-only operator projection: workflow stage is not executor liveness.

This module never authorizes, dispatches, retries, or completes a task. A next
action is advice for an operator, not an executable command or policy grant.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .service import is_paused_by_user, is_plan_only


TERMINAL = {"DONE", "CANCELED"}


def operator_status(
    task: Dict[str, Any], run: Optional[Dict[str, Any]], mode: str,
) -> Dict[str, Any]:
    state = str(task.get("state") or "UNKNOWN")
    display = str(task.get("display_state") or state)
    runtime = task.get("runtime_status") or {}
    action = task.get("human_action") or {}
    resolution = task.get("state_resolution") or {}
    run_status = str((run or {}).get("status") or "")
    missing = list(runtime.get("missing_evidence") or [])
    live = bool(resolution.get("live_run"))
    # Liveness projection may replace state/display_state with RUNNING. Preserve
    # the stored authorization intent using the service's existing rules, without
    # mutating the dashboard task or duplicating its pause/plan-only markers.
    stored_task = {**task, "state": task.get("stored_state") or state}
    paused = display == "PAUSED" or is_paused_by_user(stored_task)
    plan_only = display == "PLAN_ONLY" or is_plan_only(stored_task)
    policy = str(task.get("authorization_policy") or "").strip().lower().replace("-", "_")
    reference_only = bool(task.get("execution_read_only") or policy in {"analysis", "analysis_only", "reference_only"} or task.get("imported_from"))
    owner = str(task.get("owner_session") or "未指定主责")
    result = {
        "code": "UNKNOWN", "title": "等待核对", "detail": "尚无可靠执行状态",
        "next_action": "查看任务记录，核对当前状态", "owner": owner,
        "needs_attention": True, "active": False,
        "run_id": (run or {}).get("id"), "run_status": run_status or None,
        "missing_evidence": missing,
    }

    def status(code: str, title: str, detail: str, next_action: str,
               attention: bool = False, active: bool = False) -> Dict[str, Any]:
        result.update(code=code, title=title, detail=detail, next_action=next_action,
                      needs_attention=attention, active=active)
        return result

    # A genuinely live executor must not be hidden by a stale workflow stage.
    if live:
        if reference_only or stored_task["state"] in TERMINAL or paused or plan_only:
            return status(
                "LIVE_STATE_CONFLICT", "执行器存活，但任务权限或状态冲突",
                "存活事实不能覆盖只读授权、暂停或终态记录",
                "立即核对执行范围与原任务；如需停止，显式取消并确认进程退出", True, True,
            )
        stale = runtime.get("code") in {"EVENT_STALE", "PROCESS_LOST"}
        return status(
            "EXECUTING", "执行器运行中",
            "执行器存活；最近活动见运行记录" if not stale else "执行器仍存活，但活动事件已过期",
            "观察当前运行；无新事件不等于卡死", attention=stale, active=True,
        )
    if paused:
        return status("PAUSED", "已暂停", "不会自动领取、重试或续跑",
                      "仅在明确恢复指令后重新评估")
    if plan_only or reference_only:
        return status("REFERENCE_ONLY", "仅分析 / 参考", "此任务不具备执行授权",
                      "阅读结论；实施需新的明确授权")
    if state in TERMINAL:
        return status(state, "已完成" if state == "DONE" else "已取消",
                      "任务终态；不自动恢复", "查看证据与历史记录")
    if runtime.get("code") in {"EXTERNAL", "DELEGATED_AGENT"}:
        return status("EXTERNAL_HEARTBEAT", "外部执行器有心跳",
                      "心跳仅代表外部执行器报告，不是本机进程验证",
                      "等待外部结果与可核验回执", active=True)
    if runtime.get("code") in {"EXTERNAL_STALE", "RECOVERY_REQUIRED", "PROCESS_LOST", "NO_RUN"}:
        return status("RECOVERY_REVIEW", "执行状态需要核对",
                      str(runtime.get("label") or "未找到活动执行器"),
                      "先核对原运行和已有产物，再决定是否恢复；不会自动重派", True)
    if action.get("owner_kind") == "user":
        result["owner"] = str(action.get("owner") or "你")
        return status("USER_ACTION", "需要人工处理", str(action.get("text") or "查看任务要求"),
                      "完成所需动作后重新验证；不等于任务已完成", True)
    if action.get("owner_kind") == "external":
        result["owner"] = str(action.get("owner") or "外部主责")
        return status("EXTERNAL_WAIT", "等待外部", str(action.get("text") or "尚未收到外部回执"),
                      "查看外部依赖与期限")
    if run_status == "CANCELED":
        return status("RUN_STOPPED", "执行已停止",
                      "最新 Run 已取消；任务保留原状态，不代表仍在执行",
                      "核对取消原因和已有产物；如仍需执行，请明确发起新指令", True)
    if run_status == "FAILED" or state == "FAILED":
        return status("RUN_FAILED", "执行失败",
                      "最新执行未成功；保留现场与证据",
                      "核对失败原因、副作用与授权后再决定是否重试", True)
    if run_status in {"QUEUED", "RUNNING"}:
        return status("RUN_UNCONFIRMED", "等待执行器确认",
                      "已有运行记录，但尚未验证存活执行器",
                      "核对进程、领取状态与回执；不要重复派发", True)
    if state == "VERIFYING":
        gap = " / ".join(str(item) for item in missing)
        evidence_detail = (
            "执行已结束" if run_status == "DONE" else "尚无 Run 记录，任务处于验收阶段"
        )
        return status(
            "EVIDENCE_REQUIRED" if missing else "REVIEW_REQUIRED",
            "待补证据" if missing else "待验收结论",
            (evidence_detail + "，缺少：" + gap) if missing else evidence_detail + "，尚未确认任务验收通过",
            ("手动模式：需明确安排验收；不会自动启动 QA" if mode == "manual"
             else "等待调度器评估验收资格；尚无活动验收 Run"), True,
        )
    if state in {"INBOX", "PLANNED", "QUEUED"}:
        return status(
            "MANUAL_DISPATCH" if mode == "manual" else "DISPATCH_REVIEW",
            "待明确派发" if mode == "manual" else "待调度评估",
            "当前没有活动执行器",
            "核对范围、仓库与授权后明确派发" if mode == "manual" else "等待依赖、资源及权限检查；排队不等于已获授权",
            mode != "auto",
        )
    if state == "WAITING":
        return status("WAITING_REVIEW", "等待原因待处理",
                      str(task.get("blocking_reason") or "没有活动执行器，也没有已登记的外部动作"),
                      "核对阻塞项并明确下一步；不会用等待状态冒充后台执行", True)
    return result


def enrich_operations(payload: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Enrich only a disposable dashboard snapshot; no storage side effects.

    last_run is projected per task by the service, independent of the dashboard's
    bounded recent-runs feed. This avoids losing history for the 51st task.
    """
    recent = {}
    for run in payload.get("runs", []):
        previous = recent.get(run["task_id"])
        if previous is None or int(run.get("attempt") or 0) > int(previous.get("attempt") or 0):
            recent[run["task_id"]] = run
    active, attention, waiting, done = [], [], [], []
    for task in payload.get("tasks", []):
        run = task.get("last_run")
        if "last_run" not in task:
            run = recent.get(task["id"])
        task["operator_status"] = op = operator_status(task, run, mode)
        if op["active"]:
            active.append(task["id"])
        if op["needs_attention"]:
            attention.append(task["id"])
        if task.get("state") == "DONE":
            done.append(task["id"])
        elif not op["active"] and op["code"] not in {"PAUSED", "REFERENCE_ONLY", "CANCELED"}:
            waiting.append(task["id"])
    payload["operations"] = {
        "schema_version": 1, "mode": mode, "automatic_dispatch": mode == "auto",
        "summary": {"active": len(active), "attention": len(attention),
                    "waiting": len(waiting), "done": len(done)},
        "attention_task_ids": attention, "active_task_ids": active,
        "semantics": "read-only projection; next_action never grants execution permission",
    }
    return payload
