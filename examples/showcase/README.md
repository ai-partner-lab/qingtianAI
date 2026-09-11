# 真实引擎状态流转录像示例

真实引擎 · 合成示例 · 非生产执行。

本示例用当前 `qingtian_engine`、新建的独立临时 SQLite 库、真实 service 方法与 HTTP / SSE 看板，演示 32 条通用需求的状态流转。不是截图动画，不会修改产品 DOM 来伪造状态，不会调用模型、派发 Codex Worker、触发 manager-entry、登记业务仓库或部署。

本地 fixture 只生成带 `synthetic=true` 的 JSON 清单，并验证是否包含 10 项。故意生成 8 项时，真实证据门拒绝完成；补齐 verified artifact 后才由真实 reconciler 收口。它验证的是演示清单，不是业务需求、模型质量或 C 端 E2E。

## 产物与文件

- `scenario.json`：SC01–SC32 稳定示例 ID、通用标题和最终展示状态；每次运行生成新的真实引擎 task ID，并保留映射。
- `engine_bridge.py`：新建 `/tmp/qingtian-showcase-*` 独立库、随机 loopback 端口、manual 服务；通过 service 方法创建/流转，HTTP 回读核验。退出只停止自己创建的 PID，保留临时库作审计。
- `record.cjs`：录制未经修改的原生 UI，等待原生 SSE 将卡片投影到正确列；保存实际事件时间与镜头标记。
- `verify_recording.py`：只读打开本轮临时库，核对 32 条完整状态轨迹、证据先于 DONE、fixture SHA、无 Worker run。
- `render.cjs`：中文章节、始终可见的演示标识、详情裁切放大、全片和精华片。只后期编辑画面，不改变状态事实。
- `qa_media.cjs`：完整解码、黑屏检测、每 10 秒与详情镜头抽帧、输出哈希比对。
- `check_playback.cjs`：通过自有随机 loopback 文件服务检查 MP4 能实际播放和 seek，结束即停止该服务。

输出留在仓库外的新目录：原始 WebM、1920×1080 / H.264 / 30 fps MP4、整板截图、章节、时间码、service/SSE 事件、task ID 映射、fixture 产物与验证报告。不要把私有运行目录、数据库或大视频提交到 Git。

## 依赖

Python 3.11+（能导入本仓库即可）、Node.js、Playwright、Chromium、`@napi-rs/canvas`、FFmpeg / FFprobe（含 libx264）、可用中文字体。已验证组合：Python 3.12.14、Playwright 1.62.1、canvas 0.1.100、FFmpeg 9.0.1、macOS Hiragino Sans GB。中文字体加载失败即停止，不静默换成缺字字体。

使用者可复用自己的已安装依赖；不需要添加产品 runtime 依赖。Node 包需可被 `require()` 找到，或设置 `NODE_PATH` 指向现有依赖目录。

## 复演

以下命令从仓库根目录运行。输出变量必须指向仓库外新目录；重复使用已有 `timeline.jsonl` 的目录会被拒绝。`SHOWCASE_CHROMIUM` 可省略以使用 Playwright 管理的浏览器；设置时须为 Chromium 可执行文件。`PYTHON` 可指向你的 Python 虚拟环境。

从源码包或独立 allowlist bundle 解压后，在包含 `pyproject.toml` 的根目录运行相同命令即可；wheel 不分发本示例与品宣媒体。解压副本通常没有 `.git`，此时 `source_commit` 明确记录为 `null`，以 `source_sha256` 的逐文件摘要标识实际引擎输入，不借用上级 Git 仓库的提交。Git 命令不可用或提交不可确认也记为 `null`；这不代表旧录像已由新源码重新录制。

```sh
SHOWCASE_OUT="$(mktemp -d /tmp/qingtian-showcase-output-XXXXXX)"
node examples/showcase/record.cjs --output "$SHOWCASE_OUT" --python "${PYTHON:-python3}" --pace 1
python3 examples/showcase/verify_recording.py "$SHOWCASE_OUT"
node examples/showcase/render.cjs --input "$SHOWCASE_OUT" --ffmpeg ffmpeg --ffprobe ffprobe --font /path/to/chinese-font.ttf
node examples/showcase/qa_media.cjs "$SHOWCASE_OUT"
node examples/showcase/check_playback.cjs "$SHOWCASE_OUT"
```

`--pace 0.01` 只用于快速逻辑彩排；最终成片应使用 `--pace 1`。正式编排墙钟终点为 221.82 秒，原始 WebM 容器时长为 223.32 秒（含关闭等待/尾帧），主片 229.5 秒（片头重复展示后段真实总览 8 秒），精华 76 秒。所有段落播放速度均为 1×，短片只跳切不加速。实际时长会受本机响应时间影响。

若只重新剪辑已有录像，仅运行 `render.cjs` 和 `qa_media.cjs`，不会重新开启引擎或流转任务。剪辑会覆盖该次输出目录内生成的 MP4、字幕层、poster 与媒体验证文件；原始录像、状态事件与数据库保持不变。

## 状态与边界

八列分别为收件箱、执行中、等待中、已暂停、仅规划、验收中、已完成、已取消。PAUSED 是用户暂停 WAITING 的展示投影；PLAN_ONLY 是 analysis-only 授权策略的展示类别；PLANNED / QUEUED 归入收件箱，不能把展示标签误称成新增 raw 状态码。

默认终局数量依次为 `2 / 2 / 7 / 2 / 2 / 5 / 10 / 2`。并非所有任务都完成：有意保留等待、暂停、规划、缺证据验收、执行中和取消分支。

中断、外部资料、用户选择和恢复授权都是明确标注的合成信号。没有实际额度消费或恢复，没有宿主跨会话通信接线，也没有验证入口适配器或真实模型派发。技术详情的“模型”字段来自产品默认配置；它不是本次执行证据，本片不调用那个模型。

该手动编排器通过现有 Python service API 驱动状态，不宣称是全自动调度演示。RUNNING 使用真实外部心跳记录标记本地 fixture 活动；它不是 Codex managed Worker。引擎不受骗地声称有 Worker run：最终审计要求 runs 表为空。

## 审计文件

- `recording.json`：原始录像起点、所有命令、章节、镜头及 screenshot 时间。
- `timeline.jsonl`：每次 service 调用后 HTTP 回读的 raw/display 状态及 event cursor。
- `service-events.json`、`sse-events.json`：真实持久化事件及浏览器收到的原生 SSE。
- `state-trajectories.json`、`post-recording-audit.json`：完整预期轨迹与实际轨迹对账，完成因果检查。
- `verification.json`：引擎源码 SHA、commit、独立库/端口/PID、fixture SHA、授权审计。旧录像中的 `event_count` 曾保存 cursor 上界；实际行数以 `post-recording-audit.json.service_events` 为准，后续脚本已区分两者。
- `media-verification.json`：原始/成片时间映射、剪辑范围、时长、ffprobe、输出 SHA。
- `timecode-map.json`：编排命令到主片时间码、稳定示例 ID / 本轮引擎 task ID、服务事件 ID、fixture evidence 的连接；墙钟对齐容差 1 秒，不声称逐帧事件到达精度。
- `qa/`：每 10 秒抽帧、关键详情帧、contact sheet、完整解码日志与媒体 QA。

保留失败和彩排记录，不把失败轮次当成最终验收。发布前还应人工看抽帧和字幕，核对没有业务标题、真实任务 ID、私有仓库路径或参考图泄露。公开整板图是本轮合成数据在真实产品 UI 的截图，加了明确的编辑性演示标识；不是对用户私有截图重绘。

服务事件数与 SSE 消息数不是同一口径：正式样例有 307 条服务事件，49 条 SSE 消息的变化列表含 294 个唯一事件 ID。独立审查发现现有引擎先查 changes、后查 dashboard 并以前进后的 version 更新游标，造成 13 个事件未出现在变化列表；不能解释为“同包都收到了”。最终 SSE 全量 dashboard 的 32 项 raw/display 与最终导出一致。本片每步通过 HTTP 全量回读和原生 UI 列位置对账，不声称逐事件可靠投递已通过。该 P2 核心问题未在本示例中修改，需单独修复/验收。
