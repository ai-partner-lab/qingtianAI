from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .service import ControlPlane, is_paused_by_user, is_plan_only


REPORT_STATES = (
    "RUNNING",
    "VERIFYING",
    "WAITING",
    "PAUSED",
    "PLAN_ONLY",
    "DONE",
    "INBOX",
)
REPORT_EVIDENCE_KINDS = ("commit", "test", "deploy", "smoke")
REPORT_STATE_LABELS = {
    "RUNNING": "执行中",
    "VERIFYING": "验收中",
    "WAITING": "等待中",
    "PAUSED": "已暂停",
    "PLAN_ONLY": "仅规划",
    "DONE": "已完成",
    "INBOX": "收件箱",
}


def _display_state(state: str) -> str:
    if state in {"PLANNED", "QUEUED"}:
        return "INBOX"
    if state in {"FAILED", "CANCELED"}:
        return "WAITING"
    return state if state in REPORT_STATES else "INBOX"


def _next_step(state: str) -> str:
    return {
        "RUNNING": "按计划推进并补充执行证据",
        "VERIFYING": "完成验收并补齐证据",
        "WAITING": "解除阻塞后继续推进",
        "PAUSED": "收到明确恢复指令后再重新分发",
        "PLAN_ONLY": "完成方案评审；Go/No-Go 后另建实施任务",
        "DONE": "已完成，持续观察",
        "INBOX": "明确主责并安排执行",
    }.get(state, "按计划推进")


def daily_report_payload(
    service: ControlPlane,
    report_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    window_end = now or datetime.now(timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    else:
        window_end = window_end.astimezone(timezone.utc)
    window_start = window_end - timedelta(hours=24)
    target = report_date or window_end.astimezone(
        ZoneInfo("Asia/Shanghai")
    ).date()
    tasks = service.rolling_24h_tasks(window_end)
    generated_at = window_end.isoformat(timespec="seconds")
    rows: List[Dict[str, Any]] = []
    for task in tasks:
        evidence_rows = service.db.all(
            """
            SELECT kind, value, label, verified, created_at
            FROM evidence
            WHERE task_id=?
            ORDER BY created_at DESC
            """,
            (task["id"],),
        )
        evidence = {
            kind: [
                {
                    "value": row["value"],
                    "label": row["label"],
                    "verified": bool(row["verified"]),
                    "created_at": row["created_at"],
                }
                for row in evidence_rows
                if row["kind"] == kind
            ]
            for kind in REPORT_EVIDENCE_KINDS
        }
        display_state = (
            "PAUSED"
            if is_paused_by_user(task)
            else "PLAN_ONLY"
            if is_plan_only(task)
            else _display_state(task["state"])
        )
        rows.append(
            {
                "id": task["id"],
                "state": display_state,
                "state_label": REPORT_STATE_LABELS[display_state],
                "source_state": task["state"],
                "priority": task["priority"],
                "title": task["title"],
                "owner": task["owner_session"] or task["worker_type"] or "未分配",
                "progress": task["progress"],
                "short_summary": task["short_summary"] or "待处理",
                "evidence": evidence,
                "evidence_counts": {
                    kind: len(values) for kind, values in evidence.items()
                },
                "risk": task["blocking_reason"] or task["risk"] or "",
                "next_step": _next_step(display_state),
                "updated_at": task["updated_at"],
            }
        )

    state_counts = Counter(row["state"] for row in rows)
    total = len(rows)
    summary = {
        "total": total,
        "done": state_counts.get("DONE", 0),
        "average_progress": (
            round(sum(row["progress"] for row in rows) / total) if total else 0
        ),
        "counts": {state: state_counts.get(state, 0) for state in REPORT_STATES},
    }
    markdown = _daily_report_markdown(
        target, generated_at, window_start, window_end, rows, summary
    )
    return {
        "date": target.isoformat(),
        "generated_at": generated_at,
        "window_started_at": window_start.isoformat(timespec="seconds"),
        "window_ended_at": window_end.isoformat(timespec="seconds"),
        "summary": summary,
        "rows": rows,
        "markdown": markdown,
    }


def _daily_report_markdown(
    target: date,
    generated_at: str,
    window_start: datetime,
    window_end: datetime,
    rows: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> str:
    lines = [
        "# 擎天过去24小时日报 {}".format(target.isoformat()),
        "",
        "生成时间：{}".format(generated_at),
        "统计窗口：{} ～ {}".format(
            window_start.isoformat(timespec="seconds"),
            window_end.isoformat(timespec="seconds"),
        ),
        "",
        "| 事项 | 主责 | 状态 | 阶段指数（非完成率） | 10字概要 | Commit/Test/Deploy/Smoke | 风险/下一步 |",
        "|---|---|---|---:|---|---|---|",
    ]
    for row in rows:
        evidence_digest = " ".join(
            "{}×{}".format(kind, count)
            for kind, count in row["evidence_counts"].items()
            if count
        ) or "无"
        risk_and_next = "；".join(
            value for value in (row["risk"], row["next_step"]) if value
        )
        lines.append(
            "| {title} | {owner} | {state} | {progress}% | {summary} | {evidence} | {risk} |".format(
                title=_cell(row["title"]),
                owner=_cell(row["owner"]),
                state=_cell(row["state_label"]),
                progress=row["progress"],
                summary=_cell(row["short_summary"]),
                evidence=_cell(evidence_digest),
                risk=_cell(risk_and_next),
            )
        )

    lines.extend(
        [
            "",
            "## 汇总",
            "",
            "- 纳管事项：{}；完成：{}；平均阶段指数（非完成率）：{}%".format(
                summary["total"], summary["done"], summary["average_progress"]
            ),
            "- 执行中：{}；等待中：{}；已暂停：{}；验收中：{}".format(
                summary["counts"]["RUNNING"],
                summary["counts"]["WAITING"],
                summary["counts"]["PAUSED"],
                summary["counts"]["VERIFYING"],
            ),
            "- 完成口径：DONE 必须满足任务类型对应的证据门禁。",
            "- 时间口径：UTC 存储，按生成时刻向前滚动 24 小时；界面以 Asia/Shanghai 展示。",
            "- 隐私口径：不包含密码、Token、完整 Prompt 或原始 Codex JSONL。",
            "",
        ]
    )
    return "\n".join(lines)


def daily_report(
    service: ControlPlane, report_date: Optional[date] = None
) -> str:
    return daily_report_payload(service, report_date)["markdown"]


def write_daily_report(
    service: ControlPlane, path: Path, report_date: Optional[date] = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(daily_report(service, report_date), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
