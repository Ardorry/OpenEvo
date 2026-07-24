from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from openevo_chembench.taskwise_config_v1 import (
    CONTROL_PROTOCOL_ID,
    FULL_STREAM_SCOPES,
    ONLINE_PROTOCOL_ID,
    PILOT500_STREAM_SCOPES,
    PROTOCOL_MARKERS,
    SOURCE_COMMIT_PLACEHOLDER,
    TASKWISE_MEMORY_LIMITS_V1,
    TaskwiseConfigError,
    TaskwiseEvolutionPolicyV1,
    TaskwiseExecutorPolicyV1,
    TaskwiseExperimentConfigV1,
    TaskwiseMemoryLimitsV1,
    load_taskwise_config_v1,
    taskwise_arm_parity_findings,
)


def _config(*, arm: str) -> TaskwiseExperimentConfigV1:
    online = arm == "online"
    return TaskwiseExperimentConfigV1(
        protocol_id=ONLINE_PROTOCOL_ID if online else CONTROL_PROTOCOL_ID,
        protocol_markers=PROTOCOL_MARKERS,
        arm=arm,  # type: ignore[arg-type]
        scope="canary9",
        run_name=f"{arm}_canary9",
        output_directory=f"OpenEvo/results/chembench4k_taskwise_v1/canary9/{arm}",
        dataset_root="data/chembench4k/snapshot",
        dataset_manifest="data/chembench4k/snapshot/dataset_manifest.json",
        task_manifest="OpenEvo/benchmarks/chembench/manifests/online_v1/canary9_public.jsonl",
        private_task_manifest=(
            "OpenEvo/benchmarks/chembench/private_manifests/online_v1/canary9_private.jsonl"
        ),
        codex_cli_version="0.144.6",
        prompt_renderer_id="official_category_five_shot_v1",
        parser_id="official_first_capital_v1",
        evaluator_id="chembench4k_private_accuracy_v1",
        attempts_per_task=3,
        inter_round_slots=2,
        evolution_updates_per_task=2 if online else 0,
        carry_memory_across_tasks=online,
        fixed_round_budget=True,
        stop_when_correct=False,
        final_score_round=2,
        memory_limits=TASKWISE_MEMORY_LIMITS_V1,
        evolution=TaskwiseEvolutionPolicyV1(
            enabled=online,
            target="text_memory",
            method_id="text_memory_expel_reflector" if online else None,
        ),
        executor=TaskwiseExecutorPolicyV1(
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
        source_commit=SOURCE_COMMIT_PLACEHOLDER,
    )


def test_online_and_control_fixed_budget_contracts() -> None:
    control = _config(arm="control")
    online = _config(arm="online")
    assert control.attempts_per_task == online.attempts_per_task == 3
    assert control.stop_when_correct is online.stop_when_correct is False
    assert control.final_score_round == online.final_score_round == 2
    assert control.evolution_updates_per_task == 0
    assert online.evolution_updates_per_task == 2
    assert control.carry_memory_across_tasks is False
    assert online.carry_memory_across_tasks is True
    assert taskwise_arm_parity_findings(control, online) == ()
    assert control.memory_limits == online.memory_limits == TASKWISE_MEMORY_LIMITS_V1
    assert control.memory_limits.to_payload() == {
        "max_utf8_bytes": 16384,
        "max_estimated_tokens": 4096,
        "max_items_per_section": 24,
        "required_sections": [
            "Do",
            "Avoid",
            "Validate",
            "When Applicable",
            "Retired Or Superseded",
        ],
        "token_estimator_id": "ascii_word_or_unicode_codepoint_v1",
        "parser_id": "exact_markdown_sections_v1",
    }
    assert control.source_commit_is_explicit is online.source_commit_is_explicit is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda config: replace(config, attempts_per_task=2),
        lambda config: replace(config, stop_when_correct=True),
        lambda config: replace(config, fixed_round_budget=False),
        lambda config: replace(config, final_score_round=1),
        lambda config: replace(config, evolution_updates_per_task=3),
        lambda config: replace(config, carry_memory_across_tasks=False),
    ],
)
def test_online_budget_and_treatment_cannot_drift(mutator) -> None:
    with pytest.raises(TaskwiseConfigError):
        mutator(_config(arm="online"))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_utf8_bytes", 16383),
        ("max_estimated_tokens", 4095),
        ("max_items_per_section", 23),
        (
            "required_sections",
            ("Do", "Avoid", "Validate", "When Applicable"),
        ),
        ("token_estimator_id", "heuristic_v2"),
        ("parser_id", "permissive_markdown_v2"),
    ],
)
def test_memory_limit_policy_cannot_drift(field_name: str, value: object) -> None:
    payload = TASKWISE_MEMORY_LIMITS_V1.to_payload()
    payload["required_sections"] = tuple(payload["required_sections"])
    payload[field_name] = value
    with pytest.raises(TaskwiseConfigError, match="protocol-fixed"):
        TaskwiseMemoryLimitsV1(**payload)


def test_parity_detects_non_treatment_difference() -> None:
    control = _config(arm="control")
    online = replace(_config(arm="online"), prompt_renderer_id="changed")
    assert taskwise_arm_parity_findings(control, online) == ("ARM_PARITY_MISMATCH",)


def test_source_commit_may_be_clean_head_template_or_explicit_pin() -> None:
    config = _config(arm="online")
    assert not config.source_commit_is_explicit
    frozen = replace(config, source_commit="a" * 40)
    assert frozen.source_commit_is_explicit
    with pytest.raises(TaskwiseConfigError):
        replace(config, source_commit="main")


def test_all_ten_pilot_stream_configs_are_closed_and_paired() -> None:
    config_root = Path(__file__).resolve().parents[1] / "configs"
    outputs: set[str] = set()
    for scope in PILOT500_STREAM_SCOPES:
        control = load_taskwise_config_v1(config_root / f"control_{scope}_taskwise_online_v1.yaml")
        online = load_taskwise_config_v1(config_root / f"online_{scope}_taskwise_online_v1.yaml")
        assert taskwise_arm_parity_findings(control, online) == ()
        assert control.scope == online.scope == scope
        assert control.output_directory not in outputs
        assert online.output_directory not in outputs
        outputs.update((control.output_directory, online.output_directory))


def test_all_ten_full_stream_configs_are_closed_variable_length_and_paired() -> None:
    config_root = Path(__file__).resolve().parents[1] / "configs"
    outputs: set[str] = set()
    for scope in FULL_STREAM_SCOPES:
        control = load_taskwise_config_v1(config_root / f"control_{scope}_taskwise_online_v1.yaml")
        online = load_taskwise_config_v1(config_root / f"online_{scope}_taskwise_online_v1.yaml")
        assert taskwise_arm_parity_findings(control, online) == ()
        assert control.scope == online.scope == scope
        assert control.task_manifest == online.task_manifest
        assert control.private_task_manifest == online.private_task_manifest
        assert control.output_directory not in outputs
        assert online.output_directory not in outputs
        outputs.update((control.output_directory, online.output_directory))


def test_full_suite_configs_bind_all_streams_and_exact_4009_task_count() -> None:
    config_root = Path(__file__).resolve().parents[1] / "configs"
    suites = {
        arm: yaml.safe_load(
            (config_root / f"{arm}_full_taskwise_online_v1.yaml").read_text(encoding="utf-8")
        )
        for arm in ("control", "online")
    }
    expected_counts = [401, 401, 401, 401, 401, 401, 401, 401, 401, 400]
    for arm, suite in suites.items():
        assert suite["stream_count"] == 10
        assert suite["stream_task_counts"] == expected_counts
        assert sum(suite["stream_task_counts"]) == suite["total_item_count"] == 4009
        assert suite["reset_memory_between_streams"] is True
        assert suite["stream_configs"] == [
            f"{arm}_{scope}_taskwise_online_v1.yaml" for scope in FULL_STREAM_SCOPES
        ]
    for field_name in (
        "schema_version",
        "stream_design",
        "stream_count",
        "stream_task_counts",
        "total_item_count",
        "reset_memory_between_streams",
        "suite_summary",
        "source_commit",
    ):
        assert suites["control"][field_name] == suites["online"][field_name]
