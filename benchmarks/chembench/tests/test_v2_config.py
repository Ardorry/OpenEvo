from __future__ import annotations

from openevo_chembench.v2_config import (
    ExecutorPolicyV2,
    FrozenArtifactBinding,
    FrozenExperimentConfigV2,
    PLACEHOLDER,
    arm_parity_findings,
)


def _config(arm: str) -> FrozenExperimentConfigV2:
    enabled = arm == "evolved"
    return FrozenExperimentConfigV2(
        arm=arm,
        scope="pilot500",
        run_name=f"{arm}_pilot500_frozen_v2",
        output_directory=f"results/chembench4k_frozen_v2/pilot500/{arm}",
        dataset_root="benchmarks/chembench/data/chembench4k",
        dataset_manifest=(
            "benchmarks/chembench/data/chembench4k/chembench4k_dataset_manifest_v2.json"
        ),
        task_manifest=("benchmarks/chembench/manifests/pilot500_public_manifest.jsonl"),
        private_task_manifest=(
            "benchmarks/chembench/private_manifests/pilot500_private_manifest.jsonl"
        ),
        receipt_path="benchmarks/chembench/manifests/benchmark_execution_receipt_v2.json",
        pilot_protocol_hash=PLACEHOLDER,
        codex_cli_version="0.144.6",
        prompt_renderer_id="chembench4k_official_five_shot_v2",
        parser_id="official_first_capital_parser_v2",
        evaluator_id="chembench4k_accuracy_v2",
        artifact=FrozenArtifactBinding(
            enabled=enabled,
            frozen_artifact_id=PLACEHOLDER if enabled else None,
            frozen_artifact_sha256=PLACEHOLDER if enabled else None,
            context_resolution_digest=PLACEHOLDER if enabled else None,
            resolved_memory_sha256=PLACEHOLDER if enabled else None,
        ),
        executor=ExecutorPolicyV2(
            backend="local_codex_cli",
            harness="codex_cli",
            timeout_seconds=600,
            concurrency=1,
            infrastructure_retries_before_completion=0,
            tools_enabled=False,
            mcp_enabled=False,
            web_enabled=False,
            network_enabled=False,
            subagents_enabled=False,
        ),
    )


def test_baseline_and_evolved_configs_are_paired_except_treatment() -> None:
    assert arm_parity_findings(_config("baseline"), _config("evolved")) == ()


def test_baseline_cannot_bind_memory() -> None:
    try:
        FrozenArtifactBinding(
            enabled=False,
            frozen_artifact_id="artifact",
            frozen_artifact_sha256=None,
            context_resolution_digest=None,
            resolved_memory_sha256=None,
        )
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("baseline accepted an evolution artifact")


def test_only_evolved_arm_can_enable_memory() -> None:
    baseline = _config("baseline")
    assert baseline.artifact.enabled is False
    assert baseline.artifact.frozen_artifact_id is None
    evolved = _config("evolved")
    assert evolved.artifact.enabled is True
    assert evolved.artifact.is_frozen is False
