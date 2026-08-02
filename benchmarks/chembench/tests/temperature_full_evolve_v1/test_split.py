from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    PublicChemBench4KTask,
)
from openevo_chembench.temperature_full_evolve_v1.split import (
    HISTORICAL_EXPOSURE_POLICY,
    RESERVE_COUNT,
    TEST_COUNT,
    TRAIN_COUNT,
    TemperatureSplitV1Error,
    build_public_split_plan_v1,
    build_temperature_split_v1,
    render_temperature_split_artifacts_v1,
    write_temperature_split_artifacts_v1,
)

REPOSITORY = Path(__file__).resolve().parents[4]
SNAPSHOT = REPOSITORY / "data/chembench4k/AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
DATASET_SHA256 = "1" * 64


def _uid(index: int) -> str:
    return hashlib.sha256(f"temperature-split-{index}".encode()).hexdigest()


def _reaction(index: int) -> str:
    digest = hashlib.sha256(f"reaction-{index}".encode()).hexdigest()
    return f"C{digest[:16]}>N{digest[16:32]}>O{digest[32:48]}"


def _public_task(
    index: int,
    *,
    reaction: str | None = None,
    question_prefix: str = "Estimate the reaction temperature for",
    options: tuple[str, str, str, str] | None = None,
) -> PublicChemBench4KTask:
    selected_options = options or tuple(str(index * 10 + offset) for offset in range(4))
    return PublicChemBench4KTask(
        uid=_uid(index),
        category="Temperature_Prediction",
        question=f"{question_prefix} {reaction or _reaction(index)} ?",
        A=selected_options[0],
        B=selected_options[1],
        C=selected_options[2],
        D=selected_options[3],
        dataset_revision=CHEMBENCH4K_REVISION,
    )


def _private_task(public: PublicChemBench4KTask, *, source_index: int) -> PrivateChemBench4KTask:
    return PrivateChemBench4KTask(
        uid=public.uid,
        category=public.category,
        source_split="test",
        source_index=source_index,
        question=public.question,
        A=public.A,
        B=public.B,
        C=public.C,
        D=public.D,
        target="ABCD"[source_index % 4],
        dataset_revision=public.dataset_revision,
        dataset_sha256=DATASET_SHA256,
    )


def _synthetic_pool() -> tuple[PublicChemBench4KTask, ...]:
    return tuple(_public_task(index) for index in range(202))


def test_live_frozen_pool_builds_deterministic_100_100_2_singleton_split() -> None:
    loader = ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT)

    first = build_temperature_split_v1(loader)
    second = build_temperature_split_v1(loader)

    assert (len(first.train), len(first.test), len(first.reserve)) == (
        TRAIN_COUNT,
        TEST_COUNT,
        RESERVE_COUNT,
    )
    assert first.plan == second.plan
    assert first.plan.split_sha256 == "779e1b60098a958519bfe61296b5cb6c28afa2a2db300238876dcb693ccb1100"
    assert first.plan.group_manifest_sha256 == (
        "3458ad8491150e0eb5a1526af947c07426d664697a93d2f063860aba167a17d8"
    )
    assert first.dev_demonstration_count == 5
    assert first.plan.group_count == 202
    assert first.plan.singleton_group_count == 202
    assert first.plan.maximum_group_size == 1
    assert not ({task.uid for task in first.train} & {task.uid for task in first.test})
    assert all(task.source_split == "test" for task in (*first.train, *first.test, *first.reserve))
    assert render_temperature_split_artifacts_v1(first) == render_temperature_split_artifacts_v1(
        second
    )


def test_grouping_is_public_field_only_and_whole_group_assignment_is_deterministic() -> None:
    pool = list(_synthetic_pool())
    base = pool[0]
    pool[1] = _public_task(
        1,
        reaction=next(token for token in base.question.split() if token.count(">") == 2),
        question_prefix="Estimate the reaction temperature for",
        options=(base.D, base.C, base.B, base.A),
    )

    first = build_public_split_plan_v1(tuple(pool))
    second = build_public_split_plan_v1(tuple(reversed(pool)))

    assert first == second
    assert first.group_count == 201
    assert first.maximum_group_size == 2
    partitions = {
        uid: partition
        for partition, values in first.partitions.items()
        for uid in values
    }
    assert partitions[pool[0].uid] == partitions[pool[1].uid]


def test_component_order_and_high_similarity_reactions_are_grouped() -> None:
    pool = list(_synthetic_pool())
    pool[0] = _public_task(0, reaction="CCO.CN>O=C=O>CCOC(=O)NCC")
    pool[1] = _public_task(1, reaction="CN.CCO>O=C=O>CCOC(=O)NCC")
    pool[2] = _public_task(
        2,
        reaction="CCCCCCCCCCCCCCCCN>O=C=O>CCCCCCCCCCCCCCCCNC(=O)O",
    )
    pool[3] = _public_task(
        3,
        reaction="CCCCCCCCCCCCCCCCl>O=C=O>CCCCCCCCCCCCCCCCOC(=O)O",
    )

    plan = build_public_split_plan_v1(tuple(pool))
    partitions = {
        uid: partition
        for partition, values in plan.partitions.items()
        for uid in values
    }

    assert partitions[pool[0].uid] == partitions[pool[1].uid]
    assert partitions[pool[2].uid] == partitions[pool[3].uid]
    assert plan.maximum_group_size >= 2


def test_split_fails_closed_when_whole_groups_cannot_fill_all_arms() -> None:
    reaction = "CCO.CN>O=C=O>CCOC(=O)NCC"
    pool = tuple(
        _public_task(index, reaction=reaction, question_prefix=f"Variant {index} for")
        for index in range(202)
    )

    with pytest.raises(TemperatureSplitV1Error, match="GROUP_AWARE_SPLIT_CAPACITY_UNAVAILABLE"):
        build_public_split_plan_v1(pool)


def test_rendered_public_receipt_is_aggregate_only_and_private_repr_is_redacted() -> None:
    loader = ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT)
    split = build_temperature_split_v1(loader)
    artifacts = render_temperature_split_artifacts_v1(split)
    receipt = json.loads(artifacts.public_receipt)

    assert receipt["historical_exposure_policy"] == HISTORICAL_EXPOSURE_POLICY
    assert receipt["counts"] == {"reserve": 2, "test": 100, "train": 100}
    assert receipt["source_pool"] == {
        "dev_demonstration_count": 5,
        "dev_policy": "excluded_from_split_fixed_official_five_shot",
        "dev_partition_ordered_prompt_overlap_count": 0,
        "dev_partition_uid_overlap_count": 0,
        "dev_partition_unordered_prompt_overlap_count": 0,
        "test_pool_count": 202,
    }
    assert receipt["grouping"]["group_count"] == 202
    assert receipt["grouping"]["singleton_group_count"] == 202

    forbidden_exact_keys = {
        "uid",
        "question",
        "options",
        "answer",
        "target",
        "prediction",
        "completion",
        "transcript",
        "secret",
        "api_key",
    }

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert str(key).casefold() not in forbidden_exact_keys
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(receipt)
    sample = split.train[0]
    assert sample.uid not in repr(split)
    assert sample.uid not in repr(split.plan)
    assert sample.uid not in repr(artifacts)
    assert sample.question not in repr(split)
    assert sample.target not in repr(split)

    private_row = json.loads(artifacts.train_private_manifest.splitlines()[0])
    assert private_row["uid"] == sample.uid
    assert private_row["source_index"] == sample.source_index
    assert private_row["target"] == sample.target
    assert "question" not in private_row
    assert not ({task.uid for task in split.test} & {task.uid for task in split.train})


def test_private_manifests_are_owner_only_when_written(tmp_path: Path) -> None:
    loader = ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT)
    artifacts = render_temperature_split_artifacts_v1(build_temperature_split_v1(loader))

    write_temperature_split_artifacts_v1(artifacts, destination=tmp_path / "split")

    assert stat.S_IMODE((tmp_path / "split/public/split_receipt.json").stat().st_mode) == 0o644
    for partition in ("train", "test", "reserve"):
        path = tmp_path / f"split/private/{partition}_manifest.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_private_targets_do_not_affect_public_grouping_or_assignment() -> None:
    public = _synthetic_pool()
    first_private = tuple(_private_task(task, source_index=index) for index, task in enumerate(public))
    second_private = tuple(
        PrivateChemBench4KTask(
            uid=task.uid,
            category=task.category,
            source_split=task.source_split,
            source_index=task.source_index,
            question=task.question,
            A=task.A,
            B=task.B,
            C=task.C,
            D=task.D,
            target="DCBA"[index % 4],
            dataset_revision=task.dataset_revision,
            dataset_sha256=task.dataset_sha256,
        )
        for index, task in enumerate(first_private)
    )

    plan = build_public_split_plan_v1(public)

    assert plan == build_public_split_plan_v1(tuple(task.to_public() for task in first_private))
    assert plan == build_public_split_plan_v1(tuple(task.to_public() for task in second_private))


def test_reaction_parser_fails_closed_instead_of_silently_skipping_grouping() -> None:
    pool = list(_synthetic_pool())
    task = pool[0]
    pool[0] = PublicChemBench4KTask(
        uid=task.uid,
        category=task.category,
        question="No reaction token is present here.",
        A=task.A,
        B=task.B,
        C=task.C,
        D=task.D,
        dataset_revision=task.dataset_revision,
    )

    with pytest.raises(TemperatureSplitV1Error, match="REACTION_TOKEN_INVALID"):
        build_public_split_plan_v1(tuple(pool))
