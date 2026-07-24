from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import openevo_chembench.taskwise_round0_smoke_v1 as smoke
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.taskwise_context_binding_v1 import (
    TaskwiseSessionContextBindingV1,
    issue_taskwise_context_binding_receipt_v1,
)


def _dataset_loader(config: smoke.Round0SmokeConfigV1) -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(
        snapshot_root=smoke._resolve_workspace_path(config.dataset_root, must_exist=True),
        manifest_path=smoke._resolve_workspace_path(
            config.dataset_manifest,
            must_exist=True,
        ),
    )


def _contains_forbidden_public_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key.casefold()
            in {
                "answer",
                "correct_answer",
                "private_task",
                "source_index",
                "target",
                "target_scores",
            }
            or _contains_forbidden_public_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_public_key(item) for item in value)
    return False


class _FakeExecutor:
    def __init__(self, *, response: str = "A", failure: Exception | None = None) -> None:
        self.response = response
        self.failure = failure
        self.requests = []
        self.receipts = {}
        self.closed = False

    def execute_taskwise(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        binding = TaskwiseSessionContextBindingV1.from_memory(
            session_id=request.session_id,
            memory=request.resolved_text_memory,
        )
        self.receipts[request.session_id] = issue_taskwise_context_binding_receipt_v1(
            expected=binding,
            actual=binding,
        )
        return RawAttempt(
            response=self.response,
            transcript_reference=TranscriptReference(
                reference=f"private-smoke-transcript-{request.session_id}"
            ),
        )

    def consume_taskwise_context_receipt(self, session_id: str):
        return self.receipts.pop(session_id)

    def close(self) -> None:
        self.closed = True


class _ClosedExecutorFailure(RuntimeError):
    taskwise_failure_code = "EXECUTOR_NONZERO_EXIT"
    completion_observed = True


def _run_with_fake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    run_id: str,
    fake: _FakeExecutor,
    executor_policy_gate=None,
) -> tuple[dict[str, object], list[Path]]:
    config = smoke.load_round0_smoke_config()
    static_inputs = smoke._verify_static_inputs(config)
    monkeypatch.setattr(smoke, "_verify_static_inputs", lambda _config: static_inputs)
    resolved_roots = {
        config.output_root: tmp_path / "results",
        config.diagnostic_root: tmp_path / "diagnostics",
    }
    monkeypatch.setattr(
        smoke,
        "_resolve_workspace_path",
        lambda value, *, must_exist: resolved_roots[value],
    )
    diagnostic_paths: list[Path] = []

    def factory(_config, diagnostic_root):
        diagnostic_paths.append(diagnostic_root)
        return fake

    result = smoke.run_round0_smoke(
        run_id=run_id,
        executor_factory=factory,
        source_gate=lambda _config: "a" * 40,
        executor_policy_gate=executor_policy_gate,
    )
    return result, diagnostic_paths


def test_smoke_config_is_one_call_control_only_and_core_free() -> None:
    config = smoke.load_round0_smoke_config()

    assert config.protocol_id == "taskwise_round0_infrastructure_smoke_v1"
    assert config.arm == "control"
    assert config.scope == "smoke1"
    assert config.tasks == config.attempts_per_task == config.rounds == 1
    assert config.evolution_updates == config.core_jobs == config.core_artifacts == 0
    assert config.memory_enabled is False
    assert config.resume_enabled is False
    assert config.executor.infrastructure_retries == 0


def test_one_task_manifest_is_deterministic_and_public_target_free(tmp_path: Path) -> None:
    config = smoke.load_round0_smoke_config()
    loader = _dataset_loader(config)
    first = smoke.generate_round0_smoke_manifests(
        loader,
        public_path=tmp_path / "first" / "public.jsonl",
        private_path=tmp_path / "first" / "private.jsonl",
        summary_path=tmp_path / "first" / "summary.json",
    )
    second = smoke.generate_round0_smoke_manifests(
        loader,
        public_path=tmp_path / "second" / "public.jsonl",
        private_path=tmp_path / "second" / "private.jsonl",
        summary_path=tmp_path / "second" / "summary.json",
    )

    assert first.item_count == second.item_count == 1
    assert first.task_uid == second.task_uid
    assert first.public_path.read_bytes() == second.public_path.read_bytes()
    assert first.private_path.read_bytes() == second.private_path.read_bytes()
    assert first.summary_path.read_bytes() == second.summary_path.read_bytes()
    public_payload = json.loads(first.public_path.read_text(encoding="utf-8"))
    summary_payload = json.loads(first.summary_path.read_text(encoding="utf-8"))
    private_payload = json.loads(first.private_path.read_text(encoding="utf-8"))
    assert not _contains_forbidden_public_key(public_payload)
    assert not _contains_forbidden_public_key(summary_payload)
    assert "private_manifest_sha256" not in summary_payload
    assert "target" in private_payload
    assert first.private_path.stat().st_mode & 0o777 == 0o600


def test_dry_run_verifies_one_task_without_executor_or_core() -> None:
    result = smoke.dry_run_round0_smoke()

    assert result["status"] == "PASS"
    assert result["task_count"] == 1
    assert result["model_calls"] == 0
    assert result["sessions_created"] == 0
    assert result["evolution_updates"] == 0
    assert result["core_jobs_created"] == 0
    assert result["core_artifacts_registered"] == 0
    assert "private_manifest_sha256" not in result
    assert not _contains_forbidden_public_key(result)


def test_smoke_uses_one_fresh_taskwise_round0_session_and_private_evaluator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeExecutor(response="A")
    run_id = "smoke1_fake_success_0001"
    result, diagnostic_paths = _run_with_fake(
        monkeypatch,
        tmp_path,
        run_id=run_id,
        fake=fake,
    )

    assert result["status"] == "COMPLETED"
    assert result["sessions_created"] == result["completion_count"] == 1
    assert result["round_index"] == 0
    assert result["memory_present"] is False
    assert result["evolution_updates"] == 0
    assert result["core_jobs_created"] == 0
    assert result["core_artifacts_registered"] == 0
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.arm == "control"
    assert request.round_index == 0
    assert request.task_ordinal == 0
    assert request.resolved_text_memory is None
    assert request.run_id == run_id
    assert request.task_uid == result["task_uid"]
    assert request.session_id.startswith("smoke_session_")
    assert fake.closed is True
    assert diagnostic_paths == [tmp_path / "diagnostics" / run_id / "control"]

    output = tmp_path / "results" / run_id / "control"
    public_result = json.loads((output / "public" / "result.json").read_text(encoding="utf-8"))
    public_state = json.loads((output / "run_state.json").read_text(encoding="utf-8"))
    private_result = json.loads(
        (output / "private" / "evaluation.json").read_text(encoding="utf-8")
    )
    assert public_state["status"] == "COMPLETED"
    assert public_state["resume_allowed"] is False
    assert "official_prediction" not in public_result
    assert "strict_prediction" not in public_result
    assert "official_accuracy" not in public_result
    assert "correct" not in public_result
    assert not _contains_forbidden_public_key(public_result)
    assert not _contains_forbidden_public_key(public_state)
    assert "target" in private_result
    assert (output / "private" / "evaluation.json").stat().st_mode & 0o777 == 0o600


def test_existing_run_id_is_never_overwritten_or_resumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_fake = _FakeExecutor(response="A")
    run_id = "smoke1_immutable_0001"
    first_result, _paths = _run_with_fake(
        monkeypatch,
        tmp_path,
        run_id=run_id,
        fake=first_fake,
    )
    result_path = tmp_path / "results" / run_id / "control" / "public" / "result.json"
    original_bytes = result_path.read_bytes()

    second_factory_calls = 0

    def forbidden_factory(_config, _diagnostic_root):
        nonlocal second_factory_calls
        second_factory_calls += 1
        raise AssertionError("existing run must block before executor creation")

    with pytest.raises(smoke.Round0SmokeError, match="SMOKE_OUTPUT_TARGET_EXISTS"):
        smoke.run_round0_smoke(
            run_id=run_id,
            executor_factory=forbidden_factory,
            source_gate=lambda _config: "a" * 40,
        )

    assert first_result["status"] == "COMPLETED"
    assert second_factory_calls == 0
    assert result_path.read_bytes() == original_bytes


def test_codex_policy_gate_runs_before_fake_executor_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    fake = _FakeExecutor(response="A")

    def gate(compatibility_config):
        order.append("policy")
        assert compatibility_config.arm == "control"
        assert compatibility_config.executor.tools_enabled is False
        return {"status": "PASS", "model_calls": 0}

    original_execute = fake.execute_taskwise

    def ordered_execute(request):
        order.append("execute")
        return original_execute(request)

    fake.execute_taskwise = ordered_execute
    _run_with_fake(
        monkeypatch,
        tmp_path,
        run_id="smoke1_policy_gate_0001",
        fake=fake,
        executor_policy_gate=gate,
    )

    assert order == ["policy", "execute"]


def test_failed_first_call_is_terminal_and_same_run_id_remains_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failing = _FakeExecutor(failure=RuntimeError("private failure detail"))
    run_id = "smoke1_failed_immutable_0001"
    config = smoke.load_round0_smoke_config()
    static_inputs = smoke._verify_static_inputs(config)
    monkeypatch.setattr(smoke, "_verify_static_inputs", lambda _config: static_inputs)
    resolved_roots = {
        config.output_root: tmp_path / "results",
        config.diagnostic_root: tmp_path / "diagnostics",
    }
    monkeypatch.setattr(
        smoke,
        "_resolve_workspace_path",
        lambda value, *, must_exist: resolved_roots[value],
    )
    factory_calls = 0

    def factory(_config, _diagnostic_root):
        nonlocal factory_calls
        factory_calls += 1
        return failing

    with pytest.raises(smoke.Round0SmokeError, match="EXECUTION_FAILED"):
        smoke.run_round0_smoke(
            run_id=run_id,
            executor_factory=factory,
            source_gate=lambda _config: "a" * 40,
        )

    state_path = tmp_path / "results" / run_id / "control" / "run_state.json"
    original_state = state_path.read_bytes()
    state = json.loads(original_state)
    assert state["status"] == "EXECUTION_FAILED"
    assert state["resume_allowed"] is False
    assert state["failure_code"] == "EXECUTOR_INTERNAL_ERROR"
    assert "private failure detail" not in state_path.read_text(encoding="utf-8")

    with pytest.raises(smoke.Round0SmokeError, match="SMOKE_OUTPUT_TARGET_EXISTS"):
        smoke.run_round0_smoke(
            run_id=run_id,
            executor_factory=factory,
            source_gate=lambda _config: "a" * 40,
        )
    assert factory_calls == 1
    assert state_path.read_bytes() == original_state


def test_closed_taskwise_failure_code_is_preserved_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failing = _FakeExecutor(failure=_ClosedExecutorFailure("sensitive executor detail"))
    run_id = "smoke1_closed_failure_0001"
    config = smoke.load_round0_smoke_config()
    static_inputs = smoke._verify_static_inputs(config)
    monkeypatch.setattr(smoke, "_verify_static_inputs", lambda _config: static_inputs)
    roots = {
        config.output_root: tmp_path / "results",
        config.diagnostic_root: tmp_path / "diagnostics",
    }
    monkeypatch.setattr(
        smoke,
        "_resolve_workspace_path",
        lambda value, *, must_exist: roots[value],
    )

    with pytest.raises(smoke.Round0SmokeError, match="EXECUTION_FAILED"):
        smoke.run_round0_smoke(
            run_id=run_id,
            executor_factory=lambda _config, _diagnostics: failing,
            source_gate=lambda _config: "a" * 40,
        )

    state_path = tmp_path / "results" / run_id / "control" / "run_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["failure_code"] == "EXECUTOR_NONZERO_EXIT"
    assert state["completion_observed"] is True
    assert "sensitive executor detail" not in state_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("failure_point", "expected_code"),
    [
        ("evaluation", "SMOKE_PRIVATE_EVALUATION_FAILED"),
        ("private_persistence", "SMOKE_PRIVATE_PERSISTENCE_FAILED"),
        ("public_persistence", "SMOKE_PUBLIC_PERSISTENCE_FAILED"),
    ],
)
def test_post_completion_failures_are_terminal_closed_and_non_resumable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
    expected_code: str,
) -> None:
    fake = _FakeExecutor(response="A")
    run_id = f"smoke1_post_completion_{failure_point}"
    config = smoke.load_round0_smoke_config()
    static_inputs = smoke._verify_static_inputs(config)
    monkeypatch.setattr(smoke, "_verify_static_inputs", lambda _config: static_inputs)
    roots = {
        config.output_root: tmp_path / "results",
        config.diagnostic_root: tmp_path / "diagnostics",
    }
    monkeypatch.setattr(
        smoke,
        "_resolve_workspace_path",
        lambda value, *, must_exist: roots[value],
    )

    if failure_point == "evaluation":

        class BrokenEvaluator:
            def evaluate(self, **_kwargs):
                raise RuntimeError("private evaluator detail")

        monkeypatch.setattr(smoke, "ChemBench4KPrivateEvaluator", BrokenEvaluator)
    elif failure_point == "private_persistence":
        monkeypatch.setattr(
            smoke,
            "_write_private_json",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private persistence detail")),
        )
    else:
        original_write_public = smoke._write_public_json

        def fail_result_only(path, payload):
            if path.name == "result.json":
                raise OSError("public persistence detail")
            original_write_public(path, payload)

        monkeypatch.setattr(smoke, "_write_public_json", fail_result_only)

    with pytest.raises(smoke.Round0SmokeError, match=expected_code):
        smoke.run_round0_smoke(
            run_id=run_id,
            executor_factory=lambda _config, _diagnostics: fake,
            source_gate=lambda _config: "a" * 40,
        )

    state_path = tmp_path / "results" / run_id / "control" / "run_state.json"
    state_text = state_path.read_text(encoding="utf-8")
    state = json.loads(state_text)
    assert state["status"] == "EXECUTION_FAILED"
    assert state["completion_observed"] is True
    assert state["completion_count"] == 1
    assert state["resume_allowed"] is False
    assert state["failure_code"] == expected_code
    assert "private evaluator detail" not in state_text
    assert "private persistence detail" not in state_text
    assert "public persistence detail" not in state_text

    with pytest.raises(smoke.Round0SmokeError, match="SMOKE_OUTPUT_TARGET_EXISTS"):
        smoke.run_round0_smoke(
            run_id=run_id,
            executor_factory=lambda _config, _diagnostics: pytest.fail(
                "failed smoke cannot be retried"
            ),
            source_gate=lambda _config: "a" * 40,
        )


def test_generated_run_ids_are_valid_unique_and_contain_no_task_identity() -> None:
    first = smoke.generate_round0_smoke_run_id()
    second = smoke.generate_round0_smoke_run_id()

    assert first != second
    assert smoke.validate_run_id(first) == first
    assert smoke.validate_run_id(second) == second
    assert "488409850cd3" not in first


def test_script_is_executable_syntax_valid_and_has_no_resume_or_direct_codex() -> None:
    script = smoke.PACKAGE_ROOT / "scripts" / "run_taskwise_round0_smoke_v1.sh"
    text = script.read_text(encoding="utf-8")

    assert os.access(script, os.X_OK)
    assert "codex exec" not in text
    assert "--resume" not in text
    assert "taskwise_round0_smoke_v1" in text
    assert "run_taskwise_cli" in text


def test_smoke_cli_never_prints_arbitrary_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        smoke,
        "run_round0_smoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private stderr and path /secret")
        ),
    )

    with pytest.raises(SystemExit) as captured:
        smoke.main(["run", "--run-id", "smoke1_closed_cli_0001"])

    streams = capsys.readouterr()
    assert captured.value.code == 2
    assert streams.err == "BLOCKED: SMOKE_INTERNAL_ERROR\n"
    assert "private stderr" not in streams.err
    assert "/secret" not in streams.err
