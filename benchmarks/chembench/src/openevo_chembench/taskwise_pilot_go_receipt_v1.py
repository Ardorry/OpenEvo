"""Recomputed pilot-GO authority for taskwise full benchmark execution.

This receipt is standalone benchmark evidence.  It binds the immutable paired
pilot outcome to the current source, paired-canary authority, and every full
stream input.  Callers provide paths only; neither ``decision`` nor a claimed
PASS boolean is accepted as input.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.source_identity_v2 import sha256_file, verify_source_manifest
from openevo_chembench.taskwise_canary_receipt_v1 import (
    TaskwisePairedCanaryReceiptInputsV1,
    default_taskwise_canary_receipt_inputs_v1,
    default_taskwise_canary_receipt_path_v1,
    verify_taskwise_paired_canary_receipt_v1,
)
from openevo_chembench.taskwise_attempt_v1 import (
    require_taskwise_completed_paired_attempt_v1,
    validate_taskwise_attempt_id_v1,
)
from openevo_chembench.taskwise_config_v1 import (
    load_taskwise_config_v1,
    taskwise_arm_parity_findings,
)
from openevo_chembench.taskwise_generation_v1 import (
    derive_taskwise_generation_id_v1,
    taskwise_pilot_comparison_path_v1,
    validate_taskwise_generation_id_v1,
)
from openevo_chembench.taskwise_sampling_v1 import (
    FULL_STREAM_COUNT,
    FULL_STREAM_SCOPES,
    PILOT500_STREAM_COUNT,
    full_stream_suite_summary_bytes,
    verify_taskwise_manifests,
)


RECEIPT_SCHEMA = "taskwise_pilot_go_receipt_v1"
EVIDENCE_CLASS = "STANDALONE_MAINTAINER_BENCHMARK_EVIDENCE"
PRODUCT_DISCLAIMER = "NOT_OPENEVO_PRODUCT_RELEASE_ATTESTATION"
PILOT_GATE_CLASSIFICATION = "PREREGISTERED_TASKWISE_PILOT_GO_AUTHORITY"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_RESERVED_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_class",
        "product_release_disclaimer",
        "pilot_gate_classification",
        "created_at_utc",
        "paid_full_allowed",
        "finding_codes",
        "directly_verified_fields",
        "evidence_digest",
    }
)


class TaskwisePilotGOFindingV1(str, Enum):
    SOURCE_INVALID = "SOURCE_INVALID"
    CORE_SOURCE_DIRTY = "CORE_SOURCE_DIRTY"
    CANARY_AUTHORITY_INVALID = "CANARY_AUTHORITY_INVALID"
    PILOT_GENERATION_INVALID = "PILOT_GENERATION_INVALID"
    PILOT_REPORT_INVALID = "PILOT_REPORT_INVALID"
    PILOT_DECISION_NOT_GO = "PILOT_DECISION_NOT_GO"
    FULL_CONFIG_INVALID = "FULL_CONFIG_INVALID"
    FULL_MANIFEST_INVALID = "FULL_MANIFEST_INVALID"
    FULL_BINDING_INVALID = "FULL_BINDING_INVALID"


@dataclass(frozen=True, slots=True)
class TaskwisePilotGOReceiptInputsV1:
    """Filesystem authorities read and recomputed by the full-run gate."""

    repository_root: Path
    package_root: Path
    dataset_root: Path
    source_manifest_path: Path
    canary_inputs: TaskwisePairedCanaryReceiptInputsV1

    def __post_init__(self) -> None:
        for field_name in (
            "repository_root",
            "package_root",
            "dataset_root",
            "source_manifest_path",
        ):
            if not isinstance(getattr(self, field_name), Path):
                raise TypeError(f"{field_name} must be pathlib.Path")
        if type(self.canary_inputs) is not TaskwisePairedCanaryReceiptInputsV1:
            raise TypeError("canary_inputs must be exact paired-canary inputs")


@dataclass(frozen=True, slots=True)
class TaskwisePilotGOReceiptV1:
    """Content-free evidence whose findings exclusively derive full authority."""

    fields: Mapping[str, Any]
    finding_codes: tuple[TaskwisePilotGOFindingV1, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        if type(self.fields) is not dict or not all(type(key) is str for key in self.fields):
            raise TypeError("receipt fields must be an exact string-keyed dict")
        if _RESERVED_FIELDS.intersection(self.fields):
            raise ValueError("receipt fields cannot override reserved fields")
        if (
            type(self.finding_codes) is not tuple
            or not all(type(item) is TaskwisePilotGOFindingV1 for item in self.finding_codes)
            or len(self.finding_codes) != len(set(self.finding_codes))
        ):
            raise TypeError("finding_codes must be unique closed enum values")
        if type(self.created_at_utc) is not str or not self.created_at_utc:
            raise TypeError("created_at_utc must be non-empty text")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def paid_full_allowed(self) -> bool:
        return not self.finding_codes

    @property
    def evidence_digest(self) -> str:
        return _sha256_json(
            {
                "schema_version": RECEIPT_SCHEMA,
                "evidence_class": EVIDENCE_CLASS,
                "product_release_disclaimer": PRODUCT_DISCLAIMER,
                "pilot_gate_classification": PILOT_GATE_CLASSIFICATION,
                **self.fields,
                "finding_codes": [item.value for item in self.finding_codes],
                "paid_full_allowed": self.paid_full_allowed,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA,
            "evidence_class": EVIDENCE_CLASS,
            "product_release_disclaimer": PRODUCT_DISCLAIMER,
            "pilot_gate_classification": PILOT_GATE_CLASSIFICATION,
            **self.fields,
            "created_at_utc": self.created_at_utc,
            "paid_full_allowed": self.paid_full_allowed,
            "finding_codes": [item.value for item in self.finding_codes],
            "directly_verified_fields": sorted(self.fields),
            "evidence_digest": self.evidence_digest,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_payload())


@dataclass(frozen=True, slots=True, init=False)
class TaskwiseFullAuthorizationV1:
    """Unforgeable in-process capability issued by exact receipt recomputation."""

    receipt_sha256: str
    evidence_digest: str
    source_commit: str
    canary_receipt_sha256: str
    pilot_generation_id: str
    pilot_attempt_id: str
    pilot_report_sha256: str
    full_binding_sha256: str
    full_generation_id: str

    def __init__(
        self,
        *,
        receipt_sha256: str,
        evidence_digest: str,
        source_commit: str,
        canary_receipt_sha256: str,
        pilot_generation_id: str,
        pilot_attempt_id: str,
        pilot_report_sha256: str,
        full_binding_sha256: str,
        full_generation_id: str,
        _issuer_token: object | None = None,
    ) -> None:
        if not _authorization_token_is_valid(_issuer_token):
            raise TypeError("TaskwiseFullAuthorizationV1 is issued only by the receipt gate")
        for value in (
            receipt_sha256,
            evidence_digest,
            canary_receipt_sha256,
            pilot_report_sha256,
            full_binding_sha256,
        ):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError("authorization digests must be SHA-256")
        if type(source_commit) is not str or _COMMIT_RE.fullmatch(source_commit) is None:
            raise ValueError("authorization source_commit must be a Git commit")
        validate_taskwise_generation_id_v1(pilot_generation_id)
        validate_taskwise_attempt_id_v1(pilot_attempt_id)
        validate_taskwise_generation_id_v1(full_generation_id)
        object.__setattr__(self, "receipt_sha256", receipt_sha256)
        object.__setattr__(self, "evidence_digest", evidence_digest)
        object.__setattr__(self, "source_commit", source_commit)
        object.__setattr__(self, "canary_receipt_sha256", canary_receipt_sha256)
        object.__setattr__(self, "pilot_generation_id", pilot_generation_id)
        object.__setattr__(self, "pilot_attempt_id", pilot_attempt_id)
        object.__setattr__(self, "pilot_report_sha256", pilot_report_sha256)
        object.__setattr__(self, "full_binding_sha256", full_binding_sha256)
        object.__setattr__(self, "full_generation_id", full_generation_id)


def _make_authorization_issuer() -> tuple[Any, Any]:
    token = object()

    def token_is_valid(candidate: object | None) -> bool:
        return candidate is token

    def issue(**values: str) -> TaskwiseFullAuthorizationV1:
        return TaskwiseFullAuthorizationV1(**values, _issuer_token=token)

    return token_is_valid, issue


_authorization_token_is_valid, _issue_taskwise_full_authorization_v1 = _make_authorization_issuer()


def default_taskwise_pilot_go_receipt_inputs_v1(
    package_root: Path,
) -> TaskwisePilotGOReceiptInputsV1:
    package = package_root.resolve()
    canary = default_taskwise_canary_receipt_inputs_v1(package)
    return TaskwisePilotGOReceiptInputsV1(
        repository_root=canary.repository_root,
        package_root=package,
        dataset_root=canary.dataset_root,
        source_manifest_path=canary.source_manifest_path,
        canary_inputs=canary,
    )


def default_taskwise_pilot_go_receipt_path_v1(
    package_root: Path,
    pilot_generation_id: str,
) -> Path:
    generation = validate_taskwise_generation_id_v1(pilot_generation_id)
    return (
        package_root.resolve()
        / "state"
        / "taskwise_online_v1"
        / "pilot500_generations"
        / generation
        / "pilot_go_receipt_v1.json"
    )


def recompute_taskwise_pilot_go_receipt_v1(
    inputs: TaskwisePilotGOReceiptInputsV1,
) -> TaskwisePilotGOReceiptV1:
    """Recompute pilot GO and all full input bindings without caller claims."""

    if type(inputs) is not TaskwisePilotGOReceiptInputsV1:
        raise TypeError("inputs must be exact TaskwisePilotGOReceiptInputsV1")
    findings: set[TaskwisePilotGOFindingV1] = set()
    fields: dict[str, Any] = {
        "protocol_id": "taskwise_online_evolution_v1",
        "control_protocol_id": "repeated_session_control_v1",
    }

    source_commit: str | None = None
    try:
        source_commit = _git(inputs.repository_root, "rev-parse", "HEAD")
        if _COMMIT_RE.fullmatch(source_commit) is None:
            raise RuntimeError
        fields["source_commit"] = source_commit
        fields["chembench_source_manifest_sha256"] = verify_source_manifest(
            inputs.package_root,
            inputs.source_manifest_path,
        )
        if _git(
            inputs.repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "benchmarks/chembench",
        ):
            raise RuntimeError
    except Exception:
        fields.setdefault("source_commit", source_commit)
        fields["chembench_source_manifest_sha256"] = None
        findings.add(TaskwisePilotGOFindingV1.SOURCE_INVALID)
    try:
        if _git(
            inputs.repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "src/openevo",
        ):
            raise RuntimeError
    except Exception:
        findings.add(TaskwisePilotGOFindingV1.CORE_SOURCE_DIRTY)

    pilot_generation: str | None = None
    try:
        canary_path = default_taskwise_canary_receipt_path_v1(inputs.package_root)
        canary_authorization = verify_taskwise_paired_canary_receipt_v1(
            inputs.canary_inputs,
            canary_path,
        )
        pilot_generation = validate_taskwise_generation_id_v1(
            canary_authorization.paired_canary_generation_id
        )
        fields.update(
            {
                "canary_receipt_sha256": canary_authorization.receipt_sha256,
                "canary_evidence_digest": canary_authorization.evidence_digest,
                "paired_canary_generation_id": (canary_authorization.paired_canary_generation_id),
                "pilot_binding_sha256": canary_authorization.pilot_binding_sha256,
                "pilot_generation_id": pilot_generation,
            }
        )
        if source_commit != canary_authorization.source_commit:
            raise RuntimeError
    except Exception:
        for key in (
            "canary_receipt_sha256",
            "canary_evidence_digest",
            "paired_canary_generation_id",
            "pilot_binding_sha256",
            "pilot_generation_id",
        ):
            fields.setdefault(key, None)
        findings.add(TaskwisePilotGOFindingV1.CANARY_AUTHORITY_INVALID)

    pilot_attempt: str | None = None
    try:
        if pilot_generation is None:
            raise RuntimeError
        pilot_attempt = require_taskwise_completed_paired_attempt_v1(
            inputs.repository_root,
            suite_kind="pilot500",
            generation_id=pilot_generation,
        )
        # Local import avoids a module cycle.  The comparison routine itself
        # revalidates canary authority, all stream configs, manifests, run
        # bindings, and private paired results before rebuilding the report.
        from openevo_chembench.taskwise_cli_v1 import compare_pilot500_streams

        report = compare_pilot500_streams(
            generation_id=pilot_generation,
            attempt_id=pilot_attempt,
            persist=False,
        )
        expected_report = _canonical_bytes(report)
        report_path = taskwise_pilot_comparison_path_v1(
            inputs.repository_root,
            pilot_generation,
            pilot_attempt,
        )
        persisted = _read_regular(report_path, mode=0o600)
        if persisted != expected_report:
            raise RuntimeError
        fields.update(
            {
                "pilot_report_sha256": hashlib.sha256(expected_report).hexdigest(),
                "pilot_attempt_id": pilot_attempt,
                "pilot_report_status": report.get("status"),
                "pilot_decision": report.get("decision"),
                "pilot_completed_streams": report.get("completed_streams"),
                "pilot_stream_count": report.get("stream_count"),
                "pilot_task_count": report.get("task_count"),
            }
        )
        if (
            report.get("status") != "COMPLETED"
            or report.get("decision") != "GO"
            or report.get("completed_streams") != PILOT500_STREAM_COUNT
            or report.get("stream_count") != PILOT500_STREAM_COUNT
            or report.get("task_count") != 500
        ):
            findings.add(TaskwisePilotGOFindingV1.PILOT_DECISION_NOT_GO)
    except Exception:
        for key in (
            "pilot_report_sha256",
            "pilot_attempt_id",
            "pilot_report_status",
            "pilot_decision",
            "pilot_completed_streams",
            "pilot_stream_count",
            "pilot_task_count",
        ):
            fields.setdefault(key, None)
        findings.add(TaskwisePilotGOFindingV1.PILOT_REPORT_INVALID)

    try:
        loader = ChemBench4KDatasetLoader(snapshot_root=inputs.dataset_root)
        fields["dataset_combined_sha256"] = loader.manifest.combined_sha256
        full_binding = _recompute_full_binding(inputs, loader)
        fields.update(full_binding)
        if source_commit is None or pilot_generation is None:
            raise RuntimeError
        full_generation = derive_taskwise_generation_id_v1(
            {
                "schema_version": "taskwise_full_generation_authority_v1",
                "source_commit": source_commit,
                "pilot_generation_id": pilot_generation,
                "pilot_attempt_id": pilot_attempt,
                "pilot_report_sha256": fields.get("pilot_report_sha256"),
                "full_binding_sha256": full_binding["full_binding_sha256"],
            }
        )
        fields["full_generation_id"] = full_generation
    except Exception:
        fields.setdefault("dataset_combined_sha256", None)
        fields.setdefault("full_binding_sha256", None)
        fields.setdefault("full_suite_summary_sha256", None)
        fields.setdefault("full_ordered_uid_sha256", None)
        fields.setdefault("full_config_manifest_sha256", None)
        fields.setdefault("full_generation_id", None)
        findings.add(TaskwisePilotGOFindingV1.FULL_BINDING_INVALID)

    return TaskwisePilotGOReceiptV1(
        fields=fields,
        finding_codes=tuple(sorted(findings, key=lambda item: item.value)),
        created_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def write_taskwise_pilot_go_receipt_v1(
    inputs: TaskwisePilotGOReceiptInputsV1,
    output_path: Path,
) -> TaskwisePilotGOReceiptV1:
    receipt = recompute_taskwise_pilot_go_receipt_v1(inputs)
    if not receipt.paid_full_allowed:
        raise RuntimeError("taskwise pilot findings block full receipt freeze")
    output_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    output_path.parent.chmod(0o700)
    descriptor = os.open(
        output_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(receipt.canonical_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    return receipt


def verify_taskwise_pilot_go_receipt_v1(
    inputs: TaskwisePilotGOReceiptInputsV1,
    receipt_path: Path,
) -> TaskwiseFullAuthorizationV1:
    """Recompute every authority and issue a full-run capability."""

    stored_bytes = _read_regular(receipt_path, mode=0o600)
    try:
        stored = json.loads(stored_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("taskwise pilot GO receipt is invalid") from exc
    timestamp = stored.get("created_at_utc") if type(stored) is dict else None
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp) if type(timestamp) is str else None
    except ValueError:
        parsed_timestamp = None
    if (
        type(stored) is not dict
        or stored_bytes != _canonical_bytes(stored)
        or parsed_timestamp is None
        or parsed_timestamp.tzinfo != UTC
        or parsed_timestamp.isoformat(timespec="seconds") != timestamp
    ):
        raise RuntimeError("taskwise pilot GO receipt is invalid")
    current = recompute_taskwise_pilot_go_receipt_v1(inputs)
    expected = current.to_payload()
    expected.pop("created_at_utc", None)
    stored_without_time = dict(stored)
    stored_without_time.pop("created_at_utc", None)
    if stored_without_time != expected or not current.paid_full_allowed:
        raise RuntimeError("taskwise pilot GO receipt no longer matches evidence")
    values = {
        "source_commit": current.fields.get("source_commit"),
        "canary_receipt_sha256": current.fields.get("canary_receipt_sha256"),
        "pilot_generation_id": current.fields.get("pilot_generation_id"),
        "pilot_attempt_id": current.fields.get("pilot_attempt_id"),
        "pilot_report_sha256": current.fields.get("pilot_report_sha256"),
        "full_binding_sha256": current.fields.get("full_binding_sha256"),
        "full_generation_id": current.fields.get("full_generation_id"),
    }
    if any(type(value) is not str for value in values.values()):
        raise RuntimeError("taskwise full authorization fields are unavailable")
    return _issue_taskwise_full_authorization_v1(
        receipt_sha256=hashlib.sha256(stored_bytes).hexdigest(),
        evidence_digest=current.evidence_digest,
        **values,
    )


def _recompute_full_binding(
    inputs: TaskwisePilotGOReceiptInputsV1,
    loader: ChemBench4KDatasetLoader,
) -> dict[str, str]:
    config_root = inputs.package_root / "configs"
    manifests = []
    entries: list[dict[str, str | int]] = []
    for scope in FULL_STREAM_SCOPES:
        paired = {
            arm: load_taskwise_config_v1(config_root / f"{arm}_{scope}_taskwise_online_v1.yaml")
            for arm in ("control", "online")
        }
        if (
            paired["control"].scope != scope
            or paired["online"].scope != scope
            or taskwise_arm_parity_findings(paired["control"], paired["online"])
        ):
            raise RuntimeError("full paired config is invalid")
        public = _workspace_path(
            inputs.repository_root.parent,
            paired["control"].task_manifest,
        )
        private = _workspace_path(
            inputs.repository_root.parent,
            paired["control"].private_task_manifest,
        )
        summary = public.with_name(public.name.replace("_public_manifest.jsonl", "_summary.json"))
        manifest = verify_taskwise_manifests(
            loader,
            scope=scope,
            public_path=public,
            private_path=private,
            summary_path=summary,
        )
        manifests.append(manifest)
        entries.append(
            {
                "scope": scope,
                "item_count": manifest.item_count,
                "control_config_sha256": paired["control"].config_sha256(),
                "online_config_sha256": paired["online"].config_sha256(),
                "public_manifest_sha256": manifest.public_sha256,
                "private_manifest_sha256": manifest.private_sha256,
                "summary_sha256": manifest.summary_sha256,
                "ordered_uid_sha256": manifest.ordered_uid_sha256,
            }
        )
    if len(manifests) != FULL_STREAM_COUNT or sum(item.item_count for item in manifests) != 4009:
        raise RuntimeError("full stream coverage is invalid")
    expected_summary = full_stream_suite_summary_bytes(
        loader,
        stream_manifests=tuple(manifests),
    )
    summary_path = (
        inputs.package_root / "manifests" / "taskwise_online_v1" / "online_full_summary.json"
    )
    if summary_path.read_bytes() != expected_summary:
        raise RuntimeError("full suite summary is not frozen")
    summary = json.loads(expected_summary)
    if (
        summary.get("stream_count") != FULL_STREAM_COUNT
        or summary.get("total_item_count") != 4009
        or summary.get("reset_memory_between_streams") is not True
    ):
        raise RuntimeError("full suite summary is invalid")
    suite_configs = {
        arm: sha256_file(config_root / f"{arm}_full_taskwise_online_v1.yaml")
        for arm in ("control", "online")
    }
    binding = {
        "schema_version": "taskwise_full_binding_v1",
        "suite_summary_sha256": hashlib.sha256(expected_summary).hexdigest(),
        "ordered_uid_sha256": summary["global_ordered_uid_sha256"],
        "suite_config_sha256": suite_configs,
        "stream_configs_and_manifests": entries,
    }
    return {
        "full_binding_sha256": _sha256_json(binding),
        "full_suite_summary_sha256": binding["suite_summary_sha256"],
        "full_ordered_uid_sha256": binding["ordered_uid_sha256"],
        "full_config_manifest_sha256": _sha256_json(
            {
                "suite_config_sha256": suite_configs,
                "stream_configs": entries,
            }
        ),
    }


def _workspace_path(workspace_root: Path, relative: str) -> Path:
    if type(relative) is not str or not relative:
        raise RuntimeError("workspace-relative path is invalid")
    candidate = (workspace_root / relative).resolve()
    try:
        candidate.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise RuntimeError("workspace-relative path escapes root") from exc
    return candidate


def _read_regular(path: Path, *, mode: int) -> bytes:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise RuntimeError("private evidence file attestation failed")
    return path.read_bytes()


def _git(repository_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repository_root,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("git identity unavailable")
    return result.stdout.strip()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


__all__ = [
    "EVIDENCE_CLASS",
    "PILOT_GATE_CLASSIFICATION",
    "PRODUCT_DISCLAIMER",
    "RECEIPT_SCHEMA",
    "TaskwiseFullAuthorizationV1",
    "TaskwisePilotGOFindingV1",
    "TaskwisePilotGOReceiptInputsV1",
    "TaskwisePilotGOReceiptV1",
    "default_taskwise_pilot_go_receipt_inputs_v1",
    "default_taskwise_pilot_go_receipt_path_v1",
    "recompute_taskwise_pilot_go_receipt_v1",
    "verify_taskwise_pilot_go_receipt_v1",
    "write_taskwise_pilot_go_receipt_v1",
]
