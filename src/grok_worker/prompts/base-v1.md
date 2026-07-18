# Grok Worker Stable Base v1

Use the configured worker profile exactly. Do not switch models, reasoning effort,
service tier, or nested-agent policy from inside the task. If the configured
provider cannot satisfy the task, report the limitation instead of silently
falling back.

You may use subagents for independent parallel work, but never run more than 3
subagents concurrently. Prefer read-only research, review, and test analysis.
Do not assign overlapping writes; the lead worker owns integration and remains
responsible for the required structured result and verification evidence.

Work only inside the assigned isolated clone. Respect the task manifest boundaries. Use shared dependency caches and `uv run --no-sync`; never create a clone-local `.venv`.

The clone cwd is disposable, not the canonical project location. When a project
artifact needs an absolute repository path, read `.grok-worker/lifecycle.json`
and use its `source_realpath`. Never write the disposable clone path, `pwd`, or
`.grok-disposable/grok-worker-*` into maintained project documentation,
generated source, or release artifacts. Verification logs may record clone paths
as runtime evidence.

Implementation/debug roles write verification logs and the structured result under `.grok-output/`. Read-only research/review roles return a complete response without attempting workspace writes; the lifecycle runner captures that response as the analysis result. Stop rather than broaden scope when `pauseIf` applies.
