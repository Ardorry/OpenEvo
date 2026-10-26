from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .hashing import canonical_sha256
from .ledger import AmbiguousPhaseClaimError, PhaseLedger

_MAX_VERIFIED_NO_EFFECT_ATTEMPTS = 3
_REPLACEMENT_ACTIVATION_TOKEN_SHA256 = "replacement_activation_token_sha256"

_EVOLVED_NO_EFFECT_PREDICATE_KEYS = {
    "core_status_error",
    "exact_setup_canary_validation_error",
    "trajectory_has_zero_records",
    "trajectory_has_zero_traces",
    "workspace_result_absent",
    "core_route_bound",
    "host_codex_exec_forbidden",
    "context_injected_before_setup",
    "exact_three_context_artifact_ids",
    "context_identity_present",
    "runtime_injection_receipt_not_published",
    "credential_contract_present",
    "credential_readiness_receipt_not_published",
    "revision_authority_exact",
    "session_identity_present",
}
_PRESERVED_PRE_G2_PHASES = [
    "baseline_candidate",
    "baseline_internal_evaluator",
    "reflector_memory",
    "reflector_skill_bundle",
    "reflector_agent_system",
]


@dataclass(frozen=True, slots=True)
class ReplacementExpectation:
    attempt_ordinal: int
    receipt_sha256: str
    replacement_run_id: str | None


@dataclass(frozen=True, slots=True)
class ReplacementActivation:
    owner_token: str
    expectation: ReplacementExpectation


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_regular_json(path: Path) -> dict[str, Any]:
    try:
        entry = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"phase has no durable claim: {path.stem}") from exc
    if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode) or entry.st_nlink != 1:
        raise ValueError(f"phase claim is not a link-count-one regular file: {path.stem}")
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise TypeError(f"phase claim is not a JSON object: {path.stem}")
    return payload


def _entry_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


class _EvolvedNoEffectPredicates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    core_status_error: Literal[True]
    exact_setup_canary_validation_error: Literal[True]
    trajectory_has_zero_records: Literal[True]
    trajectory_has_zero_traces: Literal[True]
    workspace_result_absent: Literal[True]
    core_route_bound: Literal[True]
    host_codex_exec_forbidden: Literal[True]
    context_injected_before_setup: Literal[True]
    exact_three_context_artifact_ids: Literal[True]
    context_identity_present: Literal[True]
    runtime_injection_receipt_not_published: Literal[True]
    credential_contract_present: Literal[True]
    credential_readiness_receipt_not_published: Literal[True]
    revision_authority_exact: Literal[True]
    session_identity_present: Literal[True]


class _RecoveredArtifactMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text_memory: str = Field(min_length=1)
    skill_bundle: str = Field(min_length=1)
    agent_system: str = Field(min_length=1)


class _FailedInjectionAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evolved_claim_authority_sha256: str
    context_id: str = Field(min_length=1)
    context_artifact_ids: list[str] = Field(min_length=3, max_length=3)
    context_injected: Literal[True]
    revision_id: str
    runtime_injection_receipt_published: Literal[False]
    credential_readiness_receipt_published: Literal[False]


class EvolvedCandidateNoEffectReceipt(BaseModel):
    """Closed ledger authority for one proven setup-only G2 failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["chemcrow_evolved_candidate_no_effect_recovery_v1"]
    status: Literal["VERIFIED_NO_EVOLVED_CANDIDATE_EFFECT_REPLACEMENT_READY"]
    pair_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_authority_sha256: str
    phase: Literal["evolved_candidate"]
    attempt_ordinal: int = Field(ge=1)
    authority_sha256: str
    original_claim_sha256: str
    config_sha256: str
    checkpoint_sha256: str
    active_attempt_run_id: str
    replacement_run_id: str
    core_task_id: str
    core_session_id_sha256: str
    core_completion_sha256: str
    core_completion_status: Literal["ERROR"]
    error_category: Literal["credential_isolation_validation_failed"]
    input_evidence_sha256: str
    artifact_ids_by_type: _RecoveredArtifactMapping
    reflector_job_ids: list[str] = Field(min_length=3, max_length=3)
    reflector_run_ids: list[str] = Field(min_length=3, max_length=3)
    reflector_prompt_hashes: list[str] = Field(min_length=3, max_length=3)
    injection_authority: _FailedInjectionAuthority
    no_effect_predicates: _EvolvedNoEffectPredicates
    infrastructure_canary_model_call_may_have_occurred: Literal[True]
    evolved_candidate_model_call_proven_absent: Literal[True]
    prior_scientific_calls_preserved: list[str]
    prior_scientific_calls_redispatched: Literal[False]
    replacement_scope: list[Literal["evolved_candidate"]]
    duplicate_scientific_call: Literal[False]
    recorded_before_replacement_dispatch: Literal[True]

    @model_validator(mode="after")
    def _validate_closed_authority(self) -> EvolvedCandidateNoEffectReceipt:
        hash_fields = (
            self.task_authority_sha256,
            self.authority_sha256,
            self.original_claim_sha256,
            self.config_sha256,
            self.checkpoint_sha256,
            self.core_session_id_sha256,
            self.core_completion_sha256,
            self.input_evidence_sha256,
            self.injection_authority.evolved_claim_authority_sha256,
            *self.reflector_prompt_hashes,
        )
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hash_fields):
            raise ValueError("evolved no-effect receipt contains an invalid SHA256")
        expected_run_prefix = re.escape(self.pair_id) + r"-evolved-[0-9a-f]{10}"
        if (
            re.fullmatch(expected_run_prefix, self.core_task_id) is None
            or self.active_attempt_run_id != self.core_task_id
            or re.fullmatch(expected_run_prefix, self.replacement_run_id) is None
            or self.replacement_run_id == self.core_task_id
        ):
            raise ValueError("evolved no-effect attempt run authority is invalid")
        artifact_ids = [
            self.artifact_ids_by_type.text_memory,
            self.artifact_ids_by_type.skill_bundle,
            self.artifact_ids_by_type.agent_system,
        ]
        if (
            len(set(artifact_ids)) != 3
            or self.injection_authority.context_artifact_ids != artifact_ids
            or self.injection_authority.evolved_claim_authority_sha256 != self.authority_sha256
            or self.injection_authority.revision_id
            != "chemcrow-task-local-three:" + canonical_sha256(artifact_ids)
            or len(set(self.reflector_job_ids)) != 3
            or len(set(self.reflector_run_ids)) != 3
            or len(set(self.reflector_prompt_hashes)) != 3
            or self.prior_scientific_calls_preserved != _PRESERVED_PRE_G2_PHASES
            or self.replacement_scope != ["evolved_candidate"]
            or set(self.no_effect_predicates.model_fields_set) != _EVOLVED_NO_EFFECT_PREDICATE_KEYS
        ):
            raise ValueError("evolved no-effect receipt scientific authority is invalid")
        return self


def validate_evolved_candidate_no_effect_receipt(
    receipt: dict[str, Any],
    *,
    pair_id: str | None = None,
) -> dict[str, Any]:
    parsed = EvolvedCandidateNoEffectReceipt.model_validate(receipt)
    canonical = parsed.model_dump(mode="json")
    if canonical != receipt or (pair_id is not None and parsed.pair_id != pair_id):
        raise ValueError("evolved no-effect receipt is not canonical")
    return canonical


def _replacement_expectation_from_payload(
    payload: dict[str, Any],
) -> ReplacementExpectation:
    attempts = payload.get("failed_attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        raise ValueError("replacement expectation attempt history is invalid")
    latest = attempts[-1]
    receipt = latest.get("receipt")
    receipt_sha256 = latest.get("receipt_sha256")
    if (
        not isinstance(receipt, dict)
        or not isinstance(receipt_sha256, str)
        or receipt_sha256 != canonical_sha256(receipt)
    ):
        raise ValueError("replacement expectation receipt authority differs")
    replacement_run_id: str | None = None
    if receipt.get("schema_version") == "chemcrow_evolved_candidate_no_effect_recovery_v1":
        validated = validate_evolved_candidate_no_effect_receipt(
            receipt,
            pair_id=str(payload.get("pair_id") or receipt.get("pair_id")),
        )
        replacement_run_id = str(validated["replacement_run_id"])
    return ReplacementExpectation(
        attempt_ordinal=int(latest.get("attempt_ordinal")),
        receipt_sha256=receipt_sha256,
        replacement_run_id=replacement_run_id,
    )


class VerifiedReplacementPhaseLedger(PhaseLedger):
    """Three-pipeline ledger extension for a proven pre-Candidate no-effect failure."""

    def __init__(self, root: Path, *, pair_id: str) -> None:
        self.pair_id = pair_id
        super().__init__(root, pair_id=pair_id)
        root_entry = self.root.lstat()
        if not stat.S_ISDIR(root_entry.st_mode) or stat.S_ISLNK(root_entry.st_mode):
            raise ValueError("replacement ledger root is not a no-follow directory")

    @contextmanager
    def _phase_lock(self, phase: str) -> Iterator[None]:
        lock_path = self.root / f".{phase}.activation.lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ValueError(f"phase lock is not a no-follow regular file: {phase}") from exc
        try:
            entry = os.fstat(descriptor)
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise ValueError(f"phase lock is not a link-count-one regular file: {phase}")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _atomic_write_claim(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = self.root / f".{path.name}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            serialized = _canonical_json_bytes(payload)
            offset = 0
            while offset < len(serialized):
                offset += os.write(descriptor, serialized[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            directory_descriptor = os.open(
                self.root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def claim(
        self,
        phase: str,
        authority: dict[str, Any],
        *,
        allow_verified_replacement: bool = False,
        expected_replacement: ReplacementExpectation | None = None,
    ) -> ReplacementActivation | None:
        path = self._path(phase)
        with self._phase_lock(phase):
            if _entry_exists_no_follow(path):
                prior = _read_regular_json(path)
                if prior.get("status") == "replacement_ready":
                    if not allow_verified_replacement:
                        raise AmbiguousPhaseClaimError(
                            f"phase {phase} has a verified replacement awaiting explicit resume"
                        )
                    _validate_replacement_ready_claim(
                        prior,
                        pair_id=self.pair_id,
                        phase=phase,
                        authority=authority,
                    )
                    current_expectation = _replacement_expectation_from_payload(prior)
                    if expected_replacement is None or expected_replacement != current_expectation:
                        raise AmbiguousPhaseClaimError(
                            "replacement generation differs; rerun the full recovery loader"
                        )
                    activation_token = secrets.token_hex(32)
                    prior["status"] = "claimed"
                    prior["active_attempt_ordinal"] = len(prior["failed_attempts"]) + 1
                    prior["replacement_activated"] = True
                    prior[_REPLACEMENT_ACTIVATION_TOKEN_SHA256] = hashlib.sha256(
                        activation_token.encode("ascii")
                    ).hexdigest()
                    latest_receipt = prior["failed_attempts"][-1]["receipt"]
                    if (
                        latest_receipt.get("schema_version")
                        == "chemcrow_evolved_candidate_no_effect_recovery_v1"
                    ):
                        validated = validate_evolved_candidate_no_effect_receipt(
                            latest_receipt,
                            pair_id=self.pair_id,
                        )
                        prior["active_attempt_run_id"] = validated["replacement_run_id"]
                    self._atomic_write_claim(path, prior)
                    return ReplacementActivation(
                        owner_token=activation_token,
                        expectation=current_expectation,
                    )
                if prior.get("status") == "terminal" and prior.get(
                    "authority_sha256"
                ) == canonical_sha256(authority):
                    raise ValueError(f"phase already terminal: {phase}")
                raise AmbiguousPhaseClaimError(
                    f"phase {phase} has a non-terminal prior claim; "
                    "provider-side effects require audit"
                )
            if expected_replacement is not None:
                raise ValueError("replacement expectation cannot create an initial claim")
            payload = {
                "schema_version": "chemcrow_phase_claim_v1",
                "phase": phase,
                "status": "claimed",
                "authority": authority,
                "authority_sha256": canonical_sha256(authority),
            }
            self._atomic_write_claim(path, payload)
            return None

    def terminal(
        self,
        phase: str,
        receipt: dict[str, Any],
        *,
        replacement_activation: ReplacementActivation | None = None,
    ) -> None:
        path = self._path(phase)
        with self._phase_lock(phase):
            payload = _read_regular_json(path)
            if payload.get("status") != "claimed":
                raise ValueError(f"phase is not claimable: {phase}")
            failed_attempts = payload.get("failed_attempts")
            expected_token_sha256 = payload.get(_REPLACEMENT_ACTIVATION_TOKEN_SHA256)
            if failed_attempts is not None:
                if (
                    verified_no_effect_attempt_count(
                        payload,
                        pair_id=self.pair_id,
                        phase=phase,
                    )
                    < 1
                ):
                    raise ValueError("replacement terminal attempt history is absent")
                current_expectation = _replacement_expectation_from_payload(payload)
                if (
                    not isinstance(replacement_activation, ReplacementActivation)
                    or replacement_activation.expectation != current_expectation
                    or re.fullmatch(r"[0-9a-f]{64}", expected_token_sha256 or "") is None
                    or hashlib.sha256(
                        replacement_activation.owner_token.encode("ascii")
                    ).hexdigest()
                    != expected_token_sha256
                ):
                    raise ValueError("replacement terminal owner token differs")
                if current_expectation.replacement_run_id is not None and (
                    payload.get("active_attempt_run_id") != current_expectation.replacement_run_id
                    or receipt.get("run_id") != current_expectation.replacement_run_id
                ):
                    raise ValueError("replacement terminal run authority differs")
            elif replacement_activation is not None:
                raise ValueError("non-replacement terminal cannot carry an owner token")
            payload["status"] = "terminal"
            payload["receipt"] = receipt
            payload["receipt_sha256"] = canonical_sha256(receipt)
            self._atomic_write_claim(path, payload)

    def reconcile_verified_no_effect_failure(
        self,
        phase: str,
        receipt: dict[str, Any],
    ) -> None:
        """Preserve a terminal pre-dispatch failure and allow one explicit replacement."""

        with self._phase_lock(phase):
            self._reconcile_verified_no_effect_failure_locked(phase, receipt)

    def _reconcile_verified_no_effect_failure_locked(
        self,
        phase: str,
        receipt: dict[str, Any],
    ) -> None:
        path = self._path(phase)
        payload = _read_regular_json(path)
        if payload.get("status") != "claimed":
            raise ValueError(f"phase is not a claimed failed attempt: {phase}")
        prior_attempt_count = verified_no_effect_attempt_count(
            payload,
            pair_id=self.pair_id,
            phase=phase,
        )
        if prior_attempt_count >= _MAX_VERIFIED_NO_EFFECT_ATTEMPTS:
            raise ValueError(f"phase exhausted verified no-effect replacements: {phase}")
        schema_version = receipt.get("schema_version")
        if schema_version == "chemcrow_evolved_candidate_no_effect_recovery_v1":
            receipt = validate_evolved_candidate_no_effect_receipt(
                receipt,
                pair_id=self.pair_id,
            )
        expected_status = (
            "VERIFIED_NO_EVOLVED_CANDIDATE_EFFECT_REPLACEMENT_READY"
            if schema_version == "chemcrow_evolved_candidate_no_effect_recovery_v1"
            else "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY"
        )
        if receipt.get("status") != expected_status:
            raise ValueError("no-effect recovery receipt status is invalid")
        if receipt.get("pair_id") != self.pair_id or receipt.get("phase") != phase:
            raise ValueError("no-effect recovery receipt authority differs")
        if receipt.get("attempt_ordinal") != prior_attempt_count + 1:
            raise ValueError("no-effect recovery attempt ordinal is not contiguous")
        if schema_version == "chemcrow_evolved_candidate_no_effect_recovery_v1":
            active_attempt_run_id = payload.get("active_attempt_run_id")
            if prior_attempt_count > 0 and (
                active_attempt_run_id != receipt.get("active_attempt_run_id")
            ):
                raise ValueError("evolved no-effect receipt does not bind the active attempt")
            historical_receipts = [
                attempt.get("receipt", {})
                for attempt in payload.get("failed_attempts", [])
                if isinstance(attempt, dict)
            ]
            historical_core_run_ids = [
                prior_receipt.get("core_task_id") for prior_receipt in historical_receipts
            ]
            historical_replacement_run_ids = [
                prior_receipt.get("replacement_run_id") for prior_receipt in historical_receipts
            ]
            if (
                receipt.get("core_task_id") in historical_core_run_ids
                or (
                    prior_attempt_count > 0
                    and receipt.get("core_task_id") != historical_replacement_run_ids[-1]
                )
                or receipt.get("replacement_run_id")
                in {
                    *historical_core_run_ids,
                    *historical_replacement_run_ids,
                    receipt.get("core_task_id"),
                }
                or receipt.get("core_session_id_sha256")
                in {
                    prior_receipt.get("core_session_id_sha256")
                    for prior_receipt in historical_receipts
                }
                or receipt.get("core_completion_sha256")
                in {
                    prior_receipt.get("core_completion_sha256")
                    for prior_receipt in historical_receipts
                }
            ):
                raise ValueError("evolved no-effect receipt reused historical evidence")
        if receipt.get("authority_sha256") != payload.get("authority_sha256"):
            raise ValueError("no-effect recovery authority hash differs")
        original_bytes = _canonical_json_bytes(payload)
        if receipt.get("original_claim_sha256") != hashlib.sha256(original_bytes).hexdigest():
            raise ValueError("no-effect recovery original claim hash differs")
        predicates = receipt.get("no_effect_predicates")
        if (
            not isinstance(predicates, dict)
            or not predicates
            or set(predicates.values()) != {True}
        ):
            raise ValueError("no-effect recovery predicates are incomplete")
        receipt_sha256 = canonical_sha256(receipt)
        outcomes = {
            "chemcrow_pre_candidate_no_effect_recovery_v1": ("terminal_pre_candidate_no_effect"),
            "chemcrow_baseline_evaluator_no_effect_recovery_v1": (
                "terminal_baseline_evaluator_no_effect"
            ),
            "chemcrow_reflector_no_effect_recovery_v1": ("terminal_reflector_no_effect"),
            "chemcrow_evolved_candidate_no_effect_recovery_v1": (
                "terminal_evolved_candidate_no_effect"
            ),
        }
        outcome = outcomes.get(schema_version)
        if outcome is None:
            raise ValueError("no-effect recovery schema is invalid")
        allowed_phase, absent_call_key = {
            "chemcrow_pre_candidate_no_effect_recovery_v1": (
                {"baseline_candidate"},
                "candidate_model_call_proven_absent",
            ),
            "chemcrow_baseline_evaluator_no_effect_recovery_v1": (
                {"baseline_internal_evaluator"},
                "evaluator_model_call_proven_absent",
            ),
            "chemcrow_reflector_no_effect_recovery_v1": (
                {
                    "reflector_memory",
                    "reflector_skill_bundle",
                    "reflector_agent_system",
                },
                "reflector_model_call_proven_absent",
            ),
            "chemcrow_evolved_candidate_no_effect_recovery_v1": (
                {"evolved_candidate"},
                "evolved_candidate_model_call_proven_absent",
            ),
        }[schema_version]
        if (
            phase not in allowed_phase
            or receipt.get(absent_call_key) is not True
            or receipt.get("duplicate_scientific_call") is not False
            or receipt.get("recorded_before_replacement_dispatch") is not True
        ):
            raise ValueError("no-effect recovery scientific-call proof is invalid")
        payload["schema_version"] = "chemcrow_phase_claim_v2"
        payload["status"] = "replacement_ready"
        failed_attempts = list(payload.get("failed_attempts") or [])
        failed_attempts.append(
            {
                "attempt_ordinal": prior_attempt_count + 1,
                "outcome": outcome,
                "receipt": receipt,
                "receipt_sha256": receipt_sha256,
            }
        )
        payload["failed_attempts"] = failed_attempts
        payload["active_attempt_ordinal"] = None
        if schema_version == "chemcrow_evolved_candidate_no_effect_recovery_v1":
            payload["active_attempt_run_id"] = None
        payload["replacement_activated"] = False
        payload.pop(_REPLACEMENT_ACTIVATION_TOKEN_SHA256, None)
        self._atomic_write_claim(path, payload)

    def replacement_ready(self, phase: str) -> bool:
        path = self._path(phase)
        if not path.is_file():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "replacement_ready":
            return False
        _validate_replacement_ready_claim(
            payload,
            pair_id=self.pair_id,
            phase=phase,
            authority=payload.get("authority"),
        )
        return True

    def replacement_expectation(self, phase: str) -> ReplacementExpectation:
        path = self._path(phase)
        with self._phase_lock(phase):
            payload = _read_regular_json(path)
            _validate_replacement_ready_claim(
                payload,
                pair_id=self.pair_id,
                phase=phase,
                authority=payload.get("authority"),
            )
            return _replacement_expectation_from_payload(payload)

    def evolved_replacement_receipt(
        self,
        phase: str = "evolved_candidate",
    ) -> dict[str, Any]:
        path = self._path(phase)
        if not path.is_file():
            raise ValueError("evolved replacement claim is absent")
        payload = json.loads(path.read_text(encoding="utf-8"))
        _validate_replacement_ready_claim(
            payload,
            pair_id=self.pair_id,
            phase=phase,
            authority=payload.get("authority"),
        )
        return validate_evolved_candidate_no_effect_receipt(
            payload["failed_attempts"][-1]["receipt"],
            pair_id=self.pair_id,
        )

    def evolved_replacement_run_id(self, phase: str = "evolved_candidate") -> str:
        return str(self.evolved_replacement_receipt(phase)["replacement_run_id"])


def verified_no_effect_attempt_count(
    payload: dict[str, Any],
    *,
    pair_id: str,
    phase: str,
) -> int:
    """Validate and count preserved terminal pre-Candidate infrastructure attempts."""

    attempts = payload.get("failed_attempts")
    if attempts is None:
        return 0
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("failed-attempt history is invalid")
    for ordinal, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict):
            raise TypeError("failed-attempt entry is invalid")
        receipt = attempt.get("receipt")
        schema_version = receipt.get("schema_version") if isinstance(receipt, dict) else None
        if schema_version == "chemcrow_pre_candidate_no_effect_recovery_v1":
            expected_outcome = "terminal_pre_candidate_no_effect"
            model_call_absent = receipt.get("candidate_model_call_proven_absent") is True
            phase_valid = phase == "baseline_candidate"
            expected_status = "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY"
        elif schema_version == "chemcrow_baseline_evaluator_no_effect_recovery_v1":
            expected_outcome = "terminal_baseline_evaluator_no_effect"
            model_call_absent = receipt.get("evaluator_model_call_proven_absent") is True
            phase_valid = phase == "baseline_internal_evaluator"
            expected_status = "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY"
        elif schema_version == "chemcrow_reflector_no_effect_recovery_v1":
            expected_outcome = "terminal_reflector_no_effect"
            model_call_absent = receipt.get("reflector_model_call_proven_absent") is True
            phase_valid = phase in {
                "reflector_memory",
                "reflector_skill_bundle",
                "reflector_agent_system",
            }
            expected_status = "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY"
        elif schema_version == "chemcrow_evolved_candidate_no_effect_recovery_v1":
            validate_evolved_candidate_no_effect_receipt(
                receipt,
                pair_id=pair_id,
            )
            expected_outcome = "terminal_evolved_candidate_no_effect"
            model_call_absent = receipt.get("evolved_candidate_model_call_proven_absent") is True
            phase_valid = phase == "evolved_candidate"
            expected_status = "VERIFIED_NO_EVOLVED_CANDIDATE_EFFECT_REPLACEMENT_READY"
        else:
            expected_outcome = None
            model_call_absent = False
            phase_valid = False
            expected_status = None
        if (
            attempt.get("attempt_ordinal") != ordinal
            or attempt.get("outcome") != expected_outcome
            or not isinstance(receipt, dict)
            or attempt.get("receipt_sha256") != canonical_sha256(receipt)
            or receipt.get("attempt_ordinal") != ordinal
            or receipt.get("status") != expected_status
            or receipt.get("pair_id") != pair_id
            or receipt.get("phase") != phase
            or receipt.get("authority_sha256") != payload.get("authority_sha256")
            or not model_call_absent
            or not phase_valid
            or receipt.get("duplicate_scientific_call") is not False
            or receipt.get("recorded_before_replacement_dispatch") is not True
        ):
            raise ValueError("failed-attempt receipt is invalid")
        predicates = receipt.get("no_effect_predicates")
        if (
            not isinstance(predicates, dict)
            or not predicates
            or set(predicates.values()) != {True}
        ):
            raise ValueError("failed-attempt no-effect proof is incomplete")
    evolved_receipts = [
        attempt["receipt"]
        for attempt in attempts
        if attempt["receipt"].get("schema_version")
        == "chemcrow_evolved_candidate_no_effect_recovery_v1"
    ]
    if evolved_receipts:
        core_run_ids = [receipt["core_task_id"] for receipt in evolved_receipts]
        replacement_run_ids = [receipt["replacement_run_id"] for receipt in evolved_receipts]
        invariant_fields = (
            "task_id",
            "task_authority_sha256",
            "authority_sha256",
            "config_sha256",
            "checkpoint_sha256",
            "input_evidence_sha256",
            "artifact_ids_by_type",
            "reflector_job_ids",
            "reflector_run_ids",
            "reflector_prompt_hashes",
        )
        if (
            len(core_run_ids) != len(set(core_run_ids))
            or len(replacement_run_ids) != len(set(replacement_run_ids))
            or core_run_ids[1:] != replacement_run_ids[:-1]
            or core_run_ids[0] in replacement_run_ids
            or replacement_run_ids[-1] in core_run_ids
            or len({receipt["core_session_id_sha256"] for receipt in evolved_receipts})
            != len(evolved_receipts)
            or len({receipt["core_completion_sha256"] for receipt in evolved_receipts})
            != len(evolved_receipts)
            or any(
                len({canonical_sha256(receipt[field]) for receipt in evolved_receipts}) != 1
                for field in invariant_fields
            )
        ):
            raise ValueError("evolved failed-attempt history reused completion evidence")
    count = len(attempts)
    if count > _MAX_VERIFIED_NO_EFFECT_ATTEMPTS:
        raise ValueError("verified no-effect replacement limit exceeded")
    if payload.get("status") == "replacement_ready":
        if (
            payload.get("replacement_activated") is not False
            or payload.get("active_attempt_ordinal") is not None
            or _REPLACEMENT_ACTIVATION_TOKEN_SHA256 in payload
            or (
                attempts[-1]["receipt"].get("schema_version")
                == "chemcrow_evolved_candidate_no_effect_recovery_v1"
                and payload.get("active_attempt_run_id") is not None
            )
        ):
            raise ValueError("replacement-ready claim activation state is invalid")
    elif payload.get("status") in {"claimed", "terminal"}:
        latest_receipt = attempts[-1]["receipt"]
        if (
            payload.get("replacement_activated") is not True
            or payload.get("active_attempt_ordinal") != count + 1
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(payload.get(_REPLACEMENT_ACTIVATION_TOKEN_SHA256, "")),
            )
            is None
            or (
                latest_receipt.get("schema_version")
                == "chemcrow_evolved_candidate_no_effect_recovery_v1"
                and payload.get("active_attempt_run_id")
                != latest_receipt.get("replacement_run_id")
            )
        ):
            raise ValueError("replacement claim activation evidence is invalid")
    else:
        raise ValueError("replacement claim status is invalid")
    return count


def _validate_replacement_ready_claim(
    payload: dict[str, Any],
    *,
    pair_id: str,
    phase: str,
    authority: object,
) -> None:
    if (
        payload.get("schema_version") != "chemcrow_phase_claim_v2"
        or payload.get("phase") != phase
        or payload.get("status") != "replacement_ready"
        or not isinstance(authority, dict)
        or payload.get("authority_sha256") != canonical_sha256(authority)
        or payload.get("active_attempt_ordinal") is not None
        or payload.get("replacement_activated") is not False
    ):
        raise ValueError("replacement-ready claim is invalid")
    count = verified_no_effect_attempt_count(payload, pair_id=pair_id, phase=phase)
    receipt = payload["failed_attempts"][-1]["receipt"]
    if receipt.get("pair_id") != pair_id or receipt.get("phase") != phase or count < 1:
        raise ValueError("replacement-ready claim recovery authority differs")


__all__ = [
    "EvolvedCandidateNoEffectReceipt",
    "ReplacementActivation",
    "ReplacementExpectation",
    "VerifiedReplacementPhaseLedger",
    "validate_evolved_candidate_no_effect_receipt",
    "verified_no_effect_attempt_count",
]
