# 真实引擎接入手册

[安装](../README.md) 后用 `qingtian quickstart --open` 启动空任务控制台。`selftest` 只跑隔离合成检查，不调用模型；缺 Git/Codex 不影响看板启动。

## 状态与进程

全局选项放在子命令前：

```bash
qingtian --data-dir /absolute/private/engine --workspace /absolute/authorized/workspace init
qingtian --data-dir /absolute/private/engine --workspace /absolute/authorized/workspace start --port 8766 --mode manual --open
qingtian --data-dir /absolute/private/engine status --port 8766
qingtian --data-dir /absolute/private/engine stop
```

同数据目录有实例锁；端口/模式/目录身份不一致会拒绝复用。不要复制另一台电脑的 PID、活动库或旧任务启动。导入要单独明确操作，不等于恢复授权。停止控制台不会自动取消独立 Worker；取消任务用 `qingtian cancel TASK_ID`。

manual 可写、允许明确执行，只关闭后台自动领取/重试/补证。auto 必须明确开启，并先用一次性项目验收。

## 项目与执行

```bash
qingtian project register sample --repo /absolute/your-repo --base dev --role backend --role qa
qingtian task add --title "Add one small test" --project sample --owner backend --worker cli
qingtian dispatch TASK_ID --prompt-file /absolute/private/instructions.md
qingtian task show TASK_ID
```

注册已有 Git 仓库和开发基线；受保护主分支不能作为基线。工作在独立 worktree，不相关的母仓库未提交修改不应混入。可选 scope 将 Codex 工作目录设为工作树内对应子目录，并校验真实路径不越界；它不是独立权限围栏，实际权限仍依赖授权任务、Codex sandbox 与本机权限。

`project remove sample` 只删注册，不删仓库。私有注册表默认 `config/projects.local.json` 位于数据目录；可用绝对 `QINGTIAN_PROJECTS_CONFIG` 覆盖。

## 规划读取范围

默认确定性 Planner 读取文本与附件元数据，不声称有 OCR/图片语义理解。

显式 `QINGTIAN_INTAKE_PLANNER=codex` 会调用真实模型规划，使用 `--workspace`（默认启动 cwd）。**项目执行 allowlist 不约束该规划读取范围**。请选经授权的干净 workspace；不要从含不应发送给模型的资料目录开启。规划用 read-only/ephemeral；账户和数据政策由使用者负责。

代码执行需要安装/登录 Codex、可用模型、high 以上推理。不要把密钥写进 prompt、仓库或工单。[官方执行说明](https://learn.chatgpt.com/docs/non-interactive-mode)

## 验收与反馈

退出码 0 不等于 DONE。用独立检查确认真实产物后，才明确登记 verified：

```bash
qingtian task evidence TASK_ID test "your private verification receipt locator" --verified
qingtian task evidence TASK_ID commit "your verified commit hash" --verified
qingtian reconcile
qingtian report
qingtian feedback --consumer maintainer --peek
```

这些值是占位，不能原样当通过证据。`--verified` 是操作者的信任声明，不会自动执行测试。部署/浏览器等 profile 有其他要求，真实脚本由项目提供。

## 知识与验收

初始化并审查自己的知识工作区后：

```bash
qingtian knowledge configure --root /absolute/private/knowledge
qingtian knowledge status
qingtian knowledge disable
```

配置默认在数据目录 `config/knowledge.local.json`，可用绝对 `QINGTIAN_KNOWLEDGE_CONFIG` 覆盖。内置模块无需知识目录启动脚本；配置操作不采集/查询，关闭不删除资料。

接入验收使用新的一次性 Git 项目，记录包版本、环境、新 task/run/session、退出码、Git 产物和独立测试。fake process、合成 tour 和旧动画不能代替真实 CLI E2E。

支持 macOS/Linux POSIX；原生 Windows 不支持，WSL2 应独立验收。当前没有团队身份认证/RBAC，禁止当作公网服务部署。
