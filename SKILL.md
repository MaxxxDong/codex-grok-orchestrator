---
name: grok-worker
description: Use when an agent needs to delegate bounded repository analysis, implementation, debugging, research, or review to a lifecycle-managed Grok ACP worker.
---

# Grok Worker

`grok-worker` runs Grok in an isolated clone, enforces lifecycle and capacity rules, and returns verifiable artifacts for a dispatcher to review. It is an execution boundary, not an autonomous merge or publishing system.

## Required boundary

Always enter through `grok-worker`. Do not invoke the ACP adapter directly: it intentionally refuses calls without lifecycle context.

The source repository remains canonical. Workers operate in disposable clones and must never write clone paths into maintained project files. The dispatcher reviews artifacts and decides what, if anything, is integrated.

On native Windows, use the installed `grok-worker.exe` entry. It runs the same Python lifecycle implementation with Windows process locks and reads `%USERPROFILE%\.grok\config.toml` as the single active Grok configuration; do not route through WSL or maintain a second provider config.

Before the first Windows run in a task, resolve both paths explicitly:

```powershell
$repo = (Resolve-Path -LiteralPath "C:\CodexWS\YourProject").Path
(Get-Command grok-worker).Source
```

The command must resolve to `%USERPROFILE%\.local\bin\grok-worker.exe`. Pass the resolved Windows repository path to `--source`; do not translate it to `/mnt/c/...`.

## Choose a run mode

- Use `run` for one bounded task that fits one prompt.
- Use `session-start`, `session-followup`, and `session-finalize` when several turns must share one immutable task, role, mode, base commit, and permission profile.
- Use `--mode analysis` for read-only work. Use `--mode implementation` only when repository edits are expected.

## Minimal workflow

Native Windows PowerShell:

```powershell
$repo = (Resolve-Path -LiteralPath "C:\CodexWS\YourProject").Path
$promptFile = Join-Path $env:TEMP "grok-worker-task.md"
grok-worker status --source $repo --json
grok-worker run --source $repo --mode implementation --task-id focused-change --prompt-file $promptFile --max-workers 24
```

The prompt file must already exist. Prefer a prompt file for multiline tasks so PowerShell, batch wrappers, and ACP receive byte-identical text.
Use the same positive `--max-workers` value for every dispatcher sharing a disposable root when planning wide fan-out. Admission remains serialized only for clone creation; admitted workers execute concurrently. The byte cap and provider rate limits remain independent safety boundaries.

POSIX shells:

Preflight:

```bash
grok-worker status --source "$REPO" --json
```

One-shot implementation:

```bash
grok-worker run \
  --source "$REPO" \
  --mode implementation \
  --task-id focused-change \
  --prompt-file /tmp/task.md
```

Read-only review:

```bash
grok-worker run \
  --source "$REPO" \
  --mode analysis \
  --task-id release-audit \
  --prompt "Audit packaging and public-release risks. Do not edit files."
```

Only accept success after the external artifact directory contains the verified three-file contract: `changes.patch`, `worker.log`, and `verification.txt`. The structured worker result is embedded in `verification.txt`; clone-local `.grok-output/result.json` is not a fourth external artifact. Failure clones are retained for diagnosis; legacy or unmarked directories are never deleted automatically.

## Configurable profile

The public core does not hard-lock one model or provider setup. Configure it with CLI flags or environment variables:

- `GROK_WORKER_MODEL`
- `GROK_WORKER_REASONING_EFFORT`
- `GROK_WORKER_MCP_CONFIG`
- `GROK_WORKER_GROK_BIN`
- `GROK_WORKER_CACHE_ROOT`
- `GROK_WORKER_MAX_WORKERS` (or `--max-workers`; default `10`)

Subagents are disabled by default. Enable them explicitly with `--allow-subagents` only when nested delegation is intentional.

`--no-prepare-deps` is a strict no-environment-creation mode: the worker must not
run `uv`, `uv run`, `uv sync`, or `pip`. Use it only when the task can rely on
pre-existing system tools or an explicitly supplied absolute interpreter path.

## Safety invariants

- Lifecycle metadata is authoritative; notification events are advisory.
- Artifact verification happens before successful clone deletion.
- Deletion is direct-child, manifest-scoped, and symlink-safe.
- Shared dependency caches use leases; cache GC cannot evict active buckets.
- Config apply is atomic, smoke-tested, and byte-exact on rollback.
- No secrets, prompts, agent output, or environment maps belong in event logs.

See [README.md](README.md), [docs/design-principles.md](docs/design-principles.md), and [docs/operations.md](docs/operations.md) for installation and contracts.
