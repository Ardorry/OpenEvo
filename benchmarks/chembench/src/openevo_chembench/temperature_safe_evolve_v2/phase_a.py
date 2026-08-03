"""Frozen-C4 2^3 ablation runner with a global no-GT inference barrier."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openevo.evolution.store import EvolutionStore

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
    write_private_file,
)
from openevo_chembench.temperature_full_evolve_v1.split import (
    build_temperature_split_v1,
    render_temperature_split_artifacts_v1,
)
from openevo_chembench.temperature_full_evolve_v1.statistics import paired_binary_metrics_v1
from openevo_chembench.temperature_safe_evolve_v2.candidate import (
    CandidateContextV2,
    TargetInjectionV2,
)
from openevo_chembench.temperature_safe_evolve_v2.config import (
    ARM_TARGETS,
    FROZEN_C4,
    SafeEvolveConfigV2,
)
from openevo_chembench.temperature_safe_evolve_v2.execution import (
    AcceptedPhysicalResultV2,
    SafeFormalExecutionV2,
)
from openevo_chembench.temperature_safe_evolve_v2.folds import phase_a_halves_v2
from openevo_chembench.temperature_safe_evolve_v2.frozen_c4 import (
    FrozenC4BundleV2,
    recover_frozen_c4_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.gates import (
    InfrastructureOutcomeV2,
    PairedGateInputV2,
    PhaseAArmGateV2,
    select_phase_a_target_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.ledger import SafeExperimentLedgerV2
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    TemperatureRuntimeServicesIdentityV1,
)

PRIOR_PRIVATE_SPLIT = (
    "state/chembench_temperature_full_evolve_v1/preflights/"
    "preflight-20260802T190753Z-b625a8ce/split"
)
PRIOR_AGGREGATE = (
    "state/chembench_temperature_full_evolve_v1/runs/"
    "stv3-temperature-full-evolve-v1-20260802T191250Z/private/"
    "aggregate_report_input_v1.json"
)


class PhaseAError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class PhaseAInputsV2:
    frozen_c4: FrozenC4BundleV2
    test: tuple[PrivateChemBench4KTask, ...]
    dev: tuple[PrivateChemBench4KTask, ...]
    h0_uids: frozenset[str]
    h1_uids: frozenset[str]
    half_assignment_sha256: str
    prior_baseline_only_ordinals: tuple[int, ...]

    def __repr__(self) -> str:
        return "PhaseAInputsV2(<private-tasks-and-payloads-redacted>)"


@dataclass(frozen=True, slots=True)
class PhaseAOutcomeV2:
    selected_target: str | None
    target_selection_receipt: dict[str, object]
    arm_metrics: tuple[dict[str, object], ...]
    factorial_effects: dict[str, object]
    private_result_path: Path


def load_phase_a_inputs_v2(*, repository_root: Path, config: SafeEvolveConfigV2) -> PhaseAInputsV2:
    frozen = recover_frozen_c4_v2(repository_root)
    dataset_root = repository_root / str(config.payload["dataset"]["root"])
    loader = ChemBench4KDatasetLoader(snapshot_root=dataset_root)
    split = build_temperature_split_v1(loader)
    rendered = render_temperature_split_artifacts_v1(split)
    old_root = repository_root / PRIOR_PRIVATE_SPLIT
    expected = {
        "train": rendered.train_private_manifest,
        "test": rendered.test_private_manifest,
        "reserve": rendered.reserve_private_manifest,
    }
    for name, payload in expected.items():
        if _read_regular(old_root / f"private/{name}_manifest.jsonl", 4 * 1024 * 1024) != payload:
            raise PhaseAError("PHASE_A_PRIOR_TEST_MANIFEST_DRIFT")
    if len(split.test) != 100:
        raise PhaseAError("PHASE_A_PRIOR_TEST_COUNT_INVALID")
    dev = tuple(loader.load_category("Temperature_Prediction", split="dev"))
    h0_values, h1_values, half_sha = phase_a_halves_v2(tuple(task.uid for task in split.test))
    h0 = frozenset(h0_values)
    h1 = frozenset(h1_values)
    prior = json.loads(_read_regular(repository_root / PRIOR_AGGREGATE, 4 * 1024 * 1024))
    baseline = tuple(bool(value) for value in prior["baseline_test"]["correctness"])
    evolved = tuple(bool(value) for value in prior["evolved_test"]["correctness"])
    if len(baseline) != 100 or len(evolved) != 100:
        raise PhaseAError("PHASE_A_PRIOR_RESULT_VECTOR_INVALID")
    baseline_only = tuple(
        index
        for index, (base, changed) in enumerate(zip(baseline, evolved, strict=True))
        if base and not changed
    )
    if len(baseline_only) != 11:
        raise PhaseAError("PHASE_A_PRIOR_BASELINE_ONLY_SET_INVALID")
    return PhaseAInputsV2(
        frozen_c4=frozen,
        test=split.test,
        dev=dev,
        h0_uids=h0,
        h1_uids=h1,
        half_assignment_sha256=half_sha,
        prior_baseline_only_ordinals=baseline_only,
    )


class PhaseARunnerV2:
    def __init__(
        self,
        *,
        repository_root: Path,
        campaign_run_id: str,
        campaign_root: Path,
        config: SafeEvolveConfigV2,
        runtime: TemperatureRuntimeServicesIdentityV1,
        inputs: PhaseAInputsV2,
        resume: bool,
    ) -> None:
        self.repository = repository_root
        self.campaign_run_id = campaign_run_id
        self.root = campaign_root
        self.config = config
        self.runtime = runtime
        self.inputs = inputs
        self.resume = resume
        if resume:
            _require_private_directory(self.root)
        else:
            _fresh_private_directory(self.root)
        (self.root / "private").mkdir(mode=0o700, exist_ok=True)
        (self.root / "private").chmod(0o700)

    def run(self) -> PhaseAOutcomeV2:
        contexts = {
            arm: _arm_context(self.inputs.frozen_c4, targets)
            for arm, targets in ARM_TARGETS.items()
        }
        # Global no-GT barrier: all 800 completions close before any correctness
        # is computed or written to a Reflector/controller-visible structure.
        for arm_index, arm in enumerate(ARM_TARGETS):
            arm_run_id = f"{self.campaign_run_id}-ablation-{arm.lower()}"
            arm_root = self.root / "phase_a" / arm.lower()
            if self.resume and arm_root.exists():
                _require_private_directory(arm_root)
            else:
                _fresh_private_directory(arm_root)
            _ensure_empty_arm_core(arm_root)
            with SafeExperimentLedgerV2(
                path=arm_root / "private/events.jsonl", run_id=arm_run_id
            ) as ledger:
                _append_phase_a_once(
                    ledger,
                    "PHASE_A_ARM_CREATED",
                    {
                        "arm_id": arm,
                        "arm_run_id": arm_run_id,
                        "target_ids": list(ARM_TARGETS[arm]),
                        "context_binding_sha256": contexts[arm].binding_sha256,
                        "feedback_enabled": False,
                        "reflector_calls": 0,
                        "core_jobs": 0,
                        "artifact_updates": 0,
                    },
                    require_genesis=True,
                )
                engine = SafeFormalExecutionV2(
                    runtime=self.runtime,
                    ledger=ledger,
                    checkpoint_root=arm_root / "private/calls",
                    cooldown_seconds=self.config.cooldown_seconds,
                    candidate_logical_limit=100,
                    reflector_logical_limit=0,
                )
                workspace = (
                    None
                    if not (contexts[arm].skill or contexts[arm].agent_system)
                    else arm_root / "workspaces" / f"context-{contexts[arm].binding_sha256[:16]}"
                )
                for ordinal, task in enumerate(self.inputs.test):
                    prompt = render_official_five_shot_prompt(
                        task.to_public(), category_dev=self.inputs.dev
                    )
                    engine.run_candidate(
                        task=task.to_public(),
                        prompt=prompt,
                        context=contexts[arm],
                        context_workspace=workspace,
                        phase="phasea",
                        block_id=arm.lower(),
                        task_ordinal=ordinal,
                        run_id=arm_run_id,
                    )
                    self._write_status(
                        phase="PHASE_A_INFERENCE",
                        arm=arm,
                        accepted=arm_index * 100 + ordinal + 1,
                    )
                _append_phase_a_once(
                    ledger,
                    "BLOCK_INFERENCE_CLOSED",
                    {
                        "phase": "phase_a",
                        "arm_id": arm,
                        "accepted_candidate_count": 100,
                        "gt_visible_during_inference": False,
                    },
                )

        self._require_global_inference_closure()
        private_vectors: dict[str, tuple[bool, ...]] = {}
        infra: dict[str, InfrastructureOutcomeV2] = {}
        private_evaluations: dict[str, list[dict[str, object]]] = {}
        for arm in ARM_TARGETS:
            arm_run_id = f"{self.campaign_run_id}-ablation-{arm.lower()}"
            arm_root = self.root / "phase_a" / arm.lower()
            with SafeExperimentLedgerV2(
                path=arm_root / "private/events.jsonl", run_id=arm_run_id
            ) as ledger:
                _append_phase_a_once(
                    ledger,
                    "GT_RELEASED",
                    {
                        "phase": "phase_a",
                        "barrier": "ALL_EIGHT_ARMS_800_ACCEPTED",
                        "accepted_candidate_count": 800,
                    },
                )
                engine = SafeFormalExecutionV2(
                    runtime=self.runtime,
                    ledger=ledger,
                    checkpoint_root=arm_root / "private/calls",
                    cooldown_seconds=self.config.cooldown_seconds,
                    candidate_logical_limit=100,
                    reflector_logical_limit=0,
                )
                context = contexts[arm]
                workspace = (
                    None
                    if not (context.skill or context.agent_system)
                    else arm_root / "workspaces" / f"context-{context.binding_sha256[:16]}"
                )
                existing_evaluations = {
                    str(event["payload"]["logical_call_id"]): json.loads(
                        str(event["payload"]["evaluation_json"])
                    )
                    for event in ledger.events
                    if event["kind"] == "CALL_EVALUATED"
                }
                evaluations: list[dict[str, object]] = []
                for ordinal, task in enumerate(self.inputs.test):
                    accepted = engine.run_candidate(
                        task=task.to_public(),
                        prompt=render_official_five_shot_prompt(
                            task.to_public(), category_dev=self.inputs.dev
                        ),
                        context=context,
                        context_workspace=workspace,
                        phase="phasea",
                        block_id=arm.lower(),
                        task_ordinal=ordinal,
                        run_id=arm_run_id,
                    )
                    payload = existing_evaluations.get(accepted.logical_call_id)
                    if payload is None:
                        payload = _evaluate_private(task, accepted)
                        encoded = canonical_json_bytes(payload).decode("utf-8")
                        ledger.append(
                            "CALL_EVALUATED",
                            {
                                "logical_call_id": accepted.logical_call_id,
                                "evaluation_json": encoded,
                                "evaluation_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                            },
                        )
                    evaluations.append(payload)
                private_evaluations[arm] = evaluations
                private_vectors[arm] = tuple(bool(value["correct"]) for value in evaluations)
                infra[arm] = _infrastructure(ledger, evaluations)
                _append_phase_a_once(
                    ledger,
                    "PHASE_A_CLOSED",
                    {
                        "arm_id": arm,
                        "accepted_candidate_count": 100,
                        "evaluated_count": 100,
                        "feedback_enabled": False,
                        "reflector_calls": 0,
                        "core_jobs": 0,
                    },
                )
        metrics, selection, effects = self._statistics(
            private_vectors=private_vectors,
            infrastructure=infra,
            private_evaluations=private_evaluations,
        )
        private_result = {
            "schema_version": "TemperatureSafePhaseAPrivateResultV2",
            "campaign_run_id": self.campaign_run_id,
            "half_assignment_sha256": self.inputs.half_assignment_sha256,
            "evaluations": private_evaluations,
            "correctness": {key: list(value) for key, value in private_vectors.items()},
            "prior_baseline_only_ordinals": list(self.inputs.prior_baseline_only_ordinals),
        }
        path = self.root / "private/phase_a_private_result.json"
        _persist_exact(path, canonical_json_bytes(private_result))
        _persist_exact(
            self.root / "private/phase_a_public_result.json",
            canonical_json_bytes(
                {
                    "schema_version": "TemperatureSafePhaseAPublicResultV2",
                    "campaign_run_id": self.campaign_run_id,
                    "arm_metrics": list(metrics),
                    "factorial_effects": effects,
                    "target_selection": selection,
                    "contains_uid": False,
                    "contains_prediction": False,
                    "contains_target": False,
                }
            ),
        )
        self._write_status(phase="PHASE_A_COMPLETE", arm=None, accepted=800)
        return PhaseAOutcomeV2(
            selected_target=selection["selected_target"],
            target_selection_receipt=selection,
            arm_metrics=metrics,
            factorial_effects=effects,
            private_result_path=path,
        )

    def _require_global_inference_closure(self) -> None:
        total = 0
        for arm in ARM_TARGETS:
            arm_run_id = f"{self.campaign_run_id}-ablation-{arm.lower()}"
            path = self.root / "phase_a" / arm.lower() / "private/events.jsonl"
            with SafeExperimentLedgerV2(path=path, run_id=arm_run_id) as ledger:
                accepted = sum(event["kind"] == "CALL_ACCEPTED" for event in ledger.events)
                block_closed = [
                    event for event in ledger.events if event["kind"] == "BLOCK_INFERENCE_CLOSED"
                ]
                evaluated_indices = [
                    index
                    for index, event in enumerate(ledger.events)
                    if event["kind"] == "CALL_EVALUATED"
                ]
                release_indices = [
                    index
                    for index, event in enumerate(ledger.events)
                    if event["kind"] == "GT_RELEASED"
                    and event["payload"]
                    == {
                        "phase": "phase_a",
                        "barrier": "ALL_EIGHT_ARMS_800_ACCEPTED",
                        "accepted_candidate_count": 800,
                    }
                ]
                if (
                    accepted != 100
                    or len(block_closed) != 1
                    or block_closed[0]["payload"].get("accepted_candidate_count") != 100
                    or (
                        evaluated_indices
                        and (
                            len(release_indices) != 1
                            or release_indices[0] > min(evaluated_indices)
                        )
                    )
                ):
                    raise PhaseAError("PHASE_A_GLOBAL_GT_BARRIER_INVALID")
                total += accepted
        if total != 800:
            raise PhaseAError("PHASE_A_ACCEPTED_CALL_COUNT_INVALID")

    def _statistics(
        self,
        *,
        private_vectors: dict[str, tuple[bool, ...]],
        infrastructure: dict[str, InfrastructureOutcomeV2],
        private_evaluations: dict[str, list[dict[str, object]]],
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object], dict[str, object]]:
        base = private_vectors["A0"]
        h0_indices = tuple(
            index for index, task in enumerate(self.inputs.test) if task.uid in self.inputs.h0_uids
        )
        h1_indices = tuple(
            index for index, task in enumerate(self.inputs.test) if task.uid in self.inputs.h1_uids
        )
        rows: list[dict[str, object]] = []
        gates: dict[str, PhaseAArmGateV2] = {}
        for arm, targets in ARM_TARGETS.items():
            vector = private_vectors[arm]
            paired = paired_binary_metrics_v1(base, vector, bootstrap_seed=20260803)
            h0_paired = paired_binary_metrics_v1(
                tuple(base[i] for i in h0_indices),
                tuple(vector[i] for i in h0_indices),
                bootstrap_seed=20260803,
            )
            h1_paired = paired_binary_metrics_v1(
                tuple(base[i] for i in h1_indices),
                tuple(vector[i] for i in h1_indices),
                bootstrap_seed=20260803,
            )
            target_bytes = sum(
                self.inputs.frozen_c4.artifact(target).utf8_bytes for target in targets
            )
            row = {
                "arm_id": arm,
                "target_ids": list(targets),
                "correct": sum(vector),
                "n": 100,
                "accuracy": sum(vector) / 100,
                "paired_vs_a0": paired.to_dict(),
                "positive_flips": paired.candidate_only_correct,
                "negative_flips": paired.reference_only_correct,
                "utility": paired.candidate_only_correct - 2 * paired.reference_only_correct,
                "injected_bytes_per_question": target_bytes,
                "parser_success": infrastructure[arm].parser_success_count,
                "retries": infrastructure[arm].retry_count,
                "failures": infrastructure[arm].failure_count,
                "timeouts": infrastructure[arm].timeout_count,
                "old_baseline_only_recovered": sum(
                    vector[index] for index in self.inputs.prior_baseline_only_ordinals
                ),
                "old_baseline_only_count": 11,
                "h0_correct": h0_paired.candidate_correct,
                "h0_delta_pp": h0_paired.delta_percentage_points,
                "h0_positive_flips": h0_paired.candidate_only_correct,
                "h0_negative_flips": h0_paired.reference_only_correct,
                "h0_utility": h0_paired.candidate_only_correct
                - 2 * h0_paired.reference_only_correct,
                "h1_correct": h1_paired.candidate_correct,
                "h1_delta_pp": h1_paired.delta_percentage_points,
                "h1_positive_flips": h1_paired.candidate_only_correct,
                "h1_negative_flips": h1_paired.reference_only_correct,
                "h1_utility": h1_paired.candidate_only_correct
                - 2 * h1_paired.reference_only_correct,
            }
            rows.append(row)
            if arm in {"A1", "A2"}:
                overall = PairedGateInputV2(
                    base, vector, infrastructure["A0"], infrastructure[arm]
                )
                h0 = PairedGateInputV2(
                    tuple(base[i] for i in h0_indices),
                    tuple(vector[i] for i in h0_indices),
                    _subset_infra(
                        infrastructure["A0"],
                        private_evaluations["A0"],
                        h0_indices,
                    ),
                    _subset_infra(
                        infrastructure[arm],
                        private_evaluations[arm],
                        h0_indices,
                    ),
                )
                h1 = PairedGateInputV2(
                    tuple(base[i] for i in h1_indices),
                    tuple(vector[i] for i in h1_indices),
                    _subset_infra(
                        infrastructure["A0"],
                        private_evaluations["A0"],
                        h1_indices,
                    ),
                    _subset_infra(
                        infrastructure[arm],
                        private_evaluations[arm],
                        h1_indices,
                    ),
                )
                gates[arm] = PhaseAArmGateV2(
                    arm_id=arm,
                    overall=overall,
                    h0=h0,
                    h1=h1,
                    artifact_hash_valid=(
                        (
                            self.inputs.frozen_c4.artifact(targets[0]).utf8_bytes,
                            self.inputs.frozen_c4.artifact(targets[0]).payload_sha256,
                        )
                        == FROZEN_C4[targets[0]]
                    ),
                    injected_bytes=target_bytes,
                )
        selected, selection = select_phase_a_target_v2(gates["A1"], gates["A2"])
        del selected
        accuracy_pp = {row["arm_id"]: 100.0 * float(row["accuracy"]) for row in rows}
        effects = _factorial_effects(accuracy_pp)
        effects["half_assignment_sha256"] = self.inputs.half_assignment_sha256
        effects["status"] = "DESCRIPTIVE_REUSED_TEST_MECHANISM_DIAGNOSTIC"
        return tuple(rows), selection, effects

    def _write_status(self, *, phase: str, arm: str | None, accepted: int) -> None:
        payload = {
            "schema_version": "TemperatureSafeCampaignStatusV2",
            "campaign_run_id": self.campaign_run_id,
            "phase": phase,
            "phase_a_arm": arm,
            "accepted_candidate_count": accepted,
            "candidate_budget": 1300,
            "reflector_count": 0,
            "core_job_count": 0,
            "updated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "runner_pid": os.getpid(),
        }
        path = self.root / "status.json"
        write_private_file(path, canonical_json_bytes(payload), replace=True)


def _arm_context(bundle: FrozenC4BundleV2, targets: tuple[str, ...]) -> CandidateContextV2:
    if not targets:
        return CandidateContextV2.generation_zero()
    injections = tuple(
        TargetInjectionV2(
            target_id=target,  # type: ignore[arg-type]
            core_artifact_id=bundle.artifact(target).artifact_id,
            canonical_artifact_sha256=bundle.artifact(target).payload_sha256,
            canonical_artifact_utf8_bytes=bundle.artifact(target).utf8_bytes,
            payload=bundle.artifact(target).text,
            payload_sha256=bundle.artifact(target).payload_sha256,
            payload_utf8_bytes=bundle.artifact(target).utf8_bytes,
        )
        for target in targets
    )
    identity = sha256_bytes(canonical_json_bytes([value.binding() for value in injections]))
    return CandidateContextV2(
        state_identity_sha256=identity,
        injections=injections,
        mode="frozen_c4_ablation",
    )


def _evaluate_private(
    task: PrivateChemBench4KTask, accepted: AcceptedPhysicalResultV2
) -> dict[str, object]:
    evaluated = ChemBench4KPrivateEvaluator().evaluate(task=task, raw_completion=accepted.response)
    return {
        "schema_version": "TemperatureSafeCandidateEvaluationV2",
        "logical_call_id": accepted.logical_call_id,
        "task_uid": task.uid,
        "task_ordinal": accepted.task_ordinal,
        "phase": accepted.phase,
        "block_id": accepted.block_id,
        "context_hash": accepted.context_hash,
        "context_binding_sha256": accepted.context_binding_sha256,
        "official_prediction": evaluated.official.prediction,
        "official_parse_status": evaluated.official.status.value,
        "strict_prediction": evaluated.strict.prediction,
        "strict_parse_status": evaluated.strict.status.value,
        "correct": evaluated.correct,
        "response_sha256": accepted.response_sha256,
        "transcript_sha256": accepted.transcript_sha256,
    }


def _infrastructure(
    ledger: SafeExperimentLedgerV2, evaluations: list[dict[str, object]]
) -> InfrastructureOutcomeV2:
    claims = [event for event in ledger.events if event["kind"] == "CALL_CLAIMED"]
    failures = [event for event in ledger.events if event["kind"] == "CALL_NO_COMPLETION_FAILURE"]
    return InfrastructureOutcomeV2(
        accepted_count=100,
        parser_success_count=sum(
            value["official_parse_status"] == "parsed" for value in evaluations
        ),
        retry_count=len(claims) - 100,
        failure_count=len(failures),
        timeout_count=sum(
            "timeout" in str(event["payload"].get("terminal_task_status", "")).casefold()
            for event in failures
        ),
    )


def _subset_infra(
    value: InfrastructureOutcomeV2,
    evaluations: list[dict[str, object]],
    indices: tuple[int, ...],
) -> InfrastructureOutcomeV2:
    # Retries, failures, and timeouts remain arm-level selection requirements.
    # Parser parity is evaluated on the exact preregistered half so a parser
    # failure cannot disappear behind an all-or-nothing aggregate shortcut.
    parser = sum(evaluations[index]["official_parse_status"] == "parsed" for index in indices)
    return InfrastructureOutcomeV2(
        accepted_count=len(indices),
        parser_success_count=parser,
        retry_count=value.retry_count,
        failure_count=value.failure_count,
        timeout_count=value.timeout_count,
    )


def _factorial_effects(values: dict[str, float]) -> dict[str, object]:
    bits = {
        "A0": (-1, -1, -1),
        "A1": (1, -1, -1),
        "A2": (-1, 1, -1),
        "A3": (-1, -1, 1),
        "A4": (1, 1, -1),
        "A5": (1, -1, 1),
        "A6": (-1, 1, 1),
        "A7": (1, 1, 1),
    }
    contrasts = {
        "text_memory_main_effect_pp": lambda x: x[0],
        "skill_bundle_main_effect_pp": lambda x: x[1],
        "agent_system_main_effect_pp": lambda x: x[2],
        "memory_x_skill_pp": lambda x: x[0] * x[1],
        "memory_x_agent_pp": lambda x: x[0] * x[2],
        "skill_x_agent_pp": lambda x: x[1] * x[2],
        "memory_x_skill_x_agent_pp": lambda x: x[0] * x[1] * x[2],
    }
    return {
        "schema_version": "TemperatureSafePhaseAFactorialEffectsV2",
        **{
            name: sum(function(bits[arm]) * values[arm] for arm in bits) / 4.0
            for name, function in contrasts.items()
        },
    }


def _ensure_empty_arm_core(root: Path) -> None:
    core = root / "core"
    artifacts = core / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts.chmod(0o700)
    database = core / "evolution.sqlite3"
    store = EvolutionStore(db_path=database, artifact_root=artifacts)
    store.initialize()
    database.chmod(0o600)
    with store.connect() as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        changed = connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE type != ?", ("dataset",)
        ).fetchone()[0]
    if jobs or changed:
        raise PhaseAError("PHASE_A_ARM_CORE_NOT_EMPTY")


def _fresh_private_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        raise PhaseAError("PHASE_A_FRESH_ROOT_EXISTS")
    path.mkdir(mode=0o700)


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise PhaseAError("PHASE_A_PRIVATE_ROOT_INVALID")


def _read_regular(path: Path, maximum: int) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
    ):
        raise PhaseAError("PHASE_A_EVIDENCE_FILE_INVALID")
    return path.read_bytes()


def _persist_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        if _read_regular(path, len(payload) + 1) != payload:
            raise PhaseAError("PHASE_A_IMMUTABLE_RESULT_DRIFT")
    else:
        write_private_file(path, payload, replace=False)


def _append_phase_a_once(
    ledger: SafeExperimentLedgerV2,
    kind: str,
    payload: dict[str, object],
    *,
    require_genesis: bool = False,
) -> None:
    existing = [event["payload"] for event in ledger.events if event["kind"] == kind]
    if existing:
        if existing != [payload]:
            raise PhaseAError("PHASE_A_PROTOCOL_RECEIPT_DRIFT")
        return
    if require_genesis and ledger.events:
        raise PhaseAError("PHASE_A_ARM_CREATION_SEQUENCE_INVALID")
    ledger.append(kind, payload)


__all__ = [
    "PhaseAError",
    "PhaseAInputsV2",
    "PhaseAOutcomeV2",
    "PhaseARunnerV2",
    "load_phase_a_inputs_v2",
]
