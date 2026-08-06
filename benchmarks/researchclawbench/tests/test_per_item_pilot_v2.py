"""Zero-call tests for the per-item pilot v2 protocol."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openevo_researchclawbench.deepseek_codex_engineering_port import (
    CodexExecutionResult,
)
from openevo_researchclawbench.per_item_pilot_v2 import (
    MAX_JUDGE_HTTP_REQUESTS,
    PILOT_TASKS,
    PILOT_V2_PROTOCOL_NAME,
    PilotV2Config,
    PilotV2DryRunCandidate,
    PilotV2DryRunEvolution,
    PilotV2DryRunJudge,
    PilotV2Runner,
    PilotV2Stage,
    append_artifact_consumption_instructions,
)

REAL_RCB = Path("/home/lhy-h/work/researchclaw_openevo/ResearchClawBench")


def _make_fake_rcb(tmp_path: Path) -> Path:
    root = tmp_path / f"RCB_{tmp_path.name}"
    shutil.copytree(REAL_RCB / "evaluation", root / "evaluation", dirs_exist_ok=True)
    for task_id in PILOT_TASKS:
        task_dir = root / "tasks" / task_id
        (task_dir / "data").mkdir(parents=True, exist_ok=True)
        (task_dir / "related_work").mkdir(parents=True, exist_ok=True)
        (task_dir / "target_study").mkdir(parents=True, exist_ok=True)
        (task_dir / "task_info.json").write_text(
            json.dumps(
                {
                    "task": f"Public task fixture for {task_id}.",
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
            json.dumps(
                [
                    {
                        "type": "text",
                        "content": f"Criterion A for {task_id}.",
                        "keywords": ["alpha"],
                        "weight": 0.34,
                    },
                    {
                        "type": "text",
                        "content": f"Criterion B for {task_id}.",
                        "keywords": ["beta"],
                        "weight": 0.33,
                    },
                    {
                        "type": "text",
                        "content": f"Criterion C for {task_id}.",
                        "keywords": ["gamma"],
                        "weight": 0.33,
                    },
                ]
            ),
            encoding="utf-8",
        )
    return root


def _make_config(tmp_path: Path) -> PilotV2Config:
    rcb = _make_fake_rcb(tmp_path)
    output_root = tmp_path / "exp" / "out"
    output_root.mkdir(parents=True, exist_ok=True)
    raw = {
        "protocol_name": PILOT_V2_PROTOCOL_NAME,
        "split": "community",
        "task_ids": list(PILOT_TASKS),
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
            "timeout_seconds": 1800,
            "sandbox": "workspace-write",
        },
        "evolution": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "generation": 1,
            "timeout_seconds": 1800,
        },
        "judge": {
            "provider": "openai_compatible",
            "model": "openai/gpt-5.1",
            "provider_only": "azure",
            "allow_fallbacks": False,
            "timeout_seconds": 1800,
        },
        "execution": {
            "max_tasks": 3,
            "max_retries": 0,
            "max_candidate_jobs": 6,
            "max_evolution_jobs": 9,
            "max_judge_http_requests": MAX_JUDGE_HTTP_REQUESTS,
            "stop_on_infrastructure_failure": True,
            "leaderboard_eligible": False,
            "official_protocol": False,
            "dry_run": True,
        },
    }
    return PilotV2Config(tmp_path / "protocol.yaml", raw)


def _make_runner(
    config: PilotV2Config,
    *,
    batch_id: str = "pilot-test",
    candidate=None,
    evolution=None,
    judge=None,
    dry_run: bool = True,
    adopt_from: str | None = None,
    mark_nondelivery: tuple[str, ...] = (),
) -> PilotV2Runner:
    return PilotV2Runner(
        config=config,
        batch_id=batch_id,
        state_root=config.output_root / "supervisor" / batch_id,
        candidate_port=candidate or PilotV2DryRunCandidate(),
        evolution_port=evolution or PilotV2DryRunEvolution(),
        judge_port=judge or PilotV2DryRunJudge(),
        dry_run=dry_run,
        adopt_from=adopt_from,
        mark_nondelivery=mark_nondelivery,
    )


def test_pilot_v2_config_is_frozen(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.raw["execution"]["max_judge_http_requests"] == MAX_JUDGE_HTTP_REQUESTS
    assert MAX_JUDGE_HTTP_REQUESTS == 34
    assert config.raw["execution"]["max_retries"] == 0


def test_artifact_consumption_instructions_reference_three_files() -> None:
    evolved = append_artifact_consumption_instructions("# Task\nDo work.")
    for filename in ("AGENTS.md", "memory.md", "skills/skill/SKILL.md"):
        assert filename in evolved
    assert "artifact_read_requested=true" in evolved
    assert "Do not copy the ground-truth checklist" in evolved
    assert "Judge" not in evolved.replace("Judge outputs", "")


def test_baseline_prompt_has_no_artifact_references(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = _make_runner(config)
    runner.initialize()
    runner.run_next()
    state = runner.status()
    baseline_prompt = (
        Path(state["tasks"]["Astronomy_004"]["baseline_candidate_workspace"])
        / "INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")
    assert "AGENTS.md" not in baseline_prompt
    assert "memory.md" not in baseline_prompt
    assert "skills/skill/SKILL.md" not in baseline_prompt


def test_dry_run_three_tasks_complete_with_zero_provider_calls(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    candidate = PilotV2DryRunCandidate()
    evolution = PilotV2DryRunEvolution()
    judge = PilotV2DryRunJudge()
    runner = _make_runner(
        config,
        batch_id="dry-run-3",
        candidate=candidate,
        evolution=evolution,
        judge=judge,
    )
    runner.initialize()
    state = runner.run_until_terminal()
    assert state["stage"] == PilotV2Stage.PILOT_CLOSED.value
    assert candidate.provider_calls == 0
    assert evolution.provider_calls == 0
    assert judge.provider_calls == 0
    assert state["candidate_jobs"] == 6
    assert state["evolution_jobs"] == 9
    assert state["judge_logical_jobs"] == 6
    assert state["judge_http_requests"] == 18
    summary = json.loads(
        (config.output_root / "pilot_batch_result.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "PILOT_V2_CLOSED"
    assert [row["task_id"] for row in summary["rows"]] == list(PILOT_TASKS)
    assert summary["mean_delta"] == 4.0
    assert summary["median_delta"] == 4.0
    assert summary["wins"] == 3


def test_artifact_hashes_match_sealed_artifacts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = _make_runner(config, batch_id="hash-check")
    runner.initialize()
    state = runner.run_until_terminal()
    for task_id in PILOT_TASKS:
        task = state["tasks"][task_id]
        artifact_root = Path(task["evolution_receipt"]["artifact_root"])
        expected_paths = {
            "memory": artifact_root / "memory.md",
            "skill": artifact_root / "skill" / "SKILL.md",
            "agent_system": artifact_root / "agent_system" / "AGENTS.md",
        }
        candidate_workspace = Path(task["evolved_candidate_workspace"])
        injected = {
            "memory": candidate_workspace / "memory.md",
            "skill": candidate_workspace / "skills" / "skill" / "SKILL.md",
            "agent_system": candidate_workspace / "AGENTS.md",
        }
        for name, expected_hash in task["artifact_hashes"].items():
            assert expected_paths[name].read_bytes() == injected[name].read_bytes()
            import hashlib

            assert hashlib.sha256(injected[name].read_bytes()).hexdigest() == (
                expected_hash
            )


def test_evolved_instructions_require_artifact_consumption(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = _make_runner(config, batch_id="evolved-instructions")
    runner.initialize()
    state = runner.run_until_terminal()
    task = state["tasks"]["Astronomy_004"]
    prompt = (
        Path(task["evolved_candidate_workspace"]) / "INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")
    for filename in ("AGENTS.md", "memory.md", "skills/skill/SKILL.md"):
        assert filename in prompt
    meta = json.loads(
        (Path(task["evolved_candidate_workspace"]) / "_meta.json").read_text(
            encoding="utf-8"
        )
    )
    assert meta["artifact_read_requested"] is True
    assert meta["injected_artifact_paths"] == [
        "AGENTS.md",
        "memory.md",
        "skills/skill/SKILL.md",
    ]
    assert meta["injected_artifact_sha256"] == task["injected_artifact_sha256"]


def test_missing_artifact_fails_closed(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    class MissingMemoryEvolution(PilotV2DryRunEvolution):
        def evolve(self, request):
            result = super().evolve(request)
            (Path(result["artifact_root"]) / "memory.md").unlink()
            return result

    runner = _make_runner(
        config,
        batch_id="missing-artifact",
        evolution=MissingMemoryEvolution(),
    )
    runner.initialize()
    state = runner.run_until_terminal()
    assert state["stage"] == PilotV2Stage.PILOT_BLOCKED.value
    assert state["failure_code"] == "ARTIFACT_MISSING"


def test_task_b_does_not_read_task_a_artifacts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = _make_runner(config, batch_id="isolation")
    runner.initialize()
    state = runner.run_until_terminal()
    task_b = state["tasks"]["Chemistry_004"]
    prompt_b = (
        Path(task_b["evolved_candidate_workspace"]) / "INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")
    assert "Item-scoped Astronomy_004" not in prompt_b
    workspace_b = Path(task_b["evolved_candidate_workspace"])
    for artifact_name in ("memory.md", "AGENTS.md"):
        content = (workspace_b / artifact_name).read_text(encoding="utf-8")
        assert "Astronomy_004" not in content


def test_judge_http_budget_is_enforced(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = _make_runner(
        config,
        batch_id="budget-limit",
        dry_run=False,
    )
    runner._judge_http_limit = 6
    runner.initialize()
    state = runner.run_until_terminal()
    assert state["stage"] == PilotV2Stage.PILOT_BUDGET_EXHAUSTED.value
    assert state["failure_code"] == "JUDGE_HTTP_BUDGET_EXHAUSTED"
    assert state["judge_logical_jobs"] == 2
    assert state["judge_http_requests"] == 6


def test_adopt_partial_prior_batch_continues_without_rerunning_completed_task(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    runner_one = _make_runner(config, batch_id="batch-one", dry_run=False)
    runner_one._judge_http_limit = 10
    runner_one.initialize()
    state_one = runner_one.run_until_terminal()
    assert state_one["stage"] == PilotV2Stage.PILOT_BUDGET_EXHAUSTED.value
    assert "evolved_judge" in state_one["tasks"]["Astronomy_004"]
    assert "evolved_judge" not in state_one["tasks"].get("Chemistry_004", {})

    candidate_two = PilotV2DryRunCandidate()
    evolution_two = PilotV2DryRunEvolution()
    judge_two = PilotV2DryRunJudge()
    runner_two = _make_runner(
        config,
        batch_id="batch-two",
        candidate=candidate_two,
        evolution=evolution_two,
        judge=judge_two,
        adopt_from="batch-one",
    )
    runner_two.initialize()
    state_two = runner_two.run_until_terminal()
    assert state_two["stage"] == PilotV2Stage.PILOT_CLOSED.value
    assert len(candidate_two.calls) == 4
    assert len(evolution_two.calls) == 2
    assert len(judge_two.calls) == 4
    assert state_two["candidate_jobs"] == 6
    assert state_two["evolution_jobs"] == 9
    assert state_two["judge_logical_jobs"] == 6
    assert state_two["judge_http_requests"] == 18
    assert set(state_two["tasks"]) == set(PILOT_TASKS)
    astronomy_calls = [
        call for call in candidate_two.calls if call["task_id"] == "Astronomy_004"
    ]
    assert astronomy_calls == []
    summary = json.loads(
        (config.output_root / "pilot_batch_result.json").read_text(encoding="utf-8")
    )
    assert [row["task_id"] for row in summary["rows"]] == list(PILOT_TASKS)


def test_adopt_prior_completed_batch_closes_without_any_calls(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    runner_one = _make_runner(config, batch_id="batch-one")
    runner_one.initialize()
    state_one = runner_one.run_until_terminal()
    assert state_one["stage"] == PilotV2Stage.PILOT_CLOSED.value

    candidate_two = PilotV2DryRunCandidate()
    evolution_two = PilotV2DryRunEvolution()
    judge_two = PilotV2DryRunJudge()
    runner_two = _make_runner(
        config,
        batch_id="batch-two",
        candidate=candidate_two,
        evolution=evolution_two,
        judge=judge_two,
        adopt_from="batch-one",
    )
    runner_two.initialize()
    state_two = runner_two.run_until_terminal()
    assert state_two["stage"] == PilotV2Stage.PILOT_CLOSED.value
    assert candidate_two.calls == []
    assert evolution_two.calls == []
    assert judge_two.calls == []
    assert state_two["candidate_jobs"] == 6
    assert state_two["evolution_jobs"] == 9
    assert state_two["judge_logical_jobs"] == 6
    assert state_two["judge_http_requests"] == 18


def test_mark_nondelivery_continues_with_information(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    class EmptyCandidate(PilotV2DryRunCandidate):
        def execute(self, request):
            if (
                request["task_id"] == "Chemistry_004"
                and request["pass_name"] == "baseline"
            ):
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
            return super().execute(request)

    runner_one = _make_runner(
        config,
        batch_id="batch-one",
        candidate=EmptyCandidate(),
    )
    runner_one.initialize()
    state_one = runner_one.run_until_terminal()
    assert state_one["stage"] == PilotV2Stage.PILOT_BLOCKED.value
    assert state_one["failure_code"] == "WORKSPACE_VALIDATION_FAILED"
    assert "evolved_judge" in state_one["tasks"]["Astronomy_004"]
    assert "baseline_judge" not in state_one["tasks"].get("Chemistry_004", {})

    candidate_two = PilotV2DryRunCandidate()
    evolution_two = PilotV2DryRunEvolution()
    judge_two = PilotV2DryRunJudge()
    runner_two = _make_runner(
        config,
        batch_id="batch-two",
        candidate=candidate_two,
        evolution=evolution_two,
        judge=judge_two,
        adopt_from="batch-one",
        mark_nondelivery=("Chemistry_004",),
    )
    runner_two.initialize()
    state_two = runner_two.run_until_terminal()
    assert state_two["stage"] == PilotV2Stage.PILOT_CLOSED.value
    assert len(candidate_two.calls) == 2
    assert len(evolution_two.calls) == 1
    assert len(judge_two.calls) == 2
    assert state_two["candidate_jobs"] == 5
    assert state_two["evolution_jobs"] == 6
    assert state_two["judge_logical_jobs"] == 4
    assert state_two["judge_http_requests"] == 12
    assert state_two["tasks"]["Chemistry_004"]["status"] == "CANDIDATE_NONDELIVERY"
    assert state_two["tasks"]["Chemistry_004"]["candidate_failure_count"] == 1
    summary = json.loads(
        (config.output_root / "pilot_batch_result.json").read_text(encoding="utf-8")
    )
    assert [row["task_id"] for row in summary["rows"]] == [
        "Astronomy_004",
        "Information_005",
    ]
    assert summary["candidate_nondelivery_count"] == 1
    assert summary["task_completion_rate"] == round(2 / 3, 3)
    assert summary["candidate_delivery_success_rate"] == 0.8
    assert summary["infrastructure_failure_count"] == 0
    assert summary["blocked_count"] == 0


def test_candidate_evidence_is_persisted_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openevo_researchclawbench.deepseek_codex_engineering_port as port_module

    secret = "sk-dry-secret-value"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    state = {"calls": []}
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "INSTRUCTIONS.md").write_text("# Task\nDo work.\n", encoding="utf-8")

    def fake_executor(argv, *, env, cwd, prompt, timeout_seconds):
        state["calls"].append(
            {"argv": list(argv), "cwd": str(cwd), "env_keys": sorted(env)}
        )
        (Path(cwd) / "code").mkdir(exist_ok=True)
        (Path(cwd) / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (Path(cwd) / "outputs").mkdir(exist_ok=True)
        (Path(cwd) / "outputs" / "summary.json").write_text(
            json.dumps({"ok": True}), encoding="utf-8"
        )
        report = Path(cwd) / "report"
        (report / "images").mkdir(parents=True, exist_ok=True)
        (report / "report.md").write_text(
            "## Methodology\nwork\n\n## Results\nok\n\n## Discussion\nok\n\n![f](images/f.png)",
            encoding="utf-8",
        )
        (report / "images" / "f.png").write_bytes(_PILOT_PNG)
        (Path(cwd) / "_meta.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "task_id": "Astronomy_004",
                    "run_id": "Astronomy_004_a0_baseline",
                }
            ),
            encoding="utf-8",
        )
        events = "\n".join(
            (
                json.dumps(
                    {
                        "timestamp": "t1",
                        "type": "event_msg",
                        "payload": {"message": f"reading memory.md secret={secret}"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "t2",
                        "type": "agent_message",
                        "payload": {"message": "final assistant text"},
                    }
                ),
            )
        )
        return CodexExecutionResult(
            returncode=0,
            stdout=events + "\n",
            stderr="",
            duration_seconds=0.1,
        )

    port = port_module.DeepSeekCodexEngineeringPort(
        executor=fake_executor, dry_run=True
    )
    evidence_root = tmp_path / "evidence"
    receipt = port.candidate(
        {
            "task_id": "Astronomy_004",
            "run_id": "Astronomy_004_a0_baseline",
            "pass_name": "baseline",
            "workspace": str(workspace),
            "evidence_root": str(evidence_root),
        }
    )
    events = (evidence_root / "codex_events.jsonl").read_text(encoding="utf-8")
    assert secret not in events
    assert "[REDACTED]" in events
    assert '"message": "final assistant text"' in events
    final = json.loads(
        (evidence_root / "final_assistant_message.json").read_text(encoding="utf-8")
    )
    assert final["message"] == "final assistant text"
    command = json.loads(
        (evidence_root / "command.json").read_text(encoding="utf-8")
    )
    assert command["model"] == "deepseek-v4-flash"
    assert command["env_keys"] == sorted(port._environment())
    assert (evidence_root / "output_manifest.json").is_file()
    assert receipt["evidence_sha256"] == port_module._evidence_root_sha256(
        evidence_root
    )


def test_secret_scan_of_pilot_artifacts() -> None:
    from openevo_researchclawbench import per_item_pilot_v2

    source = Path(per_item_pilot_v2.__file__).read_text(encoding="utf-8")
    assert "sk-or-v1-" not in source
    config_path = (
        Path(__file__).resolve().parents[3]
        / "benchmarks"
        / "researchclaw"
        / "configs"
        / "per_item_minimal_deepseek_pilot_v2.yaml"
    )
    config_text = config_path.read_text(encoding="utf-8")
    assert "sk-or-v1-" not in config_text
    assert "Authorization" not in config_text


_PILOT_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
