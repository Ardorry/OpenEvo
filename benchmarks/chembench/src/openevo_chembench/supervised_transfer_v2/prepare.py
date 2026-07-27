"""No-model Phase-0 preparation and deterministic verification for v2."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v1.exposure_v2 import (
    ACTUAL_EXPOSURE_LABELS_V2,
    HistoricalExposureBundleV2,
    HistoricalExposureItemV2,
    build_historical_exposure_bundle_v2,
)
from openevo_chembench.supervised_transfer_v2.config import MANIFEST_ROOT, PROTOCOL_ID
from openevo_chembench.supervised_transfer_v2.split import (
    generate_train_test_split_v2,
    render_split_artifacts_v2,
    verify_split_artifacts_v2,
    write_split_artifacts_v2,
)

OLD_REPOSITORY = Path("/home/lhy-h/work/openevo_chembench/OpenEvo")
V1_MANIFEST_ROOT = Path("benchmarks/chembench/manifests/supervised_transfer_v1")
V1_RESULT_ROOT = Path("results/chembench_supervised_transfer_v1")
V1_STATE_ROOT = Path("state/chembench_supervised_transfer_v1")
OLD_V1_EXPOSURE_SHA256 = "389c6ad25283b801f3bbe0061437a7891e2af80f17c01cd81aa901883eb4f660"
OLD_BLOCKED_RECEIPT_SHA256 = "43e64dce6a4c357b5811042140b7a908820fa13e9fe0aefc0f9eab4da11ab18e"


def prepare_phase0_v2(repository_root: Path) -> dict[str, object]:
    repository, loader, destination, old_commit = _inputs(repository_root)
    v1_snapshot = build_v1_read_only_snapshot_v2(repository)
    exposure = build_historical_exposure_bundle_v2(
        test_tasks=loader.load_split("test"),
        old_repository=OLD_REPOSITORY,
        source_repository_commit=old_commit,
    )
    exposure_outputs = _exposure_outputs(exposure)
    for name, payload in exposure_outputs.items():
        write_public_file(destination / name, payload)
    split = generate_train_test_split_v2(loader, exposure=exposure)
    split_outputs = render_split_artifacts_v2(split, loader=loader, exposure=exposure)
    write_split_artifacts_v2(split_outputs, destination=destination)
    split_digests = verify_split_artifacts_v2(split_outputs, destination=destination)
    write_public_file(
        destination / "v1_read_only_snapshot_receipt_v2.json",
        canonical_pretty_json_bytes(v1_snapshot),
    )
    phase0 = {
        "schema_version": "SupervisedTransferPhase0AuditReceiptV2",
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "model_calls_made": 0,
        "source_commit": _git(repository, "rev-parse", "HEAD"),
        "old_source_commit": old_commit,
        "dataset_revision": loader.manifest.revision,
        "dataset_combined_sha256": loader.manifest.combined_sha256,
        "historical_actual_exposed_count": len(exposure.actual_exposed_uids),
        "historical_never_executed_count": len(exposure.strict_holdout_uids),
        "historical_exposure_manifest_sha256": exposure.manifest_sha256,
        "train_count": len(split.train),
        "test_count": len(split.test),
        "reserve_count": len(split.reserve),
        "test_actual_exposure_count": sum(
            task.uid in exposure.actual_exposed_uids for task in split.test
        ),
        "split_summary_sha256": split_digests["split_summary.json"],
        "split_isolation_receipt_sha256": split_digests["split_isolation_receipt_v2.json"],
        "v1_snapshot_sha256": sha256_bytes(canonical_pretty_json_bytes(v1_snapshot)),
        "v1_mutated": False,
        "src_openevo_pristine": not bool(_git(repository, "diff", "--", "src/openevo")),
    }
    write_public_file(
        destination / "phase0_audit_receipt_v2.json",
        canonical_pretty_json_bytes(phase0),
    )
    return phase0


def verify_phase0_v2(repository_root: Path) -> dict[str, object]:
    repository, loader, destination, old_commit = _inputs(repository_root)
    frozen_exposure = load_frozen_exposure_bundle_v2(destination)
    current_exposure = build_historical_exposure_bundle_v2(
        test_tasks=loader.load_split("test"),
        old_repository=OLD_REPOSITORY,
        source_repository_commit=old_commit,
    )
    current_by_uid = {item.uid: item for item in current_exposure.items}
    frozen_by_uid = {item.uid: item for item in frozen_exposure.items}
    test_uids = {
        json.loads(line)["uid"]
        for line in (destination / "test_public_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    actual_values = {label.value for label in ACTUAL_EXPOSURE_LABELS_V2}
    if any(set(current_by_uid[uid].labels) & actual_values for uid in test_uids):
        raise RuntimeError("FROZEN_TEST_POSTFREEZE_EXPOSURE_DETECTED")
    changed_exposure_items = sum(
        current_by_uid[uid].to_payload() != frozen_by_uid[uid].to_payload()
        for uid in frozen_by_uid
    )
    split = generate_train_test_split_v2(loader, exposure=frozen_exposure)
    outputs = render_split_artifacts_v2(
        split,
        loader=loader,
        exposure=frozen_exposure,
    )
    digests = verify_split_artifacts_v2(outputs, destination=destination)
    phase0 = json.loads((destination / "phase0_audit_receipt_v2.json").read_text())
    snapshot_bytes = (destination / "v1_read_only_snapshot_receipt_v2.json").read_bytes()
    if sha256_bytes(snapshot_bytes) != phase0["v1_snapshot_sha256"]:
        raise RuntimeError("FROZEN_V1_SNAPSHOT_RECEIPT_CHANGED")
    return {
        "status": "PASS",
        "protocol_id": PROTOCOL_ID,
        "model_calls_made": 0,
        "dataset_revision": loader.manifest.revision,
        "dataset_combined_sha256": loader.manifest.combined_sha256,
        "historical_actual_exposed_count": len(frozen_exposure.actual_exposed_uids),
        "historical_never_executed_count": len(frozen_exposure.strict_holdout_uids),
        "train_count": len(split.train),
        "test_count": len(split.test),
        "reserve_count": len(split.reserve),
        "test_actual_exposure_count": 0,
        "split_summary_sha256": digests["split_summary.json"],
        "split_isolation_receipt_sha256": digests["split_isolation_receipt_v2.json"],
        "v1_snapshot_sha256": sha256_bytes(snapshot_bytes),
        "phase0_receipt_sha256": sha256_bytes(
            (destination / "phase0_audit_receipt_v2.json").read_bytes()
        ),
        "phase0_source_commit": phase0["source_commit"],
        "regeneration_byte_stable": True,
        "old_repository_head_at_freeze": frozen_exposure.source_repository_commit,
        "old_repository_head_current": old_commit,
        "old_repository_head_drifted": old_commit != frozen_exposure.source_repository_commit,
        "postfreeze_exposure_item_change_count": changed_exposure_items,
        "postfreeze_test_actual_exposure_count": 0,
        "src_openevo_pristine": not bool(_git(repository, "diff", "--", "src/openevo")),
    }


def load_frozen_exposure_bundle_v2(destination: Path) -> HistoricalExposureBundleV2:
    path = destination / "historical_exposure_manifest_v2.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "source_repository_commit",
        "scanned_file_count",
        "scanned_inventory_sha256",
        "item_count",
        "items",
    }:
        raise RuntimeError("FROZEN_HISTORICAL_EXPOSURE_SCHEMA_INVALID")
    raw_items = payload["items"]
    if type(raw_items) is not list or payload["item_count"] != len(raw_items):
        raise RuntimeError("FROZEN_HISTORICAL_EXPOSURE_SCHEMA_INVALID")
    items = tuple(
        HistoricalExposureItemV2(
            uid=item["uid"],
            category=item["category"],
            labels=tuple(item["labels"]),
            first_exposure_time=item["first_exposure_time"],
            first_exposure_protocol=item["first_exposure_protocol"],
            attempt_count=item["attempt_count"],
            completion_count=item["completion_count"],
            evaluation_count=item["evaluation_count"],
            reflector_input_count=item["reflector_input_count"],
            human_item_reviewed=item["human_item_reviewed"],
            evidence_digests=tuple(item["evidence_digests"]),
            evidence_source_types=tuple(item["evidence_source_types"]),
        )
        for item in raw_items
    )
    bundle = HistoricalExposureBundleV2(
        source_repository_commit=payload["source_repository_commit"],
        scanned_file_count=payload["scanned_file_count"],
        scanned_inventory_sha256=payload["scanned_inventory_sha256"],
        aggregate_review_evidence_count=0,
        items=items,
    )
    if bundle.manifest_bytes() != path.read_bytes():
        raise RuntimeError("FROZEN_HISTORICAL_EXPOSURE_BYTES_INVALID")
    return bundle


def build_v1_read_only_snapshot_v2(repository: Path) -> dict[str, object]:
    roots = (V1_MANIFEST_ROOT, V1_RESULT_ROOT, V1_STATE_ROOT)
    summaries: list[dict[str, object]] = []
    for relative_root in roots:
        root = repository / relative_root
        tree_digest = hashlib.sha256()
        file_count = 0
        total_bytes = 0
        if not root.exists():
            summaries.append(
                {
                    "root": relative_root.as_posix(),
                    "exists": False,
                    "file_count": 0,
                    "total_bytes": 0,
                    "tree_sha256": hashlib.sha256(b"").hexdigest(),
                }
            )
            continue
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if stat.S_ISLNK(metadata.st_mode):
                link_target = os.readlink(path).encode("utf-8")
                record = {
                    "path": path.relative_to(repository).as_posix(),
                    "kind": "symlink",
                    "size_bytes": len(link_target),
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "sha256": sha256_bytes(link_target),
                }
            elif stat.S_ISREG(metadata.st_mode):
                record = {
                    "path": path.relative_to(repository).as_posix(),
                    "kind": "regular",
                    "size_bytes": metadata.st_size,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "sha256": _sha256_file(path),
                }
            else:
                raise RuntimeError("V1_SNAPSHOT_SPECIAL_FILE_UNSAFE")
            tree_digest.update(
                json.dumps(
                    record,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            )
            tree_digest.update(b"\n")
            file_count += 1
            total_bytes += metadata.st_size
        summaries.append(
            {
                "root": relative_root.as_posix(),
                "exists": True,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "tree_sha256": tree_digest.hexdigest(),
            }
        )
    return {
        "schema_version": "V1ReadOnlySnapshotReceiptV2",
        "protocol_id": PROTOCOL_ID,
        "v1_file_count": sum(int(value["file_count"]) for value in summaries),
        "v1_total_bytes": sum(int(value["total_bytes"]) for value in summaries),
        "v1_roots_sha256": sha256_bytes(canonical_pretty_json_bytes(summaries)),
        "root_summaries": summaries,
    }


def _inputs(
    repository_root: Path,
) -> tuple[Path, ChemBench4KDatasetLoader, Path, str]:
    repository = repository_root.resolve(strict=True)
    if repository != Path("/home/lhy-h/work/compare2"):
        raise RuntimeError("REPOSITORY_IDENTITY_INVALID")
    if _git(repository, "diff", "--", "src/openevo"):
        raise RuntimeError("SRC_OPENEVO_NOT_PRISTINE")
    if not OLD_REPOSITORY.is_dir():
        raise RuntimeError("OLD_REPOSITORY_UNAVAILABLE")
    old_commit = _git(OLD_REPOSITORY, "rev-parse", "HEAD")
    data_root = (
        repository / "data/chembench4k/AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
    ).resolve(strict=True)
    loader = ChemBench4KDatasetLoader(
        snapshot_root=data_root,
        manifest_path=data_root / "chembench4k_dataset_manifest_v2.json",
    )
    destination = (repository / MANIFEST_ROOT).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    return repository, loader, destination, old_commit


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exposure_outputs(exposure: HistoricalExposureBundleV2) -> dict[str, bytes]:
    return {
        "historical_exposure_manifest_v2.json": exposure.manifest_bytes(),
        "historical_exposure_summary_v2.json": exposure.summary_bytes(),
        "historical_exposure_receipt_v2.json": exposure.receipt_bytes(
            old_v1_manifest_sha256=OLD_V1_EXPOSURE_SHA256,
            old_blocked_receipt_sha256=OLD_BLOCKED_RECEIPT_SHA256,
        ),
    }


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", os.fspath(repository), *arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("GIT_IDENTITY_UNAVAILABLE")
    return completed.stdout.strip()


__all__ = ["build_v1_read_only_snapshot_v2", "prepare_phase0_v2", "verify_phase0_v2"]
