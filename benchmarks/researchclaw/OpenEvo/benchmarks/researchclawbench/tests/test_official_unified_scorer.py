from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openevo_researchclawbench.durable_evaluator_operation import (
    OPENROUTER_API_BASE,
    AmbiguousJudgeInvocation,
    DurableEvaluatorOperation,
    JudgeExecutionOutcome,
    JudgeInvocation,
    JudgePolicyIdentity,
    JudgeRuntimeIdentity,
)
from openevo_researchclawbench.formal_v11 import (
    OFFICIAL_FROZEN_POLICY,
    OFFICIAL_V11_BUDGET,
)
from openevo_researchclawbench.official_training_operations import (
    OfficialUnifiedScorerPort,
)
from openevo_researchclawbench.official_unified_scorer import (
    DurableOfficialUnifiedScorerAuthority,
    OfficialUnifiedScorerError,
    official_withheld_partitioner,
)
from openevo_researchclawbench.training_state_store import canonical_sha256


def _policy() -> JudgePolicyIdentity:
    return JudgePolicyIdentity(
        provider="openai_compatible",
        api_base=OPENROUTER_API_BASE,
        model="openai/gpt-5.1",
        model_source="OpenRouter_model_catalog",
        runs_per_attempt=1,
        scorer_commit="a" * 40,
        scorer_sha256="b" * 64,
    )


class _Judge:
    def __init__(self, policy: JudgePolicyIdentity, *, fail: bool = False) -> None:
        self.policy = policy
        self.fail = fail
        self.calls = 0

    def __call__(
        self, request: dict[str, Any], invocation: JudgeInvocation
    ) -> JudgeExecutionOutcome:
        self.calls += 1
        assert invocation.model == "openai/gpt-5.1"
        if self.fail:
            raise RuntimeError("synthetic response loss")
        return JudgeExecutionOutcome(
            raw_score={
                "total_score": float(int(request["task_id"].split("_")[1]) % 100),
                "private_item": "evaluator-only",
            },
            runtime_identity=JudgeRuntimeIdentity(
                provider=self.policy.provider,
                api_base=self.policy.api_base,
                model=self.policy.model,
                api_key_present=True,
            ),
            request_count=1,
            usage={"input_tokens": 1, "output_tokens": 1},
            cost_total_usd=0.01,
        )


def _task_ids() -> tuple[str, ...]:
    return tuple(f"Official_{index:03d}" for index in range(40))


def _request(root: Path, task_ids: tuple[str, ...]) -> dict[str, Any]:
    manifests = []
    for index, task_id in enumerate(task_ids):
        run_id = f"{task_id}_a0"
        output = root / "runs" / run_id
        output.mkdir(parents=True)
        manifests.append(
            {
                "task_id": task_id,
                "task_index": index,
                "attempt_index": 0,
                "run_id": run_id,
                "session_id": f"session-{index}",
                "dataset_id": f"dataset-{index}",
                "dataset_revision": f"artifact-{index}.v1",
                "sealed": True,
                "completed": True,
                "artifact_valid": True,
                "validator_receipt_sha256": "c" * 64,
                "artifact_root_sha256": "d" * 64,
                "candidate_output_root": str(output),
                "runtime_seconds": 1.0,
                **OFFICIAL_FROZEN_POLICY,
            }
        )
    return {
        "run_manifests": manifests,
        "official_run_set_sha256": canonical_sha256(manifests),
        "official_policy": dict(OFFICIAL_FROZEN_POLICY),
        "runs_per_attempt": 1,
        "pass_at_k": 1,
        "scoring_mode": "unified_after_all_40_sealed",
    }


def _authority(
    tmp_path: Path,
    judge: _Judge,
    task_ids: tuple[str, ...],
) -> DurableOfficialUnifiedScorerAuthority:
    operation = DurableEvaluatorOperation(
        tmp_path / "per-task",
        executor=judge,
        partitioner=official_withheld_partitioner,
    )
    return DurableOfficialUnifiedScorerAuthority(
        root=tmp_path / "aggregate",
        official_run_root=tmp_path / "official",
        official_task_ids=task_ids,
        policy=judge.policy,
        evaluator_operation=operation,
        official_budget=OFFICIAL_V11_BUDGET,
    )


def test_exact_40_unified_score_is_private_and_exactly_once(tmp_path: Path) -> None:
    task_ids = _task_ids()
    policy = _policy()
    judge = _Judge(policy)
    authority = _authority(tmp_path, judge, task_ids)
    request = _request(tmp_path / "official", task_ids)
    first = authority.execute_unified_score(request, "official-run:score-all")
    second = authority.execute_unified_score(request, "official-run:score-all")
    assert first == second
    assert judge.calls == 40
    assert first["judge_model"] == "openai/gpt-5.1"
    assert first["official_task_count"] == 40
    assert first["judge_task_operations"] == 40
    assert first["feedback_released"] is False
    assert first["attachments_created"] == 0
    assert first["artifact_updates_performed"] == 0
    assert "task_results" not in first
    private = json.loads(Path(first["private_receipt_path"]).read_text())
    assert len(private["task_results"]) == 40
    assert private["intermediate_feedback_withheld"] is True


def test_task_completion_before_aggregate_response_loss_is_not_repeated(
    tmp_path: Path,
) -> None:
    task_ids = _task_ids()
    policy = _policy()
    judge = _Judge(policy)
    authority = _authority(tmp_path, judge, task_ids)
    request = _request(tmp_path / "official", task_ids)
    manifest = request["run_manifests"][0]
    task_request = {
        "task_id": manifest["task_id"],
        "attempt_id": manifest["run_id"],
        "session_id": manifest["session_id"],
        "completed_dataset_id": manifest["dataset_id"],
        "dataset_revision": manifest["dataset_revision"],
        "validator_receipt_sha256": manifest["validator_receipt_sha256"],
        "artifact_root_sha256": manifest["artifact_root_sha256"],
        "candidate_runtime_seconds": manifest["runtime_seconds"],
        "candidate_output_root": manifest["candidate_output_root"],
        "generic_failure_tags": [],
    }
    authority.evaluator_operation.execute(
        request=task_request,
        idempotency_key="official-loss:Official_000",
        policy=policy,
    )
    result = authority.execute_unified_score(request, "official-loss")
    assert result["judge_task_operations"] == 40
    assert judge.calls == 40
    assert authority.execute_unified_score(request, "official-loss") == result
    assert judge.calls == 40


def test_invalid_artifact_is_zero_without_judge_and_never_exposes_feedback(
    tmp_path: Path,
) -> None:
    task_ids = _task_ids()
    judge = _Judge(_policy())
    authority = _authority(tmp_path, judge, task_ids)
    request = _request(tmp_path / "official", task_ids)
    request["run_manifests"][0]["artifact_valid"] = False
    request["official_run_set_sha256"] = canonical_sha256(request["run_manifests"])
    result = authority.execute_unified_score(request, "official-invalid")
    assert result["judge_task_operations"] == 39
    assert result["scored_task_operations"] == 40
    assert judge.calls == 39
    private = json.loads(Path(result["private_receipt_path"]).read_text())
    assert private["task_results"][0]["score"] == 0.0
    assert private["task_results"][0]["judge_invoked"] is False


@pytest.mark.parametrize("mutation", ("missing", "reordered", "duplicate", "unsealed"))
def test_non_exact_official_run_set_fails_before_judge(
    tmp_path: Path, mutation: str
) -> None:
    task_ids = _task_ids()
    judge = _Judge(_policy())
    authority = _authority(tmp_path, judge, task_ids)
    request = _request(tmp_path / "official", task_ids)
    manifests = request["run_manifests"]
    if mutation == "missing":
        manifests.pop()
    elif mutation == "reordered":
        manifests[0], manifests[1] = manifests[1], manifests[0]
    elif mutation == "duplicate":
        manifests[1]["task_id"] = manifests[0]["task_id"]
    else:
        manifests[0]["sealed"] = False
    request["official_run_set_sha256"] = canonical_sha256(manifests)
    with pytest.raises(OfficialUnifiedScorerError):
        authority.execute_unified_score(request, f"official-bad:{mutation}")
    assert judge.calls == 0


def test_ambiguous_judge_response_is_never_replayed(tmp_path: Path) -> None:
    task_ids = _task_ids()
    judge = _Judge(_policy(), fail=True)
    authority = _authority(tmp_path, judge, task_ids)
    request = _request(tmp_path / "official", task_ids)
    with pytest.raises(RuntimeError, match="response loss"):
        authority.execute_unified_score(request, "official-ambiguous")
    with pytest.raises(AmbiguousJudgeInvocation):
        authority.execute_unified_score(request, "official-ambiguous")
    assert judge.calls == 1


def test_official_candidate_runtime_budget_is_independent_and_fail_closed(
    tmp_path: Path,
) -> None:
    task_ids = _task_ids()
    judge = _Judge(_policy())
    authority = _authority(tmp_path, judge, task_ids)
    request = _request(tmp_path / "official", task_ids)
    request["run_manifests"][0]["runtime_seconds"] = 3601
    request["official_run_set_sha256"] = canonical_sha256(request["run_manifests"])
    with pytest.raises(OfficialUnifiedScorerError, match="artifact authority"):
        authority.execute_unified_score(request, "official-budget")
    assert judge.calls == 0


def test_official_port_validates_private_boundary_and_hash(tmp_path: Path) -> None:
    task_ids = _task_ids()
    judge = _Judge(_policy())
    authority = _authority(tmp_path, judge, task_ids)
    request = _request(tmp_path / "official", task_ids)
    port = OfficialUnifiedScorerPort(
        authority=authority,
        evaluator_private_root=authority.private_root,
    )
    result = port.execute(request, "official-port")
    assert result["scoring_complete"] is True
    assert result["raw_judge_outputs_private"] is True
    Path(result["private_receipt_path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(OfficialUnifiedScorerError, match="hash drifted"):
        port.recover(request, "official-port")
