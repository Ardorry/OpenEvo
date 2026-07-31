"""Read-only nine-shard freeze and evolved-only Final Test for v3."""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.frozen_runtime_v2 import _issue_core_resolved_text_memory_v2
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2 import experiment as v2
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
    _issue_core_resolved_auxiliary_v2,
)
from openevo_chembench.supervised_transfer_v2.core import TaskwiseCoreUpdateResultV1
from openevo_chembench.supervised_transfer_v3.category_shard_recovery import (
    _read_json,
    _read_jsonl,
    _tree_identity,
)
from openevo_chembench.supervised_transfer_v3.config import TEST_COUNT
from openevo_chembench.supervised_transfer_v3.continued_category_shard_recovery import (
    ContinuedAcceptedClosedShardsReceiptV3,
    ContinuedFinalShardCompositionReceiptV3,
    _audit_closed_category,
    _executor_digest_from_inputs,
    _model_digest_from_inputs,
)
from openevo_chembench.supervised_transfer_v3.experiment import (
    FrozenThreeTargetSetV3,
    SupervisedExperimentV3Error,
    SupervisedTransferExperimentV3,
)
from openevo_chembench.supervised_transfer_v3.same_profile_category_recovery import (
    EXPECTED_MANAGED_CODEX_SHA256,
    _validate_checkpoint_chain,
)

COMPOSED_FINAL_TEST_PROTOCOL_ID = (
    "supervised_transfer_v3_two_round_one_evolution_composed_final_test"
)
COMPOSITION_RUN_ID = "stv3-temperature-controlfix-recovery-20260731T105523Z"
_COMPOSITION_FILE = "continued_final_shard_composition_receipt_v3.json"
_CRITICAL_FINAL_TEST_FILES = (
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
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/test_ledger.py",
)
_TRANSITIONS = {
    "INITIALIZED": frozenset({"FREEZE_THREE_TARGETS"}),
    "FREEZE_THREE_TARGETS": frozenset({"EVOLVED_TEST"}),
    "EVOLVED_TEST": frozenset({"COMPLETED"}),
}


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class ComposedFinalTestPaidExecutionPlanV3(_ClosedModel):
    schema_version: Literal["ComposedFinalTestPaidExecutionPlanV3"] = (
        "ComposedFinalTestPaidExecutionPlanV3"
    )
    protocol_id: str = COMPOSED_FINAL_TEST_PROTOCOL_ID
    run_id: str
    composition_run_id: str
    source_commit: str
    source_manifest_sha256: str
    config_sha256: str
    split_reference_sha256: str
    model: str
    reasoning_effort: str
    task_timeout_seconds: int = Field(gt=0)
    candidate_calls: Literal[450] = TEST_COUNT
    reflector_calls: Literal[0] = 0
    core_jobs: Literal[0] = 0
    target_updates: Literal[0] = 0
    candidate_execution_path: Literal[
        "TaskRequest->Rollout->Gateway->CodexHarness"
    ] = "TaskRequest->Rollout->Gateway->CodexHarness"
    candidate_codex_sha256: str
    runtime_services_identity_sha256: str
    final_test_single_pass: Literal[True] = True
    test_feedback_enabled: Literal[False] = False


class ComposedFrozenTargetReceiptV3(_ClosedModel):
    schema_version: Literal["ComposedFrozenTargetReceiptV3"] = (
        "ComposedFrozenTargetReceiptV3"
    )
    protocol_id: str = COMPOSED_FINAL_TEST_PROTOCOL_ID
    composition_run_id: str
    composition_receipt_sha256: str
    source_commit: str
    source_manifest_sha256: str
    config_sha256: str
    split_reference_sha256: str
    model: str
    reasoning_effort: str
    candidate_codex_sha256: str
    test_order_sha256: str
    category_count: Literal[9] = 9
    target_count: Literal[27] = 27
    artifact_set_sha256: str
    categories: dict[str, dict[str, object]]
    checkpoint_selection: Literal["COMPOSED_CATEGORY_TASK_49_CYCLE_1_ONLY"] = (
        "COMPOSED_CATEGORY_TASK_49_CYCLE_1_ONLY"
    )
    source_artifacts_imported: Literal[False] = False
    source_databases_imported: Literal[False] = False
    source_shards_opened_read_only: Literal[True] = True
    test_evolution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _complete(self) -> ComposedFrozenTargetReceiptV3:
        if tuple(self.categories) != CHEMBENCH4K_CATEGORIES:
            raise ValueError("composed frozen categories are incomplete")
        return self


@dataclass(frozen=True, slots=True)
class ComposedCategoryAuditV3:
    category: str
    category_index: int
    source_run_id: str
    source_commit: str
    closed_receipt_sha256: str
    category_root: Path
    category_tree_sha256: str
    final_result: TaskwiseCoreUpdateResultV1


@dataclass(frozen=True, slots=True)
class ComposedTrainAuditV3:
    repository_root: Path
    composition_run_id: str
    composition_path: Path
    composition_receipt_sha256: str
    composition_digest: str
    categories: tuple[ComposedCategoryAuditV3, ...]
    config_digest: str
    split_digest: str
    model_digest: str
    executor_digest: str
    managed_codex_digest: str

    @property
    def digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "composition_run_id": self.composition_run_id,
                    "composition_receipt_sha256": self.composition_receipt_sha256,
                    "composition_digest": self.composition_digest,
                    "categories": [
                        {
                            "category": item.category,
                            "source_run_id": item.source_run_id,
                            "source_commit": item.source_commit,
                            "closed_receipt_sha256": item.closed_receipt_sha256,
                            "category_tree_sha256": item.category_tree_sha256,
                            "final_head": _final_head_receipts(item.final_result),
                        }
                        for item in self.categories
                    ],
                }
            )
        )


def audit_composed_train_shards_v3(
    *, repository_root: Path, composition_run_id: str = COMPOSITION_RUN_ID
) -> ComposedTrainAuditV3:
    repository = repository_root.resolve(strict=True)
    result_base = repository / "results/chembench_supervised_transfer_v3/runs"
    state_base = repository / "state/chembench_supervised_transfer_v3/runs"
    composition_path = result_base / composition_run_id / "public" / _COMPOSITION_FILE
    encoded = composition_path.read_bytes()
    try:
        receipt = ContinuedFinalShardCompositionReceiptV3.model_validate_json(encoded)
    except ValueError as exc:
        raise SupervisedExperimentV3Error("COMPOSED_SHARD_RECEIPT_INVALID") from exc
    if encoded != canonical_pretty_json_bytes(receipt.model_dump(mode="json")):
        raise SupervisedExperimentV3Error("COMPOSED_SHARD_RECEIPT_NONCANONICAL")
    composition_state = _read_json(state_base / composition_run_id / "run_state.json")
    if (
        composition_state.get("status") != "REMAINING_SHARDS_COMPLETED"
        or composition_state.get("final_shard_composition_sha256") != receipt.digest
    ):
        raise SupervisedExperimentV3Error("COMPOSED_SHARD_RUN_NOT_CLOSED")
    accepted_path = (
        result_base
        / composition_run_id
        / "public/continued_accepted_closed_shards_receipt_v3.json"
    )
    accepted = ContinuedAcceptedClosedShardsReceiptV3.model_validate(
        _read_json(accepted_path)
    )
    accepted_by_category = {item.category: item for item in accepted.shards}

    audited: list[ComposedCategoryAuditV3] = []
    for entry in receipt.categories:
        source_state_root = state_base / entry.source_run_id
        source_result_root = result_base / entry.source_run_id
        state = _read_json(source_state_root / "run_state.json")
        paid = _load_source_paid_plan(source_result_root)
        public_events = _read_jsonl(source_result_root / "public/events.jsonl")
        private_events = _read_jsonl(source_state_root / "private/events.jsonl")
        closed = _audit_closed_category(
            state_root=source_state_root,
            state=state,
            paid=paid,
            public_events=public_events,
            private_events=private_events,
            source_run_id=entry.source_run_id,
            category_index=entry.category_index,
            category=entry.category,
        )
        if (
            closed.source_commit != entry.source_commit
            or closed.config_digest != entry.config_digest
            or closed.split_digest != entry.split_digest
            or closed.model_digest != entry.model_digest
            or closed.executor_digest != entry.executor_digest
            or closed.managed_codex_digest != entry.managed_codex_digest
        ):
            raise SupervisedExperimentV3Error("COMPOSED_SHARD_IDENTITY_DRIFT")
        if entry.closed_receipt_sha256 != closed.closed_evidence_sha256:
            matches = []
            for path in (source_result_root / "public").glob("*closed_shard*.json"):
                try:
                    payload = _read_json(path)
                except SupervisedExperimentV3Error:
                    continue
                if payload.get("category") == entry.category:
                    matches.append(sha256_bytes(path.read_bytes()))
            inherited = accepted_by_category.get(entry.category)
            inherited_matches = (
                inherited is not None
                and inherited.source_run_id == entry.source_run_id
                and inherited.closed_evidence_sha256 == entry.closed_receipt_sha256
                and inherited.category_tree_sha256 == closed.category_tree_sha256
            )
            if matches != [entry.closed_receipt_sha256] and not inherited_matches:
                raise SupervisedExperimentV3Error("COMPOSED_CLOSED_RECEIPT_DRIFT")
        category_root = source_state_root / "private/core/formal" / entry.category
        checkpoints = _read_jsonl(category_root / "private_lineage_checkpoints_v2.jsonl")
        final = _validate_checkpoint_chain(
            checkpoints, category=entry.category, expected_tasks=50
        )[-1]
        audited.append(
            ComposedCategoryAuditV3(
                category=entry.category,
                category_index=entry.category_index,
                source_run_id=entry.source_run_id,
                source_commit=entry.source_commit,
                closed_receipt_sha256=entry.closed_receipt_sha256,
                category_root=category_root,
                category_tree_sha256=closed.category_tree_sha256,
                final_result=final,
            )
        )
    identity = receipt.categories[0]
    return ComposedTrainAuditV3(
        repository_root=repository,
        composition_run_id=composition_run_id,
        composition_path=composition_path,
        composition_receipt_sha256=sha256_bytes(encoded),
        composition_digest=receipt.digest,
        categories=tuple(audited),
        config_digest=identity.config_digest,
        split_digest=identity.split_digest,
        model_digest=identity.model_digest,
        executor_digest=identity.executor_digest,
        managed_codex_digest=identity.managed_codex_digest,
    )


def build_composed_final_test_dry_run_v3(
    *, inputs: v2.ExperimentInputsV2, audit: ComposedTrainAuditV3, run_id: str
) -> dict[str, object]:
    _require_current_compatibility(inputs, audit)
    return {
        "schema_version": "ComposedFinalTestDryRunV3",
        "protocol_id": COMPOSED_FINAL_TEST_PROTOCOL_ID,
        "status": "PASS",
        "run_id": run_id,
        "composition_run_id": audit.composition_run_id,
        "composition_audit_sha256": audit.digest,
        "category_order": list(CHEMBENCH4K_CATEGORIES),
        "source_run_ids": [item.source_run_id for item in audit.categories],
        "frozen_target_count": 27,
        "test_task_count": TEST_COUNT,
        "planned_candidate_calls": TEST_COUNT,
        "planned_reflector_calls": 0,
        "planned_core_jobs": 0,
        "source_artifacts_imported": False,
        "source_databases_imported": False,
        "model_calls": 0,
    }


def build_composed_source_compatibility_receipt_v3(
    *, inputs: v2.ExperimentInputsV2, audit: ComposedTrainAuditV3
) -> dict[str, object]:
    _require_current_compatibility(inputs, audit)
    current_hashes = {
        path: sha256_bytes((inputs.repository_root / path).read_bytes())
        for path in _CRITICAL_FINAL_TEST_FILES
    }
    source_commits = tuple(dict.fromkeys(item.source_commit for item in audit.categories))
    for commit in source_commits:
        for path, current in current_hashes.items():
            if sha256_bytes(_git_bytes(inputs.repository_root, commit, path)) != current:
                raise SupervisedExperimentV3Error("COMPOSED_TEST_SOURCE_NOT_COMPATIBLE")
    return {
        "schema_version": "ComposedFinalTestSourceCompatibilityReceiptV3",
        "protocol_id": COMPOSED_FINAL_TEST_PROTOCOL_ID,
        "composition_run_id": audit.composition_run_id,
        "category_source_commits": list(source_commits),
        "final_test_source_commit": inputs.source_commit,
        "critical_final_test_files_sha256": current_hashes,
        "config_digest": inputs.config.digest,
        "split_digest": inputs.split_receipt_sha256,
        "model_digest": _model_digest_from_inputs(_InputsAdapter(inputs)),
        "executor_digest": _executor_digest_from_inputs(_InputsAdapter(inputs)),
        "managed_codex_digest": inputs.managed_codex.executable_sha256,
        "changes_limited_to_composed_freeze_test_orchestration": True,
        "candidate_prompt_unchanged": True,
        "candidate_model_unchanged": True,
        "reasoning_effort_unchanged": True,
        "candidate_harness_unchanged": True,
        "evaluator_unchanged": True,
        "strict_parser_unchanged": True,
        "test_order_unchanged": True,
        "test_single_pass_unchanged": True,
        "test_feedback_disabled": True,
        "semantically_compatible": True,
    }


class ComposedFinalTestExperimentV3(SupervisedTransferExperimentV3):
    """Freeze exact final heads from nine immutable shards and run Test once."""

    def __init__(
        self,
        *,
        inputs: v2.ExperimentInputsV2,
        run_id: str,
        composition_audit: ComposedTrainAuditV3,
        runtime_smoke_run_id: str,
        executor_factory: Any | None = None,
    ) -> None:
        _require_current_compatibility(inputs, composition_audit)
        self.composition_audit = composition_audit
        self.runtime_smoke_run_id = runtime_smoke_run_id
        super().__init__(
            inputs=inputs,
            run_id=run_id,
            run_mode="formal_online",
            smoke_run_id=runtime_smoke_run_id,
            executor_factory=executor_factory,
        )
        self._state.update(
            {
                "schema_version": "ChemBenchComposedFinalTestRunStateV3",
                "protocol_id": COMPOSED_FINAL_TEST_PROTOCOL_ID,
                "run_mode": "composed_final_test",
                "composition_run_id": composition_audit.composition_run_id,
                "composition_receipt_sha256": (
                    composition_audit.composition_receipt_sha256
                ),
                "composition_audit_sha256": composition_audit.digest,
                "source_artifacts_imported": False,
                "source_databases_imported": False,
                "frozen_target_count": 0,
                "final_test_status": "PENDING_FREEZE",
            }
        )
        self._write_state()

    def run_composed_final_test(self) -> dict[str, object]:
        try:
            self._verify_source_trees_unchanged()
            contexts = {
                item.category: _issue_context(item.final_result)
                for item in self.composition_audit.categories
            }
            frozen = self._freeze_composed_targets(contexts)
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("online"))
                self._run_evolved_test_v3(frozen, executor)
            self._verify_source_trees_unchanged()
            rows = _read_jsonl(self.private_events)
            evaluated = [
                row
                for row in rows
                if row.get("kind") == "PRIVATE_EVALUATED"
                and row.get("stage") == "EVOLVED_TEST"
            ]
            if len(evaluated) != TEST_COUNT:
                raise SupervisedExperimentV3Error("COMPOSED_FINAL_TEST_COUNT_INVALID")
            completion = {
                "schema_version": "ComposedFinalTestCompletionReceiptV3",
                "protocol_id": COMPOSED_FINAL_TEST_PROTOCOL_ID,
                "run_id": self.run_id,
                "composition_run_id": self.composition_audit.composition_run_id,
                "frozen_artifact_set_sha256": frozen.artifact_set_digest,
                "task_count": TEST_COUNT,
                "candidate_completion_count": TEST_COUNT,
                "correct_count": sum(bool(row.get("correct")) for row in evaluated),
                "reflector_calls": 0,
                "core_jobs": 0,
                "test_feedback_enabled": False,
                "source_artifacts_imported": False,
                "source_databases_imported": False,
            }
            path = self.result_root / "public/composed_final_test_completion_receipt_v3.json"
            write_public_file(path, canonical_pretty_json_bytes(completion))
            self._set_stage("COMPLETED")
            self._state.update(
                {
                    "status": "COMPLETED",
                    "final_test_status": "COMPLETED",
                    "completed_at_utc": v2.utc_now(),
                    "completion_receipt_sha256": sha256_bytes(path.read_bytes()),
                }
            )
            self._write_state()
            return completion
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def _freeze_composed_targets(
        self, contexts: dict[str, CoreResolvedSupervisedContextV2]
    ) -> FrozenThreeTargetSetV3:
        self._set_stage("FREEZE_THREE_TARGETS")
        categories: dict[str, dict[str, object]] = {}
        by_category = {item.category: item for item in self.composition_audit.categories}
        for category in CHEMBENCH4K_CATEGORIES:
            item = by_category[category]
            context = contexts[category]
            categories[category] = {
                "source_run_id": item.source_run_id,
                "source_commit": item.source_commit,
                "closed_receipt_sha256": item.closed_receipt_sha256,
                "category_tree_sha256": item.category_tree_sha256,
                "final_task_ordinal": 49,
                "final_cycle": 1,
                "text_memory": v2._target_receipt(context.memory),
                "skill_bundle": v2._target_receipt(context.skill),
                "agent_system": v2._target_receipt(context.agent_system),
                "context_binding_sha256": sha256_bytes(
                    canonical_json_bytes(context.to_runtime_payload())
                ),
            }
        artifact_set_digest = sha256_bytes(canonical_json_bytes(categories))
        receipt = ComposedFrozenTargetReceiptV3(
            composition_run_id=self.composition_audit.composition_run_id,
            composition_receipt_sha256=(
                self.composition_audit.composition_receipt_sha256
            ),
            source_commit=self.inputs.source_commit,
            source_manifest_sha256=self.inputs.source_manifest_sha256,
            config_sha256=self.inputs.config.digest,
            split_reference_sha256=self.inputs.split_receipt_sha256,
            model=self.inputs.config.model,
            reasoning_effort=self.inputs.config.reasoning_effort,
            candidate_codex_sha256=self.inputs.candidate_codex.executable_sha256,
            test_order_sha256=v2._uid_order_digest(self.inputs.test),
            artifact_set_sha256=artifact_set_digest,
            categories=categories,
        )
        path = self.result_root / "public/frozen_three_target_transfer_receipt_v3.json"
        write_public_file(path, canonical_pretty_json_bytes(receipt.model_dump(mode="json")))
        receipt_sha256 = sha256_bytes(path.read_bytes())
        self._state.update(
            {
                "frozen_target_count": 27,
                "frozen_artifact_set_sha256": artifact_set_digest,
                "frozen_receipt_sha256": receipt_sha256,
                "final_test_status": "FROZEN_PENDING_EXECUTION",
            }
        )
        self._write_state()
        return FrozenThreeTargetSetV3(contexts, artifact_set_digest, receipt_sha256)

    def _write_paid_plan(self) -> None:
        plan = ComposedFinalTestPaidExecutionPlanV3(
            run_id=self.run_id,
            composition_run_id=self.composition_audit.composition_run_id,
            source_commit=self.inputs.source_commit,
            source_manifest_sha256=self.inputs.source_manifest_sha256,
            config_sha256=self.inputs.config.digest,
            split_reference_sha256=self.inputs.split_receipt_sha256,
            model=self.inputs.config.model,
            reasoning_effort=self.inputs.config.reasoning_effort,
            task_timeout_seconds=self.inputs.config.task_timeout_seconds,
            candidate_codex_sha256=self.inputs.candidate_codex.executable_sha256,
            runtime_services_identity_sha256=self.inputs.runtime_services.digest,
        )
        write_public_file(
            self.result_root / "public/composed_final_test_paid_execution_plan_v3.json",
            canonical_pretty_json_bytes(plan.model_dump(mode="json")),
        )

    def _set_stage(self, stage: str) -> None:
        current = str(self._state["stage"])
        if stage != current and stage not in _TRANSITIONS.get(current, frozenset()):
            raise SupervisedExperimentV3Error("COMPOSED_FINAL_TEST_STAGE_TRANSITION_INVALID")
        self._state["stage"] = stage
        self._state["status"] = "COMPLETED" if stage == "COMPLETED" else "RUNNING"
        self._state["last_progress_utc"] = v2.utc_now()
        self._write_state()

    def _require_source_frozen(self) -> None:
        super()._require_source_frozen()
        if (
            sha256_bytes(self.composition_audit.composition_path.read_bytes())
            != self.composition_audit.composition_receipt_sha256
        ):
            raise SupervisedExperimentV3Error("COMPOSED_RECEIPT_DRIFT_DURING_TEST")

    def _verify_source_trees_unchanged(self) -> None:
        for item in self.composition_audit.categories:
            if _tree_identity((item.category_root,))[0] != item.category_tree_sha256:
                raise SupervisedExperimentV3Error("COMPOSED_SOURCE_SHARD_TREE_DRIFT")


@dataclass(frozen=True, slots=True)
class _InputsAdapter:
    inputs: v2.ExperimentInputsV2


def _require_current_compatibility(
    inputs: v2.ExperimentInputsV2, audit: ComposedTrainAuditV3
) -> None:
    adapter = _InputsAdapter(inputs)
    if (
        inputs.config.digest != audit.config_digest
        or inputs.split_receipt_sha256 != audit.split_digest
        or _model_digest_from_inputs(adapter) != audit.model_digest
        or audit.managed_codex_digest != EXPECTED_MANAGED_CODEX_SHA256
    ):
        raise SupervisedExperimentV3Error("COMPOSED_FINAL_TEST_IDENTITY_MISMATCH")
    runtime_values = (
        inputs.candidate_codex,
        inputs.managed_codex,
        inputs.runtime_services,
    )
    if all(value is None for value in runtime_values):
        return
    if any(value is None for value in runtime_values):
        raise SupervisedExperimentV3Error("COMPOSED_FINAL_TEST_RUNTIME_INCOMPLETE")
    if (
        _executor_digest_from_inputs(adapter) != audit.executor_digest
        or inputs.managed_codex.executable_sha256 != audit.managed_codex_digest
        or inputs.candidate_codex.executable_sha256 != EXPECTED_MANAGED_CODEX_SHA256
    ):
        raise SupervisedExperimentV3Error("COMPOSED_FINAL_TEST_IDENTITY_MISMATCH")


def _issue_context(result: TaskwiseCoreUpdateResultV1) -> CoreResolvedSupervisedContextV2:
    by_target = {item.target_id: item for item in result.supervised_auxiliary_artifacts}
    if set(by_target) != {"skill_bundle", "agent_system"}:
        raise SupervisedExperimentV3Error("COMPOSED_FINAL_CONTEXT_INCOMPLETE")
    return CoreResolvedSupervisedContextV2(
        memory=_issue_core_resolved_text_memory_v2(
            core_artifact_id=result.core_artifact_id,
            artifact_payload_sha256=result.artifact_payload_sha256,
            context_resolution_digest=result.context_resolution_digest,
            resolved_memory_sha256=result.resolved_memory_sha256,
            markdown=result.resolved_memory,
        ),
        skill=_issue_auxiliary(by_target["skill_bundle"]),
        agent_system=_issue_auxiliary(by_target["agent_system"]),
    )


def _issue_auxiliary(value: Any):
    return _issue_core_resolved_auxiliary_v2(
        target_id=value.target_id,
        core_artifact_id=value.core_artifact_id,
        artifact_payload_sha256=value.artifact_payload_sha256,
        context_resolution_digest=value.context_resolution_digest,
        resolved_content_sha256=value.resolved_content_sha256,
        markdown=value.resolved_content,
    )


def _final_head_receipts(result: TaskwiseCoreUpdateResultV1) -> list[dict[str, str]]:
    return [
        {
            "target": "text_memory",
            "artifact_id": result.core_artifact_id,
            "payload_sha256": result.artifact_payload_sha256,
            "context_sha256": result.context_resolution_digest,
        },
        *[
            {
                "target": item.target_id,
                "artifact_id": item.core_artifact_id,
                "payload_sha256": item.artifact_payload_sha256,
                "context_sha256": item.context_resolution_digest,
            }
            for item in result.supervised_auxiliary_artifacts
        ],
    ]


def _git_bytes(repository: Path, commit: str, path: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repository), "show", f"{commit}:{path}"),
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _load_source_paid_plan(result_root: Path) -> dict[str, Any]:
    candidates = (
        result_root / "public/planned_paid_execution_receipt_v3.json",
        result_root / "public/same_profile_category_shard_paid_execution_plan_v3.json",
        result_root / "public/continued_category_shard_paid_execution_plan_v3.json",
    )
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1:
        raise SupervisedExperimentV3Error("COMPOSED_SOURCE_PAID_PLAN_INVALID")
    return _read_json(existing[0])


__all__ = [
    "COMPOSED_FINAL_TEST_PROTOCOL_ID",
    "COMPOSITION_RUN_ID",
    "ComposedFinalTestExperimentV3",
    "audit_composed_train_shards_v3",
    "build_composed_final_test_dry_run_v3",
    "build_composed_source_compatibility_receipt_v3",
]
