"""Pinned local snapshot loader and deterministic manifest for ChemBench4K."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REPOSITORY,
    CHEMBENCH4K_REVISION,
    CHOICE_LABELS,
    PrivateChemBench4KTask,
)


DATASET_MANIFEST_FILENAME = "chembench4k_dataset_manifest_v2.json"
DATASET_MANIFEST_SCHEMA = "chembench4k_dataset_manifest_v2"
_EXPECTED_FIELDS = frozenset({"question", "A", "B", "C", "D", "answer"})


class ChemBench4KIntegrityError(RuntimeError):
    """A fail-closed snapshot error that never includes dataset row content."""


@dataclass(frozen=True, slots=True)
class ChemBench4KDatasetManifest:
    """Verified immutable identity of the complete local dataset snapshot."""

    repository: str
    revision: str
    files: tuple[str, ...]
    dev_count: int
    test_count: int
    category_counts: dict[str, dict[str, int]]
    file_sha256: dict[str, str]
    combined_sha256: str
    schema_version: str
    normalized_uid_count: int
    normalized_content_duplicate_count: int

    @classmethod
    def from_payload(cls, payload: object) -> ChemBench4KDatasetManifest:
        if not isinstance(payload, dict):
            raise ChemBench4KIntegrityError("dataset manifest root must be an object")
        try:
            manifest = cls(
                repository=payload["repository"],
                revision=payload["revision"],
                files=tuple(payload["files"]),
                dev_count=payload["dev_count"],
                test_count=payload["test_count"],
                category_counts=payload["category_counts"],
                file_sha256=payload["file_sha256"],
                combined_sha256=payload["combined_sha256"],
                schema_version=payload["schema_version"],
                normalized_uid_count=payload["normalized_uid_count"],
                normalized_content_duplicate_count=payload["normalized_content_duplicate_count"],
            )
        except (KeyError, TypeError) as exc:
            raise ChemBench4KIntegrityError("dataset manifest schema is invalid") from exc
        manifest.validate_identity()
        return manifest

    def validate_identity(self) -> None:
        if self.repository != CHEMBENCH4K_REPOSITORY:
            raise ChemBench4KIntegrityError("dataset repository identity mismatch")
        if self.revision != CHEMBENCH4K_REVISION:
            raise ChemBench4KIntegrityError("dataset revision identity mismatch")
        if self.schema_version != DATASET_MANIFEST_SCHEMA:
            raise ChemBench4KIntegrityError("dataset manifest schema version mismatch")
        if len(self.files) != 18 or len(set(self.files)) != 18:
            raise ChemBench4KIntegrityError("dataset manifest must bind exactly 18 files")
        if self.normalized_uid_count != self.dev_count + self.test_count:
            raise ChemBench4KIntegrityError("dataset UID count does not match row count")
        if self.normalized_content_duplicate_count != 0:
            raise ChemBench4KIntegrityError("normalized dataset content is duplicated")
        if len(self.combined_sha256) != 64:
            raise ChemBench4KIntegrityError("combined dataset digest is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "revision": self.revision,
            "files": list(self.files),
            "dev_count": self.dev_count,
            "test_count": self.test_count,
            "category_counts": self.category_counts,
            "file_sha256": self.file_sha256,
            "combined_sha256": self.combined_sha256,
            "schema_version": self.schema_version,
            "normalized_uid_count": self.normalized_uid_count,
            "normalized_content_duplicate_count": self.normalized_content_duplicate_count,
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_payload(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")


def expected_relative_files() -> tuple[str, ...]:
    """Return the closed 18-file snapshot allowlist in deterministic order."""

    return tuple(
        f"{split}/{category}_benchmark.json"
        for split in ("dev", "test")
        for category in sorted(CHEMBENCH4K_CATEGORIES)
    )


def normalize_benchmark_text(value: object) -> str:
    """Normalize one question/choice solely for hashing and duplicate checks."""

    text = _display_text(value)
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def compute_task_uid(
    *,
    category: str,
    source_index: int,
    question: object,
    A: object,
    B: object,
    C: object,
    D: object,
) -> str:
    """Compute the protocol UID without including target or split."""

    if category not in CHEMBENCH4K_CATEGORIES:
        raise ValueError("unsupported ChemBench4K category")
    if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
        raise ValueError("source_index must be a non-negative integer")
    components = (
        CHEMBENCH4K_REVISION,
        category,
        str(source_index),
        normalize_benchmark_text(question),
        normalize_benchmark_text(A),
        normalize_benchmark_text(B),
        normalize_benchmark_text(C),
        normalize_benchmark_text(D),
    )
    return hashlib.sha256("\x1f".join(components).encode("utf-8")).hexdigest()


def build_dataset_manifest(snapshot_root: Path) -> ChemBench4KDatasetManifest:
    """Validate all rows and compute the complete deterministic snapshot identity."""

    snapshot_root = _require_snapshot_root(snapshot_root)
    expected_files = expected_relative_files()
    actual_json_files = tuple(
        sorted(
            path.relative_to(snapshot_root).as_posix()
            for split in ("dev", "test")
            for path in (snapshot_root / split).glob("*.json")
        )
    )
    if actual_json_files != expected_files:
        raise ChemBench4KIntegrityError("snapshot JSON file allowlist mismatch")

    file_sha256: dict[str, str] = {}
    category_counts: dict[str, dict[str, int]] = {
        category: {"dev": 0, "test": 0} for category in CHEMBENCH4K_CATEGORIES
    }
    seen_uids: dict[str, tuple[str, str, int]] = {}
    seen_contents: dict[str, tuple[str, str, int]] = {}
    content_duplicate_count = 0

    for relative_path in expected_files:
        path = snapshot_root / relative_path
        file_sha256[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
        split, filename = relative_path.split("/", maxsplit=1)
        category = filename.removesuffix("_benchmark.json")
        rows = _read_rows(path, split=split, category=category)
        category_counts[category][split] = len(rows)
        for source_index, row in enumerate(rows):
            _validate_row(row, split=split, category=category, source_index=source_index)
            uid = _uid_from_row(row, category=category, source_index=source_index)
            location = (split, category, source_index)
            if uid in seen_uids:
                raise ChemBench4KIntegrityError("normalized task UID collision detected")
            seen_uids[uid] = location

            normalized_content = _normalized_row_content(row)
            if normalized_content in seen_contents:
                content_duplicate_count += 1
            else:
                seen_contents[normalized_content] = location

    dev_count = sum(counts["dev"] for counts in category_counts.values())
    test_count = sum(counts["test"] for counts in category_counts.values())
    combined_sha256 = _combined_dataset_hash(
        files=expected_files,
        file_sha256=file_sha256,
    )
    manifest = ChemBench4KDatasetManifest(
        repository=CHEMBENCH4K_REPOSITORY,
        revision=CHEMBENCH4K_REVISION,
        files=expected_files,
        dev_count=dev_count,
        test_count=test_count,
        category_counts=category_counts,
        file_sha256=file_sha256,
        combined_sha256=combined_sha256,
        schema_version=DATASET_MANIFEST_SCHEMA,
        normalized_uid_count=len(seen_uids),
        normalized_content_duplicate_count=content_duplicate_count,
    )
    manifest.validate_identity()
    return manifest


def write_dataset_manifest(
    snapshot_root: Path,
    *,
    destination: Path | None = None,
) -> Path:
    """Write canonical manifest bytes atomically and return the destination."""

    snapshot_root = _require_snapshot_root(snapshot_root)
    manifest = build_dataset_manifest(snapshot_root)
    output = destination if destination is not None else snapshot_root / DATASET_MANIFEST_FILENAME
    if not isinstance(output, Path):
        raise TypeError("destination must be pathlib.Path")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_bytes(manifest.canonical_bytes())
    temporary.replace(output)
    return output


class ChemBench4KDatasetLoader:
    """Load only a locally verified AI4Chem/ChemBench4K frozen snapshot."""

    __slots__ = ("_manifest", "_snapshot_root")

    def __init__(self, *, snapshot_root: Path, manifest_path: Path | None = None) -> None:
        self._snapshot_root = _require_snapshot_root(snapshot_root)
        selected_manifest = (
            manifest_path
            if manifest_path is not None
            else self._snapshot_root / DATASET_MANIFEST_FILENAME
        )
        if not isinstance(selected_manifest, Path) or not selected_manifest.is_file():
            raise ChemBench4KIntegrityError("frozen dataset manifest is missing")
        try:
            payload = json.loads(selected_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChemBench4KIntegrityError("frozen dataset manifest is unreadable") from exc
        self._manifest = ChemBench4KDatasetManifest.from_payload(payload)
        recomputed = build_dataset_manifest(self._snapshot_root)
        if recomputed.canonical_bytes() != self._manifest.canonical_bytes():
            raise ChemBench4KIntegrityError("dataset files do not match frozen manifest")

    @property
    def manifest(self) -> ChemBench4KDatasetManifest:
        return self._manifest

    def load_category(
        self,
        category: str,
        *,
        split: Literal["dev", "test"],
    ) -> tuple[PrivateChemBench4KTask, ...]:
        if category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("unsupported ChemBench4K category")
        if split not in {"dev", "test"}:
            raise ValueError("split must be dev or test")
        path = self._snapshot_root / split / f"{category}_benchmark.json"
        rows = _read_rows(path, split=split, category=category)
        return tuple(
            _row_to_task(
                row,
                category=category,
                split=split,
                source_index=source_index,
                dataset_sha256=self._manifest.combined_sha256,
            )
            for source_index, row in enumerate(rows)
        )

    def load_split(
        self,
        split: Literal["dev", "test"],
    ) -> tuple[PrivateChemBench4KTask, ...]:
        if split not in {"dev", "test"}:
            raise ValueError("split must be dev or test")
        return tuple(
            task
            for category in CHEMBENCH4K_CATEGORIES
            for task in self.load_category(category, split=split)
        )


def _require_snapshot_root(snapshot_root: Path) -> Path:
    if not isinstance(snapshot_root, Path):
        raise TypeError("snapshot_root must be pathlib.Path")
    if not snapshot_root.is_dir():
        raise ChemBench4KIntegrityError("dataset snapshot root is missing")
    if snapshot_root.name != CHEMBENCH4K_REVISION:
        raise ChemBench4KIntegrityError("dataset snapshot path is not revision-pinned")
    return snapshot_root


def _read_rows(path: Path, *, split: str, category: str) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChemBench4KIntegrityError(
            f"dataset file is unreadable: split={split} category={category}"
        ) from exc
    if not isinstance(payload, list):
        raise ChemBench4KIntegrityError(
            f"dataset file root is not a list: split={split} category={category}"
        )
    if not payload:
        raise ChemBench4KIntegrityError(
            f"dataset file is empty: split={split} category={category}"
        )
    if not all(isinstance(row, dict) for row in payload):
        raise ChemBench4KIntegrityError(
            f"dataset file contains a non-object row: split={split} category={category}"
        )
    return payload


def _validate_row(
    row: dict[str, object],
    *,
    split: str,
    category: str,
    source_index: int,
) -> None:
    if set(row) != _EXPECTED_FIELDS:
        raise ChemBench4KIntegrityError(
            "dataset row schema mismatch: "
            f"split={split} category={category} source_index={source_index}"
        )
    if type(row["question"]) is not str or not row["question"].strip():
        raise ChemBench4KIntegrityError(
            "dataset question is invalid: "
            f"split={split} category={category} source_index={source_index}"
        )
    for label in CHOICE_LABELS:
        try:
            _display_text(row[label])
        except (TypeError, ValueError) as exc:
            raise ChemBench4KIntegrityError(
                "dataset option is invalid: "
                f"split={split} category={category} source_index={source_index}"
            ) from exc
    if type(row["answer"]) is not str or row["answer"] not in CHOICE_LABELS:
        raise ChemBench4KIntegrityError(
            "dataset answer is invalid: "
            f"split={split} category={category} source_index={source_index}"
        )


def _display_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("question and choices must be strings or finite numbers")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("question and choices must contain finite numbers")
    return str(value)


def _uid_from_row(
    row: dict[str, object],
    *,
    category: str,
    source_index: int,
) -> str:
    return compute_task_uid(
        category=category,
        source_index=source_index,
        question=row["question"],
        A=row["A"],
        B=row["B"],
        C=row["C"],
        D=row["D"],
    )


def _normalized_row_content(row: dict[str, object]) -> str:
    return "\x1f".join(
        normalize_benchmark_text(row[field]) for field in ("question", "A", "B", "C", "D")
    )


def _row_to_task(
    row: dict[str, object],
    *,
    category: str,
    split: Literal["dev", "test"],
    source_index: int,
    dataset_sha256: str,
) -> PrivateChemBench4KTask:
    _validate_row(row, split=split, category=category, source_index=source_index)
    return PrivateChemBench4KTask(
        uid=_uid_from_row(row, category=category, source_index=source_index),
        category=category,
        source_split=split,
        source_index=source_index,
        question=_display_text(row["question"]),
        A=_display_text(row["A"]),
        B=_display_text(row["B"]),
        C=_display_text(row["C"]),
        D=_display_text(row["D"]),
        target=row["answer"],
        dataset_revision=CHEMBENCH4K_REVISION,
        dataset_sha256=dataset_sha256,
    )


def _combined_dataset_hash(
    *,
    files: tuple[str, ...],
    file_sha256: dict[str, str],
) -> str:
    identity = {
        "repository": CHEMBENCH4K_REPOSITORY,
        "revision": CHEMBENCH4K_REVISION,
        "files": [{"path": path, "sha256": file_sha256[path]} for path in files],
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "DATASET_MANIFEST_FILENAME",
    "DATASET_MANIFEST_SCHEMA",
    "ChemBench4KDatasetLoader",
    "ChemBench4KDatasetManifest",
    "ChemBench4KIntegrityError",
    "build_dataset_manifest",
    "compute_task_uid",
    "expected_relative_files",
    "normalize_benchmark_text",
    "write_dataset_manifest",
]
