from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openevo_chembench.frozen_runtime_v2 import (
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
    LocalCommandResult,
)
from openevo_chembench.taskwise_config_v1 import (
    ONLINE_PROTOCOL_ID,
    PROTOCOL_MARKERS,
    SOURCE_COMMIT_PLACEHOLDER,
    TASKWISE_MEMORY_LIMITS_V1,
    TaskwiseEvolutionPolicyV1,
    TaskwiseExecutorPolicyV1,
    TaskwiseExperimentConfigV1,
)
from openevo_chembench.taskwise_online_runner_v1 import TaskwiseAgentRequestV1


def _config() -> TaskwiseExperimentConfigV1:
    return TaskwiseExperimentConfigV1(
        protocol_id=ONLINE_PROTOCOL_ID,
        protocol_markers=PROTOCOL_MARKERS,
        arm="online",
        scope="canary9",
        run_name="online_canary9",
        output_directory="OpenEvo/results/taskwise/online",
        dataset_root="data/chembench4k/snapshot",
        dataset_manifest="data/chembench4k/snapshot/manifest.json",
        task_manifest="OpenEvo/benchmarks/chembench/manifests/online/public.jsonl",
        private_task_manifest=(
            "OpenEvo/benchmarks/chembench/private_manifests/online/private.jsonl"
        ),
        codex_cli_version="0.144.6",
        prompt_renderer_id="official_category_five_shot_v1",
        parser_id="official_first_capital_v1",
        evaluator_id="chembench4k_private_accuracy_v1",
        attempts_per_task=3,
        inter_round_slots=2,
        evolution_updates_per_task=2,
        carry_memory_across_tasks=True,
        fixed_round_budget=True,
        stop_when_correct=False,
        final_score_round=2,
        memory_limits=TASKWISE_MEMORY_LIMITS_V1,
        evolution=TaskwiseEvolutionPolicyV1(
            enabled=True,
            target="text_memory",
            method_id="text_memory_expel_reflector",
        ),
        executor=TaskwiseExecutorPolicyV1(
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
        source_commit=SOURCE_COMMIT_PLACEHOLDER,
    )


def _memory():
    markdown = (
        "# General Chemistry Memory\n\n"
        "## Do\n- Verify the reasoning independently.\n\n"
        "## Avoid\n- Avoid premature selection.\n\n"
        "## Validate\n- Check units and structures.\n\n"
        "## When Applicable\n- Use conservation checks.\n\n"
        "## Retired Or Superseded\n- None.\n"
    )
    return _issue_core_resolved_text_memory_v2(
        core_artifact_id="art_taskwise_1",
        artifact_payload_sha256="a" * 64,
        context_resolution_digest="b" * 64,
        resolved_memory_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        markdown=markdown,
    )


def _request(
    *,
    session_id: str,
    task_ordinal: int,
    round_index: int,
    memory=None,
) -> TaskwiseAgentRequestV1:
    return TaskwiseAgentRequestV1(
        rendered_public_prompt=(
            "There is a single choice question about chemistry. "
            "Answer the question by replying A, B, C or D.\n"
            "Question: public sentinel?\nA. one\nB. two\nC. three\nD. four\n"
            "Answer:"
        ),
        resolved_text_memory=memory,
        session_id=session_id,
        arm="online",
        task_ordinal=task_ordinal,
        round_index=round_index,  # type: ignore[arg-type]
    )


def _transcript() -> str:
    events = (
        {"type": "thread.started"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "A"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            },
        },
    )
    return "\n".join(json.dumps(event) for event in events)


def _executor(tmp_path: Path, runner) -> LocalCodexCLIExecutor:
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    return LocalCodexCLIExecutor(
        config=_config(),
        codex_executable=Path("/bin/true"),
        auth_file=auth,
        command_runner=runner,
        verified_codex_version="0.144.6",
        isolation_parent=tmp_path,
        diagnostic_root=tmp_path / "diagnostics",
    )


def test_taskwise_prompt_contains_only_public_prompt_and_core_memory(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=0, stdout=_transcript(), stderr=""),
    )
    request = _request(
        session_id="session-task-000-round-1",
        task_ordinal=0,
        round_index=1,
        memory=_memory(),
    )

    prompt = executor.build_taskwise_prompt(request)

    assert request.session_id not in prompt
    assert "task_ordinal" not in prompt
    assert "round_index" not in prompt
    assert "General Chemistry Memory" in prompt
    assert prompt.endswith(request.rendered_public_prompt)


def test_first_online_round_must_not_receive_unapproved_memory(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=0, stdout=_transcript(), stderr=""),
    )
    request = _request(
        session_id="session-task-000-round-0",
        task_ordinal=0,
        round_index=0,
        memory=_memory(),
    )
    with pytest.raises(LocalCodexExecutionError):
        executor.build_taskwise_prompt(request)


def test_taskwise_session_id_is_single_use_without_mock_fallback(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def runner(_command, prompt, cwd, _environment, _timeout):
        assert not any(cwd.iterdir())
        calls.append(prompt)
        return LocalCommandResult(returncode=0, stdout=_transcript(), stderr="")

    executor = _executor(tmp_path, runner)
    request = _request(
        session_id="session-task-000-round-0",
        task_ordinal=0,
        round_index=0,
    )
    assert executor.execute_taskwise(request).response == "A"
    receipt = executor.consume_taskwise_context_receipt(request.session_id)
    assert receipt.passed is True
    assert receipt.expected == receipt.actual
    assert receipt.actual.to_dict() == {
        "session_id": request.session_id,
        "core_artifact_id": None,
        "artifact_payload_sha256": None,
        "resolved_memory_sha256": None,
        "context_resolution_digest": None,
    }
    with pytest.raises(RuntimeError, match="receipt is unavailable"):
        executor.consume_taskwise_context_receipt(request.session_id)
    with pytest.raises(LocalCodexExecutionError):
        executor.execute_taskwise(request)
    assert len(calls) == 1
    executor.close()
    assert not tuple(tmp_path.glob("openevo-chembench-local-codex-*"))


def test_three_taskwise_rounds_use_three_independent_invocation_roots(
    tmp_path: Path,
) -> None:
    invocation_roots: list[Path] = []

    def runner(_command, _prompt, cwd, _environment, _timeout):
        assert not any(cwd.iterdir())
        invocation_roots.append(cwd.parent)
        return LocalCommandResult(returncode=0, stdout=_transcript(), stderr="")

    executor = _executor(tmp_path, runner)
    memory = _memory()
    for round_index in range(3):
        request = _request(
            session_id=f"session-task-000-round-{round_index}",
            task_ordinal=0,
            round_index=round_index,
            memory=None if round_index == 0 else memory,
        )
        assert executor.execute_taskwise(request).response == "A"

    assert len(invocation_roots) == 3
    assert len({path.name for path in invocation_roots}) == 3
    assert all(not path.exists() for path in invocation_roots)
    executor.close()
