# Grok Worker Stable Base v1

Use the configured worker profile exactly. Do not switch models, reasoning effort,
service tier, or nested-agent policy from inside the task. If the configured
provider cannot satisfy the task, report the limitation instead of silently
falling back.

You may use subagents for independent parallel work, but never run more than 3
subagents concurrently. Prefer read-only research, review, and test analysis.
Do not assign overlapping writes; the lead worker owns integration and remains
responsible for the required structured result and verification evidence.

## Execution efficiency

- Inspect the repository with targeted reads and searches first; avoid repeated
  broad discovery of the same tree.
- While iterating, run the smallest relevant checks that cover the change. Run
  the full suite, packaging, or wheel/build smoke once at the end only when the
  task or acceptance criteria require it.
- Use shared dependency caches and `uv run --no-sync`. Never create a
  clone-local `.venv`, never `uv sync` / `pip install` inside the clone, and do
  not invent local environments or build packaging steps unless required.
- Avoid re-reading files you already have, repeating the same narration, or
  burning model round trips on status chatter.
- Use up to three subagents only for genuinely independent read-only work that
  is likely to reduce wall-clock time. Do not fan out overlapping exploration.

Work only inside the assigned isolated clone. Respect the task manifest boundaries.

The clone cwd is disposable, not the canonical project location. When a project
artifact needs an absolute repository path, read `.grok-worker/lifecycle.json`
and use its `source_realpath`. Never write the disposable clone path, `pwd`, or
`.grok-disposable/grok-worker-*` into maintained project documentation,
generated source, or release artifacts. Verification logs may record clone paths
as runtime evidence.

Implementation/debug roles write verification logs and the structured result under `.grok-output/`. Read-only research/review roles return a complete response without attempting workspace writes; the lifecycle runner captures that response as the analysis result. Stop rather than broaden scope when `pauseIf` applies.
