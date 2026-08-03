#!/usr/bin/env python3
"""Run the Safe-Evolve V2 zero-model-call formal admission gate."""

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

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

from openevo.evolution.framework.runtime import load_verified_framework_registry
from openevo.runtime.codex_isolation import codex_subscription_cli_overrides

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
)
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
    load_managed_candidate_codex_v2,
    load_managed_codex_v2,
)
from openevo_chembench.temperature_full_evolve_v1.config import (
    EXPECTED_CANDIDATE_IMAGE_ID,
)
from openevo_chembench.temperature_safe_evolve_v2.candidate import (
    CandidateContextV2,
    prepare_candidate_call_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.config import (
    EXPECTED_CODEX_SHA256,
    load_safe_evolve_config_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.folds import (
    build_safe_folds_v2,
    private_fold_manifest_bytes_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.formal_runtime import (
    load_temperature_safe_formal_runtime_v2,
    require_temperature_safe_formal_runtime_python_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.phase_a import (
    load_phase_a_inputs_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.preflight import (
    SafePreflightError,
    build_zero_call_preflight_v2,
    verify_prior_runtime_v8_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    audit_empty_temperature_safe_runtime_services_v2,
    load_temperature_safe_runtime_services_v2,
)

_PREFLIGHT_ID = re.compile(r"preflight-20[0-9]{6}T[0-9]{6}Z-[0-9a-f]{8}\Z")
_PASSED = re.compile(r"(?:^|\s)([0-9]+) passed(?:[,\s]|$)")


class _EmptyLedger:
    def accepted_call(self, _logical: str) -> None:
        return None

    def latest_claim(self, _logical: str) -> None:
        return None

    def failure_has_no_completion(self, _call_id: str) -> bool:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-id", required=True)
    arguments = parser.parse_args()
    if _PREFLIGHT_ID.fullmatch(arguments.preflight_id) is None:
        parser.error("--preflight-id is invalid")
    repository = REPOSITORY_ROOT.resolve(strict=True)
    if not sys.flags.isolated:
        raise SafePreflightError("SAFE_PREFLIGHT_ISOLATED_RUNTIME_REQUIRED")
    require_temperature_safe_formal_runtime_python_v2(repository_root=repository)
    formal = load_temperature_safe_formal_runtime_v2(repository_root=repository)
    source_commit = _git(repository, "rev-parse", "HEAD")
    branch = _git(repository, "branch", "--show-current")
    if _tracked_dirty(repository) or _experiment_paths_dirty(repository):
        raise SafePreflightError("SAFE_PREFLIGHT_SOURCE_NOT_FROZEN")
    config = load_safe_evolve_config_v2(
        repository / "benchmarks/chembench/configs/temperature_safe_evolve_v2/"
        "temperature_safe_evolve_v2.yaml"
    )
    phase_inputs = load_phase_a_inputs_v2(repository_root=repository, config=config)
    loader = ChemBench4KDatasetLoader(
        snapshot_root=repository / str(config.payload["dataset"]["root"])
    )
    folds = build_safe_folds_v2(loader)
    candidate = load_managed_candidate_codex_v2(repository_root=repository)
    reflector = load_managed_codex_v2(repository_root=repository)
    codex_subscription_auth_source_v2()
    if (
        candidate.executable_sha256 != EXPECTED_CODEX_SHA256
        or reflector.executable_sha256 != EXPECTED_CODEX_SHA256
        or candidate.image_id != EXPECTED_CANDIDATE_IMAGE_ID
    ):
        raise SafePreflightError("SAFE_PREFLIGHT_MANAGED_CODEX_IDENTITY_MISMATCH")
    overrides = set(codex_subscription_cli_overrides(allow_internet=True, tools_enabled=False))
    if (
        not {
            'web_search="disabled"',
            "features.shell_tool=false",
            "features.unified_exec=false",
        }
        <= overrides
    ):
        raise SafePreflightError("SAFE_PREFLIGHT_ZERO_TOOL_POLICY_INVALID")
    services = load_temperature_safe_runtime_services_v2(repository_root=repository)
    health = services.require_current()
    empty_before = audit_empty_temperature_safe_runtime_services_v2(repository_root=repository)
    registry = load_verified_framework_registry(formal.framework_lock)
    if not {"text_memory", "skill_bundle"} <= set(registry.snapshot.methods):
        raise SafePreflightError("SAFE_PREFLIGHT_CORE_METHODS_MISSING")
    _validate_generation_zero_dedup(
        phase_inputs=phase_inputs,
        service_identity_sha256=services.digest,
        campaign_run_id="stv3-temperature-safe-evolve-v2-20990101T000000Z",
    )
    tests = _run_regressions(repository)
    empty_after = audit_empty_temperature_safe_runtime_services_v2(repository_root=repository)
    prior_v8 = verify_prior_runtime_v8_v2(repository)
    if empty_before != empty_after:
        raise SafePreflightError("SAFE_PREFLIGHT_RUNTIME_INVENTORY_CHANGED")
    bundle = build_zero_call_preflight_v2(
        config=config,
        frozen_c4=phase_inputs.frozen_c4,
        folds=folds,
        phase_a_half_sha256=phase_inputs.half_assignment_sha256,
        source_commit=source_commit,
        branch=branch,
        source_tree_clean=True,
        formal_runtime_receipt=formal.receipt,
        formal_runtime_identity_sha256=formal.digest,
        formal_runtime_receipt_sha256=formal.receipt_sha256,
        runtime_services_run_id=services.service_run_id,
        runtime_services_identity_sha256=services.digest,
        runtime_services_health_sha256=sha256_bytes(canonical_json_bytes(health)),
        prior_runtime_v8_receipt=prior_v8,
        candidate_codex_sha256=candidate.executable_sha256,
        reflector_codex_sha256=reflector.executable_sha256,
        candidate_image_id=candidate.image_id,
        framework_registry_digest=registry.snapshot.registry_digest,
        framework_lock_sha256=_file_sha256(formal.framework_lock),
        credential_metadata_passed=True,
        empty_inventory_before_sha256=str(empty_before["inventory_sha256"]),
        empty_inventory_after_sha256=str(empty_after["inventory_sha256"]),
        test_receipt=tests,
    )
    root = repository / config.state_root_relative / "preflights" / arguments.preflight_id
    _fresh_private_root(root)
    private = root / "private"
    public = root / "public"
    private.mkdir(mode=0o700)
    public.mkdir(mode=0o700)
    _write(private / "fold_manifest.jsonl", private_fold_manifest_bytes_v2(folds))
    _write(
        private / "phase_a_test_manifest.jsonl",
        b"".join(
            canonical_json_bytes(
                {
                    "schema_version": "TemperatureSafePhaseATestPrivateRecordV2",
                    "ordinal": ordinal,
                    "uid": task.uid,
                    "target": task.target,
                }
            )
            for ordinal, task in enumerate(phase_inputs.test)
        ),
    )
    _write(
        private / "phase_a_half_manifest.json",
        canonical_json_bytes(
            {
                "schema_version": "TemperatureSafePhaseAHalfPrivateManifestV2",
                "h0_uids": sorted(phase_inputs.h0_uids),
                "h1_uids": sorted(phase_inputs.h1_uids),
                "half_assignment_sha256": phase_inputs.half_assignment_sha256,
            }
        ),
    )
    _write(
        private / "near_duplicate_groups.json",
        canonical_json_bytes(
            {
                "schema_version": "TemperatureSafeNearDuplicatePrivateManifestV2",
                "groups": [list(group) for group in folds.plan.groups],
                "group_manifest_sha256": folds.plan.group_manifest_sha256,
            }
        ),
    )
    documents = {
        "preflight_report.json": bundle.report,
        "frozen_c4_artifact_receipt.json": bundle.frozen_c4_receipt,
        "fold_manifest.json": bundle.fold_manifest,
        "near_duplicate_group_manifest.json": bundle.near_duplicate_manifest,
        "model_identity_receipt.json": bundle.model_identity_receipt,
        "runtime_identity_receipt.json": bundle.runtime_identity_receipt,
        "source_code_receipt.json": bundle.source_code_receipt,
        "preflight_test_receipt.json": bundle.preflight_test_receipt,
    }
    for name, value in documents.items():
        _write(public / name, canonical_pretty_json_bytes(value))
    _write(
        public / "PRECHECK_REPORT.md",
        _precheck_markdown(bundle.report, bundle.digest).encode("utf-8"),
    )
    _write(
        public / "EXPERIMENT_PROTOCOL.md",
        (
            repository / "benchmarks/chembench/configs/temperature_safe_evolve_v2/"
            "EXPERIMENT_PROTOCOL.md"
        ).read_bytes(),
    )
    result = {
        **bundle.report,
        "preflight_id": arguments.preflight_id,
        "preflight_root": root.relative_to(repository).as_posix(),
        "preflight_bundle_sha256": bundle.digest,
        "service_run_id": services.service_run_id,
        "framework_registry_digest": registry.snapshot.registry_digest,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _validate_generation_zero_dedup(
    *, phase_inputs: object, service_identity_sha256: str, campaign_run_id: str
) -> None:
    task = phase_inputs.test[0]
    prompt = render_official_five_shot_prompt(task.to_public(), category_dev=phase_inputs.dev)
    values = []
    for _label in ("g0", "active"):
        values.append(
            prepare_candidate_call_v2(
                task=task.to_public(),
                prompt=prompt,
                context=CandidateContextV2.generation_zero(),
                context_workspace=None,
                phase="train",
                block_id="b1",
                task_ordinal=0,
                ledger=_EmptyLedger(),
                run_id=campaign_run_id,
                service_identity_sha256=service_identity_sha256,
            )
        )
    if (
        values[0].logical_call_id != values[1].logical_call_id
        or values[0].context_hash != values[1].context_hash
        or values[0].task_request_sha256 != values[1].task_request_sha256
    ):
        raise SafePreflightError("SAFE_PREFLIGHT_CONTEXT_HASH_DEDUP_INVALID")


def _run_regressions(repository: Path) -> dict[str, object]:
    commands = (
        (
            os.fspath(repository / ".venv/bin/python"),
            "-m",
            "pytest",
            "-q",
            "benchmarks/chembench/tests/temperature_safe_evolve_v2",
        ),
        (
            os.fspath(repository / ".venv/bin/python"),
            "-m",
            "pytest",
            "-q",
            "benchmarks/chembench/tests/temperature_full_evolve_v1/test_candidate.py",
            "benchmarks/chembench/tests/temperature_full_evolve_v1/test_execution.py",
            "benchmarks/chembench/tests/temperature_full_evolve_v1/test_core_evolution.py",
            "benchmarks/chembench/tests/supervised_transfer_v2/test_final_test_safety_v2.py",
            "benchmarks/chembench/tests/supervised_transfer_v2/test_reflector_contract_v2.py",
            "tests/evolution/test_planned_jobs.py",
            "tests/evolution/test_registry_worker_dispatch.py",
        ),
    )
    environment = {
        key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ") if key in os.environ
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
    temporary_parent = repository / "state/chembench_temperature_safe_evolve_v2/preflight"
    temporary_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_parent.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="pytest-", dir=temporary_parent) as temporary:
        for index, command in enumerate(commands):
            completed = subprocess.run(
                (*command, "--basetemp", f"{temporary}/suite-{index}"),
                cwd=repository,
                env=environment,
                check=False,
                capture_output=True,
                timeout=1800,
            )
            output = completed.stdout + completed.stderr
            outputs.append(output)
            rendered = output.decode("utf-8", errors="strict")
            matches = _PASSED.findall(rendered)
            if completed.returncode or not matches:
                raise SafePreflightError(f"SAFE_PREFLIGHT_REGRESSION_SUITE_{index + 1}_FAILED")
            counts.append(int(matches[-1]))
    return {
        "schema_version": "TemperatureSafePreflightTestReceiptV2",
        "status": "PASS",
        "focused_test_count": counts[0],
        "regression_test_count": counts[1],
        "total_test_count": sum(counts),
        "failure_count": 0,
        "command_sha256": sha256_bytes(canonical_json_bytes(commands)),
        "output_sha256": sha256_bytes(b"\n--SUITE--\n".join(outputs)),
        "paid_call_gates": "CLOSED",
        "model_calls": 0,
    }


def _precheck_markdown(report: dict[str, object], digest: str) -> str:
    findings = report["findings"]
    lines = [
        "# ChemBench Temperature Safe-Evolve V2 Precheck",
        "",
        f"Status: `{report['status']}`",
        "",
        (
            "This preflight performed zero model calls. It verified the exact frozen C4 payloads, "
            "the reused official 202-item pool, the private prior Test order/GT, deterministic "
            "group-aware folds, managed identities, zero-tool policy, exactly-once tests, and an "
            "empty durable runtime inventory."
        ),
        "",
        (
            "The campaign is a repeated-pool independent-state mechanism experiment. It is not a "
            "historically never-exposed or external-generalization study."
        ),
        "",
        "## Findings",
        "",
    ]
    lines.extend(f"- {key}: `{value}`" for key, value in findings.items())
    lines.extend(("", f"Preflight bundle SHA-256: `{digest}`", ""))
    return "\n".join(lines)


def _fresh_private_root(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        raise SafePreflightError("SAFE_PREFLIGHT_DESTINATION_EXISTS")
    path.mkdir(mode=0o700)


def _write(path: Path, payload: bytes) -> None:
    write_private_file(path, payload, replace=False)


def _tracked_dirty(repository: Path) -> bool:
    return bool(_git(repository, "status", "--porcelain", "--untracked-files=no"))


def _experiment_paths_dirty(repository: Path) -> bool:
    return bool(
        _git(
            repository,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "src/openevo",
            "benchmarks/chembench/src",
            "benchmarks/chembench/configs/temperature_safe_evolve_v2",
            "benchmarks/chembench/scripts/temperature_safe_evolve_v2",
            "benchmarks/chembench/tests/temperature_safe_evolve_v2",
            "benchmarks/chembench/configs/temperature_full_evolve_v1/runtime_services_topology.yaml",
            "benchmarks/chembench/scripts/temperature_full_evolve_v1/gateway_service_bootstrap.py",
        )
    )


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafePreflightError as exc:
        print(
            json.dumps(
                {"status": "FAIL_CLOSED", "finding_code": exc.finding_code},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
