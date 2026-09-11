# 常见问题（真实引擎）

### 1. quickstart 后为什么不自动执行？

`quickstart` 固定启动 manual。新数据目录为空库，旧目录保留任务；manual 不在后台领取任务，但允许明确的 `qingtian dispatch` 和实施 intake。自动调度通过 `start --mode auto` 显式启用。

### 2. 关闭终端后为什么控制台还在？

`quickstart` 和普通 `start` 启动后台服务。使用相同数据目录运行 `qingtian stop`；它不接受 `--port`。前台运行可用 `start --foreground`。

### 3. run 退出码为 0，为什么任务还没 DONE？

run 是一次执行尝试。任务完成还要满足 profile 对 verified 证据的要求；代码通常需要 commit/test，部署还需 deploy/smoke。应核对真实产物后登记回执，不能把模型自述或退出码直接当验收。

### 4. `analyze` 和 `implement` intake 有何区别？

`analyze` 关联任务持久标记为 `analysis-only`，不会启动实施 Worker。`implement` 是明确执行意图，在 manual 下也可能派发。`implement_and_deploy_dev` 还涉及开发部署。客户端应显式使用这些 intent 值，避免依赖模式缺省值。

### 5. 没有 Idempotency-Key 会怎样？

要按入口区分。intake 无显式键时使用文本、intent、advanced 生成回退键，但不包含附件字节；相同文字搭配不同附件可能被复用。每个逻辑收件请求应使用稳定唯一键。直接 `POST /api/tasks` 不转发 header/body 幂等键，超时后不要盲目重放；CLI `task add` 也没有幂等键参数。

### 6. manual 是只读模式吗？

普通 manual 可写，健康响应为 `read_only=false`。它会核对已有事实和证据，但不在后台启动恢复或补证 Worker。合成 `tour` 才是额外限制写入的只读演示。

### 7. 旧数据库可以直接导入继续跑吗？

0.4 实验室数据库与 0.5 引擎不同。`qingtian import` 读取显式选择的 TASKS/governance 台账，不是旧 SQLite 转换器；导入任务属于 `reference-only`，实施需要另建明确授权任务。详见 [迁移](MIGRATION.md)。

### 8. 知识配置 enabled=true 就代表检索通过了吗？

不是。`knowledge configure/status` 不采集、不查询，`queried=false`。接入方需要初始化并维护自己的知识库，再用实际 Provider/Worker 回执验证检索。`knowledge disable` 停止检索并保留库文件。

### 9. selftest 检查什么？失败如何处理？

它使用临时合成数据检查状态、幂等、证据门、manual 不领取、持久化、SQLite 完整性和看板投影，共 8 项。不启动 HTTP、不测试附件、不调用模型或外网。失败时先运行 `doctor`，记录 Python/包版本和失败检查 ID 或异常，在隔离目录定位；不要把它的通过当成 Codex 或业务验收。

### 10. stop 后为什么 Worker 仍在运行？

Worker 是独立进程组。需要取消时，在同一数据目录执行 `qingtian cancel TASK_ID`，再看任务、run 和实际进程状态。取消、停止服务、删除数据是不同操作。

### 11. `status --json` 或 `health` 为什么报参数错误？

当前 CLI 只有输出 `running/stopped` 的 `status`。结构化健康信息来自 `GET /api/health`，详见 [运维手册](OPERATIONS.md)。`status` 返回 running 也不单独证明实例身份相符。

### 12. 重复添加证据并加 `--verified`，为什么仍未验证？

同一 task/kind/value 使用插入去重，不更新已有记录。返回 `inserted=false` 不代表验证成功。完成独立复核后，登记包含新复核报告引用的证据，并再次检查任务详情。

### 13. scope 能防止 Worker 读取其他文件吗？

scope 设置工作树内的 cwd 并检查路径边界，不是独立权限沙箱。Codex 权限和本机访问控制仍需接入方配置；可选 Planner 读取启动 workspace，项目 scope 不约束它。

### 14. qingtian-lab 还能使用吗？

可以，作为旧版教学入口；七步动画、旧 schema 与旧数据库属于独立实验室。当前默认引擎的体验演示使用 `qingtian tour`，两者都不能替代真实执行验收。

### 15. 对大管家说一次“继续”，能把额度也恢复吗？

不能。这个场景是大管家核实原中断、当前可执行条件、原任务及授权后，通过宿主工具续接剩余工作；不是补额度或绕限流。额度仍不足时保持阻塞，不自动充值、消费 reset、静默换模型或无限重开会话。`doctor` 不检测额度，`recover` 也不是通用账户恢复命令。[主打场景与条件](SCENARIOS.md#s01)

### 16. 装好公共 CLI，就有全自动跨会话大管家了吗？

没有这个保证。公库提供状态、Worker、证据门和有界恢复等原语；跨宿主读任务、发续接消息与通知仍需宿主工具或接入适配。公共任务详情已实现结构化会话绑定的导航入口/ID 复制，隔离验证只到生成链接，未打开真实原生目的地。规则提交和置顶元数据不等于有效角色已经回读，`workflow_ready=false`；首次账户接入与桌面工作流也不因 UI 可用而通过。[能力层级](SCENARIOS.md#能力层级)

### 17. 重启后继续工作，会把已经暂停的发布也恢复吗？

不应如此。先恢复正确实例的 manual 服务，再确认有效任务和明确允许续接的部分；“环境好了”不解除发布暂停。原任务无法安全区分本地实施与部署时先澄清，不能更改验收合同掩盖限制。外部流水线的暂停还需要接入方落实，不存在本文承诺的全局停发开关。[暂停与续接](SCENARIOS.md#s08)

### 18. 实现已经交付，但供应商资料或验收证据没到，该怎么报？

分列已交付产物、外部未接入部分、缺失证据、负责方和需要的权限。外部 E2E 未跑就写未测试，不能拿合成替身或提醒记录当供应商已完成。公库有依赖与等待原语，但不会因此自动把任意任务重构成新的父子任务；拆分交付范围是协调决定。[外部等待](SCENARIOS.md#s05) · [缺证据](SCENARIOS.md#s06)

### 19. 上下文满、换电脑或已批准方案转交，必须从头描述吗？

可靠检查点和批准记录仍在时，不必重复描述；大管家应交接已有修改、剩余工作及权限。缺少完整授权或任务关联时仍要问必要问题，不能凭摘要猜。新任务创建、跨机传输和生成工具费用分别受宿主及项目授权约束；旧知识与历史不变成新执行许可。[失联交接](SCENARIOS.md#s03) · [换环境](SCENARIOS.md#s10) · [方案交接](SCENARIOS.md#s11)

### 20. 场景回放、执行进度或 item.completed 能证明任务完成吗？

不能。文字回放说明操作方法，tour 是合成数据；进度百分比、`item.completed` 和 run 退出只能表示局部执行事实。需要对应版本的产物、实际检查与独立验收证据；失败后的返修有边界，不能删除正式测试或无限重试。[演示标签](DEMO.md#开场先贴标签) · [有界返修](SCENARIOS.md#s12)

[接入手册](ADOPTION.md) · [协作场景](SCENARIOS.md) · [运维](OPERATIONS.md) · [API](ENGINE-API.md) · [术语](TERMINOLOGY.md)
