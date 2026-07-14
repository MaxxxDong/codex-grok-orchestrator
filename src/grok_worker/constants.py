"""Fixed defaults and path names for the lifecycle runner."""

from __future__ import annotations

SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
MANAGED_BY = "grok-worker-lifecycle"

DEFAULT_CAP_BYTES = 6 * 1024**3  # exactly 6 GiB
DEFAULT_FAILURE_RETAIN_HOURS = 24
DEFAULT_TMP_AGE_HOURS = 24
DEFAULT_ACPX_TIMEOUT = 1800
MAX_CONCURRENT_WORKERS = 10
MAX_TASK_ID_LEN = 64

META_DIR_NAME = ".grok-worker"
META_FILE_NAME = "lifecycle.json"
WORKER_LOCK_NAME = "worker.lock"
ROOT_LOCK_NAME = ".lifecycle.lock"

OUTPUT_DIR_NAME = ".grok-output"
RESULT_FILE_NAME = "result.json"
VERIFICATION_DIR_NAME = "verification"

CLONE_PREFIX = "grok-worker-"
TMP_GROK_PREFIX = "grok-"
STAGING_PREFIX = ".artifact-staging-"

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
