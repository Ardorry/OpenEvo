from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import ClassVar

import pytest
from openevo_researchclawbench.native_planner import native_experiment_payload
from openevo_researchclawbench.reflector_runner import (
    NATIVE_METHODS,
    SERIAL_REQUEST_ORDER,
    NativeArtifactResult,
    consume_native_artifact_result,
    reflector_runtime_audit,
    require_native_admission_evidence,
    submit_native_triple_artifact_cycle,
)

from openevo.evolution.admission import ArtifactProposalDecisionReceipt
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.framework.profiles import execution_profile_for_release_mode
from openevo.evolution.planned_jobs import PlanBoundJobCreateRequest
from openevo.experiments.compiler import compile_experiment
from openevo.experiments.models import ExperimentConfig as NativeExperimentConfig

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _NativeEvolutionConfig:
    experiment_root = PROJECT_ROOT
    rollout_url = "http://127.0.0.1:18000"
    evolution_url = "http://127.0.0.1:18001"
    _VALUES: ClassVar[dict[str, str]] = {
        "experiment_id": "native-evolution-test",
        "candidate.reasoning_level": "high",
    }

    def require(self, key: str):
        return self._VALUES[key]


def _compiled():
    config = _NativeEvolutionConfig()
    payload = native_experiment_payload(config, task_id="Life_005", instruction="public task")
    native = NativeExperimentConfig.model_validate(payload)
    identity = ImplementationDistributionIdentity(
        distribution="openevo",
        distribution_version="0.0.test",
        distribution_digest="a" * 64,
    )
    registry = build_builtin_registry(identity)
    return compile_experiment(
        native,
        task_ids=["Life_005"],
        rounds_override=1,
        run_id="native-test",
        registry_snapshot=registry,
        execution_profile=execution_profile_for_release_mode("codex_subscription_transcript"),
    ), registry


def _decision() -> ArtifactProposalDecisionReceipt:
    content_admission = {
        "schema_version": "openevo.artifact_content_admission.v1",
        "basis_sha256": "b" * 64,
        "proposal_artifact_ids": ["artifact-1", "artifact-2"],
        "source_artifact_ids": [],
        "source_payload_sha256": None,
        "scanned_file_count": 2,
        "scanned_byte_count": 128,
        "finding_count": 0,
        "finding_categories": [],
        "passed": True,
    }
    content_admission["content_sha256"] = hashlib.sha256(
        json.dumps(content_admission, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    body = {
        "schema_version": "openevo.artifact_proposal_decision.v1",
        "decision_id": "decision-1",
        "job_id": "job-1",
        "artifact_type": "agent_system",
        "action": "update",
        "parent_artifact_id": "artifact-parent",
        "genesis": False,
        "proposal_artifact_ids": ["artifact-1", "artifact-2"],
        "selected_artifact_id": "artifact-1",
        "rejected_artifact_ids": ["artifact-2"],
        "active_artifact_id": "artifact-1",
        "reason": "validated",
        "admission": {
            "validator_id": "validator-v1",
            "schema_version": "1.0.0",
            "passed": True,
            "report_sha256": "a" * 64,
        },
        "content_admission": content_admission,
        "created_at": "2026-07-28T00:00:00+00:00",
    }
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ArtifactProposalDecisionReceipt.model_validate(body)


def test_all_three_native_targets_and_methods_exist() -> None:
    _, registry = _compiled()
    for artifact_type, method in NATIVE_METHODS.items():
        assert artifact_type in registry.targets
        assert method in registry.methods
        assert registry.methods[method].target_id == artifact_type


def test_skill_bundle_has_real_native_reflector_and_handler() -> None:
    _, registry = _compiled()
    descriptor = registry.methods["skill_bundle_reflector"]
    assert descriptor.output_artifact_types == ("skill_bundle",)
    assert registry.targets["skill_bundle"].handler_id == "skill_bundle_handler"


def test_native_plan_contains_exact_triple_artifact_methods() -> None:
    compiled, _ = _compiled()
    methods = compiled.evolution_methods_for_round(
        0, prior_dataset_artifact_ids=(), task_id="Life_005"
    )
    assert {item.artifact_type: item.method for item in methods} == NATIVE_METHODS


def test_parametric_memory_is_disabled() -> None:
    payload = native_experiment_payload(
        _NativeEvolutionConfig(), task_id="Life_005", instruction="public task"
    )
    assert "CODEX_HOME" not in payload["runtime"]["env"]
    assert payload["evolution"]["targets"]["parametric_memory"]["enabled"] is False


def test_each_artifact_is_submitted_as_independent_planned_job() -> None:
    compiled, _ = _compiled()

    class Client:
        def __init__(self):
            self.payloads = []

        def create_plan_bound_job(self, payload):
            PlanBoundJobCreateRequest.model_validate(payload)
            self.payloads.append(payload)
            return {"job_id": f"job-{len(self.payloads)}", "state": "pending"}

    client = Client()
    submissions = submit_native_triple_artifact_cycle(
        client,
        compiled_experiment=compiled,
        task_id="Life_005",
        round_index=0,
        dataset_artifact_id="dataset-current",
        context_artifact_ids={
            "dataset": [],
            "agent_system": [],
            "text_memory": [],
            "skill_bundle": [],
        },
    )
    assert tuple(item.artifact_type for item in submissions) == SERIAL_REQUEST_ORDER
    assert len({item.job_id for item in submissions}) == 3
    assert len(client.payloads) == 3


def test_native_worker_registry_result_is_consumed_not_simulated() -> None:
    submission = type(
        "Submission",
        (),
        {
            "job_id": "job-1",
            "artifact_type": "agent_system",
            "method_id": "agent_system_gepa_reflector",
        },
    )()

    class Client:
        def get_internal_job_result(self, job_id):
            return {
                "job_id": job_id,
                "state": "succeeded",
                "artifact_ids": ["report-1", "artifact-1", "artifact-2"],
            }

        def get_artifact(self, artifact_id):
            if artifact_id == "report-1":
                return {"artifact_id": artifact_id, "type": "report", "manifest": {}}
            if artifact_id == "artifact-2":
                return {
                    "artifact_id": artifact_id,
                    "type": "agent_system",
                    "promoted": False,
                    "manifest": {"action": "update", "reflection_audit": {"redaction_count": 0}},
                }
            return {
                "artifact_id": artifact_id,
                "type": "agent_system",
                "promoted": True,
                "manifest": {"action": "update", "reflection_audit": {"redaction_count": 0}},
            }

    result = consume_native_artifact_result(Client(), submission, decision=_decision())
    assert result.registry_persisted
    assert result.artifact_id == "artifact-1"
    assert result.proposal_artifact_ids == ("artifact-1", "artifact-2")
    require_native_admission_evidence(result)


def test_admission_rejects_native_redaction_dependency() -> None:
    result = NativeArtifactResult(
        artifact_type="text_memory",
        method_id="text_memory_expel_reflector",
        job_id="job-1",
        artifact_id="artifact-1",
        proposal_artifact_ids=("artifact-1",),
        registry_persisted=True,
        promoted=False,
        action="update",
        manifest={"reflection_audit": {"redaction_count": 1}},
        decision_id="decision-1",
        decision_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="redaction"):
        require_native_admission_evidence(result)


def test_updated_revision_requires_native_promotion() -> None:
    result = NativeArtifactResult(
        artifact_type="skill_bundle",
        method_id="skill_bundle_reflector",
        job_id="job-1",
        artifact_id="artifact-1",
        proposal_artifact_ids=("artifact-1",),
        registry_persisted=True,
        promoted=False,
        action="update",
        manifest={"reflection_audit": {"redaction_count": 0}},
        decision_id="decision-1",
        decision_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="admitted and promoted by OpenEvo"):
        require_native_admission_evidence(result)


def test_reflector_runtime_is_statically_pinned_and_needs_runtime_receipt() -> None:
    receipt = reflector_runtime_audit(PROJECT_ROOT)
    assert receipt["managed_reflector_service"]
    assert receipt["managed_reflector_core_service_binding"]
    assert receipt["path_fallback_rejected"]
    assert receipt["status"] == "STATICALLY_PINNED"
    assert receipt["runtime_readiness_receipt_required"] is True
    assert receipt["model_execution_allowed"] is True


def test_adapter_reflector_module_has_no_subprocess_execution() -> None:
    text = (
        PROJECT_ROOT
        / "benchmarks/researchclawbench/src/openevo_researchclawbench/reflector_runner.py"
    ).read_text(encoding="utf-8")
    assert "subprocess.run(" not in text
    assert "codex exec" not in text
