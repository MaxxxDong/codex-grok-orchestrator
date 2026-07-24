# macOS native upgrade: 0.3-0.7.2 to 0.8.0

Version 0.8.0 keeps the existing provider configuration and native Grok Build
runtime. It does not install a second provider profile or change credentials.

## Requirements

- macOS with Python 3.12+, Git, and `uv`
- Grok Build 0.2.111 or newer on `PATH`
- An independently authenticated and configured Grok CLI

## Upgrade

```bash
uv tool install --force "git+https://github.com/MaxxxDong/codex-grok-orchestrator.git@v0.8.0"
grok-worker --version
```

Existing provider, cache, session, and artifact roots remain external to the
package and are preserved by the upgrade.

## Verify

```bash
grok-worker preflight --source /path/to/repository --json
grok-worker run \
  --detach \
  --source /path/to/repository \
  --backend native \
  --reasoning-effort high \
  --mode analysis \
  --task-id macos-080-smoke \
  --prompt "Inspect the repository and report one concrete fact. Do not edit files."
```

Pass the returned run ID to `grok-worker watch --until-settled`. A valid run must
reach a terminal state, finish cleanup, and preserve the documented three-file
artifact contract.

## Roll back

```bash
uv tool install --force "git+https://github.com/MaxxxDong/codex-grok-orchestrator.git@v0.7.2"
```
