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
| `GET /api/operations-clarity` | 只读任务运维投影；逐字段返回 value/source，并显示 owner/actor、next action、due、等待原因、警告与 completion basis。 |
| `GET /api/operations-clarity/tasks/{task_id}` | 单任务运维投影、追加式更正历史和当前 HTTP admission。 |
| `GET /api/release-batches[?environment=...&task_id=...]` | 声明式发布批次列表；是已登记/已审查回执，不执行发布。 |
| `GET /api/release-batches/{batch_id}` | 批次、items、receipt revisions 及各事实的有界 assessment。 |
| `GET /api/tasks/{task_id}/admission` | 当前 HTTP 派发准入 revision/request/receipt；无接纳时字段保持空，不猜外部状态。 |
| `GET /api/v2/orchestrator` | 启用时返回 `enabled,status,holder_id,fencing_token,updated_at,summary,evidence,dead_letters,plugins`；关闭时 404、`{"enabled":false}`。 |
| `GET /api/events/stream` | `text/event-stream`，先 snapshot，再 dashboard/heartbeat；详见下节。 |

任务对象稳定关注字段：`id,title,scope_summary,priority,environment,repository,base_branch,branch,worktree,worker_type,owner_session,state,progress,blocking_reason,evidence_profile,execution_mode,requires_deploy,created_at,updated_at`。额外字段由当前实现返回，客户端应保留前向兼容，不把角色标签误认为已经连接的会话。

任务详情及看板任务还返回 `model,reasoning,speed` 和人工动作的 `action_owner_kind,action_owner,action_text,action_due,action_sensitive,action_revision,action_version`。`action_version` 是由持久任务行 revision 生成的不透明比较令牌，不是墙钟时间/内容指纹、权限或完成证据；客户端只原样回传，不自行拼接。

`requires_deploy` 输入必须是 JSON boolean；字符串 `"false"`、数字和 null 会拒绝。明确为 true 时，所有证据 profile 都额外要求 verified deploy 与 smoke；为 false 时，“未部署”等旁证不会扩大任务要求。

run 字段保留 `id,task_id,attempt,adapter,pid,process_group,session_id,status,exit_code,result_hash,retry_of,failure_kind,failure_stage,failure_type,started_at,finished_at,model,reasoning,speed`。经新准入创建的 run 另有不可变执行目标快照，精确冻结 model、reasoning、speed、worker type、owner session、branch、worktree。运行中或旧 run 不因环境/默认值变化而修改；旧历史缺完整快照时 resume 拒绝，不从任务列猜测，也不回填既有 run 列。`run.status=DONE` 不等于 `task.state=DONE`，更不等于发布；配置字段也不证明 provider 实际执行过该组合。

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

可另传当前入口支持的执行参数。所有新选择只接受 `gpt-5.6-sol` / `gpt-6-astra`、`low` / `medium` / `high` / `xhigh` / `max` / `ultra` 及独立的 `standard` / `fast`，不 clamp。优先级为显式参数 > 环境变量 > 路由/角色默认；完整边界见[模型与推理配置](ADOPTION.md#模型与推理配置)。格式接受仍不代表账户支持。

**此 HTTP 入口当前不转发 `Idempotency-Key` 或 body 的 `idempotency_key`。** 不要对超时的创建请求盲目重放；需要可重放收件流程时使用 intake 接口。

## 明确派发与任务动作

`POST /api/tasks/{task_id}/dispatch`：

```json
{
  "instruction": "Inspect the sample parser and report findings. Do not edit files.",
  "resume": false,
  "expected_revision": 1,
  "idempotency_key": "sample-dispatch-round-1"
}
```

instruction 必须非空字符串，resume 必须 boolean；`expected_revision` 从刚读取的 admission 原样回传，`idempotency_key` 对同一逻辑请求保持稳定。成功 HTTP 200 返回 task、run、request 与不可变 receipt；相同键/相同正文安全重读，同键不同正文拒绝，revision 冲突须重新 GET 后人工判断。请求可能实际启动 Codex，因此应在明确授权后调用。resume 还要求旧 run 有完整七字段不可变快照及可用 session；缺失时拒绝，不导入、猜测或回填任意旧会话目标。

上述 admission 保证**只覆盖这个 HTTP dispatch 路径**。`POST /api/tasks` 的 `auto_start`、intake 首次分发/重试、`/retry`、auto scheduler 和外部 host acknowledgement 尚未统一到同一接纳协议；外部 host ack 当前为未集成。调用方不能把其中一个路径的 receipt 当成另一路径的接纳证明，也不要对不确定结果自动重试。

其他 POST 动作通常接收空对象；heartbeat 与人工动作完成声明的正文见下表及下节：

| 路径后缀 | 作用 | 响应/约束 |
|---|---|---|
| `/cancel` | 取消当前任务及其受管进程 | 当前 manager 结果对象；不是删除任务历史。 |
| `/plan` | 明确标记为 PLANNED | 更新后的 task；可能影响 auto 的后续候选，勿当纯展示动作。 |
| `/retry` | 通过最近可用 session ID 明确恢复 | 更新后的 task；无 session 时 400。可能启动真实执行。 |
| `/complete-human-action` | 报告普通用户动作已处理，进入内部复核 | 正文携带 `expected_action_version`；成功 task 为 VERIFYING，不是审批、授权或最终完成。 |
| `/remind-external` | 写入外部提醒记录 | 更新后的 task；不代表发送了外部消息。 |
| `/heartbeat` | 登记外部/委派执行心跳 | JSON `{"mode":"external"}` 或 `{"mode":"delegated"}`；不是启动器。 |

HTTP 没有任意状态 transition、dependency 和 evidence 写接口；这类管理能力由 CLI/Python 服务层提供。CLI 的 `task evidence ... --verified` 是受信操作方的验证声明，不是自动检测结果。

证据按 task/kind/value 去重。CLI 返回 `inserted=false` 时没有更新已有记录；对同一个未验证 value 重复加 `--verified` 不会升级验证标记。完成独立复核后应登记新的可追溯复核回执，示例见 [接入手册](ADOPTION.md#核对结果和登记证据)。

`analysis-only`、`reference-only` 任务及导入参考记录不能通过 dispatch/retry 变成实施任务；切换 auto 或修改显示状态也不产生执行授权。需要实施时另建明确授权的新任务。`authorization_policy` 是任务持久字段，不从普通任务创建 HTTP 正文接收任意覆盖。

### 人工动作完成声明

先读取 `GET /api/tasks/{task_id}`，展示其完整具体要求及归属，再在用户明确确认后提交：

```json
{"expected_action_version":"COPY_THE_FRESH_TASK_ACTION_VERSION"}
```

服务在同一写事务中比较版本、检查当前动作和活跃 run，再清空人工动作并将任务置为 `VERIFYING`，留下 `task.human_action_completed` 等审计事件。它表示“用户报告已处理，等待内部核对”，不写 verified evidence，不授予审批、预算、部署或恢复暂停的权限；最终仍须独立证据门。

schema **10** 的 `tasks.action_revision` 是非负、单调递增的任务行版本。任何任务行 UPDATE 都可能让旧令牌失效，包括同值重新下发动作、清空动作或只改摘要；同一秒的相同要求也不是同一版本。旧库升级时已有行从 0 初始化，0 不是历史动作次数，既有任务字段和事件不因本次 revision 迁移而伪造回填。动作变更/完成与其审计同事务提交，审计插入失败则整个动作回滚。数据库外部删除/恢复行不属于受支持的并发机制。

- 版本不匹配或存在 `QUEUED`/`RUNNING` run：HTTP **409**，无完成写入；重新 GET 任务并让用户检查最新要求、确认新版本，不盲目重放旧声明。普通版本冲突表示读到的快照过时，不代表审批遭拒或流程卡死；升级前缓存的令牌也须重新读取。
- 非 user 归属、敏感动作、用户暂停、仅规划/参考策略、导入记录、DONE/CANCELED：普通完成入口拒绝（通常 400）。敏感操作必须走其明确授权流程，不把密钥/凭据粘到完成声明中。
- `expected_action_version` 若提供，必须是非空字符串。为兼容旧调用方，省略或 JSON null 暂仍允许原子处理“此刻的动作”，但**不能检测旧界面已经过时**；新客户端必须携带刚读取的非空令牌。当前看板在服务不提供动作版本时禁用普通完成按钮，而不是退回无版本写入。
- 读取或复制具体要求、取消确认框、发送普通提醒都不构成完成/审批。外部提醒只是账本记录，不证明通知送达。

## 收件、规划和附件

`POST /api/intakes` 仅接受 multipart/form-data：

| 字段 | 类型与含义 |
|---|---|
| `text` | 字符串，最多 20,000 字符；无附件时必须非空。 |
| `intent` | `analyze`、`implement`、`implement_and_deploy_dev`。manual 缺省 analyze，auto 缺省 implement；客户端应始终显式传入。 |
| `idempotency_key` | 同一逻辑请求稳定的非敏感键；也可用 `Idempotency-Key` 请求头，表单字段优先。 |
| `advanced` | JSON 字符串，允许 title/scope_summary/priority/environment/repository/base_branch/worker_type/requires_deploy/model/reasoning/speed/planner_model/planner_reasoning/planner_speed。规划器选择和执行选择分别记录；不会用规划器默认覆盖显式执行参数。 |
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

## 运维清晰度与完成门

`GET /api/operations-clarity` 及单任务路径把等待事实投影为带来源的字段。owner/actor、next action、due time 可来自原生 action 列或经过审查的外部记录；每个投影字段保留 source，来源冲突、过期或与当前 native basis 不一致时显示 warning 并回到当前原生事实，不猜一个结论。

`POST /api/operations-clarity/tasks/{task_id}/corrections/preview` 只预览，`POST /api/operations-clarity/tasks/{task_id}/corrections` 追加复核更正。正文必须携带 `expected_revision,idempotency_key,changes,correction_reason,actor,source`；changes 只允许 reason/next_action。更正记录 old/new values、actor、source 和 native baseline，是补充显示层，不 UPDATE 原生任务字段；同键不同正文或旧 revision 拒绝，原始事实变化后旧更正 stale。

任务进入 DONE 使用同一事务中的 `completion_basis`：核对 evidence profile 所需 verified evidence、未解决依赖/人工动作、活跃 run 与保护状态。状态、run/evidence 与审计的相关写入必须共同成功或共同回滚。`force` 只影响普通转换约束，不能越过此证据门。

## 声明式发布事实

- `POST /api/release-batches/preview`：校验并投影批次，不写入。
- `POST /api/release-batches`：以 idempotency key 创建含版本、环境、owner、artifact、task 映射及声明 facts 的批次。
- `POST /api/release-batches/{batch_id}/receipts`：携带 `expected_revision,idempotency_key,items` 追加已复核的事实变化。

事实维度限于 deployment、enablement、acceptance，各自保留 observed/source/review/proof 及有界 assessment。发布表和 receipt append-only；发布 API 不运行部署、开关或验收动作，也不独立重验声明。DONE 任务不会自动创建批次或发布事件。客户端应把 `assurance=recorded_evidence_review_not_independent_reverification` 与 source 一并展示，不能把 registered/assessed 状态改写成“系统已发布”。

## SSE 版本合同

连接可用 `?lastEventId=123`，或 `Last-Event-ID: 123`；**存在请求头时 header 优先**，这使原 URL 上的旧 query 不会覆盖浏览器自动重连携带的进度。非整数返回 400，负数归零；缺失/空值按 0。连接起始提供 `retry: 3000`。

首个 `snapshot` 与后续 `dashboard` 帧均包含 `version,cursor,changes,dashboard,reset,has_more`：

- `version` 是当前整板快照的事件上界，等于 `dashboard.version`；快照与这页事件在同一 SQLite 读取快照内获取。
- `cursor` 是本帧最后实际返回的事件 ID；没有事件时保持本页起点。SSE 的 `id:` 只使用此 cursor，不使用更靠前的快照 version。
- HTTP SSE 每页最多 100 条变化摘要，按 ID 升序。`has_more=true` 时立即继续补页；多页可以共享相同 version，却推进不同 cursor。客户端必须消费每页 changes，不能因 version 相同丢掉后续批次。
- `reset=true` 表示客户端 cursor 大于当前事件上界，服务从当前日志起点重新分页。清空旧投影并消费该帧，保存该页实际 cursor；不是直接跳到快照 version，也不是任意换库都能被识别的保证。

```text
id: 100
event: dashboard
data: {"version":250,"cursor":100,"has_more":true,"reset":false,"changes":[{"id":100}],"dashboard":{"version":250}}
```

上述只展示字段关系，省略了本页其他事件和完整 dashboard。积压清空后服务约每 0.75 秒检查更新；heartbeat 约每 15 秒一次，携带 `version,cursor`，其 `id:` 不越过已发送变化进度。heartbeat、REST dashboard 和 task 读取都不能替代 changes 的消费确认。

当前看板只在成功处理帧后把 `id:`（缺失时取 payload.cursor）写到 `sessionStorage["qingtian-event-cursor"]`，快照 version 留在内存用于渲染。旧键 `qingtian-event-version` **不迁移为新 cursor**；没有新键就从 0 补收。解析/渲染失败不前移 cursor，重连可能重复收到同一事件，消费方应按事件 ID 幂等处理。SSE 不承诺恰好一次投递或无限保留的外部审计流；换数据目录/实例时仍须核实身份。

这是当前源码合同，不追溯修复[历史录像中披露的 13 条遗漏](VIDEO-DEMO.md#必须一起展示的-sse-限制)，也不代替最终候选的重连、并发与真实浏览器验收。

## 错误与测试边界

常见结构为 `{"error":"..."}`；非法正文/动作通常 400，Origin/Host 不符 403，资源不存在 404，人工动作冲突 409，附件过大 413。不存在的静态路由可能为 HTML 404；不要宣称所有错误都符合统一 JSON 错误 schema。

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
