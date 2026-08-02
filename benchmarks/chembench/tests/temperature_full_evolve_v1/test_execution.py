from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo.rollout.models import TaskStatus

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.temperature_full_evolve_v1.candidate import (
    accept_candidate_completion_v1,
    observe_candidate_task_status_v1,
    prepare_candidate_call_v1,
)
from openevo_chembench.temperature_full_evolve_v1.execution import (
    FormalCallEnvelopeV1,
    TemperatureFormalExecutorV1,
    finalize_candidate_outcome_v1,
    recover_existing_candidate_plan_v1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)
from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    DurableNoCompletionEvidenceV1,
    PersistedRolloutResultAuditV1,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _task() -> PrivateChemBench4KTask:
    return PrivateChemBench4KTask(
        uid="a" * 64,
        category="Temperature_Prediction",
        source_split="test",
        source_index=0,
        question="Which temperature is appropriate for this synthetic conversion?",
        A="-78 C",
        B="0 C",
        C="25 C",
        D="100 C",
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
        text="Question: synthetic\nA. -78 C\nB. 0 C\nC. 25 C\nD. 100 C\nAnswer:",
    )


def _status(task_id: str) -> TaskStatus:
    transcript = "\n".join(
        (
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "A"},
                }
            ),
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
                    "session_id": "sk-openevo-test-session",
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


@dataclass
class _Runtime:
    repository_root: Path
    service_run_id: str
    digest: str
    rollout_url: str = "http://127.0.0.1:8080"

    def require_current(self) -> dict[str, object]:
        return {"runtime_services_identity_sha256": self.digest}


class _Client:
    def __init__(self, *, status: TaskStatus, submitted: list[str]) -> None:
        self.status = status
        self.submitted = submitted

    def submit_task(self, payload: dict[str, Any]) -> str:
        task_id = str(payload["task_id"])
        self.submitted.append(task_id)
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        assert task_id == self.status.task_id
        return self.status.model_dump(mode="json")

    def close(self) -> None:
        return None


def _audit(status: TaskStatus) -> PersistedRolloutResultAuditV1:
    session = status.results[0]
    return PersistedRolloutResultAuditV1(
        state="PROVEN_COMPLETE",
        service_run_id="runtime-service-001",
        task_id_sha256=_sha(status.task_id),
        session_id_sha256=_sha(session.session_id),
        result_sha256="d" * 64,
        result_size_bytes=100,
        terminal_status="COMPLETED",
        completion_exists=True,
        completion_sha256=_sha("A"),
        finding_code=None,
        result=session,
    )


def _dual_proof(**_kwargs: object) -> DurableNoCompletionEvidenceV1:
    raise AssertionError("completion path must not request no-completion proof")


def test_claim_before_submit_and_private_candidate_finalization(tmp_path: Path) -> None:
    run_id = "formal-run-2001"
    service_digest = "1" * 64
    submitted: list[str] = []
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "ledger.jsonl").resolve(), run_id=run_id
    ) as ledger:
        plan = prepare_candidate_call_v1(
            task=_task(),
            prompt=_prompt(),
            context=None,
            context_workspace=None,
            phase="baseline_test",
            task_ordinal=0,
            batch_index=None,
            ledger=ledger,
            run_id=run_id,
            service_identity_sha256=service_digest,
        )
        status = _status(plan.task_request.task_id)
        executor = TemperatureFormalExecutorV1(
            runtime_services=_Runtime(
                repository_root=tmp_path.resolve(),
                service_run_id="runtime-service-001",
                digest=service_digest,
            ),
            ledger=ledger,
            client_factory=lambda: _Client(status=status, submitted=submitted),
            audit_function=lambda **_kwargs: _audit(status),
            no_completion_audit_function=_dual_proof,
            poll_interval_seconds=0,
            max_poll_attempts=1,
        )
        outcome = executor.run_many_to_durable_terminal(
            (FormalCallEnvelopeV1.from_candidate(plan),), max_workers=1
        )[0]
        assert ledger.events[0]["kind"] == "CALL_CLAIMED"
        assert submitted == [plan.task_request.task_id]
        evaluated = finalize_candidate_outcome_v1(
            outcome=outcome,
            plan=plan,
            task=_task(),
            ledger=ledger,
        )
        assert evaluated.evaluation.correct is True
        assert [event["kind"] for event in ledger.events] == [
            "CALL_CLAIMED",
            "CALL_ACCEPTED",
            "CALL_EVALUATED",
        ]


def test_unresolved_claim_and_accepted_crash_recover_without_submit(tmp_path: Path) -> None:
    run_id = "formal-run-2002"
    service_digest = "2" * 64
    submitted: list[str] = []
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "ledger.jsonl").resolve(), run_id=run_id
    ) as ledger:
        original = prepare_candidate_call_v1(
            task=_task(),
            prompt=_prompt(),
            context=None,
            context_workspace=None,
            phase="baseline_test",
            task_ordinal=0,
            batch_index=None,
            ledger=ledger,
            run_id=run_id,
            service_identity_sha256=service_digest,
        )
        private_checkpoint = FormalCallEnvelopeV1.from_candidate(
            original
        ).to_private_checkpoint_bytes()
        ledger.append("CALL_CLAIMED", original.claim_payload)
        envelope = FormalCallEnvelopeV1.from_private_checkpoint_bytes(private_checkpoint)
        recovered = recover_existing_candidate_plan_v1(
            envelope=envelope,
            task=_task(),
            prompt=_prompt(),
            context=None,
            context_workspace=None,
            phase="baseline_test",
            task_ordinal=0,
            batch_index=None,
            ledger=ledger,
            run_id=run_id,
            service_identity_sha256=service_digest,
        )
        status = _status(recovered.task_request.task_id)
        executor = TemperatureFormalExecutorV1(
            runtime_services=_Runtime(
                repository_root=tmp_path.resolve(),
                service_run_id="runtime-service-001",
                digest=service_digest,
            ),
            ledger=ledger,
            client_factory=lambda: _Client(status=status, submitted=submitted),
            audit_function=lambda **_kwargs: _audit(status),
            no_completion_audit_function=_dual_proof,
            poll_interval_seconds=0,
            max_poll_attempts=1,
        )
        first = executor.run_many_to_durable_terminal((envelope,), max_workers=1)[0]
        observed = observe_candidate_task_status_v1(status, plan=recovered)
        accepted = accept_candidate_completion_v1(
            plan=recovered,
            observed=observed,
            ledger=ledger,
        )
        ledger.append("CALL_ACCEPTED", accepted.call_accepted_payload)
        # Crash here: restart observes the already accepted call and only appends
        # its missing private evaluation.  It does not call submit_task again.
        second = executor.run_many_to_durable_terminal((envelope,), max_workers=1)[0]
        finalize_candidate_outcome_v1(
            outcome=second,
            plan=recovered,
            task=_task(),
            ledger=ledger,
        )
        assert first.recovered_after_restart is True
        assert second.recovered_after_restart is True
        assert submitted == []
        assert sum(event["kind"] == "CALL_ACCEPTED" for event in ledger.events) == 1
        assert sum(event["kind"] == "CALL_EVALUATED" for event in ledger.events) == 1
