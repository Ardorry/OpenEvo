from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.temperature_full_evolve_v1.config import (
    EXPECTED_CANDIDATE_IMAGE_ID,
    EXPECTED_CODEX_SHA256,
    load_temperature_full_evolve_config,
)
from openevo_chembench.temperature_full_evolve_v1.preflight import (
    RegressionReceiptV1,
    RuntimePreflightEvidenceV1,
    TemperaturePreflightError,
    build_zero_model_preflight_v1,
    write_public_preflight_bundle_v1,
)
from openevo_chembench.temperature_full_evolve_v1.split import build_temperature_split_v1


def _repository() -> Path:
    return Path(__file__).resolve().parents[4]


def _config():
    return load_temperature_full_evolve_config(
        (_repository() / "benchmarks/chembench/configs/temperature_full_evolve_v1/temperature_full_evolve_v1.yaml").resolve()
    )


def _split():
    config = _config()
    snapshot = (_repository() / config.payload["dataset"]["root"]).resolve()
    loader = ChemBench4KDatasetLoader(
        snapshot_root=snapshot,
        manifest_path=snapshot / "chembench4k_dataset_manifest_v2.json",
    )
    return build_temperature_split_v1(loader)


def _runtime(**updates: object) -> RuntimePreflightEvidenceV1:
    payload: dict[str, object] = {
        "candidate_codex_executable_sha256": EXPECTED_CODEX_SHA256,
        "reflector_codex_executable_sha256": EXPECTED_CODEX_SHA256,
        "candidate_image_id": EXPECTED_CANDIDATE_IMAGE_ID,
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "capture_mode": "transcript",
        "credential_metadata_passed": True,
        "credential_content_read": False,
        "managed_runtime_passed": True,
        "framework_registry_passed": True,
        "framework_lock_sha256": "a" * 64,
        "runtime_services_ready": True,
        "runtime_services_identity_sha256": "b" * 64,
        "completion_persistence_shared": True,
        "completion_persistence_identity_sha256": "c" * 64,
        "empty_runtime_inventory_sha256": "f" * 64,
        "rollout_task_count_before_first_call": 0,
        "persisted_task_directory_count_before_first_call": 0,
        "model_visible_tools_enabled": False,
        "provider_transport_network_enabled": True,
        "model_calls": 0,
    }
    payload.update(updates)
    return RuntimePreflightEvidenceV1.model_validate(payload)


def _regression() -> RegressionReceiptV1:
    return RegressionReceiptV1(
        focused_test_count=48,
        focused_failure_count=0,
        integration_test_count=12,
        integration_failure_count=0,
        command_sha256="d" * 64,
        output_sha256="e" * 64,
        model_calls=0,
    )


def test_zero_call_preflight_closes_schedule_split_and_generation_zero(tmp_path: Path) -> None:
    split = _split()
    bundle = build_zero_model_preflight_v1(
        config=_config(),
        split=split,
        runtime=_runtime(),
        regression=_regression(),
        source_commit="f" * 40,
        source_tree_clean=True,
    )
    assert bundle.report["status"] == "PASS_READY_FOR_FORMAL_EXECUTION"
    assert bundle.report["preflight_model_calls"] == 0
    assert bundle.report["planned"] == {
        "train_count": 100,
        "test_count": 100,
        "train_batch_count": 4,
        "candidate_logical_calls": 400,
        "reflector_logical_calls": 4,
        "model_logical_calls": 404,
        "core_jobs": 12,
    }
    destination = (tmp_path / "public").resolve()
    write_public_preflight_bundle_v1(bundle, destination=destination)
    assert (destination / "regression_receipt_v1.json").is_file()
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(destination.iterdir())
    )
    assert split.plan.train_uids[0] not in combined
    assert split.plan.test_uids[0] not in combined
    assert split.train[0].question not in combined
    assert '"target"' not in json.dumps(bundle.split_manifest)
    assert "fixed_official_temperature_pool_fresh_c0" in combined


def test_preflight_rejects_dirty_source_or_nonzero_model_call() -> None:
    with pytest.raises(ValueError, match="runtime preflight identity is not admissible"):
        _runtime(model_calls=1)
    with pytest.raises(TemperaturePreflightError, match="SOURCE_IDENTITY_NOT_FROZEN"):
        build_zero_model_preflight_v1(
            config=_config(),
            split=_split(),
            runtime=_runtime(),
            regression=_regression(),
            source_commit="f" * 40,
            source_tree_clean=False,
        )
