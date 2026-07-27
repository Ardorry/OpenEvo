from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

import openevo_chembench.local_codex_executor as executor_module
from openevo_chembench.frozen_runtime_v2 import (
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
    LocalCommandResult,
    load_taskwise_success_receipts_v1,
)
from openevo_chembench.supervised_transfer_v1.artifacts import (
    CoreResolvedSupervisedContextV1,
    _issue_core_resolved_auxiliary_v1,
)
from openevo_chembench.supervised_transfer_v1.context_binding import (
    SupervisedAgentRequestV1,
    SupervisedContextBindingError,
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
    run_id: str | None = None,
    task_uid: str | None = None,
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
        run_id=run_id,
        task_uid=task_uid,
    )


def _supervised_context() -> CoreResolvedSupervisedContextV1:
    memory = _memory()
    skill_markdown = (
        "# Category Skill: Name_Conversion\n\n"
        "## When To Use\n- Use for naming tasks.\n\n"
        "## Workflow\n1. Apply nomenclature precedence.\n\n"
        "## Validation Checks\n- Round-trip the name.\n\n"
        "## Failure Guards\n- Reject incompatible locants.\n"
    )
    agent_markdown = (
        "# Category Agent System: Name_Conversion\n\n"
        "## Directives\n- Apply explicit chemical constraints.\n\n"
        "## Output Discipline\n- Return one uppercase option.\n"
    )
    return CoreResolvedSupervisedContextV1(
        memory=memory,
        skill=_issue_core_resolved_auxiliary_v1(
            target_id="skill_bundle",
            core_artifact_id="art_skill_1",
            artifact_payload_sha256="c" * 64,
            context_resolution_digest="d" * 64,
            resolved_content_sha256=hashlib.sha256(skill_markdown.encode()).hexdigest(),
            markdown=skill_markdown,
        ),
        agent_system=_issue_core_resolved_auxiliary_v1(
            target_id="agent_system",
            core_artifact_id="art_agent_1",
            artifact_payload_sha256="e" * 64,
            context_resolution_digest="f" * 64,
            resolved_content_sha256=hashlib.sha256(agent_markdown.encode()).hexdigest(),
            markdown=agent_markdown,
        ),
    )


def _supervised_request(
    *,
    session_id: str,
    context: CoreResolvedSupervisedContextV1 | None,
) -> SupervisedAgentRequestV1:
    return SupervisedAgentRequestV1(
        rendered_public_prompt=_request(
            session_id="temporary-request-id",
            task_ordinal=0,
            round_index=0,
        ).rendered_public_prompt,
        resolved_context=context,
        session_id=session_id,
        arm="online",
        task_ordinal=0,
        round_index=1,
        run_id="supervised-run-0001",
        task_uid="1" * 64,
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


def test_supervised_prompt_and_receipt_bind_all_three_core_targets(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def runner(_command, prompt, _cwd, _environment, _timeout):
        calls.append(prompt)
        return LocalCommandResult(returncode=0, stdout=_transcript(), stderr="")

    executor = _executor(tmp_path, runner)
    context = _supervised_context()
    request = _supervised_request(
        session_id="supervised-session-0001",
        context=context,
    )

    prompt = executor.build_supervised_prompt(request)
    assert context.memory.markdown in prompt
    assert context.skill.markdown in prompt
    assert context.agent_system.markdown in prompt
    assert request.session_id not in prompt
    assert prompt.endswith(request.rendered_public_prompt)

    assert executor.execute_supervised(request).response == "A"
    receipt = executor.consume_supervised_context_receipt(request.session_id)
    assert receipt.passed is True
    assert [value.target_id for value in receipt.actual.targets] == [
        "text_memory",
        "skill_bundle",
        "agent_system",
    ]
    assert len(calls) == 1


def test_supervised_context_digest_mismatch_fails_before_model_call(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def runner(*_args):
        calls.append("called")
        return LocalCommandResult(returncode=0, stdout=_transcript(), stderr="")

    executor = _executor(tmp_path, runner)
    context = _supervised_context()
    object.__setattr__(context.skill, "markdown", context.skill.markdown + "tampered\n")
    request = _supervised_request(
        session_id="supervised-session-0002",
        context=context,
    )
    with pytest.raises(SupervisedContextBindingError):
        executor.execute_supervised(request)
    assert calls == []


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


def test_successful_taskwise_invocation_writes_content_free_private_receipt(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=0, stdout=_transcript(), stderr=""),
    )
    request = _request(
        session_id="session-task-000-round-0",
        task_ordinal=0,
        round_index=0,
        run_id="receipt_run_0001",
        task_uid="d" * 64,
    )

    attempt = executor.execute_taskwise(request)

    receipts = load_taskwise_success_receipts_v1(
        tmp_path / "diagnostics",
        expected_session_ids=(request.session_id,),
    )
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.run_id == request.run_id
    assert receipt.task_uid == request.task_uid
    assert receipt.task_index == request.task_ordinal
    assert receipt.round_index == request.round_index
    assert receipt.session_id == request.session_id
    assert receipt.event_stream_sha256 == attempt.transcript_reference.reference.removeprefix(
        "local-codex-jsonl:sha256:"
    )
    assert receipt.event_count == 4
    assert receipt.tool_event_count == 0
    assert receipt.process_return_code == 0
    assert receipt.completion_observed is True
    assert receipt.cleanup_status == "COMPLETE"
    assert receipt.residual_root_count == 0
    path = next((tmp_path / "diagnostics").glob("success_*.json"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    serialized = path.read_text(encoding="utf-8")
    for forbidden in (
        "public sentinel",
        "target_scores",
        '"response"',
        '"stdout"',
        '"stderr"',
        '"prompt"',
    ):
        assert forbidden not in serialized


def test_success_receipt_loader_rejects_unknown_fields_and_duplicate_sessions(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=0, stdout=_transcript(), stderr=""),
    )
    request = _request(
        session_id="session-task-000-round-0",
        task_ordinal=0,
        round_index=0,
        run_id="receipt_run_0002",
        task_uid="e" * 64,
    )
    executor.execute_taskwise(request)
    diagnostic_root = tmp_path / "diagnostics"
    first = next(diagnostic_root.glob("success_*.json"))
    payload = json.loads(first.read_text(encoding="utf-8"))
    duplicate = diagnostic_root / f"success_{'f' * 32}.json"
    duplicate.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    with pytest.raises(ValueError, match="session is duplicated"):
        load_taskwise_success_receipts_v1(diagnostic_root)

    duplicate.unlink()
    payload["unexpected_private_field"] = "forbidden"
    first.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    first.chmod(0o600)
    with pytest.raises(ValueError, match="schema is invalid"):
        load_taskwise_success_receipts_v1(diagnostic_root)


def test_success_receipt_write_failure_fails_closed_after_completion(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=0, stdout=_transcript(), stderr=""),
    )
    request = _request(
        session_id="session-task-000-round-0",
        task_ordinal=0,
        round_index=0,
        run_id="receipt_run_0003",
        task_uid="a" * 64,
    )

    with patch.object(executor_module, "_persist_success_receipt", return_value=None):
        with pytest.raises(LocalCodexExecutionError) as raised:
            executor.execute_taskwise(request)

    assert raised.value.taskwise_failure_code == "EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED"
    assert raised.value.executor_stage == "PRIVATE_EVENT_PERSISTENCE"
    assert raised.value.completion_observed is True
    assert raised.value.retry_allowed is False
    assert raised.value.resume_allowed is False
    assert raised.value.replacement_completion_allowed is False
    assert tuple((tmp_path / "diagnostics").glob("success_*.json")) == ()


def test_success_receipt_loader_rejects_missing_unsafe_and_extra_evidence(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="directory is unavailable"):
        load_taskwise_success_receipts_v1(tmp_path / "missing")

    executor = _executor(
        tmp_path,
        lambda *_args: LocalCommandResult(returncode=0, stdout=_transcript(), stderr=""),
    )
    request = _request(
        session_id="session-task-000-round-0",
        task_ordinal=0,
        round_index=0,
        run_id="receipt_run_0004",
        task_uid="b" * 64,
    )
    executor.execute_taskwise(request)
    diagnostic_root = tmp_path / "diagnostics"
    receipt = next(diagnostic_root.glob("success_*.json"))

    receipt.chmod(0o644)
    with pytest.raises(ValueError, match="receipt is unreadable"):
        load_taskwise_success_receipts_v1(diagnostic_root)
    receipt.chmod(0o600)

    with pytest.raises(ValueError, match="set does not match sessions"):
        load_taskwise_success_receipts_v1(
            diagnostic_root,
            expected_session_ids=("unexpected-session",),
        )

    malformed = diagnostic_root / "success_not-a-valid-id.json"
    malformed.symlink_to(receipt)
    with pytest.raises(ValueError, match="filename is invalid"):
        load_taskwise_success_receipts_v1(diagnostic_root)
    malformed.unlink()

    symlink = diagnostic_root / f"success_{'c' * 32}.json"
    symlink.symlink_to(receipt)
    with pytest.raises(ValueError, match="receipt is unreadable"):
        load_taskwise_success_receipts_v1(diagnostic_root)
