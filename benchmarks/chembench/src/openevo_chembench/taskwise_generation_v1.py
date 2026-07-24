"""Immutable generation namespaces for taskwise canary and pilot evidence.

Generation identifiers are content-derived authorities.  They are used only
for path and run identity isolation; they never relax the paired-canary
authorization gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from openevo_chembench.taskwise_config_v1 import TaskwiseExperimentConfigV1
from openevo_chembench.taskwise_sampling_v1 import PILOT500_STREAM_SCOPES


GENERATION_SCHEMA_V1 = "taskwise_execution_generation_v1"
_GENERATION_ID_RE = re.compile(r"gen_[0-9a-f]{64}")


def derive_taskwise_generation_id_v1(authority: dict[str, Any]) -> str:
    """Derive one filesystem-safe generation id from closed public authority."""

    if type(authority) is not dict or not authority:
        raise TypeError("generation authority must be a non-empty dict")
    payload = {
        "schema_version": GENERATION_SCHEMA_V1,
        "authority": authority,
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return f"gen_{hashlib.sha256(encoded).hexdigest()}"


def validate_taskwise_generation_id_v1(generation_id: str) -> str:
    """Require the exact closed generation-id grammar."""

    if type(generation_id) is not str or _GENERATION_ID_RE.fullmatch(generation_id) is None:
        raise ValueError("taskwise generation id is invalid")
    return generation_id


def taskwise_canary_comparison_path_v1(
    repository_root: Path,
    generation_id: str,
) -> Path:
    generation = validate_taskwise_generation_id_v1(generation_id)
    return (
        repository_root.resolve()
        / "results"
        / "chembench4k_taskwise_online_v1"
        / "canary9"
        / "generations"
        / generation
        / "private"
        / "taskwise_comparison_v1.json"
    )


def taskwise_canary_receipt_path_v1(
    package_root: Path,
    generation_id: str,
) -> Path:
    generation = validate_taskwise_generation_id_v1(generation_id)
    return (
        package_root.resolve()
        / "state"
        / "taskwise_online_v1"
        / "canary9"
        / "generations"
        / generation
        / "paired_canary_receipt_v1.json"
    )


def taskwise_pilot_suite_state_path_v1(
    repository_root: Path,
    *,
    generation_id: str,
    arm: str,
) -> Path:
    generation = validate_taskwise_generation_id_v1(generation_id)
    if arm not in {"control", "online"}:
        raise ValueError("pilot suite arm is invalid")
    return (
        repository_root.resolve()
        / "results"
        / "chembench4k_taskwise_online_v1"
        / "pilot500_generations"
        / generation
        / f"{arm}_suite_state.json"
    )


def taskwise_pilot_comparison_path_v1(
    repository_root: Path,
    generation_id: str,
) -> Path:
    generation = validate_taskwise_generation_id_v1(generation_id)
    return (
        repository_root.resolve()
        / "results"
        / "chembench4k_taskwise_online_v1"
        / "pilot500_generations"
        / generation
        / "private"
        / "taskwise_stream_comparison_v1.json"
    )


def taskwise_pilot_runtime_config_v1(
    config: TaskwiseExperimentConfigV1,
    generation_id: str,
) -> TaskwiseExperimentConfigV1:
    """Resolve a frozen stream template into a generation-private runtime."""

    if type(config) is not TaskwiseExperimentConfigV1:
        raise TypeError("config must be exact TaskwiseExperimentConfigV1")
    if config.scope not in PILOT500_STREAM_SCOPES:
        raise ValueError("runtime generation applies only to pilot streams")
    generation = validate_taskwise_generation_id_v1(generation_id)
    return replace(
        config,
        run_name=f"{config.run_name}__{generation}",
        output_directory=(
            "OpenEvo/results/chembench4k_taskwise_online_v1/"
            f"pilot500_generations/{generation}/{config.scope}/{config.arm}"
        ),
    )


__all__ = [
    "GENERATION_SCHEMA_V1",
    "derive_taskwise_generation_id_v1",
    "taskwise_canary_comparison_path_v1",
    "taskwise_canary_receipt_path_v1",
    "taskwise_pilot_comparison_path_v1",
    "taskwise_pilot_runtime_config_v1",
    "taskwise_pilot_suite_state_path_v1",
    "validate_taskwise_generation_id_v1",
]
