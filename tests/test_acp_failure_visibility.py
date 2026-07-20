"""ACP runtime/transport failures remain visible alongside missing result.json."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from grok_worker.finalize import (
    classify_backend_failure,
    classify_live_backend_attention,
    summarize_acp_failure,
    summarize_backend_failure,
)
from grok_worker.models import WorkerMeta
from grok_worker.paths import meta_path
from grok_worker.runner import RunConfig, run_worker
from tests.fake_acpx import write_fake_acpx


def test_summarize_acp_failure_classifies_runtime_internal_error() -> None:
    summary = summarize_acp_failure("[acpx] error: RUNTIME Internal error\n")
    assert summary is not None
    assert "RUNTIME Internal error" in summary
    assert summary.startswith("upstream ACP failure:")


def test_summarize_acp_failure_ignores_arbitrary_model_output() -> None:
    long_model = "I implemented a feature:\n" + ("x" * 5000) + "\nresult.json missing on purpose\n"
    assert summarize_acp_failure(long_model) is None
    assert summarize_acp_failure("") is None


def test_provider_500_is_classified_from_late_bounded_output() -> None:
    output = ("harmless startup output\n" * 300) + (
        "responses API error status=500 Internal Server Error model_id=grok-test\n"
    )

    assert classify_live_backend_attention(output) == "provider_http_5xx"
    assert summarize_backend_failure(output) == "upstream provider failure: HTTP 500"


def test_model_text_mentioning_api_error_does_not_emit_live_attention() -> None:
    model_text = "The documentation gives API error status=500 as an example."

    assert classify_live_backend_attention(model_text) is None


def test_runtime_internal_error_visible_with_missing_structured_result(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = write_fake_acpx(tmp_roots["root"] / "bin", "acp_runtime_internal_error")
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_ACPX_BEHAVIOR", "acp_runtime_internal_error")
    outcome = run_worker(
        RunConfig(
            source=git_source,
            prompt="x",
            backend="acp",
            disposable_root=tmp_roots["disposable"],
            artifact_root=tmp_roots["artifacts"],
            shared_cache_root=tmp_roots["shared"],
            acpx_bin=str(fake),
            prepare_deps=False,
            task_id="acp-rt-err",
            failure_retain_hours=24,
            skip_post_gc=True,
        )
    )
    assert outcome.exit_code != 0  # type: ignore[attr-defined]
    assert outcome.state == "failed"  # type: ignore[attr-defined]
    message = outcome.message or ""  # type: ignore[attr-defined]
    # Preserve structured-result failure signal.
    assert "missing structured result" in message
    # Surface the recognizable upstream ACP runtime failure.
    assert "RUNTIME Internal error" in message
    assert "upstream ACP failure" in message

    clone = Path(outcome.clone_path or "")  # type: ignore[attr-defined]
    assert clone.is_dir()
    meta = WorkerMeta.read(meta_path(clone))
    assert meta.error_message is not None
    assert "missing structured result" in meta.error_message
    assert "RUNTIME Internal error" in meta.error_message
    assert meta.error_message.index("upstream ACP failure") < meta.error_message.index(
        "secondary result contract failure"
    )

    art = Path(outcome.artifact_path or "")  # type: ignore[attr-defined]
    worker = json.loads((art / "worker.log").read_text(encoding="utf-8"))
    assert worker["lifecycle"]["error_message"]
    assert "missing structured result" in worker["lifecycle"]["error_message"]
    assert "RUNTIME Internal error" in worker["lifecycle"]["error_message"]
    assert "[acpx] error: RUNTIME Internal error" in worker["agent_output"]


def test_native_budget_failures_are_primary_and_continuation_safe() -> None:
    truncated = classify_backend_failure(
        '{"type":"error","message":"Internal error: response truncated by max_tokens",'
        '"error_kind":"max_tokens_truncation"}\n'
    )
    turns = classify_backend_failure(
        '{"type":"max_turns_reached"}\nError: max turns reached\n'
    )

    assert truncated is not None
    assert truncated.kind == "max_tokens_truncation"
    assert truncated.continuation_safe is True
    assert turns is not None
    assert turns.kind == "max_turns_reached"
    assert turns.continuation_safe is True
