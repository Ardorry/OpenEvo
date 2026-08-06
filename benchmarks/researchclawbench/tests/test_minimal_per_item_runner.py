"""Zero-call tests for the minimal per-item runner."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from openevo_researchclawbench.community_evaluator import (
    _closed_evaluator_subprocess_environment,
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


WORKSPACE = Path(__file__).resolve().parents[4]
REAL_RCB = Path("/home/lhy-h/work/researchclaw_openevo/ResearchClawBench")


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
    assert Path(captured["project_root"]) == config.researchclawbench_root.parent
    assert captured["task_id"] == "Life_005"
    assert captured["attempt_id"] == "Life_005_a0_baseline"
    assert private_root.is_dir()
    assert "JUDGE_MODEL_NAME" not in os.environ
    assert "JUDGE_API_KEY" not in os.environ
    assert "JUDGE_API_BASE" not in os.environ


def test_judge_subprocess_env_has_no_deepseek_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("JUDGE_API_BASE", "https://judge.invalid")
    monkeypatch.setenv("JUDGE_MODEL_NAME", "gpt-5.1")
    import openevo

    openevo_source_root = Path(openevo.__file__).resolve(strict=True).parents[1]
    env = _closed_evaluator_subprocess_environment(
        project_root=WORKSPACE,
        judge_env_file=None,
    )
    assert "DEEPSEEK_API_KEY" not in env
    assert "CODEX_HOME" not in env
    assert env["JUDGE_API_KEY"] == "judge-key"
    assert str(openevo_source_root) in env["PYTHONPATH"]


def test_non_life_005_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _make_config(tmp_path, task_id="Alpha_004")


def test_old_runner_modules_still_import() -> None:
    import openevo_researchclawbench.training_supervisor  # noqa: F401
    import openevo_researchclawbench.community_evaluator  # noqa: F401
