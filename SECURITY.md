# Security policy

## Supported versions

Security fixes are applied to the latest released minor version. Pre-release and source snapshots are supported on a best-effort basis.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities involving unsafe deletion, path escape, lifecycle forgery, credential exposure, config rollback, or cache-lock bypass.

Use GitHub private vulnerability reporting after it is enabled for the public repository. Include the affected version, platform, minimal reproduction, expected safety invariant, and whether a disposable or protected path was touched. Do not include live credentials, private repository content, or raw provider logs.

Until a public remote with private reporting is configured, keep the report private and do not publish exploit details. This local source tree intentionally does not invent an email address or external contact that has not been established.

## Security boundaries

- Provider authentication is external and never stored by `grok-worker`.
- Workers may edit only their isolated clone in implementation mode.
- Successful clone deletion requires verified external artifacts.
- Native Windows locking is not supported; use WSL rather than weakening lock semantics.
- The `[claude_compat] imported = true` discovery guard in derived profiles does
  not replace enforcement: `grok inspect --json` still fail-closes on any
  Grok-level plugin or MCP server. See the managed-profile section in
  [README.md](README.md).
