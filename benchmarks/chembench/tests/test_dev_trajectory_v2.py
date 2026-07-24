from __future__ import annotations

import json
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.dev_trajectory_v2 import (
    collect_dev_loo_trajectories_v2,
    load_private_dev_trajectories_v2,
)
from openevo_chembench.frozen_runtime_v2 import FrozenAgentRequestV2
from openevo_chembench.models import RawAttempt, TranscriptReference


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT
    / "data"
    / "chembench4k"
    / "AI4Chem_ChemBench4K"
    / CHEMBENCH4K_REVISION
)


class _DummyExecutor:
    def __init__(self) -> None:
        self.requests: list[FrozenAgentRequestV2] = []

    def execute_frozen(self, request: FrozenAgentRequestV2) -> RawAttempt:
        self.requests.append(request)
        return RawAttempt(
            response="A",
            transcript_reference=TranscriptReference(
                reference=f"private-dev-transcript-{len(self.requests)}"
            ),
        )


def test_dev_collection_is_exactly_45_dev_only_calls(tmp_path: Path) -> None:
    loader = ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)
    executor = _DummyExecutor()
    collection = collect_dev_loo_trajectories_v2(
        loader=loader,
        executor=executor,
        output_root=tmp_path / "dev_loo",
    )
    records = load_private_dev_trajectories_v2(collection.private_records_path)
    state = json.loads(collection.public_state_path.read_text(encoding="utf-8"))

    assert collection.record_count == len(records) == len(executor.requests) == 45
    assert all(record.source_split == "dev" for record in records)
    assert {record.uid for record in records} == {
        task.uid for task in loader.load_split("dev")
    }
    assert {record.uid for record in records}.isdisjoint(
        task.uid for task in loader.load_split("test")
    )
    assert collection.private_records_path.stat().st_mode & 0o077 == 0
    assert state["contains_test_input"] is False
    assert "target" not in state


def test_dev_prompts_do_not_self_demonstrate(tmp_path: Path) -> None:
    loader = ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)
    executor = _DummyExecutor()
    collect_dev_loo_trajectories_v2(
        loader=loader,
        executor=executor,
        output_root=tmp_path / "dev_loo",
    )
    for request in executor.requests:
        # Four answered examples plus the unanswered evaluation item.
        assert request.rendered_public_prompt.count("Answer: ") == 4
        assert request.rendered_public_prompt.endswith("Answer:")
        assert request.resolved_text_memory is None
