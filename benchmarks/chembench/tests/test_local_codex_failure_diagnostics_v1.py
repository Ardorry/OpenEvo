from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import openevo_chembench.local_codex_executor as executor_module
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
    LocalCommandResult,
    _find_security_tool_use,
    _parse_jsonl_transcript,
    _redact_diagnostic_text,
)
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1
from openevo_chembench.taskwise_online_runner_v1 import TaskwiseAgentRequestV1


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTROL_CONFIG = PACKAGE_ROOT / "configs" / "control_canary9_taskwise_online_v1.yaml"
TASK_UID = "a" * 64


def _transcript(*, response: str = "A") -> str:
    events = (
        {"type": "thread.started", "thread_id": "thread-diagnostic"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": response},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        },
    )
    return "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n"


def _recovered_transport_transcript(*, response: str = "A") -> str:
    events = [json.loads(line) for line in _transcript(response=response).splitlines()]
    events.insert(
        2,
        {
            "type": "error",
            "message": "stream disconnected; reconnecting (attempt 1/5)",
        },
    )
    return "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n"


def _item_transcript(item_type: str) -> str:
    events = (
        {"type": "thread.started", "thread_id": "thread-diagnostic"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": item_type, "text": "private-event-payload"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            },
        },
    )
    return "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n"


def _request(
    *,
    session_id: str = "diagnostic_session_0001",
) -> TaskwiseAgentRequestV1:
    return TaskwiseAgentRequestV1(
        rendered_public_prompt=(
            "There is a single choice question about chemistry. "
            "Answer the question by replying A, B, C or D.\n"
            "Question: public test?\nA. one\nB. two\nC. three\nD. four\nAnswer:"
        ),
        resolved_text_memory=None,
        session_id=session_id,
        arm="control",
        task_ordinal=0,
        round_index=0,
        run_id="diagnostic_run_0001",
        task_uid=TASK_UID,
    )


def _executor(tmp_path: Path, runner) -> tuple[LocalCodexCLIExecutor, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    isolation = tmp_path / "isolation"
    isolation.mkdir(mode=0o700)
    diagnostics = tmp_path / "diagnostics"
    executor = LocalCodexCLIExecutor(
        config=load_taskwise_config_v1(CONTROL_CONFIG),
        codex_executable=Path("/bin/true"),
        auth_file=auth,
        command_runner=runner,
        verified_codex_version="0.144.6",
        isolation_parent=isolation,
        diagnostic_root=diagnostics,
    )
    return executor, diagnostics


def _only_receipt(diagnostics: Path) -> tuple[Path, dict[str, object]]:
    receipts = tuple(diagnostics.glob("receipt_*.json"))
    assert len(receipts) == 1
    path = receipts[0]
    assert path.stat().st_mode & 0o777 == 0o600
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_real_command_policy_has_output_capture_and_no_invalid_agents_override(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command, _prompt, _cwd, _environment, _timeout):
        calls.append(tuple(command))
        return LocalCommandResult(
            returncode=1,
            stdout="",
            stderr="failed to load Codex config: invalid type",
        )

    executor, _diagnostics = _executor(tmp_path, runner)
    with pytest.raises(LocalCodexExecutionError) as raised:
        executor.execute_taskwise(_request())
    executor.close()

    command = calls[0]
    assert "agents.enabled=false" not in command
    assert "--output-last-message" in command
    assert raised.value.taskwise_failure_code == "EXECUTOR_CODEX_STARTUP_FAILED"
    assert raised.value.executor_stage == "CODEX_STARTUP"
    assert raised.value.completion_observed is False


@pytest.mark.parametrize(
    ("stderr", "expected_code", "expected_stage"),
    (
        (
            "authentication required; not logged in",
            "EXECUTOR_AUTH_MATERIALIZATION_FAILED",
            "CODEX_STARTUP",
        ),
        (
            "HTTP 429 rate limit from model provider",
            "EXECUTOR_MODEL_TRANSPORT_FAILED",
            "MODEL_TRANSPORT",
        ),
        (
            "TLS certificate connection failure",
            "EXECUTOR_MODEL_TRANSPORT_FAILED",
            "MODEL_TRANSPORT",
        ),
        ("unclassified exit", "EXECUTOR_NONZERO_EXIT", "CODEX_STARTUP"),
    ),
)
def test_nonzero_failures_use_closed_classification(
    tmp_path: Path,
    stderr: str,
    expected_code: str,
    expected_stage: str,
) -> None:
    executor, _diagnostics = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=1, stdout="", stderr=stderr),
    )
    with pytest.raises(LocalCodexExecutionError) as raised:
        executor.execute_taskwise(_request())
    executor.close()

    assert raised.value.taskwise_failure_code == expected_code
    assert raised.value.executor_stage == expected_stage
    assert raised.value.completion_observed is False
    # Frozen v2 retains its pre-existing infrastructure retry contract. The
    # taskwise runner converts all formal failures to non-resumable state.
    assert raised.value.replacement_completion_allowed is True


def test_private_diagnostic_is_complete_mode_600_and_redacted(tmp_path: Path) -> None:
    sensitive = (
        "Authorization: Bearer secret-value "
        '{"access_token":"json-secret","refresh_token":"json-refresh"} '
        '{"Authorization":"Bearer json-bearer"} '
        "target_scores private-uuid https://private.invalid/path"
    )
    executor, diagnostics = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=1, stdout="", stderr=sensitive),
    )
    with pytest.raises(LocalCodexExecutionError) as raised:
        executor.execute_taskwise(_request())
    executor.close()

    path, receipt = _only_receipt(diagnostics)
    required = {
        "run_id",
        "task_uid",
        "task_index",
        "round_index",
        "session_id",
        "executor_stage",
        "exception_class",
        "redacted_exception_message",
        "process_return_code",
        "process_signal",
        "timed_out",
        "redacted_argv",
        "codex_version",
        "real_codex_binary",
        "invocation_root_id",
        "auth_materialization_status",
        "stdout_sha256",
        "stderr_sha256",
        "redacted_stdout_tail",
        "redacted_stderr_tail",
        "last_event_type",
        "event_count",
        "tool_event_count",
        "output_last_message_exists",
        "output_last_message_sha256",
        "cleanup_status",
        "residual_root_count",
        "created_at_utc",
    }
    assert required <= set(receipt)
    assert receipt["run_id"] == "diagnostic_run_0001"
    assert receipt["task_uid"] == TASK_UID
    assert receipt["task_index"] == receipt["round_index"] == 0
    assert receipt["session_id"] == "diagnostic_session_0001"
    assert receipt["cleanup_status"] == "COMPLETE"
    assert receipt["residual_root_count"] == 0
    serialized = path.read_text(encoding="utf-8")
    assert sensitive not in serialized
    assert "secret-value" not in serialized
    assert "json-secret" not in serialized
    assert "json-refresh" not in serialized
    assert "json-bearer" not in serialized
    assert "target_scores" not in serialized
    assert "private-uuid" not in serialized
    assert "private.invalid" not in serialized
    assert raised.value.diagnostic_receipt == path.name
    assert receipt["exception_class"] == "LocalCodexExecutionError"
    assert receipt["redacted_exception_message"] == "closed executor failure"


def test_malformed_event_stream_and_missing_output_are_distinct(
    tmp_path: Path,
) -> None:
    malformed_executor, _ = _executor(
        tmp_path / "malformed",
        lambda *_args: LocalCommandResult(returncode=0, stdout="{broken\n", stderr=""),
    )
    with pytest.raises(LocalCodexExecutionError) as malformed:
        malformed_executor.execute_taskwise(_request())
    malformed_executor.close()
    assert malformed.value.taskwise_failure_code == "EXECUTOR_EVENT_STREAM_INVALID"
    assert malformed.value.completion_observed is False

    missing_executor, _ = _executor(
        tmp_path / "missing",
        lambda *_args: LocalCommandResult(
            returncode=0,
            stdout=_transcript(),
            stderr="",
            output_last_message_exists=False,
            output_last_message=None,
        ),
    )
    with pytest.raises(LocalCodexExecutionError) as missing:
        missing_executor.execute_taskwise(_request())
    missing_executor.close()
    assert missing.value.taskwise_failure_code == "EXECUTOR_OUTPUT_MISSING"
    assert missing.value.completion_observed is True
    assert missing.value.retry_allowed is False


def test_zero_exit_recovered_transport_event_is_not_nonzero_failure(
    tmp_path: Path,
) -> None:
    executor, diagnostics = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(
            returncode=0,
            stdout=_recovered_transport_transcript(response="A"),
            stderr="transient transport reconnect diagnostic",
            output_last_message_exists=True,
            output_last_message="A\n",
        ),
    )

    attempt = executor.execute_taskwise(_request())
    executor.close()

    assert attempt.response == "A"
    assert not tuple(diagnostics.glob("receipt_*.json"))
    success_receipts = tuple(diagnostics.glob("success_*.json"))
    assert len(success_receipts) == 1
    success = json.loads(success_receipts[0].read_text(encoding="utf-8"))
    assert success["process_return_code"] == 0
    assert success["event_count"] == 5
    assert success["tool_event_count"] == 0


def test_output_last_message_mismatch_fails_after_completion(tmp_path: Path) -> None:
    executor, _diagnostics = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(
            returncode=0,
            stdout=_transcript(response="A"),
            stderr="",
            output_last_message_exists=True,
            output_last_message="B\n",
        ),
    )
    with pytest.raises(LocalCodexExecutionError) as raised:
        executor.execute_taskwise(_request())
    executor.close()

    assert raised.value.taskwise_failure_code == "EXECUTOR_OUTPUT_INVALID"
    assert raised.value.executor_stage == "COMPLETION_VALIDATION"
    assert raised.value.completion_observed is True
    assert raised.value.retry_allowed is False


def test_unreadable_output_last_message_is_invalid_not_missing(tmp_path: Path) -> None:
    executor, _diagnostics = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(
            returncode=0,
            stdout=_transcript(response="A"),
            stderr="",
            output_last_message_exists=True,
            output_last_message=None,
            output_last_message_invalid=True,
        ),
    )
    with pytest.raises(LocalCodexExecutionError) as raised:
        executor.execute_taskwise(_request())
    executor.close()

    assert raised.value.taskwise_failure_code == "EXECUTOR_OUTPUT_INVALID"
    assert raised.value.executor_stage == "COMPLETION_VALIDATION"
    assert raised.value.completion_observed is True


def test_ordinary_lifecycle_event_is_not_a_tool_violation() -> None:
    transcript = "\n".join(
        (
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.status.changed", "status": "in_progress"}),
        )
    )

    assert _find_security_tool_use(transcript) is None


def test_unknown_non_tool_item_is_event_invalid_not_security_violation() -> None:
    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(_item_transcript("plan"))

    assert raised.value.taskwise_failure_code == "EXECUTOR_EVENT_STREAM_INVALID"
    assert raised.value.run_status is None
    assert raised.value.event_counts == {}


def test_security_violation_survives_private_diagnostic_receipt_failure(
    tmp_path: Path,
) -> None:
    call_count = 0

    def runner(*_args):
        nonlocal call_count
        call_count += 1
        return LocalCommandResult(
            returncode=0,
            stdout=_item_transcript("file_read"),
            stderr="",
        )

    executor, _diagnostics = _executor(tmp_path, runner)
    with patch.object(executor_module, "_persist_diagnostic_receipt", return_value=None):
        with pytest.raises(LocalCodexExecutionError) as first:
            executor.execute_taskwise(_request())
        with pytest.raises(LocalCodexExecutionError) as second:
            executor.execute_taskwise(_request(session_id="diagnostic_session_0002"))
    executor.close()

    for raised in (first, second):
        assert raised.value.taskwise_failure_code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        assert raised.value.run_status == "SECURITY_TOOL_USE_VIOLATION"
        assert raised.value.event_counts == {"file_read": 1}
        assert raised.value.retry_allowed is False
        assert raised.value.resume_allowed is False
    assert first.value.private_event_reference is not None
    assert call_count == 1


def test_plugin_activity_does_not_overwrite_tool_event_evidence(tmp_path: Path) -> None:
    def runner(_command, _prompt, cwd, _environment, _timeout):
        plugin_cache = cwd.parent / "codex_home" / "plugins" / "cache" / "openai-curated-remote"
        plugin_cache.mkdir(parents=True)
        return LocalCommandResult(
            returncode=0,
            stdout=_item_transcript("file_read"),
            stderr="",
        )

    executor, diagnostics = _executor(tmp_path, runner)
    with pytest.raises(LocalCodexExecutionError) as raised:
        executor.execute_taskwise(_request())
    executor.close()

    error = raised.value
    assert error.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
    assert error.event_counts == {"file_read": 1}
    assert error.private_event_reference is not None
    _path, receipt = _only_receipt(diagnostics)
    assert receipt["event_counts"] == {"file_read": 1}
    assert "STARTUP_PLUGIN_ACTIVITY" in receipt["finding_codes"]
    assert "PRIVATE_EVENT_RETAINED" in receipt["finding_codes"]


def test_quoted_json_and_bearer_credentials_are_redacted() -> None:
    original = (
        '{"access_token":"SECRET-ACCESS","refresh_token":"SECRET-REFRESH",'
        '"Authorization":"Bearer SECRET-AUTH"} '
        "Bearer SECRET-STANDALONE"
    )
    redacted = _redact_diagnostic_text(original, isolation_root_id=None)

    assert "SECRET-" not in redacted
    assert redacted.count("<redacted-secret>") == 4


def test_legacy_frozen_infrastructure_retry_contract_is_preserved() -> None:
    infrastructure = LocalCodexExecutionError(LocalCodexExecutionErrorCode.CLI_FAILED)
    security = LocalCodexExecutionError(
        LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION,
        event_counts={"file_read": 1},
        event_digest="a" * 64,
    )

    assert infrastructure.retry_allowed is True
    assert infrastructure.resume_allowed is True
    assert infrastructure.replacement_completion_allowed is True
    assert security.retry_allowed is False
    assert security.resume_allowed is False
