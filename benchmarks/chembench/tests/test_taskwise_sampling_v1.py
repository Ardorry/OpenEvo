from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REVISION,
)
from openevo_chembench.source_identity_v2 import build_source_manifest
from openevo_chembench.taskwise_sampling_v1 import (
    CONTROL_PROTOCOL_ID,
    LEGACY_SINGLE_CHAIN_CLASSIFICATION,
    ONLINE_PROTOCOL_ID,
    PILOT500_INTERLEAVING,
    PILOT500_STREAM_COUNT,
    PILOT500_STREAM_DESIGN,
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
    PROTOCOL_CLASSIFICATION,
    PROTOCOL_FAMILY,
    generate_taskwise_manifests,
    legacy_single_chain_provenance_bytes,
    pilot500_stream_suite_summary_bytes,
    select_taskwise_pilot_streams,
    select_taskwise_stream,
    verify_taskwise_manifests,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)


@lru_cache(maxsize=1)
def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)


def _generate(root: Path, scope: str):
    return generate_taskwise_manifests(
        _loader(),
        scope=scope,
        public_path=root / f"online_{scope}_public_manifest.jsonl",
        private_path=root / "private" / f"online_{scope}_private_manifest.jsonl",
        summary_path=root / f"online_{scope}_summary.json",
    )


def test_canary9_is_exactly_one_task_per_category() -> None:
    tasks, allocation, _seed = select_taskwise_stream(_loader(), scope="canary9")

    assert len(tasks) == 9
    assert len({task.uid for task in tasks}) == 9
    assert allocation == {category: 1 for category in CHEMBENCH4K_CATEGORIES}
    assert {task.category for task in tasks} == set(CHEMBENCH4K_CATEGORIES)


def test_online_pilot_is_exactly_500_with_frozen_stratification() -> None:
    tasks, allocation, _seed = select_taskwise_stream(_loader(), scope="pilot500")

    assert len(tasks) == 500
    assert len({task.uid for task in tasks}) == 500
    assert allocation == {
        "Name_Conversion": 100,
        "Property_Prediction": 89,
        "Mol2caption": 37,
        "Caption2mol": 100,
        "Product_Prediction": 38,
        "Retrosynthesis": 37,
        "Yield_Prediction": 37,
        "Temperature_Prediction": 25,
        "Solvent_Prediction": 37,
    }


def test_pilot500_is_ten_disjoint_category_interleaved_streams() -> None:
    suite = select_taskwise_pilot_streams(_loader())
    legacy, _allocation, _seed = select_taskwise_stream(_loader(), scope="pilot500")

    assert len(suite.streams) == PILOT500_STREAM_COUNT == 10
    assert all(len(stream) == PILOT500_TASKS_PER_STREAM == 50 for stream in suite.streams)
    assert {task.uid for stream in suite.streams for task in stream} == {
        task.uid for task in legacy
    }
    assert len({task.uid for stream in suite.streams for task in stream}) == 500
    for stream in suite.streams:
        assert all(
            left.category != right.category
            for left, right in zip(stream, stream[1:], strict=False)
        )
        assert set(task.category for task in stream) == set(CHEMBENCH4K_CATEGORIES)


def test_each_pilot_stream_manifest_is_deterministic_private_and_bound_to_suite(
    tmp_path: Path,
) -> None:
    first_manifests = []
    second_manifests = []
    for scope in PILOT500_STREAM_SCOPES:
        first_manifests.append(_generate(tmp_path / "first" / scope, scope))
        second_manifests.append(_generate(tmp_path / "second" / scope, scope))
    assert all(
        left.public_path.read_bytes() == right.public_path.read_bytes()
        and left.private_path.read_bytes() == right.private_path.read_bytes()
        and left.summary_path.read_bytes() == right.summary_path.read_bytes()
        for left, right in zip(first_manifests, second_manifests, strict=True)
    )
    assert all(manifest.item_count == 50 for manifest in first_manifests)
    assert all(manifest.private_path.stat().st_mode & 0o077 == 0 for manifest in first_manifests)

    summaries = [
        json.loads(manifest.summary_path.read_text(encoding="utf-8"))
        for manifest in first_manifests
    ]
    assert [summary["stream_id"] for summary in summaries] == list(PILOT500_STREAM_SCOPES)
    assert all(summary["stream_design"] == PILOT500_STREAM_DESIGN for summary in summaries)
    assert all(summary["category_interleaving"] == PILOT500_INTERLEAVING for summary in summaries)
    assert len({summary["global_ordered_uid_sha256"] for summary in summaries}) == 1
    suite_bytes = pilot500_stream_suite_summary_bytes(
        _loader(),
        stream_manifests=tuple(first_manifests),
    )
    suite_summary = json.loads(suite_bytes)
    required_suite_keys = {
        "stream_count",
        "tasks_per_stream",
        "per_stream_ordered_uid_sha256",
        "combined_ordered_uid_sha256",
        "per_stream_category_counts",
        "reset_memory_between_streams",
        "sampling_seed",
    }
    assert required_suite_keys <= set(suite_summary)
    assert suite_summary["stream_count"] == 10
    assert suite_summary["tasks_per_stream"] == 50
    assert suite_summary["total_item_count"] == 500
    assert suite_summary["reset_memory_between_streams"] is True
    assert suite_summary["sampling_seed"]
    assert suite_summary["combined_ordered_uid_sha256"]
    assert len(suite_summary["per_stream_ordered_uid_sha256"]) == 10
    assert len(suite_summary["per_stream_category_counts"]) == 10
    assert len(suite_summary["streams"]) == 10
    assert set(suite_summary["per_stream_ordered_uid_sha256"]) == set(PILOT500_STREAM_SCOPES)
    assert set(suite_summary["per_stream_category_counts"]) == set(PILOT500_STREAM_SCOPES)


def test_original_single_chain_manifest_is_preserved_and_marked_legacy() -> None:
    public = (
        PACKAGE_ROOT / "manifests/taskwise_online_v1/legacy_single_stream/"
        "online_pilot500_public_manifest.jsonl"
    )
    private = (
        PACKAGE_ROOT / "private_manifests/taskwise_online_v1/legacy_single_stream/"
        "online_pilot500_private_manifest.jsonl"
    )
    summary = (
        PACKAGE_ROOT / "manifests/taskwise_online_v1/legacy_single_stream/"
        "online_pilot500_summary.json"
    )
    legacy = verify_taskwise_manifests(
        _loader(),
        scope="pilot500",
        public_path=public,
        private_path=private,
        summary_path=summary,
    )

    assert legacy.public_sha256 == (
        "ae016bbdba246bf86b1ee880c266e12ceda3376b5dbf91d6686c57c157564c1f"
    )
    assert legacy.private_sha256 == (
        "0873fba74a55b6942ecc5513e3671befafd561694901d3e66dbd0645697910d7"
    )
    assert legacy.summary_sha256 == (
        "124778cf491d31af545a558615afc2485f190bef4084d06149f435f00d81f01b"
    )
    assert private.stat().st_mode & 0o077 == 0
    provenance = json.loads(legacy_single_chain_provenance_bytes(legacy))
    assert provenance["classification"] == list(LEGACY_SINGLE_CHAIN_CLASSIFICATION)
    assert "LEGACY_SINGLE_STREAM_PILOT_MANIFEST" in provenance["classification"]
    assert "NOT_PRIMARY_STATISTICAL_PROTOCOL" in provenance["classification"]
    assert provenance["replacement_stream_scopes"] == list(PILOT500_STREAM_SCOPES)


def test_primary_pilot_paths_have_no_obsolete_flat_stream_or_single_chain_files() -> None:
    public_root = PACKAGE_ROOT / "manifests/taskwise_online_v1"
    private_root = PACKAGE_ROOT / "private_manifests/taskwise_online_v1"
    obsolete = [
        public_root / "online_pilot500_public_manifest.jsonl",
        private_root / "online_pilot500_private_manifest.jsonl",
        public_root / "online_pilot500_streams_summary.json",
    ]
    for stream_index in range(PILOT500_STREAM_COUNT):
        obsolete.extend(
            (
                public_root / f"online_pilot500_stream_{stream_index:02d}_public_manifest.jsonl",
                public_root / f"online_pilot500_stream_{stream_index:02d}_summary.json",
                private_root / f"online_pilot500_stream_{stream_index:02d}_private_manifest.jsonl",
            )
        )

    assert not [path for path in obsolete if path.exists()]


def test_stream_manifests_are_byte_deterministic_and_target_is_private(
    tmp_path: Path,
) -> None:
    for scope, count in (("canary9", 9), ("pilot500", 500)):
        first = _generate(tmp_path / "first" / scope, scope)
        second = _generate(tmp_path / "second" / scope, scope)

        assert first.item_count == second.item_count == count
        assert first.public_path.read_bytes() == second.public_path.read_bytes()
        assert first.private_path.read_bytes() == second.private_path.read_bytes()
        assert first.summary_path.read_bytes() == second.summary_path.read_bytes()
        assert first.private_path.stat().st_mode & 0o077 == 0
        public_rows = [
            json.loads(line) for line in first.public_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [row["ordinal"] for row in public_rows] == list(range(count))
        assert all(
            not {"target", "answer", "source_index", "source_split", "dataset_sha256"} & set(row)
            for row in public_rows
        )
        verify_taskwise_manifests(
            _loader(),
            scope=scope,
            public_path=first.public_path,
            private_path=first.private_path,
            summary_path=first.summary_path,
        )


def test_stream_manifest_identity_is_not_the_frozen_protocol(tmp_path: Path) -> None:
    generated = _generate(tmp_path, "canary9")
    summary = json.loads(generated.summary_path.read_text(encoding="utf-8"))

    assert summary["protocol_family"] == PROTOCOL_FAMILY
    assert summary["arm_protocol_ids"] == [CONTROL_PROTOCOL_ID, ONLINE_PROTOCOL_ID]
    assert summary["classification"] == list(PROTOCOL_CLASSIFICATION)
    assert summary["protocol_family"] != "chembench4k_frozen_generalization_v2"
    assert "frozen-generalization-v2" not in summary["seed"]


def test_source_identity_tracks_public_stream_manifests_but_not_private() -> None:
    manifest = build_source_manifest(PACKAGE_ROOT)
    paths = {entry["path"] for entry in manifest["files"]}

    for scope in ("canary9",):
        assert f"manifests/taskwise_online_v1/online_{scope}_public_manifest.jsonl" in paths
        assert f"manifests/taskwise_online_v1/online_{scope}_summary.json" in paths
        assert (
            f"private_manifests/taskwise_online_v1/online_{scope}_private_manifest.jsonl"
            not in paths
        )
    assert "manifests/taskwise_online_v1/online_pilot500_summary.json" in paths
    for stream_index in range(10):
        assert (
            f"manifests/taskwise_online_v1/streams/stream_{stream_index:02d}_public_manifest.jsonl"
        ) in paths
        assert (
            f"manifests/taskwise_online_v1/streams/stream_{stream_index:02d}_summary.json"
        ) in paths
        assert not any(
            path.startswith("private_manifests/taskwise_online_v1/streams/") for path in paths
        )
    assert (
        "manifests/taskwise_online_v1/legacy_single_stream/online_pilot500_public_manifest.jsonl"
    ) in paths
    assert ("manifests/taskwise_online_v1/legacy_single_stream/provenance.json") in paths
