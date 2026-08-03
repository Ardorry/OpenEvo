"""Zero-model-call receipts for Temperature Safe-Evolve V2 admission."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
)
from openevo_chembench.temperature_safe_evolve_v2.config import (
    CLASSIFICATION,
    EXPECTED_CODEX_SHA256,
    EXPECTED_RUNTIME_V8_IDENTITY,
    SafeEvolveConfigV2,
)
from openevo_chembench.temperature_safe_evolve_v2.folds import FrozenSafeFoldsV2
from openevo_chembench.temperature_safe_evolve_v2.frozen_c4 import FrozenC4BundleV2

PRIOR_V8_RECEIPT = (
    "state/chembench_temperature_full_evolve_v1/formal_runtime_v8/formal_runtime_receipt_v1.json"
)
PRIOR_V8_SOURCE_COMMIT = "b625a8ceabc512636a474502d84a93ece1f50929"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)


class SafePreflightError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class SafePreflightBundleV2:
    report: dict[str, object]
    frozen_c4_receipt: dict[str, object]
    fold_manifest: dict[str, object]
    near_duplicate_manifest: dict[str, object]
    model_identity_receipt: dict[str, object]
    runtime_identity_receipt: dict[str, object]
    source_code_receipt: dict[str, object]
    preflight_test_receipt: dict[str, object]

    @property
    def digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "report": self.report,
                    "frozen_c4_receipt": self.frozen_c4_receipt,
                    "fold_manifest": self.fold_manifest,
                    "near_duplicate_manifest": self.near_duplicate_manifest,
                    "model_identity_receipt": self.model_identity_receipt,
                    "runtime_identity_receipt": self.runtime_identity_receipt,
                    "source_code_receipt": self.source_code_receipt,
                    "preflight_test_receipt": self.preflight_test_receipt,
                }
            )
        )


def verify_prior_runtime_v8_v2(repository_root: Path) -> dict[str, object]:
    """Verify, but never mutate, the immutable V1 runtime-v8 evidence."""

    path = repository_root / PRIOR_V8_RECEIPT
    raw = _read_owner_regular(path, maximum=1024 * 1024, mode=0o600)
    try:
        receipt = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (UnicodeError, json.JSONDecodeError, SafePreflightError) as exc:
        raise SafePreflightError("SAFE_PREFLIGHT_PRIOR_RUNTIME_V8_INVALID") from exc
    if (
        type(receipt) is not dict
        or receipt.get("schema_version") != "TemperatureFormalRuntimeReceiptV1"
        or receipt.get("source_commit") != PRIOR_V8_SOURCE_COMMIT
        or receipt.get("model_calls") != 0
        or receipt.get("core_editable") is not False
        or receipt.get("chembench_editable") is not False
    ):
        raise SafePreflightError("SAFE_PREFLIGHT_PRIOR_RUNTIME_V8_INVALID")
    identity = sha256_bytes(canonical_json_bytes(receipt))
    if identity != EXPECTED_RUNTIME_V8_IDENTITY:
        raise SafePreflightError("SAFE_PREFLIGHT_PRIOR_RUNTIME_V8_IDENTITY_MISMATCH")
    for relative_field, digest_field in (
        ("core_wheel_relative", "core_wheel_sha256"),
        ("chembench_wheel_relative", "chembench_wheel_sha256"),
        ("framework_lock_relative", "framework_lock_sha256"),
    ):
        relative = receipt.get(relative_field)
        expected = receipt.get(digest_field)
        if (
            type(relative) is not str
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or _SHA256.fullmatch(str(expected)) is None
            or _file_sha256(repository_root / relative) != expected
        ):
            raise SafePreflightError("SAFE_PREFLIGHT_PRIOR_RUNTIME_V8_FILE_MISMATCH")
    return {
        "schema_version": "TemperatureSafePriorRuntimeV8CompatibilityReceiptV2",
        "status": "PASS_IMMUTABLE_PRIOR_RUNTIME_VERIFIED",
        "runtime_v8_identity_sha256": identity,
        "source_commit": receipt["source_commit"],
        "core_wheel_sha256": receipt["core_wheel_sha256"],
        "chembench_wheel_sha256": receipt["chembench_wheel_sha256"],
        "framework_lock_sha256": receipt["framework_lock_sha256"],
        "used_as_v2_executable_bundle": False,
        "used_as_compatibility_reference": True,
    }


def build_zero_call_preflight_v2(
    *,
    config: SafeEvolveConfigV2,
    frozen_c4: FrozenC4BundleV2,
    folds: FrozenSafeFoldsV2,
    phase_a_half_sha256: str,
    source_commit: str,
    branch: str,
    source_tree_clean: bool,
    formal_runtime_receipt: dict[str, object],
    formal_runtime_identity_sha256: str,
    formal_runtime_receipt_sha256: str,
    runtime_services_run_id: str,
    runtime_services_identity_sha256: str,
    runtime_services_health_sha256: str,
    prior_runtime_v8_receipt: dict[str, object],
    candidate_codex_sha256: str,
    reflector_codex_sha256: str,
    candidate_image_id: str,
    framework_registry_digest: str,
    framework_lock_sha256: str,
    credential_metadata_passed: bool,
    empty_inventory_before_sha256: str,
    empty_inventory_after_sha256: str,
    test_receipt: dict[str, object],
) -> SafePreflightBundleV2:
    values = (
        formal_runtime_identity_sha256,
        formal_runtime_receipt_sha256,
        runtime_services_identity_sha256,
        runtime_services_health_sha256,
        framework_registry_digest,
        framework_lock_sha256,
        empty_inventory_before_sha256,
        empty_inventory_after_sha256,
        phase_a_half_sha256,
    )
    if (
        type(config) is not SafeEvolveConfigV2
        or type(frozen_c4) is not FrozenC4BundleV2
        or type(folds) is not FrozenSafeFoldsV2
        or _COMMIT.fullmatch(source_commit) is None
        or branch != "chembench-temperature-safe-evolve-v2"
        or source_tree_clean is not True
        or any(_SHA256.fullmatch(value) is None for value in values)
        or re.fullmatch(
            r"stv3-temperature-safe-services-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,16}",
            runtime_services_run_id,
        )
        is None
        or candidate_codex_sha256 != EXPECTED_CODEX_SHA256
        or reflector_codex_sha256 != EXPECTED_CODEX_SHA256
        or not credential_metadata_passed
        or empty_inventory_before_sha256 != empty_inventory_after_sha256
        or prior_runtime_v8_receipt.get("runtime_v8_identity_sha256")
        != EXPECTED_RUNTIME_V8_IDENTITY
        or formal_runtime_receipt.get("source_commit") != source_commit
        or formal_runtime_receipt.get("model_calls") != 0
        or formal_runtime_receipt.get("core_editable") is not False
        or formal_runtime_receipt.get("chembench_editable") is not False
        or test_receipt.get("model_calls") != 0
        or int(test_receipt.get("failure_count", -1)) != 0
    ):
        raise SafePreflightError("SAFE_PREFLIGHT_ADMISSION_INVALID")

    frozen_receipt = frozen_c4.public_receipt()
    fold_manifest = folds.plan.public_receipt()
    near_duplicate = {
        "schema_version": "TemperatureSafeNearDuplicateGroupingManifestV2",
        "status": "PASS",
        "algorithm": fold_manifest["grouping_algorithm"],
        "group_count": fold_manifest["group_count"],
        "singleton_group_count": fold_manifest["singleton_group_count"],
        "maximum_group_size": fold_manifest["maximum_group_size"],
        "whole_group_assignment": True,
        "group_manifest_sha256": fold_manifest["group_manifest_sha256"],
        "contains_item_identities": False,
        "contains_task_content": False,
    }
    model = {
        "schema_version": "TemperatureSafeModelIdentityReceiptV2",
        "status": "PASS_ZERO_MODEL_CALLS",
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "candidate_codex_executable_sha256": candidate_codex_sha256,
        "reflector_codex_executable_sha256": reflector_codex_sha256,
        "managed_candidate_image_id": candidate_image_id,
        "candidate_baseline_stack_equal": True,
        "model_calls": 0,
    }
    runtime = {
        "schema_version": "TemperatureSafeRuntimeIdentityReceiptV2",
        "status": "PASS_ZERO_MODEL_CALLS",
        "prior_runtime_v8": prior_runtime_v8_receipt,
        "v2_formal_runtime_identity_sha256": formal_runtime_identity_sha256,
        "v2_formal_runtime_receipt_sha256": formal_runtime_receipt_sha256,
        "v2_formal_runtime_source_commit": source_commit,
        "v2_core_wheel_sha256": formal_runtime_receipt["core_wheel_sha256"],
        "v2_chembench_wheel_sha256": formal_runtime_receipt["chembench_wheel_sha256"],
        "framework_registry_digest": framework_registry_digest,
        "framework_lock_sha256": framework_lock_sha256,
        "phase_a_runtime_services_run_id": runtime_services_run_id,
        "runtime_services_identity_sha256": runtime_services_identity_sha256,
        "runtime_services_health_sha256": runtime_services_health_sha256,
        "credential_metadata_passed": True,
        "credential_content_read": False,
        "capture_mode": "transcript",
        "model_visible_tools_enabled": False,
        "provider_transport_network_enabled": True,
        "empty_inventory_before_sha256": empty_inventory_before_sha256,
        "empty_inventory_after_sha256": empty_inventory_after_sha256,
        "model_calls": 0,
    }
    source = {
        "schema_version": "TemperatureSafeSourceCodeReceiptV2",
        "status": "PASS_FROZEN",
        "source_commit": source_commit,
        "branch": branch,
        "tracked_tree_clean": True,
        "experiment_paths_tracked_and_clean": True,
        "config_sha256": config.digest,
        "formal_runtime_source_tree_sha256": formal_runtime_receipt["source_tree_sha256"],
    }
    report = {
        "schema_version": "TemperatureSafePreflightReportV2",
        "status": "PASS_READY_FOR_SAFE_EVOLVE_V2",
        "classification": CLASSIFICATION,
        "source_commit": source_commit,
        "config_sha256": config.digest,
        "fold_sha256": folds.plan.fold_sha256,
        "phase_a_half_assignment_sha256": phase_a_half_sha256,
        "official_temperature_pool_count": 202,
        "phase_a_test_count": 100,
        "phase_a_expected_candidate_accepted_calls": 800,
        "fold_counts": [50, 50, 50, 50],
        "reserve_count": 2,
        "phase_a_runtime_services_run_id": runtime_services_run_id,
        "runtime_services_identity_sha256": runtime_services_identity_sha256,
        "formal_runtime_identity_sha256": formal_runtime_identity_sha256,
        "prior_runtime_v8_identity_sha256": EXPECTED_RUNTIME_V8_IDENTITY,
        "findings": {
            "frozen_c4_exact_payloads": "PASS",
            "prior_test_uid_order_and_gt_recovered": "PASS_PRIVATE",
            "official_pool_202": "PASS",
            "group_aware_four_folds": "PASS",
            "fresh_run_namespaces": "PASS",
            "owner_private_permissions_0700": "PASS",
            "candidate_reflector_core_managed_paths": "PASS",
            "test_feedback_and_update_mechanical_gate": "PASS",
            "context_hash_dedup": "PASS",
            "accepted_completion_immutable": "PASS",
            "parser_invalid_no_answer_retry": "PASS",
            "frozen_selection_promotion_deployment_gates": "PASS",
            "unit_integration_regression_tests": "PASS",
            "runtime_inventory_unchanged": "PASS",
        },
        "preflight_model_calls": 0,
    }
    return SafePreflightBundleV2(
        report=report,
        frozen_c4_receipt=frozen_receipt,
        fold_manifest=fold_manifest,
        near_duplicate_manifest=near_duplicate,
        model_identity_receipt=model,
        runtime_identity_receipt=runtime,
        source_code_receipt=source,
        preflight_test_receipt=test_receipt,
    )


def _read_owner_regular(path: Path, *, maximum: int, mode: int | None = None) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise SafePreflightError("SAFE_PREFLIGHT_EVIDENCE_FILE_INVALID")
    return path.read_bytes()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SafePreflightError("SAFE_PREFLIGHT_EVIDENCE_FILE_UNAVAILABLE") from exc
    return digest.hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SafePreflightError("SAFE_PREFLIGHT_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


__all__ = [
    "PRIOR_V8_RECEIPT",
    "SafePreflightBundleV2",
    "SafePreflightError",
    "build_zero_call_preflight_v2",
    "verify_prior_runtime_v8_v2",
]
