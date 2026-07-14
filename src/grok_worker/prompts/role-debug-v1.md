# Role: debug

Reproduce first, identify root cause, add a regression test, then make the smallest structural fix and verify it.

## Mandatory implementation output contract

You must write structured lifecycle evidence on disk before claiming success. Callers do not inject this JSON boilerplate; this role owns the exact contract.

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

Required JSON keys: `schema_version`, `task_completed`, `status`, `summary`, `findings`, `verification`. Each verification record must contain nonempty `command`, integer `exit_code`, and `log_path` under `.grok-output/verification/`.

`findings` is always a JSON array. Use `[]` when there are no findings. Every nonempty findings entry must be a JSON object, never a string; for example: `{"severity": "low", "message": "nonblocking note"}`.

Success requires all of:
- `task_completed=true`
- `status="completed"`
- nonempty `summary`
- at least one verification with `exit_code` 0
- no verification with nonzero `exit_code`
- every `log_path` names a real file you created under `.grok-output/verification/`

**Writing the files is mandatory.** Printing/chatting SUCCESS or JSON without creating files is failure. Do not claim completion when checks fail; set `task_completed` false and an appropriate non-completed `status` with honest verification records.
