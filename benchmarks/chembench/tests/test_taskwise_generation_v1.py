from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1
from openevo_chembench.taskwise_generation_v1 import (
    derive_taskwise_generation_id_v1,
    taskwise_canary_comparison_path_v1,
    taskwise_canary_receipt_path_v1,
    taskwise_pilot_comparison_path_v1,
    taskwise_pilot_runtime_config_v1,
    taskwise_pilot_suite_state_path_v1,
    validate_taskwise_generation_id_v1,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]


def test_generation_id_and_paths_are_byte_deterministic() -> None:
    authority = {
        "source_commit": "a" * 40,
        "control_run_id": "control_generation_a",
        "online_run_id": "online_generation_a",
    }
    first = derive_taskwise_generation_id_v1(authority)
    second = derive_taskwise_generation_id_v1(dict(reversed(tuple(authority.items()))))

    assert first == second
    assert validate_taskwise_generation_id_v1(first) == first
    assert taskwise_canary_receipt_path_v1(PACKAGE_ROOT, first) == (
        PACKAGE_ROOT
        / "state"
        / "taskwise_online_v1"
        / "canary9"
        / "generations"
        / first
        / "paired_canary_receipt_v1.json"
    )
    assert first in taskwise_canary_comparison_path_v1(REPOSITORY_ROOT, first).parts
    assert first in taskwise_pilot_comparison_path_v1(REPOSITORY_ROOT, first).parts
    assert (
        first
        in taskwise_pilot_suite_state_path_v1(
            REPOSITORY_ROOT,
            generation_id=first,
            arm="online",
        ).parts
    )


def test_generation_namespaces_preserve_old_evidence(tmp_path: Path) -> None:
    old_generation = derive_taskwise_generation_id_v1({"generation": "old"})
    new_generation = derive_taskwise_generation_id_v1({"generation": "new"})
    old_path = taskwise_canary_receipt_path_v1(tmp_path, old_generation)
    new_path = taskwise_canary_receipt_path_v1(tmp_path, new_generation)
    old_path.parent.mkdir(parents=True)
    old_path.write_text("immutable-old-evidence", encoding="utf-8")

    assert old_path != new_path
    assert old_path.read_text(encoding="utf-8") == "immutable-old-evidence"
    assert not new_path.exists()


def test_pilot_runtime_namespace_is_generation_private() -> None:
    template = load_taskwise_config_v1(
        PACKAGE_ROOT / "configs" / "online_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    first = derive_taskwise_generation_id_v1({"attempt": 1})
    second = derive_taskwise_generation_id_v1({"attempt": 2})
    runtime_first = taskwise_pilot_runtime_config_v1(template, first)
    runtime_second = taskwise_pilot_runtime_config_v1(template, second)

    assert runtime_first.scope == runtime_second.scope == template.scope
    assert runtime_first.task_manifest == runtime_second.task_manifest == template.task_manifest
    assert (
        runtime_first.private_task_manifest
        == runtime_second.private_task_manifest
        == template.private_task_manifest
    )
    assert runtime_first.run_name != runtime_second.run_name
    assert runtime_first.output_directory != runtime_second.output_directory
    assert first in runtime_first.run_name
    assert first in runtime_first.output_directory


@pytest.mark.parametrize(
    "value",
    ("", "generation_abc", "gen_abc", f"gen_{'A' * 64}", "../gen_" + "a" * 64),
)
def test_generation_id_rejects_unsafe_or_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="generation id"):
        validate_taskwise_generation_id_v1(value)
