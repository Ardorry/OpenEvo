from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from openevo_researchclawbench import community_evaluator as evaluator
from openevo_researchclawbench.artifact_validator import (
    candidate_artifact_root_sha256,
)
from openevo_researchclawbench.durable_evaluator_operation import (
    OPENROUTER_API_BASE,
    UNAVAILABLE,
)
from openevo_researchclawbench.hashing import tree_sha256

PROJECT_ROOT = Path("/home/lhy-h/work/researchclaw_openevo")


def test_adapter_pins_official_scorer_dependency_exactly() -> None:
    assert evaluator.CANONICAL_SCORER_SHA256 == (
        "9492317443acc069744330f27aac693d162f1aa6837d512e40419704197c6022"
    )
    assert evaluator.CANONICAL_SCORER_GIT_COMMIT == (
        "b1175eca3deb78ff8b2c839070874474051d2261"
    )
    assert evaluator.CANONICAL_SCORER_RELATIVE_PATH == (
        "ResearchClawBench/evaluation/score.py"
    )
    assert evaluator.OFFICIAL_SCORER_SHA256 == (
        "a1c3370bc26a28b68ae06de48d333717cc6732ed746c1c0af4464791aeab188c"
    )
    pyproject = (
        Path(__file__).resolve().parents[3]
        / "benchmarks"
        / "researchclawbench"
        / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert '"packaging==26.2"' in pyproject
    assert '"structai==0.1.23"' in pyproject


def _workspace(root: Path, *, task_id: str = "Astronomy_004") -> Path:
    run_id = f"{task_id}_a0_v11"
    root.mkdir()
    (root / "_meta.json").write_text(
        json.dumps({"run_id": run_id, "task_id": task_id}), encoding="utf-8"
    )
    (root / "report").mkdir()
    (root / "report" / "report.md").write_text("result", encoding="utf-8")
    return root


def test_evaluator_rejects_attempt_path_escape_before_subprocess(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(evaluator.EvaluatorError, match="attempt identity"):
        evaluator.run_community_evaluator_detailed(
            project_root=PROJECT_ROOT,
            workspace=workspace,
            evaluator_private_root=private,
            task_id="Astronomy_004",
            attempt_id="../../escape",
            expected_model="openai/gpt-5.1",
            expected_api_base=OPENROUTER_API_BASE,
            expected_provider="openai_compatible",
        )


@pytest.mark.parametrize("hidden_name", ("target_study", "secrets", "checklist.json"))
def test_evaluator_rejects_hidden_candidate_content(
    tmp_path: Path, hidden_name: str
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    hidden = workspace / hidden_name
    if hidden_name.endswith(".json"):
        hidden.write_text("{}", encoding="utf-8")
    else:
        hidden.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(evaluator.EvaluatorError, match="hidden content"):
        evaluator.run_community_evaluator_detailed(
            project_root=PROJECT_ROOT,
            workspace=workspace,
            evaluator_private_root=private,
            task_id="Astronomy_004",
            attempt_id="Astronomy_004_a0_v11",
            expected_model="openai/gpt-5.1",
            expected_api_base=OPENROUTER_API_BASE,
            expected_provider="openai_compatible",
        )


def test_evaluator_never_follows_candidate_symlinks(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    outside = tmp_path / "outside-secret"
    outside.write_text("not evaluator input", encoding="utf-8")
    (workspace / "report" / "escape").symlink_to(outside)
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(evaluator.EvaluatorError, match="unsafe filesystem"):
        evaluator.run_community_evaluator_detailed(
            project_root=PROJECT_ROOT,
            workspace=workspace,
            evaluator_private_root=private,
            task_id="Astronomy_004",
            attempt_id="Astronomy_004_a0_v11",
            expected_model="openai/gpt-5.1",
            expected_api_base=OPENROUTER_API_BASE,
            expected_provider="openai_compatible",
        )


def test_validator_artifact_hash_is_rechecked_before_judge(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(evaluator.EvaluatorError, match="artifact authority hash"):
        evaluator.run_community_evaluator_detailed(
            project_root=PROJECT_ROOT,
            workspace=workspace,
            evaluator_private_root=private,
            task_id="Astronomy_004",
            attempt_id="Astronomy_004_a0_v11",
            expected_model="openai/gpt-5.1",
            expected_api_base=OPENROUTER_API_BASE,
            expected_provider="openai_compatible",
            expected_artifact_root_sha256="0" * 64,
        )


def test_worker_attests_exact_scorer_origin_tasks_dir_and_workspace_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    structai = types.ModuleType("structai")
    structai.LLMAgent = object
    structai.multi_thread = lambda *_args, **_kwargs: []
    monkeypatch.setitem(sys.modules, "structai", structai)
    scorer = evaluator._resolve_scorer_authority(
        PROJECT_ROOT,
        workspace,
        "Astronomy_004",
        "Astronomy_004_a0_v11",
    )
    assert scorer.__module__ == "ResearchClawBench.evaluation.score"

    from ResearchClawBench.evaluation import config as scorer_config

    monkeypatch.setattr(scorer_config, "TASKS_DIR", tmp_path / "untrusted-tasks")
    with pytest.raises(evaluator.EvaluatorError, match="TASKS_DIR authority"):
        evaluator._resolve_scorer_authority(
            PROJECT_ROOT,
            workspace,
            "Astronomy_004",
            "Astronomy_004_a0_v11",
        )


def test_scorer_preflight_rejects_structai_version_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evaluator, "version", lambda _package: "0.1.24")
    with pytest.raises(evaluator.EvaluatorError, match="dependency version differs"):
        evaluator._scorer_installation_authority(PROJECT_ROOT)


def test_evaluator_child_has_closed_environment_and_private_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    candidate_artifact_sha256 = candidate_artifact_root_sha256(workspace)
    (workspace / "validator.json").write_text(
        json.dumps({"artifact_valid": True}), encoding="utf-8"
    )
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv("JUDGE_API_KEY", "synthetic-secret-not-written")
    monkeypatch.setenv("JUDGE_API_BASE", OPENROUTER_API_BASE)
    monkeypatch.setenv("JUDGE_MODEL_NAME", "openai/gpt-5.1")
    monkeypatch.setenv("OPENEVO_CORE_CONTROL_BEARER", "must-not-inherit")
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        raw = Path(command[command.index("--raw-output") + 1])
        metadata = Path(command[command.index("--execution-metadata") + 1])
        raw.write_text(
            json.dumps(
                {
                    "run_id": "Astronomy_004_a0_v11",
                    "task_id": "Astronomy_004",
                    "agent_name": "Unknown",
                    "items": [
                        {
                            "index": 0,
                            "type": "text",
                            "content": "criterion",
                            "weight": 1.0,
                            "score": 25.0,
                            "reasoning": "addresses the criterion",
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
                        }
                    ],
                    "total_score": 25.0,
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
            ),
            encoding="utf-8",
        )
        metadata.write_text(
            json.dumps(
                {
                    "api_base": OPENROUTER_API_BASE,
                    "api_key_present": True,
                    "cost_total_usd": UNAVAILABLE,
                    "model": "openai/gpt-5.1",
                    "provider": "openai_compatible",
                    "request_count": UNAVAILABLE,
                    "requested_provider": "azure",
                    "returned_provider": "Azure",
                    "scorer_source_path": "ResearchClawBench/evaluation/score.py",
                    "scorer_git_commit": (
                        "b1175eca3deb78ff8b2c839070874474051d2261"
                    ),
                    "scorer_sha256": (
                        "9492317443acc069744330f27aac693d162f1aa6837d512e40419704197c6022"
                    ),
                    "secret_recorded": False,
                    "usage": UNAVAILABLE,
                }
            ),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(evaluator.subprocess, "run", fake_run)
    result = evaluator.run_community_evaluator_detailed(
        project_root=PROJECT_ROOT,
        workspace=workspace,
        evaluator_private_root=private,
        task_id="Astronomy_004",
        attempt_id="Astronomy_004_a0_v11",
        expected_model="openai/gpt-5.1",
        expected_api_base=OPENROUTER_API_BASE,
        expected_provider="openai_compatible",
        expected_artifact_root_sha256=candidate_artifact_sha256,
    )
    assert result.judge_outcome.raw_score["total_score"] == 25.0
    assert result.score_valid is True
    assert result.judge_completed is True
    assert result.failure_category is None
    child_env = observed["env"]
    assert isinstance(child_env, dict)
    required_child = {
        "PATH",
        "PYTHONUNBUFFERED",
        "PYTHONPATH",
        "RCB_JUDGE_API_KEY",
        "RCB_JUDGE_BASE_URL",
        "RCB_JUDGE_MODEL",
    }
    assert required_child.issubset(child_env)
    allowed_child = required_child | {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    }
    assert set(child_env) <= allowed_child
    assert "OPENEVO_CORE_CONTROL_BEARER" not in child_env
    command = observed["command"]
    assert isinstance(command, list)
    assert command[command.index("--expected-task-id") + 1] == "Astronomy_004"
    assert command[command.index("--expected-attempt-id") + 1] == "Astronomy_004_a0_v11"
    assert (private.stat().st_mode & 0o777) == 0o700
    assert "synthetic-secret-not-written" not in json.dumps(command)
    assert "synthetic-secret-not-written" not in json.dumps(result.raw_score)


def test_closed_evaluator_child_can_import_source_loaded_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "synthetic-secret-not-written")
    monkeypatch.setenv("JUDGE_API_BASE", OPENROUTER_API_BASE)
    monkeypatch.setenv("JUDGE_MODEL_NAME", "openai/gpt-5.1")
    child_env = evaluator._closed_evaluator_subprocess_environment(
        project_root=PROJECT_ROOT,
        judge_env_file=None,
    )
    python_paths = tuple(
        Path(item).resolve(strict=True)
        for item in child_env["PYTHONPATH"].split(os.pathsep)
    )
    assert Path(evaluator.__file__).resolve(strict=True).parents[1] in python_paths
    assert PROJECT_ROOT.resolve(strict=True) in python_paths

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "openevo_researchclawbench.community_evaluator",
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=child_env,
        timeout=30,
    )
    assert completed.returncode == 0
    assert completed.stderr == ""


def test_worker_rejects_symlinked_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    real = secret_root / "real.env"
    real.write_text(
        "JUDGE_API_KEY=x\nJUDGE_API_BASE=https://example.invalid\nJUDGE_MODEL_NAME=m\n",
        encoding="utf-8",
    )
    real.chmod(0o600)
    link = secret_root / "judge.env"
    link.symlink_to(real)
    monkeypatch.setenv("OPENEVO_JUDGE_ENV_FILE", os.fspath(link))
    with pytest.raises(evaluator.EvaluatorError, match="unsafe"):
        evaluator._load_judge_environment()
