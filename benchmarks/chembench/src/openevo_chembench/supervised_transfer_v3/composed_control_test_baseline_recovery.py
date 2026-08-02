"""Audited suffix recovery for the interrupted v3 Control Test baseline.

The selected parent and every earlier baseline namespace are immutable audit
inputs. A fresh recovery run executes only the ordered Test suffix beginning at
the first UID without a selected-parent completion. Historical overlapping
completions are disclosed and excluded by run identity, never by correctness.
"""

from __future__ import annotations

import math
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
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
from openevo_chembench.supervised_transfer_v2.test_ledger import (
    FinalTestAttemptV2,
    FinalTestConsumptionLedgerV2,
)
from openevo_chembench.supervised_transfer_v3.category_shard_recovery import (
    _read_json,
    _read_jsonl,
    _read_regular,
    _tree_identity,
)
from openevo_chembench.supervised_transfer_v3.composed_control_test_baseline import (
    BASELINE_PROTOCOL_ID,
    GENERATION_ZERO_CONTEXT_SET_SHA256,
    CompletedEvolvedFinalTestAuditV3,
    ComposedControlFinalTestBaselineV3,
    ControlFinalTestBaselineV3Error,
    _require_current_identity,
)
from openevo_chembench.supervised_transfer_v3.config import TEST_COUNT

CONTROL_BASELINE_RECOVERY_PROTOCOL_ID = (
    "supervised_transfer_v3_composed_control_final_test_baseline_suffix_recovery"
)
DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID = (
    "stv3-control-final-test-baseline-dockerfix-20260801T190638Z"
)
EXPECTED_PARENT_FAILURE_CODE = "RUNTIME_SERVICE_PROCESS_IDENTITY_INVALID"
EXPECTED_EXCLUDED_CONTROL_BASELINE_RUN_IDS = (
    "stv3-control-final-test-baseline-20260801T153344Z",
    "stv3-control-final-test-baseline-20260801T161457Z",
    "stv3-control-final-test-baseline-20260801T164401Z",
    "stv3-control-final-test-baseline-20260801T165930Z",
    "stv3-control-final-test-baseline-reconnect-20260801T171812Z",
)
EXPECTED_PARENT_COMPLETION_COUNT = 144
EXPECTED_EXCLUDED_COMPLETION_EVENT_COUNT = 43
EXPECTED_EXCLUDED_UNIQUE_TASK_COUNT = 27
EXPECTED_EXCLUDED_ATTEMPT_EVENT_COUNT = 90
EXPECTED_EXCLUDED_NO_COMPLETION_ATTEMPT_COUNT = 47
EXPECTED_EXCLUDED_PUBLIC_RETRY_EVENT_COUNT = 45
EXPECTED_EXCLUDED_FAILURE_CODES = {
    "stv3-control-final-test-baseline-20260801T153344Z": ("RUNTIME_SERVICE_HEALTH_INVALID"),
    "stv3-control-final-test-baseline-20260801T161457Z": "KEYBOARDINTERRUPT",
    "stv3-control-final-test-baseline-20260801T164401Z": "KEYBOARDINTERRUPT",
    "stv3-control-final-test-baseline-20260801T165930Z": "KEYBOARDINTERRUPT",
    "stv3-control-final-test-baseline-reconnect-20260801T171812Z": "TIMEOUTEXPIRED",
}
_PRE_RUN_ABORT_LOG = (
    "reports/chembench_supervised_transfer_v3/"
    "control_final_test_baseline_"
    "stv3-control-final-test-baseline-20260801T161309Z.log"
)
_LEDGER_KEYS = {
    "schema_version",
    "task_uid",
    "arm",
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
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/experiment.py",
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/managed_codex.py",
    ("benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/runtime_services.py"),
    ("benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/test_ledger.py"),
    (
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "composed_control_test_baseline.py"
    ),
)
_RECOVERY_CHANGE_ALLOWLIST = frozenset(
    {
        (
            "benchmarks/chembench/configs/supervised_transfer_v3/"
            "composed_control_test_baseline_recovery.md"
        ),
        "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json",
        "benchmarks/chembench/scripts/supervised_transfer_v3/main.py",
        (
            "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
            "composed_control_test_baseline_recovery.py"
        ),
        (
            "benchmarks/chembench/tests/supervised_transfer_v3/"
            "test_composed_control_test_baseline_recovery_v3.py"
        ),
        (
            "benchmarks/chembench/tests/supervised_transfer_v3/"
            "test_composed_control_test_baseline_v3.py"
        ),
    }
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class AcceptedControlBaselinePrefixReceiptV3(_ClosedModel):
    schema_version: Literal["AcceptedControlBaselinePrefixReceiptV3"] = (
        "AcceptedControlBaselinePrefixReceiptV3"
    )
    recovery_protocol_id: str = CONTROL_BASELINE_RECOVERY_PROTOCOL_ID
    parent_run_id: str
    parent_source_commit: str
    parent_status: Literal["FAIL_CLOSED"] = "FAIL_CLOSED"
    parent_failure_code: Literal["RUNTIME_SERVICE_PROCESS_IDENTITY_INVALID"] = (
        "RUNTIME_SERVICE_PROCESS_IDENTITY_INVALID"
    )
    parent_run_tree_sha256: str
    parent_run_log_sha256: str
    parent_ledger_sha256: str
    parent_plan_sha256: str
    parent_amendment_sha256: str
    parent_compatibility_sha256: str
    config_digest: str
    split_digest: str
    source_manifest_sha256: str
    test_order_sha256: str
    model_digest: str
    managed_codex_digest: str
    generation_zero_context_set_sha256: str
    task_start_ordinal: Literal[0] = 0
    task_end_ordinal: int = Field(ge=0, lt=TEST_COUNT)
    completion_count: int = Field(gt=0, lt=TEST_COUNT)
    accepted_attempt_count: int = Field(gt=0)
    correct_count: int = Field(ge=0, le=TEST_COUNT)
    completion_uid_order_sha256: str
    selected_without_correctness: Literal[True] = True
    final_prefix_closed: Literal[True] = True
    included_in_final_statistics: Literal[True] = True

    @model_validator(mode="after")
    def _counts_match(self) -> AcceptedControlBaselinePrefixReceiptV3:
        if self.task_end_ordinal + 1 != self.completion_count:
            raise ValueError("accepted control prefix is not contiguous")
        if self.correct_count > self.completion_count:
            raise ValueError("accepted control correctness count is invalid")
        return self


class DiscardedControlBaselineIncompleteTaskReceiptV3(_ClosedModel):
    schema_version: Literal["DiscardedControlBaselineIncompleteTaskReceiptV3"] = (
        "DiscardedControlBaselineIncompleteTaskReceiptV3"
    )
    recovery_protocol_id: str = CONTROL_BASELINE_RECOVERY_PROTOCOL_ID
    parent_run_id: str
    task_ordinal: int = Field(ge=0, lt=TEST_COUNT)
    task_uid: str
    category: str
    incomplete_attempt_count: int = Field(gt=0)
    completion_exists: Literal[False] = False
    included_in_final_statistics: Literal[False] = False
    used_as_recovery_input: Literal[False] = False


class ExcludedHistoricalControlBaselineRunV3(_ClosedModel):
    run_id: str
    source_commit: str
    failure_code: str
    run_tree_sha256: str
    run_log_sha256: str
    completion_count: int = Field(ge=0, lt=TEST_COUNT)
    attempt_event_count: int = Field(ge=0)
    no_completion_attempt_count: int = Field(ge=0)
    public_infrastructure_retry_event_count: int = Field(ge=0)
    completion_uid_order_sha256: str
    overlap_with_selected_parent_count: int = Field(ge=0, lt=TEST_COUNT)
    novel_completion_count: Literal[0] = 0
    included_in_final_statistics: Literal[False] = False
    correctness_used_for_selection: Literal[False] = False

    @model_validator(mode="after")
    def _overlap_matches(self) -> ExcludedHistoricalControlBaselineRunV3:
        if self.overlap_with_selected_parent_count != self.completion_count:
            raise ValueError("historical baseline prefix is not fully overlapped")
        if self.attempt_event_count != (self.completion_count + self.no_completion_attempt_count):
            raise ValueError("historical baseline attempt accounting is invalid")
        if self.public_infrastructure_retry_event_count > self.no_completion_attempt_count:
            raise ValueError("historical baseline retry accounting is invalid")
        return self


class ExcludedHistoricalControlBaselineRunsReceiptV3(_ClosedModel):
    schema_version: Literal["ExcludedHistoricalControlBaselineRunsReceiptV3"] = (
        "ExcludedHistoricalControlBaselineRunsReceiptV3"
    )
    recovery_protocol_id: str = CONTROL_BASELINE_RECOVERY_PROTOCOL_ID
    selected_parent_run_id: str
    selection_rule: Literal["UNIQUE_LONGEST_CONTIGUOUS_PREFIX"] = (
        "UNIQUE_LONGEST_CONTIGUOUS_PREFIX"
    )
    correctness_read_for_selection: Literal[False] = False
    historical_runs: tuple[ExcludedHistoricalControlBaselineRunV3, ...]
    excluded_completion_event_count: int = Field(ge=0)
    excluded_unique_task_count: int = Field(ge=0)
    excluded_attempt_event_count: int = Field(ge=0)
    excluded_no_completion_attempt_count: int = Field(ge=0)
    excluded_public_infrastructure_retry_event_count: int = Field(ge=0)
    all_historical_completions_are_selected_parent_subset: Literal[True] = True
    pre_run_abort_log_sha256: str
    pre_run_abort_failure_code: Literal["PAID_RUNTIME_PYTHON_INVALID"] = (
        "PAID_RUNTIME_PYTHON_INVALID"
    )
    pre_run_abort_model_calls: Literal[0] = 0

    @model_validator(mode="after")
    def _counts_match(self) -> ExcludedHistoricalControlBaselineRunsReceiptV3:
        if self.excluded_completion_event_count != sum(
            item.completion_count for item in self.historical_runs
        ):
            raise ValueError("excluded historical completion count is invalid")
        if self.excluded_attempt_event_count != sum(
            item.attempt_event_count for item in self.historical_runs
        ):
            raise ValueError("excluded historical attempt count is invalid")
        if self.excluded_no_completion_attempt_count != sum(
            item.no_completion_attempt_count for item in self.historical_runs
        ):
            raise ValueError("excluded historical no-completion count is invalid")
        if self.excluded_public_infrastructure_retry_event_count != sum(
            item.public_infrastructure_retry_event_count for item in self.historical_runs
        ):
            raise ValueError("excluded historical retry count is invalid")
        return self


class ControlBaselineSuffixRecoveryPlanV3(_ClosedModel):
    schema_version: Literal["ControlBaselineSuffixRecoveryPlanV3"] = (
        "ControlBaselineSuffixRecoveryPlanV3"
    )
    recovery_protocol_id: str = CONTROL_BASELINE_RECOVERY_PROTOCOL_ID
    recovery_run_id: str
    parent_run_id: str
    parent_prefix_receipt_sha256: str
    discarded_incomplete_task_receipt_sha256: str
    excluded_historical_runs_receipt_sha256: str
    recovery_source_commit: str
    start_task_ordinal: int = Field(ge=0, lt=TEST_COUNT)
    first_category: str
    first_category_task_ordinal: int = Field(ge=0, lt=50)
    remaining_task_count: int = Field(gt=0, le=TEST_COUNT)
    candidate_calls: int = Field(gt=0, le=TEST_COUNT)
    reflector_calls: Literal[0] = 0
    core_jobs: Literal[0] = 0
    context_resolutions: Literal[0] = 0
    typed_artifacts: Literal[0] = 0
    context_mode: Literal["generation_zero"] = "generation_zero"
    context_target_ids: tuple[()] = ()
    context_artifact_ids: tuple[()] = ()
    parent_results_or_state_modified: Literal[False] = False
    parent_ledger_modified: Literal[False] = False
    parent_completion_reexecuted: Literal[False] = False
    historical_completion_reexecuted: Literal[False] = False
    candidate_execution_path: Literal["TaskRequest->Rollout->Gateway->CodexHarness"] = (
        "TaskRequest->Rollout->Gateway->CodexHarness"
    )
    test_feedback_enabled: Literal[False] = False
    candidate_codex_sha256: str
    runtime_services_identity_sha256: str

    @model_validator(mode="after")
    def _remaining_matches(self) -> ControlBaselineSuffixRecoveryPlanV3:
        if (
            self.remaining_task_count != TEST_COUNT - self.start_task_ordinal
            or self.candidate_calls != self.remaining_task_count
        ):
            raise ValueError("control baseline recovery suffix count is inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class ControlBaselinePrefixAuditV3:
    repository_root: Path
    parent_run_id: str
    parent_state_root: Path
    parent_result_root: Path
    parent_ledger_path: Path
    accepted: AcceptedControlBaselinePrefixReceiptV3
    discarded: DiscardedControlBaselineIncompleteTaskReceiptV3
    excluded: ExcludedHistoricalControlBaselineRunsReceiptV3
    parent_private_evaluations: tuple[dict[str, Any], ...]
    protected_trees: tuple[tuple[tuple[Path, ...], str], ...]
    protected_files: tuple[tuple[Path, str], ...]

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
                    "excluded": self.excluded.digest,
                }
            )
        )


def audit_failed_control_baseline_prefix_v3(
    *,
    repository_root: Path,
    inputs: v2.ExperimentInputsV2,
    evolved_audit: CompletedEvolvedFinalTestAuditV3,
    parent_run_id: str = DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID,
) -> ControlBaselinePrefixAuditV3:
    """Validate the selected prefix and all historical overlap without writes."""

    repository = repository_root.resolve(strict=True)
    if inputs.repository_root != repository:
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_REPOSITORY_MISMATCH")
    if parent_run_id != DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID:
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_PARENT_NOT_AUTHORIZED")
    _require_current_identity(inputs, evolved_audit)
    state_base = repository / "state/chembench_supervised_transfer_v3/runs"
    result_base = repository / "results/chembench_supervised_transfer_v3/runs"
    report_base = repository / "reports/chembench_supervised_transfer_v3"
    parent_state_root = state_base / parent_run_id
    parent_result_root = result_base / parent_run_id
    parent_ledger_path = parent_state_root / "private/control_test_ledger_v3.jsonl"
    state = _read_json(parent_state_root / "run_state.json")
    plan_path = (
        parent_result_root / "public/control_final_test_baseline_paid_execution_plan_v3.json"
    )
    amendment_path = parent_result_root / "public/control_final_test_baseline_amendment_v3.json"
    compatibility_path = (
        parent_result_root
        / "public/control_final_test_baseline_source_compatibility_receipt_v3.json"
    )
    plan = _read_json(plan_path)
    amendment = _read_json(amendment_path)
    compatibility = _read_json(compatibility_path)
    _require_parent_state_and_authority(
        state=state,
        plan=plan,
        amendment=amendment,
        compatibility=compatibility,
        parent_run_id=parent_run_id,
        inputs=inputs,
        evolved_audit=evolved_audit,
    )

    private_rows = _read_jsonl(parent_state_root / "private/events.jsonl")
    public_rows = _read_jsonl(parent_result_root / "public/events.jsonl")
    private_evaluated = tuple(
        row
        for row in private_rows
        if row.get("kind") == "PRIVATE_EVALUATED" and row.get("stage") == "CONTROL_TEST"
    )
    private_attempted = tuple(
        row
        for row in private_rows
        if row.get("kind") == "TASK_MODEL_ATTEMPTED" and row.get("stage") == "CONTROL_TEST"
    )
    public_completed = tuple(
        row
        for row in public_rows
        if row.get("kind") == "TASK_MODEL_EXECUTED" and row.get("stage") == "CONTROL_TEST"
    )
    if (
        len(private_evaluated) != EXPECTED_PARENT_COMPLETION_COUNT
        or len(public_completed) != EXPECTED_PARENT_COMPLETION_COUNT
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_COMPLETION_COUNT_INVALID")
    expected_prefix = tuple(inputs.test[:EXPECTED_PARENT_COMPLETION_COUNT])
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
            or private.get("context_artifact_ids") != []
            or public.get("context_artifact_ids") != []
        ):
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_PREFIX_NOT_CONTIGUOUS")

    model_digest = _model_digest(inputs)
    entries = _load_control_ledger_read_only(parent_ledger_path)
    identity = (
        str(state["source_commit"]),
        GENERATION_ZERO_CONTEXT_SET_SHA256,
        inputs.config.digest,
        model_digest,
    )
    by_uid = _validate_control_ledger_sequence(entries, identity)
    expected_uids = tuple(task.uid for task in expected_prefix)
    ledger_completed = tuple(
        task.uid
        for task in inputs.test
        if any(item.completion_exists for item in by_uid.get(task.uid, ()))
    )
    if ledger_completed != expected_uids:
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_LEDGER_PREFIX_INVALID")
    next_task = inputs.test[EXPECTED_PARENT_COMPLETION_COUNT]
    next_attempts = tuple(by_uid.get(next_task.uid, ()))
    if not next_attempts or any(item.completion_exists for item in next_attempts):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_BOUNDARY_INVALID")
    if set(by_uid) != {*expected_uids, next_task.uid}:
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_LEDGER_SUFFIX_PRESENT")
    attempt_entries = tuple(item for item in entries if not item.completion_exists)
    if (
        len(private_attempted) != len(attempt_entries)
        or {str(row.get("session_id")) for row in private_attempted}
        != {item.attempt_id for item in attempt_entries}
        or state.get("task_sessions") != EXPECTED_PARENT_COMPLETION_COUNT
        or state.get("task") != EXPECTED_PARENT_COMPLETION_COUNT - 1
        or state.get("infrastructure_failures")
        != len(attempt_entries) - EXPECTED_PARENT_COMPLETION_COUNT
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_ATTEMPT_ACCOUNTING_INVALID")
    boundary_attempts = tuple(
        row
        for row in private_attempted
        if row.get("task_ordinal") == EXPECTED_PARENT_COMPLETION_COUNT
        and row.get("task_uid") == next_task.uid
    )
    if len(boundary_attempts) != len(next_attempts):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_BOUNDARY_ATTEMPT_INVALID")
    allowed_private_kinds = {"TASK_MODEL_ATTEMPTED", "PRIVATE_EVALUATED"}
    allowed_public_kinds = {
        "TASK_MODEL_EXECUTED",
        "INFRASTRUCTURE_RETRY_NO_COMPLETION",
        "FAIL_CLOSED",
    }
    if (
        any(row.get("kind") not in allowed_private_kinds for row in private_rows)
        or any(row.get("kind") not in allowed_public_kinds for row in public_rows)
        or len([row for row in public_rows if row.get("kind") == "FAIL_CLOSED"]) != 1
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_EVENT_SHAPE_INVALID")

    parent_roots = (parent_state_root, parent_result_root)
    parent_tree_sha256 = _tree_identity(parent_roots)[0]
    parent_log = report_base / f"control_final_test_baseline_{parent_run_id}.log"
    parent_log_sha256 = sha256_bytes(_read_regular(parent_log))
    accepted_attempt_count = sum(
        not item.completion_exists for uid in expected_uids for item in by_uid.get(uid, ())
    )
    accepted = AcceptedControlBaselinePrefixReceiptV3(
        parent_run_id=parent_run_id,
        parent_source_commit=str(state["source_commit"]),
        parent_run_tree_sha256=parent_tree_sha256,
        parent_run_log_sha256=parent_log_sha256,
        parent_ledger_sha256=sha256_bytes(_read_regular(parent_ledger_path)),
        parent_plan_sha256=sha256_bytes(_read_regular(plan_path)),
        parent_amendment_sha256=sha256_bytes(_read_regular(amendment_path)),
        parent_compatibility_sha256=sha256_bytes(_read_regular(compatibility_path)),
        config_digest=inputs.config.digest,
        split_digest=inputs.split_receipt_sha256,
        source_manifest_sha256=str(state["source_manifest_sha256"]),
        test_order_sha256=evolved_audit.test_order_sha256,
        model_digest=model_digest,
        managed_codex_digest=str(state["candidate_codex_executable_sha256"]),
        generation_zero_context_set_sha256=GENERATION_ZERO_CONTEXT_SET_SHA256,
        task_end_ordinal=EXPECTED_PARENT_COMPLETION_COUNT - 1,
        completion_count=EXPECTED_PARENT_COMPLETION_COUNT,
        accepted_attempt_count=accepted_attempt_count,
        correct_count=sum(bool(row.get("correct")) for row in private_evaluated),
        completion_uid_order_sha256=sha256_bytes(canonical_json_bytes(expected_uids)),
    )
    discarded = DiscardedControlBaselineIncompleteTaskReceiptV3(
        parent_run_id=parent_run_id,
        task_ordinal=EXPECTED_PARENT_COMPLETION_COUNT,
        task_uid=next_task.uid,
        category=next_task.category,
        incomplete_attempt_count=len(next_attempts),
    )
    excluded, history_trees, history_logs = _audit_excluded_history(
        repository=repository,
        inputs=inputs,
        selected_parent_run_id=parent_run_id,
        selected_parent_uids=expected_uids,
        state_base=state_base,
        result_base=result_base,
        report_base=report_base,
        test_order_sha256=evolved_audit.test_order_sha256,
        model_digest=model_digest,
        managed_codex_digest=evolved_audit.managed_codex_digest,
    )
    protected_files = (
        (parent_log, parent_log_sha256),
        *history_logs,
        (
            repository / _PRE_RUN_ABORT_LOG,
            excluded.pre_run_abort_log_sha256,
        ),
    )
    return ControlBaselinePrefixAuditV3(
        repository_root=repository,
        parent_run_id=parent_run_id,
        parent_state_root=parent_state_root,
        parent_result_root=parent_result_root,
        parent_ledger_path=parent_ledger_path,
        accepted=accepted,
        discarded=discarded,
        excluded=excluded,
        parent_private_evaluations=private_evaluated,
        protected_trees=((parent_roots, parent_tree_sha256), *history_trees),
        protected_files=protected_files,
    )


def build_control_baseline_recovery_dry_run_v3(
    *,
    inputs: v2.ExperimentInputsV2,
    evolved_audit: CompletedEvolvedFinalTestAuditV3,
    prefix_audit: ControlBaselinePrefixAuditV3,
    recovery_run_id: str,
) -> dict[str, object]:
    _require_current_identity(inputs, evolved_audit)
    _verify_parent_evidence_unchanged(prefix_audit)
    start = prefix_audit.start_task_ordinal
    first = inputs.test[start]
    return {
        "schema_version": "ControlBaselineSuffixRecoveryDryRunV3",
        "recovery_protocol_id": CONTROL_BASELINE_RECOVERY_PROTOCOL_ID,
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
        "planned_context_resolutions": 0,
        "planned_artifacts": 0,
        "context_mode": "generation_zero",
        "context_target_ids": [],
        "context_artifact_ids": [],
        "parent_results_or_state_modified": False,
        "parent_ledger_modified": False,
        "parent_completion_reexecuted": False,
        "excluded_historical_completion_event_count": (
            prefix_audit.excluded.excluded_completion_event_count
        ),
        "excluded_historical_unique_task_count": (
            prefix_audit.excluded.excluded_unique_task_count
        ),
        "excluded_historical_attempt_event_count": (
            prefix_audit.excluded.excluded_attempt_event_count
        ),
        "excluded_historical_no_completion_attempt_count": (
            prefix_audit.excluded.excluded_no_completion_attempt_count
        ),
        "excluded_historical_public_infrastructure_retry_event_count": (
            prefix_audit.excluded.excluded_public_infrastructure_retry_event_count
        ),
        "correctness_used_for_parent_selection": False,
        "model_calls": 0,
    }


def build_control_baseline_recovery_source_compatibility_receipt_v3(
    *,
    inputs: v2.ExperimentInputsV2,
    evolved_audit: CompletedEvolvedFinalTestAuditV3,
    prefix_audit: ControlBaselinePrefixAuditV3,
) -> dict[str, object]:
    _require_current_identity(inputs, evolved_audit)
    _verify_parent_evidence_unchanged(prefix_audit)
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
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_SOURCE_SCOPE_INVALID")
    critical_hashes: dict[str, str] = {}
    for relative in _COMPATIBILITY_CRITICAL_FILES:
        current = (inputs.repository_root / relative).read_bytes()
        parent = _git_bytes(inputs.repository_root, parent_commit, relative)
        if current != parent:
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_RECOVERY_EXECUTION_SEMANTICS_DRIFT"
            )
        critical_hashes[relative] = sha256_bytes(current)
    diff = subprocess.run(
        (
            "git",
            "-C",
            str(inputs.repository_root),
            "diff",
            "--binary",
            f"{parent_commit}..{current_commit}",
            "--",
            *changed,
        ),
        check=True,
        capture_output=True,
    ).stdout
    return {
        "schema_version": "ControlBaselineRecoverySourceCompatibilityReceiptV3",
        "recovery_protocol_id": CONTROL_BASELINE_RECOVERY_PROTOCOL_ID,
        "parent_source_commit": parent_commit,
        "recovery_source_commit": current_commit,
        "files_changed": list(changed),
        "git_diff_sha256": sha256_bytes(diff),
        "critical_execution_files_sha256": critical_hashes,
        "config_digest": inputs.config.digest,
        "split_digest": inputs.split_receipt_sha256,
        "model_digest": _model_digest(inputs),
        "managed_codex_digest": evolved_audit.managed_codex_digest,
        "changes_limited_to_control_suffix_recovery_orchestration": True,
        "candidate_prompt_unchanged": True,
        "candidate_model_unchanged": True,
        "reasoning_effort_unchanged": True,
        "candidate_harness_unchanged": True,
        "runtime_identity_checks_unchanged": True,
        "evaluator_unchanged": True,
        "strict_parser_unchanged": True,
        "test_order_unchanged": True,
        "generation_zero_context_unchanged": True,
        "test_feedback_disabled": True,
        "parent_selection_uses_correctness": False,
        "semantically_compatible": True,
    }


class ComposedControlFinalTestBaselineSuffixRecoveryV3(ComposedControlFinalTestBaselineV3):
    """Run a fresh immutable control suffix and compose it with one parent."""

    def __init__(
        self,
        *,
        inputs: v2.ExperimentInputsV2,
        run_id: str,
        evolved_audit: CompletedEvolvedFinalTestAuditV3,
        prefix_audit: ControlBaselinePrefixAuditV3,
        executor_factory: Any | None = None,
    ) -> None:
        self.prefix_audit = prefix_audit
        super().__init__(
            inputs=inputs,
            run_id=run_id,
            evolved_audit=evolved_audit,
            executor_factory=executor_factory,
        )
        self._state.update(
            {
                "schema_version": "ChemBenchControlBaselineRecoveryRunStateV3",
                "protocol_id": CONTROL_BASELINE_RECOVERY_PROTOCOL_ID,
                "run_mode": "control_test_baseline_suffix_recovery",
                "parent_control_baseline_run_id": prefix_audit.parent_run_id,
                "accepted_parent_completion_count": prefix_audit.start_task_ordinal,
                "recovery_start_task_ordinal": prefix_audit.start_task_ordinal,
                "excluded_historical_completion_event_count": (
                    prefix_audit.excluded.excluded_completion_event_count
                ),
                "excluded_historical_unique_task_count": (
                    prefix_audit.excluded.excluded_unique_task_count
                ),
                "excluded_historical_attempt_event_count": (
                    prefix_audit.excluded.excluded_attempt_event_count
                ),
                "excluded_historical_no_completion_attempt_count": (
                    prefix_audit.excluded.excluded_no_completion_attempt_count
                ),
                "excluded_historical_public_infrastructure_retry_event_count": (
                    prefix_audit.excluded.excluded_public_infrastructure_retry_event_count
                ),
                "parent_results_or_state_modified": False,
                "parent_ledger_modified": False,
                "parent_completion_reexecuted": False,
                "correctness_used_for_parent_selection": False,
            }
        )
        self._write_state()
        self._write_protocol_receipts()

    def run_control_baseline_recovery(self) -> dict[str, object]:
        try:
            self._require_baseline_source_frozen()
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("control"))
                self._set_stage("CONTROL_TEST")
                ledger_summary = self._run_control_test(executor)
            self._require_baseline_source_frozen()
            completion = self._write_composition_receipt(ledger_summary)
            self._set_stage("COMPLETED")
            completion_path = (
                self.result_root / "public/control_final_test_baseline_prefix_suffix_"
                "composition_receipt_v3.json"
            )
            self._state.update(
                {
                    "status": "COMPLETED",
                    "baseline_test_status": ("COMPLETED_BY_PREFIX_SUFFIX_COMPOSITION"),
                    "completed_at_utc": v2.utc_now(),
                    "completion_receipt_sha256": sha256_bytes(completion_path.read_bytes()),
                }
            )
            self._write_state()
            return completion
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def _run_control_test(self, executor: TaskExecutorV2) -> dict[str, object]:
        model_digest = _model_digest(self.inputs)
        if model_digest != self.evolved_audit.model_digest:
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_RECOVERY_MODEL_DIGEST_MISMATCH"
            )
        ledger = FinalTestConsumptionLedgerV2(
            path=(self.state_root / "private/control_test_suffix_ledger_v3.jsonl").resolve(),
            source_commit=self.inputs.source_commit,
            artifact_set_digest=GENERATION_ZERO_CONTEXT_SET_SHA256,
            config_digest=self.inputs.config.digest,
            model_digest=model_digest,
        )
        start = self.prefix_audit.start_task_ordinal
        for ordinal, task in enumerate(self.inputs.test[start:], start=start):
            retry_window_started_at = time.monotonic()
            for attempt_number in range(1, v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT + 1):
                if attempt_number > 1:
                    v2._require_executor_retry_window(retry_window_started_at)
                self._require_baseline_source_frozen()
                attempt_id = self._session_id("CONTROL_TEST", "control", task, 0, attempt_number)
                ledger.claim(
                    task_uid=task.uid,
                    arm="control",
                    attempt_id=attempt_id,
                    timestamp=v2.utc_now(),
                )
                try:
                    self._execute_session(
                        executor,
                        task=task,
                        context=None,
                        logical_arm="test_control",
                        executor_arm="control",
                        task_ordinal=ordinal,
                        round_index=0,
                        stage="CONTROL_TEST",
                        session_id_override=attempt_id,
                        completion_callback=lambda completion, *, _task=task, _attempt=attempt_id: (
                            ledger.complete(
                                task_uid=_task.uid,
                                arm="control",
                                attempt_id=_attempt,
                                completion=completion,
                                timestamp=v2.utc_now(),
                            )
                        ),
                    )
                    break
                except SupervisedTaskExecutionErrorV2 as exc:
                    if exc.completion_exists is True:
                        if exc.completion_sha256 is None:
                            raise ControlFinalTestBaselineV3Error(
                                "CONTROL_BASELINE_RECOVERY_COMPLETION_DIGEST_MISSING"
                            ) from exc
                        ledger.complete_digest(
                            task_uid=task.uid,
                            arm="control",
                            attempt_id=attempt_id,
                            completion_sha256=exc.completion_sha256,
                            timestamp=v2.utc_now(),
                        )
                        raise
                    retryable = (
                        exc.code is SupervisedTaskExecutionCodeV2.TASK_FAILED
                        and exc.completion_exists is False
                        and attempt_number < v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT
                    )
                    if not retryable:
                        raise
                    delay = v2._executor_retry_delay_seconds(
                        retry_window_started_at,
                        v2._FINAL_TEST_INFRASTRUCTURE_RETRY_SECONDS,
                    )
                    self._append_public(
                        {
                            "schema_version": (
                                "ControlBaselineRecoveryInfrastructureRetryPublicV3"
                            ),
                            "kind": "INFRASTRUCTURE_RETRY_NO_COMPLETION",
                            "stage": "CONTROL_TEST",
                            "arm": "control",
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
                raise ControlFinalTestBaselineV3Error(
                    "CONTROL_BASELINE_RECOVERY_INFRASTRUCTURE_RETRY_EXHAUSTED"
                )
        summary = ledger.summary()
        expected = TEST_COUNT - start
        if (
            summary["control_completion_count"] != expected
            or summary["evolved_completion_count"] != 0
        ):
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_LEDGER_INCOMPLETE")
        write_public_file(
            self.result_root / "public/control_test_suffix_ledger_receipt_v3.json",
            canonical_pretty_json_bytes(summary),
        )
        return summary

    def _require_baseline_source_frozen(self) -> None:
        super()._require_baseline_source_frozen()
        _verify_parent_evidence_unchanged(self.prefix_audit)

    def _write_protocol_receipts(self) -> None:
        values = (
            (
                "accepted_control_baseline_prefix_receipt_v3.json",
                self.prefix_audit.accepted,
            ),
            (
                "discarded_control_baseline_incomplete_task_receipt_v3.json",
                self.prefix_audit.discarded,
            ),
            (
                "excluded_historical_control_baseline_runs_receipt_v3.json",
                self.prefix_audit.excluded,
            ),
        )
        for name, model in values:
            write_public_file(
                self.result_root / "public" / name,
                canonical_pretty_json_bytes(model.model_dump(mode="json")),
            )

    def _write_paid_plan(self) -> None:
        start = self.prefix_audit.start_task_ordinal
        first = self.inputs.test[start]
        plan = ControlBaselineSuffixRecoveryPlanV3(
            recovery_run_id=self.run_id,
            parent_run_id=self.prefix_audit.parent_run_id,
            parent_prefix_receipt_sha256=self.prefix_audit.accepted.digest,
            discarded_incomplete_task_receipt_sha256=(self.prefix_audit.discarded.digest),
            excluded_historical_runs_receipt_sha256=(self.prefix_audit.excluded.digest),
            recovery_source_commit=self.inputs.source_commit,
            start_task_ordinal=start,
            first_category=first.category,
            first_category_task_ordinal=start % 50,
            remaining_task_count=TEST_COUNT - start,
            candidate_calls=TEST_COUNT - start,
            candidate_codex_sha256=self.inputs.candidate_codex.executable_sha256,
            runtime_services_identity_sha256=self.inputs.runtime_services.digest,
        )
        write_public_file(
            self.result_root / "public/control_baseline_suffix_recovery_plan_v3.json",
            canonical_pretty_json_bytes(plan.model_dump(mode="json")),
        )

    def _write_amendment(self) -> None:
        payload = {
            "schema_version": "ControlBaselineSuffixRecoveryAmendmentV3",
            "recovery_protocol_id": CONTROL_BASELINE_RECOVERY_PROTOCOL_ID,
            "authorization": "explicit_user_authorization",
            "parent_run_id": self.prefix_audit.parent_run_id,
            "paired_evolved_run_id": self.evolved_audit.run_id,
            "accepted_parent_completion_count": (self.prefix_audit.start_task_ordinal),
            "remaining_task_count": TEST_COUNT - self.prefix_audit.start_task_ordinal,
            "historical_overlapping_completion_event_count": (
                self.prefix_audit.excluded.excluded_completion_event_count
            ),
            "historical_overlapping_unique_task_count": (
                self.prefix_audit.excluded.excluded_unique_task_count
            ),
            "historical_attempt_event_count": (
                self.prefix_audit.excluded.excluded_attempt_event_count
            ),
            "historical_no_completion_attempt_count": (
                self.prefix_audit.excluded.excluded_no_completion_attempt_count
            ),
            "historical_public_infrastructure_retry_event_count": (
                self.prefix_audit.excluded.excluded_public_infrastructure_retry_event_count
            ),
            "correctness_used_for_parent_selection": False,
            "same_test_partition": True,
            "same_test_order": True,
            "same_candidate_model": True,
            "same_reasoning_effort": True,
            "same_candidate_harness": True,
            "same_private_evaluator": True,
            "evolution_artifacts_injected": False,
            "reflector_enabled": False,
            "test_feedback_enabled": False,
            "parent_completion_reexecuted": False,
            "not_a_single_uninterrupted_run": True,
            "strict_single_execution_claim": False,
        }
        write_public_file(
            self.result_root / "public/control_baseline_suffix_recovery_amendment_v3.json",
            canonical_pretty_json_bytes(payload),
        )

    def _write_composition_receipt(self, ledger_summary: dict[str, object]) -> dict[str, object]:
        suffix = tuple(
            row
            for row in _read_jsonl(self.private_events)
            if row.get("kind") == "PRIVATE_EVALUATED" and row.get("stage") == "CONTROL_TEST"
        )
        start = self.prefix_audit.start_task_ordinal
        zero_counters = (
            "reflector_calls",
            "core_jobs",
            "context_resolutions",
            "text_memory_artifacts",
            "skill_artifacts",
            "agent_system_artifacts",
            "security_findings",
            "context_findings",
            "artifact_findings",
        )
        if (
            self._state.get("task_sessions") != TEST_COUNT - start
            or self._state.get("task") != TEST_COUNT - 1
            or any(self._state.get(key) != 0 for key in zero_counters)
        ):
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_COUNTERS_INVALID")
        if len(suffix) != TEST_COUNT - start:
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_RECOVERY_COMPLETION_COUNT_INVALID"
            )
        baseline = (*self.prefix_audit.parent_private_evaluations, *suffix)
        evolved = self.evolved_audit.evolved_evaluations
        counts = {
            "both_correct": 0,
            "baseline_only_correct": 0,
            "evolved_only_correct": 0,
            "both_wrong": 0,
        }
        for ordinal, (control, trained, task) in enumerate(
            zip(baseline, evolved, self.inputs.test, strict=True)
        ):
            if (
                control.get("task_ordinal") != ordinal
                or trained.get("task_ordinal") != ordinal
                or control.get("task_uid") != task.uid
                or trained.get("task_uid") != task.uid
                or control.get("context_artifact_ids") != []
            ):
                raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_PAIRING_INVALID")
            pair = (bool(control.get("correct")), bool(trained.get("correct")))
            if pair == (True, True):
                counts["both_correct"] += 1
            elif pair == (True, False):
                counts["baseline_only_correct"] += 1
            elif pair == (False, True):
                counts["evolved_only_correct"] += 1
            else:
                counts["both_wrong"] += 1
        categories: list[dict[str, object]] = []
        for category in CHEMBENCH4K_CATEGORIES:
            control_rows = [row for row in baseline if row.get("category") == category]
            evolved_rows = [row for row in evolved if row.get("category") == category]
            if len(control_rows) != 50 or len(evolved_rows) != 50:
                raise ControlFinalTestBaselineV3Error(
                    "CONTROL_BASELINE_RECOVERY_CATEGORY_COUNT_INVALID"
                )
            categories.append(
                {
                    "category": category,
                    "task_count": 50,
                    "baseline_correct": sum(bool(row.get("correct")) for row in control_rows),
                    "evolved_correct": sum(bool(row.get("correct")) for row in evolved_rows),
                }
            )
        baseline_correct = sum(bool(row.get("correct")) for row in baseline)
        discordant = counts["baseline_only_correct"] + counts["evolved_only_correct"]
        smaller = min(counts["baseline_only_correct"], counts["evolved_only_correct"])
        paired_exact_p = min(
            1.0,
            2
            * sum(math.comb(discordant, value) for value in range(smaller + 1))
            / (2**discordant),
        )
        payload: dict[str, object] = {
            "schema_version": ("ControlBaselinePrefixSuffixCompositionReceiptV3"),
            "recovery_protocol_id": CONTROL_BASELINE_RECOVERY_PROTOCOL_ID,
            "classification": [
                "AUDITED_CONTROL_BASELINE_SUFFIX_RECOVERY",
                "PARENT_COMPLETION_PREFIX_REUSED",
                "HISTORICAL_OVERLAPPING_COMPLETIONS_DISCLOSED",
                "NO_EVOLUTION_ARTIFACTS",
                "NOT_A_SINGLE_UNINTERRUPTED_RUN",
                "DESCRIPTIVE_RECOVERY_NOT_STRICT_SINGLE_EXECUTION",
            ],
            "parent_run_id": self.prefix_audit.parent_run_id,
            "recovery_run_id": self.run_id,
            "paired_evolved_run_id": self.evolved_audit.run_id,
            "parent_task_start": 0,
            "parent_task_end": start - 1,
            "recovery_task_start": start,
            "recovery_task_end": TEST_COUNT - 1,
            "task_count": TEST_COUNT,
            "candidate_completion_count": TEST_COUNT,
            "composed_sample_duplicate_completion_count": 0,
            "historical_excluded_completion_event_count": (
                self.prefix_audit.excluded.excluded_completion_event_count
            ),
            "historical_excluded_unique_task_count": (
                self.prefix_audit.excluded.excluded_unique_task_count
            ),
            "historical_excluded_attempt_event_count": (
                self.prefix_audit.excluded.excluded_attempt_event_count
            ),
            "historical_excluded_no_completion_attempt_count": (
                self.prefix_audit.excluded.excluded_no_completion_attempt_count
            ),
            "historical_excluded_public_infrastructure_retry_event_count": (
                self.prefix_audit.excluded.excluded_public_infrastructure_retry_event_count
            ),
            "parent_accepted_attempt_count": (self.prefix_audit.accepted.accepted_attempt_count),
            "parent_discarded_incomplete_attempt_count": (
                self.prefix_audit.discarded.incomplete_attempt_count
            ),
            "recovery_attempt_count": ledger_summary["attempt_count"],
            "baseline_correct_count": baseline_correct,
            "baseline_accuracy": baseline_correct / TEST_COUNT,
            "evolved_correct_count": self.evolved_audit.correct_count,
            "evolved_accuracy": self.evolved_audit.correct_count / TEST_COUNT,
            "evolved_minus_baseline_percentage_points": 100
            * (self.evolved_audit.correct_count - baseline_correct)
            / TEST_COUNT,
            **counts,
            "paired_mcnemar_exact_p": paired_exact_p,
            "categories": categories,
            "reflector_calls": 0,
            "core_jobs": 0,
            "context_resolutions": 0,
            "typed_artifacts": 0,
            "context_target_ids": [],
            "context_artifact_ids": [],
            "test_feedback_enabled": False,
            "correctness_used_for_parent_selection": False,
            "parent_results_or_state_modified": False,
            "parent_ledger_modified": False,
            "managed_codex_digest": self.inputs.candidate_codex.executable_sha256,
            "test_order_sha256": self.evolved_audit.test_order_sha256,
            "paired_evolved_evidence_unchanged": True,
        }
        path = (
            self.result_root / "public/control_final_test_baseline_prefix_suffix_"
            "composition_receipt_v3.json"
        )
        write_public_file(path, canonical_pretty_json_bytes(payload))
        return payload


def _require_parent_state_and_authority(
    *,
    state: dict[str, Any],
    plan: dict[str, Any],
    amendment: dict[str, Any],
    compatibility: dict[str, Any],
    parent_run_id: str,
    inputs: v2.ExperimentInputsV2,
    evolved_audit: CompletedEvolvedFinalTestAuditV3,
) -> None:
    zero_counters = (
        "reflector_calls",
        "core_jobs",
        "context_resolutions",
        "text_memory_artifacts",
        "skill_artifacts",
        "agent_system_artifacts",
        "security_findings",
        "context_findings",
        "artifact_findings",
    )
    if (
        state.get("schema_version") != "ChemBenchControlFinalTestBaselineRunStateV3"
        or state.get("protocol_id") != BASELINE_PROTOCOL_ID
        or state.get("run_id") != parent_run_id
        or state.get("status") != "FAIL_CLOSED"
        or state.get("stage") != "CONTROL_TEST"
        or state.get("failure_code") != EXPECTED_PARENT_FAILURE_CODE
        or state.get("context_mode") != "generation_zero"
        or state.get("generation_zero_context_set_sha256") != GENERATION_ZERO_CONTEXT_SET_SHA256
        or state.get("parent_artifacts_imported") is not False
        or state.get("parent_databases_imported") is not False
        or state.get("reflector_enabled") is not False
        or any(state.get(key) != 0 for key in zero_counters)
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_STATE_INVALID")
    if (
        state.get("config_sha256") != inputs.config.digest
        or state.get("split_receipt_sha256") != inputs.split_receipt_sha256
        or state.get("paired_evolved_run_id") != evolved_audit.run_id
        or state.get("paired_evolved_audit_sha256") != evolved_audit.digest
        or state.get("candidate_codex_executable_sha256") != evolved_audit.managed_codex_digest
        or state.get("managed_runtime_image")
        != MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_IDENTITY_INVALID")
    if (
        plan.get("schema_version") != "ControlFinalTestBaselinePaidExecutionPlanV3"
        or plan.get("run_id") != parent_run_id
        or plan.get("source_commit") != state.get("source_commit")
        or plan.get("candidate_calls") != TEST_COUNT
        or plan.get("test_order_sha256") != evolved_audit.test_order_sha256
        or plan.get("context_mode") != "generation_zero"
        or plan.get("context_target_ids") != []
        or plan.get("context_artifact_ids") != []
        or plan.get("reflector_calls") != 0
        or plan.get("core_jobs") != 0
        or plan.get("context_resolutions") != 0
        or plan.get("typed_artifacts") != 0
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_PLAN_INVALID")
    if (
        amendment.get("schema_version") != "ControlFinalTestBaselineAmendmentV3"
        or amendment.get("test_feedback_enabled") is not False
        or amendment.get("evolution_artifacts_injected") is not False
        or amendment.get("reflector_enabled") is not False
        or amendment.get("one_valid_completion_per_task") is not True
        or compatibility.get("semantically_compatible") is not True
        or compatibility.get("baseline_source_commit") != state.get("source_commit")
        or compatibility.get("config_digest") != inputs.config.digest
        or compatibility.get("split_digest") != inputs.split_receipt_sha256
        or compatibility.get("managed_codex_digest") != evolved_audit.managed_codex_digest
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_AUTHORITY_INVALID")


def _audit_excluded_history(
    *,
    repository: Path,
    inputs: v2.ExperimentInputsV2,
    selected_parent_run_id: str,
    selected_parent_uids: tuple[str, ...],
    state_base: Path,
    result_base: Path,
    report_base: Path,
    test_order_sha256: str,
    model_digest: str,
    managed_codex_digest: str,
) -> tuple[
    ExcludedHistoricalControlBaselineRunsReceiptV3,
    tuple[tuple[tuple[Path, ...], str], ...],
    tuple[tuple[Path, str], ...],
]:
    discovered: set[str] = set()
    for state_path in state_base.glob("*/run_state.json"):
        state = _read_json(state_path)
        if state.get("protocol_id") == BASELINE_PROTOCOL_ID:
            discovered.add(str(state.get("run_id")))
    expected = {
        selected_parent_run_id,
        *EXPECTED_EXCLUDED_CONTROL_BASELINE_RUN_IDS,
    }
    if discovered != expected:
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_HISTORICAL_NAMESPACE_SET_INVALID")
    selected_uid_set = set(selected_parent_uids)
    historical: list[ExcludedHistoricalControlBaselineRunV3] = []
    unique_uids: set[str] = set()
    protected_trees: list[tuple[tuple[Path, ...], str]] = []
    protected_logs: list[tuple[Path, str]] = []
    for run_id in EXPECTED_EXCLUDED_CONTROL_BASELINE_RUN_IDS:
        state_root = state_base / run_id
        result_root = result_base / run_id
        state = _read_json(state_root / "run_state.json")
        plan = _read_json(
            result_root / "public/control_final_test_baseline_paid_execution_plan_v3.json"
        )
        amendment = _read_json(
            result_root / "public/control_final_test_baseline_amendment_v3.json"
        )
        compatibility = _read_json(
            result_root / "public/control_final_test_baseline_source_compatibility_receipt_v3.json"
        )
        if (
            state.get("run_id") != run_id
            or state.get("status") != "FAIL_CLOSED"
            or state.get("stage") != "CONTROL_TEST"
            or state.get("protocol_id") != BASELINE_PROTOCOL_ID
            or state.get("failure_code") != EXPECTED_EXCLUDED_FAILURE_CODES[run_id]
            or state.get("config_sha256") != inputs.config.digest
            or state.get("split_receipt_sha256") != inputs.split_receipt_sha256
            or state.get("candidate_codex_executable_sha256") != managed_codex_digest
            or state.get("managed_runtime_image")
            != MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id
            or any(
                state.get(key) != 0
                for key in (
                    "reflector_calls",
                    "core_jobs",
                    "context_resolutions",
                    "text_memory_artifacts",
                    "skill_artifacts",
                    "agent_system_artifacts",
                    "security_findings",
                    "context_findings",
                    "artifact_findings",
                )
            )
            or plan.get("run_id") != run_id
            or plan.get("source_commit") != state.get("source_commit")
            or plan.get("candidate_calls") != TEST_COUNT
            or plan.get("test_order_sha256") != test_order_sha256
            or plan.get("config_sha256") != inputs.config.digest
            or plan.get("context_mode") != "generation_zero"
            or plan.get("context_artifact_ids") != []
            or plan.get("context_target_ids") != []
            or amendment.get("test_feedback_enabled") is not False
            or amendment.get("evolution_artifacts_injected") is not False
            or amendment.get("one_valid_completion_per_task") is not True
            or compatibility.get("semantically_compatible") is not True
            or compatibility.get("baseline_source_commit") != state.get("source_commit")
            or compatibility.get("config_digest") != inputs.config.digest
            or compatibility.get("split_digest") != inputs.split_receipt_sha256
            or compatibility.get("model_digest") != model_digest
            or compatibility.get("managed_codex_digest") != managed_codex_digest
        ):
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_HISTORICAL_AUTHORITY_INVALID")
        private_rows = _read_jsonl(state_root / "private/events.jsonl")
        public_rows = _read_jsonl(result_root / "public/events.jsonl")
        private_evaluated = tuple(
            row
            for row in private_rows
            if row.get("kind") == "PRIVATE_EVALUATED" and row.get("stage") == "CONTROL_TEST"
        )
        private_attempted = tuple(
            row
            for row in private_rows
            if row.get("kind") == "TASK_MODEL_ATTEMPTED" and row.get("stage") == "CONTROL_TEST"
        )
        public_completed = tuple(
            row
            for row in public_rows
            if row.get("kind") == "TASK_MODEL_EXECUTED" and row.get("stage") == "CONTROL_TEST"
        )
        public_retries = tuple(
            row
            for row in public_rows
            if row.get("kind") == "INFRASTRUCTURE_RETRY_NO_COMPLETION"
            and row.get("stage") == "CONTROL_TEST"
        )
        if len(private_evaluated) != len(public_completed):
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_HISTORICAL_COMPLETION_COUNT_INVALID"
            )
        expected_prefix = tuple(inputs.test[: len(private_evaluated)])
        for ordinal, (private, public, task) in enumerate(
            zip(private_evaluated, public_completed, expected_prefix, strict=True)
        ):
            if (
                private.get("task_ordinal") != ordinal
                or public.get("task_ordinal") != ordinal
                or private.get("task_uid") != task.uid
                or public.get("task_uid") != task.uid
                or private.get("session_id") != public.get("session_id")
                or private.get("context_artifact_ids") != []
                or public.get("context_artifact_ids") != []
            ):
                raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_HISTORICAL_PREFIX_INVALID")
        uids = tuple(str(row["task_uid"]) for row in private_evaluated)
        if len(uids) >= len(selected_parent_uids) or not set(uids).issubset(selected_uid_set):
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_SELECTED_PARENT_NOT_UNIQUE_LONGEST"
            )
        ledger_path = state_root / "private/control_test_ledger_v3.jsonl"
        ledger_entries = _load_control_ledger_read_only(ledger_path)
        by_uid = _validate_control_ledger_sequence(
            ledger_entries,
            (
                str(state["source_commit"]),
                GENERATION_ZERO_CONTEXT_SET_SHA256,
                inputs.config.digest,
                model_digest,
            ),
        )
        completed_uids = tuple(
            task.uid
            for task in inputs.test
            if any(item.completion_exists for item in by_uid.get(task.uid, ()))
        )
        if completed_uids != uids:
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_HISTORICAL_LEDGER_INVALID")
        allowed_uids = set(uids)
        if len(uids) < TEST_COUNT:
            allowed_uids.add(inputs.test[len(uids)].uid)
        if not set(by_uid).issubset(allowed_uids):
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_HISTORICAL_LEDGER_SUFFIX_PRESENT"
            )
        attempt_entries = tuple(item for item in ledger_entries if not item.completion_exists)
        expected_task_ordinal = len(uids) - 1 if uids else None
        allowed_private_kinds = {"TASK_MODEL_ATTEMPTED", "PRIVATE_EVALUATED"}
        allowed_public_kinds = {
            "TASK_MODEL_EXECUTED",
            "INFRASTRUCTURE_RETRY_NO_COMPLETION",
            "FAIL_CLOSED",
        }
        if (
            len(private_attempted) != len(attempt_entries)
            or len({str(row.get("session_id")) for row in private_attempted})
            != len(private_attempted)
            or {str(row.get("session_id")) for row in private_attempted}
            != {item.attempt_id for item in attempt_entries}
            or state.get("task_sessions") != len(uids)
            or state.get("task") != expected_task_ordinal
            or state.get("infrastructure_failures") != len(attempt_entries) - len(uids)
            or len(public_retries) > len(attempt_entries) - len(uids)
            or any(row.get("kind") not in allowed_private_kinds for row in private_rows)
            or any(row.get("kind") not in allowed_public_kinds for row in public_rows)
            or len([row for row in public_rows if row.get("kind") == "FAIL_CLOSED"]) != 1
        ):
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_HISTORICAL_ATTEMPT_ACCOUNTING_INVALID"
            )
        roots = (state_root, result_root)
        tree_sha256 = _tree_identity(roots)[0]
        log_path = report_base / f"control_final_test_baseline_{run_id}.log"
        log_sha256 = sha256_bytes(_read_regular(log_path))
        historical.append(
            ExcludedHistoricalControlBaselineRunV3(
                run_id=run_id,
                source_commit=str(state["source_commit"]),
                failure_code=str(state["failure_code"]),
                run_tree_sha256=tree_sha256,
                run_log_sha256=log_sha256,
                completion_count=len(uids),
                attempt_event_count=len(attempt_entries),
                no_completion_attempt_count=len(attempt_entries) - len(uids),
                public_infrastructure_retry_event_count=len(public_retries),
                completion_uid_order_sha256=sha256_bytes(canonical_json_bytes(uids)),
                overlap_with_selected_parent_count=len(uids),
            )
        )
        unique_uids.update(uids)
        protected_trees.append((roots, tree_sha256))
        protected_logs.append((log_path, log_sha256))
    if (
        sum(item.completion_count for item in historical)
        != EXPECTED_EXCLUDED_COMPLETION_EVENT_COUNT
        or len(unique_uids) != EXPECTED_EXCLUDED_UNIQUE_TASK_COUNT
        or sum(item.attempt_event_count for item in historical)
        != EXPECTED_EXCLUDED_ATTEMPT_EVENT_COUNT
        or sum(item.no_completion_attempt_count for item in historical)
        != EXPECTED_EXCLUDED_NO_COMPLETION_ATTEMPT_COUNT
        or sum(item.public_infrastructure_retry_event_count for item in historical)
        != EXPECTED_EXCLUDED_PUBLIC_RETRY_EVENT_COUNT
    ):
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_HISTORICAL_OVERLAP_COUNT_INVALID")
    pre_run_abort = repository / _PRE_RUN_ABORT_LOG
    pre_run_abort_bytes = _read_regular(pre_run_abort)
    if b"PAID_RUNTIME_PYTHON_INVALID" not in pre_run_abort_bytes:
        raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PRE_RUN_ABORT_EVIDENCE_INVALID")
    receipt = ExcludedHistoricalControlBaselineRunsReceiptV3(
        selected_parent_run_id=selected_parent_run_id,
        historical_runs=tuple(historical),
        excluded_completion_event_count=EXPECTED_EXCLUDED_COMPLETION_EVENT_COUNT,
        excluded_unique_task_count=EXPECTED_EXCLUDED_UNIQUE_TASK_COUNT,
        excluded_attempt_event_count=EXPECTED_EXCLUDED_ATTEMPT_EVENT_COUNT,
        excluded_no_completion_attempt_count=(EXPECTED_EXCLUDED_NO_COMPLETION_ATTEMPT_COUNT),
        excluded_public_infrastructure_retry_event_count=(
            EXPECTED_EXCLUDED_PUBLIC_RETRY_EVENT_COUNT
        ),
        pre_run_abort_log_sha256=sha256_bytes(pre_run_abort_bytes),
    )
    return receipt, tuple(protected_trees), tuple(protected_logs)


def _load_control_ledger_read_only(path: Path) -> tuple[FinalTestAttemptV2, ...]:
    rows = _read_jsonl(path)
    entries: list[FinalTestAttemptV2] = []
    for payload in rows:
        if (
            set(payload) != _LEDGER_KEYS
            or payload.get("schema_version") != "FinalTestConsumptionLedgerEntryV2"
            or payload.get("arm") != "control"
        ):
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_PARENT_LEDGER_INVALID")
        try:
            entries.append(
                FinalTestAttemptV2(
                    **{key: value for key, value in payload.items() if key != "schema_version"}
                )
            )
        except (TypeError, ValueError) as exc:
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_PARENT_LEDGER_INVALID"
            ) from exc
    return tuple(entries)


def _validate_control_ledger_sequence(
    entries: tuple[FinalTestAttemptV2, ...],
    identity: tuple[str, str, str, str],
) -> dict[str, tuple[FinalTestAttemptV2, ...]]:
    values: dict[str, list[FinalTestAttemptV2]] = {}
    for entry in entries:
        if (
            entry.source_commit,
            entry.artifact_set_digest,
            entry.config_digest,
            entry.model_digest,
        ) != identity:
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_PARENT_LEDGER_IDENTITY_INVALID"
            )
        prior = values.setdefault(entry.task_uid, [])
        if entry.completion_exists:
            if (
                not prior
                or prior[-1].attempt_id != entry.attempt_id
                or prior[-1].completion_exists
                or any(item.completion_exists for item in prior)
            ):
                raise ControlFinalTestBaselineV3Error(
                    "CONTROL_BASELINE_PARENT_LEDGER_SEQUENCE_INVALID"
                )
        elif any(item.attempt_id == entry.attempt_id for item in prior):
            raise ControlFinalTestBaselineV3Error(
                "CONTROL_BASELINE_PARENT_LEDGER_SEQUENCE_INVALID"
            )
        prior.append(entry)
    return {key: tuple(items) for key, items in values.items()}


def _verify_parent_evidence_unchanged(
    audit: ControlBaselinePrefixAuditV3,
) -> None:
    for roots, expected in audit.protected_trees:
        if _tree_identity(roots)[0] != expected:
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_PARENT_TREE_MUTATED")
    for path, expected in audit.protected_files:
        if sha256_bytes(_read_regular(path)) != expected:
            raise ControlFinalTestBaselineV3Error("CONTROL_BASELINE_RECOVERY_PARENT_FILE_MUTATED")


def _model_digest(inputs: v2.ExperimentInputsV2) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "model": inputs.config.model,
                "reasoning_effort": inputs.config.reasoning_effort,
                "managed_runtime_image": MANAGED_RUNTIME_RELEASES[
                    "managed_science"
                ].loaded_image_id,
            }
        )
    )


def _git_bytes(repository: Path, commit: str, relative: str) -> bytes:
    return subprocess.run(
        ("git", "-C", str(repository), "show", f"{commit}:{relative}"),
        check=True,
        capture_output=True,
    ).stdout


__all__ = [
    "CONTROL_BASELINE_RECOVERY_PROTOCOL_ID",
    "DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID",
    "AcceptedControlBaselinePrefixReceiptV3",
    "ComposedControlFinalTestBaselineSuffixRecoveryV3",
    "ControlBaselinePrefixAuditV3",
    "ControlBaselineSuffixRecoveryPlanV3",
    "DiscardedControlBaselineIncompleteTaskReceiptV3",
    "ExcludedHistoricalControlBaselineRunsReceiptV3",
    "audit_failed_control_baseline_prefix_v3",
    "build_control_baseline_recovery_dry_run_v3",
    "build_control_baseline_recovery_source_compatibility_receipt_v3",
]
