from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from openevo_researchclawbench import cli
from openevo_researchclawbench.config import (
    MANAGED_RUNTIME_IMAGE,
    MANAGED_RUNTIME_IMAGE_ID,
    ExperimentConfig,
    ProtocolError,
)
from openevo_researchclawbench.evaluator_dependency_lock import (
    write_evaluator_dependency_lock,
)
from openevo_researchclawbench.formal_v11 import (
    FormalV11ProtocolError,
    FormalV11RuntimeAssetOverrides,
    bind_formal_core_identity,
    prepare_formal_v11_protocol,
    validate_formal_v11_identity,
)

ROOT = Path(__file__).resolve().parents[4]
BASE_PROTOCOL = (
    ROOT / "experiments" / "sequential_task_reflector_evolution_v0" / "protocol" / "protocol.yaml"
)


def _base_config(tmp_path: Path) -> ExperimentConfig:
    project = tmp_path / "project"
    experiment = project / "experiments" / "sequential_task_reflector_evolution_v0"
    protocol_root = experiment / "protocol"
    rcb = project / "ResearchClawBench"
    for path in (
        project / "OpenEvo" / "src" / "openevo",
        project / "OpenEvo" / "benchmarks" / "researchclawbench",
        protocol_root,
        rcb / "tasks",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (project / "OpenEvo" / "src" / "openevo" / "core.py").write_text(
        "CORE = True\n", encoding="utf-8"
    )
    (project / "OpenEvo" / "benchmarks" / "researchclawbench" / "adapter.py").write_text(
        "ADAPTER = True\n", encoding="utf-8"
    )
    payload = yaml.safe_load(BASE_PROTOCOL.read_text(encoding="utf-8"))
    payload["candidate"]["runtime_image"] = MANAGED_RUNTIME_IMAGE
    payload["candidate"]["runtime_image_id"] = MANAGED_RUNTIME_IMAGE_ID
    payload["reflector"]["runtime_image_digest"] = MANAGED_RUNTIME_IMAGE.split(
        "@", 1
    )[1]
    payload["paths"] = {
        "project_root": str(project),
        "experiment_root": str(experiment),
        "researchclawbench_root": str(rcb),
    }
    base = protocol_root / "base.yaml"
    base.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return ExperimentConfig.load(base)


def _runtime_assets(base: ExperimentConfig, *, name: str = "fresh") -> FormalV11RuntimeAssetOverrides:
    root = base.experiment_root / "runtime" / name
    root.mkdir(parents=True)
    lock = root / "framework-lock.json"
    bundle = root / "openevo-daemon-linux-x86_64"
    manifest = root / "openevo-daemon-bundle.json"
    readiness = root / "reflector-readiness.json"
    mount = root / "reflector-mount-readiness.json"
    evaluator_lock = root / "evaluator-dependency-lock.json"
    wheel_sha = "1" * 64
    lock.write_text(
        json.dumps(
            {
                "distribution": "openevo",
                "distribution_digest": wheel_sha,
                "distribution_version": "0.1.9",
                "schema_version": "1",
                "wheel_filename": "openevo-0.1.9-py3-none-any.whl",
            }
        ),
        encoding="utf-8",
    )
    bundle.write_bytes(b"fresh-daemon-bundle")
    def file_sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    release_identity = "2" * 64
    registry_digest = "3" * 64
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact": {
                    "filename": bundle.name,
                    "sha256": file_sha(bundle),
                    "size": bundle.stat().st_size,
                },
                "core": {
                    "framework_lock": {
                        "filename": lock.name,
                        "sha256": file_sha(lock),
                    },
                    "registry_digest": registry_digest,
                    "wheel": {
                        "filename": "openevo-0.1.9-py3-none-any.whl",
                        "sha256": wheel_sha,
                    },
                },
                "release": {
                    "identity": release_identity,
                    "source_commit": base.require("source_identity.openevo_commit"),
                },
            }
        ),
        encoding="utf-8",
    )
    mount_body = {
        "schema_version": "openevo.managed_reflector_credential_mount_readiness.v1",
        "authority_id": "4" * 64,
        "container_authority_id": "5" * 64,
        "worker_launch_id": "mrl-" + "6" * 32,
        "container_launch_id": "openevo_reflector_test",
        "adoption_nonce_sha256": "7" * 64,
        "service_identity_digest": "8" * 64,
        "runtime_profile": "managed_science",
        "runtime_digest": base.require("reflector.runtime_image_digest"),
        "docker_host_path_identity": "9" * 64,
        "generation_digest": "a" * 64,
        "daemon_release_identity": release_identity,
        "release_install_digest": "b" * 64,
        "release_registry_digest": registry_digest,
        "credential_target": "/openevo/credentials/codex",
        "container_uid": 1000,
        "container_gid": 1000,
        "authority_issued": True,
        "docker_mount_created": True,
        "container_path_visible": True,
        "container_user_can_read": True,
        "auth_file_read_only": True,
        "generation_matches": True,
        "release_identity_matches": True,
        "adoption_receipt_valid": True,
        "container_authority_verified": True,
        "cleanup_verified": True,
        "codex_cli_started": False,
        "model_started": False,
        "created_at": "2026-07-29T00:00:00+00:00",
    }
    mount_body["content_sha256"] = hashlib.sha256(
        json.dumps(
            mount_body,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    mount.write_text(json.dumps(mount_body), encoding="utf-8")
    runtime_body = {
        "schema_version": "openevo.managed_reflector_readiness.v1",
        "authority_id": mount_body["authority_id"],
        "container_authority_id": "c" * 64,
        "worker_launch_id": mount_body["worker_launch_id"],
        "container_launch_id": "openevo_reflector_runtime_test",
        "adoption_nonce_sha256": "d" * 64,
        "service_identity_digest": mount_body["service_identity_digest"],
        "generation_digest": mount_body["generation_digest"],
        "daemon_release_identity": release_identity,
        "release_install_digest": mount_body["release_install_digest"],
        "release_registry_digest": registry_digest,
        "runtime_profile": "managed_science",
        "runtime_digest": base.require("reflector.runtime_image_digest"),
        "docker_host_path_identity": mount_body["docker_host_path_identity"],
        "credential_mount_content_sha256": mount_body["content_sha256"],
        "codex_binary": "/opt/codex/bin/codex",
        "expected_cli_version": "0.144.1",
        "actual_cli_version": "0.144.1",
        "model": "readiness-only",
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "path_fallback_allowed": False,
        "exit_status": 0,
        "container_authority_verified": True,
        "adoption_receipt_valid": True,
        "cleanup_verified": True,
        "codex_cli_started": True,
        "model_started": False,
        "created_at": "2026-07-29T00:00:00+00:00",
    }
    runtime_body["content_sha256"] = hashlib.sha256(
        json.dumps(
            runtime_body,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    readiness.write_text(json.dumps(runtime_body), encoding="utf-8")
    write_evaluator_dependency_lock(evaluator_lock)
    return FormalV11RuntimeAssetOverrides(
        framework_lock=lock,
        daemon_bundle=bundle,
        daemon_manifest=manifest,
        reflector_readiness_receipt=readiness,
        reflector_credential_mount_readiness_receipt=mount,
        evaluator_dependency_lock=evaluator_lock,
    )


def test_prepare_v11_is_append_only_and_identity_verified(tmp_path: Path) -> None:
    base = _base_config(tmp_path)
    base.raw["bootstrap_scope"] = {
        "candidate_execution_allowed": False,
        "evolution_execution_allowed": False,
        "model_operations_allowed": 0,
    }
    base.raw["formal_v11_bootstrap_assets"] = {"bootstrap_only": True}
    before = base.path.read_bytes()
    output = base.path.parent / "formal-v11.yaml"
    identity = base.path.parent / "formal-v11.identity.json"
    receipt = prepare_formal_v11_protocol(
        base_config=base,
        output_path=output,
        identity_path=identity,
        community_run_id="rcb_oe_v0_community17_formal_v11",
        official_run_id="rcb_oe_v0_official40_frozen_v11",
        runtime_assets=_runtime_assets(base),
    )
    assert base.path.read_bytes() == before
    assert receipt["model_calls"] == 0
    assert receipt["judge_calls"] == 0
    config = ExperimentConfig.load(output)
    assert config.raw["protocol_name"] == "community17_official40_formal_v11"
    assert "bootstrap_scope" not in config.raw
    assert "formal_v11_bootstrap_assets" not in config.raw
    assert config.formal_runs_v11["community"]["candidate_runs"] == 51
    assert config.formal_runs_v11["official"]["candidate_runs"] == 40
    assert config.formal_runs_v11["official"]["budget"] == {
        "attempts_per_task": 1,
        "max_candidate_model_calls": 40,
        "max_judge_task_operations": 40,
        "max_unified_scorer_operations": 1,
        "max_infrastructure_retries_per_operation": 1,
        "candidate_runtime_seconds_per_task": 3600,
        "judge_runtime_seconds_per_task": 1800,
        "cumulative_runtime_seconds": 216000,
        "user_configurable": True,
    }
    assert (
        config.formal_runs_v11["runtime_assets"]["framework_lock"]["path"]
        != base.require("native_openevo.framework_lock")
    )
    assert config.community_validator_failure_policy["run_id_suffix"] == "v11"
    assert config.raw["teacher"] == {
        "enabled_on_community": True,
        "modes": ["HARD_GT", "SOFT_JUDGE", "MIXED"],
        "production_judge_partition": "SOFT_JUDGE",
        "hard_gt_authority_required": "STRUCTURED_EVALUATOR_AUTHORITY",
        "hard_gt_unavailable_behavior": "SOFT_JUDGE",
        "validator_failure_partition": "MIXED_TASK_LOCAL_VALIDATOR_ONLY",
        "task_local_overlay": True,
        "global_artifact_answers_forbidden": True,
        "official_enabled": False,
    }
    assert config.raw["reflector"]["require_credential_mount_readiness"] is True
    assert config.raw["judge"]["evaluator_dependency_lock"]["path"].endswith(
        "evaluator-dependency-lock.json"
    )
    assert validate_formal_v11_identity(config) == receipt


def test_prepare_v11_accepts_only_closed_append_only_recovery_continuation(
    tmp_path: Path,
) -> None:
    base = _base_config(tmp_path)
    source = {
        "source_namespace": "rcb_oe_v0_community17_formal_v12",
        "source_state_sha256": "1" * 64,
        "source_transition_id": "successor-source",
        "source_transition_attempt_id": "successor-attempt-source",
        "source_candidate_session_id": "session-source",
        "source_completed_dataset_id": "dataset-source",
        "source_dataset_revision": "artifact-source.v1",
        "source_attachment_id": "attachment-source",
        "source_resolved_view_sha256": "2" * 64,
        "source_attempt_index": 0,
        "candidate_reexecuted": False,
        "additional_candidate_model_calls": 0,
    }
    output = base.path.parent / "formal-v11-continuation.yaml"
    identity = base.path.parent / "formal-v11-continuation.identity.json"
    prepare_formal_v11_protocol(
        base_config=base,
        output_path=output,
        identity_path=identity,
        community_run_id="rcb_oe_v0_community17_continuation_v12",
        official_run_id="rcb_oe_v0_official40_continuation_v12",
        runtime_assets=_runtime_assets(base, name="continuation"),
        successor_recovery_continuation=source,
    )
    config = ExperimentConfig.load(output)
    community = config.formal_runs_v11["community"]
    assert community["namespace_type"] == (
        "append_only_successor_recovery_continuation"
    )
    assert community["source_attempts_consumed"] == 1
    assert community["candidate_operations_remaining"] == 50
    assert community["successor_recovery_continuation"] == source
    assert config.formal_runs_v11["legacy_authority_recovery"] == {
        "required": True,
        "classification": "APPEND_ONLY_PRE_JOB_SUCCESSOR_RECOVERY_CONTINUATION",
        "fresh_path_uses_recovery_endpoint": True,
    }


def test_prepare_formal_protocol_binds_v12_run_suffix(tmp_path: Path) -> None:
    base = _base_config(tmp_path)
    base.raw["bootstrap_scope"] = {
        "candidate_execution_allowed": False,
        "evolution_execution_allowed": False,
        "model_operations_allowed": 0,
    }
    base.raw["formal_v11_bootstrap_assets"] = {"bootstrap_only": True}
    output = base.path.parent / "formal-v12.yaml"
    identity = base.path.parent / "formal-v12.identity.json"

    prepare_formal_v11_protocol(
        base_config=base,
        output_path=output,
        identity_path=identity,
        community_run_id="rcb_oe_v0_community17_formal_v12",
        official_run_id="rcb_oe_v0_official40_frozen_v12",
        runtime_assets=_runtime_assets(base),
    )

    config = ExperimentConfig.load(output)
    assert config.community_validator_failure_policy["run_id_suffix"] == "v12"


def test_prepare_formal_protocol_accepts_exact_deployment_bootstrap(
    tmp_path: Path,
) -> None:
    base = _base_config(tmp_path)
    assets = _runtime_assets(base)
    base.raw["protocol_name"] = "formal_v11_deployment_bootstrap"
    base.raw["bootstrap_scope"] = {
        "core_control_attach_allowed": True,
        "managed_candidate_readiness_get_allowed": True,
        "managed_reflector_readiness_get_allowed": True,
        "candidate_execution_allowed": False,
        "judge_execution_allowed": False,
        "evolution_execution_allowed": False,
        "official_execution_allowed": False,
        "recovery_creation_allowed": False,
        "model_operations_allowed": 0,
    }
    base.raw["native_openevo"]["framework_lock"] = str(assets.framework_lock)
    base.raw["native_openevo"]["remote_core"]["daemon_bundle"] = str(
        assets.daemon_bundle
    )
    base.raw["native_openevo"]["remote_core"]["daemon_manifest"] = str(
        assets.daemon_manifest
    )
    base.raw["formal_v11_bootstrap_assets"] = {
        "framework_lock_sha256": hashlib.sha256(
            assets.framework_lock.read_bytes()
        ).hexdigest(),
        "daemon_bundle_sha256": hashlib.sha256(
            assets.daemon_bundle.read_bytes()
        ).hexdigest(),
        "daemon_manifest_sha256": hashlib.sha256(
            assets.daemon_manifest.read_bytes()
        ).hexdigest(),
        "managed_runtime_image_digest": base.require(
            "reflector.runtime_image_digest"
        ),
        "managed_runtime_loaded_image_id": base.require(
            "candidate.runtime_image_id"
        ),
    }

    output = base.path.parent / "formal-v12-bootstrap-derived.yaml"
    identity = base.path.parent / "formal-v12-bootstrap-derived.identity.json"
    prepare_formal_v11_protocol(
        base_config=base,
        output_path=output,
        identity_path=identity,
        community_run_id="rcb_oe_v0_community17_formal_v12",
        official_run_id="rcb_oe_v0_official40_frozen_v12",
        runtime_assets=assets,
    )

    assert ExperimentConfig.load(output).formal_runs_v11["community"]["run_id"].endswith(
        "_v12"
    )


def test_prepare_v11_refuses_overwrite_or_historical_source(tmp_path: Path) -> None:
    base = _base_config(tmp_path)
    with pytest.raises(FormalV11ProtocolError):
        prepare_formal_v11_protocol(
            base_config=base,
            output_path=base.path,
            identity_path=base.path.parent / "identity.json",
            community_run_id="rcb_oe_v0_community17_formal_v11",
            official_run_id="rcb_oe_v0_official40_frozen_v11",
            runtime_assets=_runtime_assets(base),
        )


def test_v11_identity_tamper_fails_closed(tmp_path: Path) -> None:
    base = _base_config(tmp_path)
    output = base.path.parent / "formal-v11.yaml"
    identity = base.path.parent / "formal-v11.identity.json"
    prepare_formal_v11_protocol(
        base_config=base,
        output_path=output,
        identity_path=identity,
        community_run_id="rcb_oe_v0_community17_formal_v11",
        official_run_id="rcb_oe_v0_official40_frozen_v11",
        runtime_assets=_runtime_assets(base),
    )
    value = json.loads(identity.read_text(encoding="utf-8"))
    value["community_run_id"] = "rcb_oe_v0_drifted"
    identity.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(FormalV11ProtocolError):
        validate_formal_v11_identity(ExperimentConfig.load(output))


def test_prepare_v11_refuses_inherited_runtime_assets(tmp_path: Path) -> None:
    base = _base_config(tmp_path)
    assets = _runtime_assets(base)
    payload = dict(base.raw)
    payload["native_openevo"] = dict(payload["native_openevo"])
    payload["native_openevo"]["framework_lock"] = str(assets.framework_lock)
    inherited = base.path.parent / "inherited.yaml"
    inherited.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    inherited_config = ExperimentConfig.load(inherited)
    with pytest.raises(FormalV11ProtocolError, match="must not inherit"):
        prepare_formal_v11_protocol(
            base_config=inherited_config,
            output_path=base.path.parent / "formal-v11.yaml",
            identity_path=base.path.parent / "formal-v11.identity.json",
            community_run_id="rcb_oe_v0_community17_formal_v11",
            official_run_id="rcb_oe_v0_official40_frozen_v11",
            runtime_assets=assets,
        )


def test_prepare_v11_rejects_daemon_manifest_source_commit_drift(
    tmp_path: Path,
) -> None:
    base = _base_config(tmp_path)
    assets = _runtime_assets(base)
    manifest = json.loads(assets.daemon_manifest.read_text(encoding="utf-8"))
    manifest["release"]["source_commit"] = "f" * 40
    assets.daemon_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FormalV11ProtocolError, match="release assets disagree"):
        prepare_formal_v11_protocol(
            base_config=base,
            output_path=base.path.parent / "formal-v11.yaml",
            identity_path=base.path.parent / "formal-v11.identity.json",
            community_run_id="rcb_oe_v0_community17_formal_v11",
            official_run_id="rcb_oe_v0_official40_frozen_v11",
            runtime_assets=assets,
        )


def test_prepare_v11_rejects_stale_runtime_readiness_for_current_mount(
    tmp_path: Path,
) -> None:
    base = _base_config(tmp_path)
    assets = _runtime_assets(base)
    runtime = json.loads(
        assets.reflector_readiness_receipt.read_text(encoding="utf-8")
    )
    runtime["authority_id"] = "e" * 64
    runtime.pop("content_sha256")
    runtime["content_sha256"] = hashlib.sha256(
        json.dumps(
            runtime,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assets.reflector_readiness_receipt.write_text(
        json.dumps(runtime), encoding="utf-8"
    )

    with pytest.raises(FormalV11ProtocolError, match="identities disagree"):
        prepare_formal_v11_protocol(
            base_config=base,
            output_path=base.path.parent / "formal-v11.yaml",
            identity_path=base.path.parent / "formal-v11.identity.json",
            community_run_id="rcb_oe_v0_community17_formal_v11",
            official_run_id="rcb_oe_v0_official40_frozen_v11",
            runtime_assets=assets,
        )


def _core_readiness(
    config: ExperimentConfig | None = None,
    *,
    generation: str = "a" * 32,
    release_identity: str | None = None,
) -> dict[str, Any]:
    assets = config.formal_runs_v11["runtime_assets"] if config is not None else None
    return {
        "generation": generation,
        "release_identity": release_identity
        or (assets["daemon_manifest"]["release_identity"] if assets else "b" * 64),
        "registry_digest": (
            assets["daemon_manifest"]["registry_digest"] if assets else "c" * 64
        ),
        "source_commit": (
            assets["daemon_manifest"]["source_commit"] if assets else "d" * 40
        ),
        "service_identity_id": "core-service-formal-v11",
        "bearer_present": True,
        "bearer_valid": True,
        "generation_matches": True,
        "candidate_port_authorized": True,
        "secret_recorded": False,
        "environment_fallback_used": False,
    }


def test_formal_core_binding_is_stable_and_rejects_generation_drift(
    tmp_path: Path,
) -> None:
    base = _base_config(tmp_path)
    output = base.path.parent / "formal-v11.yaml"
    identity = base.path.parent / "formal-v11.identity.json"
    prepare_formal_v11_protocol(
        base_config=base,
        output_path=output,
        identity_path=identity,
        community_run_id="rcb_oe_v0_community17_formal_v11",
        official_run_id="rcb_oe_v0_official40_frozen_v11",
        runtime_assets=_runtime_assets(base),
    )
    config = ExperimentConfig.load(output)
    first = bind_formal_core_identity(
        config=config,
        run_id="rcb_oe_v0_community17_formal_v11",
        authority_readiness=_core_readiness(config),
    )
    assert (
        bind_formal_core_identity(
            config=config,
            run_id="rcb_oe_v0_community17_formal_v11",
            authority_readiness=_core_readiness(config),
        )
        == first
    )
    with pytest.raises(FormalV11ProtocolError, match="GENERATION_OR_RELEASE_DRIFT"):
        bind_formal_core_identity(
            config=config,
            run_id="rcb_oe_v0_community17_formal_v11",
            authority_readiness=_core_readiness(config, generation="e" * 32),
        )

    with pytest.raises(FormalV11ProtocolError, match="RELEASE_IDENTITY_DRIFT"):
        bind_formal_core_identity(
            config=config,
            run_id="rcb_oe_v0_community17_formal_v11_drift",
            authority_readiness=_core_readiness(
                config,
                release_identity="e" * 64,
            ),
        )


class _FakeConfig:
    def __init__(self, root: Path) -> None:
        self.experiment_root = root
        self.formal_runs_v11 = {
            "community": {"run_id": "rcb_oe_v0_community17_formal_v11"},
            "official": {
                "run_id": "rcb_oe_v0_official40_frozen_v11",
                "source_community_run_id": "rcb_oe_v0_community17_formal_v11",
            },
        }


class _FakeCommunityControl:
    calls: ClassVar[list[str]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return {"stage": "TASK_ATTEMPT_READY"}

    def verify(self) -> dict[str, Any]:
        self.calls.append("verify")
        return {"status": "PASS"}

    def stop_owned(self) -> dict[str, Any]:
        self.calls.append("stop-owned")
        return {"status": "PASS", "stopped": []}


def test_formal_start_drops_and_reacquires_core_per_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _FakeConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(cli, "validate_formal_v11_identity", lambda _config: {})
    monkeypatch.setattr(
        cli,
        "command_formal_community_live_preflight",
        lambda _config, _run_id: {"ready": True},
    )
    binds: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli,
        "bind_formal_core_identity",
        lambda **kwargs: binds.append(kwargs["authority_readiness"]),
    )

    class Authority:
        managed_host_profile_ready = True

        def __init__(self) -> None:
            self.closed = False

        def public_readiness(self) -> dict[str, Any]:
            return _core_readiness()

        def close(self) -> None:
            self.closed = True

    authorities: list[Authority] = []

    def acquire(_config: Any) -> Authority:
        authority = Authority()
        authorities.append(authority)
        return authority

    monkeypatch.setattr(cli, "acquire_managed_core_control", acquire)

    class TransitionControl:
        transitions = 0

        def __init__(self, **kwargs: Any) -> None:
            self.production = kwargs["production"]

        def initialize(self) -> dict[str, Any]:
            assert self.production is False
            return {"stage": "INITIALIZED"}

        def run_next(self) -> dict[str, Any]:
            assert self.production is True
            type(self).transitions += 1
            return {
                "stage": ("TASK_ATTEMPT_READY" if type(self).transitions == 1 else "FINAL_FROZEN")
            }

    monkeypatch.setattr(cli, "DurableTrainingControl", TransitionControl)
    result = cli.main(
        [
            "formal-community-start",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_community17_formal_v11",
            "--until",
            "FINAL_FROZEN",
        ]
    )
    assert result == 0
    assert TransitionControl.transitions == 2
    assert len(authorities) == 2
    assert all(authority.closed for authority in authorities)
    assert len(binds) == 2


def test_response_loss_closes_tunnel_and_resume_reacquires_without_repeating_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(cli, "validate_formal_v11_identity", lambda _config: {})
    monkeypatch.setattr(cli, "bind_formal_core_identity", lambda **_kwargs: {})

    class Authority:
        managed_host_profile_ready = True

        def __init__(self) -> None:
            self.closed = False

        def public_readiness(self) -> dict[str, Any]:
            return _core_readiness()

        def close(self) -> None:
            self.closed = True

    authorities: list[Authority] = []

    def acquire(_config: Any) -> Authority:
        authority = Authority()
        authorities.append(authority)
        return authority

    monkeypatch.setattr(cli, "acquire_managed_core_control", acquire)

    class RecoveringControl:
        external_effects = 0

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_next(self) -> dict[str, Any]:
            type(self).external_effects += 1
            raise RuntimeError("synthetic SSH response loss after durable side effect")

        def resume(self) -> dict[str, Any]:
            # The real supervisor resolves the planned idempotency key here;
            # this fixture proves the CLI supplies a fresh attachment and does
            # not itself invoke run_next again.
            return {"stage": "CANDIDATE_SEALED", "recovered": True}

    monkeypatch.setattr(cli, "DurableTrainingControl", RecoveringControl)
    common = [
        "--protocol",
        str(tmp_path / "protocol.yaml"),
        "--run-id",
        "rcb_oe_v0_community17_formal_v11",
    ]
    assert cli.main(["formal-community-run-next", *common]) == 2
    assert cli.main(["formal-community-resume", *common]) == 0
    assert RecoveringControl.external_effects == 1
    assert len(authorities) == 2
    assert all(authority.closed for authority in authorities)


def test_formal_start_stops_on_blocked_terminal_without_repeating_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(cli, "validate_formal_v11_identity", lambda _config: {})
    monkeypatch.setattr(
        cli,
        "command_formal_community_live_preflight",
        lambda _config, _run_id: {"ready": True},
    )

    class Control:
        effects = 0

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def initialize(self) -> dict[str, Any]:
            return {"stage": "INITIALIZED"}

    def transition(**_kwargs: Any) -> dict[str, Any]:
        Control.effects += 1
        return {"stage": "BLOCKED", "failure_reason": "synthetic"}

    monkeypatch.setattr(cli, "DurableTrainingControl", Control)
    monkeypatch.setattr(cli, "_run_formal_community_transition", transition)
    result = cli.main(
        [
            "formal-community-start",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_community17_formal_v11",
            "--until",
            "FINAL_FROZEN",
        ]
    )
    assert result == 5
    assert Control.effects == 1


def test_formal_init_fails_before_namespace_when_live_preflight_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(cli, "validate_formal_v11_identity", lambda _config: {})
    monkeypatch.setattr(
        cli,
        "command_formal_community_live_preflight",
        lambda _config, _run_id: {
            "ready": False,
            "blockers": ["FORMAL_CORE_CONTROL_NOT_READY"],
            "candidate_intent_persisted": False,
            "evolution_intent_persisted": False,
        },
    )
    monkeypatch.setattr(
        cli,
        "DurableTrainingControl",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("namespace created")),
    )
    assert (
        cli.main(
            [
                "formal-community-init",
                "--protocol",
                str(tmp_path / "protocol.yaml"),
                "--run-id",
                "rcb_oe_v0_community17_formal_v11",
            ]
        )
        == 5
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("formal-community-status", "status"),
        ("formal-community-verify", "verify"),
        ("formal-community-stop-owned", "stop-owned"),
    ],
)
def test_formal_community_read_only_cli_never_acquires_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected: str,
) -> None:
    _FakeCommunityControl.calls = []
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(cli, "validate_formal_v11_identity", lambda _config: {})
    monkeypatch.setattr(cli, "DurableTrainingControl", _FakeCommunityControl)
    monkeypatch.setattr(
        cli,
        "acquire_managed_core_control",
        lambda _config: (_ for _ in ()).throw(AssertionError("Core acquired")),
    )
    result = cli.main(
        [
            command,
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_community17_formal_v11",
        ]
    )
    assert result == 0
    assert _FakeCommunityControl.calls == [expected]


class _FakeOfficialControl:
    calls: ClassVar[list[str]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return {"stage": "INITIALIZED"}

    def verify(self) -> dict[str, Any]:
        self.calls.append("verify")
        return {"status": "PASS"}

    def stop_owned(self) -> dict[str, Any]:
        self.calls.append("stop-owned")
        return {"status": "PASS", "stopped": []}


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("official-frozen-status", "status"),
        ("official-frozen-verify", "verify"),
        ("official-frozen-stop-owned", "stop-owned"),
    ],
)
def test_official_read_only_cli_is_separate_and_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected: str,
) -> None:
    _FakeOfficialControl.calls = []
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(cli, "OfficialTrainingControl", _FakeOfficialControl)
    monkeypatch.setattr(
        cli,
        "acquire_managed_core_control",
        lambda _config: (_ for _ in ()).throw(AssertionError("Core acquired")),
    )
    result = cli.main(
        [
            command,
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--community-run-id",
            "rcb_oe_v0_community17_formal_v11",
            "--run-id",
            "rcb_oe_v0_official40_frozen_v11",
        ]
    )
    assert result == 0
    assert _FakeOfficialControl.calls == [expected]


def test_official_start_reacquires_and_closes_core_for_each_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(cli, "validate_formal_v11_identity", lambda _config: {})
    binds: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli,
        "bind_formal_core_identity",
        lambda **kwargs: binds.append(kwargs["authority_readiness"]),
    )

    class Authority:
        managed_host_profile_ready = True

        def __init__(self) -> None:
            self.closed = False

        def public_readiness(self) -> dict[str, Any]:
            return _core_readiness()

        def close(self) -> None:
            self.closed = True

    authorities: list[Authority] = []

    def acquire(_config: Any) -> Authority:
        authority = Authority()
        authorities.append(authority)
        return authority

    monkeypatch.setattr(cli, "acquire_managed_core_control", acquire)
    operation_tokens: list[object] = []

    def build_operations(**_kwargs: Any) -> object:
        token = object()
        operation_tokens.append(token)
        return token

    monkeypatch.setattr(cli, "build_production_official_operations", build_operations)

    class Control:
        transitions = 0

        def __init__(self, **kwargs: Any) -> None:
            self.production = kwargs["production"]
            self.operations = kwargs.get("operations")

        def initialize(self) -> dict[str, Any]:
            assert self.production is False
            return {"stage": "INITIALIZED"}

        def run_next(self) -> dict[str, Any]:
            assert self.production is True
            assert self.operations in operation_tokens
            type(self).transitions += 1
            return {
                "stage": (
                    "TASK_READY" if type(self).transitions == 1 else "COMPLETE"
                )
            }

    monkeypatch.setattr(cli, "OfficialTrainingControl", Control)
    result = cli.main(
        [
            "official-frozen-start",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--community-run-id",
            "rcb_oe_v0_community17_formal_v11",
            "--run-id",
            "rcb_oe_v0_official40_frozen_v11",
        ]
    )
    assert result == 0
    assert Control.transitions == 2
    assert len(authorities) == 2
    assert len(operation_tokens) == 2
    assert len(binds) == 2
    assert all(authority.closed for authority in authorities)


def test_official_response_loss_closes_attachment_and_resume_reacquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(cli, "validate_formal_v11_identity", lambda _config: {})
    monkeypatch.setattr(cli, "bind_formal_core_identity", lambda **_kwargs: {})

    class Authority:
        managed_host_profile_ready = True

        def __init__(self) -> None:
            self.closed = False

        def public_readiness(self) -> dict[str, Any]:
            return _core_readiness()

        def close(self) -> None:
            self.closed = True

    authorities: list[Authority] = []

    def acquire(_config: Any) -> Authority:
        authority = Authority()
        authorities.append(authority)
        return authority

    monkeypatch.setattr(cli, "acquire_managed_core_control", acquire)
    monkeypatch.setattr(
        cli,
        "build_production_official_operations",
        lambda **_kwargs: object(),
    )

    class Control:
        candidate_side_effects = 0

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_next(self) -> dict[str, Any]:
            type(self).candidate_side_effects += 1
            raise RuntimeError("synthetic response loss after sealed official Task")

        def resume(self) -> dict[str, Any]:
            return {"stage": "CANDIDATE_SEALED", "recovered_by_query": True}

    monkeypatch.setattr(cli, "OfficialTrainingControl", Control)
    common = [
        "--protocol",
        str(tmp_path / "protocol.yaml"),
        "--community-run-id",
        "rcb_oe_v0_community17_formal_v11",
        "--run-id",
        "rcb_oe_v0_official40_frozen_v11",
    ]
    assert cli.main(["official-frozen-run-next", *common]) == 2
    assert cli.main(["official-frozen-resume", *common]) == 0
    assert Control.candidate_side_effects == 1
    assert len(authorities) == 2
    assert all(authority.closed for authority in authorities)


def test_official_generation_drift_closes_attachment_before_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))

    class Authority:
        managed_host_profile_ready = True
        closed = False

        def public_readiness(self) -> dict[str, Any]:
            return _core_readiness(generation="e" * 32)

        def close(self) -> None:
            self.closed = True

    authority = Authority()
    monkeypatch.setattr(cli, "acquire_managed_core_control", lambda _config: authority)
    monkeypatch.setattr(
        cli,
        "bind_formal_core_identity",
        lambda **_kwargs: (_ for _ in ()).throw(
            FormalV11ProtocolError("FORMAL_CORE_GENERATION_OR_RELEASE_DRIFT")
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_production_official_operations",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("operations built")),
    )
    result = cli.main(
        [
            "official-frozen-run-next",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--community-run-id",
            "rcb_oe_v0_community17_formal_v11",
            "--run-id",
            "rcb_oe_v0_official40_frozen_v11",
        ]
    )
    assert result == 2
    assert authority.closed is True


def test_v11_policy_cannot_be_used_without_formal_contract(tmp_path: Path) -> None:
    base = _base_config(tmp_path)
    payload = dict(base.raw)
    payload["community_validator_failure_policy"] = {
        "enabled": True,
        "consumes_attempt": True,
        "candidate_retry_same_attempt": False,
        "judge_on_validator_failure": False,
        "reflector_on_validator_failure": True,
        "feedback_source": [
            "candidate_trace",
            "candidate_report",
            "validator_receipt",
        ],
        "official_test_enabled": False,
        "run_id_suffix": "v11",
    }
    payload["official_validator_failure_policy"] = {
        "enabled": False,
        "fail_closed": True,
        "reflector_allowed": False,
        "evolution_allowed": False,
    }
    bad = base.path.parent / "bad-v11.yaml"
    bad.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(ProtocolError):
        ExperimentConfig.load(bad)


@pytest.mark.parametrize("command", ("training-start", "run-next", "resume"))
def test_formal_v11_rejects_generic_mutating_cli_before_core_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: _FakeConfig(tmp_path))
    monkeypatch.setattr(
        cli,
        "acquire_managed_core_control",
        lambda _config: (_ for _ in ()).throw(AssertionError("Core acquired")),
    )
    result = cli.main(
        [
            command,
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_community17_formal_v11",
        ]
    )
    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "FAILED_CLOSED"
    assert "formal-community" in output["message"]
