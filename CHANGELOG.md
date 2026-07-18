# Changelog

All notable public changes are recorded here. The project follows semantic versioning while the CLI is pre-1.0.

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
