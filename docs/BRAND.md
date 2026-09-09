# 擎天 AI：任务有执行、有证据、有交接

擎天是一套可直接启动的本地 AI 任务引擎：团队提供目标、授权仓库与验收环境；擎天组织职责和依赖，在独立 worktree 调用真实 Codex Worker，记录事件、失败与证据，结果返回看板和报告。

![真实能力全景](diagrams/engine-overview.png)

[可编辑白板](diagrams/engine-overview.excalidraw)

## 四个价值

- **任务不只存在于对话里。** task/run/session/event/evidence 分开持久存储。
- **分工不靠拟人承诺。** coordinator、frontend、backend、qa 表示职责路由；真实执行看 run 和证据。
- **执行与验收分开。** 退出码不等于通过，证据不足停在验收阶段。
- **知识人机共建。** Markdown/Obsidian 可读、索引可重建；Worker 按合同读引用，不把旧文档当新任务。

![执行与回执](diagrams/engine-lifecycle.png)

[可编辑白板](diagrams/engine-lifecycle.excalidraw)

`qingtian quickstart --open` 是真实空工作台；`qingtian tour --port 8767 --open` 是同一引擎的合成状态演示。实际执行需注册项目、Codex 登录，真实知识和验收不能由演示代替。

已有本机执行、证据门、有界恢复、协调锁、SSE 和知识接口。视觉基线平台、设备农场、企业认证、跨机器调度与第三方工作流需要接入或建设，不标成已实现。

0.5 从实际运行引擎通用化提取，不复制业务资料、旧任务、会话或知识正文。默认 manual，自动调度明确开启。

[架构](ENGINE-ARCHITECTURE.md) · [能力与测试角色](ENGINE-CAPABILITIES.md) · [快速开始](../README.md)
