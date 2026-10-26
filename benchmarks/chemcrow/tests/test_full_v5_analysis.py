from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks.chemcrow.scripts import build_full_v5_analysis as analysis

from openevo_chemcrow.hashing import file_sha256


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _scores(value: int) -> dict[str, int]:
    return {dimension: value for dimension in analysis.DIMENSIONS}


def _pair(task_id: str, ordinal: int) -> dict[str, object]:
    artifacts = []
    receipt: dict[str, object] = {
        "artifact_count": 3,
        "unexpected_artifact_ids": [],
    }
    receipt_fields = {
        "text_memory": "memory_artifact_id",
        "skill_bundle": "skill_artifact_id",
        "agent_system": "agent_system_artifact_id",
    }
    for artifact_index, artifact_type in enumerate(analysis.ARTIFACT_TYPES):
        artifact_id = f"art-{ordinal:02d}-{artifact_index}"
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "reflector_job_id": f"job-{ordinal:02d}-{artifact_index}",
                "reflector_run_id": f"run-{ordinal:02d}-{artifact_index}",
                "sibling_outputs_visible": False,
                "task_id": task_id,
            }
        )
        receipt[receipt_fields[artifact_type]] = artifact_id
    return {
        "schema_version": analysis.PAIR_RESULT_SCHEMA_VERSION,
        "task_id": task_id,
        "pair_id": f"pair-{ordinal:02d}",
        "baseline_internal_evaluation": {"scores": _scores(3)},
        "evolved_internal_evaluation": {"scores": _scores(4)},
        "final_evaluation": {
            "baseline_scores": _scores(2),
            "evolved_scores": _scores(3),
        },
        "artifact_bundle": {
            "task_id": task_id,
            "protocol": analysis.SEALED_PAIR_ARTIFACT_PROTOCOL,
            "artifacts": artifacts,
            "byte_identical_pairs": [],
            "normalized_identical_pairs": [],
            "near_duplicate_pairs": [],
        },
        "injection_receipt": {
            "schema_version": analysis.INJECTION_RECEIPT_SCHEMA_VERSION,
            **receipt,
        },
        "reset_receipt_sha256": "a" * 64,
    }


def _sealed_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    run_root = tmp_path / "fresh-run"
    for ordinal, task_id in enumerate(analysis.TASK_IDS):
        _write_json(run_root / f"pair--{task_id}" / "pair.result.json", _pair(task_id, ordinal))
    aggregate = tmp_path / "aggregate.json"
    _write_json(
        aggregate,
        {
            "schema_version": analysis.AGGREGATE_SCHEMA_VERSION,
            "task_count": 14,
            "artifact_count": 42,
            "artifact_protocol": analysis.AGGREGATE_AUDIT_ARTIFACT_PROTOCOL,
            "mean_baseline": _scores(2),
            "mean_evolved": _scores(3),
        },
    )
    audit = tmp_path / "completed.audit.json"
    _write_json(
        audit,
        {
            "schema_version": analysis.COMPLETED_AUDIT_SCHEMA_VERSION,
            "status": "PASS",
            "answers_included": False,
            "task_count": 14,
            "task_ids": list(analysis.TASK_IDS),
            "aggregate_sha256": file_sha256(aggregate),
            "artifact_protocol": analysis.AGGREGATE_AUDIT_ARTIFACT_PROTOCOL,
            "artifact_registration_count": 42,
            "unique_artifact_count": 42,
            "independent_reflector_job_count": 42,
            "independent_reflector_run_count": 42,
            "unique_reflector_job_count": 42,
            "unique_reflector_run_count": 42,
            "memory_reflector_job_count": 14,
            "skill_reflector_job_count": 14,
            "agent_system_reflector_job_count": 14,
            "core_evolved_injection_receipt_count": 14,
            "reset_receipt_count": 14,
            "sibling_isolation_evidence_count": 42,
            "mock_or_fixture_observations": 0,
        },
    )
    return run_root, aggregate, audit


def _materialize_artifacts(run_root: Path, artifact_state_root: Path) -> None:
    for pair_path in sorted(run_root.glob("*/pair.result.json")):
        pair = json.loads(pair_path.read_text(encoding="utf-8"))
        for artifact in pair["artifact_bundle"]["artifacts"]:
            content = f"artifact {artifact['artifact_id']} evidence procedure\n"
            payload = artifact_state_root / "workers" / artifact["reflector_job_id"] / "payload.md"
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_text(content, encoding="utf-8")
            artifact["size_bytes"] = len(content.encode("utf-8"))
            artifact["artifact_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            _write_json(
                artifact_state_root
                / "artifacts"
                / artifact["artifact_type"]
                / artifact["artifact_id"]
                / "manifest.json",
                {"uri": f"file://{payload}"},
            )
        _write_json(pair_path, pair)


def _paper_ledger_audit(*, recovery: bool) -> dict[str, object]:
    attempt_count = 43 if recovery else 42
    excluded_count = 1 if recovery else 0
    return {
        "production_ledger_class": "production",
        "claim_count": attempt_count,
        "receipt_count": attempt_count,
        "actual_upstream_call_count": attempt_count,
        "valid_target_result_count": 42,
        "excluded_infrastructure_attempt_count": excluded_count,
        "explicit_replacement_count": excluded_count,
        "unique_claim_call_id_count": attempt_count,
        "unique_receipt_call_id_count": attempt_count,
        "unique_result_call_id_count": 42,
        "unique_provider_response_id_count": attempt_count,
        "json_valid_count": 42,
        "pydantic_valid_count": 42,
        "openai_provider_count": attempt_count,
        "valid_result_openai_provider_count": 42,
        "fallback_false_count": attempt_count,
        "environment_proxy_count": attempt_count,
        "actual_openrouter_reported_cost_usd": 1.25,
        "list_price_cost_usd": 1.5,
        "automatic_provider_retries": False,
        "failed_call_id_reuse": False,
        "score_driven_retry": False,
    }


def _paper_analysis_fixture(
    tmp_path: Path,
    *,
    recovery: bool,
) -> tuple[Path, dict[str, dict[str, object]], list[dict[str, object]], dict[str, object]]:
    root = tmp_path / "paper"
    plan_path = root / "private" / "plan.json"
    result_root = root / "results"
    receipt_root = root / "openrouter-receipts"
    result_root.mkdir(parents=True)
    receipt_root.mkdir()
    calls: list[dict[str, object]] = []
    pairs: dict[str, dict[str, object]] = {}
    failed_call_id = "paper-test-chemcrow-08-baseline"
    assessment = {
        "student_a": {
            "grade": 8,
            "strengths": [],
            "weaknesses": [],
            "justification": "bound",
            "feedback": [],
        },
        "student_b": {
            "grade": 7,
            "strengths": [],
            "weaknesses": [],
            "justification": "bound",
            "feedback": [],
        },
    }
    for task_id in analysis.TASK_IDS:
        pair = {
            "pair_id": f"pair-{task_id}",
            "baseline": {"run_id": f"g1-{task_id}", "answer": f"g1 answer {task_id}"},
            "evolved": {"run_id": f"g2-{task_id}", "answer": f"g2 answer {task_id}"},
        }
        pairs[task_id] = pair
        for comparison in ("historical_control", "baseline", "evolved"):
            call_id = f"paper-test-{task_id}-{comparison}"
            call: dict[str, object] = {
                "call_id": call_id,
                "task_id": task_id,
                "comparison": comparison,
                "prompt_sha256": "b" * 64,
            }
            if comparison in {"baseline", "evolved"}:
                output = pair[comparison]
                call.update(
                    {
                        "source_pair_id": pair["pair_id"],
                        "source_pair_result_sha256": "pair-hash",
                        "source_output_id": output["run_id"],
                        "source_output_sha256": analysis.canonical_sha256(output["answer"]),
                        "target_answer_sha256": analysis.canonical_sha256(output["answer"]),
                    }
                )
            calls.append(call)
            result_call_id = (
                f"{call_id}-replacement-01" if recovery and call_id == failed_call_id else call_id
            )
            _write_json(
                result_root / f"{result_call_id}.result.json",
                {
                    "call_id": result_call_id,
                    "task_id": task_id,
                    "comparison": comparison,
                    "assessment": assessment,
                },
            )
    plan = {
        "protocol": "paper-test-protocol",
        "source_pair_protocol": analysis.SEALED_PAIR_ARTIFACT_PROTOCOL,
        "prompt_candidate_sha256": analysis.FROZEN_PAPER_PROMPT_SHA256,
        "calls": calls,
    }
    _write_json(plan_path, plan)
    ledger_audit = _paper_ledger_audit(recovery=recovery)
    _write_json(
        result_root / "aggregate.json",
        {
            "schema_version": "chemcrow_paper_evaluator_aggregate_v1",
            "status": "PROVISIONAL_LLM_JUDGED_RESULT",
            "protocol": plan["protocol"],
            "model": "openai/gpt-4",
            "task_count": 14,
            "result_count": 42,
            "reflector_access": False,
            "final_evaluation_only": True,
            "plan_sha256": file_sha256(plan_path),
            **ledger_audit,
        },
    )
    return root, pairs, calls, ledger_audit


def _patch_paper_plan_validation(monkeypatch: pytest.MonkeyPatch, calls: list[dict]) -> None:
    monkeypatch.setattr(analysis, "validate_paper_evaluation_plan", lambda _plan: calls)
    monkeypatch.setattr(analysis, "_canonical_pair_sha256", lambda _pair: "pair-hash")


def test_validate_current_run_requires_explicit_bound_aggregate_and_audit(
    tmp_path: Path,
) -> None:
    run_root, aggregate, audit = _sealed_run(tmp_path)

    pairs, paths, evidence = analysis._validate_current_run(
        run_root=run_root,
        aggregate_path=aggregate,
        completed_audit_path=audit,
    )

    assert set(pairs) == set(analysis.TASK_IDS)
    assert set(paths) == set(analysis.TASK_IDS)
    assert evidence["status"] == "PASS"
    assert evidence["unique_reflector_job_count"] == 42
    assert evidence["aggregate_sha256"] == file_sha256(aggregate)

    broken = json.loads(audit.read_text(encoding="utf-8"))
    broken["aggregate_sha256"] = "0" * 64
    _write_json(audit, broken)
    with pytest.raises(ValueError, match="not bound"):
        analysis._validate_current_run(
            run_root=run_root,
            aggregate_path=aggregate,
            completed_audit_path=audit,
        )


def test_validate_current_run_accepts_distinct_audit_and_pair_protocols(
    tmp_path: Path,
) -> None:
    run_root, aggregate, audit = _sealed_run(tmp_path)

    assert analysis.AGGREGATE_AUDIT_ARTIFACT_PROTOCOL != analysis.SEALED_PAIR_ARTIFACT_PROTOCOL
    _, _, evidence = analysis._validate_current_run(
        run_root=run_root,
        aggregate_path=aggregate,
        completed_audit_path=audit,
    )

    assert evidence["status"] == "PASS"


@pytest.mark.parametrize("authority", ["completed_audit", "sealed_pair"])
def test_validate_current_run_rejects_wrong_protocol_authority(
    tmp_path: Path,
    authority: str,
) -> None:
    run_root, aggregate, audit = _sealed_run(tmp_path)
    if authority == "completed_audit":
        payload = json.loads(audit.read_text(encoding="utf-8"))
        payload["artifact_protocol"] = analysis.SEALED_PAIR_ARTIFACT_PROTOCOL
        _write_json(audit, payload)
        expected_message = "completed-run audit artifact protocol differs"
    else:
        pair_path = min(run_root.glob("*/pair.result.json"))
        payload = json.loads(pair_path.read_text(encoding="utf-8"))
        payload["artifact_bundle"]["protocol"] = analysis.AGGREGATE_AUDIT_ARTIFACT_PROTOCOL
        _write_json(pair_path, payload)
        expected_message = "bundle protocol differs"

    with pytest.raises(ValueError, match=expected_message):
        analysis._validate_current_run(
            run_root=run_root,
            aggregate_path=aggregate,
            completed_audit_path=audit,
        )


@pytest.mark.parametrize(
    "field",
    [
        "independent_reflector_run_count",
        "unique_reflector_job_count",
        "unique_reflector_run_count",
    ],
)
def test_validate_current_run_requires_all_reflector_counts_exactly_42(
    tmp_path: Path,
    field: str,
) -> None:
    run_root, aggregate, audit = _sealed_run(tmp_path)
    payload = json.loads(audit.read_text(encoding="utf-8"))
    payload[field] = 41
    _write_json(audit, payload)

    with pytest.raises(ValueError, match=field):
        analysis._validate_current_run(
            run_root=run_root,
            aggregate_path=aggregate,
            completed_audit_path=audit,
        )


@pytest.mark.parametrize(
    ("authority", "expected_message"),
    [
        ("pair", "pair schema version differs"),
        ("injection", "injection receipt schema version differs"),
    ],
)
def test_validate_current_run_rejects_wrong_pair_or_injection_schema(
    tmp_path: Path,
    authority: str,
    expected_message: str,
) -> None:
    run_root, aggregate, audit = _sealed_run(tmp_path)
    pair_path = min(run_root.glob("*/pair.result.json"))
    payload = json.loads(pair_path.read_text(encoding="utf-8"))
    if authority == "pair":
        payload["schema_version"] = "wrong"
    else:
        payload["injection_receipt"]["schema_version"] = "wrong"
    _write_json(pair_path, payload)

    with pytest.raises(ValueError, match=expected_message):
        analysis._validate_current_run(
            run_root=run_root,
            aggregate_path=aggregate,
            completed_audit_path=audit,
        )


def test_historical_v4_is_hash_bound_report_only() -> None:
    path = Path("benchmarks/chemcrow/reports/FULL_V5_CORE_NATIVE_EXPERIMENT.json")
    expected = file_sha256(path)

    v4, deltas, evidence = analysis._load_historical_v4(
        path,
        expected_sha256=expected,
    )

    assert evidence["mode"] == "tracked_immutable_historical_report_only"
    assert evidence["raw_v4_pair_root_available"] is False
    assert evidence["live_v4_recomputation_performed"] is False
    assert set(deltas) == {
        "internal_evaluator",
        "blind_evaluator",
        "paper_evaluator",
    }
    assert statistics.mean(deltas["paper_evaluator"].values()) == pytest.approx(
        v4["paper_evaluator"]["delta_mean"]
    )

    with pytest.raises(ValueError, match="SHA256 differs"):
        analysis._load_historical_v4(path, expected_sha256="0" * 64)


def test_paired_statistics_include_raw_results_and_exact_tests() -> None:
    deltas = [2, 1, 0, -1, 0, 3, -2, 0, 1, -1, 0, 2, -3, 0]

    stats = analysis._paired_stats(deltas)

    assert stats["raw_paired_deltas"] == [float(value) for value in deltas]
    assert stats["mean_delta"] == pytest.approx(statistics.mean(deltas))
    assert stats["median_delta"] == statistics.median(deltas)
    assert (stats["wins"], stats["ties"], stats["losses"]) == (5, 5, 4)
    assert stats["nonzero_pair_count"] == 9
    assert 0 <= stats["wilcoxon_signed_rank_exact_two_sided_p"] <= 1
    assert 0 <= stats["sign_test_exact_binomial_two_sided_p"] <= 1
    assert len(stats["paired_bootstrap_95_ci_mean_delta"]) == 2


def test_delta_comparison_is_task_paired() -> None:
    historical = {task_id: float(index % 3 - 1) for index, task_id in enumerate(analysis.TASK_IDS)}
    current = {
        task_id: historical[task_id] + (1 if index < 5 else 0 if index < 9 else -1)
        for index, task_id in enumerate(analysis.TASK_IDS)
    }

    result = analysis._delta_comparison(current=current, historical=historical)

    assert (result["improved"], result["same"], result["worsened"]) == (5, 4, 5)
    assert result["per_task"][analysis.TASK_IDS[0]] == {
        "delta_v4": historical[analysis.TASK_IDS[0]],
        "delta_v5": current[analysis.TASK_IDS[0]],
        "delta_v5_minus_delta_v4": 1.0,
    }


def test_output_paths_refuse_overwrite(tmp_path: Path) -> None:
    paths = analysis._output_paths(tmp_path, "RERUN_20260830")
    paths["analysis"].write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        analysis._output_paths(tmp_path, "RERUN_20260830")


def test_missing_fresh_semantic_and_paper_evidence_are_explicit() -> None:
    semantic = analysis._load_semantic_audit(
        None,
        pairs={},
        pair_paths={},
        completed_audit_sha256="a" * 64,
    )
    paper, grades, aggregate = analysis._audit_paper(None, pairs={})

    assert semantic["status"] == "NOT_PROVIDED"
    assert paper["status"] == "NOT_PROVIDED"
    assert paper["call_count"] == 0
    assert grades is None
    assert aggregate is None


@pytest.mark.parametrize(
    ("authority", "core_completions"),
    [(Path("authority.json"), None), (None, Path("core-completions"))],
)
def test_paper_recovery_requires_both_explicit_evidence_arguments(
    tmp_path: Path,
    authority: Path | None,
    core_completions: Path | None,
) -> None:
    with pytest.raises(ValueError, match="requires both replacement authority"):
        analysis._audit_paper(
            tmp_path / "paper",
            pairs={},
            replacement_authority_path=authority,
            core_completion_root=core_completions,
        )


def test_paper_recovery_accepts_42_valid_results_and_43_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, pairs, calls, ledger_audit = _paper_analysis_fixture(tmp_path, recovery=True)
    _patch_paper_plan_validation(monkeypatch, calls)
    authority_path = root / "private" / "recovery" / "failed.replacement.json"
    _write_json(authority_path, {"sealed": True})
    core_completions = tmp_path / "core-completions"
    core_completions.mkdir()
    source_run = tmp_path / "source-run"
    source_run.mkdir()
    completed_audit = source_run / "completed_run.audit.json"
    _write_json(completed_audit, {"status": "PASS"})
    authority = SimpleNamespace(
        original_call_id="paper-test-chemcrow-08-baseline",
        replacement_call_id="paper-test-chemcrow-08-baseline-replacement-01",
    )
    observed: dict[str, object] = {}

    def fake_load(**kwargs):
        observed["load"] = kwargs
        return authority

    def fake_audit(**kwargs):
        observed["audit"] = kwargs
        return ledger_audit

    monkeypatch.setattr(analysis, "load_paper_replacement_authority", fake_load)
    monkeypatch.setattr(analysis, "_audit_production_ledger", fake_audit)

    audit, grades, aggregate = analysis._audit_paper(
        root,
        pairs=pairs,
        source_run_root=source_run,
        completed_run_audit=completed_audit,
        replacement_authority_path=authority_path,
        core_completion_root=core_completions,
    )

    assert audit["status"] == "PASS"
    assert audit["call_count"] == audit["valid_target_result_count"] == 42
    assert audit["actual_upstream_call_count"] == 43
    assert audit["claim_count"] == audit["receipt_count"] == 43
    assert audit["excluded_infrastructure_attempt_count"] == 1
    assert audit["replacement_authority_used"] is True
    assert audit["actual_openrouter_reported_cost_usd"] == 1.25
    assert grades is not None and len(grades) == 14
    assert aggregate is not None
    assert observed["load"]["core_completion_root"] == core_completions
    assert observed["audit"]["replacement_authority"] is authority
    assert observed["audit"]["replacement_authority_path"] == authority_path


def test_paper_without_replacement_uses_strict_42_call_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, pairs, calls, ledger_audit = _paper_analysis_fixture(tmp_path, recovery=False)
    _patch_paper_plan_validation(monkeypatch, calls)

    def fake_audit(**kwargs):
        assert kwargs["replacement_authority"] is None
        assert kwargs["replacement_authority_path"] is None
        return ledger_audit

    monkeypatch.setattr(analysis, "_audit_production_ledger", fake_audit)

    audit, grades, _ = analysis._audit_paper(root, pairs=pairs)

    assert audit["call_count"] == audit["actual_upstream_call_count"] == 42
    assert audit["excluded_infrastructure_attempt_count"] == 0
    assert audit["replacement_authority_used"] is False
    assert grades is not None and len(grades) == 14


@pytest.mark.parametrize(
    ("field", "drifted"),
    [("claim_count", 42), ("actual_openrouter_reported_cost_usd", 1.24)],
)
def test_paper_aggregate_must_match_every_recomputed_ledger_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    drifted: object,
) -> None:
    root, pairs, calls, ledger_audit = _paper_analysis_fixture(tmp_path, recovery=True)
    _patch_paper_plan_validation(monkeypatch, calls)
    aggregate_path = root / "results" / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate[field] = drifted
    _write_json(aggregate_path, aggregate)
    authority_path = root / "private" / "recovery" / "failed.replacement.json"
    _write_json(authority_path, {"sealed": True})
    core_completions = tmp_path / "core-completions"
    core_completions.mkdir()
    source_run = tmp_path / "source-run"
    source_run.mkdir()
    completed_audit = source_run / "completed_run.audit.json"
    _write_json(completed_audit, {"status": "PASS"})
    monkeypatch.setattr(
        analysis,
        "load_paper_replacement_authority",
        lambda **_kwargs: SimpleNamespace(
            original_call_id="paper-test-chemcrow-08-baseline",
            replacement_call_id="paper-test-chemcrow-08-baseline-replacement-01",
        ),
    )
    monkeypatch.setattr(
        analysis,
        "_audit_production_ledger",
        lambda **_kwargs: ledger_audit,
    )

    with pytest.raises(ValueError, match=rf"paper aggregate {field} differs"):
        analysis._audit_paper(
            root,
            pairs=pairs,
            source_run_root=source_run,
            completed_run_audit=completed_audit,
            replacement_authority_path=authority_path,
            core_completion_root=core_completions,
        )


def test_paper_extra_ledger_entry_rejected_by_production_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, pairs, calls, _ = _paper_analysis_fixture(tmp_path, recovery=False)
    _patch_paper_plan_validation(monkeypatch, calls)

    def reject_extra(**_kwargs):
        raise ValueError("paper production ledger contains calls outside the frozen plan")

    monkeypatch.setattr(analysis, "_audit_production_ledger", reject_extra)

    with pytest.raises(ValueError, match="outside the frozen plan"):
        analysis._audit_paper(root, pairs=pairs)


def test_offline_main_writes_only_new_incomplete_reports(tmp_path: Path) -> None:
    run_root, aggregate, audit = _sealed_run(tmp_path)
    artifact_state_root = tmp_path / "artifact-state"
    _materialize_artifacts(run_root, artifact_state_root)
    historical = Path("benchmarks/chemcrow/reports/FULL_V5_CORE_NATIVE_EXPERIMENT.json")
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    argv = [
        "--run-root",
        str(run_root),
        "--aggregate",
        str(aggregate),
        "--completed-audit",
        str(audit),
        "--core-artifacts-root",
        str(artifact_state_root),
        "--historical-v4-report",
        str(historical),
        "--historical-v4-report-sha256",
        file_sha256(historical),
        "--output-dir",
        str(output_dir),
        "--output-prefix",
        "FRESH_TEST",
    ]

    assert analysis.main(argv) == 0
    machine = json.loads((output_dir / "FRESH_TEST_EXPERIMENT.json").read_text())
    assert machine["status"] == "ANALYSIS_INCOMPLETE_MISSING_REQUIRED_EVIDENCE"
    assert machine["components"] == {
        "engineering": "PASS",
        "paper": "NOT_PROVIDED",
        "semantic": "NOT_PROVIDED",
    }
    assert len(list(output_dir.iterdir())) == 5

    with pytest.raises(ValueError, match="refusing to overwrite"):
        analysis.main(argv)
