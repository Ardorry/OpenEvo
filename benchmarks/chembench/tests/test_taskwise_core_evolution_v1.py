from __future__ import annotations

import errno
import hashlib
import importlib.metadata as importlib_metadata
import json
from pathlib import Path
import stat
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from openevo import __version__
import openevo.evolution.methods as core_methods
from openevo.evolution.framework import DistributionArtifactExpectation, canonical_digest
from openevo.evolution.framework import builtins as core_builtins
from openevo.evolution.framework.builtins import load_verified_builtin_registry
from openevo.evolution.framework.loading import _verify_distribution_install

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.frozen_runtime_v2 import CoreResolvedTextMemoryV2
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.taskwise_core_evolution_v1 import (
    METHOD_ID,
    PROTOCOL_ID,
    TaskwiseArtifactLineageReceiptV1,
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreEvolutionError,
    TaskwiseCoreFailureReceiptV1,
    TaskwiseCorePredecessorV1,
    TaskwiseCoreUpdateRequestV1,
    TaskwiseCoreUpdatePortAdapterV1,
    inspect_taskwise_text_memory_v1,
)
from openevo_chembench.taskwise_config_v1 import TASKWISE_MEMORY_LIMITS_V1
from openevo_chembench.taskwise_feedback_v1 import (
    TaskwiseSafeEvolutionSignalV1,
    TaskwiseSafeSignalCodeV1,
)
from openevo_chembench.taskwise_trajectory_v1 import (
    TaskwiseTrajectoryV1,
    ordered_safe_feedback_digest,
    ordered_taskwise_trajectory_digest,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    CoreMemoryReferenceV1,
    TaskwiseCoreUpdateOutcomeV1,
    TaskwiseRunnerCoreUpdateRequestV1,
)


class _SourceDistribution:
    metadata = {"Name": "openevo"}
    version = __version__

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, _filename: str) -> None:
        return None


def _verified_registry(temp_root: Path):
    temp_root.mkdir(parents=True, exist_ok=True)
    install_root = Path(core_builtins.__file__).resolve().parents[3]
    artifact = temp_root / f"openevo-{__version__}-py3-none-any.whl"
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith(
                (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")
            ):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            f"openevo-{__version__}.dist-info/METADATA",
            f"Name: openevo\nVersion: {__version__}\n",
        )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    verified = _verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=__version__,
            distribution_digest=digest,
        ),
        artifact,
        metadata_provider=lambda _name: _SourceDistribution(install_root),
    )
    return load_verified_builtin_registry(verified)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _trajectory(
    *,
    task_index: int,
    round_index: int,
    correct: bool,
) -> TaskwiseTrajectoryV1:
    task_uid = _sha(f"task-{task_index}")
    prompt = RenderedChemBench4KPrompt(
        uid=task_uid,
        category="Name_Conversion",
        dataset_revision=CHEMBENCH4K_REVISION,
        demonstration_uids=tuple(_sha(f"demo-{index}") for index in range(5)),
        text=(
            "There is a single choice question about chemistry.\n"
            f"Question: private-public-question-{task_index}\n"
            "A. private-option-alpha\n"
            "B. private-option-beta\n"
            "C. private-option-gamma\n"
            "D. private-option-delta\n"
            "Answer:"
        ),
    )
    signal = TaskwiseSafeEvolutionSignalV1(
        codes=(
            TaskwiseSafeSignalCodeV1.CORRECT if correct else TaskwiseSafeSignalCodeV1.INCORRECT,
            TaskwiseSafeSignalCodeV1.FORMAT_VIOLATION,
        )
    )
    return TaskwiseTrajectoryV1.from_attempt(
        task_uid=task_uid,
        task_index=task_index,
        category="Name_Conversion",
        round_index=round_index,
        session_id=f"taskwise-session-{task_index}-{round_index}",
        prompt=prompt,
        attempt=RawAttempt(
            response="A" if correct else "B",
            transcript_reference=TranscriptReference(
                f"private-transcript-{task_index}-{round_index}"
            ),
        ),
        safe_feedback=signal,
        dataset_sha256=_sha("dataset"),
    )


def _request(
    *,
    task_index: int,
    update_index: int,
    predecessor: TaskwiseCorePredecessorV1 | None,
) -> TaskwiseCoreUpdateRequestV1:
    trajectories = tuple(
        _trajectory(
            task_index=task_index,
            round_index=round_index,
            correct=round_index == 1,
        )
        for round_index in range(update_index)
    )
    return TaskwiseCoreUpdateRequestV1(
        task_uid=_sha(f"task-{task_index}"),
        task_index=task_index,
        round_index=update_index - 1,
        update_index=update_index,
        trajectories=trajectories,
        predecessor=predecessor,
        validator_forbidden_literals=(
            f"private-public-question-{task_index}",
            "private-option-alpha",
            "private-option-beta",
            _sha(f"task-{task_index}"),
        ),
    )


def _memory(version: int) -> str:
    return f"""# General Chemistry Memory

## Do
- Classify the reasoning mode before comparing choices, revision {version}.

## Avoid
- Avoid committing before an independent consistency check.

## Validate
- Verify units, conservation, structures, and output formatting.

## When Applicable
- Apply dimensional checks to numerical reasoning.

## Retired Or Superseded
- Retire advice only when a general validation rule supersedes it.
"""


def _memory_with_do_items(items: tuple[str, ...]) -> str:
    do_section = "\n".join(f"- {item}" for item in items)
    return f"""# General Chemistry Memory

## Do
{do_section}

## Avoid
- Avoid committing before an independent consistency check.

## Validate
- Verify units, conservation, structures, and output formatting.

## When Applicable
- Apply dimensional checks to numerical reasoning.

## Retired Or Superseded
- Retire advice only when a general validation rule supersedes it.
"""


def _load_only_core_failure_receipt(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "core" / "private_core_failure_diagnostics"
    paths = list(root.glob("taskwise_core_failure_*.json"))
    assert len(paths) == 1
    path = paths[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return path, json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def executable_registry(tmp_path: Path):
    assert importlib_metadata.version("openevo") == __version__
    return _verified_registry(tmp_path / "verified-registry")


@pytest.fixture
def bridge(tmp_path: Path, executable_registry) -> TaskwiseCoreEvolutionBridgeV1:
    instance = TaskwiseCoreEvolutionBridgeV1(
        db_path=tmp_path / "core" / "evolution.sqlite3",
        artifact_root=tmp_path / "core" / "artifacts",
        executable_registry=executable_registry,
    )
    yield instance
    instance.close()


def test_global_three_update_chain_uses_complete_task_prefixes(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reflected_prompts: list[str] = []
    memories = iter((_memory(1), _memory(2), _memory(3)))

    def _synthetic_reflector(prompt, *_args, **_kwargs):
        reflected_prompts.append(prompt)
        return next(memories)

    monkeypatch.setattr(core_methods, "_generate_reflector_markdown", _synthetic_reflector)
    task1_update1 = bridge.apply_update(
        _request(task_index=0, update_index=1, predecessor=None),
        test_only_allow_synthetic_reflector=True,
    )
    task1_update2 = bridge.apply_update(
        _request(
            task_index=0,
            update_index=2,
            predecessor=task1_update1.predecessor_identity(),
        ),
        test_only_allow_synthetic_reflector=True,
    )
    task2_update1 = bridge.apply_update(
        _request(
            task_index=1,
            update_index=1,
            predecessor=task1_update2.predecessor_identity(),
        ),
        test_only_allow_synthetic_reflector=True,
    )

    assert PROTOCOL_ID == "taskwise_online_evolution_v1"
    assert task1_update1.predecessor is None
    assert task1_update2.predecessor == task1_update1.predecessor_identity()
    assert task2_update1.predecessor == task1_update2.predecessor_identity()
    assert tuple(
        item.global_update_ordinal for item in (task1_update1, task1_update2, task2_update1)
    ) == (1, 2, 3)
    assert bridge.current_head() == task2_update1
    runtime_memory = bridge.issue_runtime_memory(task2_update1)
    assert type(runtime_memory) is CoreResolvedTextMemoryV2
    assert runtime_memory.core_artifact_id == task2_update1.core_artifact_id

    with bridge._store.connect() as connection:  # noqa: SLF001
        datasets = connection.execute(
            "SELECT event_count, trace_count FROM datasets ORDER BY rowid"
        ).fetchall()
        jobs = connection.execute(
            "SELECT state, method, input_artifact_ids_json, config_json FROM jobs ORDER BY rowid"
        ).fetchall()
        artifacts = connection.execute(
            "SELECT promoted, lineage_json, manifest_json FROM artifacts "
            "WHERE type = 'text_memory' ORDER BY version"
        ).fetchall()
        contexts = connection.execute(
            "SELECT selected_artifact_ids_json FROM contexts ORDER BY rowid"
        ).fetchall()
    assert [tuple(row) for row in datasets] == [(1, 1), (2, 2), (1, 1)]
    assert all(row["state"] == "succeeded" and row["method"] == METHOD_ID for row in jobs)
    assert all(
        json.loads(row["config_json"])["lineage"]["memory_limits"]
        == TASKWISE_MEMORY_LIMITS_V1.to_payload()
        for row in jobs
    )
    assert all(
        json.loads(row["config_json"])["lineage"]["memory_limits_sha256"]
        == TASKWISE_MEMORY_LIMITS_V1.digest
        for row in jobs
    )
    assert all(row["promoted"] == 1 for row in artifacts)
    assert len(contexts) == 3

    first_inputs = json.loads(jobs[0]["input_artifact_ids_json"])
    second_inputs = json.loads(jobs[1]["input_artifact_ids_json"])
    third_inputs = json.loads(jobs[2]["input_artifact_ids_json"])
    assert first_inputs == [task1_update1.dataset_artifact_id]
    assert second_inputs == [
        task1_update2.dataset_artifact_id,
        task1_update1.core_artifact_id,
    ]
    assert third_inputs == [
        task2_update1.dataset_artifact_id,
        task1_update2.core_artifact_id,
    ]
    assert [json.loads(row["manifest_json"])["record_count"] for row in artifacts] == [1, 2, 1]
    assert [json.loads(row["manifest_json"])["reflected_record_count"] for row in artifacts] == [
        1,
        2,
        1,
    ]
    assert [json.loads(row["manifest_json"])["prior_memory_count"] for row in artifacts] == [
        0,
        1,
        1,
    ]
    third_lineage = json.loads(artifacts[2]["lineage_json"])
    assert third_lineage["predecessor_artifact_id"] == task1_update2.core_artifact_id
    assert third_lineage["predecessor_memory_sha256"] == task1_update2.resolved_memory_sha256
    assert task2_update1.task_uid in third_lineage.values()
    assert task2_update1.task_uid not in task2_update1.resolved_memory

    assert "incorrect" in reflected_prompts[0]
    assert "incorrect" in reflected_prompts[1]
    assert "correct" in reflected_prompts[1]
    assert _memory(1).strip() in reflected_prompts[1]
    assert _memory(2).strip() in reflected_prompts[2]
    for result, expected_count in (
        (task1_update1, 1),
        (task1_update2, 2),
        (task2_update1, 1),
    ):
        assert result.configured_max_records == expected_count
        assert result.records_visible_to_reflector == expected_count
        assert len(result.trajectory_ids) == expected_count

    required_lineage = task2_update1.required_lineage_receipt()
    assert type(required_lineage) is TaskwiseArtifactLineageReceiptV1
    assert set(required_lineage.model_dump(mode="json")) == {
        "schema_version",
        "protocol_id",
        "task_uid",
        "task_index",
        "round_index",
        "predecessor_artifact_id",
        "predecessor_memory_sha256",
        "trajectory_ids",
        "trajectory_digest",
        "safe_feedback_digest",
        "memory_limits_sha256",
        "memory_inspection_sha256",
        "token_estimator_id",
        "utf8_byte_count",
        "estimated_token_count",
        "section_item_counts",
        "evolution_plan_id",
        "evolution_job_id",
        "core_artifact_id",
        "artifact_payload_sha256",
        "context_resolution_digest",
    }
    assert required_lineage.predecessor_artifact_id == task1_update2.core_artifact_id
    assert required_lineage.predecessor_memory_sha256 == task1_update2.resolved_memory_sha256
    assert required_lineage.evolution_plan_id == task2_update1.plan_id
    assert required_lineage.evolution_job_id == task2_update1.job_id
    assert required_lineage.memory_limits_sha256 == TASKWISE_MEMORY_LIMITS_V1.digest
    assert required_lineage.memory_inspection_sha256 == (task2_update1.memory_inspection.digest)
    assert required_lineage.section_item_counts == (1, 1, 1, 1, 1)
    assert len(required_lineage.digest) == 64


def test_shared_ordered_digests_bind_every_prefix_record() -> None:
    first = _trajectory(task_index=0, round_index=0, correct=False)
    second = _trajectory(task_index=0, round_index=1, correct=True)
    prefix = (first, second)
    assert ordered_taskwise_trajectory_digest(prefix) == (
        ordered_taskwise_trajectory_digest(prefix)
    )
    assert ordered_safe_feedback_digest(prefix) == ordered_safe_feedback_digest(prefix)
    assert ordered_taskwise_trajectory_digest(prefix) != (
        ordered_taskwise_trajectory_digest((first,))
    )


def test_one_hundred_synthetic_updates_remain_within_memory_policy(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = 0

    def _synthetic_reflector(*_args, **_kwargs):
        nonlocal version
        version += 1
        return _memory(version)

    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        _synthetic_reflector,
    )
    predecessor = None
    results = []
    for task_index in range(50):
        for update_index in (1, 2):
            result = bridge.apply_update(
                _request(
                    task_index=task_index,
                    update_index=update_index,
                    predecessor=predecessor,
                ),
                test_only_allow_synthetic_reflector=True,
            )
            results.append(result)
            predecessor = result.predecessor_identity()

    assert len(results) == 100
    assert [item.global_update_ordinal for item in results] == list(range(1, 101))
    assert all(item.memory_limits_sha256 == TASKWISE_MEMORY_LIMITS_V1.digest for item in results)
    assert all(item.memory_inspection.passed for item in results)
    assert all(
        item.memory_inspection.utf8_byte_count <= TASKWISE_MEMORY_LIMITS_V1.max_utf8_bytes
        for item in results
    )
    assert all(
        item.memory_inspection.estimated_token_count
        <= TASKWISE_MEMORY_LIMITS_V1.max_estimated_tokens
        for item in results
    )
    assert all(
        item.memory_inspection.max_section_items <= TASKWISE_MEMORY_LIMITS_V1.max_items_per_section
        for item in results
    )
    assert bridge.current_head() == results[-1]
    checkpoint = bridge._checkpoint_path  # noqa: SLF001
    assert len(checkpoint.read_text(encoding="utf-8").splitlines()) == 100
    with bridge._store.connect() as connection:  # noqa: SLF001
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE method = ?",
                (METHOD_ID,),
            ).fetchone()[0]
            == 100
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE type = 'text_memory' AND promoted = 1"
            ).fetchone()[0]
            == 100
        )
        assert connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0] == 100


def test_memory_parser_is_deterministic_and_enforces_every_frozen_limit() -> None:
    valid_payload = _memory(1).encode("utf-8")
    first = inspect_taskwise_text_memory_v1(valid_payload)
    second = inspect_taskwise_text_memory_v1(valid_payload)

    assert first == second
    assert first.digest == second.digest
    assert first.passed
    assert first.memory_limits_sha256 == TASKWISE_MEMORY_LIMITS_V1.digest
    assert first.token_estimator_id == "ascii_word_or_unicode_codepoint_v1"
    assert first.parser_id == "exact_markdown_sections_v1"
    assert first.section_item_counts == (1, 1, 1, 1, 1)
    assert valid_payload == _memory(1).encode("utf-8")

    too_many_items = _memory_with_do_items(
        tuple(f"General rule number {index}." for index in range(25))
    ).encode("utf-8")
    assert "memory_section_items_exceeded" in (
        inspect_taskwise_text_memory_v1(too_many_items).finding_codes
    )

    too_many_tokens = _memory_with_do_items(("化" * 4097,)).encode("utf-8")
    token_inspection = inspect_taskwise_text_memory_v1(too_many_tokens)
    assert token_inspection.utf8_byte_count <= TASKWISE_MEMORY_LIMITS_V1.max_utf8_bytes
    assert "memory_tokens_exceeded" in token_inspection.finding_codes

    too_many_bytes = _memory_with_do_items(("x" * 17000,)).encode("utf-8")
    byte_inspection = inspect_taskwise_text_memory_v1(too_many_bytes)
    assert "memory_bytes_exceeded" in byte_inspection.finding_codes

    duplicate = _memory_with_do_items(
        (
            "Verify units!",
            "verify units",
        )
    ).encode("utf-8")
    assert "memory_duplicate_rule" in (inspect_taskwise_text_memory_v1(duplicate).finding_codes)

    reordered = _memory(1).replace("## Do", "## TEMP", 1)
    reordered = reordered.replace("## Validate", "## Do", 1)
    reordered = reordered.replace("## TEMP", "## Validate", 1)
    assert "memory_required_sections_invalid" in (
        inspect_taskwise_text_memory_v1(reordered.encode("utf-8")).finding_codes
    )


def test_runner_port_returns_typed_outcome_and_resolves_exact_reference(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memories = iter((_memory(1), _memory(2)))
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: next(memories),
    )
    port = TaskwiseCoreUpdatePortAdapterV1(
        bridge,
        test_only_allow_synthetic_reflector=True,
    )
    first_trajectory = _trajectory(task_index=0, round_index=0, correct=False)
    first = port.update_text_memory(
        TaskwiseRunnerCoreUpdateRequestV1(
            task_uid=_sha("task-0"),
            task_index=0,
            episode_id="taskwise-episode-00000000",
            update_index=1,
            source_round_index=0,
            trajectories=(first_trajectory,),
            safe_signals=(first_trajectory.safe_feedback,),
            prior_resolved_text_memory=None,
        )
    )
    assert type(first) is TaskwiseCoreUpdateOutcomeV1
    assert first.memory_metrics.memory_limits_sha256 == TASKWISE_MEMORY_LIMITS_V1.digest
    assert first.memory_metrics.section_item_counts == (1, 1, 1, 1, 1)
    assert first.memory_metrics.utf8_byte_count == len(_memory(1).encode("utf-8"))
    reference = CoreMemoryReferenceV1.from_memory(first.resolved_text_memory)
    assert port.resolve_text_memory(reference) == first.resolved_text_memory
    second_trajectory = _trajectory(task_index=0, round_index=1, correct=True)
    second = port.update_text_memory(
        TaskwiseRunnerCoreUpdateRequestV1(
            task_uid=_sha("task-0"),
            task_index=0,
            episode_id="taskwise-episode-00000000",
            update_index=2,
            source_round_index=1,
            trajectories=(first_trajectory, second_trajectory),
            safe_signals=(
                first_trajectory.safe_feedback,
                second_trajectory.safe_feedback,
            ),
            prior_resolved_text_memory=first.resolved_text_memory,
        )
    )
    assert type(second) is TaskwiseCoreUpdateOutcomeV1
    assert second.resolved_text_memory.core_artifact_id != (
        first.resolved_text_memory.core_artifact_id
    )
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_RUNTIME_MEMORY_REFERENCE_INVALID",
    ):
        port.resolve_text_memory(reference)
    forged = CoreMemoryReferenceV1(
        core_artifact_id=reference.core_artifact_id,
        artifact_payload_sha256="0" * 64,
        context_resolution_digest=reference.context_resolution_digest,
        resolved_memory_sha256=reference.resolved_memory_sha256,
    )
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_RUNTIME_MEMORY_REFERENCE_INVALID",
    ):
        port.resolve_text_memory(forged)


def test_process_exclusive_stream_lock_prevents_a_second_writer_before_core_write(
    tmp_path: Path,
    executable_registry,
) -> None:
    db_path = tmp_path / "locked-core" / "evolution.sqlite3"
    artifact_root = tmp_path / "locked-core" / "artifacts"
    first = TaskwiseCoreEvolutionBridgeV1(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=executable_registry,
    )
    lock_path = db_path.with_name("taskwise_core_stream_v1.lock")
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_STREAM_LOCK_HELD"):
        TaskwiseCoreEvolutionBridgeV1(
            db_path=db_path,
            artifact_root=artifact_root,
            executable_registry=executable_registry,
        )

    with first._store.connect() as connection:  # noqa: SLF001
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    first.close()

    reopened = TaskwiseCoreEvolutionBridgeV1(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=executable_registry,
    )
    assert reopened.current_head() is None
    reopened.close()


def test_private_checkpoint_restores_global_head_and_continues_without_fork(
    tmp_path: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memories = iter((_memory(1), _memory(2), _memory(3)))
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: next(memories),
    )
    db_path = tmp_path / "restart-core" / "evolution.sqlite3"
    artifact_root = tmp_path / "restart-core" / "artifacts"
    first_bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=executable_registry,
    )
    first = first_bridge.apply_update(
        _request(task_index=0, update_index=1, predecessor=None),
        test_only_allow_synthetic_reflector=True,
    )
    second = first_bridge.apply_update(
        _request(
            task_index=0,
            update_index=2,
            predecessor=first.predecessor_identity(),
        ),
        test_only_allow_synthetic_reflector=True,
    )
    checkpoint = db_path.with_name("taskwise_core_lineage_checkpoints_v1.jsonl")
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    checkpoint_rows = [
        json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()
    ]
    assert len(checkpoint_rows) == 2
    assert checkpoint_rows[0]["result"]["resolved_memory"] == _memory(1)
    first_bridge.close()

    restarted = TaskwiseCoreEvolutionBridgeV1(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=executable_registry,
    )
    assert restarted.current_head() == second
    restarted_port = TaskwiseCoreUpdatePortAdapterV1(
        restarted,
        test_only_allow_synthetic_reflector=True,
    )
    reference = CoreMemoryReferenceV1(
        core_artifact_id=second.core_artifact_id,
        artifact_payload_sha256=second.artifact_payload_sha256,
        context_resolution_digest=second.context_resolution_digest,
        resolved_memory_sha256=second.resolved_memory_sha256,
    )
    prior_memory = restarted_port.resolve_text_memory(reference)
    trajectory = _trajectory(task_index=1, round_index=0, correct=False)
    third = restarted_port.update_text_memory(
        TaskwiseRunnerCoreUpdateRequestV1(
            task_uid=_sha("task-1"),
            task_index=1,
            episode_id="taskwise-episode-00000001",
            update_index=1,
            source_round_index=0,
            trajectories=(trajectory,),
            safe_signals=(trajectory.safe_feedback,),
            prior_resolved_text_memory=prior_memory,
        )
    )

    assert third.job_state == "COMPLETED"
    assert restarted.current_head() is not None
    assert restarted.current_head().global_update_ordinal == 3
    assert len(checkpoint.read_text(encoding="utf-8").splitlines()) == 3
    restarted.close()


def test_checkpoint_cannot_remove_validator_inputs_saved_in_the_core_job(
    tmp_path: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )
    db_path = tmp_path / "validator-checkpoint" / "evolution.sqlite3"
    artifact_root = tmp_path / "validator-checkpoint" / "artifacts"
    first = TaskwiseCoreEvolutionBridgeV1(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=executable_registry,
    )
    first.apply_update(
        _request(task_index=0, update_index=1, predecessor=None),
        test_only_allow_synthetic_reflector=True,
    )
    checkpoint = db_path.with_name("taskwise_core_lineage_checkpoints_v1.jsonl")
    first.close()

    row = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(row["validator_forbidden_literals"]) > 1
    row["validator_forbidden_literals"].pop()
    checkpoint.write_text(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint.chmod(0o600)

    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_VALIDATOR_INPUT_DRIFT",
    ):
        TaskwiseCoreEvolutionBridgeV1(
            db_path=db_path,
            artifact_root=artifact_root,
            executable_registry=executable_registry,
        )


def test_real_execution_requires_boundary_before_core_write(
    bridge: TaskwiseCoreEvolutionBridgeV1,
) -> None:
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_REFLECTOR_BOUNDARY_REQUIRED",
    ):
        bridge.apply_update(_request(task_index=0, update_index=1, predecessor=None))
    with bridge._store.connect() as connection:  # noqa: SLF001
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_jump_fork_and_global_rollback_fail_before_writes(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )
    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_LINEAGE_JUMP"):
        bridge.apply_update(
            _request(task_index=1, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )
    first = bridge.apply_update(
        _request(task_index=0, update_index=1, predecessor=None),
        test_only_allow_synthetic_reflector=True,
    )
    forged = first.predecessor_identity().model_copy(update={"core_artifact_id": "art_unknown"})
    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_LINEAGE_FORK"):
        bridge.apply_update(
            _request(task_index=0, update_index=2, predecessor=forged),
            test_only_allow_synthetic_reflector=True,
        )
    second = bridge.apply_update(
        _request(
            task_index=0,
            update_index=2,
            predecessor=first.predecessor_identity(),
        ),
        test_only_allow_synthetic_reflector=True,
    )
    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_LINEAGE_ROLLBACK"):
        bridge.apply_update(
            _request(
                task_index=1,
                update_index=1,
                predecessor=first.predecessor_identity(),
            ),
            test_only_allow_synthetic_reflector=True,
        )
    assert bridge.current_head() == second


def test_leaking_candidate_is_rejected_and_never_promoted(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    leaking = _memory(1).replace(
        "Classify the reasoning mode",
        "private-public-question-0 must use private-option-alpha; classify",
    )
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: leaking,
    )
    monkeypatch.setattr(
        core_methods,
        "_guard_generic_reflector_output",
        lambda markdown, **_kwargs: (
            markdown,
            {
                "finding_count": 0,
                "redaction_count": 0,
                "remaining_finding_count": 0,
                "findings": [],
            },
        ),
    )
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_ARTIFACT_VALIDATION_FAILED",
    ) as raised:
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )
    path, receipt = _load_only_core_failure_receipt(tmp_path)
    assert raised.value.diagnostic_receipt == path.name
    assert receipt["stage"] == "ARTIFACT_VALIDATION"
    assert receipt["finding_code"] == "TASKWISE_ARTIFACT_VALIDATION_FAILED"
    assert receipt["exception_class"] == "TASKWISE_CORE_EVOLUTION_ERROR"
    assert receipt["errno"] is None
    assert receipt["job_id"]
    assert receipt["artifact_id"]
    for digest_key in (
        "trajectory_digest",
        "safe_feedback_digest",
        "validator_input_digest",
        "dataset_manifest_sha256",
        "plan_digest",
        "reflector_input_digest",
        "artifact_payload_sha256",
        "artifact_lineage_sha256",
        "memory_inspection_sha256",
        "validation_receipt_sha256",
    ):
        assert len(receipt[digest_key]) == 64
    receipt_text = path.read_text(encoding="utf-8")
    assert "private-public-question-0" not in receipt_text
    assert "private-option-alpha" not in receipt_text
    assert "correct" not in receipt_text.casefold()
    assert "target" not in receipt_text.casefold()
    with bridge._store.connect() as connection:  # noqa: SLF001
        promoted = connection.execute(
            "SELECT promoted FROM artifacts WHERE type = 'text_memory'"
        ).fetchall()
    assert promoted and all(row["promoted"] == 0 for row in promoted)
    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_STREAM_TERMINAL"):
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )


@pytest.mark.parametrize(
    "answer_map",
    (
        "The answer is B",
        "answer is b",
        "answer is a",
        "answer is a!",
        "the answer is A general placeholder",
        "answer is b variable",
        "option a",
        "choose option A",
        "SELECT d",
        "pick option c",
        "question 17 -> D",
        "item: a",
    ),
)
def test_answer_map_detector_rejects_only_explicit_abcd_mappings(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    answer_map: str,
) -> None:
    candidate = _memory(1).replace(
        "Classify the reasoning mode before comparing choices, revision 1.",
        f"Apply this prohibited synthetic mapping: {answer_map}.",
    )
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        core_methods,
        "_guard_generic_reflector_output",
        lambda markdown, **_kwargs: (
            markdown,
            {
                "finding_count": 0,
                "redaction_count": 0,
                "remaining_finding_count": 0,
                "findings": [],
            },
        ),
    )

    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_ARTIFACT_VALIDATION_FAILED",
    ):
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )


@pytest.mark.parametrize(
    "ordinary_strategy",
    (
        "The answer is a general validation rule, not an option mapping.",
        "For a substitution step, select a reagent before comparing conditions.",
        "When equations use a free variable, select x and verify its dimensions.",
        "When the answer is chlorine, verify the element identity independently.",
        "For an arbitrary label, choose option z only as a local notation.",
    ),
)
def test_non_abcd_strategy_language_is_not_an_answer_map(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    ordinary_strategy: str,
) -> None:
    candidate = _memory(1).replace(
        "Classify the reasoning mode before comparing choices, revision 1.",
        ordinary_strategy,
    )
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        core_methods,
        "_guard_generic_reflector_output",
        lambda markdown, **_kwargs: (
            markdown,
            {
                "finding_count": 0,
                "redaction_count": 0,
                "remaining_finding_count": 0,
                "findings": [],
            },
        ),
    )

    result = bridge.apply_update(
        _request(task_index=0, update_index=1, predecessor=None),
        test_only_allow_synthetic_reflector=True,
    )

    assert result.validation_receipt.passed is True
    assert result.validation_receipt.finding_codes == ()
    assert result.core_artifact_id


def test_promotion_oserror_writes_closed_private_failure_receipt(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )

    def _promotion_failure(*_args, **_kwargs):
        raise OSError(errno.EIO, "PRIVATE_PROMOTION_BODY_SENTINEL")

    monkeypatch.setattr(bridge._store, "update_artifact_promotion", _promotion_failure)
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_ARTIFACT_PROMOTION_FAILED",
    ) as raised:
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )

    path, receipt = _load_only_core_failure_receipt(tmp_path)
    assert raised.value.diagnostic_receipt == path.name
    assert receipt["stage"] == "ARTIFACT_PROMOTION"
    assert receipt["finding_code"] == "TASKWISE_ARTIFACT_PROMOTION_FAILED"
    assert receipt["exception_class"] == "OS_ERROR"
    assert receipt["errno"] == errno.EIO
    assert len(receipt["artifact_payload_sha256"]) == 64
    assert len(receipt["artifact_lineage_sha256"]) == 64
    assert len(receipt["validation_receipt_sha256"]) == 64
    assert "PRIVATE_PROMOTION_BODY_SENTINEL" not in path.read_text(encoding="utf-8")


def test_regex_valid_unknown_core_finding_collapses_to_stage_default(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )

    def _unknown_finding(*_args, **_kwargs):
        raise TaskwiseCoreEvolutionError("MALICIOUS_BUT_REGEX_VALID")

    monkeypatch.setattr(bridge._store, "update_artifact_promotion", _unknown_finding)
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_ARTIFACT_PROMOTION_FAILED",
    ) as raised:
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )

    path, receipt = _load_only_core_failure_receipt(tmp_path)
    assert raised.value.finding_code == "TASKWISE_ARTIFACT_PROMOTION_FAILED"
    assert raised.value.diagnostic_receipt == path.name
    assert receipt["stage"] == "ARTIFACT_PROMOTION"
    assert receipt["finding_code"] == "TASKWISE_ARTIFACT_PROMOTION_FAILED"
    assert "MALICIOUS_BUT_REGEX_VALID" not in path.read_text(encoding="utf-8")


def test_context_failure_writes_closed_private_failure_receipt(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )

    def _context_failure(*_args, **_kwargs):
        raise RuntimeError("PRIVATE_CONTEXT_BODY_SENTINEL")

    monkeypatch.setattr(bridge._store, "resolve_materialized_context", _context_failure)
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_CONTEXT_RESOLUTION_FAILED",
    ) as raised:
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )

    path, receipt = _load_only_core_failure_receipt(tmp_path)
    assert raised.value.diagnostic_receipt == path.name
    assert receipt["stage"] == "CONTEXT_RESOLUTION"
    assert receipt["finding_code"] == "TASKWISE_CONTEXT_RESOLUTION_FAILED"
    assert receipt["exception_class"] == "RUNTIME_ERROR"
    assert receipt["errno"] is None
    assert len(receipt["core_artifact_manifest_sha256"]) == 64
    assert receipt["context_resolution_digest"] is None
    assert "PRIVATE_CONTEXT_BODY_SENTINEL" not in path.read_text(encoding="utf-8")


def test_checkpoint_oserror_has_digest_only_receipt_and_never_advances_head(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )

    def _checkpoint_failure(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "PRIVATE_CHECKPOINT_BODY_SENTINEL")

    monkeypatch.setattr(bridge, "_append_private_checkpoint", _checkpoint_failure)
    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_PRIVATE_CHECKPOINT_FAILED",
    ) as raised:
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )

    path, receipt = _load_only_core_failure_receipt(tmp_path)
    assert raised.value.diagnostic_receipt == path.name
    assert receipt["stage"] == "PRIVATE_CHECKPOINT"
    assert receipt["finding_code"] == "TASKWISE_PRIVATE_CHECKPOINT_FAILED"
    assert receipt["exception_class"] == "OS_ERROR"
    assert receipt["errno"] == errno.ENOSPC
    for digest_key in (
        "artifact_payload_sha256",
        "artifact_lineage_sha256",
        "memory_inspection_sha256",
        "validation_receipt_sha256",
        "core_artifact_manifest_sha256",
        "context_resolution_digest",
    ):
        assert len(receipt[digest_key]) == 64
    receipt_text = path.read_text(encoding="utf-8")
    assert "PRIVATE_CHECKPOINT_BODY_SENTINEL" not in receipt_text
    assert "General Chemistry Memory" not in receipt_text
    assert "private-public-question" not in receipt_text
    assert bridge.current_head() is None
    with bridge._store.connect() as connection:  # noqa: SLF001
        before = (
            connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0],
        )
    assert before == (1, 1)
    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_STREAM_TERMINAL"):
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )
    with bridge._store.connect() as connection:  # noqa: SLF001
        after = (
            connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0],
        )
    assert after == before


def test_unsafe_checkpoint_has_private_receipt_and_is_not_overwritten(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )
    checkpoint = bridge._checkpoint_path  # noqa: SLF001
    preserved = b"PRIVATE_UNSAFE_CHECKPOINT_EVIDENCE\n"
    checkpoint.write_bytes(preserved)
    checkpoint.chmod(0o644)

    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_PRIVATE_CHECKPOINT_UNSAFE",
    ) as raised:
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )

    path, receipt = _load_only_core_failure_receipt(tmp_path)
    assert raised.value.diagnostic_receipt == path.name
    assert receipt["stage"] == "PRIVATE_CHECKPOINT"
    assert receipt["finding_code"] == "TASKWISE_PRIVATE_CHECKPOINT_UNSAFE"
    assert receipt["exception_class"] == "TASKWISE_CORE_EVOLUTION_ERROR"
    assert checkpoint.read_bytes() == preserved
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o644
    assert "PRIVATE_UNSAFE_CHECKPOINT_EVIDENCE" not in path.read_text(encoding="utf-8")
    assert bridge.current_head() is None


def test_existing_same_name_failure_receipt_is_never_deleted_or_modified(
    bridge: TaskwiseCoreEvolutionBridgeV1,
) -> None:
    request = _request(task_index=0, update_index=1, predecessor=None)
    receipt = TaskwiseCoreFailureReceiptV1(
        stage="ARTIFACT_PROMOTION",
        finding_code="TASKWISE_ARTIFACT_PROMOTION_FAILED",
        exception_class="OS_ERROR",
        errno=errno.EIO,
        task_index=0,
        round_index=0,
        update_index=1,
        job_id="job_existing_receipt",
        artifact_id="art_existing_receipt",
        predecessor_artifact_id=None,
        trajectory_digest="1" * 64,
        safe_feedback_digest="2" * 64,
        validator_input_digest="3" * 64,
        dataset_manifest_sha256="4" * 64,
        plan_digest="5" * 64,
        reflector_input_digest="6" * 64,
        reflector_receipt_digest=None,
        reflector_event_stream_digest=None,
        artifact_payload_sha256="7" * 64,
        artifact_lineage_sha256="8" * 64,
        memory_inspection_sha256="9" * 64,
        validation_receipt_sha256="a" * 64,
        core_artifact_manifest_sha256=None,
        context_resolution_digest=None,
    )
    basename = f"taskwise_core_failure_{canonical_digest(receipt)[:32]}.json"
    path = bridge._private_failure_root / basename  # noqa: SLF001
    preserved = b"IMMUTABLE_PREEXISTING_CORE_FAILURE_EVIDENCE\n"
    path.write_bytes(preserved)
    path.chmod(0o600)

    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_CORE_DIAGNOSTICS_FAILED",
    ) as raised:
        bridge._raise_private_core_failure(  # noqa: SLF001
            stage=receipt.stage,
            default_finding=receipt.finding_code,
            cause=OSError(errno.EIO, "PRIVATE_DUPLICATE_BODY_SENTINEL"),
            request=request,
            job_id=receipt.job_id,
            artifact_id=receipt.artifact_id,
            trajectory_digest=receipt.trajectory_digest,
            safe_feedback_digest=receipt.safe_feedback_digest,
            validator_input_digest=receipt.validator_input_digest,
            dataset_manifest_sha256=receipt.dataset_manifest_sha256,
            plan_digest=receipt.plan_digest,
            reflector_input_digest=receipt.reflector_input_digest,
            reflector_receipt_digest=receipt.reflector_receipt_digest,
            reflector_event_stream_digest=receipt.reflector_event_stream_digest,
            artifact_payload_sha256=receipt.artifact_payload_sha256,
            artifact_lineage_sha256=receipt.artifact_lineage_sha256,
            memory_inspection_sha256=receipt.memory_inspection_sha256,
            validation_receipt_sha256=receipt.validation_receipt_sha256,
            core_artifact_manifest_sha256=receipt.core_artifact_manifest_sha256,
            context_resolution_digest=receipt.context_resolution_digest,
        )

    assert raised.value.diagnostic_receipt is None
    assert path.read_bytes() == preserved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob("taskwise_core_failure_*.json")) == [path]


def test_over_limit_candidate_is_rejected_without_truncation_or_fallback(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _memory_with_do_items(
        tuple(f"General rule number {index}." for index in range(25))
    )
    candidate_sha256 = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        core_methods,
        "_guard_generic_reflector_output",
        lambda markdown, **_kwargs: (
            markdown,
            {
                "finding_count": 0,
                "redaction_count": 0,
                "remaining_finding_count": 0,
                "findings": [],
            },
        ),
    )

    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_ARTIFACT_VALIDATION_FAILED",
    ):
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )

    with bridge._store.connect() as connection:  # noqa: SLF001
        rows = connection.execute(
            "SELECT promoted, uri FROM artifacts WHERE type = 'text_memory'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["promoted"] == 0
    payload_path = Path(str(rows[0]["uri"]).removeprefix("file://"))
    payload = payload_path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == candidate_sha256
    assert payload.decode("utf-8") == candidate
    assert bridge._head is None  # noqa: SLF001
    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_STREAM_TERMINAL"):
        bridge.apply_update(
            _request(task_index=0, update_index=1, predecessor=None),
            test_only_allow_synthetic_reflector=True,
        )


def test_head_payload_drift_blocks_runtime_capability(
    bridge: TaskwiseCoreEvolutionBridgeV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(1),
    )
    result = bridge.apply_update(
        _request(task_index=0, update_index=1, predecessor=None),
        test_only_allow_synthetic_reflector=True,
    )
    artifact = bridge._store.get_artifact(result.core_artifact_id)  # noqa: SLF001
    path = Path(artifact.uri.removeprefix("file://"))
    path.write_text(_memory(9), encoding="utf-8")
    with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_HEAD_IDENTITY_DRIFT"):
        bridge.issue_runtime_memory(result)


def test_source_has_no_legacy_or_adapter_local_evolution_imports() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "openevo_chembench"
        / "taskwise_core_evolution_v1.py"
    ).read_text(encoding="utf-8")
    assert "SafeEvolutionSignal" not in source
    assert "from openevo_chembench.reflector import" not in source
    assert "from openevo_chembench.artifacts import" not in source
    assert "from openevo_chembench.runtime_context import" not in source
