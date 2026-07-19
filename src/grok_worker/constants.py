"""Fixed defaults and path names for the lifecycle runner."""

from __future__ import annotations

SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
MANAGED_BY = "grok-worker-lifecycle"

DEFAULT_CAP_BYTES = 6 * 1024**3  # exactly 6 GiB
DEFAULT_FAILURE_RETAIN_HOURS = 24
DEFAULT_TMP_AGE_HOURS = 24
# ``--timeout`` is now an inactivity lease, renewed by observable worker activity.
# The old name remains as a compatibility alias for callers importing it.
DEFAULT_IDLE_TIMEOUT = 1800
DEFAULT_ACPX_TIMEOUT = DEFAULT_IDLE_TIMEOUT
LONG_TASK_TIMEOUT = 3600
DEFAULT_HARD_TIMEOUT = 24 * 60 * 60
LEASE_POLL_SECONDS = 2.0
MAX_CONCURRENT_WORKERS = 10  # per-dispatcher when dispatcher_id set; else root-scoped
MAX_TASK_ID_LEN = 64

# Completion-event wait bounds (caller may repeat; not a worker timeout).
DEFAULT_EVENT_WAIT_SECONDS = 30
DEFAULT_WATCH_WAIT_SECONDS = 300
MAX_EVENT_WAIT_SECONDS = 600

# Diagnostic-only health inspection interval (read-only; never terminates).
HEALTH_INSPECT_INTERVAL_SECONDS = 300

# Structured concurrency refusal code (never preempt another worker).
DISPATCHER_CONCURRENCY_BUSY = "DISPATCHER_CONCURRENCY_BUSY"

# Honest source identity for prompt-only research workspaces.
PROMPT_ONLY_SOURCE = "prompt-only"

META_DIR_NAME = ".grok-worker"
META_FILE_NAME = "lifecycle.json"
WORKER_LOCK_NAME = "worker.lock"
LEASE_FILE_NAME = "lease.json"
LEASE_LOCK_NAME = "lease.lock"
ROOT_LOCK_NAME = ".lifecycle.lock"
DISCLOSURE_FILE_NAME = "disclosure.json"

OUTPUT_DIR_NAME = ".grok-output"
RESULT_FILE_NAME = "result.json"
VERIFICATION_DIR_NAME = "verification"

CLONE_PREFIX = "grok-worker-"
TMP_GROK_PREFIX = "grok-"
STAGING_PREFIX = ".artifact-staging-"
DISPATCHER_REGISTRY_DIR = "dispatchers"

EXCLUDE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".grok-output",
        ".grok-worker",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        ".git",
        "node_modules",
        ".tox",
        ".nox",
        ".grok-disposable",
        ".grok-artifacts",
    }
)

EXCLUDE_FILE_PREFIXES: tuple[str, ...] = ("prompt-",)

# Local Python env directory name prefixes that fail a success finalize.
LOCAL_ENV_DIR_PREFIXES: tuple[str, ...] = (".venv", "venv")

PINNED_TYPER = "typer==0.15.2"
PINNED_CLICK = "click==8.1.8"
