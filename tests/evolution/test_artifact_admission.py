from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openevo.evolution.admission import (
    ArtifactAdmissionEvidence,
    ArtifactContentAdmissionBasis,
    ArtifactProposalDecisionRequest,
    DeterministicNativeArtifactAdmissionPolicy,
    NativeArtifactAdmissionService,
    NativeArtifactProposalSet,
    ProposalAction,
    artifact_content_admission_receipt,
)
from openevo.evolution.models import ArtifactResponse, ArtifactState, ArtifactType


def _artifact(artifact_id: str, *, promoted: bool = False) -> ArtifactResponse:
    return ArtifactResponse(
        artifact_id=artifact_id,
        type=ArtifactType.AGENT_SYSTEM,
        name=artifact_id,
        version=1,
        state=ArtifactState.ACTIVE,
        uri=f"file:///tmp/{artifact_id}.md",
        promoted=promoted,
    )


class _Registry:
    def __init__(self) -> None:
        self.artifacts = {
            "parent": _artifact("parent", promoted=True),
            "proposal-1": _artifact("proposal-1"),
            "proposal-2": _artifact("proposal-2"),
        }

    def get_internal_job_result(self, job_id: str):
        return {
            "job_id": job_id,
            "state": "succeeded",
            "artifact_ids": ["proposal-1", "proposal-2"],
        }

    def get_artifact(self, artifact_id: str) -> ArtifactResponse:
        return self.artifacts[artifact_id]

    def update_artifact_promotion(self, artifact_id: str, *, promoted: bool):
        self.artifacts[artifact_id] = self.artifacts[artifact_id].model_copy(
            update={"promoted": promoted}
        )
        return self.artifacts[artifact_id]

    def apply_internal_artifact_admission(self, payload):
        request = ArtifactProposalDecisionRequest.model_validate(payload)
        proposal_ids = ("proposal-1", "proposal-2")
        if (
            request.selected_artifact_id is not None
            and request.selected_artifact_id not in proposal_ids
        ):
            raise ValueError("selected artifact is not a proposal from this job")
        if request.parent_artifact_id is None:
            if not request.genesis or "parent" in self.artifacts:
                raise ValueError("parentless update lacks genesis authority")
        elif request.parent_artifact_id != "parent":
            raise ValueError("parent artifact differs from plan-bound input authority")
        active = (
            request.selected_artifact_id
            if request.action is ProposalAction.UPDATE
            else request.parent_artifact_id
        )
        for artifact_id in proposal_ids:
            self.artifacts[artifact_id] = self.artifacts[artifact_id].model_copy(
                update={"promoted": artifact_id == active}
            )
        if request.parent_artifact_id is not None:
            self.artifacts["parent"] = self.artifacts["parent"].model_copy(
                update={"promoted": True}
            )
        body = {
            "schema_version": "openevo.artifact_proposal_decision.v1",
            "decision_id": request.decision_id,
            "job_id": request.job_id,
            "artifact_type": request.artifact_type.value,
            "action": request.action.value,
            "parent_artifact_id": request.parent_artifact_id,
            "genesis": request.genesis,
            "proposal_artifact_ids": list(proposal_ids),
            "selected_artifact_id": request.selected_artifact_id,
            "rejected_artifact_ids": [
                item for item in proposal_ids if item != request.selected_artifact_id
            ],
            "active_artifact_id": active,
            "reason": request.reason,
            "admission": request.admission.model_dump(mode="json"),
            "content_admission": artifact_content_admission_receipt(
                basis=request.content_admission_basis,
                payloads={
                    proposal_id: {"AGENTS.md": "Use a generic validation workflow."}
                    for proposal_id in proposal_ids
                },
            ).model_dump(mode="json"),
            "created_at": "2026-07-29T01:00:00+00:00",
        }
        body["content_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return body


def _evidence(*, passed: bool = True) -> ArtifactAdmissionEvidence:
    return ArtifactAdmissionEvidence(
        validator_id="generic-artifact-validator-v1",
        schema_version="1.0.0",
        passed=passed,
        report_sha256="a" * 64,
    )


def _request(action: ProposalAction, **changes) -> ArtifactProposalDecisionRequest:
    payload = {
        "decision_id": f"decision-{action.value}",
        "job_id": "job-1",
        "artifact_type": "agent_system",
        "action": action,
        "parent_artifact_id": "parent",
        "selected_artifact_id": "proposal-1" if action is ProposalAction.UPDATE else None,
        "reason": "closed validation evidence",
        "admission": _evidence(),
        "content_admission_basis": ArtifactContentAdmissionBasis(),
    }
    payload.update(changes)
    return ArtifactProposalDecisionRequest.model_validate(payload)


def _service(tmp_path: Path):
    registry = _Registry()
    service = NativeArtifactAdmissionService(registry=registry, root=tmp_path / "decisions")
    authority = service.issue_admission_authority(producer="trusted_supervisor")
    return registry, service, authority


def test_update_promotes_exactly_one_successor_and_retains_other_proposals(
    tmp_path: Path,
) -> None:
    registry, service, authority = _service(tmp_path)
    receipt = service.decide(authority=authority, request=_request(ProposalAction.UPDATE))
    assert receipt.active_artifact_id == "proposal-1"
    assert receipt.selected_artifact_id == "proposal-1"
    assert receipt.rejected_artifact_ids == ("proposal-2",)
    assert registry.artifacts["parent"].promoted is True
    assert registry.artifacts["proposal-1"].promoted is True
    assert registry.artifacts["proposal-2"].promoted is False
    assert (service.root / "decision-update.json").exists()


@pytest.mark.parametrize("action", [ProposalAction.KEEP, ProposalAction.REJECT])
def test_keep_and_reject_preserve_parent_and_write_audit(
    tmp_path: Path,
    action: ProposalAction,
) -> None:
    registry, service, authority = _service(tmp_path)
    receipt = service.decide(authority=authority, request=_request(action))
    assert receipt.active_artifact_id == "parent"
    assert receipt.selected_artifact_id is None
    assert receipt.rejected_artifact_ids == ("proposal-1", "proposal-2")
    assert registry.artifacts["parent"].promoted is True


def test_failed_admission_can_only_reject(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires reject"):
        _request(ProposalAction.UPDATE, admission=_evidence(passed=False))


def test_selected_proposal_must_belong_to_exact_job(tmp_path: Path) -> None:
    _, service, authority = _service(tmp_path)
    with pytest.raises(ValueError, match="not a proposal"):
        service.decide(
            authority=authority,
            request=_request(ProposalAction.UPDATE, selected_artifact_id="other"),
        )


def test_adapter_cannot_mint_admission_authority(tmp_path: Path) -> None:
    _, service, _ = _service(tmp_path)
    with pytest.raises(PermissionError, match="Core artifact admission"):
        service.decide(authority=object(), request=_request(ProposalAction.KEEP))


def test_exact_decision_replay_is_idempotent(tmp_path: Path) -> None:
    _, service, authority = _service(tmp_path)
    request = _request(ProposalAction.KEEP)
    first = service.decide(authority=authority, request=request)
    second = service.decide(authority=authority, request=request)
    assert second == first


def test_parentless_update_requires_explicit_genesis_authority(tmp_path: Path) -> None:
    _, service, authority = _service(tmp_path)
    with pytest.raises(ValueError, match="requires genesis"):
        service.decide(
            authority=authority,
            request=_request(
                ProposalAction.UPDATE,
                parent_artifact_id=None,
            ),
        )


def test_genesis_rejects_an_existing_active_parent(tmp_path: Path) -> None:
    _, service, authority = _service(tmp_path)
    with pytest.raises(ValueError, match="parentless update"):
        service.decide(
            authority=authority,
            request=_request(
                ProposalAction.UPDATE,
                parent_artifact_id=None,
                genesis=True,
            ),
        )


def test_native_policy_selects_one_of_three_sealed_proposals_deterministically() -> None:
    proposals = tuple(
        _artifact(f"proposal-{index}").model_copy(
            update={
                "state": ArtifactState.SEALED,
                "scores": {"static_guardrail_score": score},
            }
        )
        for index, score in ((1, 0.4), (2, 0.9), (3, 0.6))
    )
    proposal_set = NativeArtifactProposalSet(
        job_id="job-gepa",
        target_id="agent_system",
        artifact_type=ArtifactType.AGENT_SYSTEM,
        parent_artifact_id="parent",
        proposals=proposals,
    )

    decision = DeterministicNativeArtifactAdmissionPolicy().decide(proposal_set)

    assert decision.action is ProposalAction.UPDATE
    assert decision.selected_artifact_id == "proposal-2"


def test_native_policy_rejects_pre_promoted_or_empty_proposal_inventory() -> None:
    promoted = _artifact("proposal-promoted", promoted=True).model_copy(
        update={"state": ArtifactState.SEALED}
    )
    with pytest.raises(ValueError, match="proposal inventory"):
        NativeArtifactProposalSet(
            job_id="job-promoted",
            target_id="agent_system",
            artifact_type=ArtifactType.AGENT_SYSTEM,
            parent_artifact_id="parent",
            proposals=(promoted,),
        )
    with pytest.raises(ValueError, match="at least 1"):
        NativeArtifactProposalSet(
            job_id="job-empty",
            target_id="agent_system",
            artifact_type=ArtifactType.AGENT_SYSTEM,
            parent_artifact_id="parent",
            proposals=(),
        )


def test_content_admission_binds_sealed_source_and_redacts_all_categories() -> None:
    source_sentence = (
        "The completed report states a source-specific conclusion that must be "
        "paraphrased before it enters a reusable global artifact."
    )
    source_record = {
        "task_id": "Astronomy_004",
        "data_file": "measurements.csv",
        "target_entity": "Source Specific Nebula",
        "candidate_report": source_sentence,
        "result": "0.123456789",
        "doi": "10.1234/source.paper",
        "absolute_path": "/private/workspace/report.md",
    }
    proposal = "\n".join(
        (
            "Astronomy_004 measurements.csv Source Specific Nebula 0.123456789",
            "10.1234/source.paper /private/workspace/report.md rubric mode",
            source_sentence,
        )
    )

    receipt = artifact_content_admission_receipt(
        basis=ArtifactContentAdmissionBasis(),
        payloads={"proposal-1": {"AGENTS.md": proposal}},
        source_payloads={
            "dataset-1": {"records.jsonl": json.dumps(source_record)}
        },
    )

    assert receipt.passed is False
    assert set(receipt.finding_categories) == {
        "absolute_path",
        "doi",
        "entity",
        "evaluator_term",
        "file_name",
        "report_sentence",
        "task_id",
        "value",
    }
    assert receipt.source_artifact_ids == ("dataset-1",)
    assert receipt.source_payload_sha256 is not None
    serialized = json.dumps(receipt.model_dump(mode="json"), sort_keys=True)
    for protected in (
        "Astronomy_004",
        "measurements.csv",
        "Source Specific Nebula",
        source_sentence,
    ):
        assert protected not in serialized


def test_content_admission_passes_generic_transferable_artifact() -> None:
    receipt = artifact_content_admission_receipt(
        basis=ArtifactContentAdmissionBasis(task_ids=("Astronomy_004",)),
        payloads={
            "proposal-1": {
                "SKILL.md": "Run a generic report completeness check before exit."
            }
        },
        source_payloads={
            "dataset-1": {
                "records.jsonl": json.dumps(
                    {
                        "task_id": "Astronomy_004",
                        "candidate_report": "A task-local result should remain local.",
                    }
                )
            }
        },
    )

    assert receipt.passed is True
    assert receipt.finding_count == 0


def test_task_local_preservation_keeps_candidate_sources_but_not_basis_literals() -> None:
    source = json.dumps(
        {
            "task_id": "Astronomy_004",
            "candidate_report": "Generated analysis used candidate_plot.png.",
            "output_file": "candidate_plot.png",
        }
    )
    scoped = ArtifactContentAdmissionBasis(
        task_ids=("Astronomy_004",),
        file_names=("evaluator_private.csv",),
        candidate_source_reuse_authorized=True,
    )

    preserved = artifact_content_admission_receipt(
        basis=scoped,
        payloads={
            "proposal-1": {
                "SKILL.md": "Reconstruct the analysis that produced candidate_plot.png."
            }
        },
        source_payloads={"dataset-1": {"records.jsonl": source}},
    )
    blocked = artifact_content_admission_receipt(
        basis=scoped,
        payloads={
            "proposal-2": {
                "SKILL.md": "Do not expose evaluator_private.csv to the candidate."
            }
        },
        source_payloads={"dataset-1": {"records.jsonl": source}},
    )

    assert preserved.passed is True
    assert preserved.source_artifact_ids == ()
    assert preserved.source_payload_sha256 is None
    assert blocked.passed is False
    assert blocked.finding_categories == ("file_name",)


def test_content_admission_allows_only_the_exact_managed_workspace_root() -> None:
    transferable = artifact_content_admission_receipt(
        basis=ArtifactContentAdmissionBasis(),
        payloads={
            "proposal-1": {
                "SKILL.md": (
                    "Treat /openevo/session/workspace as the managed project root."
                )
            }
        },
        source_payloads={
            "dataset-1": {
                "records.jsonl": json.dumps(
                    {"runtime_root": "/openevo/session/workspace"}
                )
            }
        },
    )
    descendant = artifact_content_admission_receipt(
        basis=ArtifactContentAdmissionBasis(),
        payloads={
            "proposal-2": {
                "SKILL.md": "Read /openevo/session/workspace/private/report.md."
            }
        },
    )

    assert transferable.passed is True
    assert transferable.finding_count == 0
    assert descendant.passed is False
    assert descendant.finding_categories == ("absolute_path",)
