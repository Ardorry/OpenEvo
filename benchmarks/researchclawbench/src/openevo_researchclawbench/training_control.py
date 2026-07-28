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
from pathlib import Path
import re
from typing import Any

from .config import ExperimentConfig
from .owned_resource_registry import OwnedResourceRegistry
from .training_state_store import TrainingStateStore
from .training_supervisor import (
    CommunityTrainingSupervisor,
    SupervisorIdentity,
    TrainingOperations,
)


_RUN_ID = re.compile(r"rcb_oe_v0_[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


class TrainingOperationsUnavailable(RuntimeError):
    """No production Core/evaluator operations binding was supplied."""


class _InspectionOnlyOperations:
    def __getattr__(self, name: str):
        if name.startswith(("ensure_", "validate_", "evaluate", "attach_", "evolve_", "admit_", "destroy_", "sanitize_", "freeze_")):
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
        self.supervisor = CommunityTrainingSupervisor(
            store=self.store,
            experiment_id=run_id,
            identity=supervisor_identity(config),
            operations=operations or _InspectionOnlyOperations(),
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
