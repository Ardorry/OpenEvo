from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from openevo_chemcrow.hashing import file_sha256


def _load_report_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_core_native_preflight_report.py"
    )
    spec = importlib.util.spec_from_file_location("build_core_native_preflight_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPORT = _load_report_module()
TASK_IDS = ("chemcrow-02", "chemcrow-05", "chemcrow-13")
EXPERIMENT_ID = "fresh-core-native-preflight"


def _completed_audit() -> dict:
    return {
        "schema_version": "chemcrow_three_artifact_completed_run_audit_v1",
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "task_ids": list(TASK_IDS),
        "task_count": 3,
        "unique_artifact_count": 9,
        "independent_reflector_job_count": 9,
        "artifact_registration_count": 9,
        "sibling_isolation_evidence_count": 9,
        "core_evolved_injection_receipt_count": 3,
        "reset_receipt_count": 3,
        "mock_or_fixture_observations": 0,
        "artifact_type_counts": {
            "agent_system": 3,
            "skill_bundle": 3,
            "text_memory": 3,
        },
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _fake_pair(*, task_id: str, reset_sha256: str) -> SimpleNamespace:
    artifacts = [
        SimpleNamespace(
            reflector_job_id=f"job-{task_id}-{index}",
            reflector_run_id=f"run-{task_id}-{index}",
            artifact_id=f"artifact-{task_id}-{index}",
        )
        for index in range(3)
    ]
    return SimpleNamespace(
        task_id=task_id,
        pair_id=f"{EXPERIMENT_ID}--{task_id}",
        artifact_bundle=SimpleNamespace(artifacts=artifacts),
        injection_receipt=SimpleNamespace(),
        reset_receipt_sha256=reset_sha256,
        baseline=SimpleNamespace(observations=[]),
        evolved=SimpleNamespace(observations=[]),
    )


def _pair_run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    run_root = tmp_path / "run"
    pairs = {}
    for task_id in TASK_IDS:
        item_root = run_root / f"{EXPERIMENT_ID}--{task_id}"
        reset = item_root / "reset.receipt.json"
        _write_json(reset, {"task_id": task_id, "status": "reset"})
        pair = _fake_pair(task_id=task_id, reset_sha256=file_sha256(reset))
        (item_root / "pair.result.json").write_text(task_id, encoding="utf-8")
        pairs[task_id] = pair
    monkeypatch.setattr(
        REPORT.ThreeArtifactPairResult,
        "model_validate_json",
        staticmethod(lambda marker: pairs[marker]),
    )
    return run_root, pairs


def test_completed_run_audit_must_be_exact_pass_authority(tmp_path):
    audit_path = tmp_path / "completed.audit.json"
    audit = _completed_audit()
    _write_json(audit_path, audit)

    assert REPORT._validate_completed_run_audit(audit_path) == audit

    for field, invalid in (
        ("status", "FAIL"),
        ("task_ids", ["chemcrow-02", "chemcrow-05"]),
        ("independent_reflector_job_count", 8),
        ("core_evolved_injection_receipt_count", 2),
        ("reset_receipt_count", 2),
        ("mock_or_fixture_observations", 1),
    ):
        damaged = _completed_audit()
        damaged[field] = invalid
        _write_json(audit_path, damaged)
        with pytest.raises(ValueError):
            REPORT._validate_completed_run_audit(audit_path)


def test_pair_binding_accepts_exact_inventory(tmp_path, monkeypatch):
    run_root, _ = _pair_run_root(tmp_path, monkeypatch)
    audit = _completed_audit()

    bound = REPORT._load_exact_pairs(run_root=run_root, completed_run_audit=audit)

    assert [pair.task_id for _, pair in bound] == list(TASK_IDS)


@pytest.mark.parametrize(
    ("attribute", "message"),
    (
        ("reflector_job_id", "nine unique Reflector job IDs"),
        ("reflector_run_id", "nine unique Reflector run IDs"),
        ("artifact_id", "nine unique artifact IDs"),
    ),
)
def test_pair_binding_rejects_cross_task_identity_reuse(tmp_path, monkeypatch, attribute, message):
    run_root, pairs = _pair_run_root(tmp_path, monkeypatch)
    audit = _completed_audit()
    reused = getattr(pairs["chemcrow-02"].artifact_bundle.artifacts[0], attribute)
    setattr(pairs["chemcrow-13"].artifact_bundle.artifacts[-1], attribute, reused)

    with pytest.raises(ValueError, match=message):
        REPORT._load_exact_pairs(run_root=run_root, completed_run_audit=audit)


@pytest.mark.parametrize("source", ("mock", "fixture"))
def test_pair_binding_rejects_non_real_observations(tmp_path, monkeypatch, source):
    run_root, pairs = _pair_run_root(tmp_path, monkeypatch)
    pairs["chemcrow-05"].baseline.observations = [SimpleNamespace(source=source)]

    with pytest.raises(ValueError, match="mock or fixture"):
        REPORT._load_exact_pairs(
            run_root=run_root,
            completed_run_audit=_completed_audit(),
        )


def test_pair_binding_rejects_extra_or_missing_pair_result(tmp_path, monkeypatch):
    run_root, _ = _pair_run_root(tmp_path, monkeypatch)
    audit = _completed_audit()
    extra = run_root / "unexpected" / "pair.result.json"
    extra.parent.mkdir(parents=True)
    extra.write_text("unexpected", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly three"):
        REPORT._load_exact_pairs(run_root=run_root, completed_run_audit=audit)

    extra.unlink()
    (run_root / f"{EXPERIMENT_ID}--chemcrow-13" / "pair.result.json").unlink()
    with pytest.raises(ValueError, match="exactly three"):
        REPORT._load_exact_pairs(run_root=run_root, completed_run_audit=audit)


def test_frozen_historical_report_supplies_stats_without_raw_sqlite(tmp_path):
    historical = tmp_path / "historical.json"
    rows = [
        {
            "task_id": task_id,
            "v4": {
                "artifact_word_count": 100 + index,
                "prohibition_line_count": index,
                "blind_total_delta": float(index - 1),
                "blind_winner": "tie",
            },
        }
        for index, task_id in enumerate(TASK_IDS)
    ]
    _write_json(
        historical,
        {
            "schema_version": "chemcrow_core_native_preflight_report_v1",
            "status": "PASS",
            "task_ids": list(TASK_IDS),
            "v4_v5_preflight_comparison": rows,
        },
    )

    stats, authority = REPORT._load_historical_mechanism_stats(
        argparse.Namespace(
            historical_report_json=historical,
            historical_run_root=None,
            historical_evolution_db=None,
        )
    )

    assert stats is not None and set(stats) == set(TASK_IDS)
    assert authority["status"] == "AVAILABLE"
    assert authority["source_kind"] == "frozen_report_json"
    assert authority["raw_artifacts_revalidated"] is False
    assert authority["source_sha256"] == file_sha256(historical)


def test_missing_historical_raw_authority_yields_honest_v5_only_metadata(tmp_path):
    stats, authority = REPORT._load_historical_mechanism_stats(
        argparse.Namespace(
            historical_report_json=None,
            historical_run_root=tmp_path / "missing-run",
            historical_evolution_db=tmp_path / "missing.sqlite3",
        )
    )

    assert stats is None
    assert authority["status"] == "UNAVAILABLE"
    assert authority["source_kind"] == "raw_historical_authority"
    assert authority["raw_artifacts_revalidated"] is False
    assert authority["limitations"]


def test_historical_report_rejects_ambiguous_second_sqlite_authority(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        REPORT._load_historical_mechanism_stats(
            argparse.Namespace(
                historical_report_json=tmp_path / "historical.json",
                historical_run_root=None,
                historical_evolution_db=tmp_path / "historical.sqlite3",
            )
        )


def test_build_report_rejects_failed_audit_before_other_inputs(tmp_path):
    audit_path = tmp_path / "failed.audit.json"
    audit = _completed_audit()
    audit["status"] = "FAIL"
    _write_json(audit_path, audit)
    args = argparse.Namespace(
        completed_run_audit=audit_path,
        run_root=tmp_path / "missing-run",
        tasks=tmp_path / "missing-tasks",
        semantic_audit=tmp_path / "missing-semantic",
        evolution_db=tmp_path / "missing.sqlite3",
        historical_report_json=None,
        historical_run_root=None,
        historical_evolution_db=None,
        output_json=tmp_path / "new.json",
        output_markdown=tmp_path / "new.md",
    )

    with pytest.raises(ValueError, match="completed-run audit"):
        REPORT.build_report(args)


def test_build_report_refuses_existing_outputs_before_pair_or_artifact_reads(tmp_path):
    audit_path = tmp_path / "completed.audit.json"
    _write_json(audit_path, _completed_audit())
    output = tmp_path / "existing.json"
    output.write_text("immutable historical report\n", encoding="utf-8")
    args = argparse.Namespace(
        completed_run_audit=audit_path,
        run_root=tmp_path / "missing-run",
        tasks=tmp_path / "missing-tasks",
        semantic_audit=tmp_path / "missing-semantic",
        evolution_db=tmp_path / "missing.sqlite3",
        historical_report_json=None,
        historical_run_root=None,
        historical_evolution_db=None,
        output_json=output,
        output_markdown=tmp_path / "new.md",
    )

    with pytest.raises(ValueError, match="refusing to overwrite"):
        REPORT.build_report(args)
    assert output.read_text(encoding="utf-8") == "immutable historical report\n"


def test_markdown_marks_unavailable_historical_values_as_na():
    payload = {
        "status": "PASS",
        "task_ids": list(TASK_IDS),
        "reflector_job_count": 9,
        "unique_artifact_count": 9,
        "injection_receipt_count": 3,
        "reset_receipt_count": 3,
        "mock_count": 0,
        "fixture_count": 0,
        "observation_source_counts": {},
        "completed_run_audit": {"sha256": "a" * 64},
        "historical_comparison_authority": {
            "status": "UNAVAILABLE",
            "source_kind": "none",
            "limitations": ["No historical authority supplied."],
        },
        "tasks": [],
        "v4_v5_preflight_comparison": [
            {
                "task_id": task_id,
                "v4": None,
                "v5": {
                    "artifact_word_count": 10,
                    "prohibition_line_count": 0,
                    "blind_total_delta": 0.0,
                },
                "mechanism_finding": "not compared",
            }
            for task_id in TASK_IDS
        ],
    }

    markdown = REPORT._markdown(payload)

    assert "UNAVAILABLE / none" in markdown
    assert markdown.count("n/a") == 9
    assert "No historical authority supplied." in markdown
