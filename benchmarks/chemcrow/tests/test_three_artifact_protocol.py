from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from openevo_chemcrow.cli import _assert_authoritative_role_models, _bounded_task_prefix
from openevo_chemcrow.feedback import reflector_feedback_payload, runtime_feedback
from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.ledger import PhaseLedger
from openevo_chemcrow.models import (
    ArtifactKind,
    EvaluatorFeedback,
    FeedbackMode,
    RubricScores,
    TaskItem,
    Trajectory,
)
from openevo_chemcrow.protocol import BlindJudgeResult
from openevo_chemcrow.replacement_ledger import VerifiedReplacementPhaseLedger
from openevo_chemcrow.three_artifact_evolution import (
    ThreeIsolatedEvolutionEngine,
    _find_forbidden_text,
    _strict_reflector_markdown,
    detect_artifact_duplicates,
    project_core_native_evolution_feedback,
)
from openevo_chemcrow.three_artifact_models import (
    CORE_FULL_WORKER_PROMPT_PROFILE,
    CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL,
    CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
    THREE_ARTIFACT_ORDER,
    ArtifactSeparationPolicy,
    CoreInjectionReceiptSummary,
    ThreeArtifactBundleReceipt,
    ThreeArtifactGenerationResult,
    ThreeArtifactPairResult,
    ThreeArtifactReceipt,
)
from openevo_chemcrow.three_artifact_protocol import (
    ThreeArtifactTaskLocalProtocolRunner,
)
from openevo_chemcrow.three_artifact_runtime import (
    _ordered_candidate_artifact_ids,
    _three_artifact_injection_receipt,
    candidate_pair_request_parity,
)


class FakeThreeCandidate:
    config_sha256 = "s0"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[ArtifactKind, str]]] = []

    def run_candidate_with_receipt(
        self,
        *,
        task,
        role,
        artifact_ids_by_type,
        pair_id,
        mcp_url,
        run_id=None,
    ):
        mapping = dict(artifact_ids_by_type)
        self.calls.append((task.task_id, role, mapping))
        ids = [mapping[kind] for kind in THREE_ARTIFACT_ORDER] if mapping else []
        trajectory = Trajectory(
            run_id=run_id or f"{pair_id}-{role}",
            task_id=task.task_id,
            role=role,
            status="COMPLETED",
            answer=f"{role} answer for {task.task_id}",
            candidate_config_sha256=self.config_sha256,
            artifact_ids=ids,
        )
        receipt = None
        if role == "evolved":
            receipt = CoreInjectionReceiptSummary(
                schema_version="3",
                receipt_sha256="9" * 64,
                memory_artifact_id=mapping[ArtifactKind.TEXT_MEMORY],
                skill_artifact_id=mapping[ArtifactKind.SKILL_BUNDLE],
                agent_system_artifact_id=mapping[ArtifactKind.AGENT_SYSTEM],
            )
        return trajectory, receipt


def test_real_candidate_port_accepts_bare_baseline_inventory():
    assert _ordered_candidate_artifact_ids(role="baseline", artifact_ids_by_type={}) == []
    with pytest.raises(ValueError, match="baseline cannot receive"):
        _ordered_candidate_artifact_ids(
            role="baseline",
            artifact_ids_by_type={ArtifactKind.TEXT_MEMORY: "unexpected"},
        )


def test_real_candidate_port_requires_exact_distinct_evolved_inventory():
    mapping = {
        ArtifactKind.TEXT_MEMORY: "memory",
        ArtifactKind.SKILL_BUNDLE: "skill",
        ArtifactKind.AGENT_SYSTEM: "agent-system",
    }
    assert _ordered_candidate_artifact_ids(role="evolved", artifact_ids_by_type=mapping) == [
        "memory",
        "skill",
        "agent-system",
    ]
    with pytest.raises(ValueError, match="exactly memory"):
        _ordered_candidate_artifact_ids(
            role="evolved",
            artifact_ids_by_type={ArtifactKind.TEXT_MEMORY: "memory"},
        )
    with pytest.raises(ValueError, match="one artifact ID"):
        _ordered_candidate_artifact_ids(
            role="evolved",
            artifact_ids_by_type={kind: "same" for kind in THREE_ARTIFACT_ORDER},
        )


class FakeThreeEvolution:
    def __init__(self) -> None:
        self.inputs: list[dict] = []

    def evolve_all(self, *, task, baseline, feedback_payload, pair_id):
        self.inputs.append(json.loads(json.dumps(feedback_payload)))
        evidence_hash = canonical_sha256(
            {
                "task": {
                    "task_id": task.task_id,
                    "prompt": task.prompt,
                    "sanitized_item_sha256": task.sanitized_item_sha256,
                },
                "baseline": baseline.model_dump(mode="json"),
                "feedback": feedback_payload,
            }
        )
        artifacts = []
        for index, kind in enumerate(THREE_ARTIFACT_ORDER):
            artifacts.append(
                ThreeArtifactReceipt(
                    task_id=task.task_id,
                    pair_id=pair_id,
                    parent_run_id=baseline.run_id,
                    reflector_job_id=f"job-{pair_id}-{index}",
                    reflector_run_id=f"run-{pair_id}-{index}",
                    model="gpt-5.5",
                    system_prompt_hash=f"{index + 1}" * 64,
                    prompt_hash=f"{index + 4}" * 64,
                    input_evidence_hash=evidence_hash,
                    artifact_type=kind,
                    artifact_id=f"art-{pair_id}-{kind.value}",
                    artifact_hash=f"{index + 7}" * 64,
                    normalized_text_hash=f"{index + 3}" * 64,
                    size_bytes=100,
                    generation_time_seconds=0.1,
                    registration_receipt_sha256=f"{index + 6}" * 64,
                )
            )
        return ThreeArtifactBundleReceipt(
            task_id=task.task_id,
            pair_id=pair_id,
            parent_run_id=baseline.run_id,
            input_evidence_hash=evidence_hash,
            separation_policy=ArtifactSeparationPolicy(),
            artifacts=artifacts,
        )


def _authorize_recovered_g2(*, ledger_root, task, pair_id, bundle):
    authority = {
        "task_id": task.task_id,
        "s0_hash": "s0",
        "artifact_ids_by_type": bundle.artifact_id_by_type(),
        "artifact_count": 3,
    }
    PhaseLedger(ledger_root, pair_id=pair_id).claim("evolved_candidate", authority)
    claim_path = ledger_root / pair_id / "evolved_candidate.json"
    artifact_ids = bundle.artifact_ids()
    replacement_run_id = f"{pair_id}-evolved-2222222222"
    receipt = {
        "schema_version": "chemcrow_evolved_candidate_no_effect_recovery_v1",
        "status": "VERIFIED_NO_EVOLVED_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "task_authority_sha256": canonical_sha256(task.model_dump(mode="json")),
        "phase": "evolved_candidate",
        "attempt_ordinal": 1,
        "authority_sha256": canonical_sha256(authority),
        "original_claim_sha256": file_sha256(claim_path),
        "config_sha256": "2" * 64,
        "checkpoint_sha256": "3" * 64,
        "active_attempt_run_id": f"{pair_id}-evolved-1111111111",
        "replacement_run_id": replacement_run_id,
        "core_task_id": f"{pair_id}-evolved-1111111111",
        "core_session_id_sha256": "4" * 64,
        "core_completion_sha256": "5" * 64,
        "core_completion_status": "ERROR",
        "error_category": "credential_isolation_validation_failed",
        "input_evidence_sha256": bundle.input_evidence_hash,
        "artifact_ids_by_type": bundle.artifact_id_by_type(),
        "reflector_job_ids": [item.reflector_job_id for item in bundle.artifacts],
        "reflector_run_ids": [item.reflector_run_id for item in bundle.artifacts],
        "reflector_prompt_hashes": [item.prompt_hash for item in bundle.artifacts],
        "injection_authority": {
            "evolved_claim_authority_sha256": canonical_sha256(authority),
            "context_id": "context",
            "context_artifact_ids": artifact_ids,
            "context_injected": True,
            "revision_id": "chemcrow-task-local-three:" + canonical_sha256(artifact_ids),
            "runtime_injection_receipt_published": False,
            "credential_readiness_receipt_published": False,
        },
        "no_effect_predicates": {
            "core_status_error": True,
            "exact_setup_canary_validation_error": True,
            "trajectory_has_zero_records": True,
            "trajectory_has_zero_traces": True,
            "workspace_result_absent": True,
            "core_route_bound": True,
            "host_codex_exec_forbidden": True,
            "context_injected_before_setup": True,
            "exact_three_context_artifact_ids": True,
            "context_identity_present": True,
            "runtime_injection_receipt_not_published": True,
            "credential_contract_present": True,
            "credential_readiness_receipt_not_published": True,
            "revision_authority_exact": True,
            "session_identity_present": True,
        },
        "infrastructure_canary_model_call_may_have_occurred": True,
        "evolved_candidate_model_call_proven_absent": True,
        "prior_scientific_calls_preserved": [
            "baseline_candidate",
            "baseline_internal_evaluator",
            "reflector_memory",
            "reflector_skill_bundle",
            "reflector_agent_system",
        ],
        "prior_scientific_calls_redispatched": False,
        "replacement_scope": ["evolved_candidate"],
        "duplicate_scientific_call": False,
        "recorded_before_replacement_dispatch": True,
    }
    VerifiedReplacementPhaseLedger(
        ledger_root,
        pair_id=pair_id,
    ).reconcile_verified_no_effect_failure("evolved_candidate", receipt)
    return replacement_run_id


class FakeInternalEvaluator:
    evaluator_id = "internal-gpt-5.5-config"

    def __init__(self) -> None:
        self.roles: list[str] = []

    def evaluate(self, *, task, trajectory):
        self.roles.append(trajectory.role)
        return EvaluatorFeedback(
            evaluator_role="evolution_evaluator",
            evaluator_run_id=f"internal-{trajectory.run_id}",
            scores=RubricScores(
                chemical_correctness=2,
                reasoning_quality=2,
                task_completion=2,
            ),
            actionable_critique=["verify observable evidence"],
        )


class FakeFinalEvaluator:
    evaluator_id = "final-gpt-5.5-independent-config"

    def compare(self, *, task, answer_a, answer_b):
        return BlindJudgeResult(
            scores_a=RubricScores(
                chemical_correctness=2,
                reasoning_quality=2,
                task_completion=2,
            ),
            scores_b=RubricScores(
                chemical_correctness=3,
                reasoning_quality=3,
                task_completion=3,
            ),
            winner="B",
            confidence=0.7,
        )


class FakeCoreReflector:
    def __init__(self, kind: ArtifactKind, content: str) -> None:
        self.kind = kind
        self.content = content
        self.config_sha256 = f"config-{kind.value}"
        self.candidate = {"agent": {"model_name": "gpt-5.5"}}
        self.prompts: list[str] = []

    def run_candidate(self, *, task, role, artifact_ids, pair_id, mcp_url):
        self.prompts.append(task.prompt)
        return Trajectory(
            run_id=f"{pair_id}-{self.kind.value}",
            task_id=task.task_id,
            role="baseline",
            status="COMPLETED",
            answer=self.content,
            candidate_config_sha256=self.config_sha256,
        )


def _another_task(task_item: TaskItem) -> TaskItem:
    payload = task_item.model_dump(mode="json")
    payload["task_id"] = "chemcrow-test-02"
    payload["prompt"] = "A second independent chemistry task."
    payload["sanitized_item_sha256"] = canonical_sha256(
        {"task_id": payload["task_id"], "prompt": payload["prompt"]}
    )
    return TaskItem.model_validate(payload)


def test_planned_task14_prefix_does_not_start_task15(task_item):
    second = _another_task(task_item)
    selected = _bounded_task_prefix([task_item, second], stop_after_task_id=task_item.task_id)
    assert [item.task_id for item in selected] == [task_item.task_id]
    with pytest.raises(ValueError, match="exactly one configured task"):
        _bounded_task_prefix([task_item, second], stop_after_task_id="missing")


def test_authoritative_roles_reject_silent_model_substitution():
    roles = {
        role: {"agent": {"model_name": "gpt-5.5"}}
        for role in (
            "candidate",
            "reflector_memory",
            "reflector_skill_bundle",
            "reflector_agent_system",
            "evolution_evaluator",
            "final_evaluator",
        )
    }
    _assert_authoritative_role_models(roles)
    roles["reflector_skill_bundle"]["agent"]["model_name"] = "gpt-5.4"
    with pytest.raises(ValueError, match="reflector_skill_bundle"):
        _assert_authoritative_role_models(roles)


def test_full_v4_config_is_fresh_and_three_pipeline(monkeypatch):
    monkeypatch.setenv("OPENEVO_ROLLOUT_BASE_URL", "http://127.0.0.1:8080")
    for name in (
        "OPENEVO_CANDIDATE_MODEL",
        "OPENEVO_REFLECTOR_MODEL",
        "OPENEVO_EVOLUTION_EVALUATOR_MODEL",
        "OPENEVO_FINAL_EVALUATOR_MODEL",
    ):
        monkeypatch.setenv(name, "gpt-5.5")
    path = Path(__file__).resolve().parents[1] / "configs" / "full.v4-three-pipeline.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert config["experiment_id"] == "chemcrow-task-local-full-v4-three-pipeline"
    assert config["task_ids"] == "all"
    assert config["artifact_protocol"] == "three_isolated_v1"
    assert set(config["reflectors"]) == {"memory", "skill_bundle", "agent_system"}
    assert "prior_run_root" not in config
    assert "duplicate_authorization_receipt" not in config
    assert config["run_root"].endswith("runs/full-v4-three-pipeline")


def test_core_native_v2_config_has_fresh_identity_and_no_custom_responsibility_policy():
    path = (
        Path(__file__).resolve().parents[1] / "configs" / "full.v5-core-native-three-pipeline.yaml"
    )
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert config["experiment_id"] == "chemcrow-task-local-full-v5-core-native-three-pipeline"
    assert config["artifact_protocol"] == "three_isolated_core_native_v2"
    assert config["run_root"].endswith("runs/full-v5-core-native-three-pipeline")
    assert config["cache_root"].endswith("caches/full-v5-core-native-three-pipeline")
    assert "responsibility_policy_version" not in config["artifact_separation"]
    assert "minimum_tokens_for_responsibility_check" not in config["artifact_separation"]


def test_legacy_v1_and_core_native_v2_have_distinct_protocol_identity():
    legacy = ThreeArtifactBundleReceipt.model_fields["protocol"].annotation
    assert "chemcrow_three_isolated_artifacts_v1" in str(legacy)
    assert "chemcrow_three_isolated_core_native_artifacts_v2" in str(legacy)


def test_three_pipeline_protocol_reset_lineage_and_scoring_order(tmp_path, task_item):
    candidate = FakeThreeCandidate()
    evolution = FakeThreeEvolution()
    internal = FakeInternalEvaluator()
    runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=candidate,
        evolution=evolution,
        evolution_evaluator=internal,
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=tmp_path / "claims",
    )
    first = runner.run_item(task_item, pair_id="pair-1")
    second = runner.run_item(_another_task(task_item), pair_id="pair-2")

    assert internal.roles == ["baseline", "evolved", "baseline", "evolved"]
    assert [call[1] for call in candidate.calls] == [
        "baseline",
        "evolved",
        "baseline",
        "evolved",
    ]
    assert candidate.calls[0][2] == candidate.calls[2][2] == {}
    assert set(candidate.calls[1][2]) == set(candidate.calls[3][2]) == set(THREE_ARTIFACT_ORDER)
    assert not set(candidate.calls[1][2].values()) & set(candidate.calls[3][2].values())
    for result in (first, second):
        assert len(result.artifact_bundle.artifacts) == 3
        assert len({item.reflector_job_id for item in result.artifact_bundle.artifacts}) == 3
        assert len({item.prompt_hash for item in result.artifact_bundle.artifacts}) == 3
        reset = json.loads((tmp_path / "runs" / result.pair_id / "reset.receipt.json").read_text())
        assert reset["artifact_inventory_after"] == []
        assert reset["runtime_context_after"] == "bare_s0"
        assert reset["memory_after"] == reset["skill_bundle_after"] == []
        assert reset["agent_system_after"] == []
    assert all("paper" not in json.dumps(payload).lower() for payload in evolution.inputs)

    wrong_task = json.loads(first.model_dump_json())
    wrong_task["baseline"]["task_id"] = "chemcrow-other"
    with pytest.raises(ValueError, match="trajectory task lineage"):
        ThreeArtifactPairResult.model_validate(wrong_task)

    wrong_pair = json.loads(first.model_dump_json())
    wrong_pair["artifact_bundle"]["pair_id"] = "other-pair"
    for artifact in wrong_pair["artifact_bundle"]["artifacts"]:
        artifact["pair_id"] = "other-pair"
    with pytest.raises(ValueError, match="bundle pair lineage"):
        ThreeArtifactPairResult.model_validate(wrong_pair)

    reused_internal_call = json.loads(first.model_dump_json())
    reused_internal_call["evolved_internal_evaluation"]["evaluator_run_id"] = reused_internal_call[
        "baseline_internal_evaluation"
    ]["evaluator_run_id"]
    with pytest.raises(ValueError, match="independent calls"):
        ThreeArtifactPairResult.model_validate(reused_internal_call)


def test_generation_only_scope_seals_g1_and_artifacts_without_g2(tmp_path, task_item):
    candidate = FakeThreeCandidate()
    internal = FakeInternalEvaluator()
    runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=candidate,
        evolution=FakeThreeEvolution(),
        evolution_evaluator=internal,
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=tmp_path / "claims",
        stop_after_artifact_generation=True,
    )

    result = runner.run_item(task_item, pair_id="generation-only")

    assert isinstance(result, ThreeArtifactGenerationResult)
    assert [call[1] for call in candidate.calls] == ["baseline"]
    assert internal.roles == ["baseline"]
    assert result.event_order[-2:] == ["sealed", "reset"]
    assert all(
        item.consumed_by_evolved_run_id is None for item in result.artifact_bundle.artifacts
    )
    root = tmp_path / "runs" / "generation-only"
    boundary = json.loads((root / "generation.boundary.receipt.json").read_text())
    assert boundary["g2_dispatched"] is False
    assert boundary["final_evaluator_dispatched"] is False
    assert (root / "artifact.study.result.json").is_file()


def test_sealed_generation_continuation_runs_only_g2_and_evaluators(
    tmp_path, task_item
):
    source_evolution = FakeThreeEvolution()
    source_internal = FakeInternalEvaluator()
    source_runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "source-runs",
        candidate=FakeThreeCandidate(),
        evolution=source_evolution,
        evolution_evaluator=source_internal,
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=tmp_path / "source-claims",
        stop_after_artifact_generation=True,
    )
    source_result = source_runner.run_item(task_item, pair_id="sealed-generation")
    assert isinstance(source_result, ThreeArtifactGenerationResult)

    candidate = FakeThreeCandidate()
    continuation_evolution = FakeThreeEvolution()
    continuation_internal = FakeInternalEvaluator()
    continuation_runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "continuation-runs",
        candidate=candidate,
        evolution=continuation_evolution,
        evolution_evaluator=continuation_internal,
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=tmp_path / "continuation-claims",
    )

    result = continuation_runner.run_item(
        task_item,
        pair_id="sealed-generation",
        recovered_baseline_checkpoint=(
            source_result.baseline,
            source_result.baseline_internal_evaluation,
        ),
        recovered_artifact_checkpoint=source_result.artifact_bundle,
        continue_from_sealed_generation=True,
    )

    assert isinstance(result, ThreeArtifactPairResult)
    assert continuation_evolution.inputs == []
    assert [call[1] for call in candidate.calls] == ["evolved"]
    assert continuation_internal.roles == ["evolved"]
    claim_names = {
        path.name
        for path in (tmp_path / "continuation-claims" / "sealed-generation").glob(
            "*.json"
        )
    }
    assert claim_names == {
        "evolved_candidate.json",
        "evolved_internal_evaluator.json",
        "final_evaluator.json",
    }


def test_three_pipeline_resumes_from_completed_baseline_checkpoint(tmp_path, task_item):
    candidate = FakeThreeCandidate()
    evolution = FakeThreeEvolution()
    runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=candidate,
        evolution=evolution,
        evolution_evaluator=FakeInternalEvaluator(),
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=None,
    )
    recovered_baseline = Trajectory(
        run_id="recovered-baseline",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="preserved baseline answer",
        candidate_config_sha256="s0",
    )
    recovered_evaluation = EvaluatorFeedback(
        evaluator_role="evolution_evaluator",
        evaluator_run_id="recovered-evaluator",
        scores=RubricScores(
            chemical_correctness=3,
            reasoning_quality=3,
            task_completion=3,
        ),
        confidence=1.0,
    )

    result = runner.run_item(
        task_item,
        pair_id="pair-recovered",
        recovered_baseline_checkpoint=(recovered_baseline, recovered_evaluation),
    )

    assert candidate.calls == [
        (
            task_item.task_id,
            "evolved",
            {
                ArtifactKind.TEXT_MEMORY: "art-pair-recovered-text_memory",
                ArtifactKind.SKILL_BUNDLE: "art-pair-recovered-skill_bundle",
                ArtifactKind.AGENT_SYSTEM: "art-pair-recovered-agent_system",
            },
        )
    ]
    assert result.baseline.run_id == "recovered-baseline"
    assert result.baseline_internal_evaluation.evaluator_run_id == "recovered-evaluator"


def test_generation_only_replaces_only_failed_baseline_evaluator(tmp_path, task_item):
    candidate = FakeThreeCandidate()
    internal = FakeInternalEvaluator()
    ledger_root = tmp_path / "claims"
    pair_id = "pair-evaluator-replacement"
    baseline = Trajectory(
        run_id="preserved-baseline",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="preserved baseline answer",
        candidate_config_sha256="s0",
    )
    ledger = VerifiedReplacementPhaseLedger(ledger_root, pair_id=pair_id)
    ledger.claim(
        "baseline_candidate",
        {
            "task_id": task_item.task_id,
            "s0_hash": "s0",
            "artifact_ids": [],
            "artifact_inventory": {},
        },
    )
    ledger.terminal("baseline_candidate", {"run_id": baseline.run_id})
    evaluator_authority = {
        "task_id": task_item.task_id,
        "run_id": baseline.run_id,
        "evaluator_id": internal.evaluator_id,
        "paper_evaluator": False,
    }
    ledger.claim("baseline_internal_evaluator", evaluator_authority)
    claim_path = ledger_root / pair_id / "baseline_internal_evaluator.json"
    ledger.reconcile_verified_no_effect_failure(
        "baseline_internal_evaluator",
        {
            "schema_version": "chemcrow_baseline_evaluator_no_effect_recovery_v1",
            "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
            "pair_id": pair_id,
            "phase": "baseline_internal_evaluator",
            "attempt_ordinal": 1,
            "authority_sha256": canonical_sha256(evaluator_authority),
            "original_claim_sha256": file_sha256(claim_path),
            "no_effect_predicates": {"zero_trajectory": True},
            "evaluator_model_call_proven_absent": True,
            "duplicate_scientific_call": False,
            "recorded_before_replacement_dispatch": True,
        },
    )
    runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=candidate,
        evolution=FakeThreeEvolution(),
        evolution_evaluator=internal,
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=ledger_root,
        stop_after_artifact_generation=True,
    )

    result = runner.run_item(
        task_item,
        pair_id=pair_id,
        recovered_baseline_before_evaluator=baseline,
        allow_verified_baseline_evaluator_replacement=True,
    )

    assert isinstance(result, ThreeArtifactGenerationResult)
    assert candidate.calls == []
    assert internal.roles == ["baseline"]
    claim = json.loads(claim_path.read_text())
    assert claim["status"] == "terminal"
    assert claim["active_attempt_ordinal"] == 2


def test_three_pipeline_reuses_completed_reflectors_for_replacement_g2_only(tmp_path, task_item):
    candidate = FakeThreeCandidate()
    evolution = FakeThreeEvolution()
    ledger_root = tmp_path / "claims"
    runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=candidate,
        evolution=evolution,
        evolution_evaluator=FakeInternalEvaluator(),
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=ledger_root,
    )
    recovered_baseline = Trajectory(
        run_id="recovered-baseline",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="preserved baseline answer",
        candidate_config_sha256="s0",
    )
    recovered_evaluation = EvaluatorFeedback(
        evaluator_role="evolution_evaluator",
        evaluator_run_id="recovered-evaluator",
        scores=RubricScores(
            chemical_correctness=3,
            reasoning_quality=3,
            task_completion=3,
        ),
        actionable_critique=["Preserve the exact completed Reflectors."],
        confidence=1.0,
    )
    feedback = reflector_feedback_payload(
        mode=FeedbackMode.F2,
        trajectory=recovered_baseline,
        runtime=runtime_feedback(recovered_baseline),
        evaluator=recovered_evaluation,
    )
    recovered_bundle = evolution.evolve_all(
        task=task_item,
        baseline=recovered_baseline,
        feedback_payload=feedback,
        pair_id="pair-recovered-g2",
    )
    evolution.inputs.clear()
    replacement_run_id = _authorize_recovered_g2(
        ledger_root=ledger_root,
        task=task_item,
        pair_id="pair-recovered-g2",
        bundle=recovered_bundle,
    )

    result = runner.run_item(
        task_item,
        pair_id="pair-recovered-g2",
        recovered_baseline_checkpoint=(
            recovered_baseline,
            recovered_evaluation,
        ),
        recovered_artifact_checkpoint=recovered_bundle,
        allow_verified_evolved_candidate_replacement=True,
        replacement_evolved_run_id=replacement_run_id,
    )

    assert evolution.inputs == []
    assert [call[1] for call in candidate.calls] == ["evolved"]
    assert result.artifact_bundle.artifact_ids() == recovered_bundle.artifact_ids()
    assert result.event_order == [
        "s0_asserted",
        "baseline",
        "runtime_feedback",
        "baseline_internal_evaluator",
        "reflector_memory",
        "reflector_skill_bundle",
        "reflector_agent_system",
        "artifacts_registered",
        "evolved",
        "evolved_internal_evaluator",
        "final_evaluator",
        "sealed",
        "reset",
    ]


def test_three_pipeline_refuses_recovered_artifacts_without_explicit_g2_replacement(
    tmp_path, task_item
):
    runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=FakeThreeCandidate(),
        evolution=FakeThreeEvolution(),
        evolution_evaluator=FakeInternalEvaluator(),
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=None,
    )
    baseline = Trajectory(
        run_id="baseline",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="answer",
        candidate_config_sha256="s0",
    )
    evaluation = EvaluatorFeedback(
        evaluator_role="evolution_evaluator",
        evaluator_run_id="evaluation",
        scores=RubricScores(
            chemical_correctness=3,
            reasoning_quality=3,
            task_completion=3,
        ),
    )
    feedback = reflector_feedback_payload(
        mode=FeedbackMode.F2,
        trajectory=baseline,
        runtime=runtime_feedback(baseline),
        evaluator=evaluation,
    )
    bundle = runner.evolution.evolve_all(
        task=task_item,
        baseline=baseline,
        feedback_payload=feedback,
        pair_id="pair",
    )

    with pytest.raises(ValueError, match="requires one recovered artifact checkpoint"):
        runner.run_item(
            task_item,
            pair_id="pair",
            recovered_baseline_checkpoint=(baseline, evaluation),
            recovered_artifact_checkpoint=bundle,
        )
    with pytest.raises(ValueError, match="verified replacement ledger"):
        runner.run_item(
            task_item,
            pair_id="pair",
            recovered_baseline_checkpoint=(baseline, evaluation),
            recovered_artifact_checkpoint=bundle,
            allow_verified_evolved_candidate_replacement=True,
            replacement_evolved_run_id="pair-evolved-2222222222",
        )


def test_native_three_reflectors_are_independent_and_sibling_blind(tmp_path, task_item):
    contents = {
        ArtifactKind.TEXT_MEMORY: (
            "Observed fact: the baseline omitted a returned molecular-weight observation. "
            "Remember the verified observation and its units for this retry."
        ),
        ArtifactKind.SKILL_BUNDLE: (
            "# Verification workflow\n1. Parse the question.\n2. Select the declared tool.\n"
            "3. Compare the observation with the drafted claim.\n4. Report uncertainty."
        ),
        ArtifactKind.AGENT_SYSTEM: (
            "# Same-task behavior\n- Ground claims in visible tool observations.\n"
            "- Suppress unsupported chemical details.\n- Verify units before composing the answer."
        ),
    }
    ports = {kind: FakeCoreReflector(kind, content) for kind, content in contents.items()}
    engine = ThreeIsolatedEvolutionEngine(
        run_root=tmp_path / "runs",
        reflector_rollouts=ports,
        evolution_db_path=tmp_path / "evolution.sqlite3",
        evolution_artifact_root=tmp_path / "artifacts",
        separation_policy=ArtifactSeparationPolicy(),
    )
    baseline = Trajectory(
        run_id="baseline-run",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="A short baseline answer.",
        candidate_config_sha256="s0",
    )
    bundle = engine.evolve_all(
        task=task_item,
        baseline=baseline,
        feedback_payload={
            "mode": "F0",
            "observable_trajectory": baseline.model_dump(mode="json"),
        },
        pair_id="pair-native-three",
    )
    assert len(bundle.artifacts) == 3
    assert len({item.reflector_job_id for item in bundle.artifacts}) == 3
    assert len({item.reflector_run_id for item in bundle.artifacts}) == 3
    assert len({item.prompt_hash for item in bundle.artifacts}) == 3
    assert {item.model for item in bundle.artifacts} == {"gpt-5.5"}
    assert {item.input_evidence_hash for item in bundle.artifacts} == {bundle.input_evidence_hash}
    assert bundle.protocol == CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL
    for kind, port in ports.items():
        assert len(port.prompts) == 1
        sibling_contents = [text for sibling, text in contents.items() if sibling != kind]
        assert all(content not in port.prompts[0] for content in sibling_contents)
        assert "paper EvaluatorGPT grades" in port.prompts[0]
        assert "SYSTEM CONTRACT" not in port.prompts[0]
        assert "Explicitly label" not in port.prompts[0]
        assert "direct physical execution" not in port.prompts[0]
        assert "safety_metadata" not in port.prompts[0]
        assert "allowed_tool_metadata" not in port.prompts[0]
    assert "Return only the Markdown memory.md file" in ports[ArtifactKind.TEXT_MEMORY].prompts[0]
    assert "Return only SKILL.md content" in ports[ArtifactKind.SKILL_BUNDLE].prompts[0]
    assert (
        "Return only the Markdown agent-system instruction file"
        in ports[ArtifactKind.AGENT_SYSTEM].prompts[0]
    )
    manifests = list((tmp_path / "artifacts" / "artifacts").rglob("*.json"))
    assert len(manifests) >= 3


def test_full_core_worker_prompts_are_frozen_from_claimed_datasets(tmp_path, task_item):
    contents = {
        ArtifactKind.TEXT_MEMORY: (
            "# Memory\n\nRemember the observed omission and verify units before finalizing."
        ),
        ArtifactKind.SKILL_BUNDLE: (
            "# Verification skill\n\nWhen answering, inspect the evidence and verify completion."
        ),
        ArtifactKind.AGENT_SYSTEM: (
            "# Operating rules\n\n- Before finalizing, verify every reported unit against evidence."
        ),
    }
    ports = {kind: FakeCoreReflector(kind, content) for kind, content in contents.items()}
    engine = ThreeIsolatedEvolutionEngine(
        run_root=tmp_path / "runs",
        reflector_rollouts=ports,
        evolution_db_path=tmp_path / "evolution.sqlite3",
        evolution_artifact_root=tmp_path / "artifacts",
        separation_policy=ArtifactSeparationPolicy(),
        prompt_profile=CORE_FULL_WORKER_PROMPT_PROFILE,
    )
    baseline = Trajectory(
        run_id="baseline-full-worker",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="The baseline omitted a required evidence check.",
        candidate_config_sha256="s0",
    )

    bundle = engine.evolve_all(
        task=task_item,
        baseline=baseline,
        feedback_payload={
            "mode": "F2",
            "evaluator_feedback": {
                "evaluator_run_id": "eval-full-worker",
                "scores": {
                    "chemical_correctness": 3.0,
                    "reasoning_quality": 2.0,
                    "task_completion": 1.0,
                },
                "strengths": ["The route identity was supported."],
                "weaknesses": ["The answer omitted a required evidence check."],
                "actionable_critique": ["Verify the reported units."],
                "confidence": 0.8,
            },
        },
        pair_id="pair-full-worker",
    )

    assert bundle.prompt_profile == CORE_FULL_WORKER_PROMPT_PROFILE
    memory_prompt = ports[ArtifactKind.TEXT_MEMORY].prompts[0]
    skill_prompt = ports[ArtifactKind.SKILL_BUNDLE].prompts[0]
    system_prompt = ports[ArtifactKind.AGENT_SYSTEM].prompts[0]
    assert "# Text Memory Reflection Context" in memory_prompt
    assert "# Skill Bundle Reflection Context" in skill_prompt
    assert "## Reflections From Prior Trajectories" in system_prompt
    assert all(
        "Shared Evolution Feedback" in prompt
        for prompt in (memory_prompt, skill_prompt, system_prompt)
    )
    assert all(
        "Observed Issues: The answer omitted a required evidence check." in prompt
        and "Suggested Changes: Verify the reported units." in prompt
        and "Strengths: The route identity was supported." in prompt
        and "reward=0.5" in prompt
        for prompt in (memory_prompt, skill_prompt, system_prompt)
    )
    assert all(
        "You are one of three mutually isolated" in prompt
        for prompt in (memory_prompt, skill_prompt, system_prompt)
    )
    assert all(
        "EVIDENCE JSON" not in prompt
        for prompt in (memory_prompt, skill_prompt, system_prompt)
    )
    freeze = json.loads(
        (
            tmp_path
            / "runs"
            / "pair-full-worker"
            / "reflector_prompts"
            / "freeze.receipt.json"
        ).read_text()
    )
    assert freeze["prompts_frozen_before_first_invocation"] is True
    assert len(set(freeze["prompt_sha256_by_type"].values())) == 3
    assert all(item.reflector_attempt_run_ids for item in bundle.artifacts)
    assert all(item.output_audit["finding_count"] == 0 for item in bundle.artifacts)


def test_core_native_feedback_projection_is_exact_and_score_normalized():
    feedback, reward = project_core_native_evolution_feedback(
        {
            "mode": "F2",
            "evaluator_feedback": {
                "evaluator_run_id": "eval-1",
                "scores": {
                    "chemical_correctness": 4.0,
                    "reasoning_quality": 3.0,
                    "task_completion": 2.0,
                },
                "strengths": ["strength exact"],
                "weaknesses": ["weakness exact"],
                "actionable_critique": ["critique exact"],
            },
        }
    )

    assert feedback == {
        "status": "available_for_evolution",
        "feedback_id": "eval-1",
        "decision": "evaluator-summary",
        "strengths": ["strength exact"],
        "observed_issues": ["weakness exact"],
        "suggested_changes": ["critique exact"],
    }
    assert reward == pytest.approx(0.75)


def test_full_core_worker_replacement_reuses_first_frozen_prompts(tmp_path, task_item):
    contents = {
        ArtifactKind.TEXT_MEMORY: "# Memory\n\nVerify the evidence before finalizing.",
        ArtifactKind.SKILL_BUNDLE: "# Skill\n\nInspect, answer, then validate.",
        ArtifactKind.AGENT_SYSTEM: "# Rules\n\n- Validate task completion before finalizing.",
    }
    baseline = Trajectory(
        run_id="baseline-frozen-replacement",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="The baseline omitted a required check.",
        candidate_config_sha256="s0",
    )
    feedback = {
        "mode": "F2",
        "evaluator_feedback": {
            "evaluator_run_id": "eval-frozen-replacement",
            "scores": {
                "chemical_correctness": 3.0,
                "reasoning_quality": 3.0,
                "task_completion": 2.0,
            },
            "strengths": ["The identity was supported."],
            "weaknesses": ["The check was absent."],
            "actionable_critique": ["Add the missing check."],
        },
    }

    first_ports = {kind: FakeCoreReflector(kind, content) for kind, content in contents.items()}
    first = ThreeIsolatedEvolutionEngine(
        run_root=tmp_path / "runs",
        reflector_rollouts=first_ports,
        evolution_db_path=tmp_path / "first.sqlite3",
        evolution_artifact_root=tmp_path / "first-artifacts",
        separation_policy=ArtifactSeparationPolicy(),
        prompt_profile=CORE_FULL_WORKER_PROMPT_PROFILE,
    ).evolve_all(
        task=task_item,
        baseline=baseline,
        feedback_payload=feedback,
        pair_id="pair-frozen-replacement",
    )
    second_ports = {kind: FakeCoreReflector(kind, content) for kind, content in contents.items()}
    second = ThreeIsolatedEvolutionEngine(
        run_root=tmp_path / "runs",
        reflector_rollouts=second_ports,
        evolution_db_path=tmp_path / "second.sqlite3",
        evolution_artifact_root=tmp_path / "second-artifacts",
        separation_policy=ArtifactSeparationPolicy(),
        prompt_profile=CORE_FULL_WORKER_PROMPT_PROFILE,
    ).evolve_all(
        task=task_item,
        baseline=baseline,
        feedback_payload=feedback,
        pair_id="pair-frozen-replacement",
    )

    assert [item.prompt_hash for item in second.artifacts] == [
        item.prompt_hash for item in first.artifacts
    ]
    for kind in THREE_ARTIFACT_ORDER:
        assert second_ports[kind].prompts == first_ports[kind].prompts


@pytest.mark.parametrize(
    "artifacts, expected_key",
    [
        (
            {
                ArtifactKind.TEXT_MEMORY: "same bytes",
                ArtifactKind.SKILL_BUNDLE: "same bytes",
                ArtifactKind.AGENT_SYSTEM: "different policy",
            },
            "byte_identical_pairs",
        ),
        (
            {
                ArtifactKind.TEXT_MEMORY: "Same, normalized TEXT!",
                ArtifactKind.SKILL_BUNDLE: "same normalized text",
                ArtifactKind.AGENT_SYSTEM: "different policy",
            },
            "normalized_identical_pairs",
        ),
        (
            {
                ArtifactKind.TEXT_MEMORY: "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour twentyfive twentysix twentyseven twentyeight twentynine thirty",
                ArtifactKind.SKILL_BUNDLE: "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour twentyfive twentysix twentyseven twentyeight twentynine changed",
                ArtifactKind.AGENT_SYSTEM: "ground every claim and verify all chemical units before answering",
            },
            "near_duplicate_pairs",
        ),
    ],
)
def test_duplicate_guards_fail_closed(artifacts, expected_key):
    findings = detect_artifact_duplicates(
        artifacts,
        baseline_answer="unrelated baseline answer",
        policy=ArtifactSeparationPolicy(),
    )
    assert findings[expected_key]


def test_duplicate_guard_does_not_impose_chemcrow_responsibility_keywords():
    findings = detect_artifact_duplicates(
        {
            ArtifactKind.TEXT_MEMORY: (
                "Use a procedure and checklist to select tools and validate each step carefully."
            ),
            ArtifactKind.SKILL_BUNDLE: (
                "Remember the observed baseline fact and retain the verified correction as a lesson."
            ),
            ArtifactKind.AGENT_SYSTEM: (
                "Be useful, concise, accurate, careful, clear, complete, relevant, and consistent."
            ),
        },
        baseline_answer="unrelated baseline answer",
        policy=ArtifactSeparationPolicy(),
    )
    assert not any(findings.values())


def test_forbidden_paper_feedback_hidden_in_generic_text_is_detected():
    assert _find_forbidden_text(
        {"notes": ["The paper Evaluator result and paper grade should guide the retry."]}
    ) == {"paper evaluator", "paper grade"}


def test_reflector_accepts_core_native_raw_markdown_only():
    assert _strict_reflector_markdown("# Memory\nremember this\n") == "# Memory\nremember this"
    with pytest.raises(ValueError, match="Markdown fence"):
        _strict_reflector_markdown("```markdown\n# Memory\n```")
    with pytest.raises(ValueError, match="empty"):
        _strict_reflector_markdown("   ")


def test_core_native_bundle_label_and_legacy_policy_fields_are_not_emitted():
    assert CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL.endswith("core-native-artifacts-v2")
    assert "responsibility_policy_version" not in ArtifactSeparationPolicy().model_dump(
        mode="json"
    )
    historical = ArtifactSeparationPolicy.model_validate(
        {
            "responsibility_policy_version": "chemcrow_artifact_responsibility_v1",
            "minimum_tokens_for_responsibility_check": 8,
        }
    ).model_dump(mode="json")
    assert historical["responsibility_policy_version"] == ("chemcrow_artifact_responsibility_v1")
    assert historical["minimum_tokens_for_responsibility_check"] == 8


def test_core_injection_receipt_exact_type_mapping_and_no_fourth_artifact():
    mapping = {
        ArtifactKind.TEXT_MEMORY: "art-memory",
        ArtifactKind.SKILL_BUNDLE: "art-skill",
        ArtifactKind.AGENT_SYSTEM: "art-agent",
    }
    raw = {
        "schema_version": "3",
        "artifacts": [
            {
                "artifact_id": mapping[kind],
                "artifact_type": kind.value,
                "runtime_paths": [f"evolution/{kind.value}"],
            }
            for kind in THREE_ARTIFACT_ORDER
        ],
    }
    payload = {
        "results": [
            {
                "trajectory": {
                    "metadata": {
                        "task_metadata": {
                            "evolution": {
                                "context_injected": True,
                                "context_artifact_ids": [
                                    mapping[kind] for kind in THREE_ARTIFACT_ORDER
                                ],
                                "runtime_injection_receipt": raw,
                            }
                        }
                    }
                }
            }
        ]
    }
    summary = _three_artifact_injection_receipt(payload, expected=mapping, role="evolved")
    assert summary is not None and summary.artifact_count == 3
    raw["artifacts"].append(
        {
            "artifact_id": "art-fourth",
            "artifact_type": "text_memory",
            "runtime_paths": ["evolution/fourth"],
        }
    )
    with pytest.raises(Exception, match="exactly three"):
        _three_artifact_injection_receipt(payload, expected=mapping, role="evolved")


def test_candidate_pair_parity_changes_only_artifact_authority(task_item, core_candidate_config):
    receipt = candidate_pair_request_parity(
        task=task_item,
        candidate=core_candidate_config,
        mcp_url="http://127.0.0.1:9/mcp",
    )
    assert receipt["all_invariant_fields_equal"] is True
    assert receipt["only_allowed_metadata_drift"] is True
    assert receipt["baseline_context_artifact_ids"] == []
    assert receipt["evolved_artifact_types"] == [
        "text_memory",
        "skill_bundle",
        "agent_system",
    ]


def test_three_artifact_source_has_no_host_codex_or_shell_bypass():
    sources = []
    module_root = Path(__file__).resolve().parents[1] / "src" / "openevo_chemcrow"
    for name in (
        "three_artifact_evolution.py",
        "three_artifact_runtime.py",
        "three_artifact_protocol.py",
    ):
        sources.append((module_root / name).read_text(encoding="utf-8"))
    source = "\n".join(sources)
    assert "codex exec" not in source
    assert "subprocess" not in source
    assert "mcp_servers" not in source
