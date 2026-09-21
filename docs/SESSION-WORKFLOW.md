# Portable session workflow

Qingtian can describe and record a session workflow without pretending that a
host-native Codex bridge exists. Start with the read-only checks:

```sh
qingtian doctor
qingtian session-workflow status
```

The profile reports source capabilities and current-host prerequisites. It does
not check credentials, validate a private capability manifest, contact an
app-server, start a service, create a task or copy another machine's runtime
database. Project registrations, Codex home, sockets, capability evidence and
credentials remain machine-local configuration.

Executable discovery follows the same host configuration as manager entry. If
`QINGTIAN_CODEX_BIN` is absent, Qingtian looks for `codex` on `PATH`. A nonempty
override may be an absolute executable path or a command name resolved on
`PATH`; an empty, relative-path or non-executable override is an explicit
failure and does not silently fall back to another `codex`. Status output only
reports presence and never prints or executes the selected path.

The default is `manual`. Message preparation, transport delivery, recipient
acceptance, external execution start, QA, task completion and deployment are
separate facts. A prepared message always has `native_delivery: false`; an
outbox delivery acknowledgement is not a recipient acceptance receipt. The
manual watchdog may record overdue/lost facts but returns zero dispatches and
zero resumes.

## Prepare a handoff message

```sh
qingtian session-workflow prepare \
  --body-file /path/to/reviewed-message.txt \
  --source-role 'Manager' \
  --source-session-name 'Release coordination' \
  --source-task-id TASK_ID \
  --source-ref 'task:turn-reference'
```

This prints an unsent JSON envelope. Only its `body` is suitable for copying to
an already-authorized host transport. Audit fields remain outside the body, but
the complete JSON may still contain private references and must remain private.
Conflicting origin labels are rejected. Display-name filtering is not content
DLP: review both body and audit references before sending. Do not include
secrets, private absolute paths, account identifiers or credential-bearing URLs.

`qingtian lifecycle TASK_ID` reads the current revision. Mutations use
`qingtian lifecycle TASK_ID ACTION --payload JSON` and require the current
`expected_revision`, a stable `idempotency_key`, the actual caller's `actor`,
and a traceable `source_ref`. Re-read on a revision conflict; do not blind-retry
with a new key. Handoff offer, acceptance, linked execution and finish remain
distinct records. These commands only update the local ledger; they do not
deliver a native Codex message or authorize execution.

## New-machine acceptance boundary

Use this order on every new machine, keeping each result separate:

```sh
qingtian doctor
qingtian capabilities status
qingtian manager-entry guide --workspace /path/to/workspace --data-dir /path/to/engine
qingtian manager-entry inspect --workspace /path/to/workspace --data-dir /path/to/engine --thread-id EXPLICIT_ID
qingtian manager-entry instructions
# Then perform one newly authorized manual handoff and record its actual receipts.
```

`doctor` exit zero means only that the offline dashboard prerequisites passed;
its nested workflow profile still lists unverified items. `capabilities status`
validates reviewed local advertising, not account entitlement or an actually
served model/tier. `guide`, `inspect`, and `instructions` are read-only setup
steps: their successful exit does not prove metadata synchronization, an
effective role, native delivery, recipient acceptance or workflow readiness.
The fresh manual handoff must independently preserve offer, delivery,
acceptance, linked execution and completion evidence.

### 新电脑直接照着做

先跑 `doctor`，再核对 `capabilities status`；然后用 `manager-entry guide`
拿到本机命令，以明确的会话 ID 执行 `inspect`，最后阅读 `instructions`。
这些命令显示成功也只代表各自那一步成功，不代表整套工作流已经可用。不要复制旧
电脑数据库，不要自动打开 native bridge，也不要修改全局 Codex 配置来绕过失败。

第一次真实交接仍由人明确发送，例如正文首行：

```text
【运维｜发布验收】
请按本次已确认范围核对候选版本，并分别回传接单、执行与验收证据。
```

先用 `session-workflow prepare` 生成并检查正文，再通过当前电脑上已经授权且实际
可用的宿主工具发送；没有该工具就保持“已准备、未发送”。交接 offer、发送回执、
接单、开始执行、完成和部署验证必须分开登记，不能互相代替。

Before claiming a new machine works, verify its exact Python/platform, Codex and
Git executables, private project registry, reviewed capability manifest, Codex
login/model availability, manager-entry protocol and metadata readback,
effective role instructions, and a fresh manual handoff journey.
`workflow_ready: false` and `native_delivery_ready: false` are intentional until
those facts have separate evidence. A source-level unit test, installation
receipt or successful `doctor` is not cross-machine acceptance.
