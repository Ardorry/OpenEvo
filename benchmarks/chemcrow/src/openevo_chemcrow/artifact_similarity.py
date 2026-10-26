from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256, file_sha256

ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
ARTIFACT_TYPE_SHORT = {
    "text_memory": "M",
    "skill_bundle": "K",
    "agent_system": "A",
}
SEMANTIC_FLAGS = (
    "TASK_CONFLICT",
    "CONSTRAINT_AMPLIFICATION",
    "UNSUPPORTED_GENERALIZATION",
    "INSTRUCTION_OVERLOAD",
    "POSITIVE_CORRECTION",
    "EVIDENCE_DISCIPLINE",
)
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
OUTPUT_SCHEMA_VERSION = "chemcrow_artifact_similarity_v3"
SOURCE_SCHEMA_VERSION = "chemcrow_core_native_post_generation_semantic_audit_v1"
DEFAULT_MIN_SEMANTIC_UNIT_WORDS = 6
DEFAULT_GENERAL_MATCH_THRESHOLD = 0.8
DEFAULT_GENERAL_MIN_OTHER_TASK_FRACTION = 0.4


@dataclass(frozen=True)
class ArtifactRecord:
    index: int
    label: str
    task_id: str
    artifact_type: str
    artifact_id: str
    job_id: str
    reflector_run_id: str
    content_sha256: str
    content_bytes: int
    content: str
    semantic_flags: dict[str, bool]

    @property
    def word_count(self) -> int:
        return len(self.content.split())

    def index_row(
        self,
        *,
        chunk_count: int,
        semantic_unit_count: int | None = None,
    ) -> dict[str, Any]:
        row = {
            "index": self.index,
            "label": self.label,
            "task_id": self.task_id,
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "job_id": self.job_id,
            "reflector_run_id": self.reflector_run_id,
            "content_sha256": self.content_sha256,
            "content_bytes": self.content_bytes,
            "word_count": self.word_count,
            "embedding_chunk_count": chunk_count,
            **{flag: self.semantic_flags[flag] for flag in SEMANTIC_FLAGS},
        }
        if semantic_unit_count is not None:
            row["semantic_unit_count"] = semantic_unit_count
        return row


@dataclass(frozen=True)
class TextChunk:
    text: str
    word_count: int


@dataclass(frozen=True)
class SemanticUnitSimilarityResult:
    combined_similarity: Any
    raw_similarity: Any
    general_similarity: Any
    domain_similarity: Any
    metadata: dict[str, Any]
    unit_counts: tuple[int, ...]
    artifact_composition: tuple[dict[str, Any], ...]
    unit_inventory: tuple[dict[str, Any], ...]


_MARKDOWN_LIST_PREFIX = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")
_METHOD_LABEL_PREFIX = re.compile(
    r"^(?:\*\*)?(?:trigger|action|validation(?: check)?)\s*:(?:\*\*)?\s*",
    flags=re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return value


def _task_sort_key(task_id: str) -> tuple[int, str]:
    match = re.fullmatch(r"chemcrow-(\d+)", task_id)
    _require(match is not None, f"unexpected task ID: {task_id}")
    return int(match.group(1)), task_id


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_artifacts(
    source_path: Path,
    *,
    expected_artifact_count: int | None = 42,
) -> tuple[tuple[ArtifactRecord, ...], dict[str, Any]]:
    """Load exact native artifact text from the sealed post-generation audit."""

    source_path = source_path.resolve()
    _require(source_path.is_file(), f"artifact source does not exist: {source_path}")
    payload = _load_json(source_path)
    _require(
        payload.get("status") == "POST_GENERATION_AUDIT_COMPLETE",
        "artifact source is not a completed post-generation audit",
    )
    _require(
        payload.get("created_after_all_pairs_sealed") is True,
        "artifact source was not created after pair sealing",
    )
    _require(
        payload.get("fed_to_candidate_or_reflector") is False,
        "artifact source was fed into the scientific run",
    )
    tasks = payload.get("tasks")
    _require(isinstance(tasks, dict) and tasks, "artifact source task map is absent")

    records: list[ArtifactRecord] = []
    artifact_ids: set[str] = set()
    job_ids: set[str] = set()
    for task_id in sorted(tasks, key=_task_sort_key):
        task = tasks[task_id]
        _require(isinstance(task, dict), f"task audit is not an object: {task_id}")
        findings = task.get("artifact_findings")
        _require(isinstance(findings, dict), f"artifact findings are absent: {task_id}")
        _require(
            set(findings) == set(ARTIFACT_TYPES),
            f"artifact type inventory differs: {task_id}",
        )
        for artifact_type in ARTIFACT_TYPES:
            finding = findings[artifact_type]
            _require(isinstance(finding, dict), f"artifact finding is invalid: {task_id}")
            _require(
                finding.get("artifact_type") == artifact_type,
                f"artifact type binding differs: {task_id}/{artifact_type}",
            )
            _require(
                finding.get("exact_native_content_preserved") is True,
                f"native content is not exact: {task_id}/{artifact_type}",
            )
            content = finding.get("content")
            _require(
                isinstance(content, str) and content.strip(),
                f"artifact content is empty: {task_id}/{artifact_type}",
            )
            content_sha256 = _content_sha256(content)
            _require(
                content_sha256 == finding.get("content_sha256"),
                f"artifact content hash differs: {task_id}/{artifact_type}",
            )
            _require(
                content_sha256 == finding.get("registered_artifact_hash"),
                f"registered artifact hash differs: {task_id}/{artifact_type}",
            )
            content_bytes = len(content.encode("utf-8"))
            _require(
                content_bytes == finding.get("content_bytes"),
                f"artifact content byte count differs: {task_id}/{artifact_type}",
            )
            artifact_id = finding.get("artifact_id")
            job_id = finding.get("job_id")
            reflector_run_id = finding.get("reflector_run_id")
            _require(isinstance(artifact_id, str) and artifact_id, "artifact ID is absent")
            _require(isinstance(job_id, str) and job_id, "Reflector job ID is absent")
            _require(
                isinstance(reflector_run_id, str) and reflector_run_id,
                "Reflector run ID is absent",
            )
            _require(artifact_id not in artifact_ids, f"duplicate artifact ID: {artifact_id}")
            _require(job_id not in job_ids, f"duplicate Reflector job ID: {job_id}")
            artifact_ids.add(artifact_id)
            job_ids.add(job_id)
            flags = {flag: finding.get(flag) for flag in SEMANTIC_FLAGS}
            _require(
                all(isinstance(value, bool) for value in flags.values()),
                f"semantic flag inventory differs: {task_id}/{artifact_type}",
            )
            task_number = _task_sort_key(task_id)[0]
            records.append(
                ArtifactRecord(
                    index=len(records),
                    label=f"{task_number:02d}:{ARTIFACT_TYPE_SHORT[artifact_type]}",
                    task_id=task_id,
                    artifact_type=artifact_type,
                    artifact_id=artifact_id,
                    job_id=job_id,
                    reflector_run_id=reflector_run_id,
                    content_sha256=content_sha256,
                    content_bytes=content_bytes,
                    content=content,
                    semantic_flags=flags,
                )
            )

    if expected_artifact_count is not None:
        _require(
            len(records) == expected_artifact_count,
            f"expected {expected_artifact_count} artifacts, found {len(records)}",
        )
    _require(
        payload.get("artifact_count") == len(records),
        "source artifact count differs from extracted artifacts",
    )
    _require(
        payload.get("unique_artifact_count") == len(artifact_ids),
        "source unique artifact count differs",
    )
    _require(
        payload.get("unique_reflector_job_count") == len(job_ids),
        "source unique Reflector job count differs",
    )
    return tuple(records), payload


def chunk_text(
    text: str,
    *,
    words_per_chunk: int = 180,
    overlap_words: int = 30,
) -> tuple[TextChunk, ...]:
    """Split long artifacts into deterministic whitespace-token chunks."""

    _require(words_per_chunk >= 32, "words_per_chunk must be at least 32")
    _require(0 <= overlap_words < words_per_chunk, "chunk overlap is invalid")
    words = text.split()
    _require(bool(words), "cannot chunk empty artifact text")
    chunks: list[TextChunk] = []
    step = words_per_chunk - overlap_words
    start = 0
    while start < len(words):
        current = words[start : start + words_per_chunk]
        chunks.append(TextChunk(text=" ".join(current), word_count=len(current)))
        if start + words_per_chunk >= len(words):
            break
        start += step
    return tuple(chunks)


def extract_semantic_units(
    text: str,
    *,
    min_words: int = DEFAULT_MIN_SEMANTIC_UNIT_WORDS,
) -> tuple[TextChunk, ...]:
    """Extract content-bearing propositions while discarding Markdown presentation order."""

    _require(min_words >= 3, "semantic-unit minimum must be at least three words")
    units: list[TextChunk] = []
    in_code_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence or not line or line.startswith("#"):
            continue
        line = _MARKDOWN_LIST_PREFIX.sub("", line)
        line = _METHOD_LABEL_PREFIX.sub("", line)
        line = re.sub(r"^>\s*", "", line)
        line = line.replace("`", "")
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip(" -")
        for sentence in _SENTENCE_BOUNDARY.split(line):
            normalized = re.sub(r"\s+", " ", sentence).strip(" -")
            word_count = len(normalized.split())
            if word_count >= min_words:
                units.append(TextChunk(text=normalized, word_count=word_count))
    _require(bool(units), "artifact has no content-bearing semantic units")
    return tuple(units)


def _symmetric_best_match_coverage(similarity: Any) -> float:
    """Order-invariant, bidirectional semantic-unit coverage (BERTScore-style F1)."""

    import numpy as np

    values = np.asarray(similarity, dtype=np.float64)
    _require(values.ndim == 2 and all(size > 0 for size in values.shape), "unit matrix is empty")
    left_coverage = float(np.mean(np.max(values, axis=1)))
    right_coverage = float(np.mean(np.max(values, axis=0)))
    if left_coverage + right_coverage <= 0.0:
        return 0.0
    return 2.0 * left_coverage * right_coverage / (left_coverage + right_coverage)


def classify_semantic_unit_generality(
    calibrated_unit_similarity: Any,
    task_ids: Sequence[str],
    *,
    match_threshold: float = DEFAULT_GENERAL_MATCH_THRESHOLD,
    minimum_other_task_fraction: float = DEFAULT_GENERAL_MIN_OTHER_TASK_FRACTION,
) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Classify units by semantic document frequency across *other* tasks.

    A unit counts as present in another task when its strongest match to any unit in that
    task reaches ``match_threshold``. General units must be present in at least the configured
    fraction of other tasks; the per-unit match fraction is also retained as a continuous
    generality score.
    """

    import numpy as np

    similarity = np.asarray(calibrated_unit_similarity, dtype=np.float64)
    _require(
        similarity.shape == (len(task_ids), len(task_ids)),
        "unit similarity matrix and task inventory differ",
    )
    _require(0.0 <= match_threshold <= 1.0, "general match threshold is outside [0, 1]")
    _require(
        0.0 < minimum_other_task_fraction <= 1.0,
        "minimum other-task fraction is outside (0, 1]",
    )
    unique_tasks = sorted(set(task_ids), key=_task_sort_key)
    _require(len(unique_tasks) >= 2, "generality classification requires multiple tasks")
    task_id_array = np.asarray(task_ids, dtype=object)
    task_indices = {
        task_id: np.flatnonzero(task_id_array == task_id) for task_id in unique_tasks
    }
    other_task_count = len(unique_tasks) - 1
    minimum_match_count = math.ceil(minimum_other_task_fraction * other_task_count)
    match_counts = np.zeros(len(task_ids), dtype=np.int64)
    for unit_index, own_task in enumerate(task_ids):
        match_counts[unit_index] = sum(
            float(np.max(similarity[unit_index, task_indices[other_task]])) >= match_threshold
            for other_task in unique_tasks
            if other_task != own_task
        )
    match_fractions = match_counts.astype(np.float64) / other_task_count
    general_mask = match_counts >= minimum_match_count
    return (
        match_counts,
        match_fractions,
        general_mask,
        {
            "definition": (
                "General iff a semantic unit has a calibrated match at or above the threshold "
                "in at least the minimum number of other tasks; Domain is the complement"
            ),
            "match_scope": "maximum over all semantic units in each other task",
            "match_threshold": match_threshold,
            "minimum_other_task_fraction": minimum_other_task_fraction,
            "task_count": len(unique_tasks),
            "other_task_count": other_task_count,
            "minimum_other_task_match_count": minimum_match_count,
        },
    )


def compute_tfidf_similarity(
    records: Sequence[ArtifactRecord],
) -> tuple[Any, dict[str, Any]]:
    """Compute deterministic lexical cosine similarity over unigram/bigram TF-IDF."""

    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer

    _require(len(records) >= 2, "at least two artifacts are required")
    vectorizer = TfidfVectorizer(
        lowercase=True,
        strip_accents="unicode",
        ngram_range=(1, 2),
        min_df=1,
        max_df=1.0,
        sublinear_tf=True,
        norm="l2",
        token_pattern=r"(?u)\b[\w'-]{2,}\b",
    )
    features = vectorizer.fit_transform(record.content for record in records)
    similarity = (features @ features.T).toarray().astype(np.float64)
    similarity = np.clip(similarity, -1.0, 1.0)
    np.fill_diagonal(similarity, 1.0)
    return similarity, {
        "implementation": "sklearn.feature_extraction.text.TfidfVectorizer",
        "scikit_learn_version": importlib.metadata.version("scikit-learn"),
        "feature_count": len(vectorizer.vocabulary_),
        "lowercase": True,
        "strip_accents": "unicode",
        "ngram_range": [1, 2],
        "sublinear_tf": True,
        "stop_words": None,
        "norm": "l2",
    }


def _l2_normalize(vector: Any) -> Any:
    import numpy as np

    norm = float(np.linalg.norm(vector))
    _require(math.isfinite(norm) and norm > 0.0, "embedding vector has invalid norm")
    return vector / norm


def _tree_manifest(root: Path) -> dict[str, Any]:
    _require(root.is_dir(), f"model snapshot directory is absent: {root}")
    model_cache_root = root.parent.parent.resolve()
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        _require(
            resolved.is_relative_to(model_cache_root),
            f"model snapshot link escapes its model cache: {path}",
        )
        _require(resolved.is_file(), f"model snapshot entry is not a file: {path}")
        size = resolved.stat().st_size
        total_bytes += size
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": size,
                "sha256": file_sha256(resolved),
            }
        )
    _require(bool(entries), f"model snapshot has no regular files: {root}")
    return {
        "snapshot_path": str(root),
        "resolved_revision": root.name,
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "manifest_sha256": canonical_sha256(entries),
    }


def compute_embedding_similarity(
    records: Sequence[ArtifactRecord],
    *,
    model_name: str,
    cache_dir: Path,
    words_per_chunk: int = 180,
    overlap_words: int = 30,
    batch_size: int = 16,
    threads: int = 1,
) -> tuple[Any, dict[str, Any], tuple[int, ...]]:
    """Compute semantic cosine similarity with a pretrained local ONNX encoder."""

    import numpy as np
    from fastembed import TextEmbedding

    _require(len(records) >= 2, "at least two artifacts are required")
    _require(batch_size >= 1, "batch_size must be positive")
    _require(threads >= 1, "threads must be positive")
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(
        model_name=model_name,
        cache_dir=str(cache_dir),
        threads=threads,
        cuda=False,
    )

    all_chunks: list[str] = []
    owners: list[int] = []
    weights: list[int] = []
    chunk_counts: list[int] = []
    for record_index, record in enumerate(records):
        chunks = chunk_text(
            record.content,
            words_per_chunk=words_per_chunk,
            overlap_words=overlap_words,
        )
        chunk_counts.append(len(chunks))
        for chunk in chunks:
            all_chunks.append(chunk.text)
            owners.append(record_index)
            weights.append(chunk.word_count)

    chunk_vectors = np.asarray(
        list(model.embed(all_chunks, batch_size=batch_size)),
        dtype=np.float64,
    )
    _require(
        chunk_vectors.ndim == 2 and chunk_vectors.shape[0] == len(all_chunks),
        "embedding backend returned the wrong chunk inventory",
    )
    artifact_vectors = np.zeros((len(records), chunk_vectors.shape[1]), dtype=np.float64)
    artifact_weights = np.zeros(len(records), dtype=np.float64)
    for owner, weight, vector in zip(owners, weights, chunk_vectors, strict=True):
        artifact_vectors[owner] += _l2_normalize(vector) * weight
        artifact_weights[owner] += weight
    for index in range(len(records)):
        _require(artifact_weights[index] > 0.0, "artifact has no embedding chunks")
        artifact_vectors[index] = _l2_normalize(artifact_vectors[index] / artifact_weights[index])
    similarity = np.clip(artifact_vectors @ artifact_vectors.T, -1.0, 1.0)
    np.fill_diagonal(similarity, 1.0)

    model_dir = getattr(getattr(model, "model", None), "_model_dir", None)
    _require(model_dir is not None, "fastembed did not expose its resolved model snapshot")
    model_manifest = _tree_manifest(Path(model_dir).resolve())
    return (
        similarity,
        {
            "implementation": "fastembed.TextEmbedding",
            "fastembed_version": importlib.metadata.version("fastembed"),
            "onnxruntime_version": importlib.metadata.version("onnxruntime"),
            "model_name": model_name,
            "embedding_dimension": int(artifact_vectors.shape[1]),
            "device": "CPU",
            "threads": threads,
            "batch_size": batch_size,
            "chunking": {
                "unit": "whitespace_words",
                "words_per_chunk": words_per_chunk,
                "overlap_words": overlap_words,
                "aggregation": "word-count-weighted mean of normalized chunk vectors, then L2",
                "total_chunk_count": len(all_chunks),
            },
            "model_snapshot": model_manifest,
        },
        tuple(chunk_counts),
    )


def compute_semantic_unit_similarity(
    records: Sequence[ArtifactRecord],
    *,
    model_name: str,
    cache_dir: Path,
    min_words: int = DEFAULT_MIN_SEMANTIC_UNIT_WORDS,
    calibration_median_quantile: float = 0.5,
    calibration_cap_quantile: float = 0.99,
    general_match_threshold: float = DEFAULT_GENERAL_MATCH_THRESHOLD,
    general_min_other_task_fraction: float = DEFAULT_GENERAL_MIN_OTHER_TASK_FRACTION,
    batch_size: int = 16,
    threads: int = 1,
) -> SemanticUnitSimilarityResult:
    """Match proposition sets without depending on sentence or bullet order.

    The raw matrix is symmetric best-match coverage over unit cosine values. The main matrix
    first removes the corpus background cosine floor and caps at the observed high-match
    quantile, which makes generic embedding-space similarity less visually dominant.
    """

    import numpy as np
    from fastembed import TextEmbedding

    _require(len(records) >= 2, "at least two artifacts are required")
    _require(batch_size >= 1, "batch_size must be positive")
    _require(threads >= 1, "threads must be positive")
    _require(
        0.0 < calibration_median_quantile < calibration_cap_quantile < 1.0,
        "semantic-unit calibration quantiles are invalid",
    )
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(
        model_name=model_name,
        cache_dir=str(cache_dir),
        threads=threads,
        cuda=False,
    )

    units_by_artifact = [
        extract_semantic_units(record.content, min_words=min_words) for record in records
    ]
    unit_counts = tuple(len(units) for units in units_by_artifact)
    all_units = [unit.text for units in units_by_artifact for unit in units]
    owners = [
        artifact_index for artifact_index, units in enumerate(units_by_artifact) for _ in units
    ]
    vectors = np.asarray(
        list(model.embed(all_units, batch_size=batch_size)),
        dtype=np.float64,
    )
    _require(
        vectors.ndim == 2 and vectors.shape[0] == len(all_units),
        "embedding backend returned the wrong semantic-unit inventory",
    )
    vectors = np.asarray([_l2_normalize(vector) for vector in vectors], dtype=np.float64)
    owner_array = np.asarray(owners, dtype=np.int64)
    vector_indices = [np.flatnonzero(owner_array == index) for index in range(len(records))]

    background_values: list[Any] = []
    raw_pair_matrices: dict[tuple[int, int], Any] = {}
    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            pair_matrix = vectors[vector_indices[left]] @ vectors[vector_indices[right]].T
            raw_pair_matrices[(left, right)] = pair_matrix
            background_values.append(pair_matrix.ravel())
    background = np.concatenate(background_values)
    background_floor = float(np.quantile(background, calibration_median_quantile))
    high_match_cap = float(np.quantile(background, calibration_cap_quantile))
    _require(
        math.isfinite(background_floor)
        and math.isfinite(high_match_cap)
        and high_match_cap > background_floor,
        "semantic-unit calibration distribution is invalid",
    )

    all_unit_cosines = np.clip(vectors @ vectors.T, -1.0, 1.0)
    calibrated_unit_similarity = np.clip(
        (all_unit_cosines - background_floor) / (high_match_cap - background_floor),
        0.0,
        1.0,
    )
    unit_task_ids = [records[owner].task_id for owner in owners]
    (
        other_task_match_counts,
        other_task_match_fractions,
        general_mask,
        generality_metadata,
    ) = classify_semantic_unit_generality(
        calibrated_unit_similarity,
        unit_task_ids,
        match_threshold=general_match_threshold,
        minimum_other_task_fraction=general_min_other_task_fraction,
    )
    domain_mask = np.logical_not(general_mask)

    raw_similarity = np.eye(len(records), dtype=np.float64)
    calibrated_similarity = np.eye(len(records), dtype=np.float64)
    general_similarity = np.eye(len(records), dtype=np.float64)
    domain_similarity = np.eye(len(records), dtype=np.float64)
    for (left, right), pair_matrix in raw_pair_matrices.items():
        raw_score = _symmetric_best_match_coverage(pair_matrix)
        calibrated = np.clip(
            (pair_matrix - background_floor) / (high_match_cap - background_floor),
            0.0,
            1.0,
        )
        calibrated_score = _symmetric_best_match_coverage(calibrated)
        raw_similarity[left, right] = raw_similarity[right, left] = raw_score
        calibrated_similarity[left, right] = calibrated_similarity[right, left] = calibrated_score

        left_indices = vector_indices[left]
        right_indices = vector_indices[right]
        general_pair = calibrated[np.ix_(general_mask[left_indices], general_mask[right_indices])]
        domain_pair = calibrated[np.ix_(domain_mask[left_indices], domain_mask[right_indices])]
        general_score = _symmetric_best_match_coverage(general_pair)
        domain_score = _symmetric_best_match_coverage(domain_pair)
        general_similarity[left, right] = general_similarity[right, left] = general_score
        domain_similarity[left, right] = domain_similarity[right, left] = domain_score

    unit_inventory: list[dict[str, Any]] = []
    artifact_composition: list[dict[str, Any]] = []
    global_unit_index = 0
    for record, units in zip(records, units_by_artifact, strict=True):
        record_indices = vector_indices[record.index]
        record_general = general_mask[record_indices]
        word_counts = np.asarray([unit.word_count for unit in units], dtype=np.float64)
        general_word_count = float(np.sum(word_counts[record_general]))
        total_word_count = float(np.sum(word_counts))
        generality = other_task_match_fractions[record_indices]
        artifact_composition.append(
            {
                "artifact_index": record.index,
                "label": record.label,
                "task_id": record.task_id,
                "artifact_type": record.artifact_type,
                "artifact_id": record.artifact_id,
                "semantic_unit_count": len(units),
                "general_unit_count": int(np.sum(record_general)),
                "domain_unit_count": int(np.sum(np.logical_not(record_general))),
                "general_unit_fraction": float(np.mean(record_general)),
                "semantic_unit_word_count": int(total_word_count),
                "general_unit_word_count": int(general_word_count),
                "domain_unit_word_count": int(total_word_count - general_word_count),
                "general_word_fraction": general_word_count / total_word_count,
                "mean_other_task_match_fraction": float(np.mean(generality)),
                "word_weighted_other_task_match_fraction": float(
                    np.average(generality, weights=word_counts)
                ),
            }
        )
        for local_index, unit in enumerate(units):
            unit_inventory.append(
                {
                    "global_unit_index": global_unit_index,
                    "artifact_index": record.index,
                    "artifact_label": record.label,
                    "artifact_id": record.artifact_id,
                    "task_id": record.task_id,
                    "artifact_type": record.artifact_type,
                    "local_unit_index": local_index,
                    "word_count": unit.word_count,
                    "other_task_match_count": int(other_task_match_counts[global_unit_index]),
                    "other_task_match_fraction": float(
                        other_task_match_fractions[global_unit_index]
                    ),
                    "classification": "General" if general_mask[global_unit_index] else "Domain",
                    "content": unit.text,
                }
            )
            global_unit_index += 1
    _require(global_unit_index == len(all_units), "semantic-unit inventory indexing differs")
    _require(
        all(
            row["general_unit_count"] > 0 and row["domain_unit_count"] > 0
            for row in artifact_composition
        ),
        "General/Domain split produced an empty component for at least one artifact",
    )
    threshold_sensitivity = []
    sensitivity_thresholds = sorted({0.75, general_match_threshold, 0.85})
    sensitivity_fractions = sorted({0.3, general_min_other_task_fraction, 0.5})
    artifact_type_by_unit = np.asarray(
        [records[owner].artifact_type for owner in owners], dtype=object
    )
    for sensitivity_threshold in sensitivity_thresholds:
        for sensitivity_fraction in sensitivity_fractions:
            _, _, sensitivity_mask, sensitivity_metadata = classify_semantic_unit_generality(
                calibrated_unit_similarity,
                unit_task_ids,
                match_threshold=sensitivity_threshold,
                minimum_other_task_fraction=sensitivity_fraction,
            )
            per_artifact_fractions = [
                float(np.mean(sensitivity_mask[indices])) for indices in vector_indices
            ]
            threshold_sensitivity.append(
                {
                    "match_threshold": sensitivity_threshold,
                    "minimum_other_task_fraction": sensitivity_fraction,
                    "minimum_other_task_match_count": sensitivity_metadata[
                        "minimum_other_task_match_count"
                    ],
                    "general_unit_fraction": float(np.mean(sensitivity_mask)),
                    "minimum_artifact_general_unit_fraction": min(per_artifact_fractions),
                    "maximum_artifact_general_unit_fraction": max(per_artifact_fractions),
                    "by_artifact_type": {
                        artifact_type: float(
                            np.mean(sensitivity_mask[artifact_type_by_unit == artifact_type])
                        )
                        for artifact_type in ARTIFACT_TYPES
                    },
                }
            )

    model_dir = getattr(getattr(model, "model", None), "_model_dir", None)
    _require(model_dir is not None, "fastembed did not expose its resolved model snapshot")
    model_manifest = _tree_manifest(Path(model_dir).resolve())
    return SemanticUnitSimilarityResult(
        combined_similarity=calibrated_similarity,
        raw_similarity=raw_similarity,
        general_similarity=general_similarity,
        domain_similarity=domain_similarity,
        metadata={
            "implementation": "order-invariant bidirectional semantic-unit best-match coverage",
            "embedding_backend": "fastembed.TextEmbedding",
            "fastembed_version": importlib.metadata.version("fastembed"),
            "onnxruntime_version": importlib.metadata.version("onnxruntime"),
            "model_name": model_name,
            "embedding_dimension": int(vectors.shape[1]),
            "device": "CPU",
            "threads": threads,
            "batch_size": batch_size,
            "semantic_units": {
                "extractor": (
                    "markdown headings removed; list and method labels stripped; sentence split"
                ),
                "minimum_words": min_words,
                "total_count": len(all_units),
                "per_artifact_count": list(unit_counts),
            },
            "matching": {
                "method": "harmonic mean of both directed mean-best-match coverages",
                "order_sensitive": False,
                "one_source_unit_may_match_multiple_target_units": True,
            },
            "background_calibration": {
                "population": "all semantic-unit cosine values from different artifacts",
                "value_count": int(background.size),
                "floor_quantile": calibration_median_quantile,
                "floor_cosine": background_floor,
                "cap_quantile": calibration_cap_quantile,
                "cap_cosine": high_match_cap,
                "transform": "clip((cosine-floor)/(cap-floor), 0, 1)",
            },
            "general_domain_split": {
                **generality_metadata,
                "unit_count": len(all_units),
                "general_unit_count": int(np.sum(general_mask)),
                "domain_unit_count": int(np.sum(domain_mask)),
                "general_unit_fraction": float(np.mean(general_mask)),
                "component_similarity": (
                    "same calibrated bidirectional best-match coverage, restricted to units "
                    "with the selected General or Domain label"
                ),
                "combined_metric_changed": False,
                "threshold_status": (
                    "post-hoc transparent descriptive threshold; continuous match fractions "
                    "are retained to expose boundary sensitivity"
                ),
                "threshold_sensitivity": threshold_sensitivity,
            },
            "model_snapshot": model_manifest,
        },
        unit_counts=unit_counts,
        artifact_composition=tuple(artifact_composition),
        unit_inventory=tuple(unit_inventory),
    )


def _pair_rows(
    records: Sequence[ArtifactRecord],
    tfidf_similarity: Any,
    embedding_similarity: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            first = records[left]
            second = records[right]
            tfidf = float(tfidf_similarity[left, right])
            embedding = float(embedding_similarity[left, right])
            rows.append(
                {
                    "artifact_a": first.artifact_id,
                    "artifact_b": second.artifact_id,
                    "label_a": first.label,
                    "label_b": second.label,
                    "task_a": first.task_id,
                    "task_b": second.task_id,
                    "type_a": first.artifact_type,
                    "type_b": second.artifact_type,
                    "same_task": first.task_id == second.task_id,
                    "same_type": first.artifact_type == second.artifact_type,
                    "tfidf_cosine": tfidf,
                    "embedding_cosine": embedding,
                    "embedding_minus_tfidf": embedding - tfidf,
                }
            )
    return rows


def _describe(values: Sequence[float]) -> dict[str, Any]:
    _require(bool(values), "cannot describe an empty similarity group")
    return {
        "pair_count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
    }


def summarize_general_domain_composition(
    composition: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate hard-label proportions and continuous cross-task generality."""

    _require(bool(composition), "cannot summarize an empty artifact composition")

    def summarize_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        total_units = sum(int(row["semantic_unit_count"]) for row in rows)
        general_units = sum(int(row["general_unit_count"]) for row in rows)
        total_words = sum(int(row["semantic_unit_word_count"]) for row in rows)
        general_words = sum(int(row["general_unit_word_count"]) for row in rows)
        return {
            "artifact_count": len(rows),
            "semantic_unit_count": total_units,
            "general_unit_count": general_units,
            "domain_unit_count": total_units - general_units,
            "pooled_general_unit_fraction": general_units / total_units,
            "pooled_general_word_fraction": general_words / total_words,
            "pooled_mean_other_task_match_fraction": sum(
                float(row["mean_other_task_match_fraction"])
                * int(row["semantic_unit_count"])
                for row in rows
            )
            / total_units,
            "pooled_word_weighted_other_task_match_fraction": sum(
                float(row["word_weighted_other_task_match_fraction"])
                * int(row["semantic_unit_word_count"])
                for row in rows
            )
            / total_words,
            "artifact_general_unit_fraction": _describe(
                [float(row["general_unit_fraction"]) for row in rows]
            ),
            "artifact_general_word_fraction": _describe(
                [float(row["general_word_fraction"]) for row in rows]
            ),
            "artifact_mean_other_task_match_fraction": _describe(
                [float(row["mean_other_task_match_fraction"]) for row in rows]
            ),
            "artifact_word_weighted_other_task_match_fraction": _describe(
                [float(row["word_weighted_other_task_match_fraction"]) for row in rows]
            ),
        }

    return {
        "overall": summarize_rows(composition),
        "by_artifact_type": {
            artifact_type: summarize_rows(
                [row for row in composition if row["artifact_type"] == artifact_type]
            )
            for artifact_type in ARTIFACT_TYPES
        },
    }


def summarize_similarity(
    records: Sequence[ArtifactRecord],
    tfidf_similarity: Any,
    embedding_similarity: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import numpy as np
    from scipy.cluster.hierarchy import leaves_list, linkage
    from scipy.spatial.distance import squareform
    from scipy.stats import rankdata, spearmanr

    rows = _pair_rows(records, tfidf_similarity, embedding_similarity)
    tfidf_values = [float(row["tfidf_cosine"]) for row in rows]
    embedding_values = [float(row["embedding_cosine"]) for row in rows]
    tfidf_percentiles = rankdata(tfidf_values, method="average") / len(rows)
    embedding_percentiles = rankdata(embedding_values, method="average") / len(rows)
    for row, tfidf_percentile, embedding_percentile in zip(
        rows,
        tfidf_percentiles,
        embedding_percentiles,
        strict=True,
    ):
        row["tfidf_rank_percentile"] = float(tfidf_percentile)
        row["embedding_rank_percentile"] = float(embedding_percentile)
        row["embedding_minus_tfidf_rank_percentile"] = float(
            embedding_percentile - tfidf_percentile
        )

    groups = {
        "all_pairs": rows,
        "same_task_siblings": [row for row in rows if row["same_task"]],
        "same_type_cross_task": [row for row in rows if not row["same_task"] and row["same_type"]],
        "different_type_cross_task": [
            row for row in rows if not row["same_task"] and not row["same_type"]
        ],
    }
    group_summary = {}
    for name, group in groups.items():
        group_summary[name] = {
            "tfidf": _describe([float(row["tfidf_cosine"]) for row in group]),
            "embedding": _describe([float(row["embedding_cosine"]) for row in group]),
            "embedding_minus_tfidf": _describe(
                [float(row["embedding_minus_tfidf"]) for row in group]
            ),
            "embedding_minus_tfidf_rank_percentile": _describe(
                [float(row["embedding_minus_tfidf_rank_percentile"]) for row in group]
            ),
        }

    spearman = spearmanr(tfidf_values, embedding_values)

    by_task: dict[str, dict[str, Any]] = {}
    for task_id in sorted({record.task_id for record in records}, key=_task_sort_key):
        group = [row for row in rows if row["task_a"] == task_id == row["task_b"]]
        _require(len(group) == 3, f"task does not have three sibling pairs: {task_id}")
        by_task[task_id] = {
            "tfidf_mean": statistics.fmean(float(row["tfidf_cosine"]) for row in group),
            "embedding_mean": statistics.fmean(float(row["embedding_cosine"]) for row in group),
            "pairs": [
                {
                    "types": [row["type_a"], row["type_b"]],
                    "tfidf_cosine": row["tfidf_cosine"],
                    "embedding_cosine": row["embedding_cosine"],
                }
                for row in group
            ],
        }

    distance = np.clip(1.0 - embedding_similarity, 0.0, 2.0)
    np.fill_diagonal(distance, 0.0)
    cluster_order = [
        int(value) for value in leaves_list(linkage(squareform(distance), method="average"))
    ]
    ranked_embedding = sorted(rows, key=lambda row: row["embedding_cosine"], reverse=True)
    ranked_gap = sorted(
        rows,
        key=lambda row: row["embedding_minus_tfidf_rank_percentile"],
        reverse=True,
    )
    sibling_group = group_summary["same_task_siblings"]
    matched_cross_task_group = group_summary["different_type_cross_task"]
    summary = {
        "artifact_count": len(records),
        "pair_count": len(rows),
        "groups": group_summary,
        "tfidf_embedding_spearman": {
            "rho": float(spearman.statistic),
            "p_value": float(spearman.pvalue),
            "interpretation": "descriptive only; pairwise entries are not independent",
        },
        "same_task_alignment_effect": {
            "comparator": "different_type_cross_task",
            "tfidf_mean_difference": (
                sibling_group["tfidf"]["mean"] - matched_cross_task_group["tfidf"]["mean"]
            ),
            "embedding_mean_difference": (
                sibling_group["embedding"]["mean"] - matched_cross_task_group["embedding"]["mean"]
            ),
            "interpretation": "descriptive; unordered pair cells are not independent",
        },
        "per_task_sibling_similarity": by_task,
        "highest_embedding_pairs": ranked_embedding[:20],
        "largest_semantic_over_lexical_gaps": ranked_gap[:20],
        "embedding_cluster_order": cluster_order,
        "embedding_cluster_labels": [records[index].label for index in cluster_order],
    }
    return summary, rows


def summarize_semantic_unit_similarity(
    records: Sequence[ArtifactRecord],
    calibrated_similarity: Any,
    raw_similarity: Any,
    general_similarity: Any,
    domain_similarity: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Summarize the order-invariant main-content metric and its role structure."""

    import numpy as np
    from scipy.cluster.hierarchy import leaves_list, linkage
    from scipy.spatial.distance import squareform
    from scipy.stats import rankdata

    _require(
        calibrated_similarity.shape
        == raw_similarity.shape
        == general_similarity.shape
        == domain_similarity.shape
        == (len(records), len(records)),
        "semantic-unit similarity matrix shape differs from artifact inventory",
    )
    rows: list[dict[str, Any]] = []
    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            first = records[left]
            second = records[right]
            rows.append(
                {
                    "artifact_a": first.artifact_id,
                    "artifact_b": second.artifact_id,
                    "semantic_unit_coverage": float(calibrated_similarity[left, right]),
                    "semantic_unit_raw_coverage": float(raw_similarity[left, right]),
                    "general_unit_coverage": float(general_similarity[left, right]),
                    "domain_unit_coverage": float(domain_similarity[left, right]),
                    "same_task": first.task_id == second.task_id,
                    "same_type": first.artifact_type == second.artifact_type,
                    "task_a": first.task_id,
                    "task_b": second.task_id,
                    "type_a": first.artifact_type,
                    "type_b": second.artifact_type,
                }
            )
    cross_task_rows = [row for row in rows if not row["same_task"]]
    cross_task_values = [float(row["semantic_unit_coverage"]) for row in cross_task_rows]
    cross_task_values_array = np.asarray(cross_task_values, dtype=np.float64)
    for row in rows:
        value = float(row["semantic_unit_coverage"])
        row["semantic_unit_cross_task_percentile"] = float(
            np.mean(cross_task_values_array <= value)
        )
    percentiles = rankdata(
        [float(row["semantic_unit_coverage"]) for row in rows],
        method="average",
    ) / len(rows)
    for row, percentile in zip(rows, percentiles, strict=True):
        row["semantic_unit_rank_percentile"] = float(percentile)

    groups = {
        "all_pairs": rows,
        "same_task_siblings": [row for row in rows if row["same_task"]],
        "same_type_cross_task": [row for row in rows if not row["same_task"] and row["same_type"]],
        "different_type_cross_task": [
            row for row in rows if not row["same_task"] and not row["same_type"]
        ],
    }
    group_summary = {
        name: {
            "semantic_unit_coverage": _describe(
                [float(row["semantic_unit_coverage"]) for row in group]
            ),
            "semantic_unit_raw_coverage": _describe(
                [float(row["semantic_unit_raw_coverage"]) for row in group]
            ),
            "general_unit_coverage": _describe(
                [float(row["general_unit_coverage"]) for row in group]
            ),
            "domain_unit_coverage": _describe(
                [float(row["domain_unit_coverage"]) for row in group]
            ),
        }
        for name, group in groups.items()
    }

    role_cross_task: dict[str, Any] = {}
    role_cross_task_components: dict[str, Any] = {}
    for artifact_type in ARTIFACT_TYPES:
        role_rows = [
            row for row in cross_task_rows if row["type_a"] == artifact_type == row["type_b"]
        ]
        role_cross_task[artifact_type] = _describe(
            [float(row["semantic_unit_coverage"]) for row in role_rows]
        )
        role_cross_task_components[artifact_type] = {
            "combined": role_cross_task[artifact_type],
            "general": _describe([float(row["general_unit_coverage"]) for row in role_rows]),
            "domain": _describe([float(row["domain_unit_coverage"]) for row in role_rows]),
        }

    by_task: dict[str, Any] = {}
    for task_id in sorted({record.task_id for record in records}, key=_task_sort_key):
        task_rows = [row for row in rows if row["task_a"] == task_id == row["task_b"]]
        _require(len(task_rows) == 3, f"task does not have three semantic-unit pairs: {task_id}")
        by_task[task_id] = {
            "mean": statistics.fmean(float(row["semantic_unit_coverage"]) for row in task_rows),
            "minimum": min(float(row["semantic_unit_coverage"]) for row in task_rows),
            "maximum": max(float(row["semantic_unit_coverage"]) for row in task_rows),
            "general_mean": statistics.fmean(
                float(row["general_unit_coverage"]) for row in task_rows
            ),
            "domain_mean": statistics.fmean(
                float(row["domain_unit_coverage"]) for row in task_rows
            ),
            "mean_cross_task_percentile": statistics.fmean(
                float(row["semantic_unit_cross_task_percentile"]) for row in task_rows
            ),
            "pairs": [
                {
                    "types": [row["type_a"], row["type_b"]],
                    "semantic_unit_coverage": row["semantic_unit_coverage"],
                    "general_unit_coverage": row["general_unit_coverage"],
                    "domain_unit_coverage": row["domain_unit_coverage"],
                    "cross_task_percentile": row["semantic_unit_cross_task_percentile"],
                }
                for row in task_rows
            ],
        }

    artifact_level: dict[str, Any] = {}
    for record in records:
        sibling_values = [
            float(row["semantic_unit_coverage"])
            for row in rows
            if row["same_task"] and record.artifact_id in (row["artifact_a"], row["artifact_b"])
        ]
        other_values = [
            float(row["semantic_unit_coverage"])
            for row in rows
            if not row["same_task"]
            and record.artifact_id in (row["artifact_a"], row["artifact_b"])
        ]
        _require(
            len(sibling_values) == 2, f"artifact sibling coverage differs: {record.artifact_id}"
        )
        artifact_level[record.artifact_id] = {
            "label": record.label,
            "same_task_mean": statistics.fmean(sibling_values),
            "cross_task_mean": statistics.fmean(other_values),
            "mean_difference": statistics.fmean(sibling_values) - statistics.fmean(other_values),
            "same_task_minimum": min(sibling_values),
            "cross_task_maximum": max(other_values),
            "both_siblings_exceed_every_cross_task_pair": min(sibling_values) > max(other_values),
        }

    sibling_values = [float(row["semantic_unit_coverage"]) for row in groups["same_task_siblings"]]
    cross_task_quantiles = {
        str(quantile): float(np.quantile(cross_task_values_array, quantile))
        for quantile in (0.5, 0.9, 0.95)
    }
    distance = np.clip(1.0 - calibrated_similarity, 0.0, 1.0)
    np.fill_diagonal(distance, 0.0)
    cluster_order = [
        int(value) for value in leaves_list(linkage(squareform(distance), method="average"))
    ]
    ranked = sorted(rows, key=lambda row: row["semantic_unit_coverage"], reverse=True)
    summary = {
        "groups": group_summary,
        "same_task_alignment_effect": {
            "comparator": "different_type_cross_task",
            "mean_difference": group_summary["same_task_siblings"]["semantic_unit_coverage"][
                "mean"
            ]
            - group_summary["different_type_cross_task"]["semantic_unit_coverage"]["mean"],
            "interpretation": "descriptive; unordered pair cells are not independent",
        },
        "cross_task_same_role": role_cross_task,
        "cross_task_same_role_components": role_cross_task_components,
        "component_alignment_effects": {
            component: {
                "same_task_mean": group_summary["same_task_siblings"][field]["mean"],
                "different_type_cross_task_mean": group_summary[
                    "different_type_cross_task"
                ][field]["mean"],
                "mean_difference": group_summary["same_task_siblings"][field]["mean"]
                - group_summary["different_type_cross_task"][field]["mean"],
            }
            for component, field in (
                ("combined", "semantic_unit_coverage"),
                ("general", "general_unit_coverage"),
                ("domain", "domain_unit_coverage"),
            )
        },
        "per_task": by_task,
        "artifact_level": artifact_level,
        "same_task_pair_checks": {
            "pair_count": len(sibling_values),
            "cross_task_quantiles": cross_task_quantiles,
            "above_cross_task_median": sum(
                value > cross_task_quantiles["0.5"] for value in sibling_values
            ),
            "above_cross_task_p90": sum(
                value > cross_task_quantiles["0.9"] for value in sibling_values
            ),
            "above_cross_task_p95": sum(
                value > cross_task_quantiles["0.95"] for value in sibling_values
            ),
            "artifacts_whose_both_siblings_exceed_every_cross_task_pair": sum(
                bool(value["both_siblings_exceed_every_cross_task_pair"])
                for value in artifact_level.values()
            ),
        },
        "highest_pairs": ranked[:30],
        "cluster_order": cluster_order,
        "cluster_labels": [records[index].label for index in cluster_order],
    }
    return summary, rows


def _pair_metric_matrix(
    records: Sequence[ArtifactRecord],
    pair_rows: Sequence[dict[str, Any]],
    *,
    field: str,
) -> Any:
    import numpy as np

    lookup = {record.artifact_id: record.index for record in records}
    matrix = np.zeros((len(records), len(records)), dtype=np.float64)
    observed: set[tuple[int, int]] = set()
    for row in pair_rows:
        left = lookup[str(row["artifact_a"])]
        right = lookup[str(row["artifact_b"])]
        value = float(row[field])
        matrix[left, right] = value
        matrix[right, left] = value
        observed.add((min(left, right), max(left, right)))
    _require(
        len(observed) == (len(records) * (len(records) - 1)) // 2,
        "pair metric matrix does not cover every unordered artifact pair",
    )
    return matrix


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
    )


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write_text(path, buffer.getvalue())


def _write_matrix_csv(path: Path, records: Sequence[ArtifactRecord], matrix: Any) -> None:
    rows = []
    for index, record in enumerate(records):
        row: dict[str, Any] = {"artifact": record.label}
        row.update({other.label: f"{float(matrix[index, other.index]):.10f}" for other in records})
        rows.append(row)
    _write_csv(path, rows, ["artifact", *[record.label for record in records]])


def _save_figure(fig: Any, path: Path) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    fig.savefig(
        temporary,
        format="png",
        dpi=180,
        bbox_inches="tight",
        metadata={"Software": "OpenEvo ChemCrow artifact similarity analysis"},
    )
    os.replace(temporary, path)


def _plot_matrix(
    matrix: Any,
    records: Sequence[ArtifactRecord],
    *,
    title: str,
    output_path: Path,
    order: Sequence[int] | None = None,
    cmap: str = "viridis",
    vmin: float = 0.0,
    vmax: float = 1.0,
    task_boundaries: bool = True,
    colorbar_label: str = "Cosine similarity",
    major_boundaries: Sequence[int] | None = None,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    indices = list(order) if order is not None else list(range(len(records)))
    ordered = matrix[np.ix_(indices, indices)]
    labels = [records[index].label for index in indices]
    fig, ax = plt.subplots(figsize=(16, 14))
    image = ax.imshow(ordered, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
    ax.set_title(title, pad=14)
    ax.set_xticks(range(len(labels)), labels=labels, rotation=90, fontsize=6)
    ax.set_yticks(range(len(labels)), labels=labels, fontsize=6)
    ax.set_xticks(np.arange(-0.5, len(labels), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(labels), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=0.16, alpha=0.7)
    ax.tick_params(which="minor", bottom=False, left=False)
    if task_boundaries and order is None:
        for boundary in range(3, len(records), 3):
            ax.axhline(boundary - 0.5, color="black", linewidth=0.55)
            ax.axvline(boundary - 0.5, color="black", linewidth=0.55)
    if major_boundaries is not None:
        for boundary in major_boundaries:
            ax.axhline(boundary - 0.5, color="black", linewidth=1.0)
            ax.axvline(boundary - 0.5, color="black", linewidth=1.0)
    colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    colorbar.set_label(colorbar_label)
    ax.set_xlabel("Artifact (task:type; M=memory, K=skill, A=agent system)")
    ax.set_ylabel("Artifact (task:type)")
    fig.tight_layout()
    _save_figure(fig, output_path)
    plt.close(fig)


def _task_pair_matrix(
    records: Sequence[ArtifactRecord],
    matrix: Any,
) -> tuple[list[str], Any]:
    import numpy as np

    tasks = sorted({record.task_id for record in records}, key=_task_sort_key)
    values = np.zeros((len(tasks), 3), dtype=np.float64)
    type_pairs = (
        ("text_memory", "skill_bundle"),
        ("text_memory", "agent_system"),
        ("skill_bundle", "agent_system"),
    )
    lookup = {(record.task_id, record.artifact_type): record.index for record in records}
    for task_index, task_id in enumerate(tasks):
        for pair_index, (left_type, right_type) in enumerate(type_pairs):
            values[task_index, pair_index] = matrix[
                lookup[(task_id, left_type)], lookup[(task_id, right_type)]
            ]
    return tasks, values


def _plot_task_comparison(
    records: Sequence[ArtifactRecord],
    tfidf_similarity: Any,
    embedding_similarity: Any,
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tasks, tfidf = _task_pair_matrix(records, tfidf_similarity)
    embedding_tasks, embedding = _task_pair_matrix(records, embedding_similarity)
    _require(tasks == embedding_tasks, "task order differs between similarity matrices")
    labels = [task_id.removeprefix("chemcrow-") for task_id in tasks]
    columns = ["M-K", "M-A", "K-A"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 10), sharey=True)
    for ax, values, title in zip(
        axes,
        (tfidf, embedding),
        ("TF-IDF sibling similarity", "Embedding sibling similarity"),
        strict=True,
    ):
        image = ax.imshow(values, cmap="viridis", vmin=0.0, vmax=1.0, aspect="auto")
        ax.set_title(title)
        ax.set_xticks(range(3), labels=columns)
        ax.set_yticks(range(len(labels)), labels=labels)
        ax.set_xlabel("Artifact type pair")
        for row in range(values.shape[0]):
            for column in range(values.shape[1]):
                color = "white" if values[row, column] < 0.48 else "black"
                ax.text(
                    column,
                    row,
                    f"{values[row, column]:.2f}",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color=color,
                )
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    axes[0].set_ylabel("ChemCrow task")
    fig.suptitle("Within-task three-Reflector similarity", y=1.01)
    fig.tight_layout()
    _save_figure(fig, output_path)
    plt.close(fig)


def _plot_semantic_unit_task_comparison(
    records: Sequence[ArtifactRecord],
    semantic_unit_similarity: Any,
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tasks, values = _task_pair_matrix(records, semantic_unit_similarity)
    labels = [task_id.removeprefix("chemcrow-") for task_id in tasks]
    columns = ["M-K", "M-A", "K-A"]
    fig, ax = plt.subplots(figsize=(7, 10))
    image = ax.imshow(values, cmap="viridis", vmin=0.0, vmax=1.0, aspect="auto")
    ax.set_title("Within-task order-invariant semantic-unit coverage")
    ax.set_xticks(range(3), labels=columns)
    ax.set_yticks(range(len(labels)), labels=labels)
    ax.set_xlabel("Artifact type pair")
    ax.set_ylabel("ChemCrow task")
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            color = "white" if values[row, column] < 0.48 else "black"
            ax.text(
                column,
                row,
                f"{values[row, column]:.2f}",
                ha="center",
                va="center",
                fontsize=8,
                color=color,
            )
    colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    colorbar.set_label("Background-calibrated semantic coverage")
    fig.tight_layout()
    _save_figure(fig, output_path)
    plt.close(fig)


def _plot_semantic_component_task_comparison(
    records: Sequence[ArtifactRecord],
    combined_similarity: Any,
    general_similarity: Any,
    domain_similarity: Any,
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    task_inventory = []
    component_values = []
    for matrix in (combined_similarity, general_similarity, domain_similarity):
        tasks, values = _task_pair_matrix(records, matrix)
        task_inventory.append(tasks)
        component_values.append(values)
    _require(
        task_inventory[0] == task_inventory[1] == task_inventory[2],
        "task order differs between semantic component matrices",
    )
    labels = [task_id.removeprefix("chemcrow-") for task_id in task_inventory[0]]
    columns = ["M-K", "M-A", "K-A"]
    fig, axes = plt.subplots(1, 3, figsize=(16, 10), sharey=True)
    for ax, values, title in zip(
        axes,
        component_values,
        ("Combined", "General-only", "Domain-only"),
        strict=True,
    ):
        image = ax.imshow(values, cmap="viridis", vmin=0.0, vmax=1.0, aspect="auto")
        ax.set_title(title)
        ax.set_xticks(range(3), labels=columns)
        ax.set_yticks(range(len(labels)), labels=labels)
        ax.set_xlabel("Artifact type pair")
        for row in range(values.shape[0]):
            for column in range(values.shape[1]):
                color = "white" if values[row, column] < 0.48 else "black"
                ax.text(
                    column,
                    row,
                    f"{values[row, column]:.2f}",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color=color,
                )
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    axes[0].set_ylabel("ChemCrow task")
    fig.suptitle("Within-task semantic coverage: combined versus decomposed", y=1.01)
    fig.tight_layout()
    _save_figure(fig, output_path)
    plt.close(fig)


def _plot_general_domain_composition(
    composition: Sequence[dict[str, Any]],
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    labels = [str(row["label"]) for row in composition]
    unit_fractions = np.asarray(
        [float(row["general_unit_fraction"]) for row in composition], dtype=np.float64
    )
    word_fractions = np.asarray(
        [float(row["general_word_fraction"]) for row in composition], dtype=np.float64
    )
    positions = np.arange(len(composition))
    fig, axes = plt.subplots(1, 2, figsize=(15, 13), sharey=True)
    for ax, fractions, title in zip(
        axes,
        (unit_fractions, word_fractions),
        ("General share by semantic-unit count", "General share by semantic-unit words"),
        strict=True,
    ):
        ax.barh(positions, fractions, color="#4C78A8", label="General")
        ax.barh(
            positions,
            1.0 - fractions,
            left=fractions,
            color="#F58518",
            label="Domain",
        )
        ax.set_xlim(0.0, 1.0)
        ax.set_xlabel("Share of artifact content")
        ax.set_title(title)
        ax.grid(axis="x", alpha=0.25)
    axes[0].set_yticks(positions, labels=labels, fontsize=7)
    axes[0].invert_yaxis()
    axes[1].legend(loc="lower right")
    fig.suptitle("General/Domain composition of all 42 native artifacts", y=1.005)
    fig.tight_layout()
    _save_figure(fig, output_path)
    plt.close(fig)


def _plot_cross_task_role_distribution(
    semantic_unit_rows: Sequence[dict[str, Any]],
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    values = []
    labels = []
    for artifact_type in ARTIFACT_TYPES:
        role_values = [
            float(row["semantic_unit_coverage"])
            for row in semantic_unit_rows
            if not row["same_task"] and row["type_a"] == artifact_type == row["type_b"]
        ]
        _require(bool(role_values), f"cross-task role group is empty: {artifact_type}")
        values.append(role_values)
        labels.append(ARTIFACT_TYPE_SHORT[artifact_type])
    fig, ax = plt.subplots(figsize=(8, 6))
    box = ax.boxplot(values, tick_labels=labels, patch_artist=True, showmeans=True)
    for patch, color in zip(box["boxes"], ("#4C78A8", "#F58518", "#54A24B"), strict=True):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)
    ax.set_ylim(0.0, 1.0)
    ax.set_ylabel("Background-calibrated semantic-unit coverage")
    ax.set_xlabel("Same artifact type across different tasks (M/K/A)")
    ax.set_title("Cross-task role commonality after removing wording and order effects")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    _save_figure(fig, output_path)
    plt.close(fig)


def _selected_pair(
    rows: Sequence[dict[str, Any]],
    artifact_ids: Sequence[str] | None,
) -> dict[str, Any] | None:
    if artifact_ids is None:
        return None
    _require(len(artifact_ids) == 2, "selected pair requires exactly two artifact IDs")
    _require(artifact_ids[0] != artifact_ids[1], "selected artifact IDs must differ")
    expected = frozenset(artifact_ids)
    matches = [
        row
        for row in rows
        if frozenset((str(row["artifact_a"]), str(row["artifact_b"]))) == expected
    ]
    _require(len(matches) == 1, "selected artifact pair was not found exactly once")
    return matches[0]


def _output_manifest(output_dir: Path, *, excluded: set[str]) -> dict[str, Any]:
    entries = []
    for path in sorted(output_dir.iterdir()):
        if not path.is_file() or path.name in excluded or path.is_symlink():
            continue
        entries.append(
            {
                "path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return {
        "file_count": len(entries),
        "files": entries,
        "manifest_sha256": canonical_sha256(entries),
    }


def _markdown_report(
    *,
    source_path: Path,
    source_sha256: str,
    embedding_metadata: dict[str, Any],
    semantic_unit_metadata: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    groups = summary["groups"]
    all_pairs = groups["all_pairs"]
    siblings = groups["same_task_siblings"]
    unit_analysis = summary["semantic_unit_analysis"]
    unit_groups = unit_analysis["groups"]
    unit_checks = unit_analysis["same_task_pair_checks"]
    composition = summary["general_domain_composition"]
    split = semantic_unit_metadata["general_domain_split"]
    lines = [
        "# ChemCrow v5 artifact cosine-similarity analysis",
        "",
        "Status: `COMPLETE`",
        "",
        (
            "This is a post-generation, read-only descriptive analysis. It was not fed into "
            "Candidate, Reflector, evaluator, or artifact selection."
        ),
        "",
        "## Method",
        "",
        f"- Source: `{source_path}`",
        f"- Source SHA256: `{source_sha256}`",
        f"- Artifacts: {summary['artifact_count']}",
        f"- Unordered artifact pairs: {summary['pair_count']}",
        "- Lexical metric: unigram/bigram TF-IDF cosine similarity.",
        f"- Semantic metric: `{embedding_metadata['model_name']}` via FastEmbed/ONNX on CPU.",
        (
            "- Long artifacts: deterministic overlapping word chunks, weighted mean pooling, "
            "L2 normalization."
        ),
        (
            "- Main-content metric: Markdown headings and workflow labels are removed, text is "
            "split into semantic units, and both documents' units are matched bidirectionally. "
            "Unit order does not affect the score."
        ),
        (
            "- Embedding-space background calibration: median cosine "
            f"`{semantic_unit_metadata['background_calibration']['floor_cosine']:.4f}`; "
            "99th-percentile cap "
            f"`{semantic_unit_metadata['background_calibration']['cap_cosine']:.4f}`."
        ),
        (
            "- General/Domain split: a unit is General when it has a calibrated match of at "
            f"least `{split['match_threshold']:.2f}` in at least "
            f"`{split['minimum_other_task_match_count']}/{split['other_task_count']}` other "
            "tasks; Domain is the complement. The threshold is post-hoc and descriptive."
        ),
        (
            "- Combined coverage is preserved unchanged. General-only and Domain-only matrices "
            "apply the same matching formula to the corresponding subsets."
        ),
        (
            "- The component scores are diagnostic and not additive: combined matching can pair "
            "units across the General/Domain label boundary and uses different denominators."
        ),
        "- Training performed: no.",
        "- Paid/model API calls: no.",
        "",
        "## Aggregate results",
        "",
        (
            "Raw TF-IDF and embedding cosine values have different scales. Their direct "
            "difference is retained in CSV for transparency, but the comparison heatmap uses "
            "within-metric rank percentiles."
        ),
        "",
        (
            "| Pair group | Count | Mean TF-IDF | Mean embedding | Combined | General-only | "
            "Domain-only |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for group_name in (
        "all_pairs",
        "same_task_siblings",
        "same_type_cross_task",
        "different_type_cross_task",
    ):
        group = groups[group_name]
        lines.append(
            f"| {group_name} | {group['tfidf']['pair_count']} | "
            f"{group['tfidf']['mean']:.4f} | {group['embedding']['mean']:.4f} | "
            f"{unit_groups[group_name]['semantic_unit_coverage']['mean']:.4f} | "
            f"{unit_groups[group_name]['general_unit_coverage']['mean']:.4f} | "
            f"{unit_groups[group_name]['domain_unit_coverage']['mean']:.4f} |"
        )
    correlation = summary["tfidf_embedding_spearman"]
    lines.extend(
        [
            "",
            (
                f"Across all unordered pairs, TF-IDF versus embedding Spearman rho is "
                f"`{correlation['rho']:.4f}`. Pairwise cells are not statistically independent, "
                "so this is descriptive rather than an inferential p-value claim."
            ),
            "",
            (
                "The key diagnostic is the contrast between lexical and semantic similarity: a "
                "low TF-IDF score with a high embedding score indicates different wording that "
                "may still express similar policy or advice."
            ),
            "",
            "## Order-invariant content findings",
            "",
            (
                "The semantic-unit metric treats reordered propositions as the same content. "
                f"Same-task sibling mean coverage is "
                f"`{unit_groups['same_task_siblings']['semantic_unit_coverage']['mean']:.4f}`, "
                "versus "
                f"`{unit_groups['different_type_cross_task']['semantic_unit_coverage']['mean']:.4f}` "
                "for different-role, cross-task pairs."
            ),
            (
                f"All `{unit_checks['above_cross_task_median']}/{unit_checks['pair_count']}` "
                "same-task pairs exceed the cross-task median; "
                f"`{unit_checks['above_cross_task_p90']}/{unit_checks['pair_count']}` exceed the "
                "cross-task 90th percentile and "
                f"`{unit_checks['above_cross_task_p95']}/{unit_checks['pair_count']}` exceed the "
                "95th percentile. This is an aggregate tendency, not a claim that every sibling "
                "pair beats every cross-task pair."
            ),
            "",
            "Cross-task same-role means:",
            "",
            (f"- Memory: `{unit_analysis['cross_task_same_role']['text_memory']['mean']:.4f}`"),
            (f"- Skill: `{unit_analysis['cross_task_same_role']['skill_bundle']['mean']:.4f}`"),
            (
                "- Agent System: "
                f"`{unit_analysis['cross_task_same_role']['agent_system']['mean']:.4f}`"
            ),
            "",
            "## General/Domain composition",
            "",
            (
                f"Across all `{composition['overall']['semantic_unit_count']}` units, "
                f"`{composition['overall']['general_unit_count']}` are General and "
                f"`{composition['overall']['domain_unit_count']}` are Domain. The pooled General "
                f"share is `{composition['overall']['pooled_general_unit_fraction']:.4f}` by unit "
                f"count and `{composition['overall']['pooled_general_word_fraction']:.4f}` by "
                "semantic-unit word count."
            ),
            "",
            "Pooled General unit share by artifact type:",
            "",
            (
                "- Memory: "
                f"`{composition['by_artifact_type']['text_memory']['pooled_general_unit_fraction']:.4f}`"
            ),
            (
                "- Skill: "
                f"`{composition['by_artifact_type']['skill_bundle']['pooled_general_unit_fraction']:.4f}`"
            ),
            (
                "- Agent System: "
                f"`{composition['by_artifact_type']['agent_system']['pooled_general_unit_fraction']:.4f}`"
            ),
            "",
            "## Figures and data",
            "",
            "- `tfidf_cosine_heatmap.png`: lexical 42x42 matrix.",
            "- `embedding_cosine_heatmap.png`: semantic 42x42 matrix.",
            (
                "- `embedding_clustered_heatmap.png`: semantic matrix reordered by "
                "average-linkage clustering."
            ),
            (
                "- `embedding_rank_minus_tfidf_rank_heatmap.png`: embedding percentile rank "
                "minus TF-IDF percentile rank."
            ),
            "- `within_task_3x3_comparison.png`: three sibling pairs for every task.",
            (
                "- `semantic_unit_coverage_heatmap.png`: optimized, order-invariant main-content "
                "similarity in task order."
            ),
            (
                "- `semantic_unit_coverage_role_ordered_heatmap.png`: the same matrix grouped "
                "into Memory, Skill, and Agent System blocks."
            ),
            (
                "- `semantic_unit_coverage_clustered_heatmap.png`: main-content matrix reordered "
                "by average-linkage clustering."
            ),
            (
                "- `within_task_semantic_unit_coverage.png`: optimized sibling scores for every "
                "task."
            ),
            (
                "- `cross_task_role_semantic_coverage.png`: cross-task Memory/Skill/Agent System "
                "distributions."
            ),
            (
                "- `general_unit_coverage_heatmap.png` and "
                "`general_unit_coverage_role_ordered_heatmap.png`: General-only matrices."
            ),
            (
                "- `domain_unit_coverage_heatmap.png` and "
                "`domain_unit_coverage_role_ordered_heatmap.png`: Domain-only matrices."
            ),
            (
                "- `artifact_general_domain_composition.png`: General/Domain shares by unit and "
                "semantic-unit word count."
            ),
            (
                "- `within_task_combined_general_domain_coverage.png`: the three within-task "
                "role pairs under all three content views."
            ),
            "- `artifact_index.csv`: label-to-artifact/job/hash mapping.",
            "- `tfidf_cosine_matrix.csv` and `embedding_cosine_matrix.csv`: exact matrices.",
            (
                "- `artifact_general_domain_composition.csv`: per-artifact proportions and "
                "continuous cross-task generality."
            ),
            (
                "- `semantic_unit_inventory.csv`: all units, exact text, word count, cross-task "
                "match count/fraction, and General/Domain label."
            ),
            "- `pairwise_similarity.csv`: long-form pair comparison table.",
            "- `summary.json`: aggregate, per-task, top-pair, and clustering results.",
            "",
            "## Interpretation boundary",
            "",
            (
                f"The mean same-task sibling values are `{siblings['tfidf']['mean']:.4f}` for "
                f"TF-IDF and `{siblings['embedding']['mean']:.4f}` for embeddings, versus "
                f"`{all_pairs['tfidf']['mean']:.4f}` and "
                f"`{all_pairs['embedding']['mean']:.4f}` over all pairs. Cosine similarity alone "
                "does not establish a task conflict, causal mechanism, or instruction quality; "
                "it should be read alongside the sealed semantic audit."
            ),
            (
                f"Relative to different-type pairs from different tasks, same-task siblings are "
                f"higher by `{summary['same_task_alignment_effect']['tfidf_mean_difference']:.4f}` "
                "TF-IDF cosine and "
                f"`{summary['same_task_alignment_effect']['embedding_mean_difference']:.4f}` "
                "embedding cosine."
            ),
            (
                "The optimized metric is descriptive. It removes Markdown presentation and unit "
                "order, but it can still merge distinct propositions that the frozen embedding "
                "model places close together. The manual full-text audit remains authoritative "
                "for whether two artifacts are substantively identical."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def run_analysis(
    *,
    source_path: Path,
    output_dir: Path,
    model_cache: Path,
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    expected_artifact_count: int | None = 42,
    words_per_chunk: int = 180,
    overlap_words: int = 30,
    min_semantic_unit_words: int = DEFAULT_MIN_SEMANTIC_UNIT_WORDS,
    general_match_threshold: float = DEFAULT_GENERAL_MATCH_THRESHOLD,
    general_min_other_task_fraction: float = DEFAULT_GENERAL_MIN_OTHER_TASK_FRACTION,
    batch_size: int = 16,
    threads: int = 1,
    selected_artifact_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    import numpy as np

    source_path = source_path.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        _require(not any(output_dir.iterdir()), f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    records, source_payload = load_artifacts(
        source_path,
        expected_artifact_count=expected_artifact_count,
    )
    tfidf_similarity, tfidf_metadata = compute_tfidf_similarity(records)
    embedding_similarity, embedding_metadata, chunk_counts = compute_embedding_similarity(
        records,
        model_name=model_name,
        cache_dir=model_cache,
        words_per_chunk=words_per_chunk,
        overlap_words=overlap_words,
        batch_size=batch_size,
        threads=threads,
    )
    semantic_result = compute_semantic_unit_similarity(
        records,
        model_name=model_name,
        cache_dir=model_cache,
        min_words=min_semantic_unit_words,
        general_match_threshold=general_match_threshold,
        general_min_other_task_fraction=general_min_other_task_fraction,
        batch_size=batch_size,
        threads=threads,
    )
    semantic_unit_similarity = semantic_result.combined_similarity
    semantic_unit_raw_similarity = semantic_result.raw_similarity
    general_unit_similarity = semantic_result.general_similarity
    domain_unit_similarity = semantic_result.domain_similarity
    semantic_unit_metadata = semantic_result.metadata
    semantic_unit_counts = semantic_result.unit_counts
    _require(
        tfidf_similarity.shape
        == embedding_similarity.shape
        == semantic_unit_similarity.shape
        == semantic_unit_raw_similarity.shape
        == general_unit_similarity.shape
        == domain_unit_similarity.shape
        == (len(records), len(records)),
        "similarity matrix shape differs from the artifact inventory",
    )
    _require(
        np.allclose(tfidf_similarity, tfidf_similarity.T, atol=1e-12),
        "TF-IDF matrix is not symmetric",
    )
    _require(
        np.allclose(embedding_similarity, embedding_similarity.T, atol=1e-12),
        "embedding matrix is not symmetric",
    )
    _require(
        np.allclose(semantic_unit_similarity, semantic_unit_similarity.T, atol=1e-12)
        and np.allclose(
            semantic_unit_raw_similarity,
            semantic_unit_raw_similarity.T,
            atol=1e-12,
        )
        and np.allclose(general_unit_similarity, general_unit_similarity.T, atol=1e-12)
        and np.allclose(domain_unit_similarity, domain_unit_similarity.T, atol=1e-12),
        "semantic-unit matrix is not symmetric",
    )
    _require(
        np.allclose(np.diag(tfidf_similarity), 1.0, atol=1e-12)
        and np.allclose(np.diag(embedding_similarity), 1.0, atol=1e-12)
        and np.allclose(np.diag(semantic_unit_similarity), 1.0, atol=1e-12)
        and np.allclose(np.diag(semantic_unit_raw_similarity), 1.0, atol=1e-12)
        and np.allclose(np.diag(general_unit_similarity), 1.0, atol=1e-12)
        and np.allclose(np.diag(domain_unit_similarity), 1.0, atol=1e-12),
        "similarity matrix diagonal differs from one",
    )
    summary, pair_rows = summarize_similarity(
        records,
        tfidf_similarity,
        embedding_similarity,
    )
    semantic_unit_summary, semantic_unit_rows = summarize_semantic_unit_similarity(
        records,
        semantic_unit_similarity,
        semantic_unit_raw_similarity,
        general_unit_similarity,
        domain_unit_similarity,
    )
    _require(len(pair_rows) == len(semantic_unit_rows), "pair inventories differ between metrics")
    for row, semantic_row in zip(pair_rows, semantic_unit_rows, strict=True):
        _require(
            row["artifact_a"] == semantic_row["artifact_a"]
            and row["artifact_b"] == semantic_row["artifact_b"],
            "pair order differs between metrics",
        )
        row.update(
            {
                "semantic_unit_coverage": semantic_row["semantic_unit_coverage"],
                "semantic_unit_raw_coverage": semantic_row["semantic_unit_raw_coverage"],
                "general_unit_coverage": semantic_row["general_unit_coverage"],
                "domain_unit_coverage": semantic_row["domain_unit_coverage"],
                "semantic_unit_rank_percentile": semantic_row["semantic_unit_rank_percentile"],
                "semantic_unit_cross_task_percentile": semantic_row[
                    "semantic_unit_cross_task_percentile"
                ],
            }
        )
    summary["semantic_unit_analysis"] = semantic_unit_summary
    summary["general_domain_composition"] = summarize_general_domain_composition(
        semantic_result.artifact_composition
    )
    selected_pair = _selected_pair(pair_rows, selected_artifact_ids)
    if selected_pair is not None:
        summary["selected_pair"] = selected_pair
    rank_contrast = _pair_metric_matrix(
        records,
        pair_rows,
        field="embedding_minus_tfidf_rank_percentile",
    )

    index_rows = [
        record.index_row(
            chunk_count=chunk_counts[record.index],
            semantic_unit_count=semantic_unit_counts[record.index],
        )
        for record in records
    ]
    _write_csv(output_dir / "artifact_index.csv", index_rows, list(index_rows[0]))
    _write_matrix_csv(output_dir / "tfidf_cosine_matrix.csv", records, tfidf_similarity)
    _write_matrix_csv(output_dir / "embedding_cosine_matrix.csv", records, embedding_similarity)
    _write_matrix_csv(
        output_dir / "semantic_unit_coverage_matrix.csv",
        records,
        semantic_unit_similarity,
    )
    _write_matrix_csv(
        output_dir / "semantic_unit_raw_coverage_matrix.csv",
        records,
        semantic_unit_raw_similarity,
    )
    _write_matrix_csv(
        output_dir / "general_unit_coverage_matrix.csv",
        records,
        general_unit_similarity,
    )
    _write_matrix_csv(
        output_dir / "domain_unit_coverage_matrix.csv",
        records,
        domain_unit_similarity,
    )
    _write_csv(
        output_dir / "artifact_general_domain_composition.csv",
        semantic_result.artifact_composition,
        list(semantic_result.artifact_composition[0]),
    )
    _write_csv(
        output_dir / "semantic_unit_inventory.csv",
        semantic_result.unit_inventory,
        list(semantic_result.unit_inventory[0]),
    )
    _write_csv(output_dir / "pairwise_similarity.csv", pair_rows, list(pair_rows[0]))
    _write_json(output_dir / "summary.json", summary)

    _plot_matrix(
        tfidf_similarity,
        records,
        title="ChemCrow v5 artifacts: TF-IDF cosine similarity",
        output_path=output_dir / "tfidf_cosine_heatmap.png",
    )
    _plot_matrix(
        embedding_similarity,
        records,
        title=f"ChemCrow v5 artifacts: semantic cosine ({model_name})",
        output_path=output_dir / "embedding_cosine_heatmap.png",
    )
    cluster_order = summary["embedding_cluster_order"]
    _plot_matrix(
        embedding_similarity,
        records,
        title="ChemCrow v5 artifacts: clustered semantic cosine",
        output_path=output_dir / "embedding_clustered_heatmap.png",
        order=cluster_order,
        task_boundaries=False,
    )
    _plot_matrix(
        rank_contrast,
        records,
        title="ChemCrow v5 artifacts: embedding rank minus TF-IDF rank",
        output_path=output_dir / "embedding_rank_minus_tfidf_rank_heatmap.png",
        cmap="coolwarm",
        vmin=-1.0,
        vmax=1.0,
        colorbar_label="Embedding rank percentile minus TF-IDF rank percentile",
    )
    _plot_task_comparison(
        records,
        tfidf_similarity,
        embedding_similarity,
        output_path=output_dir / "within_task_3x3_comparison.png",
    )
    _plot_matrix(
        semantic_unit_similarity,
        records,
        title="ChemCrow v5 artifacts: order-invariant main-content coverage",
        output_path=output_dir / "semantic_unit_coverage_heatmap.png",
        colorbar_label="Background-calibrated semantic-unit coverage",
    )
    role_order = [
        record.index
        for artifact_type in ARTIFACT_TYPES
        for record in records
        if record.artifact_type == artifact_type
    ]
    task_count = len(records) // len(ARTIFACT_TYPES)
    _plot_matrix(
        semantic_unit_similarity,
        records,
        title="ChemCrow v5 artifacts: main-content coverage grouped by artifact type",
        output_path=output_dir / "semantic_unit_coverage_role_ordered_heatmap.png",
        order=role_order,
        task_boundaries=False,
        major_boundaries=[task_count, task_count * 2],
        colorbar_label="Background-calibrated semantic-unit coverage",
    )
    _plot_matrix(
        semantic_unit_similarity,
        records,
        title="ChemCrow v5 artifacts: clustered order-invariant main-content coverage",
        output_path=output_dir / "semantic_unit_coverage_clustered_heatmap.png",
        order=semantic_unit_summary["cluster_order"],
        task_boundaries=False,
        colorbar_label="Background-calibrated semantic-unit coverage",
    )
    _plot_semantic_unit_task_comparison(
        records,
        semantic_unit_similarity,
        output_path=output_dir / "within_task_semantic_unit_coverage.png",
    )
    _plot_cross_task_role_distribution(
        semantic_unit_rows,
        output_path=output_dir / "cross_task_role_semantic_coverage.png",
    )
    _plot_matrix(
        general_unit_similarity,
        records,
        title="ChemCrow v5 artifacts: General-only semantic coverage",
        output_path=output_dir / "general_unit_coverage_heatmap.png",
        colorbar_label="General-unit semantic coverage",
    )
    _plot_matrix(
        general_unit_similarity,
        records,
        title="ChemCrow v5 artifacts: General-only coverage grouped by artifact type",
        output_path=output_dir / "general_unit_coverage_role_ordered_heatmap.png",
        order=role_order,
        task_boundaries=False,
        major_boundaries=[task_count, task_count * 2],
        colorbar_label="General-unit semantic coverage",
    )
    _plot_matrix(
        domain_unit_similarity,
        records,
        title="ChemCrow v5 artifacts: Domain-only semantic coverage",
        output_path=output_dir / "domain_unit_coverage_heatmap.png",
        colorbar_label="Domain-unit semantic coverage",
    )
    _plot_matrix(
        domain_unit_similarity,
        records,
        title="ChemCrow v5 artifacts: Domain-only coverage grouped by artifact type",
        output_path=output_dir / "domain_unit_coverage_role_ordered_heatmap.png",
        order=role_order,
        task_boundaries=False,
        major_boundaries=[task_count, task_count * 2],
        colorbar_label="Domain-unit semantic coverage",
    )
    _plot_semantic_component_task_comparison(
        records,
        semantic_unit_similarity,
        general_unit_similarity,
        domain_unit_similarity,
        output_path=output_dir / "within_task_combined_general_domain_coverage.png",
    )
    _plot_general_domain_composition(
        semantic_result.artifact_composition,
        output_path=output_dir / "artifact_general_domain_composition.png",
    )

    source_sha256 = file_sha256(source_path)
    _atomic_write_text(
        output_dir / "README.md",
        _markdown_report(
            source_path=source_path,
            source_sha256=source_sha256,
            embedding_metadata=embedding_metadata,
            semantic_unit_metadata=semantic_unit_metadata,
            summary=summary,
        ),
    )
    receipt_name = "artifact_similarity_receipt.json"
    receipt = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "status": "COMPLETE",
        "generated_at": datetime.now(UTC).isoformat(),
        "post_generation_only": True,
        "fed_to_candidate_reflector_or_evaluator": False,
        "training_performed": False,
        "paid_model_api_calls": 0,
        "source": {
            "path": str(source_path),
            "sha256": source_sha256,
            "schema_version": source_payload.get("schema_version"),
            "run_id": source_payload.get("run_id"),
            "artifact_count": len(records),
            "artifact_inventory_sha256": canonical_sha256(
                [
                    {
                        "artifact_id": record.artifact_id,
                        "job_id": record.job_id,
                        "content_sha256": record.content_sha256,
                    }
                    for record in records
                ]
            ),
        },
        "tfidf": tfidf_metadata,
        "embedding": embedding_metadata,
        "semantic_unit_coverage": semantic_unit_metadata,
        "selected_pair": selected_pair,
        "summary_sha256": file_sha256(output_dir / "summary.json"),
        "output_manifest": _output_manifest(output_dir, excluded={receipt_name}),
    }
    _write_json(output_dir / receipt_name, receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute TF-IDF and pretrained-embedding artifact cosine heatmaps."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--expected-artifacts", type=int, default=42)
    parser.add_argument("--words-per-chunk", type=int, default=180)
    parser.add_argument("--overlap-words", type=int, default=30)
    parser.add_argument(
        "--min-semantic-unit-words",
        type=int,
        default=DEFAULT_MIN_SEMANTIC_UNIT_WORDS,
    )
    parser.add_argument(
        "--general-match-threshold",
        type=float,
        default=DEFAULT_GENERAL_MATCH_THRESHOLD,
    )
    parser.add_argument(
        "--general-min-other-task-fraction",
        type=float,
        default=DEFAULT_GENERAL_MIN_OTHER_TASK_FRACTION,
    )
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--pair", nargs=2, metavar=("ARTIFACT_A", "ARTIFACT_B"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = run_analysis(
        source_path=args.source,
        output_dir=args.output_dir,
        model_cache=args.model_cache,
        model_name=args.embedding_model,
        expected_artifact_count=args.expected_artifacts,
        words_per_chunk=args.words_per_chunk,
        overlap_words=args.overlap_words,
        min_semantic_unit_words=args.min_semantic_unit_words,
        general_match_threshold=args.general_match_threshold,
        general_min_other_task_fraction=args.general_min_other_task_fraction,
        batch_size=args.batch_size,
        threads=args.threads,
        selected_artifact_ids=args.pair,
    )
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "artifact_count": receipt["source"]["artifact_count"],
                "output_manifest_sha256": receipt["output_manifest"]["manifest_sha256"],
                "paid_model_api_calls": receipt["paid_model_api_calls"],
                "training_performed": receipt["training_performed"],
            },
            sort_keys=True,
        )
    )
    return 0
