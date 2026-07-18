# Windows native upgrade: 0.3/0.4 to 0.5.1

This integration branch supports native Windows 10/11. It does not use WSL as
the default or as a fallback. The canonical source checkout, installed Windows
executables, managed acpx runtime, and `%USERPROFILE%\.grok\config.toml` remain
the only active runtime chain.

## What changes in 0.5.1

- One-shot `grok-worker run` defaults to the proven managed ACP chain on
  Windows. Native Grok Build headless remains available with `--backend native`.
- Named sessions also use the managed acpx runtime.
- Provider/model/effort-specific profiles preserve explicit High reasoning and
  reject reasoning downgrade warnings.
- Safe staged, unstaged, and untracked files are snapshotted automatically.
- Retained task-ID collisions allocate a fresh clone, transient clone failures
  receive one clean retry, and dependency prewarm failures become warnings.
- Repository `.mcp.json` is masked only during backend execution and restored
  before artifact capture.
- Native token/cache/reasoning metrics, backend/process health, writable
  clone-local tool caches, and `grok-worker --version` are available.
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
3. Fetch and verify the signed/annotated `v0.5.1` tag and Release.
4. Merge or port the upstream tag into an isolated worktree based on the
   existing Windows-native branch. Do not replace the Windows branch with a
   plain tag checkout.
5. Preserve Win32 file locking, reparse-safe cleanup, PowerShell 7/UTF-8,
   adaptive cmd decoding, hidden child processes, Windows process-tree cleanup,
   managed acpx, configurable worker capacity, and the shared status root.
6. Run full pytest, Ruff, mypy, local runtime/Unicode/no-window/file-tool smoke,
   and one real Grok 4.5/high end-to-end canary.
7. Install from the verified canonical source and recheck executable hashes,
   version, junction target, managed runtime, config hash, artifacts, metrics,
   `.mcp.json` restoration, and clone cleanup.

## Native preflight

```powershell
$repo = (Resolve-Path -LiteralPath "C:\CodexWS\YourProject").Path
(Get-Command grok-worker).Source
grok-worker --version
grok-worker acpx-runtime-status
grok-worker status --source $repo --json
grok models
```

`grok-worker.exe` must resolve under `%USERPROFILE%\.local\bin`. The Windows
default ACP path and named sessions require a healthy managed runtime. Explicit
native one-shot work does not require acpx. Do not switch to WSL or global acpx
when the managed runtime is unhealthy.

## Windows canary

Use a small Git repository and a prompt file. A normal dirty text file should be
snapshotted safely; secret-shaped files and escaping links must still be
rejected.

```powershell
$env:GROK_WORKER_DISPATCHER_ID = "windows-v051-canary"
grok-worker run `
  --backend acp `
  --source $repo `
  --mode implementation `
  --task-id windows-v051-acp-canary `
  --reasoning-effort high `
  --max-workers 24 `
  --prompt-file $promptFile
```

Accept a successful implementation only when the backend exits 0 and the
external artifact directory contains exactly `changes.patch`, `worker.log`, and
`verification.txt`. Confirm the structured result, tool receipts,
reasoning/cache metrics, restored `.mcp.json`, and eligible clone cleanup.

## Rollback

Restore the backed-up uv tool and executables together, then restore the
canonical repository backup or reset only the dedicated integration worktree.
Do not replace the skill junction with a copied skill and do not restore or
create a second WSL provider configuration.
