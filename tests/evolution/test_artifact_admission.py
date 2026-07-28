from __future__ import annotations

from pathlib import Path

import pytest

from openevo.evolution.admission import (
    ArtifactAdmissionEvidence,
    ArtifactProposalDecisionRequest,
    NativeArtifactAdmissionService,
    ProposalAction,
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
    assert registry.artifacts["parent"].promoted is False
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


def test_decision_receipt_cannot_be_overwritten(tmp_path: Path) -> None:
    _, service, authority = _service(tmp_path)
    request = _request(ProposalAction.KEEP)
    service.decide(authority=authority, request=request)
    with pytest.raises((FileExistsError, ValueError)):
        service.decide(authority=authority, request=request)
