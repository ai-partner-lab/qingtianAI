# 真实引擎接入与日常使用

`qingtian` 启动本地任务引擎。控制台、doctor、selftest、tour 与 Knowledge Hub 可以无模型凭据启动；执行项目任务还需要 Git、已安装并登录的 Codex、获准使用的仓库、有效的本机能力清单和可用模型额度。

## 快速上手

支持 macOS / Linux、Python 3.11 至 3.14。Windows 使用 WSL2。以下命令在源码目录执行：

```bash
git clone https://github.com/ai-partner-lab/qingtianAI.git
cd qingtianAI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
qingtian doctor
qingtian selftest
qingtian quickstart --open
```

`doctor` 检查 Python、平台、策略资源、路径及可选工具是否存在，不验证 Codex 登录或模型额度。`selftest` 在自己的临时目录完成 8 项合成检查：空库、任务幂等、未验证证据不完成、已验证证据完成、manual 不领取任务、重开持久化、SQLite 完整性、看板投影。它不启动 HTTP 服务、不调用模型，也不替代浏览器或业务验收。

首次使用新的数据目录时，`quickstart` 启动空库与 `manual` 后台服务；复用目录则保留已有任务。它固定使用 manual，不接受 `--mode`。默认数据目录是 `~/.local/share/qingtian/engine`，控制台地址是 [本机 8766 端口](http://127.0.0.1:8766/)。关闭启动终端不会停止后台服务；停止用 `qingtian stop`。

需要隔离试用时，先将下面的占位路径换成新的私有目录，再执行后续命令。此变量只影响当前 shell 及其子进程；服务和管理命令必须使用同一数据目录。

```bash
export QINGTIAN_ENGINE_HOME="/absolute/path/to/private-engine"
qingtian --workspace /absolute/path/to/authorized-workspace quickstart --port 8767 --open
qingtian status --port 8767
curl --fail http://127.0.0.1:8767/api/health
```

检查健康响应的 `service`、`data_dir`、`workspace`、`mode`、`automatic_dispatch`、`read_only`。普通 manual 实例应为 `mode=manual`、`automatic_dispatch=false`、`read_only=false`。`status` 只打印 `running` 或 `stopped`，实例身份以健康响应为准。若已设置 `QINGTIAN_PROJECTS_CONFIG` 或 `QINGTIAN_KNOWLEDGE_CONFIG`，同时确认它们指向本轮要用的私有配置。

## Codex 大管家入口

`quickstart` 默认只启动 manual 控制台，不联系 Codex。显式添加 `--manager-entry` 后，才会在启动控制台前尝试创建或复用名为“擎天大管家”的真实 Codex 入口。这个过程不启动模型轮次、不恢复旧会话，也不派发项目任务；失败会输出结构化状态，控制台仍可启动。

`--skip-manager-entry` 为既有调用兼容保留，不能与 `--manager-entry` 同时使用。以下命令沿用上文的私有数据目录；`manager-entry status` 只读本地上次同步快照并核对当前 scope，`instructions` 只打印建议的角色规则，两者都不联系 Codex：

```bash
qingtian --workspace /absolute/path/to/authorized-workspace quickstart --port 8767 --manager-entry
qingtian manager-entry status --workspace /absolute/path/to/authorized-workspace --data-dir /absolute/path/to/private-engine
qingtian manager-entry instructions
```

查看状态时须使用初始化时相同的 workspace、Codex home 和 transport；代理模式也要沿用相同的传输配置。CLI/HTTP 发现 scope 不匹配时，只读显示 `scope_mismatch`、不可用和 stale，不修改旧绑定。不要把旧 workspace 的快照当成当前入口可用。

| 状态字段 | 它能证明什么 | 当前边界 |
|---|---|---|
| `metadata_ready` | 当前 scope 匹配的上次名称与置顶回读。 | 不是实时连接或角色工作流验收。 |
| `role_configuration` | 新建时保存规则提交来源、摘要和时间，状态为 `submitted_at_creation`。 | 提交不是回读；复用或显式绑定且没有对应本地创建回执时来源为 `unknown`，`verified` 仍为 false。 |
| `workflow_ready` | 完整大管家工作流验收状态。 | 当前协议无法回读有效角色指令，本版本保持 false。 |

即使命名和置顶元数据成功，整体仍是 `partial / role_unverified`；`init`/`sync` 因角色或其他验收未完成而退出 2。只读 `status`/`instructions` 退出 0，不代表初始化成功。旧 `ready` 快照也不能自动成为角色已验收的证明。

已验证历史检查点的自动置顶以 `thread/read` 回读 `isPinned=true` 为依据。先前 codex-cli 0.153.4 的隔离冒烟未提供该字段，得到 `partial / pin_unverified`、`pinned=null`；只代表旧检查点，不表示所有版本永久不支持置顶。内建分区兼容正在单独处理，本文不将其记为独立复验通过，旧候选也不包含后续改动。真实桌面侧栏可见、置顶和有效角色协作工作流仍需端到端验收；Windows 原生 transport 清理路径也尚未验收。

接入方可以先用 `instructions` 审查建议规则，再通过受支持的客户端配置明确合并规则并保留既有约束；实际工作流测试需另行授权新的小任务。该初始化器不替你发送消息、覆盖既有指令或启动模型轮次，当前版本也不把人工确认自动写成角色 `verified=true`。

显式初始化、复用、同步、传输配置和失败恢复见 [大管家入口说明](MANAGER-ENTRY.md)。普通 `start`、`tour`、`selftest` 和 HTTP GET 不执行入口初始化。

## 把续接约定接入自己的工作流

先读 [场景库的能力层级](SCENARIOS.md#能力层级)。创建名为“大管家”的入口并不自动赋予跨任务管理权限；`instructions` 输出的规则也不是已生效的角色证明。

1. **选择协调载体。** 若宿主提供任务读取、续接消息和状态工具，按其权限与任务创建规则接入；否则采用人工协调，不假造一个公共 CLI 的“继续全部”命令。
2. **建立确切关联。** 在私有交接记录中保存用户请求、执行任务、唯一 owner、workspace、产物版本和检查点。引擎 task ID、run ID 与宿主会话标识不是同一种 ID，不能直接互换。
3. **保留批准范围。** 每次交接带上目标、方案、允许文件、测试范围、模型与推理要求、已做修改和暂停事项。明确指定的模型不可用时报告阻塞，不继承较低默认值冒充满足要求。
4. **按中断事实续接。** 核实原失败、当前额度／环境和已有活跃尝试后，只派剩余工作；账户仍不可执行时等待。不要把 `recover` 当通用补额度入口，或反复创建新会话绕限流。
5. **给出交付与回报方式。** 大管家派发后说明归属并释放入口，执行者回传实际产物和验收证据。只有确实接入通知机制才承诺通知，不假称永久回调。

操作前可先只读核对任务；下面的 `TASK_ID` 必须来自正确引擎实例，不是宿主会话 ID：

```bash
qingtian task show TASK_ID
qingtian feedback --consumer maintainer --peek
```

这些命令不查询账户额度，也不发送宿主续接消息。`doctor` 通过不代表额度恢复。宿主读取不到恢复事实时，保持未知并明确下一步；“现在可以了”不授权充值、消费 reset、换模型、跨机搬运或解除旧发布暂停。

采用 [文字续接单](SCENARIOS.md#一次续接的共同约定)保留既有批准，不要求用户重述可靠记录。真实演练须另用新通用样例项目明确授权；本手册新增步骤不代表已跑过该旅程。排障分流见 [中断续接运行单](OPERATIONS.md#中断续接运行单)。

## 接入与项目映射

在同一数据目录下注册获准执行的现有 Git 仓库。示例 `dev` 必须是可用基线；注册不创建分支，实际 worktree 准备仍会校验该 ref。`main/master/origin/main/origin/master` 不允许作为执行基线。

```bash
codex login
qingtian project register sample --repo /absolute/path/to/your-repo --base dev --role backend --role qa
qingtian project list
qingtian task add --title "Add a minimal regression test" --project sample --worker cli --owner backend
```

将返回 JSON 的 `id` 替换下列 `TASK_ID`。提示文件由接入方提供，应写明目标、允许修改的文件、验收命令、产物和停止条件。派发可能调用真实 Codex 并修改任务 worktree。

```bash
qingtian dispatch TASK_ID --prompt-file /absolute/path/to/instructions.md
qingtian task show TASK_ID
qingtian feedback --consumer maintainer --peek
```

注册表默认为数据目录的 `config/projects.local.json`，可用绝对 `QINGTIAN_PROJECTS_CONFIG` 覆盖。没有映射时拒绝仓库执行；多项目角色匹配不唯一时显式指定 `--project`。注册拒绝引擎源码、运行数据目录和安装包内的仓库。`project remove sample` 只移除注册项，不删除仓库。

可选 `--scope src` 指定仓库内的相对目录；Worker 会在新 worktree 再次检查并以此作为 cwd。scope 是工作目录约束，不是独立安全沙箱。执行仍受 Codex 沙箱和本机权限约束。任务分支/worktree 不会自动合并或推送母仓库。

## 核对结果和登记证据

`task show` 返回任务以及 `runs`、`events`、`evidence`、`dependencies`。先看最新 run 的退出码、会话、失败分类，再检查实际产物。成功退出后通常进入 `VERIFYING`；代码 profile 通常需要 verified commit 与 test，部署任务额外需要 deploy 与 smoke。

只有独立核对真实提交和测试回执后才登记。下面的 `REVIEWED_*` 是必须替换的占位值，不能原样充当验收证据：

```bash
qingtian task evidence TASK_ID commit "REVIEWED_COMMIT_AND_REPORT_REFERENCE" --verified
qingtian task evidence TASK_ID test "REVIEWED_TEST_REPORT_REFERENCE" --verified
qingtian reconcile
qingtian task show TASK_ID
qingtian report
```

`--verified` 是操作方的验证声明，命令不会替你运行测试。证据按 task/kind/value 去重；返回 `inserted=false` 表示未新增。重复提交同一个未验证 value 并加 `--verified` 不会升级原记录，应登记新的、可追溯的独立复核回执。证据可在验收前收集；状态完成仍由证据门核对，不能用 `transition --force` 代替验收。

任务完成检查与状态变更、run 收尾、证据及审计写入位于同一事务边界；缺少必需证据、仍有未解决动作/依赖、活跃 run 或保护状态时不能进入 DONE，`force` 也不能越过。`run.status=DONE` 只表示一次执行结束，不等于任务验收，更不等于发布。

`/api/operations-clarity` 以 actor/owner、next action、due time 与逐字段 source 显示等待责任。人工复核的更正是追加式补充层；它保留原生字段与旧值，不回写任务原生列，原始事实变化后旧更正会变 stale。`/api/release-batches` 只登记带审查来源的声明式 deployment/enablement/acceptance 事实和回执，不会部署、启用、验收或因 DONE 自动建发布记录。

## 通过 HTTP 收件

在自己新建的 manual 测试实例上，可以提交只分析请求：

```bash
curl --fail http://127.0.0.1:8767/api/intakes \
  -F 'text=Review the sample documentation; do not modify files.' \
  -F 'intent=analyze' \
  -F 'idempotency_key=sample-doc-review-round-1'
```

`analyze` 生成带 `analysis-only` 策略的关联任务，不启动实施 Worker。`implement` 是明确执行意图，manual 下也可能派发；`implement_and_deploy_dev` 还涉及开发部署。客户端应始终显式传 intent，并读取 intake 的状态、receipt 和 task IDs，而不是只看 HTTP 2xx。

intake 支持稳定幂等键；直接 `POST /api/tasks` 当前不转发幂等键。只有 HTTP `POST /api/tasks/{id}/dispatch` 接入了带 `expected_revision` 与 `idempotency_key` 的派发准入回执。intake 派发、retry、auto 调度和外部 host acknowledgement 尚未统一到该保证，不能据此重复提交或推断外部接纳。附件、布尔参数、SSE 与错误响应详见 [HTTP/API 合同](ENGINE-API.md)。

## 模型与推理配置

新选择只接受 `gpt-5.6-sol` 与 `gpt-6-astra`。推理强度只接受 `low`、`medium`、`high`、`xhigh`、`max`、`ultra`，精确校验且不 clamp；速度是独立的 `standard` 或 `fast`。角色默认值为 manager Astra/ultra/fast，executor 与 planner Sol/high/standard。接入者仍须确认自己的账户实际可用。设置应在新实例启动前完成；已有服务不会因另一个 shell 改环境自动更新，也不要为改配置擅自重启他人的实例。

```sh
export QINGTIAN_MODEL="gpt-6-astra"
export QINGTIAN_REASONING="xhigh"
export QINGTIAN_SPEED="fast"
```

这只是一个可选配置示例，**不验证模型/账户可用性、不调用模型、不产生执行授权**。`QINGTIAN_POLICY_PATH` 可指向你自己保存的完整策略 JSON；建议用绝对路径，并从随包 `qingtian_engine/resources/policy.json` 复制结构后审查。没有覆盖时读取包内策略。空路径、非法 JSON、缺失必需策略字段或非法模型/effort 不能作为默认配置成功运行。

| 选择项 | 新任务/规划的优先级与边界 |
|---|---|
| model | 显式参数 → `QINGTIAN_MODEL` → 路由/角色默认；必须是两个精确 ID 之一。 |
| reasoning | 显式参数 → `QINGTIAN_REASONING` → 路由/角色默认；六个值精确接受，不静默提高或降低。 |
| speed | 显式参数 → `QINGTIAN_SPEED` → 路由/角色默认；与模型/推理独立选择。 |
| 可选 Codex Planner | 使用同一模型/推理配置入口；仅 `QINGTIAN_INTAKE_PLANNER=codex` 启用后才调用模型，仍需独立读取范围授权。 |
| 执行与续接 | 新 run 保存七个不可变目标字段：model、reasoning、speed、worker type、owner session、branch、worktree。运行中和已有记录不因新默认值改变。 |

真实执行还要求 `QINGTIAN_CODEX_CAPABILITIES` 指向使用者人工审查的 schema-2 私有清单。缺失、过期、本机身份/CLI/目录来源失配或所选模型未列出时失败闭合。先用只读 `qingtian capabilities status` 核对；草稿准备、24 小时时限和人工启用步骤见 [Codex 本机能力清单](CODEX-CAPABILITIES.md)。该清单是本机 advertisement，不是账户权限、额度、实际 served tier 或执行成功证明。

旧 run 的迁移字段可能为空，表示**未知历史**。若没有完整的七字段不可变准入快照，resume 直接拒绝；不从任务列、当前默认或当前环境猜测，不补写旧行。要继续，应保留原历史并创建新的明确派发。速度字段/本地命令记录也不是提供方服务等级、耗时或额度回执。

不接受任意模型 ID，也不接受 `none` 或 `minimal`。格式和本机清单接受不表示账户可用。提供方拒绝、无额度或 CLI 失败应保持失败/阻塞事实，不自动换模型、改速度或降低推理重试。

CLI/HTTP/intake 的具体可用显式字段以各自接口合同为准；没有暴露某一参数的入口仍按环境与路由/角色默认选择。只查看配置时对照 `/api/dashboard` 的 `policy`、任务详情、准入回执和最新 run 的七字段快照；这些是选择/记录，不是 provider 回读或真实执行验收。

## 接入自己的知识库

`qingtian-kb` 随包安装。把知识工作区放在源码目录外；下面的 `--workspace` 指定获准采集的来源仓库：

```bash
mkdir -p /absolute/path/to/private-knowledge
cd /absolute/path/to/private-knowledge
qingtian-kb init --workspace /absolute/path/to/your-repo --project sample
qingtian-kb plan
```

先审查生成的 `config/sources.json` 和采集计划，确认来源范围后再执行：

```bash
qingtian-kb ingest
qingtian-kb validate
qingtian knowledge configure --root /absolute/path/to/private-knowledge
qingtian knowledge status
```

Obsidian 可打开 `vault/`；也可以直接维护 Markdown。需要 PDF 提取时，从源码目录安装 `python -m pip install -e '.[pdf]'`。`knowledge configure` 仅写配置，不采集或查询；`knowledge status` 的 `queried=false` 不能证明检索成功。真实 Provider 调用要另留回执。

Worker 默认只使用符合合同的 approved 引用；候选和历史不构成当前事实或执行授权。Provider 不可用会记录 `knowledge.unavailable`，不能当成检索成功。`qingtian knowledge disable` 停止检索并保留知识文件。知识正文、索引、来源注册表与凭据不进入公开仓库。

## 日常启动和停止

```bash
qingtian --workspace /absolute/path/to/authorized-workspace start --port 8767 --mode manual --open
qingtian status --port 8767
qingtian cancel TASK_ID
qingtian stop
```

`cancel` 仅在需要停止某个已派发任务时执行。`stop` 按数据目录定位控制台服务，不接受 `--port`，也不取消独立 Worker。`start --foreground` 可用于前台诊断。若要切换 auto，应核对现有实例和任务授权后，停止对应服务，再用同一数据目录显式 `start --mode auto`。

可选模型 Planner 通过 `QINGTIAN_INTAKE_PLANNER=codex` 开启；它读取启动 `--workspace`，不受项目注册表约束，且会调用模型。默认确定性规划不调用模型。更多运维与故障分流见 [运维手册](OPERATIONS.md)。

## 继续阅读

[架构](ENGINE-ARCHITECTURE.md) · [能力与验收](ENGINE-CAPABILITIES.md) · [API](ENGINE-API.md) · [FAQ](FAQ.md) · [术语](TERMINOLOGY.md) · [迁移](MIGRATION.md) · [演示](DEMO.md) · [旧版归档](legacy/README-v0.4.md)
