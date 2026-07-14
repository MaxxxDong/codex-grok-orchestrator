# Release checklist

1. Confirm the version and date in `pyproject.toml` and `CHANGELOG.md`.
2. Run `uv lock` and verify the lockfile diff contains only intended dependency changes.
3. Run `ruff`, `mypy`, and the full pytest suite on macOS and Linux.
4. Build both sdist and wheel, inspect their file lists, and install the wheel into a clean environment.
5. Smoke-test `grok-worker --help` and `grok-worker-agent` lifecycle refusal from the clean environment.
6. Scan the tracked tree and Git history for credentials, private endpoints, personal paths, runtime state, and generated worker artifacts.
7. Run a read-only Grok release audit and review every finding; do not let the worker publish its own release.
8. Confirm README links, license metadata, security reporting, and supported-platform claims.
9. Tag the verified commit. Create and publish the GitHub release only from that exact commit.

Release automation must never mutate a live provider config or publish when verification is incomplete.
