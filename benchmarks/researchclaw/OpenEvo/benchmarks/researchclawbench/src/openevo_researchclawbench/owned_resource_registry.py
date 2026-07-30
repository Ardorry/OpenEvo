"""Exact resource ownership checks; never discovers or kills by process name."""

from __future__ import annotations

import os
from pathlib import Path
import signal
from typing import Callable

from .training_state_store import TrainingStateStore


def process_start_ticks(pid: int) -> int:
    if type(pid) is not int or pid <= 1:
        raise ValueError("owned PID is invalid")
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    if len(fields) < 22:
        raise ValueError("owned PID stat is incomplete")
    return int(fields[21])


class OwnedResourceRegistry:
    def __init__(self, store: TrainingStateStore, experiment_id: str) -> None:
        self.store = store
        self.experiment_id = experiment_id

    def register_process(self, *, pid: int, process_group: int) -> str:
        if os.getpgid(pid) != process_group:
            raise ValueError("owned process group does not match the PID")
        resource_id = f"process-{pid}-{process_start_ticks(pid)}"
        self.store.register_resource(
            experiment_id=self.experiment_id,
            resource_id=resource_id,
            kind="process",
            identity={
                "pid": pid,
                "process_group": process_group,
                "start_ticks": process_start_ticks(pid),
            },
        )
        return resource_id

    def register_opaque(self, *, kind: str, resource_id: str, identity: dict) -> None:
        if kind not in {"container", "openevo_session", "runtime", "worker_job"}:
            raise ValueError("unsupported owned resource kind")
        self.store.register_resource(
            experiment_id=self.experiment_id,
            resource_id=resource_id,
            kind=kind,
            identity=identity,
        )

    def stop_process(self, resource_id: str) -> bool:
        matches = [
            item
            for item in self.store.active_resources(self.experiment_id)
            if item["resource_id"] == resource_id and item["kind"] == "process"
        ]
        if len(matches) != 1:
            raise PermissionError("process is not an active owned resource")
        identity = matches[0]["identity"]
        pid = identity["pid"]
        pgid = identity["process_group"]
        try:
            if (
                process_start_ticks(pid) != identity["start_ticks"]
                or os.getpgid(pid) != pgid
            ):
                raise PermissionError("owned process identity changed")
        except FileNotFoundError:
            self.store.release_resource(resource_id)
            return False
        os.killpg(pgid, signal.SIGTERM)
        self.store.release_resource(resource_id)
        return True

    def stop_opaque(
        self,
        resource_id: str,
        *,
        stopper: Callable[[str, dict], None],
    ) -> None:
        matches = [
            item
            for item in self.store.active_resources(self.experiment_id)
            if item["resource_id"] == resource_id and item["kind"] != "process"
        ]
        if len(matches) != 1:
            raise PermissionError("resource is not an active owned resource")
        item = matches[0]
        stopper(item["kind"], item["identity"])
        self.store.release_resource(resource_id)


__all__ = ["OwnedResourceRegistry", "process_start_ticks"]
