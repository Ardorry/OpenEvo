"""Durable production operation driver for frozen official benchmark runs.

The official driver is intentionally separate from Community training.  It has
no attachment, teacher, reflector, evolution, composite, overlay, or
cross-task-update operation surface.  Candidate and unified-scorer side
effects are delegated to typed authorities and recovered by idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .artifact_validator import freeze_candidate_outputs, validate_workspace
from .config import ARTIFACT_TYPES
from .freeze_gate import validate_official_run_set
from .production_training_operations import (
    FailureClass,
    OperationReceiptStore,
    OperationResult,
    OperationStatus,
    ProductionOperationError,
    ProductionOperationPort,
)
from .run_manifest import atomic_write_json
from .training_state_store import canonical_bytes, canonical_sha256

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


OFFICIAL_DISABLED_POLICY = {
    "evolution_enabled": False,
    "reflector_enabled": False,
    "training_feedback_attachment_enabled": False,
    "teacher_enabled": False,
    "task_local_overlay_enabled": False,
    "cross_task_state_updates": False,
    "feedback_released": False,
}


class OfficialOperationPort(ProductionOperationPort, Protocol):
    """External official-run authority with durable idempotent recovery."""


@dataclass(frozen=True)
class OfficialProductionPorts:
    candidate: OfficialOperationPort
    validation: OfficialOperationPort
    scorer: OfficialOperationPort


class FrozenOfficialCandidateCoreAuthority(Protocol):
    """Core-owned frozen-head execution service used by the official port.

    The authority is expected to create one independent project per task,
    inject the three frozen registry artifacts into the runtime context, and
    submit the native TaskRequest/CodexHarness run.  It is deliberately not a
    callback that can execute Codex in adapter code.
    """

    def preflight_frozen_candidate(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def recover_frozen_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None: ...

    def execute_frozen_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...


class OfficialUnifiedScorerAuthority(Protocol):
    """Evaluator-only authority for one unified official scoring operation."""

    def recover_unified_score(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None: ...

    def execute_unified_score(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...


class CoreFrozenOfficialCandidatePort:
    """Strict official port over a Core-owned frozen-candidate authority."""

    def __init__(
        self,
        *,
        authority: FrozenOfficialCandidateCoreAuthority,
        source_project_id: str,
        source_freeze_receipt_id: str,
        source_freeze_authority_sha256: str,
        source_project_head_id: str,
        source_project_head_manifest_sha256: str,
        frozen_registry_artifacts: dict[str, dict[str, str]],
        core_generation: str,
        release_identity: str,
    ) -> None:
        if set(frozen_registry_artifacts) != set(ARTIFACT_TYPES):
            raise ValueError("official Core port requires three frozen artifact types")
        for artifact_type in ARTIFACT_TYPES:
            artifact = frozen_registry_artifacts[artifact_type]
            if (
                not isinstance(artifact.get("registry_id"), str)
                or not artifact["registry_id"]
                or not isinstance(artifact.get("sha256"), str)
                or _SHA256.fullmatch(artifact["sha256"]) is None
            ):
                raise ValueError(f"official {artifact_type} authority is incomplete")
        if (
            not source_project_id
            or not source_freeze_receipt_id
            or _SHA256.fullmatch(source_freeze_authority_sha256) is None
            or not source_project_head_id
            or _SHA256.fullmatch(source_project_head_manifest_sha256) is None
            or not core_generation
            or _SHA256.fullmatch(release_identity) is None
        ):
            raise ValueError("official Core source authority is incomplete")
        self.authority = authority
        self.source_project_id = source_project_id
        self.source_freeze_receipt_id = source_freeze_receipt_id
        self.source_freeze_authority_sha256 = source_freeze_authority_sha256
        self.source_project_head_id = source_project_head_id
        self.source_project_head_manifest_sha256 = (
            source_project_head_manifest_sha256
        )
        self.frozen_registry_artifacts = json.loads(
            canonical_bytes(frozen_registry_artifacts)
        )
        self.core_generation = core_generation
        self.release_identity = release_identity

    @property
    def context_artifact_ids(self) -> list[str]:
        return [
            self.frozen_registry_artifacts[artifact_type]["registry_id"]
            for artifact_type in ARTIFACT_TYPES
        ]

    def _require_source(self, request: dict[str, Any]) -> None:
        if (
            request.get("core_project_id") != self.source_project_id
            or request.get("freeze_receipt_id") != self.source_freeze_receipt_id
            or request.get("freeze_authority_sha256")
            != self.source_freeze_authority_sha256
            or request.get("core_project_head_id") != self.source_project_head_id
            or request.get("core_project_head_manifest_sha256")
            != self.source_project_head_manifest_sha256
            or request.get("frozen_registry_artifacts")
            != self.frozen_registry_artifacts
            or request.get("official_policy") != OFFICIAL_DISABLED_POLICY
        ):
            raise ValueError("official Core candidate source authority drifted")

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        self._require_source(request)
        result = self.authority.preflight_frozen_candidate(request)
        required_true = {
            "service_reachable",
            "bearer_present",
            "bearer_valid",
            "generation_matches",
            "candidate_port_authorized",
            "source_project_head_verified",
            "triple_artifact_context_verified",
            "independent_project_creation_ready",
            "frozen_project_fork_supported",
            "all_evolution_targets_disabled",
        }
        if (
            not isinstance(result, dict)
            or any(result.get(key) is not True for key in required_true)
            or result.get("core_generation") != self.core_generation
            or result.get("release_identity") != self.release_identity
            or result.get("source_project_id") != self.source_project_id
            or result.get("source_freeze_receipt_id")
            != self.source_freeze_receipt_id
            or result.get("source_freeze_authority_sha256")
            != self.source_freeze_authority_sha256
            or result.get("source_project_head_id") != self.source_project_head_id
            or result.get("source_project_head_manifest_sha256")
            != self.source_project_head_manifest_sha256
            or result.get("context_artifact_ids") != self.context_artifact_ids
            or result.get("secret_recorded") is not False
            or result.get("environment_fallback_used") is not False
            or result.get("model_started") is not False
        ):
            raise ProductionOperationError("OFFICIAL_CORE_FREEZE_PREFLIGHT_NOT_READY")
        return {
            **result,
            "official_frozen_policy_ready": True,
            "frozen_composite_matches": True,
            "registry_artifacts_frozen": True,
        }

    def recover(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        self._require_source(request)
        result = self.authority.recover_frozen_candidate(request, idempotency_key)
        return None if result is None else self._validate_result(request, result)

    def execute(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        self._require_source(request)
        self.preflight(request)
        result = self.authority.execute_frozen_candidate(request, idempotency_key)
        return self._validate_result(request, result)

    def _validate_result(
        self, request: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise TypeError("official Core candidate result is not an object")
        destination_project_id = result.get("destination_core_project_id")
        completed = result.get("completed")
        if (
            not isinstance(destination_project_id, str)
            or not destination_project_id
            or destination_project_id == self.source_project_id
            or result.get("source_project_id") != self.source_project_id
            or result.get("source_freeze_receipt_id")
            != self.source_freeze_receipt_id
            or result.get("source_freeze_authority_sha256")
            != self.source_freeze_authority_sha256
            or result.get("source_project_head_id") != self.source_project_head_id
            or result.get("source_project_head_manifest_sha256")
            != self.source_project_head_manifest_sha256
            or result.get("context_artifact_ids") != self.context_artifact_ids
            or result.get("evolution_targets_enabled") != []
            or result.get("task_id") != request.get("task_id")
            or result.get("run_id") != request.get("run_id")
            or result.get("attempt_index") != 0
            or type(completed) is not bool
            or result.get("sealed") is not True
            or result.get("fresh_workspace") is not True
            or result.get("frozen_composite_id")
            != request.get("frozen_composite_id")
            or result.get("frozen_composite_sha256")
            != request.get("frozen_composite_sha256")
            or result.get("official_policy") != OFFICIAL_DISABLED_POLICY
            or result.get("attachment_ids") not in (None, [])
            or result.get("evolution_job_ids") not in (None, [])
        ):
            raise ProductionOperationError("official Core candidate receipt is invalid")
        for field in ("session_id", "dataset_id", "dataset_revision", "candidate_output_root"):
            if not isinstance(result.get(field), str) or not result[field]:
                raise ProductionOperationError(
                    f"official Core candidate receipt lacks {field}"
                )
        for field in (
            "frozen_project_fork_receipt_id",
            "frozen_project_fork_authority_sha256",
            "seeded_destination_project_head_id",
            "seeded_destination_project_head_manifest_sha256",
        ):
            value = result.get(field)
            if not isinstance(value, str) or not value:
                raise ProductionOperationError(
                    f"official Core candidate receipt lacks {field}"
                )
        if (
            _SHA256.fullmatch(result["frozen_project_fork_authority_sha256"])
            is None
            or _SHA256.fullmatch(
                result["seeded_destination_project_head_manifest_sha256"]
            )
            is None
            or result.get("frozen_project_fork_source_mutated") is not False
            or result.get("frozen_project_fork_append_only") is not True
        ):
            raise ProductionOperationError(
                "official Core frozen project fork receipt is invalid"
            )
        return json.loads(canonical_bytes(result))


class OfficialArtifactValidationPort:
    """Read-only official validator with an immutable adapter receipt."""

    def __init__(self, run_root: str | Path) -> None:
        self.run_root = Path(os.path.abspath(run_root))
        self.run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.receipt_root = self.run_root / "official_validator_receipts"
        self.receipt_root.mkdir(mode=0o700, exist_ok=True)

    def _path(self, idempotency_key: str) -> Path:
        return self.receipt_root / (
            hashlib.sha256(idempotency_key.encode()).hexdigest() + ".json"
        )

    def recover(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        if not path.is_file():
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("request_sha256") != canonical_sha256(request):
            raise ValueError("official validator recovery request drifted")
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise TypeError("official validator recovery receipt is invalid")
        return result

    def execute(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        recovered = self.recover(request, idempotency_key)
        if recovered is not None:
            return recovered
        candidate = request.get("candidate")
        if not isinstance(candidate, dict):
            raise TypeError("official validator lacks a candidate receipt")
        root = Path(candidate["candidate_output_root"]).resolve(strict=True)
        runs_root = (self.run_root / "runs").resolve(strict=False)
        if root.is_symlink() or not root.is_relative_to(runs_root):
            raise ValueError("official validator output root escapes the run namespace")
        validation = validate_workspace(root).to_dict()
        artifact_valid = validation["passed"] and candidate.get("completed") is True
        result = {
            "artifact_valid": artifact_valid,
            "artifact_root_sha256": validation["artifact_root_sha256"],
            "completeness": validation["validator_completeness"],
            "validator_errors": (
                validation["errors"]
                if candidate.get("completed") is True
                else [*validation["errors"], "CANDIDATE_NOT_COMPLETED"]
            ),
            "validator_receipt_id": "official-validator-"
            + canonical_sha256(validation)[:24],
            "validator_receipt_sha256": canonical_sha256(validation),
            "terminal": True,
            "judge_invoked": False,
            "reflector_invoked": False,
            "evolution_invoked": False,
            "same_attempt_retry_allowed": False,
            "outputs_frozen_read_only": True,
        }
        freeze_candidate_outputs(root)
        result = json.loads(canonical_bytes(result))
        atomic_write_json(
            self._path(idempotency_key),
            {
                "request_sha256": canonical_sha256(request),
                "result": result,
            },
        )
        return result


class OfficialUnifiedScorerPort:
    """Evaluator-only exactly-once facade for the unified official scorer."""

    def __init__(
        self,
        *,
        authority: OfficialUnifiedScorerAuthority,
        evaluator_private_root: str | Path,
    ) -> None:
        self.authority = authority
        self.evaluator_private_root = Path(
            os.path.abspath(evaluator_private_root)
        )
        self.evaluator_private_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def recover(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        result = self.authority.recover_unified_score(request, idempotency_key)
        return None if result is None else self._validate_result(request, result)

    def execute(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        recovered = self.recover(request, idempotency_key)
        if recovered is not None:
            return recovered
        return self._validate_result(
            request,
            self.authority.execute_unified_score(request, idempotency_key),
        )

    def _validate_result(
        self, request: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise TypeError("official scorer authority returned a non-object")
        private_path = result.get("private_receipt_path")
        if not isinstance(private_path, str):
            raise TypeError("official scorer lacks an evaluator-private receipt")
        resolved = Path(private_path).resolve(strict=True)
        if (
            resolved.is_symlink()
            or not resolved.is_file()
            or not resolved.is_relative_to(self.evaluator_private_root)
        ):
            raise ValueError("official scorer private receipt escaped its boundary")
        private_sha256 = _file_sha256(resolved)
        if result.get("private_receipt_sha256") != private_sha256:
            raise ValueError("official scorer private receipt hash changed")
        forbidden = {
            "raw_judge_response",
            "raw_judge_request",
            "judge_reasoning",
            "feedback_attachment",
            "task_local_overlay",
        }
        if forbidden.intersection(result):
            raise ValueError("official scorer exposed evaluator-private feedback")
        if (
            result.get("official_task_count") != 40
            or result.get("official_run_set_sha256")
            != request.get("official_run_set_sha256")
            or result.get("runs_per_attempt") != 1
            or result.get("pass_at_k") != 1
            or result.get("feedback_released") is not False
            or result.get("artifact_updates_performed") != 0
            or result.get("scoring_complete") is not True
            or result.get("raw_judge_outputs_private") is not True
        ):
            raise ValueError("official scorer authority changed the frozen policy")
        return json.loads(canonical_bytes(result))


def build_official_production_ports(
    *,
    candidate_authority: FrozenOfficialCandidateCoreAuthority,
    scorer_authority: OfficialUnifiedScorerAuthority,
    run_root: str | Path,
    evaluator_private_root: str | Path,
    source_project_id: str,
    source_freeze_receipt_id: str,
    source_freeze_authority_sha256: str,
    source_project_head_id: str,
    source_project_head_manifest_sha256: str,
    frozen_registry_artifacts: dict[str, dict[str, str]],
    core_generation: str,
    release_identity: str,
) -> OfficialProductionPorts:
    return OfficialProductionPorts(
        candidate=CoreFrozenOfficialCandidatePort(
            authority=candidate_authority,
            source_project_id=source_project_id,
            source_freeze_receipt_id=source_freeze_receipt_id,
            source_freeze_authority_sha256=source_freeze_authority_sha256,
            source_project_head_id=source_project_head_id,
            source_project_head_manifest_sha256=source_project_head_manifest_sha256,
            frozen_registry_artifacts=frozen_registry_artifacts,
            core_generation=core_generation,
            release_identity=release_identity,
        ),
        validation=OfficialArtifactValidationPort(run_root),
        scorer=OfficialUnifiedScorerPort(
            authority=scorer_authority,
            evaluator_private_root=evaluator_private_root,
        ),
    )


class OfficialTrainingOperations(Protocol):
    def candidate_readiness(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def run_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def validate_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def score_all(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...


def _operation_result_from_closed(value: dict[str, Any]) -> OperationResult:
    required = {
        "operation_id",
        "idempotency_key",
        "status",
        "started_at",
        "completed_at",
        "input_identity",
        "output_identity",
        "receipt_path",
        "content_sha256",
        "owned_resource_ids",
        "retryable",
        "failure_class",
    }
    if not required.issubset(value):
        raise ValueError("official operation result is incomplete")
    content = value.get("content_sha256")
    body = {key: item for key, item in value.items() if key != "content_sha256"}
    if (
        not isinstance(content, str)
        or _SHA256.fullmatch(content) is None
        or canonical_sha256(body) != content
    ):
        raise ValueError("official operation result hash is invalid")
    return OperationResult(
        operation_id=str(value["operation_id"]),
        idempotency_key=str(value["idempotency_key"]),
        status=OperationStatus(value["status"]),
        started_at=str(value["started_at"]),
        completed_at=(
            None if value["completed_at"] is None else str(value["completed_at"])
        ),
        input_identity=str(value["input_identity"]),
        output_identity=(
            None if value["output_identity"] is None else str(value["output_identity"])
        ),
        receipt_path=str(value["receipt_path"]),
        content_sha256=content,
        owned_resource_ids=tuple(value["owned_resource_ids"]),
        retryable=bool(value["retryable"]),
        failure_class=FailureClass(value["failure_class"]),
        payload={
            key: item
            for key, item in value.items()
            if key not in required and key != "operation_kind"
        },
    )


def _require_disabled_policy(value: object) -> dict[str, bool]:
    if not isinstance(value, dict) or value != OFFICIAL_DISABLED_POLICY:
        raise ValueError("official run changed the frozen no-evolution policy")
    return dict(OFFICIAL_DISABLED_POLICY)


def _require_task_manifest(
    manifest: dict[str, Any],
    *,
    task_id: str,
    task_index: int,
    protocol_sha256: str,
    composite_id: str,
    composite_sha256: str,
) -> None:
    required = {
        "task_id",
        "task_index",
        "attempt_index",
        "run_id",
        "session_id",
        "dataset_id",
        "dataset_revision",
        "sealed",
        "completed",
        "artifact_valid",
        "validator_receipt_id",
        "validator_receipt_sha256",
        "frozen_protocol_sha256",
        "frozen_composite_id",
        "frozen_composite_sha256",
        *OFFICIAL_DISABLED_POLICY,
    }
    if not required.issubset(manifest):
        raise ValueError("official task manifest is incomplete")
    if (
        manifest["task_id"] != task_id
        or manifest["task_index"] != task_index
        or manifest["attempt_index"] != 0
        or manifest["sealed"] is not True
        or manifest["frozen_protocol_sha256"] != protocol_sha256
        or manifest["frozen_composite_id"] != composite_id
        or manifest["frozen_composite_sha256"] != composite_sha256
        or any(manifest[key] is not expected for key, expected in OFFICIAL_DISABLED_POLICY.items())
    ):
        raise ValueError("official task manifest changed frozen execution identity")
    for key in ("session_id", "dataset_id", "dataset_revision", "run_id", "validator_receipt_id"):
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise ValueError(f"official task manifest lacks {key}")
    if (
        not isinstance(manifest["validator_receipt_sha256"], str)
        or _SHA256.fullmatch(manifest["validator_receipt_sha256"]) is None
    ):
        raise ValueError("official validator receipt hash is invalid")
    if type(manifest["artifact_valid"]) is not bool or type(manifest["completed"]) is not bool:
        raise ValueError("official terminal result flags are invalid")


class ProductionOfficialTrainingOperations:
    """Exactly-once driver over official candidate, validator, and scorer ports."""

    def __init__(
        self,
        *,
        experiment_run_id: str,
        official_task_ids: tuple[str, ...],
        frozen_protocol_sha256: str,
        frozen_composite_id: str,
        frozen_composite_sha256: str,
        receipt_root: str | Path,
        ports: OfficialProductionPorts,
    ) -> None:
        if _IDENTITY.fullmatch(experiment_run_id) is None:
            raise ValueError("unsafe official experiment run ID")
        if len(official_task_ids) != 40 or len(set(official_task_ids)) != 40:
            raise ValueError("official production operations require 40 distinct tasks")
        if any(_IDENTITY.fullmatch(task_id) is None for task_id in official_task_ids):
            raise ValueError("official task inventory contains an unsafe task ID")
        for digest in (frozen_protocol_sha256, frozen_composite_sha256):
            if _SHA256.fullmatch(digest) is None:
                raise ValueError("official frozen identity contains an invalid digest")
        if _IDENTITY.fullmatch(frozen_composite_id) is None:
            raise ValueError("official frozen composite ID is invalid")
        self.experiment_run_id = experiment_run_id
        self.official_task_ids = official_task_ids
        self.frozen_protocol_sha256 = frozen_protocol_sha256
        self.frozen_composite_id = frozen_composite_id
        self.frozen_composite_sha256 = frozen_composite_sha256
        self.receipts = OperationReceiptStore(receipt_root)
        self.ports = ports

    def _require_task_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise TypeError("official operation request must be an object")
        task_id = request.get("task_id")
        if task_id not in self.official_task_ids:
            raise ValueError("official operation task is outside the frozen inventory")
        if (
            request.get("attempt_index") != 0
            or request.get("fresh_workspace") is not True
            or request.get("resume_in_place") is not False
            or request.get("frozen_protocol_sha256") != self.frozen_protocol_sha256
            or request.get("frozen_composite_id") != self.frozen_composite_id
            or request.get("frozen_composite_sha256")
            != self.frozen_composite_sha256
        ):
            raise ValueError("official candidate request changed frozen execution identity")
        _require_disabled_policy(request.get("official_policy"))
        forbidden = {
            "attachment_id",
            "attachment_ids",
            "task_local_overlay_id",
            "task_local_overlay_scope_id",
            "evolution_job_id",
            "evolution_job_ids",
            "feedback",
            "teacher_payload",
        }
        if forbidden.intersection(request):
            raise ValueError("official candidate request contains training state")
        return json.loads(canonical_bytes(request))

    def candidate_readiness(self, request: dict[str, Any]) -> dict[str, Any]:
        request = self._require_task_request(request)
        preflight = getattr(self.ports.candidate, "preflight", None)
        if not callable(preflight):
            raise ProductionOperationError(
                "official candidate authority has no pre-intent readiness boundary"
            )
        result = preflight(request)
        required_true = {
            "service_reachable",
            "bearer_present",
            "bearer_valid",
            "generation_matches",
            "candidate_port_authorized",
            "official_frozen_policy_ready",
            "frozen_composite_matches",
            "registry_artifacts_frozen",
        }
        if (
            not isinstance(result, dict)
            or any(result.get(field) is not True for field in required_true)
            or result.get("secret_recorded") is not False
            or result.get("environment_fallback_used") is not False
            or result.get("model_started") is not False
        ):
            raise ProductionOperationError("OFFICIAL_CANDIDATE_PREFLIGHT_NOT_READY")
        return json.loads(canonical_bytes(result))

    def run_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        request = self._require_task_request(request)
        result = self._run(
            kind="official_candidate",
            port=self.ports.candidate,
            request=request,
            idempotency_key=idempotency_key,
        ).to_dict()
        required = {
            "run_id",
            "session_id",
            "dataset_id",
            "dataset_revision",
            "completed",
            "sealed",
            "fresh_workspace",
            "task_id",
            "attempt_index",
            "frozen_composite_id",
            "frozen_composite_sha256",
            "official_policy",
        }
        if not required.issubset(result):
            raise ProductionOperationError("official candidate receipt is incomplete")
        if (
            result["task_id"] != request["task_id"]
            or result["run_id"] != request["run_id"]
            or result["attempt_index"] != 0
            or type(result["completed"]) is not bool
            or result["sealed"] is not True
            or result["fresh_workspace"] is not True
            or result["frozen_composite_id"] != self.frozen_composite_id
            or result["frozen_composite_sha256"] != self.frozen_composite_sha256
        ):
            raise ProductionOperationError("official candidate authority changed")
        _require_disabled_policy(result["official_policy"])
        return result

    def validate_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        candidate = request.get("candidate")
        if not isinstance(candidate, dict):
            raise TypeError("official validation lacks a candidate authority")
        task_request = {
            key: request[key]
            for key in (
                "task_id",
                "attempt_index",
                "fresh_workspace",
                "resume_in_place",
                "frozen_protocol_sha256",
                "frozen_composite_id",
                "frozen_composite_sha256",
                "official_policy",
            )
        }
        task_request["run_id"] = candidate.get("run_id")
        self._require_task_request(task_request)
        result = self._run(
            kind="official_validation",
            port=self.ports.validation,
            request=json.loads(canonical_bytes(request)),
            idempotency_key=idempotency_key,
        ).to_dict()
        if (
            type(result.get("artifact_valid")) is not bool
            or not isinstance(result.get("validator_receipt_id"), str)
            or not isinstance(result.get("validator_receipt_sha256"), str)
            or _SHA256.fullmatch(result["validator_receipt_sha256"]) is None
            or result.get("terminal") is not True
            or result.get("judge_invoked") is not False
            or result.get("reflector_invoked") is not False
            or result.get("evolution_invoked") is not False
        ):
            raise ProductionOperationError("official validator receipt is incomplete")
        return result

    def score_all(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        manifests = request.get("run_manifests")
        if not isinstance(manifests, list):
            raise TypeError("official scorer lacks the sealed run set")
        validate_official_run_set(
            manifests,
            frozen_protocol_sha256=self.frozen_protocol_sha256,
        )
        by_task = {manifest.get("task_id"): manifest for manifest in manifests}
        if tuple(by_task) != self.official_task_ids:
            raise ValueError("official scorer run order differs from the frozen inventory")
        for task_index, task_id in enumerate(self.official_task_ids):
            _require_task_manifest(
                by_task[task_id],
                task_id=task_id,
                task_index=task_index,
                protocol_sha256=self.frozen_protocol_sha256,
                composite_id=self.frozen_composite_id,
                composite_sha256=self.frozen_composite_sha256,
            )
        if (
            request.get("official_policy") != OFFICIAL_DISABLED_POLICY
            or request.get("runs_per_attempt") != 1
            or request.get("pass_at_k") != 1
            or request.get("scoring_mode") != "unified_after_all_40_sealed"
        ):
            raise ValueError("official scorer policy changed")
        run_set_sha256 = canonical_sha256(manifests)
        closed_request = {
            **request,
            "official_run_set_sha256": run_set_sha256,
        }
        result = self._run(
            kind="official_unified_scorer",
            port=self.ports.scorer,
            request=closed_request,
            idempotency_key=idempotency_key,
        ).to_dict()
        if (
            result.get("official_task_count") != 40
            or result.get("official_run_set_sha256") != run_set_sha256
            or result.get("runs_per_attempt") != 1
            or result.get("pass_at_k") != 1
            or result.get("feedback_released") is not False
            or result.get("artifact_updates_performed") != 0
            or result.get("scoring_complete") is not True
            or result.get("raw_judge_outputs_private") is not True
            or type(result.get("total_score")) not in {int, float}
            or not math.isfinite(float(result["total_score"]))
            or not isinstance(result.get("scorer_receipt_id"), str)
        ):
            raise ProductionOperationError("official unified scorer receipt is incomplete")
        return result

    def _run(
        self,
        *,
        kind: str,
        port: OfficialOperationPort,
        request: dict[str, Any],
        idempotency_key: str,
    ) -> OperationResult:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("official operation lacks an idempotency key")
        input_identity = canonical_sha256(request)
        existing = self.receipts.get(idempotency_key)
        if existing is not None:
            if existing.get("input_identity") != input_identity:
                raise ValueError("official operation receipt request drifted")
            return _operation_result_from_closed(existing)
        recovered = port.recover(request, idempotency_key)
        status = OperationStatus.RECOVERED
        if recovered is None:
            recovered = port.execute(request, idempotency_key)
            status = OperationStatus.SUCCEEDED
        if not isinstance(recovered, dict):
            raise ProductionOperationError(
                f"{kind} authority returned a non-object receipt"
            )
        output = json.loads(canonical_bytes(recovered))
        owned = output.pop("owned_resource_ids", [])
        if (
            not isinstance(owned, list)
            or not all(
                isinstance(item, str) and _IDENTITY.fullmatch(item) is not None
                for item in owned
            )
        ):
            raise ProductionOperationError(
                f"{kind} authority returned invalid owned resources"
            )
        operation_id = "op-" + hashlib.sha256(
            f"{kind}:{idempotency_key}".encode()
        ).hexdigest()[:24]
        from datetime import UTC, datetime

        timestamp = datetime.now(UTC).isoformat()
        receipt_path = str(self.receipts._path(idempotency_key))
        closed = self.receipts.put(
            {
                "operation_id": operation_id,
                "operation_kind": kind,
                "idempotency_key": idempotency_key,
                "status": status.value,
                "started_at": timestamp,
                "completed_at": timestamp,
                "input_identity": input_identity,
                "output_identity": canonical_sha256(output),
                "receipt_path": receipt_path,
                "owned_resource_ids": owned,
                "retryable": False,
                "failure_class": FailureClass.NONE.value,
                **output,
            }
        )
        return _operation_result_from_closed(closed)


__all__ = [
    "OFFICIAL_DISABLED_POLICY",
    "CoreFrozenOfficialCandidatePort",
    "FrozenOfficialCandidateCoreAuthority",
    "OfficialArtifactValidationPort",
    "OfficialOperationPort",
    "OfficialProductionPorts",
    "OfficialTrainingOperations",
    "OfficialUnifiedScorerAuthority",
    "OfficialUnifiedScorerPort",
    "ProductionOfficialTrainingOperations",
    "build_official_production_ports",
]
