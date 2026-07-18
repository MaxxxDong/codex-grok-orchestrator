# Windows / WSL upgrade: 0.3.0 to 0.4.2

Native Windows remains unsupported because `grok-worker` relies on POSIX `flock` semantics. Use WSL2 with Ubuntu for the runtime. Keep Windows-native Codex configuration and WSL runtime paths separate.

## What changes in 0.4.2

- Dispatcher-scoped concurrency replaces the older root-only capacity assumption.
- Completion events and read-only health inspection expose terminal and active state sooner.
- Activity-renewed idle leases replace a fixed launch-time lifetime; a separate hard cap remains adjustable at runtime.
- Every Grok ACP process uses a managed profile that excludes user plugins and MCP servers, keeps credentials out of the derived config, and prevents repository `.mcp.json` discovery.
- Dirty source inclusion is path-allowlisted. Bare `--include-dirty` is refused when real nonignored dirt exists.
- Workers may use at most 3 non-overlapping concurrent subagents; `--no-subagents` disables them.
- External success artifacts remain exactly `changes.patch`, `worker.log`, and `verification.txt`.

## Recommended side-by-side upgrade

Run the following inside WSL Ubuntu, not PowerShell or native `cmd.exe`.

```bash
set -euo pipefail

stamp="$(date +%Y%m%d-%H%M%S)"
skill_root="$HOME/.codex/skills"
old="$skill_root/grok-worker"
backup="$skill_root/grok-worker-0.3-backup-$stamp"

mkdir -p "$skill_root"
if [ -d "$old" ]; then
  mv "$old" "$backup"
fi

git clone --branch v0.4.2 --depth 1 \
  https://github.com/MaxxxDong/codex-grok-orchestrator.git \
  "$old"

cd "$old"
uv sync --extra dev
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

Do not overwrite `$HOME/.grok/config.toml` or `$HOME/.grok/Agents.md` with repository examples. Provider credentials and local policy stay outside the public repository.

Expose the verified source launchers:

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/.codex/skills/grok-worker/bin/grok-worker" \
  "$HOME/.local/bin/grok-worker"
ln -sfn "$HOME/.codex/skills/grok-worker/bin/grok-acp-worker" \
  "$HOME/.local/bin/grok-acp-worker"
export PATH="$HOME/.local/bin:$PATH"
```

## Preflight and smoke

```bash
acpx --version
grok --version
grok models
grok-worker --help
grok-worker cache-status
```

Use a clean test repository for the first live run and assign one stable dispatcher ID:

```bash
export GROK_WORKER_DISPATCHER_ID="windows-upgrade-smoke-$(date +%s)"

grok-worker run \
  --source /path/to/clean/test-repository \
  --dispatcher-id "$GROK_WORKER_DISPATCHER_ID" \
  --mode analysis \
  --task-id windows-upgrade-smoke \
  --no-subagents \
  --prompt "Read-only smoke test. Report the branch and short HEAD."
```

Confirm `state=success`, `exit_code=0`, and a verified three-file artifact directory before using 0.4.2 for implementation work.

## Migration cautions

- Do not copy a 0.3 disposable directory into the new runtime. It is historical evidence, not an install source.
- Do not delete unknown legacy clones. Inspect them with `grok-worker list-legacy --disposable-root PATH`.
- Replace bare `--include-dirty` with one or more reviewed `--include-dirty-path PATH` arguments.
- Reuse one opaque `dispatcher_id` only within one Root task; different Root tasks need different IDs.
- A completion-event wait timeout means only “no matching event yet”, not worker failure.
- Use `grok-worker lease-set` to adjust an active task instead of restarting it solely to change time limits.

## Rollback

If verification fails, stop new dispatches and restore the side-by-side backup path:

```bash
failed="$HOME/.codex/skills/grok-worker-failed-$(date +%Y%m%d-%H%M%S)"
mv "$HOME/.codex/skills/grok-worker" "$failed"
mv "$HOME/.codex/skills/grok-worker-0.3-backup-REPLACE_TIMESTAMP" \
  "$HOME/.codex/skills/grok-worker"
```

Preserve the failed 0.4.2 directory and artifacts for diagnosis instead of deleting them.
