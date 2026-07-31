"""Control surface for the disjoint frozen official-40 supervisor.

The Community supervisor is the sole source of a final freeze.  This module
builds an official plan from that immutable authority and never falls back to
Community operations, fixtures, or an unfrozen composite.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .config import ExperimentConfig
from .formal_v11 import validate_formal_v11_identity
from .managed_core_control import ManagedCoreControlAuthority
from .official_candidate_authority import (
    ProductionFrozenOfficialCandidateCoreAuthority,
)
from .official_training_operations import (
    OFFICIAL_DISABLED_POLICY,
    OfficialTrainingOperations,
    ProductionOfficialTrainingOperations,
    build_official_production_ports,
)
from .official_training_supervisor import (
    OfficialFrozenRunPlan,
    OfficialTrainingIdentity,
    OfficialTrainingStateStore,
    OfficialTrainingSupervisor,
    load_official_frozen_plan,
)
from .official_unified_scorer import build_production_official_unified_scorer
from .training_control import file_sha256
from .training_state_store import TrainingStateStore, canonical_sha256

_RUN_ID = re.compile(r"rcb_oe_v0_[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


class OfficialTrainingOperationsUnavailable(RuntimeError):
    """The production frozen-candidate/scorer authorities are not bound."""


class _InspectionOnlyOfficialOperations:
    def candidate_readiness(self, _request: dict[str, Any]) -> dict[str, Any]:
        raise OfficialTrainingOperationsUnavailable("OFFICIAL_CORE_FREEZE_PORT_UNAVAILABLE")

    def run_candidate(self, _request: dict[str, Any], _idempotency_key: str) -> dict[str, Any]:
        raise OfficialTrainingOperationsUnavailable("OFFICIAL_CORE_FREEZE_PORT_UNAVAILABLE")

    def validate_candidate(
        self, _request: dict[str, Any], _idempotency_key: str
    ) -> dict[str, Any]:
        raise OfficialTrainingOperationsUnavailable("OFFICIAL_CORE_FREEZE_PORT_UNAVAILABLE")

    def score_all(self, _request: dict[str, Any], _idempotency_key: str) -> dict[str, Any]:
        raise OfficialTrainingOperationsUnavailable("OFFICIAL_UNIFIED_SCORER_PORT_UNAVAILABLE")


def _identity(config: ExperimentConfig) -> OfficialTrainingIdentity:
    return OfficialTrainingIdentity(
        protocol_sha256=file_sha256(config.path),
        core_identity_sha256=str(
            config.require("source_identity.openevo_core_source_tree_sha256")
        ),
        adapter_identity_sha256=str(config.require("source_identity.adapter_tree_sha256")),
    )


def load_final_community_state(config: ExperimentConfig, community_run_id: str) -> dict[str, Any]:
    if _RUN_ID.fullmatch(community_run_id) is None:
        raise ValueError("Community source run ID is unsafe")
    root = config.experiment_root / "supervisor" / community_run_id
    if not (root / "training-supervisor.sqlite3").is_file():
        raise ValueError("Community final-freeze state is unavailable")
    return TrainingStateStore(root).load(community_run_id)


def build_official_plan(
    *, config: ExperimentConfig, community_run_id: str
) -> OfficialFrozenRunPlan:
    formal = config.formal_runs_v11
    if formal is None:
        raise ValueError("formal v11 protocol is required")
    validate_formal_v11_identity(config)
    if formal["community"]["run_id"] != community_run_id:
        raise ValueError("official source differs from the frozen v11 Community run")
    final_state = load_final_community_state(config, community_run_id)
    return load_official_frozen_plan(
        split_config=config.project_root / "configs" / "community_split.yaml",
        expected_split_sha256=str(config.require("source_identity.community_split_sha256")),
        official_task_root=config.researchclawbench_root / "tasks",
        final_state=final_state,
        identity=_identity(config),
    )


def validate_official_control(
    *,
    config: ExperimentConfig,
    community_run_id: str,
    official_run_id: str,
    core_authority: ManagedCoreControlAuthority | None = None,
) -> dict[str, Any]:
    if _RUN_ID.fullmatch(official_run_id) is None:
        raise ValueError("official run ID is unsafe")
    formal = config.formal_runs_v11
    if formal is None:
        raise ValueError("formal v11 protocol is required")
    if formal["official"]["run_id"] != official_run_id:
        raise ValueError("official run ID differs from the frozen v11 protocol")
    plan = build_official_plan(config=config, community_run_id=community_run_id)
    result = {
        "status": "PASS",
        "mode": "official_frozen_validate_only",
        "community_run_id": community_run_id,
        "official_run_id": official_run_id,
        "source_stage": "FINAL_FROZEN",
        "official_task_count": len(plan.task_ids),
        "plan_sha256": plan.sha256,
        "freeze_receipt_sha256": plan.freeze_receipt_sha256,
        "frozen_composite_id": plan.frozen_composite_id,
        "frozen_composite_sha256": plan.frozen_composite_sha256,
        "production_candidate_authority_bound": False,
        "production_scorer_authority_bound": False,
        "mutating_run_ready": False,
        "blockers": [
            "OFFICIAL_CORE_FREEZE_PORT_UNAVAILABLE",
            "OFFICIAL_UNIFIED_SCORER_PORT_UNAVAILABLE",
        ],
        "model_started": False,
        "judge_started": False,
        "secret_recorded": False,
    }
    if core_authority is None:
        return result
    operations = build_production_official_operations(
        config=config,
        community_run_id=community_run_id,
        official_run_id=official_run_id,
        core_authority=core_authority,
    )
    task_id = plan.task_ids[0]
    run_suffix = hashlib.sha256(official_run_id.encode()).hexdigest()[:12]
    request = {
        "experiment_id": official_run_id,
        "task_id": task_id,
        "task_index": 0,
        "attempt_index": 0,
        "run_id": f"{task_id}_a0_official_{run_suffix}",
        "fresh_workspace": True,
        "resume_in_place": False,
        "frozen_protocol_sha256": plan.identity.protocol_sha256,
        "freeze_receipt_sha256": plan.freeze_receipt_sha256,
        "freeze_receipt_id": plan.freeze_receipt_id,
        "freeze_authority_sha256": plan.freeze_authority_sha256,
        "frozen_composite_id": plan.frozen_composite_id,
        "frozen_composite_sha256": plan.frozen_composite_sha256,
        "frozen_registry_artifacts": plan.frozen_registry_artifacts,
        "core_project_id": plan.core_project_id,
        "core_project_head_id": plan.core_project_head_id,
        "core_project_head_manifest_sha256": (
            plan.core_project_head_manifest_sha256
        ),
        "official_policy": dict(OFFICIAL_DISABLED_POLICY),
    }
    readiness = operations.candidate_readiness(request)
    return {
        **result,
        "production_candidate_authority_bound": True,
        "production_scorer_authority_bound": True,
        "mutating_run_ready": True,
        "blockers": [],
        "candidate_preflight_sha256": canonical_sha256(readiness),
        "core_generation": core_authority.generation,
        "release_identity": core_authority.release_identity,
    }


def build_production_official_operations(
    *,
    config: ExperimentConfig,
    community_run_id: str,
    official_run_id: str,
    core_authority: ManagedCoreControlAuthority,
) -> ProductionOfficialTrainingOperations:
    """Bind one operation-scoped managed attachment to official authorities."""

    if not isinstance(core_authority, ManagedCoreControlAuthority):
        raise TypeError("official production operations require managed Core authority")
    plan = build_official_plan(config=config, community_run_id=community_run_id)
    root = Path(
        os.path.abspath(config.experiment_root / "official_supervisor" / official_run_id)
    )
    if not root.is_relative_to(config.experiment_root):
        raise ValueError("official production root escapes the experiment")
    candidate = ProductionFrozenOfficialCandidateCoreAuthority(
        config=config,
        run_root=root,
        core_authority=core_authority,
        official_task_ids=plan.task_ids,
    )
    scorer_root = root / "evaluator_private" / "unified_scorer"
    scorer = build_production_official_unified_scorer(
        config=config,
        authority_root=scorer_root,
        official_run_root=root,
        official_task_ids=plan.task_ids,
    )
    ports = build_official_production_ports(
        candidate_authority=candidate,
        scorer_authority=scorer,
        run_root=root,
        evaluator_private_root=scorer_root / "aggregate_authority" / "private",
        source_project_id=plan.core_project_id,
        source_freeze_receipt_id=plan.freeze_receipt_id,
        source_freeze_authority_sha256=plan.freeze_authority_sha256,
        source_project_head_id=plan.core_project_head_id,
        source_project_head_manifest_sha256=(
            plan.core_project_head_manifest_sha256
        ),
        frozen_registry_artifacts=plan.frozen_registry_artifacts,
        core_generation=core_authority.generation,
        release_identity=core_authority.release_identity,
    )
    return ProductionOfficialTrainingOperations(
        experiment_run_id=official_run_id,
        official_task_ids=plan.task_ids,
        frozen_protocol_sha256=plan.identity.protocol_sha256,
        frozen_composite_id=plan.frozen_composite_id,
        frozen_composite_sha256=plan.frozen_composite_sha256,
        receipt_root=root / "production_operation_receipts",
        ports=ports,
    )


class OfficialTrainingControl:
    """Durable official supervisor binding with explicit production authority."""

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        community_run_id: str,
        official_run_id: str,
        operations: OfficialTrainingOperations | None = None,
        production: bool = False,
        require_existing: bool = False,
    ) -> None:
        formal = config.formal_runs_v11
        if formal is None:
            raise ValueError("formal v11 protocol is required")
        if (
            formal["community"]["run_id"] != community_run_id
            or formal["official"]["run_id"] != official_run_id
        ):
            raise ValueError("official control run identities drifted")
        if production and operations is None:
            raise OfficialTrainingOperationsUnavailable(
                "OFFICIAL_PRODUCTION_AUTHORITIES_UNAVAILABLE"
            )
        plan = build_official_plan(config=config, community_run_id=community_run_id)
        root = Path(
            os.path.abspath(config.experiment_root / "official_supervisor" / official_run_id)
        )
        if not root.is_relative_to(config.experiment_root):
            raise ValueError("official state root escapes experiment")
        database = root / "official-training-supervisor.sqlite3"
        if require_existing and not database.is_file():
            raise ValueError("official supervisor state does not exist")
        self.root = root
        self.store = OfficialTrainingStateStore(root)
        self.supervisor = OfficialTrainingSupervisor(
            store=self.store,
            experiment_id=official_run_id,
            plan=plan,
            operations=operations or _InspectionOnlyOfficialOperations(),
        )

    def initialize(self) -> dict[str, Any]:
        return self.supervisor.initialize()

    def status(self) -> dict[str, Any]:
        return self.supervisor.status()

    def verify(self) -> dict[str, Any]:
        return self.supervisor.verify()

    def run_next(self) -> dict[str, Any]:
        return self.supervisor.run_next()

    def resume(self) -> dict[str, Any]:
        return self.supervisor.resume()

    def stop_owned(self) -> dict[str, Any]:
        resources = self.store.active_resources(self.supervisor.experiment_id)
        if not resources:
            return {
                "status": "PASS",
                "stopped": [],
                "refused_resources": [],
            }
        # Termination belongs to the typed Core/evaluator authority.  This
        # process has no safe way to infer an endpoint from a PID/container.
        return {
            "status": "REFUSED_TYPED_STOP_PORT_UNAVAILABLE",
            "stopped": [],
            "refused_resources": [item["resource_id"] for item in resources],
        }


__all__ = [
    "OfficialTrainingControl",
    "OfficialTrainingOperationsUnavailable",
    "build_official_plan",
    "build_production_official_operations",
    "load_final_community_state",
    "validate_official_control",
]
