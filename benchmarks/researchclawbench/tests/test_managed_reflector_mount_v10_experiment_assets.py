from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from openevo_researchclawbench.legacy_supervisor_attestation import (
    _read_immutable_supervisor_state,
    build_legacy_supervisor_attested_inventory,
)
from openevo_researchclawbench.training_state_store import canonical_sha256

from openevo.backend.science_successor_recovery_v1 import (
    RecoveryExecutionIdentityV1,
)

ROOT = Path("/home/lhy-h/work/researchclaw_openevo")
SCRIPTS = ROOT / "experiments/sequential_task_reflector_evolution_v0/scripts"
EXPERIMENT = ROOT / "experiments/sequential_task_reflector_evolution_v0"


def _load(name: str) -> ModuleType:
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"v10_asset_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Config:
    def __init__(self, raw: dict) -> None:
        self.raw = raw

    def require(self, dotted: str):
        value = self.raw
        for part in dotted.split("."):
            value = value[part]
        return value


def _execution() -> dict[str, object]:
    return {
        "core_generation": "0" * 32,
        "runtime_generation_digest": "1" * 64,
        "daemon_release_identity": "2" * 64,
        "release_install_digest": "3" * 64,
        "release_registry_digest": "4" * 64,
        "framework_lock_digest": "5" * 64,
        "runtime_identity_digest": "6" * 64,
        "runtime_image_digest": f"sha256:{'7' * 64}",
        "runtime_profile": "managed_science",
        "service_identity_id": "core-reference-worker",
    }


def _source() -> dict[str, object]:
    builder = _load("build_managed_reflector_mount_v10_protocol")
    inventory, attestation = builder._legacy_source_attestation()
    attestation = {
        **attestation,
        "source_transition_id": "successor-source",
        "failed_job_id": "job-source",
        "claimed_source_core_generation": "8" * 64,
        "claimed_source_release_identity": "9" * 64,
    }
    attestation.pop("content_sha256")
    attestation["content_sha256"] = canonical_sha256(attestation)
    return {
        "resolution_idempotency_key": "closed-v10.source-resolution",
        "successor_transition_id": "successor-source",
        "failed_job_id": "job-source",
        "source_runtime_generation_digest": "8" * 64,
        "source_daemon_release_identity": "9" * 64,
        "source_failure_code": "credential_mount_adoption_failed",
        "attachment_id": "attachment-source",
        "resolved_dataset_artifact_id": "artifact-source-view",
        "source_transition_attempt_capacity": 3,
        "evolution_namespace": attestation["supervisor_namespace"],
        "evolution_supervisor_stage": attestation["supervisor_observed_state"],
        "evolution_supervisor_state_sha256": attestation[
            "supervisor_state_sha256"
        ],
        "evolution_supervisor_database_sha256": attestation[
            "supervisor_database_sha256"
        ],
        "v9_canonical_tree_sha256": attestation["supervisor_tree_sha256"],
        "legacy_supervisor_attested_inventory": inventory,
        "historical_attestation": attestation,
    }


def test_v10_source_resolve_request_contains_only_closed_historical_assertions() -> None:
    module = _load("run_managed_reflector_recovery_v10")
    request = module._source_resolve_request(_Config({"v10_recovery": {"source": _source()}}))

    assert request.successor_transition_id == "successor-source"
    assert request.failed_job_id == "job-source"
    assert request.resolution_idempotency_key == "closed-v10.source-resolution"
    assert request.attachment_ids == ("attachment-source",)
    assert request.transition_attempt_capacity == 3
    assert request.historical_attestation.provenance_tier == (
        "legacy_supervisor_attested"
    )
    assert request.historical_attestation.core_native is False
    assert request.historical_attestation.authority_minted is False
    assert request.model_fields_set == {
        "resolution_idempotency_key",
        "successor_transition_id",
        "failed_job_id",
        "historical_attestation",
        "attachment_ids",
        "resolved_dataset_artifact_id",
        "transition_attempt_capacity",
    }
    assert not hasattr(request, "source_core_generation")
    assert not hasattr(request, "source_release_identity")
    assert not hasattr(request, "failure_code")
    assert "plan" not in request.model_fields_set
    assert "source" not in request.model_fields_set


def test_v10_typed_legacy_attestation_excludes_v7_v8_auxiliary() -> None:
    builder = _load("build_managed_reflector_mount_v10_protocol")

    inventory, attestation = builder._legacy_source_attestation()

    assert inventory["provenance"]["tier"] == "legacy_supervisor_attested"
    assert inventory["provenance"]["core_native"] is False
    labels = tuple(item["label"] for item in attestation["evidence_inventory"])
    assert labels == (
        "core_failure_authority",
        "framework_lock",
        "legacy_supervisor_inventory",
        "release_bundle_manifest",
        "terminal_classification",
    )
    assert all("v7" not in label and "v8" not in label for label in labels)
    evidence = {item["label"]: item["sha256"] for item in attestation["evidence_inventory"]}
    assert evidence["legacy_supervisor_inventory"] == inventory["content_sha256"]
    assert attestation["provenance_tier"] == "legacy_supervisor_attested"
    assert attestation["core_native"] is False
    assert attestation["authority_minted"] is False
    assert attestation["supervisor_observed_state"] == "EVOLUTION_RUNNING"
    assert "supervisor_terminal_state" not in attestation
    assert attestation["legacy_supervisor_inventory_sha256"] == (
        inventory["content_sha256"]
    )
    assert attestation["terminal_classification_sha256"] == evidence[
        "terminal_classification"
    ]


def test_v10_execution_identity_separates_daemon_release_and_install() -> None:
    module = _load("run_managed_reflector_recovery_v10")
    identity = module._execution_identity(
        _Config({"v10_recovery": {"execution": _execution()}})
    )

    assert type(identity) is RecoveryExecutionIdentityV1
    assert identity.daemon_release_identity == "2" * 64
    assert identity.release_install_digest == "3" * 64
    assert identity.daemon_release_identity != identity.release_install_digest
    assert identity.max_reflector_model_calls == 1


def test_v10_receipt_writer_rejects_secret_shaped_fields(tmp_path: Path) -> None:
    module = _load("run_managed_reflector_recovery_v10")

    with pytest.raises(RuntimeError, match="secret-shaped"):
        module._write_new_json(tmp_path / "bad.json", {"bearer": "not-recorded"})

    assert not (tmp_path / "bad.json").exists()


def test_v10_scope_authorizes_only_agent_system_and_one_model_call() -> None:
    builder = (SCRIPTS / "build_managed_reflector_mount_v10_protocol.py").read_text()
    driver = (SCRIPTS / "run_managed_reflector_recovery_v10.py").read_text()

    assert '"authorized_targets_this_run": ["agent_system"]' in builder
    assert '"max_reflector_model_calls": 1' in builder
    assert '"automatic_target_progression": False' in builder
    assert '"automatic_successor_commit": False' in builder
    assert "run-agent-system" in driver
    assert "run-text-memory" not in driver
    assert "run-skill-bundle" not in driver
    assert "candidate_execution_allowed=False" in driver


def test_v10_readiness_export_waits_for_the_bounded_core_adoption_probe() -> None:
    exporter = (
        SCRIPTS / "export_managed_reflector_mount_v10_readiness.py"
    ).read_text(encoding="utf-8")

    assert "acquire_managed_core_control(config, deadline_seconds=240.0)" in exporter
    assert 'client.json("GET", ENDPOINT, timeout_seconds=150.0)' in exporter


def test_v10_active_asset_scripts_pin_v29_release_and_lifecycle_27() -> None:
    protocol_builder = _load("build_managed_reflector_mount_v10_protocol")
    deployer = _load("deploy_managed_core_v10")
    deployer_source = (
        SCRIPTS / "deploy_managed_core_v10.py"
    ).read_text(encoding="utf-8")
    refresher = (
        SCRIPTS / "refresh_managed_reflector_mount_v10_identity.py"
    ).read_text(encoding="utf-8")
    publisher = (
        SCRIPTS / "publish_managed_reflector_mount_v10_readiness.py"
    ).read_text(encoding="utf-8")

    assert protocol_builder.DEFAULT_FRAMEWORK_LOCK.name == "framework-lock.json"
    assert protocol_builder.DEFAULT_FRAMEWORK_LOCK.parent.parent.name == (
        "release-v10-v29"
    )
    assert protocol_builder.DEFAULT_FRAMEWORK_LOCK.parent.name == "core"
    assert protocol_builder.DEFAULT_DAEMON_ROOT.parent.name == "release-v10-v29"
    assert protocol_builder.DEFAULT_DAEMON_ROOT.name == "daemon"
    assert deployer.BUNDLE_ROOT.parent.name == "release-v10-v29"
    assert deployer.BUNDLE_ROOT.name == "daemon"
    assert deployer.PREDECESSOR_BUNDLE_ROOT.parent.name == "release-v10-v28"
    assert deployer.PREDECESSOR_BUNDLE_ROOT.name == "daemon"
    assert deployer.PREDECESSOR_BUNDLE_ROOT != deployer.BUNDLE_ROOT
    assert "status.lifecycle_compatibility != 27" in deployer_source
    assert 'deployment.get("lifecycle_compatibility") != 27' in (
        (SCRIPTS / "build_managed_reflector_mount_v10_protocol.py").read_text(
            encoding="utf-8"
        )
    )
    assert '"lifecycle_compatibility") != 27' in refresher
    assert '"daemon_lifecycle_compatibility": 27' in refresher
    assert '"lifecycle_compatibility") != 27' in publisher
    assert '"daemon_lifecycle_compatibility": 27' in publisher


def test_v10_no_model_readiness_assets_pin_bootstrap_v30_and_receipts_v29() -> None:
    bootstrap_builder = _load("build_managed_reflector_mount_v10_bootstrap")
    exporter = _load("export_managed_reflector_mount_v10_readiness")
    exporter_source = (
        SCRIPTS / "export_managed_reflector_mount_v10_readiness.py"
    ).read_text(encoding="utf-8")
    protocol_builder = (
        SCRIPTS / "build_managed_reflector_mount_v10_protocol.py"
    ).read_text(encoding="utf-8")
    publisher = (
        SCRIPTS / "publish_managed_reflector_mount_v10_readiness.py"
    ).read_text(encoding="utf-8")

    assert bootstrap_builder.TARGET.name == (
        "managed_reflector_mount_v10_bootstrap_v30.yaml"
    )
    assert bootstrap_builder.CORE_ROOT.parent.name == "release-v10-v29"
    assert bootstrap_builder.CORE_ROOT.name == "core"
    assert bootstrap_builder.DAEMON_ROOT.parent.name == "release-v10-v29"
    assert bootstrap_builder.DAEMON_ROOT.name == "daemon"
    assert exporter.BOOTSTRAP == bootstrap_builder.TARGET
    for source in (protocol_builder, exporter_source, publisher):
        assert "daemon_deploy_v10_v29.json" in source
        assert "managed_reflector_credential_mount_readiness_v29.json" in source
    for source in (protocol_builder, exporter_source):
        assert "managed_reflector_execution_readiness_v29.json" in source


def test_v10_protocol_builder_closes_release_assets_against_deployment(
    tmp_path: Path,
) -> None:
    builder = _load("build_managed_reflector_mount_v10_protocol")
    framework_lock = tmp_path / "framework-lock.json"
    bundle = tmp_path / "openevo-daemon-linux-x86_64"
    manifest = tmp_path / "openevo-daemon-bundle.json"
    framework_lock.write_bytes(b"framework")
    bundle.write_bytes(b"bundle")
    release_identity = "a" * 64
    registry_digest = "b" * 64
    manifest.write_text(
        json.dumps(
            {
                "artifact": {
                    "filename": bundle.name,
                    "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    "size": bundle.stat().st_size,
                },
                "build_environment_distributions": [],
                "core": {
                    "framework_lock": {
                        "filename": framework_lock.name,
                        "sha256": hashlib.sha256(
                            framework_lock.read_bytes()
                        ).hexdigest(),
                    },
                    "registry_digest": registry_digest,
                    "wheel": {},
                },
                "dependency_lock": {},
                "platform": {"architecture": "x86_64", "system": "linux"},
                "release": {
                    "identity": release_identity,
                    "source_commit": "3a7ee037b4106451aa47d1fcdedb148b2bc19160",
                },
                "runtime": {},
                "schema_version": 1,
                "smoke": {
                    "backend_readiness": "passed",
                    "controlled_exit": "passed",
                    "identity": "passed",
                },
            }
        ),
        encoding="utf-8",
    )
    deployment = {
        "bundle_path": str(bundle),
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "manifest_path": str(manifest),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "registry_digest": registry_digest,
        "release_identity": release_identity,
        "source_commit": "3a7ee037b4106451aa47d1fcdedb148b2bc19160",
    }

    builder._require_deployment_assets(
        deployment,
        framework_lock=framework_lock,
        daemon_bundle=bundle,
        daemon_manifest=manifest,
    )
    deployment["bundle_path"] = str(tmp_path / "different-release")
    with pytest.raises(RuntimeError, match="does not bind"):
        builder._require_deployment_assets(
            deployment,
            framework_lock=framework_lock,
            daemon_bundle=bundle,
            daemon_manifest=manifest,
        )
    deployment["bundle_path"] = str(bundle)
    deployment["release_identity"] = "c" * 64
    with pytest.raises(RuntimeError, match="manifest identity differs"):
        builder._require_deployment_assets(
            deployment,
            framework_lock=framework_lock,
            daemon_bundle=bundle,
            daemon_manifest=manifest,
        )


def test_v10_runner_uses_core_resolver_and_never_builds_source_authority() -> None:
    driver = (SCRIPTS / "run_managed_reflector_recovery_v10.py").read_text()

    assert 'SOURCE_RESOLVE_PATH = RECOVERY_BASE + "/sources/resolve"' in driver
    assert "ScienceSuccessorRecoverySourceResponseV1.model_validate" in driver
    assert "ExhaustedSuccessorRecoveryAuthorityV1(" not in driver
    assert "subprocess" not in driver
    assert "codex exec" not in driver


def test_v10_launch_is_fail_closed_between_create_and_paid_target() -> None:
    launcher = (SCRIPTS / "launch_managed_reflector_recovery_v10.sh").read_text()

    assert "set -Eeuo pipefail" in launcher
    assert '"$PY" "$DRIVER" preflight' in launcher
    assert '"$PY" "$DRIVER" create' in launcher
    assert '"$PY" "$DRIVER" run-agent-system' in launcher
    assert launcher.index('"$PY" "$DRIVER" preflight') < launcher.index(
        '"$PY" "$DRIVER" create'
    )
    assert launcher.index('"$PY" "$DRIVER" create') < launcher.index(
        '"$PY" "$DRIVER" run-agent-system'
    )
    assert "source judge.env" not in launcher
    assert "codex" not in launcher.lower()


def test_v10_watcher_is_read_only() -> None:
    watcher = (SCRIPTS / "watch_managed_reflector_recovery_v10.sh").read_text()

    assert '"$PY" "$DRIVER" status' in watcher
    assert '"$PY" "$DRIVER" verify' in watcher
    assert "run-agent-system" not in watcher
    assert " create" not in watcher
    assert "resume" not in watcher
    assert "stop-owned" not in watcher


def test_v10_tmux_launcher_strips_core_and_judge_credentials() -> None:
    launcher = (SCRIPTS / "start_managed_reflector_recovery_v10_tmux.sh").read_text()

    assert "tmux kill-server" not in launcher
    assert "tmux kill-session" not in launcher
    assert "unset OPENEVO_CORE_CONTROL_BEARER JUDGE_API_KEY" in launcher
    assert launcher.index("unset OPENEVO_CORE_CONTROL_BEARER") < launcher.index(
        "tmux -L"
    )
    assert launcher.index('"$OWNERSHIP" intent') < launcher.index("tmux -L")
    assert launcher.index('"$OWNERSHIP" intent') < launcher.index(
        'tmux -L "$socket_name" new-session'
    )
    assert launcher.index("--step runner_dispatch_intent") < launcher.index(
        'tmux -L "$socket_name" respawn-pane -k -t "$runner_pane_id"'
    )
    assert all(
        " -L \"$socket_name\"" in line
        for line in launcher.splitlines()
        if line.lstrip().startswith("tmux ")
    )
    assert "-u OPENEVO_CORE_CONTROL_BEARER" in launcher
    assert "-u JUDGE_API_KEY" in launcher
    assert "rcb_oe_reflector_mount_v10" in launcher


def _tmux_ownership_fixture(tmp_path: Path, monkeypatch):
    module = _load("managed_reflector_v10_tmux_ownership")
    root = tmp_path / "project"
    experiment = root / "experiments/sequential_task_reflector_evolution_v0"
    log_root = experiment / "logs/v10-owned"
    protocol = experiment / "protocol/managed_reflector_credential_mount_v10_revision6.yaml"
    active_identity = experiment / "protocol/active_protocol_identity.json"
    runner = experiment / "scripts/launch.sh"
    watcher = experiment / "scripts/watch.sh"
    for directory in (log_root, protocol.parent, runner.parent):
        directory.mkdir(parents=True, exist_ok=True)
    protocol.write_text(
        yaml.safe_dump(
            {"v10_recovery": {"namespace": "v10_namespace_static_test"}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    active_identity.write_text(
        json.dumps(
            {
                "protocol_path": str(protocol.resolve()),
                "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    watcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "EXPERIMENT", experiment)
    return module, log_root, protocol, active_identity, runner, watcher


def test_v10_tmux_launch_intent_precedes_resources_and_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module, log_root, protocol, active_identity, runner, watcher = (
        _tmux_ownership_fixture(tmp_path, monkeypatch)
    )
    kwargs = {
        "log_root": log_root,
        "session": "rcb_oe_reflector_mount_v10",
        "socket_name": "rcb_oe_reflector_mount_v10_static_socket",
        "protocol": protocol,
        "active_identity": active_identity,
        "runner": runner,
        "watcher": watcher,
    }

    first = module.create_launch_intent(
        **kwargs,
        created_at="2026-07-29T00:00:00Z",
    )
    replay = module.create_launch_intent(
        **kwargs,
        created_at="2026-07-29T00:01:00Z",
    )

    assert replay == first
    assert first["namespace"] == "v10_namespace_static_test"
    assert first["run_identity"] == first["namespace"]
    assert first["tmux_socket"] == {
        "mode": "dedicated_named_socket",
        "name": "rcb_oe_reflector_mount_v10_static_socket",
    }
    assert first["secret_recorded"] is False
    intent = log_root / "tmux_launch_intent.json"
    assert stat.S_IMODE(intent.stat().st_mode) == 0o600
    source = (
        SCRIPTS / "managed_reflector_v10_tmux_ownership.py"
    ).read_text(encoding="utf-8")
    assert "os.O_EXCL" in source
    assert "os.O_NOFOLLOW" in source
    assert source.count("os.fsync(") >= 2


def test_v10_tmux_crash_window_is_append_only_and_never_infers_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module, log_root, protocol, active_identity, runner, watcher = (
        _tmux_ownership_fixture(tmp_path, monkeypatch)
    )
    intent = module.create_launch_intent(
        log_root=log_root,
        session="rcb_oe_reflector_mount_v10",
        socket_name="rcb_oe_reflector_mount_v10_crash_socket",
        protocol=protocol,
        active_identity=active_identity,
        runner=runner,
        watcher=watcher,
        created_at="2026-07-29T00:00:00Z",
    )

    module.append_observation(
        log_root=log_root,
        launch_id=intent["launch_id"],
        step="session_created",
        observation={
            "session_id": "$1",
            "window_name": "runner",
            "window_id": "@1",
            "pane_id": "%1",
            "pane_pid": 101,
            "command_state": "placeholder",
        },
        observed_at="2026-07-29T00:00:01Z",
    )
    failure = module.append_failure(
        log_root=log_root,
        launch_id=intent["launch_id"],
        failed_step="session_created",
        exit_code=17,
        failed_at="2026-07-29T00:00:02Z",
    )

    assert failure["cleanup_attempted"] is False
    assert failure["broad_termination_used"] is False
    assert not (log_root / "tmux_metadata.json").exists()
    with pytest.raises(RuntimeError, match="unsafe ownership evidence path"):
        module.append_observation(
            log_root=log_root,
            launch_id=intent["launch_id"],
            step="watch_dispatch_intent",
            observation={
                "session_id": "$1",
                "window_name": "watch",
                "window_id": "@2",
                "pane_id": "%2",
                "script_sha256": "a" * 64,
            },
        )


def test_v10_tmux_observation_chain_and_final_metadata_are_stable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module, log_root, protocol, active_identity, runner, watcher = (
        _tmux_ownership_fixture(tmp_path, monkeypatch)
    )
    intent = module.create_launch_intent(
        log_root=log_root,
        session="rcb_oe_reflector_mount_v10",
        socket_name="rcb_oe_reflector_mount_v10_chain_socket",
        protocol=protocol,
        active_identity=active_identity,
        runner=runner,
        watcher=watcher,
        created_at="2026-07-29T00:00:00Z",
    )
    runner_identity = {
        "session_id": "$1",
        "window_name": "runner",
        "window_id": "@1",
        "pane_id": "%1",
    }
    watch_identity = {
        "session_id": "$1",
        "window_name": "watch",
        "window_id": "@2",
        "pane_id": "%2",
    }
    observations = (
        (
            "session_created",
            {**runner_identity, "pane_pid": 101, "command_state": "placeholder"},
        ),
        (
            "watch_window_created",
            {**watch_identity, "pane_pid": 102, "command_state": "placeholder"},
        ),
        ("watch_dispatch_intent", {**watch_identity, "script_sha256": "a" * 64}),
        (
            "watch_dispatched",
            {**watch_identity, "pane_pid": 103, "command_state": "watcher"},
        ),
        ("runner_dispatch_intent", {**runner_identity, "script_sha256": "b" * 64}),
        (
            "runner_dispatched",
            {**runner_identity, "pane_pid": 104, "command_state": "runner"},
        ),
    )
    for index, (step, observation) in enumerate(observations, start=1):
        module.append_observation(
            log_root=log_root,
            launch_id=intent["launch_id"],
            step=step,
            observation=observation,
            observed_at=f"2026-07-29T00:00:{index:02d}Z",
        )
    metadata = module.finalize_metadata(
        log_root=log_root,
        launch_id=intent["launch_id"],
        finalized_at="2026-07-29T00:01:00Z",
    )
    replay = module.finalize_metadata(
        log_root=log_root,
        launch_id=intent["launch_id"],
        finalized_at="2026-07-29T00:02:00Z",
    )

    assert replay == metadata
    assert metadata["runner"]["pane_id"] == "%1"
    assert metadata["watch"]["pane_id"] == "%2"
    assert metadata["secret_recorded"] is False
    assert len(metadata["observation_receipt_sha256"]) == 6


def test_v10_dedicated_tmux_server_and_pane_do_not_inherit_secrets(
    tmp_path: Path,
) -> None:
    socket_name = "rcb_oe_v10_test_" + hashlib.sha256(
        str(tmp_path).encode()
    ).hexdigest()[:16]
    session = "rcb_oe_v10_secret_probe"
    pane_environment = tmp_path / "pane.env"
    injected = {
        "OPENEVO_CORE_CONTROL_BEARER": "sentinel-core-bearer",
        "JUDGE_API_KEY": "sentinel-judge-key",
        "JUDGE_API_BASE": "sentinel-judge-base",
        "JUDGE_MODEL_NAME": "sentinel-judge-model",
    }
    environment = {**os.environ, **injected}
    pane_command = (
        "env -u OPENEVO_CORE_CONTROL_BEARER -u JUDGE_API_KEY "
        "-u JUDGE_API_BASE -u JUDGE_MODEL_NAME sh -c "
        + shlex.quote(
            f"env > {shlex.quote(str(pane_environment))}; exec sleep 30"
        )
    )
    launch = (
        "unset OPENEVO_CORE_CONTROL_BEARER JUDGE_API_KEY "
        "JUDGE_API_BASE JUDGE_MODEL_NAME; "
        f"exec tmux -L {shlex.quote(socket_name)} new-session -d "
        f"-s {shlex.quote(session)} -n probe {shlex.quote(pane_command)}"
    )
    try:
        subprocess.run(["bash", "-c", launch], check=True, env=environment)
        global_environment = subprocess.run(
            ["tmux", "-L", socket_name, "show-environment", "-g"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for _ in range(50):
            if pane_environment.is_file():
                break
            time.sleep(0.02)
        assert pane_environment.is_file()
        pane_value = pane_environment.read_text(encoding="utf-8")
        for name, sentinel in injected.items():
            assert name not in global_environment
            assert sentinel not in global_environment
            assert name not in pane_value
            assert sentinel not in pane_value
    finally:
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def test_v10_protocol_builder_pins_mount_and_source_identity_layers() -> None:
    builder = (SCRIPTS / "build_managed_reflector_mount_v10_protocol.py").read_text()

    for field in (
        "source_runtime_generation_digest",
        "source_daemon_release_identity",
        "daemon_release_identity",
        "release_install_digest",
        "runtime_generation_digest",
        "credential_mount_daemon_release_identity",
        "resolved_dataset_artifact_id",
    ):
        assert field in builder
    assert "current_adapter_identity" not in builder


def test_v10_immutable_supervisor_readback_does_not_create_sqlite_sidecars(
    tmp_path: Path,
) -> None:
    module = _load("run_managed_reflector_recovery_v10")
    database = tmp_path / "training-supervisor.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE experiment_state ("
            "experiment_id TEXT PRIMARY KEY, stage TEXT NOT NULL, "
            "state_sha256 TEXT NOT NULL, revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO experiment_state VALUES (?, ?, ?, ?)",
            ("sealed-source", "BLOCKED", "a" * 64, 7),
        )
        connection.commit()
    finally:
        connection.close()
    before = module._sha256_file(database)

    authority = module._read_supervisor_state_immutable(
        database,
        namespace="sealed-source",
    )

    assert authority == {
        "namespace": "sealed-source",
        "stage": "BLOCKED",
        "state_sha256": "a" * 64,
        "revision": 7,
        "supervisor_database_sha256": before,
    }
    assert module._sha256_file(database) == before
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


def test_v10_protocol_separates_historical_auxiliary_from_v9_authority() -> None:
    builder = (SCRIPTS / "build_managed_reflector_mount_v10_protocol.py").read_text()

    assert '"historical_auxiliary"' in builder
    assert builder.count('"historical_auxiliary_non_authoritative"') == 2
    assert builder.count('"logical_state_read": False') == 2
    assert builder.count('"sidecars_present": True') == 2
    assert '"evolution_supervisor_database_sha256"' in builder
    assert '"evolution_supervisor_state_sha256"' in builder
    assert '"evolution_supervisor_stage": "EVOLUTION_RUNNING"' in builder
    assert "mode=ro&immutable=1" in (
        SCRIPTS / "run_managed_reflector_recovery_v10.py"
    ).read_text()
    assert '"canonical_tree_hash_algorithm"' in builder
    assert '"historical_baseline_hash_method": "unknown"' in builder
    assert '"historical_baseline_hash_authority": "non_authoritative"' in builder
    assert '"v9_canonical_tree_sha256"' in builder
    assert '"v9_historical_baseline_tree_sha256"' in builder


def _legacy_inventory_inputs() -> dict[str, object]:
    log_root = (
        EXPERIMENT
        / "logs/community_validator_learning_v9_replacement4_20260729T012211Z"
    )
    return {
        "experiment_root": EXPERIMENT,
        "supervisor_namespace": (
            "rcb_oe_v0_astronomy004_validator_learning_v9_20260729T012211Z"
        ),
        "expected_supervisor_stage": "EVOLUTION_RUNNING",
        "expected_supervisor_state_sha256": (
            "223aa9c691025dc678838fb8322f8c2fea20f503137ec72b288c79a29e1448de"
        ),
        "expected_supervisor_database_sha256": (
            "5cd02cc306a85793ac1956a2a9527b3b5323bd3887d093e5a374f26bed2c04a4"
        ),
        "expected_supervisor_tree_sha256": (
            "71eb3c8d66bd9333142078580aaa9616382576259d2ea9d8fab243558593f067"
        ),
        "expected_supervisor_files_sha256": {
            "composites/510838231de602675543e08a514f97e6b3e107c9f8ec1bdefcd218351827af00.json": (
                "adf7c18a7e83ecb114a2bb084573ff5aa8a2f01c032ca62967a69a369b96697b"
            ),
            "operations/99d6bd92ea35ab801fa94ea33eb97ed4f7a1fa2b04c2b58e2da179045b165ea0.json": (
                "4c63e2928402cebe632015b53c18f6479f231628994d0bc21df4c4089c70a5d3"
            ),
            "receipts/13df4d4a0609ca4034aac82c68fb1f2f8684528fd8cac91e398320d0a18ce233.json": (
                "992b51673bd747cbca625859741c2f2a539498fa8f501040c14d4aeacb1f4efa"
            ),
            "receipts/637079bb0874115afcf82c8bbbc0b95469754ff11c48ad26336d82ac2bf13afe.json": (
                "17664e5b01518158462467e9303afd62248e06840f60210198c989ca3d58d39b"
            ),
            "receipts/915502c96930e7995f47e49b41da73ec71d6f8b1521c5d2660245b2d5e5fc9fa.json": (
                "c9c40882142ad81f397e3d81036967aa641aa32418771a51d85cb6cca5a2aa88"
            ),
            "receipts/af591610c4c0fbed1abe1fbf4726082ccf04e57cb329c4c2e921f036b819c81c.json": (
                "ffc1482126f5e310b7f2b7e151c94c106aab95dafbdca79bd6c029bdf9374cfb"
            ),
            "receipts/ddc753e1a65c8fdfed4ff043130bfae39442fa785a929da6d166c0b3b2071e6d.json": (
                "d6b6b3d38b1adf727f1b2cedc32e6fd2c089abea2369fd555cd41bcae053cea6"
            ),
            "validator-learning-post-feedback-initialization.json": (
                "ab5555401f0338a7293ee8ef403632fdc77033357910afb47ca22d1dcb7fdb0f"
            ),
            "validator-learning-post-feedback-replacement.json": (
                "d7a3481df6623f252893575f980ed6f1606d1562ee9ecc4d7eaa77cb51ab539c"
            ),
        },
        "historical_baseline_tree_sha256": (
            "13ea58f6fe00a1a1b1678a3acd6c5fe21fcc3fc65f609948173a586de2e43592"
        ),
        "evidence_files": {
            "core_failure_authority": log_root / "core_failure_authority.json",
            "terminal_classification": log_root / "terminal_stop_classification.json",
            "release_bundle_manifest": (
                EXPERIMENT
                / "runtime/daemon-release-docker2952-v7-v18-20260728T200057Z/"
                "openevo-daemon-bundle.json"
            ),
            "framework_lock": (
                EXPERIMENT
                / "runtime/core-release-v7-v18-20260728T200022Z/framework-lock.json"
            ),
        },
        "expected_evidence_sha256": {
            "core_failure_authority": (
                "5f751c4c7650e1b0c4989eaf18e5b5ccd70a05b96fc8e0c01363a95836709aff"
            ),
            "terminal_classification": (
                "659e0f64b659f324ca401ba73d0725c12c6dafdad3dcc3a35083734672d33565"
            ),
            "release_bundle_manifest": (
                "f8d7d068f2bb56bb064a10db3e5ab1a6e8b13194b4733fc7932a1b7b905229ec"
            ),
            "framework_lock": (
                "ece4afdef2dff28371c6497fdcc690daac53f06c5c848cb1609bd31c2804c4ce"
            ),
        },
        "expected_core_cross_check": {
            "successor_transition_id": "successor-83fa61502cfbc5039866a4b4e37deb61",
            "failed_job_id": "job_20fb2d86d876424a",
            "dataset_id": (
                "ds-f54a6b8119fd8f8e2e80e9b4d07160e4c96009e09f98cf0635f21a6f2a031ed7"
            ),
            "dataset_revision": "art_f9e44bad46f24000.v1",
            "attachment_id": "tfa_0ff887d8e19774871ffa35527441b340",
            "attachment_sha256": (
                "70204ed48193ba227beabb49d544246f7a64cd183d68db2288282b138c399135"
            ),
            "resolved_dataset_artifact_id": "art_574286090a0f4825",
            "resolved_view_sha256": (
                "29c3c4d6f56854de699768cd240e2ad6c0330e4214fd9841ca5ea94104bf5622"
            ),
        },
    }


def test_v10_legacy_supervisor_attestation_is_closed_and_content_addressed() -> None:
    inventory = build_legacy_supervisor_attested_inventory(
        **_legacy_inventory_inputs()
    )

    assert inventory["schema_version"].endswith("legacy_supervisor_attested.v1")
    assert inventory["provenance"] == {
        "tier": "legacy_supervisor_attested",
        "core_native": False,
        "authority_minted": False,
        "requires_core_resolution": True,
    }
    assert inventory["supervisor"]["stage"] == "EVOLUTION_RUNNING"
    assert inventory["supervisor"]["database"]["sha256_before"] == (
        inventory["supervisor"]["database"]["sha256_after"]
    )
    assert set(inventory["evidence_files"]) == {
        "core_failure_authority",
        "terminal_classification",
        "release_bundle_manifest",
        "framework_lock",
    }
    assert all(
        not item["experiment_relative_path"].startswith("/")
        for item in inventory["evidence_files"].values()
    )
    canonical_tree = inventory["supervisor"]["canonical_tree"]
    assert canonical_tree["algorithm"] == "openevo-reconciliation-source-tree-v1"
    assert canonical_tree["sha256"] == (
        "71eb3c8d66bd9333142078580aaa9616382576259d2ea9d8fab243558593f067"
    )
    assert canonical_tree["file_count"] == 9
    assert len(canonical_tree["files"]) == 9
    historical = inventory["supervisor"]["historical_baseline"]
    assert historical == {
        "sha256": (
            "13ea58f6fe00a1a1b1678a3acd6c5fe21fcc3fc65f609948173a586de2e43592"
        ),
        "method": "unknown",
        "authority": "non_authoritative",
        "preserved_unchanged": True,
        "equivalent_to_canonical_tree": False,
    }
    assert inventory["expected_core_cross_check"]["core_native"] is False
    payload = dict(inventory)
    content_sha256 = payload.pop("content_sha256")
    assert content_sha256 == canonical_sha256(payload)


def test_v10_legacy_attestation_rejects_evidence_outside_experiment_root() -> None:
    inputs = _legacy_inventory_inputs()
    evidence = dict(inputs["evidence_files"])
    evidence["framework_lock"] = Path("/etc/hosts")
    inputs["evidence_files"] = evidence

    with pytest.raises(ValueError, match="outside the experiment root"):
        build_legacy_supervisor_attested_inventory(**inputs)


def test_v10_legacy_attestation_rejects_non_sqlite_file_inventory_drift() -> None:
    inputs = _legacy_inventory_inputs()
    inventory = dict(inputs["expected_supervisor_files_sha256"])
    inventory["validator-learning-post-feedback-replacement.json"] = "0" * 64
    inputs["expected_supervisor_files_sha256"] = inventory

    with pytest.raises(ValueError, match="differs from its attestation"):
        build_legacy_supervisor_attested_inventory(**inputs)


def test_legacy_attestation_accepts_exact_pre_job_cross_check() -> None:
    inputs = _legacy_inventory_inputs()
    cross_check = dict(inputs["expected_core_cross_check"])
    failed_job_id = cross_check.pop("failed_job_id")
    cross_check["failed_pre_job_operation_id"] = failed_job_id
    inputs["expected_core_cross_check"] = cross_check
    inputs["allow_stable_empty_wal_sidecars"] = True

    inventory = build_legacy_supervisor_attested_inventory(**inputs)

    values = inventory["expected_core_cross_check"]["values"]
    assert values["failed_pre_job_operation_id"] == failed_job_id
    assert "failed_job_id" not in values


def test_legacy_attestation_rejects_ambiguous_failed_source_cross_check() -> None:
    inputs = _legacy_inventory_inputs()
    inputs["expected_core_cross_check"] = {
        **inputs["expected_core_cross_check"],
        "failed_pre_job_operation_id": "successor-attempt-source",
    }

    with pytest.raises(ValueError, match="cross-check inventory is not closed"):
        build_legacy_supervisor_attested_inventory(**inputs)


def test_immutable_supervisor_read_accepts_only_stable_empty_wal_sidecars(
    tmp_path: Path,
) -> None:
    namespace = "source-namespace"
    database = tmp_path / "training-supervisor.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE experiment_state ("
            "experiment_id TEXT PRIMARY KEY, stage TEXT NOT NULL, "
            "state_sha256 TEXT NOT NULL, revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO experiment_state VALUES (?, ?, ?, ?)",
            (namespace, "EVOLUTION_RUNNING", "1" * 64, 8),
        )
        connection.commit()
    finally:
        connection.close()
    Path(str(database) + "-wal").write_bytes(b"")
    Path(str(database) + "-shm").write_bytes(b"closed-shm")

    state = _read_immutable_supervisor_state(
        database=database,
        namespace=namespace,
        allow_stable_empty_wal_sidecars=True,
    )

    assert state["stage"] == "EVOLUTION_RUNNING"
    assert state["stable_empty_wal_sidecars"]["wal"]["size_bytes"] == 0
    assert state["stable_empty_wal_sidecars"]["shm"]["size_bytes"] == 10

    Path(str(database) + "-wal").write_bytes(b"not-empty")
    with pytest.raises(ValueError, match="WAL is not empty"):
        _read_immutable_supervisor_state(
            database=database,
            namespace=namespace,
            allow_stable_empty_wal_sidecars=True,
        )


def test_v10_runner_reconciles_historical_and_canonical_tree_hashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load("run_managed_reflector_recovery_v10")
    definitions = {
        "v7": ("candidate-source", "BLOCKED", "1" * 64, "a" * 64),
        "v8": ("validator-source", "FAILED", "2" * 64, "b" * 64),
        "v9": ("evolution-source", "EVOLUTION_RUNNING", "3" * 64, "c" * 64),
    }
    values: dict[str, dict[str, object]] = {}
    for version, (namespace, stage, state_sha256, historical) in definitions.items():
        root = tmp_path / "supervisor" / namespace
        root.mkdir(parents=True)
        (root / "sealed-receipt.json").write_text(
            f'{{"source":"{version}"}}\n', encoding="utf-8"
        )
        database = root / "training-supervisor.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE experiment_state ("
                "experiment_id TEXT PRIMARY KEY, stage TEXT NOT NULL, "
                "state_sha256 TEXT NOT NULL, revision INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO experiment_state VALUES (?, ?, ?, ?)",
                (namespace, stage, state_sha256, 1),
            )
            connection.commit()
        finally:
            connection.close()
        auxiliary_files = None
        if version in {"v7", "v8"}:
            (root / "training-supervisor.sqlite3-wal").write_bytes(b"")
            (root / "training-supervisor.sqlite3-shm").write_bytes(b"shm")
            auxiliary_files = {
                "database": module._historical_file_identity(database, root=root),
                "wal": module._historical_file_identity(
                    root / "training-supervisor.sqlite3-wal", root=root
                ),
                "shm": module._historical_file_identity(
                    root / "training-supervisor.sqlite3-shm", root=root
                ),
            }
        values[version] = {
            "database": module._sha256_file(database),
            "tree": module._tree_sha256(root),
            "historical": historical,
            "files": auxiliary_files,
        }
    monkeypatch.setattr(module, "EXPERIMENT", tmp_path)
    config = _Config(
        {
            "v10_recovery": {
                "source": {
                    "historical_auxiliary": {
                        "v7": {
                            "namespace": "candidate-source",
                            "authority": (
                                "historical_auxiliary_non_authoritative"
                            ),
                            "logical_state_read": False,
                            "sidecars_present": True,
                            "canonical_tree_sha256": values["v7"]["tree"],
                            "historical_baseline_tree_sha256": values["v7"][
                                "historical"
                            ],
                            "historical_baseline_hash_method": "unknown",
                            "files": values["v7"]["files"],
                        },
                        "v8": {
                            "namespace": "validator-source",
                            "authority": (
                                "historical_auxiliary_non_authoritative"
                            ),
                            "logical_state_read": False,
                            "sidecars_present": True,
                            "canonical_tree_sha256": values["v8"]["tree"],
                            "historical_baseline_tree_sha256": values["v8"][
                                "historical"
                            ],
                            "historical_baseline_hash_method": "unknown",
                            "files": values["v8"]["files"],
                        },
                    },
                    "evolution_namespace": "evolution-source",
                    "evolution_supervisor_stage": "EVOLUTION_RUNNING",
                    "evolution_supervisor_state_sha256": "3" * 64,
                    "evolution_supervisor_database_sha256": values["v9"]["database"],
                    "canonical_tree_hash_algorithm": (
                        "openevo-reconciliation-source-tree-v1"
                    ),
                    "historical_baseline_hash_method": "unknown",
                    "historical_baseline_hash_authority": "non_authoritative",
                    "v9_canonical_tree_sha256": values["v9"]["tree"],
                    "v9_historical_baseline_tree_sha256": values["v9"][
                        "historical"
                    ],
                }
            }
        }
    )

    observed = module._verify_immutable_sources(config)

    assert observed["v9"]["canonical_tree_sha256"] == values["v9"]["tree"]
    assert observed["v9"]["historical_baseline"] == {
        "sha256": "c" * 64,
        "method": "unknown",
        "authority": "non_authoritative",
        "equivalent_to_canonical_tree": False,
    }
    assert observed["v7"]["authority"] == (
        "historical_auxiliary_non_authoritative"
    )
    assert observed["v7"]["logical_state_read"] is False
    assert observed["v7"]["sidecars_present"] is True
    assert observed["v7"]["participates_in_core_source_authority"] is False


def test_v10_reflector_runtime_receipt_is_checked_against_frozen_identity() -> None:
    module = _load("run_managed_reflector_recovery_v10")
    execution = _execution()
    execution.update(
        {
            "codex_binary": "/opt/codex/bin/codex",
            "codex_cli_version": "0.144.1",
        }
    )
    receipt = SimpleNamespace(
        runtime_profile="managed_science",
        runtime_digest="7" * 64,
        codex_binary="/opt/codex/bin/codex",
        actual_cli_version="0.144.1",
        model_name="gpt-5.5",
        reasoning_effort="high",
        auth_mode="subscription",
        capture_mode="transcript",
        path_fallback_allowed=False,
        exit_status=0,
        model_dump=lambda **_kwargs: {"actual_cli_version": "0.144.1"},
    )
    operation = SimpleNamespace(
        state="succeeded",
        result=SimpleNamespace(reflector_runtime_receipt=receipt),
    )
    config = _Config(
        {
            "reflector": {"model": "gpt-5.5", "reasoning_level": "high"},
            "v10_recovery": {"execution": execution},
        }
    )

    assert module._validate_reflector_runtime_receipt(config, operation) == {
        "actual_cli_version": "0.144.1"
    }
    receipt.path_fallback_allowed = True
    with pytest.raises(RuntimeError, match="frozen identity"):
        module._validate_reflector_runtime_receipt(config, operation)


def test_v10_reservation_and_admission_receipts_are_closed_and_hashed() -> None:
    module = _load("run_managed_reflector_recovery_v10")

    class Dumpable(SimpleNamespace):
        def model_dump(self, *, mode="json", exclude=None):
            assert mode == "json"
            omitted = set() if exclude is None else set(exclude)
            return {
                key: value
                for key, value in self.payload.items()
                if key not in omitted
            }

    runtime_receipt = {"actual_cli_version": "0.144.1", "exit_status": 0}
    reservation_payload = {
        "reservation_id": "reservation-v10",
        "job_id": "job-v10",
        "request_id": "request-v10",
        "state": "completed",
        "max_model_calls": 1,
        "model_calls_started": 1,
        "started_at": "2026-07-29T00:00:00Z",
        "completed_at": "2026-07-29T00:01:00Z",
        "runtime_receipt_sha256": module.canonical_sha256(runtime_receipt),
    }
    reservation_payload["content_sha256"] = module.canonical_sha256(
        reservation_payload
    )
    reservation = Dumpable(payload=reservation_payload, **reservation_payload)
    decision_payload = {
        "decision_id": "decision-v10",
        "job_id": "job-v10",
        "artifact_type": "agent_system",
        "action": "update",
        "parent_artifact_id": "parent-v10",
        "proposal_artifact_ids": ["artifact-v10"],
        "selected_artifact_id": "artifact-v10",
        "rejected_artifact_ids": [],
        "active_artifact_id": "artifact-v10",
        "reason": "accepted by the Core-owned admission gate",
        "admission": {
            "validator_id": "validator-v10",
            "schema_version": "1",
            "passed": True,
            "report_sha256": "b" * 64,
        },
        "created_at": "2026-07-29T00:01:00Z",
    }
    decision_payload["content_sha256"] = module.canonical_sha256(decision_payload)
    decision = Dumpable(
        payload=decision_payload,
        job_id="job-v10",
        artifact_type=SimpleNamespace(value="agent_system"),
        action=SimpleNamespace(value="update"),
        proposal_artifact_ids=("artifact-v10",),
        selected_artifact_id="artifact-v10",
        active_artifact_id="artifact-v10",
        admission=SimpleNamespace(passed=True),
        content_sha256=decision_payload["content_sha256"],
    )
    result = SimpleNamespace(
        job_id="job-v10",
        proposal_ids=("artifact-v10",),
        registry_artifact_id="artifact-v10",
        output=SimpleNamespace(artifact_type="agent_system"),
        reflector_inference_reservation=reservation,
        admission_decision=decision,
    )
    operation = SimpleNamespace(state="succeeded", result=result)

    exported_reservation, exported_decision = (
        module._validate_inference_and_admission_receipts(
            operation,
            runtime_receipt=runtime_receipt,
        )
    )
    assert exported_reservation["state"] == "completed"
    assert exported_decision["action"] == "update"

    reservation.max_model_calls = 2
    with pytest.raises(RuntimeError, match="reservation or admission"):
        module._validate_inference_and_admission_receipts(
            operation,
            runtime_receipt=runtime_receipt,
        )


def test_v10_requires_core_native_manifest_bound_agent_system_audit() -> None:
    module = _load("run_managed_reflector_recovery_v10")

    class Dumpable(SimpleNamespace):
        def model_dump(self, *, mode="json", exclude=None):
            assert mode == "json"
            omitted = set() if exclude is None else set(exclude)
            return {
                key: value
                for key, value in self.payload.items()
                if key not in omitted
            }

    audit_payload = {
        "agent_system_audit_receipt_contract_version": "1",
        "enabled": True,
        "repair_count": 0,
        "finding_count": 0,
        "forbidden_literal_count": 7,
        "leakage_basis_sha256": "a" * 64,
    }
    audit_payload["content_sha256"] = module.canonical_sha256(audit_payload)
    audit = Dumpable(payload=audit_payload, **audit_payload)
    result = SimpleNamespace(
        target_id="agent_system",
        method_id="agent_system_gepa_reflector",
        output=SimpleNamespace(artifact_type="agent_system"),
        registry_artifact_manifest_sha256="b" * 64,
        agent_system_audit=audit,
    )
    operation = SimpleNamespace(state="succeeded", result=result)

    public = module._validate_native_agent_system_audit_receipt(operation)

    assert public == {
        "registry_artifact_manifest_sha256": "b" * 64,
        "agent_system_audit": audit_payload,
    }
    assert set(public["agent_system_audit"]) == {
        "agent_system_audit_receipt_contract_version",
        "enabled",
        "repair_count",
        "finding_count",
        "forbidden_literal_count",
        "leakage_basis_sha256",
        "content_sha256",
    }
    assert "leakage_basis" not in public["agent_system_audit"]

    audit.finding_count = 1
    with pytest.raises(RuntimeError, match="protected-literal policy"):
        module._validate_native_agent_system_audit_receipt(operation)
    audit.finding_count = 0
    audit.repair_count = 1
    with pytest.raises(RuntimeError, match="protected-literal policy"):
        module._validate_native_agent_system_audit_receipt(operation)
    audit.repair_count = 0
    audit.forbidden_literal_count = 0
    with pytest.raises(RuntimeError, match="protected-literal policy"):
        module._validate_native_agent_system_audit_receipt(operation)


def test_v10_paid_target_timeout_and_unknown_outcome_are_at_most_once() -> None:
    driver = (SCRIPTS / "run_managed_reflector_recovery_v10.py").read_text()

    assert "timeout_seconds=_target_request_timeout_seconds(config)" in driver
    assert '"repeat_post_allowed": False' in driver
    assert '"readback_only": True' in driver
    assert "only Core GET readback is legal" in driver
    assert 'paths["target_unknown"]' in driver
    unknown_block = driver[driver.index("if operation is None:") :]
    assert '_write_idempotent(paths["target_failure"]' not in unknown_block.split(
        'if operation.state == "failed":', 1
    )[0]


def test_v10_readiness_failure_precedes_all_recovery_intents(monkeypatch) -> None:
    module = _load("run_managed_reflector_recovery_v10")
    operation_paths_called = False

    def fail_context():
        raise RuntimeError("REFLECTOR_CREDENTIAL_MOUNT_NOT_READY")

    def observe_paths(_config):
        nonlocal operation_paths_called
        operation_paths_called = True
        return {}

    monkeypatch.setattr(module, "_load_context", fail_context)
    monkeypatch.setattr(module, "_operation_paths", observe_paths)

    with pytest.raises(RuntimeError, match="REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"):
        module.create()
    assert operation_paths_called is False


def test_v10_lost_target_response_never_causes_a_second_post(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load("run_managed_reflector_recovery_v10")
    config = _Config(
        {
            "reflector": {"max_runtime_seconds": 900},
            "v10_recovery": {
                "namespace": "closed_v10_namespace",
                "target_authorization_order": [
                    "agent_system",
                    "text_memory",
                    "skill_bundle",
                ],
            },
        }
    )
    paths = {
        "root": tmp_path,
        "namespace": tmp_path / "namespace.json",
        "source": tmp_path / "source.json",
        "create_intent": tmp_path / "create-intent.json",
        "create": tmp_path / "create.json",
        "target_intent": tmp_path / "target-intent.json",
        "target": tmp_path / "target.json",
        "target_failure": tmp_path / "target-failure.json",
        "target_unknown": tmp_path / "target-unknown.json",
    }
    fake_record = SimpleNamespace(
        state="awaiting_target_authorization",
        completed_target_ids=(),
        remaining_target_ids=("agent_system", "text_memory", "skill_bundle"),
        targets=(SimpleNamespace(state="pending"),),
    )
    fake_snapshot = SimpleNamespace(record=fake_record)
    fake_authority = SimpleNamespace(close=lambda: None)
    requests: list[str] = []

    class FakeClient:
        def __init__(self, _authority):
            pass

        def readiness(self):
            return {"service_reachable": True}

        def json(self, method, _path, **_kwargs):
            requests.append(method)
            if method == "POST":
                raise module.CoreControlError("synthetic response loss")
            return {}

        def close(self):
            pass

    phase = {"value": 0}

    def readback(*_args, **_kwargs):
        if phase["value"] == 0:
            phase["value"] = 1
            return None, "synthetic_unreadable"
        return (
            SimpleNamespace(
                state="failed",
                result=None,
                failure=SimpleNamespace(
                    model_dump=lambda **_kwargs: {
                        "failure_code": "synthetic_terminal"
                    }
                ),
                model_dump=lambda **_kwargs: {"state": "failed"},
            ),
            None,
        )

    monkeypatch.setattr(module, "_load_context", lambda: (config, {}, {}))
    monkeypatch.setattr(module, "_verify_immutable_sources", lambda _c: {"v7": {}})
    monkeypatch.setattr(
        module,
        "_require_sources_unchanged",
        lambda _c, before: before,
    )
    monkeypatch.setattr(module, "_operation_paths", lambda _c: paths)
    monkeypatch.setattr(module, "_load_recovery_id", lambda _paths: "recovery-v10")
    monkeypatch.setattr(module, "_acquire", lambda _c: fake_authority)
    monkeypatch.setattr(module, "CoreControlV2Client", FakeClient)
    monkeypatch.setattr(module, "_read_live_mount_readiness", lambda *_args: None)
    monkeypatch.setattr(
        module.ScienceSuccessorRecoverySnapshotV1,
        "model_validate",
        lambda _value: fake_snapshot,
    )
    monkeypatch.setattr(module, "_validate_created_snapshot", lambda *_args: None)
    monkeypatch.setattr(module, "_read_target_until_terminal", readback)
    monkeypatch.setattr(module, "_safe_snapshot", lambda _snapshot: {})

    with pytest.raises(RuntimeError, match="outcome remains unknown"):
        module.run_agent_system()
    assert paths["target_intent"].is_file()
    assert paths["target_unknown"].is_file()
    assert not paths["target_failure"].exists()
    assert requests.count("POST") == 1

    with pytest.raises(RuntimeError, match="failed terminally"):
        module.run_agent_system()
    assert requests.count("POST") == 1
    assert paths["target_failure"].is_file()
