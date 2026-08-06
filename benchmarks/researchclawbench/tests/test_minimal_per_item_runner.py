"""Zero-call tests for the minimal per-item runner."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

import openevo_researchclawbench.community_evaluator as community_evaluator
import openevo_researchclawbench.openrouter_judge as openrouter_judge
from openevo_researchclawbench.community_evaluator import (
    _closed_evaluator_subprocess_environment,
)
from openevo_researchclawbench.artifact_validator import (
    candidate_artifact_root_sha256,
)
from openevo_researchclawbench.deepseek_codex_engineering_port import (
    CodexExecutionResult,
    DeepSeekEngineeringError,
    DeepSeekCodexEngineeringPort,
)
from openevo_researchclawbench.minimal_per_item_runner import (
    DryRunCandidatePort,
    DryRunEvolutionPort,
    DryRunJudgePort,
    ExistingJudgeAdapter,
    MinimalPerItemConfig,
    MinimalPerItemRunner,
    MinimalStage,
    _MINIMAL_PNG,
)
from openevo_researchclawbench.rejudge_sealed import (
    RejudgeSealedError,
    _file_inventory,
    run_rejudge_sealed,
)
from openevo_researchclawbench.training_state_store import TrainingStateStore


WORKSPACE = Path(__file__).resolve().parents[4]
REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_RCB = Path("/home/lhy-h/work/researchclaw_openevo/ResearchClawBench")


def _real_score_module():
    """Import the canonical ResearchClawBench scorer used by the Judge worker."""

    parent = str(REAL_RCB.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from ResearchClawBench.evaluation import score

    return score


def _make_fake_rcb(tmp_path: Path) -> Path:
    root = tmp_path / f"RCB_{tmp_path.name}"
    shutil.copytree(
        REAL_RCB / "evaluation",
        root / "evaluation",
        dirs_exist_ok=True,
    )
    task_dir = root / "tasks" / "Life_005"
    (task_dir / "data").mkdir(parents=True, exist_ok=True)
    (task_dir / "related_work").mkdir(parents=True, exist_ok=True)
    (task_dir / "target_study").mkdir(parents=True, exist_ok=True)
    (task_dir / "task_info.json").write_text(
        json.dumps(
            {
                "task": "RNA triplet repeat sequence analysis.",
                "data": [
                    {
                        "name": "input.csv",
                        "path": "./data/input.csv",
                        "description": "Input table.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "data" / "input.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (task_dir / "related_work" / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    (task_dir / "target_study" / "checklist.json").write_text(
        json.dumps({"checks": [{"name": "method"}, {"name": "results"}]}),
        encoding="utf-8",
    )
    return root


def _make_config(tmp_path: Path, *, task_id: str = "Life_005") -> MinimalPerItemConfig:
    rcb = _make_fake_rcb(tmp_path)
    (tmp_path / "exp").mkdir(parents=True, exist_ok=True)
    output_root = tmp_path / "exp" / "out"
    output_root.mkdir(parents=True, exist_ok=True)
    raw = {
        "protocol_name": "researchclaw_per_item_minimal_deepseek_canary_v1",
        "task_id": task_id,
        "run_purpose": "engineering_deepseek_canary",
        "candidate_backend": "deepseek",
        "evolution_backend": "deepseek",
        "judge_backend": "openai_compatible_gpt_5_1",
        "native_core_route": False,
        "leaderboard_eligible": False,
        "official_protocol": False,
        "paths": {
            "project_root": str(tmp_path),
            "researchclawbench_root": str(rcb),
            "output_root": str(output_root),
        },
        "candidate": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "profile": "~/.codex-deepseek",
            "timeout_seconds": 1800,
            "sandbox": "workspace-write",
        },
        "evolution": {"provider": "deepseek", "model": "deepseek-v4-flash", "generation": 1},
        "judge": {
            "provider": "openai_compatible",
            "model": "openai/gpt-5.1",
            "api_key_env": "RCB_JUDGE_API_KEY",
            "base_url_env": "RCB_JUDGE_BASE_URL",
            "model_env": "RCB_JUDGE_MODEL",
            "timeout_seconds": 1800,
        },
        "execution": {
            "max_tasks": 1,
            "max_provider_jobs": 7,
            "max_retries": 0,
            "stop_on_first_failure": True,
            "leaderboard_eligible": False,
            "dry_run": True,
        },
    }
    return MinimalPerItemConfig(tmp_path / "protocol.yaml", raw)


def _write_candidate_deliverables(workspace: Path) -> None:
    """Deterministic fake-model outputs that satisfy the sealed workspace checks."""

    (workspace / "code").mkdir(parents=True, exist_ok=True)
    (workspace / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    (workspace / "outputs" / "summary.json").write_text(
        json.dumps({"estimated_effect": 0.42, "observations": 128}),
        encoding="utf-8",
    )
    report = workspace / "report"
    (report / "images").mkdir(parents=True, exist_ok=True)
    (report / "images" / "f.png").write_bytes(_MINIMAL_PNG)
    (report / "report.md").write_text(
        "\n".join(
            (
                "# Fake engineering report",
                "",
                "## Methodology",
                "This fake model output implements the deterministic engineering "
                "wiring test. The experimental design applies the published "
                "methodology to the provided input tables and computes the "
                "required summary statistics. The pipeline is implemented in "
                "Python and follows the procedure described in the related "
                "work, including the triplet pattern aggregation and the "
                "strand-count sweeps. Every input file is read from the read-only "
                "data directory, and every intermediate result is written into "
                "the outputs directory so that the full pipeline is reproducible "
                "from the sealed workspace alone.",
                "",
                "## Results",
                "The primary quantitative result is an estimated effect of "
                "0.42 with 128 synthetic observations across the designed "
                "comparison groups. Figure f shows the expected curve shape "
                "and confirms that the analysis pipeline runs end to end. The "
                "summary table reports the group means, the standard deviations, "
                "and the paired differences, all of which are consistent with "
                "the synthetic fixture used for this wiring test. The figure "
                "was generated from the same output tables and is referenced "
                "directly from the results section.",
                "",
                "## Discussion",
                "The findings are consistent with the task expectations and "
                "the interpretation follows the published discussion. The "
                "main limitation is that this content is a deterministic "
                "fixture used only to exercise the runner wiring, so no "
                "biological conclusion should be drawn from it. The important "
                "conclusion for engineering purposes is that the candidate "
                "executor contract, the workspace sealing step, and the judge "
                "input boundary all accept the same sealed artifact format, "
                "which validates the full canary path before any live model "
                "invocation is attempted.",
                "",
                "![figure](images/f.png)",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _make_fake_executors():
    """Return deterministic candidate/evolution executor doubles."""

    state = {"candidate_calls": [], "evolution_calls": []}

    def candidate_executor(argv, *, env, cwd, prompt, timeout_seconds):
        assert isinstance(argv, list)
        assert "RCB_JUDGE_API_KEY" not in env
        assert "RCB_JUDGE_BASE_URL" not in env
        assert "RCB_JUDGE_MODEL" not in env
        state["candidate_calls"].append(
            {"argv": list(argv), "cwd": str(cwd), "env_keys": sorted(env)}
        )
        _write_candidate_deliverables(Path(cwd))
        return CodexExecutionResult(returncode=0, stdout="", stderr="", duration_seconds=0.1)

    def evolution_executor(argv, *, env, cwd, prompt, timeout_seconds):
        assert isinstance(argv, list)
        state["evolution_calls"].append(
            {"argv": list(argv), "cwd": str(cwd), "env_keys": sorted(env)}
        )
        match = re.search(r"artifact to: (\S+)", prompt)
        assert match is not None
        destination = Path(match.group(1))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "# Engineering artifact\nDeterministic fake artifact content.\n",
            encoding="utf-8",
        )
        return CodexExecutionResult(returncode=0, stdout="", stderr="", duration_seconds=0.1)

    return state, candidate_executor, evolution_executor


def _runner(
    config: MinimalPerItemConfig,
    *,
    run_id: str = "minimal-test",
    candidate_port=None,
    evolution_port=None,
    judge_port=None,
) -> MinimalPerItemRunner:
    return MinimalPerItemRunner(
        config=config,
        state_root=config.output_root / "supervisor" / run_id,
        run_id=run_id,
        candidate_port=candidate_port or DryRunCandidatePort(),
        evolution_port=evolution_port or DryRunEvolutionPort(),
        judge_port=judge_port or DryRunJudgePort(),
    )


def _complete(config: MinimalPerItemConfig, run_id: str = "minimal-test"):
    candidate = DryRunCandidatePort()
    evolution = DryRunEvolutionPort()
    judge = DryRunJudgePort()
    runner = _runner(config, run_id=run_id, candidate_port=candidate, evolution_port=evolution, judge_port=judge)
    runner.initialize()
    state = runner.run_until_item_closed()
    return runner, candidate, evolution, judge, state


def test_baseline_injection_is_empty(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner, _, _, _, state = _complete(config)
    assert "context_artifact_ids" not in state["active_baseline_request"]


def test_evolved_injects_exactly_three_artifacts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner, _, _, _, state = _complete(config)
    assert state["active_evolved_request"]["context_artifact_ids"] == [
        "memory",
        "skill",
        "agent_system",
    ]
    context = Path(state["evolved_context"])
    assert (context / "memory.md").is_file()
    assert (context / "skill" / "SKILL.md").is_file()
    assert (context / "agent_system" / "AGENTS.md").is_file()
    evolved_candidate = Path(state["evolved_candidate_workspace"])
    assert (evolved_candidate / "memory.md").is_file()
    assert (evolved_candidate / "skills" / "skill" / "SKILL.md").is_file()
    assert (evolved_candidate / "AGENTS.md").is_file()


def test_snapshot_hashes_equal(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    _, _, _, _, state = _complete(config)
    assert state["snapshot_sha256"] == state["evolved_snapshot_sha256"]


def test_workspaces_are_distinct(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    _, _, _, _, state = _complete(config)
    assert state["baseline_workspace"] != state["evolved_workspace"]
    assert state["baseline_candidate_workspace"] != state["evolved_candidate_workspace"]


def test_second_run_does_not_inherit_artifacts(tmp_path: Path) -> None:
    config_a = _make_config(tmp_path / "a")
    config_b = _make_config(tmp_path / "b")
    _, _, _, _, first = _complete(config_a, run_id="run-a")
    _, _, _, _, second = _complete(config_b, run_id="run-b")
    first_artifacts = Path(first["evolution_receipt"]["artifact_root"]).resolve()
    second_artifacts = Path(second["evolution_receipt"]["artifact_root"]).resolve()
    assert first_artifacts != second_artifacts
    assert "run-a" not in json.dumps(second)


def test_different_run_ids_same_task_do_not_collide(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    _, _, _, _, first = _complete(config, run_id="run-a")
    _, _, _, _, second = _complete(config, run_id="run-b")
    assert first["stage"] == MinimalStage.COMPLETE.value
    assert second["stage"] == MinimalStage.COMPLETE.value
    assert first["baseline_workspace"] != second["baseline_workspace"]
    assert first["evolved_workspace"] != second["evolved_workspace"]
    assert Path(first["baseline_workspace"]).parent.name == "run-a"
    assert Path(second["baseline_workspace"]).parent.name == "run-b"
    first_artifact_root = Path(first["evolution_receipt"]["artifact_root"])
    second_artifact_root = Path(second["evolution_receipt"]["artifact_root"])
    assert first_artifact_root != second_artifact_root
    assert first_artifact_root.parent.name == "evolution_artifacts"
    assert second_artifact_root.parent.name == "evolution_artifacts"
    assert first_artifact_root.name == "run-a"
    assert second_artifact_root.name == "run-b"
    first_result = (
        config.output_root / "items" / "Life_005" / "paired_results" / "run-a.json"
    )
    second_result = (
        config.output_root / "items" / "Life_005" / "paired_results" / "run-b.json"
    )
    assert first_result.is_file()
    assert second_result.is_file()


def test_resume_same_run_finds_original_workspace(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    candidate = DryRunCandidatePort()
    evolution = DryRunEvolutionPort()
    judge = DryRunJudgePort()
    runner_one = _runner(
        config,
        run_id="run-a",
        candidate_port=candidate,
        evolution_port=evolution,
        judge_port=judge,
    )
    runner_one.initialize()
    runner_one.run_next()
    prepared = runner_one.status()
    assert prepared["stage"] == MinimalStage.BASELINE_PREPARED.value
    baseline_workspace = prepared["baseline_workspace"]
    assert Path(baseline_workspace).is_dir()
    # Simulate a process restart: a new runner instance with the same run id
    # resumes from the persisted state and reuses the same namespace.
    runner_two = _runner(
        config,
        run_id="run-a",
        candidate_port=candidate,
        evolution_port=evolution,
        judge_port=judge,
    )
    state = runner_two.run_until_item_closed()
    assert state["stage"] == MinimalStage.COMPLETE.value
    assert state["baseline_workspace"] == baseline_workspace
    assert Path(state["baseline_workspace"]).is_dir()
    assert Path(state["baseline_workspace"]).parent == Path(
        state["evolved_workspace"]
    ).parent


def test_evolution_request_has_no_judge_output(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    evolution = DryRunEvolutionPort()
    runner = _runner(config, evolution_port=evolution)
    runner.initialize()
    runner.run_until_item_closed()
    request = evolution.calls[0]
    for key in ("score", "judge", "rubric", "reasoning", "raw_response"):
        assert key not in request
    assert "baseline_sealed_root" in request
    assert "gt_path" in request


def test_evolution_gets_baseline_and_gt_only(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    evolution = DryRunEvolutionPort()
    runner = _runner(config, evolution_port=evolution)
    runner.initialize()
    runner.run_until_item_closed()
    request = evolution.calls[0]
    assert Path(request["baseline_sealed_root"]).is_dir()
    assert Path(request["gt_path"]).is_file()


def test_paired_result_is_correct(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    _, _, _, _, state = _complete(config)
    paired = state["paired_result"]
    assert paired["baseline_score"] == 62.0
    assert paired["evolved_score"] == 67.0
    assert paired["delta"] == 5.0
    assert paired["status"] == "COMPLETED"


def test_missing_round_has_no_paired_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    class FailingCandidate(DryRunCandidatePort):
        def execute(self, request):
            if request["pass_name"] == "evolved":
                raise DeepSeekEngineeringError("BLOCKED_CANDIDATE_AUTH", "auth")
            return super().execute(request)

    runner = _runner(config, candidate_port=FailingCandidate())
    runner.initialize()
    state = runner.run_until_item_closed()
    assert state["stage"] == MinimalStage.BLOCKED_CANDIDATE_AUTH.value
    assert "paired_result" not in state


def test_seal_validation_failure_is_durable(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    class EmptyCandidate(DryRunCandidatePort):
        def execute(self, request):
            return {
                "task_id": request["task_id"],
                "run_id": request["run_id"],
                "pass_name": request["pass_name"],
                "session_id": f"empty-{request['run_id']}",
                "candidate_output_root": str(request["workspace"]),
                "exit_status": 0,
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "deepseek_key_present": False,
                "secret_recorded": False,
            }

    runner = _runner(config, candidate_port=EmptyCandidate())
    runner.initialize()
    state = runner.run_until_item_closed()
    assert state["stage"] == MinimalStage.FAILED.value
    assert "workspace validation failed" in state["failure_message"]
    effects = {
        item["kind"]: item["status"]
        for item in runner.store.side_effects_for_experiment("minimal-test")
    }
    assert effects.get("candidate-baseline") == "completed"
    assert "judge-baseline" not in effects


def test_sealed_side_effect_not_repeated(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner, _, _, _, _ = _complete(config)
    before = len(runner.store.side_effects_for_experiment("minimal-test"))
    runner.run_until_item_closed()
    after = len(runner.store.side_effects_for_experiment("minimal-test"))
    assert after == before


def test_fake_integration_closes_life_005(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    _, _, _, _, state = _complete(config)
    assert state["stage"] == MinimalStage.COMPLETE.value
    assert state["task_id"] == "Life_005"


def test_dry_run_provider_call_count_is_zero(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner, candidate, evolution, judge, state = _complete(config)
    assert state["stage"] == MinimalStage.COMPLETE.value
    assert candidate.real_calls == 0
    assert evolution.real_calls == 0
    assert judge.real_calls == 0


def test_deepseek_env_has_no_judge_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present")
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "judge-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    port = DeepSeekCodexEngineeringPort(dry_run=True)
    env = port._environment()
    assert "RCB_JUDGE_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"].endswith(".codex-deepseek")


def test_deepseek_candidate_execute_contract(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    workspace = config.output_root / "items" / "Life_005" / "runs" / "contract" / "baseline_candidate"
    workspace.mkdir(parents=True)
    (workspace / "INSTRUCTIONS.md").write_text("# Task\nDo the work.\n", encoding="utf-8")
    state, candidate_executor, _ = _make_fake_executors()
    port = DeepSeekCodexEngineeringPort(executor=candidate_executor, dry_run=True)
    receipt = port.execute(
        {
            "task_id": "Life_005",
            "run_id": "Life_005_a0_baseline",
            "pass_name": "baseline",
            "workspace": str(workspace),
        }
    )
    assert port.real_calls == 1
    assert len(state["candidate_calls"]) == 1
    assert receipt["exit_status"] == 0
    assert receipt["pass_name"] == "baseline"
    assert receipt["run_id"] == "Life_005_a0_baseline"
    assert receipt["provider"] == "deepseek"
    assert receipt["model"] == "deepseek-v4-flash"
    assert receipt["candidate_output_root"] == str(workspace)
    assert receipt["secret_recorded"] is False
    assert receipt["session_id"].startswith("ds-")
    argv = state["candidate_calls"][0]["argv"]
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--profile" not in argv
    assert "--model" in argv and "deepseek-v4-flash" in argv
    assert "--ephemeral" in argv
    assert "--sandbox" in argv and "workspace-write" in argv
    assert "--cd" in argv
    meta = json.loads((workspace / "_meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["exit_code"] == 0
    assert meta["task_id"] == "Life_005"
    assert meta["run_id"] == "Life_005_a0_baseline"


def test_real_ports_full_wiring_fake_integration(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state, candidate_executor, evolution_executor = _make_fake_executors()
    candidate_port = DeepSeekCodexEngineeringPort(executor=candidate_executor, dry_run=True)
    evolution_port = DeepSeekCodexEngineeringPort(executor=evolution_executor, dry_run=True)
    judge_port = DryRunJudgePort()
    runner = _runner(
        config,
        run_id="wiring-run",
        candidate_port=candidate_port,
        evolution_port=evolution_port,
        judge_port=judge_port,
    )
    runner.initialize()
    final_state = runner.run_until_item_closed()
    assert final_state["stage"] == MinimalStage.COMPLETE.value
    assert candidate_port.real_calls == 2
    assert evolution_port.real_calls == 3
    assert judge_port.real_calls == 0
    assert len(state["candidate_calls"]) == 2
    assert len(state["evolution_calls"]) == 3
    baseline_workspace = Path(final_state["baseline_candidate_workspace"])
    evolved_workspace = Path(final_state["evolved_candidate_workspace"])
    assert baseline_workspace != evolved_workspace
    assert not (baseline_workspace / "memory.md").exists()
    assert not (baseline_workspace / "skills" / "skill" / "SKILL.md").exists()
    assert not (baseline_workspace / "AGENTS.md").exists()
    assert (evolved_workspace / "memory.md").is_file()
    assert (evolved_workspace / "skills" / "skill" / "SKILL.md").is_file()
    assert (evolved_workspace / "AGENTS.md").is_file()
    artifact_root = Path(final_state["evolution_receipt"]["artifact_root"])
    assert artifact_root.name == "wiring-run"
    assert (artifact_root / "memory.md").is_file()
    assert (artifact_root / "skill" / "SKILL.md").is_file()
    assert (artifact_root / "agent_system" / "AGENTS.md").is_file()
    paired = final_state["paired_result"]
    assert paired["status"] == "COMPLETED"


def test_judge_adapter_expected_model_matches_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("RCB_JUDGE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("RCB_JUDGE_MODEL", "openai/gpt-5.1")
    captured: dict[str, object] = {}

    class FakeExecution:
        raw_score = {"total_score": 55.0}
        score_valid = True
        judge_completed = True
        failure_category = None

    def fake_detailed(**kwargs):
        captured.update(kwargs)
        return FakeExecution()

    import openevo_researchclawbench.community_evaluator as community_evaluator

    monkeypatch.setattr(
        community_evaluator,
        "run_community_evaluator_detailed",
        fake_detailed,
    )
    private_root = tmp_path / "evaluator_private" / "deepseek_life_005_minimal_canary_v3"
    adapter = ExistingJudgeAdapter(config, evaluator_private_root=private_root)
    result = adapter.evaluate(
        {
            "task_id": "Life_005",
            "run_id": "Life_005_a0_baseline",
            "pass_name": "baseline",
            "candidate_output_root": str(tmp_path / "candidate"),
        }
    )
    assert result["total_score"] == 55.0
    assert captured["expected_model"] == "openai/gpt-5.1"
    assert captured["expected_api_base"] == "https://openrouter.ai/api/v1"
    assert captured["expected_provider"] == "openai_compatible"
    assert captured["expected_requested_provider"] == "azure"
    assert Path(captured["project_root"]) == config.researchclawbench_root.parent
    assert captured["task_id"] == "Life_005"
    assert captured["attempt_id"] == "Life_005_a0_baseline"
    assert private_root.is_dir()
    assert "JUDGE_MODEL_NAME" not in os.environ
    assert "JUDGE_API_KEY" not in os.environ
    assert "JUDGE_API_BASE" not in os.environ


def test_judge_subprocess_env_has_rcb_credentials_and_no_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("CODEX_HOME", "/home/user/.codex-deepseek")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:10809")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:10809")
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("RCB_JUDGE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("RCB_JUDGE_MODEL", "openai/gpt-5.1")
    import openevo

    openevo_source_root = Path(openevo.__file__).resolve(strict=True).parents[1]
    env = _closed_evaluator_subprocess_environment(
        project_root=WORKSPACE,
        judge_env_file=None,
    )
    assert "DEEPSEEK_API_KEY" not in env
    assert "CODEX_HOME" not in env
    assert env["RCB_JUDGE_API_KEY"] == "judge-key"
    assert env["RCB_JUDGE_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["RCB_JUDGE_MODEL"] == "openai/gpt-5.1"
    assert "judge-key" not in env["PYTHONPATH"]
    assert env["https_proxy"] == "http://proxy.invalid:10809"
    assert env["http_proxy"] == "http://proxy.invalid:10809"
    assert str(openevo_source_root) in env["PYTHONPATH"]


def test_non_life_005_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _make_config(tmp_path, task_id="Alpha_004")


def test_old_runner_modules_still_import() -> None:
    import openevo_researchclawbench.training_supervisor  # noqa: F401
    import openevo_researchclawbench.community_evaluator  # noqa: F401


# ---------------------------------------------------------------------------
# Azure-routed Judge client and failure semantics (adapter transport)
# ---------------------------------------------------------------------------


def _fake_http_response(
    content: str,
    *,
    provider: str = "Azure",
    status: int = 200,
) -> tuple[int, dict, str]:
    body = {
        "id": "probe",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-5.1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": None,
        "provider": provider,
    }
    return status, body, json.dumps(body)


def test_azure_provider_routing_is_written_to_judge_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict] = []

    def fake_post(**kwargs):
        posted.append(kwargs["payload"])
        return _fake_http_response('{"reasoning":"ok","score":50}')

    monkeypatch.setattr(openrouter_judge, "http_judge_post", fake_post)
    client = openrouter_judge.make_judge_client(
        api_key="judge-key",
        api_base="https://openrouter.ai/api/v1",
        model="openai/gpt-5.1",
        max_try=1,
    )
    result = client("rubric_0", "Rate this report.")
    assert result["score_valid"] is True
    assert len(posted) == 1
    payload = posted[0]
    assert payload["model"] == "openai/gpt-5.1"
    assert payload["stream"] is False
    assert payload["provider"] == {
        "only": ["azure"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    assert "max_completion_tokens" in payload
    assert "temperature" not in payload
    assert result["returned_model"] == "openai/gpt-5.1"
    assert result["returned_provider"] == "Azure"
    assert result["request_id"] == "probe"
    assert result["parse_status"] == "ok"
    assert result["latency_ms"] >= 0
    assert result["retry_count"] == 1


def test_model_and_provider_are_pinned_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict] = []

    def fake_post(**kwargs):
        posted.append(kwargs["payload"])
        if len(posted) == 1:
            return (
                503,
                {"error": {"message": "Azure unavailable", "code": 503}},
                json.dumps({"error": {"message": "Azure unavailable", "code": 503}}),
            )
        return _fake_http_response('{"reasoning":"retry","score":60}')

    monkeypatch.setattr(openrouter_judge, "http_judge_post", fake_post)
    client = openrouter_judge.make_judge_client(
        api_key="judge-key",
        api_base="https://openrouter.ai/api/v1",
        model="openai/gpt-5.1",
        max_try=2,
    )
    result = client("rubric_0", "Rate this report.")
    assert result["score_valid"] is True
    assert len(posted) == 2
    assert [payload["model"] for payload in posted] == [
        "openai/gpt-5.1",
        "openai/gpt-5.1",
    ]
    assert [payload["provider"] for payload in posted] == [
        {"only": ["azure"], "allow_fallbacks": False, "require_parameters": True},
        {"only": ["azure"], "allow_fallbacks": False, "require_parameters": True},
    ]
    assert result["retry_count"] == 2


def test_403_region_block_never_becomes_valid_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**kwargs):
        return (
            403,
            {
                "error": {
                    "message": "This model is not available in your region.",
                    "code": 403,
                }
            },
            json.dumps(
                {
                    "error": {
                        "message": "This model is not available in your region.",
                        "code": 403,
                    }
                }
            ),
        )

    monkeypatch.setattr(openrouter_judge, "http_judge_post", fake_post)
    client = openrouter_judge.make_judge_client(
        api_key="judge-key",
        api_base="https://openrouter.ai/api/v1",
        model="openai/gpt-5.1",
        max_try=1,
    )
    result = client("rubric_0", "Rate this report.")
    assert result["score"] is None
    assert result["score_valid"] is False
    assert result["judge_completed"] is False
    assert result["failure_category"] == "BLOCKED_JUDGE_REGION"


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (401, "Invalid API key", "BLOCKED_JUDGE_AUTH"),
        (402, "Insufficient credits", "BLOCKED_JUDGE_QUOTA"),
        (429, "Rate limit exceeded", "BLOCKED_JUDGE_RATE_LIMIT"),
        (500, "Internal server error", "JUDGE_PROVIDER_FAILED"),
        (503, "Bad gateway", "JUDGE_PROVIDER_FAILED"),
        (403, "Forbidden", "JUDGE_PROVIDER_FAILED"),
    ],
)
def test_judge_http_failure_classification(
    status: int, message: str, expected: str
) -> None:
    assert (
        openrouter_judge.classify_judge_http_status(
            status,
            {"error": {"message": message, "code": status}},
        )
        == expected
    )


def test_invalid_json_never_becomes_valid_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**kwargs):
        return _fake_http_response("not-json")

    monkeypatch.setattr(openrouter_judge, "http_judge_post", fake_post)
    client = openrouter_judge.make_judge_client(
        api_key="judge-key",
        api_base="https://openrouter.ai/api/v1",
        model="openai/gpt-5.1",
        max_try=1,
    )
    result = client("rubric_0", "Rate this report.")
    assert result["score"] is None
    assert result["score_valid"] is False
    assert result["judge_completed"] is False
    assert result["failure_category"] == "JUDGE_RESPONSE_INVALID"


def test_explicit_valid_zero_remains_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**kwargs):
        return _fake_http_response('{"reasoning":"criterion is absent","score":0}')

    monkeypatch.setattr(openrouter_judge, "http_judge_post", fake_post)
    client = openrouter_judge.make_judge_client(
        api_key="judge-key",
        api_base="https://openrouter.ai/api/v1",
        model="openai/gpt-5.1",
        max_try=1,
    )
    result = client("rubric_0", "Rate this report.")
    assert result["score"] == 0.0
    assert result["score_valid"] is True
    assert result["judge_completed"] is True
    assert result["failure_category"] is None


def test_aggregate_marks_pass_invalid_when_any_rubric_fails() -> None:
    score = _real_score_module()
    valid = {
        "score": 50.0,
        "weight": 1.0,
        "score_valid": True,
        "failure_category": None,
        "returned_provider": "Azure",
        "http_requests": 1,
        "usage": None,
    }
    failed = {
        "score": None,
        "weight": 1.0,
        "score_valid": False,
        "failure_category": "BLOCKED_JUDGE_REGION",
        "returned_provider": None,
        "http_requests": 2,
        "usage": None,
    }
    summary = score._aggregate_score([valid, failed])
    assert summary["score_valid"] is False
    assert summary["judge_completed"] is False
    assert summary["total_score"] is None
    assert summary["failure_category"] == "BLOCKED_JUDGE_REGION"
    assert summary["http_requests"] == 3


# ---------------------------------------------------------------------------
# rejudge-sealed
# ---------------------------------------------------------------------------


def _valid_pass_payload(
    score: float,
    *,
    run_id: str = "Life_005_a0_baseline",
) -> dict:
    return {
        "run_id": run_id,
        "task_id": "Life_005",
        "agent_name": "Unknown",
        "items": [
            {
                "index": 0,
                "type": "image",
                "content": "criterion",
                "weight": 1.0,
                "score": score,
                "reasoning": "The report addresses the criterion.",
                "score_valid": True,
                "judge_completed": True,
                "failure_category": None,
                "requested_model": "openai/gpt-5.1",
                "requested_provider": "azure",
                "returned_provider": "Azure",
                "http_status": 200,
                "response_body_present": True,
                "content_present": True,
                "json_parse_success": True,
                "schema_valid": True,
                "http_requests": 1,
                "usage": None,
            }
        ],
        "total_score": score,
        "total_weight": 1.0,
        "score_valid": True,
        "judge_completed": True,
        "failure_category": None,
        "requested_model": "openai/gpt-5.1",
        "requested_provider": "azure",
        "returned_provider": "Azure",
        "http_requests": 1,
        "usage": None,
    }


def _make_source_experiment(
    tmp_path: Path,
    *,
    baseline_hash: str | None = None,
    evolved_hash: str | None = None,
) -> Path:
    exp = tmp_path / "exp"
    baseline = exp / "items" / "Life_005" / "runs" / "src" / "baseline_candidate"
    evolved = exp / "items" / "Life_005" / "runs" / "src" / "evolved_candidate"
    for root, run_id in (
        (baseline, "Life_005_a0_baseline"),
        (evolved, "Life_005_a0_evolved"),
    ):
        root.mkdir(parents=True, exist_ok=True)
        _write_candidate_deliverables(root)
        (root / "_meta.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "task_id": "Life_005",
                    "run_id": run_id,
                }
            ),
            encoding="utf-8",
        )
    observed_baseline = candidate_artifact_root_sha256(baseline)
    observed_evolved = candidate_artifact_root_sha256(evolved)
    store = TrainingStateStore(exp / "supervisor" / "src")
    store.initialize_experiment(
        experiment_id="src",
        protocol_sha256="0" * 64,
        core_identity_sha256="1" * 64,
        adapter_identity_sha256="2" * 64,
        initial_state={
            "task_id": "Life_005",
            "baseline_receipt": {"candidate_output_root": str(baseline)},
            "evolved_receipt": {"candidate_output_root": str(evolved)},
            "baseline_sealed_hash": baseline_hash or observed_baseline,
            "evolved_sealed_hash": evolved_hash or observed_evolved,
        },
        initial_stage=MinimalStage.COMPLETE.value,
    )
    return exp


def _fake_probe_ok(**kwargs) -> dict:
    return {
        "status": "JUDGE_SUBPROCESS_OK",
        "failure_category": None,
        "http_status": 200,
        "requested_model": "openai/gpt-5.1",
        "requested_provider": "azure",
        "returned_provider": "Azure",
        "content": "JUDGE_SUBPROCESS_OK",
    }


def test_rejudge_sealed_uses_exactly_two_judge_jobs_and_no_candidate_evolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source_experiment(tmp_path)
    (tmp_path / "rcb").mkdir()
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("RCB_JUDGE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("RCB_JUDGE_MODEL", "openai/gpt-5.1")
    judge_calls: list[dict] = []
    baseline_before = _file_inventory(
        exp / "items" / "Life_005" / "runs" / "src" / "baseline_candidate"
    )
    evolved_before = _file_inventory(
        exp / "items" / "Life_005" / "runs" / "src" / "evolved_candidate"
    )

    def fake_judge(**kwargs):
        judge_calls.append(kwargs)
        pass_name = (
            "evolved" if kwargs["attempt_id"].endswith("evolved") else "baseline"
        )
        score = 66.0 if pass_name == "evolved" else 62.0
        return SimpleNamespace(
            raw_score=_valid_pass_payload(score, run_id=kwargs["attempt_id"])
        )

    result = run_rejudge_sealed(
        experiment_root=exp,
        source_run_id="src",
        rejudge_id="rejudge-azure-v1",
        researchclawbench_root=tmp_path / "rcb",
        judge_executor=fake_judge,
        probe_executor=_fake_probe_ok,
    )
    assert result["status"] == "REJUDGE_SEALED_CLOSED"
    assert result["candidate_jobs"] == 0
    assert result["evolution_jobs"] == 0
    assert result["judge_logical_jobs"] == 2
    assert result["actual_http_requests"] == 3
    assert result["baseline_score"] == 62.0
    assert result["evolved_score"] == 66.0
    assert result["delta"] == 4.0
    assert result["score_valid"] is True
    assert result["paired_score_valid"] is True
    assert len(judge_calls) == 2
    identities = [
        (
            call["expected_model"],
            call["expected_api_base"],
            call["expected_requested_provider"],
            call["task_id"],
        )
        for call in judge_calls
    ]
    assert identities == [
        ("openai/gpt-5.1", "https://openrouter.ai/api/v1", "azure", "Life_005"),
        ("openai/gpt-5.1", "https://openrouter.ai/api/v1", "azure", "Life_005"),
    ]
    baseline_after = _file_inventory(
        exp / "items" / "Life_005" / "runs" / "src" / "baseline_candidate"
    )
    evolved_after = _file_inventory(
        exp / "items" / "Life_005" / "runs" / "src" / "evolved_candidate"
    )
    assert baseline_after == baseline_before
    assert evolved_after == evolved_before
    result_path = (
        exp
        / "items"
        / "Life_005"
        / "rejudges"
        / "rejudge-azure-v1"
        / "rejudge_result.json"
    )
    assert result_path.is_file()
    assert result["baseline_sealed_hash_matches"] is True
    assert result["evolved_sealed_hash_matches"] is True
    assert (exp / "evaluator_private" / "rejudge-azure-v1").is_dir()


def test_rejudge_rejects_sealed_hash_mismatch_before_any_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source_experiment(tmp_path, baseline_hash="0" * 64)
    (tmp_path / "rcb").mkdir()
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("RCB_JUDGE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("RCB_JUDGE_MODEL", "openai/gpt-5.1")
    calls: list[str] = []

    def fake_probe(**kwargs):
        calls.append("probe")
        return _fake_probe_ok(**kwargs)

    def fake_judge(**kwargs):
        calls.append("judge")
        raise AssertionError("judge must not be called")

    with pytest.raises(RejudgeSealedError, match="BASELINE"):
        run_rejudge_sealed(
            experiment_root=exp,
            source_run_id="src",
            rejudge_id="rejudge-mismatch",
            researchclawbench_root=tmp_path / "rcb",
            judge_executor=fake_judge,
            probe_executor=fake_probe,
        )
    assert calls == []


def test_rejudge_is_blocked_when_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source_experiment(tmp_path)
    (tmp_path / "rcb").mkdir()
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("RCB_JUDGE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("RCB_JUDGE_MODEL", "openai/gpt-5.1")
    judge_calls: list[dict] = []

    def fake_probe(**kwargs) -> dict:
        return {
            "status": "JUDGE_SUBPROCESS_FAILED",
            "failure_category": "BLOCKED_JUDGE_AUTH",
            "http_status": None,
            "requested_model": "openai/gpt-5.1",
            "requested_provider": "azure",
            "returned_provider": None,
            "content": None,
        }

    def fake_judge(**kwargs):
        judge_calls.append(kwargs)
        raise AssertionError("judge must not be called")

    result = run_rejudge_sealed(
        experiment_root=exp,
        source_run_id="src",
        rejudge_id="rejudge-probe-fail",
        researchclawbench_root=tmp_path / "rcb",
        judge_executor=fake_judge,
        probe_executor=fake_probe,
    )
    assert result["status"] == "REJUDGE_SEALED_BLOCKED"
    assert result["failure_category"] == "BLOCKED_JUDGE_AUTH"
    assert result["judge_logical_jobs"] == 0
    assert judge_calls == []


def test_judge_probe_uses_closed_evaluator_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "probe-key")
    monkeypatch.setenv("RCB_JUDGE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("RCB_JUDGE_MODEL", "openai/gpt-5.1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("CODEX_HOME", "/home/user/.codex-deepseek")
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        output = Path(command[command.index("--probe-output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(_fake_probe_ok()), encoding="utf-8")
        return Completed()

    monkeypatch.setattr(community_evaluator.subprocess, "run", fake_run)
    result = community_evaluator.run_judge_probe_subprocess(
        project_root=WORKSPACE,
        output_root=tmp_path / "probe",
        expected_model="openai/gpt-5.1",
        expected_api_base="https://openrouter.ai/api/v1",
    )
    assert result["status"] == "JUDGE_SUBPROCESS_OK"
    command = observed["command"]
    assert isinstance(command, list)
    assert "--judge-probe" in command
    child_env = observed["env"]
    assert isinstance(child_env, dict)
    assert child_env["RCB_JUDGE_API_KEY"] == "probe-key"
    assert child_env["RCB_JUDGE_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert child_env["RCB_JUDGE_MODEL"] == "openai/gpt-5.1"
    assert "DEEPSEEK_API_KEY" not in child_env
    assert "CODEX_HOME" not in child_env
