# Windows / WSL upgrade: 0.3-0.5.3 to 0.7.0

Native Windows remains unsupported because `grok-worker` uses POSIX `flock`,
signals, and process-group semantics. Run it inside WSL2 Ubuntu. Native Grok
headless means “direct Grok Build CLI without ACP”; it does not remove the WSL
requirement.

## What changes in 0.7.0

- Optional execution contracts on manifests and `--execution-manifest`.
- Native same-task continuation (`--continue` / `--write-continuation`) with an
  automatic 24-hour bounded keep; not routed through ACP.
- Runner-owned native JSON Schema final-result capture; ACP still writes
  `result.json` on disk.
- Opt-in `--disable-web-search`, `--disallowed-tool`, `--max-turns`.
- Productive-progress attention (`--stall-turns` / `--stall-seconds`).
- Stable prompt fingerprints and honest cache A/B metric fields.

## What changes in 0.6.1

- `cache-status --json` and `cache-gc --json` are accepted compatibility flags.
- Invalid CLI options print a concise Click usage error (no Python/Rich traceback).
- Metrics persist bounded cache ratios with an explicit basis, optional
  `model_calls`, and monotonic `process_duration_seconds` for one-shot runs.
- The stable Worker prompt includes concise execution-efficiency rules.

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

## Recommended WSL2 layout

Keep the runtime and active repositories in the WSL filesystem, for example
`/home/<user>/CodexWS`, rather than `/mnt/c`. This avoids slow metadata calls and
Windows/WSL permission translation. Keep Windows-native Codex configuration and
WSL `~/.codex` / `~/.grok` separate.

Do not overwrite `~/.grok/config.toml`, `~/.grok/Agents.md`, provider credentials,
or OAuth state during the upgrade.

## Side-by-side upgrade

Run inside WSL Ubuntu:

```bash
set -euo pipefail

stamp="$(date +%Y%m%d-%H%M%S)"
skill_root="$HOME/.codex/skills"
old="$skill_root/grok-worker"
backup="$skill_root/grok-worker-pre-0.5-$stamp"

mkdir -p "$skill_root"
if [ -d "$old" ]; then
  mv "$old" "$backup"
fi

git clone --branch v0.7.0 --depth 1 \
  https://github.com/MaxxxDong/codex-grok-orchestrator.git \
  "$old"

cd "$old"
uv sync --extra dev
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

Expose the verified launcher:

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/.codex/skills/grok-worker/bin/grok-worker" \
  "$HOME/.local/bin/grok-worker"
export PATH="$HOME/.local/bin:$PATH"
```

Only expose the ACP adapter when named sessions or `--backend acp` are needed:

```bash
ln -sfn "$HOME/.codex/skills/grok-worker/bin/grok-acp-worker" \
  "$HOME/.local/bin/grok-acp-worker"
```

## Preflight

```bash
grok --version
grok-worker --version
grok models
grok-worker --help
grok-worker run --help
grok-worker cache-status
```

For the ACP compatibility path also run:

```bash
acpx --version
```

## Native smoke

Use a small Git repository under the WSL filesystem. It may contain an ordinary
uncommitted text file; v0.5 should snapshot it instead of refusing startup.

```bash
export GROK_WORKER_DISPATCHER_ID="windows-v05-smoke-$(date +%s)"

grok-worker run \
  --backend native \
  --source /home/<user>/CodexWS/smoke-repository \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --mode analysis \
  --task-id windows-v05-native-smoke \
  --no-subagents \
  --prompt "Read-only smoke. Report branch, short HEAD, and working-tree state."
```

Verify current evidence, not only terminal text:

```bash
grok-worker status --source /home/<user>/CodexWS/smoke-repository --json
grok-worker health --source /home/<user>/CodexWS/smoke-repository --json
```

Accept the smoke only when the lifecycle reports `exit_code=0` and the artifact
directory contains exactly `changes.patch`, `worker.log`, and `verification.txt`.
Inspect metrics when present. A cache miss is not a failure; an ignored reasoning
effort warning is a failure in v0.5.

## ACP compatibility smoke

Run this only when ACP is needed:

```bash
grok-worker run \
  --backend acp \
  --source /home/<user>/CodexWS/smoke-repository \
  --mode analysis \
  --task-id windows-v05-acp-smoke \
  --prompt "Read-only ACP compatibility smoke."
```

Named `session-start` / `session-followup` / `session-finalize` commands remain
ACP-backed in 0.5.x.

## Migration cautions

- Do not copy old disposable clones into the new runtime. They are evidence, not
  installation files.
- Do not delete unknown legacy clones. Inspect them with
  `grok-worker list-legacy --disposable-root PATH`.
- Old dirty allowlist flags may remain in scripts, but they no longer filter the
  snapshot; all safe nonignored dirt is included. Remove the flags after
  confirming v0.5 behavior.
- Reuse one opaque dispatcher ID only within one Root task.
- A completion-event wait timeout means only “no matching event yet.”
- Use `grok-worker lease-set` to adjust active idle/hard limits rather than
  restarting a healthy worker.
- Keep a hard cap unless a specific controlled task needs `--hard-timeout 0`.

## Rollback

Stop new dispatches, preserve failed artifacts, then restore the backup:

```bash
failed="$HOME/.codex/skills/grok-worker-failed-$(date +%Y%m%d-%H%M%S)"
mv "$HOME/.codex/skills/grok-worker" "$failed"
mv "$HOME/.codex/skills/grok-worker-pre-0.5-REPLACE_TIMESTAMP" \
  "$HOME/.codex/skills/grok-worker"
```

Rollback restores the Skill/runtime only. It must not replace `~/.grok` provider
configuration or delete retained worker evidence.
