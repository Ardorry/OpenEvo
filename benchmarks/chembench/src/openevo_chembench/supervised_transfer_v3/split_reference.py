"""Read-only reference to the frozen v2 Train/Test selection."""

from __future__ import annotations

import json
from pathlib import Path

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES, CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v3.config import (
    PROTOCOL_ID,
    RESERVE_COUNT,
    SPLIT_REFERENCE_RELATIVE,
    TEST_COUNT,
    TRAIN_COUNT,
    V2_MANIFEST_ROOT,
)


def build_split_reference_receipt_v3(repository_root: Path) -> dict[str, object]:
    repository = repository_root.resolve(strict=True)
    root = (repository / V2_MANIFEST_ROOT).resolve(strict=True)
    train = _public_rows(root / "train_public_manifest.jsonl")
    test = _public_rows(root / "test_public_manifest.jsonl")
    reserve = _public_rows(root / "reserve_public_manifest.jsonl")
    if len(train) != TRAIN_COUNT or len(test) != TEST_COUNT or len(reserve) != RESERVE_COUNT:
        raise RuntimeError("V3_SOURCE_SPLIT_COUNT_INVALID")
    train_uids = tuple(str(row["uid"]) for row in train)
    test_uids = tuple(str(row["uid"]) for row in test)
    reserve_uids = tuple(str(row["uid"]) for row in reserve)
    if set(train_uids) & set(test_uids) or set(train_uids) & set(reserve_uids) or set(test_uids) & set(reserve_uids):
        raise RuntimeError("V3_SOURCE_SPLIT_INTERSECTION")
    per_category = {
        category: {
            "train": sum(row["category"] == category for row in train),
            "test": sum(row["category"] == category for row in test),
            "reserve": sum(row["category"] == category for row in reserve),
        }
        for category in CHEMBENCH4K_CATEGORIES
    }
    if any(values["train"] != 50 or values["test"] != 50 for values in per_category.values()):
        raise RuntimeError("V3_SOURCE_SPLIT_BALANCE_INVALID")
    source_receipt = root / "split_isolation_receipt_v2.json"
    source_summary = root / "split_summary.json"
    phase0 = json.loads((root / "phase0_audit_receipt_v2.json").read_text(encoding="utf-8"))
    if phase0.get("test_actual_exposure_count") != 0:
        raise RuntimeError("V3_SOURCE_TEST_EXPOSED")
    return {
        "schema_version": "SplitReferenceReceiptV3",
        "protocol_id": PROTOCOL_ID,
        "source_split_protocol": "chembench_supervised_transfer_v2",
        "source_split_receipt": sha256_bytes(source_receipt.read_bytes()),
        "source_split_summary": sha256_bytes(source_summary.read_bytes()),
        "dataset_revision": CHEMBENCH4K_REVISION,
        "train_count": len(train),
        "test_count": len(test),
        "reserve_count": len(reserve),
        "per_category_count": per_category,
        "test_historical_exposure": 0,
        "train_uid_sha256": sha256_bytes(canonical_json_bytes(train_uids)),
        "test_uid_sha256": sha256_bytes(canonical_json_bytes(test_uids)),
        "reserve_uid_sha256": sha256_bytes(canonical_json_bytes(reserve_uids)),
        "split_sha256": sha256_bytes(
            canonical_json_bytes({"train": train_uids, "test": test_uids, "reserve": reserve_uids})
        ),
        "source_files_sha256": {
            name: sha256_bytes((root / name).read_bytes())
            for name in (
                "train_public_manifest.jsonl",
                "train_private_manifest.jsonl",
                "test_public_manifest.jsonl",
                "test_private_manifest.jsonl",
                "reserve_public_manifest.jsonl",
                "split_summary.json",
                "split_isolation_receipt_v2.json",
            )
        },
    }


def write_split_reference_receipt_v3(repository_root: Path) -> dict[str, object]:
    repository = repository_root.resolve(strict=True)
    payload = build_split_reference_receipt_v3(repository)
    path = repository / SPLIT_REFERENCE_RELATIVE
    write_public_file(path, canonical_pretty_json_bytes(payload))
    return {"status": "PASS", "path": SPLIT_REFERENCE_RELATIVE, "sha256": sha256_bytes(path.read_bytes()), **payload}


def verify_split_reference_receipt_v3(repository_root: Path) -> dict[str, object]:
    repository = repository_root.resolve(strict=True)
    path = repository / SPLIT_REFERENCE_RELATIVE
    encoded = path.read_bytes()
    expected = build_split_reference_receipt_v3(repository)
    if json.loads(encoded) != expected or encoded != canonical_pretty_json_bytes(expected):
        raise RuntimeError("V3_SPLIT_REFERENCE_DRIFT")
    for name in ("train_private_manifest.jsonl", "test_private_manifest.jsonl"):
        if (repository / V2_MANIFEST_ROOT / name).stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("V3_PRIVATE_MANIFEST_MODE_INVALID")
    return {"status": "PASS", "sha256": sha256_bytes(encoded), **expected}


def _public_rows(path: Path) -> tuple[dict[str, object], ...]:
    rows = tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    if any(type(row) is not dict or "uid" not in row or "category" not in row for row in rows):
        raise RuntimeError("V3_SOURCE_PUBLIC_MANIFEST_INVALID")
    if len({row["uid"] for row in rows}) != len(rows):
        raise RuntimeError("V3_SOURCE_PUBLIC_MANIFEST_DUPLICATE")
    return rows


__all__ = [
    "build_split_reference_receipt_v3",
    "verify_split_reference_receipt_v3",
    "write_split_reference_receipt_v3",
]
