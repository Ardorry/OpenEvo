"""Durable exactly-once model-call engine shared by Phase A and fold runs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo_chembench.chembench4k_models import (
    PublicChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    write_private_file,
)
from openevo_chembench.temperature_full_evolve_v1.execution import (
    FormalCallEnvelopeV1,
    TemperatureFormalExecutorV1,
)
from openevo_chembench.temperature_safe_evolve_v2.candidate import (
    CandidateContextV2,
    SafeCandidatePlanV2,
    accept_candidate_v2,
    observe_candidate_status_v2,
    prepare_candidate_call_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.ledger import SafeExperimentLedgerV2
from openevo_chembench.temperature_safe_evolve_v2.packet import SealedReflectorPacketV2
from openevo_chembench.temperature_safe_evolve_v2.reflector import (
    SafeReflectorError,
    SafeReflectorPlanV2,
    accept_reflection_v2,
    observe_reflector_status_v2,
    prepare_reflector_call_v2,
    rebuild_accepted_reflection_v2,
    rejected_completion_payload_v2,
    render_reflector_prompt_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    TemperatureRuntimeServicesIdentityV1,
    audit_durable_no_completion_v2,
    audit_persisted_rollout_result_v2,
)


class SafeExecutionError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class AcceptedPhysicalResultV2:
    logical_call_id: str
    call_id: str
    task_uid: str
    task_ordinal: int
    phase: str
    block_id: str
    context_hash: str
    context_binding_sha256: str
    response: str
    response_sha256: str
    task_result_sha256: str
    transcript_sha256: str
    completion_identity_sha256: str

    def __repr__(self) -> str:
        return "AcceptedPhysicalResultV2(<completion-redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class AcceptedReflectionResultV2:
    merged_entries_payload: tuple[dict[str, object], ...]
    projection: str
    synthesis_receipt: dict[str, object]

    def __repr__(self) -> str:
        return "AcceptedReflectionResultV2(<reflection-content-redacted>)"


class SafeFormalExecutionV2:
    def __init__(
        self,
        *,
        runtime: TemperatureRuntimeServicesIdentityV1,
        ledger: SafeExperimentLedgerV2,
        checkpoint_root: Path,
        cooldown_seconds: float,
        candidate_logical_limit: int,
        reflector_logical_limit: int,
    ) -> None:
        if not checkpoint_root.is_absolute():
            raise TypeError("checkpoint root must be absolute")
        checkpoint_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint_root.chmod(0o700)
        self._runtime = runtime
        self._ledger = ledger
        self._checkpoint_root = checkpoint_root
        if candidate_logical_limit < 0 or reflector_logical_limit < 0:
            raise ValueError("logical call limits must be non-negative")
        self._candidate_logical_limit = candidate_logical_limit
        self._reflector_logical_limit = reflector_logical_limit
        self._executor = TemperatureFormalExecutorV1(
            runtime_services=runtime,
            ledger=ledger,
            audit_function=audit_persisted_rollout_result_v2,
            no_completion_audit_function=audit_durable_no_completion_v2,
            post_durable_terminal_cooldown_seconds=cooldown_seconds,
            poll_interval_seconds=1.0,
            max_poll_attempts=2400,
        )

    def run_candidate(
        self,
        *,
        task: PublicChemBench4KTask,
        prompt: RenderedChemBench4KPrompt,
        context: CandidateContextV2,
        context_workspace: Path | None,
        phase: str,
        block_id: str,
        task_ordinal: int,
        run_id: str,
    ) -> AcceptedPhysicalResultV2:
        """Close one unique physical context before returning."""

        logical = _candidate_logical_identity(
            task=task,
            prompt=prompt,
            context=context,
            context_workspace=context_workspace,
            phase=phase,
            block_id=block_id,
            task_ordinal=task_ordinal,
            run_id=run_id,
            service_identity_sha256=self._runtime.digest,
        )
        existing = self._ledger.accepted_call(logical)
        if existing is not None:
            latest = self._ledger.latest_claim(logical)
            if latest is None:
                raise SafeExecutionError("SAFE_EXECUTION_ACCEPTED_CLAIM_MISSING")
            recovered_plan = _candidate_plan_from_envelope(
                self._load_checkpoint(str(latest["call_id"])),
                task=task,
                task_ordinal=task_ordinal,
                phase=phase,
                block_id=block_id,
                context_binding_sha256=context.binding_sha256,
            )
            if recovered_plan.logical_call_id != logical:
                raise SafeExecutionError("SAFE_EXECUTION_LOGICAL_IDENTITY_DRIFT")
            return _accepted_result(
                existing,
                task=task,
                task_ordinal=task_ordinal,
                phase=phase,
                block_id=block_id,
                context_hash=recovered_plan.context_hash,
                context_binding_sha256=context.binding_sha256,
            )
        if self._ledger.accepted_logical_count("safe_candidate") >= self._candidate_logical_limit:
            raise SafeExecutionError("SAFE_CANDIDATE_LOGICAL_BUDGET_EXCEEDED")
        while True:
            latest = self._ledger.latest_claim(logical)
            if latest is not None and not self._ledger.failure_has_no_completion(
                str(latest["call_id"])
            ):
                envelope = self._load_checkpoint(str(latest["call_id"]))
                plan = _candidate_plan_from_envelope(
                    envelope,
                    task=task,
                    task_ordinal=task_ordinal,
                    phase=phase,
                    block_id=block_id,
                    context_binding_sha256=context.binding_sha256,
                )
                if plan.logical_call_id != logical:
                    raise SafeExecutionError("SAFE_EXECUTION_LOGICAL_IDENTITY_DRIFT")
            else:
                plan = prepare_candidate_call_v2(
                    task=task,
                    prompt=prompt,
                    context=context,
                    context_workspace=context_workspace,
                    phase=phase,
                    block_id=block_id,
                    task_ordinal=task_ordinal,
                    ledger=self._ledger,
                    run_id=run_id,
                    service_identity_sha256=self._runtime.digest,
                )
                if plan.logical_call_id != logical:
                    raise SafeExecutionError("SAFE_EXECUTION_LOGICAL_IDENTITY_DRIFT")
                envelope = FormalCallEnvelopeV1(
                    logical_call_id=plan.logical_call_id,
                    call_id=plan.call_id,
                    task_request=plan.task_request,
                    task_request_sha256=plan.task_request_sha256,
                    claim_payload=dict(plan.claim_payload),
                )
                self._write_checkpoint(envelope)
            outcome = self._executor.run_many_to_durable_terminal((envelope,), max_workers=1)[0]
            if outcome.state == "terminal_no_completion":
                continue
            if outcome.status is None:
                raise SafeExecutionError("SAFE_EXECUTION_COMPLETION_BODY_MISSING")
            observed = observe_candidate_status_v2(outcome.status, plan=plan)
            accepted = accept_candidate_v2(plan=plan, observed=observed, ledger=self._ledger)
            self._ledger.append("CALL_ACCEPTED", accepted.call_accepted_payload)
            return _accepted_result(
                accepted.call_accepted_payload,
                task=task,
                task_ordinal=task_ordinal,
                phase=phase,
                block_id=block_id,
                context_hash=plan.context_hash,
                context_binding_sha256=plan.context_binding_sha256,
            )

    def run_reflector(
        self,
        *,
        packet: SealedReflectorPacketV2,
        synthesis_path: Path,
    ) -> AcceptedReflectionResultV2:
        """Close one logical schema-bound synthesis with at most three attempts."""

        logical = _reflector_logical_identity(packet)
        accepted_payload = self._ledger.accepted_call(logical)
        if synthesis_path.exists():
            if accepted_payload is None:
                raise SafeExecutionError("SAFE_REFLECTOR_SYNTHESIS_WITHOUT_ACCEPTANCE")
            latest = self._ledger.latest_claim(logical)
            if latest is None:
                raise SafeExecutionError("SAFE_REFLECTOR_ACCEPTED_CLAIM_MISSING")
            plan = _reflector_plan_from_envelope(
                self._load_checkpoint(str(latest["call_id"])), packet=packet
            )
            try:
                entries, projection, receipt = rebuild_accepted_reflection_v2(
                    packet=packet,
                    plan=plan,
                    accepted_payload=accepted_payload,
                )
            except SafeReflectorError as exc:
                raise SafeExecutionError("SAFE_REFLECTOR_ACCEPTED_RESPONSE_INVALID") from exc
            stored = _load_reflection_result(synthesis_path, packet=packet)
            expected_entries = tuple(entry.model_dump(mode="json") for entry in entries)
            ledger_receipts = [
                event["payload"]
                for event in self._ledger.events
                if event["kind"] == "REFLECTOR_ACCEPTED"
                and event["payload"].get("logical_call_id") == logical
            ]
            if (
                stored.merged_entries_payload != expected_entries
                or stored.projection != projection
                or stored.synthesis_receipt != receipt
                or ledger_receipts != [receipt]
            ):
                raise SafeExecutionError("SAFE_REFLECTOR_SYNTHESIS_RECEIPT_DRIFT")
            return stored
        if accepted_payload is not None:
            latest = self._ledger.latest_claim(logical)
            if latest is None:
                raise SafeExecutionError("SAFE_REFLECTOR_ACCEPTED_CLAIM_MISSING")
            plan = _reflector_plan_from_envelope(
                self._load_checkpoint(str(latest["call_id"])), packet=packet
            )
            try:
                entries, projection, receipt = rebuild_accepted_reflection_v2(
                    packet=packet,
                    plan=plan,
                    accepted_payload=accepted_payload,
                )
            except SafeReflectorError as exc:
                raise SafeExecutionError("SAFE_REFLECTOR_ACCEPTED_RESPONSE_INVALID") from exc
            existing_receipts = [
                event["payload"]
                for event in self._ledger.events
                if event["kind"] == "REFLECTOR_ACCEPTED"
                and event["payload"].get("logical_call_id") == logical
            ]
            if existing_receipts:
                if existing_receipts != [receipt]:
                    raise SafeExecutionError("SAFE_REFLECTOR_SYNTHESIS_RECEIPT_DRIFT")
            else:
                self._ledger.append("REFLECTOR_ACCEPTED", receipt)
            payload = _reflection_checkpoint_payload(
                packet=packet,
                entries=tuple(entry.model_dump(mode="json") for entry in entries),
                projection=projection,
                receipt=receipt,
            )
            _write_exact_private(synthesis_path, canonical_json_bytes(payload))
            return AcceptedReflectionResultV2(
                merged_entries_payload=tuple(payload["merged_entries"]),
                projection=projection,
                synthesis_receipt=receipt,
            )
        if self._ledger.accepted_logical_count("safe_reflector") >= self._reflector_logical_limit:
            raise SafeExecutionError("SAFE_REFLECTOR_LOGICAL_BUDGET_EXCEEDED")
        while True:
            latest = self._ledger.latest_claim(logical)
            if latest is not None and not (
                self._ledger.failure_has_no_completion(str(latest["call_id"]))
                or self._ledger.completion_was_rejected(str(latest["call_id"]))
            ):
                envelope = self._load_checkpoint(str(latest["call_id"]))
                plan = _reflector_plan_from_envelope(envelope, packet=packet)
            else:
                plan = prepare_reflector_call_v2(
                    packet=packet,
                    ledger=self._ledger,
                    service_identity_sha256=self._runtime.digest,
                )
                if plan.logical_call_id != logical:
                    raise SafeExecutionError("SAFE_REFLECTOR_LOGICAL_IDENTITY_DRIFT")
                envelope = FormalCallEnvelopeV1(
                    logical_call_id=plan.logical_call_id,
                    call_id=plan.call_id,
                    task_request=plan.task_request,
                    task_request_sha256=plan.task_request_sha256,
                    claim_payload=dict(plan.claim_payload),
                )
                self._write_checkpoint(envelope)
            outcome = self._executor.run_many_to_durable_terminal((envelope,), max_workers=1)[0]
            if outcome.state == "terminal_no_completion":
                continue
            if outcome.status is None:
                raise SafeExecutionError("SAFE_REFLECTOR_COMPLETION_BODY_MISSING")
            observed, task_result_sha, completion_sha = observe_reflector_status_v2(
                outcome.status, plan=plan
            )
            acceptance_view: Any = self._ledger
            already_accepted = self._ledger.accepted_call(logical) is not None
            if already_accepted:
                acceptance_view = _BeforeAcceptanceLedger(self._ledger, logical)
            try:
                accepted = accept_reflection_v2(
                    packet=packet,
                    plan=plan,
                    observed=observed,
                    task_result_sha256=task_result_sha,
                    completion_identity_sha256=completion_sha,
                    ledger=acceptance_view,
                )
            except SafeReflectorError as exc:
                if already_accepted:
                    raise SafeExecutionError("SAFE_REFLECTOR_ACCEPTED_RESPONSE_INVALID") from exc
                try:
                    rejection = rejected_completion_payload_v2(
                        plan=plan,
                        observed=observed,
                        task_result_sha256=task_result_sha,
                        completion_identity_sha256=completion_sha,
                        rejection=exc,
                        ledger=self._ledger,
                    )
                except SafeReflectorError:
                    raise exc
                self._ledger.append("CALL_REJECTED_COMPLETION", rejection)
                continue
            if not already_accepted:
                self._ledger.append("CALL_ACCEPTED", accepted.call_accepted_payload)
            self._ledger.append("REFLECTOR_ACCEPTED", accepted.synthesis_receipt)
            payload = _reflection_checkpoint_payload(
                packet=packet,
                entries=tuple(entry.model_dump(mode="json") for entry in accepted.merged_entries),
                projection=accepted.projection,
                receipt=accepted.synthesis_receipt,
            )
            _write_exact_private(synthesis_path, canonical_json_bytes(payload))
            return AcceptedReflectionResultV2(
                merged_entries_payload=tuple(payload["merged_entries"]),
                projection=accepted.projection,
                synthesis_receipt=accepted.synthesis_receipt,
            )

    def _checkpoint_path(self, call_id: str) -> Path:
        if not call_id or "/" in call_id or "\\" in call_id:
            raise SafeExecutionError("SAFE_EXECUTION_CALL_ID_INVALID")
        return self._checkpoint_root / f"{call_id}.json"

    def _write_checkpoint(self, envelope: FormalCallEnvelopeV1) -> None:
        path = self._checkpoint_path(envelope.call_id)
        payload = envelope.to_private_checkpoint_bytes()
        if path.exists():
            if _read_private(path) != payload:
                raise SafeExecutionError("SAFE_EXECUTION_CHECKPOINT_DRIFT")
            return
        write_private_file(path, payload, replace=False)

    def _load_checkpoint(self, call_id: str) -> FormalCallEnvelopeV1:
        return FormalCallEnvelopeV1.from_private_checkpoint_bytes(
            _read_private(self._checkpoint_path(call_id))
        )


def _reflection_checkpoint_payload(
    *,
    packet: SealedReflectorPacketV2,
    entries: tuple[dict[str, object], ...],
    projection: str,
    receipt: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "TemperatureSafeAcceptedReflectionCheckpointV2",
        "packet_sha256": packet.packet_sha256,
        "test_isolation_receipt_sha256": packet.test_isolation_receipt_sha256,
        "merged_entries": list(entries),
        "projection": projection,
        "projection_sha256": hashlib.sha256(projection.encode("utf-8")).hexdigest(),
        "synthesis_receipt": receipt,
    }


class _EmptyLedgerView:
    """Build-only view used to derive a physical logical context identity."""

    def accepted_call(self, logical_call_id: str) -> None:
        del logical_call_id

    def latest_claim(self, logical_call_id: str) -> None:
        del logical_call_id

    def failure_has_no_completion(self, call_id: str) -> bool:
        del call_id
        return False

    def completion_was_rejected(self, call_id: str) -> bool:
        del call_id
        return False


class _BeforeAcceptanceLedger:
    def __init__(self, ledger: SafeExperimentLedgerV2, logical_call_id: str) -> None:
        self._ledger = ledger
        self._logical = logical_call_id

    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None:
        if logical_call_id == self._logical:
            return None
        return self._ledger.accepted_call(logical_call_id)

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None:
        return self._ledger.latest_claim(logical_call_id)

    def failure_has_no_completion(self, call_id: str) -> bool:
        return self._ledger.failure_has_no_completion(call_id)

    def completion_was_rejected(self, call_id: str) -> bool:
        return self._ledger.completion_was_rejected(call_id)


def _candidate_logical_identity(**values: Any) -> str:
    plan = prepare_candidate_call_v2(ledger=_EmptyLedgerView(), **values)
    return plan.logical_call_id


def _reflector_logical_identity(packet: SealedReflectorPacketV2) -> str:
    plan = prepare_reflector_call_v2(
        packet=packet,
        ledger=_EmptyLedgerView(),
        service_identity_sha256="0" * 64,
    )
    return plan.logical_call_id


def _candidate_plan_from_envelope(
    envelope: FormalCallEnvelopeV1,
    *,
    task: PublicChemBench4KTask,
    task_ordinal: int,
    phase: str,
    block_id: str,
    context_binding_sha256: str,
) -> SafeCandidatePlanV2:
    metadata = envelope.task_request.metadata.get("openevo_chembench")
    if not isinstance(metadata, dict):
        raise SafeExecutionError("SAFE_EXECUTION_CHECKPOINT_METADATA_INVALID")
    context_hash = metadata.get("physical_context_hash")
    if (
        metadata.get("context_binding_sha256") != context_binding_sha256
        or metadata.get("phase") != phase
        or metadata.get("block_id") != block_id
        or type(context_hash) is not str
    ):
        raise SafeExecutionError("SAFE_EXECUTION_CHECKPOINT_METADATA_INVALID")
    claim = dict(envelope.claim_payload)
    return SafeCandidatePlanV2(
        logical_call_id=envelope.logical_call_id,
        call_id=envelope.call_id,
        attempt_number=int(claim["attempt_number"]),
        task_uid=task.uid,
        task_ordinal=task_ordinal,
        phase=phase,
        block_id=block_id,
        context_hash=context_hash,
        context_binding_sha256=context_binding_sha256,
        task_request=envelope.task_request,
        task_request_sha256=envelope.task_request_sha256,
        claim_payload=claim,
    )


def _accepted_result(
    payload: dict[str, Any],
    *,
    task: PublicChemBench4KTask,
    task_ordinal: int,
    phase: str,
    block_id: str,
    context_hash: str,
    context_binding_sha256: str,
) -> AcceptedPhysicalResultV2:
    response = payload.get("response")
    if type(response) is not str or hashlib.sha256(response.encode()).hexdigest() != payload.get(
        "response_sha256"
    ):
        raise SafeExecutionError("SAFE_EXECUTION_ACCEPTED_PAYLOAD_INVALID")
    return AcceptedPhysicalResultV2(
        logical_call_id=str(payload["logical_call_id"]),
        call_id=str(payload["call_id"]),
        task_uid=task.uid,
        task_ordinal=task_ordinal,
        phase=phase,
        block_id=block_id,
        context_hash=context_hash,
        context_binding_sha256=context_binding_sha256,
        response=response,
        response_sha256=str(payload["response_sha256"]),
        task_result_sha256=str(payload["task_result_sha256"]),
        transcript_sha256=str(payload["transcript_sha256"]),
        completion_identity_sha256=str(payload["completion_identity_sha256"]),
    )


def _reflector_plan_from_envelope(
    envelope: FormalCallEnvelopeV1, *, packet: SealedReflectorPacketV2
) -> SafeReflectorPlanV2:
    prompt = render_reflector_prompt_v2(packet)
    metadata = envelope.task_request.metadata.get("openevo_chembench")
    if (
        not isinstance(metadata, dict)
        or metadata.get("packet_sha256") != packet.packet_sha256
        or metadata.get("prompt_sha256") != prompt.prompt_sha256
        or metadata.get("test_isolation_receipt_sha256") != packet.test_isolation_receipt_sha256
    ):
        raise SafeExecutionError("SAFE_REFLECTOR_CHECKPOINT_METADATA_INVALID")
    claim = dict(envelope.claim_payload)
    return SafeReflectorPlanV2(
        logical_call_id=envelope.logical_call_id,
        call_id=envelope.call_id,
        attempt_number=int(claim["attempt_number"]),
        prompt=prompt,
        task_request=envelope.task_request,
        task_request_sha256=envelope.task_request_sha256,
        claim_payload=claim,
    )


def _load_reflection_result(
    path: Path, *, packet: SealedReflectorPacketV2
) -> AcceptedReflectionResultV2:
    try:
        payload = json.loads(_read_private(path))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SafeExecutionError("SAFE_REFLECTOR_SYNTHESIS_CHECKPOINT_INVALID") from exc
    expected = {
        "schema_version",
        "packet_sha256",
        "test_isolation_receipt_sha256",
        "merged_entries",
        "projection",
        "projection_sha256",
        "synthesis_receipt",
    }
    projection = payload.get("projection") if isinstance(payload, dict) else None
    entries = payload.get("merged_entries") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload["schema_version"] != "TemperatureSafeAcceptedReflectionCheckpointV2"
        or payload["packet_sha256"] != packet.packet_sha256
        or payload["test_isolation_receipt_sha256"] != packet.test_isolation_receipt_sha256
        or type(projection) is not str
        or hashlib.sha256(projection.encode()).hexdigest() != payload["projection_sha256"]
        or type(entries) is not list
        or any(type(value) is not dict for value in entries)
        or canonical_json_bytes(payload) != _read_private(path)
    ):
        raise SafeExecutionError("SAFE_REFLECTOR_SYNTHESIS_CHECKPOINT_INVALID")
    return AcceptedReflectionResultV2(
        merged_entries_payload=tuple(entries),
        projection=projection,
        synthesis_receipt=dict(payload["synthesis_receipt"]),
    )


def _read_private(path: Path, *, maximum: int = 32 * 1024 * 1024) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= maximum
    ):
        raise SafeExecutionError("SAFE_EXECUTION_CHECKPOINT_UNSAFE")
    return path.read_bytes()


def _write_exact_private(path: Path, payload: bytes) -> None:
    if path.exists():
        if _read_private(path, maximum=len(payload) + 1) != payload:
            raise SafeExecutionError("SAFE_EXECUTION_CHECKPOINT_DRIFT")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    write_private_file(path, payload, replace=False)


__all__ = [
    "AcceptedPhysicalResultV2",
    "AcceptedReflectionResultV2",
    "SafeExecutionError",
    "SafeFormalExecutionV2",
]
