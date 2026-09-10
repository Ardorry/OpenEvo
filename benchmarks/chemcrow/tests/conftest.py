from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import pytest
from openevo.runtime.managed import (
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
    MANAGED_WORKSPACE,
)

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.models import TaskItem

_DEFAULT_TEST_DEPS_ROOT = Path(__file__).resolve().parents[4]


def _configured_fixture_root(
    *,
    env_var: str,
    default: Path,
    required_relative_path: Path,
    required_path_kind: Literal["directory", "file"],
) -> Path:
    configured = os.environ.get(env_var)
    if configured is not None and not configured.strip():
        pytest.fail(f"{env_var} is set but empty")

    source = env_var if configured is not None else "the default vendor fixture"
    root = Path(configured).expanduser() if configured is not None else default
    if not root.is_dir():
        pytest.fail(
            f"ChemCrow test fixture root from {source} is not a directory: {root}. "
            f"Set {env_var} to the corresponding repository root."
        )

    required_path = root / required_relative_path
    required_path_matches = (
        required_path.is_dir() if required_path_kind == "directory" else required_path.is_file()
    )
    if not required_path_matches:
        pytest.fail(
            f"ChemCrow test fixture root from {source} is missing required "
            f"{required_path_kind}: {required_path}. Set {env_var} to the "
            "corresponding repository root."
        )
    return root


@pytest.fixture
def task_item() -> TaskItem:
    body = {
        "task_id": "chemcrow-test-01",
        "prompt": "Explain a chemistry concept without physical execution.",
        "broad_category": "chemical_logic",
        "allowed_tool_metadata": {"registry": "test"},
        "safety_metadata": {"benchmark_only": True},
        "provenance": {
            "source_notebook": "tasks/test.ipynb",
            "source_sha256": "0" * 64,
            "extraction_method": "test",
        },
    }
    return TaskItem(**body, sanitized_item_sha256=canonical_sha256(body))


@pytest.fixture
def runs_root() -> Path:
    return _configured_fixture_root(
        env_var="CHEMCROW_TEST_RUNS_ROOT",
        default=_DEFAULT_TEST_DEPS_ROOT / "vendor" / "chemcrow-runs",
        required_relative_path=Path("tasks"),
        required_path_kind="directory",
    )


@pytest.fixture
def controlled_csv() -> Path:
    public_root = _configured_fixture_root(
        env_var="CHEMCROW_TEST_PUBLIC_ROOT",
        default=_DEFAULT_TEST_DEPS_ROOT / "vendor" / "chemcrow-public",
        required_relative_path=Path("chemcrow/data/chem_wep_smi.csv"),
        required_path_kind="file",
    )
    return public_root / "chemcrow" / "data" / "chem_wep_smi.csv"


@pytest.fixture
def core_candidate_config() -> dict:
    return {
        "timeout_seconds": 10,
        "runtime": {
            "backend": "docker",
            "profile": "managed_science",
            "container_user": "host",
            "image": "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b",
            "prepare": [{"type": "exec", "command": MANAGED_SUBSCRIPTION_PREPARE_COMMAND}],
            "env": {"HOME": MANAGED_HOME, "PATH": MANAGED_PATH},
            "network": "host",
            "workdir": MANAGED_WORKSPACE,
            "gpus": 0,
            "allow_internet": True,
            "import_path": None,
            "kwargs": {},
        },
        "agent": {
            "harness": "codex",
            "import_path": None,
            "model_name": "gpt-5.5",
            "settings": {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "reasoning_effort": "high",
                "reasoning_summary": "auto",
            },
            "env": {},
            "mcp_servers": [],
            "skills_path": None,
            "custom_shell": None,
        },
        "builder": {"strategy": "agent_transcript", "config": {}},
        "metadata": {"policy_version": "v1"},
    }
