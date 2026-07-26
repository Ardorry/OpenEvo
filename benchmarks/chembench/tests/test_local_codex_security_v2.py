from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

import pytest

import openevo_chembench.frozen_runtime_v2 as frozen_runtime_module
from openevo_chembench.artifacts import ArtifactKind
from openevo_chembench.feedback import SafeEvolutionSignal, SignalOutcome
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    FrozenAgentRequestV2,
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
    LocalCommandResult,
    _attest_invocation,
    _create_invocation_layout,
    _event_stream_summary,
    _parse_jsonl_transcript,
)
from openevo_chembench.reflector import SafeEvolutionReflector
from openevo_chembench.v2_config import (
    ExecutorPolicyV2,
    FrozenArtifactBinding,
    FrozenExperimentConfigV2,
)


_OFFICIAL_PROMPT = """There is a single choice question about chemistry. Answer the question by replying A, B, C or D.
Question: Public chemistry question?
A. Alpha
B. Beta
C. Gamma
D. Delta
Answer:"""
_SENSITIVE_EVENT_PAYLOAD = "private-event-payload target_scores uuid answer must-not-be-public"
_MEMORY_MARKDOWN = "Check chemical constraints, then output one letter."
_MEMORY_SHA256 = hashlib.sha256(_MEMORY_MARKDOWN.encode()).hexdigest()


def _safe_transcript() -> str:
    return (
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {"type": "thread.started", "thread_id": "thread-safe"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "message-safe",
                        "type": "agent_message",
                        "text": "A",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 0,
                    },
                },
            )
        )
        + "\n"
    )


def _recoverable_transport_transcript() -> str:
    events = [json.loads(line) for line in _safe_transcript().splitlines()]
    events.insert(
        2,
        {
            "type": "error",
            "message": "stream disconnected; reconnecting (attempt 1/5)",
        },
    )
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


def _tool_transcript(item_type: str) -> str:
    return (
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {"type": "thread.started", "thread_id": "thread-safe"},
                {"type": "turn.started"},
                {
                    "type": "item.started",
                    "item": {
                        "id": "tool-private",
                        "type": item_type,
                        "payload": _SENSITIVE_EVENT_PAYLOAD,
                    },
                },
            )
        )
        + "\n"
    )


def _todo_list_transcript(
    *,
    started_item: dict[str, object] | None = None,
    completed_item: dict[str, object] | None = None,
) -> str:
    todo = {
        "id": "todo-safe",
        "type": "todo_list",
        "items": [{"text": "Check the public task.", "completed": False}],
    }
    completed = {
        "id": "todo-safe",
        "type": "todo_list",
        "items": [{"text": "Check the public task.", "completed": True}],
    }
    return (
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {"type": "thread.started", "thread_id": "thread-safe"},
                {"type": "turn.started"},
                {"type": "item.started", "item": started_item or todo},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "message-safe",
                        "type": "agent_message",
                        "text": "A",
                    },
                },
                {"type": "item.completed", "item": completed_item or completed},
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 0,
                    },
                },
            )
        )
        + "\n"
    )


class _RecordingRunner:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.calls: list[tuple[tuple[str, ...], str | None, Path, dict[str, str], float]] = []

    def __call__(
        self,
        command,
        input_text,
        cwd,
        environment,
        timeout_seconds,
    ) -> LocalCommandResult:
        assert list(cwd.iterdir()) == []
        root = cwd.parent
        assert cwd == root / "work"
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for name in (
            "home",
            "codex_home",
            "xdg_config",
            "xdg_cache",
            "xdg_state",
            "tmp",
            "work",
            "private_events",
        ):
            child = root / name
            assert child.is_dir()
            assert not child.is_symlink()
            assert stat.S_IMODE(child.stat().st_mode) == 0o700
        self.calls.append(
            (
                tuple(command),
                input_text,
                cwd,
                dict(environment),
                timeout_seconds,
            )
        )
        return LocalCommandResult(returncode=0, stdout=self.transcript, stderr="")


def _v2_config(*, arm: str) -> FrozenExperimentConfigV2:
    enabled = arm == "evolved"
    digest = "a" * 64
    return FrozenExperimentConfigV2(
        arm=arm,
        scope="canary18",
        run_name=f"{arm}_canary18",
        output_directory=f"results/v2/{arm}",
        dataset_root="data/chembench4k",
        dataset_manifest="data/chembench4k/manifest.json",
        task_manifest="manifests/canary18_public.jsonl",
        private_task_manifest="manifests/canary18_private.jsonl",
        receipt_path="receipts/benchmark_receipt.json",
        pilot_protocol_hash=digest,
        codex_cli_version="0.144.6",
        prompt_renderer_id="chembench4k_official_five_shot_v2",
        parser_id="official_first_capital_parser_v2",
        evaluator_id="chembench4k_accuracy_v2",
        artifact=FrozenArtifactBinding(
            enabled=enabled,
            frozen_artifact_id="artifact-text-memory-v2" if enabled else None,
            frozen_artifact_sha256=digest if enabled else None,
            context_resolution_digest="b" * 64 if enabled else None,
            resolved_memory_sha256=_MEMORY_SHA256 if enabled else None,
        ),
        executor=ExecutorPolicyV2(
            backend="local_codex_cli",
            harness="codex_cli",
            timeout_seconds=600,
            concurrency=1,
            infrastructure_retries_before_completion=0,
            tools_enabled=False,
            mcp_enabled=False,
            web_enabled=False,
            network_enabled=False,
            subagents_enabled=False,
        ),
    )


def _memory() -> CoreResolvedTextMemoryV2:
    return _issue_core_resolved_text_memory_v2(
        core_artifact_id="artifact-text-memory-v2",
        artifact_payload_sha256="a" * 64,
        context_resolution_digest="b" * 64,
        resolved_memory_sha256=_MEMORY_SHA256,
        markdown=_MEMORY_MARKDOWN,
    )


def test_core_resolved_memory_has_no_public_raw_issuer() -> None:
    assert not hasattr(frozen_runtime_module, "_CORE_MEMORY_ISSUER")
    assert not hasattr(frozen_runtime_module, "issue_core_resolved_text_memory_v2")
    with pytest.raises(TypeError):
        CoreResolvedTextMemoryV2(
            core_artifact_id="artifact-v2",
            artifact_payload_sha256="a" * 64,
            context_resolution_digest="b" * 64,
            resolved_memory_sha256=_MEMORY_SHA256,
            markdown=_MEMORY_MARKDOWN,
        )


def _executor(
    outer: Path,
    *,
    arm: str,
    runner: _RecordingRunner,
) -> LocalCodexCLIExecutor:
    outer.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth = outer / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    isolation = outer / "isolation"
    isolation.mkdir(mode=0o700)
    return LocalCodexCLIExecutor(
        config=_v2_config(arm=arm),
        codex_executable=Path("/usr/bin/codex"),
        auth_file=auth,
        command_runner=runner,
        verified_codex_version="0.144.6",
        isolation_parent=isolation,
        diagnostic_root=outer / "private_audit",
    )


@pytest.mark.parametrize(
    ("item_type", "category"),
    (
        ("shell_call", "shell"),
        ("command_execution", "command_execution"),
        ("file_read", "file_read"),
        ("file_write", "file_write"),
        ("file_change", "file_write"),
        ("apply_patch", "file_write"),
        ("unified_exec", "file_write"),
        ("mcp_tool_call", "mcp"),
        ("network_call", "network"),
        ("web_search", "web"),
        ("browser_action", "browser"),
        ("plugin_call", "plugin"),
        ("app_call", "app"),
        ("subagent_call", "subagent"),
        ("collab_tool_call", "subagent"),
        ("external_tool_call", "external_tool"),
        ("function_call", "unknown_tool"),
        ("image_generation", "unknown_tool"),
        ("dynamic_tool_call", "unknown_tool"),
        ("future_tool_kind", "unknown_tool"),
    ),
)
def test_zero_tool_event_matrix(item_type: str, category: str) -> None:
    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(_tool_transcript(item_type))

    error = raised.value
    assert error.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
    assert error.run_status == "SECURITY_TOOL_USE_VIOLATION"
    assert error.event_counts == {category: 1}
    assert error.retry_allowed is False
    assert error.resume_allowed is False
    assert error.replacement_completion_allowed is False


def test_reasoning_and_message_events_do_not_false_positive() -> None:
    response, usage, digest = _parse_jsonl_transcript(_safe_transcript())
    assert response == "A"
    assert usage["output_tokens"] == 1
    assert len(digest) == 64


def test_current_codex_cache_write_usage_field_is_strictly_accepted() -> None:
    events = [json.loads(line) for line in _safe_transcript().splitlines()]
    events[-1]["usage"]["cache_write_input_tokens"] = 0
    response, usage, digest = _parse_jsonl_transcript(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
    )
    assert response == "A"
    assert usage["cache_write_input_tokens"] == 0
    assert len(digest) == 64


def test_unknown_future_usage_field_remains_fail_closed() -> None:
    events = [json.loads(line) for line in _safe_transcript().splitlines()]
    events[-1]["usage"]["future_tokens"] = 0
    with pytest.raises(LocalCodexExecutionError, match="transcript_invalid"):
        _parse_jsonl_transcript(
            "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
        )


def test_nested_tool_schema_inside_agent_message_is_security_violation() -> None:
    events = [json.loads(line) for line in _safe_transcript().splitlines()]
    message = events[2]["item"]
    assert isinstance(message, dict)
    message["tool_calls"] = [{"type": "file_read"}]

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(
            "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
        )

    assert raised.value.taskwise_failure_code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
    assert raised.value.event_counts == {"file_read": 1, "unknown_tool": 1}


def test_innocuous_unknown_message_schema_is_invalid_not_tool_use() -> None:
    events = [json.loads(line) for line in _safe_transcript().splitlines()]
    message = events[2]["item"]
    assert isinstance(message, dict)
    message["future_metadata"] = "opaque"

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(
            "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
        )

    assert raised.value.taskwise_failure_code == "EXECUTOR_EVENT_STREAM_INVALID"
    assert raised.value.run_status is None


def test_recoverable_transport_error_followed_by_completion_is_accepted() -> None:
    transcript = _recoverable_transport_transcript()
    response, usage, digest = _parse_jsonl_transcript(transcript)

    assert response == "A"
    assert usage["output_tokens"] == 1
    assert len(digest) == 64
    assert _event_stream_summary(transcript) == {
        "event_count": 5,
        "tool_event_count": 0,
        "last_event_type": "turn.completed",
    }


@pytest.mark.parametrize(
    "error_event",
    (
        {"type": "error", "message": "fatal model failure"},
        {"type": "error", "message": 1},
        {"type": "error", "message": "stream disconnected; reconnecting", "extra": True},
        {
            "type": "error",
            "message": ("stream disconnected; reconnecting " + ("x" * 1024)),
        },
    ),
)
def test_unrecognized_or_malformed_error_event_remains_fail_closed(
    error_event: dict[str, object],
) -> None:
    events = [json.loads(line) for line in _safe_transcript().splitlines()]
    events.insert(2, error_event)

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(
            "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
        )

    assert raised.value.code is LocalCodexExecutionErrorCode.CLI_FAILED
    assert raised.value.taskwise_failure_code == "EXECUTOR_MODEL_TRANSPORT_FAILED"
    assert raised.value.executor_stage == "MODEL_TRANSPORT"


def test_recoverable_transport_error_without_completion_fails_closed() -> None:
    events = [json.loads(line) for line in _recoverable_transport_transcript().splitlines()]
    transcript = "\n".join(
        json.dumps(event, sort_keys=True)
        for event in events
        if event["type"] not in {"item.completed", "turn.completed"}
    )

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(transcript + "\n")

    assert raised.value.code is LocalCodexExecutionErrorCode.CLI_FAILED
    assert raised.value.taskwise_failure_code == "EXECUTOR_MODEL_TRANSPORT_FAILED"
    assert raised.value.executor_stage == "MODEL_TRANSPORT"


def test_turn_failed_remains_terminal_after_reconnect_notice() -> None:
    events = [json.loads(line) for line in _recoverable_transport_transcript().splitlines()]
    events[-1] = {"type": "turn.failed"}

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(
            "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
        )

    assert raised.value.code is LocalCodexExecutionErrorCode.CLI_FAILED
    assert raised.value.taskwise_failure_code == "EXECUTOR_MODEL_TRANSPORT_FAILED"
    assert raised.value.executor_stage == "MODEL_TRANSPORT"


def test_strict_codex_todo_list_lifecycle_is_not_a_tool_or_parse_failure() -> None:
    transcript = _todo_list_transcript()
    response, usage, digest = _parse_jsonl_transcript(transcript)
    summary = _event_stream_summary(transcript)

    assert response == "A"
    assert usage["output_tokens"] == 1
    assert len(digest) == 64
    assert summary == {
        "event_count": 6,
        "tool_event_count": 0,
        "last_event_type": "turn.completed",
    }


@pytest.mark.parametrize(
    "malformed_item",
    (
        {
            "id": "todo-safe",
            "type": "todo_list",
            "items": [{"text": "Check.", "completed": False, "command": "forbidden"}],
        },
        {
            "id": "todo-safe",
            "type": "todo_list",
            "items": [{"text": "Check.", "completed": 0}],
        },
        {
            "id": "todo-safe",
            "type": "todo_list",
            "items": "not-a-list",
        },
        {
            "id": "todo-safe",
            "type": "todo_list",
            "items": [],
            "payload": "unexpected",
        },
    ),
)
def test_malformed_todo_list_lifecycle_remains_fail_closed(
    malformed_item: dict[str, object],
) -> None:
    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(_todo_list_transcript(started_item=malformed_item))

    entries = malformed_item.get("items")
    has_command_schema = (
        isinstance(entries, list)
        and bool(entries)
        and isinstance(entries[0], dict)
        and "command" in entries[0]
    )
    if has_command_schema:
        assert raised.value.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
        assert raised.value.event_counts == {"command_execution": 1}
    else:
        assert raised.value.code is LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID
        assert raised.value.taskwise_failure_code == "EXECUTOR_EVENT_STREAM_INVALID"
        assert raised.value.event_counts == {}


def test_unobserved_todo_list_update_event_remains_fail_closed() -> None:
    transcript = _todo_list_transcript().replace(
        '"type": "item.started"',
        '"type": "item.updated"',
        1,
    )

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(transcript)

    assert raised.value.code is LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID


def test_todo_list_does_not_mask_a_real_tool_event() -> None:
    lines = _todo_list_transcript().splitlines()
    lines.insert(
        3,
        json.dumps(
            {
                "type": "item.started",
                "item": {"id": "tool-private", "type": "file_read"},
            },
            sort_keys=True,
        ),
    )

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript("\n".join(lines) + "\n")

    assert raised.value.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
    assert raised.value.event_counts == {"file_read": 1}


def test_executor_accepts_strict_todo_lifecycle_and_cleans_isolation() -> None:
    runner = _RecordingRunner(_todo_list_transcript())
    with tempfile.TemporaryDirectory() as temporary:
        outer = Path(temporary)
        executor = _executor(outer, arm="baseline", runner=runner)
        attempt = executor.execute_frozen(
            FrozenAgentRequestV2(rendered_public_prompt=_OFFICIAL_PROMPT)
        )
        executor.close()

        assert attempt.response == "A"
        assert list((outer / "isolation").iterdir()) == []


def test_violation_is_terminal_retained_privately_and_public_safe() -> None:
    runner = _RecordingRunner(_tool_transcript("file_read"))
    with tempfile.TemporaryDirectory() as temporary:
        outer = Path(temporary)
        executor = _executor(outer, arm="baseline", runner=runner)
        request = FrozenAgentRequestV2(rendered_public_prompt=_OFFICIAL_PROMPT)

        with pytest.raises(LocalCodexExecutionError) as first:
            executor.execute_frozen(request)
        with pytest.raises(LocalCodexExecutionError) as second:
            executor.execute_frozen(request)

        assert len(runner.calls) == 1
        assert executor.run_status == "SECURITY_TOOL_USE_VIOLATION"
        assert first.value.private_event_reference is not None
        event_path = outer / "private_audit" / first.value.private_event_reference
        assert event_path.is_file()
        assert stat.S_IMODE(event_path.stat().st_mode) == 0o600
        assert _SENSITIVE_EVENT_PAYLOAD in event_path.read_text(encoding="utf-8")
        public = first.value.to_log_fields()
        assert public["event_counts"] == {"file_read": 1}
        assert public["event_types"] == ["file_read"]
        assert _SENSITIVE_EVENT_PAYLOAD not in json.dumps(public, sort_keys=True)
        assert "private_event_reference" not in public
        assert second.value.retry_allowed is False
        assert list((outer / "isolation").iterdir()) == []
        executor.close()


def test_invocation_environment_and_empty_work_are_attested() -> None:
    runner = _RecordingRunner(_safe_transcript())
    with tempfile.TemporaryDirectory() as temporary:
        outer = Path(temporary)
        executor = _executor(outer, arm="baseline", runner=runner)
        attempt = executor.execute_frozen(
            FrozenAgentRequestV2(rendered_public_prompt=_OFFICIAL_PROMPT)
        )
        executor.close()

    assert attempt.response == "A"
    command, prompt, cwd, environment, _timeout = runner.calls[0]
    root = cwd.parent
    assert prompt is not None and prompt.endswith(_OFFICIAL_PROMPT)
    assert Path(environment["HOME"]) == root / "home"
    assert Path(environment["CODEX_HOME"]) == root / "codex_home"
    assert Path(environment["XDG_CONFIG_HOME"]) == root / "xdg_config"
    assert Path(environment["XDG_CACHE_HOME"]) == root / "xdg_cache"
    assert Path(environment["XDG_STATE_HOME"]) == root / "xdg_state"
    assert Path(environment["TMPDIR"]) == root / "tmp"
    assert Path(environment["CODEX_SQLITE_HOME"]) == root / "xdg_state" / "sqlite"
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert 'model_reasoning_effort="medium"' in command
    assert not root.exists()


def test_attestation_rejects_symlinked_or_nonempty_workdir() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        parent = Path(temporary)
        layout = _create_invocation_layout(parent)
        environment = {
            "PATH": os.defpath,
            "HOME": str(layout.home),
        }
        auth = layout.codex_home / "auth.json"
        auth.write_text("{}\n", encoding="utf-8")
        auth.chmod(0o600)
        (layout.work / "unexpected").write_text("not empty", encoding="utf-8")
        with pytest.raises(LocalCodexExecutionError) as nonempty:
            _attest_invocation(layout, environment=environment)
        assert nonempty.value.code is LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED

        (layout.work / "unexpected").unlink()
        layout.work.rmdir()
        layout.work.symlink_to(layout.tmp, target_is_directory=True)
        with pytest.raises(LocalCodexExecutionError) as symlink:
            _attest_invocation(layout, environment=environment)
        assert symlink.value.code is LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED


def test_frozen_request_preserves_official_prompt_and_treatment_only() -> None:
    baseline_runner = _RecordingRunner(_safe_transcript())
    evolved_runner = _RecordingRunner(_safe_transcript())
    with tempfile.TemporaryDirectory() as temporary:
        outer = Path(temporary)
        baseline = _executor(
            outer / "baseline",
            arm="baseline",
            runner=baseline_runner,
        )
        evolved = _executor(
            outer / "evolved",
            arm="evolved",
            runner=evolved_runner,
        )
        baseline_prompt = baseline.build_frozen_prompt(
            FrozenAgentRequestV2(rendered_public_prompt=_OFFICIAL_PROMPT)
        )
        evolved_prompt = evolved.build_frozen_prompt(
            FrozenAgentRequestV2(
                rendered_public_prompt=_OFFICIAL_PROMPT,
                resolved_text_memory=_memory(),
            )
        )

    assert baseline_prompt.endswith(_OFFICIAL_PROMPT)
    assert evolved_prompt.endswith(_OFFICIAL_PROMPT)
    assert "Frozen Core-resolved text memory:" not in baseline_prompt
    assert "Frozen Core-resolved text memory:" in evolved_prompt


def test_core_memory_requires_issuer_and_candidate_is_rejected() -> None:
    markdown = "Check units."
    with pytest.raises(TypeError):
        CoreResolvedTextMemoryV2(
            core_artifact_id="artifact",
            artifact_payload_sha256="a" * 64,
            context_resolution_digest="b" * 64,
            resolved_memory_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
            markdown=markdown,
        )

    candidate = SafeEvolutionReflector().reflect(
        SafeEvolutionSignal(outcome=SignalOutcome.SATISFACTORY),
        artifact_type=ArtifactKind.TEXT_MEMORY,
        round_index=0,
    )
    with pytest.raises(TypeError):
        FrozenAgentRequestV2(
            rendered_public_prompt=_OFFICIAL_PROMPT,
            resolved_text_memory=candidate,  # type: ignore[arg-type]
        )

    runner = _RecordingRunner(_safe_transcript())
    with tempfile.TemporaryDirectory() as temporary:
        executor = _executor(
            Path(temporary),
            arm="baseline",
            runner=runner,
        )
        with pytest.raises(TypeError):
            executor.execute_frozen(candidate)  # type: ignore[arg-type]
        executor.close()
    assert runner.calls == []
