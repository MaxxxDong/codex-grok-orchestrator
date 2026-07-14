# Release notes / 发布说明

Canonical public release history for **codex-grok-orchestrator**.
The installable package and CLI remain **`grok-worker`**.

Package versioning details also appear in [CHANGELOG.md](../../CHANGELOG.md).

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
