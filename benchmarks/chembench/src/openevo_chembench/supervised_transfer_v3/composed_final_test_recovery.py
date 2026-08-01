"""Audited suffix recovery for an interrupted composed v3 Final Test.

The failed parent run and its protocol-global ledger are immutable inputs.  A
fresh recovery run re-freezes the same composed Train heads, proves the same
artifact-set digest, and executes only the ordered Test suffix beginning with
the first UID that has no parent completion.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2 import experiment as v2
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedTaskExecutionCodeV2,
    SupervisedTaskExecutionErrorV2,
)
from openevo_chembench.supervised_transfer_v2.experiment import TaskExecutorV2
from openevo_chembench.supervised_transfer_v3.category_shard_recovery import (
    _read_json,
    _read_jsonl,
    _tree_identity,
)
from openevo_chembench.supervised_transfer_v3.composed_final_test import (
    ComposedFinalTestExperimentV3,
    ComposedTrainAuditV3,
    _git_bytes,
    _InputsAdapter,
    _issue_context,
    _model_digest_from_inputs,
    _require_current_compatibility,
)
from openevo_chembench.supervised_transfer_v3.config import TEST_COUNT
from openevo_chembench.supervised_transfer_v3.experiment import (
    FrozenThreeTargetSetV3,
    SupervisedExperimentV3Error,
)
from openevo_chembench.supervised_transfer_v3.same_profile_category_recovery import (
    EXPECTED_MANAGED_CODEX_SHA256,
)
from openevo_chembench.supervised_transfer_v3.test_ledger import (
    FinalTestAttemptV3,
    FinalTestConsumptionLedgerV3,
)

FINAL_TEST_RECOVERY_PROTOCOL_ID = (
    "supervised_transfer_v3_composed_final_test_suffix_recovery"
)
DEFAULT_PARENT_FINAL_TEST_RUN_ID = "stv3-composed-final-test-20260731T155440Z"
EXPECTED_PARENT_FAILURE_CODE = "EXECUTOR_STALLED"

_LEDGER_KEYS = {
    "schema_version",
    "arm",
    "task_uid",
    "attempt_id",
    "completion_exists",
    "completion_sha256",
    "source_commit",
    "artifact_set_digest",
    "config_digest",
    "model_digest",
    "timestamp",
}
_COMPATIBILITY_CRITICAL_FILES = (
    (
        "benchmarks/chembench/configs/supervised_transfer_v3/"
        "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
    ),
    "benchmarks/chembench/src/openevo_chembench/chembench4k_dataset.py",
    "benchmarks/chembench/src/openevo_chembench/chembench4k_evaluation.py",
    "benchmarks/chembench/src/openevo_chembench/chembench4k_models.py",
    "benchmarks/chembench/src/openevo_chembench/chembench4k_prompt.py",
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/executor.py",
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/experiment.py",
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/composed_final_test.py",
)
_LEDGER_SERIALIZATION_FILE = (
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/test_ledger.py"
)
_RECOVERY_CHANGE_ALLOWLIST = frozenset(
    {
        "benchmarks/chembench/configs/supervised_transfer_v3/composed_final_test_recovery.md",
        "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json",
        "benchmarks/chembench/scripts/supervised_transfer_v3/main.py",
        (
            "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
            "composed_final_test_recovery.py"
        ),
        (
            "benchmarks/chembench/tests/supervised_transfer_v3/"
            "test_composed_final_test_recovery_v3.py"
        ),
        "benchmarks/chembench/tests/supervised_transfer_v3/test_protocol_v3.py",
        _LEDGER_SERIALIZATION_FILE,
    }
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class AcceptedFinalTestPrefixReceiptV3(_ClosedModel):
    schema_version: Literal["AcceptedFinalTestPrefixReceiptV3"] = (
        "AcceptedFinalTestPrefixReceiptV3"
    )
    recovery_protocol_id: str = FINAL_TEST_RECOVERY_PROTOCOL_ID
    parent_run_id: str
    parent_source_commit: str
    parent_status: Literal["FAIL_CLOSED"] = "FAIL_CLOSED"
    parent_failure_code: Literal["EXECUTOR_STALLED"] = "EXECUTOR_STALLED"
    parent_run_tree_sha256: str
    parent_ledger_sha256: str
    frozen_receipt_sha256: str
    frozen_artifact_set_sha256: str
    config_digest: str
    split_digest: str
    model_digest: str
    managed_codex_digest: str
    task_start_ordinal: Literal[0] = 0
    task_end_ordinal: int = Field(ge=0, lt=TEST_COUNT)
    completion_count: int = Field(gt=0, le=TEST_COUNT)
    correct_count: int = Field(ge=0, le=TEST_COUNT)
    completion_uid_order_sha256: str
    final_prefix_closed: Literal[True] = True
    included_in_final_statistics: Literal[True] = True

    @model_validator(mode="after")
    def _counts_match(self) -> AcceptedFinalTestPrefixReceiptV3:
        if self.task_end_ordinal + 1 != self.completion_count:
            raise ValueError("accepted prefix is not contiguous")
        if self.correct_count > self.completion_count:
            raise ValueError("accepted prefix correctness count is invalid")
        return self


class DiscardedIncompleteTaskReceiptV3(_ClosedModel):
    schema_version: Literal["DiscardedIncompleteTaskReceiptV3"] = (
        "DiscardedIncompleteTaskReceiptV3"
    )
    recovery_protocol_id: str = FINAL_TEST_RECOVERY_PROTOCOL_ID
    parent_run_id: str
    task_ordinal: int = Field(ge=0, lt=TEST_COUNT)
    task_uid: str
    category: str
    incomplete_attempt_count: int = Field(gt=0)
    completion_exists: Literal[False] = False
    included_in_final_statistics: Literal[False] = False
    used_as_recovery_input: Literal[False] = False


class FinalTestSuffixRecoveryPlanV3(_ClosedModel):
    schema_version: Literal["FinalTestSuffixRecoveryPlanV3"] = (
        "FinalTestSuffixRecoveryPlanV3"
    )
    recovery_protocol_id: str = FINAL_TEST_RECOVERY_PROTOCOL_ID
    recovery_run_id: str
    parent_run_id: str
    parent_prefix_receipt_sha256: str
    discarded_incomplete_task_receipt_sha256: str
    recovery_source_commit: str
    composition_run_id: str
    start_task_ordinal: int = Field(ge=0, lt=TEST_COUNT)
    first_category: str
    first_category_task_ordinal: int = Field(ge=0, lt=50)
    remaining_task_count: int = Field(gt=0, le=TEST_COUNT)
    candidate_calls: int = Field(gt=0, le=TEST_COUNT)
    reflector_calls: Literal[0] = 0
    core_jobs: Literal[0] = 0
    frozen_target_count: Literal[27] = 27
    parent_results_or_state_modified: Literal[False] = False
    parent_ledger_modified: Literal[False] = False
    parent_completion_reexecuted: Literal[False] = False
    candidate_execution_path: Literal[
        "TaskRequest->Rollout->Gateway->CodexHarness"
    ] = "TaskRequest->Rollout->Gateway->CodexHarness"
    test_feedback_enabled: Literal[False] = False
    candidate_codex_sha256: str
    runtime_services_identity_sha256: str

    @model_validator(mode="after")
    def _remaining_matches(self) -> FinalTestSuffixRecoveryPlanV3:
        if (
            self.remaining_task_count != TEST_COUNT - self.start_task_ordinal
            or self.candidate_calls != self.remaining_task_count
        ):
            raise ValueError("recovery suffix count is inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class FinalTestPrefixAuditV3:
    repository_root: Path
    parent_run_id: str
    parent_state_root: Path
    parent_result_root: Path
    parent_ledger_path: Path
    accepted: AcceptedFinalTestPrefixReceiptV3
    discarded: DiscardedIncompleteTaskReceiptV3
    parent_private_evaluations: tuple[dict[str, Any], ...]

    @property
    def start_task_ordinal(self) -> int:
        return self.accepted.completion_count

    @property
    def digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "accepted": self.accepted.digest,
                    "discarded": self.discarded.digest,
                }
            )
        )


def audit_failed_final_test_prefix_v3(
    *,
    repository_root: Path,
    inputs: v2.ExperimentInputsV2,
    composition_audit: ComposedTrainAuditV3,
    parent_run_id: str = DEFAULT_PARENT_FINAL_TEST_RUN_ID,
) -> FinalTestPrefixAuditV3:
    """Validate a unique contiguous parent completion prefix without writes."""

    repository = repository_root.resolve(strict=True)
    if inputs.repository_root != repository:
        raise SupervisedExperimentV3Error("FINAL_TEST_RECOVERY_REPOSITORY_MISMATCH")
    _require_current_compatibility(inputs, composition_audit)
    parent_state_root = (
        repository
        / inputs.config.payload["roots"]["state"]
        / "runs"
        / parent_run_id
    )
    parent_result_root = (
        repository
        / inputs.config.payload["roots"]["results"]
        / "runs"
        / parent_run_id
    )
    parent_ledger_path = (
        repository
        / inputs.config.payload["roots"]["state"]
        / "private/final_test_consumption_ledger_v3.jsonl"
    )
    state = _read_json(parent_state_root / "run_state.json")
    if (
        state.get("run_id") != parent_run_id
        or state.get("status") != "FAIL_CLOSED"
        or state.get("stage") != "EVOLVED_TEST"
        or state.get("failure_code") != EXPECTED_PARENT_FAILURE_CODE
        or state.get("resume_allowed") is not False
        or state.get("security_findings") != 0
        or state.get("context_findings") != 0
        or state.get("artifact_findings") != 0
    ):
        raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_STATE_INVALID")
    if (
        state.get("config_sha256") != inputs.config.digest
        or state.get("split_receipt_sha256") != inputs.split_receipt_sha256
        or state.get("candidate_codex_executable_sha256")
        != EXPECTED_MANAGED_CODEX_SHA256
        or state.get("frozen_target_count") != 27
    ):
        raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_IDENTITY_INVALID")

    frozen_path = parent_result_root / "public/frozen_three_target_transfer_receipt_v3.json"
    frozen = _read_json(frozen_path)
    frozen_sha256 = sha256_bytes(frozen_path.read_bytes())
    if (
        state.get("frozen_receipt_sha256") != frozen_sha256
        or state.get("frozen_artifact_set_sha256") != frozen.get("artifact_set_sha256")
        or frozen.get("composition_run_id") != composition_audit.composition_run_id
        or frozen.get("composition_receipt_sha256")
        != composition_audit.composition_receipt_sha256
        or frozen.get("test_order_sha256") != v2._uid_order_digest(inputs.test)
        or frozen.get("test_evolution_allowed") is not False
    ):
        raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_FROZEN_RECEIPT_INVALID")

    private_rows = _read_jsonl(parent_state_root / "private/events.jsonl")
    private_evaluated = tuple(
        row
        for row in private_rows
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("stage") == "EVOLVED_TEST"
    )
    public_rows = _read_jsonl(parent_result_root / "public/events.jsonl")
    public_completed = tuple(
        row
        for row in public_rows
        if row.get("kind") == "TASK_MODEL_EXECUTED"
        and row.get("stage") == "EVOLVED_TEST"
    )
    if not private_evaluated or len(private_evaluated) != len(public_completed):
        raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_COMPLETION_COUNT_INVALID")
    completed_count = len(private_evaluated)
    expected_prefix = tuple(inputs.test[:completed_count])
    expected_uids = tuple(task.uid for task in expected_prefix)
    for ordinal, (private, public, task) in enumerate(
        zip(private_evaluated, public_completed, expected_prefix, strict=True)
    ):
        if (
            private.get("task_ordinal") != ordinal
            or public.get("task_ordinal") != ordinal
            or private.get("task_uid") != task.uid
            or public.get("task_uid") != task.uid
            or private.get("session_id") != public.get("session_id")
            or private.get("category") != task.category
            or public.get("category") != task.category
        ):
            raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_PREFIX_NOT_CONTIGUOUS")

    ledger_entries = _load_parent_ledger_read_only(parent_ledger_path)
    parent_identity = (
        state["source_commit"],
        state["frozen_artifact_set_sha256"],
        state["config_sha256"],
        _model_digest_from_inputs(_InputsAdapter(inputs)),
    )
    by_uid = _validate_parent_ledger_sequence(ledger_entries, parent_identity)
    ledger_completed = tuple(
        task.uid
        for task in inputs.test
        if any(item.completion_exists for item in by_uid.get(task.uid, ()))
    )
    if ledger_completed != expected_uids:
        raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_LEDGER_PREFIX_INVALID")
    next_task = inputs.test[completed_count]
    next_attempts = tuple(by_uid.get(next_task.uid, ()))
    if not next_attempts or any(item.completion_exists for item in next_attempts):
        raise SupervisedExperimentV3Error("FINAL_TEST_RECOVERY_BOUNDARY_INVALID")
    allowed_uids = {*expected_uids, next_task.uid}
    if set(by_uid) != allowed_uids:
        raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_LEDGER_SUFFIX_PRESENT")

    parent_tree_sha256 = _tree_identity((parent_state_root, parent_result_root))[0]
    accepted = AcceptedFinalTestPrefixReceiptV3(
        parent_run_id=parent_run_id,
        parent_source_commit=str(state["source_commit"]),
        parent_run_tree_sha256=parent_tree_sha256,
        parent_ledger_sha256=sha256_bytes(parent_ledger_path.read_bytes()),
        frozen_receipt_sha256=frozen_sha256,
        frozen_artifact_set_sha256=str(state["frozen_artifact_set_sha256"]),
        config_digest=str(state["config_sha256"]),
        split_digest=str(state["split_receipt_sha256"]),
        model_digest=parent_identity[3],
        managed_codex_digest=str(state["candidate_codex_executable_sha256"]),
        task_end_ordinal=completed_count - 1,
        completion_count=completed_count,
        correct_count=sum(bool(row.get("correct")) for row in private_evaluated),
        completion_uid_order_sha256=sha256_bytes(canonical_json_bytes(expected_uids)),
    )
    discarded = DiscardedIncompleteTaskReceiptV3(
        parent_run_id=parent_run_id,
        task_ordinal=completed_count,
        task_uid=next_task.uid,
        category=next_task.category,
        incomplete_attempt_count=len(next_attempts),
    )
    return FinalTestPrefixAuditV3(
        repository_root=repository,
        parent_run_id=parent_run_id,
        parent_state_root=parent_state_root,
        parent_result_root=parent_result_root,
        parent_ledger_path=parent_ledger_path,
        accepted=accepted,
        discarded=discarded,
        parent_private_evaluations=private_evaluated,
    )


def build_final_test_recovery_dry_run_v3(
    *,
    inputs: v2.ExperimentInputsV2,
    composition_audit: ComposedTrainAuditV3,
    prefix_audit: FinalTestPrefixAuditV3,
    recovery_run_id: str,
) -> dict[str, object]:
    _require_current_compatibility(inputs, composition_audit)
    start = prefix_audit.start_task_ordinal
    first = inputs.test[start]
    return {
        "schema_version": "ComposedFinalTestRecoveryDryRunV3",
        "recovery_protocol_id": FINAL_TEST_RECOVERY_PROTOCOL_ID,
        "status": "PASS",
        "recovery_run_id": recovery_run_id,
        "parent_run_id": prefix_audit.parent_run_id,
        "accepted_parent_completion_count": start,
        "start_task_ordinal": start,
        "first_category": first.category,
        "first_category_task_ordinal": start % 50,
        "remaining_task_count": TEST_COUNT - start,
        "planned_candidate_calls": TEST_COUNT - start,
        "planned_reflector_calls": 0,
        "planned_core_jobs": 0,
        "frozen_target_count": 27,
        "parent_results_or_state_modified": False,
        "parent_ledger_modified": False,
        "parent_completion_reexecuted": False,
        "model_calls": 0,
    }


def build_final_test_recovery_source_compatibility_receipt_v3(
    *,
    inputs: v2.ExperimentInputsV2,
    composition_audit: ComposedTrainAuditV3,
    prefix_audit: FinalTestPrefixAuditV3,
) -> dict[str, object]:
    _require_current_compatibility(inputs, composition_audit)
    parent_commit = prefix_audit.accepted.parent_source_commit
    current_commit = inputs.source_commit
    changed = tuple(
        line
        for line in v2._git(
            inputs.repository_root,
            "diff",
            "--name-only",
            f"{parent_commit}..{current_commit}",
            "--",
            "benchmarks/chembench",
        ).splitlines()
        if line
    )
    if not changed or not set(changed).issubset(_RECOVERY_CHANGE_ALLOWLIST):
        raise SupervisedExperimentV3Error("FINAL_TEST_RECOVERY_SOURCE_SCOPE_INVALID")
    critical_hashes: dict[str, str] = {}
    for relative in _COMPATIBILITY_CRITICAL_FILES:
        current = (inputs.repository_root / relative).read_bytes()
        parent = _git_bytes(inputs.repository_root, parent_commit, relative)
        if current != parent:
            raise SupervisedExperimentV3Error(
                "FINAL_TEST_RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE"
            )
        critical_hashes[relative] = sha256_bytes(current)
    diff = v2._git(
        inputs.repository_root,
        "diff",
        "--binary",
        f"{parent_commit}..{current_commit}",
        "--",
        *changed,
    )
    parent_ledger_source = _git_bytes(
        inputs.repository_root,
        parent_commit,
        _LEDGER_SERIALIZATION_FILE,
    )
    current_ledger_source = (
        inputs.repository_root / _LEDGER_SERIALIZATION_FILE
    ).read_bytes()
    return {
        "schema_version": "FinalTestRecoverySourceCompatibilityReceiptV3",
        "recovery_protocol_id": FINAL_TEST_RECOVERY_PROTOCOL_ID,
        "parent_source_commit": parent_commit,
        "recovery_source_commit": current_commit,
        "git_diff_sha256": sha256_bytes(diff.encode()),
        "files_changed": list(changed),
        "critical_execution_files_sha256": critical_hashes,
        "ledger_serialization_file": _LEDGER_SERIALIZATION_FILE,
        "parent_ledger_serialization_source_sha256": sha256_bytes(
            parent_ledger_source
        ),
        "recovery_ledger_serialization_source_sha256": sha256_bytes(
            current_ledger_source
        ),
        "ledger_change_limited_to_canonical_newline_and_legacy_read": True,
        "config_digest": inputs.config.digest,
        "split_digest": inputs.split_receipt_sha256,
        "model_digest": _model_digest_from_inputs(_InputsAdapter(inputs)),
        "managed_codex_digest": inputs.candidate_codex.executable_sha256,
        "changes_limited_to_suffix_recovery_orchestration": True,
        "candidate_prompt_unchanged": True,
        "candidate_model_unchanged": True,
        "reasoning_effort_unchanged": True,
        "candidate_harness_unchanged": True,
        "evaluator_unchanged": True,
        "strict_parser_unchanged": True,
        "test_order_unchanged": True,
        "test_single_pass_unchanged": True,
        "frozen_artifact_set_unchanged": True,
        "semantically_compatible": True,
    }


class ComposedFinalTestSuffixRecoveryV3(ComposedFinalTestExperimentV3):
    """Run a fresh immutable Test suffix and compose it with a parent prefix."""

    def __init__(
        self,
        *,
        inputs: v2.ExperimentInputsV2,
        run_id: str,
        composition_audit: ComposedTrainAuditV3,
        prefix_audit: FinalTestPrefixAuditV3,
        runtime_smoke_run_id: str,
        executor_factory: Any | None = None,
    ) -> None:
        self.prefix_audit = prefix_audit
        super().__init__(
            inputs=inputs,
            run_id=run_id,
            composition_audit=composition_audit,
            runtime_smoke_run_id=runtime_smoke_run_id,
            executor_factory=executor_factory,
        )
        self._state.update(
            {
                "schema_version": "ChemBenchComposedFinalTestRecoveryRunStateV3",
                "protocol_id": FINAL_TEST_RECOVERY_PROTOCOL_ID,
                "run_mode": "composed_final_test_suffix_recovery",
                "parent_final_test_run_id": prefix_audit.parent_run_id,
                "accepted_parent_completion_count": prefix_audit.start_task_ordinal,
                "recovery_start_task_ordinal": prefix_audit.start_task_ordinal,
                "parent_results_or_state_modified": False,
                "parent_ledger_modified": False,
                "parent_completion_reexecuted": False,
            }
        )
        self._write_state()
        self._write_protocol_receipts()

    def run_final_test_recovery(self) -> dict[str, object]:
        try:
            self._verify_parent_unchanged()
            self._verify_source_trees_unchanged()
            contexts = {
                item.category: _issue_context(item.final_result)
                for item in self.composition_audit.categories
            }
            frozen = self._freeze_composed_targets(contexts)
            if (
                frozen.artifact_set_digest
                != self.prefix_audit.accepted.frozen_artifact_set_sha256
            ):
                raise SupervisedExperimentV3Error(
                    "FINAL_TEST_RECOVERY_ARTIFACT_SET_DRIFT"
                )
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("online"))
                self._run_suffix(frozen, executor)
            self._verify_parent_unchanged()
            self._verify_source_trees_unchanged()
            completion = self._write_composition_receipt(frozen)
            self._set_stage("COMPLETED")
            path = (
                self.result_root
                / "public/final_test_prefix_suffix_composition_receipt_v3.json"
            )
            self._state.update(
                {
                    "status": "COMPLETED",
                    "final_test_status": "COMPLETED_BY_PREFIX_SUFFIX_COMPOSITION",
                    "completed_at_utc": v2.utc_now(),
                    "completion_receipt_sha256": sha256_bytes(path.read_bytes()),
                }
            )
            self._write_state()
            return completion
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def _run_suffix(
        self,
        frozen: FrozenThreeTargetSetV3,
        executor: TaskExecutorV2,
    ) -> None:
        self._set_stage("EVOLVED_TEST")
        model_digest = sha256_bytes(
            canonical_json_bytes(
                {
                    "model": self.inputs.config.model,
                    "reasoning_effort": self.inputs.config.reasoning_effort,
                    "managed_runtime_image": MANAGED_RUNTIME_RELEASES[
                        "managed_science"
                    ].loaded_image_id,
                }
            )
        )
        ledger = FinalTestConsumptionLedgerV3(
            path=(self.state_root / "private/final_test_suffix_ledger_v3.jsonl").resolve(),
            source_commit=self.inputs.source_commit,
            artifact_set_digest=frozen.artifact_set_digest,
            config_digest=self.inputs.config.digest,
            model_digest=model_digest,
        )
        for ordinal, task in enumerate(
            self.inputs.test[self.prefix_audit.start_task_ordinal :],
            start=self.prefix_audit.start_task_ordinal,
        ):
            retry_window_started_at = time.monotonic()
            for attempt_number in range(
                1, v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT + 1
            ):
                if attempt_number > 1:
                    v2._require_executor_retry_window(retry_window_started_at)
                self._require_source_frozen()
                self._verify_parent_unchanged()
                context = frozen.contexts[task.category]
                attempt_id = self._session_id(
                    "EVOLVED_TEST", "evolved", task, 0, attempt_number
                )
                ledger.claim(
                    task_uid=task.uid,
                    attempt_id=attempt_id,
                    timestamp=v2.utc_now(),
                )
                try:
                    self._execute_session(
                        executor,
                        task=task,
                        context=context,
                        logical_arm="test_evolved",
                        executor_arm="online",
                        task_ordinal=ordinal,
                        round_index=0,
                        stage="EVOLVED_TEST",
                        session_id_override=attempt_id,
                        completion_callback=lambda completion, *, _task=task, _attempt=attempt_id: ledger.complete(
                            task_uid=_task.uid,
                            attempt_id=_attempt,
                            completion=completion,
                            timestamp=v2.utc_now(),
                        ),
                    )
                    break
                except SupervisedTaskExecutionErrorV2 as exc:
                    if exc.completion_exists is True:
                        if exc.completion_sha256 is None:
                            raise SupervisedExperimentV3Error(
                                "TEST_COMPLETION_DIGEST_MISSING"
                            ) from exc
                        ledger.complete_digest(
                            task_uid=task.uid,
                            attempt_id=attempt_id,
                            completion_sha256=exc.completion_sha256,
                            timestamp=v2.utc_now(),
                        )
                        raise
                    retryable = (
                        exc.code is SupervisedTaskExecutionCodeV2.TASK_FAILED
                        and exc.completion_exists is False
                        and attempt_number
                        < v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT
                    )
                    if not retryable:
                        raise
                    delay = v2._executor_retry_delay_seconds(
                        retry_window_started_at,
                        v2._FINAL_TEST_INFRASTRUCTURE_RETRY_SECONDS,
                    )
                    self._append_public(
                        {
                            "schema_version": "FinalTestRecoveryInfrastructureRetryPublicV3",
                            "kind": "INFRASTRUCTURE_RETRY_NO_COMPLETION",
                            "stage": "EVOLVED_TEST",
                            "task_uid": task.uid,
                            "task_ordinal": ordinal,
                            "attempt_number": attempt_number,
                            "completion_exists": False,
                            "retry_after_seconds": delay,
                            "recorded_at_utc": v2.utc_now(),
                        }
                    )
                    time.sleep(delay)
            else:
                raise SupervisedExperimentV3Error(
                    "FINAL_TEST_INFRASTRUCTURE_RETRY_EXHAUSTED"
                )
        summary = ledger.summary()
        expected = TEST_COUNT - self.prefix_audit.start_task_ordinal
        if summary["completion_count"] != expected:
            raise SupervisedExperimentV3Error("FINAL_TEST_RECOVERY_LEDGER_INCOMPLETE")
        write_public_file(
            self.result_root / "public/final_test_suffix_ledger_receipt_v3.json",
            canonical_pretty_json_bytes(summary),
        )

    def _write_protocol_receipts(self) -> None:
        accepted_path = self.result_root / "public/accepted_final_test_prefix_receipt_v3.json"
        discarded_path = (
            self.result_root / "public/discarded_incomplete_task_receipt_v3.json"
        )
        write_public_file(
            accepted_path,
            canonical_pretty_json_bytes(
                self.prefix_audit.accepted.model_dump(mode="json")
            ),
        )
        write_public_file(
            discarded_path,
            canonical_pretty_json_bytes(
                self.prefix_audit.discarded.model_dump(mode="json")
            ),
        )

    def _write_paid_plan(self) -> None:
        start = self.prefix_audit.start_task_ordinal
        first = self.inputs.test[start]
        plan = FinalTestSuffixRecoveryPlanV3(
            recovery_run_id=self.run_id,
            parent_run_id=self.prefix_audit.parent_run_id,
            parent_prefix_receipt_sha256=self.prefix_audit.accepted.digest,
            discarded_incomplete_task_receipt_sha256=(
                self.prefix_audit.discarded.digest
            ),
            recovery_source_commit=self.inputs.source_commit,
            composition_run_id=self.composition_audit.composition_run_id,
            start_task_ordinal=start,
            first_category=first.category,
            first_category_task_ordinal=start % 50,
            remaining_task_count=TEST_COUNT - start,
            candidate_calls=TEST_COUNT - start,
            candidate_codex_sha256=self.inputs.candidate_codex.executable_sha256,
            runtime_services_identity_sha256=self.inputs.runtime_services.digest,
        )
        write_public_file(
            self.result_root / "public/final_test_suffix_recovery_plan_v3.json",
            canonical_pretty_json_bytes(plan.model_dump(mode="json")),
        )

    def _write_composition_receipt(
        self, frozen: FrozenThreeTargetSetV3
    ) -> dict[str, object]:
        suffix = tuple(
            row
            for row in _read_jsonl(self.private_events)
            if row.get("kind") == "PRIVATE_EVALUATED"
            and row.get("stage") == "EVOLVED_TEST"
        )
        start = self.prefix_audit.start_task_ordinal
        if len(suffix) != TEST_COUNT - start:
            raise SupervisedExperimentV3Error(
                "FINAL_TEST_RECOVERY_COMPLETION_COUNT_INVALID"
            )
        combined = (*self.prefix_audit.parent_private_evaluations, *suffix)
        for ordinal, (row, task) in enumerate(zip(combined, self.inputs.test, strict=True)):
            if row.get("task_ordinal") != ordinal or row.get("task_uid") != task.uid:
                raise SupervisedExperimentV3Error(
                    "FINAL_TEST_RECOVERY_COMPOSITION_ORDER_INVALID"
                )
        payload: dict[str, object] = {
            "schema_version": "FinalTestPrefixSuffixCompositionReceiptV3",
            "recovery_protocol_id": FINAL_TEST_RECOVERY_PROTOCOL_ID,
            "classification": [
                "AUDITED_FINAL_TEST_SUFFIX_RECOVERY",
                "PARENT_COMPLETION_PREFIX_REUSED",
                "INCOMPLETE_PARENT_TASK_REEXECUTED",
                "NOT_A_SINGLE_UNINTERRUPTED_RUN",
            ],
            "parent_run_id": self.prefix_audit.parent_run_id,
            "recovery_run_id": self.run_id,
            "composition_run_id": self.composition_audit.composition_run_id,
            "frozen_artifact_set_sha256": frozen.artifact_set_digest,
            "parent_task_start": 0,
            "parent_task_end": start - 1,
            "recovery_task_start": start,
            "recovery_task_end": TEST_COUNT - 1,
            "task_count": TEST_COUNT,
            "candidate_completion_count": TEST_COUNT,
            "correct_count": sum(bool(row.get("correct")) for row in combined),
            "duplicate_completion_count": 0,
            "reflector_calls": 0,
            "core_jobs": 0,
            "test_feedback_enabled": False,
            "parent_results_or_state_modified": False,
            "parent_ledger_modified": False,
            "uid_order_sha256": v2._uid_order_digest(self.inputs.test),
        }
        path = (
            self.result_root
            / "public/final_test_prefix_suffix_composition_receipt_v3.json"
        )
        write_public_file(path, canonical_pretty_json_bytes(payload))
        return payload

    def _verify_parent_unchanged(self) -> None:
        current_tree = _tree_identity(
            (
                self.prefix_audit.parent_state_root,
                self.prefix_audit.parent_result_root,
            )
        )[0]
        if current_tree != self.prefix_audit.accepted.parent_run_tree_sha256:
            raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_RUN_MUTATED")
        if (
            sha256_bytes(self.prefix_audit.parent_ledger_path.read_bytes())
            != self.prefix_audit.accepted.parent_ledger_sha256
        ):
            raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_LEDGER_MUTATED")


def _load_parent_ledger_read_only(path: Path) -> tuple[FinalTestAttemptV3, ...]:
    result: list[FinalTestAttemptV3] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_LEDGER_UNREADABLE") from exc
    for line in lines:
        if line == "":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_LEDGER_INVALID") from exc
        if (
            type(payload) is not dict
            or set(payload) != _LEDGER_KEYS
            or payload.get("schema_version") != "FinalTestConsumptionLedgerEntryV3"
            or payload.get("arm") != "evolved"
        ):
            raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_LEDGER_INVALID")
        result.append(
            FinalTestAttemptV3(
                **{
                    key: value
                    for key, value in payload.items()
                    if key not in {"schema_version", "arm"}
                }
            )
        )
    return tuple(result)


def _validate_parent_ledger_sequence(
    entries: tuple[FinalTestAttemptV3, ...],
    identity: tuple[str, str, str, str],
) -> dict[str, tuple[FinalTestAttemptV3, ...]]:
    values: dict[str, list[FinalTestAttemptV3]] = {}
    for entry in entries:
        if (
            entry.source_commit,
            entry.artifact_set_digest,
            entry.config_digest,
            entry.model_digest,
        ) != identity:
            raise SupervisedExperimentV3Error("FINAL_TEST_PARENT_LEDGER_IDENTITY_INVALID")
        prior = values.setdefault(entry.task_uid, [])
        if entry.completion_exists:
            if (
                not prior
                or prior[-1].attempt_id != entry.attempt_id
                or prior[-1].completion_exists
                or any(item.completion_exists for item in prior)
            ):
                raise SupervisedExperimentV3Error(
                    "FINAL_TEST_PARENT_LEDGER_SEQUENCE_INVALID"
                )
        elif any(item.attempt_id == entry.attempt_id for item in prior):
            raise SupervisedExperimentV3Error(
                "FINAL_TEST_PARENT_LEDGER_SEQUENCE_INVALID"
            )
        prior.append(entry)
    return {key: tuple(items) for key, items in values.items()}


__all__ = [
    "DEFAULT_PARENT_FINAL_TEST_RUN_ID",
    "FINAL_TEST_RECOVERY_PROTOCOL_ID",
    "AcceptedFinalTestPrefixReceiptV3",
    "ComposedFinalTestSuffixRecoveryV3",
    "DiscardedIncompleteTaskReceiptV3",
    "FinalTestPrefixAuditV3",
    "FinalTestSuffixRecoveryPlanV3",
    "audit_failed_final_test_prefix_v3",
    "build_final_test_recovery_dry_run_v3",
    "build_final_test_recovery_source_compatibility_receipt_v3",
]
