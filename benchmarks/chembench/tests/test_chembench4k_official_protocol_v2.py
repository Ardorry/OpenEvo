from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import (
    ChemBench4KParseStatus,
    ChemBench4KPrivateEvaluator,
    official_first_capital_parser,
    strict_single_letter_parser,
)
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REVISION,
)
from openevo_chembench.chembench4k_prompt import (
    OFFICIAL_INSTRUCTION,
    render_official_five_shot_prompt,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)
GOLDEN_PROMPT_SHA256 = {
    "Name_Conversion": ("a43dc38d4f1b354dfe3778f647e7f53772fa214de8fcd4c77ce8e07c9af52be2"),
    "Property_Prediction": ("027fe14c981f0e5c69d101376f2c1d5b04a7b377fff5ada63fc78d59a1148df4"),
    "Mol2caption": ("7b1f3f49a60b33060b4eff7e0ff404e994001da382d266e952182d5e046a1624"),
    "Caption2mol": ("35a3f735af64898e7b1fd0244075a4eccd51ef9a04cc1fcfb390a34020d7e312"),
    "Product_Prediction": ("abd638cf2095f37fd6c98c557af393e671737677ce188442262281e3a7cb8d27"),
    "Retrosynthesis": ("cd5d25ba66dff24b152fc3cef68c3d27eb8b7d0a2b45acf119ba2eb7d7919e7d"),
    "Yield_Prediction": ("d2711385eae6a2f46fb55fc3f81f606a501d8c1b4f3ee0e4a62091c6adbfebc3"),
    "Temperature_Prediction": ("869df9d1369c33e528cbf896e58e87130276a874043ef382589d4ed4fab558e6"),
    "Solvent_Prediction": ("dbe8ee2d5e4e698f58e422eb08d65988bb5b610d0cce3909afced8d9ef78d3db"),
}


@lru_cache(maxsize=1)
def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)


@pytest.mark.parametrize("category", CHEMBENCH4K_CATEGORIES)
def test_nine_category_official_prompt_golden_snapshots(category: str) -> None:
    dev = _loader().load_category(category, split="dev")
    test_task = _loader().load_category(category, split="test")[0]

    rendered = render_official_five_shot_prompt(
        test_task.to_public(),
        category_dev=dev,
    )

    assert (
        hashlib.sha256(rendered.text.encode("utf-8")).hexdigest()
        == (GOLDEN_PROMPT_SHA256[category])
    )
    assert rendered.text.count(OFFICIAL_INSTRUCTION) == 6
    assert rendered.demonstration_uids == tuple(task.uid for task in dev)
    assert rendered.text.endswith("Answer:")
    assert set(rendered.to_agent_payload()) == {"instruction"}


def test_five_shot_must_use_frozen_indices_zero_through_four() -> None:
    category = "Caption2mol"
    dev = _loader().load_category(category, split="dev")
    test_task = _loader().load_category(category, split="test")[0].to_public()

    with pytest.raises(ValueError, match="ordering"):
        render_official_five_shot_prompt(test_task, category_dev=tuple(reversed(dev)))


def test_five_shot_rejects_other_category_or_test_demonstrations() -> None:
    test_task = _loader().load_category("Caption2mol", split="test")[0].to_public()
    wrong_category = _loader().load_category("Mol2caption", split="dev")
    test_rows = _loader().load_category("Caption2mol", split="test")[:5]

    with pytest.raises(ValueError, match="category-local"):
        render_official_five_shot_prompt(test_task, category_dev=wrong_category)
    with pytest.raises(ValueError, match="category-local"):
        render_official_five_shot_prompt(test_task, category_dev=test_rows)


@pytest.mark.parametrize(
    ("raw", "prediction", "status"),
    (
        ("A", "A", ChemBench4KParseStatus.PARSED),
        (" C\n", "C", ChemBench4KParseStatus.PARSED),
        ("The answer is A", "T", ChemBench4KParseStatus.PARSED),
        ("Answer: B", "A", ChemBench4KParseStatus.PARSED),
        ("answer: D", "D", ChemBench4KParseStatus.PARSED),
        ("option c", "", ChemBench4KParseStatus.NO_UPPERCASE),
        ("", "", ChemBench4KParseStatus.NO_UPPERCASE),
    ),
)
def test_official_first_capital_parser_opencompass_compatibility(
    raw: str,
    prediction: str | None,
    status: ChemBench4KParseStatus,
) -> None:
    result = official_first_capital_parser(raw)

    assert result.prediction == prediction
    assert result.status is status


@pytest.mark.parametrize(
    ("raw", "prediction"),
    (
        ("A", "A"),
        ("  B\n", "B"),
        ("C because", None),
        ("answer: D", None),
        ("d", None),
        ("AB", None),
    ),
)
def test_strict_single_letter_parser(raw: str, prediction: str | None) -> None:
    result = strict_single_letter_parser(raw)

    assert result.prediction == prediction
    assert result.parsed is (prediction is not None)


def test_private_evaluator_exposes_no_target_in_public_result() -> None:
    task = _loader().load_category("Caption2mol", split="test")[0]
    result = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion=task.target,
    )
    public = result.to_public_result().to_public_dict()

    assert result.correct is True
    assert public["official_accuracy"] == 1.0
    assert public["strict_parse_success"] is True
    assert "target" not in public
    assert "raw_completion" not in public


def test_non_choice_first_capital_is_parsed_and_scores_incorrect() -> None:
    task = _loader().load_category("Caption2mol", split="test")[0]
    result = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion=f"The answer is {task.target}",
    )
    public = result.to_public_result().to_public_dict()

    assert result.official.prediction == "T"
    assert result.official.parsed is True
    assert result.correct is False
    assert public["official_prediction"] == "T"
    assert public["official_parse_status"] == "parsed"
    assert public["official_accuracy"] == 0.0
    assert public["strict_parse_success"] is False
