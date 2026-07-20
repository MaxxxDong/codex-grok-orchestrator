## 2026-07-20 — Efficiency, continuation, and native structured results / 提效、续跑与原生结构化结果

**Version:** `grok-worker` 0.7.0

Bounded execution contracts let Root pass targets, failure evidence, focused
checks, final gates, and risk tags without polluting the stable prompt prefix.
Native same-task continuation reuses `grok --continue` under an explicit
compatibility contract (task, source, clone, base SHA, model, High reasoning,
tool signature, prompt version, contract hash) with TTL and exact session cleanup
when not retained. Native implementation runs use Grok `--json-schema` so the
lifecycle runner—not the model—atomically persists `.grok-output/result.json`.
ACP/legacy paths keep the disk write contract. Opt-in tool policy, productive-
progress attention, and prompt fingerprints with honest cache A/B metrics complete
the efficiency surface. Provider cache hits are never claimed without A/B evidence;
logical shared cwd is not applied because Grok sessions key by physical path.

有界执行契约、原生同任务续跑、runner 落盘 JSON Schema 结果、可选工具策略、有效
进展告警与稳定提示指纹。不把未证明的 provider 缓存命中写进文档；物理 clone 仍唯一。

Release verification: full pytest, Ruff, mypy, sdist+wheel smoke, path/secret scan.

---

# Release notes / 发布说明

Canonical public release history for **codex-grok-orchestrator**.
The installable package and CLI remain **`grok-worker`**.

Package versioning details also appear in [CHANGELOG.md](../../CHANGELOG.md).

---

## 2026-07-20 — Clean CI binary isolation / 干净 CI 二进制隔离

**Version:** `grok-worker` 0.7.2

The native-command construction test now explicitly injects its fake Grok
binary instead of depending on the developer machine's `PATH`. This closes the
GitHub Actions failure on clean Ubuntu and macOS runners. Production Grok binary
discovery and all 0.7.1 runtime behavior are unchanged.

原生命令构造测试现在显式注入测试用 Grok 二进制名，不再依赖开发机 `PATH`。这修复
干净 Ubuntu/macOS GitHub runner 上的失败；生产 Grok 二进制发现和 0.7.1 运行行为不变。

Release verification: full GitHub Actions matrix on Ubuntu and macOS with
Python 3.12 and 3.13, plus release build and payload checks.

---

## 2026-07-20 — Sandbox-portable concurrency tests / 沙箱兼容并发测试

**Version:** `grok-worker` 0.7.1

Five concurrency regressions previously used Python `multiprocessing` barriers
and events. Grok Build's macOS sandbox rejects the underlying named semaphore
creation before the production locks are exercised, producing deterministic
`_multiprocessing.SemLock` permission failures. Version 0.7.1 replaces only that
test harness with independent Python subprocesses coordinated by plain ready/go
files. The same POSIX file locks, cache leases, dispatcher capacity,
same-source exclusion, and process-exit release behavior remain under test.

五个并发回归测试此前依赖 Python `multiprocessing` 屏障与事件，Grok Build 的 macOS
沙箱会在真正测试生产锁之前拒绝底层命名信号量。0.7.1 仅将测试协调层替换为独立 Python
子进程和普通 ready/go 文件；生产锁、缓存租约、并发容量、同源互斥和进程退出释放语义不变。

Release verification: host focused suite `25 passed`, full suite `292 passed`,
Ruff and strict mypy; Grok Build macOS sandbox focused suite `25 passed in 2.69s`
with no `SemLock`, `PermissionError`, or repository changes.

---

## 2026-07-20 — CLI compatibility, honest cache metrics / CLI 兼容与诚实缓存指标

**Version:** `grok-worker` 0.6.1

Maintenance release: `cache-status --json` and `cache-gc --json` are accepted
compatibility flags without changing the single-JSON output. Invalid CLI options
now surface Click's concise usage error instead of a Python/Rich traceback when
the entry point runs with `standalone_mode=False`.

Token metrics distinguish Grok separate cache-read input from OpenAI nested
cached tokens, persist a bounded `cache_ratio` with a machine-readable basis
(or null for incoherent totals),
and optionally record `model_calls` from `num_turns` / `modelCalls`. One-shot
runs also record monotonic `process_duration_seconds`. The stable Worker prompt
adds concise execution-efficiency rules (targeted inspection, smallest checks
while iterating, full suite once at the end when required, no clone-local
environments, limited independent subagents).

维护发布：`cache-status --json` 与 `cache-gc --json` 作为兼容标志被接受，输出
仍是单一 JSON。入口在 `standalone_mode=False` 下对非法选项返回 Click 简洁用法
错误，而不再抛出 Python/Rich 堆栈。

Token 指标区分 Grok 独立 cache-read 与 OpenAI 嵌套 cached tokens，持久化有界
`cache_ratio` 与 basis，并可选记录 `model_calls`。一次性运行额外记录单调时钟
`process_duration_seconds`。稳定 Worker 提示补充执行效率规则。

The repository README and GitHub Pages landing page now include a bilingual
version-evolution summary from 0.3.0 through 0.6.1. This page remains the
canonical detailed history instead of duplicating full release notes on every
surface.

仓库 README 与 GitHub Pages 首页新增 0.3.0 至 0.6.1 的双语版本演进摘要；本文件
继续作为详细发布历史的唯一权威来源，避免在多个页面重复维护完整说明。

Release verification: focused CLI/metrics/prompt tests, full pytest suite, Ruff,
strict mypy, and clean wheel build/install smoke.

---

## 2026-07-20 — Detached event-first orchestration / 分离启动与事件优先编排

**Version:** `grok-worker` 0.6.0

Codex-dispatched one-shot work now uses `grok-worker run --detach`. The command
returns a structured launch receipt immediately, while the detached child reuses
the same native or ACP execution lifecycle, explicit reasoning checks,
three-file artifact contract, retention, and guarded cleanup as foreground
`run`. Dispatchers then wait with `watch`: terminal, settled, or attention
events wake immediately, and a compact 300-second heartbeat remains the fallback.

Detached launcher logs are private shared-cache entries covered by the existing
quota and TTL/LRU cleanup. Parallel workers remain isolated by run ID and
dispatcher capacity. Version consistency tests now keep package metadata,
runtime `--version`, lockfile, install commands, upgrade documentation,
changelog, and release notes synchronized.

Recognizable live provider failures and ignored reasoning effort now emit one
non-sensitive `attention` event within the 2-second lease poll interval. This
wakes `watch` without killing a Worker that may still recover; terminal and
settled events remain authoritative for the final outcome. Late provider errors
also remain visible in bounded final failure summaries.

由 Codex 调度的一次性任务现在默认使用 `grok-worker run --detach`。命令会立即
返回结构化启动回执，分离子进程仍复用前台 `run` 的原生或 ACP 执行链、显式
推理强度检查、三文件制品、失败保留与安全清理。调度器随后使用 `watch`：
终态、清理完成或需介入事件会立即唤醒，只有没有事件时才返回 300 秒一次的
精简健康心跳。

分离启动日志属于受共享缓存配额与 TTL/LRU 管理的私有条目；并行 Worker 继续
按 run ID 和 dispatcher 容量隔离。新增版本一致性测试，确保包元数据、运行时
版本、锁文件、安装命令、升级文档、变更记录和发布说明不再发生版本漂移。

可识别的运行中 provider 故障或推理强度被忽略时，会在 2 秒 lease 检查周期内
发出一次不含错误正文的 `attention`。它会立即唤醒 `watch`，但不会杀掉仍可能
恢复的 Worker；最终结果仍由后续 terminal/settled 事件决定。

Release verification: 260-test pytest suite, Ruff, strict mypy, offline lock
resolution, sdist/wheel build, clean-wheel version/resource/help smoke, and
source-launcher version smoke. A live provider-500 canary returned a detached
receipt in 0.144 seconds, emitted `running/attention` before terminal failure,
and preserved terminal/settled cleanup plus a provider-specific final summary.

---

## 2026-07-19 — Immediate lifecycle signals and simpler preflight / 即时通知与简化预检

**Version:** `grok-worker` 0.5.3

Dispatchers can now use `grok-worker watch` for event-first waits: terminal or
attention events wake immediately, while a compact 300-second health heartbeat
remains as the fallback. Notification records distinguish `terminal`, `settled`,
and `attention`, and startup failures receive a run ID early enough to notify an
already waiting dispatcher.

The new `grok-worker preflight` command performs one disclosure scan and lists
every blocked relative path and rule code without exposing matched values. The
credential scanner no longer classifies long runtime identifier assignments as
literal secrets; quoted literals and high-confidence unquoted secrets still fail
closed. Operations guidance now separates Codex tenant approval rejection from
runner, provider, quota, and lifecycle failures.

调度器现在可用 `grok-worker watch` 进行事件优先等待：终态或需介入事件会立即
唤醒，同时保留 300 秒一次的精简健康心跳作为兜底。通知明确区分 `terminal`、
`settled` 与 `attention`，启动失败也能及时通知已经等待的调度器。

新增的 `grok-worker preflight` 只扫描一次并列出全部被拦截的相对路径与规则码，
不输出命中值。凭据扫描不再把长运行时标识符误判为字面量密钥；带引号字面量和
高置信度未加引号密钥仍然严格拒绝。运维文档也明确区分 Codex 租户审批拒绝、
runner 故障、服务商/额度故障与生命周期故障。

Release verification: 251-test pytest suite, Ruff, strict mypy, offline sdist/wheel
build, clean-wheel CLI smoke, source launcher smoke, and public-tree disclosure
checks.

---

## 2026-07-19 — Native Grok, bounded caches, simple startup / 原生配置与简化启动

**Version:** `grok-worker` 0.5.2

One-shot CLI and library calls now default to native Grok Build and use the user's
normal Grok home. Plugins, MCP servers, OAuth state, provider settings, bundled
resources, explicit High reasoning, and prompt-cache eligibility remain available.
Repository `.mcp.json` is visible. A lightweight environment inspection runs first,
but it is advisory: extension errors are logged and the actual Grok launch proceeds.

The launcher validates cache ownership, rejects symlinks, enforces private mode,
and falls back when the host cache is sandbox-read-only. It prefers an existing
virtual environment, avoiding network access during normal starts. Source-checkout
development uses ignored `.uv-cache/`.

Mutable UV/PIP/NPM/Poetry caches stay inside the disposable clone; prepared
environments and package downloads remain shared and leased. One-shot native calls
use `--no-memory` and remove only the exact clone-keyed Grok session bucket after
exit. Provider cache metrics remain observable, but a different clone cwd may miss
even when the stable prompt is identical. Worker concurrency remains ten per
dispatcher; each Grok prompt limits internal subagents to three.

Release verification: 241 tests, Ruff, strict mypy, offline sdist/wheel build,
clean-wheel CLI smoke, and a native Grok Worker smoke.

---

## 2026-07-19 — Native sandbox cache hotfix / 原生沙箱缓存热修

**Version:** `grok-worker` 0.5.1

Native workers now keep mutable UV/PIP/NPM/Poetry caches under the disposable
workspace while reusing prepared shared environments read-only. This removes
the permission-failure/retry cycle seen when Grok's workspace sandbox tried to
write the host cache. The release also adds `grok-worker --version` and verifies
it from a clean installed wheel in CI.

Native Worker 现在把会写入的 UV/PIP/NPM/Poetry 缓存放在 disposable workspace
内，同时继续只读复用共享依赖环境，消除 Grok workspace 沙箱写宿主缓存时的
权限失败与绕路重试。本版本还新增 `grok-worker --version`，并在 CI 中从干净
安装的 wheel 验证该命令。

---

## 2026-07-19 — Native headless and lower-friction startup / 原生执行与启动提效

**Version:** `grok-worker` 0.5.0

**Upgrade from:** 0.3.x / 0.4.x

**Public identity:** `MaxxxDong/codex-grok-orchestrator`

### English

One-shot `run` now uses Grok Build's native headless CLI by default. The ACP
transport remains available through `--backend acp`, and named sessions remain
ACP-backed for compatibility.

**Reasoning and isolation**

- Workers use a private runtime `HOME` with the native `~/.grok` layout instead
  of `GROK_HOME` override mode.
- Runtime homes are shared only by identical source/provider/model/effort
  profiles; the same model ID on another endpoint receives a different home.
- The managed model explicitly declares reasoning support and High effort. A
  Grok warning that effort was ignored invalidates the run.
- API keys remain child-environment-only. `Agents.md` stays linked; user plugins
  and MCP servers are disabled.
- Repository `.mcp.json` is atomically masked only inside the disposable clone
  during execution, hidden from Git, and restored byte-for-byte before
  patch/artifact capture. Interrupted masks self-recover on the next launch.

**Lower-friction startup**

- Safe staged, unstaged, and untracked files are snapshotted automatically.
  Ignored files are excluded; suspected secrets and escaping symlinks remain
  hard failures. Legacy dirty allowlists no longer omit other safe dirt.
- Retained task-ID collisions allocate a fresh suffixed task and clone.
- A transient Git clone/baseline failure gets one clean rescan and retry;
  half-created task destinations are moved to age-gated temporary quarantine.
- Dependency prewarm failure is recorded as a warning and execution continues;
  verified task output is still mandatory.
- Independent implementation workers may start in separate clones. Root remains
  the sole integration owner.

**Observability and evidence**

- Health output adds `backend`, `process_pid`, and `process_live`; old `acpx_*`
  names remain for v0.3/v0.4 readers.
- Native JSON usage records input, cache-read, output, and reasoning tokens when
  available. Cache hits remain provider-dependent and are never inferred.
- The external success contract is unchanged: exactly `changes.patch`,
  `worker.log`, and `verification.txt`.

**Measured live comparison**

On the same bounded Python implementation task through the same configured
provider, native headless completed in about 69 seconds with 22 passing tests;
ACP completed in about 110 seconds with 18 passing tests. Both stored High
reasoning. This is one controlled sample, not a universal benchmark.

A final post-audit release smoke on the same three-test repository completed in
34.31 seconds through native and 59.86 seconds through ACP. Both produced the
same minimal implementation and passed 3/3 tests. Native exposed 74,294 input,
1,719 output, and 252 reasoning tokens; ACP quiet output did not expose token usage.
Three repeated native runs reported zero cache-read tokens, so this release does
not claim a cache hit for that relay/task combination.

### 中文

一次性 `run` 现在默认直接使用 Grok Build 原生 Headless CLI。旧 ACP 通信层
通过 `--backend acp` 保留，命名会话在 0.5.0 仍由 ACP 承载。

**思考强度与隔离**

- Worker 使用独立 runtime `HOME` 下的原生 `~/.grok`，不再进入会破坏
  reasoning/cache 行为的 `GROK_HOME` override 模式。
- 只有 source/provider/model/effort 完全一致才共享 runtime home；同模型不同
  端点不会互相覆盖配置。
- 托管模型显式声明 High 能力；只要 Grok 报告忽略思考强度，本次运行就判失败并保留。
- API Key 只进入子进程环境；继续链接 `Agents.md`，禁用用户 plugin/MCP。
- 仓库 `.mcp.json` 只在 disposable clone 的 Grok 执行期间原子隐藏，Git 不会
  看到临时删除；中断后下次启动会自恢复，原文件按字节优先保留。

**减少启动阻断**

- 普通 staged、unstaged、untracked 文件自动安全快照；ignored 文件不复制，
  疑似密钥和越界软链接仍硬拒绝；旧 allowlist 不再漏掉其他安全脏文件。
- 已保留的同 task-id 不再阻断，新任务自动得到后缀和独立 clone。
- Git clone/脏基线若遇到瞬时失败，会重新扫描后干净重试一次；半成品原子移出
  task 命名空间，进入有 24 小时年龄门的临时隔离区。
- 依赖预热失败记录为 warning 后继续；最终仍必须通过真实验证。
- 独立实现任务可在各自 clone 启动，Root 继续作为唯一集成者。

**可观测性与证据**

- health 新增 `backend`、`process_pid`、`process_live`，旧 `acpx_*` 字段保留兼容。
- 原生 JSON 可见时记录 input/cache-read/output/reasoning token；缓存命中仍由
  服务商决定，不做推断。
- 外部成功制品保持严格三文件：`changes.patch`、`worker.log`、`verification.txt`。

**真实对照样例**

同一服务商、同一有边界的 Python 实现题中，原生 Headless 约 69 秒并通过
22 个测试；ACP 约 110 秒并通过 18 个测试。两者均保存 High 思考。这是一组
受控样例，不代表所有任务的固定倍率。

最终审计后 smoke 在同一个三测试仓库中，Native 为 34.31 秒，ACP 为 59.86 秒；
两者产出相同的最小实现并通过 3/3 测试。Native 可观测到 74,294 input、1,719
output、252 reasoning tokens；ACP quiet 输出不暴露 token。相同 Native 请求连续
三次均为 0 cache-read，因此本版本不宣称该渠道/任务已经命中缓存。

Windows 请按 [0.3/0.4 → 0.5.1 WSL2 升级指南](../windows-upgrade.md)执行。

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
