from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from openevo.rollout.models import TaskStatus

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.frozen_runtime_v2 import _issue_core_resolved_text_memory_v2
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
    _issue_core_resolved_auxiliary_v2,
)
from openevo_chembench.temperature_full_evolve_v1.candidate import (
    TemperatureCandidateError,
    accept_candidate_completion_v1,
    evaluate_candidate_completion_v1,
    observe_candidate_task_status_v1,
    prepare_candidate_call_v1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _task() -> PrivateChemBench4KTask:
    return PrivateChemBench4KTask(
        uid="a" * 64,
        category="Temperature_Prediction",
        source_split="test",
        source_index=0,
        question="Which temperature is appropriate for this synthetic test conversion?",
        A="-78 °C",
        B="0 °C",
        C="25 °C",
        D="100 °C",
        target="A",
        dataset_revision=CHEMBENCH4K_REVISION,
        dataset_sha256="b" * 64,
    )


def _prompt() -> RenderedChemBench4KPrompt:
    return RenderedChemBench4KPrompt(
        uid="a" * 64,
        category="Temperature_Prediction",
        dataset_revision=CHEMBENCH4K_REVISION,
        demonstration_uids=("c" * 64,) * 5,
        text=(
            "There is a single choice question about chemistry.\n"
            "Question: synthetic test\nA. -78 C\nB. 0 C\nC. 25 C\nD. 100 C\nAnswer:"
        ),
    )


def _context() -> CoreResolvedSupervisedContextV2:
    memory = "# Temperature Prediction Memory\n\n## Do\n\n- Prefer explicit cooling evidence.\n"
    skill = "# Temperature Prediction Skill\n\n## Workflow\n\n1. Check units.\n"
    system = "# Temperature Prediction Agent System\n\n## Directives\n\n- Check units.\n"
    return CoreResolvedSupervisedContextV2(
        memory=_issue_core_resolved_text_memory_v2(
            core_artifact_id="temperature-memory-b1",
            artifact_payload_sha256=_sha(memory),
            context_resolution_digest="1" * 64,
            resolved_memory_sha256=_sha(memory),
            markdown=memory,
        ),
        skill=_issue_core_resolved_auxiliary_v2(
            target_id="skill_bundle",
            core_artifact_id="temperature-skill-b1",
            artifact_payload_sha256=_sha(skill),
            context_resolution_digest="2" * 64,
            resolved_content_sha256=_sha(skill),
            markdown=skill,
        ),
        agent_system=_issue_core_resolved_auxiliary_v2(
            target_id="agent_system",
            core_artifact_id="temperature-system-b1",
            artifact_payload_sha256=_sha(system),
            context_resolution_digest="3" * 64,
            resolved_content_sha256=_sha(system),
            markdown=system,
        ),
    )


def _status(task_id: str, *, tool_event: bool = False) -> TaskStatus:
    item = (
        {"type": "command_execution", "command": "forbidden"}
        if tool_event
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
    return TaskStatus.model_validate(
        {
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
                                "response_messages": [
                                    {"role": "assistant", "content": "A"}
                                ],
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
    )


def _failure(call_id: str) -> dict[str, object]:
    return {
        "logical_call_id": call_id.rsplit("-a", 1)[0],
        "call_id": call_id,
        "failure_class": "infrastructure_no_completion",
        "terminal_task_status": "failed",
        "durable_rollout_no_completion": True,
        "durable_gateway_absent": True,
        "no_completion_evidence_sha256": "f" * 64,
    }


def test_generation_zero_candidate_uses_formal_template_and_exactly_once_ledger(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-1001") as ledger:
        plan = prepare_candidate_call_v1(
            task=_task(),
            prompt=_prompt(),
            context=None,
            context_workspace=None,
            phase="baseline_test",
            task_ordinal=0,
            batch_index=None,
            ledger=ledger,
            run_id="formal-run-1001",
            service_identity_sha256="1" * 64,
        )
        assert plan.task_request.agent.model_name == "gpt-5.5"
        assert plan.task_request.agent.settings["reasoning_effort"] == "medium"
        assert plan.task_request.agent.settings["tool_policy"] == "disabled"
        assert plan.task_request.agent.mcp_servers == []
        assert plan.task_request.runtime.profile == "managed_science"
        assert (
            plan.task_request.metadata["openevo_chembench"][
                "runtime_services_identity_sha256"
            ]
            == "1" * 64
        )
        policy = plan.task_request.metadata["openevo_chembench"][
            "temperature_full_evolve"
        ]
        assert policy["model_visible_tools_enabled"] is False
        assert policy["provider_transport_network_enabled"] is True
        assert plan.task_request.instruction.endswith(_prompt().text)
        assert "Approved Core-resolved category memory" not in plan.task_request.instruction
        ledger.append("CALL_CLAIMED", plan.claim_payload)
        observed = observe_candidate_task_status_v1(
            _status(plan.task_request.task_id),
            plan=plan,
        )
        accepted = accept_candidate_completion_v1(
            plan=plan,
            observed=observed,
            ledger=ledger,
        )
        ledger.append("CALL_ACCEPTED", accepted.call_accepted_payload)
        evaluated = evaluate_candidate_completion_v1(task=_task(), accepted=accepted)
        ledger.append("CALL_EVALUATED", evaluated.call_evaluated_payload)
        assert evaluated.evaluation.correct is True
        assert evaluated.evaluation.strict.prediction == "A"


def test_evolved_context_is_stably_materialized_and_baseline_rejects_it(tmp_path: Path) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    workspace = (tmp_path / "contexts" / "c1").resolve()
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-1002") as ledger:
        plan = prepare_candidate_call_v1(
            task=_task(),
            prompt=_prompt(),
            context=_context(),
            context_workspace=workspace,
            phase="train_post",
            task_ordinal=0,
            batch_index=1,
            ledger=ledger,
            run_id="formal-run-1002",
            service_identity_sha256="2" * 64,
        )
        assert "Approved Core-resolved category memory" in plan.task_request.instruction
        assert (workspace / "AGENTS.md").is_file()
        assert tuple(workspace.glob(".openevo-approved-skills/*/SKILL.md"))
        upload = plan.task_request.runtime.prepare[0]
        assert upload.type == "upload_dir"
        assert upload.source == str(workspace)
        with pytest.raises(TemperatureCandidateError, match="BASELINE_CONTEXT_FORBIDDEN"):
            prepare_candidate_call_v1(
                task=_task(),
                prompt=_prompt(),
                context=_context(),
                context_workspace=workspace,
                phase="baseline_test",
                task_ordinal=0,
                batch_index=None,
                ledger=ledger,
                run_id="formal-run-1002",
                service_identity_sha256="2" * 64,
            )


def test_retry_needs_proven_no_completion_and_tool_event_fails_closed(tmp_path: Path) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    arguments = {
        "task": _task(),
        "prompt": _prompt(),
        "context": None,
        "context_workspace": None,
        "phase": "train_pre",
        "task_ordinal": 0,
        "batch_index": 1,
        "run_id": "formal-run-1003",
        "service_identity_sha256": "3" * 64,
    }
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-1003") as ledger:
        plan = prepare_candidate_call_v1(ledger=ledger, **arguments)
        ledger.append("CALL_CLAIMED", plan.claim_payload)
        with pytest.raises(TemperatureCandidateError, match="CANDIDATE_CALL_OWNERSHIP_UNRESOLVED"):
            prepare_candidate_call_v1(ledger=ledger, **arguments)
        ledger.append("CALL_NO_COMPLETION_FAILURE", _failure(plan.call_id))
        retry = prepare_candidate_call_v1(ledger=ledger, **arguments)
        assert retry.attempt_number == 2
        assert (
            retry.claim_payload["retry_semantics_sha256"]
            == plan.claim_payload["retry_semantics_sha256"]
        )
        ledger.append("CALL_CLAIMED", retry.claim_payload)
        with pytest.raises(TemperatureCandidateError, match="CANDIDATE_TERMINAL_RESULT_INVALID"):
            observe_candidate_task_status_v1(
                _status(retry.task_request.task_id, tool_event=True),
                plan=retry,
            )
