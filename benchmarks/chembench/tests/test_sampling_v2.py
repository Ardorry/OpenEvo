from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.sampling_v2 import (
    generate_task_manifests,
    largest_remainder_allocation,
    verify_task_manifests,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)


@lru_cache(maxsize=1)
def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)


def test_largest_remainder_allocation_is_exactly_500() -> None:
    population = {
        category: values["test"] for category, values in _loader().manifest.category_counts.items()
    }
    allocation = largest_remainder_allocation(population, sample_size=500)
    assert sum(allocation.values()) == 500
    assert all(count >= 1 for count in allocation.values())
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


def test_pilot_manifest_is_byte_deterministic_and_public_target_free(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = generate_task_manifests(
        _loader(),
        scope="pilot500",
        public_path=first_root / "pilot500_public_manifest.jsonl",
        private_path=first_root / "private" / "pilot500_private_manifest.jsonl",
        summary_path=first_root / "pilot500_manifest_summary.json",
    )
    second = generate_task_manifests(
        _loader(),
        scope="pilot500",
        public_path=second_root / "pilot500_public_manifest.jsonl",
        private_path=second_root / "private" / "pilot500_private_manifest.jsonl",
        summary_path=second_root / "pilot500_manifest_summary.json",
    )

    assert first.item_count == second.item_count == 500
    assert first.public_path.read_bytes() == second.public_path.read_bytes()
    assert first.private_path.read_bytes() == second.private_path.read_bytes()
    assert first.summary_path.read_bytes() == second.summary_path.read_bytes()
    public_rows = [
        json.loads(line) for line in first.public_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(public_rows) == 500
    assert all("target" not in row and "answer" not in row for row in public_rows)
    assert len({row["uid"] for row in public_rows}) == 500
    assert first.private_path.stat().st_mode & 0o077 == 0
    verify_task_manifests(
        _loader(),
        scope="pilot500",
        public_path=first.public_path,
        private_path=first.private_path,
        summary_path=first.summary_path,
    )


def test_canary_and_full_manifests_have_exact_local_counts(tmp_path: Path) -> None:
    for scope, expected in (("canary18", 18), ("full", 4009)):
        generated = generate_task_manifests(
            _loader(),
            scope=scope,
            public_path=tmp_path / scope / "public.jsonl",
            private_path=tmp_path / scope / "private.jsonl",
            summary_path=tmp_path / scope / "summary.json",
        )
        assert generated.item_count == expected
