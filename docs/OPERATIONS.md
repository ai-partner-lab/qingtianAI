# 日常运维手册（真实引擎）

日常管理先确认数据目录与实例身份，再检查任务、run 和证据。以下示例假设当前 shell 已设置指向目标私有目录的 `QINGTIAN_ENGINE_HOME`；也可以在各引擎管理命令前显式传 `--data-dir`。

## 服务生命周期

```bash
qingtian --workspace /absolute/path/to/authorized-workspace start --mode manual --port 8767 --open
qingtian status --port 8767
curl --fail http://127.0.0.1:8767/api/health
qingtian stop
```

`start` 和 `quickstart` 默认在后台运行，关闭启动终端不会停止服务。前台诊断使用 `start --foreground`。`stop` 依据所选数据目录的实例锁定位服务，没有 `--port` 参数；不删除任务库，也不取消独立 Worker。`quickstart` 固定使用 manual；auto 使用明确的 `start --mode auto`。

`status` 只输出 `running/stopped`，不提供 JSON，也不严格证明端口上的实例就是指定目录。`GET /api/health` 才提供 `service,pid,mode,automatic_dispatch,read_only,data_dir,workspace,watchdog`。对照预期目录和端口，普通 manual 应为可写且不自动派发。若出现实例身份不符或未核验提示，先选其他端口或明确停止正确实例。

manual 仍会核对已有事实、完成已满足证据门的任务和解除已完成依赖；不会在后台启动领取、恢复或补证 Worker。切换 auto 前先检查该目录中所有待执行任务、项目配置及授权。

## 命令速查

| 目的 | CLI | 说明 |
|---|---|---|
| 环境诊断 | `qingtian doctor` | 不检测登录/额度，不启动服务。 |
| 能力清单 | `qingtian capabilities status` | 只读核对已启用 reviewed manifest；不调用模型。 |
| 项目映射 | `qingtian project list` | 查看本地注册项。 |
| 任务列表 | `qingtian task list` | 可重复传 `--state` 筛选。 |
| 任务详情 | `qingtian task show TASK_ID` | 含依赖、运行、事件与证据。 |
| 明确派发 | `qingtian dispatch TASK_ID --prompt-file /path/to/instructions.md` | 顶层命令，可能启动真实执行。 |
| 取消任务 | `qingtian cancel TASK_ID` | 按任务取消受管进程，保留历史。 |
| 状态核对 | `qingtian reconcile` | 根据已有进程与证据事实核对。 |
| 基础设施恢复 | `qingtian recover TASK_ID` | 有前置条件，可能启动新 run；不是所有失败的通用重试。 |
| 日报 | `qingtian report` | 查看报告；`--out` 可写私有报告文件。 |
| 查看反馈 | `qingtian feedback --consumer maintainer --peek` | 不推进消费者游标。 |
| 知识配置 | `qingtian knowledge status` | 只读配置，`queried=false`。 |

CLI 没有 `health`、`task dispatch`、`task cancel` 或 `task reconcile` 子命令。`retry`、`complete-human-action` 和 `remind-external` 是任务 HTTP 动作，路径与副作用见 [API 合同](ENGINE-API.md)。

## 处理具体人工待办

待办区先看具体要求、归属和敏感标记，不用“等待中”等概括文字代替原要求。提交普通“已处理”声明前重新读取任务详情并确认 `action_version`，以 `expected_action_version` 随请求发送；409 表示任务行版本已变化或仍有活跃 run，重新 GET 并展示最新要求后再次确认，不循环重放旧请求。schema 10 使用持久单调 revision；即使动作文字没变、只更新了摘要，旧令牌也可能过时，不是秒级时间或内容指纹。

敏感动作、暂停、仅规划/参考、导入记录、已完成/已取消与非 user 归属不能由普通完成按钮处理；没有动作版本的旧服务也不能由新看板退回无版本确认。旧 API 省略版本虽保留兼容，但不能防止过时界面的误确认。[完整请求/错误边界](ENGINE-API.md#人工动作完成声明)

成功声明只进入 `VERIFYING` 和内部复核，不变成审批、预算、部署授权或 verified 证据。需要资料、审批或外部操作时仍走原指定渠道；不要把凭据填入待办正文。复制/查看要求以及取消确认均不写完成事实。

## 模型和实时连接排障

先运行只读 `qingtian capabilities status`，再对照[模型/推理优先级](ADOPTION.md#模型与推理配置)、目标任务、HTTP admission 和 run 的持久选择。缺失/过期/失配 manifest 或未广告的目标组合失败闭合；按 [能力清单流程](CODEX-CAPABILITIES.md)在私有新路径准备并人工审查，不修改旧清单时间。新 run 冻结七字段目标；旧历史缺完整不可变快照就拒绝 resume，不从当前任务/环境猜测，也不补写旧行。未调用 provider 时只能确认配置和本机 advertisement，不能宣称账户权限、served tier、额度或真实执行。

SSE 的整板 `version` 与已消费 `cursor` 分开：积压页的快照版本可相同，但 changes 必须逐页处理；`has_more` 表示还在补收。浏览器在成功消费帧后保存新的 `qingtian-event-cursor`，不会把旧 `qingtian-event-version` 快照缓存迁移为消费进度。旧会话首次升级可能从 0 补页；正常的低频 REST 刷新不确认 SSE 事件。重连用 header 优先的 `Last-Event-ID`，reset/换实例处理见 [SSE 合同](ENGINE-API.md#sse-版本合同)。

## 任务排障顺序

1. 读取 `/api/health` 核对实例身份、模式与 watchdog，再查看目标任务的 `state`、`blocking_reason` 和依赖。
2. 查看最新 `runs` 的 `attempt,status,exit_code,failure_kind,failure_stage,failure_type`，以及相关事件。`task show` 只提供最近的有界事件列表，不是无限历史导出。
3. 若没有 run，确认是否尚未明确派发、缺项目映射、依赖未完成，或持久策略为 `analysis-only/reference-only`。这些策略不会因切换 auto 变成实施授权。
4. 若停在 `VERIFYING`，检查 profile、`requires_deploy` 和已登记的 verified 证据，独立核对产物后补齐真实回执。
5. 若存在人工/外部等待，读取 `/api/operations-clarity/tasks/{id}`，把 actor/owner、next action、due、逐字段 source 与 native baseline 一起核对。更正只是追加补充，不覆盖原生事实；外部提醒只记账，不代表邮件或聊天消息已发出。

`run.status=DONE` 与 `task.state=DONE` 是两个不同事实，二者都不等于 release。任务完成的事务证据门还会检查 verified evidence、未解决动作/依赖、活跃 run 和保护状态；`force` 不能越过。任务字段 `blocking_reason`、看板等待分类和 run 的失败分类也不能互换。

## 证据与反馈

场景化解释见 [执行结束但缺证据](SCENARIOS.md#s06)。应把“已交付产物”“缺失回执”“补证 owner”“所需权限”分开列，不能只回报一个未完成状态，让用户猜下一步。

`task evidence ... --verified` 是受信操作方的声明，不自动运行测试。相同 task/kind/value 重复登记会返回 `inserted=false`，也不会把未验证记录升级为已验证。需要增加实际独立复核回执时，登记其新的可追溯引用。操作示例见 [接入手册](ADOPTION.md#核对结果和登记证据)。

普通 `feedback` 读取会推进该 consumer 的游标；巡检使用 `--peek`。`--catch-up` 把游标移到当前头部，应仅在明确要跳过积压反馈时使用。它们都不代表通知已经送达外部平台。

`/api/release-batches` 只登记经过审查的声明式 deployment/enablement/acceptance facts 与追加 receipt。它不执行部署、开关或验收，不因 task/run DONE 自动产生记录，也不独立复验声明来源。发布操作仍按接入方明确授权的独立流程进行，并保留实际 artifact、环境与外部回执。

## 中断续接运行单

适用于 [额度中断](SCENARIOS.md#s01)、[重启](SCENARIOS.md#s02)与 [检查点交接](SCENARIOS.md#s03)。这是操作约定，不是自动恢复脚本；不要将下表全部映射为一次 `qingtian recover`。

| 查到的事实 | 下一步与负责方 | 停止条件 |
|---|---|---|
| 宿主报告额度／限流 | 大管家读取原失败及当前宿主可用状态；环境允许后续接确切执行任务。 | 额度仍不足或状态未知，不派发、不充值、不消费 reset、不切模型试探。 |
| 服务离线、原库仍在 | 操作方确认目录与备份，用 manual 恢复服务，核对 `/api/health`，再列仍获准任务。 | 实例身份、数据库或配置不符，停止恢复；不要批量唤醒旧任务。 |
| 会话不可用或上下文不足 | 大管家检查 owner 是否仍在写入，整理检查点和剩余清单，再按宿主规则交接。 | 活跃状态不明、缺检查点、没有新任务／迁移授权，不能重复派发。 |
| run 已结束但验证不全 | 大管家指出具体缺证据及负责方；执行者只在授权内补证。 | 发布暂停或验收环境缺失时等待，不改变合同凑完成。 |
| 本地成果已交，供应商未就绪 | 分列本地交付和外部等待，写明谁提供哪份资料／权限。 | 外部未接通保持未测试；提醒记账不算消息已发送。 |
| 本轮测试失败 | 文件 owner 根据原失败返修，独立复验者核对相同版本。 | 触及别的 owner、需要新权限、达到尝试上限或同因无新信息时停止。 |

只读开始：

```bash
qingtian task list
qingtian task show TASK_ID
qingtian feedback --consumer maintainer --peek
```

然后按“原失败 → 当前可执行条件 → 确切任务／owner → 批准与检查点 → 仅剩余工作 → 派发回执 → 实际验收”的顺序记录。task、run、宿主会话分别关联；只有标题或截图不够。缺信息时问最少的必要问题，不猜被截断的批准内容。

每次续接仍保留用户暂停、`analysis-only/reference-only`、文件归属与模型约束。manual 的状态核对不会后台启动恢复 Worker；切 auto 会改变调度行为，不能作为排障快捷键。显式 `dispatch`／`recover` 可能启动真实执行，须先核实适用失败类型、原授权和活跃 run，不能用它们盲目重试限流。

私有记录至少含：环境／版本、原任务和 owner、失败类别、恢复依据、产物检查点、允许与禁止动作、剩余工作、尝试上限、派发与最终验收回执。保留旧失败和旧候选；新源码变化后不能沿用旧通过作为新版本证明。使用 [标准续接单](SCENARIOS.md#一次续接的共同约定)，公开时另做脱敏摘要。

大管家派发后释放入口，详细检查记录由执行任务保留。未接通知工具时只报告当前事实，不承诺“之后会自动叫醒你”；访问任务详情也不等于续接或授权执行。

## 数据、日志与升级

运行日志位于数据目录的 `run/server.log`。诊断时只保留必要片段并脱敏；任务、提示、附件、Codex 会话、知识库和 worktree 都可能含项目私有资料。

同目录重启会保留任务。新一轮隔离验收使用新的数据目录，并核对项目/知识配置覆盖变量。备份应包含一致的 SQLite 状态及相关附件/配置；不要在服务写入期间只复制主数据库文件而忽略 WAL。安排停机备份前，先处理活跃任务和独立 Worker，再停止对应服务。恢复与版本兼容见 [迁移说明](MIGRATION.md)。

HTTP 仅供本机受信客户端使用，没有多用户认证。导出报告、截图或发布工单前审查项目路径、知识正文和凭据。不要凭一个历史 PID 文件或模糊进程名批量杀进程，应先确认任务/run 与实际进程归属。

[接入手册](ADOPTION.md) · [协作场景](SCENARIOS.md) · [FAQ](FAQ.md) · [术语](TERMINOLOGY.md) · [安全策略](../SECURITY.md)
