"""Aggregate-only capacity gate for a fresh Temperature full-evolve run.

The gate reads private historical ledgers, but its returned payload contains
only counts, source identities, and set digests. It never emits benchmark
questions, options, labels, per-item results, completions, transcripts, or UID
values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from openevo_chembench.chembench4k_dataset import (
    DATASET_MANIFEST_FILENAME,
    ChemBench4KDatasetLoader,
    normalize_benchmark_text,
)
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import sha256_bytes

PROTOCOL_ID = "chembench_temperature_full_evolve_v1"
SCHEMA_VERSION = "TemperatureFullEvolveCapacityPreflightV1"
FINDING_CODE = "INSUFFICIENT_NEVER_EXPOSED_TEMPERATURE_DATA"
CATEGORY = "Temperature_Prediction"
ARM_SIZE_TIERS = (200, 175, 150, 125, 100)
MINIMUM_ARM_SIZE = 100
MINIMUM_REQUIRED_TOTAL = 2 * MINIMUM_ARM_SIZE

_UID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ACTUAL_EXPOSURE_LABELS = frozenset(
    {
        "HUMAN_ITEM_REVIEWED",
        "PRIVATE_EVALUATED",
        "REFLECTOR_SUPERVISED",
        "TASK_MODEL_ATTEMPTED",
        "TASK_MODEL_EXECUTED",
    }
)
_PROTOCOLS = ("v1", "v2", "v3")
_NAMED_RUN_IDS = (
    "stv3-temperature-controlfix-recovery-20260731T105523Z",
    "stv3-composed-final-test-20260731T155440Z",
    "stv3-composed-final-test-recovery-20260801T031645Z",
    "stv3-control-final-test-baseline-dockerfix-20260801T190638Z",
    "stv3-control-final-test-baseline-recovery-20260802T033900Z",
)


class TemperatureCapacityPreflightError(RuntimeError):
    """Closed failure while reconstructing the authoritative capacity gate."""


def uid_set_sha256(values: Iterable[str]) -> str:
    """Hash a non-empty UID set as sorted lowercase hex lines with a final LF."""

    unique = sorted(set(values))
    if not unique or any(_UID_RE.fullmatch(value) is None for value in unique):
        raise ValueError("UID set must contain lowercase SHA-256 values")
    return hashlib.sha256(("\n".join(unique) + "\n").encode("utf-8")).hexdigest()


def select_equal_arm_size(eligible_count: int) -> int | None:
    """Select the largest permitted equal arm size, or return ``None``."""

    if isinstance(eligible_count, bool) or not isinstance(eligible_count, int):
        raise TypeError("eligible_count must be an integer")
    if eligible_count < 0:
        raise ValueError("eligible_count must be non-negative")
    return next((size for size in ARM_SIZE_TIERS if eligible_count >= 2 * size), None)


def build_capacity_preflight(repository_root: Path) -> dict[str, object]:
    """Reconstruct current Temperature exposure and apply the first hard gate."""

    repository = _require_repository_root(repository_root)
    snapshot_root = (
        repository / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
    )
    loader = ChemBench4KDatasetLoader(snapshot_root=snapshot_root)
    temperature_test = loader.load_category(CATEGORY, split="test")
    temperature_dev = loader.load_category(CATEGORY, split="dev")
    dataset_uids = {task.uid for task in temperature_test}
    if len(dataset_uids) != len(temperature_test):
        raise TemperatureCapacityPreflightError("TEMPERATURE_DATASET_UID_DUPLICATE")

    historical_path = (
        repository / "benchmarks/chembench/manifests/supervised_transfer_v2/"
        "historical_exposure_manifest_v2.json"
    )
    historical_payload = _read_json_object(historical_path)
    historical_all, prior_actual = _historical_temperature_sets(historical_payload)
    if historical_all != dataset_uids:
        raise TemperatureCapacityPreflightError("HISTORICAL_MANIFEST_DATASET_MISMATCH")

    full_public_path = repository / "benchmarks/chembench/manifests/v2/full_public_manifest.jsonl"
    full_public_uids = _category_uids_from_public_manifest(full_public_path)
    if full_public_uids != dataset_uids:
        raise TemperatureCapacityPreflightError("FULL_PUBLIC_MANIFEST_DATASET_MISMATCH")

    partitions, partition_paths = _load_v2_partitions(repository, dataset_uids)
    event_scan = _scan_event_ledgers(repository, dataset_uids, partitions)
    event_uids = set(event_scan.pop("_event_uids"))
    exposed_uids = prior_actual | event_uids
    if not exposed_uids <= dataset_uids:
        raise TemperatureCapacityPreflightError("HISTORICAL_UID_OUTSIDE_FROZEN_DATASET")
    eligible_upper_bound = dataset_uids - exposed_uids
    selected_arm_size = select_equal_arm_size(len(eligible_upper_bound))

    normalized_groups = _normalized_group_summary(temperature_test)
    source_manifest = snapshot_root / DATASET_MANIFEST_FILENAME
    config_path = (
        repository / "benchmarks/chembench/configs/supervised_transfer_v3/"
        "chembench_supervised_transfer_v3.yaml"
    )
    v3_split_path = (
        repository / "benchmarks/chembench/manifests/supervised_transfer_v3/"
        "split_reference_receipt_v3.json"
    )
    source_hashes = {
        "dataset_manifest_sha256": _file_sha256(source_manifest),
        "temperature_dev_file_sha256": _file_sha256(
            snapshot_root / "dev/Temperature_Prediction_benchmark.json"
        ),
        "temperature_test_file_sha256": _file_sha256(
            snapshot_root / "test/Temperature_Prediction_benchmark.json"
        ),
        "full_public_manifest_sha256": _file_sha256(full_public_path),
        "historical_exposure_manifest_sha256": _file_sha256(historical_path),
        "v2_train_public_manifest_sha256": _file_sha256(partition_paths["train"]),
        "v2_test_public_manifest_sha256": _file_sha256(partition_paths["test"]),
        "v2_reserve_public_manifest_sha256": _file_sha256(partition_paths["reserve"]),
        "v2_train_private_manifest_sha256": _file_sha256(
            partition_paths["train"].with_name("train_private_manifest.jsonl")
        ),
        "v2_test_private_manifest_sha256": _file_sha256(
            partition_paths["test"].with_name("test_private_manifest.jsonl")
        ),
        "v3_split_reference_receipt_sha256": _file_sha256(v3_split_path),
        "stv3_config_sha256": _file_sha256(config_path),
    }

    status = "PASS" if selected_arm_size is not None else "BLOCKED"
    finding_code = None if selected_arm_size is not None else FINDING_CODE
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "status": status,
        "finding_code": finding_code,
        "evidence_level": "AUTHORITATIVE_LOCAL_MANIFEST_AND_PRIVATE_EVENT_LEDGER",
        "dataset": {
            "repository": "AI4Chem/ChemBench4K",
            "revision": CHEMBENCH4K_REVISION,
            "combined_sha256": loader.manifest.combined_sha256,
            "temperature_test_count": len(temperature_test),
            "temperature_dev_count": len(temperature_dev),
            "dev_usage": "EXCLUDED_OFFICIAL_FIVE_SHOT_DEMONSTRATIONS_WITH_ANSWERS",
            "raw_row_fields": ["A", "B", "C", "D", "answer", "question"],
            "upstream_group_fields": [],
            "temperature_uid_set_sha256": uid_set_sha256(dataset_uids),
        },
        "historical_exposure": {
            "taxonomy_actual_labels": sorted(_ACTUAL_EXPOSURE_LABELS),
            "prior_snapshot_actual_count": len(prior_actual),
            "event_ledger_exposed_count": len(event_uids),
            "actual_exposed_union_count": len(exposed_uids),
            "actual_exposed_uid_set_sha256": uid_set_sha256(exposed_uids),
            "event_scan": event_scan,
        },
        "v2_partition_crosscheck": {
            "partition_counts": {name: len(values) for name, values in partitions.items()},
            "exposed_union_by_partition": {
                name: len(values & exposed_uids) for name, values in partitions.items()
            },
            "remaining_by_partition": {
                name: len(values - exposed_uids) for name, values in partitions.items()
            },
        },
        "near_duplicate_precheck": normalized_groups,
        "capacity_gate": {
            "arm_size_tiers": list(ARM_SIZE_TIERS),
            "minimum_train_count": MINIMUM_ARM_SIZE,
            "minimum_test_count": MINIMUM_ARM_SIZE,
            "minimum_required_total": MINIMUM_REQUIRED_TOTAL,
            "maximum_provable_never_exposed_count_before_near_duplicate_grouping": len(
                eligible_upper_bound
            ),
            "maximum_provable_never_exposed_uid_set_sha256": uid_set_sha256(eligible_upper_bound),
            "capacity_shortfall_before_near_duplicate_grouping": max(
                0, MINIMUM_REQUIRED_TOTAL - len(eligible_upper_bound)
            ),
            "selected_equal_arm_size": selected_arm_size,
            "split_created": False,
            "split_sha256": None,
        },
        "side_effects": {
            "formal_run_created": False,
            "tmux_session_created": False,
            "candidate_calls": 0,
            "reflector_calls": 0,
            "core_jobs": 0,
            "baseline_calls": 0,
            "total_model_calls": 0,
        },
        "source_files_sha256": source_hashes,
    }


def _require_repository_root(repository_root: Path) -> Path:
    if not isinstance(repository_root, Path) or not repository_root.is_absolute():
        raise TypeError("repository_root must be an absolute Path")
    repository = repository_root.resolve(strict=True)
    if not (repository / "benchmarks/chembench").is_dir():
        raise TemperatureCapacityPreflightError("CHEMBENCH_REPOSITORY_ROOT_INVALID")
    return repository


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemperatureCapacityPreflightError("AUTHORITATIVE_JSON_UNREADABLE") from exc
    if not isinstance(payload, dict):
        raise TemperatureCapacityPreflightError("AUTHORITATIVE_JSON_ROOT_INVALID")
    return payload


def _historical_temperature_sets(
    payload: dict[str, object],
) -> tuple[set[str], set[str]]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise TemperatureCapacityPreflightError("HISTORICAL_EXPOSURE_ITEMS_INVALID")
    all_uids: set[str] = set()
    actual_uids: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("category") != CATEGORY:
            continue
        uid = item.get("uid")
        labels = item.get("labels")
        if (
            not isinstance(uid, str)
            or _UID_RE.fullmatch(uid) is None
            or not isinstance(labels, list)
            or any(not isinstance(label, str) for label in labels)
        ):
            raise TemperatureCapacityPreflightError("HISTORICAL_EXPOSURE_ITEM_INVALID")
        if uid in all_uids:
            raise TemperatureCapacityPreflightError("HISTORICAL_EXPOSURE_UID_DUPLICATE")
        all_uids.add(uid)
        if set(labels) & _ACTUAL_EXPOSURE_LABELS:
            actual_uids.add(uid)
    if not all_uids:
        raise TemperatureCapacityPreflightError("HISTORICAL_TEMPERATURE_SET_EMPTY")
    return all_uids, actual_uids


def _category_uids_from_public_manifest(path: Path) -> set[str]:
    rows = _read_jsonl(path)
    uids: set[str] = set()
    for row in rows:
        if row.get("category") != CATEGORY:
            continue
        uid = row.get("uid")
        if not isinstance(uid, str) or _UID_RE.fullmatch(uid) is None or uid in uids:
            raise TemperatureCapacityPreflightError("PUBLIC_MANIFEST_UID_INVALID")
        uids.add(uid)
    if not uids:
        raise TemperatureCapacityPreflightError("PUBLIC_TEMPERATURE_SET_EMPTY")
    return uids


def _load_v2_partitions(
    repository: Path,
    dataset_uids: set[str],
) -> tuple[dict[str, set[str]], dict[str, Path]]:
    root = repository / "benchmarks/chembench/manifests/supervised_transfer_v2"
    paths = {
        "train": root / "train_public_manifest.jsonl",
        "test": root / "test_public_manifest.jsonl",
        "reserve": root / "reserve_public_manifest.jsonl",
    }
    partitions = {name: _category_uids_from_public_manifest(path) for name, path in paths.items()}
    names = tuple(partitions)
    if any(
        partitions[left] & partitions[right]
        for index, left in enumerate(names)
        for right in names[index + 1 :]
    ):
        raise TemperatureCapacityPreflightError("V2_TEMPERATURE_PARTITION_OVERLAP")
    if set().union(*partitions.values()) != dataset_uids:
        raise TemperatureCapacityPreflightError("V2_TEMPERATURE_PARTITION_CLOSURE_INVALID")
    return partitions, paths


def _scan_event_ledgers(
    repository: Path,
    dataset_uids: set[str],
    partitions: dict[str, set[str]],
) -> dict[str, object]:
    inventory_entries: list[bytes] = []
    event_uids_by_protocol: dict[str, set[str]] = {}
    evaluated_uids: set[str] = set()
    attempted_uids: set[str] = set()
    named_runs: dict[str, dict[str, object]] = {}
    file_count = 0
    byte_count = 0
    malformed_count = 0

    for protocol in _PROTOCOLS:
        root = repository / f"state/chembench_supervised_transfer_{protocol}/runs"
        if not root.is_dir():
            raise TemperatureCapacityPreflightError("HISTORICAL_EVENT_ROOT_MISSING")
        paths = sorted(root.glob("*/private/events.jsonl"))
        if not paths:
            raise TemperatureCapacityPreflightError("HISTORICAL_EVENT_LEDGER_MISSING")
        protocol_uids: set[str] = set()
        for path in paths:
            raw = path.read_bytes()
            relative = path.relative_to(repository).as_posix()
            inventory_entries.append(
                (relative + "\0" + sha256_bytes(raw) + "\0" + str(len(raw)) + "\n").encode("utf-8")
            )
            file_count += 1
            byte_count += len(raw)
            run_uids: set[str] = set()
            for line in raw.splitlines():
                if not line:
                    malformed_count += 1
                    continue
                try:
                    event = json.loads(line)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise TemperatureCapacityPreflightError(
                        "HISTORICAL_EVENT_LEDGER_MALFORMED"
                    ) from exc
                if not isinstance(event, dict):
                    raise TemperatureCapacityPreflightError(
                        "HISTORICAL_EVENT_LEDGER_EVENT_INVALID"
                    )
                if event.get("category") != CATEGORY:
                    continue
                uid = event.get("task_uid")
                if not isinstance(uid, str) or _UID_RE.fullmatch(uid) is None:
                    raise TemperatureCapacityPreflightError(
                        "HISTORICAL_TEMPERATURE_EVENT_UID_INVALID"
                    )
                if uid not in dataset_uids:
                    raise TemperatureCapacityPreflightError(
                        "HISTORICAL_UID_OUTSIDE_FROZEN_DATASET"
                    )
                protocol_uids.add(uid)
                run_uids.add(uid)
                if event.get("kind") == "TASK_MODEL_ATTEMPTED":
                    attempted_uids.add(uid)
                if event.get("kind") == "PRIVATE_EVALUATED":
                    evaluated_uids.add(uid)
            run_id = path.parent.parent.name
            if run_id in _NAMED_RUN_IDS:
                named_runs[run_id] = {
                    "temperature_uid_count": len(run_uids),
                    "event_file_sha256": sha256_bytes(raw),
                }
        event_uids_by_protocol[protocol] = protocol_uids

    if malformed_count:
        raise TemperatureCapacityPreflightError("HISTORICAL_EVENT_LEDGER_BLANK_RECORD")
    if set(named_runs) != set(_NAMED_RUN_IDS):
        raise TemperatureCapacityPreflightError("NAMED_HISTORICAL_RUN_EVIDENCE_MISSING")

    event_union = set().union(*event_uids_by_protocol.values())
    return {
        "file_count": file_count,
        "byte_count": byte_count,
        "malformed_record_count": malformed_count,
        "inventory_sha256": sha256_bytes(b"".join(inventory_entries)),
        "per_protocol": {
            protocol: {
                "event_ledger_count": len(
                    list(
                        (repository / f"state/chembench_supervised_transfer_{protocol}/runs").glob(
                            "*/private/events.jsonl"
                        )
                    )
                ),
                "temperature_uid_count": len(values),
                "by_v2_partition": {
                    name: len(values & partition) for name, partition in partitions.items()
                },
            }
            for protocol, values in event_uids_by_protocol.items()
        },
        "attempted_but_never_evaluated_uid_count": len(attempted_uids - evaluated_uids),
        "unknown_temperature_uid_count": 0,
        "named_run_crosscheck": {name: named_runs[name] for name in _NAMED_RUN_IDS},
        "_event_uids": sorted(event_union),
    }


def _normalized_group_summary(tasks: tuple[object, ...]) -> dict[str, object]:
    counters = {
        "question_ordered_options": Counter(),
        "question_unordered_options": Counter(),
        "question_only": Counter(),
        "unordered_option_set_only": Counter(),
    }
    for task in tasks:
        question = normalize_benchmark_text(task.question)
        options = [normalize_benchmark_text(getattr(task, label)) for label in "ABCD"]
        counters["question_ordered_options"]["\x1f".join([question, *options])] += 1
        counters["question_unordered_options"]["\x1f".join([question, *sorted(options)])] += 1
        counters["question_only"][question] += 1
        counters["unordered_option_set_only"]["\x1f".join(sorted(options))] += 1

    exact_checks = {
        name: {
            "unique_group_count": len(counter),
            "duplicate_group_count": sum(count > 1 for count in counter.values()),
            "duplicate_item_count": sum(count - 1 for count in counter.values()),
            "maximum_group_size": max(counter.values()),
        }
        for name, counter in counters.items()
    }
    return {
        "exact_normalized_checks": exact_checks,
        "upstream_group_fields_available": [],
        "semantic_near_duplicate_grouping_status": "NOT_RUN_AFTER_EARLIER_CAPACITY_GATE",
        "interpretation": (
            "Exact duplicate checks cannot establish semantic source-group isolation; "
            "additional grouping can only reduce the eligible upper bound."
        ),
    }


def _read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TemperatureCapacityPreflightError("AUTHORITATIVE_JSONL_UNREADABLE") from exc
    rows: list[dict[str, object]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TemperatureCapacityPreflightError("AUTHORITATIVE_JSONL_MALFORMED") from exc
        if not isinstance(row, dict):
            raise TemperatureCapacityPreflightError("AUTHORITATIVE_JSONL_ROW_INVALID")
        rows.append(row)
    if not rows:
        raise TemperatureCapacityPreflightError("AUTHORITATIVE_JSONL_EMPTY")
    return tuple(rows)


def _file_sha256(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise TemperatureCapacityPreflightError("AUTHORITATIVE_FILE_UNREADABLE") from exc


__all__ = [
    "ARM_SIZE_TIERS",
    "CATEGORY",
    "FINDING_CODE",
    "MINIMUM_ARM_SIZE",
    "MINIMUM_REQUIRED_TOTAL",
    "PROTOCOL_ID",
    "SCHEMA_VERSION",
    "TemperatureCapacityPreflightError",
    "build_capacity_preflight",
    "select_equal_arm_size",
    "uid_set_sha256",
]
