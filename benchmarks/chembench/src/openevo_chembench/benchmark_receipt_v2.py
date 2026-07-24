"""Directly recomputed standalone benchmark evidence for ChemBench4K v2.

This is intentionally not an OpenEvo Desktop/Daemon product attestation.  It
binds the standalone maintainer benchmark to local dataset, source, Core
registry/job/artifact/context, executor, prompt, config, and task-manifest
state.  Callers provide paths, never verification booleans.
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

from openevo_chembench.artifact_validator_v2 import (
    ChemBench4KTextMemoryArtifactValidatorV2,
)
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.core_evolution_v2 import (
    FrozenCoreTextMemoryV2,
    OpenEvoTextMemoryLifecycleV2,
)
from openevo_chembench.sampling_v2 import verify_task_manifests
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorBoundaryStatusV2,
    ReflectorExecutionReceiptV2,
)
from openevo_chembench.source_identity_v2 import (
    sha256_file,
    verify_source_manifest,
)
from openevo_chembench.v2_config import (
    DATASET_REPOSITORY,
    DATASET_REVISION,
    EXECUTION_MODE,
    MODEL,
    PROTOCOL_ID,
    REASONING_EFFORT,
    FrozenExperimentConfigV2,
    arm_parity_findings,
    load_frozen_config_v2,
)


RECEIPT_SCHEMA = "benchmark_execution_receipt_v2"
DISCLAIMER = "STANDALONE_MAINTAINER_BENCHMARK_EVIDENCE"
PRODUCT_DISCLAIMER = "NOT_OPENEVO_PRODUCT_RELEASE_ATTESTATION"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESERVED_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_class",
        "product_release_disclaimer",
        "created_at_utc",
        "paid_execution_allowed",
        "finding_codes",
        "directly_verified_fields",
        "evidence_digest",
    }
)


class ReceiptFindingV2(str, Enum):
    DATASET_INVALID = "DATASET_INVALID"
    SOURCE_MANIFEST_INVALID = "SOURCE_MANIFEST_INVALID"
    CORE_SOURCE_DIRTY = "CORE_SOURCE_DIRTY"
    CORE_COMMIT_MISMATCH = "CORE_COMMIT_MISMATCH"
    BENCHMARK_SOURCE_UNCOMMITTED = "BENCHMARK_SOURCE_UNCOMMITTED"
    BENCHMARK_SOURCE_ACCEPTANCE_INVALID = "BENCHMARK_SOURCE_ACCEPTANCE_INVALID"
    PILOT_MANIFEST_INVALID = "PILOT_MANIFEST_INVALID"
    CONFIG_INVALID = "CONFIG_INVALID"
    ARM_PARITY_MISMATCH = "ARM_PARITY_MISMATCH"
    CODEX_VERSION_MISMATCH = "CODEX_VERSION_MISMATCH"
    FROZEN_ARTIFACT_MISSING = "FROZEN_ARTIFACT_MISSING"
    FRAMEWORK_LOCK_INVALID = "FRAMEWORK_LOCK_INVALID"
    METHOD_REGISTRY_INVALID = "METHOD_REGISTRY_INVALID"
    CORE_JOB_INVALID = "CORE_JOB_INVALID"
    CORE_ARTIFACT_INVALID = "CORE_ARTIFACT_INVALID"
    CORE_CONTEXT_INVALID = "CORE_CONTEXT_INVALID"
    ARTIFACT_CONFIG_BINDING_INVALID = "ARTIFACT_CONFIG_BINDING_INVALID"
    REFLECTOR_SECURITY_INVALID = "REFLECTOR_SECURITY_INVALID"


@dataclass(frozen=True, slots=True)
class BenchmarkReceiptInputsV2:
    """Only filesystem/identity inputs read by the receipt gate."""

    workspace_root: Path
    repository_root: Path
    package_root: Path
    dataset_root: Path
    source_manifest_path: Path
    pilot_public_manifest_path: Path
    pilot_private_manifest_path: Path
    pilot_summary_path: Path
    baseline_config_path: Path
    evolved_config_path: Path
    framework_lock_path: Path
    core_database_path: Path
    core_artifact_root: Path
    frozen_record_path: Path
    reflector_private_audit_root: Path
    source_acceptance_path: Path | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_root",
            "repository_root",
            "package_root",
            "dataset_root",
            "source_manifest_path",
            "pilot_public_manifest_path",
            "pilot_private_manifest_path",
            "pilot_summary_path",
            "baseline_config_path",
            "evolved_config_path",
            "framework_lock_path",
            "core_database_path",
            "core_artifact_root",
            "frozen_record_path",
            "reflector_private_audit_root",
        ):
            if not isinstance(getattr(self, field_name), Path):
                raise TypeError(f"{field_name} must be pathlib.Path")
        if self.source_acceptance_path is not None and not isinstance(
            self.source_acceptance_path,
            Path,
        ):
            raise TypeError("source_acceptance_path must be pathlib.Path or None")


@dataclass(frozen=True, slots=True)
class BenchmarkExecutionReceiptV2:
    """Public, content-free standalone evidence and launch decision."""

    fields: Mapping[str, Any]
    finding_codes: tuple[ReceiptFindingV2, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        if type(self.fields) is not dict or not all(type(key) is str for key in self.fields):
            raise TypeError("receipt fields must be an exact string-keyed dict")
        if _RESERVED_RECEIPT_FIELDS & set(self.fields):
            raise ValueError("receipt fields cannot override reserved evidence fields")
        object.__setattr__(
            self,
            "fields",
            MappingProxyType(dict(self.fields)),
        )
        if (
            type(self.finding_codes) is not tuple
            or not all(type(item) is ReceiptFindingV2 for item in self.finding_codes)
            or len(self.finding_codes) != len(set(self.finding_codes))
        ):
            raise TypeError("finding_codes must be unique ReceiptFindingV2 values")
        if type(self.created_at_utc) is not str or not self.created_at_utc:
            raise TypeError("created_at_utc must be non-empty text")

    @property
    def paid_execution_allowed(self) -> bool:
        return not self.finding_codes

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA,
            "evidence_class": DISCLAIMER,
            "product_release_disclaimer": PRODUCT_DISCLAIMER,
            **self.fields,
            "created_at_utc": self.created_at_utc,
            "paid_execution_allowed": self.paid_execution_allowed,
            "finding_codes": [finding.value for finding in self.finding_codes],
            "directly_verified_fields": sorted(self.fields),
            "evidence_digest": self.evidence_digest,
        }

    @property
    def evidence_digest(self) -> str:
        return hashlib.sha256(
            _canonical_json_bytes(
                {
                    "schema_version": RECEIPT_SCHEMA,
                    "evidence_class": DISCLAIMER,
                    "product_release_disclaimer": PRODUCT_DISCLAIMER,
                    **self.fields,
                    "finding_codes": [finding.value for finding in self.finding_codes],
                    "paid_execution_allowed": self.paid_execution_allowed,
                }
            )
        ).hexdigest()

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_payload())


@dataclass(frozen=True, slots=True, init=False)
class BenchmarkAuthorizationV2:
    """Unforgeable-in-process launch token issued after full recomputation."""

    receipt_sha256: str
    evidence_digest: str

    def __init__(
        self,
        *,
        receipt_sha256: str,
        evidence_digest: str,
        _issuer_token: object | None = None,
    ) -> None:
        if not _authorization_token_is_valid(_issuer_token):
            raise TypeError("BenchmarkAuthorizationV2 is issued only by the receipt gate")
        if _SHA256.fullmatch(receipt_sha256) is None or _SHA256.fullmatch(evidence_digest) is None:
            raise ValueError("authorization digests must be SHA-256")
        object.__setattr__(self, "receipt_sha256", receipt_sha256)
        object.__setattr__(self, "evidence_digest", evidence_digest)


def _make_authorization_issuer() -> tuple[Any, Any]:
    """Keep the raw issuer capability out of module globals."""

    issuer_token = object()

    def token_is_valid(candidate: object | None) -> bool:
        return candidate is issuer_token

    def issue(
        *,
        receipt_sha256: str,
        evidence_digest: str,
    ) -> BenchmarkAuthorizationV2:
        return BenchmarkAuthorizationV2(
            receipt_sha256=receipt_sha256,
            evidence_digest=evidence_digest,
            _issuer_token=issuer_token,
        )

    return token_is_valid, issue


(
    _authorization_token_is_valid,
    _issue_benchmark_authorization_v2,
) = _make_authorization_issuer()


def _canonical_json_bytes(value: object) -> bytes:
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


def _git(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("git identity command failed")
    return completed.stdout.strip()


def _core_source_manifest_digest(repository_root: Path) -> str:
    source_root = repository_root / "src" / "openevo"
    if not source_root.is_dir() or source_root.is_symlink():
        raise RuntimeError("Core source root is unavailable")
    entries: list[dict[str, object]] = []
    for path in sorted(source_root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise RuntimeError("Core source contains a symlink")
        if not path.is_file():
            continue
        entries.append(
            {
                "path": path.relative_to(repository_root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not entries:
        raise RuntimeError("Core source manifest is empty")
    return hashlib.sha256(_canonical_json_bytes(entries)).hexdigest()


def _module_sha256(package_root: Path, filename: str) -> str:
    return sha256_file(package_root / "src" / "openevo_chembench" / filename)


def _actual_codex_version(repository_root: Path) -> str:
    completed = subprocess.run(
        ("codex", "--version"),
        cwd=repository_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=15,
        env={"PATH": os.environ.get("PATH", "")},
    )
    if completed.returncode != 0:
        raise RuntimeError("Codex CLI version command failed")
    match = re.fullmatch(r"codex-cli ([0-9A-Za-z.+-]+)\s*", completed.stdout)
    if match is None:
        raise RuntimeError("Codex CLI version output is invalid")
    return match.group(1)


def _source_is_committed_or_accepted(
    inputs: BenchmarkReceiptInputsV2,
    source_manifest_sha256: str,
) -> tuple[bool, bool]:
    relative_package = inputs.package_root.resolve().relative_to(inputs.repository_root.resolve())
    status = _git(
        inputs.repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        relative_package.as_posix(),
    )
    committed = status == ""
    if committed:
        return True, False
    acceptance = inputs.source_acceptance_path
    if acceptance is None or not acceptance.is_file():
        return False, False
    try:
        payload = json.loads(acceptance.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, False
    expected_keys = {
        "schema_version",
        "accepted_by",
        "accepted_at_utc",
        "source_manifest_sha256",
        "risk_acknowledgement",
    }
    accepted = (
        isinstance(payload, dict)
        and set(payload) == expected_keys
        and payload.get("schema_version") == "chembench_source_manifest_acceptance_v2"
        and type(payload.get("accepted_by")) is str
        and bool(payload.get("accepted_by"))
        and type(payload.get("accepted_at_utc")) is str
        and bool(payload.get("accepted_at_utc"))
        and payload.get("source_manifest_sha256") == source_manifest_sha256
        and payload.get("risk_acknowledgement")
        == "I accept source-manifest-only audit for this paid benchmark run."
    )
    return False, bool(accepted)


def _load_bound_reflector_receipt(
    audit_root: Path,
    *,
    expected_digest: str | None,
) -> ReflectorExecutionReceiptV2:
    if (
        expected_digest is None
        or _SHA256.fullmatch(expected_digest) is None
        or not audit_root.is_dir()
        or audit_root.is_symlink()
    ):
        raise RuntimeError("reflector execution receipt is unavailable")
    matches: list[ReflectorExecutionReceiptV2] = []
    for path in sorted(audit_root.glob("*/receipt.json")):
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            receipt = ReflectorExecutionReceiptV2.from_payload(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
            continue
        if receipt.digest != expected_digest:
            continue
        reference = Path(receipt.private_event_reference)
        if (
            reference.is_absolute()
            or len(reference.parts) != 2
            or ".." in reference.parts
            or reference.parts[0] != receipt.invocation_id
            or reference.name != "events.jsonl"
        ):
            continue
        event_path = audit_root / reference
        try:
            event_metadata = event_path.lstat()
            if (
                stat.S_ISLNK(event_metadata.st_mode)
                or not stat.S_ISREG(event_metadata.st_mode)
                or stat.S_IMODE(event_metadata.st_mode) != 0o600
                or sha256_file(event_path) != receipt.event_stream_sha256
            ):
                continue
        except OSError:
            continue
        matches.append(receipt)
    if len(matches) != 1:
        raise RuntimeError("reflector execution receipt is unavailable")
    return matches[0]


def recompute_benchmark_receipt_v2(
    inputs: BenchmarkReceiptInputsV2,
) -> BenchmarkExecutionReceiptV2:
    """Read every authority directly and return a fail-closed receipt."""

    if type(inputs) is not BenchmarkReceiptInputsV2:
        raise TypeError("inputs must be exact BenchmarkReceiptInputsV2")
    findings: set[ReceiptFindingV2] = set()
    fields: dict[str, Any] = {
        "protocol_id": PROTOCOL_ID,
        "execution_mode": EXECUTION_MODE,
        "dataset_repository": DATASET_REPOSITORY,
        "dataset_revision": DATASET_REVISION,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
    }

    loader: ChemBench4KDatasetLoader | None = None
    try:
        loader = ChemBench4KDatasetLoader(snapshot_root=inputs.dataset_root)
        fields["dataset_combined_sha256"] = loader.manifest.combined_sha256
        fields["dataset_manifest_sha256"] = sha256_file(
            inputs.dataset_root / "chembench4k_dataset_manifest_v2.json"
        )
    except Exception:
        findings.add(ReceiptFindingV2.DATASET_INVALID)
        fields["dataset_combined_sha256"] = None
        fields["dataset_manifest_sha256"] = None

    try:
        source_manifest_sha256 = verify_source_manifest(
            inputs.package_root,
            inputs.source_manifest_path,
        )
        fields["chembench_package_manifest_sha256"] = source_manifest_sha256
    except Exception:
        source_manifest_sha256 = ""
        fields["chembench_package_manifest_sha256"] = None
        findings.add(ReceiptFindingV2.SOURCE_MANIFEST_INVALID)

    try:
        fields["openevo_core_git_commit"] = _git(
            inputs.repository_root,
            "rev-parse",
            "HEAD",
        )
        fields["source_tree_manifest_sha256"] = _core_source_manifest_digest(
            inputs.repository_root
        )
        core_status = _git(
            inputs.repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "src/openevo",
        )
        if core_status:
            findings.add(ReceiptFindingV2.CORE_SOURCE_DIRTY)
    except Exception:
        fields["openevo_core_git_commit"] = None
        fields["source_tree_manifest_sha256"] = None
        findings.add(ReceiptFindingV2.CORE_COMMIT_MISMATCH)

    try:
        committed, accepted = _source_is_committed_or_accepted(
            inputs,
            source_manifest_sha256,
        )
        fields["benchmark_source_committed"] = committed
        fields["source_manifest_only_acceptance"] = accepted
        if not committed and not accepted:
            findings.add(ReceiptFindingV2.BENCHMARK_SOURCE_UNCOMMITTED)
    except Exception:
        fields["benchmark_source_committed"] = False
        fields["source_manifest_only_acceptance"] = False
        findings.add(ReceiptFindingV2.BENCHMARK_SOURCE_ACCEPTANCE_INVALID)

    baseline: FrozenExperimentConfigV2 | None = None
    evolved: FrozenExperimentConfigV2 | None = None
    try:
        baseline = load_frozen_config_v2(inputs.baseline_config_path)
        evolved = load_frozen_config_v2(inputs.evolved_config_path)
        fields["baseline_config_sha256"] = baseline.config_sha256()
        fields["evolved_config_sha256"] = evolved.config_sha256()
        parity = arm_parity_findings(baseline, evolved)
        if parity:
            findings.add(ReceiptFindingV2.ARM_PARITY_MISMATCH)
    except Exception:
        fields["baseline_config_sha256"] = None
        fields["evolved_config_sha256"] = None
        findings.add(ReceiptFindingV2.CONFIG_INVALID)

    fields["prompt_renderer_sha256"] = _module_sha256(
        inputs.package_root,
        "chembench4k_prompt.py",
    )
    fields["parser_sha256"] = _module_sha256(
        inputs.package_root,
        "chembench4k_evaluation.py",
    )
    fields["evaluator_sha256"] = fields["parser_sha256"]
    fields["executor_policy_sha256"] = hashlib.sha256(
        _canonical_json_bytes(
            {
                "executor_module_sha256": _module_sha256(
                    inputs.package_root,
                    "local_codex_executor.py",
                ),
                "baseline_executor": (
                    None if baseline is None else baseline.to_payload()["executor"]
                ),
                "evolved_executor": (
                    None if evolved is None else evolved.to_payload()["executor"]
                ),
            }
        )
    ).hexdigest()

    if loader is not None:
        try:
            pilot = verify_task_manifests(
                loader,
                scope="pilot500",
                public_path=inputs.pilot_public_manifest_path,
                private_path=inputs.pilot_private_manifest_path,
                summary_path=inputs.pilot_summary_path,
            )
            fields["pilot_manifest_sha256"] = pilot.public_sha256
            fields["pilot_manifest_summary_sha256"] = pilot.summary_sha256
            fields["pilot_ordered_uid_hash"] = pilot.ordered_uid_hash
        except Exception:
            fields["pilot_manifest_sha256"] = None
            fields["pilot_manifest_summary_sha256"] = None
            fields["pilot_ordered_uid_hash"] = None
            findings.add(ReceiptFindingV2.PILOT_MANIFEST_INVALID)
    else:
        fields["pilot_manifest_sha256"] = None
        fields["pilot_manifest_summary_sha256"] = None
        fields["pilot_ordered_uid_hash"] = None
        findings.add(ReceiptFindingV2.PILOT_MANIFEST_INVALID)

    expected_codex = baseline.codex_cli_version if baseline is not None else None
    try:
        actual_codex = _actual_codex_version(inputs.repository_root)
        fields["codex_cli_version"] = actual_codex
        if (
            expected_codex is None
            or evolved is None
            or evolved.codex_cli_version != expected_codex
            or actual_codex != expected_codex
        ):
            findings.add(ReceiptFindingV2.CODEX_VERSION_MISMATCH)
    except Exception:
        fields["codex_cli_version"] = None
        findings.add(ReceiptFindingV2.CODEX_VERSION_MISMATCH)

    frozen: FrozenCoreTextMemoryV2 | None = None
    if inputs.frozen_record_path.is_file():
        try:
            frozen = FrozenCoreTextMemoryV2.model_validate_json(
                inputs.frozen_record_path.read_text(encoding="utf-8")
            )
        except Exception:
            findings.add(ReceiptFindingV2.CORE_ARTIFACT_INVALID)
    else:
        findings.add(ReceiptFindingV2.FROZEN_ARTIFACT_MISSING)

    core_evidence: dict[str, Any] | None = None
    lifecycle: OpenEvoTextMemoryLifecycleV2 | None = None
    if frozen is not None:
        try:
            lifecycle = OpenEvoTextMemoryLifecycleV2.from_framework_lock(
                db_path=inputs.core_database_path,
                artifact_root=inputs.core_artifact_root,
                framework_lock=inputs.framework_lock_path,
            )
            fields["framework_lock_sha256"] = sha256_file(inputs.framework_lock_path)
            if lifecycle.method_evidence != frozen.execution.prepared_job.method:
                findings.add(ReceiptFindingV2.METHOD_REGISTRY_INVALID)
            if loader is None:
                raise RuntimeError("dataset authority is unavailable")
            validator = ChemBench4KTextMemoryArtifactValidatorV2(
                dev_tasks=loader.load_split("dev"),
                test_uids=frozenset(task.uid for task in loader.load_split("test")),
                expected_dev_uid_set_sha256=(frozen.execution.prepared_job.dev_uid_set_sha256),
            )
            core_evidence = lifecycle.verify_frozen_record(
                frozen,
                validator=validator,
            )
        except Exception:
            findings.add(ReceiptFindingV2.FRAMEWORK_LOCK_INVALID)
    else:
        fields["framework_lock_sha256"] = None

    if core_evidence is None:
        fields.update(
            {
                "method_id": None,
                "method_descriptor_digest": None,
                "evolution_plan_id": None,
                "evolution_job_id": None,
                "core_artifact_id": None,
                "core_artifact_manifest_digest": None,
                "artifact_payload_sha256": None,
                "context_resolution_digest": None,
                "resolved_memory_sha256": None,
                "reflector_execution_receipt_sha256": None,
                "reflector_event_stream_sha256": None,
                "reflector_isolation_mechanism": None,
            }
        )
    else:
        fields.update(
            {
                "method_id": core_evidence["method_id"],
                "method_descriptor_digest": core_evidence["method_descriptor_digest"],
                "evolution_plan_id": core_evidence["plan_id"],
                "evolution_job_id": core_evidence["evolution_job_id"],
                "core_artifact_id": core_evidence["core_artifact_id"],
                "core_artifact_manifest_digest": core_evidence["core_artifact_manifest_sha256"],
                "artifact_payload_sha256": core_evidence["artifact_payload_sha256"],
                "context_resolution_digest": core_evidence["context_resolution_digest"],
                "resolved_memory_sha256": core_evidence["resolved_memory_sha256"],
            }
        )
        try:
            if frozen is None:
                raise RuntimeError("frozen reflector evidence is unavailable")
            receipt_digest = frozen.execution.reflector_execution_receipt_sha256
            event_digest = frozen.execution.reflector_event_stream_sha256
            reflector_receipt = _load_bound_reflector_receipt(
                inputs.reflector_private_audit_root,
                expected_digest=receipt_digest,
            )
            if (
                receipt_digest is None
                or event_digest is None
                or reflector_receipt.status is not ReflectorBoundaryStatusV2.COMPLETED
                or not reflector_receipt.wrapper_invoked
                or not reflector_receipt.cleanup_complete
                or reflector_receipt.event_counts
                or reflector_receipt.event_stream_sha256 != event_digest
                or reflector_receipt.retry_allowed
                or reflector_receipt.resume_allowed
                or reflector_receipt.replacement_completion_allowed
            ):
                raise RuntimeError("reflector receipt is not a safe terminal receipt")
            fields["reflector_execution_receipt_sha256"] = receipt_digest
            fields["reflector_event_stream_sha256"] = event_digest
            fields["reflector_isolation_mechanism"] = reflector_receipt.mechanism
        except Exception:
            fields["reflector_execution_receipt_sha256"] = None
            fields["reflector_event_stream_sha256"] = None
            fields["reflector_isolation_mechanism"] = None
            findings.add(ReceiptFindingV2.REFLECTOR_SECURITY_INVALID)
        if baseline is None or evolved is None or frozen is None:
            findings.add(ReceiptFindingV2.ARTIFACT_CONFIG_BINDING_INVALID)
        else:
            binding = evolved.artifact
            if (
                baseline.artifact.enabled
                or not binding.is_frozen
                or binding.frozen_artifact_id != frozen.execution.core_artifact_id
                or binding.frozen_artifact_sha256 != frozen.execution.artifact_payload_sha256
                or binding.context_resolution_digest != frozen.context_resolution_digest
                or binding.resolved_memory_sha256 != frozen.resolved_memory_sha256
            ):
                findings.add(ReceiptFindingV2.ARTIFACT_CONFIG_BINDING_INVALID)

    return BenchmarkExecutionReceiptV2(
        fields=fields,
        finding_codes=tuple(sorted(findings, key=lambda item: item.value)),
        created_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def write_benchmark_receipt_v2(
    inputs: BenchmarkReceiptInputsV2,
    output_path: Path,
) -> BenchmarkExecutionReceiptV2:
    """Write a fresh audit receipt atomically; blocked receipts stay blocked."""

    receipt = recompute_benchmark_receipt_v2(inputs)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(receipt.canonical_bytes())
    temporary.replace(output_path)
    output_path.chmod(0o644)
    return receipt


def verify_receipt_and_issue_authorization_v2(
    inputs: BenchmarkReceiptInputsV2,
    receipt_path: Path,
) -> BenchmarkAuthorizationV2:
    """Recompute every gate and issue a token only for an exact positive receipt."""

    try:
        stored_bytes = receipt_path.read_bytes()
        stored = json.loads(stored_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("benchmark receipt is unavailable or invalid") from exc
    if type(stored) is not dict:
        raise RuntimeError("benchmark receipt is unavailable or invalid")
    current = recompute_benchmark_receipt_v2(inputs)
    current_payload = current.to_payload()
    for key in ("created_at_utc",):
        current_payload.pop(key, None)
        stored.pop(key, None)
    if stored != current_payload:
        raise RuntimeError("benchmark receipt no longer matches recomputed evidence")
    if not current.paid_execution_allowed:
        raise RuntimeError("paid execution is blocked by benchmark receipt findings")
    receipt_sha256 = hashlib.sha256(stored_bytes).hexdigest()
    return _issue_benchmark_authorization_v2(
        receipt_sha256=receipt_sha256,
        evidence_digest=current.evidence_digest,
    )


__all__ = [
    "BenchmarkAuthorizationV2",
    "BenchmarkExecutionReceiptV2",
    "BenchmarkReceiptInputsV2",
    "DISCLAIMER",
    "PRODUCT_DISCLAIMER",
    "RECEIPT_SCHEMA",
    "ReceiptFindingV2",
    "recompute_benchmark_receipt_v2",
    "verify_receipt_and_issue_authorization_v2",
    "write_benchmark_receipt_v2",
]
