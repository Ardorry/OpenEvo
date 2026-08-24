from __future__ import annotations

from pathlib import Path

import pytest

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
