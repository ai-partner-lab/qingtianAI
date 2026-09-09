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
let lastVersion = 0;
try {
  lastVersion = Number(sessionStorage.getItem("qingtian-event-version") || 0);
} catch (_error) {
  lastVersion = 0;
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
  try {
    sessionStorage.setItem("qingtian-event-version", String(lastVersion));
  } catch (_error) {
    // Storage can be unavailable in private contexts; in-memory dedupe still works.
  }
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
  if (!response.ok) throw new Error(payload.error || "请求失败");
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

function interventionCard(task, changed = false) {
  const action = task.human_action || {};
  const card = el("article", `intervention-card kind-${action.owner_kind || "none"}`);
  card.dataset.taskId = task.id;
  card.tabIndex = 0;
  if (changed) card.classList.add("is-updated");
  const head = el("div", "intervention-card-head");
  head.append(
    el("span", `intervention-priority p${task.priority}`, `P${task.priority}`),
    el("span", "intervention-owner", action.owner_kind === "user" ? "需要你处理" : "等待外部")
  );
  card.append(head, el("h3", "", task.title), el("p", "intervention-action", task.display_action));
  const timing = el("p", "intervention-timing");
  timing.textContent = `${action.owner || (action.owner_kind === "user" ? "你" : "外部主责")} · ` +
    `${action.due ? `截止 ${formatReportTime(action.due)}` : "未设截止"} · ${formatActionAge(action.since)}`;
  card.append(timing);
  if (action.sensitive) {
    card.append(el("p", "secret-entry-note", "生产 Key 仅通过安全 Secret 入口提供，禁止粘贴聊天。"));
  }
  const actions = el("div", "intervention-actions");
  if (action.owner_kind === "user") {
    const handle = el("button", "primary compact-action", "去处理");
    handle.type = "button";
    handle.addEventListener("click", (event) => {
      event.stopPropagation();
      openDetail(task.id);
    });
    const complete = el("button", "secondary compact-action", "我已完成，重新验证");
    complete.type = "button";
    complete.addEventListener("click", async (event) => {
      event.stopPropagation();
      complete.disabled = true;
      complete.textContent = "重新验证中…";
      try {
        await api(`/api/tasks/${task.id}/complete-human-action`, {
          method: "POST",
          body: "{}",
        });
        await refresh();
      } catch (error) {
        complete.disabled = false;
        complete.textContent = error.message;
      }
    });
    actions.append(handle, complete);
  } else if (action.sensitive) {
    const secure = el("button", "secondary compact-action", "查看安全提交方式");
    secure.type = "button";
    secure.addEventListener("click", (event) => {
      event.stopPropagation();
      openDetail(task.id);
    });
    actions.append(secure);
  } else {
    const remind = el("button", "secondary compact-action", "提醒对方");
    remind.type = "button";
    remind.title = "在控制面记录一次提醒，不会发送敏感内容";
    remind.addEventListener("click", async (event) => {
      event.stopPropagation();
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
    if (event.key === "Enter" || event.key === " ") openDetail(task.id);
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
  action.textContent = task.display_action || "无需你处理";
  const actionKind = task.human_action?.owner_kind || "none";
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
    return `- ${title}｜${shortSummary}｜${progress}%｜${stateLabel}`;
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

function applyDashboard(next, {force = false, changes = []} = {}) {
  const version = Number(next.version || 0);
  if (!force && dashboard && version && version <= lastVersion) return false;
  const previous = dashboard;
  const previousById = new Map((previous?.tasks || []).map((task) => [task.id, task]));
  const changedTaskIds = new Set(changes.map((change) => change.task_id).filter(Boolean));
  for (const task of next.tasks || []) {
    const old = previousById.get(task.id);
    if (!old || taskSignature(old) !== taskSignature(task)) changedTaskIds.add(task.id);
  }
  dashboard = next;
  if (version) rememberVersion(version);
  renderActionOverview(next);
  renderStats(next);
  renderBoard(next, previous, changedTaskIds, Boolean(previous));
  renderInterventions(next, changedTaskIds);
  renderSessions(next);
  renderEvents(next, previous);
  return true;
}

async function refresh({force = true, quiet = false} = {}) {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const [next, health] = await Promise.all([api("/api/dashboard"), api("/api/health")]);
      engineMode = health.mode || "";
      engineReadOnly = health.read_only === true;
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
  eventSource = new EventSource(`/api/events/stream?lastEventId=${encodeURIComponent(lastVersion)}`);
  const receive = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.reset) rememberVersion(0);
      applyDashboard(payload.dashboard, {
        force: Boolean(payload.reset),
        changes: payload.changes || [],
      });
      lastLiveAt = Date.now();
      reconnectAttempt = 0;
      setConnection("live", "实时");
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

async function openDetail(taskId) {
  const task = await api(`/api/tasks/${taskId}`);
  const liveTask = (dashboard?.tasks || []).find((item) => item.id === taskId);
  document.querySelector("#detailTitle").textContent = task.title;
  const body = document.querySelector("#detailBody");
  body.replaceChildren();
  const action = liveTask?.human_action || {
    owner_kind: task.action_owner_kind || "none",
    owner: task.action_owner || "",
    text: task.action_text || "",
    due: task.action_due || "",
    sensitive: Boolean(task.action_sensitive),
  };
  const actionCallout = el(
    "section",
    `detail-action action-${action.owner_kind || "none"}`,
    liveTask?.display_action || "无需你处理"
  );
  if (action.sensitive) {
    actionCallout.append(el("small", "", "敏感信息请通过安全渠道提供，禁止粘贴聊天"));
  }
  body.append(actionCallout);
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
  if (["RUNNING", "QUEUED"].includes(task.state)) {
    const cancel = el("button", "secondary", "取消任务");
    cancel.addEventListener("click", async () => {
      await api(`/api/tasks/${task.id}/cancel`, {method: "POST", body: "{}"});
      document.querySelector("#detailDialog").close();
      refresh();
    });
    actions.append(cancel);
  }
  if (["FAILED", "WAITING"].includes(task.state) && task.runs.some((run) => run.session_id)) {
    const retry = el("button", "primary", "继续会话");
    retry.addEventListener("click", async () => {
      await api(`/api/tasks/${task.id}/retry`, {method: "POST", body: "{}"});
      document.querySelector("#detailDialog").close();
      refresh();
    });
    actions.append(retry);
  }
  body.append(actions);
  document.querySelector("#detailDialog").showModal();
}

document.querySelector("#newTaskButton").addEventListener("click", () => {
  const dialog = document.querySelector("#intakeDialog");
  document.querySelector("#composerStatus").textContent = "";
  document.querySelector("#composerStatus").className = "";
  dialog.showModal();
  document.querySelector("#intakeText").focus();
});
document.querySelector("#systemDetailsButton").addEventListener("click", () => {
  document.querySelector("#systemDetailsDialog").showModal();
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
