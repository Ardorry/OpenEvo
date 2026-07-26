#!/usr/bin/env python3
"""Prepare, verify, dry-run, and gate ChemBench supervised transfer v1."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v1.config import (
    MANIFEST_ROOT,
    PROTOCOL_ID,
    TOTAL_MODEL_CALLS,
    load_supervised_transfer_config_v1,
)
from openevo_chembench.supervised_transfer_v1.experiment import (
    ExperimentInputsV1,
    SupervisedTransferExperimentV1,
    build_complete_dry_run_v1,
    load_experiment_inputs_v1,
)
from openevo_chembench.supervised_transfer_v1.exposure import (
    load_historical_exposure_manifest,
)
from openevo_chembench.supervised_transfer_v1.exposure_v2 import (
    build_historical_exposure_bundle_v2,
    verify_historical_exposure_artifacts_v2,
    write_historical_exposure_artifacts_v2,
)
from openevo_chembench.supervised_transfer_v1.pause import (
    record_administrative_pause_v1,
)
from openevo_chembench.supervised_transfer_v1.preflight import (
    verify_preflight_authority_v1,
)
from openevo_chembench.supervised_transfer_v1.source_manifest import (
    SOURCE_IMPORT_MANIFEST,
    render_source_import_manifest_v1,
)
from openevo_chembench.supervised_transfer_v1.split import (
    build_supervised_dataset_manifest,
)
from openevo_chembench.supervised_transfer_v1.split_v2 import (
    generate_balanced_split_v2,
    render_split_artifacts_v2,
    verify_split_artifacts_v2,
    write_split_artifacts_v2,
)

DEFAULT_CONFIG = PACKAGE_ROOT / "configs" / "chembench_supervised_transfer_v1.yaml"
DEFAULT_OLD_REPOSITORY = Path("/home/lhy-h/work/openevo_chembench/OpenEvo")
DEFAULT_SNAPSHOT_ROOT = (
    WORKSPACE_ROOT
    / "data"
    / "chembench4k"
    / "AI4Chem_ChemBench4K"
    / CHEMBENCH4K_REVISION
)
DEFAULT_MANIFEST_ROOT = WORKSPACE_ROOT / MANIFEST_ROOT
OLD_V1_EXPOSURE_SHA256 = "389c6ad25283b801f3bbe0061437a7891e2af80f17c01cd81aa901883eb4f660"
OLD_BLOCKED_RECEIPT_SHA256 = "43e64dce6a4c357b5811042140b7a908820fa13e9fe0aefc0f9eab4da11ab18e"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--old-repository", type=Path, default=DEFAULT_OLD_REPOSITORY)
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare", help="write exposure and split artifacts; no model calls")
    subparsers.add_parser("verify", help="recompute every artifact without mutation")
    subparsers.add_parser("dry-run", help="simulate the complete experiment with zero calls")
    preflight = subparsers.add_parser(
        "run-preflight",
        help="execute only paid smoke, canaries, and checkpoint-zero Probe",
    )
    preflight.add_argument("--run-id", required=True)
    verify_preflight = subparsers.add_parser(
        "verify-preflight",
        help="verify one terminal preflight authority without model calls",
    )
    verify_preflight.add_argument("--run-id", required=True)
    formal = subparsers.add_parser(
        "run-formal",
        help="execute Control-first formal Train, Probe 10-50, and frozen Test",
    )
    formal.add_argument("--run-id", required=True)
    formal.add_argument("--preflight-run-id", required=True)
    formal.add_argument(
        "--test-manifest",
        choices=("test_primary", "test_recovery_01"),
        default="test_primary",
    )
    pause = subparsers.add_parser(
        "record-pause",
        help="record immutable zero-process evidence for an externally stopped run",
    )
    pause.add_argument("--run-id", required=True)
    return parser.parse_args()


def _git_output(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git identity check failed")
    return completed.stdout.strip()


def _require_roots(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    old_repository = args.old_repository.resolve()
    snapshot_root = args.snapshot_root.resolve()
    manifest_root = args.manifest_root.resolve()
    if old_repository != DEFAULT_OLD_REPOSITORY:
        raise RuntimeError("old repository identity differs from the preregistered source")
    if snapshot_root != DEFAULT_SNAPSHOT_ROOT:
        raise RuntimeError("dataset root is not anchored inside the current repository")
    if manifest_root != DEFAULT_MANIFEST_ROOT:
        raise RuntimeError("manifest root is not anchored inside the current repository")
    if WORKSPACE_ROOT != Path("/home/lhy-h/work/compare2"):
        raise RuntimeError("workspace root does not match the repository override")
    return old_repository, snapshot_root, manifest_root


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[ChemBench4KDatasetLoader, Path, Path, str]:
    old_repository, snapshot_root, manifest_root = _require_roots(args)
    config = load_supervised_transfer_config_v1(args.config.resolve())
    if (WORKSPACE_ROOT / config.dataset_root).resolve() != snapshot_root:
        raise RuntimeError("config dataset root binding mismatch")
    if (WORKSPACE_ROOT / config.historical_exposure_manifest).resolve() != (
        manifest_root / "historical_exposure_manifest_v2.json"
    ):
        raise RuntimeError("config exposure path binding mismatch")
    loader = ChemBench4KDatasetLoader(
        snapshot_root=snapshot_root,
        manifest_path=snapshot_root / "chembench4k_dataset_manifest_v2.json",
    )
    old_commit = _git_output(old_repository, "rev-parse", "HEAD")
    if _git_output(old_repository, "diff", "--", "src/openevo"):
        raise RuntimeError("old repository src/openevo is not pristine")
    return loader, old_repository, manifest_root, old_commit


def _require_legacy_blocker_unchanged(manifest_root: Path) -> tuple[str, str]:
    old_v1_path = manifest_root / "historical_exposed_uid_manifest.json"
    old_blocker_path = manifest_root / "split_generation_blocked_receipt_v1.json"
    old_v1 = load_historical_exposure_manifest(old_v1_path)
    old_v1_sha256 = sha256_bytes(old_v1_path.read_bytes())
    old_blocker_sha256 = sha256_bytes(old_blocker_path.read_bytes())
    if old_v1.digest != old_v1_sha256 or old_v1_sha256 != OLD_V1_EXPOSURE_SHA256:
        raise RuntimeError("legacy V1 exposure manifest is not the frozen blocked input")
    if old_blocker_sha256 != OLD_BLOCKED_RECEIPT_SHA256:
        raise RuntimeError("legacy blocked receipt changed")
    return old_v1_sha256, old_blocker_sha256


def _prepare(args: argparse.Namespace) -> dict[str, object]:
    loader, old_repository, manifest_root, old_commit = _load_inputs(args)
    old_v1_sha256, old_blocker_sha256 = _require_legacy_blocker_unchanged(manifest_root)
    dataset_manifest_bytes = canonical_pretty_json_bytes(
        build_supervised_dataset_manifest(loader)
    )
    write_public_file(
        manifest_root / "chembench4k_dataset_manifest_supervised_v1.json",
        dataset_manifest_bytes,
    )
    dataset_manifest_digest = sha256_bytes(dataset_manifest_bytes)
    exposure = build_historical_exposure_bundle_v2(
        test_tasks=loader.load_split("test"),
        old_repository=old_repository,
        source_repository_commit=old_commit,
    )
    exposure_digests = write_historical_exposure_artifacts_v2(
        exposure,
        destination_root=manifest_root,
        old_v1_manifest_sha256=old_v1_sha256,
        old_blocked_receipt_sha256=old_blocker_sha256,
    )
    split = generate_balanced_split_v2(loader, exposure_bundle=exposure)
    rendered = render_split_artifacts_v2(
        split,
        loader=loader,
        exposure_bundle=exposure,
    )
    generated = write_split_artifacts_v2(rendered, destination_root=manifest_root)
    verified = verify_split_artifacts_v2(
        expected=rendered,
        destination_root=manifest_root,
    )
    if generated.sha256 != verified.sha256:
        raise RuntimeError("split write/verify digests differ")
    source_manifest = render_source_import_manifest_v1(
        repository_root=WORKSPACE_ROOT,
        old_repository=old_repository,
    )
    write_public_file(manifest_root / SOURCE_IMPORT_MANIFEST, source_manifest)
    return {
        "status": "PASS",
        "protocol_id": PROTOCOL_ID,
        "command": "prepare",
        "model_calls_made": 0,
        "dataset_sha256": loader.manifest.combined_sha256,
        "old_source_commit": old_commit,
        "historical_actual_exposed_uid_count": len(exposure.actual_exposed_uids),
        "historical_strict_never_executed_uid_count": len(exposure.strict_holdout_uids),
        "historical_exposure_manifest_v2_sha256": exposure.manifest_sha256,
        "historical_exposure_receipt_v2_sha256": exposure_digests[
            "historical_exposure_receipt_v2.json"
        ],
        "train_count": len(split.train),
        "probe_count": len(split.probe),
        "primary_test_count": len(split.test_primary),
        "recovery_test_count": len(split.recovery_tests),
        "reserve_count": len(split.reserve),
        "split_summary_sha256": generated.split_summary_sha256,
        "split_isolation_receipt_sha256": generated.isolation_receipt_sha256,
        "dataset_manifest_sha256": dataset_manifest_digest,
        "source_import_manifest_sha256": sha256_bytes(source_manifest),
        "old_v1_exposure_sha256": old_v1_sha256,
        "old_blocked_receipt_sha256": old_blocker_sha256,
    }


def _verify(args: argparse.Namespace, *, command: str) -> dict[str, object]:
    loader, old_repository, manifest_root, old_commit = _load_inputs(args)
    old_v1_sha256, old_blocker_sha256 = _require_legacy_blocker_unchanged(manifest_root)
    dataset_manifest_bytes = canonical_pretty_json_bytes(
        build_supervised_dataset_manifest(loader)
    )
    dataset_manifest_path = (
        manifest_root / "chembench4k_dataset_manifest_supervised_v1.json"
    )
    if dataset_manifest_path.read_bytes() != dataset_manifest_bytes:
        raise RuntimeError("supervised dataset manifest does not match regeneration")
    exposure = build_historical_exposure_bundle_v2(
        test_tasks=loader.load_split("test"),
        old_repository=old_repository,
        source_repository_commit=old_commit,
    )
    exposure_digests = verify_historical_exposure_artifacts_v2(
        exposure,
        destination_root=manifest_root,
        old_v1_manifest_sha256=old_v1_sha256,
        old_blocked_receipt_sha256=old_blocker_sha256,
    )
    split = generate_balanced_split_v2(loader, exposure_bundle=exposure)
    rendered = render_split_artifacts_v2(
        split,
        loader=loader,
        exposure_bundle=exposure,
    )
    verified = verify_split_artifacts_v2(
        expected=rendered,
        destination_root=manifest_root,
    )
    source_manifest = render_source_import_manifest_v1(
        repository_root=WORKSPACE_ROOT,
        old_repository=old_repository,
    )
    if (manifest_root / SOURCE_IMPORT_MANIFEST).read_bytes() != source_manifest:
        raise RuntimeError("source import manifest does not match regeneration")
    config = load_supervised_transfer_config_v1(args.config.resolve())
    src_diff = _git_output(WORKSPACE_ROOT, "diff", "--", "src/openevo")
    staged_src_diff = _git_output(WORKSPACE_ROOT, "diff", "--cached", "--", "src/openevo")
    if src_diff or staged_src_diff:
        raise RuntimeError("src/openevo protection boundary changed")
    result = {
        "status": "PASS",
        "protocol_id": PROTOCOL_ID,
        "command": command,
        "model_calls_made": 0,
        "paid_calls_started": False,
        "planned_model_calls": TOTAL_MODEL_CALLS,
        "call_budget": config.call_budget,
        "dataset_sha256": loader.manifest.combined_sha256,
        "historical_actual_exposed_uid_count": len(exposure.actual_exposed_uids),
        "historical_strict_never_executed_uid_count": len(exposure.strict_holdout_uids),
        "historical_exposure_manifest_v2_sha256": exposure.manifest_sha256,
        "historical_exposure_receipt_v2_sha256": exposure_digests[
            "historical_exposure_receipt_v2.json"
        ],
        "split_summary_sha256": verified.split_summary_sha256,
        "split_isolation_receipt_sha256": verified.isolation_receipt_sha256,
        "source_import_manifest_sha256": sha256_bytes(source_manifest),
        "train_count": len(split.train),
        "probe_count": len(split.probe),
        "primary_test_count": len(split.test_primary),
        "recovery_test_count": len(split.recovery_tests),
        "reserve_count": len(split.reserve),
        "old_v1_exposure_sha256": old_v1_sha256,
        "old_blocked_receipt_sha256": old_blocker_sha256,
        "src_openevo_pristine": True,
        "regeneration_byte_stable": True,
        "train_probe_test_strictly_isolated": True,
        "reflector_uid_allowlist": "train-only",
        "probe_test_evolution_jobs_allowed": False,
        "ready_for_paid_smoke": False,
    }
    if command == "dry-run":
        inputs = load_experiment_inputs_v1(WORKSPACE_ROOT, config)
        complete = build_complete_dry_run_v1(inputs)
        result.update(complete)
        result["command"] = command
        result["model_calls_made"] = 0
        result["paid_calls_started"] = False
    return result


def _require_paid_gate(
    args: argparse.Namespace,
    *,
    test_manifest: str,
) -> ExperimentInputsV1:
    verification = _verify(args, command="prepaid-run-gate")
    config = load_supervised_transfer_config_v1(args.config.resolve())
    inputs = load_experiment_inputs_v1(
        WORKSPACE_ROOT,
        config,
        test_manifest=test_manifest,
    )
    dry_run = build_complete_dry_run_v1(inputs)
    if verification["status"] != "PASS" or dry_run["ready_for_paid_smoke"] is not True:
        raise RuntimeError("paid run gate is not satisfied")
    return inputs


def _run_preflight(args: argparse.Namespace) -> dict[str, object]:
    inputs = _require_paid_gate(args, test_manifest="test_primary")
    return SupervisedTransferExperimentV1(
        inputs=inputs,
        run_id=args.run_id,
        run_mode="preflight",
    ).run_preflight()


def _verify_preflight(args: argparse.Namespace) -> dict[str, object]:
    inputs = _require_paid_gate(args, test_manifest="test_primary")
    receipt, digest = verify_preflight_authority_v1(
        inputs=inputs,
        source_run_id=args.run_id,
    )
    return {
        "status": "PASS",
        "model_calls_made": 0,
        "run_id": args.run_id,
        "preflight_authority_sha256": digest,
        "task_sessions": receipt["task_sessions"],
        "reflector_completions": receipt["reflector_completions"],
        "test_model_calls": receipt["test_model_calls"],
    }


def _run_formal(args: argparse.Namespace) -> dict[str, object]:
    inputs = _require_paid_gate(args, test_manifest=args.test_manifest)
    verify_preflight_authority_v1(
        inputs=inputs,
        source_run_id=args.preflight_run_id,
    )
    return SupervisedTransferExperimentV1(
        inputs=inputs,
        run_id=args.run_id,
        run_mode="formal",
        preflight_run_id=args.preflight_run_id,
    ).run_formal()


def _record_pause(args: argparse.Namespace) -> dict[str, object]:
    config = load_supervised_transfer_config_v1(args.config.resolve())
    receipt, digest = record_administrative_pause_v1(
        repository_root=WORKSPACE_ROOT,
        result_root_relative=config.result_root,
        state_root_relative=config.state_root,
        run_id=args.run_id,
    )
    return {
        "status": receipt["administrative_status"],
        "run_id": args.run_id,
        "pause_receipt_sha256": digest,
        "active_model_calls": 0,
        "resume_allowed": False,
    }


def main() -> int:
    args = _parse_args()
    if args.command == "prepare":
        result = _prepare(args)
    elif args.command in {"verify", "dry-run"}:
        result = _verify(args, command=args.command)
    elif args.command == "run-preflight":
        result = _run_preflight(args)
    elif args.command == "verify-preflight":
        result = _verify_preflight(args)
    elif args.command == "run-formal":
        result = _run_formal(args)
    elif args.command == "record-pause":
        result = _record_pause(args)
    else:
        raise AssertionError("unhandled command")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
