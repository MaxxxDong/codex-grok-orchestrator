# Changelog

All notable public changes are recorded here. The project follows semantic versioning while the CLI is pre-1.0.

## [0.3.0] - 2026-07-14

### Added

- Initial standalone public repository.
- Configurable model, reasoning effort, optional MCP path, and explicit subagent policy.
- Installable `grok-worker` and `grok-worker-agent` console entry points.
- Public design, operations, contribution, security, and release documentation.
- A complete Simplified Chinese introduction, feature overview, and usage guide.

### Changed

- Moved stable worker prompts into the Python package for wheel installation.
- Removed personal paths, private provider configuration, and competition-specific policy from the public core.
- Native Windows now fails clearly where POSIX locking is required; WSL is recommended.
