from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.supervised_transfer_v1.exposure_v2 import (
    ExposureLabelV2,
    HistoricalExposureV2Error,
    audit_post_freeze_exposure_isolation_v2,
    build_historical_exposure_bundle_v2,
    load_historical_exposure_artifacts_v2,
    write_historical_exposure_artifacts_v2,
)
from openevo_chembench.supervised_transfer_v1.split_v2 import (
    generate_balanced_split_v2,
    render_split_artifacts_v2,
    verify_split_artifacts_v2,
    write_split_artifacts_v2,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
OLD_REPOSITORY = Path("/home/lhy-h/work/openevo_chembench/OpenEvo")
MANIFEST_ROOT = (
    WORKSPACE_ROOT / "benchmarks" / "chembench" / "manifests" / "supervised_transfer_v1"
)
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT
    / "data"
    / "chembench4k"
    / "AI4Chem_ChemBench4K"
    / CHEMBENCH4K_REVISION
)
OLD_V1_SHA256 = "389c6ad25283b801f3bbe0061437a7891e2af80f17c01cd81aa901883eb4f660"
OLD_BLOCKER_SHA256 = "43e64dce6a4c357b5811042140b7a908820fa13e9fe0aefc0f9eab4da11ab18e"


def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_json_bytes(row) for row in rows))


def _synthetic_bundle(
    tmp_path: Path,
    *,
    evidence: dict[str, list[dict[str, object]]],
):
    tasks = _loader().load_split("test")[:9]
    old = tmp_path / "old"
    _write_jsonl(
        old / "benchmarks" / "chembench" / "manifests" / "full_public_manifest.jsonl",
        [{"uid": task.uid, "category": task.category} for task in tasks],
    )
    (old / "results").mkdir(parents=True)
    for relative, rows in evidence.items():
        _write_jsonl(old / "results" / relative, rows)
    return tasks, build_historical_exposure_bundle_v2(
        test_tasks=tasks,
        old_repository=old.resolve(),
        source_repository_commit="a" * 40,
    )


def _labels(bundle, uid: str) -> set[str]:
    return set(next(item for item in bundle.items if item.uid == uid).labels)


def _frozen_bundle():
    bundle, _digests = load_historical_exposure_artifacts_v2(
        destination_root=MANIFEST_ROOT.resolve(),
        old_v1_manifest_sha256=OLD_V1_SHA256,
        old_blocked_receipt_sha256=OLD_BLOCKER_SHA256,
    )
    return bundle


def test_manifest_listing_alone_remains_holdout_eligible(tmp_path: Path) -> None:
    tasks, bundle = _synthetic_bundle(tmp_path, evidence={})
    assert _labels(bundle, tasks[0].uid) == {ExposureLabelV2.MANIFEST_LISTED_ONLY.value}
    assert tasks[0].uid in bundle.strict_holdout_uids


def test_real_session_attempt_is_not_holdout_eligible(tmp_path: Path) -> None:
    tasks = _loader().load_split("test")[:9]
    row = {
        "task_uid": tasks[0].uid,
        "category": tasks[0].category,
        "session_id": "attempt-session-1",
        "completion_observed": False,
    }
    _tasks, bundle = _synthetic_bundle(tmp_path, evidence={"run/private/failures.jsonl": [row]})
    assert ExposureLabelV2.TASK_MODEL_ATTEMPTED.value in _labels(bundle, tasks[0].uid)
    assert tasks[0].uid not in bundle.strict_holdout_uids


def test_completion_without_evaluation_is_still_excluded(tmp_path: Path) -> None:
    tasks = _loader().load_split("test")[:9]
    row = {
        "kind": "completion",
        "task_uid": tasks[1].uid,
        "category": tasks[1].category,
        "session_id": "completion-session-1",
        "round_index": 0,
        "runtime_metadata": {"started_at_utc": "2026-07-24T00:00:00+00:00"},
    }
    _tasks, bundle = _synthetic_bundle(tmp_path, evidence={"run/public/events.jsonl": [row]})
    labels = _labels(bundle, tasks[1].uid)
    assert ExposureLabelV2.TASK_MODEL_EXECUTED.value in labels
    assert ExposureLabelV2.PRIVATE_EVALUATED.value not in labels
    assert tasks[1].uid not in bundle.strict_holdout_uids


def test_private_evaluation_is_excluded(tmp_path: Path) -> None:
    tasks = _loader().load_split("test")[:9]
    row = {
        "task_uid": tasks[2].uid,
        "category": tasks[2].category,
        "round_index": 0,
        "raw_completion": "A",
        "target": "B",
        "correct": False,
        "runtime_metadata": {"started_at_utc": "2026-07-24T00:00:01+00:00"},
    }
    _tasks, bundle = _synthetic_bundle(
        tmp_path,
        evidence={"run/private/evaluations.jsonl": [row]},
    )
    assert ExposureLabelV2.PRIVATE_EVALUATED.value in _labels(bundle, tasks[2].uid)
    assert tasks[2].uid not in bundle.strict_holdout_uids


def test_reflector_input_is_excluded(tmp_path: Path) -> None:
    tasks = _loader().load_split("test")[:9]
    row = {
        "kind": "core_update",
        "task_uid": tasks[3].uid,
        "core_job_id": "job-1",
        "evolution_job_id": "evolution-job-1",
        "protocol_id": "synthetic-supervised",
    }
    _tasks, bundle = _synthetic_bundle(tmp_path, evidence={"run/public/events.jsonl": [row]})
    assert ExposureLabelV2.REFLECTOR_SUPERVISED.value in _labels(bundle, tasks[3].uid)
    assert tasks[3].uid not in bundle.strict_holdout_uids


def test_aggregate_review_without_uid_does_not_pollute_holdout(tmp_path: Path) -> None:
    tasks, bundle = _synthetic_bundle(
        tmp_path,
        evidence={
            "audit/aggregate.jsonl": [
                {
                    "schema_version": "aggregate_review_receipt_v1",
                    "overall_accuracy": 0.5,
                    "review_scope": "aggregate_only",
                }
            ]
        },
    )
    assert bundle.aggregate_review_evidence_count == 1
    assert tasks[4].uid in bundle.strict_holdout_uids
    assert _labels(bundle, tasks[4].uid) == {ExposureLabelV2.MANIFEST_LISTED_ONLY.value}


def test_human_item_review_pollutes_exact_uid(tmp_path: Path) -> None:
    tasks = _loader().load_split("test")[:9]
    row = {
        "schema_version": "human_item_review_index_v1",
        "review_scope": "item_specific",
        "task_uid": tasks[5].uid,
        "category": tasks[5].category,
        "reviewed_at_utc": "2026-07-24T00:00:02+00:00",
        "protocol_id": "manual-audit",
    }
    _tasks, bundle = _synthetic_bundle(tmp_path, evidence={"audit/human.jsonl": [row]})
    item = next(item for item in bundle.items if item.uid == tasks[5].uid)
    assert item.human_item_reviewed is True
    assert ExposureLabelV2.HUMAN_ITEM_REVIEWED.value in item.labels
    assert tasks[5].uid not in bundle.strict_holdout_uids


def test_multiple_evidence_uses_all_strict_labels(tmp_path: Path) -> None:
    tasks = _loader().load_split("test")[:9]
    evaluation = {
        "task_uid": tasks[6].uid,
        "category": tasks[6].category,
        "round_index": 0,
        "raw_completion": "A",
        "target": "A",
        "correct": True,
    }
    update = {
        "kind": "core_update",
        "task_uid": tasks[6].uid,
        "core_job_id": "job-2",
        "evolution_job_id": "evolution-job-2",
    }
    _tasks, bundle = _synthetic_bundle(
        tmp_path,
        evidence={
            "run/private/evaluations.jsonl": [evaluation],
            "run/public/events.jsonl": [update],
        },
    )
    labels = _labels(bundle, tasks[6].uid)
    assert {
        ExposureLabelV2.TASK_MODEL_ATTEMPTED.value,
        ExposureLabelV2.TASK_MODEL_EXECUTED.value,
        ExposureLabelV2.PRIVATE_EVALUATED.value,
        ExposureLabelV2.REFLECTOR_SUPERVISED.value,
    } <= labels


def test_full_manifest_no_longer_makes_never_executed_pool_zero() -> None:
    bundle = _frozen_bundle()
    assert len(bundle.items) == 4009
    assert len(bundle.actual_exposed_uids) == 508
    assert len(bundle.strict_holdout_uids) == 3501
    assert bundle.summary_payload()["label_counts"]["MANIFEST_LISTED_ONLY"] == 3501


def test_v1_blocked_receipt_and_exposure_remain_byte_immutable() -> None:
    assert sha256_bytes(
        (MANIFEST_ROOT / "historical_exposed_uid_manifest.json").read_bytes()
    ) == OLD_V1_SHA256
    assert sha256_bytes(
        (MANIFEST_ROOT / "split_generation_blocked_receipt_v1.json").read_bytes()
    ) == OLD_BLOCKER_SHA256


def test_v2_split_is_balanced_isolated_and_deterministic(tmp_path: Path) -> None:
    loader = _loader()
    bundle = _frozen_bundle()
    first = generate_balanced_split_v2(loader, exposure_bundle=bundle)
    second = generate_balanced_split_v2(loader, exposure_bundle=bundle)
    assert {name: len(tasks) for name, tasks in first.partitions().items()} == {
        "train": 450,
        "probe": 90,
        "test_primary": 450,
        "test_recovery_01": 450,
        "reserve": 2569,
    }
    assert first == second
    assert len(first.recovery_tests) == 1
    for name, tasks in first.partitions().items():
        if name == "probe" or name.startswith("test_"):
            assert not ({task.uid for task in tasks} & bundle.actual_exposed_uids)
    outputs = render_split_artifacts_v2(first, loader=loader, exposure_bundle=bundle)
    assert outputs == render_split_artifacts_v2(second, loader=loader, exposure_bundle=bundle)
    generated = write_split_artifacts_v2(outputs, destination_root=tmp_path)
    verified = verify_split_artifacts_v2(expected=outputs, destination_root=tmp_path)
    assert generated.sha256 == verified.sha256
    assert all(
        (path.stat().st_mode & 0o777) == 0o600
        for name, path in generated.paths.items()
        if name.endswith("_private_manifest.jsonl")
    )


def test_frozen_exposure_artifacts_round_trip_from_canonical_evidence(tmp_path: Path) -> None:
    tasks, bundle = _synthetic_bundle(tmp_path / "source", evidence={})
    destination = (tmp_path / "frozen").resolve()
    destination.mkdir()
    expected = write_historical_exposure_artifacts_v2(
        bundle,
        destination_root=destination,
        old_v1_manifest_sha256=OLD_V1_SHA256,
        old_blocked_receipt_sha256=OLD_BLOCKER_SHA256,
    )
    loaded, actual = load_historical_exposure_artifacts_v2(
        destination_root=destination,
        old_v1_manifest_sha256=OLD_V1_SHA256,
        old_blocked_receipt_sha256=OLD_BLOCKER_SHA256,
    )
    assert loaded == bundle
    assert actual == expected
    assert set(loaded.strict_holdout_uids) == {task.uid for task in tasks}


def test_post_freeze_new_execution_outside_holdout_is_recorded(tmp_path: Path) -> None:
    tasks, frozen = _synthetic_bundle(tmp_path / "frozen", evidence={})
    attempted = {
        "task_uid": tasks[0].uid,
        "category": tasks[0].category,
        "session_id": "post-freeze-attempt",
        "completion_observed": False,
    }
    _tasks, live = _synthetic_bundle(
        tmp_path / "live",
        evidence={"run/private/failures.jsonl": [attempted]},
    )
    audit = audit_post_freeze_exposure_isolation_v2(
        frozen_bundle=frozen,
        live_bundle=live,
        frozen_holdout_uids=frozenset(task.uid for task in tasks[1:]),
    )
    assert audit.new_actual_exposed_uid_count == 1
    assert audit.holdout_overlap_count == 0


def test_post_freeze_execution_of_frozen_holdout_fails_closed(tmp_path: Path) -> None:
    tasks, frozen = _synthetic_bundle(tmp_path / "frozen", evidence={})
    completed = {
        "kind": "completion",
        "task_uid": tasks[1].uid,
        "category": tasks[1].category,
        "session_id": "post-freeze-completion",
        "round_index": 0,
    }
    _tasks, live = _synthetic_bundle(
        tmp_path / "live",
        evidence={"run/public/events.jsonl": [completed]},
    )
    with pytest.raises(
        HistoricalExposureV2Error,
        match="post-freeze actual exposure intersects frozen holdout",
    ):
        audit_post_freeze_exposure_isolation_v2(
            frozen_bundle=frozen,
            live_bundle=live,
            frozen_holdout_uids=frozenset({tasks[1].uid}),
        )


def test_public_v2_manifests_have_no_private_keys() -> None:
    forbidden = {
        "target",
        "correct_answer",
        "correct_option",
        "private_explanation",
        "historical_prediction",
        "historical_correctness",
    }
    for path in MANIFEST_ROOT.glob("*_public_manifest.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert not (set(json.loads(line)) & forbidden)
