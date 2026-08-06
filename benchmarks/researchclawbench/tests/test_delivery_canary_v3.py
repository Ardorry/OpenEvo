"""Zero-call tests for the Protocol v3 delivery reliability canary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openevo_researchclawbench.artifact_validator import validate_workspace
from openevo_researchclawbench.delivery_canary_v3 import (
    CANARY_TASKS,
    CANARY_V3_PROTOCOL_NAME,
    DeliveryCanaryConfig,
    DeliveryCanaryDryRunCandidate,
    DeliveryCanaryError,
    DeliveryCanaryV3Runner,
    CanaryStage,
)
from openevo_researchclawbench.delivery_contract import (
    DELIVERY_CONTRACT_BLOCK,
    analyze_delivery_evidence,
    append_delivery_contract,
)

REAL_RCB = Path("/home/lhy-h/work/researchclaw_openevo/ResearchClawBench")


def _long_report() -> str:
    return (
        "# Report\n\n"
        "## Methodology\n"
        "This deterministic fixture implements the delivery contract. "
        "The analysis reads the provided task inputs and related work, "
        "computes the required summary statistics, and writes reproducible "
        "code and outputs into the workspace. Every input table is loaded "
        "from the read-only data directory, every intermediate result is "
        "written into the outputs directory, and the final figures are "
        "generated into the report images directory so the full pipeline is "
        "reproducible from the sealed workspace alone. The implementation "
        "entry points are documented in the code directory and the exact "
        "commands are listed in the reproducibility section below.\n\n"
        "## Results\n"
        "The primary quantitative result is an estimated effect of 0.42 "
        "with 128 observations, shown in figure f. The summary table "
        "reports the group means, the standard deviations, and the paired "
        "differences, all consistent with the deterministic fixture. The "
        "figure was generated from the same output tables and is referenced "
        "directly from this results section, which makes the numerical "
        "claims traceable to the outputs directory. A second validation "
        "run confirms that the numbers do not drift between executions and "
        "that the reported effect size remains stable under the same "
        "random seed, providing a reproducibility check for the fixture.\n\n"
        "## Discussion\n"
        "The delivery contract was followed: the scaffold was created "
        "early, intermediate results were saved in outputs/, and the final "
        "report was completed before the self-check. The important "
        "conclusion for engineering purposes is that the workspace sealing "
        "step and the delivery evidence analyzer accept the same artifact "
        "format, which validates the Protocol v3 candidate delivery path "
        "before any live model invocation is attempted. The remaining "
        "limitation is that this fixture is deterministic, so it exercises "
        "the contract mechanics rather than the scientific content of a "
        "real candidate run.\n\n"
        "![f](images/f.png)\n"
    )


def _make_fake_rcb(tmp_path: Path) -> Path:
    root = tmp_path / f"RCB_{tmp_path.name}"
    shutil.copytree(REAL_RCB / "evaluation", root / "evaluation", dirs_exist_ok=True)
    for task_id in CANARY_TASKS:
        task_dir = root / "tasks" / task_id
        (task_dir / "data").mkdir(parents=True, exist_ok=True)
        (task_dir / "related_work").mkdir(parents=True, exist_ok=True)
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
    return root


def _make_config(tmp_path: Path) -> DeliveryCanaryConfig:
    rcb = _make_fake_rcb(tmp_path)
    output_root = tmp_path / "exp" / "out"
    output_root.mkdir(parents=True, exist_ok=True)
    raw = {
        "protocol_name": CANARY_V3_PROTOCOL_NAME,
        "split": "community",
        "task_ids": list(CANARY_TASKS),
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
        "execution": {
            "max_candidate_jobs": 2,
            "max_evolution_jobs": 0,
            "max_judge_jobs": 0,
            "max_retries": 0,
            "leaderboard_eligible": False,
            "official_protocol": False,
            "dry_run": True,
        },
    }
    return DeliveryCanaryConfig(tmp_path / "protocol.yaml", raw)


def _runner(config: DeliveryCanaryConfig, *, batch_id: str = "canary-test", candidate=None):
    return DeliveryCanaryV3Runner(
        config=config,
        batch_id=batch_id,
        state_root=config.output_root / "supervisor" / batch_id,
        candidate_port=candidate or DeliveryCanaryDryRunCandidate(),
        dry_run=True,
    )


def _write_evidence(
    evidence_root: Path,
    *,
    events: list[dict],
    final_message: str | None = "The task is complete.",
) -> None:
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "codex_events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    (evidence_root / "stdout.log").write_text("", encoding="utf-8")
    (evidence_root / "stderr.log").write_text("", encoding="utf-8")
    if final_message is not None:
        (evidence_root / "final_assistant_message.json").write_text(
            json.dumps({"message": final_message}),
            encoding="utf-8",
        )


def test_canary_v3_config_is_frozen(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.raw["execution"]["max_candidate_jobs"] == 2
    assert config.raw["execution"]["max_evolution_jobs"] == 0
    assert config.raw["execution"]["max_judge_jobs"] == 0
    assert config.raw["execution"]["max_retries"] == 0


def test_delivery_contract_is_generic_and_complete() -> None:
    for required in (
        "code/",
        "outputs/",
        "report/report.md",
        "report/images/",
        "find code outputs report images",
        "final assistant message",
    ):
        assert required in DELIVERY_CONTRACT_BLOCK
    for forbidden in ("Chemistry_004", "Information_005", "Astronomy_004"):
        assert forbidden not in DELIVERY_CONTRACT_BLOCK


def test_same_delivery_contract_for_baseline_and_evolved() -> None:
    baseline = append_delivery_contract("# Baseline task")
    evolved = append_delivery_contract("# Evolved task with artifacts")
    assert baseline.endswith(DELIVERY_CONTRACT_BLOCK)
    assert evolved.endswith(DELIVERY_CONTRACT_BLOCK)
    assert baseline.split(DELIVERY_CONTRACT_BLOCK)[0] != evolved.split(
        DELIVERY_CONTRACT_BLOCK
    )[0]


def test_canary_baseline_instructions_include_contract(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = _runner(config)
    runner.initialize()
    runner.run_next()
    state = runner.status()
    workspace = Path(
        state["tasks"]["Chemistry_004"]["baseline_candidate_workspace"]
    )
    instructions = (workspace / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert DELIVERY_CONTRACT_BLOCK in instructions


def _deliverable_workspace(root: Path, *, include: set[str]) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "INSTRUCTIONS.md").write_text("# Task\nDo work.\n", encoding="utf-8")
    (workspace / "_meta.json").write_text(
        json.dumps({"status": "completed", "exit_code": 0}),
        encoding="utf-8",
    )
    if "code" in include:
        (workspace / "code").mkdir()
        (workspace / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    if "outputs" in include:
        (workspace / "outputs").mkdir()
        (workspace / "outputs" / "summary.json").write_text(
            json.dumps(
                {"estimated_effect": 0.42, "observations": 128, "ok": True}
            ),
            encoding="utf-8",
        )
    if "report" in include:
        report = workspace / "report"
        (report / "images").mkdir(parents=True)
        (report / "report.md").write_text(_long_report(), encoding="utf-8")
    if "png" in include:
        png_dir = workspace / "report" / "images"
        png_dir.mkdir(parents=True, exist_ok=True)
        (png_dir / "f.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    return workspace


@pytest.mark.parametrize(
    "missing",
    ["code", "outputs", "report", "png"],
)
def test_seal_fails_when_core_deliverable_missing(
    tmp_path: Path, missing: str
) -> None:
    include = {"code", "outputs", "report", "png"} - {missing}
    workspace = _deliverable_workspace(tmp_path, include=include)
    result = validate_workspace(workspace)
    assert result.passed is False


def test_seal_passes_when_all_deliverables_present(tmp_path: Path) -> None:
    workspace = _deliverable_workspace(
        tmp_path, include={"code", "outputs", "report", "png"}
    )
    result = validate_workspace(workspace)
    assert result.passed is True


def _tool_event(item_type: str, command: str = "") -> dict:
    return {
        "type": "item.started",
        "item": (
            {"type": item_type, "command": command}
            if item_type == "command_execution"
            else {"type": item_type}
        ),
    }


def test_analyzer_marks_late_first_write(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    events = [
        _tool_event("command_execution", "ls -la"),
        _tool_event("command_execution", "which python3"),
        _tool_event("command_execution", "cat INSTRUCTIONS.md"),
        _tool_event("command_execution", "strings paper.pdf | head"),
        _tool_event("command_execution", "grep -r alpha ."),
        _tool_event("command_execution", "python3 - <<'PY'\nprint(1)\nPY"),
        _tool_event("file_change"),
    ]
    _write_evidence(evidence_root, events=events)
    result = analyze_delivery_evidence(
        evidence_root=evidence_root,
        workspace=tmp_path / "missing",
    )
    assert result["first_workspace_write_event_index"] == 7
    assert result["first_workspace_write_at"] is not None


def test_analyzer_detects_early_write(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    events = [
        _tool_event("command_execution", "ls -la"),
        _tool_event(
            "command_execution",
            "mkdir -p code outputs report/images && cat > report/report.md",
        ),
    ]
    _write_evidence(evidence_root, events=events)
    result = analyze_delivery_evidence(
        evidence_root=evidence_root,
        workspace=tmp_path / "missing",
    )
    assert result["first_workspace_write_event_index"] == 2


def test_analyzer_tmp_writes_are_not_delivery(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    events = [
        _tool_event("command_execution", "cat > /tmp/scratch.py <<'PY'\nprint(1)\nPY"),
        _tool_event(
            "command_execution",
            "python3 -m pip install --target /tmp/deps pypdf",
        ),
    ]
    _write_evidence(evidence_root, events=events)
    result = analyze_delivery_evidence(
        evidence_root=evidence_root,
        workspace=tmp_path / "missing",
    )
    assert result["workspace_write_event_count"] == 0
    assert result["tmp_write_event_count"] == 2
    assert result["delivery_classification"] == (
        "CANDIDATE_NONDELIVERY_NO_WORKSPACE_WRITE"
    )


def test_analyzer_metadata_is_not_candidate_delivery(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "_meta.json").write_text(
        json.dumps({"status": "completed", "exit_code": 0}),
        encoding="utf-8",
    )
    _write_evidence(
        evidence_root,
        events=[_tool_event("command_execution", "ls -la")],
    )
    result = analyze_delivery_evidence(
        evidence_root=evidence_root,
        workspace=workspace,
    )
    assert result["workspace_write_event_count"] == 0
    assert result["delivery_classification"] == (
        "CANDIDATE_NONDELIVERY_NO_WORKSPACE_WRITE"
    )


def test_analyzer_missing_final_message_classification(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    workspace = _deliverable_workspace(
        tmp_path, include={"code", "outputs", "report", "png"}
    )
    _write_evidence(
        evidence_root,
        events=[_tool_event("file_change")],
        final_message=None,
    )
    result = analyze_delivery_evidence(
        evidence_root=evidence_root,
        workspace=workspace,
    )
    assert result["required_deliverables_valid"] is True
    assert result["final_message_present"] is False
    assert result["delivery_classification"] == (
        "CANDIDATE_NONDELIVERY_NO_FINAL_MESSAGE"
    )


def test_dry_run_canary_both_tasks_close_with_2_2(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    candidate = DeliveryCanaryDryRunCandidate()
    runner = _runner(config, batch_id="canary-ok", candidate=candidate)
    runner.initialize()
    state = runner.run_until_terminal()
    assert state["stage"] == CanaryStage.CANARY_CLOSED.value
    assert state["candidate_jobs"] == 2
    assert state["canary_result"]["real_candidate_jobs"] == 0
    assert state["evolution_jobs"] == 0
    assert state["judge_jobs"] == 0
    assert len(candidate.calls) == 2
    summary = json.loads(
        (config.output_root / "canary_batch_result.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "DEEPSEEK_DELIVERY_RELIABILITY_PASSED"
    assert summary["candidate_delivery_success_rate"] == 1.0
    assert summary["candidate_jobs"] == 2
    assert all(row["seal"] is True for row in summary["rows"])
    assert all(
        row["delivery_classification"] == "CANDIDATE_DELIVERY_VALID"
        for row in summary["rows"]
    )
    assert summary["evolution_jobs"] == 0
    assert summary["judge_jobs"] == 0
    assert summary["retries"] == 0


class FailingCandidate(DeliveryCanaryDryRunCandidate):
    def execute(self, request):
        self.calls.append(dict(request))
        if request["task_id"] == "Chemistry_004":
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


def test_canary_failure_stops_without_second_task(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    candidate = FailingCandidate()
    runner = _runner(config, batch_id="canary-fail", candidate=candidate)
    runner.initialize()
    state = runner.run_until_terminal()
    assert state["stage"] == CanaryStage.CANARY_FAILED.value
    assert len(candidate.calls) == 1
    summary = json.loads(
        (config.output_root / "canary_batch_result.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "DEEPSEEK_DELIVERY_RELIABILITY_FAILED"
    assert summary["candidate_delivery_success_rate"] == 0.0
    assert summary["candidate_jobs"] == 1
    assert summary["real_candidate_jobs"] == 0
    assert summary["rows"][0]["result"] == "CANDIDATE_NONDELIVERY"
    assert summary["rows"][1]["result"] is None


def test_provider_failure_after_request_counts_candidate_job(
    tmp_path: Path,
) -> None:
    from openevo_researchclawbench.deepseek_codex_engineering_port import (
        DeepSeekEngineeringError,
    )

    class QuotaCandidate(DeliveryCanaryDryRunCandidate):
        def execute(self, request):
            self.calls.append(dict(request))
            raise DeepSeekEngineeringError(
                "BLOCKED_CANDIDATE_QUOTA", "quota exceeded"
            )

    config = _make_config(tmp_path)
    candidate = QuotaCandidate()
    runner = _runner(config, batch_id="canary-quota", candidate=candidate)
    runner.initialize()
    state = runner.run_until_terminal()
    assert state["stage"] == CanaryStage.CANARY_FAILED.value
    assert state["tasks"]["Chemistry_004"]["status"] == "PROVIDER_BLOCKED"
    assert state["tasks"]["Chemistry_004"]["failure_code"] == (
        "BLOCKED_CANDIDATE_QUOTA"
    )
    summary = json.loads(
        (config.output_root / "canary_batch_result.json").read_text(encoding="utf-8")
    )
    assert summary["candidate_jobs"] == 1
    assert summary["rows"][0]["result"] == "PROVIDER_BLOCKED"


def test_pre_dispatch_failure_counts_zero_candidate_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_path)
    runner = _runner(config, batch_id="canary-predispatch")

    def failing_reserve(units: int, key: str) -> None:
        raise DeliveryCanaryError("CANDIDATE_BUDGET_EXHAUSTED", "budget")

    monkeypatch.setattr(runner, "_reserve", failing_reserve)
    runner.initialize()
    state = runner.run_until_terminal()
    assert state["stage"] == CanaryStage.CANARY_FAILED.value
    summary = json.loads(
        (config.output_root / "canary_batch_result.json").read_text(encoding="utf-8")
    )
    assert summary["candidate_jobs"] == 0
    assert summary["real_candidate_jobs"] == 0


def test_gpt_codex_port_argv_and_environment_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openevo_researchclawbench.deepseek_codex_engineering_port import (
        CodexExecutionResult,
        CodexEngineeringPort,
    )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "judge-secret")
    monkeypatch.setenv("RCB_JUDGE_BASE_URL", "https://judge.invalid")
    monkeypatch.setenv("RCB_JUDGE_MODEL", "openai/gpt-5.1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    state = {"calls": []}

    def fake_executor(argv, *, env, cwd, prompt, timeout_seconds):
        state["calls"].append((list(argv), dict(env)))
        workspace = Path(cwd)
        (workspace / "code").mkdir(exist_ok=True)
        (workspace / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "outputs").mkdir(exist_ok=True)
        (workspace / "outputs" / "summary.json").write_text(
            '{"ok": true}', encoding="utf-8"
        )
        (workspace / "report" / "images").mkdir(parents=True, exist_ok=True)
        (workspace / "report" / "images" / "f.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        (workspace / "report" / "report.md").write_text(
            "# Report\n\n## Methodology\nok\n\n## Results\nok\n\n"
            "## Discussion\nok\n\n![f](images/f.png)\n",
            encoding="utf-8",
        )
        return CodexExecutionResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.1
        )

    port = CodexEngineeringPort(
        model="gpt-5.5",
        profile="~/.codex",
        executor=fake_executor,
        dry_run=True,
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "INSTRUCTIONS.md").write_text("# Task\n", encoding="utf-8")
    receipt = port.candidate(
        {
            "task_id": "Chemistry_004",
            "run_id": "Chemistry_004_a0_baseline",
            "pass_name": "baseline",
            "workspace": str(workspace),
            "evidence_root": str(tmp_path / "evidence"),
        }
    )
    argv, env = state["calls"][0]
    assert argv[:3] == ["codex", "exec", "--model"]
    assert "gpt-5.5" in argv
    assert "--profile" not in argv
    assert "--sandbox" in argv and "workspace-write" in argv
    assert env["CODEX_HOME"] == str(Path("~/.codex").expanduser())
    for secret_key in (
        "DEEPSEEK_API_KEY",
        "RCB_JUDGE_API_KEY",
        "RCB_JUDGE_BASE_URL",
        "RCB_JUDGE_MODEL",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    ):
        assert secret_key not in env
    assert receipt["profile"] == "~/.codex"
    assert receipt["credential_present"] is True
    assert receipt["credential_recorded"] is False
    assert "deepseek_key_present" not in receipt


def test_gpt55_reliability_config_is_frozen() -> None:
    import yaml

    path = (
        Path(__file__).resolve().parents[3]
        / "benchmarks"
        / "researchclaw"
        / "configs"
        / "per_item_minimal_gpt55_reliability_v1.yaml"
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["protocol_name"] == "per_item_minimal_gpt55_reliability_v1"
    assert raw["task_ids"] == ["Chemistry_004", "Information_005"]
    assert raw["candidate"]["provider"] == "native_codex"
    assert raw["candidate"]["model"] == "gpt-5.5"
    assert raw["candidate"]["profile"] == "~/.codex"
    assert raw["execution"]["max_candidate_jobs"] == 2
    assert raw["execution"]["max_evolution_jobs"] == 0
    assert raw["execution"]["max_judge_jobs"] == 0
    assert raw["execution"]["max_retries"] == 0


def test_v2_and_v3_stats_are_not_merged(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = _runner(config, batch_id="canary-stats")
    runner.initialize()
    runner.run_until_terminal()
    summary = json.loads(
        (config.output_root / "canary_batch_result.json").read_text(encoding="utf-8")
    )
    assert "mean_delta" not in summary
    assert "pilot_v2" not in summary.get("schema_version", "")
    assert summary["schema_version"].startswith(
        "openevo.researchclawbench.delivery_canary_v3"
    )


def test_pilot_v3_config_is_prepared_but_locked() -> None:
    import yaml

    path = (
        Path(__file__).resolve().parents[3]
        / "benchmarks"
        / "researchclaw"
        / "configs"
        / "per_item_minimal_deepseek_pilot_v3.yaml"
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["protocol_name"] == "per_item_minimal_deepseek_pilot_v3"
    assert raw["task_ids"] == ["Earth_004", "Material_004", "Physics_004"]
    assert raw["execution"]["dry_run"] is True
    assert "Chemistry_004" not in raw["task_ids"]
    assert "Information_005" not in raw["task_ids"]


def test_real_port_receipt_includes_delivery_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openevo_researchclawbench.deepseek_codex_engineering_port import (
        CodexExecutionResult,
        DeepSeekCodexEngineeringPort,
    )

    def fake_executor(argv, *, env, cwd, prompt, timeout_seconds):
        workspace = Path(cwd)
        (workspace / "code").mkdir(exist_ok=True)
        (workspace / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "outputs").mkdir(exist_ok=True)
        (workspace / "outputs" / "summary.json").write_text(
            json.dumps({"estimated_effect": 0.42, "observations": 128}),
            encoding="utf-8",
        )
        (workspace / "report" / "images").mkdir(parents=True, exist_ok=True)
        (workspace / "report" / "images" / "f.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        (workspace / "report" / "report.md").write_text(
            _long_report(), encoding="utf-8"
        )
        events = "\n".join(
            (
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "file_change",
                            "changes": [
                                {
                                    "path": str(
                                        workspace / "report" / "report.md"
                                    ),
                                    "kind": "add",
                                }
                            ],
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        ) + "\n"
        return CodexExecutionResult(
            returncode=0,
            stdout=events,
            stderr="",
            duration_seconds=0.1,
        )

    port = DeepSeekCodexEngineeringPort(executor=fake_executor, dry_run=True)
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "INSTRUCTIONS.md").write_text("# Task\n", encoding="utf-8")
    evidence_root = tmp_path / "evidence"
    receipt = port.candidate(
        {
            "task_id": "Chemistry_004",
            "run_id": "Chemistry_004_a0_baseline",
            "pass_name": "baseline",
            "workspace": str(workspace),
            "evidence_root": str(evidence_root),
        }
    )
    evidence = receipt["delivery_evidence"]
    assert evidence["workspace_write_event_count"] >= 1
    assert evidence["final_message_present"] is False
    assert evidence["delivery_classification"] == (
        "CANDIDATE_NONDELIVERY_NO_FINAL_MESSAGE"
    )
