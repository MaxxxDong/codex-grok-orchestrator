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

## Native Windows runtime

Windows 10/11 is supported by the Python package. Lifecycle, cache, and config
transactions use shared/exclusive `LockFileEx` byte-range locks through the
standard library; per-worker lock files live under the disposable root's hidden
`.grok-worker-locks` directory so a verified clone can be removed after success.

Install `grok-worker`, `grok-worker-agent`, Node.js, PowerShell 7 (`pwsh`), and
Grok Build on the native Windows `PATH`. Before the first worker, build the
immutable grok-worker-owned acpx runtime from the pinned `acpx 0.12.0` package:

```powershell
grok-worker acpx-runtime-install
grok-worker acpx-runtime-status
```

The installer verifies the exact upstream JavaScript hash, copies the package
under `%LOCALAPPDATA%\grok-worker\runtimes\acpx`, applies the audited Windows
terminal patch to that copy, and writes a hash-verified `current.json` pointer.
It never edits the global npm package. Normal one-shot and named-session runs
resolve only this managed runtime and fail closed if it is missing, corrupted,
or bound to a different PowerShell 7 executable. `--acpx-bin` is an explicit
test/development override; there is no silent fallback to global acpx or WSL.

The managed runtime routes PowerShell terminal work and process snapshots
through PowerShell 7, explicitly sets UTF-8 for PowerShell output, records the
machine OEM code page and adaptively transcodes captured UTF-8/OEM `cmd` output
without changing redirection semantics, batches
concurrent CIM snapshots, uses Windows process-tree cleanup for cancellation,
and never uses Windows `detached` terminal launches. These constraints preserve
ACP pipes and prevent transient console windows from taking focus.

The native runtime reads the user's normal Grok configuration directly from:

```text
%USERPROFILE%\.grok\config.toml
```

Do not maintain a second WSL Grok configuration for a native deployment. Keep
backup files if required, but only the Windows path above is an active source of
provider/model settings.

For parallel dispatch, pass the same positive `--max-workers` value to all
one-shot and named-session starts sharing a disposable root, or set
`GROK_WORKER_MAX_WORKERS`. The default is 10; higher values change admission,
not the independent disposable-byte cap or upstream provider rate limits.
Clone admission is protected by a short root lock, while worker execution and
fingerprint-distinct dependency work remain concurrent. Same-fingerprint uv
preparation is serialized once and then reused by all admitted workers.

On Windows, the Grok agent launcher uses hidden `STARTUPINFO` window state when
invoking the configured executable. This keeps `.cmd`-based Grok installations
silent without detaching the ACP stdin/stdout pipes, preserving the same
foreground lifecycle, cancellation, timeout, and process-tree cleanup behavior.

`--no-prepare-deps` disables environment creation completely. Its injected
contract forbids `uv`, `uv run`, `uv sync`, and `pip`, because even
`uv run --no-sync` creates a clone-local `.venv` when
`UV_PROJECT_ENVIRONMENT` is absent. Tasks using this option must rely on
pre-existing system tools or an explicitly supplied absolute interpreter.

## 1. Completion-event notifications

### What it is

When a managed worker reaches a terminal state (`success`, `failed`, `keep`, …),
the runner appends **one** JSON line under:

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

Forbidden in events: prompt text, tokens, API keys, environment maps,
stdout/stderr, model/agent output, MCP config.

### Dedup and concurrency

- Dedup key: `(task_id, state)`. Re-finalize / re-reconcile of the same terminal
  pair does not append again.
- Appends take an exclusive lock beside the log; writes are full JSON lines with
  flush/fsync so concurrent writers do not leave half-line JSON.
- **Best-effort only**: notification I/O or serialization failures never reverse
  an already-persisted lifecycle terminal state, `RunOutcome`, artifact path, or
  GC dead-worker reconcile. Callers treat emit as advisory.
- Readers **discard** malformed JSONL rows, empty objects (`{}`), missing
  required pointer fields, and wrong-typed values; they never surface incomplete
  rows as events.

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

## 2. Status summary

### Per-clone fields (`status --json`)

Each managed clone entry includes:

| Field | Source / notes |
|---|---|
| `phase` | Lifecycle state only (never progress `"success"` over running) |
| `last_activity_at` | Best usable of lifecycle `updated_at`, progress timestamps, file mtimes; timestamps more than **5s** in the future of status time (clock-skew tolerance) are ignored so they cannot manufacture recent activity |
| `elapsed_seconds` | Active (`creating`/`running`/`finalizing`/…): now − `created_at`. Terminal (`success`/`failed`/`keep`/…): frozen at `updated_at − created_at` |
| `timeout_seconds` | Optional lifecycle field set at create; null on legacy metadata |
| `remaining_seconds` | Active: `timeout − elapsed` when timeout known. Terminal: always **null** (countdown is not live) |
| `result_ready` | True only if clone has a real `.grok-output/result.json` file |
| `artifact_ready` | True only when metadata marks complete **and** artifact path exists |
| `resources.cpu_percent` / `resources.rss_bytes` | Best-effort via short-timeout `ps` on preferred PID `acpx_pid` → `runner_pid` → legacy `pid`; null when inactive/unsupported |

### Fail-soft progress

Illegal, truncated, or wrong-typed `progress.json` is ignored. Status collection
must not raise. Progress must never promote a running lifecycle into success.
Future or unparseable advisory timestamps never override lifecycle `updated_at`.

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

The initial standalone public release is `0.3.0`. Lifecycle and artifact formats
remain versioned independently so future CLI releases can preserve compatibility.
