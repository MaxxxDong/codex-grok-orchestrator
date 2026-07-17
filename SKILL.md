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

## Choose a run mode

- Use `run` for one bounded task that fits one prompt.
- Use `session-start`, `session-followup`, and `session-finalize` when several turns must share one immutable task, role, mode, base commit, and permission profile.
- Use `--mode analysis` for read-only work. Use `--mode implementation` only when repository edits are expected.

## Minimal workflow

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

Subagents are disabled by default. Enable them explicitly with `--allow-subagents` only when nested delegation is intentional.

## Safety invariants

- Lifecycle metadata is authoritative; notification events are advisory.
- Artifact verification happens before successful clone deletion.
- Deletion is direct-child, manifest-scoped, and symlink-safe.
- Shared dependency caches use leases; cache GC cannot evict active buckets.
- Config apply is atomic, smoke-tested, and byte-exact on rollback.
- No secrets, prompts, agent output, or environment maps belong in event logs.

See [README.md](README.md), [docs/design-principles.md](docs/design-principles.md), and [docs/operations.md](docs/operations.md) for installation and contracts.
