from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from openevo.rollout.models import TaskStatus

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
)
from openevo_chembench.supervised_transfer_v2.executor import task_request_digest_v2
from openevo_chembench.temperature_full_evolve_v1.candidate import (
    accept_candidate_completion_v1,
    observe_candidate_task_status_v1,
    prepare_candidate_call_v1,
)
from openevo_chembench.temperature_full_evolve_v1.execution import (
    MAX_CANDIDATE_WORKERS,
    FormalCallEnvelopeV1,
    TemperatureFormalExecutionError,
    TemperatureFormalExecutorV1,
    finalize_candidate_outcome_v1,
    recover_existing_candidate_plan_v1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)
from openevo_chembench.temperature_full_evolve_v1.reflector import (
    prepare_reflector_call_v1,
)
from openevo_chembench.temperature_full_evolve_v1.retry_semantics import (
    task_request_retry_semantics_sha256_v1,
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


def _terminal_no_completion(call_id: str, logical_call_id: str) -> dict[str, object]:
    return {
        "logical_call_id": logical_call_id,
        "call_id": call_id,
        "failure_class": "durable_rollout_terminal_no_completion",
        "terminal_task_status": "FAILED",
        "durable_rollout_no_completion": True,
        "durable_gateway_absent": True,
        "no_completion_evidence_sha256": "f" * 64,
    }


def test_executor_rejects_parallel_workers_before_any_side_effect(
    tmp_path: Path,
) -> None:
    service_digest = "0" * 64
    client_factory_calls: list[str] = []
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "serial-ledger.jsonl").resolve(),
        run_id="formal-run-serial-workers-0001",
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
            run_id="formal-run-serial-workers-0001",
            service_identity_sha256=service_digest,
        )

        def forbidden_client_factory() -> _Client:
            client_factory_calls.append("called")
            raise AssertionError("parallel policy must fail before transport")

        executor = TemperatureFormalExecutorV1(
            runtime_services=_Runtime(
                repository_root=tmp_path.resolve(),
                service_run_id="runtime-service-serial-001",
                digest=service_digest,
            ),
            ledger=ledger,
            client_factory=forbidden_client_factory,
            audit_function=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("parallel policy must fail before audit")
            ),
            no_completion_audit_function=_dual_proof,
            poll_interval_seconds=0,
            max_poll_attempts=1,
        )

        assert MAX_CANDIDATE_WORKERS == 1
        with pytest.raises(ValueError, match="formal execution batch is invalid"):
            executor.run_many_to_durable_terminal(
                (FormalCallEnvelopeV1.from_candidate(plan),),
                max_workers=2,
            )
        assert ledger.events == ()
        assert client_factory_calls == []


def test_executor_closes_first_durable_terminal_before_second_admission(
    tmp_path: Path,
) -> None:
    run_id = "formal-run-serial-order-0001"
    service_digest = "e" * 64
    trace: list[str] = []

    class _TracingLedger:
        def __init__(self, inner: TemperatureExperimentLedgerV1) -> None:
            self.inner = inner

        @property
        def events(self) -> tuple[dict[str, Any], ...]:
            return self.inner.events

        def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            if kind in {"CALL_CLAIMED", "CALL_NO_COMPLETION_FAILURE"}:
                trace.append(f"ledger:{kind}:{payload['call_id']}")
            return self.inner.append(kind, payload)

        def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None:
            return self.inner.accepted_call(logical_call_id)

        def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None:
            return self.inner.latest_claim(logical_call_id)

        def failure_has_no_completion(self, call_id: str) -> bool:
            return self.inner.failure_has_no_completion(call_id)

        def completion_was_rejected(self, call_id: str) -> bool:
            return self.inner.completion_was_rejected(call_id)

    class _TracingRuntime:
        repository_root = tmp_path.resolve()
        service_run_id = "runtime-service-001"
        digest = service_digest

        def __init__(self) -> None:
            self.health_checks = 0

        def require_current(self) -> dict[str, object]:
            self.health_checks += 1
            trace.append(f"health:{self.health_checks}")
            return {"runtime_services_identity_sha256": self.digest}

    class _TracingClient:
        def submit_task(self, payload: dict[str, Any]) -> str:
            task_id = str(payload["task_id"])
            trace.append(f"submit:{task_id}")
            return task_id

        def get_task(self, task_id: str) -> dict[str, Any]:
            trace.append(f"poll:{task_id}")
            raise RuntimeError("force durable evidence path")

        def close(self) -> None:
            return None

    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "serial-order-ledger.jsonl").resolve(),
        run_id=run_id,
    ) as inner:
        ledger = _TracingLedger(inner)
        first = prepare_candidate_call_v1(
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
        second = prepare_candidate_call_v1(
            task=_task(),
            prompt=_prompt(),
            context=None,
            context_workspace=None,
            phase="baseline_test",
            task_ordinal=1,
            batch_index=None,
            ledger=ledger,
            run_id=run_id,
            service_identity_sha256=service_digest,
        )
        first_task_id = first.task_request.task_id
        second_task_id = second.task_request.task_id
        second_status = _status(second_task_id)

        def audit(**values: object) -> PersistedRolloutResultAuditV1:
            task_id = str(values["task_id"])
            trace.append(f"durable_audit:{task_id}")
            if task_id == second_task_id:
                return _audit(second_status)
            assert task_id == first_task_id
            return PersistedRolloutResultAuditV1(
                state="PROVEN_TERMINAL_NO_COMPLETION",
                service_run_id="runtime-service-001",
                task_id_sha256=_sha(task_id),
                session_id_sha256="1" * 64,
                result_sha256="2" * 64,
                result_size_bytes=100,
                terminal_status="FAILED",
                completion_exists=False,
                completion_sha256=None,
                finding_code=None,
                result=None,
            )

        def no_completion_audit(**values: object) -> DurableNoCompletionEvidenceV1:
            task_id = str(values["task_id"])
            assert task_id == first_task_id
            trace.append(f"no_completion_proof:{task_id}")
            return DurableNoCompletionEvidenceV1(
                state="PROVEN_NO_COMPLETION",
                service_run_id="runtime-service-001",
                task_id_sha256=_sha(task_id),
                session_id_sha256="1" * 64,
                rollout_result_sha256="2" * 64,
                rollout_terminal_status="FAILED",
                durable_rollout_no_completion=True,
                durable_gateway_absent=True,
                gateway_absence_basis="gateway_completion_absent",
                finding_code=None,
            )

        executor = TemperatureFormalExecutorV1(
            runtime_services=_TracingRuntime(),
            ledger=ledger,
            client_factory=_TracingClient,
            audit_function=audit,
            no_completion_audit_function=no_completion_audit,
            poll_interval_seconds=0,
            max_poll_attempts=1,
        )
        outcomes = executor.run_many_to_durable_terminal(
            (
                FormalCallEnvelopeV1.from_candidate(first),
                FormalCallEnvelopeV1.from_candidate(second),
            ),
            max_workers=1,
        )

        assert tuple(outcome.state for outcome in outcomes) == (
            "terminal_no_completion",
            "completion",
        )
        assert inner.failure_has_no_completion(first.call_id)
        assert trace == [
            "health:1",
            f"ledger:CALL_CLAIMED:{first.call_id}",
            f"submit:{first_task_id}",
            "health:2",
            f"poll:{first_task_id}",
            f"durable_audit:{first_task_id}",
            f"no_completion_proof:{first_task_id}",
            f"ledger:CALL_NO_COMPLETION_FAILURE:{first.call_id}",
            "health:3",
            f"ledger:CALL_CLAIMED:{second.call_id}",
            f"submit:{second_task_id}",
            "health:4",
            f"poll:{second_task_id}",
            f"durable_audit:{second_task_id}",
        ]


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
        tampered_checkpoint = json.loads(private_checkpoint)
        tampered_checkpoint["envelope"]["claim_payload"][
            "retry_semantics_sha256"
        ] = "f" * 64
        tampered_checkpoint["envelope_sha256"] = sha256_bytes(
            canonical_json_bytes(tampered_checkpoint["envelope"])
        )
        with pytest.raises(
            TemperatureFormalExecutionError,
            match="FORMAL_CALL_CHECKPOINT_INVALID",
        ):
            FormalCallEnvelopeV1.from_private_checkpoint_bytes(
                canonical_json_bytes(tampered_checkpoint)
            )
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


def test_proven_no_completion_admits_only_the_exact_successor_attempt(
    tmp_path: Path,
) -> None:
    run_id = "formal-run-2003"
    service_digest = "3" * 64
    submitted: list[str] = []
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "ledger.jsonl").resolve(), run_id=run_id
    ) as ledger:
        first = prepare_candidate_call_v1(
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
        ledger.append("CALL_CLAIMED", first.claim_payload)
        ledger.append(
            "CALL_NO_COMPLETION_FAILURE",
            _terminal_no_completion(first.call_id, first.logical_call_id),
        )
        successor = prepare_candidate_call_v1(
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
        status = _status(successor.task_request.task_id)
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

        # A closed predecessor is immutable; only its separately identified
        # attempt+1 successor may create a new side effect.
        with pytest.raises(
            TemperatureFormalExecutionError,
            match="FORMAL_COMPLETED_CLAIM_CANNOT_RESUBMIT",
        ):
            executor.run_many_to_durable_terminal(
                (FormalCallEnvelopeV1.from_candidate(first),), max_workers=1
            )
        assert submitted == []

        mismatched_claim = {
            **successor.claim_payload,
            "logical_arm": "mismatched-candidate-arm",
        }
        mismatched = FormalCallEnvelopeV1(
            logical_call_id=successor.logical_call_id,
            call_id=successor.call_id,
            task_request=successor.task_request,
            task_request_sha256=successor.task_request_sha256,
            claim_payload=mismatched_claim,
        )
        with pytest.raises(
            TemperatureFormalExecutionError,
            match="FORMAL_CLAIM_IDENTITY_MISMATCH",
        ):
            executor.run_many_to_durable_terminal((mismatched,), max_workers=1)
        assert submitted == []

        changed_request = successor.task_request.model_copy(
            update={
                "instruction": successor.task_request.instruction
                + "\nsemantic retry drift",
            }
        )
        changed_claim = {
            **successor.claim_payload,
            "task_request_sha256": task_request_digest_v2(changed_request),
            "retry_semantics_sha256": task_request_retry_semantics_sha256_v1(
                changed_request
            ),
        }
        changed = FormalCallEnvelopeV1(
            logical_call_id=successor.logical_call_id,
            call_id=successor.call_id,
            task_request=changed_request,
            task_request_sha256=task_request_digest_v2(changed_request),
            claim_payload=changed_claim,
        )
        with pytest.raises(
            TemperatureFormalExecutionError,
            match="FORMAL_CLAIM_IDENTITY_MISMATCH",
        ):
            executor.run_many_to_durable_terminal((changed,), max_workers=1)
        assert submitted == []

        outcome = executor.run_many_to_durable_terminal(
            (FormalCallEnvelopeV1.from_candidate(successor),), max_workers=1
        )[0]
        assert outcome.state == "completion"
        assert outcome.recovered_after_restart is False
        assert successor.attempt_number == 2
        assert submitted == [successor.task_request.task_id]
        assert [
            event["payload"]["call_id"]
            for event in ledger.events
            if event["kind"] == "CALL_CLAIMED"
        ] == [first.call_id, successor.call_id]


def test_rejected_reflector_completion_cannot_retry_with_changed_prompt(
    tmp_path: Path,
) -> None:
    from benchmarks.chembench.tests.temperature_full_evolve_v1 import (
        test_reflector as reflector_fixtures,
    )

    service_digest = "4" * 64
    client_factory_calls: list[str] = []

    def forbidden_client_factory() -> _Client:
        client_factory_calls.append("called")
        raise AssertionError("semantic drift must be rejected before submit")

    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "reflector-ledger.jsonl").resolve(),
        run_id="formal-run-reflector-semantics-2004",
    ) as ledger:
        sealed = reflector_fixtures._sealed()
        first = prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=reflector_fixtures.RUN_ID,
            service_identity_sha256=service_digest,
        )
        ledger.append("CALL_CLAIMED", first.claim_payload)
        ledger.append(
            "CALL_REJECTED_COMPLETION",
            {
                "logical_call_id": first.logical_call_id,
                "call_id": first.call_id,
                "rejection_code": "REFLECTOR_RESPONSE_PACKET_SEQUENCE_INVALID",
                "response_sha256": "5" * 64,
                "task_result_sha256": "6" * 64,
                "transcript_sha256": "7" * 64,
                "completion_identity_sha256": "8" * 64,
                "durable_completion": True,
                "tool_event_count": 0,
                "tool_policy_validated": True,
            },
        )
        successor = prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=reflector_fixtures.RUN_ID,
            service_identity_sha256=service_digest,
        )
        assert (
            successor.claim_payload["retry_semantics_sha256"]
            == first.claim_payload["retry_semantics_sha256"]
        )
        changed_request = successor.task_request.model_copy(
            update={"instruction": successor.task_request.instruction + "\nsemantic drift"}
        )
        changed_digest = task_request_digest_v2(changed_request)
        changed = FormalCallEnvelopeV1(
            logical_call_id=successor.logical_call_id,
            call_id=successor.call_id,
            task_request=changed_request,
            task_request_sha256=changed_digest,
            claim_payload={
                **successor.claim_payload,
                "task_request_sha256": changed_digest,
                "retry_semantics_sha256": task_request_retry_semantics_sha256_v1(
                    changed_request
                ),
            },
        )
        executor = TemperatureFormalExecutorV1(
            runtime_services=_Runtime(
                repository_root=tmp_path.resolve(),
                service_run_id="runtime-service-004",
                digest=service_digest,
            ),
            ledger=ledger,
            client_factory=forbidden_client_factory,
            audit_function=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("semantic drift must not be audited")
            ),
            no_completion_audit_function=_dual_proof,
            poll_interval_seconds=0,
            max_poll_attempts=1,
        )
        with pytest.raises(
            TemperatureFormalExecutionError,
            match="FORMAL_CLAIM_IDENTITY_MISMATCH",
        ):
            executor.run_many_to_durable_terminal((changed,), max_workers=1)
        assert client_factory_calls == []
        assert ledger.latest_claim(first.logical_call_id) == first.claim_payload
