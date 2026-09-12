# 版本迁移与边界

## 0.6.0rc2 候选与追加 schema

当前源码版本为 **0.6.0rc2** 候选，不是已发布稳定版。最终候选回归、独立安装与包审查仍待完成；真实 provider 和原生 Codex 首次接入/有效角色工作流没有因版本号变化而通过验收。下方 0.4 → 0.5 说明保留历史上下文，不能充当本次升级回执。

本候选的引擎数据库 schema 为 **10**。初始化旧引擎库时新增 `tasks.action_revision`，旧行初值 0 只是迁移基线，不是重建的历史动作次数；revision 迁移不回填旧事件，不重建或替换任务表。既有参考/只分析授权策略迁移仍适用，不会把这些记录变成实施授权。

数据库触发器在每次任务行 UPDATE 后单调推进 revision，包括同值写入。人工动作 `action_version` 基于任务 ID 与该 revision，客户端视为不透明令牌；摘要等其他字段更新也可使旧令牌失效。升级后先重新 GET，遇到 409 时再次展示最新要求并确认，不能重放升级前缓存的完成声明。省略版本的旧调用只保留原子操作兼容，不具备过时界面检测。详见 [人工动作合同](ENGINE-API.md#人工动作完成声明)。

rc2 为新 HTTP dispatch 添加 revisioned admission request/receipt，并对实际创建的新 run 冻结七个不可变目标字段：model、reasoning、speed、worker type、owner session、branch、worktree。既有 run 列原样保留；旧历史若没有完整不可变快照，resume 失败闭合，不从任务字段、环境或当前默认猜测，不回填旧记录。intake/retry/auto/external-host ack 尚未统一到此接纳合同。详见 [配置与续接](ADOPTION.md#模型与推理配置)。

模型迁移不做宽松兼容：新选择只允许 Sol/Astra、`medium/high/xhigh/ultra` effort 和独立 standard/fast，且不 clamp。默认值只作用于新选择，不改正在执行或历史参数。真实执行还要求使用者在源码/数据/发布目录之外保存人工审查、最多 24 小时有效的 schema-2 capability manifest；它含本机敏感路径/指纹，不迁入公库或发布包。[准备与启用](CODEX-CAPABILITIES.md)

rc2 的 operations、admission 与 release 表/trigger 是追加结构，不重写既有 task/run/event/evidence 行。运维更正保留 native baseline 和旧/新值，作为补充投影而非覆盖原列；release batch/receipt 只登记声明式事实，不部署，也不从历史 DONE 合成发布记录。旧事实缺少可靠来源或当前 basis 时保持未知/stale。

接入前安排一致性备份，并先用副本或全新私有目录验证；不要直接操作正在工作的控制台、Worker 或旧任务。版本迁移会写入目标库，因此执行前须确认实例归属和停机授权。没有本环境的实际迁移/恢复回执，不声称无损回退；回退应使用相应旧版本与升级前一致性备份，不让旧二进制直接写 schema 10 库。

## 历史：0.4 到 0.5

0.5 默认入口 `qingtian` 使用真实引擎。0.4 的 `qingtian_core`、七步动画和合同保留在 `qingtian-lab` / `qingtian legacy`。两套运行时不共享数据库或状态模型，升级可执行文件不会把旧任务自动变成新的执行授权。

## 新实例接入

1. 记录原版本、原命令和数据位置，保留一致性备份；不要覆盖旧库。
2. 安装 0.5，选择源码之外的新私有数据目录，并核对项目与知识配置覆盖变量。
3. 运行无凭据自检，启动新的 manual 实例，检查健康响应的目录、模式和端口。
4. 重新登记获准使用的仓库、角色和现有开发基线；准备新任务的目标、范围和验收说明。
5. 按新 task/run 与实际产物核对执行；旧截图、旧报告或导入状态不能替代本轮证据。

下面的路径须替换为新实例的实际目录；端口须与已有服务分开：

```bash
export QINGTIAN_ENGINE_HOME="/absolute/path/to/private-engine-v05"
qingtian doctor
qingtian selftest
qingtian --workspace /absolute/path/to/authorized-workspace quickstart --port 8767 --open
curl --fail http://127.0.0.1:8767/api/health
qingtian project register sample --repo /absolute/path/to/your-repo --base dev --role backend
```

`selftest` 始终使用自己的临时库；它通过后仍需验证新实例的 HTTP、项目接入和真实执行。`quickstart` 默认不创建 manager entry；确需尝试真实入口时显式加 `--manager-entry`，`--skip-manager-entry` 仅保留旧调用兼容。完整步骤见 [接入手册](ADOPTION.md)。

## 保留历史参考

`qingtian import` 是显式文本台账导入入口，不接受旧 SQLite 数据库。支持指定 `--tasks`、`--governance`，或用 `--defaults` 明确扫描启动 workspace 的默认台账。普通 init/start/quickstart 不执行该扫描。

```bash
qingtian import --tasks /absolute/path/to/reviewed-TASKS.md
qingtian task list
```

导入前先审查文件内容与来源。导入任务带 `reference-only` 策略；新提交的只分析 intake 使用 `analyze` 并带 `analysis-only`。两者都不能通过切换 auto、重试或改显示状态升级为实施任务。实施需要另建任务并提供当前指令，不存在把任意历史会话接回执行的迁移捷径。

## 知识库与配置

知识工作区、索引、凭据和项目映射由接入方持有。0.5 不附带知识正文，也不把旧任务自动写成 approved 知识。保留现有知识内容时，先检查其格式、来源、review/authority 元数据与 Provider 合同，再按 [知识接入步骤](ADOPTION.md#接入自己的知识库) 显式配置；未经验证的兼容性列为待验证。

## 并存与回退

旧实验室和新引擎分别使用端口与数据目录；不要让它们读写同一个任务数据库。停止新服务使用新数据目录下的 `qingtian stop`，需要取消 Worker 时另行按任务取消。回退使用原版本及其对应备份，不能用 0.4 打开 0.5 状态库。

引擎数据库、附件、worktree 和 Codex 会话有各自的生命周期。升级包、停止服务或移除项目注册均不等于删除这些数据。没有经过实际恢复演练，不应宣称可无损跨版本回退。

[旧版归档](legacy/README-v0.4.md) · [合同边界](CONTRACTS.md) · [运维](OPERATIONS.md) · [发布清单](RELEASING.md)
