# Grok Worker Operations

Operational reference for completion-event notifications, status summaries, and
transactional config apply. Lifecycle metadata remains the sole authority for
worker state.

## Authority boundaries

| Mechanism | Role | Not allowed |
|---|---|---|
| `.grok-worker/lifecycle.json` | Authoritative worker state | — |
| `$CACHE/notifications/completion-events.jsonl` | Notification index only | Second state source; sensitive payloads |
| `.grok-worker/progress.json` | Advisory activity hints | Override lifecycle phase/state |
| External three-file artifact | Verified success evidence | Fake readiness via progress alone |

Never treat the notification log as truth for GC, capacity, or success. Always
re-read lifecycle (and artifacts when needed).

## 1. Completion-event notifications

### What it is

When a managed worker reaches a terminal state (`success`, `failed`, `keep`, …),
the runner appends an immediate `terminal` JSON line. After one-shot session and
clone cleanup finishes, it appends a distinct `settled` line under:

```text
$SHARED_CACHE_ROOT/notifications/completion-events.jsonl
```

Shared cache root resolution uses `GROK_WORKER_CACHE_ROOT`, then
XDG/macOS/Linux defaults. One-shot finalize uses `RunConfig.shared_cache_root`.
Dead-worker reconcile / GC pass an explicit shared root when known; otherwise
they use the default cache root.

### Event shape (non-sensitive pointers only)

Required pointer fields:

- `event_id` — stable unique id for cursor polling
- `task_id`
- `state` — terminal lifecycle state string
- `timestamp` — ISO-8601 UTC
- `artifact_path` — string or null

Optional pointer fields on new emits (when known):

- `run_id` — unique per execution
- `dispatcher_id` — explicit dispatcher scope
- `kind` — `terminal`, `settled`, or `attention`
- `exit_code`, `artifact_ready`, `clone_cleaned`, `session_cleaned`
- `attention_required`, `reason_code`

Forbidden in events: prompt text, tokens, API keys, environment maps,
stdout/stderr, model/agent output, MCP config. Never include secret values or
file contents.

### Dedup and concurrency

- Dedup key: `(run_id, state, kind)` when `run_id` is present. Legacy rows without
  `run_id` still dedupe as `(task_id, state, kind)` among run_id-less events.
- Re-finalize / re-reconcile of the same terminal pair does not append again.
- Appends take an exclusive lock beside the log; writes are full JSON lines with
  flush/fsync so concurrent writers do not leave half-line JSON.
- **Best-effort only**: notification I/O or serialization failures never reverse
  an already-persisted lifecycle terminal state, `RunOutcome`, artifact path, or
  GC dead-worker reconcile. Callers treat emit as advisory.
- Readers **discard** malformed JSONL rows, empty objects (`{}`), missing
  required pointer fields, and wrong-typed values; they never surface incomplete
  rows as events.
- Optional filters: `--run-id` / `--dispatcher-id`. Omitting filters preserves
  unfiltered (compatible) reads.

### Event wait bounds

- Default `--wait-seconds` is **30**.
- Explicit **0** is non-blocking.
- Values must be in **0..600** inclusive; negatives and values greater than 600
  are rejected. The default `events` wait remains 30; `watch` defaults to 300.

### Typical commands

Immediate poll (empty cursor = from start):

```bash
grok-worker events \
  --shared-cache-root "$CACHE" \
  --after "" \
  --wait-seconds 0 \
  --json
```

Bounded long-poll after a known cursor:

```bash
grok-worker events \
  --shared-cache-root "$CACHE" \
  --after "$EVENT_ID" \
  --wait-seconds 30 \
  --json
```

JSON envelope includes an `events` array. Exit 0 on successful query even when
the list is empty.

### Default low-noise watch loop

Codex starts one-shot work with `grok-worker run --detach`. The command sends the
normal `RunConfig` to a detached child over a private stdin pipe and returns a
receipt containing `run_id`, `dispatcher_id`, PID, roots, and a bounded launcher
log path. The receipt means only that launch was accepted. The child still uses
the same `run_worker` lifecycle, permissions, reasoning checks, cache, artifacts,
retention, and cleanup path as foreground `run`.

Use the returned unique `run_id` so observation can start before the worker
finishes:

```bash
grok-worker watch \
  --shared-cache-root "$CACHE" \
  --disposable-root "$DISPOSABLE" \
  --run-id "$RUN_ID" \
  --after "$CURSOR" \
  --wait-seconds 300 \
  --json
```

The command returns immediately with `kind=events` when a matching terminal,
settled, or attention event appears. Otherwise it returns one `kind=heartbeat`
snapshot after the bounded wait. Feed `next_cursor` into the next call. One
dispatcher-scoped watch can cover a parallel wave. This command never reads full
logs and never mutates, restarts, or cleans workers.

Exact duplicate attention events are suppressed per run and reason code. A
different reason, or the same stall reason after productive recovery, remains
observable instead of being hidden by terminal-style deduplication.

Do not keep the detached launch terminal open and do not issue 10/30-second
`write_stdin` polls. One bounded 300-second `watch` call is the routine wait; a
terminal, settled, or attention event wakes it immediately. For parallel work,
one dispatcher-scoped watch covers the wave. Detached launcher logs are private
shared-cache entries governed by the same quota and TTL/LRU GC.

When a terminal integration returns a `session_id` because the blocking watch is
still running, continue that exact session with an empty blocking
`write_stdin`/wait until the command returns. Tool-level yields may require
another wait on the same session; this is one long-poll, not repeated
status polling. Waiting only on an outer orchestration cell leaves the actual
watch process unread and can add a full heartbeat interval before Root notices a
completion event. Returned events include `watch_delivery_latency_seconds` for
the `emitted_at` event-write to watch-return interval; `timestamp` remains the
authoritative lifecycle-state time.

While a Worker is still running, the 2-second lease loop classifies only bounded,
recognizable top-level provider HTTP/auth/rate-limit/unavailable failures,
structured ACP transport errors, and ignored reasoning effort. The first match
emits one `running/attention` event containing only a reason code. It does not
terminate the process: Grok may recover, and a later terminal/settled event still
defines the outcome. Plugin-level MCP warnings do not match this classifier.

On `terminal/success`, consume the verified artifact and wait once more for
`settled`. On failure or `attention_required=true`, inspect authoritative
lifecycle and only then a bounded log tail. Do not repeatedly read unchanged
status/log output between heartbeats.

## 1b. Per-dispatcher concurrency (OS flock slot leases)

There is **no** machine-global worker limit. With an explicit `--dispatcher-id`
(or `GROK_WORKER_DISPATCHER_ID`), capacity is reserved by non-blocking exclusive
`flock` on fixed slot files under the shared cache:

```text
$CACHE/dispatchers/<dispatcher_hash>/slots/00.lock .. 09.lock
```

Acquiring one of **10** nonblocking slot locks is the atomic capacity
reservation. The `FileLock` is held for the entire active CLI / ACP invocation
and released in `finally`; process crash releases the lease automatically via
OS flock semantics. If all ten are held, the runner raises
`DISPATCHER_CONCURRENCY_BUSY` with `active=10` / `limit=10` and **never**
preempts another worker.

Other dispatcher IDs use different hash directories and never count or block one
another. Without `dispatcher_id`, only **root-scoped** concurrency applies
(backward compatible). Documentation must not claim cross-root enforcement
unless an explicit dispatcher ID is set.

**Active capacity means active Grok invocations/processes**, not idle open
session objects. Named-session `session-start`, each follow-up, and finalize
acquire and release a **transient** dispatcher slot around their actual ACP
invocation. Idle `SESSION_OPEN` does **not** permanently consume the budget.
Root-scoped active counts also exclude `SESSION_OPEN`.

Same-source policy: independent workers may start concurrently because each one
edits a standalone clone. Root remains the sole integration owner and must
review/serialize acceptance when changes overlap. A different dispatcher never
blocks merely because it uses the same source path.

There is **no** persistent `roots.json` registry or advisory slot-pointer JSON;
the only reservation primitive is the held OS flock.

## 1c. Timeouts and health

| Policy | Value | Notes |
|---|---|---|
| Inactivity lease | **1800** s | Renewed by observable Grok/session/workspace activity |
| Hard safety cap | **86400** s | Separate absolute cap; `--hard-timeout 0` disables |
| Health inspect interval | **300** s | Diagnostic / read-only via `grok-worker health` |

Health inspection records lifecycle, bounded non-symlink workspace activity,
the fixed advisory progress step, result/artifact readiness, PID identity, and
CPU/RSS when available. It does **not** terminate, interrupt, restart, or mutate a
running worker merely because the interval elapsed. The foreground runner owns
termination: it reads `.grok-worker/lease.json`, renews the inactivity deadline
from managed Grok session events, progress/result writes, agent-log growth, and
bounded workspace activity, and terminates the backend process tree only when the
idle lease or hard cap expires. Neither backend receives a fixed transport-level
timeout, so the
policy may be changed during execution:

```bash
grok-worker lease-set --disposable-root "$DISPOSABLE" --task-id TASK \
  --idle-timeout 3600 --hard-timeout 172800
```

The lease file is root-owned control/telemetry. The worker may not edit it;
lifecycle remains the authority for worker state and terminal outcome.

## 1d. Dirty disclosure and prompt-only

- Untracked discovery and fingerprint paths always use `--exclude-standard`;
  ignored files such as `.env` are never copied into the clone baseline.
- Safe staged, unstaged, and untracked material is included automatically after
  disclosure scanning. Legacy `--include-dirty` and `--include-dirty-path` are
  accepted for script compatibility but are no longer ordinary startup gates or
  filters; all safe nonignored dirt is included.
- The exact materialized clone bytes are scanned again after copying, closing
  the source scan/copy race. A sensitive post-scan rewrite is refused before the
  backend starts.
- A transient Git clone or dirty-baseline failure is cleaned and retried once
  after a fresh disclosure scan. Partial directories are atomically moved out of
  the task namespace and become eligible for the existing 24-hour system-temp
  GC; startup does not recursively delete a just-failed clone path.
- Absolute paths, `..`, NUL, `.git`/managed paths, ignored paths, and file or
  directory symlink escapes are rejected. Renames may require both old and new
  paths. Already-deleted tracked paths are allowed (deletion is safe) and are
  not blocked by path/content scanning.
- Conventional template basenames (`.env.example`, `.env.sample`,
  `.env.template`, `.env.dist`) are exempt from path-only refusal but still have
  disclosed content scanned for high-confidence secrets.
- Fail closed when `git check-ignore` or reading selected material errors; never
  include file contents or secret values in the error.
- Before clone/deps, high-confidence sensitive dirty/non-git material is refused
  without logging secret values. Clean committed Git content stays trusted.
- `grok-worker preflight --source "$REPO" --json` performs the same value-free
  source scan without creating a clone. A refusal reports every blocked relative
  path and rule code in one response, so synthetic PAT/Bearer/API-key fixtures
  can all be changed to runtime composition before one retry. Direct `run`
  refusals also print the complete blocked-path list. Neither path logs matched
  values; the scanner remains fail-closed.
- A structured disclosure summary (source_kind, base SHA, counts, relative
  included paths, reason codes, risk decision — values/content/prompt/env-free)
  is written under `.grok-worker/disclosure.json` and also stored on
  `WorkerMeta` / lifecycle so the final `worker.log` retains it after successful
  clone deletion.
- `--prompt-only` runs analysis/research in a fresh empty managed workspace with
  honest `source_realpath=prompt-only`, the same three-file artifact contract,
  and never synthesizes implementation success. Implementation mode, dirty
  flags, and a non-null `source` are rejected (CLI and library/API path).

## 1e. Backend and startup recovery

- `grok-worker run` defaults to `--backend native`, which directly invokes Grok
  Build headless with a prompt file and JSON output.
- `--backend acp` retains the previous transport. Named sessions remain ACP-only
  in 0.5.x.
- Native one-shot execution uses the user's normal `HOME` and `~/.grok`. Plugins,
  MCP servers, OAuth state, provider settings, and stable-channel metadata remain
  available. `grok inspect --json` runs as an advisory environment check; timeout
  or nonzero exit is logged and the actual Grok launch still proceeds.
- Native `analysis` and `research` use Grok's OS `read-only` sandbox plus `plan`
  permission mode. Implementation alone receives workspace write approval.
- Native mutable UV/PIP/NPM/Poetry caches live under the disposable
  `.grok-output/.runtime-cache`; prepared environments remain shared and
  read-only. This avoids host-cache permission retries inside Grok's sandbox.
- One-shot native commands use `--no-memory`. After the process exits, the exact
  `~/.grok/sessions/<encoded-clone>` bucket is safely removed; unrelated Grok
  sessions, plugins, credentials, and shared package caches are untouched.
- Source-checkout development uses ignored `.uv-cache/` through `uv.toml` so
  `uv run`, `uv sync`, and `uv build` do not touch a sandbox-read-only host cache.
- The selected worker model is also used for session summaries, preventing the
  built-in `grok-build` auxiliary model from reaching an incompatible relay.
- A warning that Grok ignored requested reasoning effort changes the backend
  outcome to failure even if the worker wrote a completed result.
- Repository `.mcp.json` remains visible to Grok. MCP/plugin startup diagnostics
  are ordinary backend log entries, not lifecycle launch gates.
- Dependency prewarm errors become `startup_warnings`; they do not prevent the
  backend from trying task-local verification.
- Startup exceptions after CLI configuration append an `attention/startup_failed`
  event with a sanitized reason code. Validation errors returned synchronously
  before a run configuration exists remain ordinary CLI errors.

### Codex tenant approval happens before the runner

An execution approval reviewer may reject a command that would send a private
repository to an unapproved external Grok service. That rejection occurs before
the process is created: there is no clone, lifecycle, Worker event, provider call,
or quota use. `grok-worker`, Grok, and Skill documentation cannot override or
circumvent it. Do not retry the same command. Use an administrator-approved
provider/command, or let the user run the exact command directly in their local
terminal and have Codex consume only local lifecycle/artifacts afterward.
- If an explicit task ID already belongs to a retained clone, the runner keeps
  that evidence and allocates a suffixed ID. It never overwrites the old clone.

## 2. Status summary

### Per-clone fields (`status --json`)

Each managed clone entry includes:

| Field | Source / notes |
|---|---|
| `phase` | Lifecycle state only (never progress `"success"` over running) |
| `last_activity_at` | Best usable of lifecycle `updated_at`, progress/result timestamps, or a bounded non-symlink workspace/verification-file scan; timestamps more than **5s** in the future of status time are ignored |
| `activity_source` | `lifecycle`, `progress`, `workspace`, or `result`; source of `last_activity_at`, never a success signal |
| `progress_step` | `planning`, `editing`, `verifying`, `finalizing`, or null; arbitrary worker-authored text is never returned |
| `elapsed_seconds` | Active (`creating`/`running`/`finalizing`/…): now − `created_at`. Terminal (`success`/`failed`/`keep`/…): frozen at `updated_at − created_at` |
| `timeout_seconds` | Active lease inactivity window; legacy clones retain the old fixed-timeout value |
| `remaining_seconds` | Activity lease: `idle timeout − time since last observed activity`; terminal: always null |
| `timeout_mode` | `activity_lease` for new runs, `fixed_legacy` for old metadata |
| `hard_timeout_seconds` / `hard_remaining_seconds` | Separate absolute safety cap and live remainder; null when disabled or terminal |
| `lease_revision` | Increments when an operator changes policy with `lease-set` |
| `result_ready` | True only if clone has a real `.grok-output/result.json` file |
| `artifact_ready` | True only when metadata marks complete **and** artifact path exists |
| `backend` | `native` or `acp` for v0.5 runs |
| `process_pid` / `process_live` | Generic backend process identity; preferred for new consumers |
| `acpx_pid` / `acpx_live` | Compatibility aliases retained for v0.3/v0.4 consumers |
| `resources.cpu_percent` / `resources.rss_bytes` | Best-effort via short-timeout `ps` on preferred PID `acpx_pid` → `runner_pid` → legacy `pid`; null when inactive/unsupported |

### Fail-soft progress

Implementation/debug roles must create `.grok-worker/progress.json` and a valid
`status=partial` `.grok-output/result.json` checkpoint through same-directory
temporary files plus atomic rename before extensive work. They update the fixed
step at phase transitions and atomically replace the result only after verification.
The partial checkpoint is evidence only and can never satisfy semantic success.

Illegal, truncated, wrong-typed, future-dated, or non-allowlisted progress is
ignored. Status collection must not raise. Progress and workspace activity must
never promote a running lifecycle into success. The workspace scan is capped at
20,000 entries / 16 levels and never follows symlinks or enters managed/cache dirs.

### Typical command

```bash
grok-worker status --disposable-root "$DISPOSABLE_ROOT" --json
```

## 3. Transactional config apply

### Command

```bash
grok-worker config-apply \
  --config "$LIVE_TOML" \
  --candidate "$CANDIDATE_TOML" \
  --smoke-argv-json '["/path/to/smoke","--flag"]' \
  --smoke-timeout 30 \
  --json
```

### Safety rules

1. **Candidate parse first** — Python 3.12 `tomllib`. Invalid TOML leaves live
   config bytes untouched.
2. **Finite positive smoke timeout** — `smoke_timeout` must be finite and `> 0`
   (rejects NaN, ±Inf, 0, negatives) **before** any live-config mutation.
3. **Regular files only** — both live and candidate must be existing regular
   non-symlink files (live must already exist). Parent symlink chains are not
   specially policed; only the live/candidate path itself is checked.
4. **Serialized transaction** — the full read → backup → replace → smoke →
   keep/rollback section for a given live config uses a same-directory exclusive
   `FileLock` so concurrent applies cannot interleave replace/rollback.
5. **Same-dir atomic write** — backup and replace use tempfile + flush/fsync +
   `os.replace`, then fsync of the parent directory when the platform allows.
6. **Smoke without shell** — `--smoke-argv-json` is a nonempty JSON string array
   executed with `subprocess` `shell=False`. stdout/stderr are captured and
   discarded; they are never echoed.
7. **Rollback** — smoke exit ≠ 0 or timeout restores the **exact original
   bytes** of the live config and returns nonzero. Backup is an exact byte copy
   of the pre-apply live config.
8. **Receipt metadata only** — paths, SHA-256 hashes, smoke exit/timeout,
   `rolled_back`, `applied`. Never config body, API keys, env secrets, or smoke
   output.

### Failure / rollback semantics

| Condition | Live config | Exit | `rolled_back` |
|---|---|---|---|
| Invalid candidate TOML | Unchanged | ≠ 0 | false |
| Smoke exit 0 | Candidate bytes | 0 | false |
| Smoke nonzero | Original bytes restored | ≠ 0 | true |
| Smoke timeout | Original bytes restored | ≠ 0 | true (`timed_out`) |
| Symlink / missing live | Unchanged | ≠ 0 | false |

### Operational restriction

Automation and tests must only apply config under pytest/tmp directories. Do not
point unattended smokes at a real user secret store such as
`~/.grok/config.toml`.

## Shared cache buckets (reminder)

Completion events live under the shared cache root (`notifications/`), alongside
existing buckets (`context-packs`, `venvs`, `uv`, `pip`, `npm`, `poetry`,
`metrics`). Events are small pointer records; they are not capacity-managed
worker state.

## Artifact-root privacy

The exact external artifact contract is `changes.patch`, `worker.log`, and
`verification.txt`. `worker.log` intentionally captures full agent output for
review and diagnosis. Treat the artifact root as project-sensitive storage:
restrict access, apply an appropriate retention policy, and never publish it
automatically. Completion events do not copy that output.

## Version note

The current public release is `0.7.2`. Lifecycle and artifact formats remain
versioned independently so native and ACP backends preserve older evidence and
status readers.

### 0.7.2 CI portability

The clean CI command-construction test injects its test Grok binary explicitly.
Production binary discovery and runtime behavior remain unchanged from 0.7.1.

### 0.7.1 test portability

The repository's cross-process lock tests use independent Python subprocesses
and a plain-file barrier instead of `multiprocessing` semaphores. This preserves
real POSIX lock contention while allowing the tests to run in Grok Build's
macOS sandbox. Runtime worker behavior is unchanged from 0.7.0.

### 0.7.0 CLI surface (efficiency)

One-shot native (default):

```bash
grok-worker run --detach \
  --source "$REPO" \
  --mode implementation \
  --task-id my-task \
  --prompt-file ./task.md \
  --execution-manifest ./examples/task-manifest.json \
  --disable-web-search \
  --disallowed-tool WebSearch \
  --max-turns 80 \
  --stall-turns 8 \
  --stall-seconds 900
```

Native same-task continuation (kept clone only; not ACP):

```bash
# First turn: TTL-retain clone and write continuation metadata
grok-worker run --source "$REPO" --mode implementation \
  --task-id my-task --prompt-file ./task.md \
  --write-continuation

# Continue same task/source/clone/model/tools
grok-worker run --source "$REPO" --mode implementation \
  --task-id my-task --prompt-file ./followup.md \
  --continue --write-continuation

# Final turn without --write-continuation → normal finalize + exact session GC
grok-worker run --source "$REPO" --mode implementation \
  --task-id my-task --prompt-file ./finalize.md --continue
```

Disable runner-owned JSON Schema result capture (ACP-like disk `result.json`):

```bash
grok-worker run --source "$REPO" --mode implementation \
  --prompt-file ./task.md --no-native-json-schema
```

Cache A/B metrics appear under shared-cache `metrics/worker-runs.jsonl` with
`fresh_input_tokens`, `cached_input_tokens`, `model_calls`,
`process_duration_seconds`, and `prompt_fingerprint`. Provider cache improvement
is **not** claimed without comparing runs.

The 0.7.0 validation compared three identical one-turn prompts in three unique
physical clones: all had the same stable prefix fingerprint, but each reported
only 128 cached input tokens (about 0.76%) and took 9.0-10.6 seconds. Stable
fingerprinting is therefore diagnostic, not a cache mechanism. A real same-clone
continuation improved observed cache ratio from 72.6% to 82.5% and process time
from 123.6s to 42.2s for the tested follow-up; task sizes differed, so this is
evidence for continuation reuse, not a universal speed multiplier.
