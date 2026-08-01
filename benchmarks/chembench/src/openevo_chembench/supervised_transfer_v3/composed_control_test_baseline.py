"""Paired no-evolution Control Test baseline for completed v3 Final Test."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    PrivateChemBench4KTask,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2 import experiment as v2
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedManagedCodexExecutorV2,
    SupervisedTaskExecutionCodeV2,
    SupervisedTaskExecutionErrorV2,
)
from openevo_chembench.supervised_transfer_v2.experiment import (
    ExperimentInputsV2,
    TaskExecutorV2,
)
from openevo_chembench.supervised_transfer_v2.test_ledger import (
    FinalTestConsumptionLedgerV2,
)
from openevo_chembench.supervised_transfer_v3.config import TEST_COUNT
from openevo_chembench.supervised_transfer_v3.source_identity import (
    verify_source_manifest_v3,
)

BASELINE_PROTOCOL_ID = "supervised_transfer_v3_composed_control_final_test_baseline"
EVOLVED_FINAL_TEST_RUN_ID = "stv3-composed-final-test-recovery-20260801T031645Z"
EVOLVED_FINAL_TEST_PARENT_RUN_ID = "stv3-composed-final-test-20260731T155440Z"
EXPECTED_MANAGED_CODEX_SHA256 = (
    "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
)
GENERATION_ZERO_CONTEXT_SET_SHA256 = sha256_bytes(
    canonical_json_bytes(
        {
            "context_mode": "generation_zero",
            "target_ids": [],
            "artifact_ids": [],
            "workspace_uploaded": False,
        }
    )
)
_FINAL_COMPATIBILITY_RECEIPT = (
    "public/final_test_recovery_source_compatibility_receipt_v3.json"
)
_FINAL_COMPOSITION_RECEIPT = (
    "public/final_test_prefix_suffix_composition_receipt_v3.json"
)
_EXECUTOR_SOURCE = (
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/executor.py"
)
_EVOLVED_EXECUTOR_SHA256 = (
    "0230a650f5a5f0854d113626574cefcf8c66fba87e3f3b60d6a51d5c825aa798"
)
_NETWORK_RECOVERY_EXECUTOR_SHA256 = (
    "eb5d6309cc47285c3af56ef3ab9ce0853cad06894fa7f7d1cf518cd2b32598bc"
)
_RUNTIME_SERVICES_SOURCE = (
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/"
    "runtime_services.py"
)
_PRE_DOCKER_INSPECT_RECOVERY_RUNTIME_SERVICES_SHA256 = (
    "be52736a684360e53c1ae1fc7db8184f5029da2c769a0b32a7f19a9be54c99e9"
)
_DOCKER_INSPECT_RECOVERY_RUNTIME_SERVICES_SHA256 = (
    "afdcb4fee4d219ea2bbbe418a7a083ac49b6977987d0343b50b346bfaf3f015e"
)
_ALLOWED_BASELINE_SOURCE_CHANGES = {
    (
        "benchmarks/chembench/configs/supervised_transfer_v3/"
        "composed_control_test_baseline.md"
    ),
    "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json",
    "benchmarks/chembench/scripts/supervised_transfer_v3/main.py",
    _EXECUTOR_SOURCE,
    _RUNTIME_SERVICES_SOURCE,
    (
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "composed_control_test_baseline.py"
    ),
    (
        "benchmarks/chembench/tests/supervised_transfer_v3/"
        "test_composed_control_test_baseline_v3.py"
    ),
    (
        "benchmarks/chembench/tests/supervised_transfer_v2/"
        "test_executor_v2.py"
    ),
    (
        "benchmarks/chembench/tests/supervised_transfer_v2/"
        "test_runtime_services_v2.py"
    ),
}


class ControlFinalTestBaselineV3Error(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class CompletedEvolvedFinalTestAuditV3:
    repository_root: Path
    run_id: str
    parent_run_id: str
    completion_receipt_sha256: str
    compatibility_receipt_sha256: str
    test_order_sha256: str
    config_digest: str
    split_digest: str
    model_digest: str
    managed_codex_digest: str
    frozen_artifact_set_sha256: str
    correct_count: int
    protected_files_sha256: tuple[tuple[str, str], ...]
    evolved_evaluations: tuple[dict[str, object], ...]

    @property
    def digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "run_id": self.run_id,
                    "parent_run_id": self.parent_run_id,
                    "completion_receipt_sha256": self.completion_receipt_sha256,
                    "compatibility_receipt_sha256": self.compatibility_receipt_sha256,
                    "test_order_sha256": self.test_order_sha256,
                    "config_digest": self.config_digest,
                    "split_digest": self.split_digest,
                    "model_digest": self.model_digest,
                    "managed_codex_digest": self.managed_codex_digest,
                    "frozen_artifact_set_sha256": self.frozen_artifact_set_sha256,
                    "correct_count": self.correct_count,
                    "protected_files_sha256": dict(self.protected_files_sha256),
                }
            )
        )


def audit_completed_evolved_final_test_v3(
    *,
    repository_root: Path,
    inputs: ExperimentInputsV2,
    run_id: str = EVOLVED_FINAL_TEST_RUN_ID,
) -> CompletedEvolvedFinalTestAuditV3:
    repository = repository_root.resolve(strict=True)
    if inputs.repository_root != repository:
        raise ControlFinalTestBaselineV3Error("BASELINE_REPOSITORY_IDENTITY_INVALID")
    state_base = repository / "state/chembench_supervised_transfer_v3/runs"
    result_base = repository / "results/chembench_supervised_transfer_v3/runs"
    result_root = result_base / run_id
    state_root = state_base / run_id
    completion_path = result_root / _FINAL_COMPOSITION_RECEIPT
    compatibility_path = result_root / _FINAL_COMPATIBILITY_RECEIPT
    completion = _read_json(completion_path)
    compatibility = _read_json(compatibility_path)
    state = _read_json(state_root / "run_state.json")
    expected_completion_keys = {
        "schema_version",
        "recovery_protocol_id",
        "classification",
        "parent_run_id",
        "recovery_run_id",
        "composition_run_id",
        "frozen_artifact_set_sha256",
        "parent_task_start",
        "parent_task_end",
        "recovery_task_start",
        "recovery_task_end",
        "task_count",
        "candidate_completion_count",
        "correct_count",
        "duplicate_completion_count",
        "reflector_calls",
        "core_jobs",
        "test_feedback_enabled",
        "parent_results_or_state_modified",
        "parent_ledger_modified",
        "uid_order_sha256",
    }
    if (
        set(completion) != expected_completion_keys
        or completion.get("schema_version")
        != "FinalTestPrefixSuffixCompositionReceiptV3"
        or completion.get("recovery_run_id") != run_id
        or completion.get("task_count") != TEST_COUNT
        or completion.get("candidate_completion_count") != TEST_COUNT
        or completion.get("duplicate_completion_count") != 0
        or completion.get("reflector_calls") != 0
        or completion.get("core_jobs") != 0
        or completion.get("test_feedback_enabled") is not False
        or completion.get("parent_results_or_state_modified") is not False
        or completion.get("parent_ledger_modified") is not False
    ):
        raise ControlFinalTestBaselineV3Error("EVOLVED_FINAL_TEST_RECEIPT_INVALID")
    if (
        state.get("status") != "COMPLETED"
        or state.get("stage") != "COMPLETED"
        or state.get("final_test_status")
        != "COMPLETED_BY_PREFIX_SUFFIX_COMPOSITION"
        or state.get("security_findings") != 0
        or state.get("context_findings") != 0
        or state.get("artifact_findings") != 0
    ):
        raise ControlFinalTestBaselineV3Error("EVOLVED_FINAL_TEST_NOT_CLOSED")
    if (
        compatibility.get("semantically_compatible") is not True
        or compatibility.get("config_digest") != inputs.config.digest
        or compatibility.get("split_digest") != inputs.split_receipt_sha256
        or compatibility.get("managed_codex_digest")
        != EXPECTED_MANAGED_CODEX_SHA256
    ):
        raise ControlFinalTestBaselineV3Error("EVOLVED_FINAL_TEST_IDENTITY_INVALID")
    test_order = v2._uid_order_digest(inputs.test)
    if completion.get("uid_order_sha256") != test_order:
        raise ControlFinalTestBaselineV3Error("BASELINE_TEST_ORDER_MISMATCH")
    parent_run_id = str(completion["parent_run_id"])
    parent_state_root = state_base / parent_run_id
    parent_result_root = result_base / parent_run_id
    parent_rows = _read_jsonl(parent_state_root / "private/events.jsonl")
    recovery_rows = _read_jsonl(state_root / "private/events.jsonl")
    evolved = [
        row
        for row in parent_rows
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("stage") == "EVOLVED_TEST"
        and 0 <= int(row.get("task_ordinal", -1)) <= 157
    ] + [
        row
        for row in recovery_rows
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("stage") == "EVOLVED_TEST"
        and 158 <= int(row.get("task_ordinal", -1)) < TEST_COUNT
    ]
    evolved.sort(key=lambda row: int(row["task_ordinal"]))
    if len(evolved) != TEST_COUNT:
        raise ControlFinalTestBaselineV3Error("EVOLVED_FINAL_TEST_EVENTS_INCOMPLETE")
    for ordinal, (row, task) in enumerate(zip(evolved, inputs.test, strict=True)):
        if row.get("task_ordinal") != ordinal or row.get("task_uid") != task.uid:
            raise ControlFinalTestBaselineV3Error("EVOLVED_FINAL_TEST_EVENT_ORDER_INVALID")
    correct_count = sum(bool(row.get("correct")) for row in evolved)
    if correct_count != completion.get("correct_count"):
        raise ControlFinalTestBaselineV3Error("EVOLVED_FINAL_TEST_CORRECTNESS_INVALID")
    protected = (
        completion_path,
        compatibility_path,
        state_root / "run_state.json",
        state_root / "private/events.jsonl",
        state_root / "private/final_test_suffix_ledger_v3.jsonl",
        parent_state_root / "run_state.json",
        parent_state_root / "private/events.jsonl",
        parent_result_root / "public/frozen_three_target_transfer_receipt_v3.json",
        repository
        / "state/chembench_supervised_transfer_v3/private/"
        "final_test_consumption_ledger_v3.jsonl",
    )
    protected_hashes = tuple(
        (path.relative_to(repository).as_posix(), sha256_bytes(path.read_bytes()))
        for path in protected
    )
    return CompletedEvolvedFinalTestAuditV3(
        repository_root=repository,
        run_id=run_id,
        parent_run_id=parent_run_id,
        completion_receipt_sha256=sha256_bytes(completion_path.read_bytes()),
        compatibility_receipt_sha256=sha256_bytes(compatibility_path.read_bytes()),
        test_order_sha256=test_order,
        config_digest=inputs.config.digest,
        split_digest=inputs.split_receipt_sha256,
        model_digest=str(compatibility["model_digest"]),
        managed_codex_digest=str(compatibility["managed_codex_digest"]),
        frozen_artifact_set_sha256=str(completion["frozen_artifact_set_sha256"]),
        correct_count=correct_count,
        protected_files_sha256=protected_hashes,
        evolved_evaluations=tuple(evolved),
    )


def build_control_baseline_dry_run_v3(
    *,
    inputs: ExperimentInputsV2,
    audit: CompletedEvolvedFinalTestAuditV3,
    run_id: str,
) -> dict[str, object]:
    _require_current_identity(inputs, audit)
    prompts = {
        task.uid: sha256_bytes(v2._render_prompt(inputs.loader, task).text.encode())
        for task in inputs.test
    }
    if len(prompts) != TEST_COUNT:
        raise ControlFinalTestBaselineV3Error("BASELINE_PROMPT_COUNT_INVALID")
    return {
        "schema_version": "ControlFinalTestBaselineDryRunV3",
        "protocol_id": BASELINE_PROTOCOL_ID,
        "status": "PASS",
        "run_id": run_id,
        "paired_evolved_run_id": audit.run_id,
        "test_task_count": TEST_COUNT,
        "test_order_sha256": audit.test_order_sha256,
        "prompt_binding_sha256": sha256_bytes(canonical_json_bytes(prompts)),
        "planned_candidate_calls": TEST_COUNT,
        "planned_reflector_calls": 0,
        "planned_core_jobs": 0,
        "planned_context_resolutions": 0,
        "planned_artifacts": 0,
        "context_mode": "generation_zero",
        "context_target_ids": [],
        "context_artifact_ids": [],
        "parent_artifacts_imported": False,
        "parent_databases_imported": False,
        "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
        "model_calls": 0,
    }


def build_control_baseline_source_compatibility_receipt_v3(
    *,
    inputs: ExperimentInputsV2,
    audit: CompletedEvolvedFinalTestAuditV3,
) -> dict[str, object]:
    _require_current_identity(inputs, audit)
    prior = _read_json(
        inputs.repository_root
        / "results/chembench_supervised_transfer_v3/runs"
        / audit.run_id
        / _FINAL_COMPATIBILITY_RECEIPT
    )
    critical = dict(prior.get("critical_execution_files_sha256", {}))
    if not critical:
        raise ControlFinalTestBaselineV3Error("BASELINE_CRITICAL_FILES_MISSING")
    current = {
        relative: sha256_bytes((inputs.repository_root / relative).read_bytes())
        for relative in critical
    }
    drifted = {
        relative
        for relative, expected in critical.items()
        if current.get(relative) != expected
    }
    if drifted != {_EXECUTOR_SOURCE}:
        raise ControlFinalTestBaselineV3Error("BASELINE_EXECUTION_SEMANTICS_DRIFT")
    if (
        critical.get(_EXECUTOR_SOURCE) != _EVOLVED_EXECUTOR_SHA256
        or current.get(_EXECUTOR_SOURCE) != _NETWORK_RECOVERY_EXECUTOR_SHA256
    ):
        raise ControlFinalTestBaselineV3Error("BASELINE_EXECUTOR_RECOVERY_DRIFT")
    changed = tuple(
        line
        for line in _git_output(
            inputs.repository_root,
            "diff",
            "--name-only",
            f"{prior['recovery_source_commit']}..HEAD",
            "--",
            "benchmarks/chembench",
        ).splitlines()
        if line
    )
    if not set(changed).issubset(_ALLOWED_BASELINE_SOURCE_CHANGES):
        raise ControlFinalTestBaselineV3Error("BASELINE_SOURCE_SCOPE_INVALID")
    current_runtime_services_sha256 = sha256_bytes(
        (inputs.repository_root / _RUNTIME_SERVICES_SOURCE).read_bytes()
    )
    if (
        current_runtime_services_sha256
        != _DOCKER_INSPECT_RECOVERY_RUNTIME_SERVICES_SHA256
    ):
        raise ControlFinalTestBaselineV3Error(
            "BASELINE_RUNTIME_SERVICES_RECOVERY_DRIFT"
        )
    diff = subprocess.run(
        (
            "git",
            "-C",
            str(inputs.repository_root),
            "diff",
            "--binary",
            f"{prior['recovery_source_commit']}..HEAD",
            "--",
            "benchmarks/chembench",
        ),
        check=True,
        capture_output=True,
    ).stdout
    return {
        "schema_version": "ControlFinalTestBaselineSourceCompatibilityReceiptV3",
        "protocol_id": BASELINE_PROTOCOL_ID,
        "paired_evolved_run_id": audit.run_id,
        "evolved_source_commit": prior["recovery_source_commit"],
        "baseline_source_commit": inputs.source_commit,
        "files_changed": list(changed),
        "git_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "critical_execution_files_sha256": current,
        "config_digest": inputs.config.digest,
        "split_digest": inputs.split_receipt_sha256,
        "model_digest": audit.model_digest,
        "managed_codex_digest": audit.managed_codex_digest,
        "reviewed_executor_transition": {
            "path": _EXECUTOR_SOURCE,
            "evolved_sha256": _EVOLVED_EXECUTOR_SHA256,
            "baseline_sha256": _NETWORK_RECOVERY_EXECUTOR_SHA256,
            "change_class": "runtime_health_recovery_only",
        },
        "reviewed_runtime_services_transition": {
            "path": _RUNTIME_SERVICES_SOURCE,
            "prior_sha256": (
                _PRE_DOCKER_INSPECT_RECOVERY_RUNTIME_SERVICES_SHA256
            ),
            "baseline_sha256": current_runtime_services_sha256,
            "change_class": "docker_identity_read_timeout_recovery_only",
        },
        "candidate_prompt_unchanged": True,
        "candidate_model_unchanged": True,
        "reasoning_effort_unchanged": True,
        "candidate_harness_unchanged": True,
        "evaluator_unchanged": True,
        "strict_parser_unchanged": True,
        "test_order_unchanged": True,
        "test_single_pass_unchanged": True,
        "terminal_completion_semantics_unchanged": True,
        "runtime_health_recovery_reviewed": True,
        "docker_identity_read_timeout_recovery_reviewed": True,
        "baseline_change_limited_to_no_context_control_orchestration": True,
        "semantically_compatible": True,
    }


class ComposedControlFinalTestBaselineV3(v2.SupervisedTransferExperimentV2):
    """Run the frozen v3 Test once through managed control harness context."""

    def __init__(
        self,
        *,
        inputs: ExperimentInputsV2,
        run_id: str,
        evolved_audit: CompletedEvolvedFinalTestAuditV3,
        executor_factory: Any | None = None,
    ) -> None:
        _require_current_identity(inputs, evolved_audit)
        self.evolved_audit = evolved_audit
        super().__init__(
            inputs=inputs,
            run_id=run_id,
            run_mode="preflight",
            executor_factory=executor_factory,
        )
        self.run_mode = "formal_control_test_baseline"  # type: ignore[assignment]
        self._state.update(
            {
                "schema_version": "ChemBenchControlFinalTestBaselineRunStateV3",
                "protocol_id": BASELINE_PROTOCOL_ID,
                "run_mode": "formal_control_test_baseline",
                "paired_evolved_run_id": evolved_audit.run_id,
                "paired_evolved_audit_sha256": evolved_audit.digest,
                "context_mode": "generation_zero",
                "generation_zero_context_set_sha256": (
                    GENERATION_ZERO_CONTEXT_SET_SHA256
                ),
                "parent_artifacts_imported": False,
                "parent_databases_imported": False,
                "control_test_enabled": True,
                "reflector_enabled": False,
                "baseline_test_status": "PENDING_EXECUTION",
            }
        )
        self._write_state()
        self._write_amendment()

    def run_control_baseline(self) -> dict[str, object]:
        try:
            self._require_baseline_source_frozen()
            with ExitStack() as stack:
                executor = v2._enter_if_context(
                    stack, self._executor_factory("control")
                )
                self._set_stage("CONTROL_TEST")
                ledger_summary = self._run_control_test(executor)
            self._require_baseline_source_frozen()
            completion = self._write_completion_receipt(ledger_summary)
            self._set_stage("COMPLETED")
            completion_path = (
                self.result_root
                / "public/control_final_test_baseline_completion_receipt_v3.json"
            )
            self._state.update(
                {
                    "status": "COMPLETED",
                    "baseline_test_status": "COMPLETED",
                    "completed_at_utc": v2.utc_now(),
                    "completion_receipt_sha256": sha256_bytes(
                        completion_path.read_bytes()
                    ),
                }
            )
            self._write_state()
            return completion
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def _run_control_test(self, executor: TaskExecutorV2) -> dict[str, object]:
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
        if model_digest != self.evolved_audit.model_digest:
            raise ControlFinalTestBaselineV3Error("BASELINE_MODEL_DIGEST_MISMATCH")
        ledger = FinalTestConsumptionLedgerV2(
            path=(self.state_root / "private/control_test_ledger_v3.jsonl").resolve(),
            source_commit=self.inputs.source_commit,
            artifact_set_digest=GENERATION_ZERO_CONTEXT_SET_SHA256,
            config_digest=self.inputs.config.digest,
            model_digest=model_digest,
        )
        for ordinal, task in enumerate(self.inputs.test):
            retry_window_started_at = time.monotonic()
            for attempt_number in range(
                1, v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT + 1
            ):
                if attempt_number > 1:
                    v2._require_executor_retry_window(retry_window_started_at)
                self._require_baseline_source_frozen()
                attempt_id = self._session_id(
                    "CONTROL_TEST", "control", task, 0, attempt_number
                )
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
                        completion_callback=lambda completion, *, _task=task, _attempt=attempt_id: ledger.complete(
                            task_uid=_task.uid,
                            arm="control",
                            attempt_id=_attempt,
                            completion=completion,
                            timestamp=v2.utc_now(),
                        ),
                    )
                    break
                except SupervisedTaskExecutionErrorV2 as exc:
                    if exc.completion_exists is True:
                        if exc.completion_sha256 is None:
                            raise ControlFinalTestBaselineV3Error(
                                "BASELINE_COMPLETION_DIGEST_MISSING"
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
                            "schema_version": (
                                "ControlFinalTestInfrastructureRetryPublicV3"
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
                    "BASELINE_INFRASTRUCTURE_RETRY_EXHAUSTED"
                )
        summary = ledger.summary()
        if (
            summary["control_completion_count"] != TEST_COUNT
            or summary["evolved_completion_count"] != 0
        ):
            raise ControlFinalTestBaselineV3Error("BASELINE_LEDGER_INCOMPLETE")
        write_public_file(
            self.result_root / "public/control_test_ledger_receipt_v3.json",
            canonical_pretty_json_bytes(summary),
        )
        return summary

    def _execute_single_session(
        self,
        executor: TaskExecutorV2,
        *,
        task: PrivateChemBench4KTask,
        context: CoreResolvedSupervisedContextV2 | None,
        logical_arm: str,
        executor_arm: Literal["control", "online"],
        task_ordinal: int,
        round_index: Literal[0, 1, 2, 3],
        stage: str,
        session_id: str,
        completion_callback: Any,
    ) -> v2.SessionOutcomeV2:
        if (
            stage != "CONTROL_TEST"
            or logical_arm != "test_control"
            or executor_arm != "control"
            or context is not None
            or round_index != 0
        ):
            raise ControlFinalTestBaselineV3Error(
                "BASELINE_CONTROL_SESSION_INVALID"
            )
        return v2.SupervisedTransferExperimentV2._execute_single_session(
            self,
            executor,
            task=task,
            context=None,
            logical_arm=logical_arm,
            executor_arm=executor_arm,
            task_ordinal=task_ordinal,
            round_index=round_index,
            stage=stage,
            session_id=session_id,
            completion_callback=completion_callback,
        )

    def _default_executor(self, arm: Literal["control", "online"]) -> TaskExecutorV2:
        if arm != "control":
            raise ControlFinalTestBaselineV3Error("BASELINE_ONLINE_ARM_FORBIDDEN")
        return SupervisedManagedCodexExecutorV2(
            arm="control",
            timeout_seconds=self.inputs.config.task_timeout_seconds,
            rollout_url=self.inputs.config.rollout_url,
            runtime_services=self.inputs.runtime_services,
            protocol_id=BASELINE_PROTOCOL_ID,
        )

    def _set_stage(self, stage: str) -> None:
        current = str(self._state["stage"])
        allowed = {
            "INITIALIZED": frozenset({"CONTROL_TEST"}),
            "CONTROL_TEST": frozenset({"COMPLETED"}),
        }
        if stage != current and stage not in allowed.get(current, frozenset()):
            raise ControlFinalTestBaselineV3Error(
                "BASELINE_STAGE_TRANSITION_INVALID"
            )
        self._state["stage"] = stage
        self._state["status"] = "COMPLETED" if stage == "COMPLETED" else "RUNNING"
        self._state["last_progress_utc"] = v2.utc_now()
        self._write_state()

    def _require_baseline_source_frozen(self) -> None:
        if (
            v2._git(self.inputs.repository_root, "rev-parse", "HEAD")
            != self.inputs.source_commit
            or v2._benchmark_status(self.inputs.repository_root)
            or v2._git(self.inputs.repository_root, "diff", "--", "src/openevo")
        ):
            raise ControlFinalTestBaselineV3Error("BASELINE_SOURCE_IDENTITY_DRIFT")
        source = verify_source_manifest_v3(self.inputs.repository_root)
        if source["manifest_sha256"] != self.inputs.source_manifest_sha256:
            raise ControlFinalTestBaselineV3Error("BASELINE_SOURCE_MANIFEST_DRIFT")
        for relative, expected in self.evolved_audit.protected_files_sha256:
            if sha256_bytes((self.inputs.repository_root / relative).read_bytes()) != expected:
                raise ControlFinalTestBaselineV3Error(
                    "EVOLVED_FINAL_TEST_EVIDENCE_MUTATED"
                )

    def _write_paid_plan(self) -> None:
        payload = {
            "schema_version": "ControlFinalTestBaselinePaidExecutionPlanV3",
            "protocol_id": BASELINE_PROTOCOL_ID,
            "run_id": self.run_id,
            "paired_evolved_run_id": self.evolved_audit.run_id,
            "source_commit": self.inputs.source_commit,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "config_sha256": self.inputs.config.digest,
            "split_reference_sha256": self.inputs.split_receipt_sha256,
            "test_order_sha256": self.evolved_audit.test_order_sha256,
            "model": self.inputs.config.model,
            "reasoning_effort": self.inputs.config.reasoning_effort,
            "task_timeout_seconds": self.inputs.config.task_timeout_seconds,
            "candidate_calls": TEST_COUNT,
            "reflector_calls": 0,
            "core_jobs": 0,
            "context_resolutions": 0,
            "typed_artifacts": 0,
            "context_mode": "generation_zero",
            "context_target_ids": [],
            "context_artifact_ids": [],
            "candidate_execution_path": (
                "TaskRequest->Rollout->Gateway->CodexHarness"
            ),
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "candidate_codex_executable_sha256": (
                self.inputs.candidate_codex.executable_sha256
            ),
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "issued_at_utc": v2.utc_now(),
        }
        write_public_file(
            self.result_root
            / "public/control_final_test_baseline_paid_execution_plan_v3.json",
            canonical_pretty_json_bytes(payload),
        )

    def _write_amendment(self) -> None:
        payload = {
            "schema_version": "ControlFinalTestBaselineAmendmentV3",
            "protocol_id": BASELINE_PROTOCOL_ID,
            "authorization": "explicit_user_authorization",
            "paired_evolved_run_id": self.evolved_audit.run_id,
            "paired_evolved_audit_sha256": self.evolved_audit.digest,
            "same_test_partition": True,
            "same_test_order": True,
            "same_candidate_model": True,
            "same_reasoning_effort": True,
            "same_candidate_harness": True,
            "same_private_evaluator": True,
            "evolution_artifacts_injected": False,
            "reflector_enabled": False,
            "test_feedback_enabled": False,
            "one_valid_completion_per_task": True,
            "not_a_single_uninterrupted_run": True,
        }
        write_public_file(
            self.result_root / "public/control_final_test_baseline_amendment_v3.json",
            canonical_pretty_json_bytes(payload),
        )

    def _write_completion_receipt(
        self, ledger_summary: dict[str, object]
    ) -> dict[str, object]:
        baseline = [
            row
            for row in _read_jsonl(self.private_events)
            if row.get("kind") == "PRIVATE_EVALUATED"
            and row.get("stage") == "CONTROL_TEST"
        ]
        baseline.sort(key=lambda row: int(row["task_ordinal"]))
        evolved = self.evolved_audit.evolved_evaluations
        if len(baseline) != TEST_COUNT:
            raise ControlFinalTestBaselineV3Error("BASELINE_EVALUATIONS_INCOMPLETE")
        counts = {
            "both_correct": 0,
            "baseline_only_correct": 0,
            "evolved_only_correct": 0,
            "both_wrong": 0,
        }
        categories: list[dict[str, object]] = []
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
                raise ControlFinalTestBaselineV3Error("BASELINE_PAIRING_INVALID")
            pair = (bool(control.get("correct")), bool(trained.get("correct")))
            if pair == (True, True):
                counts["both_correct"] += 1
            elif pair == (True, False):
                counts["baseline_only_correct"] += 1
            elif pair == (False, True):
                counts["evolved_only_correct"] += 1
            else:
                counts["both_wrong"] += 1
        for category in CHEMBENCH4K_CATEGORIES:
            control_rows = [row for row in baseline if row.get("category") == category]
            evolved_rows = [row for row in evolved if row.get("category") == category]
            if len(control_rows) != 50 or len(evolved_rows) != 50:
                raise ControlFinalTestBaselineV3Error("BASELINE_CATEGORY_COUNT_INVALID")
            categories.append(
                {
                    "category": category,
                    "task_count": 50,
                    "baseline_correct": sum(
                        bool(row.get("correct")) for row in control_rows
                    ),
                    "evolved_correct": sum(
                        bool(row.get("correct")) for row in evolved_rows
                    ),
                }
            )
        baseline_correct = sum(bool(row.get("correct")) for row in baseline)
        discordant = (
            counts["baseline_only_correct"] + counts["evolved_only_correct"]
        )
        smaller = min(
            counts["baseline_only_correct"], counts["evolved_only_correct"]
        )
        paired_exact_p = min(
            1.0,
            2
            * sum(math.comb(discordant, value) for value in range(smaller + 1))
            / (2**discordant),
        )
        payload = {
            "schema_version": "ControlFinalTestBaselineCompletionReceiptV3",
            "protocol_id": BASELINE_PROTOCOL_ID,
            "classification": [
                "PAIRED_CONTROL_FINAL_TEST",
                "NO_EVOLUTION_ARTIFACTS",
                "SAME_TEST_ORDER_MODEL_HARNESS_EVALUATOR",
                "NOT_A_SINGLE_UNINTERRUPTED_RUN",
            ],
            "run_id": self.run_id,
            "paired_evolved_run_id": self.evolved_audit.run_id,
            "task_count": TEST_COUNT,
            "candidate_completion_count": TEST_COUNT,
            "candidate_attempt_count": ledger_summary["attempt_count"],
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
            "duplicate_completion_count": 0,
            "reflector_calls": 0,
            "core_jobs": 0,
            "context_resolutions": 0,
            "typed_artifacts": 0,
            "context_target_ids": [],
            "context_artifact_ids": [],
            "test_feedback_enabled": False,
            "managed_codex_digest": self.inputs.candidate_codex.executable_sha256,
            "test_order_sha256": self.evolved_audit.test_order_sha256,
            "paired_evolved_evidence_unchanged": True,
        }
        path = (
            self.result_root
            / "public/control_final_test_baseline_completion_receipt_v3.json"
        )
        write_public_file(path, canonical_pretty_json_bytes(payload))
        return payload


def _require_current_identity(
    inputs: ExperimentInputsV2, audit: CompletedEvolvedFinalTestAuditV3
) -> None:
    model_digest = sha256_bytes(
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
    if (
        inputs.config.digest != audit.config_digest
        or inputs.split_receipt_sha256 != audit.split_digest
        or model_digest != audit.model_digest
        or audit.managed_codex_digest != EXPECTED_MANAGED_CODEX_SHA256
        or v2._uid_order_digest(inputs.test) != audit.test_order_sha256
    ):
        raise ControlFinalTestBaselineV3Error("BASELINE_IDENTITY_MISMATCH")
    runtime_values = (
        inputs.candidate_codex,
        inputs.managed_codex,
        inputs.runtime_services,
    )
    if all(value is None for value in runtime_values):
        return
    if any(value is None for value in runtime_values):
        raise ControlFinalTestBaselineV3Error("BASELINE_RUNTIME_INCOMPLETE")
    if (
        inputs.candidate_codex.executable_sha256 != EXPECTED_MANAGED_CODEX_SHA256
        or inputs.managed_codex.executable_sha256 != EXPECTED_MANAGED_CODEX_SHA256
    ):
        raise ControlFinalTestBaselineV3Error("BASELINE_MANAGED_CODEX_MISMATCH")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlFinalTestBaselineV3Error("BASELINE_EVIDENCE_UNREADABLE") from exc
    if type(payload) is not dict:
        raise ControlFinalTestBaselineV3Error("BASELINE_EVIDENCE_INVALID")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlFinalTestBaselineV3Error("BASELINE_EVIDENCE_UNREADABLE") from exc


def _git_output(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


__all__ = [
    "BASELINE_PROTOCOL_ID",
    "EVOLVED_FINAL_TEST_RUN_ID",
    "CompletedEvolvedFinalTestAuditV3",
    "ComposedControlFinalTestBaselineV3",
    "audit_completed_evolved_final_test_v3",
    "build_control_baseline_dry_run_v3",
    "build_control_baseline_source_compatibility_receipt_v3",
]
