# Release notes / 发布说明

Canonical public release history for **codex-grok-orchestrator**.
The installable package and CLI remain **`grok-worker`**.

Package versioning details also appear in [CHANGELOG.md](../../CHANGELOG.md).

---

## 2026-07-19 — Lifecycle, isolation, and observability / 生命周期、隔离与可观测性

**Version:** `grok-worker` 0.4.2

**Upgrade from:** 0.3.0

**Public identity:** `MaxxxDong/codex-grok-orchestrator`

### English

This release promotes the verified 0.4 runtime used by the maintainer into the public repository.

**Dispatcher and capacity**

- Per-dispatcher OS-lock slot leases allow up to 10 active invocations for one explicit dispatcher ID without imposing a machine-global limit.
- Completion events support bounded 0–120 second waits, while read-only health inspection reports lifecycle, activity, progress, resource usage, and remaining lease time.
- Prompt-only research and repository-backed work now have explicit, separate source semantics.

**Adaptive lifecycle**

- `--timeout` is an inactivity lease renewed by managed Grok session events, bounded workspace activity, agent-log growth, and structured progress/result files.
- A separate 24-hour hard cap remains the default safety ceiling and can be changed or disabled at runtime.
- `grok-worker lease-set` changes idle and hard limits without restarting the active ACP session.

**Grok isolation**

- Every Worker clone derives a private managed `GROK_HOME` from the selected model profile; concurrent clones cannot overwrite one another's config.
- Provider credentials are resolved in memory and passed only to the child process; derived TOML contains no plaintext API key.
- User marketplaces, plugins, and Grok-level MCP servers fail closed after `grok inspect --json`.
- `[claude_compat] imported = true` prevents a repository-root `.mcp.json` from entering managed sessions while the fail-closed inspection remains authoritative.
- The canonical `Agents.md` remains linked into the managed profile.

**Execution policy and evidence**

- The stable Worker prompt permits at most 3 non-overlapping concurrent subagents; this numeric cap is prompt-enforced. `--no-subagents` remains the runtime hard-disable, and the lead Worker owns integration and the structured result contract.
- Dirty-source inclusion uses repeatable `--include-dirty-path PATH`; bare `--include-dirty` is refused when nonignored dirt exists.
- External success remains exactly three files: `changes.patch`, `worker.log`, and `verification.txt`.
- Result artifacts now record effective lease policy and observable token/cache metrics without claiming unavailable provider data.

**Upgrade**

- macOS and Linux are supported. Native Windows remains unsupported; use WSL2.
- Follow the [Windows / WSL 0.3.0 to 0.4.2 guide](../windows-upgrade.md) for a side-by-side, reversible upgrade.
- Provider credentials, relay URLs, live MCP configuration, and organization policy remain private overlays and are not included in this release.

### 中文

本次发布把维护者已经实际使用并验证的 0.4 正式运行时同步到公开仓库。

**调度与容量**

- 每个显式 dispatcher ID 使用操作系统锁槽，最多允许 10 个活动调用；不引入错误的机器全局并发上限。
- 完成事件支持 0–120 秒有界等待；只读 health 检查可报告生命周期、活动来源、进度、资源使用和剩余租约。
- Prompt-only 研究与仓库型任务具有明确分离的 source 语义。

**自适应生命周期**

- `--timeout` 改为活动续期的空闲租约，由 Grok 会话事件、受限 workspace 活动、agent log 增长和结构化进度/结果续期。
- 独立的 24 小时 hard cap 继续作为默认安全上限，并可在运行中修改或禁用。
- `grok-worker lease-set` 可以在不重启 ACP 会话的情况下调整空闲和硬上限。

**Grok 隔离**

- 每个 Worker clone 都会从选中模型生成私有、托管的 `GROK_HOME`；并发 clone 不会互相覆盖配置。
- 服务商凭据仅在内存解析并注入子进程；派生 TOML 不保存明文 API Key。
- 用户 marketplace、plugin 和 Grok 级 MCP 在 `grok inspect --json` 后 fail-closed。
- `[claude_compat] imported = true` 阻止仓库根 `.mcp.json` 进入托管会话，同时保留 inspect 安全门作为权威检查。
- 规范 `Agents.md` 仍链接到托管 profile。

**执行策略与证据**

- 稳定 Worker 提示词要求最多使用 3 个写范围不重叠的并发子代理；该数量上限由提示词约束，`--no-subagents` 仍提供运行时硬关闭。主 Worker 负责集成和结构化结果契约。
- 未提交源状态改为重复使用 `--include-dirty-path PATH` 精确授权；存在非忽略脏文件时拒绝裸 `--include-dirty`。
- 成功任务的外部制品仍严格只有 `changes.patch`、`worker.log`、`verification.txt` 三个文件。
- 制品记录实际生效的租约策略，以及仅在服务商可观测时记录 token/cache 指标。

**升级**

- 支持 macOS 与 Linux。原生 Windows 仍不支持，请使用 WSL2。
- Windows 0.3.0 用户按 [Windows / WSL 0.3.0 → 0.4.2 指南](../windows-upgrade.md)做可回滚的并排升级。
- 服务商凭据、中转地址、在线 MCP 和组织策略仍属于私有叠加层，不进入公开发布。

---

## 2026-07-14 — Initial public release / 首次公开发布

**Version:** `grok-worker` 0.3.0
**Public identity:** `MaxxxDong/codex-grok-orchestrator`
**Site:** [https://maxxxdong.github.io/codex-grok-orchestrator/](https://maxxxdong.github.io/codex-grok-orchestrator/)

### English

Initial standalone public repository for Codex–Grok orchestration.

**Positioning**

- Codex (or another ACP dispatcher) dispatches and reviews.
- Grok runs as an isolated, lifecycle-managed worker via `grok-worker`.
- Evidence—the verified three-file external artifact contract—determines acceptance.
- Workers do not merge, push, publish, submit, or self-approve.

**Shipped**

- Installable `grok-worker` and `grok-worker-agent` console entry points.
- Configurable model, reasoning effort, optional MCP path, and explicit subagent policy (off by default).
- Disposable-clone isolation, authoritative lifecycle metadata, fail-closed deletion, leased shared caches, and transactional config apply.
- External three-file contract: `changes.patch`, `worker.log`, `verification.txt`.
- One-shot `run` and named-session `session-start` / `session-followup` / `session-finalize`.
- Same-file bilingual README (`中文` / `English` anchors) with a minimal `README.zh-CN.md` compatibility pointer.
- Static GitHub Pages landing page at `docs/index.html` (no CDN, no analytics).
- Public design, operations, contribution, security, and release documentation.
- Acknowledgements to `stdevMac/grok-in-codex` and `Cjbuilds/Codex-Orchestration` (no source code copied).

**Platforms**

- macOS and Linux supported; native Windows experimental / unsupported (prefer WSL).

**Portability**

- Lifecycle-owned dirty-source baseline commits use a command-scoped synthetic Git identity (`grok-worker` / `grok-worker@localhost`) so Ubuntu runners without global `user.name`/`user.email` succeed the same way as macOS.

**License**

- Apache License 2.0.

### 中文

面向 Codex × Grok 编排的首次独立公开发布。

**定位**

- Codex（或其它 ACP 调度器）负责分发与审核。
- Grok 通过 `grok-worker` 以隔离、生命周期托管的 Worker 身份执行。
- 证据——已验证的三文件外部制品契约——决定是否接受结果。
- Worker 不合并、不推送、不发布、不提交外部事务、不自行批准自己的工作。

**交付内容**

- 可安装的 `grok-worker` 与 `grok-worker-agent` 控制台入口。
- 可配置的模型、推理强度、可选 MCP 路径，以及显式的子代理策略（默认关闭）。
- Disposable clone 隔离、权威 lifecycle、删除 fail-closed、带租约的共享缓存、可回滚的配置事务。
- 外部三文件契约：`changes.patch`、`worker.log`、`verification.txt`。
- 一次性 `run` 与命名会话 `session-start` / `session-followup` / `session-finalize`。
- 同文件双语 README（`中文` / `English` 锚点），`README.zh-CN.md` 仅为兼容指针。
- 无依赖静态 GitHub Pages 落地页 `docs/index.html`（无 CDN、无分析脚本）。
- 公开的设计、运维、贡献、安全与发布文档。
- 致谢 `stdevMac/grok-in-codex` 与 `Cjbuilds/Codex-Orchestration`（未复制任何源代码）。

**平台**

- 支持 macOS 与 Linux；原生 Windows 为实验性 / 暂不支持（建议 WSL）。

**可移植性**

- 生命周期托管的 dirty-source 基线提交使用命令级合成 Git 身份（`grok-worker` / `grok-worker@localhost`），使无全局 `user.name`/`user.email` 的 Ubuntu runner 与 macOS 行为一致。

**许可证**

- Apache License 2.0。
