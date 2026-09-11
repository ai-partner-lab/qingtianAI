# 术语与状态（真实引擎）

## 记录各自代表什么

| 名称 | 含义 | 不能据此推断 |
|---|---|---|
| task | 持久工作单元，含目标、依赖、负责人、状态、证据要求和授权策略。也可承载分析或历史参考。 | 有 task 就具备实施授权。 |
| run | 一次运行尝试，有 attempt、状态、PID/进程组、退出码和可用时的 session ID；一个 task 可有多次 run。 | run DONE 或退出 0 就是任务验收通过。 |
| session | 执行会话与恢复关联；恢复依赖实际捕获且可用的 Codex session。 | 可任意复用其他会话或从旧库自动恢复。 |
| evidence | 产物/验证回执引用，含 kind/value/verified；按 task/kind/value 去重。 | `--verified` 自动运行检查，或同值重提会升级未验证记录。 |
| event | 持久事件、状态变化和有界元数据；事件源和原始产物仍需核对。 | 事件摘要就是完整模型日志或独立真实性证明。 |
| intake | 文本、附件、intent、规划草案与任务关联的收件记录。 | 收件成功就代表派发成功。 |
| owner_session | 职责路由标识，例如 frontend/backend/qa。 | 每个名称都连接了独立模型或真人。 |
| evidence_profile | 任务完成所需证据类别的选择；`requires_deploy=true` 额外要求 deploy/smoke。 | 所有任务共用一条“pass”字符串即可完成。 |
| worktree | 任务使用的独立 Git 工作树与分支。 | 容器隔离、自动合并或自动推送。 |
| scope | 注册仓库内的相对工作目录，Worker 在 worktree 中再次检查。 | 独立权限沙箱，或同时约束 Planner workspace。 |

## 模式、授权与展示

| 名称 | 实际含义 |
|---|---|
| manual | 默认可写模式；允许明确执行，后台只核对已有事实，不启动自动领取、恢复或补证 Worker。 |
| auto | 显式启用后台调度与有界恢复，仍受任务授权、依赖、环境及配额限制。 |
| analysis-only | `analyze` intake 写入的持久策略，禁止作为实施授权。 |
| reference-only | 台账导入的持久策略，历史参考不变成实施授权。 |
| tour | 同一引擎的临时只读合成演示；卡片状态不代表真实执行。 |
| qingtian-lab | 保留的旧版实验室，使用自己的 schema 和数据库。 |

## 持久状态和看板状态

引擎持久任务状态包含 `INBOX`、`PLANNED`、`QUEUED`、`RUNNING`、`WAITING`、`VERIFYING`、`FAILED`、`DONE`、`CANCELED`。它们分别描述收件、计划、排队、运行、等待、验收、失败、完成和取消；不是每个任务都必须依次走遍这些状态。

看板的 `PAUSED`、`PLAN_ONLY` 和等待类别含投影逻辑，不都是可直接传给 CLI 的持久状态。任务用 `blocking_reason` 描述阻塞，run 用 `failure_kind/failure_stage/failure_type` 描述失败，不能混用为一个 `waiting_reason` 字段。

`VERIFYING` 表示还需核对完成证据。manual 下已有 verified 证据满足要求后，状态核对仍能把任务推进 DONE。`DONE` 表示引擎证据门满足，其实际业务可信度还取决于回执的真实来源和独立复核。

[架构](ENGINE-ARCHITECTURE.md) · [API](ENGINE-API.md) · [运维](OPERATIONS.md)
