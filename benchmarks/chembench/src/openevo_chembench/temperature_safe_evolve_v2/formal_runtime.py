"""One source-bound immutable wheel runtime for Temperature Safe-Evolve V2.

The audited V1 runtime builder already provides offline wheel construction,
inventory verification, atomic publication and source-commit binding.  V2 uses
that implementation with a distinct root/schema/source scope; this adapter is
the sole place where those experiment-local constants are configured.
"""

from __future__ import annotations

from pathlib import Path

from openevo_chembench.temperature_full_evolve_v1 import formal_runtime as _base

FORMAL_RUNTIME_SCHEMA = "TemperatureSafeEvolveFormalRuntimeReceiptV2"
FORMAL_RUNTIME_ROOT_RELATIVE = "state/chembench_temperature_safe_evolve_v2/formal_runtime_v1"
FORMAL_RUNTIME_PYTHON_RELATIVE = f"{FORMAL_RUNTIME_ROOT_RELATIVE}/runtime_venv/bin/python"
FORMAL_FRAMEWORK_LOCK_RELATIVE = f"{FORMAL_RUNTIME_ROOT_RELATIVE}/framework/framework-lock.json"
FORMAL_RUNTIME_RECEIPT_RELATIVE = f"{FORMAL_RUNTIME_ROOT_RELATIVE}/formal_runtime_receipt_v2.json"
FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE = (
    "state/chembench_temperature_safe_evolve_v2/formal_runtime_v1_failures"
)
_FORMAL_SOURCE_PATHS = (
    "src/openevo",
    "benchmarks/chembench/src",
    "benchmarks/chembench/configs/temperature_safe_evolve_v2",
    "benchmarks/chembench/scripts/temperature_safe_evolve_v2",
    "benchmarks/chembench/tests/temperature_safe_evolve_v2",
    "benchmarks/chembench/configs/temperature_full_evolve_v1/runtime_services_topology.yaml",
    "benchmarks/chembench/scripts/temperature_full_evolve_v1/gateway_service_bootstrap.py",
)

TemperatureFormalRuntimeError = _base.TemperatureFormalRuntimeError
TemperatureFormalRuntimeIdentityV1 = _base.TemperatureFormalRuntimeIdentityV1


def _configure() -> None:
    values = {
        "FORMAL_RUNTIME_SCHEMA": FORMAL_RUNTIME_SCHEMA,
        "FORMAL_RUNTIME_ROOT_RELATIVE": FORMAL_RUNTIME_ROOT_RELATIVE,
        "FORMAL_RUNTIME_PYTHON_RELATIVE": FORMAL_RUNTIME_PYTHON_RELATIVE,
        "FORMAL_FRAMEWORK_LOCK_RELATIVE": FORMAL_FRAMEWORK_LOCK_RELATIVE,
        "FORMAL_RUNTIME_RECEIPT_RELATIVE": FORMAL_RUNTIME_RECEIPT_RELATIVE,
        "FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE": FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE,
        "_FORMAL_SOURCE_PATHS": _FORMAL_SOURCE_PATHS,
    }
    for name, value in values.items():
        setattr(_base, name, value)


def prepare_temperature_safe_formal_runtime_v2(
    *, repository_root: Path, bootstrap_python: Path
) -> TemperatureFormalRuntimeIdentityV1:
    _configure()
    return _base.prepare_temperature_formal_runtime_v1(
        repository_root=repository_root,
        bootstrap_python=bootstrap_python,
    )


def load_temperature_safe_formal_runtime_v2(
    *, repository_root: Path
) -> TemperatureFormalRuntimeIdentityV1:
    _configure()
    return _base.load_temperature_formal_runtime_v1(repository_root=repository_root)


def require_temperature_safe_formal_runtime_python_v2(*, repository_root: Path) -> None:
    _configure()
    _base.require_temperature_formal_runtime_python_v1(repository_root=repository_root)


__all__ = [
    "FORMAL_FRAMEWORK_LOCK_RELATIVE",
    "FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE",
    "FORMAL_RUNTIME_PYTHON_RELATIVE",
    "FORMAL_RUNTIME_RECEIPT_RELATIVE",
    "FORMAL_RUNTIME_ROOT_RELATIVE",
    "FORMAL_RUNTIME_SCHEMA",
    "TemperatureFormalRuntimeError",
    "TemperatureFormalRuntimeIdentityV1",
    "load_temperature_safe_formal_runtime_v2",
    "prepare_temperature_safe_formal_runtime_v2",
    "require_temperature_safe_formal_runtime_python_v2",
]
