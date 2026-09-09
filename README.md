# Qingtian AI · 擎天真实任务引擎

[![CI](https://github.com/ai-partner-lab/qingtianAI/actions/workflows/ci.yml/badge.svg)](https://github.com/ai-partner-lab/qingtianAI/actions/workflows/ci.yml) · [Apache-2.0](LICENSE) · Python 3.11+ · macOS / Linux

**0.5 从实际运行的本地引擎重新提取。默认入口不再是早期演示。**

擎天将请求、任务、执行进程、证据与知识引用放进可追溯的本地工作台：收件箱、规划与依赖、独立 Git worktree、真实 Codex Worker、核对验收、报告反馈。角色是职责路由，不是人物动画；模型说“完成了”也不等于验收通过。

![真实引擎架构](docs/diagrams/engine-overview.png)

[可编辑 Excalidraw](docs/diagrams/engine-overview.excalidraw) · [架构](docs/ENGINE-ARCHITECTURE.md) · [能力与测试角色](docs/ENGINE-CAPABILITIES.md) · [HTTP/API](docs/ENGINE-API.md)

## 三分钟启动

Python 3.11+（建议 3.12+）、macOS 或 Linux。默认启动不需要 npm、Docker、模型凭据或 Obsidian。

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

打开 [本地控制台](http://127.0.0.1:8766/)。默认**空任务库 + manual**；不导入旧任务，不自动启动 Worker。点击“能力与上手”逐步查看接入方法。

- 默认数据目录是 `~/.local/share/qingtian/engine`，不写源码或安装目录；支持 `QINGTIAN_ENGINE_HOME` 或全局 `--data-dir`。
- `qingtian stop` 停止该数据目录的控制台，不等于取消已派发的独立 Worker。
- `qingtian tour --port 8767 --open` 用临时库展示**同一引擎**的合成卡片；Ctrl+C 退出清理，不调用模型。
- `qingtian selftest` 无凭据核验状态、幂等、证据门、持久化和 manual 不自动派发。
- 当前没有多用户认证，不要向公网开放端口。

## 执行自己的真实项目

控制台开箱即用；代码执行需要使用者安装并登录 Codex、注册授权仓库和已有开发基线：

```bash
codex login
qingtian project register sample --repo /absolute/path/to/your-repo --base dev --role backend
qingtian project list
qingtian task add --title "Add a small regression test" --project sample --worker cli --owner backend
```

将返回的新 task ID 与你编写的任务说明文件传入：

```bash
qingtian dispatch TASK_ID --prompt-file /absolute/path/to/instructions.md
qingtian task show TASK_ID
qingtian feedback --consumer maintainer --peek
```

缺项目映射会阻止真实执行，不猜旧电脑路径。每个代码任务创建独立 worktree/分支；不自动合并或推送母仓库。`main/master/origin/main/origin/master` 不能作为执行基线。scope 注册字段的实际边界见接入手册，不是权限沙箱。

默认策略是 `gpt-5.6-sol`、high 或以上，需要账户实际可用。执行采用 stdin + `codex exec --json`，不绕过沙箱。[官方非交互执行说明](https://learn.chatgpt.com/docs/non-interactive-mode)

manual 可写并允许明确 dispatch 或实施请求，不是只读。auto 会自动领取符合规则的任务，请先在隔离环境理解其范围。[接入手册](docs/ADOPTION.md)

## 当前能力

| 已提取实现 | 接入方提供 |
|---|---|
| SQLite task/run/session/event/evidence、幂等、父子依赖 | 当前目标、范围与验收规则 |
| 文本/附件 intake、确定性规划、通用职责路由 | 可选模型规划的授权 workspace |
| Codex 子进程、JSONL、PID/进程组、取消与 resume | Codex 登录、模型额度、仓库 |
| worktree、证据门、有界恢复、lease/fencing、检查点 | 真实测试、部署和独立验收 |
| SSE 看板、等待分类、24 小时报告、反馈游标 | 通知平台、团队访问层 |
| Obsidian 兼容 Markdown 知识库、只读 approved 上下文 | 自己的文档、来源审查与维护 |

不内建视觉像素门禁、设备实验室、Dify/Hatchet/OpenHands/Langfuse 集成或跨机器分布式调度。模型标签、角色名称与动画不能证明这些集成已完成。[完整能力和黑盒测试清单](docs/ENGINE-CAPABILITIES.md)

## 知识库：带走体系，不带走内容

`qingtian-kb` 随包安装。知识工作区放在源码之外：

```bash
mkdir -p /absolute/path/to/private-knowledge
cd /absolute/path/to/private-knowledge
qingtian-kb init --workspace /absolute/path/to/your-repo --project sample
qingtian-kb plan
# 先审查 config/sources.json 的来源范围，再采集：
qingtian-kb ingest
qingtian-kb validate
qingtian knowledge configure --root /absolute/path/to/private-knowledge
qingtian knowledge status
```

Obsidian 可以打开这里的 `vault/`，不用 Obsidian 也能编辑 Markdown。PDF 提取可在源码目录另装 `pip install -e '.[pdf]'`。

Worker 默认只取合格的 approved 引用。候选和历史不是当前事实，更不是新的执行授权；未知来源不能默认为可信。`qingtian knowledge disable` 关闭检索但不删除知识文件。知识模块文档见 [文档目录](docs/)。

## 验证与分发

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m build
qingtian bundle --root . --output release/qingtianAI.tar.gz
qingtian bundle-verify --bundle release/qingtianAI.tar.gz
```

Wheel 包含真实引擎、静态控制台、知识模块及显式 legacy 入口；源码包还包含文档、Excalidraw、接口和测试。CI 检查公开边界、Python 矩阵、离开 checkout 的 wheel 和源码包复验。[发布清单](docs/RELEASING.md)

公开仓库不含业务规则、知识正文、历史任务/会话、数据库、凭据、本机映射、prompt spool、worktree 或生产资料。生成的数据保留在接入方私有目录；内置扫描不是完整 DLP。

## 导航与旧版

[接入](docs/ADOPTION.md) · [架构](docs/ENGINE-ARCHITECTURE.md) · [能力](docs/ENGINE-CAPABILITIES.md) · [API](docs/ENGINE-API.md) · [演示/重置](docs/DEMO.md) · [品宣](docs/BRAND.md) · [安全](SECURITY.md) · [变更](CHANGELOG.md)

早期 `qingtian_core` 和七步动画保留为 **`qingtian-lab` / `qingtian legacy`**。它们的状态模型、schemas、examples 与真实引擎不同，不能互换数据库，也不是新引擎验收证据。[旧版归档](docs/legacy/README-v0.4.md)
