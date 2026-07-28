from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from openevo.evolution import methods
from openevo.evolution.framework.execution import (
    HarnessInferenceRequest,
    HarnessInferenceResponse,
    ManagedReflectorRuntimeConfig,
    MethodExecutionContext,
    MethodExecutionServices,
    ReflectorInferenceRequest,
    ReflectorInferenceResponse,
    ReflectorRuntimeReceipt,
    build_execution_envelope,
    invoke_legacy_method,
    require_active_reflector_service,
)
from openevo.evolution.managed_reflector import (
    ManagedCodexReflectorService,
    _managed_reflector_exec_command,
    default_managed_reflector_runtime,
)
from openevo.evolution.models import ArtifactRegisterRequest, WorkerClaimedJob


class _Harness:
    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse:
        return HarnessInferenceResponse(
            request_id=request.request_id,
            text="unused",
            capture_mode="transcript",
        )


class _Reflector:
    def __init__(self) -> None:
        self.requests: list[ReflectorInferenceRequest] = []

    def infer(self, request: ReflectorInferenceRequest) -> ReflectorInferenceResponse:
        self.requests.append(request)
        return ReflectorInferenceResponse(
            request_id=request.request_id,
            text="# Managed result",
            receipt=ReflectorRuntimeReceipt(
                request_id=request.request_id,
                session_id="reflector-session-1",
                runtime_profile="managed_science",
                runtime_digest=request.runtime.image_digest.removeprefix("sha256:"),
                codex_binary="/opt/codex/bin/codex",
                actual_cli_version="0.144.1",
                model_name=request.model_name,
                reasoning_effort=request.reasoning_effort,
                auth_mode="subscription",
                capture_mode="transcript",
                path_fallback_allowed=False,
                exit_status=0,
                transcript_sha256="b" * 64,
            ),
        )


def test_default_managed_reflector_is_exact_and_has_no_path_fallback() -> None:
    runtime = default_managed_reflector_runtime()
    assert runtime.mode == "managed"
    assert runtime.profile == "managed_science"
    assert runtime.codex_binary == "/opt/codex/bin/codex"
    assert runtime.expected_cli_version == "0.144.1"
    assert runtime.path_fallback_allowed is False
    assert runtime.image_digest == (
        "sha256:af67c6b8c9cb0debd3a29addc23f518a680369ad53ec5347a829ef7318529c5c"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_digest", "sha256:" + "0" * 64),
        ("codex_binary", "/usr/bin/codex"),
        ("expected_cli_version", "0.145.0"),
        ("path_fallback_allowed", True),
        ("auth_mode", "api_key"),
        ("capture_mode", "none"),
    ],
)
def test_managed_reflector_identity_drift_is_rejected(field: str, value: object) -> None:
    payload = default_managed_reflector_runtime().model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValueError, match="Core release"):
        ManagedReflectorRuntimeConfig.model_validate(payload)


def test_codex_cli_requires_explicit_runtime_and_absolute_legacy_binary() -> None:
    base = WorkerClaimedJob(
        job_id="job-1",
        lease_id="lease-1",
        job_type="reflect",
        method="text_memory_reflector",
        config={"reflector_llm": {"provider": "codex_cli", "model": "gpt-5.5"}},
    )
    with pytest.raises(ValueError, match="explicit runtime"):
        methods._reflector_llm_config(base)
    payload = base.model_dump(mode="python")
    payload["config"]["reflector_llm"].update(
        {
            "runtime": {"mode": "legacy_path", "path_fallback_allowed": True},
            "codex_bin": "codex",
        }
    )
    with pytest.raises(ValueError, match="absolute codex_bin"):
        methods._reflector_llm_config(WorkerClaimedJob.model_validate(payload))


def test_legacy_method_receives_core_reflector_service_only_during_invocation(
    tmp_path: Path,
) -> None:
    runtime = default_managed_reflector_runtime()
    envelope = build_execution_envelope(
        plan_id="plan-1",
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="agent_system",
        method_id="agent_system_reflector",
        method_identity_digest="c" * 64,
        user_config={},
        core_config={},
        input_bindings=(),
        output_artifact_types=("agent_system",),
    )
    job = WorkerClaimedJob(
        job_id="job-1",
        lease_id="lease-1",
        job_type="reflect",
        method="agent_system_reflector",
        config={},
    )
    reflector = _Reflector()
    context = MethodExecutionContext(
        job=job,
        artifact_root=tmp_path,
        envelope=envelope,
        services=MethodExecutionServices(harness=_Harness(), reflector=reflector),
    )

    def legacy(
        projected: WorkerClaimedJob, artifact_root: Path
    ) -> list[ArtifactRegisterRequest]:
        del projected, artifact_root
        response = require_active_reflector_service().infer(
            ReflectorInferenceRequest(
                request_id="request-1",
                prompt="reflect synthetic trajectory",
                model_name="gpt-5.5",
                reasoning_effort="high",
                timeout_seconds=900,
                runtime=runtime,
            )
        )
        assert response.text == "# Managed result"
        return []

    assert invoke_legacy_method(legacy, context) == []
    assert len(reflector.requests) == 1
    with pytest.raises(ValueError, match="unavailable"):
        require_active_reflector_service()


def test_managed_reflector_preserves_hashed_failure_receipt(tmp_path: Path) -> None:
    service = object.__new__(ManagedCodexReflectorService)
    service.root = tmp_path
    service.receipts_root = tmp_path / "receipts"
    service.transcripts_root = tmp_path / "transcripts"
    service.receipts_root.mkdir()
    service.transcripts_root.mkdir()
    session_id = "rcb_oe_v0_reflector_failed"
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    class _Runtime:
        calls = 0

        async def start(self) -> None:
            return None

        async def exec(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    return_code=0, stdout="codex-cli 0.144.1\n", stderr=""
                )
            return SimpleNamespace(
                return_code=1,
                stdout='{"type":"error","message":"synthetic failure"}\n',
                stderr="synthetic stderr\n",
            )

    runtime = _Runtime()

    def _create_runtime(self, request):
        del self, request
        return runtime, (session_id, session_dir, None, tmp_path / "credential", None, None)

    async def _cleanup(self, runtime_arg, **kwargs):
        del self, runtime_arg, kwargs

    service._create_runtime = MethodType(_create_runtime, service)
    service._stop_and_cleanup = MethodType(_cleanup, service)
    request = ReflectorInferenceRequest(
        request_id="failed-request",
        prompt="synthetic prompt",
        model_name="gpt-5.5",
        reasoning_effort="high",
        timeout_seconds=30,
        runtime=default_managed_reflector_runtime(),
    )

    with pytest.raises(RuntimeError, match="execution failed"):
        service.infer(request)

    receipt = (
        service.receipts_root / f"{session_id}.failure.json"
    ).read_text(encoding="utf-8")
    assert '"exit_status": 1' in receipt
    assert '"path_fallback_allowed": false' in receipt
    assert (service.transcripts_root / f"{session_id}.failed.jsonl").is_file()
    assert (service.transcripts_root / f"{session_id}.failed.stderr").is_file()


def test_managed_reflector_renders_policy_overrides_as_two_cli_arguments() -> None:
    request = ReflectorInferenceRequest(
        request_id="render-request",
        prompt="synthetic prompt",
        model_name="gpt-5.5",
        reasoning_effort="high",
        timeout_seconds=30,
        runtime=default_managed_reflector_runtime(),
    )

    command = _managed_reflector_exec_command(request)

    assert "-c 'default_permissions=" in command
    assert "'-c '" not in command
    assert command.endswith("--output-last-message /openevo/session/last-message.md -")
