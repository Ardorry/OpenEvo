from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from openevo.runtime import managed_candidate_readiness as candidate_module
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CANARY_OK,
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
    CodexSubscriptionIsolationError,
)
from openevo.runtime.docker_host import DockerHostPathSpec
from openevo.runtime.managed_candidate_readiness import (
    ManagedCandidateRuntimeProbe,
    ManagedCandidateRuntimeReadiness,
    managed_candidate_readiness_failure_code,
    managed_candidate_runtime_probe_failure_frame,
)


class _Snapshot:
    def verify(self) -> None:
        pass

    def prepare_snapshot(self):
        return self

    def close(self) -> None:
        pass


class _HeldRoot:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create_private_directory(self, prefix: str):
        path = self.root / f"{prefix}-{len(tuple(self.root.iterdir()))}"
        path.mkdir(mode=0o700)
        state = path.stat()
        return path, (state.st_dev, state.st_ino, state.st_uid)

    def close(self) -> None:
        pass


class _Runtime:
    absence_proven = True

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def start(self) -> None:
        pass

    async def exec(self, command: str, **_kwargs):
        self.commands.append(command)
        if command.startswith("/usr/bin/test -x /usr/bin/bwrap"):
            return SimpleNamespace(return_code=0, stdout="", stderr="")
        if command.startswith("/bin/sh"):
            return SimpleNamespace(
                return_code=0,
                stdout=f"{candidate_module.os.getuid()}\n{candidate_module.os.getgid()}\n",
                stderr="",
            )
        if command == "/opt/codex/bin/codex --version":
            return SimpleNamespace(return_code=0, stdout="codex-cli 0.144.1\n", stderr="")
        return SimpleNamespace(
            return_code=0,
            stdout=CODEX_SUBSCRIPTION_CANARY_OK + "\n",
            stderr="",
        )

    async def stop(self) -> None:
        pass


class _RuntimeWithoutAbsenceProof(_Runtime):
    absence_proven = False


def test_candidate_probe_failure_frame_is_closed_and_non_secret() -> None:
    frame = managed_candidate_runtime_probe_failure_frame(
        CodexSubscriptionIsolationError("sandbox_invocation_failed"),
        generation_digest="1" * 64,
        release_registry_digest="2" * 64,
    )

    assert frame == {
        "component": "managed_candidate_subscription_isolation",
        "event": "managed_candidate_runtime_probe_terminal",
        "status": "failed",
        "failure_code": "sandbox_invocation_failed",
        "phase": "subscription_isolation",
        "generation_digest": "1" * 64,
        "release_registry_digest": "2" * 64,
        "process_uid": candidate_module.os.getuid(),
        "codex_cli_started": True,
        "model_started": False,
        "retryable": False,
        "secret_recorded": False,
    }
    assert managed_candidate_readiness_failure_code("sandbox_invocation_failed") == (
        "CANDIDATE_SUBSCRIPTION_ISOLATION_NOT_READY:sandbox_invocation_failed"
    )
    assert managed_candidate_readiness_failure_code("untrusted") == (
        "CANDIDATE_RUNTIME_NOT_READY"
    )


def test_candidate_probe_failure_frame_preserves_only_closed_diagnostics() -> None:
    frame = managed_candidate_runtime_probe_failure_frame(
        CodexSubscriptionIsolationError(
            "sandbox_child_not_started",
            nested_return_code=1,
            probe_progress="not_started",
            stderr_class="policy_rejected",
            helper_started=True,
            helper_return_code=127,
            missing_executable_basename="bwrap",
        ),
        generation_digest="1" * 64,
        release_registry_digest="2" * 64,
    )

    assert frame["failure_code"] == "sandbox_child_not_started"
    assert frame["nested_return_code"] == 1
    assert frame["probe_progress"] == "not_started"
    assert frame["stderr_class"] == "policy_rejected"
    assert frame["helper_started"] is True
    assert frame["helper_return_code"] == 127
    assert frame["missing_executable_basename"] == "bwrap"
    assert frame["model_started"] is False
    assert frame["secret_recorded"] is False


@pytest.mark.asyncio
async def test_candidate_canary_adopts_mount_reads_as_container_user_and_starts_no_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _HeldRoot(tmp_path / "mapped")
    held.root.mkdir()
    runtime = _Runtime()
    monkeypatch.setattr(
        candidate_module.HeldDockerSessionRoot,
        "open",
        lambda _mapping: held,
    )
    monkeypatch.setattr(
        candidate_module,
        "stage_codex_subscription_auth",
        lambda **_kwargs: SimpleNamespace(auth_identity=(1, 2, 3, 4, 1, 5, 6, 7)),
    )
    def create_runtime(_spec, _session_id, session_dir, **_kwargs):
        assert (session_dir / "home/.openevo-codex-readiness").is_dir()
        return runtime

    monkeypatch.setattr(candidate_module, "create_runtime", create_runtime)
    monkeypatch.setattr(
        candidate_module,
        "remove_credential_tree",
        lambda path, *_a: shutil.rmtree(path),
    )
    monkeypatch.setattr(
        candidate_module,
        "remove_session_tree",
        lambda path, *_a: shutil.rmtree(path),
    )
    mapping = DockerHostPathSpec.model_construct(
        schema_version=1,
        container_id="a" * 64,
        mount_destination="/data",
        mount_source="/host",
        runtime_container_root="/data/runtime",
        runtime_daemon_root="/host/runtime",
        mount_device=1,
        mount_inode=2,
        runtime_device=1,
        runtime_inode=3,
        sessions_device=1,
        sessions_inode=4,
        runtime_uid=candidate_module.os.getuid(),
        identity_digest="5" * 64,
    )
    receipt = await ManagedCandidateRuntimeProbe(
        root=tmp_path / "receipts",
        credential_authority=_Snapshot(),  # type: ignore[arg-type]
        docker_host_path=mapping,
        generation_digest="1" * 64,
        release_registry_digest="2" * 64,
        framework_lock_digest="3" * 64,
    ).verify()
    assert receipt.adoption_verified is True
    assert receipt.actual_cli_version == "0.144.1"
    assert receipt.model_started is False
    assert receipt.tool_sandbox_credential_hidden is True
    assert receipt.isolation_probe_no_model is True
    assert len(runtime.commands) == 5


def test_candidate_readiness_rejects_tamper() -> None:
    payload = {
        "schema_version": "openevo.managed_candidate_runtime_readiness.v1",
        "generation_digest": "1" * 64,
        "release_registry_digest": "2" * 64,
        "framework_lock_digest": "3" * 64,
        "runtime_profile": "managed_science",
        "runtime_digest": "sha256:" + "4" * 64,
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
        "content_sha256": "0" * 64,
    }
    with pytest.raises(ValueError, match="digest"):
        ManagedCandidateRuntimeReadiness.model_validate(payload)


@pytest.mark.asyncio
async def test_candidate_canary_retains_private_trees_without_absence_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _HeldRoot(tmp_path / "mapped")
    held.root.mkdir()
    runtime = _RuntimeWithoutAbsenceProof()
    monkeypatch.setattr(
        candidate_module.HeldDockerSessionRoot,
        "open",
        lambda _mapping: held,
    )
    monkeypatch.setattr(
        candidate_module,
        "stage_codex_subscription_auth",
        lambda **_kwargs: SimpleNamespace(auth_identity=(1, 2, 3, 4, 1, 5, 6, 7)),
    )
    monkeypatch.setattr(candidate_module, "create_runtime", lambda *_a, **_kw: runtime)
    removed: list[Path] = []
    monkeypatch.setattr(
        candidate_module,
        "remove_credential_tree",
        lambda path, *_a: removed.append(path),
    )
    monkeypatch.setattr(
        candidate_module,
        "remove_session_tree",
        lambda path, *_a: removed.append(path),
    )
    mapping = DockerHostPathSpec.model_construct(
        schema_version=1,
        container_id="a" * 64,
        mount_destination="/data",
        mount_source="/host",
        runtime_container_root="/data/runtime",
        runtime_daemon_root="/host/runtime",
        mount_device=1,
        mount_inode=2,
        runtime_device=1,
        runtime_inode=3,
        sessions_device=1,
        sessions_inode=4,
        runtime_uid=candidate_module.os.getuid(),
        identity_digest="5" * 64,
    )
    with pytest.raises(RuntimeError, match="absence was not proven"):
        await ManagedCandidateRuntimeProbe(
            root=tmp_path / "receipts",
            credential_authority=_Snapshot(),  # type: ignore[arg-type]
            docker_host_path=mapping,
            generation_digest="1" * 64,
            release_registry_digest="2" * 64,
            framework_lock_digest="3" * 64,
        ).verify()
    assert removed == []
    assert sorted(path.name.split("-")[0] for path in held.root.iterdir()) == [
        "credentials",
        "session",
    ]
