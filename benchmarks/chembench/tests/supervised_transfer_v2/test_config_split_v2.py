from __future__ import annotations

import json
import stat
from functools import lru_cache
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES, CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.exposure_v2 import (
    ACTUAL_EXPOSURE_LABELS_V2,
    ExposureLabelV2,
)
from openevo_chembench.supervised_transfer_v2.config import (
    MANIFEST_ROOT,
    load_config_v2,
)
from openevo_chembench.supervised_transfer_v2.prepare import (
    load_frozen_exposure_bundle_v2,
)
from openevo_chembench.supervised_transfer_v2.source_identity import (
    build_source_manifest_v2,
    verify_source_manifest_v2,
)
from openevo_chembench.supervised_transfer_v2.split import (
    generate_train_test_split_v2,
    load_private_partition_v2,
    render_split_artifacts_v2,
    verify_split_artifacts_v2,
)

REPOSITORY = Path(__file__).resolve().parents[4]
CONFIG = (
    REPOSITORY
    / "benchmarks/chembench/configs/supervised_transfer_v2/chembench_supervised_transfer_v2.yaml"
)
MANIFESTS = REPOSITORY / MANIFEST_ROOT


@lru_cache(maxsize=1)
def _inputs():
    data = REPOSITORY / "data/chembench4k/AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
    loader = ChemBench4KDatasetLoader(
        snapshot_root=data,
        manifest_path=data / "chembench4k_dataset_manifest_v2.json",
    )
    exposure = load_frozen_exposure_bundle_v2(MANIFESTS)
    split = generate_train_test_split_v2(loader, exposure=exposure)
    return loader, exposure, split


def test_config_has_exact_one_call_three_target_strategy() -> None:
    config = load_config_v2(CONFIG.resolve())
    assert config.call_budget == {
        "control_train_task_calls": 1800,
        "online_train_task_calls": 1800,
        "online_train_reflector_calls": 1350,
        "online_train_core_jobs": 4050,
        "final_test_task_calls": 900,
        "total_model_calls": 5850,
    }
    assert config.payload["targets"] == ["text_memory", "skill_bundle", "agent_system"]
    assert config.payload["executor"]["task_attempts_per_train_item"] == 4
    assert config.payload["executor"]["evolution_cycles_per_train_item"] == 3
    assert config.payload["executor"]["allow_internet"] is True
    assert config.payload["executor"]["runtime_services_identity_required"] is True
    assert config.payload["executor"]["runtime_services_state_root"] == (
        "state/chembench_supervised_transfer_v2/runtime_services"
    )
    assert (
        config.payload["executor"]["network_policy"]
        == "model_transport_with_zero_tool_fail_closed"
    )


def test_split_is_two_formal_sets_only_and_is_byte_stable() -> None:
    loader, exposure, split = _inputs()
    assert len(split.train) == 450
    assert len(split.test) == 450
    assert len(split.reserve) == 3109
    assert not ({task.uid for task in split.train} & {task.uid for task in split.test})
    assert not ({task.uid for task in split.test} & exposure.actual_exposed_uids)
    for tasks in (split.train, split.test):
        assert {
            category: sum(task.category == category for task in tasks)
            for category in CHEMBENCH4K_CATEGORIES
        } == {category: 50 for category in CHEMBENCH4K_CATEGORIES}
    first = render_split_artifacts_v2(split, loader=loader, exposure=exposure)
    second = render_split_artifacts_v2(
        generate_train_test_split_v2(loader, exposure=exposure),
        loader=loader,
        exposure=exposure,
    )
    assert first == second
    assert not any("probe" in name for name in first)
    verify_split_artifacts_v2(first, destination=MANIFESTS)


def test_public_manifests_have_no_private_answer_fields() -> None:
    forbidden = {
        "target",
        "correct_answer",
        "correct_option",
        "correct_option_text",
        "private_feedback",
        "prediction",
        "correctness",
    }
    for name in (
        "train_public_manifest.jsonl",
        "test_public_manifest.jsonl",
        "reserve_public_manifest.jsonl",
    ):
        for line in (MANIFESTS / name).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            assert not (set(row) & forbidden)


def test_private_manifests_are_owner_only_and_reconstruct_exact_tasks() -> None:
    loader, _exposure, split = _inputs()
    for partition, expected in (("train", split.train), ("test", split.test)):
        path = MANIFESTS / f"{partition}_private_manifest.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert load_private_partition_v2(loader, partition=partition, path=path) == expected


def test_manifest_listing_alone_does_not_exclude_a_holdout_uid() -> None:
    _loader, exposure, split = _inputs()
    by_uid = {item.uid: item for item in exposure.items}
    actual_values = {label.value for label in ACTUAL_EXPOSURE_LABELS_V2}
    assert any(
        by_uid[task.uid].labels == (ExposureLabelV2.MANIFEST_LISTED_ONLY.value,)
        for task in split.test
    )
    assert all(not (set(by_uid[task.uid].labels) & actual_values) for task in split.test)


def test_split_receipt_directly_attests_zero_test_exposure() -> None:
    receipt = json.loads((MANIFESTS / "split_isolation_receipt_v2.json").read_text())
    assert receipt["status"] == "PASS"
    assert receipt["test_actual_exposure_count"] == 0
    assert receipt["test_evolution_jobs_allowed"] is False
    assert receipt["test_consumption_policy"] == "one-valid-completion-per-uid-per-arm"


def test_source_manifest_is_byte_stable_and_excludes_private_manifests() -> None:
    first = build_source_manifest_v2(REPOSITORY.resolve())
    second = build_source_manifest_v2(REPOSITORY.resolve())
    assert first == second
    assert verify_source_manifest_v2(REPOSITORY.resolve())["status"] == "PASS"
    paths = {row["path"] for row in first["source_files"]}
    assert not any(path.endswith("_private_manifest.jsonl") for path in paths)
    assert "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/core.py" in paths
