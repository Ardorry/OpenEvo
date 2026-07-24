from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_dataset import (
    DATASET_MANIFEST_FILENAME,
    ChemBench4KDatasetLoader,
    build_dataset_manifest,
    expected_relative_files,
    normalize_benchmark_text,
    write_dataset_manifest,
)
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REPOSITORY,
    CHEMBENCH4K_REVISION,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)
EXPECTED_COMBINED_SHA256 = "cb6c17c54d4c0cf103b38f12e9ce05663515b05bfdc342d83e3245d39f3a4b3a"
EXPECTED_CATEGORY_COUNTS = {
    "Caption2mol": {"dev": 5, "test": 800},
    "Mol2caption": {"dev": 5, "test": 299},
    "Name_Conversion": {"dev": 5, "test": 799},
    "Product_Prediction": {"dev": 5, "test": 300},
    "Property_Prediction": {"dev": 5, "test": 709},
    "Retrosynthesis": {"dev": 5, "test": 300},
    "Solvent_Prediction": {"dev": 5, "test": 300},
    "Temperature_Prediction": {"dev": 5, "test": 202},
    "Yield_Prediction": {"dev": 5, "test": 300},
}
EXPECTED_FILE_SHA256 = {
    "dev/Caption2mol_benchmark.json": (
        "65985fe667c033a769ffc555595269f81bcdf99c8347ce58aaa74ea203f8b475"
    ),
    "dev/Mol2caption_benchmark.json": (
        "c66ca2d7fd9d307e2f46616f37a7a2ad1e9f6e866bb3a81a973dd825ba0bc738"
    ),
    "dev/Name_Conversion_benchmark.json": (
        "582619db87a7c654a012efd6e6c536635b1ac786d4f134f467632880a230010f"
    ),
    "dev/Product_Prediction_benchmark.json": (
        "e730ab99ab1089bc42047ce81af285f3a31555f27ddc4a9f2468a70a89626d2f"
    ),
    "dev/Property_Prediction_benchmark.json": (
        "b5c1eb1bc2f9bd459f704e0bdc0e32f326fd2a764b0929185100a202776f10e1"
    ),
    "dev/Retrosynthesis_benchmark.json": (
        "e1cfbe53371cd805618e2f8c74002762045dbde88b1d108524e9d77aaa982a0e"
    ),
    "dev/Solvent_Prediction_benchmark.json": (
        "8c1807b5db987ab94db2196927a550958fd071f8d7a9c5d0aa274b8a6c4bf209"
    ),
    "dev/Temperature_Prediction_benchmark.json": (
        "e4413780de535c15c1592dea1126628d802607eed18753f9c9960e276133287b"
    ),
    "dev/Yield_Prediction_benchmark.json": (
        "e8a6ebe1e25a4b81ac54db790134704fd32b8bbedb5a3ed84a64a021e1e287ef"
    ),
    "test/Caption2mol_benchmark.json": (
        "3376c65085b60d0be9c3459b528587f66b09de89ee56a60b92a0ee7150075dd6"
    ),
    "test/Mol2caption_benchmark.json": (
        "a7557bae188828b6792f5c42cad7aeb6e18c4adbe7c6e0a49ca6f17db22384bf"
    ),
    "test/Name_Conversion_benchmark.json": (
        "de0cef0ecef8065c2112c4d11bd822143fb8f0c30bb70251d7dbfc00aeff9ed2"
    ),
    "test/Product_Prediction_benchmark.json": (
        "03a5f7dc136c54cf3ebdebedb3705f7810148e1922c7e84db980233793bf6400"
    ),
    "test/Property_Prediction_benchmark.json": (
        "f465a42f19cfdb686c912c3b79db0d08f4cf585ec72e190b5adff3625c447a19"
    ),
    "test/Retrosynthesis_benchmark.json": (
        "7fd21ef010f06fa58fceb2de73a49c59957a91e6affa3b45357d6b7dc7d1f0ab"
    ),
    "test/Solvent_Prediction_benchmark.json": (
        "bd08fe4922ede07796125a96256b6bdce31aec1294db8b77392719e1abc4c8b3"
    ),
    "test/Temperature_Prediction_benchmark.json": (
        "c427a2c782f7808ed12881e90378be97a9cd9fb9c72222fbc638a7a241945fb5"
    ),
    "test/Yield_Prediction_benchmark.json": (
        "c8cd5b50f58e47db6912bee394ef7fb35927620dd9ce040c6b785cc0140342ad"
    ),
}


@lru_cache(maxsize=1)
def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=SNAPSHOT_ROOT)


def _private_key_names(value: object) -> set[str]:
    private_names = {
        "target",
        "answer",
        "source_split",
        "source_index",
        "dataset_sha256",
    }
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in private_names:
                found.add(str(key).casefold())
            found.update(_private_key_names(child))
    elif isinstance(value, (tuple, list)):
        for child in value:
            found.update(_private_key_names(child))
    return found


def test_fixed_dataset_identity_and_exact_snapshot_hashes() -> None:
    manifest = build_dataset_manifest(SNAPSHOT_ROOT)

    assert manifest.repository == CHEMBENCH4K_REPOSITORY
    assert manifest.revision == CHEMBENCH4K_REVISION
    assert manifest.files == expected_relative_files()
    assert manifest.dev_count == 45
    assert manifest.test_count == 4009
    assert manifest.category_counts == EXPECTED_CATEGORY_COUNTS
    assert manifest.file_sha256 == EXPECTED_FILE_SHA256
    assert manifest.combined_sha256 == EXPECTED_COMBINED_SHA256
    assert manifest.normalized_uid_count == 4054
    assert manifest.normalized_content_duplicate_count == 0


def test_exactly_eighteen_expected_json_files_exist() -> None:
    actual = tuple(
        sorted(
            path.relative_to(SNAPSHOT_ROOT).as_posix()
            for split in ("dev", "test")
            for path in (SNAPSHOT_ROOT / split).glob("*.json")
        )
    )

    assert actual == expected_relative_files()
    assert len(actual) == 18


def test_all_roots_rows_answers_and_fields_are_valid() -> None:
    for relative_path in expected_relative_files():
        rows = json.loads((SNAPSHOT_ROOT / relative_path).read_text(encoding="utf-8"))
        assert isinstance(rows, list)
        assert rows
        for row in rows:
            assert set(row) == {"question", "A", "B", "C", "D", "answer"}
            assert row["answer"] in {"A", "B", "C", "D"}


def test_manifest_generation_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    first = write_dataset_manifest(SNAPSHOT_ROOT, destination=tmp_path / "first.json")
    second = write_dataset_manifest(SNAPSHOT_ROOT, destination=tmp_path / "second.json")

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == (SNAPSHOT_ROOT / DATASET_MANIFEST_FILENAME).read_bytes()


def test_loader_verifies_manifest_and_exact_split_counts() -> None:
    loader = _loader()

    assert len(loader.load_split("dev")) == 45
    assert len(loader.load_split("test")) == 4009
    for category in CHEMBENCH4K_CATEGORIES:
        assert len(loader.load_category(category, split="dev")) == 5
        assert (
            len(loader.load_category(category, split="test"))
            == EXPECTED_CATEGORY_COUNTS[category]["test"]
        )


def test_uid_and_normalized_content_are_unique_across_dev_and_test() -> None:
    tasks = _loader().load_split("dev") + _loader().load_split("test")
    uids = [task.uid for task in tasks]
    normalized = [
        "\x1f".join(
            normalize_benchmark_text(getattr(task, field))
            for field in ("question", "A", "B", "C", "D")
        )
        for task in tasks
    ]

    assert len(uids) == len(set(uids)) == 4054
    assert len(normalized) == len(set(normalized)) == 4054
    assert {task.uid for task in _loader().load_split("dev")}.isdisjoint(
        task.uid for task in _loader().load_split("test")
    )


def test_private_to_public_projection_is_recursive_allowlist() -> None:
    private = _loader().load_category("Name_Conversion", split="test")[0]
    public = private.to_public()
    payload = public.to_public_dict()

    assert not hasattr(public, "target")
    assert not hasattr(public, "source_split")
    assert not hasattr(public, "source_index")
    assert not hasattr(public, "dataset_sha256")
    assert _private_key_names(payload) == set()
    assert set(payload) == {
        "uid",
        "category",
        "question",
        "A",
        "B",
        "C",
        "D",
        "dataset_revision",
    }
    assert "target" not in json.dumps(payload, ensure_ascii=False).casefold()


def test_loader_is_independent_of_legacy_chembench_adapter() -> None:
    module_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "openevo_chembench"
        / "chembench4k_dataset.py"
    ).read_text(encoding="utf-8")

    assert "from openevo_chembench.dataset import" not in module_source
    assert "jablonkagroup" not in module_source.casefold()


def test_wrong_or_missing_manifest_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="manifest"):
        ChemBench4KDatasetLoader(
            snapshot_root=SNAPSHOT_ROOT,
            manifest_path=tmp_path / "missing.json",
        )
