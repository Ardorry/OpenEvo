#!/usr/bin/env python3
"""Freeze the Temperature 100/100 split and emit a zero-model-call preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo.evolution.framework import load_verified_framework_registry
from openevo.runtime.codex_isolation import codex_subscription_cli_overrides

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
    load_managed_candidate_codex_v2,
    load_managed_codex_v2,
    require_paid_runtime_python_v2,
)
from openevo_chembench.temperature_full_evolve_v1.config import (
    EXPECTED_CANDIDATE_IMAGE_ID,
    EXPECTED_CODEX_SHA256,
    load_temperature_full_evolve_config,
)
from openevo_chembench.temperature_full_evolve_v1.preflight import (
    RegressionReceiptV1,
    RuntimePreflightEvidenceV1,
    build_zero_model_preflight_v1,
    write_public_preflight_bundle_v1,
)
from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    audit_empty_temperature_runtime_services_v1,
    load_temperature_runtime_services_v1,
)
from openevo_chembench.temperature_full_evolve_v1.split import (
    build_temperature_split_v1,
    render_temperature_split_artifacts_v1,
    write_temperature_split_artifacts_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--private-split-root", required=True, type=Path)
    arguments = parser.parse_args()

    repository = REPOSITORY_ROOT.resolve(strict=True)
    destination, private_split_root = _validated_preflight_paths(
        repository=repository,
        destination=arguments.destination,
        private_split_root=arguments.private_split_root,
    )
    require_paid_runtime_python_v2(repository_root=repository)
    source_commit = _git(repository, "rev-parse", "HEAD")
    if _tracked_tree_dirty(repository) or _experiment_tree_not_frozen(repository):
        raise RuntimeError("SOURCE_IDENTITY_NOT_FROZEN")

    config_path = (
        repository / "benchmarks/chembench/configs/temperature_full_evolve_v1/"
        "temperature_full_evolve_v1.yaml"
    )
    config = load_temperature_full_evolve_config(config_path)
    dataset_root = repository / str(config.payload["dataset"]["root"])
    split = build_temperature_split_v1(ChemBench4KDatasetLoader(snapshot_root=dataset_root))

    candidate = load_managed_candidate_codex_v2(repository_root=repository)
    reflector = load_managed_codex_v2(repository_root=repository)
    codex_subscription_auth_source_v2()  # metadata validation only; content is never read here
    if (
        candidate.executable_sha256 != EXPECTED_CODEX_SHA256
        or reflector.executable_sha256 != EXPECTED_CODEX_SHA256
        or candidate.image_id != EXPECTED_CANDIDATE_IMAGE_ID
    ):
        raise RuntimeError("MANAGED_CODEX_IDENTITY_MISMATCH")

    services = load_temperature_runtime_services_v1(repository_root=repository)
    services_health = services.require_current()
    empty_inventory_before = audit_empty_temperature_runtime_services_v1(
        repository_root=repository
    )
    framework_lock = repository / config.framework_lock_relative
    registry = load_verified_framework_registry(framework_lock)
    required_methods = {"text_memory", "skill_bundle", "agent_system"}
    if not required_methods <= set(registry.snapshot.methods):
        raise RuntimeError("FRAMEWORK_REGISTRY_TARGET_METHODS_MISSING")

    completion_identity_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "service_run_id": services.service_run_id,
                "root": services.completion_root.payload,
                "rollout_save_dir_bound": True,
                "gateway_auxiliary_path_bound": True,
            }
        )
    )
    completion_only_overrides = codex_subscription_cli_overrides(
        allow_internet=True,
        tools_enabled=False,
    )
    if not {
        'web_search="disabled"',
        "features.shell_tool=false",
        "features.unified_exec=false",
        (
            "permissions.openevo_codex_subscription_v1.network.enabled="
            "true"
        ),
    } <= set(completion_only_overrides):
        raise RuntimeError("MANAGED_CODEX_COMPLETION_ONLY_POLICY_MISMATCH")
    regression = _run_regression_suites(repository)
    empty_inventory_after = audit_empty_temperature_runtime_services_v1(
        repository_root=repository
    )
    if empty_inventory_after != empty_inventory_before:
        raise RuntimeError("PREFLIGHT_RUNTIME_INVENTORY_CHANGED")
    runtime = RuntimePreflightEvidenceV1(
        candidate_codex_executable_sha256=candidate.executable_sha256,
        reflector_codex_executable_sha256=reflector.executable_sha256,
        candidate_image_id=candidate.image_id,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        capture_mode="transcript",
        credential_metadata_passed=True,
        credential_content_read=False,
        managed_runtime_passed=True,
        framework_registry_passed=True,
        framework_lock_sha256=_file_sha256(framework_lock),
        runtime_services_ready=True,
        runtime_services_identity_sha256=services.digest,
        completion_persistence_shared=True,
        completion_persistence_identity_sha256=completion_identity_sha256,
        empty_runtime_inventory_sha256=str(empty_inventory_before["inventory_sha256"]),
        rollout_task_count_before_first_call=0,
        persisted_task_directory_count_before_first_call=0,
        model_visible_tools_enabled=False,
        provider_transport_network_enabled=True,
        model_calls=0,
    )
    bundle = build_zero_model_preflight_v1(
        config=config,
        split=split,
        runtime=runtime,
        regression=regression,
        source_commit=source_commit,
        source_tree_clean=True,
    )

    write_temperature_split_artifacts_v1(
        render_temperature_split_artifacts_v1(split),
        destination=private_split_root,
    )
    _write_private_split_closure(split=split, destination=private_split_root / "private")
    write_public_preflight_bundle_v1(bundle, destination=destination)
    write_public_file(
        destination / "EXPERIMENT_PROTOCOL.md",
        (
            repository / "benchmarks/chembench/configs/temperature_full_evolve_v1/"
            "EXPERIMENT_PROTOCOL.md"
        ).read_bytes(),
    )
    output = {
        "status": bundle.report["status"],
        "source_commit": source_commit,
        "split_sha256": split.plan.split_sha256,
        "config_sha256": config.digest,
        "runtime_services_identity_sha256": services.digest,
        "service_run_id": services.service_run_id,
        "framework_registry_digest": registry.snapshot.registry_digest,
        "preflight_bundle_sha256": bundle.digest,
        "runtime_health_receipt_sha256": sha256_bytes(canonical_json_bytes(services_health)),
        "preflight_model_calls": 0,
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


def _validated_preflight_paths(
    *,
    repository: Path,
    destination: Path,
    private_split_root: Path,
) -> tuple[Path, Path]:
    """Keep private split evidence inside one owner-local preflight namespace."""

    root = (
        repository
        / "state/chembench_temperature_full_evolve_v1/preflights"
    ).resolve()
    public = destination.absolute().resolve()
    private = private_split_root.absolute().resolve()
    try:
        public_relative = public.relative_to(root)
        private_relative = private.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("PREFLIGHT_PATH_OUTSIDE_OWNER_PRIVATE_STATE") from exc
    if (
        len(public_relative.parts) < 2
        or len(private_relative.parts) < 2
        or public_relative.parts[0] != private_relative.parts[0]
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}",
            public_relative.parts[0],
            re.ASCII,
        )
        is None
        or public == private
        or public.is_relative_to(private)
        or private.is_relative_to(public)
    ):
        raise RuntimeError("PREFLIGHT_PATH_NAMESPACE_INVALID")
    return public, private


def _write_private_split_closure(*, split: object, destination: Path) -> None:
    # Import-time type annotation is intentionally avoided to keep this CLI small;
    # callers can only reach this helper with the exact split constructed above.
    plan = split.plan
    eligible = canonical_pretty_json_bytes(
        {
            "schema_version": "TemperatureEligiblePoolPrivateManifestV1",
            "historical_exposure_policy": "fixed_official_temperature_pool_fresh_c0",
            "uids": sorted((*plan.train_uids, *plan.test_uids, *plan.reserve_uids)),
            "count": 202,
            "split_sha256": plan.split_sha256,
        }
    )
    groups = canonical_pretty_json_bytes(
        {
            "schema_version": "TemperatureNearDuplicateGroupPrivateManifestV1",
            "groups": [list(group) for group in plan.groups],
            "group_manifest_sha256": plan.group_manifest_sha256,
            "group_assignment_sha256": plan.group_assignment_sha256,
        }
    )
    write_private_file(destination / "eligible_pool_manifest.json", eligible, replace=False)
    write_private_file(destination / "near_duplicate_groups.json", groups, replace=False)


def _tracked_tree_dirty(repository: Path) -> bool:
    completed = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=no"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return bool(completed.stdout.strip())


def _experiment_tree_not_frozen(repository: Path) -> bool:
    """Require every formal experiment source, test and config to be tracked.

    The repository intentionally contains unrelated user-owned untracked files,
    so the global cleanliness check ignores untracked paths.  This scoped check
    closes the corresponding gap for every path that can define or validate the
    formal run.
    """

    experiment_paths = (
        # Formal PYTHONPATH roots are closed in full so an untracked module
        # cannot shadow an audited dependency outside this experiment package.
        "src/openevo",
        "benchmarks/chembench/src",
        "benchmarks/chembench/configs/temperature_full_evolve_v1",
        "benchmarks/chembench/scripts/temperature_full_evolve_v1",
        "benchmarks/chembench/tests/temperature_full_evolve_v1",
    )
    completed = subprocess.run(
        (
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *experiment_paths,
        ),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return bool(completed.stdout.strip())


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed.stdout.strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_PYTEST_PASSED = re.compile(r"(?:^|\s)([0-9]+) passed(?:[,\s]|$)")
_FOCUSED_TESTS = ("benchmarks/chembench/tests/temperature_full_evolve_v1",)
_INTEGRATION_TESTS = (
    "benchmarks/chembench/tests/supervised_transfer_v2/test_executor_v2.py",
    "benchmarks/chembench/tests/supervised_transfer_v2/test_final_test_safety_v2.py",
    "benchmarks/chembench/tests/supervised_transfer_v2/test_managed_codex_v2.py",
    "benchmarks/chembench/tests/supervised_transfer_v2/test_reflector_contract_v2.py",
    "benchmarks/chembench/tests/supervised_transfer_v2/test_runtime_services_v2.py",
    "benchmarks/chembench/tests/supervised_transfer_v2/test_gateway_service_bootstrap_v2.py",
    "tests/evolution/test_planned_jobs.py",
    "tests/evolution/test_registry_worker_dispatch.py",
    "tests/evolution/test_context_projection_resolver.py",
    "tests/evolution/test_context_materialization.py",
    "tests/runtime/test_codex_isolation.py",
    "tests/test_evolution_agent_harnesses.py",
)


def _run_regression_suites(repository: Path) -> RegressionReceiptV1:
    """Run the fixed zero-model suites and derive the receipt from subprocess evidence.

    Counts, failure status, command identity and output identity are computed here;
    callers cannot self-attest them through CLI arguments.  The selected tests are
    local unit/integration tests and the environment explicitly disables the paid
    call gate.
    """

    test_python = (repository / ".venv/bin/python").resolve(strict=True)
    if repository not in test_python.parents:
        raise RuntimeError("REGRESSION_PYTHON_OUTSIDE_REPOSITORY")
    commands = (
        (str(test_python), "-m", "pytest", "-q", *_FOCUSED_TESTS),
        (str(test_python), "-m", "pytest", "-q", *_INTEGRATION_TESTS),
    )
    # Regression subprocesses receive a minimal, non-secret environment.  In
    # particular no API key, credential, proxy, MCP or caller-specific runtime
    # variable is inherited even though the paid-call gates are also closed.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TZ")
        if key in os.environ
    }
    environment.update(
        {
            "PYTHONPATH": "benchmarks/chembench/src:src",
            "PYTHONHASHSEED": "0",
            "OPENEVO_ALLOW_PAID_CALLS": "0",
            "CHEMBENCH_ALLOW_PAID_CALLS": "0",
        }
    )
    outputs: list[bytes] = []
    counts: list[int] = []
    state_root = repository / "state/chembench_temperature_full_evolve_v1/preflight"
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    with tempfile.TemporaryDirectory(prefix="pytest-", dir=state_root) as temporary:
        for index, command in enumerate(commands):
            completed = subprocess.run(
                (*command, "--basetemp", str(Path(temporary) / f"suite-{index}")),
                cwd=repository,
                env=environment,
                check=False,
                capture_output=True,
                timeout=1_800,
            )
            output = completed.stdout + completed.stderr
            outputs.append(output)
            if completed.returncode != 0:
                raise RuntimeError(f"REGRESSION_SUITE_{index + 1}_FAILED")
            try:
                rendered = output.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise RuntimeError("REGRESSION_OUTPUT_NOT_UTF8") from exc
            matches = _PYTEST_PASSED.findall(rendered)
            if not matches or int(matches[-1]) < 1:
                raise RuntimeError("REGRESSION_PASS_COUNT_UNPROVEN")
            counts.append(int(matches[-1]))
    return RegressionReceiptV1(
        focused_test_count=counts[0],
        focused_failure_count=0,
        integration_test_count=counts[1],
        integration_failure_count=0,
        command_sha256=sha256_bytes(canonical_json_bytes(commands)),
        output_sha256=sha256_bytes(b"\n---SUITE-BOUNDARY---\n".join(outputs)),
        model_calls=0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
