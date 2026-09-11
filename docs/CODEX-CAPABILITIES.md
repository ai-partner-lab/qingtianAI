# Codex 本机能力清单

真实 Codex Worker 必须显式接入。Qingtian 不从模型名称、登录状态、历史
run 或策略默认值猜测当前安装可用的模型组合。执行前必须有一份由使用者人工
审查、仍在有效期内并与当前本机安装一致的 capability manifest；缺失、过期、
来源变化或目标组合未列出时均失败闭合，不自动换模型、降低推理强度或改变速度。

本机制只证明一份**本机广告清单**经过了有界审查。它不证明账户有权使用模型，
不证明额度、区域、服务层或 `fast` 实际可用，也不证明 provider 最终按所选组合
完成了执行。真实成功仍以 Codex 自身回执和项目验收为准。

## 查看当前状态

```bash
qingtian capabilities status
```

`status` 是只读命令。它读取 `QINGTIAN_CODEX_CAPABILITIES` 指向的 reviewed
manifest，并核对 schema、有效期、当前 OS 安装身份摘要、有效用户、
`CODEX_HOME`、Codex CLI 路径/版本/二进制摘要和当前 `models_cache` 来源摘要。
成功只表示清单可用于本机执行准入；失败会说明下一步操作，但不会创建、更新、
启用或删除清单，也不会启动执行任务、app-server、会话或调用模型。已有清单时
会执行只读的 CLI `--version`；缺清单时在任何本机探测之前拒绝。

## 生成待审草稿

显式选择一个位于源码、数据目录和发布产物之外的私有新路径：

```bash
qingtian capabilities prepare --output /private/location/draft.json
```

`prepare` 只接受不存在的新文件，不覆盖已有草稿。它读取当前操作系统安装身份并
仅保存其 SHA-256 摘要、有效用户 ID、`CODEX_HOME`、Codex CLI 的 `--version`
结果与二进制 SHA-256，以及当前 `CODEX_HOME/models_cache.json`。它不读取凭据、
主会话或对话，不探测账户权限，不调用模型，也不自动启用生成的文件。

草稿使用 schema 2，包含本机路径与安装指纹，属于本机敏感资料，不应提交、打包、
贴入 issue 或放入共享日志。`observed_at` 必须精确等于模型目录的
`catalog.fetched_at`；`expires_at` 最多晚 24 小时。若目录时间已过期，命令拒绝
生成看似新鲜的草稿，使用者应先通过 Codex 自己的受支持流程刷新目录，再重新准备。

## 人工审查与启用

1. 在私有位置打开草稿，确认安装身份、有效用户、路径、CLI 版本/摘要、目录来源、
   时间边界和模型列表确实属于本次目标环境。
2. 确认只列出 Qingtian 允许的精确模型 ID：`gpt-5.6-sol`、`gpt-6-astra`。
   清单不应被扩写为没有在当前目录中广告的组合。
3. 保留已审查文件的原始字节，然后在启动 Qingtian 的同一环境显式启用。
   将下例路径替换为实际已审文件的私有路径；工具不会自动把 draft 改名为 reviewed：

   ```bash
   export QINGTIAN_CODEX_CAPABILITIES=/private/location/reviewed.json
   qingtian capabilities status
   ```

修改文件、CLI、二进制、`CODEX_HOME`、有效用户、OS 安装身份或模型目录后，旧清单
会失配；到期后也会拒绝执行。重新运行 `prepare` 到另一个新文件并重新人工审查，
不要延长旧时间、猜测缺失字段或把草稿路径自动改成启用路径。

## 与执行参数的关系

新执行只接受 `gpt-5.6-sol` 或 `gpt-6-astra`，推理强度只接受
`low`、`medium`、`high`、`xhigh`、`max`、`ultra`，不做 clamp。速度是独立参数，
只接受 `standard` 或 `fast`。角色默认值为：

| 角色 | 模型 | 推理 | 速度 |
|---|---|---|---|
| manager | `gpt-6-astra` | `ultra` | `fast` |
| executor | `gpt-5.6-sol` | `high` | `standard` |
| planner | `gpt-5.6-sol` | `high` | `standard` |

选择优先级是显式参数 > 环境变量 > 路由/角色默认值。环境变量分别为
`QINGTIAN_MODEL`、`QINGTIAN_REASONING`、`QINGTIAN_SPEED`。默认值只是新选择，
不会修改正在执行的 run 或旧 run 参数；能力清单也不授权恢复旧任务。

经新准入创建的 run 会保存 model、reasoning、speed、worker type、owner session、
branch、worktree 七个不可变目标参数。旧历史若没有完整不可变快照，resume 必须拒绝；
不猜、不回填、不覆盖既有 run 列。要继续工作，应在保留旧记录的前提下按当前授权
创建新的明确任务/派发，而不是伪造历史执行目标。

## 无凭据路径

`qingtian start`、`doctor`、`selftest`、`tour` 与 Knowledge Hub 的离线流程仍可在
没有 Codex 凭据和 capability manifest 时使用；它们不因此获得真实模型执行能力。
`quickstart` 默认只启动 manual 控制台，不创建 manager entry。确需尝试真实入口时
显式加 `--manager-entry`；`--skip-manager-entry` 仅为兼容保留，不能与 opt-in 同时使用。
