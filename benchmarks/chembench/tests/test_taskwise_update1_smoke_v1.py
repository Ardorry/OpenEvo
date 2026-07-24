from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from openevo import __version__
from openevo.evolution.framework import DistributionArtifactExpectation
from openevo.evolution.framework import builtins as core_builtins
from openevo.evolution.framework.builtins import load_verified_builtin_registry
from openevo.evolution.framework.loading import _verify_distribution_install
import openevo.evolution.methods as core_methods
import openevo_chembench.taskwise_update1_smoke_v1 as smoke
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.taskwise_config_v1 import TASKWISE_MEMORY_LIMITS_V1
from openevo_chembench.taskwise_context_binding_v1 import (
    TaskwiseSessionContextBindingV1,
    issue_taskwise_context_binding_receipt_v1,
)
from openevo_chembench.taskwise_core_evolution_v1 import (
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreEvolutionError,
    TaskwiseCoreUpdatePortAdapterV1,
    build_taskwise_core_port_at_roots_v1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    CoreMemoryReferenceV1,
    TaskwiseCoreUpdateOutcomeV1,
    TaskwiseMemoryPublicMetricsV1,
)
from openevo_chembench.taskwise_trajectory_v1 import (
    ordered_safe_feedback_digest,
    ordered_taskwise_trajectory_digest,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _SourceDistribution:
    metadata = {"Name": "openevo"}
    version = __version__

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, _filename: str) -> None:
        return None


def _verified_registry(temp_root: Path):
    temp_root.mkdir(parents=True, exist_ok=True)
    install_root = Path(core_builtins.__file__).resolve().parents[3]
    artifact = temp_root / f"openevo-{__version__}-py3-none-any.whl"
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith(
                (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")
            ):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            f"openevo-{__version__}.dist-info/METADATA",
            f"Name: openevo\nVersion: {__version__}\n",
        )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    verified = _verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=__version__,
            distribution_digest=digest,
        ),
        artifact,
        metadata_provider=lambda _name: _SourceDistribution(install_root),
    )
    return load_verified_builtin_registry(verified)


def _memory() -> str:
    return """# General Chemistry Memory

## Do
- Classify the reasoning mode before comparing choices.

## Avoid
- Avoid committing before an independent consistency check.

## Validate
- Verify units, conservation, structures, and output formatting.

## When Applicable
- Apply dimensional checks to numerical reasoning.

## Retired Or Superseded
- Retire advice only when a general validation rule supersedes it.
"""


def _issued_memory(label: str = "one") -> CoreResolvedTextMemoryV2:
    markdown = _memory()
    return _issue_core_resolved_text_memory_v2(
        core_artifact_id=f"artifact_{label}",
        artifact_payload_sha256=_sha(f"payload-{label}"),
        context_resolution_digest=_sha(f"context-{label}"),
        resolved_memory_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        markdown=markdown,
    )


def _metrics() -> TaskwiseMemoryPublicMetricsV1:
    encoded = _memory().encode()
    return TaskwiseMemoryPublicMetricsV1(
        memory_limits_sha256=TASKWISE_MEMORY_LIMITS_V1.digest,
        inspection_sha256=_sha("inspection"),
        token_estimator_id=TASKWISE_MEMORY_LIMITS_V1.token_estimator_id,
        parser_id=TASKWISE_MEMORY_LIMITS_V1.parser_id,
        utf8_byte_count=len(encoded),
        estimated_token_count=64,
        section_item_counts=(1, 1, 1, 1, 1),
        total_section_items=5,
        max_section_items=1,
    )


class _FakeExecutor:
    def __init__(self, response: str = "A") -> None:
        self.response = response
        self.requests = []
        self.receipts = {}
        self.closed = False

    def execute_taskwise(self, request):
        if self.requests:
            raise AssertionError("bounded smoke attempted a second task session")
        self.requests.append(request)
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
                f"private-update1-smoke-{request.session_id}"
            ),
        )

    def consume_taskwise_context_receipt(self, session_id: str):
        return self.receipts.pop(session_id)

    def close(self) -> None:
        self.closed = True


class _FakeCore:
    def __init__(
        self,
        *,
        update_error: Exception | None = None,
        resolve_mismatch: bool = False,
    ) -> None:
        self.update_error = update_error
        self.resolve_mismatch = resolve_mismatch
        self.requests = []
        self.references: list[CoreMemoryReferenceV1] = []
        self.memory = _issued_memory()
        self.closed = False

    def update_text_memory(self, request):
        self.requests.append(request)
        if self.update_error is not None:
            raise self.update_error
        return TaskwiseCoreUpdateOutcomeV1(
            task_uid=request.task_uid,
            task_index=request.task_index,
            update_index=request.update_index,
            global_update_ordinal=request.task_index * 2 + request.update_index,
            predecessor_artifact_id=(
                None
                if request.prior_resolved_text_memory is None
                else request.prior_resolved_text_memory.core_artifact_id
            ),
            predecessor_memory_sha256=(
                None
                if request.prior_resolved_text_memory is None
                else request.prior_resolved_text_memory.resolved_memory_sha256
            ),
            trajectory_ids=tuple(item.trajectory_id for item in request.trajectories),
            trajectory_digest=ordered_taskwise_trajectory_digest(request.trajectories),
            safe_feedback_digest=ordered_safe_feedback_digest(request.trajectories),
            core_artifact_id=self.memory.core_artifact_id,
            job_id="job_update1_smoke_0001",
            job_state="COMPLETED",
            core_context_id="context_update1_smoke_0001",
            validation_receipt_sha256=_sha("validation"),
            memory_metrics=_metrics(),
            resolved_text_memory=self.memory,
        )

    def resolve_text_memory(self, reference):
        self.references.append(reference)
        if self.resolve_mismatch:
            return _issued_memory("mismatch")
        return self.memory

    def close(self) -> None:
        self.closed = True


class _ReflectorSecurityFailure(RuntimeError):
    finding_code = "TASKWISE_REFLECTOR_SECURITY_TOOL_USE_VIOLATION"


def _contains_forbidden_public_key(value: object) -> bool:
    forbidden = smoke._PUBLIC_FORBIDDEN_KEYS
    if isinstance(value, dict):
        return any(
            key.casefold().replace("-", "_") in forbidden or _contains_forbidden_public_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_public_key(item) for item in value)
    return False


def _run_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    run_id: str,
    executor: _FakeExecutor,
    core: object,
) -> tuple[dict[str, object], Path]:
    config = smoke.load_update1_smoke_config()
    inputs = smoke._verify_static_inputs(config)
    monkeypatch.setattr(smoke, "_verify_static_inputs", lambda _config: inputs)
    original_resolve = smoke._resolve_workspace_path
    roots = {
        config.output_root: tmp_path / "results",
        config.state_root: tmp_path / "state",
        config.diagnostic_root: tmp_path / "diagnostics",
    }

    def resolve(value: str, *, must_exist: bool):
        if value in roots:
            return roots[value]
        return original_resolve(value, must_exist=must_exist)

    monkeypatch.setattr(smoke, "_resolve_workspace_path", resolve)
    selected_core_factory = (
        core
        if callable(core) and not callable(getattr(core, "update_text_memory", None))
        else (lambda _config, _state: core)
    )
    result = smoke.run_update1_smoke(
        run_id=run_id,
        executor_factory=lambda _config, _diagnostics: executor,
        core_port_factory=selected_core_factory,
        source_gate=lambda _config: "a" * 40,
        executor_policy_gate=lambda _config: {"status": "PASS", "model_calls": 0},
    )
    return result, tmp_path / "results" / run_id / "online"


def test_config_freezes_one_round_zero_and_one_update_before_round_one() -> None:
    config = smoke.load_update1_smoke_config()

    assert config.protocol_id == "taskwise_update1_infrastructure_smoke_v1"
    assert config.arm == "online"
    assert config.tasks == config.task_sessions == config.rounds_completed == 1
    assert config.evolution_updates == config.core_jobs == config.core_artifacts == 1
    assert config.core_context_resolutions == 1
    assert config.round_1_sessions == 0
    assert config.memory_injected_into_task_session is False
    assert config.resume_enabled is False
    assert config.executor.infrastructure_retries == 0


def test_dry_run_constructs_no_executor_or_core_and_calls_no_model() -> None:
    result = smoke.dry_run_update1_smoke()

    assert result["status"] == "PASS"
    assert result["model_calls"] == 0
    assert result["executor_instantiated"] is False
    assert result["core_port_instantiated"] is False
    assert result["sessions_created"] == 0
    assert result["evolution_updates"] == 0
    assert result["round_1_session_created"] is False
    assert not _contains_forbidden_public_key(result)


def test_success_executes_exact_round0_update_and_resolve_then_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _FakeExecutor(response="A")
    core = _FakeCore()
    result, output = _run_with_fakes(
        monkeypatch,
        tmp_path,
        run_id="update1_smoke_success_0001",
        executor=executor,
        core=core,
    )

    assert result["status"] == "COMPLETED_BEFORE_ROUND_1"
    assert result["sessions_created"] == result["completion_count"] == 1
    assert result["evolution_updates"] == 1
    assert result["core_jobs_created"] == 1
    assert result["core_artifacts_registered"] == 1
    assert result["core_context_resolutions"] == 1
    assert result["context_reference_revalidation_count"] == 1
    assert result["round_1_session_created"] is False
    assert result["memory_injected_into_task_session"] is False
    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.arm == "online"
    assert request.round_index == 0
    assert request.resolved_text_memory is None
    assert executor.closed is True
    assert core.closed is True
    assert len(core.requests) == 1
    update = core.requests[0]
    assert update.update_index == 1
    assert update.source_round_index == 0
    assert len(update.trajectories) == len(update.safe_signals) == 1
    assert update.trajectories[0].round_index == 0
    assert update.safe_signals == (update.trajectories[0].safe_feedback,)
    assert update.prior_resolved_text_memory is None
    assert len(core.references) == 1
    assert core.references[0] == CoreMemoryReferenceV1.from_memory(core.memory)

    state = json.loads((output / "run_state.json").read_text())
    public = json.loads((output / "public" / "result.json").read_text())
    private = json.loads((output / "private" / "round_0_evaluation.json").read_text())
    assert state["state"] == state["status"] == "COMPLETED_BEFORE_ROUND_1"
    assert state["round_1_session_created"] is False
    assert not _contains_forbidden_public_key(state)
    assert not _contains_forbidden_public_key(public)
    assert _memory() not in json.dumps(state)
    assert _memory() not in json.dumps(public)
    assert "target" in private
    assert "raw_completion" in private
    assert private["safe_feedback_digest"] == update.safe_signals[0].digest
    assert (output / "private" / "round_0_evaluation.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("core", "expected_status"),
    [
        (
            _FakeCore(update_error=RuntimeError("private core detail")),
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
        ),
        (_FakeCore(update_error=_ReflectorSecurityFailure()), "SECURITY_TOOL_USE_VIOLATION"),
        (_FakeCore(resolve_mismatch=True), "TASKWISE_EVOLUTION_UPDATE_FAILED"),
    ],
)
def test_core_failures_are_terminal_without_round_one_retry_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    core: _FakeCore,
    expected_status: str,
) -> None:
    executor = _FakeExecutor(response="B")
    run_id = f"update1_smoke_failure_{expected_status.casefold()}"
    with pytest.raises(smoke.Update1SmokeError, match=expected_status):
        _run_with_fakes(
            monkeypatch,
            tmp_path,
            run_id=run_id,
            executor=executor,
            core=core,
        )

    output = tmp_path / "results" / run_id / "online"
    state_text = (output / "run_state.json").read_text()
    state = json.loads(state_text)
    assert state["status"] == expected_status
    assert state["completion_observed"] is True
    assert state["round_1_session_created"] is False
    assert state["retry_allowed"] is False
    assert state["resume_allowed"] is False
    assert state["replacement_completion_allowed"] is False
    assert len(executor.requests) == 1
    assert executor.closed is True
    assert core.closed is True
    assert "private core detail" not in state_text
    assert (output / "private" / "failure.json").stat().st_mode & 0o777 == 0o600

    with pytest.raises(smoke.Update1SmokeError, match="UPDATE1_SMOKE_OUTPUT_TARGET_EXISTS"):
        smoke.run_update1_smoke(
            run_id=run_id,
            executor_factory=lambda *_args: pytest.fail("failed smoke must not retry"),
            core_port_factory=lambda *_args: pytest.fail("failed update must not replay"),
            source_gate=lambda _config: "a" * 40,
            executor_policy_gate=lambda _config: {"status": "PASS"},
        )


def test_real_core_synthetic_worker_registers_and_resolves_one_typed_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _FakeExecutor(response="A")
    core_root_holder: list[Path] = []
    registry = _verified_registry(tmp_path / "verified-registry")
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(),
    )

    def core_factory(_config, core_root):
        core_root_holder.append(core_root)
        bridge = TaskwiseCoreEvolutionBridgeV1(
            db_path=core_root / "evolution.sqlite3",
            artifact_root=core_root / "artifacts",
            executable_registry=registry,
            checkpoint_path=core_root / "private_lineage_checkpoints.jsonl",
        )
        return TaskwiseCoreUpdatePortAdapterV1(
            bridge,
            test_only_allow_synthetic_reflector=True,
        )

    result, _output = _run_with_fakes(
        monkeypatch,
        tmp_path,
        run_id="update1_smoke_real_core_0001",
        executor=executor,
        core=core_factory,
    )

    assert result["status"] == "COMPLETED_BEFORE_ROUND_1"
    assert result["core_jobs_created"] == result["core_artifacts_registered"] == 1
    assert result["core_context_resolutions"] == 1
    assert len(core_root_holder) == 1
    database = core_root_holder[0] / "evolution.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM artifacts "
                "WHERE type = 'text_memory' AND state = 'active' AND promoted = 1"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0] == 1


def test_explicit_root_builder_rejects_missing_framework_lock_before_state_write(
    tmp_path: Path,
) -> None:
    state_root = (tmp_path / "state").resolve()
    missing = (tmp_path / "missing-framework-lock.json").resolve()

    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_VERIFIED_FRAMEWORK_LOCK_MISSING",
    ):
        build_taskwise_core_port_at_roots_v1(
            state_root=state_root,
            framework_lock=missing,
            timeout_seconds=600,
        )
    assert not state_root.exists()


def test_script_is_executable_has_no_resume_and_never_calls_codex_directly() -> None:
    script = smoke.PACKAGE_ROOT / "scripts" / "run_taskwise_update1_smoke_v1.sh"
    text = script.read_text()

    assert os.access(script, os.X_OK)
    assert "codex exec" not in text
    assert "--resume" not in text
    assert "taskwise_update1_smoke_v1" in text
    assert "run_taskwise_cli" in text
