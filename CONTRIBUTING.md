# Contributing

Thanks for helping improve `grok-worker`. Keep changes narrow, evidence-backed, and consistent with the hard invariants in [docs/design-principles.md](docs/design-principles.md).

## Development setup

```bash
git clone <your-fork>
cd grok-worker
uv sync --extra dev
uv run pytest
```

Before opening a pull request, run:

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest
uv build
```

Tests that exercise deletion, config apply, or cache GC must use temporary directories. Never point a test at a real home directory, credential store, source checkout, or shared cache.

## Pull requests

- Explain the invariant or user-visible behavior being changed.
- Add a focused regression test before changing high-risk lifecycle behavior.
- Keep prompts, generated artifacts, provider credentials, local runtime state, and personal paths out of commits.
- Do not add silent provider/model fallbacks or another source of lifecycle truth.
- Update README, operations, schema, or changelog when their contract changes.

## Version discipline

- Every releasable code or behavior update must choose a new semantic version
  before it is merged for publication. Do not leave released behavior under the
  previous version number.
- Keep `pyproject.toml`, `grok_worker.__version__`, `uv.lock`, `CHANGELOG.md`,
  release notes, current install commands, and upgrade documentation on the same
  version. The public-release tests enforce this invariant.
- Tags are immutable and use `v<version>`. Tag only the verified commit, then
  create the GitHub Release from that exact tag.

Report security-sensitive findings through the private process in [SECURITY.md](SECURITY.md), not a public issue.
