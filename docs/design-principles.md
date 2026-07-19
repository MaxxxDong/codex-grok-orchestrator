# Design principles

This document separates the public engine's hard safety invariants from deployment policy that users may configure. That boundary prevents a reusable worker runner from becoming a bundle of one operator's paths, provider choices, or approval rules.

## Public core: hard invariants

### 1. Isolate before execution

Every task starts from a source repository and runs in a disposable clone. The source remains canonical. A worker must not edit the source checkout directly or persist a disposable path into maintained project artifacts.

### 2. One authoritative lifecycle

`.grok-worker/lifecycle.json` is the state authority. Progress hints, completion events, logs, and process observations are advisory. They may help a dispatcher wake quickly, but cannot manufacture success, override lifecycle state, or authorize deletion.

Concurrency is **per dispatcher** when an explicit dispatcher ID is set: up to
10 concurrent **active Grok invocations** via fixed OS `flock` leases under
`$CACHE/dispatchers/<hash>/slots/00.lock..09.lock`. There is no machine-global
worker limit and no persistent roots/slot JSON registry. Without an explicit ID,
only root-scoped limits apply—never claim a cross-root guarantee that cannot be
enforced. Idle named sessions (`SESSION_OPEN`) do not permanently reserve
capacity; each ACP turn takes a transient slot. Same-source implementation work
may run concurrently because every worker owns an independent clone; the Root
dispatcher remains the sole integration owner and must serialize acceptance of
overlapping changes. Capacity refusals never preempt peers.

### 3. Evidence before reclamation

A successful task is reclaimable only after its external artifact directory passes the three-file contract:

- `changes.patch` — reviewable diff, including an empty but valid patch for read-only work
- `worker.log` — lifecycle, session provenance, and captured agent output
- `verification.txt` — structured result, verification records, cleanup receipt, metrics, and hashes

The clone-local `.grok-output/result.json` is embedded into `verification.txt`; it is not a fourth external artifact.

Failed and ambiguous runs are retained for diagnosis. Legacy or unmarked directories are never silently adopted or deleted.

### 4. Deletion fails closed

Deletion targets must be direct managed children of the configured disposable root, match lifecycle identity, and survive realpath and symlink checks. Protected paths include the source, artifact root, shared cache, home directory, and disposable root itself.

### 5. Permissions are part of task identity

Mode, agent entry, MCP config, model, reasoning profile, and subagent policy form a permission signature. Named-session follow-ups must match the original immutable contract; drift requires a new session.

The agent process uses the user's native Grok home. Existing plugins, MCP servers,
OAuth state, and provider configuration remain available; the runner never edits
that configuration. A pre-launch `grok inspect` is advisory and cannot block the
actual process because an optional extension failure is not proof that Grok is
unusable. Native analysis additionally uses Grok's OS `read-only` sandbox and
`plan` permission mode. The disposable clone, not a second user profile, remains
the repository write boundary.

### 6. Shared caches need leases

Workers may reuse dependency environments and package caches, but active buckets hold leases. Capacity checks and GC take exclusive locks and must not evict resources still in use.

### 7. Runtime limits should follow activity, not launch estimates

A task's expected duration is not knowable at process start. Workers therefore
use a renewable inactivity lease plus a separately adjustable hard safety cap.
Managed Grok session events and bounded filesystem signals renew the lease;
mere PID existence does not. Operators can change either policy without
restarting the ACP session.

### 8. Observability stays secret-minimal

Notifications contain identifiers, terminal state, timestamps, and artifact pointers—not prompts, API keys, tokens, environment maps, stdout, stderr, or model output. Lifecycle truth is re-read after every wake-up.

### 9. Configuration changes are transactional

Config apply parses the candidate first, writes atomically, runs a bounded shell-free smoke command, and restores the exact original bytes on failure or timeout. Receipts contain hashes and state, never config bodies or captured output.

### 10. Integration remains a reviewer decision

Workers produce evidence; they do not merge, push, publish, submit, or approve their own work. The dispatcher or human reviewer owns acceptance and external side effects.

## Configurable public policy

These values have safe defaults but are intentionally adjustable:

- model and reasoning profile
- whether nested subagents are allowed
- worker concurrency and disposable capacity
- cache size and TTL
- ACP/MCP configuration path
- one-shot versus named-session execution
- analysis versus implementation permission mode

Changing one of these must remain explicit and observable. The runner must not silently fall back to another model, provider, permission mode, or execution path.

## Private overlays: intentionally excluded

The public repository must not contain:

- personal absolute paths, hostnames, or account identifiers
- API keys, OAuth state, relay endpoints, or live MCP files
- competition-specific Gate A/B/C, registration, submission, or scoring policy
- a mandatory single model/provider rule presented as a universal invariant
- organization-specific approval chains, document names, or reviewer identities

Those concerns can be layered on top through a private Skill, dispatcher prompt, CI policy, or environment configuration without forking the lifecycle engine.
