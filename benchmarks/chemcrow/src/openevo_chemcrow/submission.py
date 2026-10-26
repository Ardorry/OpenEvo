from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .hashing import canonical_sha256, file_sha256
from .models import ArtifactKind, RubricScores, TaskItem
from .paper_evaluator import FROZEN_PAPER_TASK_IDS
from .replacement_ledger import verified_no_effect_attempt_count
from .three_artifact_aggregate import aggregate_three_artifact_results
from .three_artifact_models import THREE_ARTIFACT_ORDER, ThreeArtifactPairResult

SUBMISSION_SCHEMA_VERSION = "chemcrow_submission_v1"
PROTOCOL_VERSION = "chemcrowrun_v1"
TASK_MANIFEST_SHA256 = "8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604"
REPRODUCTION_CONTRACT: dict[str, Any] = {
    "protocol_version": PROTOCOL_VERSION,
    "task_manifest_sha256": TASK_MANIFEST_SHA256,
    "task_count": 14,
    "random_seed": 20260825,
    "expected_paid_model_calls": 112,
    "automatic_retries": 0,
    "replacement_policy": "explicit_verified_authority_only",
    "judge_protocol": "chemcrow-final-blind-evaluator-v1",
    "scoring_rule": "three_axes_0_to_4_sum_12_v1",
}
REQUIRED_MODEL_ROLES = frozenset(
    {
        "candidate",
        "reflector_memory",
        "reflector_skill_bundle",
        "reflector_agent_system",
        "evolution_evaluator",
        "final_evaluator",
    }
)
_ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_HEX40 = re.compile(r"[0-9a-f]{40}")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmissionModelBinding(_StrictModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    capture_mode: Literal["transcript"] = "transcript"


class ModelSubstitution(_StrictModel):
    variant: Literal["chemcrowrun_v1_model_substitution_v1"] = (
        "chemcrowrun_v1_model_substitution_v1"
    )
    historical_model: Literal["gpt-5.5"] = "gpt-5.5"
    actual_model: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def _different_model(self) -> ModelSubstitution:
        if self.actual_model == self.historical_model:
            raise ValueError("model substitution must identify a different model")
        return self


class SubmissionExecution(_StrictModel):
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_manifest_sha256: Literal[TASK_MANIFEST_SHA256] = TASK_MANIFEST_SHA256
    started_at: datetime
    completed_at: datetime
    random_seed: int
    model_bindings: dict[str, SubmissionModelBinding]
    model_substitution: ModelSubstitution | None = None
    automatic_retry_count: Literal[0] = 0
    replacements: list[SubmissionReplacement] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        payload = handler(self)
        if self.model_substitution is None:
            payload.pop("model_substitution", None)
        return payload

    @model_validator(mode="after")
    def _validate_execution(self) -> SubmissionExecution:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("submission timestamps must include a timezone")
        if self.completed_at < self.started_at:
            raise ValueError("submission completion precedes start")
        if set(self.model_bindings) != REQUIRED_MODEL_ROLES:
            raise ValueError("submission model roles differ from protocol")
        if self.model_substitution and any(
            binding.model != self.model_substitution.actual_model
            for binding in self.model_bindings.values()
        ):
            raise ValueError("model bindings differ from declared substitution")
        return self


class SubmissionRunRef(_StrictModel):
    run_id: str = Field(min_length=1, max_length=512)
    answer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SubmissionReplacement(_StrictModel):
    task_id: str
    phase: str
    failed_call_id: str
    replacement_call_id: str
    authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SubmissionArtifact(_StrictModel):
    artifact_type: ArtifactKind
    artifact_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_bytes: int = Field(ge=1)
    reflector_job_id: str
    reflector_run_id: str
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SubmissionBlindEvaluation(_StrictModel):
    baseline_scores: RubricScores
    evolved_scores: RubricScores
    winner: Literal["baseline", "evolved", "tie"]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_seal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provisional_llm_judged: Literal[True] = True


class SubmissionTaskResult(_StrictModel):
    task_id: str
    pair_id: str
    task_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    s0_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline: SubmissionRunRef
    evolved: SubmissionRunRef
    baseline_internal_scores: RubricScores
    evolved_internal_scores: RubricScores
    artifacts: list[SubmissionArtifact]
    injection_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reset_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blind_evaluation: SubmissionBlindEvaluation

    @model_validator(mode="after")
    def _validate_task_result(self) -> SubmissionTaskResult:
        if [item.artifact_type for item in self.artifacts] != list(THREE_ARTIFACT_ORDER):
            raise ValueError("submission artifact order differs")
        if len({item.artifact_id for item in self.artifacts}) != 3:
            raise ValueError("submission artifact IDs are not independent")
        if self.baseline.run_id == self.evolved.run_id:
            raise ValueError("submission Candidate runs are not independent")
        return self


class SubmissionAggregate(_StrictModel):
    task_count: Literal[14] = 14
    artifact_count: Literal[42] = 42
    mean_baseline: RubricScores
    mean_evolved: RubricScores
    mean_delta: SubmissionScoreDelta
    wins: int = Field(ge=0, le=14)
    losses: int = Field(ge=0, le=14)
    ties: int = Field(ge=0, le=14)

    @model_validator(mode="after")
    def _validate_counts(self) -> SubmissionAggregate:
        if self.wins + self.losses + self.ties != self.task_count:
            raise ValueError("submission pairwise counts differ from task count")
        return self


class SubmissionScoreDelta(_StrictModel):
    chemical_correctness: float = Field(ge=-4.0, le=4.0)
    reasoning_quality: float = Field(ge=-4.0, le=4.0)
    task_completion: float = Field(ge=-4.0, le=4.0)


class SubmissionEvidence(_StrictModel):
    audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_status: Literal["PASS"] = "PASS"
    raw_answers_included: Literal[False] = False
    secrets_included: Literal[False] = False
    host_paths_included: Literal[False] = False


class ChemCrowSubmission(_StrictModel):
    schema_version: Literal[SUBMISSION_SCHEMA_VERSION] = SUBMISSION_SCHEMA_VERSION
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    submission_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_kind: Literal["authoritative_original", "third_party_reproduction"]
    experiment_id: str = Field(min_length=1, max_length=512)
    task_ids: list[str]
    execution: SubmissionExecution
    judge_protocol: Literal["chemcrow-final-blind-evaluator-v1"] = (
        "chemcrow-final-blind-evaluator-v1"
    )
    scoring_rule: Literal["three_axes_0_to_4_sum_12_v1"] = "three_axes_0_to_4_sum_12_v1"
    results: list[SubmissionTaskResult]
    aggregate: SubmissionAggregate
    evidence: SubmissionEvidence
    human_review_status: Literal["PROVISIONAL_NOT_COMPLETED"] = "PROVISIONAL_NOT_COMPLETED"

    @model_validator(mode="after")
    def _validate_submission(self) -> ChemCrowSubmission:
        if self.run_kind == "authoritative_original" and self.execution.model_substitution:
            raise ValueError("a model substitution cannot be the authoritative original")
        expected = list(FROZEN_PAPER_TASK_IDS)
        if self.task_ids != expected or [item.task_id for item in self.results] != expected:
            raise ValueError("submission task IDs or order differ from frozen authority")
        if len({item.pair_id for item in self.results}) != 14:
            raise ValueError("submission pair IDs are not unique")
        run_ids = [
            run_id
            for item in self.results
            for run_id in (item.baseline.run_id, item.evolved.run_id)
        ]
        if len(set(run_ids)) != 28:
            raise ValueError("submission Candidate run IDs are not globally unique")
        artifact_ids = [
            artifact.artifact_id for item in self.results for artifact in item.artifacts
        ]
        if len(set(artifact_ids)) != 42:
            raise ValueError("submission artifact IDs are not globally unique")
        expected_aggregate = aggregate_submission_results(self.results)
        if self.aggregate != expected_aggregate:
            raise ValueError("submission aggregate differs from task rows")
        payload = self.model_dump(mode="json", exclude={"submission_id"})
        if self.submission_id != canonical_sha256(payload):
            raise ValueError("submission ID differs from canonical payload")
        return self


def aggregate_submission_results(results: list[SubmissionTaskResult]) -> SubmissionAggregate:
    if len(results) != 14:
        raise ValueError("submission aggregate requires exactly 14 task rows")

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    baseline = [item.blind_evaluation.baseline_scores for item in results]
    evolved = [item.blind_evaluation.evolved_scores for item in results]
    winners = Counter(item.blind_evaluation.winner for item in results)
    return SubmissionAggregate(
        mean_baseline=RubricScores(
            chemical_correctness=mean([item.chemical_correctness for item in baseline]),
            reasoning_quality=mean([item.reasoning_quality for item in baseline]),
            task_completion=mean([item.task_completion for item in baseline]),
        ),
        mean_evolved=RubricScores(
            chemical_correctness=mean([item.chemical_correctness for item in evolved]),
            reasoning_quality=mean([item.reasoning_quality for item in evolved]),
            task_completion=mean([item.task_completion for item in evolved]),
        ),
        mean_delta=SubmissionScoreDelta(
            chemical_correctness=mean(
                [
                    e.chemical_correctness - b.chemical_correctness
                    for b, e in zip(baseline, evolved)
                ]
            ),
            reasoning_quality=mean(
                [e.reasoning_quality - b.reasoning_quality for b, e in zip(baseline, evolved)]
            ),
            task_completion=mean(
                [e.task_completion - b.task_completion for b, e in zip(baseline, evolved)]
            ),
        ),
        wins=winners["evolved"],
        losses=winners["baseline"],
        ties=winners["tie"],
    )


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.fullmatch(value)
        if match:
            resolved = os.environ.get(match.group(1))
            if not resolved:
                raise ValueError(f"required environment variable is absent: {match.group(1)}")
            return resolved
        return value
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    return value


def _config_path(config_path: Path, value: object) -> Path:
    if not isinstance(value, str):
        raise TypeError("configured path must be a string")
    path = Path(value)
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _answer_hash(answer: str) -> str:
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()


def _role_configs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reflectors = config.get("reflectors")
    if not isinstance(reflectors, dict):
        raise TypeError("submission config has no three-Reflector mapping")
    return {
        "candidate": config["candidate"],
        "reflector_memory": reflectors["memory"],
        "reflector_skill_bundle": reflectors["skill_bundle"],
        "reflector_agent_system": reflectors["agent_system"],
        "evolution_evaluator": config["evolution_evaluator"],
        "final_evaluator": config["final_evaluator"],
    }


def _git_identity(repository_root: Path, source_commit: str) -> None:
    head = subprocess.run(
        ["git", "-C", os.fspath(repository_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != source_commit:
        raise ValueError("submission source commit differs from checkout HEAD")
    dirty = subprocess.run(
        ["git", "-C", os.fspath(repository_root), "status", "--porcelain=v1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise ValueError("submission export requires a clean source checkout")


def collect_submission_replacements(
    ledger_root: Path, experiment_id: str, task_ids: list[str], audit: dict[str, Any]
) -> list[SubmissionReplacement]:
    """Export only verified terminal histories and reconcile them with the PASS audit."""
    count_fields = {
        "baseline_candidate": "verified_pre_candidate_no_effect_attempt_count",
        "baseline_internal_evaluator": "verified_baseline_evaluator_no_effect_attempt_count",
        "evolved_candidate": "verified_evolved_candidate_no_effect_attempt_count",
        **{
            f"reflector_{kind}": "verified_reflector_no_effect_attempt_count"
            for kind in ("memory", "skill_bundle", "agent_system")
        },
    }
    counts: Counter = Counter()
    replacements = []
    for task_id in task_ids:
        pair_id = f"{experiment_id}--{task_id}"
        for path in sorted((ledger_root / pair_id).glob("*.json")):
            claim = json.loads(path.read_text())
            if "failed_attempts" not in claim:
                continue
            phase = path.stem
            if (
                phase not in count_fields
                or claim.get("phase") != phase
                or claim.get("status") != "terminal"
                or claim.get("authority_sha256") != canonical_sha256(claim.get("authority"))
                or claim.get("receipt_sha256") != canonical_sha256(claim.get("receipt"))
            ):
                raise ValueError("replacement claim is not sealed with matching authority")
            counts[count_fields[phase]] += verified_no_effect_attempt_count(
                claim, pair_id=pair_id, phase=phase
            )
            terminal = claim["receipt"]
            final_id = (
                terminal.get("run_id")
                or terminal.get("evaluator_run_id")
                or terminal.get("reflector_run_id")
            )
            attempts = claim["failed_attempts"]
            for index, attempt in enumerate(attempts):
                failed_id = attempt["receipt"].get("core_task_id")
                successor = (
                    attempts[index + 1]["receipt"].get("core_task_id")
                    if index + 1 < len(attempts)
                    else final_id
                )
                if (
                    not all(isinstance(value, str) and value for value in (failed_id, successor))
                    or failed_id == successor
                ):
                    raise ValueError("replacement call identities are absent or ambiguous")
                replacements.append(
                    SubmissionReplacement(
                        task_id=task_id,
                        phase=phase,
                        failed_call_id=failed_id,
                        replacement_call_id=successor,
                        authority_sha256=attempt["receipt_sha256"],
                    )
                )
    if any(counts[key] != audit.get(key, 0) for key in set(count_fields.values())):
        raise ValueError("submission replacement counts differ from completed-run audit")
    return replacements


def export_submission(
    *,
    config_path: Path,
    audit_path: Path,
    aggregate_path: Path,
    repository_root: Path,
    source_commit: str,
    provider: str,
    started_at: datetime,
    completed_at: datetime,
    run_kind: Literal["authoritative_original", "third_party_reproduction"],
) -> ChemCrowSubmission:
    _git_identity(repository_root.resolve(), source_commit)
    if _HEX40.fullmatch(source_commit) is None:
        raise ValueError("submission source commit is not a full Git hash")
    config_path = config_path.resolve()
    config = _resolve_env(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    if not isinstance(config, dict):
        raise TypeError("submission config must be an object")
    if config.get("artifact_protocol") != "three_isolated_core_native_v2":
        raise ValueError("submission protocol is not Core-native three-artifact v2")
    if config.get("execution_scope", "full_g1_g2_pair") != "full_g1_g2_pair":
        raise ValueError("submission export requires one complete G1/G2 run")

    manifest_path = _config_path(config_path, config["task_manifest"])
    if file_sha256(manifest_path) != TASK_MANIFEST_SHA256:
        raise ValueError("submission task manifest differs from frozen authority")
    tasks = [
        TaskItem.model_validate_json(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if tuple(item.task_id for item in tasks) != FROZEN_PAPER_TASK_IDS:
        raise ValueError("submission task order differs from frozen authority")

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("status") != "PASS" or audit.get("task_count") != 14:
        raise ValueError("submission completed-run audit is not PASS for 14 tasks")
    run_root = _config_path(config_path, config["run_root"])
    results: list[ThreeArtifactPairResult] = []
    for task in tasks:
        pair_id = f"{config['experiment_id']}--{task.task_id}"
        pair_path = run_root / pair_id / "pair.result.json"
        pair = ThreeArtifactPairResult.model_validate_json(pair_path.read_text(encoding="utf-8"))
        if pair.pair_id != pair_id:
            raise ValueError("submission pair identity differs")
        results.append(pair)

    observed_aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    expected_aggregate = aggregate_three_artifact_results(results)
    if observed_aggregate != expected_aggregate:
        raise ValueError("submission aggregate differs from sealed pair rows")

    rows: list[SubmissionTaskResult] = []
    for pair in results:
        rows.append(
            SubmissionTaskResult(
                task_id=pair.task_id,
                pair_id=pair.pair_id,
                task_prompt_sha256=pair.task_prompt_hash,
                s0_sha256=pair.s0_hash,
                baseline=SubmissionRunRef(
                    run_id=pair.baseline.run_id,
                    answer_sha256=_answer_hash(pair.baseline.answer),
                ),
                evolved=SubmissionRunRef(
                    run_id=pair.evolved.run_id,
                    answer_sha256=_answer_hash(pair.evolved.answer),
                ),
                baseline_internal_scores=pair.baseline_internal_evaluation.scores,
                evolved_internal_scores=pair.evolved_internal_evaluation.scores,
                artifacts=[
                    SubmissionArtifact(
                        artifact_type=item.artifact_type,
                        artifact_id=item.artifact_id,
                        artifact_sha256=item.artifact_hash,
                        artifact_bytes=item.size_bytes,
                        reflector_job_id=item.reflector_job_id,
                        reflector_run_id=item.reflector_run_id,
                        prompt_sha256=item.prompt_hash,
                        input_evidence_sha256=item.input_evidence_hash,
                    )
                    for item in pair.artifact_bundle.artifacts
                ],
                injection_receipt_sha256=pair.injection_receipt.receipt_sha256,
                reset_receipt_sha256=pair.reset_receipt_sha256,
                blind_evaluation=SubmissionBlindEvaluation(
                    baseline_scores=pair.final_evaluation.baseline_scores,
                    evolved_scores=pair.final_evaluation.evolved_scores,
                    winner=pair.final_evaluation.winner,
                    confidence=pair.final_evaluation.confidence,
                    mapping_seal_sha256=pair.final_evaluation.mapping_seal_sha256,
                ),
            )
        )
    role_configs = _role_configs(config)
    substitution = (
        ModelSubstitution.model_validate(config["model_substitution"])
        if config.get("model_substitution") is not None
        else None
    )
    expected_model = substitution.actual_model if substitution else "gpt-5.5"
    if any(role["agent"]["model_name"] != expected_model for role in role_configs.values()):
        raise ValueError("submission export requires an explicit matching model condition")
    bindings = {
        role: SubmissionModelBinding(
            provider=provider,
            model=str(role_config["agent"]["model_name"]),
        )
        for role, role_config in role_configs.items()
    }
    execution = SubmissionExecution(
        model_substitution=config.get("model_substitution"),
        source_commit=source_commit,
        config_sha256=file_sha256(config_path),
        started_at=started_at,
        completed_at=completed_at,
        random_seed=int(config.get("random_seed", 20260825)),
        model_bindings=bindings,
        replacements=collect_submission_replacements(
            _config_path(config_path, config.get("ledger_root", str(run_root / "claims"))),
            str(config["experiment_id"]),
            list(FROZEN_PAPER_TASK_IDS),
            audit,
        ),
    )
    base_payload: dict[str, Any] = {
        "schema_version": SUBMISSION_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "run_kind": run_kind,
        "experiment_id": str(config["experiment_id"]),
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "execution": execution.model_dump(mode="json"),
        "judge_protocol": "chemcrow-final-blind-evaluator-v1",
        "scoring_rule": "three_axes_0_to_4_sum_12_v1",
        "results": [item.model_dump(mode="json") for item in rows],
        "aggregate": aggregate_submission_results(rows).model_dump(mode="json"),
        "evidence": SubmissionEvidence(
            audit_sha256=file_sha256(audit_path),
            aggregate_sha256=file_sha256(aggregate_path),
        ).model_dump(mode="json"),
        "human_review_status": "PROVISIONAL_NOT_COMPLETED",
    }
    return ChemCrowSubmission(
        submission_id=canonical_sha256(base_payload),
        **base_payload,
    )


def validate_submission_file(path: Path) -> ChemCrowSubmission:
    return ChemCrowSubmission.model_validate_json(path.read_text(encoding="utf-8"))


def aggregate_submissions(submissions: list[ChemCrowSubmission]) -> dict[str, Any]:
    if not submissions:
        raise ValueError("at least one submission is required")
    if len({item.submission_id for item in submissions}) != len(submissions):
        raise ValueError("duplicate submission ID")
    compatibility = {
        (
            item.schema_version,
            item.protocol_version,
            tuple(item.task_ids),
            item.judge_protocol,
            tuple(
                sorted(
                    (role, binding.provider, binding.model)
                    for role, binding in item.execution.model_bindings.items()
                )
            ),
            item.execution.model_substitution.variant
            if item.execution.model_substitution
            else None,
        )
        for item in submissions
    }
    if len(compatibility) != 1:
        raise ValueError("submissions are not protocol-compatible")

    def total(scores: RubricScores) -> float:
        return scores.chemical_correctness + scores.reasoning_quality + scores.task_completion

    entries = [
        {
            "submission_id": item.submission_id,
            "experiment_id": item.experiment_id,
            "run_kind": item.run_kind,
            "source_commit": item.execution.source_commit,
            "completed_at": item.execution.completed_at.isoformat(),
            "mean_baseline_total": total(item.aggregate.mean_baseline),
            "mean_evolved_total": total(item.aggregate.mean_evolved),
            "mean_delta_total": total(item.aggregate.mean_delta),
            "wins": item.aggregate.wins,
            "losses": item.aggregate.losses,
            "ties": item.aggregate.ties,
            "human_review_status": item.human_review_status,
        }
        for item in submissions
    ]
    entries.sort(key=lambda item: (-item["mean_evolved_total"], item["submission_id"]))
    return {
        "schema_version": "chemcrow_submission_aggregate_v1",
        "status": "PASS",
        "protocol_version": submissions[0].protocol_version,
        "task_manifest_sha256": submissions[0].execution.task_manifest_sha256,
        "judge_protocol": submissions[0].judge_protocol,
        "submission_count": len(submissions),
        "entries": entries,
    }


def write_submission_schema(path: Path) -> None:
    rendered = json.dumps(ChemCrowSubmission.model_json_schema(), indent=2, sort_keys=True) + "\n"
    path.write_text(rendered, encoding="utf-8")
