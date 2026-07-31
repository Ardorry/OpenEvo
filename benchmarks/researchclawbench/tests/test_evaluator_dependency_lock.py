from __future__ import annotations

import json
from pathlib import Path

import pytest
from openevo_researchclawbench.evaluator_dependency_lock import (
    ROOT_DISTRIBUTIONS,
    EvaluatorDependencyLockError,
    validate_evaluator_dependency_lock,
    write_evaluator_dependency_lock,
)


def test_evaluator_dependency_lock_round_trip_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "evaluator-dependency-lock.json"
    created = write_evaluator_dependency_lock(path)

    assert validate_evaluator_dependency_lock(path) == created
    assert created["root_distributions"] == ROOT_DISTRIBUTIONS
    assert {item["name"] for item in created["distributions"]} >= {
        "openai",
        "packaging",
        "structai",
    }
    assert all(item["record_sha256"] for item in created["distributions"])
    with pytest.raises(FileExistsError):
        write_evaluator_dependency_lock(path)


def test_evaluator_dependency_lock_tamper_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "evaluator-dependency-lock.json"
    write_evaluator_dependency_lock(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["distributions"][0]["version"] = "0.0.0-tampered"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        EvaluatorDependencyLockError, match="environment drifted"
    ):
        validate_evaluator_dependency_lock(path)


def test_evaluator_dependency_lock_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    write_evaluator_dependency_lock(target)
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)

    with pytest.raises(EvaluatorDependencyLockError, match="unsafe"):
        validate_evaluator_dependency_lock(alias)
