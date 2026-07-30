from __future__ import annotations

from types import SimpleNamespace

import pytest

from openevo.backend.science_candidate_readiness_control_v1 import (
    ManagedCandidateReadinessError,
    ProductionManagedCandidateReadinessProviderV1,
)
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceRunBinding,
    SupervisorStateError,
)
from openevo.internal_auth import InternalServiceIdentity
from openevo.runtime.managed import MANAGED_RUNTIME_IMAGES, MANAGED_RUNTIME_RELEASES
from openevo.runtime.managed_candidate_readiness import _canonical_sha256
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
)


def _binding() -> ServiceRunBinding:
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest="1" * 64,
        registry_digest="2" * 64,
        framework_lock_digest="3" * 64,
        credential="candidate-readiness-test-credential-" + "x" * 40,
    )
    return ServiceRunBinding(
        execution_mode=ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        codex_model="gpt-5.5",
        runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
        runtime_image_immutable_reference=(
            MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id
        ),
        runtime_identity_digest="4" * 64,
        generation_digest=identity.generation_digest,
        registry_digest=identity.registry_digest,
        framework_lock_digest=identity.framework_lock_digest,
        rollout_url="http://127.0.0.1:41001",
        evolution_backend_url="http://127.0.0.1:41002",
        gateway_url="http://127.0.0.1:41003",
        _identity=identity,
    )


def _gateway_payload(binding: ServiceRunBinding) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": "openevo.managed_candidate_runtime_readiness.v1",
        "generation_digest": binding.generation_digest,
        "release_registry_digest": binding.registry_digest,
        "framework_lock_digest": binding.framework_lock_digest,
        "runtime_profile": "managed_science",
        "runtime_digest": MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest,
        "docker_host_path_identity": "5" * 64,
        "codex_binary": "/opt/codex/bin/codex",
        "expected_cli_version": "0.144.1",
        "actual_cli_version": "0.144.1",
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "path_fallback_allowed": False,
        "authority_issued": True,
        "docker_mount_created": True,
        "container_path_visible": True,
        "container_user_can_read": True,
        "auth_file_read_only": True,
        "codex_cli_auth_visible": True,
        "host_source_hidden": True,
        "tool_sandbox_credential_hidden": True,
        "tool_environment_clean": True,
        "parent_process_secret_unreadable": True,
        "generation_matches": True,
        "image_digest_matches": True,
        "adoption_verified": True,
        "cleanup_verified": True,
        "codex_cli_started": True,
        "isolation_policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
        "isolation_policy_sha256": CODEX_SUBSCRIPTION_POLICY_SHA256,
        "isolation_probe_no_model": True,
        "model_started": False,
        "exit_status": 0,
        "created_at": "2026-07-29T00:00:00+00:00",
    }
    receipt["content_sha256"] = _canonical_sha256(receipt)
    return {"managed_candidate_runtime_readiness": receipt}


class _Services:
    def __init__(self, binding: ServiceRunBinding) -> None:
        self.binding = binding
        self.closed = False

    def adopt_current_run_binding(self, *_args, **_kwargs):
        return (
            SimpleNamespace(run_ready=True),
            SimpleNamespace(binding=self.binding, close=self._close),
        )

    def ensure_run_binding(self, *_args, **_kwargs):
        raise AssertionError("current binding should be adopted")

    def _close(self) -> None:
        self.closed = True


class _RestartedCoreServices(_Services):
    def adopt_current_run_binding(self, *_args, **_kwargs):
        raise SupervisorStateError("prior owner is not adoptable")

    def ensure_run_binding(self, *_args, **_kwargs):
        return (
            SimpleNamespace(run_ready=True),
            SimpleNamespace(binding=self.binding, close=self._close),
        )


class _FailedGatewayServices(_Services):
    def adopt_current_run_binding(self, *_args, **_kwargs):
        return None

    def ensure_run_binding(self, *_args, **_kwargs):
        gateway = SimpleNamespace(error_code="sandbox_invocation_failed")
        snapshot = SimpleNamespace(
            run_ready=False,
            service=lambda service_id: gateway if service_id == "gateway" else None,
        )
        return snapshot, None


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, object]:
        return self.payload


class _Client:
    payload: dict[str, object]

    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def get(self, path: str) -> _Response:
        assert path == "/health"
        return _Response(self.payload)


def test_provider_returns_generation_release_bound_no_model_receipt(monkeypatch) -> None:
    binding = _binding()
    services = _Services(binding)
    _Client.payload = _gateway_payload(binding)
    monkeypatch.setattr(
        "openevo.backend.science_candidate_readiness_control_v1.httpx.Client",
        _Client,
    )
    receipt = ProductionManagedCandidateReadinessProviderV1(
        services=services,
        daemon_release_identity="6" * 64,
        release_install_digest="7" * 64,
    ).read_managed_candidate_readiness()
    assert receipt.core_generation == binding.generation_digest
    assert receipt.credential_mount_adopted is True
    assert receipt.codex_cli_started is True
    assert receipt.model_started is False
    assert services.closed is True


def test_provider_reissues_binding_after_typed_core_restart(monkeypatch) -> None:
    binding = _binding()
    services = _RestartedCoreServices(binding)
    _Client.payload = _gateway_payload(binding)
    monkeypatch.setattr(
        "openevo.backend.science_candidate_readiness_control_v1.httpx.Client",
        _Client,
    )

    receipt = ProductionManagedCandidateReadinessProviderV1(
        services=services,
        daemon_release_identity="6" * 64,
        release_install_digest="7" * 64,
    ).read_managed_candidate_readiness()

    assert receipt.candidate_runtime_ready is True
    assert services.closed is True


def test_provider_preserves_field_level_gateway_isolation_failure() -> None:
    provider = ProductionManagedCandidateReadinessProviderV1(
        services=_FailedGatewayServices(_binding()),
        daemon_release_identity="6" * 64,
        release_install_digest="7" * 64,
    )

    with pytest.raises(ManagedCandidateReadinessError) as raised:
        provider.read_managed_candidate_readiness()

    assert raised.value.code == (
        "CANDIDATE_SUBSCRIPTION_ISOLATION_NOT_READY:sandbox_invocation_failed"
    )


def test_provider_rejects_gateway_generation_drift(monkeypatch) -> None:
    binding = _binding()
    services = _Services(binding)
    payload = _gateway_payload(binding)
    runtime = dict(payload["managed_candidate_runtime_readiness"])  # type: ignore[arg-type]
    runtime["generation_digest"] = "8" * 64
    runtime["content_sha256"] = _canonical_sha256(
        {key: value for key, value in runtime.items() if key != "content_sha256"}
    )
    _Client.payload = {"managed_candidate_runtime_readiness": runtime}
    monkeypatch.setattr(
        "openevo.backend.science_candidate_readiness_control_v1.httpx.Client",
        _Client,
    )
    provider = ProductionManagedCandidateReadinessProviderV1(
        services=services,
        daemon_release_identity="6" * 64,
        release_install_digest="7" * 64,
    )
    with pytest.raises(ManagedCandidateReadinessError, match="identity"):
        provider.read_managed_candidate_readiness()
