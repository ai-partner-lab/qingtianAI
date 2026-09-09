# Qingtian Engine 本地 HTTP 合约

适用于 `qingtian_engine/server.py` 的真实控制台，**不是离线演示 API**。以下域名/任务内容均为通用例子。建议测试时使用独立端口和全新数据目录；不要对正在工作的实例执行示例写请求。

## 传输与权限

- 默认 HTTP loopback `127.0.0.1:8766`；Host 必须是 loopback 主机且匹配实际端口。
- POST 带 Origin 时必须与 Host 同源；无 Origin 的本机客户端允许调用。这不是认证或 RBAC。
- JSON 写接口使用 `Content-Type: application/json`。JSON 正文上限 64 KiB，超限返回 413；拒绝 Transfer-Encoding、重复/非法 Content-Length 和截断正文。
- multipart intake 请求总读取边界 102 MiB；附件业务上限另为总计 100 MiB、单个 25 MiB、最多 10 个。
- 返回中有私有工作区路径、任务内容和证据；仅交给对应项目授权成员。不要公开原始响应。
- 不支持远程跨域调用、API Token 认证、任意 HTTP 证据写入或通用 OpenAPI 自动发现。

## 读取接口

| 方法/路径 | 响应与语义 |
|---|---|
| `GET /api/health` | HTTP 200 或 watchdog 不健康时 503；`ok,service,pid,mode,automatic_dispatch,read_only,data_dir,workspace,watchdog`。manual 的 read_only 仍是 false。 |
| `GET /api/dashboard` | 版本化看板；有任务、状态汇总、运行/等待投影等。启用协调器时附加 `qingtian_v2`。 |
| `GET /api/tasks/{task_id}` | task 字段及 `dependencies,evidence,events,runs`；events 最多最近 100 条，event 的 `payload_json` 是 JSON 字符串。不存在返回 404。 |
| `GET /api/intakes?limit=30` | `{"intakes":[...]}`；建议使用正整数 limit。 |
| `GET /api/intakes/{intake_id}` | intake 详情，含 `draft,attachments,messages,tasks,reused`；不暴露内部 execution_prompt/advanced/附件 local_path。 |
| `GET /api/intakes/{intake_id}/attachments/{attachment_id}` | 经归属/路径/hash 检查后的附件字节；图片 inline，其余 attachment。不是外链代理。 |
| `GET /api/report` | 滚动 24 小时报告，含结构化分组/任务与 Markdown。不是“自然日零点到零点”查询。 |
| `GET /api/v2/orchestrator` | 启用时返回 `enabled,status,holder_id,fencing_token,updated_at,summary,evidence,dead_letters,plugins`；关闭时 404、`{"enabled":false}`。 |
| `GET /api/events/stream` | `text/event-stream`，先 snapshot，再 dashboard/heartbeat；详见下节。 |

任务对象稳定关注字段：`id,title,scope_summary,priority,environment,repository,base_branch,branch,worktree,worker_type,owner_session,state,progress,blocking_reason,evidence_profile,execution_mode,requires_deploy,created_at,updated_at`。额外字段由当前实现返回，客户端应保留前向兼容，不把角色标签误认为已经连接的会话。

`requires_deploy` 输入必须是 JSON boolean；字符串 `"false"`、数字和 null 会拒绝。明确为 true 时，所有证据 profile 都额外要求 verified deploy 与 smoke；为 false 时，“未部署”等旁证不会扩大任务要求。

run 字段包括 `id,task_id,attempt,adapter,pid,process_group,session_id,status,exit_code,result_hash,retry_of,failure_kind,failure_stage,failure_type,started_at,finished_at`。`run.status=DONE` 不等于 `task.state=DONE`。

## 新建任务：默认不执行

`POST /api/tasks` 接收以下字段，成功 HTTP 201 返回 task 对象：

```json
{
  "title": "Review the sample parser",
  "scope_summary": "Inspect only; do not change files or deploy.",
  "priority": 2,
  "environment": "local",
  "repository": "",
  "base_branch": "",
  "worker_type": "auto",
  "requires_deploy": false,
  "auto_start": false
}
```

字段默认值如上；title 应非空。`auto_start` 必须是 JSON boolean。仅当它为 true 且提供非空 `instruction` 时，此入口才调用 dispatch。创建成功本身不是派发回执。repository 可指定已注册项目名或精确仓库路径；base_branch 必须与注册配置及可用 Git 基线一致。多项目角色匹配不唯一时需要显式选择，不能猜测。

**此 HTTP 入口当前不转发 `Idempotency-Key` 或 body 的 `idempotency_key`。** 不要对超时的创建请求盲目重放；需要可重放收件流程时使用 intake 接口。

## 明确派发与任务动作

`POST /api/tasks/{task_id}/dispatch`：

```json
{
  "instruction": "Inspect the sample parser and report findings. Do not edit files.",
  "resume": false
}
```

instruction 必须非空字符串，resume 必须 boolean。成功 HTTP 200：`{"task":{...},"run":{...}}`。请求可能实际启动 Codex，因此应在明确授权后调用。resume 需要可用的既有 Codex session，不是任意旧会话 ID 的导入接口。

其他 POST 动作接收空对象，heartbeat 除外：

| 路径后缀 | 作用 | 响应/约束 |
|---|---|---|
| `/cancel` | 取消当前任务及其受管进程 | 当前 manager 结果对象；不是删除任务历史。 |
| `/plan` | 明确标记为 PLANNED | 更新后的 task；可能影响 auto 的后续候选，勿当纯展示动作。 |
| `/retry` | 通过最近可用 session ID 明确恢复 | 更新后的 task；无 session 时 400。可能启动真实执行。 |
| `/complete-human-action` | 标记人工动作已处理 | 更新后的 task；不替代最终证据验收。 |
| `/remind-external` | 写入外部提醒记录 | 更新后的 task；不代表发送了外部消息。 |
| `/heartbeat` | 登记外部/委派执行心跳 | JSON `{"mode":"external"}` 或 `{"mode":"delegated"}`；不是启动器。 |

HTTP 没有任意状态 transition、dependency 和 evidence 写接口；这类管理能力由 CLI/Python 服务层提供。CLI 的 `task evidence ... --verified` 是受信操作方的验证声明，不是自动检测结果。

`analysis-only`、`reference-only` 任务及导入参考记录不能通过 dispatch/retry 变成实施任务；切换 auto 或修改显示状态也不产生执行授权。需要实施时另建明确授权的新任务。`authorization_policy` 是任务持久字段，不从普通任务创建 HTTP 正文接收任意覆盖。

## 收件、规划和附件

`POST /api/intakes` 仅接受 multipart/form-data：

| 字段 | 类型与含义 |
|---|---|
| `text` | 字符串，最多 20,000 字符；无附件时必须非空。 |
| `intent` | `analyze`、`implement`、`implement_and_deploy_dev`。manual 缺省 analyze，auto 缺省 implement；客户端应始终显式传入。 |
| `idempotency_key` | 同一逻辑请求稳定的非敏感键；也可用 `Idempotency-Key` 请求头，表单字段优先。 |
| `advanced` | JSON 字符串，允许 title/scope_summary/priority/environment/repository/base_branch/worker_type/requires_deploy。 |
| 文件字段 | multipart 的带 filename 项视为附件；建议重复使用 `files` 字段名。 |

安全的“只规划”示例，端口假设已启动新的测试实例：

```sh
curl --fail-with-body http://127.0.0.1:8766/api/intakes \
  -F 'text=Review the sample documentation; do not execute or modify files.' \
  -F 'intent=analyze' \
  -F 'idempotency_key=sample-review-round-1' \
  -F 'advanced={"environment":"local"}'
```

首次成功 201；幂等重用 200 且 `reused=true`。返回详情包含 receipt 消息、规划草案和关联 task IDs。`NEEDS_INPUT`/`FAILED` 可能作为 intake 状态出现在 JSON 中，不能只以 HTTP 2xx 判断分发成功。显式 implement 意图可以在 manual 下启动执行；manual 只禁止后台自行领取。

analyze 意图会写入每个关联任务的 `analysis-only` 策略，不依赖标题中是否出现“只分析”等文字。原 analyze 请求的 retry 只重新规划，不派发 Worker。

无显式键时回退键由文本、意图和 advanced 得出，不包含附件字节。因此调用方应为每个逻辑收件请求生成稳定唯一键，尤其不要用相同文字搭配不同附件却复用同一键。

`POST /api/intakes/{intake_id}/retry` 重新尝试规划/分发；它继承原 intent，若原请求是实施就可能再次执行。不是只读刷新。

## SSE 版本合同

连接可用 `?lastEventId=123`，或 `Last-Event-ID: 123`；query 优先。游标须为整数，错误返回 400。连接起始提供 `retry: 3000`。首个 `snapshot` 携带 `version,changes,dashboard,reset`；`reset=true` 表示当前版本低于客户端旧游标，需要重置本地投影。

```text
id: 123
event: dashboard
data: {"version":123,"changes":[],"dashboard":{}}
```

上述仅展示事件帧结构，空 dashboard 不是完整实例响应。后续 dashboard 事件带当前全量快照及最多 100 条变化摘要；heartbeat 约每 15 秒一次，只有 version。服务约每 0.75 秒查看更新。客户端按 version 去重，重连时重新应用 snapshot；不要把 changes 当作无限保留、完整重放的审计流。

## 错误与测试边界

常见结构为 `{"error":"..."}`；非法正文/动作通常 400，Origin/Host 不符 403，资源不存在 404，附件过大 413。不存在的静态路由可能为 HTML 404；不要宣称所有错误都符合统一 JSON 错误 schema。

建议黑盒用新建样例任务验证：创建不执行、明确派发、重复 intake、附件边界与 hash、manual 重启不自动重派、SSE 重连、缺证据停留 VERIFYING、Provider 不可用事件。真实模型/测试/视觉/部署验收另需接入方的独立环境与产物，详见 [能力清单](ENGINE-CAPABILITIES.md)。

## 项目注册合同（CLI，不是 HTTP）

项目注册表默认位于引擎数据目录的 `config/projects.local.json`；可用 `QINGTIAN_PROJECTS_CONFIG` 指定绝对配置路径。引擎数据目录可由 `--data-dir` 或 `QINGTIAN_ENGINE_HOME` 指定，默认位于用户数据目录，不写进安装包。

```sh
qingtian project register sample-api --repo /path/to/sample-api --base dev --role backend --role qa
qingtian project list
```

注册只登记显式授权的现有 Git 仓库；示例 dev 分支必须真实存在，不能替换成受保护基线。配置结构：

```json
{
  "schema_version": 1,
  "projects": {
    "sample-api": {
      "repository": "/path/to/sample-api",
      "base_branch": "dev",
      "roles": ["backend", "qa"],
      "scope": "src"
    }
  }
}
```

scope 可省略；提供时必须是仓库内真实存在的相对目录。Worker 在独立工作树内重新校验该目录，并作为 Codex cwd；不存在或越界会拒绝。cwd 不是独立权限沙箱。注册表未知字段、重复键、非法路径/角色/基线会拒绝。`project remove` 仅删除注册项，不删除仓库文件。不要在公开文档提交填好真实路径的配置。

## 知识 Provider 合同（stdin JSON，不是 HTTP）

引擎数据目录的 `config/knowledge.local.json` 可显式开启：

```json
{
  "schema_version": 1,
  "enabled": true,
  "provider": "builtin-module",
  "home": "/path/to/project-knowledge"
}
```

home 必须是已初始化的本地知识工作区。外部可执行接入用 `provider:"external-executable"`，其 home 下须有受信 `qingtian-kb` 可执行入口。禁用配置为 `{"schema_version":1,"enabled":false}`。`QINGTIAN_KNOWLEDGE_CONFIG` 可显式覆盖配置路径。

Provider 通过 stdin 接受请求，不把查询放进 argv：

```json
{
  "schema_version": "1.0",
  "request_id": "sample-query-round-1",
  "query": "release evidence policy",
  "caller_id": "sample.local",
  "purpose": "agent-context",
  "retrieval_modes": ["approved"],
  "top_k": 5
}
```

请求用途为 agent-context/human-research/test；调用身份和用途只是非敏感标签，不是认证。响应有 `results`、`metadata_warnings` 和每条引用的 `authority,authoritative,eligible_for_generation,usage_constraint,provenance`，不会回显 query。引擎核对 schema、关联 request ID、权限标记、来源完整性与结果数量，默认超时 5 秒、stdout 上限 512 KiB、stderr 上限 32 KiB。

候选引用需要 API 调用方显式 `include_candidates=True`，不会变成 approved。Worker 默认只取 approved；无匹配与 Provider 不可用是不同结果，后者写入 `knowledge.unavailable`。知识库的完整请求/响应 schema 随包位于 `qingtian_kb/resources/contracts/`。
