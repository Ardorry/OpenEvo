from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from openevo.runtime.managed import (
    MANAGED_CODEX_BINARY,
    MANAGED_RUNTIME_RELEASES,
    MANAGED_WORKSPACE,
)

from openevo_chembench.frozen_runtime_v2 import _issue_core_resolved_text_memory_v2
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
    _issue_core_resolved_auxiliary_v2,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedAgentRequestV2,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedManagedCodexExecutorV2,
    SupervisedTaskExecutionCodeV2,
    SupervisedTaskExecutionErrorV2,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _context() -> CoreResolvedSupervisedContextV2:
    memory = "# Category Memory: Name_Conversion\n\n## Output Discipline\n- answer once\n"
    skill = "# Category Skill: Name_Conversion\n\n## Workflow\n- validate naming\n"
    agent_system = (
        "# Category Agent System: Name_Conversion\n\n## Directives\n- use approved context\n"
    )
    return CoreResolvedSupervisedContextV2(
        memory=_issue_core_resolved_text_memory_v2(
            core_artifact_id="memory-artifact-v2",
            artifact_payload_sha256=_digest(memory),
            context_resolution_digest="1" * 64,
            resolved_memory_sha256=_digest(memory),
            markdown=memory,
        ),
        skill=_issue_core_resolved_auxiliary_v2(
            target_id="skill_bundle",
            core_artifact_id="skill-artifact-v2",
            artifact_payload_sha256=_digest(skill),
            context_resolution_digest="2" * 64,
            resolved_content_sha256=_digest(skill),
            markdown=skill,
        ),
        agent_system=_issue_core_resolved_auxiliary_v2(
            target_id="agent_system",
            core_artifact_id="system-artifact-v2",
            artifact_payload_sha256=_digest(agent_system),
            context_resolution_digest="3" * 64,
            resolved_content_sha256=_digest(agent_system),
            markdown=agent_system,
        ),
    )


def _request(
    *,
    arm: str = "control",
    context: CoreResolvedSupervisedContextV2 | None = None,
) -> SupervisedAgentRequestV2:
    return SupervisedAgentRequestV2(
        rendered_public_prompt=(
            "There is a single choice question about chemistry.\n"
            "Answer the question by replying A, B, C or D.\n"
            "Question: synthetic question\n"
            "A. alpha\nB. beta\nC. gamma\nD. delta\nAnswer:"
        ),
        resolved_context=context,
        session_id="session-v2-00000001",
        arm=arm,  # type: ignore[arg-type]
        task_ordinal=0,
        round_index=0,
        run_id="run-v2-00000001",
        task_uid="a" * 64,
    )


class _NoopClient:
    def submit_task(self, payload: dict[str, object]) -> str:
        return str(payload["task_id"])

    def get_task(self, task_id: str) -> dict[str, object]:
        raise AssertionError(task_id)

    def cancel_task(self, task_id: str) -> dict[str, object]:
        raise AssertionError(task_id)

    def close(self) -> None:
        return None


class _CompletedClient:
    def __init__(self, *, tool_event: bool = False) -> None:
        self.payload: dict[str, object] | None = None
        self.tool_event = tool_event
        self.upload_source: Path | None = None

    def submit_task(self, payload: dict[str, object]) -> str:
        self.payload = payload
        runtime = payload["runtime"]
        assert isinstance(runtime, dict)
        prepare = runtime["prepare"]
        assert isinstance(prepare, list)
        upload = prepare[0]
        assert isinstance(upload, dict)
        source = Path(str(upload["source"]))
        self.upload_source = source
        assert (source / "AGENTS.md").is_file()
        assert tuple(source.glob(".openevo-approved-skills/*/SKILL.md"))
        return str(payload["task_id"])

    def get_task(self, task_id: str) -> dict[str, object]:
        item = (
            {"type": "command_execution", "command": "forbidden"}
            if self.tool_event
            else {"type": "agent_message", "text": "A"}
        )
        transcript = "\n".join(
            (
                json.dumps({"type": "thread.started"}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.completed", "item": item}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                        },
                    }
                ),
            )
        )
        return {
            "task_id": task_id,
            "status": "completed",
            "total_sessions": 1,
            "completed_sessions": 1,
            "results": [
                {
                    "session_id": "sk-openevo-synthetic",
                    "task_id": task_id,
                    "status": "COMPLETED",
                    "trajectory": {
                        "status": "COMPLETED",
                        "metadata": {"capture_mode": "transcript"},
                        "traces": [
                            {
                                "response_messages": [{"role": "assistant", "content": "A"}],
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "transcript": transcript,
                                },
                            }
                        ],
                    },
                }
            ],
        }

    def cancel_task(self, task_id: str) -> dict[str, object]:
        raise AssertionError(task_id)

    def close(self) -> None:
        return None


class _FailedClient(_CompletedClient):
    def __init__(self, *, completion: str | None) -> None:
        super().__init__()
        self.completion = completion

    def get_task(self, task_id: str) -> dict[str, object]:
        response_messages = (
            [] if self.completion is None else [{"role": "assistant", "content": self.completion}]
        )
        return {
            "task_id": task_id,
            "status": "failed",
            "total_sessions": 1,
            "completed_sessions": 1,
            "results": [
                {
                    "session_id": "failed-session",
                    "task_id": task_id,
                    "status": "ERROR",
                    "trajectory": {
                        "status": "ERROR",
                        "metadata": {"capture_mode": "transcript"},
                        "traces": [
                            {
                                "response_messages": response_messages,
                                "metadata": {"capture_mode": "transcript"},
                            }
                        ],
                    },
                    "error": "redacted infrastructure failure",
                }
            ],
        }


class _CompletedTaskFailedSessionClient(_FailedClient):
    def get_task(self, task_id: str) -> dict[str, object]:
        result = super().get_task(task_id)
        result["status"] = "completed"
        return result


def test_owned_rollout_client_requires_runtime_service_attestation() -> None:
    with pytest.raises(ValueError, match="attested v2 service identity"):
        SupervisedManagedCodexExecutorV2(
            arm="control",
            timeout_seconds=1200,
        )


def test_control_compiles_to_official_managed_task_request() -> None:
    executor = SupervisedManagedCodexExecutorV2(
        arm="control",
        timeout_seconds=1200,
        rollout_client=_NoopClient(),
    )

    task = executor.build_task_request(_request())

    assert task.agent.harness == "codex"
    assert task.agent.model_name == "gpt-5.5"
    assert task.agent.settings == {
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "reasoning_effort": "medium",
    }
    assert task.agent.env == {}
    assert task.agent.mcp_servers == []
    assert task.agent.skills_path is None
    assert task.runtime is not None
    assert task.runtime.profile == "managed_science"
    assert task.runtime.image == MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id
    assert task.runtime.network is None
    assert task.runtime.allow_internet is True
    assert task.runtime.workdir == MANAGED_WORKSPACE
    assert MANAGED_CODEX_BINARY.startswith("/opt/codex/")
    assert task.builder.strategy == "agent_transcript"
    assert task.evaluator is None


def test_online_context_is_uploaded_through_managed_runtime() -> None:
    client = _CompletedClient()
    executor = SupervisedManagedCodexExecutorV2(
        arm="online",
        timeout_seconds=1200,
        rollout_client=client,
    )

    attempt = executor.execute(_request(arm="online", context=_context()))

    assert attempt.response == "A"
    assert attempt.transcript_reference.reference.startswith("openevo-rollout-jsonl:sha256:")
    assert client.payload is not None
    assert client.payload["agent"]["harness"] == "codex"  # type: ignore[index]
    assert client.payload["instruction"].endswith("Answer:")
    assert "Approved Core-resolved category memory" in client.payload["instruction"]
    receipt = executor.consume_context_receipt("session-v2-00000001")
    assert receipt.passed


def test_real_online_context_stages_inside_gateway_visible_private_service_root(
    tmp_path: Path,
) -> None:
    receipt_path = (
        tmp_path
        / "state/chembench_supervised_transfer_v2/runtime_services/runs/service-v2"
        / "runtime_services_receipt_v2.json"
    )
    receipt_path.parent.mkdir(mode=0o700, parents=True)
    client = _CompletedClient()
    runtime_services = SimpleNamespace(
        digest="f" * 64,
        receipt_path=receipt_path,
        repository_root=tmp_path,
        require_current=dict,
    )
    executor = SupervisedManagedCodexExecutorV2(
        arm="online",
        timeout_seconds=1200,
        rollout_client=client,
        runtime_services=runtime_services,
    )

    attempt = executor.execute(_request(arm="online", context=_context()))

    assert attempt.response == "A"
    assert client.upload_source is not None
    staging_root = receipt_path.parent / "candidate_workspace_staging"
    assert client.upload_source.is_relative_to(staging_root)
    assert stat.S_IMODE(staging_root.stat().st_mode) == 0o700
    assert not client.upload_source.exists()


def test_tool_event_fails_closed_after_official_transcript_capture() -> None:
    executor = SupervisedManagedCodexExecutorV2(
        arm="online",
        timeout_seconds=1200,
        rollout_client=_CompletedClient(tool_event=True),
    )

    with pytest.raises(SupervisedTaskExecutionErrorV2) as captured:
        executor.execute(_request(arm="online", context=_context()))

    assert captured.value.code is SupervisedTaskExecutionCodeV2.TOOL_EVENT


def test_candidate_module_has_no_direct_codex_process_path() -> None:
    source = Path(
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/executor.py"
    ).read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "LocalCodexCLIExecutor" not in source
    assert "codex_executable" not in source
    assert "TaskRequest(" in source


@pytest.mark.parametrize("completion", [None, "B"])
def test_terminal_failure_reports_only_completion_presence_and_digest(
    completion: str | None,
) -> None:
    executor = SupervisedManagedCodexExecutorV2(
        arm="online",
        timeout_seconds=1200,
        rollout_client=_FailedClient(completion=completion),
    )

    with pytest.raises(SupervisedTaskExecutionErrorV2) as captured:
        executor.execute(_request(arm="online", context=_context()))

    assert captured.value.code is SupervisedTaskExecutionCodeV2.TASK_FAILED
    assert captured.value.completion_exists is (completion is not None)
    assert captured.value.completion_sha256 == (
        None if completion is None else hashlib.sha256(completion.encode()).hexdigest()
    )


@pytest.mark.parametrize("completion", [None, "B"])
def test_completed_rollout_task_with_failed_session_is_retry_classified_by_completion(
    completion: str | None,
) -> None:
    executor = SupervisedManagedCodexExecutorV2(
        arm="online",
        timeout_seconds=1200,
        rollout_client=_CompletedTaskFailedSessionClient(completion=completion),
    )

    with pytest.raises(SupervisedTaskExecutionErrorV2) as captured:
        executor.execute(_request(arm="online", context=_context()))

    assert captured.value.code is SupervisedTaskExecutionCodeV2.TASK_FAILED
    assert captured.value.completion_exists is (completion is not None)
    assert captured.value.completion_sha256 == (
        None if completion is None else hashlib.sha256(completion.encode()).hexdigest()
    )
