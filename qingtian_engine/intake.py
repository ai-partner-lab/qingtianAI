from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterable, List, Optional, Sequence

from .config import ensure_data_dirs, runtime_policy, EXECUTION_EFFORTS
from .execution_parameters import codex_command_prefix
from .db import utc_now
from .project_config import ProjectConfigError, load_project_config
from .redaction import contains_secret, fingerprint, redact_text
from .router import ROLE_PATTERN, Route, matching_routes, route_task
from .service import ControlPlane


MAX_ATTACHMENTS = 10
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARS = 20_000
INTENTS = {"analyze", "implement", "implement_and_deploy_dev"}
SAFE_ENVIRONMENTS = {"local", "dev", "test"}
SAFE_WORKERS = {"cli", "browser", "qa", "infra", "security", "manager"}
SAFE_REASONING = EXECUTION_EFFORTS

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
DECLARED_MIME_ALIASES = {
    ".md": {"text/plain", "text/markdown", "application/octet-stream"},
    ".txt": {"text/plain", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
        "application/octet-stream",
    },
}


class IntakeError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class IntakeNeedsInput(IntakeError):
    pass


@dataclass
class AttachmentUpload:
    name: str
    mime: str
    file: BinaryIO


def new_intake_id() -> str:
    return "intake-{}".format(uuid.uuid4().hex)


def new_attachment_id() -> str:
    return "attachment-{}".format(uuid.uuid4().hex)


def sanitize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.replace("\\", "/").split("/")[-1].strip()
    normalized = "".join(char for char in normalized if ord(char) >= 32)
    normalized = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", normalized)
    normalized = normalized.lstrip(".-_")[:120]
    if not normalized or normalized in {".", ".."}:
        raise IntakeError("附件文件名无效")
    suffix = Path(normalized).suffix.lower()
    if suffix not in MIME_BY_SUFFIX:
        raise IntakeError("不支持的附件类型：{}".format(suffix or "无扩展名"))
    stem = normalized[: -len(suffix)] if suffix else normalized
    return (stem[: 120 - len(suffix)] or "attachment") + suffix


def _validate_signature(path: Path, suffix: str) -> None:
    with path.open("rb") as handle:
        head = handle.read(16)
    if suffix == ".png" and not head.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IntakeError("PNG 附件内容与扩展名不一致")
    if suffix in {".jpg", ".jpeg"} and not head.startswith(b"\xff\xd8\xff"):
        raise IntakeError("JPEG 附件内容与扩展名不一致")
    if suffix == ".webp" and not (
        head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP"
    ):
        raise IntakeError("WebP 附件内容与扩展名不一致")
    if suffix == ".gif" and not (
        head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
    ):
        raise IntakeError("GIF 附件内容与扩展名不一致")
    if suffix == ".pdf" and not head.startswith(b"%PDF-"):
        raise IntakeError("PDF 附件内容与扩展名不一致")
    if suffix in {".docx", ".xlsx"}:
        if not zipfile.is_zipfile(str(path)):
            raise IntakeError("Office 附件不是有效的 OOXML 文件")
        try:
            with zipfile.ZipFile(str(path)) as archive:
                names = set(archive.namelist())
                expected_prefix = "word/" if suffix == ".docx" else "xl/"
                if "[Content_Types].xml" not in names or not any(
                    name.startswith(expected_prefix) for name in names
                ):
                    raise IntakeError("Office 附件类型与内容不一致")
                if any(
                    name.startswith("/")
                    or ".." in Path(name).parts
                    or name.endswith("/")
                    and Path(name).name == ".."
                    for name in names
                ):
                    raise IntakeError("Office 附件包含不安全路径")
        except zipfile.BadZipFile as exc:
            raise IntakeError("Office 附件损坏") from exc
    if suffix in {".txt", ".md"}:
        data = path.read_bytes()
        if b"\x00" in data:
            raise IntakeError("文本附件包含二进制内容")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntakeError("文本附件必须为 UTF-8") from exc


def validate_planner_draft(value: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise IntakeError("Planner 输出不是 JSON 对象")
    required = {
        "title": str,
        "scope": str,
        "acceptance": list,
        "priority": int,
        "environment": str,
        "repository": str,
        "worker_type": str,
        "owner_session": str,
        "reasoning": str,
        "requires_deploy": bool,
        "route_reason": str,
        "dependencies": list,
        "execution_prompt": str,
    }
    for key, expected in required.items():
        if key not in value or not isinstance(value[key], expected):
            raise IntakeError("Planner 输出字段无效：{}".format(key))
    if not value["title"].strip() or len(value["title"]) > 180:
        raise IntakeError("Planner 标题无效")
    if not 0 <= value["priority"] <= 3:
        raise IntakeError("Planner 优先级无效")
    if value["worker_type"] not in SAFE_WORKERS:
        raise IntakeError("Planner 执行器无效")
    if value["reasoning"] not in SAFE_REASONING:
        raise IntakeError("Planner 推理强度无效")
    if ROLE_PATTERN.fullmatch(value["owner_session"]) is None:
        raise IntakeError("Planner owner 无效")
    if not all(isinstance(item, str) and item.strip() for item in value["acceptance"]):
        raise IntakeError("Planner 验收项无效")
    if not all(isinstance(item, str) for item in value["dependencies"]):
        raise IntakeError("Planner 依赖无效")
    clean = dict(value)
    clean["title"] = redact_text(value["title"], max_chars=180)
    clean["scope"] = redact_text(value["scope"], max_chars=1000)
    clean["route_reason"] = redact_text(value["route_reason"], max_chars=300)
    clean["execution_prompt"] = redact_text(value["execution_prompt"], max_chars=1000)
    clean["acceptance"] = [
        redact_text(item, max_chars=300) for item in value["acceptance"][:20]
    ]
    clean["dependencies"] = [
        redact_text(item, max_chars=180) for item in value["dependencies"][:20]
    ]
    return clean


class DeterministicPlannerAdapter:
    name = "deterministic"

    def plan(
        self,
        text: str,
        intent: str,
        attachments: Sequence[Dict[str, Any]],
        advanced: Dict[str, Any],
    ) -> Dict[str, Any]:
        source = advanced.get("title") or text
        title = re.split(r"[\n。！？!?]", str(source).strip(), maxsplit=1)[0].strip()
        if not title:
            title = "处理附件 {}".format(attachments[0]["name"]) if attachments else "待理解任务"
        title = title[:180]
        scope = str(advanced.get("scope_summary") or text).strip()[:1000]
        route = route_task(title, scope, str(advanced.get("repository", "")))
        priority = _priority_from_text(text)
        if str(advanced.get("priority", "")).isdigit():
            priority = int(advanced["priority"])
        requires_deploy = intent == "implement_and_deploy_dev" or bool(
            advanced.get("requires_deploy")
        )
        environment = str(
            advanced.get("environment") or ("dev" if requires_deploy else "local")
        )
        worker = str(advanced.get("worker_type") or route.worker_type).lower()
        if worker == "auto":
            worker = route.worker_type
        acceptance = ["完成与描述直接相关的验证"]
        if intent != "analyze":
            acceptance = ["完成实现", "运行相关测试", "回传可核验结果"]
        if requires_deploy:
            acceptance.extend(["仅推 dev", "完成 dev 最小冒烟"])
        if attachments:
            acceptance.append("核对 {} 个附件中的可见需求".format(len(attachments)))
        method = "只分析并给出结论" if intent == "analyze" else "先 Plan，再 Coding"
        return {
            "title": title,
            "scope": scope,
            "acceptance": acceptance,
            "priority": priority,
            "environment": environment,
            "repository": str(advanced.get("repository", "")),
            "worker_type": worker,
            "owner_session": route.owner_session,
            "reasoning": advanced.get("reasoning", route.reasoning),
            "requires_deploy": requires_deploy,
            "route_reason": route.reason,
            "dependencies": [],
            "execution_prompt": "{}：{}。验收：{}".format(
                method, title, "；".join(acceptance)
            ),
        }


class CodexPlannerAdapter:
    """Opt-in real planner. Default runtime stays deterministic and testable."""

    name = "codex-exec"

    def __init__(self, cwd: Path):
        self.cwd = Path(cwd).expanduser().resolve()
        if not self.cwd.is_dir():
            raise IntakeError("Planner workspace 必须是显式存在的目录")

    def plan(
        self,
        text: str,
        intent: str,
        attachments: Sequence[Dict[str, Any]],
        advanced: Dict[str, Any],
    ) -> Dict[str, Any]:
        attachment_summary = [
            {
                "id": item.get("id", ""),
                "name": item["name"],
                "mime": item["mime"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in attachments
        ]
        prompt = (
            "你是擎天 Intake Planner。只输出符合给定字段的 JSON 对象，不执行任务。"
            "确定性政策层会在你之后校验 owner、模型、环境、仓库和部署。输入："
            + json.dumps(
                {
                    "text": redact_text(text, max_chars=MAX_TEXT_CHARS),
                    "intent": intent,
                    "attachments": attachment_summary,
                    "advanced": advanced,
                },
                ensure_ascii=False,
            )
        )
        # Planner's own model is separate from the execution choices requested
        # for the resulting task. Each explicit planner selection is preserved.
        policy = runtime_policy(advanced.get("planner_reasoning"),
                                requested_model=advanced.get("planner_model"),
                                requested_speed=advanced.get("planner_speed"), role="planner")
        command = [
            *codex_command_prefix(policy, "exec"),
            "--json",
            "--ephemeral", "--sandbox", "read-only",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.cwd),
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IntakeError("真实 Planner 不可用：{}".format(type(exc).__name__)) from exc
        if completed.returncode != 0:
            raise IntakeError(
                "真实 Planner 失败：{}".format(
                    redact_text(completed.stderr, max_chars=180)
                )
            )
        candidate = ""
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                candidate = item["text"]
        if not candidate:
            raise IntakeError("真实 Planner 未返回结构化草案")
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise IntakeError("真实 Planner 返回了无效 JSON") from exc


def _priority_from_text(text: str) -> int:
    match = re.search(r"\b[Pp]([0-3])\b", text or "")
    if match:
        return int(match.group(1))
    if any(word in (text or "") for word in ("紧急", "阻断", "严重", "立刻")):
        return 0
    return 2


class DeterministicIntakePolicy:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()

    def apply(
        self, draft: Dict[str, Any], text: str, advanced: Dict[str, Any]
    ) -> Dict[str, Any]:
        final = validate_planner_draft(draft)
        environment = str(final["environment"]).lower()
        base_branch = str(advanced.get("base_branch", "")).strip()
        if environment not in SAFE_ENVIRONMENTS:
            raise IntakeNeedsInput("pre/pro/production 需要正式发布门禁，不能从收件箱自动路由")
        if base_branch.lower() in {"main", "master", "origin/main", "origin/master"}:
            raise IntakeNeedsInput("main/master 需要正式发布门禁")
        repository = str(final["repository"]).strip()
        if repository:
            try:
                projects = load_project_config(data_dir=self.data_dir)
            except ProjectConfigError:
                raise IntakeNeedsInput("本地项目注册表无效") from None
            project = projects.resolve_reference(repository)
            if project is None:
                raise IntakeNeedsInput("repository 必须先通过 project register 注册")
            if base_branch and base_branch != project.base_branch:
                raise IntakeNeedsInput("base_branch 与已注册项目不一致")
            repository = project.name
            base_branch = project.base_branch
        elif base_branch:
            raise IntakeNeedsInput("base_branch 需要同时显式选择已注册 project")

        route = route_task(final["title"], text, repository)
        explicit_worker = str(advanced.get("worker_type", "")).lower()
        if re.search(r"@browser\b", text, re.I) or explicit_worker == "browser":
            route = Route("browser", "browser-qa", "high", "explicit browser worker")
        elif explicit_worker == "qa":
            route = Route("qa", "qa", "high", "explicit QA worker")
        elif explicit_worker == "infra":
            route = Route(
                "infra", "infrastructure", "xhigh", "explicit infrastructure worker"
            )
        elif (
            route.owner_session in {"qa", "browser-qa"}
            and any(
                word.lower() in text.lower()
                for word in ("修复", "实现", "页面", "ui", "交互", "前端", "web")
            )
            and not any(
                word.lower() in text.lower()
                for word in ("验收", "冒烟", "登录态", "浏览器", "chrome")
            )
        ):
            route = Route("cli", "frontend", "high", "frontend implementation work")

        requires_deploy = bool(final["requires_deploy"])
        if requires_deploy:
            environment = "dev"
        policy = runtime_policy(advanced.get("reasoning", None if "QINGTIAN_REASONING" in os.environ else route.reasoning),
                                requested_model=advanced.get("model", final.get("model")),
                                requested_speed=advanced.get("speed", final.get("speed")),
                                role="manager" if route.worker_type == "manager" else "executor")
        final.update(
            {
                "environment": environment,
                "repository": repository,
                "base_branch": base_branch,
                "worker_type": route.worker_type,
                "owner_session": route.owner_session,
                "reasoning": policy.reasoning,
                "model": policy.model,
                "speed": policy.speed,
                "requires_deploy": requires_deploy,
                "route_reason": route.reason,
            }
        )
        return final


Dispatcher = Callable[[str, str], Any]


class IntakeService:
    def __init__(
        self,
        control: ControlPlane,
        data_dir: Path,
        planner: Optional[Any] = None,
    ):
        self.control = control
        self.db = control.db
        self.paths = ensure_data_dirs(Path(data_dir))
        self.intake_root = self.paths["intake"].resolve()
        self.planner = planner or DeterministicPlannerAdapter()
        self.policy = DeterministicIntakePolicy(Path(data_dir))

    def create_intake(
        self,
        text: str,
        intent: str,
        uploads: Sequence[AttachmentUpload],
        idempotency_key: str,
        advanced: Optional[Dict[str, Any]] = None,
        dispatcher: Optional[Dispatcher] = None,
    ) -> Dict[str, Any]:
        advanced = self._clean_advanced(advanced or {})
        clean_key = re.sub(r"[^A-Za-z0-9._:-]", "", idempotency_key or "")[:160]
        if not clean_key:
            clean_key = fingerprint(
                "{}|{}|{}".format(text, intent, json.dumps(advanced, sort_keys=True))
            )
        existing = self.db.one(
            "SELECT id FROM intakes WHERE idempotency_key=?", (clean_key,)
        )
        if existing:
            result = self.get_intake(existing["id"])
            result["reused"] = True
            return result
        if intent not in INTENTS:
            raise IntakeError("intent 无效")
        if len(text or "") > MAX_TEXT_CHARS:
            raise IntakeError("描述不能超过 {} 字".format(MAX_TEXT_CHARS), status=413)
        if not (text or "").strip() and not uploads:
            raise IntakeError("请输入描述或添加附件")
        if len(uploads) > MAX_ATTACHMENTS:
            raise IntakeError("最多上传 {} 个附件".format(MAX_ATTACHMENTS), status=413)

        intake_id = new_intake_id()
        now = utc_now()
        clean_text = redact_text(text or "", max_chars=MAX_TEXT_CHARS)
        warning = contains_secret(text or "")
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO intakes(
                    id, idempotency_key, text, intent, status, planner_adapter,
                    secret_warning, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'RECEIVED', ?, ?, ?, ?)
                """,
                (
                    intake_id,
                    clean_key,
                    clean_text,
                    intent,
                    self.planner.name,
                    int(warning),
                    now,
                    now,
                ),
            )
        self._add_message(intake_id, "user", "request", clean_text or "已添加附件")
        receipt = "擎天已收到，正在理解 {} 个附件和你的描述…".format(len(uploads))
        self._add_message(
            intake_id,
            "assistant",
            "received",
            receipt,
            {"attachment_count": len(uploads)},
        )
        try:
            self._store_uploads(intake_id, uploads)
        except IntakeError:
            # Upload validation errors are request errors: the browser retains the
            # composer state, while the key remains reusable after correction.
            self.db.execute("DELETE FROM intakes WHERE id=?", (intake_id,))
            raise
        try:
            return self._plan_and_route(intake_id, advanced, dispatcher)
        except IntakeNeedsInput as exc:
            self._mark_status(intake_id, "NEEDS_INPUT", str(exc))
            self._add_message(
                intake_id, "assistant", "needs_input", str(exc), {"retryable": True}
            )
            return self.get_intake(intake_id)
        except Exception as exc:
            self._mark_status(intake_id, "FAILED", str(exc))
            self._add_message(
                intake_id,
                "assistant",
                "failed",
                "理解或分发失败，可安全重试：{}".format(redact_text(str(exc), 240)),
                {"retryable": True},
            )
            if isinstance(exc, IntakeError):
                raise
            raise IntakeError("理解或分发失败：{}".format(type(exc).__name__)) from exc

    def retry_intake(
        self, intake_id: str, dispatcher: Optional[Dispatcher] = None
    ) -> Dict[str, Any]:
        intake = self.get_intake(intake_id, include_internal=True)
        if intake["status"] not in {"FAILED", "NEEDS_INPUT"}:
            return self.get_intake(intake_id)
        draft = intake.get("draft") or {}
        task_ids = draft.get("task_ids") if isinstance(draft, dict) else None
        if task_ids and dispatcher and intake["intent"] != "analyze":
            try:
                for task_id in task_ids:
                    dispatcher(task_id, str(draft.get("execution_prompt", "")))
                self._mark_status(intake_id, "ROUTED", "")
                self._add_message(
                    intake_id,
                    "assistant",
                    "retry",
                    "已重新分发，原始描述和附件均保留。",
                    {"task_ids": task_ids},
                )
                return self.get_intake(intake_id)
            except Exception as exc:
                self._mark_status(intake_id, "FAILED", str(exc))
                raise IntakeError("重新分发失败：{}".format(redact_text(str(exc), 180)))
        advanced = draft.get("advanced", {}) if isinstance(draft, dict) else {}
        return self._plan_and_route(intake_id, advanced, dispatcher)

    def list_intakes(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self.db.all(
            "SELECT id FROM intakes ORDER BY created_at DESC LIMIT ?",
            (max(1, min(100, int(limit))),),
        )
        return [self.get_intake(row["id"]) for row in rows]

    def get_intake(
        self, intake_id: str, include_internal: bool = False
    ) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM intakes WHERE id=?", (intake_id,))
        if not row:
            raise KeyError("intake not found")
        row["secret_warning"] = bool(row["secret_warning"])
        try:
            row["draft"] = json.loads(row.pop("draft_json") or "{}")
        except json.JSONDecodeError:
            row["draft"] = {}
        if not include_internal and isinstance(row["draft"], dict):
            row["draft"].pop("execution_prompt", None)
            row["draft"].pop("advanced", None)
        attachments = self.db.all(
            """
            SELECT id, intake_id, name, mime, size, sha256, local_path, created_at
            FROM intake_attachments WHERE intake_id=? ORDER BY created_at ASC
            """,
            (intake_id,),
        )
        for attachment in attachments:
            if not include_internal:
                attachment.pop("local_path", None)
            attachment["url"] = "/api/intakes/{}/attachments/{}".format(
                intake_id, attachment["id"]
            )
        messages = self.db.all(
            """
            SELECT id, role, kind, content, metadata_json, created_at
            FROM intake_messages WHERE intake_id=? ORDER BY created_at ASC, rowid ASC
            """,
            (intake_id,),
        )
        for message in messages:
            try:
                message["metadata"] = json.loads(message.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                message["metadata"] = {}
        tasks = self.db.all(
            """
            SELECT id, parent_id, title, state, owner_session, worker_type, progress,
                model, reasoning, speed
            FROM tasks WHERE source_request_id=? ORDER BY created_at ASC
            """,
            (intake_id,),
        )
        row["attachments"] = attachments
        row["messages"] = messages
        row["tasks"] = tasks
        row["reused"] = False
        return row

    def read_attachment(
        self, intake_id: str, attachment_id: str
    ) -> tuple[Dict[str, Any], bytes]:
        row = self.db.one(
            """
            SELECT * FROM intake_attachments WHERE id=? AND intake_id=?
            """,
            (attachment_id, intake_id),
        )
        if not row:
            raise KeyError("attachment not found")
        path = self._controlled_path(row["local_path"])
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(path), flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != int(row["size"]):
                raise IntakeError("附件文件状态异常")
            data = b""
            while len(data) < int(row["size"]):
                chunk = os.read(fd, min(1024 * 1024, int(row["size"]) - len(data)))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
        if len(data) != int(row["size"]) or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise IntakeError("附件完整性校验失败")
        return row, data

    def _plan_and_route(
        self,
        intake_id: str,
        advanced: Dict[str, Any],
        dispatcher: Optional[Dispatcher],
    ) -> Dict[str, Any]:
        self._mark_status(intake_id, "PLANNING", "")
        intake = self.get_intake(intake_id, include_internal=True)
        planner_attachments = []
        for attachment in intake["attachments"]:
            item = dict(attachment)
            item["local_path"] = str(self._controlled_path(attachment["local_path"]))
            planner_attachments.append(item)
        draft = self.planner.plan(
            intake["text"], intake["intent"], planner_attachments, advanced
        )
        final = self.policy.apply(draft, intake["text"], advanced)
        final["advanced"] = advanced
        if planner_attachments:
            final["execution_prompt"] = "{}。受控本地附件：{}".format(
                final["execution_prompt"],
                "；".join(
                    "{} ({})".format(item["name"], item["local_path"])
                    for item in planner_attachments
                ),
            )[:1000]
        task_ids = self._create_tasks(intake, final)
        final["task_ids"] = task_ids
        final["parent_task_id"] = task_ids[0]
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE intakes SET status='ROUTED', draft_json=?, task_id=?,
                    error='', updated_at=? WHERE id=?
                """,
                (
                    json.dumps(final, ensure_ascii=False, sort_keys=True),
                    task_ids[0],
                    utc_now(),
                    intake_id,
                ),
            )
        method = (
            "只分析，不启动执行"
            if intake["intent"] == "analyze"
            else "先 Plan，再 Coding"
        )
        target = (
            "相关测试 + dev 最小冒烟"
            if final["requires_deploy"]
            else "相关测试与可核验证据"
        )
        receipt = {
            "understanding": final["title"],
            "owner": final["owner_session"],
            "method": method,
            "target": target,
            "task_id": task_ids[0],
            "task_ids": task_ids,
            "route_reason": final["route_reason"],
            "model": final["model"],
            "reasoning": final["reasoning"],
            "speed": final["speed"],
            "environment": final["environment"],
            "repository": final["repository"],
            "children": max(0, len(task_ids) - 1),
            "planner_adapter": self.planner.name,
            "planner_note": (
                "当前使用本地确定性 Planner；已校验附件元数据，但未声称识别图片语义。"
                if self.planner.name == "deterministic" and planner_attachments
                else ""
            ),
        }
        self._add_message(
            intake_id,
            "assistant",
            "routed",
            "我理解为：{}。已分发给 {}，任务 {}。".format(
                final["title"], final["owner_session"], task_ids[0]
            ),
            receipt,
        )
        if intake["intent"] != "analyze" and dispatcher:
            try:
                for task_id in task_ids:
                    if len(task_ids) > 1 and task_id == task_ids[0]:
                        continue
                    dispatcher(task_id, final["execution_prompt"])
            except Exception as exc:
                self._mark_status(intake_id, "FAILED", str(exc))
                self._add_message(
                    intake_id,
                    "assistant",
                    "failed",
                    "草案已保留，但后台分发失败；可重试：{}".format(
                        redact_text(str(exc), 180)
                    ),
                    {"retryable": True, "task_ids": task_ids},
                )
        return self.get_intake(intake_id)

    def _create_tasks(
        self, intake: Dict[str, Any], draft: Dict[str, Any]
    ) -> List[str]:
        # Persist the user's intent outside editable/truncated display text.
        # The execution layer must keep this gate on retries and auto cycles.
        authorization_policy = (
            "analysis-only" if intake["intent"] == "analyze" else "normal"
        )
        routes = matching_routes(
            draft["title"], intake["text"], draft["repository"]
        )
        split_markers = (
            "跨",
            "同时",
            "分别",
            "前后端",
            "全链路",
            "以及",
            "并且",
            "full stack",
            "across",
            "both",
        )
        should_split = len(routes) >= 2 and any(
            marker in intake["text"] for marker in split_markers
        )
        if not should_split:
            task = self.control.create_task(
                title=draft["title"],
                idempotency_key="intake:{}:task".format(intake["idempotency_key"]),
                scope_summary=draft["scope"],
                priority=draft["priority"],
                environment=draft["environment"],
                repository=draft["repository"],
                base_branch=draft.get("base_branch", ""),
                worker_type=draft["worker_type"],
                owner_session=draft["owner_session"],
                reasoning=draft["reasoning"],
                model=draft["model"],
                speed=draft["speed"],
                authorization_policy=authorization_policy,
                requires_deploy=draft["requires_deploy"],
                state="PLANNED",
                source_request_id=intake["id"],
            )
            return [task["id"]]

        parent = self.control.create_task(
            title=draft["title"],
            idempotency_key="intake:{}:parent".format(intake["idempotency_key"]),
            scope_summary=draft["scope"],
            priority=draft["priority"],
            environment=draft["environment"],
            worker_type="manager",
            owner_session="coordinator",
            reasoning=draft["reasoning"],
            model=draft["model"],
            speed=draft["speed"],
            authorization_policy=authorization_policy,
            requires_deploy=draft["requires_deploy"],
            state="PLANNED",
            source_request_id=intake["id"],
        )
        task_ids = [parent["id"]]
        for route in routes[:5]:
            child = self.control.create_task(
                title="{} · {}".format(route.owner_session, draft["title"])[:180],
                idempotency_key="intake:{}:child:{}".format(
                    intake["idempotency_key"], route.owner_session
                ),
                scope_summary="{}；{}".format(route.reason, draft["scope"])[:500],
                priority=draft["priority"],
                environment=draft["environment"],
                repository=draft["repository"],
                base_branch=draft.get("base_branch", ""),
                worker_type=route.worker_type,
                owner_session=route.owner_session,
                reasoning=draft["reasoning"],
                model=draft["model"],
                speed=draft["speed"],
                authorization_policy=authorization_policy,
                requires_deploy=(
                    draft["requires_deploy"]
                    and route.owner_session == "infrastructure"
                ),
                state="PLANNED",
                parent_id=parent["id"],
                source_request_id=intake["id"],
            )
            task_ids.append(child["id"])
        draft["owner_session"] = "coordinator"
        draft["worker_type"] = "manager"
        draft["route_reason"] = "跨域任务由 Qingtian 拆分，coordinator 负责父任务"
        return task_ids

    def _store_uploads(
        self, intake_id: str, uploads: Sequence[AttachmentUpload]
    ) -> None:
        if not uploads:
            return
        target_dir = self.intake_root / intake_id
        target_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.chmod(str(target_dir), 0o700)
        total = 0
        seen_hashes = set()
        created_paths: List[Path] = []
        try:
            for upload in uploads:
                name = sanitize_filename(upload.name)
                suffix = Path(name).suffix.lower()
                declared = (upload.mime or "application/octet-stream").split(";", 1)[0].lower()
                aliases = DECLARED_MIME_ALIASES.get(suffix, {MIME_BY_SUFFIX[suffix]})
                if declared not in aliases:
                    raise IntakeError("附件 MIME 与扩展名不一致：{}".format(name))
                attachment_id = new_attachment_id()
                path = target_dir / "{}-{}".format(attachment_id, name)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(str(path), flags, 0o600)
                digest = hashlib.sha256()
                size = 0
                try:
                    while True:
                        chunk = upload.file.read(1024 * 1024)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise IntakeError("附件流必须为二进制")
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise IntakeError(
                                "单个附件不能超过 25 MB", status=413
                            )
                        if total > MAX_TOTAL_BYTES:
                            raise IntakeError("附件总计不能超过 100 MB", status=413)
                        os.write(fd, chunk)
                        digest.update(chunk)
                finally:
                    os.close(fd)
                created_paths.append(path)
                os.chmod(str(path), 0o600)
                _validate_signature(path, suffix)
                if suffix in {".txt", ".md"}:
                    try:
                        if contains_secret(path.read_text(encoding="utf-8")):
                            self.db.execute(
                                "UPDATE intakes SET secret_warning=1 WHERE id=?",
                                (intake_id,),
                            )
                    except OSError:
                        pass
                sha256 = digest.hexdigest()
                if sha256 in seen_hashes:
                    path.unlink()
                    created_paths.remove(path)
                    continue
                seen_hashes.add(sha256)
                relative = str(path.relative_to(self.paths["root"].resolve()))
                self.db.execute(
                    """
                    INSERT INTO intake_attachments(
                        id, intake_id, name, mime, size, sha256, local_path, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attachment_id,
                        intake_id,
                        name,
                        MIME_BY_SUFFIX[suffix],
                        size,
                        sha256,
                        relative,
                        utc_now(),
                    ),
                )
        except Exception:
            with self.db.connect() as connection:
                connection.execute(
                    "DELETE FROM intake_attachments WHERE intake_id=?", (intake_id,)
                )
            if target_dir.exists() and target_dir.parent == self.intake_root:
                shutil.rmtree(str(target_dir))
            raise

    def _controlled_path(self, relative: str) -> Path:
        candidate = self.paths["root"] / relative
        if candidate.is_symlink():
            raise IntakeError("禁止读取符号链接附件")
        resolved = candidate.resolve(strict=True)
        if self.intake_root not in resolved.parents:
            raise IntakeError("附件路径越界")
        current = resolved.parent
        while current != self.intake_root:
            if current.is_symlink():
                raise IntakeError("附件目录不能是符号链接")
            current = current.parent
        return resolved

    def _mark_status(self, intake_id: str, status_value: str, error: str) -> None:
        self.db.execute(
            "UPDATE intakes SET status=?, error=?, updated_at=? WHERE id=?",
            (
                status_value,
                redact_text(error, max_chars=500),
                utc_now(),
                intake_id,
            ),
        )

    def _add_message(
        self,
        intake_id: str,
        role: str,
        kind: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO intake_messages(
                id, intake_id, role, kind, content, metadata_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "message-{}".format(uuid.uuid4().hex),
                intake_id,
                role,
                kind,
                redact_text(content, max_chars=2000),
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )

    @staticmethod
    def _clean_advanced(value: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise IntakeError("高级设置必须是对象")
        if "requires_deploy" in value and not isinstance(value["requires_deploy"], bool):
            raise IntakeError("requires_deploy 必须是 boolean")
        for field in ("model", "reasoning"):
            if field in value and (not isinstance(value[field], str) or not value[field]):
                raise IntakeError("{} must be a nonempty string".format(field))
        try:
            runtime_policy(value.get("reasoning"), requested_model=value.get("model"), requested_speed=value.get("speed"))
            runtime_policy(value.get("planner_reasoning"), requested_model=value.get("planner_model"),
                           requested_speed=value.get("planner_speed"), role="planner")
        except ValueError as exc:
            raise IntakeError(str(exc)) from exc
        allowed = {
            "title",
            "scope_summary",
            "priority",
            "environment",
            "repository",
            "base_branch",
            "worker_type",
            "requires_deploy",
            "model", "reasoning", "speed",
            "planner_model", "planner_reasoning", "planner_speed",
        }
        clean: Dict[str, Any] = {}
        for key in allowed:
            if key in {"model", "reasoning", "speed", "planner_model", "planner_reasoning", "planner_speed"} and key in value:
                if not isinstance(value[key], str) or not value[key]:
                    raise IntakeError("执行参数必须是明确非空字符串：" + key)
                clean[key] = value[key]
                continue
            item = value.get(key)
            if item in (None, "", False):
                continue
            if isinstance(item, bool):
                clean[key] = item
            elif isinstance(item, (str, int)):
                clean[key] = redact_text(str(item), max_chars=500)
        return clean


def memory_upload(name: str, mime: str, data: bytes) -> AttachmentUpload:
    return AttachmentUpload(name=name, mime=mime, file=io.BytesIO(data))
