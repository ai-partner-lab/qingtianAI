# 演示、上手与重置

```bash
qingtian selftest
qingtian quickstart --open
```

selftest 在临时库运行 8 项无凭据检查，明确返回 `model_called=false`、`business_acceptance=false`。quickstart 启动真实空库；“能力与上手”支持下一步、上一步及展开能力。浏览说明不创建/推进任务。

## 合成 tour

```bash
qingtian tour --port 8767 --open
```

同一引擎、临时库、5 张明确标记的合成状态卡；不启动 Worker。

**重置：Ctrl+C 结束 tour，再运行。** 每次新临时库，退出清理，不碰真实数据。真实工作台没有“一键抹掉任务”；新一轮验收请选新私有 `--data-dir`。

## 旧动画

```bash
qingtian-lab demo-web --port 8787
qingtian-lab demo-check --capability api-e2e
```

这是独立 0.4 七步教学实验，不证明真实 Codex、外部恢复或业务 E2E。[旧演示说明](legacy/DEMO.md)
