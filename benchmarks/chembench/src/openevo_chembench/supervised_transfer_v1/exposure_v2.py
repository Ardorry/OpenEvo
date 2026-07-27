"""Evidence-derived historical exposure taxonomy for supervised transfer v1.

The scanner deliberately separates a UID appearing in a frozen manifest from a
UID for which an executor, evaluator, or reflector actually ran.  Public output
contains identities, counts, labels, and digests only; benchmark content is never
copied from the historical repository.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    PrivateChemBench4KTask,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    require_git_commit,
    require_sha256,
    sha256_bytes,
    write_public_file,
)

EXPOSURE_MANIFEST_SCHEMA_V2 = "historical_exposure_manifest_v2"
EXPOSURE_SUMMARY_SCHEMA_V2 = "historical_exposure_summary_v2"
EXPOSURE_RECEIPT_SCHEMA_V2 = "historical_exposure_receipt_v2"
HUMAN_REVIEW_SCHEMA_V1 = "human_item_review_index_v1"
PROMPT_RENDER_SCHEMA_V1 = "chembench_prompt_render_receipt_v1"
AGGREGATE_REVIEW_SCHEMA_V1 = "aggregate_review_receipt_v1"


class ExposureLabelV2(str, Enum):
    MANIFEST_LISTED_ONLY = "MANIFEST_LISTED_ONLY"
    PROMPT_RENDERED_ONLY = "PROMPT_RENDERED_ONLY"
    TASK_MODEL_ATTEMPTED = "TASK_MODEL_ATTEMPTED"
    TASK_MODEL_EXECUTED = "TASK_MODEL_EXECUTED"
    PRIVATE_EVALUATED = "PRIVATE_EVALUATED"
    REFLECTOR_SUPERVISED = "REFLECTOR_SUPERVISED"
    HUMAN_ITEM_REVIEWED = "HUMAN_ITEM_REVIEWED"
    AGGREGATE_ONLY_REVIEWED = "AGGREGATE_ONLY_REVIEWED"


ACTUAL_EXPOSURE_LABELS_V2 = frozenset(
    {
        ExposureLabelV2.TASK_MODEL_ATTEMPTED,
        ExposureLabelV2.TASK_MODEL_EXECUTED,
        ExposureLabelV2.PRIVATE_EVALUATED,
        ExposureLabelV2.REFLECTOR_SUPERVISED,
        ExposureLabelV2.HUMAN_ITEM_REVIEWED,
    }
)
NON_EXCLUDING_LABELS_V2 = frozenset(
    {
        ExposureLabelV2.MANIFEST_LISTED_ONLY,
        ExposureLabelV2.PROMPT_RENDERED_ONLY,
        ExposureLabelV2.AGGREGATE_ONLY_REVIEWED,
    }
)
_JSON_SUFFIXES = frozenset({".json", ".jsonl"})


class HistoricalExposureV2Error(RuntimeError):
    """Fail-closed exposure reconstruction error."""


@dataclass(frozen=True, slots=True)
class HistoricalExposureItemV2:
    uid: str
    category: str
    labels: tuple[str, ...]
    first_exposure_time: str | None
    first_exposure_protocol: str
    attempt_count: int
    completion_count: int
    evaluation_count: int
    reflector_input_count: int
    human_item_reviewed: bool
    evidence_digests: tuple[str, ...]
    evidence_source_types: tuple[str, ...]

    def __post_init__(self) -> None:
        require_sha256(self.uid, "uid")
        if self.category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("exposure category is unsupported")
        expected_order = tuple(
            label.value for label in ExposureLabelV2 if label.value in set(self.labels)
        )
        if self.labels != expected_order or not self.labels:
            raise ValueError("exposure labels are not a non-empty canonical subset")
        label_set = {ExposureLabelV2(label) for label in self.labels}
        if ExposureLabelV2.MANIFEST_LISTED_ONLY in label_set and len(label_set) != 1:
            raise ValueError("manifest-listed-only cannot accompany another label")
        if ExposureLabelV2.PROMPT_RENDERED_ONLY in label_set and label_set & ACTUAL_EXPOSURE_LABELS_V2:
            raise ValueError("prompt-rendered-only cannot accompany actual exposure")
        if type(self.human_item_reviewed) is not bool:
            raise ValueError("human review flag must be boolean")
        if self.human_item_reviewed != (
            ExposureLabelV2.HUMAN_ITEM_REVIEWED in label_set
        ):
            raise ValueError("human review boolean does not match derived label")
        for value, name in (
            (self.attempt_count, "attempt_count"),
            (self.completion_count, "completion_count"),
            (self.evaluation_count, "evaluation_count"),
            (self.reflector_input_count, "reflector_input_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.completion_count > self.attempt_count:
            raise ValueError("completion count exceeds attempt count")
        if self.first_exposure_time is not None:
            _require_timestamp(self.first_exposure_time)
        if type(self.first_exposure_protocol) is not str or not self.first_exposure_protocol:
            raise ValueError("first exposure protocol is missing")
        if not self.evidence_digests or any(
            require_sha256(digest, "evidence_digest") != digest
            for digest in self.evidence_digests
        ):
            raise ValueError("evidence digests are invalid")
        if tuple(sorted(set(self.evidence_digests))) != self.evidence_digests:
            raise ValueError("evidence digests are not unique and sorted")
        if (
            not self.evidence_source_types
            or tuple(sorted(set(self.evidence_source_types))) != self.evidence_source_types
        ):
            raise ValueError("evidence source types are not unique and sorted")

    @property
    def actual_exposure(self) -> bool:
        return bool({ExposureLabelV2(label) for label in self.labels} & ACTUAL_EXPOSURE_LABELS_V2)

    def to_payload(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "category": self.category,
            "labels": list(self.labels),
            "first_exposure_time": self.first_exposure_time,
            "first_exposure_protocol": self.first_exposure_protocol,
            "attempt_count": self.attempt_count,
            "completion_count": self.completion_count,
            "evaluation_count": self.evaluation_count,
            "reflector_input_count": self.reflector_input_count,
            "human_item_reviewed": self.human_item_reviewed,
            "evidence_digests": list(self.evidence_digests),
            "evidence_source_types": list(self.evidence_source_types),
        }


@dataclass(frozen=True, slots=True)
class HistoricalExposureBundleV2:
    source_repository_commit: str
    scanned_file_count: int
    scanned_inventory_sha256: str
    aggregate_review_evidence_count: int
    items: tuple[HistoricalExposureItemV2, ...]

    def __post_init__(self) -> None:
        require_git_commit(self.source_repository_commit, "source_repository_commit")
        require_sha256(self.scanned_inventory_sha256, "scanned_inventory_sha256")
        if (
            type(self.scanned_file_count) is not int
            or type(self.aggregate_review_evidence_count) is not int
            or self.scanned_file_count < 1
            or self.aggregate_review_evidence_count < 0
        ):
            raise ValueError("exposure scan counts are invalid")
        if tuple(sorted(self.items, key=lambda item: item.uid)) != self.items:
            raise ValueError("exposure items are not UID-sorted")
        if len({item.uid for item in self.items}) != len(self.items):
            raise ValueError("exposure items contain duplicate UIDs")

    @property
    def actual_exposed_uids(self) -> frozenset[str]:
        return frozenset(item.uid for item in self.items if item.actual_exposure)

    @property
    def strict_holdout_uids(self) -> frozenset[str]:
        return frozenset(item.uid for item in self.items if not item.actual_exposure)

    def manifest_payload(self) -> dict[str, object]:
        return {
            "schema_version": EXPOSURE_MANIFEST_SCHEMA_V2,
            "source_repository_commit": self.source_repository_commit,
            "scanned_file_count": self.scanned_file_count,
            "scanned_inventory_sha256": self.scanned_inventory_sha256,
            "item_count": len(self.items),
            "items": [item.to_payload() for item in self.items],
        }

    def manifest_bytes(self) -> bytes:
        return canonical_pretty_json_bytes(self.manifest_payload())

    @property
    def manifest_sha256(self) -> str:
        return sha256_bytes(self.manifest_bytes())

    def summary_payload(self) -> dict[str, object]:
        label_counts = Counter(
            label for item in self.items for label in item.labels
        )
        category = {
            name: {
                "dataset_items": sum(item.category == name for item in self.items),
                "actual_exposed": sum(
                    item.category == name and item.actual_exposure for item in self.items
                ),
                "strict_never_executed": sum(
                    item.category == name and not item.actual_exposure for item in self.items
                ),
            }
            for name in CHEMBENCH4K_CATEGORIES
        }
        return {
            "schema_version": EXPOSURE_SUMMARY_SCHEMA_V2,
            "source_repository_commit": self.source_repository_commit,
            "manifest_sha256": self.manifest_sha256,
            "taxonomy": {
                "actual_exposure_labels": sorted(label.value for label in ACTUAL_EXPOSURE_LABELS_V2),
                "non_excluding_labels": sorted(label.value for label in NON_EXCLUDING_LABELS_V2),
                "manifest_listing_alone_excludes_holdout": False,
            },
            "label_counts": {
                label.value: label_counts[label.value] for label in ExposureLabelV2
            },
            "actual_exposed_uid_count": len(self.actual_exposed_uids),
            "strict_never_executed_uid_count": len(self.strict_holdout_uids),
            "aggregate_review_evidence_count": self.aggregate_review_evidence_count,
            "per_category": category,
        }

    def summary_bytes(self) -> bytes:
        return canonical_pretty_json_bytes(self.summary_payload())

    @property
    def summary_sha256(self) -> str:
        return sha256_bytes(self.summary_bytes())

    def receipt_payload(
        self,
        *,
        old_v1_manifest_sha256: str,
        old_blocked_receipt_sha256: str,
    ) -> dict[str, object]:
        require_sha256(old_v1_manifest_sha256, "old_v1_manifest_sha256")
        require_sha256(old_blocked_receipt_sha256, "old_blocked_receipt_sha256")
        taxonomy_contract = {
            "labels": [label.value for label in ExposureLabelV2],
            "actual_exposure_labels": sorted(label.value for label in ACTUAL_EXPOSURE_LABELS_V2),
            "manifest_listing_alone_excludes_holdout": False,
            "labels_are_derived_from_file_evidence": True,
        }
        return {
            "schema_version": EXPOSURE_RECEIPT_SCHEMA_V2,
            "status": "PASS",
            "source_repository_commit": self.source_repository_commit,
            "scanned_file_count": self.scanned_file_count,
            "scanned_inventory_sha256": self.scanned_inventory_sha256,
            "taxonomy_contract_sha256": sha256_bytes(canonical_json_bytes(taxonomy_contract)),
            "manifest_sha256": self.manifest_sha256,
            "summary_sha256": self.summary_sha256,
            "old_v1_manifest_sha256": old_v1_manifest_sha256,
            "old_blocked_receipt_sha256": old_blocked_receipt_sha256,
            "old_blocker_preserved": True,
            "actual_exposed_uid_count": len(self.actual_exposed_uids),
            "strict_never_executed_uid_count": len(self.strict_holdout_uids),
        }

    def receipt_bytes(
        self,
        *,
        old_v1_manifest_sha256: str,
        old_blocked_receipt_sha256: str,
    ) -> bytes:
        return canonical_pretty_json_bytes(
            self.receipt_payload(
                old_v1_manifest_sha256=old_v1_manifest_sha256,
                old_blocked_receipt_sha256=old_blocked_receipt_sha256,
            )
        )


@dataclass(frozen=True, slots=True)
class PostFreezeExposureAuditV2:
    """Content-free proof that later evidence did not consume frozen holdouts."""

    frozen_manifest_sha256: str
    live_manifest_sha256: str
    new_actual_exposed_uid_count: int
    frozen_holdout_uid_count: int
    holdout_overlap_count: int
    new_actual_exposed_uid_set_sha256: str
    holdout_overlap_uid_set_sha256: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.frozen_manifest_sha256, "frozen_manifest_sha256"),
            (self.live_manifest_sha256, "live_manifest_sha256"),
            (self.new_actual_exposed_uid_set_sha256, "new_actual_exposed_uid_set_sha256"),
            (self.holdout_overlap_uid_set_sha256, "holdout_overlap_uid_set_sha256"),
        ):
            require_sha256(value, name)
        for value in (
            self.new_actual_exposed_uid_count,
            self.frozen_holdout_uid_count,
            self.holdout_overlap_count,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("post-freeze exposure counts are invalid")
        if self.holdout_overlap_count:
            raise HistoricalExposureV2Error(
                "post-freeze actual exposure intersects frozen holdout"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "HistoricalExposurePostFreezeIsolationAuditV2",
            "status": "PASS",
            "frozen_manifest_sha256": self.frozen_manifest_sha256,
            "live_manifest_sha256": self.live_manifest_sha256,
            "new_actual_exposed_uid_count": self.new_actual_exposed_uid_count,
            "frozen_holdout_uid_count": self.frozen_holdout_uid_count,
            "holdout_overlap_count": self.holdout_overlap_count,
            "new_actual_exposed_uid_set_sha256": self.new_actual_exposed_uid_set_sha256,
            "holdout_overlap_uid_set_sha256": self.holdout_overlap_uid_set_sha256,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_payload()))


@dataclass(slots=True)
class _ItemAccumulator:
    task: PrivateChemBench4KTask
    manifest_evidence: set[str] = field(default_factory=set)
    prompt_evidence: set[str] = field(default_factory=set)
    actual_labels: set[ExposureLabelV2] = field(default_factory=set)
    attempt_ids: set[str] = field(default_factory=set)
    completion_ids: set[str] = field(default_factory=set)
    evaluation_ids: set[str] = field(default_factory=set)
    reflector_ids: set[str] = field(default_factory=set)
    evidence_digests: set[str] = field(default_factory=set)
    evidence_source_types: set[str] = field(default_factory=set)
    exposure_order: list[tuple[str | None, str, str]] = field(default_factory=list)

    def add_evidence(
        self,
        *,
        source_type: str,
        evidence_digest: str,
        protocol: str,
        timestamp: str | None,
        labels: set[ExposureLabelV2],
        identity: str,
    ) -> None:
        self.evidence_digests.add(evidence_digest)
        self.evidence_source_types.add(source_type)
        self.exposure_order.append((timestamp, protocol, evidence_digest))
        if source_type == "manifest_listing":
            self.manifest_evidence.add(identity)
        if source_type == "prompt_render_receipt":
            self.prompt_evidence.add(identity)
        self.actual_labels.update(labels & ACTUAL_EXPOSURE_LABELS_V2)
        if ExposureLabelV2.TASK_MODEL_ATTEMPTED in labels:
            self.attempt_ids.add(identity)
        if ExposureLabelV2.TASK_MODEL_EXECUTED in labels:
            self.completion_ids.add(identity)
        if ExposureLabelV2.PRIVATE_EVALUATED in labels:
            self.evaluation_ids.add(identity)
        if ExposureLabelV2.REFLECTOR_SUPERVISED in labels:
            self.reflector_ids.add(identity)

    def freeze(self) -> HistoricalExposureItemV2:
        labels = set(self.actual_labels)
        if not labels:
            if self.prompt_evidence:
                labels.add(ExposureLabelV2.PROMPT_RENDERED_ONLY)
            elif self.manifest_evidence:
                labels.add(ExposureLabelV2.MANIFEST_LISTED_ONLY)
            else:
                raise HistoricalExposureV2Error("UID has no classified evidence")
        order = sorted(
            self.exposure_order,
            key=lambda row: (row[0] is None, row[0] or "", row[1], row[2]),
        )
        first_time, first_protocol, _digest = order[0]
        return HistoricalExposureItemV2(
            uid=self.task.uid,
            category=self.task.category,
            labels=tuple(label.value for label in ExposureLabelV2 if label in labels),
            first_exposure_time=first_time,
            first_exposure_protocol=first_protocol,
            attempt_count=len(self.attempt_ids),
            completion_count=len(self.completion_ids),
            evaluation_count=len(self.evaluation_ids),
            reflector_input_count=len(self.reflector_ids),
            human_item_reviewed=ExposureLabelV2.HUMAN_ITEM_REVIEWED in labels,
            evidence_digests=tuple(sorted(self.evidence_digests)),
            evidence_source_types=tuple(sorted(self.evidence_source_types)),
        )


def build_historical_exposure_bundle_v2(
    *,
    test_tasks: tuple[PrivateChemBench4KTask, ...],
    old_repository: Path,
    source_repository_commit: str,
) -> HistoricalExposureBundleV2:
    """Derive every label from immutable JSON evidence below two approved roots."""

    if (
        not isinstance(test_tasks, tuple)
        or not test_tasks
        or any(type(task) is not PrivateChemBench4KTask for task in test_tasks)
    ):
        raise TypeError("test_tasks must be a non-empty exact tuple")
    require_git_commit(source_repository_commit, "source_repository_commit")
    if not isinstance(old_repository, Path) or not old_repository.is_absolute():
        raise TypeError("old_repository must be an absolute Path")
    old_repository = old_repository.resolve()
    roots = (
        old_repository / "results",
        old_repository / "benchmarks" / "chembench" / "manifests",
    )
    if any(not root.is_dir() or root.is_symlink() for root in roots):
        raise HistoricalExposureV2Error("historical evidence roots are missing or unsafe")

    by_uid = {task.uid: task for task in test_tasks}
    if len(by_uid) != len(test_tasks):
        raise ValueError("test task UIDs are not unique")
    accumulators = {uid: _ItemAccumulator(task) for uid, task in by_uid.items()}
    inventory: list[dict[str, object]] = []
    aggregate_review_count = 0
    for root in roots:
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix.casefold() not in _JSON_SUFFIXES
            ):
                continue
            payload_bytes = path.read_bytes()
            relative = path.relative_to(old_repository).as_posix()
            file_digest = sha256_bytes(payload_bytes)
            inventory.append(
                {"path": relative, "sha256": file_digest, "size": len(payload_bytes)}
            )
            payloads = tuple(_parse_payloads(payload_bytes, suffix=path.suffix.casefold()))
            if path.is_relative_to(roots[1]):
                for ordinal, payload in enumerate(payloads):
                    protocol = _protocol(payload, relative)
                    for uid in sorted(_known_uids(payload, frozenset(by_uid))):
                        evidence_digest = _evidence_digest(
                            relative=relative,
                            file_sha256=file_digest,
                            ordinal=ordinal,
                            pointer="manifest",
                            source_type="manifest_listing",
                            uid=uid,
                            labels=(),
                        )
                        accumulators[uid].add_evidence(
                            source_type="manifest_listing",
                            evidence_digest=evidence_digest,
                            protocol=protocol,
                            timestamp=None,
                            labels=set(),
                            identity=evidence_digest,
                        )
                continue
            for ordinal, payload in enumerate(payloads):
                if isinstance(payload, dict) and payload.get("schema_version") == AGGREGATE_REVIEW_SCHEMA_V1:
                    aggregate_review_count += 1
                for pointer, record in _walk_records(payload):
                    uid = record.get("task_uid")
                    if not isinstance(uid, str) or uid not in accumulators:
                        continue
                    category = record.get("category")
                    if category is not None and category != by_uid[uid].category:
                        raise HistoricalExposureV2Error("historical category binding mismatch")
                    classified = _classify_result_record(
                        record,
                        relative=relative,
                        uid=uid,
                    )
                    if classified is None:
                        continue
                    source_type, labels, identity, timestamp, protocol = classified
                    evidence_digest = _evidence_digest(
                        relative=relative,
                        file_sha256=file_digest,
                        ordinal=ordinal,
                        pointer=pointer,
                        source_type=source_type,
                        uid=uid,
                        labels=tuple(sorted(label.value for label in labels)),
                    )
                    accumulators[uid].add_evidence(
                        source_type=source_type,
                        evidence_digest=evidence_digest,
                        protocol=protocol,
                        timestamp=timestamp,
                        labels=labels,
                        identity=sha256_bytes(identity.encode("utf-8")),
                    )

    missing_listing = [uid for uid, item in accumulators.items() if not item.manifest_evidence]
    if missing_listing:
        raise HistoricalExposureV2Error("dataset UID is absent from the historical manifest inventory")
    items = tuple(accumulators[uid].freeze() for uid in sorted(accumulators))
    return HistoricalExposureBundleV2(
        source_repository_commit=source_repository_commit,
        scanned_file_count=len(inventory),
        scanned_inventory_sha256=sha256_bytes(canonical_json_bytes(inventory)),
        aggregate_review_evidence_count=aggregate_review_count,
        items=items,
    )


def write_historical_exposure_artifacts_v2(
    bundle: HistoricalExposureBundleV2,
    *,
    destination_root: Path,
    old_v1_manifest_sha256: str,
    old_blocked_receipt_sha256: str,
) -> dict[str, str]:
    if type(bundle) is not HistoricalExposureBundleV2:
        raise TypeError("bundle must be exact HistoricalExposureBundleV2")
    outputs = {
        "historical_exposure_manifest_v2.json": bundle.manifest_bytes(),
        "historical_exposure_summary_v2.json": bundle.summary_bytes(),
        "historical_exposure_receipt_v2.json": bundle.receipt_bytes(
            old_v1_manifest_sha256=old_v1_manifest_sha256,
            old_blocked_receipt_sha256=old_blocked_receipt_sha256,
        ),
    }
    for name, payload in outputs.items():
        write_public_file(destination_root / name, payload)
    return {name: sha256_bytes(payload) for name, payload in outputs.items()}


def verify_historical_exposure_artifacts_v2(
    bundle: HistoricalExposureBundleV2,
    *,
    destination_root: Path,
    old_v1_manifest_sha256: str,
    old_blocked_receipt_sha256: str,
) -> dict[str, str]:
    expected = {
        "historical_exposure_manifest_v2.json": bundle.manifest_bytes(),
        "historical_exposure_summary_v2.json": bundle.summary_bytes(),
        "historical_exposure_receipt_v2.json": bundle.receipt_bytes(
            old_v1_manifest_sha256=old_v1_manifest_sha256,
            old_blocked_receipt_sha256=old_blocked_receipt_sha256,
        ),
    }
    for name, payload in expected.items():
        if (destination_root / name).read_bytes() != payload:
            raise HistoricalExposureV2Error(f"{name} does not match deterministic regeneration")
    return {name: sha256_bytes(payload) for name, payload in expected.items()}


def load_historical_exposure_artifacts_v2(
    *,
    destination_root: Path,
    old_v1_manifest_sha256: str,
    old_blocked_receipt_sha256: str,
) -> tuple[HistoricalExposureBundleV2, dict[str, str]]:
    """Load the frozen evidence snapshot and rederive every canonical artifact byte."""

    if not isinstance(destination_root, Path) or not destination_root.is_absolute():
        raise TypeError("destination_root must be an absolute Path")
    manifest_path = destination_root / "historical_exposure_manifest_v2.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoricalExposureV2Error("frozen exposure manifest is unavailable") from exc
    required_manifest_keys = {
        "schema_version",
        "source_repository_commit",
        "scanned_file_count",
        "scanned_inventory_sha256",
        "item_count",
        "items",
    }
    if type(payload) is not dict or set(payload) != required_manifest_keys:
        raise HistoricalExposureV2Error("frozen exposure manifest schema is invalid")
    if payload["schema_version"] != EXPOSURE_MANIFEST_SCHEMA_V2:
        raise HistoricalExposureV2Error("frozen exposure manifest version is invalid")
    raw_items = payload["items"]
    if type(raw_items) is not list or payload["item_count"] != len(raw_items):
        raise HistoricalExposureV2Error("frozen exposure item count is invalid")
    required_item_keys = {
        "uid",
        "category",
        "labels",
        "first_exposure_time",
        "first_exposure_protocol",
        "attempt_count",
        "completion_count",
        "evaluation_count",
        "reflector_input_count",
        "human_item_reviewed",
        "evidence_digests",
        "evidence_source_types",
    }
    items: list[HistoricalExposureItemV2] = []
    try:
        for raw in raw_items:
            if type(raw) is not dict or set(raw) != required_item_keys:
                raise HistoricalExposureV2Error("frozen exposure item schema is invalid")
            if type(raw["labels"]) is not list or type(raw["evidence_digests"]) is not list:
                raise HistoricalExposureV2Error("frozen exposure item collection is invalid")
            if type(raw["evidence_source_types"]) is not list:
                raise HistoricalExposureV2Error("frozen exposure source types are invalid")
            items.append(
                HistoricalExposureItemV2(
                    uid=raw["uid"],
                    category=raw["category"],
                    labels=tuple(raw["labels"]),
                    first_exposure_time=raw["first_exposure_time"],
                    first_exposure_protocol=raw["first_exposure_protocol"],
                    attempt_count=raw["attempt_count"],
                    completion_count=raw["completion_count"],
                    evaluation_count=raw["evaluation_count"],
                    reflector_input_count=raw["reflector_input_count"],
                    human_item_reviewed=raw["human_item_reviewed"],
                    evidence_digests=tuple(raw["evidence_digests"]),
                    evidence_source_types=tuple(raw["evidence_source_types"]),
                )
            )
        bundle = HistoricalExposureBundleV2(
            source_repository_commit=payload["source_repository_commit"],
            scanned_file_count=payload["scanned_file_count"],
            scanned_inventory_sha256=payload["scanned_inventory_sha256"],
            aggregate_review_evidence_count=json.loads(
                (destination_root / "historical_exposure_summary_v2.json").read_text(
                    encoding="utf-8"
                )
            )["aggregate_review_evidence_count"],
            items=tuple(items),
        )
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, HistoricalExposureV2Error):
            raise
        raise HistoricalExposureV2Error("frozen exposure artifact is invalid") from exc
    digests = verify_historical_exposure_artifacts_v2(
        bundle,
        destination_root=destination_root,
        old_v1_manifest_sha256=old_v1_manifest_sha256,
        old_blocked_receipt_sha256=old_blocked_receipt_sha256,
    )
    return bundle, digests


def audit_post_freeze_exposure_isolation_v2(
    *,
    frozen_bundle: HistoricalExposureBundleV2,
    live_bundle: HistoricalExposureBundleV2,
    frozen_holdout_uids: frozenset[str],
) -> PostFreezeExposureAuditV2:
    """Reject later real execution of any UID reserved as Probe or Test."""

    if type(frozen_bundle) is not HistoricalExposureBundleV2:
        raise TypeError("frozen_bundle must be exact HistoricalExposureBundleV2")
    if type(live_bundle) is not HistoricalExposureBundleV2:
        raise TypeError("live_bundle must be exact HistoricalExposureBundleV2")
    if type(frozen_holdout_uids) is not frozenset or any(
        type(uid) is not str for uid in frozen_holdout_uids
    ):
        raise TypeError("frozen_holdout_uids must be a frozenset of strings")
    known_uids = frozenset(item.uid for item in frozen_bundle.items)
    if frozenset(item.uid for item in live_bundle.items) != known_uids:
        raise HistoricalExposureV2Error("live exposure dataset identity changed")
    if not frozen_holdout_uids or not frozen_holdout_uids <= known_uids:
        raise HistoricalExposureV2Error("frozen holdout identity is invalid")
    new_actual = live_bundle.actual_exposed_uids - frozen_bundle.actual_exposed_uids
    overlap = live_bundle.actual_exposed_uids & frozen_holdout_uids

    def uid_set_digest(values: frozenset[str]) -> str:
        return sha256_bytes(("\n".join(sorted(values)) + "\n").encode("utf-8"))

    return PostFreezeExposureAuditV2(
        frozen_manifest_sha256=frozen_bundle.manifest_sha256,
        live_manifest_sha256=live_bundle.manifest_sha256,
        new_actual_exposed_uid_count=len(new_actual),
        frozen_holdout_uid_count=len(frozen_holdout_uids),
        holdout_overlap_count=len(overlap),
        new_actual_exposed_uid_set_sha256=uid_set_digest(frozenset(new_actual)),
        holdout_overlap_uid_set_sha256=uid_set_digest(frozenset(overlap)),
    )


def _classify_result_record(
    record: dict[str, Any],
    *,
    relative: str,
    uid: str,
) -> tuple[str, set[ExposureLabelV2], str, str | None, str] | None:
    labels: set[ExposureLabelV2] = set()
    source_type = ""
    schema = record.get("schema_version")
    kind = record.get("kind")
    if schema == HUMAN_REVIEW_SCHEMA_V1:
        if record.get("review_scope") != "item_specific":
            return None
        labels.add(ExposureLabelV2.HUMAN_ITEM_REVIEWED)
        source_type = "human_item_review_index"
    elif schema == PROMPT_RENDER_SCHEMA_V1:
        if record.get("model_invocation_started") is not False:
            raise HistoricalExposureV2Error("prompt-only receipt is not explicitly non-executed")
        source_type = "prompt_render_receipt"
    elif kind == "completion" and _text(record.get("session_id")):
        labels.update(
            {ExposureLabelV2.TASK_MODEL_ATTEMPTED, ExposureLabelV2.TASK_MODEL_EXECUTED}
        )
        source_type = "session_completion_event"
    elif kind == "core_update" and (
        _text(record.get("core_job_id")) or _text(record.get("evolution_job_id"))
    ):
        labels.add(ExposureLabelV2.REFLECTOR_SUPERVISED)
        source_type = "core_update_event"
    elif "raw_completion" in record and isinstance(record.get("raw_completion"), str):
        labels.update(
            {ExposureLabelV2.TASK_MODEL_ATTEMPTED, ExposureLabelV2.TASK_MODEL_EXECUTED}
        )
        source_type = "completion_record"
        if "target" in record and ("correct" in record or "score" in record):
            labels.add(ExposureLabelV2.PRIVATE_EVALUATED)
            source_type = "private_evaluation_row"
    elif relative.endswith("failures.jsonl"):
        labels.add(ExposureLabelV2.TASK_MODEL_ATTEMPTED)
        if record.get("completion_observed") is True:
            labels.add(ExposureLabelV2.TASK_MODEL_EXECUTED)
        source_type = "task_attempt_failure_metadata"
    elif (
        relative.endswith("result.json")
        and isinstance(record.get("completion_count"), int)
        and record["completion_count"] > 0
    ):
        labels.update(
            {ExposureLabelV2.TASK_MODEL_ATTEMPTED, ExposureLabelV2.TASK_MODEL_EXECUTED}
        )
        source_type = "session_result_receipt"
        if (record.get("core_jobs_created") or 0) > 0 or (
            record.get("core_artifacts_registered") or 0
        ) > 0:
            labels.add(ExposureLabelV2.REFLECTOR_SUPERVISED)
    else:
        return None

    protocol = _protocol(record, relative)
    timestamp = _timestamp(record)
    round_index = record.get("round_index", record.get("source_round_index", -1))
    run_scope = _run_scope(relative)
    identity_value = next(
        (
            value
            for value in (
                _text(record.get("session_id")),
                _nested_text(record, "context_binding", "session_id"),
                _nested_text(record, "trajectory", "session_id"),
                _text(record.get("core_job_id")),
                _text(record.get("evolution_job_id")),
                _text(record.get("transcript_reference")),
                _text(record.get("run_id")),
            )
            if value
        ),
        f"{run_scope}:{uid}:{round_index}:{source_type}",
    )
    # Evaluation/result/event copies for the same run-round must share one count.
    if source_type in {
        "session_completion_event",
        "completion_record",
        "private_evaluation_row",
        "session_result_receipt",
    }:
        identity_value = f"{run_scope}:{uid}:{round_index}"
    return source_type, labels, identity_value, timestamp, protocol


def _parse_payloads(payload: bytes, *, suffix: str) -> Iterable[object]:
    try:
        text = payload.decode("utf-8")
        if suffix == ".jsonl":
            for line in text.splitlines():
                if line.strip():
                    yield json.loads(line)
        else:
            yield json.loads(text)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return


def _walk_records(value: object, pointer: str = "$") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield pointer, value
        for key in sorted(value):
            yield from _walk_records(value[key], f"{pointer}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_records(item, f"{pointer}[{index}]")


def _known_uids(value: object, known: frozenset[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key in known:
                found.add(key)
            found.update(_known_uids(item, known))
    elif isinstance(value, list):
        for item in value:
            found.update(_known_uids(item, known))
    elif isinstance(value, str) and value in known:
        found.add(value)
    return found


def _evidence_digest(
    *,
    relative: str,
    file_sha256: str,
    ordinal: int,
    pointer: str,
    source_type: str,
    uid: str,
    labels: tuple[str, ...],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "relative_path": relative,
                "file_sha256": file_sha256,
                "record_ordinal": ordinal,
                "record_pointer": pointer,
                "source_type": source_type,
                "task_uid": uid,
                "labels": list(labels),
            }
        )
    )


def _protocol(record: object, relative: str) -> str:
    if isinstance(record, dict):
        for key in ("protocol_id", "protocol", "protocol_name"):
            value = _text(record.get(key))
            if value:
                return value[:160]
    lowered = relative.casefold()
    for token in (
        "taskwise_online_v1",
        "frozen_generalization_v2",
        "chembench_evolution",
        "smoke1",
        "canary9",
        "pilot500",
    ):
        if token in lowered:
            return token
    return Path(relative).stem[:160] or "unknown_protocol"


def _timestamp(record: dict[str, Any]) -> str | None:
    candidates = (
        record.get("started_at_utc"),
        record.get("created_at_utc"),
        record.get("reviewed_at_utc"),
        (record.get("runtime_metadata") or {}).get("started_at_utc")
        if isinstance(record.get("runtime_metadata"), dict)
        else None,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return _require_timestamp(candidate)
    return None


def _require_timestamp(value: str) -> str:
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HistoricalExposureV2Error("historical evidence timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise HistoricalExposureV2Error("historical evidence timestamp lacks timezone")
    return value


def _run_scope(relative: str) -> str:
    path = Path(relative)
    parts = path.parts
    for marker in ("private", "public"):
        if marker in parts:
            return "/".join(parts[: parts.index(marker)])
    return "/".join(parts[:-1])


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nested_text(record: dict[str, Any], parent: str, child: str) -> str | None:
    value = record.get(parent)
    return _text(value.get(child)) if isinstance(value, dict) else None


__all__ = [
    "ACTUAL_EXPOSURE_LABELS_V2",
    "EXPOSURE_MANIFEST_SCHEMA_V2",
    "EXPOSURE_RECEIPT_SCHEMA_V2",
    "EXPOSURE_SUMMARY_SCHEMA_V2",
    "ExposureLabelV2",
    "HistoricalExposureBundleV2",
    "HistoricalExposureItemV2",
    "HistoricalExposureV2Error",
    "PostFreezeExposureAuditV2",
    "audit_post_freeze_exposure_isolation_v2",
    "build_historical_exposure_bundle_v2",
    "load_historical_exposure_artifacts_v2",
    "verify_historical_exposure_artifacts_v2",
    "write_historical_exposure_artifacts_v2",
]
