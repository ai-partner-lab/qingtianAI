import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

const root = path.resolve(import.meta.dirname, "../..");
const source = fs.readFileSync(path.join(root, "qingtian_engine/static/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "qingtian_engine/static/styles.css"), "utf8");
const start = source.indexOf("/* lifecycle-ui:test:start */");
const end = source.indexOf("function setConnection", start);
assert(start >= 0 && end > start, "lifecycle UI test seam must remain available");

class Node {
  constructor(tag, className = "", text = undefined) {
    this.tag = tag;
    this.className = className;
    this.children = [];
    this.textContent = text === undefined ? "" : String(text);
  }
  append(...children) { this.children.push(...children); }
}

const sandbox = {Date, console, Node};
vm.createContext(sandbox);
vm.runInContext(`function el(tag, className, text) { return new Node(tag, className, text); }\n${source.slice(start, end)}\nglobalThis.ui={lifecycleView,lifecyclePanel};`, sandbox);
const {lifecycleView, lifecyclePanel} = sandbox.ui;
const NOW = Date.parse("2026-09-13T12:00:00Z");

function task(status = "idle", extra = {}) {
  return {
    id: "t-1",
    requires_deploy: false,
    evidence: [],
    lifecycle: {
      revision: 4,
      stage: "handoff",
      status,
      next_action: {owner_kind: "manager", owner: "release-manager", text: "核对封存包", due: "2026-09-13T13:00:00Z"},
      external_executions: [], handoffs: [], outbox: [], completion_blockers: [],
      notification_bridge: {mode: "manual", native_delivery: false},
      ...extra,
    },
  };
}

function textOf(node) {
  return [node.textContent, ...node.children.flatMap((child) => textOf(child))].join(" ");
}

test("old payload without lifecycle stays compatible", () => {
  assert.equal(lifecycleView({id: "legacy"}).available, false);
  assert.equal(lifecyclePanel({id: "legacy"}), null);
});

test("idle lifecycle shows the actual next owner and action", () => {
  const view = lifecycleView(task(), NOW);
  assert.equal(view.headline, "生命周期待推进");
  assert.equal(view.nextOwner, "release-manager");
  assert.equal(view.nextText, "核对封存包");
});

test("offered handoff names the recipient instead of claiming execution", () => {
  const view = lifecycleView(task("awaiting_acceptance", {handoffs: [{id: "h1", status: "offered", recipient: "publisher", deadline: "2026-09-13T13:00:00Z"}]}), NOW);
  assert.equal(view.headline, "待接单 · publisher");
  assert(!view.headline.includes("执行中"));
});

test("expired offered handoff is visibly overdue", () => {
  const view = lifecycleView(task("awaiting_acceptance", {handoffs: [{id: "h1", status: "offered", recipient: "publisher", deadline: "2026-09-13T11:00:00Z"}]}), NOW);
  assert.equal(view.overdue, true);
  assert.equal(view.headline, "交接超时 · publisher");
});

test("accepted handoff remains distinct from started or done", () => {
  const view = lifecycleView(task("accepted", {handoffs: [{id: "h1", status: "accepted", recipient: "publisher"}]}), NOW);
  assert.equal(view.headline, "已接单 · 未登记关联执行 · publisher");
  assert.match(view.delivery, /不等于已开始或完成/);
});

test("unrelated execution activity does not prove an accepted handoff started", () => {
  const view = lifecycleView(task("accepted", {
    handoffs: [{id: "h1", status: "accepted", recipient: "publisher"}],
    external_executions: [{id: "x1", executor: "publisher", status: "active", last_activity_at: "2026-09-13T11:58:00Z"}],
  }), NOW);
  assert.equal(view.headline, "已接单 · 未登记关联执行 · publisher");
});

test("only structured handoff linkage proves accepted execution started", () => {
  const view = lifecycleView(task("accepted", {
    handoffs: [{id: "h1", status: "accepted", recipient: "publisher", execution_started: true, execution_ids: ["x1"]}],
    external_executions: [{id: "x1", executor: "publisher", status: "active", last_activity_at: "2026-09-13T11:58:00Z"}],
  }), NOW);
  assert.equal(view.headline, "已接单 · 已登记关联执行 · publisher");
});

test("accepted but not started handoff shows its own overdue warning", () => {
  const view = lifecycleView(task("accepted", {handoffs: [{id: "h1", status: "accepted", recipient: "publisher", start_overdue: true, overdue: true}]}), NOW);
  assert.equal(view.startOverdue, true);
  assert.equal(view.headline, "已接单 · 启动逾期 · publisher");
});

test("rejection exposes reason and every missing item", () => {
  const view = lifecycleView(task("rejected", {handoffs: [{id: "h1", status: "rejected", recipient: "publisher", rejection_reason: "包不完整", missing_items: ["commit", "smoke"]}]}), NOW);
  assert.equal(view.rejectionReason, "包不完整");
  assert.deepEqual([...view.missingItems], ["commit", "smoke"]);
});

test("active external execution uses executor activity, not task state", () => {
  const view = lifecycleView(task("execution_active", {external_executions: [{id: "x1", executor: "worker-a", status: "running", last_activity_at: "2026-09-13T11:59:00Z"}]}), NOW);
  assert.equal(view.headline, "外部执行中 · worker-a");
  assert.equal(view.activityAt, "2026-09-13T11:59:00Z");
});

test("detail history keeps every executor and actual model visible", () => {
  const sample = task("execution_active", {external_executions: [
    {id: "x1", executor: "worker-sol", model: "gpt-5.6-sol", reasoning: "high", speed: "standard", status: "finished", last_activity_at: "2026-09-13T11:30:00Z"},
    {id: "x2", executor: "worker-astra", model: "gpt-6-astra", reasoning: "ultra", speed: "fast", status: "active", last_activity_at: "2026-09-13T11:59:00Z"},
  ]});
  const rendered = textOf(lifecyclePanel(sample));
  assert.match(rendered, /worker-sol.*gpt-5\.6-sol.*high/s);
  assert.match(rendered, /worker-astra.*gpt-6-astra.*ultra/s);
  assert.match(rendered, /历史不等于当前活动/);
});

test("lost execution is explicit and does not say merely waiting", () => {
  const view = lifecycleView(task("execution_lost", {external_executions: [{id: "x1", executor: "worker-a", status: "lost", last_activity_at: "2026-09-13T10:00:00Z"}]}), NOW);
  assert.equal(view.headline, "外部执行失联 · worker-a");
});

test("lost headline identifies the lost owner among newer healthy executors", () => {
  const view = lifecycleView(task("execution_lost", {
    next_action: {owner_kind: "agent", owner: "worker-lost", text: "核对失联执行"},
    external_executions: [
      {id: "x1", executor: "worker-lost", status: "active", display_status: "lost", last_activity_at: "2026-09-13T11:40:00Z"},
      {id: "x2", executor: "worker-healthy", status: "active", display_status: "active", last_activity_at: "2026-09-13T11:59:00Z"},
    ],
  }), NOW);
  assert.equal(view.headline, "外部执行失联 · worker-lost");
  assert.equal(view.activityText, "worker-lost · lost");
});

test("pending outbox is pending notification, not sent", () => {
  const view = lifecycleView(task("awaiting_acceptance", {handoffs: [{id: "h1", status: "offered", recipient: "publisher"}], outbox: [{id: "o1", handoff_id: "h1", status: "pending"}]}), NOW);
  assert.match(view.delivery, /^待通知/);
});

test("claimed outbox is still not delivered", () => {
  const view = lifecycleView(task("awaiting_acceptance", {handoffs: [{id: "h1", status: "offered"}], outbox: [{id: "o1", handoff_id: "h1", status: "claimed"}]}), NOW);
  assert.match(view.delivery, /尚未送达/);
});

test("manual bridge delivered row is only a recorded transport receipt", () => {
  const view = lifecycleView(task("awaiting_acceptance", {handoffs: [{id: "h1", status: "offered"}], outbox: [{id: "o1", handoff_id: "h1", status: "delivered"}]}), NOW);
  assert.match(view.delivery, /已记录 transport 回执/);
  assert.match(view.delivery, /不能证明实际送达或接单/);
});

test("native bridge delivery still does not imply acceptance", () => {
  const view = lifecycleView(task("awaiting_acceptance", {notification_bridge: {mode: "native", native_delivery: true}, handoffs: [{id: "h1", status: "offered"}], outbox: [{id: "o1", handoff_id: "h1", status: "delivered"}]}), NOW);
  assert.match(view.delivery, /已确认送达/);
  assert.match(view.delivery, /不等于已接单/);
});

test("execution activity and evidence update clocks stay separate", () => {
  const sample = task("execution_active", {external_executions: [{executor: "worker-a", status: "running", last_activity_at: "2026-09-13T11:59:00Z"}]});
  sample.evidence = [{kind: "test", created_at: "2026-09-13T11:30:00Z"}];
  const view = lifecycleView(sample, NOW);
  assert.equal(view.activityAt, "2026-09-13T11:59:00Z");
  assert.equal(view.evidenceAt, "2026-09-13T11:30:00Z");
  assert.notEqual(view.activityAt, view.evidenceAt);
});

test("untrusted lifecycle text is inserted as text, never parsed markup", () => {
  const sample = task("rejected", {handoffs: [{id: "h1", status: "rejected", recipient: "<img src=x onerror=alert(1)>", rejection_reason: "<script>bad()</script>", missing_items: ["<b>commit</b>"]}]});
  const panel = lifecyclePanel(sample);
  const rendered = textOf(panel);
  assert.match(rendered, /<script>bad\(\)<\/script>/);
  assert.equal(panel.children.some((child) => child.tag === "script"), false);
});

test("release task explicitly requires deploy and smoke", () => {
  const sample = task();
  sample.requires_deploy = true;
  assert.match(textOf(lifecyclePanel(sample)), /verified deploy \+ smoke/);
});

test("small screen lifecycle layout stacks the header", () => {
  assert.match(css, /@media \(max-width: 520px\)/);
  assert.match(css, /\.lifecycle-head \{ display: grid;/);
});

test("empty migrated lifecycle stays quiet on compact task cards", () => {
  const migrated = task("idle", {revision: 0, stage: "unassigned", next_action: {}});
  assert.equal(lifecyclePanel(migrated, true), null);
  assert.notEqual(lifecyclePanel(migrated, false), null);
});

test("stage and owner kind use readable Chinese labels", () => {
  const view = lifecycleView(task("idle", {stage: "qa", next_action: {owner_kind: "agent", owner: "qa-a", text: "复验"}}), NOW);
  assert.equal(view.stage, "质量验收");
  assert.equal(view.nextOwnerKindLabel, "执行者");
});

test("native engine state is labelled separately from external lifecycle activity", () => {
  assert.match(source, /querySelector\("\.state-label"\)\.textContent = "引擎："/);
  assert.match(source, /\["受管 Run",/);
  assert.match(source, /\["受管执行槽",/);
  assert.match(source, /生命周期摘要：已登记外部执行/);
  assert.match(textOf(lifecyclePanel(task("execution_active", {external_executions: [{executor: "worker", status: "active", last_activity_at: "2026-09-13T11:59:00Z"}]}))), /外部执行中/);
});
