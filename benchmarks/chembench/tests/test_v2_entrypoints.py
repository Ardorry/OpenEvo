from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from openevo_chembench import v2_cli
from openevo_chembench.v2_cli import (
    DATASET_ROOT,
    PACKAGE_ROOT,
    _load_requested_config,
    _validate_run_state,
    _verify_config_scope_manifests,
    _verify_public_completed_result,
)
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.v2_config import (
    PLACEHOLDER,
    arm_parity_findings,
    load_frozen_config_v2,
)


_SCRIPT_NAMES = (
    "prepare_chembench4k_v2_runtime.sh",
    "build_dev_loo_dataset_v2.sh",
    "run_text_memory_evolution_v2.sh",
    "validate_frozen_text_memory_v2.sh",
    "freeze_benchmark_receipt_v2.sh",
    "dry_run_frozen_v2.sh",
    "run_baseline_canary18_frozen_v2.sh",
    "run_evolved_canary18_frozen_v2.sh",
    "compare_canary18_frozen_v2.sh",
    "run_baseline_pilot500_frozen_v2.sh",
    "run_evolved_pilot500_frozen_v2.sh",
    "compare_pilot500_frozen_v2.sh",
    "run_baseline_full_frozen_v2.sh",
    "run_evolved_full_frozen_v2.sh",
    "compare_full_frozen_v2.sh",
)


def test_v2_scripts_are_executable_and_shell_syntax_is_valid() -> None:
    scripts = PACKAGE_ROOT / "scripts"
    for name in _SCRIPT_NAMES:
        path = scripts / name
        assert path.is_file()
        assert os.access(path, os.X_OK)
        subprocess.run(
            ("bash", "-n", os.fspath(path)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=10,
        )


def test_dry_run_uses_bootstrap_and_contains_no_paid_command() -> None:
    text = (PACKAGE_ROOT / "scripts" / "dry_run_frozen_v2.sh").read_text(encoding="utf-8")
    assert "run_v2_cli_no_paid" in text
    assert "collect-dev-loo" not in text
    assert "run-evolution" not in text
    assert "run-arm" not in text


def test_receipt_freeze_script_does_not_rewrite_source_manifest() -> None:
    text = (PACKAGE_ROOT / "scripts" / "freeze_benchmark_receipt_v2.sh").read_text(
        encoding="utf-8"
    )
    assert "source-manifest" not in text
    assert text.count("freeze-receipt") == 1


def test_all_six_configs_are_scope_paired_and_output_isolated() -> None:
    protocol_hashes: set[str] = set()
    outputs: set[str] = set()
    for scope in ("canary18", "pilot500", "full"):
        baseline = load_frozen_config_v2(
            PACKAGE_ROOT / "configs" / f"baseline_{scope}_frozen_v2.yaml"
        )
        evolved = load_frozen_config_v2(
            PACKAGE_ROOT / "configs" / f"evolved_{scope}_frozen_v2.yaml"
        )
        assert arm_parity_findings(baseline, evolved) == ()
        assert baseline.scope == scope
        assert evolved.scope == scope
        assert baseline.task_manifest == evolved.task_manifest
        assert baseline.private_task_manifest == evolved.private_task_manifest
        assert baseline.output_directory != evolved.output_directory
        protocol_hashes.update((baseline.pilot_protocol_hash, evolved.pilot_protocol_hash))
        outputs.update((baseline.output_directory, evolved.output_directory))
    assert len(outputs) == 6
    assert len(protocol_hashes) == 1
    protocol_hash = protocol_hashes.pop()
    assert protocol_hash == PLACEHOLDER or len(protocol_hash) == 64


def test_static_report_proves_all_45_records_are_visible_to_reflector() -> None:
    report = v2_cli.validate_static_state()

    assert report["loo_record_count"] == 45
    assert report["configured_max_records"] == 45
    assert report["records_visible_to_reflector"] == 45
    ordered_digest = report["ordered_loo_uid_sha256"]
    assert isinstance(ordered_digest, str)
    assert len(ordered_digest) == 64


def test_text_memory_config_cannot_fall_back_to_upstream_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "text_memory.yaml"
    config.write_text(
        "dev_trajectory:\n  expected_records: 45\nreflector:\n  max_records: 20\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(v2_cli, "TEXT_MEMORY_EVOLUTION_CONFIG", config)

    with pytest.raises(RuntimeError, match="explicitly configure all 45"):
        v2_cli._configured_reflector_max_records()


def test_run_arm_rejects_unregistered_config_path(tmp_path: Path) -> None:
    canonical = PACKAGE_ROOT / "configs" / "baseline_canary18_frozen_v2.yaml"
    assert _load_requested_config(canonical).arm == "baseline"
    copied = tmp_path / canonical.name
    copied.write_bytes(canonical.read_bytes())
    with pytest.raises(RuntimeError, match="not a registered"):
        _load_requested_config(copied)


def test_run_arm_recomputes_canonical_scope_manifest() -> None:
    config = load_frozen_config_v2(PACKAGE_ROOT / "configs" / "baseline_canary18_frozen_v2.yaml")
    loader = ChemBench4KDatasetLoader(snapshot_root=DATASET_ROOT)
    _verify_config_scope_manifests(config=config, loader=loader)
    substituted = replace(
        config,
        task_manifest=("OpenEvo/benchmarks/chembench/manifests/v2/pilot500_public_manifest.jsonl"),
    )
    with pytest.raises(RuntimeError, match="canonical"):
        _verify_config_scope_manifests(config=substituted, loader=loader)


def test_evolution_preflight_detects_existing_v2_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v2_cli, "WORKSPACE_ROOT", tmp_path)
    output = tmp_path / "OpenEvo" / "results" / "chembench4k_frozen_v2" / "canary18" / "baseline"
    output.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="result exists"):
        v2_cli._assert_v2_result_roots_absent()


def test_completed_resumed_run_counts_failed_no_completion_invocation() -> None:
    config = load_frozen_config_v2(PACKAGE_ROOT / "configs" / "baseline_canary18_frozen_v2.yaml")
    state = {
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "execution_mode": "standalone_openevo_maintainer_benchmark",
        "run_name": config.run_name,
        "arm": config.arm,
        "scope": config.scope,
        "config_sha256": config.config_sha256(),
        "execution_receipt_sha256": "a" * 64,
        "planned_tasks": 18,
        "completed_tasks": 18,
        "model_calls": 19,
        "public_result_chain_sha256": "b" * 64,
        "private_result_chain_sha256": "c" * 64,
        "status": "COMPLETED",
        "resume_count": 1,
        "finding_codes": [],
        "resume_allowed": False,
    }
    _validate_run_state(
        config=config,
        state=state,
        expected_receipt_sha256="a" * 64,
        planned_tasks=18,
        private_rows=18,
        public_chain="b" * 64,
        private_chain="c" * 64,
    )
    state["model_calls"] = 18
    with pytest.raises(RuntimeError, match="internally inconsistent"):
        _validate_run_state(
            config=config,
            state=state,
            expected_receipt_sha256="a" * 64,
            planned_tasks=18,
            private_rows=18,
            public_chain="b" * 64,
            private_chain="c" * 64,
        )


def test_public_comparison_result_is_recomputed_exactly() -> None:
    task = ChemBench4KDatasetLoader(snapshot_root=DATASET_ROOT).load_split("test")[0]
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion="A",
    )
    row = {
        "schema_version": "chembench4k_public_item_result_v2",
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "arm": "baseline",
        "scope": "canary18",
        "ordinal": 0,
        "uid": task.uid,
        "category": task.category,
        "raw_completion": "A",
        "parsed_prediction": "A",
        "score": 1.0 if evaluation.correct else 0.0,
        "official_parse_status": "parsed",
        "strict_parse_status": "parsed",
        "strict_parse_success": True,
        "transcript_reference": "sha256:audit",
        "runtime_metadata": None,
    }
    _verify_public_completed_result(
        row=row,
        task=task,
        evaluation=evaluation,
        arm="baseline",
        scope="canary18",
        ordinal=0,
    )
    row["score"] = 0.0 if row["score"] == 1.0 else 1.0
    with pytest.raises(RuntimeError, match="no longer matches"):
        _verify_public_completed_result(
            row=row,
            task=task,
            evaluation=evaluation,
            arm="baseline",
            scope="canary18",
            ordinal=0,
        )
