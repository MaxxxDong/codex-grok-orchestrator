---
name: grok-worker
description: Use when Codex should delegate bounded repository analysis, implementation, debugging, research, or review to xAI Grok Build through a lifecycle-managed Grok 4.5/high worker.
---

# Grok Worker

Codex is always the dispatcher, reviewer, and decision owner. This skill is a foreground worker mechanism, not a daemon, scheduler, autonomous product, or replacement for user approval gates.

Use only the lifecycle entry point `bin/grok-worker`. One-shot `run` defaults to
native Grok Build headless on every platform. Managed ACP remains available only
for explicit compatibility runs and named sessions. Do not invoke raw `grok`,
`acpx`, or `grok-acp-worker` for repository work outside lifecycle diagnosis.

For Codex-dispatched one-shot work, `run --detach` is the default launch path.
It returns a receipt immediately; observe the `run_id` with `watch`, not an open
terminal session. Do not use repeated `write_stdin` polling. Foreground `run`
remains available only for direct interactive use and launcher diagnosis.

## Runtime defaults

- Model comes from `GROK_WORKER_MODEL` and defaults to `grok-4.5`.
- Reasoning comes from `GROK_WORKER_REASONING_EFFORT` and defaults to `high`.
- Never Fast and never silently fall back to another model.
- Workers use the user's native `HOME` and `~/.grok` directly. Configured plugins,
  MCP servers, OAuth state, bundled resources, and provider settings stay available.
  A lightweight `grok inspect --json` check runs before launch, but failure only
  warns; the actual Grok process remains the availability test.
- The command always passes the selected model and reasoning effort explicitly.
  If Grok says it ignored the requested effort, the run fails and is retained.
- One-shot native runs add `--no-memory`; after the process exits, only the exact
  global Grok session bucket keyed by that disposable clone is removed. Normal
  interactive Grok sessions and plugin installations are untouched.
- Global plugins and repository MCP definitions run with the task's permissions.
  Treat them as trusted inputs for implementation mode; a broken extension warns
  or loses that tool, but does not become a lifecycle startup gate.
- The selected model also handles session summaries; do not restore the
  incompatible built-in `grok-build` auxiliary route in relay-backed profiles.
- Each Grok worker may run at most 3 subagents concurrently for independent work.
  Prefer read-only research, review, and test analysis. Never assign overlapping
  writes; the lead worker owns integration and the structured result contract.
- Root Codex may run independent direct workers concurrently. One Root task uses one stable opaque `dispatcher_id`; its active limit defaults to 10 and may be raised consistently with `--max-workers`/`GROK_WORKER_MAX_WORKERS` (for example 24). Root itself is not counted. Other Root tasks use different dispatcher IDs and neither count nor block this task. Do not claim or enforce a machine-global limit.
- Each direct worker owns one clone, task manifest, cwd, process/session, log, and artifact directory. Never assign overlapping writes concurrently.
- Root Codex reviews every result and decides whether to integrate it.
- A clone path is disposable runtime evidence, never a canonical project path. Project artifacts that need an absolute repository path must use `.grok-worker/lifecycle.json` → `source_realpath`; never persist `pwd` or `.grok-disposable/grok-worker-*` in README, HANDOFF, submission material, or generated source.

On native Windows, always use the installed `grok-worker.exe`. It runs this same
Python lifecycle implementation with Win32 file locks, hidden child processes,
Windows process-tree cleanup, PowerShell 7/UTF-8 policy, and the single active
`%USERPROFILE%\.grok\config.toml`. Do not route through WSL and do not maintain a
second provider configuration.

Before the first Windows run in a task, resolve the repository and CLI paths.
For ACP compatibility or named sessions, also verify the managed acpx runtime:

```powershell
$repo = (Resolve-Path -LiteralPath "C:\CodexWS\YourProject").Path
(Get-Command grok-worker).Source
grok-worker acpx-runtime-status
```

The command must resolve to `%USERPROFILE%\.local\bin\grok-worker.exe`. Pass the
resolved Windows path to `--source`; never translate it to `/mnt/c/...`. If the
managed runtime check fails, stop and report it instead of falling back to a
global acpx or WSL.

Preflight: verify `grok --version`, `grok models`, repository path, available
disk, and `grok-worker status`. Verify `acpx --version` only for `--backend acp`
or named sessions. At the start of a Root task, generate one opaque dispatcher
ID and reuse it for every worker in that task through
`GROK_WORKER_DISPATCHER_ID` or `--dispatcher-id`; never reuse that ID for a
different Root task. Run a minimal live smoke test before a significant wave
when there is no recent successful proof.

When the source has uncommitted test credentials or a prior disclosure refusal,
run `grok-worker preflight --source "$REPO" --json` once before retrying. It
reports every blocked relative path and rule code in one pass without values.
Do not weaken the scanner or retry one path at a time. An ordinary `run` refusal
also prints the complete blocked-path list.

If all configured slots for the current dispatcher are busy, do not preempt or
replace workers. Use `watch` for the dispatcher and retry the exact bounded task
after a terminal event. A wait timeout means only “no matching event yet,” not
Worker failure.

## Choose one-shot or named session

Use `run` for one bounded turn.

`run` defaults to `--backend native` on every platform. Windows terminal and
file tools are verified with Grok Build 0.2.106. Managed `--backend acp` remains
available for compatibility and continues to power named sessions.

Prefer **native same-task continuation** (`run --write-continuation`, then
`run --continue`) when the same logical one-shot task needs another native turn
without ACP. Compatibility requires identical task ID, source realpath, clone,
base SHA, model, High reasoning, tool signature, prompt version, and contract
hash (including the bounded execution contract). TTL expires unused continuation metadata; finalize/GC without retained
continuation cleans the exact worker-owned Grok session.

Use `session-start` → zero or more `session-followup` → `session-finalize` only when the same logical task needs continuous **ACP** iteration.
Named sessions remain ACP-backed.

A named session may be reused only when all of these remain identical:

- task ID
- source realpath and worker clone
- base SHA
- role
- analysis/implementation mode
- model, agent, MCP config, and permission signature
- stable prompt prefix and context-pack hash

A new task, audit/review role, repository/cwd, base, permission, model, or MCP change requires a new worker and new session. Context-pack reuse and session reuse do not prove provider-side cache hits.

## Task manifest

Named sessions require a JSON manifest conforming to `schemas/task-manifest.schema.json`.
Optional `execution` (or flat aliases) is the dynamic bounded run contract:

```json
{
  "taskId": "bounded-task-id",
  "outcome": "Concrete result to produce",
  "verification": ["pytest -q"],
  "constraints": ["grok-4.5/high", "no Fast", "at most 3 concurrent subagents"],
  "boundaries": {
    "allowedWrites": ["src/", "tests/"],
    "forbiddenWrites": [".env", "secrets"]
  },
  "iterationPolicy": "Continue only this logical task",
  "stopWhen": "Acceptance checks pass",
  "pauseIf": "A user decision or scope expansion is required",
  "execution": {
    "targetFiles": ["src/pkg/module.py"],
    "focusedChecks": ["pytest -q tests/test_module.py"],
    "finalGates": ["pytest -q", "ruff check src tests", "mypy src"],
    "riskTags": ["package"],
    "subtasks": [
      {"name": "scan-tests", "goal": "Read-only inventory of failing tests", "readonly": true}
    ]
  }
}
```

The stable prompt prefix is versioned base instructions + role instructions + a content-addressed context pack + a fixed delimiter. The task manifest and execution contract are the dynamic suffix. Follow-ups send only the dynamic suffix. Never put run IDs, disposable absolute paths, or timestamps in the stable prefix.

Risk tags expand the final verification matrix. Never replace a previously failed required gate with a narrower focused check. Shared API/schema/security/cache/concurrency/build/migration/package changes must expand final gates.

Runner-owned `finalGates` start at the clone root and must be complete executable
commands, not task aliases such as `pytest` or `testDebugUnitTest`. Include the
repository wrapper or working directory, for example `npm --prefix services/api
test`, `.\gradlew.bat -p apps/android testDebugUnitTest`, or `uv run --no-sync
pytest -q`. When a PowerShell gate sets an environment variable, use
`Set-Item Env:JAVA_HOME 'C:\path'`; do not put `$env:...` inside an interpolating
manifest here-string.
The runner inherits the launcher process environment only. Environment variables
set inside a Worker's focused-check shell do not flow back into runner-owned
gates, so repeat required toolchain setup in each self-contained final gate.

## Commands

Create one stable opaque ID for the current Root task and reuse it for all commands below:

```bash
export GROK_WORKER_DISPATCHER_ID="codex-<opaque-current-task-id>"
export GROK_WORKER_RUN_ID="run-<opaque-current-run-id>"
```

One-shot implementation:

```bash
grok-worker run \
  --detach \
  --source "$REPO" \
  --backend native \
  --run-id "$GROK_WORKER_RUN_ID" \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --disposable-root "$DISPOSABLE_ROOT" \
  --artifact-root "$ARTIFACT_ROOT" \
  --mode implementation \
  --prompt-file "$PROMPT_FILE" \
  --execution-manifest "$TASK_JSON"
```

Native same-task continuation (not ACP):

```bash
# Turn 1: retain clone + continuation metadata
grok-worker run \
  --source "$REPO" --backend native --mode implementation \
  --task-id "$TASK_ID" --prompt-file "$PROMPT_FILE" \
  --write-continuation \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID"

# Turn 2+: same task/source/clone/model/tools
grok-worker run \
  --source "$REPO" --backend native --mode implementation \
  --task-id "$TASK_ID" --prompt-file "$FOLLOWUP_FILE" \
  --continue --write-continuation \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID"

# Final without --write-continuation: normal finalize + session GC
grok-worker run \
  --source "$REPO" --backend native --mode implementation \
  --task-id "$TASK_ID" --prompt-file "$FINAL_FILE" \
  --continue --dispatcher-id "$GROK_WORKER_DISPATCHER_ID"
```

One-shot read-only analysis/review:

```bash
grok-worker run \
  --detach \
  --source "$REPO" \
  --backend native \
  --run-id "$GROK_WORKER_RUN_ID" \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --disposable-root "$DISPOSABLE_ROOT" \
  --artifact-root "$ARTIFACT_ROOT" \
  --mode analysis \
  --prompt-file "$PROMPT_FILE"
```

Native analysis/research is enforced with Grok's OS `read-only` sandbox and
`plan` permission mode; it is not merely a prompt instruction.

Prompt-only research before a repository exists:

```bash
grok-worker run \
  --detach \
  --prompt-only \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --mode research \
  --task-id bounded-research \
  --prompt-file "$PROMPT_FILE"
```

Prompt-only has no source tree and cannot be used for implementation success.

Named session:

```bash
grok-worker session-start \
  --source "$REPO" --manifest-file "$TASK_JSON" \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --role implement --mode implementation \
  --disposable-root "$DISPOSABLE_ROOT" \
  --artifact-root "$ARTIFACT_ROOT"

grok-worker session-followup \
  --source "$REPO" --manifest-file "$TASK_JSON" \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --role implement --mode implementation \
  --disposable-root "$DISPOSABLE_ROOT" \
  --artifact-root "$ARTIFACT_ROOT"

grok-worker session-finalize \
  --source "$REPO" --manifest-file "$TASK_JSON" \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --role implement --mode implementation \
  --disposable-root "$DISPOSABLE_ROOT" \
  --artifact-root "$ARTIFACT_ROOT"
```

The role is one of `implement`, `debug`, `review`, or `research`. Do not finalize until the session has produced the required structured result and verification evidence.

Status, notifications, config apply, and cleanup:

```bash
grok-worker status --disposable-root "$DISPOSABLE_ROOT" --json
grok-worker health --disposable-root "$DISPOSABLE_ROOT" \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" --json
grok-worker lease-set --disposable-root "$DISPOSABLE_ROOT" \
  --task-id "$TASK_ID" --idle-timeout 3600 --hard-timeout 86400
grok-worker events --shared-cache-root "$CACHE" --after "" \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" --wait-seconds 30 --json
grok-worker watch --shared-cache-root "$CACHE" \
  --disposable-root "$DISPOSABLE_ROOT" --run-id "$GROK_WORKER_RUN_ID" \
  --after "" --wait-seconds 300 --until-settled --json
grok-worker preflight --source "$REPO" --json
grok-worker config-apply \
  --config "$CONFIG" --candidate "$CANDIDATE" \
  --smoke-argv-json '["/usr/bin/true"]' --smoke-timeout 5 --json
grok-worker gc --disposable-root "$DISPOSABLE_ROOT" --source "$REPO"
grok-worker cache-status
grok-worker cache-gc
grok-worker list-legacy --disposable-root "$DISPOSABLE_ROOT"
```

The start receipt is launch acceptance, not task success. After it returns, call
`watch --until-settled` with the receipt's `run_id`; carry `next_cursor` into each subsequent
call. If the Codex terminal tool yields a `session_id` while that `watch` process
is still running, resume that exact terminal session with a blocking empty
`write_stdin`/wait until the command exits. Tool-level yields may require another
wait on the same session; that is continuation of one long-poll, not status polling;
never wait only on an outer orchestration cell and abandon the inner terminal
session. A 300-second heartbeat is the only routine fallback. For parallel work,
use one dispatcher-scoped watch for the whole wave rather than one terminal or
watcher per Worker.

See [docs/operations.md](docs/operations.md) for completion events, status summary
fields, config-apply rollback semantics, and authority boundaries.

Optional one-shot controls:

- `--backend native|acp`: native is the one-shot default; ACP is the compatibility and named-session transport and requires `acpx`.
- `--execution-manifest PATH`: bounded targets/checks/risk tags/subtasks (dynamic suffix).
- `--continue` / `--write-continuation`: native same-task continuation. The writer automatically keeps the clone with a 24-hour TTL; final `--continue` without another writer flag closes and cleans it.
- `--disable-web-search`, `--disallowed-tool NAME` (repeatable): opt-in pure-code tool policy via native Grok flags (plugins/MCP stay available by default). The runner intentionally exposes no model-turn cap; inactivity and the absolute safety cap govern runaway work.
- `--stall-turns N`, `--stall-seconds S`: productive-progress attention thresholds (never kill solely for stall).
- `--no-native-json-schema`: disable runner-owned JSON Schema result capture (ACP-like disk `result.json`).
- `--keep "REASON"`: explicit indefinite clone retention.
- Safe staged, unstaged, and untracked files are snapshotted automatically.
  Ignored files are never copied. Sensitive paths/content and escaping symlinks
  still fail closed. `--include-dirty` and `--include-dirty-path` are deprecated
  compatibility inputs and are not required for ordinary dirt.
- Legacy dirty allowlists no longer filter the snapshot. All safe nonignored
  dirt is included, and the exact clone bytes are scanned again before launch.
- A transient Git clone/dirty-baseline failure is cleaned, rescanned, and retried
  once. Partial directories move to age-gated system-temp quarantine; do not
  manually delete or reuse them.
- `--prompt-only`: source-free analysis/research before a repository exists; rejects implementation and dirty/source flags.
- `--cap-bytes 6442450944`: disposable clone-domain limit.
- `--cache-max-bytes 10737418240`: independent shared-cache limit.
- `--cache-ttl-hours 2160`: shared-cache TTL before LRU quota eviction.
- `--timeout 1800` is a renewable inactivity lease, not a total lifetime.
  `--hard-timeout 86400` is the default absolute safety cap; pass 0 to disable it.
  `lease-set` may adjust either value while the same backend process is running.
- `--task-id`, `--failure-retain-hours 24`.
- `--max-workers N` (or `GROK_WORKER_MAX_WORKERS`) sets the per-dispatcher or
  legacy per-root active limit; all dispatchers sharing the same logical task
  must use the same value.
- `--no-prepare-deps`: explicit opt-out of shared dependency preparation.

## Two independent capacity domains

### Disposable clones

- Default root: repository-adjacent `.grok-disposable`.
- Default cap: exactly 6 GiB.
- Active invocations per explicit dispatcher ID are limited by `--max-workers` (default 10) across disposable roots. There is no machine-global limit; without a dispatcher ID, only the legacy per-root limit applies. Idle named sessions do not hold slots; each start/followup/finalize invocation takes a transient slot.
- Before creation: reconcile dead workers, run eligible GC, enforce concurrency and capacity.
- After creation: remeasure; if over cap, roll back only the new clone.
- Unmarked legacy directories count toward capacity and are never deleted by ordinary GC.
- A repository-root `.mcp.json` remains visible to native Grok. Plugin and MCP
  startup diagnostics are logged but do not become lifecycle launch gates.

### Shared cache

Resolution order:

1. `GROK_WORKER_CACHE_ROOT`
2. `$XDG_CACHE_HOME/grok-worker`
3. macOS `~/Library/Caches/grok-worker`
4. Linux `~/.cache/grok-worker`

The cache is outside the disposable root and has its own default 10 GiB quota and 90-day TTL/LRU policy. Buckets include `context-packs`, `venvs`, `uv`, `pip`, `npm`, `poetry`, `metrics`, and detached `launch-logs`.

Workers hold a shared cache-use lease. Cache GC requires an exclusive nonblocking lease and defers while any worker is using cache entries. If TTL then LRU cannot reduce usage below quota, new workers are refused before clone creation.

Never create clone-local `.venv`. Python environments are fingerprinted shared
environments under `venvs/`; uv, pip, npm, and Poetry caches use their shared
buckets. A locked Python project attempts one frozen dependency prewarm. Every
nested npm project with `package.json` plus `package-lock.json` receives a
clone-local `npm ci` from the shared download cache; source `node_modules` is
never copied or linked. Prewarm failure is recorded as a startup warning and
does not prevent Grok from attempting the task; real task verification still
determines success.

## Lifecycle and retention

| Outcome | Clone | External artifact |
|---|---|---|
| Verified success | delete immediately | keep |
| Failure, invalid result, interrupt, dependency/artifact failure | retain 24 hours | keep available evidence |
| `--keep REASON` | keep indefinitely | keep |
| Open named session | keep until explicit finalize | no final artifact yet |
| Unmarked legacy | never delete automatically | none |

Dead creating/running/finalizing processes become failed with a new 24-hour deadline. `finalizing` is non-deletable. Never persist success before verified external artifacts exist.

A successful implementation requires backend exit 0 and a strict
`.grok-output/result.json` with `task_completed=true`, `status=completed`, and at
least one passing verification record. On **native** implementation runs the
runner captures the model’s JSON Schema final object and atomically writes
`result.json` itself; the model must still create real verification logs under
`.grok-output/verification/`. **ACP/legacy** paths still require the model to
write `result.json` on disk. Analysis runs are permissioned read-only;
when the backend exits 0 with a nonempty response but cannot write
`.grok-output`, the lifecycle runner creates a clearly identified root-owned
analysis result and retains the response in `worker.log`. Analysis may have an
empty verification list. Missing/empty analysis output, partial/failed results,
malformed structured output, reasoning downgrade, and unverifiable
implementation results are failures.

Native `max_tokens_truncation` or `max_turns_reached` triggers automatic
same-session continuation inside the same lifecycle, bounded only by the
renewable inactivity lease and absolute hard timeout. If recovery still cannot
finish, the run remains failed, the exact clone/session gets compatible
continuation metadata, and the budget error remains the primary lifecycle cause;
a missing structured result is only a secondary contract consequence.

When an execution manifest supplies `finalGates`, they are runner-owned and are
not included as executable commands in the Grok prompt. Grok may run
`focusedChecks` while editing; after native structured output the runner executes
each final gate exactly once, writes atomic `runner-gate-*` evidence, and uses the
observed exit codes. Gates share the remaining hard-time budget and stop after the
first failure.

### Lifecycle / observability

- **Authority**: `.grok-worker/lifecycle.json` is the only state source. Shared-cache
  completion events and optional `progress.json` are notification/advisory only.
- **Completion events**: terminal transitions append an immediate `terminal`
  pointer; one-shot cleanup then appends `settled`. Startup failures that occur
  after CLI configuration emit `attention`. Events are deduplicated by
  `(run_id, state, kind)` and never carry prompts, tokens, env, file contents, or
  agent output. Each modern run also has a small run-specific receipt so a watcher
  does not repeatedly parse global history. Emit remains fail-closed: if terminal
  receipt persistence fails, a successful clone is retained and the lifecycle
  fallback reports the notification fault instead of losing the outcome.
- **Default waiting**: for one run, call `grok-worker watch --until-settled` with
  its explicit `run_id`; it consumes `terminal` and waits through cleanup for
  `settled` in the same command. For a parallel wave, use one dispatcher-scoped
  `watch` without `--until-settled`. A watch long-polls up to 300 seconds and only
  returns one compact health heartbeat on timeout. Preserve `next_cursor` between
  calls.
- **Codex tool-use rule**: launch one-shots with `run --detach`, then make one
  bounded `watch --until-settled --wait-seconds 300` call at a time. Never keep the launch shell
  alive for 10/30-second `write_stdin` checks. When the terminal tool yields a
  live `session_id` for the blocking watch, continue that same session with an
  empty blocking `write_stdin`/wait until it exits; abandoning it loses the
  immediate wakeup. Repeated tool-level yields on that same process are not
  health polling. A watcher returning early means a
  real event arrived; an unchanged heartbeat does not justify reading full logs.
- **Handoff rule**: a per-run `--until-settled` response is ready for artifact
  inspection only when `settled=true`. A running `attention` returns immediately;
  inspect lifecycle and the bounded log tail, then resume from `next_cursor` if
  appropriate. An unchanged heartbeat needs no full log read and no user-facing
  narration.
- **Live backend attention**: a recognized provider HTTP/auth/rate-limit/
  unavailable failure or ignored reasoning effort emits one non-sensitive
  `running/attention` pointer within the lease poll interval. It wakes `watch`
  but does not kill a Worker that may recover. Preserve the returned cursor and
  decide whether to keep waiting from lifecycle plus a bounded log tail.
- **Health checks**: `health` remains a diagnostic-only read-only fallback. It reports lifecycle,
  bounded non-symlink workspace activity, fixed progress step, result/artifact
  readiness, process identity, CPU/RSS, and timeout remaining, but never kills,
  restarts, preempts, or disposes a Worker. Without an explicit root it reads the
  bounded shared root registry and aggregates all known disposable roots; pass
  `--disposable-root` for a deliberately single-root view. The runner renews an activity lease
  from managed Grok session events, progress/result files, agent-log growth, and
  bounded workspace activity. A truly quiet worker expires after 1800 seconds by
  default; the separate 24h hard cap prevents an active infinite loop.
- **Platform approval boundary**: if Codex rejects the `grok-worker run` command
  before process creation because an external Grok service is not approved for
  private repository disclosure, no Worker exists and the Skill cannot emit a
  lifecycle event or override that tenant policy. Do not retry or disguise the
  command. Use an administrator-approved provider/command, or have the user run
  the exact command directly in their local terminal and let Codex consume only
  the resulting local lifecycle/artifacts.
- **Status summary**: `grok-worker status --json` adds per-clone `phase`,
  `last_activity_at`, `activity_source`, `progress_step`, `elapsed_seconds`,
  `timeout_seconds`, `remaining_seconds`, `timeout_mode`, hard-cap fields,
  `result_ready`, `artifact_ready`, and
  `resources{cpu_percent,rss_bytes}`. `progress_step` is restricted to
  `planning|editing|verifying|finalizing`; arbitrary worker-authored progress text
  is never surfaced. `phase` follows lifecycle state; illegal/future progress
  fails soft. Terminal elapsed is frozen; remaining is null. Resource PID prefers
  `process_pid`; `acpx_pid` remains a v0.3/v0.4 compatibility alias.
- **Early implementation checkpoint**: implementation/debug roles atomically write
  allowlisted `progress.json` plus a valid `status=partial`, `task_completed=false`
  result before extensive work, then atomically replace the result after real
  verification. A partial checkpoint remains a failure and never relaxes the
  strict success contract.
- **Config apply**: `grok-worker config-apply` parses a TOML candidate, serializes
  the transaction with a same-dir lock, requires a finite positive smoke timeout,
  atomically replaces the live file with backup, runs a shell-free smoke argv, and
  rolls back exact original bytes on smoke failure/timeout. Receipts are
  path/hash/metadata only. Use only test/tmp configs in automation.

Full operational detail: [docs/operations.md](docs/operations.md).

## Exact external artifact contract

Every finalized worker exposes exactly three regular non-symlink files:

- `changes.patch`
- `worker.log`
- `verification.txt`

`worker.log` embeds the task manifest, lifecycle state, session contract/close state, and agent output. `verification.txt` embeds the structured result, verification logs and hashes, token/cache metrics when observable, cleanup receipt, and SHA-256 hashes for `changes.patch` and `worker.log`.

The directory is staged outside both clone and disposable root, exact-set verified, then atomically renamed. Extra files, directories, symlinks, missing keys, or hash mismatch invalidate it. v1 `MANIFEST.sha256` artifacts are read-only compatible for historical GC but are never produced by v2.

Only delete a successful clone when:

- the three-file artifact verifies,
- the session is closed or the run is one-shot,
- the cleanup receipt authorizes clone deletion,
- the artifact path is outside clone and disposable root.

## Cache observability

Write per-run metrics to `metrics/worker-runs.jsonl`. Native JSON output exposes
input, cached-read, output, and reasoning tokens when Grok reports them. A cache
ratio is observable only when provider output contains both input-token and
cached-read-token values. Metrics also record `model_calls`,
`process_duration_seconds`, `prompt_fingerprint` (stable prefix hash + logical
workspace id), and `provider_cache_claim=unproven_without_ab`. The native profile
and named-session reuse preserve cache eligibility, but different disposable
clone paths change Grok's cwd context and may miss across one-shot runs. A
stable **logical** shared cwd is not applied (unsafe shared session writes).
Relay thresholds and eviction can also make identical calls miss. Report
unobservable metrics as unobservable. Never claim a provider cache improvement
without A/B evidence from fresh vs cached input tokens across runs.

## Legacy and migration

Ordinary GC never deletes unmarked historical directories. Use explicit classification:

```bash
grok-worker import-legacy \
  --disposable-root "$DISPOSABLE_ROOT" \
  --name "<direct-child>" \
  --classification keep|retain-24h|expire \
  --reason "reviewed reason" \
  [--confirm-expire] [--base-commit <sha>] \
  [--artifact-root "$ARTIFACT_ROOT"]
```

Destructive legacy classification requires a verified binary-safe archive against a trustworthy Git base. Never infer `HEAD` as the baseline. Non-Git destructive classification fails closed.

When migrating from `~/.cache/grok-worker` on macOS, first stop workers, verify both roots, copy or move only reviewed cache buckets into `~/Library/Caches/grok-worker`, then run `cache-status` and `cache-gc`. Do not merge disposable clones into cache and do not delete retained failure clones before their lifecycle permits it.

## Worker-failure triage (Root dispatcher)

On any Worker failure or malformed/missing artifact, Root first inspects lifecycle state plus all three external artifacts (`changes.patch`, `worker.log`, `verification.txt`) when present. Classify the failure as exactly one of:

1. **Bounded task/content failure** — the worker correctly exercised the Skill/runner contracts but the assigned work was wrong, incomplete, or out of scope.
2. **Environment/account/dependency failure** — host, credentials, disk, network, tool versions, or shared-deps preparation blocked a correct run.
3. **Systemic Skill/runner/base-role/task-prompt contract defect** — lifecycle, artifact, prompt, or base-role invariants failed at the shared seam (Skill, runner, base/role prompts, or task-prompt contract).

If evidence indicates a systemic defect, fix the Skill/runner/prompt at the shared seam and add a regression test before retrying the original task. Do not normalize repeated caller-side boilerplate or ad-hoc prompt workarounds when the invariant belongs in the Skill. Do not weaken the implementation validator or synthesize implementation success.

After systemic repair: run a minimal live smoke with an ordinary task prompt that
does not duplicate lifecycle JSON, then resume the original task. The configured
model/high policy, no-Fast policy, and maximum 3 non-overlapping subagents remain
unchanged.

## Handoff

Root Codex must:

1. verify the exact three-file artifact,
2. review the patch and embedded evidence,
3. independently run proportionate checks,
4. report verified facts separately from Grok claims,
5. integrate only the approved result.

Do not imply a provider cache hit, successful cleanup, or completed task without current verification evidence.
