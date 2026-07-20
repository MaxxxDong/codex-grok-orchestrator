# Changelog

All notable public changes are recorded here. The project follows semantic versioning while the CLI is pre-1.0.

## [Unreleased]

## [0.7.1] - 2026-07-20

### Fixed

- Replaced the five macOS concurrency tests that depended on
  `multiprocessing` semaphores with real Python subprocesses coordinated by a
  plain-file barrier. The tests still exercise the production POSIX file locks
  across independent processes, but now run inside Grok Build's macOS sandbox
  without deterministic `_multiprocessing.SemLock` permission failures.
- Kept the existing test count and assertions for shared cache leases,
  dispatcher capacity, same-source exclusion, and OS lock release after a
  process exits. No production lock, cache, lifecycle, tool, or reasoning
  behavior changed.

### Verification

- Host: focused concurrency suite `25 passed`; full suite `292 passed`; Ruff
  and strict mypy passed.
- Grok Build macOS sandbox: focused concurrency suite `25 passed in 2.69s`, no
  `SemLock` or `PermissionError`, with an empty changes patch.

## [0.7.0] - 2026-07-20

### Added

- Bounded **execution contract** on task manifests (`execution` / flat aliases):
  `targetFiles`, `targetModules`, `knownFailureEvidence`, `focusedChecks`,
  `finalGates`, `riskTags`, named read-only `subtasks` (max 3), and
  `requiredFailedGates`. Risk tags expand the final verification matrix;
  previously failed required gates cannot be replaced by a narrower check.
- **Native same-task continuation**: `--write-continuation` automatically keeps
  the clone for 24 hours; `--continue` reopens it, and another
  `--write-continuation` extends the bounded workflow. Metadata lives under
  `.grok-worker/continuation.json` and reuses `grok --continue` for the same
  task/source/clone/base/model/reasoning/tool signature. Unrelated tasks stay
  one-shot. Exact worker-owned Grok session cleanup still runs on finalize/GC
  when continuation is not retained.
- Completion events now separate lifecycle `timestamp` from actual `emitted_at`;
  `watch_delivery_latency_seconds` measures event write to consumer return.
- Distinct attention reasons in one run are independently observable while exact
  duplicates remain suppressed.
- Continuation metadata is now written only after semantic success, and its
  compatibility hash includes the bounded execution contract.
- **Task-scoped tool policy** (native flags only): `--disable-web-search`,
  `--disallowed-tool` (repeatable), `--max-turns`. Effective tool signature is
  part of continuation compatibility. User plugins/MCP remain available by
  default.
- **Native JSON Schema final-result capture**: implementation native runs pass
  `--json-schema` for WorkerResult; the runner validates model output and
  atomically writes `.grok-output/result.json`. ACP/legacy still require the
  model to write `result.json` on disk. Malformed structured output fails
  closed with a precise error.
- **Productive-progress detection** distinct from lease liveness: workspace
  changes, verification logs, result/progress phase. After configurable
  `--stall-turns` / `--stall-seconds` without productive progress, emit one
  `attention` event (`no_productive_progress`) without killing the worker.
- **Stable prompt/cache fingerprinting** with logical workspace id (hash of
  source realpath) and metrics fields for fresh/cached input, model calls, and
  duration. Physical clone cwd remains unique; logical shared cwd is **not**
  applied (Grok sessions key by physical path). Do not claim provider cache
  hits without A/B evidence.
- CLI: `--execution-manifest`, `--continue`, `--write-continuation`,
  `--disable-web-search`, `--disallowed-tool`, `--max-turns`, `--stall-turns`,
  `--stall-seconds`, `--no-native-json-schema`.

### Changed

- Implement/debug role prompts document native structured-output vs ACP/legacy
  disk result paths while preserving verification-log and success criteria.
- Package and public docs bumped to **0.7.0**.
- Codex watch guidance now consumes the same yielded terminal session until the
  long-poll exits, preventing the observed multi-minute acknowledgement gap.

### Verification

- Focused contract/native/prompt tests, full pytest suite, Ruff, strict mypy,
  sdist+wheel build and clean-wheel smoke, `git diff --check`, secret/path scan.

## [0.6.1] - 2026-07-20

### Fixed

- `cache-status --json` and `cache-gc --json` are accepted as compatibility flags;
  output remains a single JSON document.
- Invalid CLI options print Click's concise usage error and exit code instead of
  an uncaught Python/Rich traceback under `standalone_mode=False`.
- Worker-owned `.grok-output/` runtime evidence is excluded from source-release
  build contexts, so verification caches and symlinks cannot contaminate sdists.
- Cache ratio is bounded to `[0, 1]` with an explicit basis field; incoherent
  total/cached fields remain unobservable instead of being clamped. Grok separate
  `cache_read_input_tokens` / `cacheReadInputTokens` uses `cached/(fresh+cached)`;
  OpenAI nested `input_tokens_details.cached_tokens` uses `cached/total`; legacy
  top-level `cached_tokens` keeps `cached/input`.

### Added

- Optional `model_calls` on token metrics from native Grok `num_turns` /
  `modelCalls` without double-counting nested duplicates.
- One-shot metrics record `process_duration_seconds` from a monotonic clock.
- Stable base prompt execution-efficiency rules: targeted inspection, smallest
  relevant checks while iterating, full suite/build once at the end when required,
  no clone-local environments, avoid repeated narration, and at most three
  independent read-only subagents when they reduce wall time.
- README and GitHub Pages now summarize the purpose of every public version from
  0.3.0 through 0.6.1, with canonical detailed release notes linked once.

### Verification

- Focused CLI/metrics/prompt tests, full pytest suite, Ruff, strict mypy, and
  clean wheel build/install smoke.

## [0.6.0] - 2026-07-20

### Added

- `grok-worker run --detach` starts the existing one-shot lifecycle in a
  detached child and immediately returns a structured launch receipt.
- The hidden detached-child entry accepts one validated `RunConfig` payload and
  reuses the same lifecycle, reasoning, artifact, retention, and cleanup code as
  foreground execution.

### Changed

- Codex dispatch guidance now uses detached launch plus event-first `watch`
  instead of keeping a foreground shell open for frequent status polling.
- Detached launcher logs are private `launch-logs` entries governed by the
  shared cache's existing quota and TTL/LRU cleanup.
- Cache accounting includes detached launcher logs, preserving bounded cleanup
  across parallel workers.
- Recognizable live provider HTTP/auth/rate-limit/unavailable failures and ignored
  reasoning effort now emit one immediate, non-sensitive `attention` event while
  the Worker remains free to recover. Final failure summaries scan bounded head
  and tail windows so a late provider error is not hidden by missing result JSON.
- Public release surfaces and CI tests now enforce one coherent package version
  across runtime metadata, lockfile, installation commands, operations,
  changelog, and release notes.

### Verification

- Full suite: 260 tests passed; Ruff and strict mypy passed.
- Offline lock resolution, sdist/wheel build, clean-wheel version/resource/help
  smoke, and source-launcher version smoke passed.
- A live provider-500 canary returned its detached receipt in 0.144 seconds,
  emitted `running/attention` before terminal failure, then produced bounded
  terminal/settled events and a provider-specific final summary.

## [0.5.3] - 2026-07-19
### Added

- `grok-worker watch`: event-first waits with immediate terminal/attention wakeup
  and a compact 300-second health heartbeat fallback.
- Distinct `terminal`, `settled`, and `attention` notification kinds with cleanup,
  artifact readiness, exit, and attention pointers. Dedup now includes event kind.
- `grok-worker preflight`: one-pass disclosure scan listing every blocked relative
  path and rule code without matched values. Direct run refusals expose the same
  complete path list.

### Changed

- Maximum bounded event wait is 600 seconds; `events` still defaults to 30 and
  `watch` defaults to 300.
- CLI-created runs now allocate their `run_id` before lifecycle startup so
  startup failures can notify an already waiting dispatcher.
- Credential scanning no longer mistakes long runtime identifier assignments for
  literal secrets; quoted literals and high-confidence unquoted values still
  fail closed.
- Operations documentation distinguishes pre-process Codex tenant approval
  rejection from runner, provider, quota, and lifecycle failures.

### Verification

- Full suite: 251 tests passed; Ruff and strict mypy passed.
- Offline sdist/wheel build, clean-wheel CLI/resource smoke, source-launcher
  smoke, repository disclosure preflight, and archive content scans passed.

## [0.5.2] - 2026-07-19

### Changed

- One-shot library/API callers now default to native Grok Build headless, matching
  the CLI. ACP is explicit compatibility transport and remains the v0.5 named-session backend.
- Stable Skill/role instructions stay at the start of one-shot prompts. Dynamic
  clone, dependency, and cache paths remain process environment only.
- Native and ACP processes use the user's normal Grok home. Configured plugins,
  MCP servers, OAuth state, bundled resources, provider settings, High reasoning,
  and prompt-cache eligibility remain available. Repository `.mcp.json` is no
  longer masked.
- Every launch runs an advisory `grok inspect --json`. Failure is logged but does
  not block the actual process; extension diagnostics are not availability gates.
- The source launcher validates cache ownership, rejects symlinks, enforces mode
  `0700`, and falls back when the platform default is sandbox-read-only. It uses
  an existing project virtual environment first, so normal starts remain offline.
- Repository development commands use an ignored, writable `.uv-cache`, so a
  sandboxed `uv run` no longer fails against the host `~/.cache/uv` before tests start.
- One-shot native calls disable cross-session memory and remove only their exact
  clone-keyed Grok session bucket after completion. Mutable package caches remain
  disposable; prepared environments and package downloads remain shared and leased.
- Removed the duplicate pre-launch GC pass. Dependency prewarm I/O failures are
  warnings, and post-run GC errors no longer override a completed task.
- macOS process birth identity now uses `libproc` before `ps`, preserving accurate
  cross-process health checks inside restricted hosts.

### Verification

- Full suite: 241 tests passed; Ruff and strict mypy passed; sdist and wheel built offline.
- A restricted-cache launcher smoke completed from the existing virtual environment
  without network access.
- Controlled native Krill runs retained High reasoning and provider cache-read
  metrics. Repeated calls in one cwd hit cache; different clone cwd values may miss.

## [0.5.1] - 2026-07-19

### Fixed

- Native workers now keep mutable UV/PIP/NPM/Poetry caches under the disposable
  workspace while continuing to reuse prepared shared environments read-only.
  This removes repeated sandbox permission failures against the host-level cache.
- Added a standard `grok-worker --version` command and clean-wheel CI coverage.

## [0.5.0] - 2026-07-19

### Added

- Native Grok Build headless execution for one-shot `run`; select the legacy
  transport explicitly with `--backend acp`.
- Stable isolated runtime `HOME` profiles with native `~/.grok` layout, explicit
  reasoning capability, plugin/MCP isolation, and cache-preserving reuse only
  for identical source/provider/model/effort profiles.
- Generic `backend`, `process_pid`, and `process_live` health fields while
  retaining v0.3/v0.4 `acpx_*` compatibility fields.
- Native token, cache-read, output, and reasoning metric extraction.

### Changed

- Safe staged, unstaged, and untracked files are snapshotted automatically;
  legacy allowlists no longer filter them, ignored files are excluded, exact
  clone bytes are rescanned, and sensitive material still fails closed.
- Repository-root `.mcp.json` is atomically masked only inside the disposable
  clone while Grok runs, hidden from Git with an owned `skip-worktree` flag,
  then restored byte-for-byte before artifact capture. Interrupted masks recover
  on the next launch; a worker-created replacement is quarantined, not promoted.
- Transient Git workspace materialization failures get one fresh disclosure scan
  and retry. Partial directories are atomically moved out of the task namespace
  to the existing age-gated system-temp cleanup domain; startup never recursively
  deletes a just-failed clone path.
- Dependency prewarm failure becomes a visible warning and execution continues;
  verification remains mandatory for success.
- A retained task-ID collision allocates a new suffixed task/clone instead of
  refusing startup. Independent implementation workers may start in separate
  clones; Root remains the single integration owner.
- Any native warning that the requested reasoning effort was ignored invalidates
  the run instead of accepting a lower-effort result.
- Native analysis/research uses the OS `read-only` sandbox and `plan` permission
  mode; workspace write approval is implementation-only.
- Session-title generation uses the selected worker model instead of the
  built-in `grok-build` auxiliary route, avoiding a relay-side 404 request.
- Native metric extraction handles pretty JSON embedded after Grok warning lines.

### Compatibility

- Named sessions remain ACP-backed in 0.5.0. `acpx` is optional for native
  one-shot runs but remains required for `--backend acp` and session commands.
- Native Windows remains unsupported; use WSL2.

## [0.4.2] - 2026-07-19

### Added

- Activity-renewed worker leases with Grok session, progress/result, agent-log,
  and bounded workspace signals.
- Runtime `lease-set` control for changing inactivity and hard limits without
  restarting the active ACP session.
- Status/health fields for timeout mode, hard-cap remainder, and lease revision.
- Final worker artifacts record the effective activity-lease policy.

### Changed

- `--timeout` now controls inactivity rather than total process lifetime.
- Removed the launch-time `acpx --timeout`; a separate 24h hard safety cap is
  enforced by the lifecycle runner and can be changed or disabled at runtime.
- Isolated worker profiles set `[claude_compat] imported = true` so repository
  `.mcp.json` is not discovered into managed sessions; `grok inspect --json`
  remains the fail-closed enforcement for empty plugins/MCP.
- Managed `GROK_HOME` directories are scoped by disposable clone so concurrent
  workers using different model profiles cannot overwrite each other's config.

## [0.4.1] - 2026-07-18

### Added

- Managed, plugin-free `GROK_HOME` derivation for every Grok ACP process.
- In-memory credential resolution with child-only `GROK_WORKER_API_KEY` injection.
- Atomic private profile refresh, canonical `Agents.md` linking, and fail-closed
  `grok inspect --json` verification before agent startup.

### Security

- User marketplaces, plugins, and Grok-level MCP servers no longer leak into
  workers by default. Explicit ACP MCP configuration remains independently
  supported.
- Derived configuration contains no plaintext provider credential and refuses
  unmanaged nonempty profile directories.

## [0.4.0] - 2026-07-17

### Added

- Per-dispatcher concurrency via fixed OS flock slot leases
  (`dispatchers/<hash>/slots/00.lock..09.lock`), unique `run_id`, and
  structured `DISPATCHER_CONCURRENCY_BUSY` refusal (no preemption). Max 10
  means **active Grok invocations**, not idle session objects.
- Same-source implementation exclusion via nonblocking hashed source locks under
  the same dispatcher hash; analysis workers may coexist; other dispatchers do
  not block.
- Completion events carry optional `run_id` / `dispatcher_id`; dedup by
  `(run_id, state)`; event wait default 30s, max 120s, explicit 0 nonblocking.
- Explicit long-task timeout constant 3600s; diagnostic-only `health` command
  (300s interval policy, never terminates workers).
- Dirty path allowlist (`--include-dirty-path`), structured disclosure summary on
  lifecycle/`WorkerMeta` (survives clone deletion into `worker.log`), secret and
  symlink-escape gates on dirty/non-git material only.
- Prompt-only research/analysis mode (`--prompt-only`) with honest source
  identity and empty managed workspace; library rejects `prompt_only` with
  non-null source.

### Changed

- Untracked discovery and fingerprint paths always use `--exclude-standard`;
  ignored files such as `.env` are never copied.
- Legacy bare `--include-dirty` refuses when nonignored dirty material exists
  (migration to `--include-dirty-path`); ignored-only dirt remains excluded.
- Named sessions take transient slot leases around ACP turns only; idle
  `SESSION_OPEN` is removed from capacity budgets.
- Nested subagents are enabled by default; the stable Worker prompt limits use
  to at most 3 non-overlapping concurrent subagents. `--no-subagents` remains
  the runtime-enforced opt-out.
- Removed roots.json / advisory slot-pointer reservation design.
- Default event wait is 30 seconds (was immediate 0).

### Security

- High-confidence sensitive dirty content/paths refused without logging secret
  values; outbound/absolute file and directory symlinks blocked before
  materialization.
- `.env.example` / `.env.sample` / `.env.template` / `.env.dist` exempt from
  path-only refusal; content still scanned.
- Deleting an already-tracked sensitive-named file is allowed (absent path).
- Fail closed on `git check-ignore` or material read errors without logging
  contents.

## [0.3.0] - 2026-07-14

### Added

- Initial standalone public repository.
- Configurable model, reasoning effort, optional MCP path, and explicit subagent policy.
- Installable `grok-worker` and `grok-worker-agent` console entry points.
- Public design, operations, contribution, security, and release documentation.
- A complete Simplified Chinese introduction, feature overview, and usage guide.

### Changed

- Moved stable worker prompts into the Python package for wheel installation.
- Removed personal paths, private provider configuration, and competition-specific policy from the public core.
- Native Windows now fails clearly where POSIX locking is required; WSL is recommended.
