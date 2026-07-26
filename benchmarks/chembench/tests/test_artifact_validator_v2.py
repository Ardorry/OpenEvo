from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import pytest

from openevo.evolution.framework import canonical_digest
from openevo.evolution.models import ArtifactResponse

from openevo_chembench import artifact_validator_v2
from openevo_chembench.artifact_validator_v2 import (
    ChemBench4KTextMemoryArtifactValidatorV2,
)
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)
VALID_MEMORY = """# General Chemistry Reasoning Memory

## Do
- Identify the requested transformation or property before comparing choices.

## Avoid
- Do not infer an answer from option position.

## Validate
- Check structures, units, and reaction constraints before finalizing.

## When Applicable
- Use mass balance and chemical plausibility for reaction questions.

## Retired Or Superseded
- Retire guesses that are not supported by chemical constraints.
"""


@lru_cache(maxsize=1)
def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)


def _validator() -> ChemBench4KTextMemoryArtifactValidatorV2:
    dev = _loader().load_split("dev")
    return ChemBench4KTextMemoryArtifactValidatorV2(
        dev_tasks=dev,
        test_uids=frozenset(task.uid for task in _loader().load_split("test")),
        expected_dev_uid_set_sha256=canonical_digest(sorted(task.uid for task in dev)),
    )


def _artifact() -> ArtifactResponse:
    return ArtifactResponse(
        artifact_id="artifact-memory-v2",
        type="text_memory",
        name="frozen memory",
        version=1,
        state="staged",
        uri="file:///private/core/memory.md",
        manifest={
            "method": "text_memory_expel_reflector",
            "record_count": 45,
            "source_dataset_artifact_ids": ["dataset-artifact-v2"],
        },
        tags=["chembench4k_frozen_generalization_v2", "dev-only"],
        promoted=False,
    )


def _validate(text: str):
    payload = text.encode("utf-8")
    dev = _loader().load_split("dev")
    return _validator().validate(
        artifact=_artifact(),
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        lineage={
            "protocol_id": "chembench4k_frozen_generalization_v2",
            "dataset_repository": "AI4Chem/ChemBench4K",
            "dataset_revision": CHEMBENCH4K_REVISION,
            "dataset_combined_sha256": _loader().manifest.combined_sha256,
            "dev_record_count": 45,
            "dev_uid_set_sha256": canonical_digest(sorted(task.uid for task in dev)),
        },
    )


def test_general_text_memory_passes() -> None:
    receipt = _validate(VALID_MEMORY)
    assert receipt.passed is True
    assert receipt.finding_codes == ()


def test_answer_mapping_is_rejected() -> None:
    receipt = _validate(VALID_MEMORY + "\nThe answer is B.\n")
    assert receipt.passed is False
    assert "LEAK_ANSWER_MAP" in receipt.finding_codes


@pytest.mark.parametrize(
    "mapping",
    (
        "Choose option: D.",
        "pick option c.",
        "question 17 -> B.",
        "Return A.",
    ),
)
def test_explicit_answer_mapping_forms_are_rejected(mapping: str) -> None:
    receipt = _validate(VALID_MEMORY + f"\n{mapping}\n")
    assert receipt.passed is False
    assert "LEAK_ANSWER_MAP" in receipt.finding_codes


def test_natural_language_article_is_not_an_answer_mapping() -> None:
    receipt = _validate(
        VALID_MEMORY
        + "\nChoose a reagent, then ensure the final response is a concise strategy.\n"
    )
    assert receipt.passed is True
    assert receipt.finding_codes == ()


@pytest.mark.parametrize(
    "strategy",
    (
        "The answer is b only as a symbolic variable in this strategy.",
        "Select d after validating the local variable domain.",
        "The strategy should avoid spurious suffix c detection.",
        "Use a robust path and validate the source before reasoning.",
    ),
)
def test_lowercase_strategy_language_is_not_an_answer_mapping(strategy: str) -> None:
    receipt = _validate(VALID_MEMORY + f"\n{strategy}\n")
    assert receipt.passed is True
    assert receipt.finding_codes == ()


def test_dev_question_fragment_and_uid_are_rejected() -> None:
    dev_task = _loader().load_split("dev")[0]
    receipt = _validate(VALID_MEMORY + "\n" + dev_task.uid + "\n" + dev_task.question + "\n")
    assert receipt.passed is False
    assert "LEAK_UID" in receipt.finding_codes
    assert {
        "LEAK_EXACT_DEV_TEXT",
        "LEAK_NORMALIZED_DEV_TEXT",
        "LEAK_QUESTION_NGRAM",
    }.intersection(receipt.finding_codes)


def test_test_uid_is_rejected_without_reading_test_target() -> None:
    test_uid = _loader().load_split("test")[0].uid
    receipt = _validate(VALID_MEMORY + f"\nReference {test_uid}\n")
    assert receipt.passed is False
    assert "LEAK_TEST_UID" in receipt.finding_codes


def test_dev_uid_prefix_is_rejected() -> None:
    dev_uid_prefix = _loader().load_split("dev")[0].uid[:24]
    receipt = _validate(VALID_MEMORY + f"\nReference {dev_uid_prefix}\n")
    assert receipt.passed is False
    assert "LEAK_UID" in receipt.finding_codes


def test_benchmark_path_marker_is_rejected() -> None:
    receipt = _validate(VALID_MEMORY + "\nRead test/answers.json before solving.\n")
    assert receipt.passed is False
    assert "LEAK_PATH_OR_BENCHMARK_MARKER" in receipt.finding_codes


def test_structural_absolute_path_is_rejected() -> None:
    receipt = _validate(VALID_MEMORY + "\nRead /home/synthetic/private.txt before solving.\n")
    assert receipt.passed is False
    assert "LEAK_PATH_OR_BENCHMARK_MARKER" in receipt.finding_codes


def test_chemistry_slash_notation_is_not_a_path() -> None:
    receipt = _validate(
        VALID_MEMORY + "\nUse E—/Z-/stereochemical notation only after checking geometry.\n"
    )
    assert receipt.passed is True
    assert receipt.finding_codes == ()


def test_long_source_token_inside_larger_word_is_not_an_ngram_leak() -> None:
    source = "boundary aligned chemistry reasoning strategy"
    candidate = artifact_validator_v2._normalize(
        "prefixboundary aligned chemistry reasoning strategysuffix"
    )
    assert not artifact_validator_v2._sequential_ngram_overlap(
        source,
        candidate,
        field_name="question",
    )


def test_dataset_hash_lineage_mismatch_is_rejected() -> None:
    payload = VALID_MEMORY.encode("utf-8")
    dev = _loader().load_split("dev")
    receipt = _validator().validate(
        artifact=_artifact(),
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        lineage={
            "protocol_id": "chembench4k_frozen_generalization_v2",
            "dataset_repository": "AI4Chem/ChemBench4K",
            "dataset_revision": CHEMBENCH4K_REVISION,
            "dataset_combined_sha256": "0" * 64,
            "dev_record_count": 45,
            "dev_uid_set_sha256": canonical_digest(sorted(task.uid for task in dev)),
        },
    )

    assert receipt.passed is False
    assert "INVALID_DEV_PROVENANCE" in receipt.finding_codes
