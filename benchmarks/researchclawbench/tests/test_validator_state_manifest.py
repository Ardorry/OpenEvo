from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from openevo_researchclawbench.artifact_validator import validate_workspace
from openevo_researchclawbench.evolution_loop import (
    AttemptRecord,
    EvolutionState,
    ReflectionProposal,
    SequentialEvolutionStateMachine,
    select_best_attempt,
)
from openevo_researchclawbench.run_manifest import ManifestError, validate_attempt_manifest


def _png(path: Path) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"\x00\xff\x00\x00"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _valid_workspace(root: Path) -> None:
    for name in ("code", "outputs", "report/images"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "code" / "analysis.py").write_text("print(42)\n", encoding="utf-8")
    (root / "outputs" / "metrics.txt").write_text("validated_metric=42\n", encoding="utf-8")
    words = " ".join(["evidence"] * 260)
    (root / "report" / "report.md").write_text(
        f"# Method\nReproducible method {words}\n# Results\nThe validated metric is 42. ![plot](images/plot.png)\n# Discussion\nEvidence is traced to outputs.\n",
        encoding="utf-8",
    )
    _png(root / "report" / "images" / "plot.png")
    (root / "_meta.json").write_text(json.dumps({"status": "completed", "exit_code": 0}), encoding="utf-8")


def test_validator_passes_complete_artifact_and_hash_is_stable(tmp_path: Path) -> None:
    _valid_workspace(tmp_path)
    first = validate_workspace(tmp_path)
    second = validate_workspace(tmp_path)
    assert first.passed and second.passed
    assert first.artifact_root_sha256 == second.artifact_root_sha256


def test_validator_fails_closed_for_symlink_absolute_path_and_timeout(tmp_path: Path) -> None:
    _valid_workspace(tmp_path)
    (tmp_path / "report" / "report.md").write_text("![x](/etc/passwd) TODO", encoding="utf-8")
    (tmp_path / "code" / "escape").symlink_to("/etc/passwd")
    result = validate_workspace(tmp_path, timed_out=True)
    assert not result.passed
    assert "UNSAFE_FILESYSTEM_ENTRY" in result.errors
    assert "ABSOLUTE_PATH_REFERENCE" in result.errors
    assert "TIMEOUT" in result.errors


def _record(index: int, score: float, runtime: int, size: int = 100) -> AttemptRecord:
    return AttemptRecord(
        task_id="Astronomy_004", attempt_index=index, input_composite_revision=f"c00{index}",
        agent_system_revision=f"as00{index}", text_memory_revision=f"tm00{index}",
        skill_bundle_revision=f"sk00{index}", completed=True, artifact_valid=True,
        score=score, runtime_seconds=runtime, cost_total_usd=0.0,
        output_artifact_hash="a" * 64, reflector_round=index if index < 2 else None,
        proposed_composite=f"c00{index + 1}" if index < 2 else None,
        admission_status="accepted", validator_completeness=10, composite_size_bytes=size,
    )


def _proposal(attempt: int) -> ReflectionProposal:
    return ReflectionProposal.from_dict({
        "source_task_id": "Astronomy_004", "source_attempt_id": f"Astronomy_004_a{attempt}",
        "parent_composite_revision": f"c00{attempt}", "observed_score": 20.0,
        "best_score_on_current_task": 20.0,
        "agent_system": {"action": "keep", "reason": "No safe general update", "proposed_revision": f"as00{attempt}"},
        "text_memory": {"action": "update", "reason": "General debugging evidence", "proposed_revision": f"tm00{attempt + 1}"},
        "skill_bundle": {"action": "keep", "reason": "No repeated operation", "add": [], "modify": [], "remove": [], "proposed_revision": f"sk00{attempt}"},
        "expected_generalization": "More reliable debugging across tasks",
        "task_specific_content_removed": [], "admission_required": True,
    })


def test_state_machine_runs_three_attempts_two_joint_reflections_and_selects_best() -> None:
    machine = SequentialEvolutionStateMachine()
    machine.record_attempt(_record(0, 20, 120))
    assert machine.state is EvolutionState.REFLECTION_PENDING
    machine.record_reflection(_proposal(0))
    machine.record_admission(True)
    machine.record_attempt(_record(1, 25, 130))
    machine.record_reflection(_proposal(1))
    machine.record_admission(True)
    machine.record_attempt(_record(2, 23, 100))
    best = machine.select_task_best()
    assert best.attempt_index == 1
    machine.record_sanitization(True)
    assert machine.current_task == "Chemistry_004"
    assert machine.state is EvolutionState.READY


def test_best_selection_does_not_force_monotonicity() -> None:
    best = select_best_attempt([_record(0, 40, 100), _record(1, 35, 90), _record(2, 37, 80)])
    assert best.attempt_index == 0


def test_two_consecutive_admission_rejections_stop() -> None:
    machine = SequentialEvolutionStateMachine()
    machine.record_attempt(_record(0, 20, 120))
    machine.record_reflection(_proposal(0))
    machine.record_admission(False)
    machine.record_attempt(_record(1, 20, 120))
    machine.record_reflection(_proposal(1))
    machine.record_admission(False)
    assert machine.state is EvolutionState.STOPPED


def test_attempt_manifest_completeness() -> None:
    payload = {
        "experiment_id": "sequential_task_reflector_evolution_v0", "task_id": "Life_005",
        "attempt_id": "Life_005_a0", "attempt_index": 0, "input_composite_revision": "c000",
        "agent_system_revision": "as000", "text_memory_revision": "tm000", "skill_bundle_revision": "sk000",
        "protocol_sha256": "a" * 64, "researchclawbench_commit": "b" * 40, "openevo_commit": "c" * 40,
        "adapter_diff_sha256": "d" * 64, "candidate_image_id": "sha256:" + "e" * 64,
        "candidate_model": "model", "reasoning_level": "high", "network": "disabled",
        "started_at": "x", "finished_at": "y", "completed": True, "exit_code": 0,
        "runtime_seconds": 10, "cost_total_usd": 0.1, "artifact_valid": True,
        "output_artifact_hash": "f" * 64, "feedback_sha256": "0" * 64, "transfer_only": False,
        "rollout_task_id": "rollout-Life_005_a0", "native_session_id": "session-1",
        "task_request_sha256": "1" * 64, "workspace_result_manifest_sha256": "2" * 64,
        "input_artifact_ids": [],
    }
    validate_attempt_manifest(payload)
    payload.pop("feedback_sha256")
    with pytest.raises(ManifestError):
        validate_attempt_manifest(payload)
