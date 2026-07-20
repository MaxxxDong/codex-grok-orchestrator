# Windows native upgrade: 0.3-0.5.3 to 0.6.0

This integration branch supports native Windows 10/11. It does not use WSL as
the default or as a fallback. The canonical source checkout, installed Windows
executables, managed acpx runtime, and `%USERPROFILE%\.grok\config.toml` remain
the only active runtime chain.

## What changes in 0.6.0

- Codex dispatchers launch one-shot work with `run --detach`, receive a
  structured receipt immediately, and wait through `watch` instead of keeping a
  foreground shell open.
- Detached execution reuses the same lifecycle, High-reasoning checks, cache
  profile, three-file artifacts, failure retention, and cleanup as foreground
  execution.
- Detached launcher logs are covered by the existing shared-cache quota and
  TTL/LRU cleanup.

## Changes retained from 0.5.3 and earlier

- `grok-worker watch` wakes immediately for terminal or attention events and
  retains a compact 300-second health heartbeat as fallback.
- `grok-worker preflight` reports all blocked relative paths and rule codes in
  one scan without exposing matched secret values.
- Startup failures can notify an already waiting dispatcher; runtime identifiers
  are no longer mistaken for literal credentials.
- The 0.5.2 native Grok, cache, session-cleanup, plugin/MCP, and High-reasoning
  behavior remains unchanged.

- One-shot `grok-worker run` defaults to native headless and no longer requires
  `acpx`.
- `--backend acp` and named sessions remain available and still require `acpx`.
- Windows terminal and file tools are verified with Grok Build 0.2.103. The
  managed acpx runtime remains the only ACP compatibility path; there is no
  global-acpx or WSL fallback.
- Workers use the native Grok home, so configured plugins, MCP servers, OAuth,
  stable-channel metadata, explicit High reasoning, and prompt-cache behavior
  remain available.
- Project-local `.uv-cache` and launcher fallback prevent sandbox-denied writes
  to the host UV cache before the Worker starts.
- The launcher prefers its installed virtual environment, so normal starts do not
  require package-network access. Clone-owned Grok sessions are removed on exit.
- Repository `.mcp.json` remains visible; extension diagnostics do not block launch.
- Ordinary staged, unstaged, and untracked files are snapshotted automatically.
  Ignored files stay excluded; suspected secrets and escaping symlinks still
  fail closed.
- Dependency prewarm failure becomes a visible warning. Task verification still
  decides success.
- A retained task-ID collision gets a fresh suffixed clone instead of blocking
  startup.
- Completion events, activity-renewed leases, verified three-file artifacts,
  capacity limits, and guarded cleanup remain in force.
- Native token/cache/reasoning metrics, hidden child processes, Win32
  process-tree cleanup, PowerShell 7/UTF-8 routing, and adaptive cmd decoding
  remain part of the Windows integration.
- Sensitive files, escaping symlinks/reparse points, artifact verification, and
  cleanup ownership remain hard gates.

## Single-source layout

- Canonical source checkout: one maintained Windows repository.
- Codex skill: a directory junction to that canonical checkout, not a copy.
- Installed commands: `%USERPROFILE%\.local\bin\grok-worker.exe` and
  `%USERPROFILE%\.local\bin\grok-worker-agent.exe`.
- ACP compatibility: the immutable grok-worker-owned runtime reported by
  `grok-worker acpx-runtime-status`; never a silent global-acpx fallback.
- Grok provider configuration: `%USERPROFILE%\.grok\config.toml`; upgrades do
  not rewrite provider URL, key, model, effort, or backend fields.

## Upgrade sequence

1. Record the current branch/commit, executable hashes, skill-junction target,
   managed acpx status, and a hash of the Grok config.
2. Back up the full canonical repository (including `.git` and dirty files) and
   the installed uv tool plus both executables.
3. Fetch and verify the annotated `v0.6.0` tag and Release.
4. Merge or port the upstream tag into an isolated worktree based on the
   existing Windows-native branch. Do not replace the Windows branch with a
   plain tag checkout or a second `git clone --branch v0.6.0` installation.
5. Preserve Win32 file locking, reparse-safe cleanup, PowerShell 7/UTF-8,
   adaptive cmd decoding, hidden child processes, Windows process-tree cleanup,
   managed acpx, configurable worker capacity, and the shared status root.
6. Run full pytest, Ruff, mypy, local runtime/Unicode/no-window/file-tool smoke,
   and one real Grok 4.5/high end-to-end canary.
7. Install from the verified canonical source and recheck executable hashes,
   version, junction target, managed runtime, config hash, artifacts, metrics,
   `.mcp.json` visibility/integrity, Native session cleanup, and clone cleanup.

## Native preflight

```powershell
$repo = (Resolve-Path -LiteralPath "C:\CodexWS\YourProject").Path
(Get-Command grok-worker).Source
grok --version
grok-worker --version
grok-worker acpx-runtime-status
grok-worker preflight --source $repo --json
grok-worker status --source $repo --json
grok models
```

`grok-worker.exe` must resolve under `%USERPROFILE%\.local\bin`. Default Native
one-shot work does not require acpx. Named sessions and explicit ACP runs require
the healthy managed runtime. Do not switch to WSL or global acpx when that
runtime is unhealthy.

## Windows canary

Use a small Git repository and a prompt file. A normal dirty text file should be
snapshotted safely; secret-shaped files and escaping links must still be
rejected.

```powershell
$env:GROK_WORKER_DISPATCHER_ID = "windows-v053-canary"
grok-worker run `
  --source $repo `
  --mode implementation `
  --task-id windows-v053-native-canary `
  --reasoning-effort high `
  --max-workers 24 `
  --prompt-file $promptFile
```

Accept a successful implementation only when the backend exits 0 and the
external artifact directory contains exactly `changes.patch`, `worker.log`, and
`verification.txt`. Confirm the structured result, tool receipts,
reasoning/cache metrics, unchanged `.mcp.json`, and eligible clone cleanup.

## Rollback

Restore the backed-up uv tool and executables together, then restore the
canonical repository backup or reset only the dedicated integration worktree.
Do not replace the skill junction with a copied skill and do not restore or
create a second WSL provider configuration.
