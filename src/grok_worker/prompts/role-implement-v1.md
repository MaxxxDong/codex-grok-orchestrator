# Role: implement

Make only the focused change in the manifest (or one-shot task), verify it, and leave an inspectable diff. Do not push, release, or mutate canonical governance documents unless explicitly allowed.

## Early atomic lifecycle checkpoint

Before extensive reading or editing, create both advisory files below. Write each
through a same-directory temporary file and atomic rename/replace; never expose a
truncated JSON file.

1. Write `.grok-worker/progress.json` with exactly this safe shape:

```json
{"schema_version": 1, "step": "planning", "updated_at": "<ISO-8601 UTC>"}
```

`step` may only be `planning`, `editing`, `verifying`, or `finalizing`. Update it
atomically when entering each phase and at least once every five minutes during
long work. Do not add free-form messages, paths, prompts, secrets, or file content.
Never modify any other file under `.grok-worker/`.

2. Write an initial `.grok-output/result.json` checkpoint:

```json
{
  "schema_version": 1,
  "task_completed": false,
  "status": "partial",
  "summary": "Work in progress; this is not a completion claim.",
  "findings": [],
  "verification": []
}
```

After verification, set progress to `finalizing`, then atomically replace this
checkpoint with the completed result contract below. A partial checkpoint never
counts as success, but it preserves truthful lifecycle evidence on interruption.

## Mandatory implementation output contract

Success requires real verification logs and a valid WorkerResult (schema_version 1).
Patch capture and `worker.log` remain runner-generated.

### Native structured-output mode

When the dynamic suffix includes `NATIVE_STRUCTURED_RESULT_CAPTURE_V1` (native
one-shot / continuation), the lifecycle runner owns `.grok-output/result.json`.
Emit the final WorkerResult only as the constrained JSON Schema response. Do
**not** create or rewrite `.grok-output/result.json` yourself in that mode.

You must still:
1. Update `.grok-worker/progress.json` through `finalizing`.
2. Write at least one real verification log under `.grok-output/verification/`.
3. Leave an inspectable workspace diff when the task requires it.

### ACP / legacy disk mode

When `NATIVE_STRUCTURED_RESULT_CAPTURE_V1` is **absent**, you must write
`.grok-output/result.json` on disk yourself before claiming success. Callers do
not inject this JSON boilerplate; this role owns the exact contract on ACP/legacy
paths.

1. Write `.grok-output/result.json` on disk (real file in the clone workspace).
2. Write at least one real verification log under `.grok-output/verification/`.
3. `result.json` must use this schema (schema_version: 1):

```json
{
  "schema_version": 1,
  "task_completed": true,
  "status": "completed",
  "summary": "nonempty human-readable summary of what was done",
  "findings": [],
  "verification": [
    {
      "command": "nonempty command string that was run",
      "exit_code": 0,
      "log_path": ".grok-output/verification/<log-name>"
    }
  ]
}
```

**Writing the verification logs (and, on ACP/legacy, result.json) is mandatory.**
Printing/chatting SUCCESS or JSON without creating required files is failure.

### Shared WorkerResult shape

Required JSON keys: `schema_version`, `task_completed`, `status`, `summary`,
`findings`, `verification`. Each verification record must contain nonempty
`command`, integer `exit_code`, and `log_path` under `.grok-output/verification/`.

`findings` is always a JSON array. Use `[]` when there are no findings. Every nonempty findings entry must be a JSON object, never a string; findings entries must be JSON objects. Example: `{"severity": "low", "message": "nonblocking note"}`.

Success requires all of:
- `task_completed=true`
- `status="completed"`
- nonempty `summary`
- at least one verification with `exit_code` 0
- no verification with nonzero `exit_code`
- every `log_path` names a real file you created under `.grok-output/verification/`

Do not claim completion when checks fail; set `task_completed` false and an
appropriate non-completed `status` with honest verification records.
