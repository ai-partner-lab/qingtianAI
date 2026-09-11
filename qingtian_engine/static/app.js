const STATES = [
  ["INBOX", "收件箱"],
  ["RUNNING", "执行中"],
  ["WAITING", "等待中"],
  ["PAUSED", "已暂停"],
  ["PLAN_ONLY", "仅规划"],
  ["VERIFYING", "验收中"],
  ["DONE", "已完成"],
  ["CANCELED", "已取消"],
];
const WAITING_CATEGORY_ORDER = [
  "external",
  "user",
  "internal_qa",
  "internal_release",
  "dependency",
  "execution_recovery",
  "internal",
];

const stateColors = {
  INBOX: "#70a5ff",
  RUNNING: "#37d6d0",
  WAITING: "#ffbf69",
  PAUSED: "#9aa7b5",
  PLAN_ONLY: "#b996ff",
  VERIFYING: "#ff3d9a",
  DONE: "#45d69c",
  CANCELED: "#66717e",
};
const stateLabels = Object.fromEntries(STATES);

function taskDisplayState(task) {
  if (task.display_state) return task.display_state;
  if (["PLANNED", "QUEUED"].includes(task.state)) return "INBOX";
  if (task.state === "FAILED") return "WAITING";
  return task.state;
}

let dashboard = null;
let engineMode = "";
let engineReadOnly = false;
let runtimeIdentity = null;
const guideSteps = [
  ["确认当前运行库", "这里的数据来自真实 SQLite。默认手动模式不会自动领取、重试或续跑。可以先在隔离环境完成无需凭据的自检。", "qingtian doctor\nqingtian selftest"],
  ["登记你自己的项目", "先登记允许执行的 Git 仓库和明确开发基线；不会复制现有 checkout 的未提交修改。仓库或路径不明确时，引擎会阻断并要求补充。", "qingtian project --help"],
  ["准备执行器与知识", "真实 Worker 使用你自己的 Codex 登录。知识库是可选引用源，不是运行事实源，更不会带来旧任务授权。", "codex login\nqingtian knowledge --help"],
  ["提交一条新任务", "点击“交给擎天”：只分析用于规划；选择“实现并测试”才明确请求派发。复杂任务可拆为父子任务，关联依赖。不要把历史文档里的待办当成新授权。", "qingtian task add --help\nqingtian dispatch --help"],
  ["检查证据再收口", "实时看板显示执行、等待和验收状态。Worker 退出 0 后仍须证据门禁；缺少浏览器、部署或人工授权时如实保持阻塞。先验证项目自己的验收规则，再考虑开启自动调度。", "qingtian report\nqingtian feedback --consumer QT-00"],
];
let guideStep = 0;
function renderGuide() {
  const [title, description, command] = guideSteps[guideStep];
  document.querySelector("#guideContent").replaceChildren(el("h3", "", title), el("p", "", description), el("code", "", command));
  const progress = document.querySelector("#guideProgress");
  progress.replaceChildren();
  guideSteps.forEach((step, index) => {
    const button = el("button", "", String(index + 1).padStart(2, "0"));
    button.title = step[0];
    button.setAttribute("aria-current", index === guideStep ? "step" : "false");
    button.addEventListener("click", () => { guideStep = index; renderGuide(); });
    progress.append(button);
  });
  document.querySelector("#guideCounter").textContent = `${guideStep + 1} / ${guideSteps.length}`;
  document.querySelector("#guidePrevious").disabled = guideStep === 0;
  document.querySelector("#guideNext").textContent = guideStep === guideSteps.length - 1 ? "开始使用" : "下一步";
}
document.querySelector("#guideButton").addEventListener("click", () => { renderGuide(); document.querySelector("#guideDialog").showModal(); });
document.querySelector("#closeGuide").addEventListener("click", () => document.querySelector("#guideDialog").close());
document.querySelector("#guidePrevious").addEventListener("click", () => { guideStep = Math.max(0, guideStep - 1); renderGuide(); });
document.querySelector("#guideNext").addEventListener("click", () => {
  if (guideStep === guideSteps.length - 1) document.querySelector("#guideDialog").close();
  else { guideStep++; renderGuide(); }
});
let reportMarkdown = "";
let reportRows = [];
let reportSummaryData = null;
let reportFilter = "ALL";
let reportReturnFocus = null;
let pendingFiles = [];
let intakeIdempotencyKey = "";
let intakeSending = false;
let eventSource = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let refreshPromise = null;
let lastLiveAt = 0;
let connectionMode = "connecting";
let onlyMyActions = false;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let lastVersion = 0; // Whole-dashboard snapshot, never an SSE consumption cursor.
let lastCursor = 0;
let dashboardRenderPending = false;
let managerEntryRenderedHost = null;
let managerEntryRenderKey = null;
try {
  const saved = Number(sessionStorage.getItem("qingtian-event-cursor") || 0);
  lastCursor = Number.isSafeInteger(saved) && saved >= 0 ? saved : 0;
} catch (_error) {
  lastCursor = 0;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setConnection(mode, label) {
  connectionMode = mode;
  const health = document.querySelector("#health");
  health.className = `pill connection-status is-${mode}`;
  health.textContent = label;
  health.title = {
    live: "SSE 实时连接正常",
    connecting: "正在建立实时连接",
    reconnecting: "连接中断，正在自动恢复",
    fallback: "SSE 不可用，已静默降级为低频同步",
    offline: "当前无法连接本地控制面",
  }[mode] || label;
}

function rememberVersion(value) {
  lastVersion = Math.max(0, Number(value || 0));
}

function rememberCursor(value) {
  const cursor = Number(value);
  if (!Number.isSafeInteger(cursor) || cursor < 0) throw new Error("Invalid event cursor");
  lastCursor = cursor;
  try { sessionStorage.setItem("qingtian-event-cursor", String(lastCursor)); }
  catch (_error) { /* The consumed-frame cursor can remain in memory. */ }
}

function animateNumber(node, from, to) {
  if (reducedMotion.matches || from === to) {
    node.textContent = `${to}%`;
    return;
  }
  const started = performance.now();
  const duration = 420;
  const tick = (now) => {
    const ratio = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - ratio, 3);
    node.textContent = `${Math.round(from + (to - from) * eased)}%`;
    if (ratio < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    ...options,
    headers,
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || "请求失败");
    error.status = response.status;
    error.code = payload.code;
    throw error;
  }
  return payload;
}

function uniqueKey() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function fileIcon(mime) {
  if (mime.startsWith("image/")) return "IMG";
  if (mime === "application/pdf") return "PDF";
  if (mime.includes("wordprocessing")) return "DOCX";
  if (mime.includes("spreadsheet")) return "XLSX";
  return "FILE";
}

function allowedFile(file) {
  const suffix = `.${(file.name.split(".").pop() || "").toLowerCase()}`;
  const allowed = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".docx", ".xlsx", ".txt", ".md"];
  return allowed.includes(suffix);
}

function renderPendingFiles() {
  const tray = document.querySelector("#attachmentTray");
  tray.replaceChildren();
  for (const item of pendingFiles) {
    const card = el("article", "attachment-chip");
    const preview = el("div", "attachment-preview");
    if (item.file.type.startsWith("image/")) {
      const image = document.createElement("img");
      image.src = item.url;
      image.alt = "";
      preview.append(image);
    } else {
      preview.append(el("span", "", fileIcon(item.file.type)));
    }
    const meta = el("div", "attachment-meta");
    meta.append(el("strong", "", item.file.name), el("small", "", `${item.file.type || "未知类型"} · ${formatBytes(item.file.size)}`));
    const remove = el("button", "attachment-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除 ${item.file.name}`);
    remove.addEventListener("click", () => {
      URL.revokeObjectURL(item.url);
      pendingFiles = pendingFiles.filter((candidate) => candidate !== item);
      renderPendingFiles();
    });
    card.append(preview, meta, remove);
    tray.append(card);
  }
  document.querySelector("#attachmentQuota").textContent = pendingFiles.length
    ? `${pendingFiles.length}/10 · 共 ${formatBytes(pendingFiles.reduce((sum, item) => sum + item.file.size, 0))}`
    : "最多 10 个 · 单个 25 MB · 共 100 MB";
}

function addFiles(files) {
  const status = document.querySelector("#composerStatus");
  const additions = [];
  for (const original of Array.from(files)) {
    let file = original;
    if (!file.name && file.type.startsWith("image/")) {
      const suffix = file.type === "image/jpeg" ? "jpg" : (file.type.split("/")[1] || "png");
      file = new File([file], `粘贴截图-${Date.now()}.${suffix}`, {type: file.type});
    }
    if (!allowedFile(file)) {
      status.textContent = `不支持 ${file.name || "该文件"} 的类型`;
      status.className = "error";
      continue;
    }
    if (file.size > 25 * 1024 * 1024) {
      status.textContent = `${file.name} 超过 25 MB`;
      status.className = "error";
      continue;
    }
    const duplicate = [...pendingFiles, ...additions].some((item) =>
      item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified
    );
    if (!duplicate) additions.push({file, url: URL.createObjectURL(file)});
  }
  const candidate = [...pendingFiles, ...additions];
  if (candidate.length > 10 || candidate.reduce((sum, item) => sum + item.file.size, 0) > 100 * 1024 * 1024) {
    additions.forEach((item) => URL.revokeObjectURL(item.url));
    status.textContent = "附件最多 10 个，总计不能超过 100 MB";
    status.className = "error";
    return;
  }
  pendingFiles = candidate;
  status.textContent = additions.length ? `已添加 ${additions.length} 个附件` : status.textContent;
  status.className = "";
  renderPendingFiles();
}

function renderStats(data) {
  const host = document.querySelector("#stats");
  host.replaceChildren();
  const rolling = data.rolling_24h_summary || {};
  for (const [state, label] of STATES) {
    const count = state === "DONE"
      ? Number(rolling.done || 0)
      : (data.columns[state] || []).length;
    const displayLabel = state === "DONE" ? "过去24小时完成" : label;
    const card = el("div", "stat");
    const strong = el("strong", "", String(count));
    strong.style.color = stateColors[state];
    card.append(strong, el("span", "", displayLabel));
    if (state === "WAITING") {
      card.append(el("small", "waiting-breakdown", waitingBreakdown(data)));
    } else if (state === "PAUSED") {
      card.append(el("small", "waiting-breakdown", "用户明确暂停 · 不参与调度"));
    } else if (state === "PLAN_ONLY") {
      card.append(el("small", "waiting-breakdown", "仅方案评审 · 不参与调度或人工待办"));
    } else if (state === "CANCELED") {
      card.append(el("small", "waiting-breakdown", "终态记录 · 不计入等待"));
    }
    host.append(card);
  }
  const average = el("div", "stat");
  const averageValue = el("strong", "", `${Number(rolling.average_progress || 0)}%`);
  averageValue.style.color = "#b996ff";
  average.append(averageValue, el("span", "", "过去24小时平均阶段指数（非完成率）"));
  host.append(average);
  if (data.qingtian_v2) {
    const v2 = data.qingtian_v2;
    const health = el("div", `stat orchestrator-health is-${String(v2.status || "starting").toLowerCase()}`);
    const value = el("strong", "", engineMode === "manual" ? "手动" : v2.status === "READY" ? "正常" : (v2.status || "启动中"));
    value.style.color = v2.status === "READY" ? "#45d69c" : "#ffbf69";
    health.append(
      value,
      el("span", "", "擎天 2.0 恢复协调器"),
      el("small", "waiting-breakdown", engineMode === "manual" ? "自动调度未开启 · 已有运行继续对账" : `证据缺口 ${Number(v2.evidence?.missing || 0)} · 异常队列 ${Number(v2.dead_letters?.open || 0)}`),
    );
    host.append(health);
  }
}

function waitingBreakdown(data) {
  const counts = data.waiting_summary || {};
  const labels = data.waiting_labels || {};
  const parts = WAITING_CATEGORY_ORDER
    .filter((key) => Number(counts[key] || 0) > 0)
    .map((key) => `${labels[key] || key} ${Number(counts[key])}`);
  return parts.length ? parts.join(" · ") : "暂无等待事项";
}

function waitingAndPausedBreakdown(data) {
  const waiting = waitingBreakdown(data);
  const paused = Number(data.paused_count || 0);
  if (paused <= 0) return waiting;
  return waiting === "暂无等待事项" ? `已暂停 ${paused}` : `${waiting} · 已暂停 ${paused}`;
}

function renderActionOverview(data) {
  const summary = data.action_summary || {};
  const userCount = Number(summary.user || 0);
  const externalCount = Number(summary.external || 0);
  const layout = document.querySelector(".layout");
  const overview = document.querySelector(".mobile-action-overview");
  const panel = document.querySelector("#interventionPanel");
  layout.classList.toggle("has-user-actions", userCount > 0);
  overview.classList.toggle("has-user-actions", userCount > 0);
  panel.classList.toggle("has-user-actions", userCount > 0);
  if (!userCount && !panel.classList.contains("is-open")) {
    panel.setAttribute("aria-hidden", "true");
  } else {
    panel.removeAttribute("aria-hidden");
  }
  document.querySelector("#mobileActionLabel").textContent = userCount
    ? `待我处理 ${userCount}`
    : "✓ 暂无需处理";
  document.querySelector("#mobileActionBreakdown").textContent =
    waitingAndPausedBreakdown(data);
  document.querySelector("#interventionCount").textContent = String(userCount);
  const filter = document.querySelector("#myActionFilter");
  filter.setAttribute("aria-pressed", String(onlyMyActions));
  filter.textContent = onlyMyActions ? "◎ 全部" : "◉ 我的";
  filter.classList.toggle("is-active", onlyMyActions);
  document.querySelector("#mobileActionBar").setAttribute(
    "aria-expanded",
    String(document.querySelector("#interventionPanel").classList.contains("is-open"))
  );
}

function formatActionAge(value) {
  if (!value) return "刚刚进入等待";
  const started = new Date(value).getTime();
  if (!Number.isFinite(started)) return "等待时长未知";
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  if (seconds < 60) return "等待不足 1 分钟";
  if (seconds < 3600) return `已等待 ${Math.floor(seconds / 60)} 分钟`;
  if (seconds < 86400) return `已等待 ${Math.floor(seconds / 3600)} 小时`;
  return `已等待 ${Math.floor(seconds / 86400)} 天`;
}

// One action projection for board, intervention cards, and fresh task detail.
function humanAction(task) {
  const raw = Object.prototype.hasOwnProperty.call(task, "action_owner_kind");
  const stored = raw ? {
    owner_kind: task.action_owner_kind, owner: task.action_owner, text: task.action_text,
    due: task.action_due, sensitive: task.action_sensitive, kind: task.action_kind,
    since: task.human_action?.since,
  } : (task.human_action || {});
  const inactive = ["PAUSED", "PLAN_ONLY", "CANCELED", "DONE"].includes(taskDisplayState(task)) || Boolean(task.state_resolution?.live_run);
  return {
    owner_kind: inactive ? "none" : (stored.owner_kind || "none"),
    owner: typeof stored.owner === "string" ? stored.owner : "",
    text: typeof stored.text === "string" ? stored.text.trim() : "",
    due: stored.due || null, sensitive: stored.sensitive === true || stored.sensitive === 1,
    kind: ({approve: "approval", "provide-info": "information", "external-operation": "external_operation", approval: "approval", information: "information", external_operation: "external_operation"})[stored.kind] || "unspecified",
    since: stored.since || null,
  };
}

function humanActionSummary(task) {
  const action = humanAction(task);
  if (["user", "external"].includes(action.owner_kind)) {
    const owner = action.owner_kind === "user" ? "你需要" : `等待 ${action.owner || "外部主责"}`;
    return `${owner}：${action.text || "具体要求尚未登记，请联系任务主责补充缺什么、提交渠道和下一步。"}`;
  }
  if (["PAUSED", "PLAN_ONLY", "CANCELED", "DONE"].includes(taskDisplayState(task))) {
    return `无需你处理 · ${stateLabels[taskDisplayState(task)] || taskDisplayState(task)}`;
  }
  return task.display_action || task.operator_status?.next_action || "无需你处理";
}

function actionInstruction(action) {
  if (action.kind === "approval") return "审批事项：先在原授权渠道明确批准范围。这里的处理声明不是批准，不授予迁移、部署、付款或预算权限。";
  if (action.kind === "information") return "资料事项：通过要求中指定的渠道提供材料。敏感凭据只走安全入口，提交声明不会代为上传资料。";
  if (action.kind === "external_operation") return "外部操作：由指定主责在对应系统完成后，再提交处理声明；这里不会代为操作或启动执行。";
  return "先核对具体要求与原授权渠道：批准、提供资料、完成外部操作是不同步骤。这里不会代批、代交资料或代为执行。";
}

function actionFingerprint(task) {
  const action = humanAction(task);
  return JSON.stringify({
    id: task.id, state: task.state, display_state: taskDisplayState(task),
    owner_kind: action.owner_kind, owner: action.owner, text: action.text,
    due: action.due, sensitive: action.sensitive, kind: action.kind,
    version: task.action_version ?? null,
  });
}

async function submitActionStatement(task, button, feedback) {
  if (engineReadOnly || button.disabled || humanAction(task).owner_kind !== "user") return;
  if (humanAction(task).sensitive || task.action_version == null) return;
  button.disabled = true;
  let submitted = false;
  feedback.textContent = "正在重新核对当前要求…";
  try {
    const fresh = await api(`/api/tasks/${encodeURIComponent(task.id)}`);
    if (!button.isConnected || !document.querySelector("#detailDialog").open) return;
    if (fresh.id !== task.id || actionFingerprint(fresh) !== actionFingerprint(task)) {
      feedback.textContent = "要求或任务状态已变化，未提交。请重新打开详情核对。";
      return;
    }
    const action = humanAction(fresh);
    const accepted = window.confirm(`请确认你已按原授权渠道处理以下要求：\n\n${action.text}\n\n此操作只提交处理声明：清除此项待办并进入内部复核，不代表验收通过，不授予权限，也不会派发、恢复或完成任务。尚未处理请取消。`);
    if (!accepted) {
      feedback.textContent = "已取消，待办与任务状态未改变。";
      return;
    }
    if (engineReadOnly || !button.isConnected || !document.querySelector("#detailDialog").open) return;
    if (humanAction(fresh).sensitive || fresh.action_version == null) {
      feedback.textContent = "当前要求需要安全渠道处理或缺少版本信息，未提交。请联系主责核验接入和授权路径。";
      return;
    }
    const body = {expected_action_version: fresh.action_version};
    submitted = true;
    await api(`/api/tasks/${encodeURIComponent(task.id)}/complete-human-action`, {
      method: "POST", body: JSON.stringify(body),
    });
    feedback.textContent = "已提交处理声明，等待内部复核；不代表审批或验收通过。";
    await refresh();
    await openDetail(task.id);
  } catch (error) {
    if (error.status === 409) {
      await openDetail(task.id, "具体要求或任务状态已变化，本次声明未写入。请重新阅读最新要求后再确认。");
    } else {
      feedback.textContent = submitted
        ? "提交结果尚未确认，请重新打开详情核对，勿重复提交。此操作不代表审批或验收通过。"
        : `无法核对当前要求：${error.message}。未提交，请联系主责核对范围。`;
    }
  } finally {
    button.disabled = engineReadOnly || submitted;
  }
}

function renderActionRequirement(task) {
  const action = humanAction(task);
  const section = el("section", `detail-action action-${action.owner_kind} action-requirements`);
  section.append(el("h3", "", "具体要求"), el("p", "action-body", humanActionSummary(task)));
  if (["user", "external"].includes(action.owner_kind)) {
    section.append(
      el("p", "action-responsibility", `由谁处理：${action.owner || (action.owner_kind === "user" ? "你" : "外部主责")} · ${action.due ? "截止 " + formatReportTime(action.due) : "未设截止"}`),
      el("p", "action-next-step", actionInstruction(action)),
    );
    const general = task.operator_status?.next_action;
    if (general && general !== action.text) section.append(el("small", "action-context", `系统提示：${general}`));
  }
  if (action.sensitive) section.append(el("small", "secret-entry-note", "敏感信息只通过指定安全渠道提供，禁止粘贴聊天或处理说明。"));
  if (action.owner_kind === "user") {
    const complete = el("button", "secondary", "提交处理声明，进入复核");
    complete.type = "button";
    complete.disabled = engineReadOnly || action.sensitive || task.action_version == null;
    const explanation = engineReadOnly ? "合成演示只读，不提交声明、不修改待办。"
      : action.sensitive ? "敏感动作不能使用普通处理声明。请按上述要求通过指定安全渠道提供信息，再由任务主责核验结果并明确后续授权；不要把 Secret 粘贴到聊天。"
      : task.action_version == null ? "当前服务未提供动作版本，无法安全提交。请联系接入主责核对服务版本，再重新读取本任务；具体资料或审批仍通过原指定渠道处理。"
      : "只有实际处理后才提交；这不是批准按钮，也不触发派发。";
    const feedback = el("p", "action-feedback", explanation);
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");
    complete.addEventListener("click", () => submitActionStatement(task, complete, feedback));
    section.append(complete, feedback);
  }
  return section;
}

function interventionCard(task, changed = false) {
  const action = humanAction(task);
  const card = el("article", `intervention-card kind-${action.owner_kind || "none"}`);
  card.dataset.taskId = task.id;
  card.tabIndex = 0;
  if (changed) card.classList.add("is-updated");
  const head = el("div", "intervention-card-head");
  head.append(
    el("span", `intervention-priority p${task.priority}`, `P${task.priority}`),
    el("span", "intervention-owner", action.owner_kind === "user" ? "需要你处理" : "等待外部")
  );
  card.append(head, el("h3", "", task.title), el("p", "intervention-action", humanActionSummary(task)));
  if (task.operator_status?.next_action) card.append(el("small", "action-context", `系统提示：${task.operator_status.next_action}`));
  const timing = el("p", "intervention-timing");
  timing.textContent = `${action.owner || (action.owner_kind === "user" ? "你" : "外部主责")} · ` +
    `${action.due ? `截止 ${formatReportTime(action.due)}` : "未设截止"} · ${formatActionAge(action.since)}`;
  card.append(timing);
  if (action.sensitive) {
    card.append(el("p", "secret-entry-note", "生产 Key 仅通过安全 Secret 入口提供，禁止粘贴聊天。"));
  }
  const actions = el("div", "intervention-actions");
  if (action.owner_kind === "user") {
    const handle = el("button", "primary compact-action", "查看要求并提交处理声明");
    handle.type = "button";
    handle.addEventListener("click", (event) => {
      event.stopPropagation();
      openDetail(task.id);
    });
    actions.append(handle);
  } else if (action.sensitive) {
    const secure = el("button", "secondary compact-action", "查看安全提交方式");
    secure.type = "button";
    secure.addEventListener("click", (event) => {
      event.stopPropagation();
      openDetail(task.id);
    });
    actions.append(secure);
  } else {
    const remind = el("button", "secondary compact-action", "记录提醒（不发送）");
    remind.disabled = engineReadOnly;
    remind.type = "button";
    remind.title = "在控制面记录一次提醒，不会发送敏感内容";
    remind.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (engineReadOnly) return;
      remind.disabled = true;
      remind.textContent = "记录中…";
      try {
        await api(`/api/tasks/${task.id}/remind-external`, {
          method: "POST",
          body: "{}",
        });
        remind.textContent = "已记录提醒";
        await refresh();
      } catch (error) {
        remind.disabled = false;
        remind.textContent = error.message;
      }
    });
    actions.append(remind);
  }
  card.append(actions);
  card.addEventListener("click", () => openDetail(task.id));
  card.addEventListener("keydown", (event) => {
    if (event.target === card && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openDetail(task.id);
    }
  });
  return card;
}

function renderInterventions(data, changedTaskIds = new Set()) {
  const host = document.querySelector("#interventions");
  const actionable = (data.tasks || []).filter((task) =>
    ["user", "external"].includes(task.human_action?.owner_kind)
  );
  const userTasks = actionable.filter((task) => task.human_action?.owner_kind === "user");
  const externalTasks = actionable.filter((task) => task.human_action?.owner_kind === "external");
  host.replaceChildren();
  if (userTasks.length) {
    const section = el("section", "intervention-group user-group");
    const heading = el("div", "intervention-group-head");
    heading.append(el("h3", "", "需要你处理"), el("span", "", String(userTasks.length)));
    section.append(heading);
    for (const task of userTasks) {
      section.append(interventionCard(task, changedTaskIds.has(task.id)));
    }
    host.append(section);
  }
  if (externalTasks.length && !onlyMyActions) {
    const external = el("details", "external-waiting");
    if (!userTasks.length) external.open = true;
    const summary = el("summary");
    summary.append(
      el("span", "", `外部等待 ${externalTasks.length}`),
      el("small", "", "展开查看")
    );
    external.append(summary);
    const body = el("div", "external-waiting-body");
    for (const task of externalTasks) {
      body.append(interventionCard(task, changedTaskIds.has(task.id)));
    }
    external.append(body);
    host.append(external);
  }
  if (!userTasks.length && (!externalTasks.length || onlyMyActions)) {
    const empty = el("div", "intervention-empty");
    empty.append(el("strong", "", "✓ 暂无需要你处理的事项"));
    host.append(empty);
  }
}

function filtered(tasks) {
  const query = document.querySelector("#search").value.trim().toLowerCase();
  return tasks.filter((task) => {
    if (onlyMyActions && task.human_action?.owner_kind !== "user") return false;
    if (!query) return true;
    return [
      task.title,
      task.scope_summary,
      task.owner_session,
      task.repository,
      task.display_action,
      humanAction(task).text,
      task.human_action?.owner,
      task.waiting_category?.label,
    ].join(" ").toLowerCase().includes(query);
  });
}

function durationLabel(seconds) {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  return minutes < 1 ? "不足 1 分钟" : minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
}

function taskActivity(task) {
  const activity = task.activity || {};
  if (activity.synthetic) return "合成演示 · 未启动 Worker";
  const parts = [];
  const started = Date.parse(activity.run_started_at || task.started_at || "");
  const ended = Date.parse(activity.run_finished_at || "");
  if (Number.isFinite(started)) parts.push(`已运行 ${durationLabel(((Number.isFinite(ended) ? ended : Date.now()) - started) / 1000)}`);
  const latest = Date.parse(activity.last_event_at || "");
  if (Number.isFinite(latest)) {
    const silentSeconds = Math.max(0, (Date.now() - latest) / 1000);
    parts.push(`最近活动 ${new Date(latest).toLocaleTimeString()}`);
    if (task.state === "RUNNING" && silentSeconds > 180) parts.push(`${durationLabel(silentSeconds)} 无新事件（不等于卡死）`);
  } else if (task.state === "RUNNING") parts.push("尚未收到 Worker 活动事件");
  if (["PROCESS_LOST", "NO_RUN"].includes(task.runtime_status?.code)) parts.push(task.runtime_status.label);
  return parts.join(" · ");
}

function taskCard(task, previousTask = null, changed = false) {
  const node = document.querySelector("#taskCardTemplate").content.firstElementChild.cloneNode(true);
  node.dataset.taskId = task.id;
  node.querySelector(".priority").textContent = `P${task.priority}`;
  const visualState = taskDisplayState(task);
  const badge = node.querySelector(".state-badge");
  badge.classList.add(`state-${visualState.toLowerCase()}`);
  badge.style.setProperty("--state-color", stateColors[visualState] || "#ff6b75");
  node.querySelector(".state-label").textContent =
    visualState === "WAITING" && task.waiting_category
      ? task.waiting_category.label
      : (stateLabels[visualState] || visualState);
  node.querySelector("h3").textContent = task.title;
  const action = node.querySelector(".action-summary");
  action.textContent = humanActionSummary(task);
  const actionKind = humanAction(task).owner_kind;
  action.classList.add(`action-${actionKind}`);
  if (task.state === "FAILED") action.classList.add("action-failed");
  if (task.human_action?.sensitive) {
    action.classList.add("action-sensitive");
    action.setAttribute("aria-label", `${action.textContent}，敏感信息请勿粘贴聊天`);
  }
  const progressTrack = node.querySelector(".progress");
  progressTrack.hidden = true;
  const progressValue = node.querySelector(".progress-value");
  progressValue.textContent = `阶段：${stateLabels[visualState] || visualState}`;
  progressValue.setAttribute("aria-label", "当前阶段，不是实际工作完成百分比");
  const activity = el("div", "task-activity", taskActivity(task));
  activity.classList.toggle("is-stale", ["EVENT_STALE", "PROCESS_LOST", "NO_RUN"].includes(task.runtime_status?.code));
  node.append(activity);
  if (changed) node.classList.add("is-updated");
  if (previousTask && previousTask.state !== "DONE" && task.state === "DONE") {
    node.classList.add("just-completed");
  }
  node.addEventListener("click", () => openDetail(task.id));
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") openDetail(task.id);
  });
  return node;
}

function renderBoard(data, previousData = null, changedTaskIds = new Set(), animate = true) {
  const board = document.querySelector("#board");
  const before = new Map();
  if (animate && !reducedMotion.matches) {
    board.querySelectorAll("[data-task-id]").forEach((node) => {
      before.set(node.dataset.taskId, node.getBoundingClientRect());
    });
  }
  const previousById = new Map((previousData?.tasks || []).map((task) => [task.id, task]));
  const fragment = document.createDocumentFragment();
  for (const [state, label] of STATES) {
    const column = el("section", "column");
    column.dataset.state = state;
    const head = el("div", "column-head");
    const cards = el("div", "cards");
    const tasks = filtered(data.columns[state] || []);
    const total = (data.columns[state] || []).length;
    head.append(
      el("span", "column-title", label),
      el("span", "column-count", onlyMyActions || document.querySelector("#search").value.trim()
        ? `${tasks.length}/${total}`
        : String(total))
    );
    if (state === "WAITING") {
      head.classList.add("has-breakdown");
      head.append(el("small", "column-breakdown", waitingBreakdown(data)));
    }
    if (!tasks.length) cards.append(el("p", "empty", "暂无任务"));
    for (const task of tasks) {
      cards.append(taskCard(task, previousById.get(task.id), changedTaskIds.has(task.id)));
    }
    column.append(head, cards);
    fragment.append(column);
  }
  board.replaceChildren(fragment);
  if (!animate || reducedMotion.matches) return;
  requestAnimationFrame(() => {
    board.querySelectorAll("[data-task-id]").forEach((node) => {
      const first = before.get(node.dataset.taskId);
      if (!first || typeof node.animate !== "function") return;
      const last = node.getBoundingClientRect();
      const dx = first.left - last.left;
      const dy = first.top - last.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
      node.animate(
        [
          {transform: `translate(${dx}px, ${dy}px)`, zIndex: 3},
          {transform: "translate(0, 0)", zIndex: 3},
        ],
        {duration: 360, easing: "cubic-bezier(.2,.8,.2,1)"}
      );
    });
  });
}

function renderSessions(data) {
  const host = document.querySelector("#sessions");
  host.replaceChildren();
  for (const session of data.sessions) {
    const row = el("div", "session");
    const title = el("strong");
    title.append(el("span", "session-code", session.code), document.createTextNode(session.name));
    row.append(title, el("small", "", `${session.worker_type} · ${session.model}/${session.reasoning}\n${session.scope_summary}`));
    host.append(row);
  }
}

function renderEvents(data, previousData = null) {
  const host = document.querySelector("#events");
  const previousIds = new Set((previousData?.events || []).map((event) => event.id));
  host.replaceChildren();
  for (const event of data.events) {
    const row = el("div", "event");
    if (previousData && !previousIds.has(event.id)) row.classList.add("is-new");
    row.append(el("strong", "", event.summary), el("small", "", `${event.producer} · ${event.title}\n${event.occurred_at}`));
    host.append(row);
  }
}

const REPORT_STATES = [
  ["RUNNING", "执行中", "当前正在推进的事项"],
  ["VERIFYING", "验收中", "等待验证与证据闭环的事项"],
  ["WAITING", "等待中", "按外部、用户、内部、依赖与恢复原因分类的事项"],
  ["PAUSED", "已暂停", "用户明确暂停、不参与自动调度的事项"],
  ["PLAN_ONLY", "仅规划", "仅方案评审、不进入自动执行的事项"],
  ["DONE", "已完成", "过去24小时内完成的事项"],
  ["INBOX", "收件箱", "待明确主责或排期的事项"],
];

const REPORT_FILTERS = [
  ["ALL", "纳管事项", "total"],
  ["RUNNING", "执行中", "running"],
  ["VERIFYING", "验收中", "verifying"],
  ["WAITING", "等待中", "waiting"],
  ["PAUSED", "已暂停", "paused"],
  ["PLAN_ONLY", "仅规划", "planned"],
  ["DONE", "已完成", "done"],
];

const EVIDENCE_LABELS = {
  commit: "Commit",
  test: "Test",
  deploy: "Deploy",
  smoke: "Smoke",
};

function formatReportTime(value) {
  if (!value) return "未知";
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return value;
  return time.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });
}

function renderReportSummary(summary) {
  const host = document.querySelector("#reportSummary");
  host.replaceChildren();
  for (const [filter, label, tone] of REPORT_FILTERS) {
    const value = filter === "ALL"
      ? summary.total
      : filter === "DONE"
        ? summary.done
        : summary.counts[filter] || 0;
    const card = el("button", `report-stat report-filter ${tone}`);
    card.type = "button";
    card.dataset.filter = filter;
    card.setAttribute("aria-pressed", String(reportFilter === filter));
    card.title = filter === "ALL" ? "显示全部事项" : `只显示${label}事项；再次点击返回全部`;
    card.append(el("strong", "", String(value)), el("span", "", label));
    card.addEventListener("click", () => {
      const nextFilter = reportFilter === filter && filter !== "ALL" ? "ALL" : filter;
      setReportFilter(nextFilter, {restoreFocus: true});
    });
    host.append(card);
  }
  const progress = el("div", "report-stat average");
  progress.append(
    el("strong", "", `${summary.average_progress}%`),
    el("span", "", "平均阶段指数（非完成率）")
  );
  host.append(progress);
}

function reportEvidence(row) {
  const section = el("section", "report-evidence");
  section.append(el("h5", "", "Commit / Test / Deploy / Smoke 证据"));
  const grid = el("div", "report-evidence-grid");
  for (const [kind, label] of Object.entries(EVIDENCE_LABELS)) {
    const block = el("div", "report-evidence-item");
    const evidenceRows = (row.evidence && row.evidence[kind]) || [];
    const heading = el("div", "evidence-heading");
    heading.append(el("strong", "", label), el("span", "", evidenceRows.length ? String(evidenceRows.length) : "缺失"));
    block.append(heading);
    if (!evidenceRows.length) {
      block.append(el("p", "evidence-missing", "暂无证据"));
    } else {
      const list = el("ul");
      for (const evidence of evidenceRows) {
        const item = el("li");
        const value = evidence.label ? `${evidence.label}：${evidence.value}` : evidence.value;
        item.append(document.createTextNode(value));
        if (evidence.verified) item.append(el("span", "verified", "已验证"));
        list.append(item);
      }
      block.append(list);
    }
    grid.append(block);
  }
  section.append(grid);
  return section;
}

function reportTask(row) {
  const card = el("article", "report-task");
  const head = el("div", "report-task-head");
  const titleWrap = el("div", "report-task-title");
  const titleLine = el("div", "report-title-line");
  titleLine.append(el("span", `report-priority p${row.priority}`, `P${row.priority}`), el("h4", "", row.title));
  titleWrap.append(titleLine, el("p", "report-owner", `主责：${row.owner || "未分配"}`));
  const summary = el("div", "report-short-summary");
  summary.append(el("span", "", "10字概要"), el("strong", "", row.short_summary || "待处理"));
  head.append(titleWrap, summary);

  const progressRow = el("div", "report-progress-row");
  const progressLabel = el("div", "report-progress-label");
  progressLabel.append(el("span", "", "阶段指数（非完成率）"), el("strong", "", `${row.progress}%`));
  const progressTrack = el("div", "report-progress");
  const progressValue = el("span");
  progressValue.style.width = `${Math.max(0, Math.min(100, Number(row.progress) || 0))}%`;
  progressTrack.append(progressValue);
  progressRow.append(progressLabel, progressTrack);

  card.append(head, progressRow, reportEvidence(row));

  const guidance = el("div", "report-guidance");
  if (row.risk) {
    const risk = el("div", "report-risk");
    risk.append(el("span", "", "风险 / 阻塞"), el("p", "", row.risk));
    guidance.append(risk);
  }
  if (row.next_step) {
    const next = el("div", "report-next");
    next.append(el("span", "", "下一步"), el("p", "", row.next_step));
    guidance.append(next);
  }
  if (guidance.childElementCount) card.append(guidance);
  return card;
}

function renderReportGroups(rows, filter = "ALL") {
  const host = document.querySelector("#reportGroups");
  host.replaceChildren();
  const states = filter === "ALL"
    ? REPORT_STATES
    : REPORT_STATES.filter(([state]) => state === filter);
  for (const [state, label, description] of states) {
    const stateRows = rows.filter((row) => row.state === state);
    const group = el("details", `report-group state-${state.toLowerCase()}`);
    if (filter !== "ALL" || ["RUNNING", "VERIFYING", "WAITING", "PAUSED"].includes(state)) group.open = true;
    const summary = el("summary");
    const heading = el("div", "report-group-heading");
    const title = el("div", "report-group-title");
    title.append(el("span", "report-state-dot"), el("h3", "", label), el("span", "report-group-count", String(stateRows.length)));
    heading.append(title, el("p", "", description));
    summary.append(heading, el("span", "report-chevron", "⌄"));
    group.append(summary);
    const body = el("div", "report-group-body");
    if (!stateRows.length) {
      body.append(el("p", "report-empty", "暂无事项"));
    } else {
      for (const row of stateRows) body.append(reportTask(row));
    }
    group.append(body);
    host.append(group);
  }
}

function setReportFilter(filter, {restoreFocus = false} = {}) {
  const validFilters = new Set(REPORT_FILTERS.map(([value]) => value));
  reportFilter = validFilters.has(filter) ? filter : "ALL";
  if (reportSummaryData) renderReportSummary(reportSummaryData);
  renderReportGroups(reportRows, reportFilter);
  const visibleRows = reportFilter === "ALL"
    ? reportRows
    : reportRows.filter((row) => row.state === reportFilter);
  const label = REPORT_FILTERS.find(([value]) => value === reportFilter)?.[1] || "纳管事项";
  document.querySelector("#reportFilterStatus").textContent =
    reportFilter === "ALL"
      ? `当前显示全部 ${visibleRows.length} 项`
      : `已筛选：${label} · ${visibleRows.length} 项`;
  document.querySelector("#copyReportButton").disabled = visibleRows.length === 0;
  if (restoreFocus) {
    requestAnimationFrame(() => {
      document.querySelector(`.report-filter[data-filter="${reportFilter}"]`)?.focus();
    });
  }
}

function renderReport(report) {
  document.querySelector("#reportTitle").textContent = `擎天过去24小时日报 · ${report.date}`;
  document.querySelector("#reportMeta").textContent =
    `统计窗口 ${formatReportTime(report.window_started_at)} ～ ${formatReportTime(report.window_ended_at)}`;
  reportRows = report.rows || [];
  reportSummaryData = report.summary;
  reportFilter = "ALL";
  setReportFilter("ALL");
  reportMarkdown = report.markdown || "";
  document.querySelector("#reportMarkdown").textContent = reportMarkdown;
  document.querySelector("#rawReport").hidden = true;
  const toggle = document.querySelector("#toggleRawReportButton");
  toggle.textContent = "查看原始 Markdown";
  toggle.setAttribute("aria-expanded", "false");
  document.querySelector("#reportCopyStatus").textContent = "";
  document.querySelector(".report-scroll").scrollTop = 0;
}

function compactReportText(value, fallback = "") {
  return String(value || fallback)
    .replace(/\s+/g, " ")
    .replace(/\|/g, "｜")
    .trim();
}

function reportCopyMarkdown() {
  const rows = reportFilter === "ALL"
    ? reportRows
    : reportRows.filter((row) => row.state === reportFilter);
  return rows.map((row) => {
    const title = compactReportText(row.title, "未命名事项");
    const shortSummary = Array.from(compactReportText(row.short_summary, "待补充概要"))
      .slice(0, 10)
      .join("");
    const progress = Math.max(0, Math.min(100, Number(row.progress) || 0));
    const stateLabel = compactReportText(row.state_label, stateLabels[row.state] || row.state);
    return `- ${title}｜${shortSummary}｜阶段指数 ${progress}%（非完成率）｜${stateLabel}`;
  }).join("\n");
}

async function copyReportMarkdown() {
  const status = document.querySelector("#reportCopyStatus");
  const markdown = reportCopyMarkdown();
  if (!markdown) {
    status.textContent = "当前筛选暂无可复制事项";
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(markdown);
    } else {
      const textarea = el("textarea", "clipboard-fallback");
      textarea.value = markdown;
      textarea.setAttribute("readonly", "");
      document.body.append(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("copy unavailable");
    }
    const count = markdown.split("\n").length;
    status.textContent = `已复制当前筛选的 ${count} 项简洁列表`;
  } catch (_error) {
    status.textContent = "复制失败，请重试";
  }
}

function taskSignature(task) {
  return JSON.stringify([
    task.state,
    task.display_state,
    task.progress,
    task.evidence_count,
    task.owner_session,
    task.recovery_status,
    task.runtime_status?.code,
    task.runtime_status?.label,
    task.display_action,
    task.human_action?.owner_kind,
    task.human_action?.owner,
    task.human_action?.text,
  ]);
}

function renderManagerEntry(entry) {
  const host = document.querySelector("#managerEntry");
  if (!host) return;
  const guidance = entry?.onboarding?.action_steps;
  // Ignore unrendered polling metadata, but include every visible input.
  // Stable content must retain selection, focus, copy feedback and scrolling.
  const renderKey = JSON.stringify([
    entry?.status, entry?.thread_id, entry?.scope_valid === false,
    Boolean(entry?.metadata_ready), entry?.role_configuration?.source === "thread/start",
    entry?.pinned, Boolean(entry?.stale), entry?.last_synced_at,
    entry?.error_code, entry?.error_code ? entry?.message || "" : null,
    Array.isArray(guidance) && guidance.length ? guidance.map((step) => [
      step.title || "接入步骤", step.detail || "",
      typeof step.command === "string" ? step.command : null,
    ]) : null,
    runtimeIdentity?.workspace || "",
  ]);
  if (host === managerEntryRenderedHost && renderKey === managerEntryRenderKey && host.children.length) return;
  const labels = {ready: "入口状态待核对", partial: "入口接入未完成", unavailable: "入口暂不可用",
    unsupported: "当前版本不支持", needs_attention: "入口绑定待核对", not_initialized: "尚未初始化",
    initializing: "入口正在同步"};
  const item = el("div", "session");
  item.append(el("strong", "", labels[entry?.status] || "尚未初始化"));
  if (entry?.thread_id) item.append(el("small", "", `入口 ID：${entry.thread_id}`));
  if (entry?.scope_valid === false) item.append(el("small", "", "原绑定不适用于当前工作区或 Codex 配置，请核对接入范围。"));
  if (entry?.metadata_ready) item.append(el("small", "", "名称与置顶元数据已核验；大管家角色尚未验收。"));
  const role = entry?.role_configuration;
  item.append(el("small", "", role?.source === "thread/start"
    ? "角色规则：创建时已提交，尚无回读核验。" : "角色规则：来源未知，需明确配置与验收。"));
  item.append(el("small", "", "完整大管家工作流尚未验收。可用 manager-entry instructions 查看人工接入规则。"));
  const pin = entry?.pinned === true ? "已置顶" : entry?.pinned === false ? "未置顶" : "置顶状态未核验";
  item.append(el("small", "", `${pin} · 最近同步快照${entry?.stale ? "，需要重新同步" : ""}`));
  if (entry?.last_synced_at) item.append(el("small", "", `核验时间：${formatReportTime(entry.last_synced_at)}`));
  if (entry?.error_code) item.append(el("small", "", `${entry.error_code}：${entry.message || ""}`));
  item.append(el("small", "", "首用先核对接入范围；scan_incomplete 不是没有入口，不要删除绑定或重复创建。"));
  const steps = el("ol", "manager-onboarding");
  if (Array.isArray(guidance) && guidance.length) {
    for (const step of guidance) {
      const row = el("li");
      row.append(el("strong", "", step.title || "接入步骤"), el("p", "", step.detail || ""));
      if (typeof step.command === "string" && step.command) {
        row.append(el("code", "manager-command", step.command));
        const copy = el("button", "secondary", "复制命令（不执行）");
        copy.type = "button";
        const feedback = el("small", "conversation-feedback");
        feedback.setAttribute("role", "status");
        copy.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(step.command); feedback.textContent = "命令已复制，请在本机核对范围后手动执行。"; }
          catch (_error) { feedback.textContent = "浏览器未允许复制，请选中命令手动复制。"; }
        });
        row.append(copy, feedback);
      }
      steps.append(row);
    }
  } else {
    for (const detail of [
      "在本机运行 qingtian manager-entry guide 获取携带准确 workspace、data-dir 和 transport 的接入步骤；网页不会替你执行命令。",
      "确认一个专用入口的准确 ID。若尚无入口，由你在客户端明确创建；不要重建现有或来源不明的入口。",
      "先 inspect 核对该 ID，再 sync --thread-id 显式绑定。同步会修改选定入口元数据，不启动模型轮次。",
      "导出 instructions，人工审阅并合并到受支持的配置渠道；有效角色和完整工作流仍需单独验收。",
    ]) steps.append(el("li", "", detail));
  }
  item.append(steps, el("small", "", "以上仅展示接入指引；角色规则提交、元数据核验、桌面显示与完整工作流是不同验收层级。"));
  if (runtimeIdentity?.workspace) item.append(el("small", "", `当前服务工作区：${runtimeIdentity.workspace}`));
  if (entry?.thread_id) {
    const id = el("code", "manager-bound-id", entry.thread_id);
    const copy = el("button", "secondary", "复制已记录入口 ID");
    copy.type = "button";
    const feedback = el("small", "conversation-feedback");
    feedback.setAttribute("role", "status");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(entry.thread_id); feedback.textContent = "已复制本地记录的 ID；使用前仍需核对范围和真实入口。"; }
      catch (_error) {
        const range = document.createRange(); range.selectNodeContents(id);
        const selection = window.getSelection(); selection?.removeAllRanges(); selection?.addRange(range);
        feedback.textContent = "已选中 ID，请手动复制；未操作 Codex。";
      }
    });
    item.append(id, copy, feedback);
  }
  host.replaceChildren(item);
  managerEntryRenderedHost = host;
  managerEntryRenderKey = renderKey;
}

function applyDashboard(next, {force = false, changes = []} = {}) {
  // Entry snapshots change independently of the task event cursor.
  renderManagerEntry(next.manager_entry);
  const version = Number(next.version || 0);
  if (!force && !dashboardRenderPending && dashboard && version && version <= lastVersion) return false;
  const previous = dashboard;
  const previousById = new Map((previous?.tasks || []).map((task) => [task.id, task]));
  const changedTaskIds = new Set(changes.map((change) => change.task_id).filter(Boolean));
  for (const task of next.tasks || []) {
    const old = previousById.get(task.id);
    if (!old || taskSignature(old) !== taskSignature(task)) changedTaskIds.add(task.id);
  }
  dashboard = next;
  try {
    // Renderers may read the current candidate through dashboard. Commit its
    // version only after every renderer succeeds; a retry rebuilds all views.
    renderActionOverview(next);
    renderStats(next);
    renderBoard(next, previous, changedTaskIds, Boolean(previous));
    renderInterventions(next, changedTaskIds);
    renderSessions(next);
    renderEvents(next, previous);
  } catch (error) {
    dashboard = previous;
    dashboardRenderPending = true;
    throw error;
  }
  rememberVersion(version);
  dashboardRenderPending = false;
  return true;
}

async function refresh({force = true, quiet = false} = {}) {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const [next, health] = await Promise.all([api("/api/dashboard"), api("/api/health")]);
      engineMode = health.mode || "";
      engineReadOnly = health.read_only === true;
      runtimeIdentity = {workspace: health.workspace, data_dir: health.data_dir};
      document.querySelector("#intakeForm").querySelectorAll("input, textarea, select, button").forEach((control) => {
        control.disabled = engineReadOnly;
      });
      document.querySelector("#submitIntake").textContent = engineReadOnly ? "合成演示 · 只读" : "交给擎天";
      document.querySelector("#composerStatus").textContent = engineReadOnly ? "此演示不派发、不调用模型；请使用 quickstart 接入自己的任务。" : "";
      document.querySelector("#runtimeMode").textContent = health.synthetic
        ? "只读合成演示 · 不调用模型、不修改真实项目 · Ctrl+C 后重开即可重置"
        : health.mode === "manual"
        ? "手动派发 · 后台不自动领取、重试或续跑任务"
        : health.mode === "auto" ? "自动调度已开启" : "模式未确认，请勿派发";
      document.querySelector("#runtimePath").textContent = health.data_dir
        ? `当前运行库：${health.data_dir} · 历史任务不自动导入`
        : "未取得运行库身份，请核对服务。";
      applyDashboard(next, {force});
      if (connectionMode !== "live" && !quiet) setConnection("fallback", "低频同步");
      return true;
    } catch (error) {
      if (connectionMode !== "live") setConnection("offline", "离线");
      return false;
    }
  })();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

function closeEventStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function scheduleReconnect() {
  closeEventStream();
  if (reconnectTimer) return;
  reconnectAttempt += 1;
  if (reconnectAttempt >= 3) setConnection("fallback", "低频同步");
  else setConnection("reconnecting", "重连中");
  const delay = Math.min(30000, 1000 * Math.pow(2, reconnectAttempt - 1));
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connectEventStream();
  }, delay);
}

function connectEventStream() {
  if (!("EventSource" in window)) {
    setConnection("fallback", "低频同步");
    return;
  }
  closeEventStream();
  setConnection(reconnectAttempt ? "reconnecting" : "connecting", reconnectAttempt ? "重连中" : "连接中");
  eventSource = new EventSource(`/api/events/stream?lastEventId=${encodeURIComponent(lastCursor)}`);
  const receive = (event) => {
    try {
      const payload = JSON.parse(event.data);
      const cursorValue = event.lastEventId !== undefined && event.lastEventId !== ""
        ? event.lastEventId : payload.cursor;
      const cursor = Number(cursorValue);
      if (cursorValue === undefined || cursorValue === null || !Number.isSafeInteger(cursor) || cursor < 0
          || Number(payload.version) !== Number(payload.dashboard?.version)
          || (!payload.reset && cursor < lastCursor)) throw new Error("Invalid event frame");
      // A reset can move backwards, but must not replace committed state or
      // the durable cursor until its full snapshot has rendered successfully.
      applyDashboard(payload.dashboard, {
        force: Boolean(payload.reset),
        changes: payload.changes || [],
      });
      rememberCursor(cursor); // Only after this frame was successfully consumed.
      lastLiveAt = Date.now();
      reconnectAttempt = 0;
      setConnection("live", payload.has_more ? "补收记录中" : "实时");
    } catch (_error) {
      scheduleReconnect();
    }
  };
  eventSource.addEventListener("snapshot", receive);
  eventSource.addEventListener("dashboard", receive);
  eventSource.addEventListener("heartbeat", () => {
    lastLiveAt = Date.now();
    reconnectAttempt = 0;
    setConnection("live", "实时");
  });
  eventSource.onopen = () => {
    lastLiveAt = Date.now();
    reconnectAttempt = 0;
    setConnection("live", "实时");
  };
  eventSource.onerror = () => scheduleReconnect();
}

// Conversation entry: read-only, explicit bindings only; never infer from prose or role sessions.
function conversationId(value) {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
    ? value.toLowerCase() : "";
}

function conversationUrl(id) {
  // Verified against the installed Codex app's own Copy Conversation Link implementation.
  const valid = conversationId(id);
  return valid ? `codex://threads/${valid}` : "";
}

function taskConversations(task) {
  const runs = [...(Array.isArray(task.runs) ? task.runs : [])].sort((a, b) => Number(b.attempt || 0) - Number(a.attempt || 0));
  const threadEvidence = (Array.isArray(task.evidence) ? task.evidence : []).filter((item) => item.kind === "thread");
  const verified = threadEvidence.filter((item) => item.verified === 1 || item.verified === true);
  const external = ["external", "delegated"].includes(task.execution_mode);
  const candidates = new Map();
  const add = (id, source, current = false) => {
    id = conversationId(id);
    if (!id) return;
    const previous = candidates.get(id);
    if (!previous || (current && !previous.current)) candidates.set(id, {id, source, current});
  };
  const latest = runs[0];
  const latestStatus = new Map();
  for (const run of runs) {
    const id = conversationId(run.session_id);
    if (id && !latestStatus.has(id)) latestStatus.set(id, run.status);
    const current = !external && Number.isFinite(Number(run.attempt)) && Number(run.attempt) === Number(latest?.attempt) && run.status !== "CANCELED";
    add(run.session_id, `第 ${Number(run.attempt) || "?"} 次执行 · ${run.status || "状态未知"}`, current);
  }
  for (const item of verified) add(item.value, "已核验的 thread 绑定",
    (external || !runs.length) && latestStatus.get(conversationId(item.value)) !== "CANCELED");
  let choices = [...candidates.values()];
  let current = choices.filter((item) => item.current);
  let reason = "";
  // A newer unverified registration must not silently fall back to an old owner.
  const newestVerified = Math.max(0, ...verified.map((item) => Date.parse(item.created_at) || 0));
  const pending = threadEvidence.some((item) => !(item.verified === 1 || item.verified === true)
    && (!Date.parse(item.created_at) || Date.parse(item.created_at) >= newestVerified));
  if (task.imported_from || task.state === "CANCELED") {
    reason = task.imported_from ? "导入记录仅供历史参考，未确认本机执行会话。" : "任务已取消；以下会话仅供查看历史，不代表当前执行者。";
    choices = choices.map((item) => ({...item, current: false}));
    current = [];
  } else if ((external || !runs.length) && (pending || verified.some((item) => !conversationId(item.value)))) {
    reason = "会话登记尚未核验或 ID 无效；请确认绑定，不能默认打开旧会话。";
    choices = choices.map((item) => ({...item, current: false}));
    current = [];
  } else if (current.length > 1) {
    reason = "存在多个已登记会话，未明确唯一主会话或替代关系；请选择后打开。";
  } else if (!current.length) {
    reason = choices.length ? "当前执行尚无有效会话绑定；下方仅有历史记录，请核对后选择。"
      : "尚未绑定执行会话。任务可能还未执行，或外部执行者尚未登记已核验的 thread ID。";
  }
  choices.sort((a, b) => Number(b.current) - Number(a.current) || a.id.localeCompare(b.id));
  return {choices, selected: current.length === 1 ? current[0].id : "", reason};
}

function renderTaskConversation(task, notice = "") {
  const host = document.querySelector("#detailConversation");
  host.replaceChildren();
  host.dataset.taskId = task.id;
  const binding = taskConversations(task);
  const heading = el("div", "conversation-heading");
  heading.append(el("strong", "", "执行会话"), el("small", "", "仅查看 · 不新建、不重跑"));
  host.append(heading);
  if (binding.reason) host.append(el("p", "conversation-reason", binding.reason));
  let selected = binding.selected;
  const identity = el("p", "conversation-identity");
  const idText = el("code");
  const source = el("small");
  identity.append(idText, source);
  const actions = el("div", "conversation-actions");
  const open = el("button", "conversation-open", "打开执行会话");
  open.type = "button";
  const copy = el("button", "secondary", "复制会话 ID");
  copy.type = "button";
  const feedback = el("p", "conversation-feedback", notice);
  feedback.setAttribute("role", "status");
  feedback.setAttribute("aria-live", "polite");
  const update = () => {
    const choice = binding.choices.find((item) => item.id === selected);
    const href = choice && conversationUrl(choice.id);
    open.disabled = !href;
    open.setAttribute("aria-disabled", href ? "false" : "true");
    open.tabIndex = href ? 0 : -1;
    copy.disabled = !href;
    idText.textContent = choice?.id || "未选择会话";
    source.textContent = choice ? `${choice.current ? "绑定来源" : "历史记录"}：${choice.source}` : "不根据任务标题、正文或主责代码猜测会话。";
  };
  if (binding.choices.length > 1 || (binding.choices.length && !selected)) {
    const label = el("label", "conversation-select", "选择执行会话");
    const select = el("select");
    const empty = el("option", "", "请核对来源并选择会话");
    empty.value = "";
    select.append(empty);
    for (const choice of binding.choices) {
      const option = el("option", "", `${choice.current ? "已登记" : "历史"} · ${choice.source} · ${choice.id}`);
      option.value = choice.id;
      select.append(option);
    }
    select.value = selected;
    select.addEventListener("change", () => { selected = select.value; feedback.textContent = ""; update(); });
    label.append(select);
    host.append(label);
  }
  let opening = false;
  open.addEventListener("click", async (event) => {
    event.preventDefault();
    if (!selected || opening || !conversationUrl(selected)) return;
    const requestedId = selected;
    opening = true;
    feedback.textContent = "正在核对会话绑定…";
    try {
      const fresh = await api(`/api/tasks/${encodeURIComponent(task.id)}`);
      if (host.dataset.taskId !== task.id || !open.isConnected || !document.querySelector("#detailDialog").open) return;
      if (fresh.id !== task.id || selected !== requestedId) {
        feedback.textContent = "任务或选择已变化；未打开会话，请重新核对。";
        return;
      }
      if (JSON.stringify(taskConversations(fresh)) !== JSON.stringify(binding)) {
        renderTaskConversation(fresh, "会话绑定已变化，请重新核对后打开。");
        return;
      }
      feedback.textContent = "已请求浏览器打开 Codex，无法确认是否打开成功；若无响应，请复制会话 ID 手动查找。";
      window.location.href = conversationUrl(requestedId);
    } catch (_error) {
      feedback.textContent = "无法核对或打开会话；未启动任何执行。请稍后重试，或复制会话 ID 手动查找。";
    } finally { opening = false; }
  });
  copy.addEventListener("click", async () => {
    if (!selected) return;
    try {
      await navigator.clipboard.writeText(selected);
      feedback.textContent = "会话 ID 已复制。可在 Codex 中手动查找；不会启动执行。";
    } catch (_error) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(idText);
      selection?.removeAllRanges();
      selection?.addRange(range);
      feedback.textContent = "浏览器未允许复制，已选中会话 ID；请手动复制。";
    }
  });
  update();
  actions.append(open, copy);
  host.append(identity, actions, el("p", "conversation-help", "需安装 Codex 并允许浏览器打开应用。归档、已删除或不可访问的会话可能无法打开；此入口不会取消归档或恢复执行。"), feedback);
}

// Admission receipts live inside existing task details/cards, not a demo view.
const ADMISSION_LABELS = {saved: "需求已保存（未确认派单）", pending: "派单待确认", queued: "已接纳排队（不等于正在执行）", coalesced: "合并复用已有运行", deferred: "执行暂缓", rejected: "执行拒绝", uncertain: "派单结果不确定"};
const admissionDrafts = new Map();

function admissionReceiptMatches(result, taskId, request) {
  const receipt = result?.receipt;
  return result?.schema_version === 1 && result.task_id === taskId && receipt?.task_id === taskId
    && receipt.idempotency_key === request.idempotency_key && receipt.source_revision === request.expected_revision
    && typeof receipt.id === "string" && Number.isSafeInteger(receipt.sequence)
    && Object.hasOwn(ADMISSION_LABELS, receipt.state);
}

function admissionPanel(snapshot) {
  const panel = el("section", "admission-panel");
  panel.append(el("h4", "", "调度接纳与当前等待"));
  if (!snapshot || snapshot.schema_version !== 1 || !snapshot.current) {
    panel.append(el("p", "clarity-warning", "接单回执未接入 / 读取失败；不能据此认定已派发。"));
    return panel;
  }
  const current = snapshot.current;
  if (snapshot.receipt) panel.append(el("p", "", `原始接纳回执：${ADMISSION_LABELS[snapshot.receipt.state] || "未核对"} · ${snapshot.receipt.idempotency_key || "key未登记"}`));
  panel.append(el("p", "", `当前接纳 / 限制：${ADMISSION_LABELS[current.state] || "状态未核对"}`));
  const fields = [
    ["原因码", current.reason_code], ["处理责任", `${current.responsibility?.kind || "未登记"} · ${current.responsibility?.declared_owner || "具体责任人未登记"}`],
    ["下一动作", current.next_action], ["恢复条件", current.recovery_condition],
    ["事实版本", snapshot.revision ?? "未登记"], ["真实运行", current.run_id || "未确认，不创建占位执行"],
    ["运行当前状态（与接单分开）", current.run_status || "未登记"],
    ["执行槽", current.executor ? `${current.executor.adapter} / attempt ${current.executor.attempt}；机器与人员身份未登记` : "未绑定唯一执行槽"],
    ["会话", current.session_id || "未登记，不猜会话链接"],
  ];
  const list = el("dl", "clarity-fields");
  for (const [label, value] of fields) {
    const field = el("div");
    field.append(el("dt", "", label), el("dd", "", String(value ?? "未登记")));
    list.append(field);
  }
  panel.append(list);
  if (snapshot.basis_stale) panel.append(el("p", "clarity-warning", "原回执的理由已失效，历史保留；请按当前事实重新核对，不会自动恢复。"));
  panel.append(el("p", "release-assurance", "发消息成功不是接单；心跳不是实质进展。仅手动派发入口有此回执；宿主ack与旧intake/retry/自动派发尚未接入。"));
  const history = el("details", "admission-history");
  history.append(el("summary", "", "查看原始回执与不可变历史"), el("pre", "", JSON.stringify({receipt: snapshot.receipt, original_receipt: snapshot.original_receipt, history: snapshot.history}, null, 2)));
  panel.append(history);
  return panel;
}

function admissionDispatchForm(task, isCurrent) {
  const form = el("section", "admission-dispatch");
  form.append(el("h4", "", "明确手动派发"), el("p", "release-note", "只在你点击确认后提交。此入口不恢复暂停/取消，不代替审批，不保证宿主已接单。"));
  const instruction = el("textarea", "");
  instruction.setAttribute("aria-label", "本次明确执行指令");
  instruction.maxLength = 32000;
  const existing = admissionDrafts.get(task.id);
  instruction.value = existing?.request.instruction || "";
  instruction.readOnly = Boolean(existing);
  const status = el("p", "release-feedback");
  status.setAttribute("role", "status");
  const resultHost = el("div", "");
  const submit = el("button", "primary", existing ? "用原请求核对 / 重试" : "确认范围并手动派发");
  submit.type = "button";
  submit.disabled = !existing && !task.admission?.dispatch_allowed;
  const reset = el("button", "secondary", "重新审阅新请求（不会运行）");
  reset.type = "button";
  reset.disabled = !existing?.resolved;
  reset.addEventListener("click", () => {
    if (!isCurrent() || !admissionDrafts.get(task.id)?.resolved) return;
    admissionDrafts.delete(task.id);
    openDetail(task.id);
  });
  submit.addEventListener("click", async () => {
    if (!isCurrent()) return;
    let draft = admissionDrafts.get(task.id);
    if (!draft) {
      if (!instruction.value.trim() || !task.admission?.dispatch_allowed) {
        status.textContent = "请填写指令并刷新核对当前允许派发的版本。";
        return;
      }
      draft = {request: {instruction: instruction.value, resume: false, expected_revision: task.admission.revision, idempotency_key: uniqueKey()}, generation: 0, resolved: false};
      admissionDrafts.set(task.id, draft);
    }
    const generation = ++draft.generation;
    instruction.readOnly = true;
    submit.disabled = true;
    status.textContent = `请求 ${draft.request.idempotency_key} 已保留；等待真实回执，不代表接单。`;
    try {
      const result = await api(`/api/tasks/${encodeURIComponent(task.id)}/dispatch`, {method: "POST", body: JSON.stringify(draft.request)});
      if (!isCurrent() || generation !== draft.generation || admissionDrafts.get(task.id) !== draft) return;
      if (!admissionReceiptMatches(result, task.id, draft.request)) throw new Error("回执身份/原始版本不匹配");
      draft.resolved = !["pending", "uncertain"].includes(result.receipt.state);
      resultHost.replaceChildren(admissionPanel(result));
      status.textContent = `原回执：${ADMISSION_LABELS[result.receipt.state]}；当前状态见上方回执区域。`;
      reset.disabled = !draft.resolved;
    } catch (error) {
      if (!isCurrent() || generation !== draft.generation) return;
      status.textContent = `结果不确定：${error.message}。保留原key/版本/指令；不要换key或从其他入口重启。`;
    } finally {
      if (isCurrent() && generation === draft.generation) {
        submit.disabled = false;
        submit.textContent = "用原请求核对 / 重试";
      }
    }
  });
  form.append(instruction, submit, reset, status, resultHost);
  return form;
}

let detailRequest = 0;
async function openDetail(taskId, notice = "") {
  const request = ++detailRequest;
  const dialog = document.querySelector("#detailDialog");
  const body = document.querySelector("#detailBody");
  document.querySelector("#detailTitle").textContent = "正在读取任务…";
  document.querySelector("#detailConversation").replaceChildren();
  body.replaceChildren(el("p", "", "正在读取当前要求与会话绑定…"));
  if (!dialog.open) dialog.showModal();
  let task;
  try { task = await api(`/api/tasks/${encodeURIComponent(taskId)}`); }
  catch (_error) {
    if (request === detailRequest && dialog.open) body.replaceChildren(el("p", "error", "无法读取当前任务，未操作任何执行。请关闭后重试。"));
    return;
  }
  if (request !== detailRequest || !dialog.open) return;
  if (task.id !== taskId) { body.replaceChildren(el("p", "error", "返回的任务 ID 不一致，已停止展示。")); return; }
  const liveTask = (dashboard?.tasks || []).find((item) => item.id === taskId);
  document.querySelector("#detailTitle").textContent = task.title;
  renderTaskConversation(task);
  body.replaceChildren(renderActionRequirement(task));
  if (notice) {
    const feedback = el("p", "action-feedback", notice);
    feedback.setAttribute("role", "status");
    body.append(feedback);
  }
  body.append(admissionPanel(task.admission), admissionDispatchForm(task, () => request === detailRequest && document.querySelector("#detailDialog").open));
  const action = humanAction(task);
  const recovery = liveTask?.runtime_status?.recovery;
  if (recovery?.required) {
    const recoveryEntry = el("details", "detail-recovery");
    recoveryEntry.append(el("summary", "", "恢复入口（不会自动运行）"));
    recoveryEntry.append(
      el("p", "", recovery.instruction || "先恢复原执行，再由执行器提交真实心跳。"),
      el("small", "", `心跳模式：${recovery.mode || "external"} · 看板不会伪造心跳`),
    );
    body.append(recoveryEntry);
  }
  if (task.scope_summary) {
    const scope = el("section", "detail-scope");
    scope.append(el("small", "", "任务范围"), el("p", "", task.scope_summary));
    body.append(scope);
  }
  const technical = el("details", "detail-technical");
  const technicalSummary = el("summary", "", "查看技术详情与证据");
  technical.append(technicalSummary);
  const grid = el("div", "detail-grid");
  const fields = [
    ["当前阶段", `${stateLabels[taskDisplayState(liveTask || task)] || task.state}（非工作量百分比）`],
    ["运行活动", taskActivity(liveTask || task) || "暂无执行活动"],
    ["人工动作主责", action.owner || "无"],
    ["动作期限", action.due || "未设置"],
    ["主责", `${task.owner_session || "-"} · ${task.worker_type}`],
    ["模型", `${task.model} / ${task.reasoning} / ${task.speed}`],
    ["环境", task.environment],
    ["分支", task.branch || task.base_branch || "-"],
    ["Worktree", task.worktree || "-"],
    ["阻塞", task.blocking_reason || "无"],
    ["证据", `${task.evidence.length} 项`],
    [
      "运行健康",
      liveTask?.runtime_status?.label || liveTask?.recovery_status || "正常",
    ],
  ];
  for (const [label, value] of fields) {
    const cell = el("div", "detail-cell");
    cell.append(el("small", "", label), el("span", "", value));
    grid.append(cell);
  }
  technical.append(grid);
  body.append(technical);
  const timeline = el("div", "timeline");
  for (const event of task.events) {
    const row = el("div");
    row.append(el("strong", "", event.summary), el("small", "", `${event.producer} · ${event.occurred_at}`));
    timeline.append(row);
  }
  body.append(timeline);
  const actions = el("div", "dialog-actions");
  if (!engineReadOnly && ["RUNNING", "QUEUED"].includes(task.state)) {
    const cancel = el("button", "secondary", "取消任务");
    cancel.addEventListener("click", async () => {
      await api(`/api/tasks/${task.id}/cancel`, {method: "POST", body: "{}"});
      document.querySelector("#detailDialog").close();
      refresh();
    });
    actions.append(cancel);
  }
  if (!engineReadOnly && ["FAILED", "WAITING"].includes(task.state) && (task.runs || []).some((run) => run.session_id)) {
    const retry = el("button", "secondary", "请求重试执行（会派发）");
    retry.addEventListener("click", async () => {
      if (engineReadOnly || !window.confirm("这不是查看会话：确认重新派发本任务的重试执行？请先核对授权范围。")) return;
      await api(`/api/tasks/${task.id}/retry`, {method: "POST", body: "{}"});
      document.querySelector("#detailDialog").close();
      refresh();
    });
    actions.append(retry);
  }
  body.append(actions);
  if (!dialog.open) dialog.showModal();
}

document.querySelector("#newTaskButton").addEventListener("click", () => {
  const dialog = document.querySelector("#intakeDialog");
  document.querySelector("#composerStatus").textContent = "";
  document.querySelector("#composerStatus").className = "";
  dialog.showModal();
  document.querySelector("#intakeText").focus();
});
document.querySelector("#managerEntryButton").addEventListener("click", () => {
  document.querySelector("#managerEntryDetails").open = true;
  document.querySelector("#systemDetailsDialog").showModal();
  refresh({force: false, quiet: true});
});
document.querySelector("#detailDialog").addEventListener("close", () => { detailRequest++; });
document.querySelector("#systemDetailsButton").addEventListener("click", () => {
  document.querySelector("#systemDetailsDialog").showModal();
  refresh({force: false, quiet: true});
});
document.querySelector(".close-system-details").addEventListener(
  "click",
  () => document.querySelector("#systemDetailsDialog").close()
);
document.querySelector("#myActionFilter").addEventListener("click", () => {
  onlyMyActions = !onlyMyActions;
  if (dashboard) {
    renderActionOverview(dashboard);
    renderBoard(dashboard, dashboard, new Set(), false);
    renderInterventions(dashboard);
  }
});
document.querySelector("#mobileActionBar").addEventListener("click", () => {
  const panel = document.querySelector("#interventionPanel");
  panel.classList.toggle("is-open");
  if (panel.classList.contains("is-open")) panel.removeAttribute("aria-hidden");
  else if (!panel.classList.contains("has-user-actions")) panel.setAttribute("aria-hidden", "true");
  document.querySelector("#mobileActionBar").setAttribute(
    "aria-expanded",
    String(panel.classList.contains("is-open"))
  );
});
document.querySelector("#closeIntervention").addEventListener("click", () => {
  const panel = document.querySelector("#interventionPanel");
  panel.classList.remove("is-open");
  if (!panel.classList.contains("has-user-actions")) panel.setAttribute("aria-hidden", "true");
  document.querySelector("#mobileActionBar").setAttribute("aria-expanded", "false");
});
document.querySelector(".close-intake").addEventListener("click", () => document.querySelector("#intakeDialog").close());
document.querySelectorAll(".close-detail").forEach((node) => node.addEventListener("click", () => document.querySelector("#detailDialog").close()));
document.querySelectorAll(".close-report").forEach((node) => node.addEventListener("click", () => document.querySelector("#reportDialog").close()));
document.querySelector("#refreshButton").addEventListener("click", refresh);
document.querySelector("#search").addEventListener("input", () => {
  if (dashboard) renderBoard(dashboard, dashboard, new Set(), false);
});
document.querySelector("#reportButton").addEventListener("click", async () => {
  const button = document.querySelector("#reportButton");
  const originalText = button.textContent;
  reportReturnFocus = button;
  button.disabled = true;
  button.textContent = "加载中…";
  try {
    const report = await api("/api/report");
    renderReport(report);
    const dialog = document.querySelector("#reportDialog");
    dialog.showModal();
    dialog.querySelector(".close-report").focus();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});
document.querySelector("#copyReportButton").addEventListener("click", copyReportMarkdown);
document.querySelector("#toggleRawReportButton").addEventListener("click", (event) => {
  const raw = document.querySelector("#rawReport");
  raw.hidden = !raw.hidden;
  event.currentTarget.textContent = raw.hidden ? "查看原始 Markdown" : "隐藏原始 Markdown";
  event.currentTarget.setAttribute("aria-expanded", String(!raw.hidden));
  if (!raw.hidden) raw.scrollIntoView({block: "nearest"});
});
document.querySelector("#reportDialog").addEventListener("close", () => {
  if (reportReturnFocus && typeof reportReturnFocus.focus === "function") reportReturnFocus.focus();
  reportReturnFocus = null;
});

document.querySelector("#attachmentInput").addEventListener("change", (event) => {
  addFiles(event.currentTarget.files);
  event.currentTarget.value = "";
});

const dropZone = document.querySelector("#dropZone");
for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
}
dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));

document.querySelector("#intakeText").addEventListener("paste", (event) => {
  const files = Array.from(event.clipboardData.files || []);
  if (files.length) addFiles(files);
});
document.querySelector("#intakeText").addEventListener("input", (event) => {
  const secretPattern = /\b(?:password|passwd|pwd|token|secret|api[_-]?key|authorization)\b\s*[:=]|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b/i;
  document.querySelector("#secretWarning").hidden = !secretPattern.test(event.currentTarget.value);
});

document.querySelector("#intakeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (engineReadOnly) return;
  if (intakeSending) return;
  const formElement = event.currentTarget;
  const text = document.querySelector("#intakeText").value.trim();
  if (!text && !pendingFiles.length) {
    const status = document.querySelector("#composerStatus");
    status.textContent = "请输入描述或添加附件";
    status.className = "error";
    return;
  }
  intakeSending = true;
  const submit = document.querySelector("#submitIntake");
  submit.disabled = true;
  submit.textContent = "处理中…";
  const status = document.querySelector("#composerStatus");
  status.textContent = "";
  status.className = "";
  if (!intakeIdempotencyKey) intakeIdempotencyKey = uniqueKey();
  const raw = new FormData(formElement);
  const intent = raw.get("intent") || "implement";
  const advanced = {};
  for (const [name, value] of raw.entries()) {
    if (!name.startsWith("advanced_") || !value) continue;
    advanced[name.slice("advanced_".length)] = value;
  }
  if (formElement.elements.advanced_requires_deploy.checked) {
    advanced.requires_deploy = true;
  }
  const payload = new FormData();
  payload.append("text", text);
  payload.append("intent", intent);
  payload.append("idempotency_key", intakeIdempotencyKey);
  payload.append("advanced", JSON.stringify(advanced));
  for (const item of pendingFiles) payload.append("attachments", item.file, item.file.name);
  try {
    const result = await api("/api/intakes", {method: "POST", body: payload});
    if (["FAILED", "NEEDS_INPUT"].includes(result.status)) {
      throw new Error(result.error || "提交失败，请稍后重试");
    }
    pendingFiles.forEach((item) => URL.revokeObjectURL(item.url));
    pendingFiles = [];
    renderPendingFiles();
    formElement.reset();
    document.querySelector("#secretWarning").hidden = true;
    intakeIdempotencyKey = "";
    status.textContent = "";
    status.className = "";
    document.querySelector("#intakeDialog").close();
    await refresh();
  } catch (error) {
    status.textContent = error.message;
    status.className = "error";
  } finally {
    intakeSending = false;
    submit.disabled = false;
    submit.textContent = "交给擎天";
  }
});

refresh({force: true, quiet: true}).finally(connectEventStream);
window.setInterval(() => {
  const streamStale = connectionMode === "live" && Date.now() - lastLiveAt > 45000;
  if (connectionMode !== "live" || streamStale) {
    refresh({force: true, quiet: true});
  }
}, 30000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  const streamStale = connectionMode !== "live" || Date.now() - lastLiveAt > 20000;
  if (streamStale) {
    refresh({force: true, quiet: true});
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    reconnectAttempt = 0;
    connectEventStream();
  }
});
window.addEventListener("pagehide", closeEventStream);

// BEGIN RELEASE BATCH UI: independent fetches, never driven by task SSE or DONE.
const RELEASE_ASSURANCE = "登记的证据与人工核查，不是本系统独立重验";
const RELEASE_ENVIRONMENTS = {dev: "Dev", test: "Test", prod: "Prod（生产）"};
const RELEASE_BATCH_LABELS = {
  released: "已部署（已登记核查）", partial: "部分部署（已登记核查）",
  rolled_back: "已回滚（登记事实）", unverified: "部署尚未核查完整",
};
const RELEASE_LAYER_LABELS = {
  deployment: {confirmed: "已部署（已登记核查）", reported: "已报告部署事实，登记核查不完整", failed: "部署失败（登记事实）", rolled_back: "已回滚（登记事实）", unknown: "部署未知"},
  enablement: {enabled: "已启用（已登记核查）", disabled: "未启用（已登记核查）", reported: "已报告启用事实，登记核查不完整", unknown: "启用未知"},
  acceptance: {passed: "验收通过（已登记核查）", failed: "验收失败（登记事实）", reported: "已报告验收事实，登记核查不完整", not_tested: "尚未验收"},
};
let releaseSnapshot = null;
let releaseLoadedEnvironment = "";
let releaseLoadedAt = "";
let releaseReadSequence = 0;
let releaseReadController = null;
let releaseRecordBusy = "";
let releaseRecordGeneration = 0;
let releaseApprovedReceipt = null;
let releaseTaskReturnFocus = null;
const releaseExpanded = new Map();
const releaseDetailCache = new Map();

function releaseText(value, fallback = "未登记") {
  return value === undefined || value === null || value === "" ? fallback
    : typeof value === "object" ? JSON.stringify(value) : String(value);
}

function releaseErrorLines(value, prefix = "") {
  if (value === undefined || value === null || value === "") return [];
  if (Array.isArray(value)) return value.flatMap((entry) => releaseErrorLines(entry, prefix));
  if (typeof value === "object") {
    if (value.message) return [`${value.path || value.field || prefix || "请求"}: ${releaseText(value.message)}`];
    return Object.entries(value).flatMap(([key, entry]) => releaseErrorLines(entry, prefix ? `${prefix}.${key}` : key));
  }
  return [`${prefix ? `${prefix}: ` : ""}${String(value)}`];
}

async function releaseRequest(path, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) abort();
  options.signal?.addEventListener("abort", abort, {once: true});
  const timeout = window.setTimeout(abort, 15000);
  try {
    const response = await fetch(path, {
      ...options, signal: controller.signal, cache: "no-store",
      headers: {...(options.body ? {"Content-Type": "application/json"} : {}), ...(options.headers || {})},
    });
    let result = null;
    try { result = await response.json(); } catch (_error) { /* An old backend may return HTML. */ }
    if ([405, 501].includes(response.status) || (response.status === 404 && result?.code !== "missing")) {
      const error = new Error(`当前后端不支持发布批次接口 / 待集成（HTTP ${response.status}）。这不表示没有发布记录。`);
      error.unsupported = true;
      throw error;
    }
    const errors = releaseErrorLines(result?.errors);
    if (!response.ok || result?.ok === false || result?.valid === false || result?.success === false || result?.error || errors.length) {
      const lines = [...releaseErrorLines(result?.error), ...errors];
      if (!lines.length && result?.message) lines.push(releaseText(result.message));
      throw new Error(`HTTP ${response.status}: ${lines.join("\n") || "请求未成功或响应未通过验证"}`);
    }
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new Error("发布接口响应格式不受支持 / 待集成；未取得可确认结果。");
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("发布接口请求超时或已取消；未取得可确认结果。");
    throw error;
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}

function releaseFeedback(id, text, state = "") {
  const node = document.querySelector(id);
  node.textContent = text;
  node.dataset.state = state;
}

function releaseTime(value) {
  if (!value) return "未登记";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? `${releaseText(value)}（时间格式未识别）`
    : `${date.toLocaleString("zh-CN", {hour12: false})}（${value}）`;
}

function releaseDeploymentCaption(batch) {
  // Only the backend's reviewed deployment timestamp is a release time.
  // A rollback receipt or registration timestamp must never replace it.
  if (batch.status === "rolled_back") {
    return batch.released_at
      ? `曾于 ${releaseTime(batch.released_at)} 部署，现已回滚（登记事实）`
      : "现已回滚（登记事实）；原部署时间未登记 / 未证实";
  }
  return `部署事实时间：${releaseTime(batch.released_at)}${batch.counts?.rolled_back > 0 ? "；部分功能项现已回滚（登记事实）" : ""}`;
}

function releaseFields(entries) {
  const grid = el("dl", "release-fields");
  for (const [label, value] of entries) {
    const field = el("div");
    field.append(el("dt", "", label), el("dd", "", releaseText(value)));
    grid.append(field);
  }
  return grid;
}

function releaseBadge(state, label) {
  const badge = el("span", "release-badge", label);
  badge.dataset.state = state || "unknown";
  return badge;
}

function releaseCounts(counts) {
  const host = el("div", "release-counts");
  for (const [key, label] of [["total", "功能项"], ["deployed", "部署核查完整"], ["enabled", "启用核查完整"], ["accepted", "验收核查完整"], ["rolled_back", "已回滚"], ["unverified", "部署未核查完整"]]) {
    const value = counts?.[key];
    const node = el("span", "", `${label} ${Number.isInteger(value) && value >= 0 ? value : "未返回"}`);
    node.dataset.releaseCount = key;
    host.append(node);
  }
  return host;
}

function releaseRememberUI() {
  document.querySelectorAll("#releaseBatches details[data-release-section]").forEach((node) => {
    releaseExpanded.set(node.dataset.releaseSection, node.open);
  });
  return document.activeElement?.dataset.releaseFocus;
}

function releaseRestoreFocus(key) {
  if (!key) return;
  Array.from(document.querySelectorAll("#releaseBatches [data-release-focus]"))
    .find((node) => node.dataset.releaseFocus === key)?.focus({preventScroll: true});
}

function releaseDisclosure(key, label, className) {
  const node = el("details", className);
  node.dataset.releaseSection = key;
  node.open = releaseExpanded.get(key) === true;
  const summary = el("summary", "", label);
  summary.dataset.releaseFocus = key;
  node.append(summary);
  node.addEventListener("toggle", () => {
    if (node.isConnected) releaseExpanded.set(key, node.open);
  });
  return node;
}

function releaseFact(item, layer, key) {
  const heading = {deployment: "部署", enablement: "启用", acceptance: "验收"}[layer];
  const fact = item.facts?.[layer] || {};
  const assessment = item.assessment?.[layer];
  const state = assessment?.state || (layer === "acceptance" ? "not_tested" : "unknown");
  const host = el("section", "release-layer");
  host.dataset.releaseLayer = layer;
  host.dataset.state = state;
  host.append(el("h4", "", heading), releaseBadge(state, RELEASE_LAYER_LABELS[layer][state] || `未识别核查状态：${state}`));
  const reasons = releaseErrorLines(assessment?.reasons);
  if (!assessment) reasons.push("未返回该层核查结果；不从任务完成或其他层推断。");
  if (reasons.length) {
    const list = el("ul", "release-reasons");
    reasons.forEach((reason) => list.append(el("li", "", reason)));
    host.append(list);
  }
  host.append(releaseFields([
    ["回执原始状态", fact.status], ["事实观察时间", releaseTime(fact.observed_at)],
    ["证据 source.kind", fact.source?.kind], ["证据 source.ref", fact.source?.ref],
    ["证据 source.sha256", fact.source?.sha256],
    ["核查人（回执登记来源，非认证身份）", fact.review?.reviewer],
    ["核查时间（回执登记来源）", releaseTime(fact.review?.checked_at)],
    ["核查方法（回执登记来源）", fact.review?.method], ["核查备注（回执登记来源）", fact.review?.notes],
  ]));
  const proof = releaseDisclosure(`${key}:${layer}:proof`, "查看 proof 原文（登记来源）", "release-proof");
  proof.append(el("pre", "release-json", fact.proof ? JSON.stringify(fact.proof, null, 2) : "未登记 proof；不能视作已核查。"));
  host.append(proof);
  return host;
}

function releaseItem(item, batchKey) {
  const key = `${batchKey}:item:${item.item_key}`;
  const host = el("article", "release-item");
  host.dataset.releaseItemKey = releaseText(item.item_key, "");
  host.append(el("h3", "", releaseText(item.title)), releaseFields([
    ["item_key", item.item_key], ["feature_key", item.feature_key], ["组件 component", item.component],
    ["源码 source_revision", item.artifact?.source_revision], ["制品 digest", item.artifact?.digest], ["运维 ops_revision", item.artifact?.ops_revision],
  ]));
  const tasks = el("div", "release-actions release-task-links");
  const feedback = el("p", "release-feedback");
  feedback.setAttribute("role", "status");
  for (const taskId of (Array.isArray(item.task_ids) ? item.task_ids : [])) {
    const button = el("button", "release-task-link", `关联任务 ${taskId}`);
    button.type = "button";
    button.dataset.releaseTaskId = taskId;
    button.dataset.releaseFocus = `${key}:task:${taskId}`;
    button.addEventListener("click", async () => {
      feedback.textContent = "正在打开关联任务详情…";
      button.disabled = true;
      releaseTaskReturnFocus = button;
      try {
        await openDetail(taskId);
        feedback.textContent = "会话入口位于任务详情，沿用现有结构化绑定校验。";
      } catch (error) {
        feedback.textContent = `任务详情未打开：${error.message}`;
        feedback.dataset.state = "error";
      } finally { button.disabled = false; }
    });
    tasks.append(button);
  }
  if (!tasks.children.length) tasks.append(el("span", "release-note", "未关联任务"));
  host.append(tasks, el("p", "release-note", "关联任务可跨功能项、批次与环境；任务状态和会话入口保持原有规则。"), feedback);
  const layers = el("div", "release-layers");
  for (const layer of ["deployment", "enablement", "acceptance"]) layers.append(releaseFact(item, layer, key));
  host.append(layers);
  return host;
}

function renderReleaseBody(host, batch) {
  host.replaceChildren();
  host.append(releaseFields([
    ["批次 ID", batch.id], ["批次 revision", batch.revision], ["批次主责（登记来源）", batch.owner],
    ["部署历史与当前状态（登记来源）", releaseDeploymentCaption(batch)],
    ["历史已核查部署时间（非回滚或登记时间）", releaseTime(batch.released_at)], ["批次登记时间", releaseTime(batch.recorded_at)],
  ]));
  if (Array.isArray(batch.items)) {
    if (!batch.items.length) host.append(el("p", "release-note", "本批次没有功能项；不代表任何部署完成。"));
    for (const item of batch.items) host.append(releaseItem(item, String(batch.id)));
  } else host.append(el("p", "release-note", "功能项明细未返回，展开后独立读取。"));
  const history = releaseDisclosure(`${batch.id}:history`, `回执历史（追加保留）${Array.isArray(batch.history) ? ` · ${batch.history.length}` : " · 待读取"}`, "release-history");
  if (Array.isArray(batch.history)) {
    if (!batch.history.length) history.append(el("p", "release-note", "本批次未返回历史回执条目。"));
    batch.history.forEach((entry, index) => {
      const row = releaseDisclosure(`${batch.id}:history:${entry.id || index}`, `回执 ${index + 1} · revision ${releaseText(entry.revision)} · 登记于 ${releaseTime(entry.received_at)}`, "release-history-entry");
      row.append(releaseFields([
        ["回执 ID", entry.id], ["回执 revision", entry.revision], ["回执登记时间", releaseTime(entry.received_at)],
      ]));
      for (const delta of (Array.isArray(entry.items) ? entry.items : [])) {
        for (const layer of ["deployment", "enablement", "acceptance"]) {
          const fact = delta.facts?.[layer];
          if (!fact) continue;
          const label = layer === "deployment" && fact.status === "rolled_back" ? "回滚"
            : {deployment: "部署", enablement: "启用", acceptance: "验收"}[layer];
          row.append(releaseFields([
            ["功能项 item_key", delta.item_key], ["回执维度", label], ["回执原始状态", fact.status],
            [`${label}事实观察时间`, releaseTime(fact.observed_at)],
            ["核查时间（回执登记来源）", releaseTime(fact.review?.checked_at)],
            ["核查人（回执登记来源，非认证身份）", fact.review?.reviewer],
          ]));
        }
      }
      row.append(el("pre", "release-json", JSON.stringify(entry, null, 2)));
      history.append(row);
    });
  } else history.append(el("p", "release-note", "历史明细尚未取得，不能推断没有回滚。"));
  host.append(history);
  const appendContract = releaseDisclosure(`${batch.id}:append`, "追加回执接口合同（不在此执行）", "release-append-contract");
  appendContract.append(
    el("p", "release-note", "追加仅支持已有 item_key 的 facts。必须人工确认当前 revision；过期 revision 或幂等键载荷冲突返回 409。/preview 仅用于新建批次，不能预览追加回执。此处不发送请求。"),
    el("pre", "release-json", `POST /api/release-batches/${encodeURIComponent(batch.id)}/receipts\n${JSON.stringify({idempotency_key: "<new-idempotency-key>", expected_revision: batch.revision, items: [{item_key: batch.items?.[0]?.item_key || "<existing-item-key>", facts: {}}]}, null, 2)}`),
  );
  host.append(appendContract);
}

async function loadReleaseDetail(batch, body) {
  if (Array.isArray(batch.items) && Array.isArray(batch.history)) return;
  const cached = releaseDetailCache.get(batch.id);
  if (cached?.revision === batch.revision && ["loading", "ready"].includes(cached.state)) return;
  releaseDetailCache.set(batch.id, {revision: batch.revision, state: "loading"});
  const status = el("p", "release-feedback", "正在独立读取批次明细…");
  body.append(status);
  try {
    const response = await releaseRequest(`/api/release-batches/${encodeURIComponent(batch.id)}`);
    const detail = response.batch || response;
    if (detail.id !== batch.id || detail.revision !== batch.revision || !Array.isArray(detail.items) || !Array.isArray(detail.history)) {
      throw new Error("批次明细版本或结构不一致，请刷新批次列表后重试。");
    }
    releaseDetailCache.set(batch.id, {revision: batch.revision, state: "ready", data: detail});
    if (body.isConnected) {
      const focus = releaseRememberUI();
      renderReleaseBody(body, detail);
      releaseRestoreFocus(focus);
    }
  } catch (error) {
    releaseDetailCache.set(batch.id, {revision: batch.revision, state: "error"});
    status.dataset.state = "error";
    status.textContent = `批次明细未取得 / stale：${error.message} 已有内容保留；可收起后重新展开重试。`;
  }
}

function renderReleaseBatches(batches) {
  const focus = releaseRememberUI();
  const host = document.querySelector("#releaseBatches");
  const fragment = document.createDocumentFragment();
  const time = (batch) => Date.parse(batch.released_at || batch.recorded_at || "") || 0;
  const sorted = [...batches].sort((a, b) => time(b) - time(a) || String(b.id).localeCompare(String(a.id)));
  for (const batch of sorted) {
    const row = releaseDisclosure(`batch:${batch.id}`, "", "release-batch");
    row.dataset.releaseId = batch.id;
    row.dataset.releaseStatus = batch.status;
    const summary = row.firstElementChild;
    summary.append(el("strong", "release-summary-title", `${releaseText(batch.name)} · ${releaseText(batch.version)}`), el("span", "release-summary-meta", `${RELEASE_ENVIRONMENTS[batch.environment] || releaseText(batch.environment)} · 主责（登记来源）：${releaseText(batch.owner)}`), releaseBadge(batch.status, RELEASE_BATCH_LABELS[batch.status] || `未知批次状态：${releaseText(batch.status)}`));
    summary.append(el("span", "release-summary-meta", releaseDeploymentCaption(batch)), releaseCounts(batch.counts));
    const body = el("div", "release-batch-body");
    const cached = releaseDetailCache.get(batch.id);
    renderReleaseBody(body, cached?.revision === batch.revision && cached.state === "ready" ? cached.data : batch);
    row.append(body);
    row.addEventListener("toggle", () => { if (row.open && row.isConnected) loadReleaseDetail(batch, body); });
    fragment.append(row);
  }
  host.replaceChildren(fragment);
  releaseRestoreFocus(focus);
}

async function refreshReleaseBatches() {
  const sequence = ++releaseReadSequence;
  releaseReadController?.abort();
  releaseReadController = new AbortController();
  const environment = document.querySelector("#releaseEnvironment").value;
  const panel = document.querySelector("#releasePanel");
  panel.dataset.state = "loading";
  panel.dataset.stale = String(Boolean(releaseSnapshot));
  panel.setAttribute("aria-busy", "true");
  document.querySelector("#releaseEmpty").hidden = true;
  releaseFeedback("#releaseStatus", releaseSnapshot ? "正在刷新；当前仍显示上次成功读取的数据，尚未确认最新状态。" : "正在读取发布批次…", "loading");
  try {
    const result = await releaseRequest(`/api/release-batches${environment ? `?environment=${encodeURIComponent(environment)}` : ""}`, {signal: releaseReadController.signal});
    if (sequence !== releaseReadSequence) return false;
    if (result.schema_version !== 1 || !Array.isArray(result.batches) || result.batches.some((batch) => !batch || !batch.id)) {
      throw new Error("发布列表合同不受支持 / 待集成；不能显示为没有记录。");
    }
    renderReleaseBatches(result.batches);
    releaseSnapshot = result;
    releaseLoadedEnvironment = environment;
    releaseLoadedAt = new Date().toISOString();
    panel.dataset.state = "ready";
    panel.dataset.stale = "false";
    document.querySelector("#releaseEmpty").hidden = result.batches.length !== 0;
    document.querySelector("#releaseLoadedScope").textContent = `当前数据：${RELEASE_ENVIRONMENTS[releaseLoadedEnvironment] || "全部环境"} · ${result.batches.length} 个批次 · 最近在前 · 读取于 ${releaseTime(releaseLoadedAt)}`;
    releaseFeedback("#releaseStatus", `${result.batches.length ? "已读取登记事实" : "读取成功，当前筛选没有记录"}；${RELEASE_ASSURANCE}。`, "ready");
    return true;
  } catch (error) {
    if (sequence !== releaseReadSequence) return false;
    const stale = Boolean(releaseSnapshot);
    panel.dataset.state = stale ? "stale" : error.unsupported ? "unsupported" : "error";
    panel.dataset.stale = String(stale);
    releaseFeedback("#releaseStatus", `${stale ? `数据已过期 / stale，保留上次 ${RELEASE_ENVIRONMENTS[releaseLoadedEnvironment] || "全部环境"} 数据；本次筛选或刷新未成功。\n` : "未取得发布记录，不能判断为空。\n"}${error.message}`, panel.dataset.state);
    return false;
  } finally {
    if (sequence === releaseReadSequence) panel.setAttribute("aria-busy", "false");
  }
}

function setReleaseView(releases) {
  for (const [id, active] of [["releaseViewButton", releases], ["taskViewButton", !releases]]) {
    const tab = document.querySelector(`#${id}`);
    tab.setAttribute("aria-selected", String(active));
    tab.setAttribute("aria-pressed", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  document.querySelector("#taskPanel").hidden = releases;
  document.querySelector("#releasePanel").hidden = !releases;
  if (releases && !releaseSnapshot) refreshReleaseBatches();
}

function invalidateReleasePreview() {
  releaseRecordGeneration += 1;
  releaseApprovedReceipt = null;
  document.querySelector("#releaseConfirmButton").disabled = true;
  document.querySelector("#releasePreviewSummary").hidden = true;
}

function releaseRecordControls() {
  document.querySelector("#releasePreviewButton").disabled = Boolean(releaseRecordBusy);
  document.querySelector("#releaseTemplateButton").disabled = Boolean(releaseRecordBusy);
  document.querySelector("#releaseReceiptInput").readOnly = releaseRecordBusy === "save";
  document.querySelector("#releaseConfirmButton").disabled = Boolean(releaseRecordBusy) || !releaseApprovedReceipt;
}

async function previewReleaseReceipt() {
  if (releaseRecordBusy) return;
  invalidateReleasePreview();
  const generation = releaseRecordGeneration;
  const raw = document.querySelector("#releaseReceiptInput").value;
  let receipt;
  try {
    receipt = JSON.parse(raw);
    if (!receipt || Array.isArray(receipt) || typeof receipt !== "object" || typeof receipt.idempotency_key !== "string" || !receipt.idempotency_key.trim()) throw new Error("必须是 JSON 对象，并包含非空 idempotency_key。");
  } catch (error) {
    releaseFeedback("#releaseRecordFeedback", `JSON 回执未通过检查：${error.message}`, "error");
    return;
  }
  releaseRecordBusy = "preview";
  releaseRecordControls();
  releaseFeedback("#releaseRecordFeedback", "正在预览；尚未登记，也不会执行发布。", "loading");
  try {
    const response = await releaseRequest("/api/release-batches/preview", {method: "POST", body: JSON.stringify(receipt)});
    if (generation !== releaseRecordGeneration || raw !== document.querySelector("#releaseReceiptInput").value) return;
    const batch = response.batch || response;
    if (!batch.name || !batch.version || !RELEASE_ENVIRONMENTS[batch.environment] || !batch.counts || !Array.isArray(batch.items)) {
      throw new Error("预览未返回合同要求的批次摘要，不能确认登记 / 待集成。");
    }
    const summary = document.querySelector("#releasePreviewSummary");
    summary.replaceChildren(el("h3", "", "请人工审阅后再登记"), el("p", "release-assurance", `这是预览，尚未写入。${RELEASE_ASSURANCE}。所有核查信息均来自回执登记来源，非当前认证身份。`), releaseFields([["批次", batch.name], ["版本", batch.version], ["环境", RELEASE_ENVIRONMENTS[batch.environment]], ["主责（登记来源）", batch.owner], ["幂等键", receipt.idempotency_key]]), releaseBadge(batch.status, RELEASE_BATCH_LABELS[batch.status] || releaseText(batch.status)), releaseCounts(batch.counts));
    for (const item of batch.items) {
      const itemSummary = el("div", "release-item");
      itemSummary.append(el("h3", "", releaseText(item.title)), releaseFields([["item_key", item.item_key], ["component", item.component], ["关联任务", item.task_ids]]));
      for (const layer of ["deployment", "enablement", "acceptance"]) {
        const assessment = item.assessment?.[layer];
        itemSummary.append(el("p", "release-note", `${{deployment: "部署", enablement: "启用", acceptance: "验收"}[layer]}：${RELEASE_LAYER_LABELS[layer][assessment?.state] || "核查结果未返回"}${assessment?.reasons?.length ? `；${releaseErrorLines(assessment.reasons).join("；")}` : ""}`));
      }
      summary.append(itemSummary);
    }
    const projection = el("details", "release-proof");
    projection.append(el("summary", "", "完整预览响应（含证据与核查来源）"), el("pre", "release-json", JSON.stringify(response, null, 2)));
    summary.append(projection);
    summary.hidden = false;
    releaseApprovedReceipt = {raw, body: JSON.stringify(receipt)};
    releaseFeedback("#releaseRecordFeedback", "预览完成，尚未登记。请核对以上三层状态、缺失原因和回执来源，再点击“确认摘要并登记”。", "ready");
  } catch (error) {
    if (generation === releaseRecordGeneration) releaseFeedback("#releaseRecordFeedback", `预览失败，不能登记：${error.message}`, error.unsupported ? "unsupported" : "error");
  } finally {
    releaseRecordBusy = "";
    releaseRecordControls();
  }
}

async function confirmReleaseReceipt() {
  if (releaseRecordBusy || !releaseApprovedReceipt) return;
  if (releaseApprovedReceipt.raw !== document.querySelector("#releaseReceiptInput").value) {
    invalidateReleasePreview();
    releaseFeedback("#releaseRecordFeedback", "JSON 已修改，请重新预览。", "error");
    return;
  }
  const approved = releaseApprovedReceipt;
  releaseRecordBusy = "save";
  releaseRecordControls();
  releaseFeedback("#releaseRecordFeedback", "正在登记人工回执，不执行发布；请保留此幂等键。", "loading");
  try {
    const response = await releaseRequest("/api/release-batches", {method: "POST", body: approved.body});
    const batch = response.batch || response;
    if (!batch.id || !Number.isInteger(batch.revision)) throw new Error("接口未返回可确认的批次 ID / revision。");
    invalidateReleasePreview();
    const message = `${response.reused ? "相同回执已存在，未重复登记" : "已登记人工回执"}：${batch.id} · revision ${batch.revision}。登记不执行发布，也不表示各层核查通过。`;
    releaseFeedback("#releaseRecordFeedback", message, "success");
    const refreshed = await refreshReleaseBatches();
    if (!refreshed) releaseFeedback("#releaseRecordFeedback", `${message}\n列表刷新失败，列表状态以 stale / 错误提示为准。`, "stale");
  } catch (error) {
    invalidateReleasePreview();
    releaseFeedback("#releaseRecordFeedback", `未确认登记成功：${error.message}\n原 JSON 与幂等键已保留；若请求已送达，使用相同载荷重新预览并重试核对，避免重复记录。`, error.unsupported ? "unsupported" : "error");
  } finally {
    releaseRecordBusy = "";
    releaseRecordControls();
  }
}

function releaseSyntheticTemplate() {
  return {
    idempotency_key: `synthetic-${uniqueKey()}`, name: "合成示例（不是真实发布）", version: "synthetic-v1", environment: "dev", owner: "合成登记来源",
    items: [{item_key: "synthetic-item", feature_key: "synthetic-feature", title: "合成功能项（未知状态）", component: "synthetic-web", artifact: {source_revision: "synthetic-source", digest: `sha256:${"a".repeat(64)}`, ops_revision: "synthetic-ops"}, task_ids: [], facts: {deployment: {status: "unknown"}, enablement: {status: "unknown"}, acceptance: {status: "not_tested"}}}],
  };
}

function initReleaseUI() {
  document.querySelector("#releaseViewButton").addEventListener("click", () => setReleaseView(true));
  document.querySelector("#taskViewButton").addEventListener("click", () => setReleaseView(false));
  for (const id of ["taskViewButton", "releaseViewButton"]) {
    document.querySelector(`#${id}`).addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const releases = event.key === "End" || (event.key !== "Home" && id === "taskViewButton");
      setReleaseView(releases);
      document.querySelector(releases ? "#releaseViewButton" : "#taskViewButton").focus();
    });
  }
  document.querySelector("#releaseEnvironment").addEventListener("change", refreshReleaseBatches);
  document.querySelector("#releaseRefresh").addEventListener("click", refreshReleaseBatches);
  document.querySelector("#releaseRecordButton").addEventListener("click", () => {
    document.querySelector("#releaseRecordDialog").showModal();
    document.querySelector("#releaseReceiptInput").focus();
  });
  document.querySelector("#releaseRecordClose").addEventListener("click", () => document.querySelector("#releaseRecordDialog").close());
  document.querySelector("#releaseRecordDialog").addEventListener("close", () => {
    invalidateReleasePreview();
    document.querySelector("#releaseRecordButton").focus();
  });
  document.querySelector("#releaseReceiptInput").addEventListener("input", () => {
    invalidateReleasePreview();
    releaseFeedback("#releaseRecordFeedback", "输入已修改，请先预览新摘要。", "");
  });
  document.querySelector("#releaseTemplateButton").addEventListener("click", () => {
    if (document.querySelector("#releaseReceiptInput").value.trim()) {
      releaseFeedback("#releaseRecordFeedback", "已保留现有输入；如需合成模板，请先明确清空输入。", "");
      return;
    }
    document.querySelector("#releaseReceiptInput").value = JSON.stringify(releaseSyntheticTemplate(), null, 2);
    invalidateReleasePreview();
    releaseFeedback("#releaseRecordFeedback", "已填入合成未知状态示例，不是真实发布事实；尚未预览或登记。", "");
  });
  document.querySelector("#releasePreviewButton").addEventListener("click", previewReleaseReceipt);
  document.querySelector("#releaseConfirmButton").addEventListener("click", confirmReleaseReceipt);
  document.querySelector("#detailDialog").addEventListener("close", () => {
    if (releaseTaskReturnFocus?.isConnected) releaseTaskReturnFocus.focus({preventScroll: true});
    releaseTaskReturnFocus = null;
  });
}
initReleaseUI();
// END RELEASE BATCH UI

// BEGIN OPERATIONS CLARITY UI: separate projection and explicit annotation.
const CLARITY_ASSURANCE = "经审阅登记的来源声明，不是独立验证或执行许可";
const CLARITY_NOTICE = "说明更正，不等于解除真实依赖/授权/任务可调度/清理业务原字段";
const CLARITY_CATEGORIES = [
  ["executing", "正在执行"], ["internal", "内部处理 / 评审"],
  ["pending_release", "待发布"], ["user_action", "待你处理"],
  ["external_blocked", "外部阻塞"], ["deferred", "暂缓 / 已暂停"],
  ["unclassified", "未分类 / 待核对"], ["history", "历史记录"],
];
const CLARITY_FIELDS = [["completed", "已完成什么"], ["blocker", "当前阻塞"],
  ["next_action", "下一步"], ["owner", "唯一主责"], ["meaningful_progress", "最近实质进展时间"]];
const CLARITY_INPUTS = ["clarityUseReason", "clarityReason", "clarityUseNext", "clarityNextAction",
  "clarityCorrectionReason", "clarityActor", "claritySourceRef", "claritySourceHash",
  "clarityObservedAt", "clarityReviewedAt", "clarityReviewer"];
let claritySnapshot = null;
let clarityReadSequence = 0;
let clarityReadController = null;
let clarityReadAt = "";
let clarityDraft = null;
let clarityGeneration = 0;
let clarityBusy = "";
let clarityApproved = null;
let clarityView = "clarity";
let clarityTaskReturnFocus = null;
const clarityExpanded = new Map();
const clarityDrafts = new Map();
const clarityNode = (id) => document.querySelector(`#${id}`);
const clarityObject = (value) => Boolean(value && typeof value === "object" && !Array.isArray(value));
const clarityText = (value) => value === null || value === undefined || value === "" ? "未登记"
  : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
const clarityTimezone = (value) => typeof value === "string" && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));

function clarityWritableRevision(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function clarityRevisionText(value) {
  return clarityWritableRevision(value) ? String(value) : "未登记 / 待核对";
}

function clarityDraftCanWrite() {
  return Boolean(clarityDraft && !clarityDraft.readOnly && clarityWritableRevision(clarityDraft.task.revision));
}

function clarityCompletionValid(basis) {
  return clarityObject(basis) && typeof basis.profile === "string" && typeof basis.eligible === "boolean"
    && ["required", "present", "missing", "unresolved_actions", "unresolved_dependencies"].every((key) => Array.isArray(basis[key]))
    && (basis.last_completion_event === null || clarityObject(basis.last_completion_event))
    && basis.assurance === "task_evidence_gate_not_authorization_deployment_or_functional_acceptance";
}

function clarityTaskValid(task) {
  return clarityObject(task) && typeof task.id === "string" && Boolean(task.id.trim())
    && typeof task.title === "string" && typeof task.state === "string"
    && (task.revision === null || clarityWritableRevision(task.revision))
    && CLARITY_CATEGORIES.some(([key]) => key === task.category) && typeof task.urgent === "boolean"
    && CLARITY_FIELDS.every(([key]) => clarityObject(task[key])
      && (task[key].value === null || typeof task[key].value === "string")
      && (task[key].source === null || clarityObject(task[key].source)))
    && clarityObject(task.lineage) && typeof task.lineage.status === "string"
    && (task.lineage.successor_task_id === null || typeof task.lineage.successor_task_id === "string")
    && Array.isArray(task.warnings) && task.warnings.every((warning) => clarityObject(warning)
      && typeof warning.code === "string" && typeof warning.message === "string" && Array.isArray(warning.sources))
    && clarityObject(task.legacy) && Array.isArray(task.corrections) && clarityCompletionValid(task.completion_basis);
}

function claritySnapshotValid(result) {
  if (!clarityObject(result) || result.schema_version !== 1
    || result.assurance !== "recorded_sources_not_independent_verification"
    || typeof result.source_status !== "string" || !Array.isArray(result.tasks)
    || !result.tasks.every(clarityTaskValid) || !Array.isArray(result.groups)
    || !clarityObject(result.counts) || result.published?.source !== "/api/release-batches"
    || result.published.count !== null) return false;
  const ids = new Set(result.tasks.map((task) => task.id));
  return ids.size === result.tasks.length && result.groups.every((group) => clarityObject(group)
    && typeof group.id === "string" && group.id && typeof group.blocker_key === "string" && group.blocker_key.trim()
    && clarityObject(group.scope) && ["project_id", "environment", "target", "authorization_scope"]
      .every((key) => typeof group.scope[key] === "string" && group.scope[key].trim())
    && Array.isArray(group.task_ids) && group.task_ids.length > 0
    && new Set(group.task_ids).size === group.task_ids.length
    && group.task_ids.every((id) => result.tasks.some((task) => task.id === id && task.category === "external_blocked"))
    && group.channel?.kind === "task" && ids.has(group.channel.task_id));
}

async function clarityRequest(path, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) abort();
  options.signal?.addEventListener("abort", abort, {once: true});
  const timer = window.setTimeout(abort, 15000);
  try {
    const response = await fetch(path, {...options, signal: controller.signal, cache: "no-store",
      headers: {...(options.body ? {"Content-Type": "application/json"} : {}), ...(options.headers || {})}});
    let payload = null;
    try { payload = await response.json(); } catch (_error) { /* Missing routes may return HTML. */ }
    if (!response.ok || payload?.error) {
      const unsupported = [405, 501].includes(response.status) || (response.status === 404 && payload?.code !== "missing");
      const error = new Error(unsupported ? `工作总览接口未接入 / 待集成（HTTP ${response.status}），不能视作空列表。`
        : `HTTP ${response.status} · ${clarityText(payload?.code)}：${clarityText(payload?.error)}`);
      error.status = response.status;
      error.unsupported = unsupported;
      throw error;
    }
    if (!clarityObject(payload)) throw new Error("工作总览响应不符合共享契约，未取得可确认结果。");
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时或已取消；结果未确认。");
    throw error;
  } finally {
    window.clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}

function clarityFeedback(id, message, state = "") {
  clarityNode(id).textContent = message;
  clarityNode(id).dataset.state = state;
}

function clarityDisclosure(key, title, data) {
  const node = el("details", "clarity-disclosure");
  node.dataset.claritySection = key;
  node.open = clarityExpanded.get(key) === true;
  const summary = el("summary", "", title);
  summary.dataset.clarityFocus = key;
  node.append(summary, el("pre", "release-json", clarityText(data)));
  node.addEventListener("toggle", () => {
    if (node.isConnected) clarityExpanded.set(key, node.open);
  });
  return node;
}

function clarityTaskButton(taskId, title, key, feedback) {
  const button = el("button", "secondary", title);
  button.type = "button";
  button.dataset.clarityTaskId = taskId;
  button.dataset.clarityFocus = key;
  button.addEventListener("click", async () => {
    if (!claritySnapshot?.tasks.some((task) => task.id === taskId) || button.disabled) return;
    button.disabled = true;
    clarityTaskReturnFocus = button;
    try {
      await openDetail(taskId);
      feedback.textContent = "已打开现有任务详情；此入口仅导航。";
    } catch (error) { feedback.textContent = `任务详情未打开：${error.message}`; }
    finally { button.disabled = false; }
  });
  return button;
}

function clarityCard(task) {
  const card = el("article", "clarity-card");
  card.dataset.clarityTask = task.id;
  card.dataset.category = task.category;
  card.append(el("h3", "", task.title), el("p", "release-note", `${task.id} · 原状态 ${task.state} · revision ${clarityRevisionText(task.revision)}`));
  if (!clarityWritableRevision(task.revision)) {
    card.append(el("p", "clarity-warning", "版本未登记 / 待核对：仅可只读查看任务、来源与关联记录；说明更正和普通完成报告不可用。"));
  }
  card.append(el("p", "release-note", `${clarityText(task.category_label)}${task.category === "deferred" ? " · 不计入紧急事项" : ""}`));
  card.append(admissionPanel(task.admission));
  const fields = el("dl", "clarity-fields");
  for (const [key, label] of CLARITY_FIELDS) {
    const field = el("div");
    field.dataset.clarityField = key;
    const value = key === "meaningful_progress" && !clarityTimezone(task[key].value) ? null : task[key].value;
    field.append(el("dt", "", label), el("dd", "", clarityText(value)), clarityDisclosure(`${task.id}:field:${key}`, "查看字段来源", task[key].source));
    fields.append(field);
  }
  card.append(fields, clarityDisclosure(`${task.id}:classification`, "分类依据与来源", task.classification_source));
  const feedback = el("p", "release-feedback");
  feedback.setAttribute("role", "status");
  const links = el("div", "release-actions");
  links.append(clarityTaskButton(task.id, "查看任务详情", `${task.id}:detail`, feedback));
  const lineage = task.lineage;
  const successor = lineage.status === "valid" && lineage.successor_task_id && lineage.successor_task_id !== task.id
    && claritySnapshot?.tasks.some((candidate) => candidate.id === lineage.successor_task_id);
  if (task.state === "CANCELED" && successor) {
    card.append(el("p", "clarity-warning", "CANCELED · 已由新任务接续（保留原取消历史）"));
    links.append(clarityTaskButton(lineage.successor_task_id, `查看接续任务 ${lineage.successor_task_id}`, `${task.id}:successor`, feedback));
  } else if (task.state === "CANCELED") {
    card.append(el("p", "release-note", `接续未确认：${clarityText(lineage.reason)}`));
  }
  card.append(clarityDisclosure(`${task.id}:lineage`, "接续核对与原始来源（仅任务关联）", lineage));
  for (const [index, warning] of task.warnings.entries()) {
    card.append(el("p", "clarity-warning", `${warning.code}：${warning.message}`),
      clarityDisclosure(`${task.id}:warning:${index}`, "查看警告的新旧来源", warning.sources));
  }
  card.append(clarityDisclosure(`${task.id}:legacy`, "业务原始说明与原有时间（保留）", task.legacy),
    clarityDisclosure(`${task.id}:corrections`, "说明更正审计：旧值 / 新值 / 证据 / 原始依据", task.corrections));
  const completion = clarityDisclosure(`${task.id}:completion`, "任务完成依据（当前证据门禁）", task.completion_basis);
  completion.append(el("p", "release-assurance", "任务证据门禁不等于授权/部署/功能验收；任务 DONE 不代表功能启用或新角色功能验收。手动模式也可能自动闭合有效门禁，不承诺新的 QA。"));
  const basis = task.completion_basis;
  for (const [key, label] of [["profile", "门禁类型"], ["required", "所需证据"], ["present", "当前已有证据"], ["missing", "仍缺少证据"],
    ["unresolved_actions", "当前未解决事项"], ["unresolved_dependencies", "当前未解决依赖"], ["last_completion_event", "实际最近完成事件"], ["eligible", "当前门禁是否满足"]]) {
    completion.append(el("p", "release-note", `${label}：${clarityText(basis[key])}`));
  }
  card.append(completion);
  const correct = el("button", "secondary", "说明更正（预览后人工确认）");
  correct.type = "button";
  correct.disabled = !clarityWritableRevision(task.revision);
  correct.dataset.clarityFocus = `${task.id}:correct`;
  correct.addEventListener("click", () => openClarityCorrection(task.id, correct));
  links.append(correct);
  card.append(links, feedback);
  return card;
}

function renderClarityGroups() {
  const host = clarityNode("clarityGroups");
  host.replaceChildren();
  for (const group of claritySnapshot.groups) {
    const card = el("article", "clarity-group");
    card.dataset.clarityGroup = group.id;
    card.append(el("h4", "", `阻塞 ${group.blocker_key}`));
    const scope = el("dl", "release-fields");
    for (const [key, label] of [["project_id", "项目"], ["environment", "环境"], ["target", "目标"], ["authorization_scope", "精确授权范围"]]) {
      const item = el("div");
      item.append(el("dt", "", label), el("dd", "", group.scope[key]));
      scope.append(item);
    }
    const feedback = el("p", "release-feedback");
    feedback.setAttribute("role", "status");
    card.append(scope, el("p", "release-note", `待决事项：${clarityText(group.decision)}`),
      el("p", "release-note", "全部受影响任务（不受下方任务卡筛选隐藏）："));
    const tasks = el("ul");
    for (const id of group.task_ids) {
      const task = claritySnapshot.tasks.find((candidate) => candidate.id === id);
      const item = el("li");
      item.append(clarityTaskButton(id, `${task.title} · ${id}`, `group:${group.id}:${id}`, feedback));
      tasks.append(item);
    }
    card.append(tasks, clarityTaskButton(group.channel.task_id, `查看处理渠道任务 ${group.channel.task_id}`, `group:${group.id}:channel`, feedback), feedback);
    host.append(card);
  }
  if (!claritySnapshot.groups.length) host.append(el("p", "release-note", "没有可确认的精确范围分组。范围或处理渠道未登记的阻塞仍在各自任务中保留。"));
}

function renderClarityCategories() {
  const host = clarityNode("clarityCategories");
  host.replaceChildren();
  for (const [key, label] of CLARITY_CATEGORIES) {
    const button = el("button", "clarity-category");
    button.type = "button";
    button.dataset.clarityCategory = key;
    button.dataset.clarityFocus = `category:${key}`;
    button.setAttribute("aria-pressed", String(clarityNode("clarityFilter").value === key));
    button.append(el("span", "", label), el("strong", "", claritySnapshot ? String(claritySnapshot.tasks.filter((task) => task.category === key).length) : "未读取"));
    if (key === "deferred") button.append(el("small", "", "暂缓与暂停，不计紧急"));
    button.addEventListener("click", () => { clarityNode("clarityFilter").value = key; renderClarity(); });
    host.append(button);
    if (key === "pending_release") {
      const published = el("button", "clarity-category");
      published.type = "button";
      published.dataset.clarityCategory = "published";
      published.dataset.clarityFocus = "category:published";
      published.append(el("span", "", "已发布上线"), el("strong", "", "查看批次"), el("small", "", "部署 / 启用 / 验收分别核查；总览不推算发布数"));
      published.addEventListener("click", () => setClarityView("release"));
      host.append(published);
    }
  }
}

function renderClarity() {
  if (!claritySnapshot) return;
  const focus = document.activeElement?.dataset.clarityFocus;
  clarityNode("clarityPanel").querySelectorAll("details[data-clarity-section]").forEach((node) => clarityExpanded.set(node.dataset.claritySection, node.open));
  const query = clarityNode("claritySearch").value.trim().toLocaleLowerCase();
  const category = clarityNode("clarityFilter").value;
  const tasks = claritySnapshot.tasks.filter((task) => (category === "all" || task.category === category)
    && (!query || JSON.stringify(task).toLocaleLowerCase().includes(query)));
  renderClarityCategories();
  renderClarityGroups();
  const host = clarityNode("clarityTasks");
  host.replaceChildren(...tasks.map(clarityCard));
  if (!tasks.length && claritySnapshot.tasks.length) host.append(el("p", "release-note", "当前搜索或类别没有匹配任务；其他类别与历史记录仍保留。"));
  const urgent = claritySnapshot.tasks.filter((task) => task.urgent === true
    && !["deferred", "history", "unclassified"].includes(task.category)).length;
  clarityNode("clarityLoadedScope").textContent = `快照 ${claritySnapshot.tasks.length} 项 · 筛选显示 ${tasks.length} 项 · 经登记的紧急事项 ${urgent} 项 · 读取于 ${clarityReadAt || "未登记"}`;
  if (focus) Array.from(clarityNode("clarityPanel").querySelectorAll("[data-clarity-focus]"))
    .find((node) => node.dataset.clarityFocus === focus)?.focus({preventScroll: true});
}

async function refreshClarity() {
  const sequence = ++clarityReadSequence;
  clarityReadController?.abort();
  clarityReadController = new AbortController();
  const panel = clarityNode("clarityPanel");
  panel.dataset.state = "loading";
  panel.dataset.stale = String(Boolean(claritySnapshot));
  panel.setAttribute("aria-busy", "true");
  clarityNode("clarityEmpty").hidden = true;
  clarityFeedback("clarityStatus", claritySnapshot ? "正在刷新；保留上次快照，最新状态尚未确认。" : "正在读取工作总览…", "loading");
  try {
    const result = await clarityRequest("/api/operations-clarity", {signal: clarityReadController.signal});
    if (sequence !== clarityReadSequence) return false;
    if (!claritySnapshotValid(result)) throw new Error("工作总览响应与共享契约不一致，不能视作空列表或采用不完整数据。");
    claritySnapshot = result;
    clarityReadAt = new Date().toISOString();
    if (clarityDraft) {
      const current = result.tasks.find((task) => task.id === clarityDraft.task.id);
      clarityDraft.readOnly = !current || !clarityWritableRevision(current.revision);
      if (!current || current.revision !== clarityDraft.task.revision) {
        clarityDraft.revisionStale = true;
        invalidateClarityPreview();
        clarityRevisionLabel();
        clarityFeedback("clarityCorrectionFeedback", `当前版本${current ? `已变为 ${clarityRevisionText(current.revision)}` : "未取得"}；输入保留，请读取当前版本后重新预览。`, "stale");
      }
      clarityCorrectionControls();
      clarityRevisionLabel();
    }
    renderClarity();
    panel.dataset.state = "ready";
    panel.dataset.stale = "false";
    clarityNode("clarityEmpty").hidden = result.tasks.length !== 0;
    clarityNode("claritySourceStatus").textContent = result.source_status === "adapter_not_configured"
      ? "默认记录适配器未配置：没有业务确认的权威阻塞范围或进展字段；缺失信息保持未知。"
      : `记录来源状态：${result.source_status}；${CLARITY_ASSURANCE}。`;
    clarityFeedback("clarityStatus", `${result.tasks.length ? "已读取工作总览" : "读取成功，当前投影没有任务记录"}；${CLARITY_ASSURANCE}。`, "ready");
    return true;
  } catch (error) {
    if (sequence !== clarityReadSequence) return false;
    const stale = Boolean(claritySnapshot);
    panel.dataset.state = stale ? "stale" : error.unsupported ? "unsupported" : "error";
    panel.dataset.stale = String(stale);
    clarityFeedback("clarityStatus", `${stale ? "刷新失败 / stale：保留上次快照。" : "未取得投影，不能判断为空。"}${error.message}`, panel.dataset.state);
    return false;
  } finally { if (sequence === clarityReadSequence) panel.setAttribute("aria-busy", "false"); }
}

function setClarityView(view) {
  if (!["clarity", "release", "task"].includes(view)) return;
  clarityView = view;
  if (view !== "clarity") setReleaseView(view === "release");
  for (const [key, button, panel] of [["clarity", "clarityViewButton", "clarityPanel"], ["release", "releaseViewButton", "releasePanel"], ["task", "taskViewButton", "taskPanel"]]) {
    const active = key === view;
    clarityNode(button).setAttribute("aria-selected", String(active));
    clarityNode(button).setAttribute("aria-pressed", String(active));
    clarityNode(button).tabIndex = active ? 0 : -1;
    clarityNode(panel).hidden = !active;
  }
}

function clarityFormValues() {
  return Object.fromEntries(CLARITY_INPUTS.map((id) => [id, ["clarityUseReason", "clarityUseNext"].includes(id) ? clarityNode(id).checked : clarityNode(id).value]));
}

function clarityRememberDraft() {
  if (clarityDraft) clarityDraft.values = clarityFormValues();
}

function clarityCorrectionControls() {
  clarityNode("clarityCorrectionPreview").disabled = Boolean(clarityBusy) || !clarityDraftCanWrite()
    || (clarityDraft.revisionStale && !clarityDraft.request?.attempted);
  clarityNode("clarityCorrectionPreview").textContent = clarityDraft?.request?.attempted ? "核对原更正回执（只读）" : "预览说明更正（只读）";
  clarityNode("clarityCorrectionConfirm").disabled = Boolean(clarityBusy) || !clarityApproved || !clarityDraftCanWrite() || clarityDraft?.revisionStale;
  clarityNode("clarityCorrectionReload").disabled = Boolean(clarityBusy) || !clarityDraft;
  for (const id of CLARITY_INPUTS) {
    if (["clarityUseReason", "clarityUseNext"].includes(id)) clarityNode(id).disabled = clarityBusy === "save";
    else clarityNode(id).readOnly = clarityBusy === "save";
  }
}

function invalidateClarityPreview() {
  clarityGeneration += 1;
  clarityApproved = null;
  clarityNode("clarityCorrectionSummary").hidden = true;
  clarityCorrectionControls();
}

function clarityRevisionLabel() {
  if (!clarityDraft) return;
  clarityNode("clarityCorrectionRevision").textContent = `当前已读取 revision：${clarityDraft.readOnly ? "未登记 / 待核对" : clarityRevisionText(clarityDraft.task.revision)}${clarityDraft.revisionStale ? "（需重新读取）" : ""}；以 task.revision 为准。`;
  clarityNode("clarityIdempotency").textContent = `本次幂等键：${clarityDraft.key || "预览时创建"}`;
}

function openClarityCorrection(taskId, returnFocus) {
  if (clarityBusy) return;
  const task = claritySnapshot?.tasks.find((candidate) => candidate.id === taskId);
  if (!task) return;
  clarityRememberDraft();
  clarityDraft = clarityDrafts.get(taskId);
  if (!clarityDraft) {
    clarityDraft = {task, values: Object.fromEntries(CLARITY_INPUTS.map((id) => [id, ""])), key: "", request: null, revisionStale: false};
    Object.assign(clarityDraft.values, {clarityUseReason: true, clarityUseNext: true, clarityReason: task.blocker.value || "", clarityNextAction: task.next_action.value || ""});
    clarityDrafts.set(taskId, clarityDraft);
  } else if (clarityDraft.task.revision !== task.revision) clarityDraft.revisionStale = true;
  clarityDraft.readOnly = !clarityWritableRevision(task.revision);
  clarityDraft.returnFocus = returnFocus;
  for (const [id, value] of Object.entries(clarityDraft.values)) {
    if (["clarityUseReason", "clarityUseNext"].includes(id)) clarityNode(id).checked = value;
    else clarityNode(id).value = value;
  }
  invalidateClarityPreview();
  clarityNode("clarityCorrectionTask").textContent = `更正任务：${task.title} · ${task.id}`;
  clarityRevisionLabel();
  clarityFeedback("clarityCorrectionFeedback", clarityDraft.request?.attempted
    ? "上次更正结果未确认，原载荷和幂等键保留；可只读核对原更正回执。"
    : clarityDraft.revisionStale ? "版本已变化，草稿保留；先读取当前版本。" : "尚未预览或写入。", "");
  clarityNode("clarityCorrectionDialog").showModal();
  clarityNode("clarityReason").focus();
}

function clarityCorrectionInputChanged() {
  clarityRememberDraft();
  invalidateClarityPreview();
  if (clarityDraft) { clarityDraft.key = ""; clarityDraft.request = null; }
  clarityRevisionLabel();
  clarityFeedback("clarityCorrectionFeedback", "输入已修改，预览失效；需要重新预览并人工确认。", "");
}

async function reloadClarityCorrection() {
  if (!clarityDraft || clarityBusy) return;
  invalidateClarityPreview();
  const draft = clarityDraft;
  const generation = clarityGeneration;
  clarityBusy = "read";
  clarityCorrectionControls();
  try {
    const task = await clarityRequest(`/api/operations-clarity/tasks/${encodeURIComponent(draft.task.id)}`);
    if (draft !== clarityDraft || generation !== clarityGeneration) return;
    if (!clarityTaskValid(task) || task.id !== draft.task.id) throw new Error("任务详情不符合共享契约。");
    draft.task = task;
    draft.readOnly = !clarityWritableRevision(task.revision);
    draft.revisionStale = draft.readOnly;
    draft.key = "";
    draft.request = null;
    // In-flight list reads must not replace the freshly fetched task basis.
    ++clarityReadSequence;
    clarityReadController?.abort();
    if (claritySnapshot) {
      claritySnapshot = {...claritySnapshot, tasks: claritySnapshot.tasks.map((old) => old.id === task.id ? task : old)};
      renderClarity();
      clarityNode("clarityPanel").dataset.stale = "true";
      clarityNode("clarityPanel").setAttribute("aria-busy", "false");
      clarityFeedback("clarityStatus", "已单独读取当前任务；其他任务与分组仍是上次快照，请刷新总览。", "stale");
    }
    clarityRevisionLabel();
    clarityFeedback("clarityCorrectionFeedback", draft.readOnly
      ? "版本未登记 / 待核对，输入保留；当前仅可只读查看，恢复有效正整数版本前不能预览或登记更正。"
      : "已读取当前版本，输入未改变。请对照当前任务原始事实重新预览。", draft.readOnly ? "stale" : "ready");
  } catch (error) {
    if (draft === clarityDraft && generation === clarityGeneration) clarityFeedback("clarityCorrectionFeedback", `当前版本读取失败：${error.message}；输入已保留。`, "error");
  } finally { clarityBusy = ""; clarityCorrectionControls(); }
}

function clarityCorrectionPayload() {
  if (!clarityDraftCanWrite()) throw new Error("当前 revision 未登记 / 待核对，仅可只读查看。");
  const values = clarityFormValues();
  if (!values.clarityUseReason && !values.clarityUseNext) throw new Error("至少选择 reason 或 next_action 一项。");
  for (const id of ["clarityCorrectionReason", "clarityActor", "claritySourceRef", "claritySourceHash", "clarityObservedAt", "clarityReviewedAt", "clarityReviewer"]) {
    if (!values[id].trim()) throw new Error("请填写更正原因、声明人和全部证据来源信息。");
  }
  if (!/^[a-fA-F0-9]{64}$/.test(values.claritySourceHash)) throw new Error("证据 SHA256 需为 64 位十六进制值。");
  if (!clarityTimezone(values.clarityObservedAt) || !clarityTimezone(values.clarityReviewedAt)) throw new Error("观察与审阅时间必须是包含时区的 ISO 时间。");
  const changes = {};
  if (values.clarityUseReason) changes.reason = values.clarityReason;
  if (values.clarityUseNext) changes.next_action = values.clarityNextAction;
  const fingerprint = JSON.stringify(values);
  if (clarityDraft.request?.fingerprint === fingerprint && clarityDraft.request.payload.expected_revision === clarityDraft.task.revision) return clarityDraft.request;
  clarityDraft.key = uniqueKey();
  const payload = {idempotency_key: clarityDraft.key, expected_revision: clarityDraft.task.revision, changes,
    correction_reason: values.clarityCorrectionReason, actor: {id: values.clarityActor, origin: "declared_business_review"},
    source: {ref: values.claritySourceRef, sha256: values.claritySourceHash, observed_at: values.clarityObservedAt,
      reviewed_at: values.clarityReviewedAt, reviewer: values.clarityReviewer}};
  clarityDraft.request = {fingerprint, payload, body: JSON.stringify(payload)};
  clarityRevisionLabel();
  return clarityDraft.request;
}

function clarityCorrectionResultValid(result, taskId) {
  return clarityTaskValid(result.task) && result.task.id === taskId && clarityObject(result.correction)
    && clarityObject(result.correction.old_values) && clarityObject(result.correction.new_values)
    && clarityObject(result.correction.native_baseline) && typeof result.reused === "boolean";
}

function clarityCorrectionReceiptMatches(result, taskId, request) {
  const receipt = result.correction;
  const changes = request.payload.changes;
  return typeof receipt.id === "string" && Boolean(receipt.id.trim())
    && receipt.task_id === taskId && clarityWritableRevision(receipt.revision)
    && receipt.idempotency_key === request.payload.idempotency_key
    && receipt.native_baseline.revision === request.payload.expected_revision
    && Object.keys(receipt.new_values).length === Object.keys(changes).length
    && Object.keys(changes).every((key) => Object.prototype.hasOwnProperty.call(receipt.new_values, key)
      && receipt.new_values[key] === changes[key]);
}

async function previewClarityCorrection() {
  if (!clarityDraftCanWrite() || clarityBusy || (clarityDraft.revisionStale && !clarityDraft.request?.attempted)) return;
  invalidateClarityPreview();
  const draft = clarityDraft;
  const generation = clarityGeneration;
  let request;
  try { request = clarityCorrectionPayload(); }
  catch (error) { clarityFeedback("clarityCorrectionFeedback", error.message, "error"); return; }
  clarityBusy = "preview";
  clarityCorrectionControls();
  clarityFeedback("clarityCorrectionFeedback", "正在预览；尚未写入说明更正。", "loading");
  try {
    const result = await clarityRequest(`/api/operations-clarity/tasks/${encodeURIComponent(draft.task.id)}/corrections/preview`, {method: "POST", body: request.body});
    if (generation !== clarityGeneration || draft !== clarityDraft || request.fingerprint !== JSON.stringify(clarityFormValues())) return;
    if (!clarityCorrectionResultValid(result, draft.task.id) || result.preview !== true
      || (!result.reused && result.task.revision !== request.payload.expected_revision)) {
      throw new Error("预览的结构或当前 revision 不一致，请重新读取版本。");
    }
    if (result.reused && !clarityCorrectionReceiptMatches(result, draft.task.id, request)) {
      throw new Error("已登记回执与保留的原任务、幂等键或原始版本不匹配，结果未确认。");
    }
    const summary = clarityNode("clarityCorrectionSummary");
    summary.replaceChildren(el("h3", "", `${result.reused ? "已登记的重试回执" : "待人工确认"} · 当前 revision ${clarityRevisionText(result.task.revision)}`), el("p", "release-assurance", CLARITY_NOTICE),
      clarityDisclosure("preview:old", "原展示值 old_values", result.correction.old_values),
      clarityDisclosure("preview:new", "新展示值 new_values", result.correction.new_values),
      clarityDisclosure("preview:native", "业务原始依据 native_baseline（不会被清理）", result.correction.native_baseline),
      clarityDisclosure("preview:source", "声明人、更正原因与证据来源", request.payload),
      clarityDisclosure("preview:response", "完整预览与来源保证", result));
    summary.hidden = false;
    if (result.reused) {
      summary.append(el("p", "release-note", `当前任务阻塞：${clarityText(result.task.blocker.value)}`),
        el("p", "release-note", `当前任务下一步：${clarityText(result.task.next_action.value)}`),
        clarityDisclosure("preview:current-native", "当前原始事实与警告（不采用旧更正覆盖）", {revision: result.task.revision, legacy: result.task.legacy, warnings: result.task.warnings}));
      result.task.warnings.forEach((warning) => summary.append(el("p", "clarity-warning", `${warning.code}：${warning.message}`)));
      ++clarityReadSequence;
      clarityReadController?.abort();
      draft.task = result.task;
      draft.readOnly = !clarityWritableRevision(result.task.revision);
      draft.revisionStale = draft.readOnly;
      draft.key = "";
      draft.request = null;
      clarityApproved = null;
      clarityRevisionLabel();
      if (claritySnapshot) {
        claritySnapshot = {...claritySnapshot, tasks: claritySnapshot.tasks.map((task) => task.id === result.task.id ? result.task : task)};
        renderClarity();
        clarityNode("clarityPanel").dataset.stale = "true";
        clarityNode("clarityPanel").setAttribute("aria-busy", "false");
        clarityFeedback("clarityStatus", "已只读核对原更正回执与当前任务事实；其他任务和分组仍为上次快照，请刷新总览。", "stale");
      }
      clarityFeedback("clarityCorrectionFeedback", `已登记的重试回执，未追加写入。原始审计保留；当前任务 revision ${clarityRevisionText(result.task.revision)}，以后续事实与警告为准。${CLARITY_NOTICE}。`, "success");
      return;
    }
    clarityApproved = {draft, generation, request};
    clarityFeedback("clarityCorrectionFeedback", `预览完成，尚未写入；${CLARITY_ASSURANCE}。请人工核对旧值、新值和证据后确认。`, "ready");
  } catch (error) {
    if (generation === clarityGeneration && draft === clarityDraft) {
      if (error.status === 409) draft.revisionStale = true;
      clarityFeedback("clarityCorrectionFeedback", `预览失败：${error.message}${error.status === 409 ? "；版本或幂等键冲突，当前 revision 未确认，请读取当前版本。" : ""}`, "error");
      clarityRevisionLabel();
    }
  } finally { clarityBusy = ""; clarityCorrectionControls(); }
}

async function confirmClarityCorrection() {
  if (!clarityApproved || !clarityDraftCanWrite() || clarityBusy || clarityDraft.revisionStale) return;
  const approved = clarityApproved;
  if (approved.draft !== clarityDraft || approved.generation !== clarityGeneration
    || approved.request.fingerprint !== JSON.stringify(clarityFormValues())) {
    clarityCorrectionInputChanged();
    return;
  }
  clarityBusy = "save";
  approved.request.attempted = true;
  clarityCorrectionControls();
  clarityFeedback("clarityCorrectionFeedback", "正在登记说明更正；原载荷和幂等键保留。", "loading");
  try {
    const result = await clarityRequest(`/api/operations-clarity/tasks/${encodeURIComponent(approved.draft.task.id)}/corrections`, {method: "POST", body: approved.request.body});
    if (approved.generation !== clarityGeneration || approved.draft !== clarityDraft) return;
    if (!clarityCorrectionResultValid(result, approved.draft.task.id) || result.preview === true
      || !clarityCorrectionReceiptMatches(result, approved.draft.task.id, approved.request)) {
      throw new Error("持久更正回执的身份、原始版本或更正内容与已审阅请求不一致；结果未确认，保留原载荷和幂等键用于核对。");
    }
    ++clarityReadSequence;
    clarityReadController?.abort();
    clarityDraft.task = result.task;
    clarityDraft.readOnly = !clarityWritableRevision(result.task.revision);
    clarityDraft.revisionStale = clarityDraft.readOnly;
    clarityDraft.key = "";
    clarityDraft.request = null;
    invalidateClarityPreview();
    clarityRememberDraft();
    clarityRevisionLabel();
    if (claritySnapshot) {
      claritySnapshot = {...claritySnapshot, tasks: claritySnapshot.tasks.map((task) => task.id === result.task.id ? result.task : task)};
      renderClarity();
      clarityNode("clarityPanel").dataset.stale = "true";
      clarityNode("clarityPanel").setAttribute("aria-busy", "false");
      clarityFeedback("clarityStatus", "说明更正回执已读取；其他任务和分组仍为上次快照，请刷新总览。", "stale");
    }
    clarityFeedback("clarityCorrectionFeedback", `${result.reused ? "同一更正已存在，未重复登记" : "已登记独立展示说明"} · 当前 revision ${clarityRevisionText(result.task.revision)}。${CLARITY_NOTICE}。`, "success");
  } catch (error) {
    if (approved.generation !== clarityGeneration || approved.draft !== clarityDraft) return;
    if (error.status === 409) {
      clarityDraft.revisionStale = true;
      invalidateClarityPreview();
    }
    clarityRevisionLabel();
    clarityFeedback("clarityCorrectionFeedback", `登记结果未确认：${error.message}\n输入、原载荷与幂等键已保留。${error.status === 409
      ? "版本或幂等键冲突；读取当前版本，再重新预览。"
      : "可明确点击确认，以同一载荷和幂等键重试核对；不会自动重试。"}`, "error");
  } finally { clarityBusy = ""; clarityCorrectionControls(); }
}

function initClarityUI() {
  // Capture top-level tab events without modifying inherited release handlers.
  const views = [["clarity", "clarityViewButton"], ["release", "releaseViewButton"], ["task", "taskViewButton"]];
  views.forEach(([view, id], index) => {
    const button = clarityNode(id);
    button.addEventListener("click", (event) => {
      event.stopImmediatePropagation();
      setClarityView(view);
    }, true);
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const target = event.key === "Home" ? 0 : event.key === "End" ? 2 : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
      setClarityView(views[target][0]);
      clarityNode(views[target][1]).focus();
    }, true);
  });
  clarityNode("clarityRefresh").addEventListener("click", refreshClarity);
  clarityNode("claritySearch").addEventListener("input", renderClarity);
  clarityNode("clarityFilter").addEventListener("change", renderClarity);
  CLARITY_INPUTS.forEach((id) => {
    clarityNode(id).addEventListener("input", clarityCorrectionInputChanged);
    if (["clarityUseReason", "clarityUseNext"].includes(id)) clarityNode(id).addEventListener("change", clarityCorrectionInputChanged);
  });
  clarityNode("clarityCorrectionForm").addEventListener("submit", (event) => event.preventDefault());
  clarityNode("clarityCorrectionPreview").addEventListener("click", previewClarityCorrection);
  clarityNode("clarityCorrectionConfirm").addEventListener("click", confirmClarityCorrection);
  clarityNode("clarityCorrectionReload").addEventListener("click", reloadClarityCorrection);
  clarityNode("clarityCorrectionClose").addEventListener("click", () => clarityNode("clarityCorrectionDialog").close());
  clarityNode("clarityCorrectionDialog").addEventListener("close", () => {
    clarityRememberDraft();
    invalidateClarityPreview();
    if (clarityDraft?.returnFocus?.isConnected) clarityDraft.returnFocus.focus({preventScroll: true});
  });
  clarityNode("detailDialog").addEventListener("close", () => {
    if (clarityTaskReturnFocus?.isConnected) clarityTaskReturnFocus.focus({preventScroll: true});
    clarityTaskReturnFocus = null;
  });
  renderClarityCategories();
  setClarityView("clarity");
  return refreshClarity();
}
// The page script runs before DOMContentLoaded. Synthetic tests call the same initializer.
if (typeof document.addEventListener === "function") document.addEventListener("DOMContentLoaded", initClarityUI, {once: true});
// END OPERATIONS CLARITY UI
