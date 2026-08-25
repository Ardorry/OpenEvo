from __future__ import annotations

from pathlib import Path

import pytest
from openevo.runtime.managed import (
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
    MANAGED_WORKSPACE,
)

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.models import TaskItem


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
    return Path(__file__).resolve().parents[4] / "vendor" / "chemcrow-runs"


@pytest.fixture
def controlled_csv() -> Path:
    return (
        Path(__file__).resolve().parents[4]
        / "vendor"
        / "chemcrow-public"
        / "chemcrow"
        / "data"
        / "chem_wep_smi.csv"
    )


@pytest.fixture
def core_candidate_config() -> dict:
    return {
        "timeout_seconds": 10,
        "runtime": {
            "backend": "docker",
            "profile": "managed_science",
            "container_user": "host",
            "image": "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b",
            "prepare": [
                {"type": "exec", "command": MANAGED_SUBSCRIPTION_PREPARE_COMMAND}
            ],
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
