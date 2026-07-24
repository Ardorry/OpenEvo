from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from openevo_chembench.config import ExperimentConfig
from openevo_chembench.protocol_guard import (
    CHEMBENCH4K_CATEGORIES,
    FROZEN_GENERALIZATION_PROTOCOL_ID,
    FrozenProtocolEvidence,
    ProtocolFindingCode,
    ProtocolGateReceipt,
    assess_frozen_generalization_v2,
    audit_current_adapter_for_frozen_v2,
)


_REVISION = "1" * 40
_ARTIFACT_HASH = "2" * 64
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _ready_evidence(**overrides: object) -> FrozenProtocolEvidence:
    values: dict[str, object] = {
        "dataset_repository": "AI4Chem/ChemBench4K",
        "dataset_revision": _REVISION,
        "dataset_splits": ("dev", "test"),
        "dataset_categories": CHEMBENCH4K_CATEGORIES,
        "dataset_integrity_verified": True,
        "opencompass_equivalence_verified": False,
        "protocol_id": FROZEN_GENERALIZATION_PROTOCOL_ID,
        "official_prompt_renderer_verified": True,
        "official_evaluator_verified": True,
        "core_dataset_job_contracts_used": True,
        "registered_text_memory_method": "text_memory_reflector",
        "core_artifact_lifecycle_used": True,
        "core_context_resolution_used": True,
        "executor_zero_tool_boundary_verified": True,
        "frozen_artifact_sha256": _ARTIFACT_HASH,
    }
    values.update(overrides)
    return FrozenProtocolEvidence(**values)  # type: ignore[arg-type]


def _load_script_module(script_name: str) -> ModuleType:
    script_path = _REPOSITORY_ROOT / "benchmarks" / "chembench" / "scripts" / script_name
    module_name = f"_chembench_protocol_guard_test_{script_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_complete_claims_still_require_verified_attestation() -> None:
    receipt = assess_frozen_generalization_v2(_ready_evidence())

    assert receipt.passed is False
    assert receipt.finding_codes == (ProtocolFindingCode.VERIFIED_ATTESTATION_MISSING,)
    assert receipt.to_audit_payload()["paid_execution_allowed"] is False


def test_positive_receipt_cannot_be_constructed_directly() -> None:
    with pytest.raises(ValueError, match="verified attestor"):
        ProtocolGateReceipt(
            protocol_id=FROZEN_GENERALIZATION_PROTOCOL_ID,
            dataset_repository="AI4Chem/ChemBench4K",
            dataset_revision=_REVISION,
            passed=True,
            finding_codes=(),
        )


def test_jablonkagroup_dataset_is_rejected_fail_closed() -> None:
    receipt = assess_frozen_generalization_v2(
        _ready_evidence(dataset_repository="jablonkagroup/ChemBench")
    )

    assert receipt.passed is False
    assert ProtocolFindingCode.DATASET_IDENTITY_MISMATCH in receipt.finding_codes
    assert receipt.to_audit_payload()["paid_execution_allowed"] is False


def test_missing_core_method_lifecycle_is_rejected() -> None:
    receipt = assess_frozen_generalization_v2(
        _ready_evidence(
            core_dataset_job_contracts_used=False,
            registered_text_memory_method=None,
            core_artifact_lifecycle_used=False,
            core_context_resolution_used=False,
        )
    )

    assert receipt.passed is False
    assert ProtocolFindingCode.OPENEVO_METHOD_INTEGRATION_MISSING in receipt.finding_codes


def test_opencompass_identity_requires_byte_equivalence_proof() -> None:
    receipt = assess_frozen_generalization_v2(
        _ready_evidence(dataset_repository="opencompass/ChemBench4K")
    )

    assert receipt.passed is False
    assert ProtocolFindingCode.DATASET_EQUIVALENCE_UNPROVEN in receipt.finding_codes


def test_opencompass_equivalence_claim_clears_only_equivalence_finding() -> None:
    receipt = assess_frozen_generalization_v2(
        _ready_evidence(
            dataset_repository="opencompass/ChemBench4K",
            opencompass_equivalence_verified=True,
        )
    )

    assert receipt.passed is False
    assert ProtocolFindingCode.DATASET_EQUIVALENCE_UNPROVEN not in receipt.finding_codes
    assert ProtocolFindingCode.VERIFIED_ATTESTATION_MISSING in receipt.finding_codes


def test_unverified_zero_tool_boundary_is_rejected() -> None:
    receipt = assess_frozen_generalization_v2(
        _ready_evidence(executor_zero_tool_boundary_verified=False)
    )

    assert receipt.passed is False
    assert ProtocolFindingCode.ZERO_TOOL_BOUNDARY_UNVERIFIED in receipt.finding_codes


def test_current_adapter_receipt_names_all_material_blockers() -> None:
    config = ExperimentConfig(
        openevo_revision="3" * 40,
        chembench_revision=_REVISION,
        codex_cli_version="codex-cli 0.144.6",
        prompt_version="legacy",
    )

    receipt = audit_current_adapter_for_frozen_v2(config)

    assert receipt.passed is False
    assert set(receipt.finding_codes) == {
        ProtocolFindingCode.DATASET_IDENTITY_MISMATCH,
        ProtocolFindingCode.DATASET_SPLIT_MISMATCH,
        ProtocolFindingCode.DATASET_CATEGORY_MISMATCH,
        ProtocolFindingCode.DATASET_INTEGRITY_UNVERIFIED,
        ProtocolFindingCode.LEGACY_PROTOCOL_ID,
        ProtocolFindingCode.OFFICIAL_PROTOCOL_INCOMPATIBLE,
        ProtocolFindingCode.OPENEVO_METHOD_INTEGRATION_MISSING,
        ProtocolFindingCode.ZERO_TOOL_BOUNDARY_UNVERIFIED,
        ProtocolFindingCode.FROZEN_ARTIFACT_MISSING,
        ProtocolFindingCode.VERIFIED_ATTESTATION_MISSING,
    }


def test_gate_receipt_has_no_task_or_target_fields() -> None:
    payload = assess_frozen_generalization_v2(
        _ready_evidence(dataset_repository="jablonkagroup/ChemBench")
    ).to_audit_payload()
    serialized = repr(payload).lower()

    assert set(payload) == {
        "schema_version",
        "protocol_id",
        "dataset_repository",
        "dataset_revision",
        "passed",
        "paid_execution_allowed",
        "finding_codes",
    }
    for forbidden in (
        "question",
        "options",
        "answer",
        "target",
        "target_scores",
        "uuid",
        "canary",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("script_name", "config_name", "execution_function"),
    (
        (
            "run_chembench_formal.py",
            "local_codex_baseline.yaml",
            "_run_formal",
        ),
        (
            "run_debug_gpt55_evolution.py",
            "debug_gpt55_evolution.yaml",
            "_run_debug",
        ),
    ),
)
def test_entrypoints_block_before_preflight_dataset_or_execution(
    script_name: str,
    config_name: str,
    execution_function: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script_module(script_name)
    config_path = _REPOSITORY_ROOT / "benchmarks" / "chembench" / "configs" / config_name
    unexpected = AssertionError("blocked entrypoint crossed the protocol gate")

    with (
        patch.object(
            module,
            "run_local_codex_preflight",
            side_effect=unexpected,
        ),
        patch.object(
            module,
            "run_real_execution_preflight",
            side_effect=unexpected,
        ),
        patch.object(module, "ChemBenchDatasetLoader", side_effect=unexpected),
        patch.object(module, execution_function, side_effect=unexpected),
    ):
        return_code = module.main(["--config", os.fspath(config_path), "--preflight-only"])
    payload = json.loads(capsys.readouterr().out)

    assert return_code == 2
    assert payload["status"] == "blocked"
    assert payload["dataset_loaded"] is False
    assert payload["results_written"] is False
    assert payload["model_calls_made"] == 0


def test_formal_entrypoint_blocks_before_dataset_or_model_call() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.fspath(_REPOSITORY_ROOT / "benchmarks" / "chembench" / "src")
    completed = subprocess.run(
        (
            sys.executable,
            os.fspath(
                _REPOSITORY_ROOT
                / "benchmarks"
                / "chembench"
                / "scripts"
                / "run_chembench_formal.py"
            ),
            "--config",
            os.fspath(
                _REPOSITORY_ROOT
                / "benchmarks"
                / "chembench"
                / "configs"
                / "local_codex_baseline.yaml"
            ),
            "--preflight-only",
        ),
        cwd=_REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert payload["status"] == "blocked"
    assert payload["dataset_loaded"] is False
    assert payload["results_written"] is False
    assert payload["model_calls_made"] == 0
    assert payload["protocol_gate"]["paid_execution_allowed"] is False
