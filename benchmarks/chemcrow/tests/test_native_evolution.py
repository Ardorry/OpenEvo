from __future__ import annotations

from pathlib import Path

from openevo.evolution.models import ArtifactRegisterRequest, ArtifactType

from openevo_chemcrow.models import ArtifactKind, Trajectory
from openevo_chemcrow.native_evolution import NativeEvolutionEngine


def test_native_core_event_dataset_job_and_artifact(monkeypatch, tmp_path, task_item):
    calls = 0

    def fake_run_method(job, *, artifact_root):
        nonlocal calls
        calls += 1
        output = Path(artifact_root) / "workers" / job.job_id / "text_memory" / "memory.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# Task-local memory\nValidate tool evidence.\n", encoding="utf-8")
        return [
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name="test task memory",
                uri=output.resolve().as_uri(),
                manifest={"content_path": "memory.md"},
                lineage={"input_artifact_ids": [job.input_artifacts[0].artifact_id]},
                compatibility={"task_tags": [task_item.task_id]},
                tags=["task-local"],
            )
        ]

    monkeypatch.setattr("openevo_chemcrow.native_evolution.run_method", fake_run_method)
    engine = NativeEvolutionEngine(
        run_root=tmp_path,
        artifact_kind=ArtifactKind.TEXT_MEMORY,
        reflector_config={"provider": "mock", "model": "no-call"},
    )
    baseline = Trajectory(
        run_id="baseline-run",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="baseline answer",
        candidate_config_sha256="s0",
    )
    receipt = engine.evolve(
        task=task_item,
        baseline=baseline,
        feedback_payload={"mode": "F0", "observable_trajectory": baseline.model_dump(mode="json")},
        pair_id="pair-native",
    )
    assert calls == 1
    assert receipt.task_id == task_item.task_id
    assert receipt.artifact_type == ArtifactKind.TEXT_MEMORY
    assert receipt.size_bytes > 0
    assert (tmp_path / "pair-native" / "evolution" / "core.sqlite3").is_file()
