from __future__ import annotations

import hashlib
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import PublicChemBench4KTask
from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    CanonicalEntryV2,
    SafeArtifactStateV2,
    render_full_projection_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.config import (
    FROZEN_C4,
    load_safe_evolve_config_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.folds import (
    build_safe_folds_v2,
    private_fold_manifest_bytes_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.frozen_c4 import recover_frozen_c4_v2
from openevo_chembench.temperature_safe_evolve_v2.phase_a import load_phase_a_inputs_v2
from openevo_chembench.temperature_safe_evolve_v2.retrieval import (
    retrieve_sparse_context_v2,
)


def _entry(*, target: str = "text_memory") -> CanonicalEntryV2:
    return CanonicalEntryV2(
        entry_id="entry-" + "1" * 24,
        entry_type="category_rule" if target == "text_memory" else "reasoning_step",
        content=(
            "Strong cryogenic reagent signals favor a low-temperature regime."
            if target == "text_memory"
            else "Extract explicit cryogenic reagent signals before comparing temperature ranges."
        ),
        applicability="Use when the visible prompt explicitly contains a cryogenic reagent signal.",
        exclusion_conditions="Skip when the visible prompt explicitly specifies heating or reflux.",
        support_uid_refs=("1" * 64, "2" * 64),
        supporting_batches=(1,),
        first_seen_batch=1,
        last_evaluated_block=1,
        support_count=2,
        contradiction_count=0,
        positive_flip_count=0,
        negative_flip_count=0,
        confidence=0.8,
        status="active",
        source_packet_sha256="3" * 64,
    )


def _state(target: str = "text_memory") -> SafeArtifactStateV2:
    entry = _entry(target=target)
    projection = render_full_projection_v2(selected_target=target, entries=(entry,))
    return SafeArtifactStateV2(
        run_id="stv3-temperature-safe-evolve-v2-20990101T000000Z-r0",
        fold_id="R0",
        selected_target=target,
        state_kind="active",
        batch_index=1,
        predecessor_state_sha256="4" * 64,
        prior_evidence_sha256="5" * 64,
        source_packet_sha256="6" * 64,
        entries=(entry,),
        projection=projection,
        projection_sha256=hashlib.sha256(projection.encode()).hexdigest(),
        projection_utf8_bytes=len(projection.encode()),
        core_artifact_id="art_safe_001",
        core_job_id="job_safe_001",
        core_validation_receipt_sha256="7" * 64,
        core_payload_sha256="8" * 64,
        core_payload_utf8_bytes=100,
        promoted=True,
    )


def test_exact_frozen_c4_and_prior_test_recovery(repository_root: Path) -> None:
    config = load_safe_evolve_config_v2(
        repository_root / "benchmarks/chembench/configs/temperature_safe_evolve_v2/"
        "temperature_safe_evolve_v2.yaml"
    )
    bundle = recover_frozen_c4_v2(repository_root)
    phase = load_phase_a_inputs_v2(repository_root=repository_root, config=config)

    assert bundle.combined_context_bytes == FROZEN_C4["combined"][0]
    assert bundle.combined_context_sha256 == FROZEN_C4["combined"][1]
    assert {
        value.target_id: (value.utf8_bytes, value.payload_sha256) for value in bundle.artifacts
    } == {key: value for key, value in FROZEN_C4.items() if key != "combined"}
    assert len(phase.test) == 100
    assert len(phase.h0_uids) == len(phase.h1_uids) == 50
    assert not phase.h0_uids & phase.h1_uids
    assert len(phase.prior_baseline_only_ordinals) == 11


def test_four_fold_manifest_is_exact_group_aware_and_private(repository_root: Path) -> None:
    config = load_safe_evolve_config_v2(
        repository_root / "benchmarks/chembench/configs/temperature_safe_evolve_v2/"
        "temperature_safe_evolve_v2.yaml"
    )
    folds = build_safe_folds_v2(
        ChemBench4KDatasetLoader(
            snapshot_root=repository_root / str(config.payload["dataset"]["root"])
        )
    )
    assert [len(value) for value in folds.plan.folds] == [50, 50, 50, 50]
    assert len(folds.plan.reserve) == 2
    assert len(folds.plan.groups) == 202
    assert (
        folds.plan.fold_sha256
        == "3912df5d3cd32cfe44bafb87232eeabe36d08dcca559bc0ff571658cff9dc0fa"
    )
    private = private_fold_manifest_bytes_v2(folds)
    assert b'"normalized_question"' in private
    assert b'"normalized_options_ordered"' in private
    assert b'"near_duplicate_group_sha256"' in private


def test_sparse_retrieval_uses_only_visible_question_options_and_obeys_budget() -> None:
    common = {
        "category": "Temperature_Prediction",
        "question": "A cryogenic reagent is added before workup. Which temperature is used?",
        "A": "-78 C",
        "B": "0 C",
        "C": "25 C",
        "D": "100 C",
        "dataset_revision": "f8ad41a980170f4c5d0cc97e57722d06887c8f53",
    }
    left = PublicChemBench4KTask(uid="a" * 64, **common)
    right = PublicChemBench4KTask(uid="b" * 64, **common)
    left_receipt = retrieve_sparse_context_v2(left, state=_state())
    right_receipt = retrieve_sparse_context_v2(right, state=_state())

    assert left_receipt.retrieval_input_sha256 == right_receipt.retrieval_input_sha256
    assert [value.entry_id for value in left_receipt.selected] == ["entry-" + "1" * 24]
    assert left_receipt.injected_utf8_bytes <= 1200
    assert len(left_receipt.selected) <= 2
