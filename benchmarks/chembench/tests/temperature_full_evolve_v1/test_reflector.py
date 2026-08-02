from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from openevo.rollout.models import SessionResult, SessionStatus, TaskStatus
from openevo.trajectory.models import Trace, Trajectory
from pydantic import ValidationError

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.supervised_transfer_v2.executor import task_request_digest_v2
from openevo_chembench.temperature_full_evolve_v1.evidence import RuleEvidenceIndexV1
from openevo_chembench.temperature_full_evolve_v1.execution import (
    DurableFormalOutcomeV1,
    FormalCallEnvelopeV1,
    finalize_reflector_outcome_v1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)
from openevo_chembench.temperature_full_evolve_v1.packet import (
    BATCH_SIZE,
    BatchSupervisedPacketV1,
    BatchTrainCaseV1,
    ReflectorArtifactSnapshotV1,
    TemperatureBatchPacketError,
    build_batch_supervised_packet_v1,
    seal_batch_supervised_packet_v1,
)
from openevo_chembench.temperature_full_evolve_v1.reflector import (
    MODEL,
    PROMPT_MAX_UTF8_BYTES,
    PROMPT_SCHEMA_SHA256,
    REASONING_EFFORT,
    RESPONSE_MAX_UTF8_BYTES,
    RESPONSE_SCHEMA_SHA256,
    VISIBLE_PACKET_SCHEMA_SHA256,
    ObservedReflectorCompletionV1,
    TemperatureReflectorError,
    accept_reflector_synthesis_v1,
    build_reflector_rejected_completion_payload_v1,
    is_retryable_reflector_completion_rejection_v1,
    observe_reflector_task_status_v1,
    prepare_reflector_call_v1,
    recover_accepted_reflector_synthesis_v1,
    render_reflector_prompt_v1,
    validate_reflector_transcripts_v1,
)
from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    PersistedRolloutResultAuditV1,
)


def _uid(scope: str, index: int) -> str:
    return hashlib.sha256(f"{scope}-{index}".encode()).hexdigest()


TRAIN_UIDS = frozenset(_uid("train", index) for index in range(100))
TEST_UIDS = frozenset(_uid("test", index) for index in range(100))
SERVICE_IDENTITY = hashlib.sha256(b"runtime-services").hexdigest()
RUN_ID = "stv3-temperature-full-evolve-v1-20260802T120000Z"


def _case(
    ordinal: int,
    *,
    question: str | None = None,
    uid: str | None = None,
) -> BatchTrainCaseV1:
    labels = ("A", "B", "C", "D")
    target = labels[ordinal % 4]
    options = ("-78 degC", "0 degC", "25 degC", "80 degC")
    completion = target
    return BatchTrainCaseV1(
        uid=uid or _uid("train", ordinal),
        batch_position=ordinal % BATCH_SIZE + 1,
        question=question
        or f"Estimate the reaction temperature for private Train item {ordinal}.",
        options=options,
        ground_truth=target,
        correct_option=options[labels.index(target)],
        candidate_completion=completion,
        official_prediction=target,
        official_parse_status="parsed",
        strict_prediction=target,
        strict_parse_status="parsed",
        correct=True,
        train_ordinal=ordinal,
        task_request_sha256=hashlib.sha256(f"request-{ordinal}".encode()).hexdigest(),
        accepted_completion_sha256=hashlib.sha256(completion.encode()).hexdigest(),
        transcript_sha256=hashlib.sha256(f"transcript-{ordinal}".encode()).hexdigest(),
    )


def _c0_artifacts() -> tuple[ReflectorArtifactSnapshotV1, ...]:
    return tuple(
        ReflectorArtifactSnapshotV1.generation_zero(target)  # type: ignore[arg-type]
        for target in ("text_memory", "skill_bundle", "agent_system")
    )


def _packet(
    *,
    cases: tuple[BatchTrainCaseV1, ...] | None = None,
) -> BatchSupervisedPacketV1:
    selected = cases or tuple(_case(index) for index in range(BATCH_SIZE))
    return build_batch_supervised_packet_v1(
        run_id=RUN_ID,
        source_commit="a" * 40,
        split_sha256="b" * 64,
        config_sha256="c" * 64,
        dataset_sha256="d" * 64,
        batch_index=1,
        current_train_cases=selected,
        current_artifacts=_c0_artifacts(),
        prior_evidence=RuleEvidenceIndexV1.generation_zero(),
        prior_batch_diagnostics=(),
        all_train_uids=TRAIN_UIDS,
        seen_train_uids=frozenset(_uid("train", index) for index in range(25)),
    )


def _sealed():
    return seal_batch_supervised_packet_v1(
        _packet(),
        all_train_uids=TRAIN_UIDS,
        seen_train_uids=frozenset(_uid("train", index) for index in range(25)),
        test_uids=TEST_UIDS,
    )


def _safe_transcript(response: str) -> str:
    return "\n".join(
        (
            json.dumps({"type": "thread.started"}, separators=(",", ":")),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": response},
                },
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                    },
                },
                separators=(",", ":"),
            ),
        )
    )


def _reflector_status(response: str, *, task_id: str) -> TaskStatus:
    return TaskStatus(
        task_id=task_id,
        status="completed",
        total_sessions=1,
        completed_sessions=1,
        results=[
            SessionResult(
                session_id="sk-openevo-reflector-rejected-session",
                task_id=task_id,
                status=SessionStatus.COMPLETED,
                trajectory=Trajectory(
                    status="COMPLETED",
                    metadata={"capture_mode": "transcript"},
                    traces=[
                        Trace(
                            response_messages=[
                                {"role": "assistant", "content": response}
                            ],
                            metadata={
                                "capture_mode": "transcript",
                                "transcript": _safe_transcript(response),
                            },
                        )
                    ],
                ),
            )
        ],
    )


def _response(*, support_uid: str | None = None, batch_index: int = 1) -> str:
    proposals: list[dict[str, object]] = []
    if support_uid is not None:
        proposals.append(
            {
                "operation": "add",
                "rule_id": None,
                "kind": "category_knowledge",
                "content": "Use convergent reaction-condition evidence to infer a temperature regime.",
                "applicability": "When multiple independent thermal signals agree.",
                "validation": "Check units, scale, and contradictory constraints.",
                "supporting_train_uids": [support_uid],
                "counterexample_train_uids": [],
                "confidence": 0.75,
            }
        )
    return json.dumps(
        {
            "schema_version": "TemperatureBatchReflectionV1",
            "batch_index": batch_index,
            "prior_evidence_sha256": None,
            "proposals": proposals,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _observed(response: str) -> ObservedReflectorCompletionV1:
    return ObservedReflectorCompletionV1(
        response=response,
        response_sha256=hashlib.sha256(response.encode()).hexdigest(),
        transcript=validate_reflector_transcripts_v1((_safe_transcript(response),)),
    )


class _Ledger:
    def __init__(self) -> None:
        self.claims: dict[str, list[dict[str, Any]]] = {}
        self.accepted: dict[str, dict[str, Any]] = {}
        self.no_completion: set[str] = set()
        self.rejected_completion: set[str] = set()

    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None:
        value = self.accepted.get(logical_call_id)
        return None if value is None else dict(value)

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None:
        values = self.claims.get(logical_call_id, [])
        return None if not values else dict(values[-1])

    def failure_has_no_completion(self, call_id: str) -> bool:
        return call_id in self.no_completion

    def completion_was_rejected(self, call_id: str) -> bool:
        return call_id in self.rejected_completion

    def add_claim(self, payload: dict[str, object]) -> None:
        self.claims.setdefault(str(payload["logical_call_id"]), []).append(dict(payload))


def test_packet_is_exactly_25_train_items_and_prompt_is_test_blind_and_bounded() -> None:
    sealed = _sealed()
    packet = sealed.packet
    visible = packet.to_reflector_packet()
    prompt = render_reflector_prompt_v1(sealed)

    assert len(packet.current_train_cases) == 25
    assert len(visible.current_train_cases) == 25
    assert packet.current_artifacts == _c0_artifacts()
    assert visible.prior_evidence == RuleEvidenceIndexV1.generation_zero()
    assert prompt.prompt_schema_sha256 == PROMPT_SCHEMA_SHA256
    assert prompt.visible_packet_schema_sha256 == VISIBLE_PACKET_SCHEMA_SHA256
    assert prompt.response_schema_sha256 == RESPONSE_SCHEMA_SHA256
    assert prompt.prompt_utf8_byte_count <= PROMPT_MAX_UTF8_BYTES
    assert prompt.response_max_utf8_bytes == RESPONSE_MAX_UTF8_BYTES
    assert visible.digest in prompt.instruction
    assert packet.current_train_cases[0].uid in prompt.instruction
    assert packet.current_train_cases[0].ground_truth in prompt.instruction
    assert not any(uid in prompt.instruction for uid in TEST_UIDS)
    assert "Do not use shell, files, network tools, web search" in prompt.instruction
    assert "MCP" in prompt.instruction
    assert "exactly one JSON object" in prompt.instruction
    assert packet.current_train_cases[0].question not in repr(packet)
    assert packet.current_train_cases[0].uid not in repr(sealed)


def test_packet_rejects_wrong_batch_size_and_aggregate_utf8_overflow() -> None:
    cases = tuple(_case(index) for index in range(24))
    with pytest.raises(TemperatureBatchPacketError, match="BATCH_OR_EVIDENCE_UID_SCOPE_INVALID"):
        _packet(cases=cases)

    large_cases = tuple(
        _case(index, question=("reaction condition " * 1400) + str(index)) for index in range(25)
    )
    with pytest.raises(ValidationError, match="packet exceeds its UTF-8 budget"):
        _packet(cases=large_cases)


def test_case_rejects_answer_or_completion_digest_inconsistency() -> None:
    payload = _case(0).model_dump(mode="python")
    payload["correct"] = False
    with pytest.raises(ValidationError, match="inconsistent"):
        BatchTrainCaseV1.model_validate(payload)

    payload = _case(0).model_dump(mode="python")
    payload["accepted_completion_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="completion digest"):
        BatchTrainCaseV1.model_validate(payload)

    payload = _case(0).model_dump(mode="python")
    payload.update(
        {
            "official_prediction": "E",
            "official_parse_status": "parsed",
            "correct": False,
        }
    )
    with pytest.raises(ValidationError, match="inconsistent"):
        BatchTrainCaseV1.model_validate(payload)


def test_test_uid_overlap_is_explicitly_rejected_and_never_serialized() -> None:
    packet = _packet()
    overlapping_test = frozenset({*list(TEST_UIDS)[1:], _uid("train", 0)})
    assert len(overlapping_test) == 100
    with pytest.raises(TemperatureBatchPacketError, match="TRAIN_TEST_UID_ISOLATION_VIOLATION"):
        seal_batch_supervised_packet_v1(
            packet,
            all_train_uids=TRAIN_UIDS,
            seen_train_uids=frozenset(_uid("train", index) for index in range(25)),
            test_uids=overlapping_test,
        )


def test_task_request_uses_only_formal_managed_rollout_harness_surface() -> None:
    sealed = _sealed()
    ledger = _Ledger()
    plan = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    request = plan.task_request

    assert request.task_id.startswith("chembench-temperature-reflector-")
    assert request.num_samples == 1
    assert request.timeout_seconds == 1800
    assert request.agent.harness == "codex"
    assert request.agent.model_name == MODEL
    assert request.agent.settings == {
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "reasoning_effort": REASONING_EFFORT,
        "tool_policy": "disabled",
    }
    assert request.agent.mcp_servers == []
    assert request.agent.custom_shell is None
    assert request.agent.skills_path is None
    assert request.runtime is not None
    assert request.runtime.profile == "managed_science"
    assert request.runtime.backend == "docker"
    assert request.runtime.allow_internet is True
    metadata = request.metadata["openevo_chembench"]
    assert metadata["model_visible_tools_enabled"] is False
    assert metadata["provider_transport_network_enabled"] is True
    assert len(request.runtime.prepare) == 1
    assert request.runtime.prepare[0].type == "exec"
    assert request.evaluator is None
    assert request.callback_url is None
    assert plan.task_request_sha256 == task_request_digest_v2(request)
    assert plan.claim_payload["task_request_sha256"] == plan.task_request_sha256
    serialized = json.dumps(request.model_dump(mode="json"), sort_keys=True)
    assert not any(uid in serialized for uid in TEST_UIDS)
    assert "codex_bin" not in serialized
    assert "api_key" not in serialized.casefold()


def test_transcript_validator_accepts_agent_message_and_rejects_shell_web_and_mcp() -> None:
    response = _response()
    receipt = validate_reflector_transcripts_v1((_safe_transcript(response),))
    assert receipt.tool_event_count == 0
    assert dict(receipt.usage)["output_tokens"] == 20

    for unsafe_type in ("command_execution", "web_search", "mcp_tool_call"):
        unsafe = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": unsafe_type, "text": "forbidden"},
            }
        )
        with pytest.raises(
            TemperatureReflectorError,
            match="REFLECTOR_TRANSCRIPT_TOOL_OR_SCHEMA_VIOLATION",
        ):
            validate_reflector_transcripts_v1((unsafe,))


def test_official_terminal_status_observer_reuses_rollout_transcript_boundary() -> None:
    response = _response()
    transcript = _safe_transcript(response)
    task_id = "chembench-temperature-reflector-terminal-observation"
    status = TaskStatus(
        task_id=task_id,
        status="completed",
        total_sessions=1,
        completed_sessions=1,
        results=[
            SessionResult(
                session_id="sk-openevo-reflector-session",
                task_id=task_id,
                status=SessionStatus.COMPLETED,
                trajectory=Trajectory(
                    status="COMPLETED",
                    metadata={"capture_mode": "transcript"},
                    traces=[
                        Trace(
                            response_messages=[{"role": "assistant", "content": response}],
                            metadata={
                                "capture_mode": "transcript",
                                "transcript": transcript,
                            },
                        )
                    ],
                ),
            )
        ],
    )
    observed = observe_reflector_task_status_v1(status, expected_task_id=task_id)
    assert observed.response == response
    assert observed.transcript.tool_event_count == 0


def test_exactly_once_acceptance_returns_ledger_payload_and_recovery_never_recalls() -> None:
    sealed = _sealed()
    ledger = _Ledger()
    plan = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    ledger.add_claim(plan.claim_payload)
    response = _response(support_uid=_uid("train", 0))
    transcript = validate_reflector_transcripts_v1((_safe_transcript(response),))
    observed = ObservedReflectorCompletionV1(
        response=response,
        response_sha256=hashlib.sha256(response.encode()).hexdigest(),
        transcript=transcript,
    )

    accepted = accept_reflector_synthesis_v1(
        sealed=sealed,
        plan=plan,
        observed=observed,
        ledger=ledger,
        task_result_sha256="c" * 64,
        completion_identity_sha256="e" * 64,
    )
    assert accepted.next_evidence.batch_index == 1
    assert accepted.next_evidence.rules[0].status == "provisional"
    assert accepted.call_accepted_payload["logical_call_id"] == plan.logical_call_id
    assert accepted.reflector_accepted_payload["accepted_synthesis_count_for_batch"] == 1
    assert accepted.call_accepted_payload["tool_event_count"] == 0
    assert set(accepted.call_accepted_payload) == {
        "logical_call_id",
        "call_id",
        "response",
        "response_sha256",
        "task_result_sha256",
        "transcript_sha256",
        "completion_identity_sha256",
        "tool_event_count",
        "tool_policy_validated",
    }
    assert accepted.reflector_accepted_payload["packet_sha256"] == sealed.packet.digest

    ledger.accepted[plan.logical_call_id] = dict(accepted.call_accepted_payload)
    with pytest.raises(
        TemperatureReflectorError,
        match="REFLECTOR_LOGICAL_SYNTHESIS_ALREADY_ACCEPTED",
    ):
        prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )
    recovered = recover_accepted_reflector_synthesis_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
    )
    assert recovered.recovered_from_ledger is True
    assert recovered.response_sha256 == accepted.response_sha256
    assert recovered.next_evidence.digest == accepted.next_evidence.digest


def test_accepted_payloads_append_to_strict_ledger(tmp_path: Path) -> None:
    sealed = _sealed()
    ledger_path = (tmp_path / "private" / "ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(
        path=ledger_path,
        run_id="formal-reflector-run-0001",
    ) as ledger:
        plan = prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )
        ledger.append("CALL_CLAIMED", plan.claim_payload)
        response = _response(support_uid=_uid("train", 0))
        observed = ObservedReflectorCompletionV1(
            response=response,
            response_sha256=hashlib.sha256(response.encode()).hexdigest(),
            transcript=validate_reflector_transcripts_v1((_safe_transcript(response),)),
        )
        accepted = accept_reflector_synthesis_v1(
            sealed=sealed,
            plan=plan,
            observed=observed,
            ledger=ledger,
            task_result_sha256="c" * 64,
            completion_identity_sha256="e" * 64,
        )
        ledger.append("CALL_ACCEPTED", accepted.call_accepted_payload)
        assert accepted.reflector_accepted_payload["accepted_synthesis_count_for_batch"] == 1
        recovered = recover_accepted_reflector_synthesis_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=RUN_ID,
        )
        assert recovered.next_evidence.digest == accepted.next_evidence.digest


def test_retry_requires_prior_no_completion_and_keeps_one_logical_call_id() -> None:
    sealed = _sealed()
    ledger = _Ledger()
    first = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    ledger.add_claim(first.claim_payload)
    with pytest.raises(
        TemperatureReflectorError,
        match="REFLECTOR_CALL_OWNERSHIP_UNRESOLVED",
    ):
        prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )
    ledger.no_completion.add(first.call_id)
    second = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    assert second.attempt_number == 2
    assert second.logical_call_id == first.logical_call_id
    assert second.call_id != first.call_id
    assert second.task_request_sha256 != first.task_request_sha256
    assert (
        second.claim_payload["retry_semantics_sha256"]
        == first.claim_payload["retry_semantics_sha256"]
    )


@pytest.mark.parametrize(
    ("response", "expected_code"),
    (
        (_response(batch_index=2), "REFLECTOR_RESPONSE_PACKET_SEQUENCE_INVALID"),
        (
            '{"schema_version":"TemperatureBatchReflectionV1","batch_index":1,'
            + '"batch_index":1,"prior_evidence_sha256":null,"proposals":[]}',
            "REFLECTOR_RESPONSE_SCHEMA_OR_EVIDENCE_INVALID",
        ),
        (
            _response(support_uid=next(iter(TEST_UIDS))),
            "REFLECTOR_RESPONSE_SCHEMA_OR_EVIDENCE_INVALID",
        ),
    ),
)
def test_only_allowlisted_durable_response_rejections_create_redacted_retry_proof(
    response: str,
    expected_code: str,
) -> None:
    sealed = _sealed()
    ledger = _Ledger()
    plan = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    ledger.add_claim(plan.claim_payload)
    observed = _observed(response)
    with pytest.raises(TemperatureReflectorError) as captured:
        accept_reflector_synthesis_v1(
            sealed=sealed,
            plan=plan,
            observed=observed,
            ledger=ledger,
            task_result_sha256="c" * 64,
            completion_identity_sha256="e" * 64,
        )
    assert captured.value.finding_code == expected_code
    assert is_retryable_reflector_completion_rejection_v1(captured.value)
    payload = build_reflector_rejected_completion_payload_v1(
        plan=plan,
        observed=observed,
        ledger=ledger,
        task_result_sha256="c" * 64,
        completion_identity_sha256="e" * 64,
        rejection=captured.value,
    )
    assert payload["rejection_code"] == expected_code
    assert "response" not in payload
    assert response not in json.dumps(payload, sort_keys=True)
    assert payload["durable_completion"] is True
    ledger.rejected_completion.add(plan.call_id)
    retry = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    assert retry.logical_call_id == plan.logical_call_id
    assert retry.attempt_number == 2


def test_non_response_validation_error_cannot_authorize_rejected_completion_retry() -> None:
    sealed = _sealed()
    ledger = _Ledger()
    plan = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    ledger.add_claim(plan.claim_payload)
    response = _response()
    with pytest.raises(
        TemperatureReflectorError,
        match="REFLECTOR_REJECTED_COMPLETION_NOT_ALLOWLISTED",
    ):
        build_reflector_rejected_completion_payload_v1(
            plan=plan,
            observed=_observed(response),
            ledger=ledger,
            task_result_sha256="c" * 64,
            completion_identity_sha256="e" * 64,
            rejection=TemperatureReflectorError("REFLECTOR_TRANSCRIPT_INVALID"),
        )


def test_formal_finalizer_records_durable_schema_rejection_without_accepting_content(
    tmp_path: Path,
) -> None:
    sealed = _sealed()
    path = (tmp_path / "private/finalizer-ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(
        path=path,
        run_id="formal-reflector-finalizer-rejected-0001",
    ) as ledger:
        plan = prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )
        ledger.append("CALL_CLAIMED", plan.claim_payload)
        response = _response(batch_index=2)
        status = _reflector_status(response, task_id=plan.task_request.task_id)
        session = status.results[0]
        outcome = DurableFormalOutcomeV1(
            envelope=FormalCallEnvelopeV1.from_reflector(plan),
            state="completion",
            status=status,
            audit=PersistedRolloutResultAuditV1(
                state="PROVEN_COMPLETE",
                service_run_id="runtime-service-reflector-rejected-001",
                task_id_sha256=hashlib.sha256(
                    plan.task_request.task_id.encode()
                ).hexdigest(),
                session_id_sha256=hashlib.sha256(
                    session.session_id.encode()
                ).hexdigest(),
                result_sha256="a" * 64,
                result_size_bytes=100,
                terminal_status="COMPLETED",
                completion_exists=True,
                completion_sha256=hashlib.sha256(response.encode()).hexdigest(),
                finding_code=None,
                result=session,
            ),
            recovered_after_restart=False,
        )
        with pytest.raises(
            TemperatureReflectorError,
            match="REFLECTOR_RESPONSE_PACKET_SEQUENCE_INVALID",
        ):
            finalize_reflector_outcome_v1(
                outcome=outcome,
                plan=plan,
                sealed=sealed,
                ledger=ledger,
            )
        assert ledger.accepted_call(plan.logical_call_id) is None
        assert ledger.completion_was_rejected(plan.call_id)
        event = ledger.events[-1]
        assert event["kind"] == "CALL_REJECTED_COMPLETION"
        assert "response" not in event["payload"]
        assert event["payload"]["response_sha256"] == hashlib.sha256(
            response.encode()
        ).hexdigest()
        assert response not in path.read_text(encoding="utf-8")


def test_rejected_completion_retry_is_restart_safe_bounded_and_acceptance_terminal(
    tmp_path: Path,
) -> None:
    sealed = _sealed()
    path = (tmp_path / "private/events.jsonl").resolve()
    ledger_run_id = "formal-reflector-rejected-run-0001"
    response = _response(batch_index=2)
    with TemperatureExperimentLedgerV1(path=path, run_id=ledger_run_id) as ledger:
        first = prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )
        ledger.append("CALL_CLAIMED", first.claim_payload)
        observed = _observed(response)
        with pytest.raises(TemperatureReflectorError) as captured:
            accept_reflector_synthesis_v1(
                sealed=sealed,
                plan=first,
                observed=observed,
                ledger=ledger,
                task_result_sha256="c" * 64,
                completion_identity_sha256="e" * 64,
            )
        rejected = build_reflector_rejected_completion_payload_v1(
            plan=first,
            observed=observed,
            ledger=ledger,
            task_result_sha256="c" * 64,
            completion_identity_sha256="e" * 64,
            rejection=captured.value,
        )
        ledger.append("CALL_REJECTED_COMPLETION", rejected)

    raw = path.read_text(encoding="utf-8")
    assert response not in raw
    assert "proposals" not in raw
    with TemperatureExperimentLedgerV1(path=path, run_id=ledger_run_id) as ledger:
        second = prepare_reflector_call_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )
        assert second.attempt_number == 2
        ledger.append("CALL_CLAIMED", second.claim_payload)
        second_response = _response()
        accepted = accept_reflector_synthesis_v1(
            sealed=sealed,
            plan=second,
            observed=_observed(second_response),
            ledger=ledger,
            task_result_sha256="a" * 64,
            completion_identity_sha256="b" * 64,
        )
        ledger.append("CALL_ACCEPTED", accepted.call_accepted_payload)
        with pytest.raises(
            TemperatureReflectorError,
            match="REFLECTOR_LOGICAL_SYNTHESIS_ALREADY_ACCEPTED",
        ):
            prepare_reflector_call_v1(
                sealed=sealed,
                ledger=ledger,
                run_id=RUN_ID,
                service_identity_sha256=SERVICE_IDENTITY,
            )

    bounded = _Ledger()
    for attempt in range(1, 4):
        plan = prepare_reflector_call_v1(
            sealed=sealed,
            ledger=bounded,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )
        assert plan.attempt_number == attempt
        bounded.add_claim(plan.claim_payload)
        bounded.rejected_completion.add(plan.call_id)
    with pytest.raises(
        TemperatureReflectorError,
        match="REFLECTOR_ATTEMPT_RETRY_EXHAUSTED",
    ):
        prepare_reflector_call_v1(
            sealed=sealed,
            ledger=bounded,
            run_id=RUN_ID,
            service_identity_sha256=SERVICE_IDENTITY,
        )


def test_response_with_test_uid_wrong_batch_or_duplicate_keys_is_never_accepted() -> None:
    sealed = _sealed()
    ledger = _Ledger()
    plan = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=ledger,
        run_id=RUN_ID,
        service_identity_sha256=SERVICE_IDENTITY,
    )
    ledger.add_claim(plan.claim_payload)

    for response in (
        _response(support_uid=next(iter(TEST_UIDS))),
        _response(batch_index=2),
        (
            '{"schema_version":"TemperatureBatchReflectionV1","batch_index":1,'
            '"batch_index":1,"prior_evidence_sha256":null,"proposals":[]}'
        ),
    ):
        observed = ObservedReflectorCompletionV1(
            response=response,
            response_sha256=hashlib.sha256(response.encode()).hexdigest(),
            transcript=validate_reflector_transcripts_v1((_safe_transcript(response),)),
        )
        with pytest.raises(TemperatureReflectorError):
            accept_reflector_synthesis_v1(
                sealed=sealed,
                plan=plan,
                observed=observed,
                ledger=ledger,
                task_result_sha256="c" * 64,
                completion_identity_sha256="e" * 64,
            )


def test_prompt_and_response_schema_digests_are_canonical() -> None:
    sealed = _sealed()
    prompt = render_reflector_prompt_v1(sealed)
    assert len(prompt.prompt_schema_sha256) == 64
    assert len(prompt.response_schema_sha256) == 64
    assert len(prompt.visible_packet_schema_sha256) == 64
    assert prompt.prompt_sha256 == hashlib.sha256(prompt.instruction.encode()).hexdigest()
    receipt = prompt.to_receipt()
    assert receipt["prompt_utf8_byte_count"] == len(prompt.instruction.encode())
    assert receipt["response_max_utf8_bytes"] == 65536
    assert canonical_json_bytes(receipt).endswith(b"\n")
