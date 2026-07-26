"""Content-free historical ChemBench4K exposure inventory."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    require_git_commit,
    sha256_bytes,
    write_public_file,
)

HISTORICAL_EXPOSURE_SCHEMA = "historical_exposed_uid_manifest_v1"
_RUN_ID_KEYS = (
    "run_id",
    "suite_run_id",
    "experiment_run_id",
    "generation_id",
)
_PROTOCOL_KEYS = ("protocol_id", "protocol", "protocol_name")
_JSON_SUFFIXES = frozenset({".json", ".jsonl"})
_PATH_PROTOCOL_TOKENS = (
    "taskwise_online_v1",
    "frozen_generalization_v2",
    "chembench_evolution",
    "online",
    "baseline",
)


class HistoricalExposureError(RuntimeError):
    """Fail-closed historical inventory error without task content."""


@dataclass(frozen=True, slots=True)
class HistoricalExposureManifestV1:
    source_repository_commit: str
    scanned_file_count: int
    exposed_uid_count: int
    items: tuple[dict[str, object], ...]
    schema_version: str = HISTORICAL_EXPOSURE_SCHEMA

    def __post_init__(self) -> None:
        require_git_commit(self.source_repository_commit, "source_repository_commit")
        if self.schema_version != HISTORICAL_EXPOSURE_SCHEMA:
            raise ValueError("historical exposure schema mismatch")
        if self.exposed_uid_count != len(self.items):
            raise ValueError("historical exposure count mismatch")
        seen: set[str] = set()
        for item in self.items:
            if set(item) != {
                "uid",
                "category",
                "exposure_source_type",
                "source_run_id_hash",
                "first_exposed_protocol",
                "exposure_count",
            }:
                raise ValueError("historical exposure item is not closed")
            uid = item["uid"]
            if type(uid) is not str or len(uid) != 64 or uid in seen:
                raise ValueError("historical exposure UID is invalid or duplicated")
            seen.add(uid)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_repository_commit": self.source_repository_commit,
            "scanned_file_count": self.scanned_file_count,
            "exposed_uid_count": self.exposed_uid_count,
            "items": list(self.items),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_pretty_json_bytes(self.to_payload())

    @property
    def digest(self) -> str:
        return sha256_bytes(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class _ExposureEvidence:
    source_type: str
    source_id_hash: str
    protocol: str
    source_order: str


def build_historical_exposure_manifest(
    *,
    test_tasks: tuple[PrivateChemBench4KTask, ...],
    old_repository: Path,
    source_repository_commit: str,
) -> HistoricalExposureManifestV1:
    """Scan only old results/manifests and retain no benchmark content."""

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
        raise HistoricalExposureError("historical source roots are missing or unsafe")

    task_by_uid = {task.uid: task for task in test_tasks}
    if len(task_by_uid) != len(test_tasks):
        raise ValueError("test task UIDs are not unique")
    evidence_by_uid: dict[str, set[_ExposureEvidence]] = defaultdict(set)
    scanned_file_count = 0
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in _JSON_SUFFIXES:
                continue
            scanned_file_count += 1
            relative = path.relative_to(old_repository).as_posix()
            source_type = _infer_source_type(relative)
            fallback_protocol = _infer_protocol(relative)
            fallback_source_id = _infer_source_id(relative)
            for payload in _read_payloads(path):
                source_id = _find_first_string(payload, _RUN_ID_KEYS) or fallback_source_id
                source_id_hash = sha256_bytes(source_id.encode("utf-8"))
                root_protocol = _find_first_string(payload, _PROTOCOL_KEYS) or fallback_protocol
                for uid, protocol in _walk_uid_evidence(
                    payload,
                    known_uids=frozenset(task_by_uid),
                    inherited_protocol=root_protocol,
                ):
                    evidence_by_uid[uid].add(
                        _ExposureEvidence(
                            source_type=source_type,
                            source_id_hash=source_id_hash,
                            protocol=protocol or fallback_protocol,
                            source_order=relative,
                        )
                    )

    items: list[dict[str, object]] = []
    for uid in sorted(evidence_by_uid):
        evidences = sorted(
            evidence_by_uid[uid],
            key=lambda item: (
                item.source_order,
                item.protocol,
                item.source_type,
                item.source_id_hash,
            ),
        )
        items.append(
            {
                "uid": uid,
                "category": task_by_uid[uid].category,
                "exposure_source_type": sorted({item.source_type for item in evidences}),
                "source_run_id_hash": sorted({item.source_id_hash for item in evidences}),
                "first_exposed_protocol": evidences[0].protocol,
                "exposure_count": len(evidences),
            }
        )
    return HistoricalExposureManifestV1(
        source_repository_commit=source_repository_commit,
        scanned_file_count=scanned_file_count,
        exposed_uid_count=len(items),
        items=tuple(items),
    )


def write_historical_exposure_manifest(
    manifest: HistoricalExposureManifestV1,
    *,
    destination: Path,
) -> Path:
    if type(manifest) is not HistoricalExposureManifestV1:
        raise TypeError("manifest must be exact HistoricalExposureManifestV1")
    write_public_file(destination, manifest.canonical_bytes())
    return destination


def load_historical_exposure_manifest(path: Path) -> HistoricalExposureManifestV1:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoricalExposureError("historical exposure manifest is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "source_repository_commit",
        "scanned_file_count",
        "exposed_uid_count",
        "items",
    }:
        raise HistoricalExposureError("historical exposure manifest is not closed")
    items = payload["items"]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise HistoricalExposureError("historical exposure items are invalid")
    manifest = HistoricalExposureManifestV1(
        schema_version=payload["schema_version"],
        source_repository_commit=payload["source_repository_commit"],
        scanned_file_count=payload["scanned_file_count"],
        exposed_uid_count=payload["exposed_uid_count"],
        items=tuple(dict(item) for item in items),
    )
    if manifest.canonical_bytes() != path.read_bytes():
        raise HistoricalExposureError("historical exposure manifest is not canonical")
    return manifest


def _read_payloads(path: Path) -> Iterable[object]:
    try:
        if path.suffix.lower() == ".jsonl":
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        yield json.loads(line)
        else:
            yield json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return


def _walk_uid_evidence(
    value: object,
    *,
    known_uids: frozenset[str],
    inherited_protocol: str,
) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        protocol = next(
            (
                item
                for key in _PROTOCOL_KEYS
                if isinstance((item := value.get(key)), str) and item.strip()
            ),
            inherited_protocol,
        )
        for key, item in value.items():
            if isinstance(key, str) and key in known_uids:
                yield key, protocol
            yield from _walk_uid_evidence(
                item,
                known_uids=known_uids,
                inherited_protocol=protocol,
            )
    elif isinstance(value, list):
        for item in value:
            yield from _walk_uid_evidence(
                item,
                known_uids=known_uids,
                inherited_protocol=inherited_protocol,
            )
    elif isinstance(value, str) and value in known_uids:
        yield value, inherited_protocol


def _find_first_string(value: object, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for item in value.values():
            found = _find_first_string(item, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_string(item, keys)
            if found is not None:
                return found
    return None


def _infer_source_type(relative: str) -> str:
    normalized = relative.casefold()
    if "checkpoint" in normalized:
        return "checkpoint"
    if "trajectory" in normalized or "attempt" in normalized:
        return "trajectory"
    if "smoke" in normalized:
        return "smoke"
    if "canary" in normalized:
        return "canary"
    if "pilot500" in normalized:
        if "control" in normalized:
            return "pilot500_control"
        if "online" in normalized or "evolution" in normalized:
            return "pilot500_online"
        return "pilot500"
    if "private" in normalized and "manifest" in normalized:
        return "private_task_manifest"
    if "manifest" in normalized:
        return "public_task_manifest"
    if "failed" in normalized or "failure" in normalized:
        return "failed_run"
    return "historical_result"


def _infer_protocol(relative: str) -> str:
    normalized = relative.casefold()
    for token in _PATH_PROTOCOL_TOKENS:
        if token in normalized:
            return token
    stem = Path(relative).stem
    return re.sub(r"[^a-z0-9_.-]+", "_", stem.casefold())[:128] or "unknown_protocol"


def _infer_source_id(relative: str) -> str:
    parts = Path(relative).parts
    if parts and parts[0] == "results" and len(parts) >= 3:
        return "/".join(parts[1:-1])
    return relative


__all__ = [
    "HISTORICAL_EXPOSURE_SCHEMA",
    "HistoricalExposureError",
    "HistoricalExposureManifestV1",
    "build_historical_exposure_manifest",
    "load_historical_exposure_manifest",
    "write_historical_exposure_manifest",
]
