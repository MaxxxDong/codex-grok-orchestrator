# Release checklist

1. Choose a new semantic version for every releasable code or behavior update.
   Confirm the same version and date in `pyproject.toml`, runtime
   `grok_worker.__version__`, `uv.lock`, `CHANGELOG.md`, release notes,
   install commands, operations, and upgrade documentation. Never publish new
   behavior under the previous release number.
2. Run `uv lock` and verify the lockfile diff contains only intended dependency changes.
3. Run `ruff`, `mypy`, and the full pytest suite on macOS and Linux.
4. Build both sdist and wheel, inspect their file lists, and install the wheel into a clean environment.
5. Smoke-test `grok-worker --help` and `grok-worker-agent` lifecycle refusal from the clean environment.
6. Scan the tracked tree and Git history for credentials, private endpoints, personal paths, runtime state, and generated worker artifacts.
7. Run a read-only Grok release audit and review every finding; do not let the worker publish its own release.
8. Confirm README links, license metadata, security reporting, and supported-platform claims.
9. Regression-check: safe dirt auto-snapshots; ignored `.env`, sensitive content,
   and escaping symlinks remain blocked/excluded; native High downgrade fails;
   repository `.mcp.json` is restored; retained task IDs allocate new clones;
   OS flock slot leases, event wait 0/30/600 bounds, prompt-only source rules,
   adaptive leases, and diagnostic-only health remain correct.
   Detached launch must return a receipt, wake `watch` on terminal/attention,
   preserve the foreground lifecycle contract, and keep launch logs under shared
   cache quota/TTL cleanup.
10. Run one native live smoke and, when ACP compatibility is shipped, one ACP
    smoke. Verify exact artifacts and inspect locally observable token/reasoning
    metrics without inferring provider cache hits.
11. Tag the verified commit. Create and publish the GitHub release only from that exact commit.

Release automation must never mutate a live provider config or publish when verification is incomplete.
