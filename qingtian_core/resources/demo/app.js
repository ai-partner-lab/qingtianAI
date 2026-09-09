"use strict";

(() => {
  const RECORD_KEYS = ["sessions", "runs", "evidence", "checkpoints", "knowledge"];
  const ROLE_IDS = ["planner", "keeper", "builder", "tester", "reviewer", "archivist"];
  const REQUEST_TIMEOUT_MS = 10000;
  const CAPABILITY_RUN_TIMEOUT_MS = 70000;
  const RESET_CONFIRM_MS = 5000;
  const RUNNABLE_CAPABILITY_IDS = new Set(["api-e2e", "browser-e2e"]);

  const COPY = {
    zh: {
      skip: "跳到主要内容",
      brandSub: "深夜调度舞台",
      offlineMode: "离线引导模式",
      heroLineOne: "让 AI 协作的每一步",
      heroLineTwo: "都有据可查",
      heroDescription: "无需登录、无需云服务。沿着七步引导，观察任务、回执与知识记录如何确定性地流转。",
      factOffline: "完全离线",
      factReceipt: "回执驱动",
      factNoLogin: "无需登录",
      currentTask: "当前任务",
      connecting: "连接中",
      connected: "已连接",
      disconnected: "连接失败",
      loadingTask: "正在读取本地任务快照…",
      taskState: "任务状态",
      overallProgress: "整体进度",
      guidedRoute: "GUIDED ROUTE",
      sevenSteps: "AI 调度脑图",
      stepHint: "从 Leader 到专项 Agent、工作项逐层展开；浏览不会推进主流程。",
      crewStage: "RESPONSIBILITY MAP",
      dispatchStage: "AI 协作分工",
      snapshotLive: "快照实时映射",
      taskCore: "任务核心",
      waiting: "待命",
      active: "刚执行",
      nextRoleStatus: "下一步",
      receiptSeen: "有回执",
      selectRole: "选择一项协作职责查看说明",
      roleHint: "职责状态严格来自当前服务端快照。",
      avatarBoundaryTitle: "Q 版形象仅用于职责示意",
      avatarBoundaryText: "它们不是真人，也不表示已有六个自主模型连接或正在后台执行。",
      nextMove: "NEXT MOVE",
      waitingGuide: "正在准备下一步…",
      waitingGuideDescription: "本地服务返回引导后即可继续。",
      activeRole: "下一步职责",
      nextStep: "推进下一步",
      receiptOnly: "界面只呈现服务端返回的事件，不模拟执行成功。",
      retry: "重新连接",
      routeDone: "七步已完成",
      routeDoneHint: "所有展示均来自最终快照。",
      reset: "重置演示",
      resetConfirm: "再次点击确认重置",
      resetting: "正在重置…",
      resetHint: "需二次点击确认，避免误触。",
      resetArmedHint: "确认将在 5 秒后自动取消。按 Esc 也可取消。",
      receiptStream: "RECEIPT STREAM",
      eventTimeline: "事件回执时间线",
      events: "条",
      noEvents: "尚无回执。推进第一步后，服务端事件会出现在这里。",
      traceDesk: "TRACE DESK",
      recordsTitle: "记录与证据明细",
      localSnapshot: "本地快照",
      taskRecord: "任务 Task",
      sessions: "会话 Sessions",
      runs: "运行 Runs",
      evidence: "证据 Evidence",
      checkpoints: "检查点 Checkpoints",
      knowledge: "范围内知识 Scoped Knowledge",
      demoBoundary: "演示边界：",
      demoBoundaryText: "这是离线、引导式、确定性的产品演示，不是生产调度器，也不会访问真实知识库或执行外部任务。",
      footerMode: "离线引导演示",
      lastUpdated: "快照更新",
      requestFailed: "无法读取本地演示。请确认一键启动命令仍在运行。",
      invalidSnapshot: "本地服务返回了不完整的演示快照。",
      staleReloaded: "步骤已在其他窗口变化，已刷新为最新快照，请再次确认下一步。",
      stepFailed: "这一步没有完成。服务端未返回可展示的新快照。",
      resetFailed: "重置失败，当前快照保持不变。",
      resetRequired: "步骤未完成，真实状态已保留。为避免重复执行，请重置演示后再继续。",
      loadingNext: "等待服务端回执…",
      redacted: "[不显示敏感字段]",
      record: "记录",
      selectedStage: "正在查看",
      completedStage: "已完成阶段",
      currentStage: "当前引导阶段",
      upcomingStage: "尚未到达",
      capabilitiesLoading: "正在读取阶段能力…",
      capabilitiesLoadingHint: "能力目录独立于上方引导进度，不会自动推进任务。",
      capabilitiesUnavailable: "阶段能力目录暂不可用。主引导仍可独立使用。",
      capabilitiesRetrying: "正在重新读取能力目录…",
      retryCapabilities: "重试能力目录",
      mindRootTitle: "Qingtian 总控",
      mindRootMission: "统筹七个阶段，只呈现有回执的真实状态",
      expandMindTree: "展开 Qingtian 调度脑图",
      collapseMindTree: "收起 Qingtian 调度脑图",
      phaseLeader: "PHASE LEADER",
      leader: "Leader",
      leaderLoading: "正在读取阶段负责人使命…",
      phaseContractSummary: "查看阶段契约（前置条件 / 输入 / 输出）",
      collapseStage: "收起阶段详情",
      expandStage: "展开阶段详情",
      prerequisites: "前置条件",
      inputs: "输入",
      outputs: "输出",
      noneDeclared: "未声明",
      stageCapabilities: "本阶段专项 Agent",
      capabilityBoundary: "展开 Agent 可查看工作树与能力详情；未实现能力不会出现假运行按钮。",
      specialistAgent: "专项 Agent",
      agentMission: "Agent 使命",
      agentRunIdle: "未运行",
      agentRunActive: "独立验证中",
      workItems: "具体工作树",
      noWorkItems: "尚未登记工作项",
      workAction: "行动",
      workCheck: "检查",
      workArtifact: "产物",
      statusUnrun: "未运行",
      statusPartial: "回执未覆盖",
      statusPassed: "已通过",
      statusFailed: "失败",
      statusBlocked: "阻塞",
      linkedChecks: "关联检查",
      independentTitle: "独立验证，不推进主引导",
      independentBoundary: "可运行能力会创建独立临时任务，只验证本地开源 Demo；不代表真实业务验收，也不会改变上方七步进度。",
      capabilityPrerequisites: "能力前置",
      capabilityOutputs: "能力输出",
      capabilityBoundaryLabel: "边界",
      command: "命令",
      copy: "复制",
      copied: "已复制",
      copyFailed: "复制失败，请手动选择命令",
      runIndependent: "独立运行",
      runningIndependent: "独立运行中…",
      viewOnly: "仅查看契约",
      receipt: "独立运行回执",
      checks: "检查清单",
      checkItems: "项检查",
      checkPassed: "通过",
      checkFailed: "失败",
      checkBlocked: "阻塞",
      setupCommands: "可选安装指引",
      capabilityTimeout: "独立验证在 70 秒内未返回回执；未据此判定通过或失败。",
      capabilityBusy: "另一项独立验证正在运行，请等待其回执。",
      capabilityRunFailed: "无法取得独立验证回执；主引导未受影响。",
      noCapabilities: "此阶段尚未登记能力。",
      availabilityBundled: "内置可用",
      availabilityOptional: "内置 · 可选依赖",
      availabilityAdapter: "需要适配器",
      availabilityPlanned: "规划中",
      availabilityUnknown: "状态未声明",
    },
    en: {
      skip: "Skip to main content",
      brandSub: "Night dispatch stage",
      offlineMode: "Offline guided mode",
      heroLineOne: "Make every step of AI collaboration",
      heroLineTwo: "traceable by evidence",
      heroDescription: "No login and no cloud service. Follow seven guided steps to see tasks, receipts, and knowledge records move deterministically.",
      factOffline: "Fully offline",
      factReceipt: "Receipt-driven",
      factNoLogin: "No login",
      currentTask: "Current task",
      connecting: "Connecting",
      connected: "Connected",
      disconnected: "Connection failed",
      loadingTask: "Reading the local task snapshot…",
      taskState: "Task state",
      overallProgress: "Overall progress",
      guidedRoute: "GUIDED ROUTE",
      sevenSteps: "Dispatch mind map",
      stepHint: "Expand from Leader to specialist Agent and work items. Browsing never advances the main guide.",
      crewStage: "RESPONSIBILITY MAP",
      dispatchStage: "AI collaboration responsibilities",
      snapshotLive: "Snapshot mapped live",
      taskCore: "Task core",
      waiting: "Stand by",
      active: "Last receipt",
      nextRoleStatus: "Next",
      receiptSeen: "Receipt",
      selectRole: "Select a collaboration responsibility",
      roleHint: "Responsibility state comes strictly from the current server snapshot.",
      avatarBoundaryTitle: "Chibi figures illustrate responsibilities only",
      avatarBoundaryText: "They are not people and do not imply that six autonomous models are connected or working in the background.",
      nextMove: "NEXT MOVE",
      waitingGuide: "Preparing the next move…",
      waitingGuideDescription: "Continue when the local service returns its guide.",
      activeRole: "Next responsibility",
      nextStep: "Advance one step",
      receiptOnly: "The interface presents returned events only; it never simulates execution success.",
      retry: "Reconnect",
      routeDone: "All seven steps complete",
      routeDoneHint: "Everything shown comes from the final snapshot.",
      reset: "Reset demo",
      resetConfirm: "Click again to confirm reset",
      resetting: "Resetting…",
      resetHint: "A second click is required to prevent accidental resets.",
      resetArmedHint: "Confirmation cancels after 5 seconds. Press Esc to cancel now.",
      receiptStream: "RECEIPT STREAM",
      eventTimeline: "Receipt event timeline",
      events: "events",
      noEvents: "No receipts yet. Server events will appear after the first step.",
      traceDesk: "TRACE DESK",
      recordsTitle: "Records and evidence",
      localSnapshot: "Local snapshot",
      taskRecord: "Task",
      sessions: "Sessions",
      runs: "Runs",
      evidence: "Evidence",
      checkpoints: "Checkpoints",
      knowledge: "Scoped Knowledge",
      demoBoundary: "Demo boundary:",
      demoBoundaryText: "This is an offline, guided, deterministic product demo—not a production scheduler. It does not access a real knowledge base or execute external work.",
      footerMode: "Offline guided demo",
      lastUpdated: "Snapshot updated",
      requestFailed: "The local demo could not be reached. Confirm that the one-command launcher is still running.",
      invalidSnapshot: "The local service returned an incomplete demo snapshot.",
      staleReloaded: "The step changed in another window. The latest snapshot is loaded; please confirm the next move again.",
      stepFailed: "This step did not complete. The server returned no new displayable snapshot.",
      resetFailed: "Reset failed. The current snapshot is unchanged.",
      resetRequired: "The step did not complete and its real state is preserved. Reset the demo before continuing to avoid duplicate work.",
      loadingNext: "Waiting for server receipt…",
      redacted: "[sensitive field hidden]",
      record: "Record",
      selectedStage: "Selected view",
      completedStage: "Completed phase",
      currentStage: "Current guide phase",
      upcomingStage: "Not reached",
      capabilitiesLoading: "Loading phase capabilities…",
      capabilitiesLoadingHint: "The capability catalog is independent of guide progress and never advances the task.",
      capabilitiesUnavailable: "The phase capability catalog is unavailable. The main guide remains independent.",
      capabilitiesRetrying: "Reloading the capability catalog…",
      retryCapabilities: "Retry capability catalog",
      mindRootTitle: "Qingtian control",
      mindRootMission: "Coordinates seven phases and shows only receipt-backed state",
      expandMindTree: "Expand the Qingtian dispatch mind map",
      collapseMindTree: "Collapse the Qingtian dispatch mind map",
      phaseLeader: "PHASE LEADER",
      leader: "Leader",
      leaderLoading: "Loading the phase leader mission…",
      phaseContractSummary: "View phase contract (prerequisites / inputs / outputs)",
      collapseStage: "Collapse phase details",
      expandStage: "Expand phase details",
      prerequisites: "Prerequisites",
      inputs: "Inputs",
      outputs: "Outputs",
      noneDeclared: "None declared",
      stageCapabilities: "Specialist agents in this phase",
      capabilityBoundary: "Expand an agent to inspect its work tree and capability details. Unimplemented capabilities never get a fake run button.",
      specialistAgent: "Specialist agent",
      agentMission: "Agent mission",
      agentRunIdle: "Not run",
      agentRunActive: "Independent run active",
      workItems: "Work tree",
      noWorkItems: "No work items registered",
      workAction: "Action",
      workCheck: "Check",
      workArtifact: "Artifact",
      statusUnrun: "Not run",
      statusPartial: "Not covered by receipt",
      statusPassed: "Passed",
      statusFailed: "Failed",
      statusBlocked: "Blocked",
      linkedChecks: "Linked checks",
      independentTitle: "Independent verification; guide unchanged",
      independentBoundary: "Runnable capabilities create temporary independent tasks to verify this local open-source demo. They are not real product acceptance and do not advance the seven-step guide.",
      capabilityPrerequisites: "Prerequisites",
      capabilityOutputs: "Outputs",
      capabilityBoundaryLabel: "Boundary",
      command: "Command",
      copy: "Copy",
      copied: "Copied",
      copyFailed: "Copy failed; select the command manually",
      runIndependent: "Run independently",
      runningIndependent: "Independent run in progress…",
      viewOnly: "Contract only",
      receipt: "Independent run receipt",
      checks: "Checks",
      checkItems: "checks",
      checkPassed: "passed",
      checkFailed: "failed",
      checkBlocked: "blocked",
      setupCommands: "Optional setup commands",
      capabilityTimeout: "No receipt returned within 70 seconds. This is not classified as passed or failed.",
      capabilityBusy: "Another independent verification is running; wait for its receipt.",
      capabilityRunFailed: "No independent verification receipt was obtained. The main guide is unaffected.",
      noCapabilities: "No capabilities are registered for this phase.",
      availabilityBundled: "Bundled",
      availabilityOptional: "Bundled · optional dependency",
      availabilityAdapter: "Adapter required",
      availabilityPlanned: "Planned",
      availabilityUnknown: "Availability unspecified",
    },
  };

  const STEP_LABELS = {
    zh: ["规划", "知识准备", "执行", "回执丢失", "核对与接手", "审查", "归档"],
    en: ["Plan", "Prepare knowledge", "Execute", "Receipt loss", "Reconcile & hand off", "Review", "Archive"],
  };

  const ROLE_COPY = {
    zh: {
      planner: ["规划者", "把目标拆成可验证、可交接的下一步。", "规"],
      keeper: ["知识管理员", "登记来源、证据等级与知识边界。", "知"],
      builder: ["执行者", "按明确输入推进本地确定性执行。", "执"],
      tester: ["测试者", "核对契约与可复现的验证结果。", "测"],
      reviewer: ["审查者", "检查证据，保留未知与分歧。", "审"],
      archivist: ["归档者", "机器人管理员，封存检查点和最终记录。", "档"],
    },
    en: {
      planner: ["Planner", "Turns the objective into verifiable, handoff-ready next moves.", "PL"],
      keeper: ["Knowledge keeper", "Registers sources, evidence levels, and knowledge boundaries.", "KN"],
      builder: ["Builder", "Advances deterministic local work from explicit inputs.", "BU"],
      tester: ["Tester", "Checks contracts and reproducible verification results.", "TE"],
      reviewer: ["Reviewer", "Reviews evidence while preserving unknowns and disagreement.", "RE"],
      archivist: ["Archivist", "A robot custodian that seals checkpoints and final records.", "AR"],
    },
  };

  const dom = {
    languageToggle: document.querySelector("#language-toggle"),
    connectionPill: document.querySelector("#connection-pill"),
    connectionLabel: document.querySelector("#connection-label"),
    missionTitle: document.querySelector("#mission-title"),
    missionObjective: document.querySelector("#mission-objective"),
    taskState: document.querySelector("#task-state"),
    coreState: document.querySelector("#core-state"),
    stepValue: document.querySelector("#step-value"),
    stepTotal: document.querySelector("#step-total"),
    progressTrack: document.querySelector("#progress-track"),
    progressFill: document.querySelector("#progress-fill"),
    mindRootToggle: document.querySelector("#mind-root-toggle"),
    mindRootBranches: document.querySelector("#mind-root-branches"),
    stepRail: document.querySelector("#step-rail"),
    phaseExplorer: document.querySelector("#phase-explorer"),
    phaseIndex: document.querySelector("#phase-index"),
    phaseState: document.querySelector("#phase-state"),
    phaseTitle: document.querySelector("#phase-title"),
    phaseSummary: document.querySelector("#phase-summary"),
    phaseToggle: document.querySelector("#phase-toggle"),
    phaseDetailContent: document.querySelector("#phase-detail-content"),
    phasePrerequisites: document.querySelector("#phase-prerequisites"),
    phaseInputs: document.querySelector("#phase-inputs"),
    phaseOutputs: document.querySelector("#phase-outputs"),
    phaseLeaderName: document.querySelector("#phase-leader-name"),
    phaseLeaderMission: document.querySelector("#phase-leader-mission"),
    capabilityGrid: document.querySelector("#capability-grid"),
    capabilityMessage: document.querySelector("#capability-message"),
    capabilityRetry: document.querySelector("#capability-retry"),
    crewStage: document.querySelector("#crew-stage"),
    crewCards: Array.from(document.querySelectorAll(".crew-card[data-role]")),
    roleInspectorName: document.querySelector("#role-inspector-name"),
    roleInspectorDescription: document.querySelector("#role-inspector-description"),
    roleInspectorIcon: document.querySelector("#role-inspector-icon"),
    guideStepNumber: document.querySelector("#guide-step-number"),
    guideTitle: document.querySelector("#guide-title"),
    guideDescription: document.querySelector("#guide-description"),
    activeRoleLabel: document.querySelector("#active-role-label"),
    nextStep: document.querySelector("#next-step"),
    nextLabel: document.querySelector("#next-label"),
    errorBanner: document.querySelector("#error-banner"),
    errorMessage: document.querySelector("#error-message"),
    retryButton: document.querySelector("#retry-button"),
    doneBanner: document.querySelector("#done-banner"),
    resetButton: document.querySelector("#reset-button"),
    resetLabel: document.querySelector("#reset-label"),
    resetHint: document.querySelector("#reset-hint"),
    eventList: document.querySelector("#event-list"),
    eventEmpty: document.querySelector("#event-empty"),
    eventCount: document.querySelector("#event-count"),
    recordGroups: Array.from(document.querySelectorAll(".record-group[data-record-key]")),
    lastUpdated: document.querySelector("#last-updated"),
  };

  let language = "zh";
  let snapshot = null;
  let selectedRole = null;
  let busy = false;
  let resetArmed = false;
  let resetTimer = 0;
  let pulseTimer = 0;
  let renderedEventIds = new Set();
  let selectedStage = 1;
  let mindTreeExpanded = true;
  let phaseExpanded = true;
  let capabilityCatalog = null;
  let capabilityCatalogError = null;
  let capabilityCatalogLoading = false;
  let capabilityBusyId = null;
  const capabilityReceipts = new Map();
  const capabilityErrors = new Map();
  const expandedAgentIds = new Set();
  const expandedWorkKeys = new Set();

  function t(key) {
    return COPY[language][key] || COPY.zh[key] || key;
  }

  function setText(node, value) {
    if (node) {
      node.textContent = value == null ? "" : String(value);
    }
  }

  function focusWithoutScroll(node) {
    if (!node || typeof node.focus !== "function") {
      return;
    }
    try {
      node.focus({ preventScroll: true });
    } catch (_error) {
      node.focus();
    }
  }

  function applyStaticLanguage() {
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.querySelectorAll("[data-i18n]").forEach((node) => {
      const key = node.getAttribute("data-i18n");
      if (key && COPY[language][key]) {
        node.textContent = COPY[language][key];
      }
    });
    dom.languageToggle.setAttribute(
      "aria-label",
      language === "zh" ? "Switch to English" : "切换为中文",
    );
    document.title = language === "zh" ? "Qingtian · 深夜调度舞台" : "Qingtian · Night Dispatch Stage";
    dom.stepRail.setAttribute("aria-label", t("sevenSteps"));
    updateMindRoot();
    buildStepRail();
    renderPhaseExplorer();
  }

  function updateMindRoot() {
    dom.mindRootToggle.setAttribute("aria-expanded", String(mindTreeExpanded));
    dom.mindRootToggle.setAttribute(
      "aria-label",
      mindTreeExpanded ? t("collapseMindTree") : t("expandMindTree"),
    );
    dom.mindRootBranches.hidden = !mindTreeExpanded;
    dom.mindRootToggle.parentElement.dataset.expanded = String(mindTreeExpanded);
  }

  function toggleMindTree() {
    mindTreeExpanded = !mindTreeExpanded;
    updateMindRoot();
  }

  function buildStepRail() {
    const nodes = STEP_LABELS[language].map((label, index) => {
      const item = document.createElement("li");
      item.className = "step-node";
      item.dataset.state = "waiting";
      item.dataset.selected = String(index + 1 === selectedStage);

      const button = document.createElement("button");
      button.className = "step-button";
      button.id = `step-phase-${index + 1}`;
      button.type = "button";
      button.dataset.phaseStep = String(index + 1);
      button.setAttribute("aria-controls", "phase-detail-content");
      button.setAttribute("aria-expanded", String(index + 1 === selectedStage && phaseExpanded));
      button.setAttribute("aria-pressed", String(index + 1 === selectedStage));

      const dot = document.createElement("span");
      dot.className = "step-dot";
      dot.setAttribute("aria-hidden", "true");
      dot.textContent = String(index + 1).padStart(2, "0");
      const text = document.createElement("span");
      text.className = "step-label";
      text.textContent = label;
      const leader = document.createElement("span");
      leader.className = "step-leader";
      leader.dataset.stepLeader = String(index + 1);
      setText(leader, `${t("leader")} · —`);
      button.append(dot, text, leader);
      item.append(button);
      return item;
    });
    dom.stepRail.replaceChildren(...nodes);
    updateStepRail();
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function validateSnapshot(value) {
    if (
      !isObject(value) ||
      value.mode !== "offline-guided" ||
      !Number.isInteger(value.step) ||
      !Number.isInteger(value.total_steps) ||
      value.step < 0 ||
      value.total_steps < 1 ||
      value.step > value.total_steps ||
      typeof value.csrf_token !== "string" ||
      value.csrf_token.length < 1 ||
      !isObject(value.guide) ||
      !Array.isArray(value.events) ||
      !Array.isArray(value.roles) ||
      !isObject(value.records) ||
      typeof value.done !== "boolean" ||
      typeof value.requires_reset !== "boolean" ||
      !(value.error === null || isObject(value.error))
    ) {
      throw new Error(t("invalidSnapshot"));
    }
    for (const key of RECORD_KEYS) {
      if (!Array.isArray(value.records[key])) {
        throw new Error(t("invalidSnapshot"));
      }
    }
    return value;
  }

  function taskField(task, keys, fallback) {
    if (!isObject(task)) {
      return fallback;
    }
    for (const key of keys) {
      if (typeof task[key] === "string" && task[key].trim()) {
        return task[key].trim();
      }
    }
    return fallback;
  }

  function roleFromSnapshot(roleId) {
    if (!snapshot || !roleId) {
      return null;
    }
    return snapshot.roles.find((role) => isObject(role) && role.id === roleId) || null;
  }

  function rolePresentation(roleId) {
    const local = ROLE_COPY[language][roleId] || [roleId || "—", t("roleHint"), "?"];
    const server = roleFromSnapshot(roleId);
    if (!server) {
      return { title: local[0], description: local[1], monogram: local[2], name: roleId || "—" };
    }
    const titleField = language === "zh" ? server.name : server.title;
    const title = typeof titleField === "string" && titleField.trim() ? titleField.trim() : local[0];
    const description = language === "zh" && typeof server.description === "string" && server.description.trim()
      ? server.description.trim()
      : local[1];
    const nameField = language === "zh" ? server.title : server.id;
    const name = typeof nameField === "string" && nameField.trim() ? nameField.trim() : roleId;
    return { title, description, monogram: local[2], name };
  }

  function phaseForStep(step) {
    return capabilityCatalog
      ? capabilityCatalog.phases.find((phase) => phase.step === step) || null
      : null;
  }

  function updateStepRail() {
    const step = snapshot ? snapshot.step : 0;
    const total = snapshot ? snapshot.total_steps : STEP_LABELS[language].length;
    Array.from(dom.stepRail.children).forEach((item, index) => {
      const ordinal = index + 1;
      const button = item.querySelector(".step-button");
      const dot = item.querySelector(".step-dot");
      const leaderLabel = item.querySelector("[data-step-leader]");
      const phase = phaseForStep(ordinal);
      if (ordinal <= step) {
        item.dataset.state = "complete";
      } else if (ordinal === step + 1 && step < total) {
        item.dataset.state = "active";
      } else {
        item.dataset.state = "waiting";
      }
      item.dataset.selected = String(ordinal === selectedStage);
      dot.textContent = item.dataset.state === "complete" ? "✓" : String(ordinal).padStart(2, "0");
      button.setAttribute("aria-current", item.dataset.state === "active" ? "step" : "false");
      button.setAttribute("aria-expanded", String(ordinal === selectedStage && phaseExpanded));
      button.setAttribute("aria-pressed", String(ordinal === selectedStage));
      setText(leaderLabel, `${t("leader")} · ${phase ? phase.leader.name : "—"}`);
      button.setAttribute(
        "aria-label",
        `${STEP_LABELS[language][index]} · ${t("leader")} ${phase ? phase.leader.name : "—"}`,
      );
    });
  }

  function selectStage(step) {
    if (!Number.isInteger(step) || step < 1 || step > STEP_LABELS[language].length) {
      return;
    }
    selectedStage = step;
    phaseExpanded = true;
    updateStepRail();
    renderPhaseExplorer();
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    dom.phaseExplorer.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
  }

  function togglePhaseDetails() {
    phaseExpanded = !phaseExpanded;
    dom.phaseDetailContent.hidden = !phaseExpanded;
    dom.phaseToggle.setAttribute("aria-expanded", String(phaseExpanded));
    const label = dom.phaseToggle.querySelector("span");
    setText(label, phaseExpanded ? t("collapseStage") : t("expandStage"));
    updateStepRail();
  }

  function isStringArray(value) {
    return Array.isArray(value) && value.every((item) => typeof item === "string");
  }

  function validateWorkItems(workItems) {
    if (!Array.isArray(workItems)) {
      return false;
    }
    const ids = new Set();
    const pending = [...workItems];
    while (pending.length) {
      const item = pending.pop();
      if (
        !isObject(item) ||
        typeof item.id !== "string" ||
        !item.id ||
        ids.has(item.id) ||
        typeof item.title !== "string" ||
        !["action", "check", "artifact"].includes(item.kind) ||
        typeof item.description !== "string" ||
        !isStringArray(item.check_ids) ||
        !Array.isArray(item.children)
      ) {
        return false;
      }
      ids.add(item.id);
      pending.push(...item.children);
    }
    return true;
  }

  function validateCapabilityCatalog(value) {
    if (
      !isObject(value) ||
      value.schema_version !== 1 ||
      !Array.isArray(value.phases) ||
      !Array.isArray(value.capabilities)
    ) {
      throw new Error(t("capabilitiesUnavailable"));
    }
    const phaseIds = new Set();
    const phaseSteps = new Set();
    for (const phase of value.phases) {
      if (
        !isObject(phase) ||
        typeof phase.id !== "string" ||
        !phase.id ||
        !Number.isInteger(phase.step) ||
        phase.step < 1 ||
        typeof phase.title !== "string" ||
        typeof phase.summary !== "string" ||
        !Array.isArray(phase.inputs) ||
        !Array.isArray(phase.outputs) ||
        !Array.isArray(phase.prerequisites) ||
        !Array.isArray(phase.capability_ids) ||
        !isObject(phase.leader) ||
        typeof phase.leader.name !== "string" ||
        !phase.leader.name ||
        typeof phase.leader.mission !== "string" ||
        phaseIds.has(phase.id) ||
        phaseSteps.has(phase.step)
      ) {
        throw new Error(t("capabilitiesUnavailable"));
      }
      phaseIds.add(phase.id);
      phaseSteps.add(phase.step);
    }
    const capabilityIds = new Set();
    for (const capability of value.capabilities) {
      if (
        !isObject(capability) ||
        typeof capability.id !== "string" ||
        !capability.id ||
        typeof capability.phase_id !== "string" ||
        typeof capability.title !== "string" ||
        typeof capability.category !== "string" ||
        typeof capability.availability !== "string" ||
        typeof capability.runnable !== "boolean" ||
        typeof capability.description !== "string" ||
        !Array.isArray(capability.prerequisites) ||
        !Array.isArray(capability.outputs) ||
        typeof capability.boundary !== "string" ||
        !isObject(capability.agent) ||
        typeof capability.agent.name !== "string" ||
        !capability.agent.name ||
        typeof capability.agent.mission !== "string" ||
        !validateWorkItems(capability.agent.work_items) ||
        capabilityIds.has(capability.id)
      ) {
        throw new Error(t("capabilitiesUnavailable"));
      }
      capabilityIds.add(capability.id);
    }
    return value;
  }

  function replaceTextList(node, values) {
    const source = Array.isArray(values) && values.length ? values : [t("noneDeclared")];
    const items = source.map((value) => {
      const item = document.createElement("li");
      setText(item, displayValue(value));
      return item;
    });
    node.replaceChildren(...items);
  }

  function availabilityPresentation(value) {
    const mapping = {
      bundled: [t("availabilityBundled"), "bundled"],
      "bundled-optional": [t("availabilityOptional"), "optional"],
      "adapter-required": [t("availabilityAdapter"), "adapter"],
      planned: [t("availabilityPlanned"), "planned"],
    };
    return mapping[value] || [`${t("availabilityUnknown")} · ${value || "—"}`, "unknown"];
  }

  function appendCapabilityList(parent, titleText, values) {
    const section = document.createElement("section");
    const title = document.createElement("h6");
    const list = document.createElement("ul");
    list.className = "capability-list";
    setText(title, titleText);
    replaceTextList(list, values);
    section.append(title, list);
    parent.append(section);
  }

  function appendReceiptMeta(list, key, value) {
    if (value === undefined || value === null || value === "") {
      return;
    }
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    setText(term, key);
    setText(detail, displayValue(value));
    list.append(term, detail);
  }

  function createReceiptPanel(receipt) {
    const panel = document.createElement("section");
    panel.className = "capability-receipt";
    panel.dataset.capabilityReceipt = receipt.capability_id;

    const heading = document.createElement("div");
    heading.className = "receipt-heading";
    const title = document.createElement("strong");
    const status = document.createElement("span");
    status.className = "receipt-status";
    status.dataset.tone = receipt.status;
    setText(title, t("receipt"));
    setText(status, receipt.status);
    heading.append(title, status);

    const metadata = document.createElement("dl");
    metadata.className = "receipt-meta";
    appendReceiptMeta(metadata, "run_id", receipt.run_id);
    appendReceiptMeta(metadata, "scope", receipt.scope);
    appendReceiptMeta(metadata, "started_at", receipt.started_at);
    appendReceiptMeta(metadata, "duration_ms", receipt.duration_ms);
    appendReceiptMeta(metadata, "reason_code", receipt.reason_code);
    appendReceiptMeta(metadata, "browser", receipt.browser);
    appendReceiptMeta(metadata, "task_ids", receipt.task_ids);

    const checkCounts = { passed: 0, failed: 0, blocked: 0 };
    for (const check of receipt.checks) {
      if (isObject(check)) {
        const statusKey = String(check.status || "").toLowerCase();
        if (Object.prototype.hasOwnProperty.call(checkCounts, statusKey)) {
          checkCounts[statusKey] += 1;
        }
      }
    }
    const checkDetails = document.createElement("details");
    checkDetails.className = "receipt-check-details";
    const checksSummary = document.createElement("summary");
    const summaryLabel = `${t("checks")} · ${receipt.checks.length} ${t("checkItems")} · ${checkCounts.passed} ${t("checkPassed")} · ${checkCounts.failed} ${t("checkFailed")} · ${checkCounts.blocked} ${t("checkBlocked")}`;
    setText(checksSummary, summaryLabel);
    const checks = document.createElement("ul");
    checks.className = "receipt-checks";
    for (const check of receipt.checks) {
      const item = document.createElement("li");
      item.className = "receipt-check";
      const checkState = document.createElement("span");
      const checkDetail = document.createElement("span");
      setText(checkState, isObject(check) ? `${check.id || "check"} · ${check.status || "—"}` : "check");
      setText(checkDetail, isObject(check) ? check.detail || "—" : displayValue(check));
      item.append(checkState, checkDetail);
      checks.append(item);
    }
    checkDetails.append(checksSummary, checks);

    panel.append(heading, metadata, checkDetails);
    if (receipt.setup_commands.length) {
      const setup = document.createElement("div");
      setup.className = "setup-block";
      const setupTitle = document.createElement("strong");
      const setupList = document.createElement("ul");
      setupList.className = "setup-list";
      setText(setupTitle, t("setupCommands"));
      for (const command of receipt.setup_commands) {
        const item = document.createElement("li");
        const code = document.createElement("code");
        code.tabIndex = 0;
        setText(code, displayValue(command));
        item.append(code);
        setupList.append(item);
      }
      setup.append(setupTitle, setupList);
      panel.append(setup);
    }
    return panel;
  }

  function workKindPresentation(kind) {
    const labels = {
      action: t("workAction"),
      check: t("workCheck"),
      artifact: t("workArtifact"),
    };
    return labels[kind] || kind;
  }

  function receiptCheckMap(receipt) {
    const checks = new Map();
    if (!receipt) {
      return checks;
    }
    for (const check of receipt.checks) {
      if (isObject(check) && typeof check.id === "string") {
        checks.set(check.id, check);
      }
    }
    return checks;
  }

  function normalizeCheckStatus(status) {
    const normalized = String(status || "").toLowerCase();
    return ["passed", "failed", "blocked"].includes(normalized) ? normalized : "partial";
  }

  function workStatus(item, receipt, checksById) {
    if (!item.check_ids.length) {
      return null;
    }
    if (!receipt) {
      return "unrun";
    }
    const statuses = item.check_ids.map((checkId) => {
      const check = checksById.get(checkId);
      return check ? normalizeCheckStatus(check.status) : "partial";
    });
    if (statuses.includes("failed")) {
      return "failed";
    }
    if (statuses.includes("blocked")) {
      return "blocked";
    }
    if (statuses.includes("partial")) {
      return "partial";
    }
    return statuses.every((status) => status === "passed") ? "passed" : "partial";
  }

  function workStatusLabel(status) {
    const keys = {
      unrun: "statusUnrun",
      partial: "statusPartial",
      passed: "statusPassed",
      failed: "statusFailed",
      blocked: "statusBlocked",
    };
    return t(keys[status] || "statusPartial");
  }

  function domToken(value) {
    return encodeURIComponent(String(value)).replace(/%/g, "_");
  }

  function createWorkNode(item, capability, path, receipt, checksById) {
    const workPath = [...path, item.id];
    const workKey = [capability.id, ...workPath].join("/");
    const expanded = expandedWorkKeys.has(workKey);
    const node = document.createElement("li");
    node.className = "work-node";
    node.dataset.workNode = item.id;
    node.dataset.workKind = item.kind;
    node.dataset.expanded = String(expanded);

    const toggle = document.createElement("button");
    toggle.className = "work-node-toggle";
    toggle.type = "button";
    toggle.dataset.workToggle = item.id;
    toggle.dataset.workKey = workKey;
    toggle.setAttribute("aria-expanded", String(expanded));
    const detailId = `work-detail-${domToken(workKey)}`;
    toggle.setAttribute("aria-controls", detailId);

    const branchMark = document.createElement("span");
    branchMark.className = "work-branch-mark";
    branchMark.setAttribute("aria-hidden", "true");
    setText(branchMark, item.kind === "action" ? "→" : item.kind === "check" ? "✓" : "◇");
    const copy = document.createElement("span");
    copy.className = "work-node-copy";
    const kind = document.createElement("small");
    const title = document.createElement("strong");
    setText(kind, workKindPresentation(item.kind));
    setText(title, item.title);
    copy.append(kind, title);
    toggle.append(branchMark, copy);

    const status = workStatus(item, receipt, checksById);
    if (status) {
      const badge = document.createElement("span");
      badge.className = "work-status";
      badge.dataset.tone = status;
      setText(badge, workStatusLabel(status));
      toggle.append(badge);
    }
    const chevron = document.createElement("span");
    chevron.className = "tree-chevron";
    chevron.setAttribute("aria-hidden", "true");
    setText(chevron, "⌄");
    toggle.append(chevron);

    const detail = document.createElement("div");
    detail.className = "work-node-detail";
    detail.id = detailId;
    detail.dataset.workDetail = item.id;
    detail.hidden = !expanded;
    const description = document.createElement("p");
    setText(description, item.description);
    detail.append(description);

    if (item.check_ids.length) {
      const checkBlock = document.createElement("div");
      checkBlock.className = "work-check-links";
      const checkLabel = document.createElement("strong");
      setText(checkLabel, t("linkedChecks"));
      const checkList = document.createElement("div");
      checkList.className = "work-check-list";
      for (const checkId of item.check_ids) {
        const check = checksById.get(checkId);
        const checkTone = !receipt ? "unrun" : check ? normalizeCheckStatus(check.status) : "partial";
        const checkBadge = document.createElement("span");
        checkBadge.className = "work-check-badge";
        checkBadge.dataset.tone = checkTone;
        setText(checkBadge, `${checkId} · ${workStatusLabel(checkTone)}`);
        checkList.append(checkBadge);
      }
      checkBlock.append(checkLabel, checkList);
      detail.append(checkBlock);
    }

    if (item.children.length) {
      detail.append(createWorkTree(item.children, capability, workPath, receipt, checksById));
    }
    node.append(toggle, detail);
    return node;
  }

  function createWorkTree(workItems, capability, path = [], receipt = null, checksById = new Map()) {
    const tree = document.createElement("ul");
    tree.className = path.length ? "work-tree work-tree-nested" : "work-tree";
    for (const item of workItems) {
      tree.append(createWorkNode(item, capability, path, receipt, checksById));
    }
    return tree;
  }

  function createAgentNode(capability, index) {
    const expanded = expandedAgentIds.has(capability.id);
    const receipt = capabilityReceipts.get(capability.id) || null;
    const executionState = capabilityBusyId === capability.id
      ? "running"
      : receipt
        ? receipt.status
        : "unrun";
    const node = document.createElement("section");
    node.className = "agent-tree-node";
    node.dataset.agentId = capability.id;
    node.dataset.expanded = String(expanded);
    node.dataset.execution = executionState;

    const toggle = document.createElement("button");
    toggle.className = "agent-node-toggle";
    toggle.type = "button";
    toggle.dataset.agentToggle = capability.id;
    toggle.setAttribute("aria-expanded", String(expanded));
    const detailId = `agent-detail-${domToken(capability.id)}`;
    toggle.setAttribute("aria-controls", detailId);

    const branch = document.createElement("span");
    branch.className = "agent-branch-mark";
    branch.setAttribute("aria-hidden", "true");
    setText(branch, `A${String(index + 1).padStart(2, "0")}`);
    const identity = document.createElement("span");
    identity.className = "agent-node-copy";
    const type = document.createElement("small");
    const name = document.createElement("strong");
    const capabilityTitle = document.createElement("span");
    setText(type, t("specialistAgent"));
    setText(name, capability.agent.name);
    setText(capabilityTitle, capability.title);
    identity.append(type, name, capabilityTitle);
    const availability = document.createElement("span");
    const availabilityView = availabilityPresentation(capability.availability);
    availability.className = "availability-badge agent-availability";
    availability.dataset.tone = availabilityView[1];
    availability.title = capability.availability;
    setText(availability, availabilityView[0]);
    const badges = document.createElement("span");
    badges.className = "agent-node-badges";
    const execution = document.createElement("span");
    execution.className = "agent-execution-status";
    execution.dataset.agentExecutionStatus = capability.id;
    execution.dataset.tone = executionState;
    execution.setAttribute("aria-live", "polite");
    setText(
      execution,
      executionState === "running"
        ? t("agentRunActive")
        : executionState === "unrun"
          ? t("agentRunIdle")
          : workStatusLabel(executionState),
    );
    badges.append(availability, execution);
    const chevron = document.createElement("span");
    chevron.className = "tree-chevron";
    chevron.setAttribute("aria-hidden", "true");
    setText(chevron, "⌄");
    toggle.append(branch, identity, badges, chevron);

    const detail = document.createElement("div");
    detail.className = "agent-node-detail";
    detail.id = detailId;
    detail.dataset.agentDetail = capability.id;
    detail.hidden = !expanded;
    if (expanded) {
      const mission = document.createElement("div");
      mission.className = "agent-mission";
      const missionLabel = document.createElement("strong");
      const missionText = document.createElement("p");
      setText(missionLabel, t("agentMission"));
      setText(missionText, capability.agent.mission);
      mission.append(missionLabel, missionText);

      const workSection = document.createElement("section");
      workSection.className = "agent-work-section";
      const workHeading = document.createElement("h5");
      setText(workHeading, t("workItems"));
      workSection.append(workHeading);
      const checksById = receiptCheckMap(receipt);
      if (capability.agent.work_items.length) {
        workSection.append(createWorkTree(capability.agent.work_items, capability, [], receipt, checksById));
      } else {
        const empty = document.createElement("p");
        empty.className = "work-empty";
        setText(empty, t("noWorkItems"));
        workSection.append(empty);
      }
      detail.append(mission, workSection, createCapabilityCard(capability));
    }
    node.append(toggle, detail);
    return node;
  }

  function createCapabilityCard(capability) {
    const card = document.createElement("article");
    card.className = "capability-card";
    card.dataset.capabilityId = capability.id;
    const isRunnable = capability.runnable === true && RUNNABLE_CAPABILITY_IDS.has(capability.id);
    card.dataset.runnable = String(isRunnable);

    const heading = document.createElement("div");
    heading.className = "capability-card-heading";
    const headingCopy = document.createElement("div");
    const title = document.createElement("h5");
    const category = document.createElement("span");
    category.className = "capability-category";
    setText(title, capability.title);
    setText(category, `${capability.category} · ${capability.id}`);
    headingCopy.append(title, category);
    const availability = document.createElement("span");
    const availabilityView = availabilityPresentation(capability.availability);
    availability.className = "availability-badge";
    availability.dataset.tone = availabilityView[1];
    availability.title = capability.availability;
    setText(availability, availabilityView[0]);
    heading.append(headingCopy, availability);

    const description = document.createElement("p");
    description.className = "capability-description";
    setText(description, capability.description);

    const metadata = document.createElement("div");
    metadata.className = "capability-meta-group";
    appendCapabilityList(metadata, t("capabilityPrerequisites"), capability.prerequisites);
    appendCapabilityList(metadata, t("capabilityOutputs"), capability.outputs);

    const boundary = document.createElement("p");
    boundary.className = "capability-boundary";
    setText(boundary, `${t("capabilityBoundaryLabel")} · ${capability.boundary}`);
    card.append(heading, description, metadata, boundary);

    const commandValue = typeof capability.command === "string"
      ? capability.command
      : Array.isArray(capability.command)
        ? capability.command.join(" ")
        : "";
    if (commandValue) {
      const commandRow = document.createElement("div");
      commandRow.className = "command-row";
      const code = document.createElement("code");
      code.tabIndex = 0;
      code.dataset.commandFor = capability.id;
      setText(code, commandValue);
      const copy = document.createElement("button");
      copy.className = "copy-command";
      copy.type = "button";
      copy.dataset.copyCapability = capability.id;
      setText(copy, t("copy"));
      commandRow.append(code, copy);
      card.append(commandRow);
    }

    const actions = document.createElement("div");
    actions.className = "capability-actions";
    const actionStatus = document.createElement("span");
    actionStatus.className = "capability-action-status";
    actionStatus.setAttribute("aria-live", "polite");
    const capabilityError = capabilityErrors.get(capability.id);
    setText(actionStatus, capabilityError || (!isRunnable ? t("viewOnly") : ""));
    actions.append(actionStatus);
    if (isRunnable) {
      const run = document.createElement("button");
      run.className = "capability-run";
      run.id = `run-capability-${capability.id}`;
      run.type = "button";
      run.dataset.runCapability = capability.id;
      run.disabled = capabilityBusyId !== null || !snapshot;
      if (capabilityBusyId === capability.id) {
        run.classList.add("is-loading");
        setText(run, t("runningIndependent"));
      } else {
        setText(run, t("runIndependent"));
      }
      actions.append(run);
    }
    card.append(actions);

    const receipt = capabilityReceipts.get(capability.id);
    if (receipt) {
      card.append(createReceiptPanel(receipt));
    }
    return card;
  }

  function phaseProgressPresentation(step) {
    if (!snapshot) {
      return [t("selectedStage"), "selected"];
    }
    if (step <= snapshot.step) {
      return [t("completedStage"), "complete"];
    }
    if (step === snapshot.step + 1 && !snapshot.done) {
      return [t("currentStage"), "current"];
    }
    return [t("upcomingStage"), "selected"];
  }

  function setCapabilityMessage(message, allowRetry = false) {
    dom.capabilityMessage.hidden = !message;
    setText(dom.capabilityMessage, message || "");
    dom.capabilityRetry.hidden = !allowRetry;
    dom.capabilityRetry.disabled = capabilityCatalogLoading;
  }

  function renderPhaseExplorer() {
    if (!dom.phaseExplorer) {
      return;
    }
    dom.phaseDetailContent.hidden = !phaseExpanded;
    dom.phaseToggle.setAttribute("aria-expanded", String(phaseExpanded));
    setText(dom.phaseToggle.querySelector("span"), phaseExpanded ? t("collapseStage") : t("expandStage"));
    setText(dom.phaseIndex, `PHASE ${String(selectedStage).padStart(2, "0")}`);
    const progressView = phaseProgressPresentation(selectedStage);
    setText(dom.phaseState, progressView[0]);
    dom.phaseState.dataset.tone = progressView[1];
    updateStepRail();

    const phase = phaseForStep(selectedStage);
    if (!phase) {
      setText(dom.phaseTitle, STEP_LABELS[language][selectedStage - 1] || t("capabilitiesLoading"));
      setText(
        dom.phaseSummary,
        capabilityCatalogError
          ? t("capabilitiesUnavailable")
          : capabilityCatalogLoading
            ? t("capabilitiesRetrying")
            : t("capabilitiesLoadingHint"),
      );
      setText(dom.phaseLeaderName, "—");
      setText(dom.phaseLeaderMission, t("leaderLoading"));
      replaceTextList(dom.phasePrerequisites, []);
      replaceTextList(dom.phaseInputs, []);
      replaceTextList(dom.phaseOutputs, []);
      dom.capabilityGrid.replaceChildren();
      setCapabilityMessage(
        capabilityCatalogError ? t("capabilitiesUnavailable") : capabilityCatalogLoading ? t("capabilitiesRetrying") : null,
        Boolean(capabilityCatalogError),
      );
      return;
    }

    setText(dom.phaseTitle, phase.title);
    setText(dom.phaseSummary, phase.summary);
    setText(dom.phaseLeaderName, phase.leader.name);
    setText(dom.phaseLeaderMission, phase.leader.mission);
    replaceTextList(dom.phasePrerequisites, phase.prerequisites);
    replaceTextList(dom.phaseInputs, phase.inputs);
    replaceTextList(dom.phaseOutputs, phase.outputs);
    const byId = new Map(capabilityCatalog.capabilities.map((capability) => [capability.id, capability]));
    const capabilities = phase.capability_ids
      .map((id) => byId.get(id))
      .filter((capability) => capability && capability.phase_id === phase.id);
    dom.capabilityGrid.replaceChildren(...capabilities.map(createAgentNode));
    setCapabilityMessage(capabilities.length ? null : t("noCapabilities"));
  }

  function validateCapabilityReceipt(value, capabilityId) {
    if (
      !isObject(value) ||
      value.schema_version !== 1 ||
      value.capability_id !== capabilityId ||
      typeof value.run_id !== "string" ||
      !["passed", "failed", "blocked"].includes(value.status) ||
      value.scope !== "synthetic-local-demo" ||
      typeof value.started_at !== "string" ||
      !Number.isFinite(value.duration_ms) ||
      !Array.isArray(value.checks) ||
      !Array.isArray(value.setup_commands) ||
      !Array.isArray(value.task_ids)
    ) {
      throw new Error(t("capabilityRunFailed"));
    }
    return value;
  }

  async function loadCapabilities() {
    if (capabilityCatalogLoading) {
      return;
    }
    capabilityCatalogLoading = true;
    capabilityCatalogError = null;
    renderPhaseExplorer();
    try {
      capabilityCatalog = validateCapabilityCatalog(
        await requestSnapshot("/api/capabilities", { method: "GET" }),
      );
    } catch (_error) {
      capabilityCatalog = null;
      capabilityCatalogError = "capabilitiesUnavailable";
    } finally {
      capabilityCatalogLoading = false;
    }
    buildStepRail();
    renderPhaseExplorer();
  }

  function toggleAgent(capabilityId) {
    if (!capabilityCatalog || !capabilityCatalog.capabilities.some((item) => item.id === capabilityId)) {
      return;
    }
    if (expandedAgentIds.has(capabilityId)) {
      expandedAgentIds.delete(capabilityId);
    } else {
      expandedAgentIds.add(capabilityId);
    }
    renderPhaseExplorer();
    const replacement = Array.from(dom.capabilityGrid.querySelectorAll("[data-agent-toggle]")).find(
      (button) => button.dataset.agentToggle === capabilityId,
    );
    focusWithoutScroll(replacement);
  }

  function toggleWorkNode(workKey) {
    if (!workKey) {
      return;
    }
    if (expandedWorkKeys.has(workKey)) {
      expandedWorkKeys.delete(workKey);
    } else {
      expandedWorkKeys.add(workKey);
    }
    renderPhaseExplorer();
    const replacement = Array.from(dom.capabilityGrid.querySelectorAll("[data-work-key]")).find(
      (button) => button.dataset.workKey === workKey,
    );
    focusWithoutScroll(replacement);
  }

  async function runCapability(capabilityId) {
    if (
      capabilityBusyId !== null ||
      !snapshot ||
      !RUNNABLE_CAPABILITY_IDS.has(capabilityId) ||
      !capabilityCatalog
    ) {
      return;
    }
    const capability = capabilityCatalog.capabilities.find((item) => item.id === capabilityId);
    if (!capability || capability.runnable !== true) {
      return;
    }
    const restoreRunFocus = document.activeElement instanceof HTMLElement &&
      document.activeElement.dataset.runCapability === capabilityId;
    capabilityBusyId = capabilityId;
    capabilityErrors.delete(capabilityId);
    capabilityReceipts.delete(capabilityId);
    renderPhaseExplorer();
    try {
      const receipt = await requestSnapshot(
        "/api/capabilities/run",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Qingtian-Demo-Token": snapshot.csrf_token,
          },
          body: JSON.stringify({ capability_id: capabilityId }),
        },
        CAPABILITY_RUN_TIMEOUT_MS,
      );
      capabilityReceipts.set(capabilityId, validateCapabilityReceipt(receipt, capabilityId));
    } catch (error) {
      const code = error && isObject(error.payload) ? error.payload.error : null;
      const message = error && error.name === "AbortError"
        ? t("capabilityTimeout")
        : code === "capability_busy"
          ? t("capabilityBusy")
          : t("capabilityRunFailed");
      capabilityErrors.set(capabilityId, message);
    } finally {
      capabilityBusyId = null;
      const focusWasLost = restoreRunFocus &&
        (document.activeElement === document.body || document.activeElement === document.documentElement);
      renderPhaseExplorer();
      if (focusWasLost) {
        focusWithoutScroll(document.getElementById(`run-capability-${capabilityId}`));
      }
    }
  }

  async function copyCapabilityCommand(button) {
    const card = button.closest(".capability-card");
    const code = card ? card.querySelector("[data-command-for]") : null;
    const command = code ? code.textContent : "";
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(command);
      setText(button, t("copied"));
    } catch (_error) {
      setText(button, t("copyFailed"));
      if (code) {
        code.focus();
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(code);
        selection.removeAllRanges();
        selection.addRange(range);
      }
    }
  }

  function setConnection(kind) {
    dom.connectionPill.dataset.state = kind;
    setText(
      dom.connectionLabel,
      kind === "connected" ? t("connected") : kind === "error" ? t("disconnected") : t("connecting"),
    );
  }

  function showError(message, resetRequired = false) {
    setText(dom.errorMessage, message || t("requestFailed"));
    dom.errorBanner.hidden = false;
    dom.retryButton.hidden = resetRequired;
  }

  function clearError() {
    dom.errorBanner.hidden = true;
    dom.retryButton.hidden = false;
    setText(dom.errorMessage, "");
  }

  function eventTone(state) {
    const normalized = String(state || "").toUpperCase();
    if (["DONE", "COMPLETED", "VERIFIED", "ACCEPTED", "RECORDED"].includes(normalized)) {
      return "complete";
    }
    if (["RUNNING", "ACTIVE", "PENDING", "STARTED", "REVIEW_PENDING"].includes(normalized)) {
      return "active";
    }
    if (normalized === "UNKNOWN") {
      return "unknown";
    }
    if (normalized === "ERROR") {
      return "error";
    }
    return "neutral";
  }

  function renderRoles() {
    const eventRoles = new Set(
      snapshot.events
        .filter((event) => isObject(event) && typeof event.role === "string")
        .map((event) => event.role),
    );
    dom.crewCards.forEach((card) => {
      const roleId = card.dataset.role;
      const view = rolePresentation(roleId);
      const title = card.querySelector("[data-role-title]");
      const name = card.querySelector("[data-role-name]");
      const status = card.querySelector("[data-role-status]");
      setText(title, view.title);
      setText(name, view.name);
      card.setAttribute("aria-label", `${view.title}: ${view.description}`);
      if (roleId === snapshot.active_role && eventRoles.has(roleId)) {
        card.dataset.state = "active";
        setText(status, t("active"));
      } else if (roleId === snapshot.guide.role && !snapshot.done) {
        card.dataset.state = "next";
        setText(status, t("nextRoleStatus"));
      } else if (eventRoles.has(roleId)) {
        card.dataset.state = "reported";
        setText(status, t("receiptSeen"));
      } else {
        card.dataset.state = "waiting";
        setText(status, t("waiting"));
      }
    });
    const inspectorRole = selectedRole || snapshot.active_role || ROLE_IDS[0];
    renderRoleInspector(inspectorRole);
  }

  function renderRoleInspector(roleId) {
    if (!ROLE_IDS.includes(roleId)) {
      return;
    }
    selectedRole = roleId;
    const view = rolePresentation(roleId);
    setText(dom.roleInspectorName, `${view.title} · ${view.name}`);
    setText(dom.roleInspectorDescription, view.description);
    setText(dom.roleInspectorIcon, view.monogram);
  }

  function createEventItem(event, isNew) {
    const item = document.createElement("li");
    item.className = isNew ? "event-item is-new" : "event-item";

    const avatar = document.createElement("span");
    avatar.className = "event-avatar";
    const roleId = typeof event.role === "string" ? event.role : "?";
    setText(avatar, rolePresentation(roleId).monogram);
    avatar.setAttribute("aria-hidden", "true");

    const copy = document.createElement("div");
    copy.className = "event-copy";
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    const identifier = document.createElement("small");
    setText(title, typeof event.title === "string" ? event.title : t("record"));
    setText(detail, typeof event.detail === "string" ? event.detail : "—");
    setText(identifier, typeof event.id === "string" ? event.id : "—");
    copy.append(title, detail, identifier);

    const state = document.createElement("span");
    state.className = "event-state";
    state.dataset.tone = eventTone(event.state);
    setText(state, typeof event.state === "string" ? event.state : "—");

    item.append(avatar, copy, state);
    return item;
  }

  function renderEvents(newEventIds) {
    const events = snapshot.events.filter(isObject);
    const items = events.map((event) => {
      const eventId = typeof event.id === "string" ? event.id : "";
      return createEventItem(event, Boolean(eventId && newEventIds.has(eventId)));
    });
    dom.eventList.replaceChildren(...items);
    dom.eventEmpty.hidden = events.length > 0;
    setText(dom.eventCount, events.length);
    renderedEventIds = new Set(
      events.map((event) => event.id).filter((value) => typeof value === "string" && value),
    );
  }

  function displayValue(value) {
    if (value === null) {
      return "null";
    }
    if (typeof value === "string") {
      return value;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
    try {
      return JSON.stringify(value);
    } catch (_error) {
      return "[unavailable]";
    }
  }

  function createRecordEntry(record, index) {
    const wrapper = document.createElement("article");
    wrapper.className = "record-entry";
    const heading = document.createElement("h3");
    heading.className = "record-entry-title";

    const recordObject = isObject(record) ? record : { value: record };
    const identityKeys = ["id", "task_id", "session_id", "run_id", "evidence_id", "checkpoint_id", "knowledge_id", "title"];
    const identity = identityKeys
      .map((key) => recordObject[key])
      .find((value) => typeof value === "string" && value.trim());
    setText(heading, identity || `${t("record")} ${index + 1}`);

    const fields = document.createElement("dl");
    fields.className = "record-fields";
    Object.keys(recordObject).sort().forEach((key) => {
      const term = document.createElement("dt");
      const value = document.createElement("dd");
      setText(term, key);
      if (/(?:csrf|password|secret|token)/i.test(key)) {
        setText(value, t("redacted"));
      } else {
        setText(value, displayValue(recordObject[key]));
      }
      if (/(?:sha|hash|digest)/i.test(key)) {
        value.classList.add("hash-value");
      }
      fields.append(term, value);
    });

    wrapper.append(heading, fields);
    return wrapper;
  }

  function renderRecords() {
    dom.recordGroups.forEach((group) => {
      const key = group.dataset.recordKey;
      const source = key === "task"
        ? (isObject(snapshot.task) ? [snapshot.task] : [])
        : snapshot.records[key];
      const records = source.filter((value) => value !== undefined);
      const count = group.querySelector("[data-record-count]");
      const list = group.querySelector("[data-record-list]");
      setText(count, records.length);
      list.replaceChildren(...records.map(createRecordEntry));
    });
  }

  function pulseReceipt(event) {
    if (!isObject(event) || typeof event.role !== "string" || !ROLE_IDS.includes(event.role)) {
      return;
    }
    window.clearTimeout(pulseTimer);
    dom.crewStage.dataset.pulseRole = event.role;
    const card = dom.crewCards.find((candidate) => candidate.dataset.role === event.role);
    if (card) {
      card.classList.remove("is-receipt-new");
      requestAnimationFrame(() => card.classList.add("is-receipt-new"));
    }
    pulseTimer = window.setTimeout(() => {
      delete dom.crewStage.dataset.pulseRole;
      if (card) {
        card.classList.remove("is-receipt-new");
      }
    }, 1350);
  }

  function render(nextSnapshot, options = {}) {
    const previousState = snapshot ? taskField(snapshot.task, ["state", "status"], "—") : null;
    snapshot = validateSnapshot(nextSnapshot);
    const currentState = taskField(snapshot.task, ["state", "status"], "—");
    const taskTitle = taskField(snapshot.task, ["title", "name"], "Qingtian Demo");
    const taskObjective = taskField(snapshot.task, ["objective", "description"], t("loadingTask"));
    const total = snapshot.total_steps;
    const step = snapshot.step;

    setText(dom.missionTitle, taskTitle);
    setText(dom.missionObjective, taskObjective);
    setText(dom.taskState, currentState);
    setText(dom.coreState, currentState);
    setText(dom.stepValue, step);
    setText(dom.stepTotal, total);
    dom.progressTrack.setAttribute("aria-valuemax", String(total));
    dom.progressTrack.setAttribute("aria-valuenow", String(step));
    dom.progressTrack.setAttribute("aria-valuetext", `${step} / ${total}`);
    dom.progressTrack.dataset.step = String(Math.max(0, Math.min(7, step)));

    if (previousState && previousState !== currentState) {
      dom.taskState.classList.remove("state-flip");
      requestAnimationFrame(() => dom.taskState.classList.add("state-flip"));
    }

    setText(dom.guideStepNumber, String(Math.min(step + 1, total)).padStart(2, "0"));
    setText(dom.guideTitle, typeof snapshot.guide.title === "string" ? snapshot.guide.title : t("waitingGuide"));
    setText(
      dom.guideDescription,
      typeof snapshot.guide.description === "string" ? snapshot.guide.description : t("waitingGuideDescription"),
    );
    const nextRole = typeof snapshot.guide.role === "string" ? snapshot.guide.role : null;
    setText(dom.activeRoleLabel, rolePresentation(nextRole).title);
    setText(
      dom.nextLabel,
      typeof snapshot.guide.next_label === "string" && snapshot.guide.next_label.trim()
        ? snapshot.guide.next_label
        : t("nextStep"),
    );

    updateStepRail();
    renderPhaseExplorer();
    renderRoles();
    renderEvents(options.newEventIds || new Set());
    renderRecords();
    dom.doneBanner.hidden = !snapshot.done;
    setBusy(false);
    setConnection("connected");
    clearError();
    if (snapshot.requires_reset) {
      showError(t("resetRequired"), true);
    }
    setText(dom.lastUpdated, `${t("lastUpdated")} · ${new Date().toLocaleTimeString(language === "zh" ? "zh-CN" : "en", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`);

    const newEvents = snapshot.events.filter((event) => isObject(event) && options.newEventIds && options.newEventIds.has(event.id));
    if (newEvents.length) {
      pulseReceipt(newEvents[newEvents.length - 1]);
    }
  }

  function setBusy(value, action = "step") {
    busy = value;
    const unavailable = !snapshot || busy;
    dom.nextStep.disabled = unavailable || Boolean(snapshot && (snapshot.done || snapshot.requires_reset));
    dom.resetButton.disabled = unavailable;
    dom.nextStep.classList.toggle("is-loading", busy && action === "step");
    if (busy && action === "step") {
      setText(dom.nextLabel, t("loadingNext"));
    }
    if (busy && action === "reset") {
      setText(dom.resetLabel, t("resetting"));
    }
  }

  async function readJsonResponse(response) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      throw new Error(`${t("requestFailed")} (HTTP ${response.status})`);
    }
    if (!response.ok) {
      const serverMessage = isObject(payload) && typeof payload.error === "string"
        ? payload.error
        : isObject(payload) && typeof payload.message === "string"
          ? payload.message
          : `${t("requestFailed")} (HTTP ${response.status})`;
      const error = new Error(serverMessage);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  async function requestSnapshot(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        ...options,
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      return await readJsonResponse(response);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function loadState(options = {}) {
    setConnection("loading");
    dom.nextStep.disabled = true;
    dom.resetButton.disabled = true;
    try {
      const value = await requestSnapshot("/api/state", { method: "GET" });
      const known = options.initial ? new Set() : new Set(renderedEventIds);
      const newIds = new Set(
        value.events
          .filter((event) => isObject(event) && typeof event.id === "string" && !known.has(event.id))
          .map((event) => event.id),
      );
      render(value, { newEventIds: options.initial ? new Set() : newIds });
      if (capabilityCatalogError && !capabilityCatalogLoading) {
        loadCapabilities();
      }
      if (options.stale) {
        showError(t("staleReloaded"));
      }
      return true;
    } catch (error) {
      setConnection("error");
      setBusy(false);
      dom.nextStep.disabled = true;
      showError(error instanceof Error ? error.message : t("requestFailed"));
      return false;
    }
  }

  async function advanceStep() {
    if (!snapshot || busy || snapshot.done) {
      return;
    }
    disarmReset();
    clearError();
    setBusy(true, "step");
    const expectedStep = snapshot.step;
    const token = snapshot.csrf_token;
    const previousIds = new Set(renderedEventIds);
    try {
      const value = await requestSnapshot("/api/step", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Qingtian-Demo-Token": token,
        },
        body: JSON.stringify({ expected_step: expectedStep }),
      });
      const newIds = new Set(
        value.events
          .filter((event) => isObject(event) && typeof event.id === "string" && !previousIds.has(event.id))
          .map((event) => event.id),
      );
      render(value, { newEventIds: newIds });
    } catch (error) {
      const failure = error && isObject(error.payload) ? error.payload : null;
      if (failure && failure.requires_reset === true && isObject(failure.state)) {
        const newIds = new Set(
          failure.state.events
            .filter((event) => isObject(event) && typeof event.id === "string" && !previousIds.has(event.id))
            .map((event) => event.id),
        );
        render(failure.state, { newEventIds: newIds });
        showError(t("resetRequired"), true);
        return;
      }
      if (error && error.status === 409) {
        await loadState({ stale: true });
        return;
      }
      setBusy(false);
      setConnection("error");
      dom.nextStep.disabled = true;
      showError(error instanceof Error ? error.message : t("stepFailed"));
    }
  }

  function armReset() {
    resetArmed = true;
    dom.resetButton.classList.add("is-confirming");
    setText(dom.resetLabel, t("resetConfirm"));
    setText(dom.resetHint, t("resetArmedHint"));
    window.clearTimeout(resetTimer);
    resetTimer = window.setTimeout(disarmReset, RESET_CONFIRM_MS);
  }

  function disarmReset() {
    resetArmed = false;
    window.clearTimeout(resetTimer);
    dom.resetButton.classList.remove("is-confirming");
    setText(dom.resetLabel, t("reset"));
    setText(dom.resetHint, t("resetHint"));
  }

  async function resetDemo() {
    if (!snapshot || busy) {
      return;
    }
    if (!resetArmed) {
      armReset();
      return;
    }
    const token = snapshot.csrf_token;
    disarmReset();
    clearError();
    setBusy(true, "reset");
    try {
      const value = await requestSnapshot("/api/reset", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Qingtian-Demo-Token": token,
        },
        body: "{}",
      });
      renderedEventIds = new Set();
      selectedRole = null;
      render(value, { newEventIds: new Set() });
    } catch (error) {
      setBusy(false);
      setConnection("error");
      showError(error instanceof Error ? error.message : t("resetFailed"));
    }
  }

  function toggleLanguage() {
    language = language === "zh" ? "en" : "zh";
    applyStaticLanguage();
    if (snapshot) {
      render(snapshot, { newEventIds: new Set() });
    } else {
      setConnection("loading");
    }
  }

  function bindEvents() {
    dom.languageToggle.addEventListener("click", toggleLanguage);
    dom.nextStep.addEventListener("click", advanceStep);
    dom.retryButton.addEventListener("click", () => loadState());
    dom.resetButton.addEventListener("click", resetDemo);
    dom.mindRootToggle.addEventListener("click", toggleMindTree);
    dom.phaseToggle.addEventListener("click", togglePhaseDetails);
    dom.capabilityRetry.addEventListener("click", loadCapabilities);
    dom.stepRail.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target ? target.closest(".step-button[data-phase-step]") : null;
      if (button) {
        selectStage(Number(button.dataset.phaseStep));
      }
    });
    dom.capabilityGrid.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target : null;
      const agentButton = target ? target.closest("[data-agent-toggle]") : null;
      if (agentButton) {
        toggleAgent(agentButton.dataset.agentToggle);
        return;
      }
      const workButton = target ? target.closest("[data-work-toggle]") : null;
      if (workButton) {
        toggleWorkNode(workButton.dataset.workKey);
        return;
      }
      const runButton = target ? target.closest("[data-run-capability]") : null;
      if (runButton) {
        runCapability(runButton.dataset.runCapability);
        return;
      }
      const copyButton = target ? target.closest("[data-copy-capability]") : null;
      if (copyButton) {
        copyCapabilityCommand(copyButton);
      }
    });
    dom.crewCards.forEach((card, index) => {
      card.addEventListener("click", () => renderRoleInspector(card.dataset.role));
      card.addEventListener("keydown", (event) => {
        if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"].includes(event.key)) {
          return;
        }
        event.preventDefault();
        const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
        const nextIndex = (index + direction + dom.crewCards.length) % dom.crewCards.length;
        dom.crewCards[nextIndex].focus();
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && resetArmed) {
        disarmReset();
        dom.resetButton.focus();
      }
    });
  }

  applyStaticLanguage();
  bindEvents();
  loadState({ initial: true });
  loadCapabilities();
})();
