from __future__ import annotations

import json
from pathlib import Path

import pytest
from openevo_researchclawbench.config import FROZEN_TASKS, ExperimentConfig, ProtocolError
from openevo_researchclawbench.hashing import UnsafePathError
from openevo_researchclawbench.prompt_composer import compose_native_instruction
from openevo_researchclawbench.task_loader import load_public_task
from openevo_researchclawbench.workspace import build_official_workspace

PROJECT_ROOT = Path(__file__).resolve().parents[4]
PROTOCOL = PROJECT_ROOT / "experiments/sequential_task_reflector_evolution_v0/protocol/protocol.yaml"


def _fake_task(root: Path, task_id: str = "Life_005") -> Path:
    task = root / "tasks" / task_id
    (task / "data").mkdir(parents=True)
    (task / "related_work").mkdir()
    (task / "data" / "public.csv").write_text("x\n1\n", encoding="utf-8")
    (task / "related_work" / "paper.pdf").write_bytes(b"public")
    (task / "task_info.json").write_text(
        json.dumps({"task": "public research question", "data": [{"name": "public", "path": "./data/public.csv"}]}),
        encoding="utf-8",
    )
    return task


def test_protocol_parser_is_frozen_and_has_closed_budget_configuration() -> None:
    config = ExperimentConfig.load(PROTOCOL)
    assert tuple(config.require("tasks")) == FROZEN_TASKS
    assert config.require("candidate.codex_cli_version") == "0.144.1"
    assert config.unresolved_fields() == []
    assert config.require("candidate.max_tool_steps") == 300
    assert config.require("budgets.max_reflector_model_calls") == 170
    readiness = config.environment_readiness({})
    assert readiness["ready"] is False
    assert readiness["reflector_runtime_receipt_valid"] is True
    assert "REFLECTOR_CODEX_RUNTIME_NOT_REPRODUCIBLY_PINNED" not in readiness["blockers"]
    assert "NATIVE_POST_RUN_TRAINING_FEEDBACK_ATTACHMENT_UNAVAILABLE" not in readiness["blockers"]
    assert "PRODUCTION_TRAINING_FEEDBACK_TRANSPORT_UNAVAILABLE" not in readiness["blockers"]
    assert "FORMAL_TRAINING_ORCHESTRATOR_UNAVAILABLE" not in readiness["blockers"]
    assert readiness["formal_training_orchestrator_status"] == "READY"
    assert readiness["framework_lock_present"] is True
    assert "JUDGE_CREDENTIALS_MISSING" in readiness["blockers"]


def test_config_rejects_judge_environment_in_candidate() -> None:
    config = ExperimentConfig.load(PROTOCOL)
    config.raw["candidate"]["codex_binary"] = "/usr/bin/codex"
    with pytest.raises(ProtocolError, match="managed Codex"):
        config.validate_static()


def test_task_loader_rejects_data_path_escape(tmp_path: Path) -> None:
    task = _fake_task(tmp_path)
    (task / "task_info.json").write_text(
        json.dumps({"task": "x", "data": [{"path": "../target_study/paper.pdf"}]}),
        encoding="utf-8",
    )
    with pytest.raises(UnsafePathError):
        load_public_task(tmp_path, "Life_005")


def test_task_loader_accepts_a_safe_declared_data_directory(tmp_path: Path) -> None:
    task = _fake_task(tmp_path)
    source = task / "data" / "source"
    (source / "nested").mkdir(parents=True)
    (source / "alpha.txt").write_text("alpha", encoding="utf-8")
    (source / "nested" / "beta.txt").write_text("beta", encoding="utf-8")
    (task / "task_info.json").write_text(
        json.dumps({"task": "x", "data": [{"path": "./data/source"}]}),
        encoding="utf-8",
    )

    loaded = load_public_task(tmp_path, "Life_005")

    assert tuple(
        path.relative_to(task).as_posix()
        for path in loaded.declared_data_files
    ) == ("data/source/alpha.txt", "data/source/nested/beta.txt")


def test_task_loader_rejects_a_symlink_in_declared_data_directory(
    tmp_path: Path,
) -> None:
    task = _fake_task(tmp_path)
    source = task / "data" / "source"
    source.mkdir()
    (source / "public.txt").write_text("public", encoding="utf-8")
    (source / "alias.txt").symlink_to(source / "public.txt")
    (task / "task_info.json").write_text(
        json.dumps({"task": "x", "data": [{"path": "./data/source"}]}),
        encoding="utf-8",
    )

    with pytest.raises(UnsafePathError):
        load_public_task(tmp_path, "Life_005")


def test_task_loader_requires_an_explicit_exact_official_allowlist(
    tmp_path: Path,
) -> None:
    _fake_task(tmp_path, "Astronomy_001")
    with pytest.raises(ValueError, match="exact public-task allowlist"):
        load_public_task(tmp_path, "Astronomy_001")
    official = tuple(f"Astronomy_{index:03d}" for index in range(1, 41))
    loaded = load_public_task(
        tmp_path,
        "Astronomy_001",
        allowed_task_ids=official,
    )
    assert loaded.task_id == "Astronomy_001"


def test_workspace_builder_calls_setup_only_and_rejects_hidden_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_task(tmp_path)
    called = {"setup": 0, "run": 0}

    class FakeRunner:
        def __init__(self, task_id, agent_cmd, agent_name):
            self.task_id = task_id
            self.agent_cmd = agent_cmd
            self.agent_name = agent_name

        def setup_workspace(self):
            called["setup"] += 1
            self.workspace.mkdir(parents=True)
            for name in ("data", "related_work", "code", "outputs", "report", "report/images"):
                (self.workspace / name).mkdir(parents=True, exist_ok=True)
            (self.workspace / "INSTRUCTIONS.md").write_text("official", encoding="utf-8")
            (self.workspace / "_meta.json").write_text(
                json.dumps({"task_id": self.task_id, "run_id": self.run_id}), encoding="utf-8"
            )

        def run(self):
            called["run"] += 1

    monkeypatch.setattr("openevo_researchclawbench.workspace._load_official_task_runner", lambda root: FakeRunner)
    receipt = build_official_workspace(tmp_path, "Life_005", tmp_path / "runs", "Life_005_a0_test")
    assert receipt.instructions.read_text(encoding="utf-8") == "official"
    assert called == {"setup": 1, "run": 0}
    assert not (receipt.workspace / "target_study").exists()
    assert set(path.name for path in receipt.candidate_workspace.iterdir()) == {
        "INSTRUCTIONS.md", "data", "related_work", "code", "outputs", "report", "artifacts", "skills"
    }
    assert not (receipt.candidate_workspace / "_meta.json").exists()


def test_native_instruction_preserves_official_text_and_local_overlay(tmp_path: Path) -> None:
    instructions = tmp_path / "INSTRUCTIONS.md"
    instructions.write_text("OFFICIAL TASK", encoding="utf-8")
    prompt = compose_native_instruction(instructions, task_local_overlay="CURRENT TASK CORRECTION")
    assert prompt.sections == (
        "official_instructions",
        "runtime_capture_safety",
        "workspace_publication_safety",
        "deliverable_validation_contract",
        "task_local_overlay",
    )
    assert prompt.text.startswith("OFFICIAL TASK")
    assert (
        prompt.text.index("OFFICIAL TASK")
        < prompt.text.index("Runtime capture safety")
        < prompt.text.index("Workspace publication safety")
        < prompt.text.index("Deliverable validation contract")
        < prompt.text.index("CURRENT TASK CORRECTION")
    )
    assert "printable UTF-8 text" in prompt.text
    assert "never use Latin-1 as a binary-to-text fallback" in prompt.text
    assert "Agent System" not in prompt.text


def test_native_instruction_always_binds_binary_safe_terminal_output(tmp_path: Path) -> None:
    instructions = tmp_path / "INSTRUCTIONS.md"
    instructions.write_text("OFFICIAL TASK\n", encoding="utf-8")

    prompt = compose_native_instruction(instructions)

    assert prompt.sections == (
        "official_instructions",
        "runtime_capture_safety",
        "workspace_publication_safety",
        "deliverable_validation_contract",
    )
    assert prompt.text.startswith("OFFICIAL TASK\n\n---\n\n## Runtime capture safety")
    assert "Never print,\ndump, or decode raw binary file bytes" in prompt.text
    assert "including PDF bytes" in prompt.text
    assert "scientific task" in prompt.text
    assert "## Workspace publication safety" in prompt.text
    assert "outside the current workspace" in prompt.text
    assert "no symbolic links, sockets, FIFOs, or device files" in prompt.text
    assert "all three semantic groups" in prompt.text
    assert "**Method**" in prompt.text
    assert "**Results**" in prompt.text
    assert "**Discussion**" in prompt.text
    assert "relative path" in prompt.text
    assert "traceable to an output file" in prompt.text
    assert ".venv" in prompt.text
