from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REVISION,
)
from openevo_chembench.chembench4k_prompt import (
    build_all_dev_leave_one_out_tasks,
    build_dev_leave_one_out_tasks,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)


@lru_cache(maxsize=1)
def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)


def _all_loo_tasks():
    dev_by_category = {
        category: _loader().load_category(category, split="dev")
        for category in CHEMBENCH4K_CATEGORIES
    }
    return build_all_dev_leave_one_out_tasks(dev_by_category)


def test_dev_leave_one_out_has_exactly_45_complete_unique_evaluations() -> None:
    loo = _all_loo_tasks()
    expected_uids = {task.uid for task in _loader().load_split("dev")}

    assert len(loo) == 45
    assert {item.evaluation_task.uid for item in loo} == expected_uids
    assert len({item.evaluation_task.uid for item in loo}) == 45


def test_each_dev_item_uses_other_four_in_original_order() -> None:
    for category in CHEMBENCH4K_CATEGORIES:
        dev = _loader().load_category(category, split="dev")
        loo = build_dev_leave_one_out_tasks(dev)
        for item in loo:
            expected = tuple(task.uid for task in dev if task.uid != item.evaluation_task.uid)
            assert item.prompt.demonstration_uids == expected
            assert len(item.prompt.demonstration_uids) == 4
            assert item.evaluation_task.uid not in item.prompt.demonstration_uids
            assert item.prompt.text.endswith("Answer:")


def test_leave_one_out_prompt_cannot_include_own_answer_as_a_demo_binding() -> None:
    for item in _all_loo_tasks():
        final_block = item.prompt.text.rsplit("\n\n", maxsplit=1)[-1]

        assert final_block.endswith("Answer:")
        assert f"Answer: {item.evaluation_task.target}" not in final_block
        assert item.evaluation_task.uid not in item.prompt.demonstration_uids


def test_dev_trajectory_contains_no_test_uid() -> None:
    test_uids = {task.uid for task in _loader().load_split("test")}
    loo = _all_loo_tasks()
    all_referenced = {
        uid for item in loo for uid in (item.evaluation_task.uid, *item.prompt.demonstration_uids)
    }

    assert all_referenced.isdisjoint(test_uids)


def test_leave_one_out_rejects_test_input() -> None:
    test_tasks = _loader().load_category("Caption2mol", split="test")[:5]

    with pytest.raises(ValueError, match="dev"):
        build_dev_leave_one_out_tasks(test_tasks)


def test_complete_loo_builder_requires_all_nine_categories() -> None:
    incomplete = {
        category: _loader().load_category(category, split="dev")
        for category in CHEMBENCH4K_CATEGORIES[:-1]
    }

    with pytest.raises(ValueError, match="nine"):
        build_all_dev_leave_one_out_tasks(incomplete)
