from __future__ import annotations

from openevo_chemcrow.models import ArtifactKind, Trajectory
from openevo_chemcrow.native_evolution import NativeEvolutionEngine


class FakeCoreReflectorRollout:
    config_sha256 = "reflector-s0"

    def __init__(self):
        self.candidate = {"agent": {"model_name": "gpt-5.5"}}
        self.calls = []

    def run_candidate(self, *, task, role, artifact_ids, pair_id, mcp_url):
        self.calls.append((task.task_id, role, artifact_ids, pair_id, mcp_url))
        return Trajectory(
            run_id="core-reflector-run",
            task_id=task.task_id,
            role="baseline",
            status="COMPLETED",
            answer="# Task-local memory\nValidate tool evidence.",
            candidate_config_sha256=self.config_sha256,
        )


def test_native_core_event_dataset_job_and_core_managed_reflector(tmp_path, task_item):
    reflector = FakeCoreReflectorRollout()

    engine = NativeEvolutionEngine(
        run_root=tmp_path,
        artifact_kind=ArtifactKind.TEXT_MEMORY,
        reflector_rollout=reflector,
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
    assert len(reflector.calls) == 1
    assert reflector.calls[0][1:] == ("baseline", [], "pair-native-reflector-core", None)
    assert receipt.task_id == task_item.task_id
    assert receipt.artifact_type == ArtifactKind.TEXT_MEMORY
    assert receipt.reflector_run_id == "core-reflector-run"
    assert receipt.size_bytes > 0
    assert (tmp_path / "pair-native" / "evolution" / "core.sqlite3").is_file()


def test_native_evolution_has_no_host_codex_or_run_method_path():
    import inspect

    import openevo_chemcrow.native_evolution as module

    source = inspect.getsource(module)
    assert "run_method" not in source
    assert "subprocess" not in source
    assert "codex exec" not in source
