"""Private 45-item leave-one-out trajectory collection for v2 evolution."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.chembench4k_prompt import build_dev_leave_one_out_tasks
from openevo_chembench.core_evolution_v2 import CoreDevTrajectoryV2
from openevo_chembench.frozen_runtime_v2 import FrozenAgentRequestV2
from openevo_chembench.models import RawAttempt


class DevExecutorV2(Protocol):
    def execute_frozen(self, request: FrozenAgentRequestV2) -> RawAttempt: ...


@dataclass(frozen=True, slots=True)
class DevTrajectoryCollectionV2:
    private_records_path: Path
    public_state_path: Path
    record_count: int
    records_sha256: str


def collect_dev_loo_trajectories_v2(
    *,
    loader: ChemBench4KDatasetLoader,
    executor: DevExecutorV2,
    output_root: Path,
) -> DevTrajectoryCollectionV2:
    """Make exactly 45 dev calls and persist target-bearing records privately."""

    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be exact ChemBench4KDatasetLoader")
    if not callable(getattr(executor, "execute_frozen", None)):
        raise TypeError("executor must implement execute_frozen")
    if output_root.exists():
        raise RuntimeError("dev trajectory output already exists")
    output_root.mkdir(parents=True, mode=0o700)
    private_root = output_root / "private"
    public_root = output_root / "public"
    private_root.mkdir(mode=0o700)
    public_root.mkdir(mode=0o755)
    private_path = private_root / "private_dev_trajectories.jsonl"
    public_state_path = public_root / "run_state.json"
    _create_file(private_path, 0o600)
    evaluator = ChemBench4KPrivateEvaluator()
    count = 0

    for category in CHEMBENCH4K_CATEGORIES:
        dev = loader.load_category(category, split="dev")
        for loo in build_dev_leave_one_out_tasks(dev):
            attempt = executor.execute_frozen(
                FrozenAgentRequestV2(rendered_public_prompt=loo.prompt.text)
            )
            if type(attempt) is not RawAttempt:
                raise TypeError("dev executor must return exact RawAttempt")
            evaluation = evaluator.evaluate(
                task=loo.evaluation_task,
                raw_completion=attempt.response,
            )
            taxonomy: list[str] = []
            if not evaluation.official.prediction:
                taxonomy.append("format_failure")
            elif not evaluation.correct:
                taxonomy.append("incorrect_selection")
            if evaluation.strict.prediction is None:
                taxonomy.append("strict_format_mismatch")
            record = CoreDevTrajectoryV2(
                uid=loo.evaluation_task.uid,
                category=category,
                source_index=loo.evaluation_task.source_index,
                public_prompt_sha256=hashlib.sha256(loo.prompt.text.encode("utf-8")).hexdigest(),
                raw_completion=evaluation.raw_completion,
                parsed_prediction=evaluation.official.prediction,
                target=evaluation.target,
                correct=evaluation.correct,
                private_error_taxonomy=tuple(taxonomy),
                dataset_revision=loo.evaluation_task.dataset_revision,
                dataset_sha256=loo.evaluation_task.dataset_sha256,
            )
            _append_private_record(private_path, record)
            count += 1
            _write_public_state(
                public_state_path,
                status="RUNNING",
                completed=count,
                dataset_sha256=loader.manifest.combined_sha256,
            )
    if count != 45:
        raise RuntimeError("dev leave-one-out trajectory count is not 45")
    _write_public_state(
        public_state_path,
        status="COMPLETED",
        completed=count,
        dataset_sha256=loader.manifest.combined_sha256,
    )
    return DevTrajectoryCollectionV2(
        private_records_path=private_path,
        public_state_path=public_state_path,
        record_count=count,
        records_sha256=_sha256_file(private_path),
    )


def load_private_dev_trajectories_v2(path: Path) -> tuple[CoreDevTrajectoryV2, ...]:
    """Load an exact 45-record private evolution input."""

    try:
        if path.stat().st_mode & 0o077:
            raise RuntimeError("private dev trajectory permissions are unsafe")
        lines = path.read_text(encoding="utf-8").splitlines()
        records = tuple(CoreDevTrajectoryV2.model_validate_json(line) for line in lines)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("private dev trajectories are unavailable or invalid") from exc
    if len(records) != 45 or len({record.uid for record in records}) != 45:
        raise RuntimeError("private dev trajectory set must contain 45 unique records")
    if any(record.source_split != "dev" for record in records):
        raise RuntimeError("private evolution input contains a non-dev record")
    return records


def _create_file(path: Path, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.close(descriptor)
    path.chmod(mode)


def _append_private_record(path: Path, record: CoreDevTrajectoryV2) -> None:
    encoded = (record.model_dump_json(exclude_none=False, by_alias=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _write_public_state(
    path: Path,
    *,
    status: str,
    completed: int,
    dataset_sha256: str,
) -> None:
    payload = {
        "schema_version": "chembench4k_dev_loo_collection_state_v2",
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "status": status,
        "planned": 45,
        "completed": completed,
        "dataset_sha256": dataset_sha256,
        "model_calls": completed,
        "contains_test_input": False,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "DevExecutorV2",
    "DevTrajectoryCollectionV2",
    "collect_dev_loo_trajectories_v2",
    "load_private_dev_trajectories_v2",
]
