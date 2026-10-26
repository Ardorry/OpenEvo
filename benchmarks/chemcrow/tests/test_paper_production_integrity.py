from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import openevo_chemcrow.paper_evaluator as paper_evaluator_module
from openevo_chemcrow import cli as chemcrow_cli
from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.models import TaskItem, Trajectory
from openevo_chemcrow.paper_core import (
    _audit_production_ledger,
    _validate_claim_and_receipt,
    _validate_production_ledger_layout,
    run_paper_evaluation_plan,
)
from openevo_chemcrow.paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_HISTORICAL_DATASET_SHA256,
    DualStudentAssessment,
    HistoricalAnswers,
    PaperEvaluationCall,
    PaperEvaluationResult,
    build_paper_evaluation_plan,
    load_completed_run_audit_binding,
    validate_paper_evaluation_plan,
    validate_paper_plan_source_audit,
)
from openevo_chemcrow.paper_recovery import (
    authorize_paper_failed_attempt_replacement,
    load_paper_replacement_authority,
    replacement_claim_metadata,
)
from openevo_chemcrow.three_artifact_models import (
    CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL,
    ThreeArtifactBundleReceipt,
    ThreeArtifactPairResult,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _tasks() -> list[TaskItem]:
    return [
        TaskItem.model_validate_json(line)
        for line in (PACKAGE_ROOT / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _historical() -> dict[str, HistoricalAnswers]:
    dataset = json.loads(
        (PACKAGE_ROOT / "data" / "paper_evaluator_historical_calibration.json").read_text(
            encoding="utf-8"
        )
    )
    assert canonical_sha256(dataset) == PAPER_EVALUATOR_HISTORICAL_DATASET_SHA256
    return {
        record["repo_task_id"]: HistoricalAnswers(
            task_id=record["repo_task_id"],
            chemcrow_answer=record["historical_chemcrow_final_answer"],
            gpt4_answer=record["historical_no_tools_gpt4_final_answer"],
            notebook_path=Path(record["source_notebook"]),
            notebook_sha256=record["source_notebook_sha256"],
        )
        for record in dataset["tasks"]
    }


def _pairs(tasks: list[TaskItem]) -> dict[str, ThreeArtifactPairResult]:
    return {
        task.task_id: ThreeArtifactPairResult.model_construct(
            task_id=task.task_id,
            pair_id=f"fresh-full-v5--{task.task_id}",
            artifact_bundle=ThreeArtifactBundleReceipt.model_construct(
                protocol=CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL
            ),
            baseline=Trajectory(
                run_id=f"fresh-g1-{task.task_id}",
                task_id=task.task_id,
                role="baseline",
                status="COMPLETED",
                answer=f"sealed G1 {task.task_id}",
                candidate_config_sha256="s0",
            ),
            evolved=Trajectory(
                run_id=f"fresh-g2-{task.task_id}",
                task_id=task.task_id,
                role="evolved",
                status="COMPLETED",
                answer=f"sealed G2 {task.task_id}",
                candidate_config_sha256="s0",
                artifact_ids=["memory", "skill", "agent-system"],
            ),
        )
        for task in tasks
    }


def _plan() -> dict:
    tasks = _tasks()
    return build_paper_evaluation_plan(
        tasks=tasks,
        pairs=_pairs(tasks),
        historical=_historical(),
        call_id_prefix="paper-fresh-replication",
        source_completed_run_audit_sha256="a" * 64,
        source_aggregate_sha256="b" * 64,
        source_experiment_id="fresh-full-v5",
    )


def _write_valid_source_authority(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "fresh-full-v5"
    run_root.mkdir()
    aggregate = {
        "schema_version": "chemcrow_three_artifact_aggregate_v1",
        "status": "PROVISIONAL_LLM_JUDGED_RESULT",
        "task_count": 14,
        "artifact_count": 42,
        "artifact_protocol": "chemcrow-three-isolated-core-native-artifacts-v2",
    }
    aggregate_path = run_root / "aggregate.json"
    aggregate_path.write_text(json.dumps(aggregate, sort_keys=True), encoding="utf-8")
    audit = {
        "schema_version": "chemcrow_three_artifact_completed_run_audit_v1",
        "status": "PASS",
        "experiment_id": "fresh-full-v5",
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "task_count": 14,
        "artifact_protocol": "chemcrow-three-isolated-core-native-artifacts-v2",
        "artifact_registration_count": 42,
        "unique_artifact_count": 42,
        "unique_reflector_job_count": 42,
        "unique_reflector_run_count": 42,
        "independent_reflector_job_count": 42,
        "independent_reflector_run_count": 42,
        "memory_reflector_job_count": 14,
        "skill_reflector_job_count": 14,
        "agent_system_reflector_job_count": 14,
        "sibling_isolation_evidence_count": 42,
        "core_evolved_injection_receipt_count": 14,
        "reset_receipt_count": 14,
        "mock_or_fixture_observations": 0,
        "aggregate_sha256": file_sha256(aggregate_path),
    }
    audit_path = run_root / "completed_run.audit.json"
    audit_path.write_text(json.dumps(audit, sort_keys=True), encoding="utf-8")
    return run_root, audit_path


def _source_bound_plan(run_root: Path, audit_path: Path) -> dict:
    binding = load_completed_run_audit_binding(
        run_root=run_root,
        experiment_id="fresh-full-v5",
        task_ids=list(FROZEN_PAPER_TASK_IDS),
        completed_run_audit=audit_path,
    )
    return build_paper_evaluation_plan(
        tasks=_tasks(),
        pairs=_pairs(_tasks()),
        historical=_historical(),
        call_id_prefix="paper-fresh-replication",
        source_completed_run_audit_sha256=binding.completed_run_audit_sha256,
        source_aggregate_sha256=binding.aggregate_sha256,
        source_experiment_id=binding.experiment_id,
    )


def test_production_plan_validator_binds_exact_frozen_14_by_3_authority():
    plan = _plan()
    calls = validate_paper_evaluation_plan(plan)

    assert len(calls) == 42
    assert len({call.call_id for call in calls}) == 42
    assert {call.task_id for call in calls} == set(FROZEN_PAPER_TASK_IDS)
    assert plan["environment_proxy_required"] is True
    assert plan["automatic_provider_retries"] is False
    assert plan["failed_call_id_reuse"] is False
    assert plan["source_completed_run_audit_sha256"] == "a" * 64
    assert plan["source_aggregate_sha256"] == "b" * 64


def test_production_plan_validator_rejects_missing_completed_run_audit_hash():
    plan = _plan()
    plan.pop("source_completed_run_audit_sha256")

    with pytest.raises(ValueError, match="schema fields|audit SHA256"):
        validate_paper_evaluation_plan(plan)


def test_paid_source_audit_binding_rejects_wrong_hash(tmp_path):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(run_root, audit_path)
    plan["source_completed_run_audit_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="audit SHA256 differs"):
        validate_paper_plan_source_audit(
            plan=plan,
            run_root=run_root,
            completed_run_audit=audit_path,
        )


def test_paid_source_audit_binding_rejects_symlink(tmp_path):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(run_root, audit_path)
    real_audit = tmp_path / "detached.audit.json"
    audit_path.replace(real_audit)
    audit_path.symlink_to(real_audit)

    with pytest.raises(ValueError, match="regular file|symlink"):
        validate_paper_plan_source_audit(
            plan=plan,
            run_root=run_root,
            completed_run_audit=audit_path,
        )


def test_paid_source_audit_binding_rejects_pathname_replacement(
    tmp_path,
    monkeypatch,
):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(run_root, audit_path)
    replacement = run_root / "replacement.audit.json"
    replacement.write_bytes(audit_path.read_bytes())
    original_open = os.open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == audit_path.name and not replaced and not (flags & os.O_DIRECTORY):
            os.replace(replacement, audit_path)
            replaced = True
        return descriptor

    monkeypatch.setattr(paper_evaluator_module.os, "open", replacing_open)
    with pytest.raises(ValueError, match="identity|replaced"):
        validate_paper_plan_source_audit(
            plan=plan,
            run_root=run_root,
            completed_run_audit=audit_path,
        )
    assert replaced is True


def test_paid_source_audit_binding_rejects_content_change(tmp_path):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(run_root, audit_path)
    audit_path.write_bytes(audit_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="audit SHA256 differs"):
        validate_paper_plan_source_audit(
            plan=plan,
            run_root=run_root,
            completed_run_audit=audit_path,
        )


def test_paid_source_audit_binding_rejects_aggregate_change(tmp_path):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(run_root, audit_path)
    aggregate_path = run_root / "aggregate.json"
    aggregate_path.write_bytes(aggregate_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="aggregate SHA256 differs"):
        validate_paper_plan_source_audit(
            plan=plan,
            run_root=run_root,
            completed_run_audit=audit_path,
        )


def test_paid_source_audit_binding_rejects_non_42_reflector_count(tmp_path):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["unique_reflector_job_count"] = 41
    audit_path.write_text(json.dumps(audit, sort_keys=True), encoding="utf-8")
    plan = _plan()
    plan["source_completed_run_audit_sha256"] = file_sha256(audit_path)
    plan["source_aggregate_sha256"] = audit["aggregate_sha256"]

    with pytest.raises(ValueError, match="three-artifact frozen task inventory"):
        validate_paper_plan_source_audit(
            plan=plan,
            run_root=run_root,
            completed_run_audit=audit_path,
        )


def test_paid_source_audit_binding_accepts_exact_bound_authority(tmp_path):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(run_root, audit_path)

    binding = validate_paper_plan_source_audit(
        plan=plan,
        run_root=run_root,
        completed_run_audit=audit_path,
    )

    assert binding.completed_run_audit_sha256 == file_sha256(audit_path)
    assert binding.aggregate_sha256 == file_sha256(run_root / "aggregate.json")


def test_paid_runner_revalidates_audit_before_network_or_ledger_mutation(
    tmp_path,
    monkeypatch,
):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(run_root, audit_path)
    ledger = tmp_path / "paper-v2"
    plan_path = ledger / "private" / "plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    audit_path.write_bytes(audit_path.read_bytes() + b"\n")
    network_touched = False

    def unexpected_network(_):
        nonlocal network_touched
        network_touched = True
        raise AssertionError("network must remain untouched")

    monkeypatch.setattr(
        "openevo_chemcrow.paper_core._assert_dedicated_core_node",
        unexpected_network,
    )
    with pytest.raises(ValueError, match="audit SHA256 differs"):
        run_paper_evaluation_plan(
            plan_path=plan_path,
            rollout_base_url="http://127.0.0.1:1",
            runtime={},
            result_root=ledger / "results",
            shim_receipt_root=ledger / "openrouter-receipts",
            historical_runs_root=tmp_path / "historical",
            source_run_root=run_root,
            completed_run_audit=audit_path,
            allow_paid=True,
        )
    assert network_touched is False
    assert not (ledger / "results").exists()


def test_preflight_persists_exact_source_audit_binding(tmp_path, monkeypatch):
    run_root, audit_path = _write_valid_source_authority(tmp_path)
    plan_path = tmp_path / "paper-v2" / "private" / "plan.json"
    output_path = tmp_path / "paper-v2" / "preflight.json"
    config_path = tmp_path / "paper-v2.yaml"
    config_path.write_text("placeholder: true\n", encoding="utf-8")
    config = {
        "task_manifest": str(tmp_path / "tasks.jsonl"),
        "historical_runs_root": str(tmp_path / "historical"),
        "run_root": str(run_root),
        "completed_run_audit": str(audit_path),
        "experiment_id": "fresh-full-v5",
        "source_pair_protocol": "chemcrow-three-isolated-core-native-artifacts-v2",
        "call_id_prefix": "paper-fresh-replication",
        "plan_path": str(plan_path),
    }
    tasks = _tasks()
    monkeypatch.setattr(chemcrow_cli, "_load_yaml", lambda _: config)
    monkeypatch.setattr(chemcrow_cli, "_read_tasks", lambda _: tasks)
    monkeypatch.setattr(
        chemcrow_cli,
        "assert_sealed_run_ready",
        lambda **_: _pairs(tasks),
    )
    monkeypatch.setattr(
        chemcrow_cli,
        "extract_historical_answers",
        lambda **_: _historical(),
    )

    status = chemcrow_cli.command_paper_evaluator_preflight(
        SimpleNamespace(config=config_path, output=output_path)
    )

    assert status == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "chemcrow_paper_evaluator_preflight_v2"
    assert plan["schema_version"] == "chemcrow_paper_evaluator_plan_v2"
    assert report["source_completed_run_audit_sha256"] == file_sha256(audit_path)
    assert plan["source_completed_run_audit_sha256"] == file_sha256(audit_path)
    assert report["source_aggregate_sha256"] == file_sha256(run_root / "aggregate.json")
    assert plan["source_aggregate_sha256"] == file_sha256(run_root / "aggregate.json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_candidate_sha256", "0" * 64),
        ("provider_only", ["openai", "other"]),
        ("allow_fallbacks", True),
        ("environment_proxy_required", False),
        ("automatic_provider_retries", True),
        ("failed_call_id_reuse", True),
        ("call_count", 41),
    ],
)
def test_production_plan_validator_rejects_frozen_authority_drift(field, value):
    plan = _plan()
    plan[field] = value

    with pytest.raises(ValueError, match="authority|42 calls"):
        validate_paper_evaluation_plan(plan)


def test_production_plan_validator_rejects_duplicate_call_id():
    plan = _plan()
    plan["calls"][1]["call_id"] = plan["calls"][0]["call_id"]

    with pytest.raises(ValueError, match="inventory|unique"):
        validate_paper_evaluation_plan(plan)


def test_production_plan_validator_rejects_prompt_source_drift_and_extra_fields():
    plan = _plan()
    plan["calls"][1]["prompt"] = plan["calls"][1]["prompt"].replace(
        "sealed G1 chemcrow-01", "different answer"
    )
    plan["calls"][1]["prompt_sha256"] = canonical_sha256(plan["calls"][1]["prompt"])

    with pytest.raises(ValueError, match="prompt/source hash"):
        validate_paper_evaluation_plan(plan)

    plan = _plan()
    plan["unfrozen_field"] = True
    with pytest.raises(ValueError, match="schema fields"):
        validate_paper_evaluation_plan(plan)


def test_plan_builder_rejects_historical_control_hash_drift():
    tasks = _tasks()
    historical = _historical()
    historical["chemcrow-01"] = replace(
        historical["chemcrow-01"], chemcrow_answer="drifted answer"
    )

    with pytest.raises(ValueError, match="historical control hash differs"):
        build_paper_evaluation_plan(
            tasks=tasks,
            pairs=_pairs(tasks),
            historical=historical,
            call_id_prefix="paper-fresh-replication",
            source_completed_run_audit_sha256="a" * 64,
            source_aggregate_sha256="b" * 64,
            source_experiment_id="fresh-full-v5",
        )


def test_production_claim_receipt_validator_pins_route_proxy_and_cost(tmp_path):
    call = PaperEvaluationCall.model_validate(_plan()["calls"][0])
    claim_path = tmp_path / f"{call.call_id}.claim.json"
    receipt_path = tmp_path / f"{call.call_id}.receipt.json"
    request_sha256 = "a" * 64
    claim = {
        "schema_version": "chemcrow_paper_openrouter_claim_v1",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "claimed_at": "2026-08-30T00:00:00+00:00",
        "prompt_or_response_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
    }
    receipt = {
        "schema_version": "chemcrow_paper_openrouter_receipt_v1",
        "status": "terminal_success",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "response_sha256": "b" * 64,
        "response_id": "generation-unique",
        "model": "openai/gpt-4",
        "provider": "OpenAI",
        "upstream_http_status": 200,
        "temperature": 0.1,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "list_price_cost_usd": 0.006,
        "openrouter_reported_cost_usd": 0.006,
        "completed_at": "2026-08-30T00:00:01+00:00",
        "prompt_or_response_included": False,
        "credential_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
        "allow_fallbacks": False,
        "provider_only": ["openai"],
        "require_parameters": True,
        "data_collection": "allow",
        "internal_response_format_validated": True,
        "upstream_response_format_omitted": True,
        "internal_call_identity_validated": True,
        "upstream_user_omitted": True,
        "upstream_request_descriptor": {
            "model": "openai/gpt-4",
            "temperature": 0.1,
            "max_tokens": 1200,
            "stream": False,
            "provider": {
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "allow",
            },
            "user_present": False,
            "response_format_present": False,
            "message_count": 2,
            "message_roles": ["system", "user"],
        },
        "environment_proxy": {
            "mode": "environment_proxy",
            "credentials_present": False,
        },
    }
    claim_path.write_text(json.dumps(claim), encoding="utf-8")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    _validate_claim_and_receipt(
        call=call,
        claim_path=claim_path,
        receipt_path=receipt_path,
    )
    receipt["allow_fallbacks"] = True
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="authority differs"):
        _validate_claim_and_receipt(
            call=call,
            claim_path=claim_path,
            receipt_path=receipt_path,
        )


def test_result_envelope_is_pydantic_bound_to_assessment_hash():
    assessment = {
        "student_a": {
            "grade": 8,
            "strengths": [],
            "weaknesses": [],
            "justification": "valid",
            "feedback": [],
        },
        "student_b": {
            "grade": 7,
            "strengths": [],
            "weaknesses": [],
            "justification": "valid",
            "feedback": [],
        },
    }
    normalized_assessment = DualStudentAssessment.model_validate(assessment).model_dump(
        mode="json"
    )
    payload = {
        "schema_version": "chemcrow_paper_evaluator_result_v1",
        "call_id": "paper-fresh-replication-chemcrow-01-historical_control",
        "task_id": "chemcrow-01",
        "comparison": "historical_control",
        "student_a_system": "historical_chemcrow",
        "student_b_system": "historical_gpt4",
        "prompt_sha256": "a" * 64,
        "assessment": normalized_assessment,
        "assessment_sha256": canonical_sha256(normalized_assessment),
        "openrouter_receipt_sha256": "b" * 64,
        "core_terminal_payload_sha256": "c" * 64,
        "execution_route": "dedicated_openevo_rollout_gateway_openrouter_shim_v1",
        "reflector_access": False,
        "evolution_feedback_access": False,
        "provisional_llm_judged": True,
    }
    PaperEvaluationResult.model_validate(payload)
    drifted = copy.deepcopy(payload)
    drifted["assessment"]["student_a"]["grade"] = 1

    with pytest.raises(ValueError, match="assessment hash"):
        PaperEvaluationResult.model_validate(drifted)


def test_production_plan_result_receipt_roots_share_one_dedicated_ledger(tmp_path):
    ledger = tmp_path / "paper-fresh-replication"
    plan_path = ledger / "private" / "plan.json"
    _validate_production_ledger_layout(
        plan_path=plan_path,
        result_root=ledger / "results",
        shim_receipt_root=ledger / "openrouter-receipts",
    )

    with pytest.raises(ValueError, match="ledger layout"):
        _validate_production_ledger_layout(
            plan_path=plan_path,
            result_root=tmp_path / "other" / "results",
            shim_receipt_root=ledger / "openrouter-receipts",
        )


def _paper_test_assessment() -> dict:
    return {
        "student_a": {
            "grade": 8.0,
            "strengths": ["bound"],
            "weaknesses": [],
            "justification": "valid sealed assessment",
            "feedback": [],
        },
        "student_b": {
            "grade": 7.0,
            "strengths": ["bound"],
            "weaknesses": [],
            "justification": "valid sealed assessment",
            "feedback": [],
        },
    }


def _write_paper_success(
    *,
    call: PaperEvaluationCall,
    result_root: Path,
    receipt_root: Path,
    response_id: str,
    replacement_metadata: dict | None = None,
) -> None:
    replacement_metadata = dict(replacement_metadata or {})
    claimed_at = datetime.now(UTC).isoformat()
    request_sha256 = canonical_sha256({"call_id": call.call_id, "prompt": call.prompt})
    claim = {
        "schema_version": "chemcrow_paper_openrouter_claim_v1",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "claimed_at": claimed_at,
        "prompt_or_response_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
        **replacement_metadata,
    }
    receipt = {
        "schema_version": "chemcrow_paper_openrouter_receipt_v1",
        "status": "terminal_success",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "response_sha256": canonical_sha256({"id": response_id}),
        "response_id": response_id,
        "model": "openai/gpt-4",
        "provider": "OpenAI",
        "upstream_http_status": 200,
        "temperature": 0.1,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "list_price_cost_usd": 0.006,
        "openrouter_reported_cost_usd": 0.006,
        "completed_at": datetime.now(UTC).isoformat(),
        "prompt_or_response_included": False,
        "credential_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
        "allow_fallbacks": False,
        "provider_only": ["openai"],
        "require_parameters": True,
        "data_collection": "allow",
        "internal_response_format_validated": True,
        "upstream_response_format_omitted": True,
        "internal_call_identity_validated": True,
        "upstream_user_omitted": True,
        "upstream_request_descriptor": {
            "model": "openai/gpt-4",
            "temperature": 0.1,
            "max_tokens": 1200,
            "stream": False,
            "provider": {
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "allow",
            },
            "user_present": False,
            "response_format_present": False,
            "message_count": 2,
            "message_roles": ["system", "user"],
        },
        "environment_proxy": {
            "mode": "environment_proxy",
            "credentials_present": False,
        },
        **replacement_metadata,
    }
    assessment = _paper_test_assessment()
    result = {
        "schema_version": "chemcrow_paper_evaluator_result_v1",
        "call_id": call.call_id,
        "task_id": call.task_id,
        "comparison": call.comparison,
        "student_a_system": call.student_a_system,
        "student_b_system": call.student_b_system,
        "prompt_sha256": call.prompt_sha256,
        "assessment": assessment,
        "assessment_sha256": canonical_sha256(assessment),
        "openrouter_receipt_sha256": "pending",
        "core_terminal_payload_sha256": "c" * 64,
        "execution_route": "dedicated_openevo_rollout_gateway_openrouter_shim_v1",
        "reflector_access": False,
        "evolution_feedback_access": False,
        "provisional_llm_judged": True,
    }
    claim_path = receipt_root / f"{call.call_id}.claim.json"
    receipt_path = receipt_root / f"{call.call_id}.receipt.json"
    claim_path.write_text(json.dumps(claim, sort_keys=True), encoding="utf-8")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    result["openrouter_receipt_sha256"] = file_sha256(receipt_path)
    (result_root / f"{call.call_id}.result.json").write_text(
        json.dumps(result, sort_keys=True),
        encoding="utf-8",
    )


def _write_zero_cost_provider_failure(
    *,
    call: PaperEvaluationCall,
    result_root: Path,
    receipt_root: Path,
    core_root: Path,
) -> None:
    request = {
        "model": "openai/gpt-4",
        "temperature": 0.1,
        "max_tokens": 1200,
        "stream": False,
        "user": call.call_id,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
            },
            {"role": "user", "content": call.prompt},
        ],
    }
    claimed_request = {
        **request,
        "logprobs": True,
        "top_logprobs": 0,
        "return_token_ids": True,
    }
    request_sha256 = canonical_sha256(claimed_request)
    response_id = "provider-failure-response"
    response = {
        "id": response_id,
        "model": "openai/gpt-4",
        "provider": "OpenAI",
        "choices": [
            {
                "index": 0,
                "finish_reason": "error",
                "message": {"role": "assistant", "content": '{"student_a":'},
                "error": {
                    "code": 502,
                    "message": "provider unavailable",
                    "metadata": {"error_type": "provider_unavailable"},
                },
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0,
        },
    }
    claim = {
        "schema_version": "chemcrow_paper_openrouter_claim_v1",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "claimed_at": "2026-08-30T00:00:00+00:00",
        "prompt_or_response_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
    }
    receipt = {
        "schema_version": "chemcrow_paper_openrouter_receipt_v1",
        "status": "terminal_success",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "response_sha256": canonical_sha256(response),
        "response_id": response_id,
        "model": "openai/gpt-4",
        "provider": "OpenAI",
        "upstream_http_status": 200,
        "temperature": 0.1,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "list_price_cost_usd": 0.0,
        "openrouter_reported_cost_usd": 0.0,
        "completed_at": "2026-08-30T00:00:01+00:00",
        "prompt_or_response_included": False,
        "credential_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
        "allow_fallbacks": False,
        "provider_only": ["openai"],
        "require_parameters": True,
        "data_collection": "allow",
        "internal_response_format_validated": True,
        "upstream_response_format_omitted": True,
        "internal_call_identity_validated": True,
        "upstream_user_omitted": True,
        "upstream_request_descriptor": {
            "model": "openai/gpt-4",
            "temperature": 0.1,
            "max_tokens": 1200,
            "stream": False,
            "provider": {
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "allow",
            },
            "user_present": False,
            "response_format_present": False,
            "message_count": 2,
            "message_roles": ["system", "user"],
        },
        "environment_proxy": {
            "mode": "environment_proxy",
            "credentials_present": False,
        },
    }
    (receipt_root / f"{call.call_id}.claim.json").write_text(
        json.dumps(claim, sort_keys=True), encoding="utf-8"
    )
    (receipt_root / f"{call.call_id}.receipt.json").write_text(
        json.dumps(receipt, sort_keys=True), encoding="utf-8"
    )
    session_id = "sk-openevo-provider-failure"
    task_root = core_root / f"task_{call.call_id}"
    completion_dir = task_root / "sessions" / session_id / "completions"
    completion_dir.mkdir(parents=True)
    session = {
        "task_id": call.call_id,
        "session_id": session_id,
        "status": "ERROR",
        "error": "harness transcript unavailable",
        "trajectory": {"traces": []},
        "workspace_result": None,
    }
    completion = {
        "task_id": call.call_id,
        "session_id": session_id,
        "api_type": "openai_chat",
        "model_requested": "openai/gpt-4",
        "model_used": "openai/gpt-4",
        "original_request": request,
        "transformed_request": request,
        "response": response,
    }
    (task_root / f"ses_{session_id}.json").write_text(
        json.dumps(session, sort_keys=True), encoding="utf-8"
    )
    (completion_dir / "0001-provider-failure.json").write_text(
        json.dumps(completion, sort_keys=True), encoding="utf-8"
    )
    assert not (result_root / f"{call.call_id}.result.json").exists()


def _write_pre_response_402_failure(
    *,
    call: PaperEvaluationCall,
    result_root: Path,
    receipt_root: Path,
    core_root: Path,
) -> None:
    request = {
        "model": "openai/gpt-4",
        "temperature": 0.1,
        "max_tokens": 1200,
        "stream": False,
        "user": call.call_id,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
            },
            {"role": "user", "content": call.prompt},
        ],
    }
    claimed_request = {
        **request,
        "logprobs": True,
        "top_logprobs": 0,
        "return_token_ids": True,
    }
    request_sha256 = canonical_sha256(claimed_request)
    claim = {
        "schema_version": "chemcrow_paper_openrouter_claim_v1",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "claimed_at": "2026-08-30T00:00:00+00:00",
        "prompt_or_response_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
    }
    receipt = {
        "schema_version": "chemcrow_paper_openrouter_receipt_v1",
        "status": "terminal_failure_or_ambiguous",
        "call_id": call.call_id,
        "request_sha256": request_sha256,
        "completed_at": "2026-08-30T00:00:01+00:00",
        "failure": "upstream_http_402",
        "upstream_http_status": 402,
        "upstream_error_code": 402,
        "upstream_error_category": "budget_or_credit_guardrail",
        "upstream_error_message_sha256": "1" * 64,
        "upstream_error_metadata_sha256": "2" * 64,
        "upstream_response_sha256": "3" * 64,
        "openrouter_metadata_sha256": "4" * 64,
        "openrouter_summary_sha256": "5" * 64,
        "upstream_response_body_included": False,
        "upstream_error_metadata_body_included": False,
        "openrouter_metadata_body_included": False,
        "prompt_or_response_included": False,
        "credential_included": False,
        "ledger_class": "production",
        "production_ledger_included": True,
        "internal_call_identity_validated": True,
        "internal_response_format_validated": True,
        "upstream_response_format_omitted": True,
        "upstream_user_omitted": True,
        "openrouter_requested": "openai/gpt-4",
        "openrouter_strategy": "direct",
        "openrouter_is_byok": False,
        "openrouter_endpoints": [
            {"model": "openai/gpt-4", "provider": "OpenAI", "selected": False}
        ],
        "upstream_request_descriptor": {
            "model": "openai/gpt-4",
            "temperature": 0.1,
            "max_tokens": 1200,
            "stream": False,
            "provider": {
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "allow",
            },
            "user_present": False,
            "response_format_present": False,
            "message_count": 2,
            "message_roles": ["system", "user"],
        },
        "environment_proxy": {
            "mode": "environment_proxy",
            "credentials_present": False,
        },
    }
    (receipt_root / f"{call.call_id}.claim.json").write_text(
        json.dumps(claim, sort_keys=True), encoding="utf-8"
    )
    (receipt_root / f"{call.call_id}.receipt.json").write_text(
        json.dumps(receipt, sort_keys=True), encoding="utf-8"
    )
    task_root = core_root / f"task_{call.call_id}"
    task_root.mkdir(parents=True)
    session = {
        "task_id": call.call_id,
        "session_id": "sk-openevo-http-402",
        "status": "ERROR",
        "error": "step 0 exited with code 1",
        "trajectory": {"traces": [{}]},
        "workspace_result": None,
    }
    (task_root / "ses_sk-openevo-http-402.json").write_text(
        json.dumps(session, sort_keys=True), encoding="utf-8"
    )
    assert not (result_root / f"{call.call_id}.result.json").exists()


def _paper_recovery_fixture(tmp_path: Path):
    source_run_root, completed_run_audit = _write_valid_source_authority(tmp_path)
    plan = _source_bound_plan(source_run_root, completed_run_audit)
    calls = validate_paper_evaluation_plan(plan)
    ledger = tmp_path / "paper-production"
    plan_path = ledger / "private" / "plan.json"
    result_root = ledger / "results"
    receipt_root = ledger / "openrouter-receipts"
    plan_path.parent.mkdir(parents=True)
    result_root.mkdir()
    receipt_root.mkdir()
    plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    failed = calls[22]
    for index, call in enumerate(calls[:22], start=1):
        _write_paper_success(
            call=call,
            result_root=result_root,
            receipt_root=receipt_root,
            response_id=f"success-{index}",
        )
    core_root = tmp_path / "core-completions"
    _write_zero_cost_provider_failure(
        call=failed,
        result_root=result_root,
        receipt_root=receipt_root,
        core_root=core_root,
    )
    return {
        "source_run_root": source_run_root,
        "completed_run_audit": completed_run_audit,
        "plan": plan,
        "calls": calls,
        "plan_path": plan_path,
        "result_root": result_root,
        "receipt_root": receipt_root,
        "core_root": core_root,
        "failed": failed,
    }


def test_paper_failed_attempt_authority_is_append_only_and_source_bound(tmp_path):
    fixture = _paper_recovery_fixture(tmp_path)
    protected = {
        path: file_sha256(path)
        for root in (fixture["result_root"], fixture["receipt_root"])
        for path in root.glob("*.json")
    }
    authority_path, authority = authorize_paper_failed_attempt_replacement(
        plan_path=fixture["plan_path"],
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        source_run_root=fixture["source_run_root"],
        completed_run_audit=fixture["completed_run_audit"],
        core_completion_root=fixture["core_root"],
        original_call_id=fixture["failed"].call_id,
    )

    assert authority.original_plan_ordinal == 23
    assert authority.replacement_attempt_ordinal == 2
    assert authority.replacement_call_id == f"{fixture['failed'].call_id}-replacement-01"
    assert authority.original_openrouter_reported_cost_usd == 0
    assert authority.no_valid_result_predicates.valid_score_not_observed is True
    assert all(file_sha256(path) == digest for path, digest in protected.items())
    assert file_sha256(fixture["plan_path"]) == authority.plan_sha256
    assert (
        load_paper_replacement_authority(
            authority_path=authority_path,
            plan_path=fixture["plan_path"],
            result_root=fixture["result_root"],
            receipt_root=fixture["receipt_root"],
            source_run_root=fixture["source_run_root"],
            completed_run_audit=fixture["completed_run_audit"],
            core_completion_root=fixture["core_root"],
        )
        == authority
    )


def test_paper_invalid_structured_assessment_authorizes_fresh_call_id(tmp_path):
    fixture = _paper_recovery_fixture(tmp_path)
    failed = fixture["failed"]
    task_root = fixture["core_root"] / f"task_{failed.call_id}"
    session_path = next(task_root.glob("ses_*.json"))
    completion_path = next(task_root.rglob("completions/*.json"))
    receipt_path = fixture["receipt_root"] / f"{failed.call_id}.receipt.json"

    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    response = completion["response"]
    response["choices"] = [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "student_a": _paper_test_assessment()["student_a"],
                        "student_b": {
                            "grade": 7.0,
                            "strengthness": ["misspelled schema field"],
                            "weaknesses": [],
                            "justification": "invalid sealed assessment",
                            "feedback": [],
                        },
                    }
                ),
            },
        }
    ]
    response["usage"] = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost": 0.006,
    }
    completion_path.write_text(json.dumps(completion, sort_keys=True), encoding="utf-8")

    session = json.loads(session_path.read_text(encoding="utf-8"))
    session.update(
        {
            "status": "COMPLETED",
            "error": None,
            "trajectory": {"traces": [{}]},
        }
    )
    session_path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.update(
        {
            "response_sha256": canonical_sha256(response),
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "list_price_cost_usd": 0.006,
            "openrouter_reported_cost_usd": 0.006,
        }
    )
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    authority_path, authority = authorize_paper_failed_attempt_replacement(
        plan_path=fixture["plan_path"],
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        source_run_root=fixture["source_run_root"],
        completed_run_audit=fixture["completed_run_audit"],
        core_completion_root=fixture["core_root"],
        original_call_id=failed.call_id,
    )

    assert authority.schema_version == "chemcrow_paper_invalid_assessment_replacement_v2"
    assert authority.failure_class == "invalid_structured_assessment"
    assert authority.original_openrouter_reported_cost_usd == 0.006
    assert authority.no_valid_result_predicates.valid_score_not_observed is True
    assert authority.replacement_call_id == f"{failed.call_id}-replacement-01"
    assert load_paper_replacement_authority(
        authority_path=authority_path,
        plan_path=fixture["plan_path"],
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        source_run_root=fixture["source_run_root"],
        completed_run_audit=fixture["completed_run_audit"],
        core_completion_root=fixture["core_root"],
    ) == authority


def test_paper_second_replacement_authority_accepts_pre_response_http_402(tmp_path):
    fixture = _paper_recovery_fixture(tmp_path)
    first_authority_path, first_authority = authorize_paper_failed_attempt_replacement(
        plan_path=fixture["plan_path"],
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        source_run_root=fixture["source_run_root"],
        completed_run_audit=fixture["completed_run_audit"],
        core_completion_root=fixture["core_root"],
        original_call_id=fixture["failed"].call_id,
    )
    replacement = first_authority.replacement_call(fixture["failed"])
    _write_paper_success(
        call=replacement,
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        response_id="first-replacement-success",
        replacement_metadata=replacement_claim_metadata(
            first_authority,
            authority_path=first_authority_path,
        ),
    )
    second_failed = fixture["calls"][23]
    _write_pre_response_402_failure(
        call=second_failed,
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        core_root=fixture["core_root"],
    )

    second_authority_path, second_authority = authorize_paper_failed_attempt_replacement(
        plan_path=fixture["plan_path"],
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        source_run_root=fixture["source_run_root"],
        completed_run_audit=fixture["completed_run_audit"],
        core_completion_root=fixture["core_root"],
        original_call_id=second_failed.call_id,
        existing_authority_paths=[first_authority_path],
    )

    assert second_authority.schema_version == "chemcrow_paper_pre_response_replacement_v3"
    assert second_authority.failure_class == "upstream_http_402_no_response"
    assert second_authority.prior_replacement_count == 1
    assert second_authority.actual_upstream_call_count_after_success == 44
    assert second_authority.original_response_id is None
    assert second_authority.core_completion_relative_path is None
    assert second_authority.no_valid_result_predicates.upstream_http_402 is True
    assert load_paper_replacement_authority(
        authority_path=second_authority_path,
        plan_path=fixture["plan_path"],
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        source_run_root=fixture["source_run_root"],
        completed_run_audit=fixture["completed_run_audit"],
        core_completion_root=fixture["core_root"],
    ) == second_authority


def test_paper_replacement_audit_reports_42_results_and_43_attempts(tmp_path):
    fixture = _paper_recovery_fixture(tmp_path)
    authority_path, authority = authorize_paper_failed_attempt_replacement(
        plan_path=fixture["plan_path"],
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        source_run_root=fixture["source_run_root"],
        completed_run_audit=fixture["completed_run_audit"],
        core_completion_root=fixture["core_root"],
        original_call_id=fixture["failed"].call_id,
    )
    replacement = authority.replacement_call(fixture["failed"])
    _write_paper_success(
        call=replacement,
        result_root=fixture["result_root"],
        receipt_root=fixture["receipt_root"],
        response_id="success-replacement",
        replacement_metadata=replacement_claim_metadata(
            authority,
            authority_path=authority_path,
        ),
    )
    for index, call in enumerate(fixture["calls"][23:], start=24):
        _write_paper_success(
            call=call,
            result_root=fixture["result_root"],
            receipt_root=fixture["receipt_root"],
            response_id=f"success-{index}",
        )

    audit = _audit_production_ledger(
        calls=fixture["calls"],
        result_root=fixture["result_root"],
        shim_receipt_root=fixture["receipt_root"],
        replacement_authority=authority,
        replacement_authority_path=authority_path,
    )

    assert audit["valid_target_result_count"] == 42
    assert audit["actual_upstream_call_count"] == 43
    assert audit["claim_count"] == audit["receipt_count"] == 43
    assert audit["unique_provider_response_id_count"] == 43
    assert audit["excluded_infrastructure_attempt_count"] == 1
    assert audit["explicit_replacement_count"] == 1
    assert audit["automatic_provider_retries"] is False
    assert audit["failed_call_id_reuse"] is False
    assert audit["score_driven_retry"] is False


def test_paper_recovery_rejects_provider_failure_evidence_drift(tmp_path):
    fixture = _paper_recovery_fixture(tmp_path)
    completion_path = next(fixture["core_root"].rglob("completions/*.json"))
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    completion["response"]["choices"][0]["error"]["metadata"]["error_type"] = "model_output_error"
    completion_path.write_text(json.dumps(completion, sort_keys=True), encoding="utf-8")

    with pytest.raises(
        ValueError, match="response hash differs|allowed zero-cost provider failure"
    ):
        authorize_paper_failed_attempt_replacement(
            plan_path=fixture["plan_path"],
            result_root=fixture["result_root"],
            receipt_root=fixture["receipt_root"],
            source_run_root=fixture["source_run_root"],
            completed_run_audit=fixture["completed_run_audit"],
            core_completion_root=fixture["core_root"],
            original_call_id=fixture["failed"].call_id,
        )
