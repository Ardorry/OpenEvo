from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path("/home/lhy-h/work/researchclaw_openevo")
EXPERIMENT = ROOT / "experiments/sequential_task_reflector_evolution_v0"
SCRIPTS = EXPERIMENT / "scripts"


def _load(name: str) -> ModuleType:
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"formal_v11_asset_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_formal_v11_deployer_rejects_existing_receipt_before_remote_effect(
    tmp_path: Path,
) -> None:
    deployer = _load("deploy_formal_v11_core")
    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    log_root = EXPERIMENT / "logs" / f"pytest-formal-v11-{suffix}"
    log_root.mkdir(mode=0o700)
    try:
        existing = log_root / "deploy.json"
        existing.write_text("historical\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not a new experiment-owned path"):
            deployer._require_new_receipt_path(existing)
        assert existing.read_text(encoding="utf-8") == "historical\n"
    finally:
        existing.unlink(missing_ok=True)
        log_root.rmdir()


def test_formal_v11_deployer_pins_source_lifecycle_and_runtime_boundary() -> None:
    deployer = _load("deploy_formal_v11_core")
    source = (SCRIPTS / "deploy_formal_v11_core.py").read_text(encoding="utf-8")

    assert deployer.V2_DAEMON_LIFECYCLE_COMPATIBILITY == 95
    assert "args.expected_lifecycle != V2_DAEMON_LIFECYCLE_COMPATIBILITY" in source
    assert "_require_new_receipt_path(args.receipt)" in source
    assert "candidate_started" in source
    assert "reflector_started" in source
    assert "judge_started" in source
    assert '"model_started": False' in source


def test_formal_v11_bootstrap_and_readiness_are_no_mutation_surfaces() -> None:
    bootstrap = (SCRIPTS / "build_formal_v11_bootstrap.py").read_text(
        encoding="utf-8"
    )
    readiness = (SCRIPTS / "export_formal_v11_readiness.py").read_text(
        encoding="utf-8"
    )

    for claim in (
        '"candidate_execution_allowed": False',
        '"judge_execution_allowed": False',
        '"evolution_execution_allowed": False',
        '"official_execution_allowed": False',
        '"recovery_creation_allowed": False',
        '"model_operations_allowed": 0',
    ):
        assert claim in bootstrap
    assert 'client.json("GET", ENDPOINT, timeout_seconds=150.0)' in readiness
    assert '"codex_cli_started": True' in readiness
    assert '"model_started": False' in readiness
    assert "--runtime-output" in readiness


def test_formal_v11_evaluator_lock_builder_is_append_only_and_no_model() -> None:
    source = (SCRIPTS / "build_formal_v11_evaluator_lock.py").read_text(
        encoding="utf-8"
    )

    assert "write_evaluator_dependency_lock(destination)" in source
    assert '"secret_recorded": False' in source
    assert '"model_started": False' in source
    assert '"judge_request_started": False' in source
    assert "os.O_EXCL" in (
        ROOT
        / "OpenEvo/benchmarks/researchclawbench/src/"
        "openevo_researchclawbench/evaluator_dependency_lock.py"
    ).read_text(encoding="utf-8")
