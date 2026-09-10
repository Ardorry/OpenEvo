from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openevo_chemcrow import artifact_similarity as similarity
from openevo_chemcrow import manual_scope_similarity as manual_scope


def _artifact(task_id: str, artifact_type: str, ordinal: int, content: str) -> dict[str, object]:
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "artifact_type": artifact_type,
        "artifact_id": f"art-{ordinal}",
        "job_id": f"job-{ordinal}",
        "reflector_run_id": f"run-{ordinal}",
        "content": content,
        "content_bytes": len(content.encode("utf-8")),
        "content_sha256": content_sha256,
        "registered_artifact_hash": content_sha256,
        "exact_native_content_preserved": True,
        **{flag: False for flag in similarity.SEMANTIC_FLAGS},
    }


def _source(path: Path) -> Path:
    tasks = {}
    ordinal = 0
    for task_number in (1, 2):
        task_id = f"chemcrow-{task_number:02d}"
        findings = {}
        for artifact_type in similarity.ARTIFACT_TYPES:
            findings[artifact_type] = _artifact(
                task_id,
                artifact_type,
                ordinal,
                f"{task_id} {artifact_type} evidence verification policy number {ordinal}",
            )
            ordinal += 1
        tasks[task_id] = {"artifact_findings": findings}
    payload = {
        "schema_version": similarity.SOURCE_SCHEMA_VERSION,
        "status": "POST_GENERATION_AUDIT_COMPLETE",
        "created_after_all_pairs_sealed": True,
        "fed_to_candidate_or_reflector": False,
        "artifact_count": 6,
        "unique_artifact_count": 6,
        "unique_reflector_job_count": 6,
        "tasks": tasks,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_artifacts_binds_exact_content_and_stable_order(tmp_path: Path) -> None:
    records, _ = similarity.load_artifacts(
        _source(tmp_path / "source.json"),
        expected_artifact_count=6,
    )
    assert [record.label for record in records] == [
        "01:M",
        "01:K",
        "01:A",
        "02:M",
        "02:K",
        "02:A",
    ]
    assert len({record.artifact_id for record in records}) == 6
    assert len({record.job_id for record in records}) == 6


def test_load_artifacts_rejects_content_hash_drift(tmp_path: Path) -> None:
    path = _source(tmp_path / "source.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tasks"]["chemcrow-01"]["artifact_findings"]["text_memory"]["content"] = "mutated"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash differs"):
        similarity.load_artifacts(path, expected_artifact_count=6)


def test_chunk_text_is_deterministic_and_overlapping() -> None:
    text = " ".join(f"word-{index}" for index in range(100))
    chunks = similarity.chunk_text(text, words_per_chunk=40, overlap_words=10)
    assert [chunk.word_count for chunk in chunks] == [40, 40, 40]
    assert chunks[0].text.split()[-10:] == chunks[1].text.split()[:10]
    assert chunks[1].text.split()[-10:] == chunks[2].text.split()[:10]
    assert chunks == similarity.chunk_text(text, words_per_chunk=40, overlap_words=10)


def test_semantic_units_discard_markdown_scaffolding_and_preserve_content() -> None:
    text = """# Heading

- **Trigger:** The task asks for alpha beta gamma chemistry evidence.
- **Action:** Explain delta epsilon zeta with careful validation.
- Short label
"""
    units = similarity.extract_semantic_units(text)
    assert [unit.text for unit in units] == [
        "The task asks for alpha beta gamma chemistry evidence.",
        "Explain delta epsilon zeta with careful validation.",
    ]


def test_semantic_unit_coverage_is_invariant_to_abc_bac_reordering() -> None:
    np = pytest.importorskip("numpy")
    abc_to_bac = np.asarray(
        [
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    assert similarity._symmetric_best_match_coverage(abc_to_bac) == pytest.approx(1.0)
    assert similarity._symmetric_best_match_coverage(abc_to_bac.T) == pytest.approx(1.0)


def test_generality_uses_semantic_document_frequency_across_other_tasks() -> None:
    np = pytest.importorskip("numpy")
    calibrated = np.eye(6, dtype=float)
    calibrated[0, 2] = calibrated[2, 0] = 0.9
    calibrated[0, 4] = calibrated[4, 0] = 0.85
    task_ids = [
        "chemcrow-01",
        "chemcrow-01",
        "chemcrow-02",
        "chemcrow-02",
        "chemcrow-03",
        "chemcrow-03",
    ]

    counts, fractions, general, metadata = similarity.classify_semantic_unit_generality(
        calibrated,
        task_ids,
        match_threshold=0.8,
        minimum_other_task_fraction=0.75,
    )

    assert counts.tolist() == [2, 0, 1, 0, 1, 0]
    assert fractions.tolist() == pytest.approx([1.0, 0.0, 0.5, 0.0, 0.5, 0.0])
    assert general.tolist() == [True, False, False, False, False, False]
    assert metadata["minimum_other_task_match_count"] == 2


def test_tree_manifest_hashes_huggingface_style_snapshot_links(tmp_path: Path) -> None:
    model_root = tmp_path / "models--example"
    blob = model_root / "blobs" / "digest"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"model-bytes")
    snapshot = model_root / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "model.onnx").symlink_to(Path("../../blobs/digest"))

    manifest = similarity._tree_manifest(snapshot)
    assert manifest["resolved_revision"] == "revision"
    assert manifest["file_count"] == 1
    assert manifest["total_bytes"] == len(b"model-bytes")


def test_tfidf_and_summary_separate_lexical_from_semantic(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    records, _ = similarity.load_artifacts(
        _source(tmp_path / "source.json"),
        expected_artifact_count=6,
    )
    tfidf, metadata = similarity.compute_tfidf_similarity(records)
    assert tfidf.shape == (6, 6)
    assert np.allclose(np.diag(tfidf), 1.0)
    assert metadata["ngram_range"] == [1, 2]

    embedding = 0.2 + (0.6 * tfidf)
    np.fill_diagonal(embedding, 1.0)
    summary, rows = similarity.summarize_similarity(records, tfidf, embedding)
    assert summary["artifact_count"] == 6
    assert summary["pair_count"] == 15
    assert summary["groups"]["same_task_siblings"]["tfidf"]["pair_count"] == 6
    assert summary["groups"]["all_pairs"]["embedding_minus_tfidf_rank_percentile"][
        "mean"
    ] == pytest.approx(0.0)
    assert len(rows) == 15
    assert all("embedding_minus_tfidf_rank_percentile" in row for row in rows)
    rank_matrix = similarity._pair_metric_matrix(
        records,
        rows,
        field="embedding_minus_tfidf_rank_percentile",
    )
    assert np.allclose(rank_matrix, rank_matrix.T)
    assert np.allclose(np.diag(rank_matrix), 0.0)


def test_selected_pair_is_order_independent() -> None:
    rows = [
        {
            "artifact_a": "art-a",
            "artifact_b": "art-b",
            "tfidf_cosine": 0.1,
            "embedding_cosine": 0.8,
        }
    ]
    assert similarity._selected_pair(rows, ["art-b", "art-a"]) == rows[0]
    with pytest.raises(ValueError, match="must differ"):
        similarity._selected_pair(rows, ["art-a", "art-a"])


def test_semantic_unit_summary_reports_task_and_role_structure(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    records, _ = similarity.load_artifacts(
        _source(tmp_path / "source.json"),
        expected_artifact_count=6,
    )
    calibrated = np.full((6, 6), 0.2, dtype=float)
    np.fill_diagonal(calibrated, 1.0)
    for left in range(3):
        for right in range(left + 1, 3):
            calibrated[left, right] = calibrated[right, left] = 0.9
            calibrated[left + 3, right + 3] = calibrated[right + 3, left + 3] = 0.9
    calibrated[0, 3] = calibrated[3, 0] = 0.3
    calibrated[1, 4] = calibrated[4, 1] = 0.5
    calibrated[2, 5] = calibrated[5, 2] = 0.7
    raw = 0.5 + (0.5 * calibrated)
    np.fill_diagonal(raw, 1.0)

    summary, rows = similarity.summarize_semantic_unit_similarity(
        records,
        calibrated,
        raw,
        calibrated * 0.9 + (np.eye(6) * 0.1),
        calibrated * 0.8 + (np.eye(6) * 0.2),
    )
    assert len(rows) == 15
    assert summary["groups"]["same_task_siblings"]["semantic_unit_coverage"][
        "mean"
    ] == pytest.approx(0.9)
    assert summary["cross_task_same_role"]["text_memory"]["mean"] == pytest.approx(0.3)
    assert summary["cross_task_same_role"]["skill_bundle"]["mean"] == pytest.approx(0.5)
    assert summary["cross_task_same_role"]["agent_system"]["mean"] == pytest.approx(0.7)
    assert summary["groups"]["same_task_siblings"]["general_unit_coverage"][
        "mean"
    ] == pytest.approx(0.81)
    assert all("domain_unit_coverage" in row for row in rows)


def test_general_domain_composition_reports_pooled_and_per_role_shares() -> None:
    rows = [
        {
            "artifact_type": "text_memory",
            "semantic_unit_count": 10,
            "general_unit_count": 4,
            "semantic_unit_word_count": 100,
            "general_unit_word_count": 30,
            "general_unit_fraction": 0.4,
            "general_word_fraction": 0.3,
            "mean_other_task_match_fraction": 0.5,
            "word_weighted_other_task_match_fraction": 0.45,
        },
        {
            "artifact_type": "skill_bundle",
            "semantic_unit_count": 20,
            "general_unit_count": 12,
            "semantic_unit_word_count": 200,
            "general_unit_word_count": 140,
            "general_unit_fraction": 0.6,
            "general_word_fraction": 0.7,
            "mean_other_task_match_fraction": 0.65,
            "word_weighted_other_task_match_fraction": 0.7,
        },
        {
            "artifact_type": "agent_system",
            "semantic_unit_count": 30,
            "general_unit_count": 24,
            "semantic_unit_word_count": 300,
            "general_unit_word_count": 240,
            "general_unit_fraction": 0.8,
            "general_word_fraction": 0.8,
            "mean_other_task_match_fraction": 0.8,
            "word_weighted_other_task_match_fraction": 0.8,
        },
    ]

    summary = similarity.summarize_general_domain_composition(rows)

    assert summary["overall"]["pooled_general_unit_fraction"] == pytest.approx(2 / 3)
    assert summary["overall"]["pooled_general_word_fraction"] == pytest.approx(410 / 600)
    assert summary["by_artifact_type"]["agent_system"][
        "pooled_general_unit_fraction"
    ] == pytest.approx(0.8)


def test_csv_output_uses_repository_safe_lf_line_endings(tmp_path: Path) -> None:
    output = tmp_path / "matrix.csv"
    similarity._write_csv(output, [{"a": "x", "b": 1}], ["a", "b"])
    assert output.read_bytes() == b"a,b\nx,1\n"


def test_manual_scope_annotations_bind_source_and_every_semantic_unit(tmp_path: Path) -> None:
    source_path = _source(tmp_path / "source.json")
    records, _ = similarity.load_artifacts(source_path, expected_artifact_count=6)
    units = [similarity.extract_semantic_units(record.content) for record in records]
    canonical_units = manual_scope._canonical_unit_inventory(records, units)
    annotations = {
        "schema_version": manual_scope.ANNOTATION_SCHEMA_VERSION,
        "status": "FULL_CORPUS_RUBRIC_ADJUDICATION_COMPLETE",
        "reviewed_unit_count": 6,
        "source": {"sha256": similarity.file_sha256(source_path)},
        "semantic_unit_inventory_sha256": similarity.canonical_sha256(canonical_units),
        "adjudication_method": {},
        "task_annotations": {
            "chemcrow-01": {
                "unit_range": [0, 2],
                "hybrid_indices": [1],
                "task_specific_indices": [2],
                "review_note": "test",
            },
            "chemcrow-02": {
                "unit_range": [3, 5],
                "hybrid_indices": [],
                "task_specific_indices": [5],
                "review_note": "test",
            },
        },
    }
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(json.dumps(annotations), encoding="utf-8")

    labels, _, digest = manual_scope.load_manual_scope_labels(
        annotation_path,
        source_path=source_path,
        records=records,
        units_by_artifact=units,
    )

    assert labels == (
        "General",
        "Hybrid",
        "Task-specific",
        "General",
        "General",
        "Task-specific",
    )
    assert digest == annotations["semantic_unit_inventory_sha256"]


def test_manual_scope_annotations_reject_unit_inventory_drift(tmp_path: Path) -> None:
    source_path = _source(tmp_path / "source.json")
    records, _ = similarity.load_artifacts(source_path, expected_artifact_count=6)
    units = [similarity.extract_semantic_units(record.content) for record in records]
    annotations = {
        "schema_version": manual_scope.ANNOTATION_SCHEMA_VERSION,
        "status": "FULL_CORPUS_RUBRIC_ADJUDICATION_COMPLETE",
        "reviewed_unit_count": 6,
        "source": {"sha256": similarity.file_sha256(source_path)},
        "semantic_unit_inventory_sha256": "0" * 64,
        "task_annotations": {},
    }
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(json.dumps(annotations), encoding="utf-8")

    with pytest.raises(ValueError, match="semantic-unit inventory SHA256 differs"):
        manual_scope.load_manual_scope_labels(
            annotation_path,
            source_path=source_path,
            records=records,
            units_by_artifact=units,
        )


def test_manual_scope_component_summary_preserves_missing_values() -> None:
    summary = manual_scope._describe_finite([0.2, float("nan"), 0.8])
    assert summary["available_pair_count"] == 2
    assert summary["missing_pair_count"] == 1
    assert summary["mean"] == pytest.approx(0.5)

    empty = manual_scope._describe_finite([float("nan")])
    assert empty["available_pair_count"] == 0
    assert empty["mean"] is None


def test_manual_scope_report_uses_current_counts_and_dynamic_task_extremes(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.json"
    annotation_path = tmp_path / "annotations.json"
    source_path.write_text("{}", encoding="utf-8")
    annotation_path.write_text("{}", encoding="utf-8")

    def described(mean: float) -> dict[str, object]:
        return {
            "available_pair_count": 1,
            "missing_pair_count": 0,
            "mean": mean,
            "median": mean,
            "minimum": mean,
            "maximum": mean,
        }

    role_composition = {
        artifact_type: {
            "general_unit_fraction": 0.5,
            "hybrid_unit_fraction": 0.25,
            "task_specific_unit_fraction": 0.25,
        }
        for artifact_type in similarity.ARTIFACT_TYPES
    }
    components = ("combined", "general", "hybrid", "task_specific")
    group = {component: described(0.5) for component in components}
    role_similarity = {
        artifact_type: {component: described(0.5) for component in components}
        for artifact_type in similarity.ARTIFACT_TYPES
    }
    summary = {
        "artifact_count": 6,
        "composition": {
            "overall": {
                "semantic_unit_count": 7,
                "general_unit_count": 3,
                "general_unit_fraction": 3 / 7,
                "general_word_count": 30,
                "general_word_fraction": 0.3,
                "hybrid_unit_count": 2,
                "hybrid_unit_fraction": 2 / 7,
                "hybrid_word_count": 20,
                "hybrid_word_fraction": 0.2,
                "task_specific_unit_count": 2,
                "task_specific_unit_fraction": 2 / 7,
                "task_specific_word_count": 50,
                "task_specific_word_fraction": 0.5,
            },
            "by_artifact_type": role_composition,
            "by_task": {
                "chemcrow-01": {
                    "task_specific_unit_count": 0,
                    "task_specific_unit_fraction": 0.0,
                },
                "chemcrow-02": {
                    "task_specific_unit_count": 2,
                    "task_specific_unit_fraction": 0.5,
                },
            },
        },
        "groups": {
            name: group
            for name in (
                "all_pairs",
                "same_task_siblings",
                "same_type_cross_task",
                "different_type_cross_task",
            )
        },
        "cross_task_same_role": role_similarity,
        "highest_combined_pairs": [],
        "same_task_alignment_effect": 0.1,
        "exact_content_duplicate_count": 0,
        "semantic_near_duplicate_threshold": 0.95,
        "semantic_near_duplicate_pair_count": 0,
    }
    result = manual_scope.ManualScopeResult(
        matrices={},
        metadata={
            "embedding": {"model_name": "test-model", "embedding_dimension": 384},
            "background_calibration": {"floor_cosine": 0.1, "cap_cosine": 0.9},
        },
        artifact_composition=(),
        unit_inventory=(),
        pair_rows=(),
        summary=summary,
    )

    report = manual_scope._markdown_report(
        source_path=source_path,
        annotation_path=annotation_path,
        result=result,
    )

    assert "All 7 semantic units" in report
    assert "Tasks 01 contain no strictly Task-specific unit" in report
    assert "Task 02 has the largest Task-specific unit share (0.500)" in report
    assert "ChemCrow v5" not in report
