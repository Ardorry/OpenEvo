"""Fail-closed durable supervisor control surface.

Read-only inspection and exact owned-process termination do not require a
candidate/evaluator driver.  Mutating transitions require a concrete
``TrainingOperations`` binding; this module never substitutes a fixture or
infers completion from logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .config import FROZEN_TASKS, ExperimentConfig
from .managed_core_control import ManagedCoreControlAuthority
from .owned_resource_registry import OwnedResourceRegistry
from .production_operation_ports import build_production_ports
from .production_training_operations import ProductionTrainingOperations
from .training_state_store import TrainingStateStore
from .training_supervisor import (
    CommunityTrainingSupervisor,
    SupervisorIdentity,
    TrainingBudgetPolicy,
    TrainingOperations,
)

_RUN_ID = re.compile(r"rcb_oe_v0_[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


class TrainingOperationsUnavailable(RuntimeError):
    """No production Core/evaluator operations binding was supplied."""


class _InspectionOnlyOperations:
    def __getattr__(self, name: str):
        if name.startswith(("candidate_", "ensure_", "validate_", "evaluate", "attach_", "evolve_", "admit_", "destroy_", "sanitize_", "freeze_")):
            raise TrainingOperationsUnavailable(
                "mutating supervisor command requires production TrainingOperations"
            )
        raise AttributeError(name)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def supervisor_identity(config: ExperimentConfig) -> SupervisorIdentity:
    return SupervisorIdentity(
        protocol_sha256=file_sha256(config.path),
        core_identity_sha256=str(
            config.require("source_identity.openevo_core_source_tree_sha256")
        ),
        adapter_identity_sha256=str(
            config.require("source_identity.adapter_tree_sha256")
        ),
    )


class DurableTrainingControl:
    def __init__(
        self,
        *,
        config: ExperimentConfig,
        run_id: str,
        operations: TrainingOperations | None = None,
        require_existing: bool = False,
        task_ids: tuple[str, ...] | None = None,
        production: bool = False,
        core_control_authority: ManagedCoreControlAuthority | None = None,
    ) -> None:
        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("training run ID must use the rcb_oe_v0_ namespace")
        root = config.experiment_root / "supervisor" / run_id
        resolved = Path(os.path.abspath(root))
        if not resolved.is_relative_to(config.experiment_root):
            raise ValueError("training state root escapes the experiment")
        self.root = resolved
        if require_existing and not (
            self.root / "training-supervisor.sqlite3"
        ).is_file():
            raise ValueError("training supervisor state does not exist")
        self.store = TrainingStateStore(self.root)
        if require_existing:
            persisted = self.store.load(run_id)
            persisted_tasks = tuple(persisted.get("task_ids", ()))
            if task_ids is not None and task_ids != persisted_tasks:
                raise ValueError("requested task scope differs from persisted supervisor state")
            task_ids = persisted_tasks
        task_ids = task_ids or FROZEN_TASKS
        if production and operations is None:
            if core_control_authority is None:
                raise TrainingOperationsUnavailable(
                    "managed Core-control attachment is required before production state"
                )
            operations = ProductionTrainingOperations(
                config=config,
                experiment_run_id=run_id,
                receipt_root=self.root / "operations",
                ports=build_production_ports(
                    config,
                    self.root,
                    core_authority=core_control_authority,
                ),
            )
        self.supervisor = CommunityTrainingSupervisor(
            store=self.store,
            experiment_id=run_id,
            identity=supervisor_identity(config),
            operations=operations or _InspectionOnlyOperations(),
            task_ids=task_ids,
            validator_failure_policy=getattr(
                config, "community_validator_failure_policy", None
            ),
            budget_policy=TrainingBudgetPolicy(
                max_candidate_model_calls=int(
                    config.require("budgets.max_candidate_model_calls")
                ),
                max_reflector_model_calls=int(
                    config.require("budgets.max_reflector_model_calls")
                ),
                max_judge_operations=(
                    len(task_ids)
                    * 3
                    * int(config.require("judge.runs_per_attempt"))
                ),
                cumulative_runtime_seconds=int(
                    config.require("budgets.cumulative_runtime_seconds")
                ),
            ),
        )
        self.resources = OwnedResourceRegistry(self.store, run_id)

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
        stopped: list[str] = []
        already_gone: list[str] = []
        refused: list[str] = []
        for item in self.store.active_resources(self.supervisor.experiment_id):
            resource_id = item["resource_id"]
            if item["kind"] != "process":
                # Container/session/worker termination must go through their
                # typed Core owner.  A process-only CLI must never guess.
                refused.append(resource_id)
                continue
            if self.resources.stop_process(resource_id):
                stopped.append(resource_id)
            else:
                already_gone.append(resource_id)
        return {
            "status": "PASS" if not refused else "PARTIAL_FAIL_CLOSED",
            "stopped": stopped,
            "already_gone": already_gone,
            "refused_non_process_resources": refused,
        }


def print_closed_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


__all__ = [
    "DurableTrainingControl",
    "TrainingOperationsUnavailable",
    "file_sha256",
    "print_closed_json",
    "supervisor_identity",
]
