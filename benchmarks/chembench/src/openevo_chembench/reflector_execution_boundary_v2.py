"""OS-isolated, zero-tool execution boundary for the v2 Core reflector.

The registered OpenEvo method invokes an executable named ``codex``.  Formal
ChemBench4K execution temporarily prepends a single-use launcher to ``PATH``;
that launcher enters this module and runs the real Codex installation inside a
minimal bubblewrap filesystem.  The model transport network remains shared,
but the repository, benchmark test data, results, user home, and prior
transcripts are not mounted.

Preflight invokes only ``codex --version`` and ``codex debug prompt-input`` in
an isolated empty home to validate the exact config parser policy.  It never
uses ``codex exec``, authenticates, or invokes a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from openevo_chembench.local_codex_executor import (
    _DISABLED_CODEX_FEATURES,
    _MODEL_TRANSPORT_ENV_KEYS,
    LocalCodexExecutionError,
    _close_process_pipes,
    _find_security_tool_use,
    _parse_jsonl_transcript,
    _path_is_inside_any_protected_root,
    _sanitized_model_transport_environment,
    _terminate_invocation_processes,
)
from openevo_chembench.supervised_transfer_v1.memory import (
    CORE_EXPEL_REQUIRED_SECTIONS,
    SUPERVISED_MEMORY_REQUIRED_SECTIONS,
)

PROTOCOL_ID = "chembench4k_frozen_generalization_v2"
TASKWISE_PROTOCOL_ID = "taskwise_online_evolution_v1"
SUPERVISED_TRANSFER_PROTOCOL_ID = "chembench_supervised_transfer_v1"
EXPECTED_RECORDS = 45
MAX_BOUNDARY_RECORDS = 512
TASKWISE_SOURCE_SPLIT = "taskwise_safe_signal"
SUPERVISED_TRAIN_SOURCE_SPLIT = "supervised_train"
_ALLOWED_SOURCE_SPLITS = frozenset(
    {"dev", TASKWISE_SOURCE_SPLIT, SUPERVISED_TRAIN_SOURCE_SPLIT}
)
_PROTOCOL_BY_SOURCE_SPLIT = {
    "dev": PROTOCOL_ID,
    TASKWISE_SOURCE_SPLIT: TASKWISE_PROTOCOL_ID,
    SUPERVISED_TRAIN_SOURCE_SPLIT: SUPERVISED_TRANSFER_PROTOCOL_ID,
}
CONFIG_ENV = "OPENEVO_CHEMBENCH_REFLECTOR_BOUNDARY_CONFIG_V2"
WRAPPER_STATUS_ENV = "OPENEVO_CHEMBENCH_REFLECTOR_WRAPPER_V2"
ISOLATION_FINDING = "REFLECTOR_FILESYSTEM_ISOLATION_MISSING"
TOOL_VIOLATION_STATUS = "REFLECTOR_SECURITY_TOOL_USE_VIOLATION"
_CONFIG_SCHEMA = "chembench4k_reflector_wrapper_config_v2"
_RECEIPT_SCHEMA_V2 = "chembench4k_reflector_execution_receipt_v2"
_RECEIPT_SCHEMA_V3 = "chembench4k_reflector_execution_receipt_v3"
_RECEIPT_SCHEMA_V4 = "chembench4k_reflector_execution_receipt_v4"
_RECEIPT_SCHEMA = "chembench4k_reflector_execution_receipt_v5"
_ROOT_PREFIX = "openevo-chembench-reflector-v2-"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_EVENT_BYTES = 16 * 1024 * 1024
_MAX_LAST_MESSAGE_BYTES = 64 * 1024
_MAX_TRANSPORT_CA_FILE_BYTES = 8 * 1024 * 1024
_MAX_TRANSPORT_CA_DIRECTORY_BYTES = 32 * 1024 * 1024
_MAX_TRANSPORT_CA_DIRECTORY_ENTRIES = 2048
_TRANSPORT_CA_FILE_KEYS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
_TRANSPORT_CA_DIRECTORY_KEY = "SSL_CERT_DIR"
_CODEX_POLICY_PROBE_TIMEOUT_SECONDS = 15.0
_EXPECTED_CODEX_VERSION = "codex-cli 0.145.0"
_WRAPPER_EXIT_INVALID = 80
_WRAPPER_EXIT_CODEX = 81
_WRAPPER_EXIT_TOOL = 86
_WRAPPER_EXIT_CLEANUP = 87
_HELPER_SYSTEM_DIRS = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"))
_SAFE_ETC_PATHS = (
    Path("/etc/ssl"),
    Path("/etc/hosts"),
    Path("/etc/resolv.conf"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/host.conf"),
    Path("/etc/gai.conf"),
    Path("/etc/ld.so.cache"),
    Path("/etc/localtime"),
)
_REFLECTOR_HARDENING_CONFIG = (
    'model_reasoning_effort="medium"',
    'web_search="disabled"',
    'approval_policy="never"',
    'forced_login_method="chatgpt"',
    "check_for_update_on_startup=false",
    "allow_login_shell=false",
    'shell_environment_policy.inherit="none"',
)
_REFLECTOR_POLICY_FINDINGS = frozenset(
    {
        "REFLECTOR_CODEX_CONFIG_AGENTS_ENABLED_FORBIDDEN",
        "REFLECTOR_CODEX_CONFIG_POLICY_INVALID",
        "REFLECTOR_CODEX_CONFIG_PROBE_REJECTED",
        "REFLECTOR_CODEX_CONFIG_PROBE_UNAVAILABLE",
        "REFLECTOR_CODEX_VERSION_MISMATCH",
    }
)
_STDERR_TAIL_CODES = frozenset(
    {
        "CODEX_AUTH_ERROR",
        "CODEX_CONFIG_PARSE_ERROR",
        "CODEX_RATE_LIMIT",
        "CODEX_STDERR_REDACTED",
        "CODEX_TIMEOUT",
        "CODEX_TRANSPORT_ERROR",
    }
)
_TASKWISE_OPERATIONAL_ID_RE = re.compile(
    r"\b(?:job|art|ds)_[A-Za-z0-9_.:-]{6,}\b",
    re.ASCII,
)
_TASKWISE_OPERATIONAL_LINE_RE = re.compile(
    r"^\s*-\s*(?:job_id|dataset_artifact_ids?)\s*:",
    re.IGNORECASE | re.ASCII,
)
_TASKWISE_EXACT_H1 = "# General Chemistry Memory"
_SUPERVISED_SECTION_NORMALIZATION_ID = "supervised_memory_section_merge_v1"
_NO_OUTPUT_NORMALIZATION_ID = "none"
_SUPERVISED_EXACT_SECTIONS = (
    *SUPERVISED_MEMORY_REQUIRED_SECTIONS,
    *CORE_EXPEL_REQUIRED_SECTIONS,
)
_H2_LINE_RE = re.compile(r"^##\s+(.+?)\s*$", re.ASCII)
_TASKWISE_PROMPT_CONTRACT = (
    "Taskwise benchmark output requirements:\n"
    f"- The first non-empty line must be exactly `{_TASKWISE_EXACT_H1}`.\n"
    "- Preserve every required level-2 memory section and merge or retire "
    "superseded rules instead of growing memory without bound."
)
_SUPERVISED_PROMPT_CONTRACT = (
    "Supervised category-memory output requirements:\n"
    "- Reconstruct the ordered PACKET_PART records and use only that Train packet "
    "plus the supplied existing memory.\n"
    "- Return one complete replacement memory, never an append-only patch or a copy "
    "followed by revised sections.\n"
    "- The first non-empty line must be `# Category Memory: <packet category>`.\n"
    "- Use these exact level-2 headings in order: Confirmed Principles; "
    "Provisional Principles; Common Failure Modes; Option Elimination Checks; "
    "Retired Or Contradicted; Output Discipline; Do; Avoid; Validate; When Applicable; "
    "Retired Or Superseded.\n"
    "- Every principle rule is one bullet with Rule ID, Status, Category, Trigger, "
    "Principle, Action, Validation, Evidence Count, and Evidence digest fields.\n"
    "- A provisional rule has Evidence Count 1. A confirmed rule requires at least "
    "two independent training-item evidence digests. Never copy a question, option, "
    "answer mapping, UID, ordinal, or path.\n"
    "- Before returning, compare the draft against every packet question and option. "
    "Paraphrase any shared contiguous span of four or more complete tokens and 32 or "
    "more characters; retain only the abstract chemistry principle.\n"
    "- Put `- None.` in a section with no current entries. Keep every compatibility "
    "section non-empty and do not use additional level-2 headings.\n"
    "- Emit every listed level-2 heading exactly once. Before returning, count the "
    "headings: there must be exactly eleven, with no duplicate heading."
)

ReflectorCodexPolicyProbeRunnerV2 = Callable[
    [Sequence[str], Path, Mapping[str, str], float],
    tuple[int, str],
]


class ReflectorBoundaryStatusV2(str, Enum):
    """Closed wrapper result vocabulary."""

    COMPLETED = "COMPLETED"
    CODEX_FAILED = "CODEX_FAILED"
    INVALID_INVOCATION = "INVALID_INVOCATION"
    SECURITY_TOOL_USE_VIOLATION = TOOL_VIOLATION_STATUS
    CLEANUP_FAILED = "REFLECTOR_CLEANUP_FAILED"


class ReflectorBoundaryError(RuntimeError):
    """Sanitized boundary failure that contains no event or benchmark payload."""

    def __init__(self, finding_code: str) -> None:
        if not isinstance(finding_code, str) or not finding_code:
            raise TypeError("finding_code must be non-empty text")
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class ReflectorIsolationCapabilityV2:
    available: bool
    mechanism: str
    finding_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReflectorExecutionReceiptV2:
    invocation_id: str
    status: ReflectorBoundaryStatusV2
    mechanism: str
    wrapper_invoked: bool
    real_codex_sha256: str
    dev_artifact_sha256: str
    ordered_records_sha256: str
    record_count: int
    event_stream_sha256: str
    event_counts: tuple[tuple[str, int], ...]
    private_event_reference: str
    last_message_sha256: str | None
    cleanup_complete: bool
    retry_allowed: bool
    resume_allowed: bool
    replacement_completion_allowed: bool
    codex_returncode: int | None = None
    stderr_sha256: str = hashlib.sha256(b"").hexdigest()
    stderr_tail_codes: tuple[str, ...] = ()
    protocol_id: str = PROTOCOL_ID
    source_split: str = "dev"
    projected_prompt_sha256: str | None = None
    source_last_message_sha256: str | None = None
    output_normalization_id: str = _NO_OUTPUT_NORMALIZATION_ID
    output_normalization_applied: bool = False

    @property
    def digest(self) -> str:
        return _canonical_sha256(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        source_last_message_sha256 = self.source_last_message_sha256
        if source_last_message_sha256 is None and not self.output_normalization_applied:
            source_last_message_sha256 = self.last_message_sha256
        payload = {
            "schema_version": (
                _RECEIPT_SCHEMA
                if self.source_split == SUPERVISED_TRAIN_SOURCE_SPLIT
                else _RECEIPT_SCHEMA_V4
            ),
            "protocol_id": self.protocol_id,
            "invocation_id": self.invocation_id,
            "status": self.status.value,
            "mechanism": self.mechanism,
            "wrapper_invoked": self.wrapper_invoked,
            "real_codex_sha256": self.real_codex_sha256,
            "dev_artifact_sha256": self.dev_artifact_sha256,
            "ordered_records_sha256": self.ordered_records_sha256,
            "record_count": self.record_count,
            "event_stream_sha256": self.event_stream_sha256,
            "event_counts": dict(self.event_counts),
            "private_event_reference": self.private_event_reference,
            "last_message_sha256": self.last_message_sha256,
            "cleanup_complete": self.cleanup_complete,
            "retry_allowed": self.retry_allowed,
            "resume_allowed": self.resume_allowed,
            "replacement_completion_allowed": self.replacement_completion_allowed,
            "codex_returncode": self.codex_returncode,
            "stderr_sha256": self.stderr_sha256,
            "stderr_tail_codes": list(self.stderr_tail_codes),
            "source_split": self.source_split,
            "projected_prompt_sha256": self.projected_prompt_sha256,
        }
        if self.source_split == SUPERVISED_TRAIN_SOURCE_SPLIT:
            payload.update(
                {
                    "source_last_message_sha256": source_last_message_sha256,
                    "output_normalization_id": self.output_normalization_id,
                    "output_normalization_applied": self.output_normalization_applied,
                }
            )
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReflectorExecutionReceiptV2:
        legacy_expected = {
            "schema_version",
            "protocol_id",
            "invocation_id",
            "status",
            "mechanism",
            "wrapper_invoked",
            "real_codex_sha256",
            "dev_artifact_sha256",
            "ordered_records_sha256",
            "record_count",
            "event_stream_sha256",
            "event_counts",
            "private_event_reference",
            "last_message_sha256",
            "cleanup_complete",
            "retry_allowed",
            "resume_allowed",
            "replacement_completion_allowed",
        }
        v3_expected = legacy_expected | {
            "codex_returncode",
            "source_split",
            "stderr_sha256",
            "stderr_tail_codes",
        }
        v4_expected = v3_expected | {"projected_prompt_sha256"}
        current_expected = v4_expected | {
            "source_last_message_sha256",
            "output_normalization_id",
            "output_normalization_applied",
        }
        schema_version = payload.get("schema_version")
        if not (
            (schema_version == _RECEIPT_SCHEMA_V2 and set(payload) == legacy_expected)
            or (schema_version == _RECEIPT_SCHEMA_V3 and set(payload) == v3_expected)
            or (schema_version == _RECEIPT_SCHEMA_V4 and set(payload) == v4_expected)
            or (schema_version == _RECEIPT_SCHEMA and set(payload) == current_expected)
        ):
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        if (
            payload["protocol_id"] not in _PROTOCOL_BY_SOURCE_SPLIT.values()
            or payload["mechanism"] != "bubblewrap"
            or type(payload["wrapper_invoked"]) is not bool
            or type(payload["cleanup_complete"]) is not bool
            or type(payload["retry_allowed"]) is not bool
            or type(payload["resume_allowed"]) is not bool
            or type(payload["replacement_completion_allowed"]) is not bool
            or type(payload["record_count"]) is not int
            or type(payload["event_counts"]) is not dict
            or payload["retry_allowed"]
            or payload["resume_allowed"]
            or payload["replacement_completion_allowed"]
        ):
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        digests = (
            payload["real_codex_sha256"],
            payload["dev_artifact_sha256"],
            payload["ordered_records_sha256"],
            payload["event_stream_sha256"],
        )
        if any(type(value) is not str or _SHA256_RE.fullmatch(value) is None for value in digests):
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        last_digest = payload["last_message_sha256"]
        if last_digest is not None and (
            type(last_digest) is not str or _SHA256_RE.fullmatch(last_digest) is None
        ):
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        if schema_version in {
            _RECEIPT_SCHEMA_V3,
            _RECEIPT_SCHEMA_V4,
            _RECEIPT_SCHEMA,
        }:
            codex_returncode = payload["codex_returncode"]
            source_split = payload["source_split"]
            stderr_sha256 = payload["stderr_sha256"]
            stderr_tail_codes = payload["stderr_tail_codes"]
            if (
                (
                    codex_returncode is not None
                    and (
                        isinstance(codex_returncode, bool) or not isinstance(codex_returncode, int)
                    )
                )
                or source_split not in _ALLOWED_SOURCE_SPLITS
                or payload["protocol_id"] != _PROTOCOL_BY_SOURCE_SPLIT[source_split]
                or type(stderr_sha256) is not str
                or _SHA256_RE.fullmatch(stderr_sha256) is None
                or type(stderr_tail_codes) is not list
                or len(stderr_tail_codes) > 4
                or any(
                    type(code) is not str or code not in _STDERR_TAIL_CODES
                    for code in stderr_tail_codes
                )
            ):
                raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        else:
            codex_returncode = None
            source_split = "dev"
            stderr_sha256 = hashlib.sha256(b"").hexdigest()
            stderr_tail_codes = []
        projected_prompt_sha256 = (
            payload["projected_prompt_sha256"]
            if schema_version in {_RECEIPT_SCHEMA_V4, _RECEIPT_SCHEMA}
            else None
        )
        if projected_prompt_sha256 is not None and (
            type(projected_prompt_sha256) is not str
            or _SHA256_RE.fullmatch(projected_prompt_sha256) is None
        ):
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        if schema_version == _RECEIPT_SCHEMA:
            source_last_digest = payload["source_last_message_sha256"]
            normalization_id = payload["output_normalization_id"]
            normalization_applied = payload["output_normalization_applied"]
            if (
                (
                    source_last_digest is not None
                    and (
                        type(source_last_digest) is not str
                        or _SHA256_RE.fullmatch(source_last_digest) is None
                    )
                )
                or normalization_id
                not in {
                    _NO_OUTPUT_NORMALIZATION_ID,
                    _SUPERVISED_SECTION_NORMALIZATION_ID,
                }
                or type(normalization_applied) is not bool
                or (
                    normalization_id == _NO_OUTPUT_NORMALIZATION_ID
                    and normalization_applied
                )
                or (
                    normalization_id == _SUPERVISED_SECTION_NORMALIZATION_ID
                    and source_split != SUPERVISED_TRAIN_SOURCE_SPLIT
                )
                or (source_last_digest is None) != (last_digest is None)
                or (
                    not normalization_applied
                    and source_last_digest != last_digest
                )
            ):
                raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        else:
            source_last_digest = None
            normalization_id = _NO_OUTPUT_NORMALIZATION_ID
            normalization_applied = False
        counts: list[tuple[str, int]] = []
        for key, value in payload["event_counts"].items():
            if type(key) is not str or type(value) is not int or value <= 0:
                raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
            counts.append((key, value))
        try:
            status = ReflectorBoundaryStatusV2(payload["status"])
        except (TypeError, ValueError) as exc:
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID") from exc
        return cls(
            invocation_id=str(payload["invocation_id"]),
            status=status,
            mechanism=str(payload["mechanism"]),
            wrapper_invoked=payload["wrapper_invoked"],
            real_codex_sha256=str(payload["real_codex_sha256"]),
            dev_artifact_sha256=str(payload["dev_artifact_sha256"]),
            ordered_records_sha256=str(payload["ordered_records_sha256"]),
            record_count=payload["record_count"],
            event_stream_sha256=str(payload["event_stream_sha256"]),
            event_counts=tuple(sorted(counts)),
            private_event_reference=str(payload["private_event_reference"]),
            last_message_sha256=last_digest,
            cleanup_complete=payload["cleanup_complete"],
            retry_allowed=payload["retry_allowed"],
            resume_allowed=payload["resume_allowed"],
            replacement_completion_allowed=payload["replacement_completion_allowed"],
            codex_returncode=codex_returncode,
            stderr_sha256=stderr_sha256,
            stderr_tail_codes=tuple(stderr_tail_codes),
            protocol_id=str(payload["protocol_id"]),
            source_split=source_split,
            projected_prompt_sha256=projected_prompt_sha256,
            source_last_message_sha256=source_last_digest,
            output_normalization_id=normalization_id,
            output_normalization_applied=normalization_applied,
        )


@dataclass(frozen=True, slots=True)
class ReflectorBoundaryActivationV2:
    invocation_id: str
    wrapper_path: Path
    receipt_path: Path
    expected_records_sha256: str
    expected_record_count: int
    expected_real_codex_sha256: str
    expected_protocol_id: str
    expected_source_split: str

    def require_receipt(self) -> ReflectorExecutionReceiptV2:
        """Require a successful, cleaned, zero-tool wrapper invocation."""

        receipt = self.load_receipt_for_audit()
        if (
            receipt.status is not ReflectorBoundaryStatusV2.COMPLETED
            or not receipt.cleanup_complete
            or receipt.event_counts
            or receipt.last_message_sha256 is None
        ):
            raise ReflectorBoundaryError(receipt.status.value)
        return receipt

    def load_receipt_for_audit(self) -> ReflectorExecutionReceiptV2:
        """Load a bound failure receipt without accepting it as successful."""

        try:
            metadata = self.receipt_path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OSError
            payload = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReflectorBoundaryError("REFLECTOR_WRAPPER_RECEIPT_MISSING") from exc
        if not isinstance(payload, dict):
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        receipt = ReflectorExecutionReceiptV2.from_payload(payload)
        expected_private_reference = f"{self.invocation_id}/events.jsonl"
        if (
            receipt.invocation_id != self.invocation_id
            or receipt.record_count != self.expected_record_count
            or receipt.ordered_records_sha256 != self.expected_records_sha256
            or receipt.real_codex_sha256 != self.expected_real_codex_sha256
            or receipt.protocol_id != self.expected_protocol_id
            or receipt.source_split != self.expected_source_split
            or (
                receipt.status is ReflectorBoundaryStatusV2.COMPLETED
                and receipt.projected_prompt_sha256 is None
            )
            or not receipt.wrapper_invoked
            or receipt.private_event_reference != expected_private_reference
        ):
            raise ReflectorBoundaryError("REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID")
        event_path = self.receipt_path.parent / "events.jsonl"
        try:
            event_metadata = event_path.lstat()
            if (
                stat.S_ISLNK(event_metadata.st_mode)
                or not stat.S_ISREG(event_metadata.st_mode)
                or stat.S_IMODE(event_metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and event_metadata.st_uid != os.getuid())
                or event_metadata.st_size > _MAX_EVENT_BYTES
            ):
                raise OSError
            event_bytes = event_path.read_bytes()
        except OSError as exc:
            raise ReflectorBoundaryError("REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID") from exc
        if hashlib.sha256(event_bytes).hexdigest() != receipt.event_stream_sha256:
            raise ReflectorBoundaryError("REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID")
        if receipt.status is ReflectorBoundaryStatusV2.COMPLETED:
            if receipt.codex_returncode != 0 or not event_bytes:
                raise ReflectorBoundaryError("REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID")
            try:
                event_response, _usage, _event_digest = _parse_jsonl_transcript(
                    event_bytes.decode("utf-8")
                )
            except (UnicodeError, LocalCodexExecutionError) as exc:
                raise ReflectorBoundaryError("REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID") from exc
            if receipt.source_split == SUPERVISED_TRAIN_SOURCE_SPLIT:
                source_message = event_response.strip() + "\n"
                normalized_message, normalization_applied = (
                    _normalize_supervised_memory_sections(source_message)
                )
                if (
                    receipt.output_normalization_id
                    != _SUPERVISED_SECTION_NORMALIZATION_ID
                    or receipt.source_last_message_sha256
                    != hashlib.sha256(source_message.encode("utf-8")).hexdigest()
                    or receipt.last_message_sha256
                    != hashlib.sha256(normalized_message.encode("utf-8")).hexdigest()
                    or receipt.output_normalization_applied != normalization_applied
                ):
                    raise ReflectorBoundaryError("REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID")
        return receipt


class ReflectorExecutionBoundaryV2:
    """Prepare one single-use PATH interception and bubblewrap invocation."""

    def __init__(
        self,
        *,
        dev_artifact_path: str | Path,
        expected_records_sha256: str,
        expected_record_count: int = EXPECTED_RECORDS,
        expected_source_split: str = "dev",
        private_audit_root: str | Path,
        real_codex_binary: str | Path | None = None,
        auth_source: str | Path | None = None,
        bwrap_binary: str | Path | None = None,
        timeout_seconds: float = 900.0,
        temporary_parent: str | Path | None = None,
        config_probe_runner: ReflectorCodexPolicyProbeRunnerV2 | None = None,
    ) -> None:
        self.dev_artifact_path = Path(dev_artifact_path).resolve()
        self.expected_records_sha256 = expected_records_sha256
        self.expected_record_count = expected_record_count
        self.expected_source_split = expected_source_split
        self.private_audit_root = Path(private_audit_root).resolve()
        resolved_codex = real_codex_binary or shutil.which("codex")
        resolved_bwrap = bwrap_binary or shutil.which("bwrap")
        if not resolved_codex:
            raise ReflectorBoundaryError("REFLECTOR_CODEX_BINARY_MISSING")
        if not resolved_bwrap:
            raise ReflectorBoundaryError(ISOLATION_FINDING)
        self.real_codex_binary = _resolve_native_codex_binary(Path(resolved_codex).resolve())
        self.bwrap_binary = Path(resolved_bwrap).resolve()
        configured_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.auth_source = Path(auth_source or configured_home / "auth.json").resolve()
        self.timeout_seconds = timeout_seconds
        self.temporary_parent = (
            None if temporary_parent is None else Path(temporary_parent).resolve()
        )
        self.config_probe_runner = config_probe_runner
        if _SHA256_RE.fullmatch(expected_records_sha256) is None:
            raise ValueError("expected_records_sha256 must be a lowercase SHA-256")
        if (
            isinstance(expected_record_count, bool)
            or not isinstance(expected_record_count, int)
            or not 1 <= expected_record_count <= MAX_BOUNDARY_RECORDS
        ):
            raise ValueError(
                f"expected_record_count must be between 1 and {MAX_BOUNDARY_RECORDS}"
            )
        if expected_source_split not in _ALLOWED_SOURCE_SPLITS:
            raise ValueError("expected_source_split is outside the closed allowlist")
        if not 0 < timeout_seconds <= 86_400:
            raise ValueError("timeout_seconds must be positive and bounded")

    @staticmethod
    def detect_capability(
        bwrap_binary: str | Path | None = None,
    ) -> ReflectorIsolationCapabilityV2:
        candidate = bwrap_binary or shutil.which("bwrap")
        if not candidate:
            return ReflectorIsolationCapabilityV2(
                available=False,
                mechanism="none",
                finding_codes=(ISOLATION_FINDING,),
            )
        command = [
            os.fspath(Path(candidate).resolve()),
            "--unshare-all",
            "--share-net",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "/usr/bin/true",
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        available = completed is not None and completed.returncode == 0
        return ReflectorIsolationCapabilityV2(
            available=available,
            mechanism="bubblewrap" if available else "none",
            finding_codes=() if available else (ISOLATION_FINDING,),
        )

    def preflight(self) -> ReflectorIsolationCapabilityV2:
        capability = self.detect_capability(self.bwrap_binary)
        if not capability.available:
            return capability
        _require_owned_regular_file(self.real_codex_binary, executable=True)
        _require_owned_regular_file(self.auth_source, exact_mode=0o600)
        records, digest = _read_and_validate_records(
            self.dev_artifact_path,
            expected_record_count=self.expected_record_count,
            expected_source_split=self.expected_source_split,
        )
        if len(records) != self.expected_record_count or digest != self.expected_records_sha256:
            raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_BINDING_INVALID")
        verify_reflector_codex_policy_v2(
            self.real_codex_binary,
            probe_runner=self.config_probe_runner,
        )
        return capability

    @contextmanager
    def activate(self) -> Iterator[ReflectorBoundaryActivationV2]:
        capability = self.preflight()
        if not capability.available:
            raise ReflectorBoundaryError(ISOLATION_FINDING)
        self.private_audit_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.private_audit_root.chmod(0o700)
        invocation_id = uuid.uuid4().hex
        audit_directory = self.private_audit_root / invocation_id
        audit_directory.mkdir(mode=0o700)
        receipt_path = audit_directory / "receipt.json"
        real_codex_sha256 = _sha256_file(self.real_codex_binary)
        dev_artifact_sha256 = _sha256_file(self.dev_artifact_path)
        parent = os.fspath(self.temporary_parent) if self.temporary_parent is not None else None
        with tempfile.TemporaryDirectory(
            prefix="openevo-chembench-reflector-launcher-v2-",
            dir=parent,
        ) as temporary:
            temporary_path = Path(temporary)
            temporary_path.chmod(0o700)
            wrapper_path = temporary_path / "codex"
            config_path = temporary_path / "boundary-config.json"
            launcher = (
                f"#!{sys.executable}\n"
                "from openevo_chembench.reflector_execution_boundary_v2 "
                "import reflector_codex_wrapper_main\n"
                "raise SystemExit(reflector_codex_wrapper_main())\n"
            )
            _exclusive_write(wrapper_path, launcher.encode("utf-8"), mode=0o700)
            config = {
                "schema_version": _CONFIG_SCHEMA,
                "protocol_id": _PROTOCOL_BY_SOURCE_SPLIT[self.expected_source_split],
                "invocation_id": invocation_id,
                "bwrap_binary": os.fspath(self.bwrap_binary),
                "real_codex_binary": os.fspath(self.real_codex_binary),
                "real_codex_sha256": real_codex_sha256,
                "auth_source": os.fspath(self.auth_source),
                "dev_artifact_source": os.fspath(self.dev_artifact_path),
                "dev_artifact_sha256": dev_artifact_sha256,
                "ordered_records_sha256": self.expected_records_sha256,
                "record_count": self.expected_record_count,
                "source_split": self.expected_source_split,
                "private_audit_directory": os.fspath(audit_directory),
                "receipt_path": os.fspath(receipt_path),
                "timeout_seconds": self.timeout_seconds,
                "temporary_parent": parent,
            }
            _exclusive_write(
                config_path,
                (_canonical_json(config) + "\n").encode("utf-8"),
                mode=0o600,
            )
            original_path = os.environ.get("PATH")
            original_config = os.environ.get(CONFIG_ENV)
            original_marker = os.environ.get(WRAPPER_STATUS_ENV)
            runtime_bin = os.fspath(Path(sys.executable).resolve().parent)
            os.environ["PATH"] = os.pathsep.join(
                (os.fspath(temporary_path), runtime_bin, original_path or "")
            )
            os.environ[CONFIG_ENV] = os.fspath(config_path)
            os.environ[WRAPPER_STATUS_ENV] = invocation_id
            activation = ReflectorBoundaryActivationV2(
                invocation_id=invocation_id,
                wrapper_path=wrapper_path,
                receipt_path=receipt_path,
                expected_records_sha256=self.expected_records_sha256,
                expected_record_count=self.expected_record_count,
                expected_real_codex_sha256=real_codex_sha256,
                expected_protocol_id=_PROTOCOL_BY_SOURCE_SPLIT[self.expected_source_split],
                expected_source_split=self.expected_source_split,
            )
            try:
                yield activation
            finally:
                _restore_environment("PATH", original_path)
                _restore_environment(CONFIG_ENV, original_config)
                _restore_environment(WRAPPER_STATUS_ENV, original_marker)

    def probe_visibility(
        self,
        *,
        denied_paths: Sequence[str | Path],
    ) -> dict[str, bool]:
        """Run a no-model helper in the same filesystem mount policy."""

        capability = self.preflight()
        if not capability.available:
            raise ReflectorBoundaryError(ISOLATION_FINDING)
        with tempfile.TemporaryDirectory(prefix=_ROOT_PREFIX) as temporary:
            layout = _create_layout(Path(temporary))
            _copy_private_file(self.auth_source, layout["codex_home"] / "auth.json", mode=0o600)
            _copy_private_file(
                self.dev_artifact_path,
                layout["inputs"] / "dev_loo_dataset.jsonl",
                mode=0o400,
            )
            script = (
                "test -r /inputs/dev_loo_dataset.jsonl || exit 10; "
                'for candidate in "$@"; do '
                'if test -e "$candidate"; then exit 20; fi; '
                "done"
            )
            command = _bubblewrap_base_command(
                bwrap_binary=self.bwrap_binary,
                layout=layout,
                executable_source=Path("/bin/sh"),
                executable_command=("/bin/sh", "-c", script, "probe"),
                include_codex=False,
            )
            command.extend(os.fspath(Path(path).resolve()) for path in denied_paths)
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=20,
                check=False,
            )
            return {
                "allowed_dev_artifact_readable": completed.returncode != 10,
                "all_denied_paths_invisible": completed.returncode == 0,
                "probe_passed": completed.returncode == 0,
            }


def write_reflector_dev_artifact_v2(
    records: Sequence[Any],
    *,
    destination: str | Path,
    expected_records_sha256: str,
) -> Path:
    """Write the exact 45 ordered private records for one isolated invocation."""

    if len(records) != EXPECTED_RECORDS:
        raise ValueError("reflector input must contain exactly 45 records")
    payloads: list[dict[str, Any]] = []
    for record in records:
        model_dump = getattr(record, "model_dump", None)
        payload = (
            model_dump(mode="json")
            if callable(model_dump)
            else dict(record)
            if isinstance(record, Mapping)
            else None
        )
        if not isinstance(payload, dict) or payload.get("source_split") != "dev":
            raise TypeError("reflector records must be private dev record objects")
        payloads.append(payload)
    if _canonical_sha256(payloads) != expected_records_sha256:
        raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_BINDING_INVALID")
    path = Path(destination)
    if not path.is_absolute():
        raise ValueError("reflector dev artifact destination must be absolute")
    encoded = "".join(_canonical_json(payload) + "\n" for payload in payloads).encode("utf-8")
    _exclusive_write(path, encoded, mode=0o600)
    parsed, parsed_digest = _read_and_validate_records(path)
    if len(parsed) != EXPECTED_RECORDS or parsed_digest != expected_records_sha256:
        path.unlink(missing_ok=True)
        raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_BINDING_INVALID")
    return path


def _classify_reflector_event_stream(event_stream: str) -> dict[str, int]:
    """Apply the shared zero-tool classifier plus strict JSONL framing."""

    malformed = 0
    for raw_line in event_stream.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError):
            malformed += 1
            continue
        if not isinstance(event, Mapping) or type(event.get("type")) is not str:
            malformed += 1
    violation = _find_security_tool_use(event_stream)
    counts = {} if violation is None else dict(violation.event_counts)
    if malformed:
        counts["unknown_tool"] = counts.get("unknown_tool", 0) + malformed
    return dict(sorted(counts.items()))


def reflector_codex_wrapper_main(argv: Sequence[str] | None = None) -> int:
    """Entry point used only by the single-use PATH launcher."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    config_path_value = os.environ.pop(CONFIG_ENV, "")
    invocation_marker = os.environ.pop(WRAPPER_STATUS_ENV, "")
    if not config_path_value or not invocation_marker:
        return _WRAPPER_EXIT_INVALID
    try:
        config = _load_wrapper_config(Path(config_path_value))
        if config["invocation_id"] != invocation_marker:
            raise ReflectorBoundaryError("REFLECTOR_WRAPPER_CONFIG_INVALID")
        return _run_wrapper(arguments, config)
    except ReflectorBoundaryError as exc:
        print(exc.finding_code, file=sys.stderr)
        return (
            _WRAPPER_EXIT_TOOL
            if exc.finding_code == TOOL_VIOLATION_STATUS
            else _WRAPPER_EXIT_INVALID
        )


def _run_wrapper(arguments: list[str], config: dict[str, Any]) -> int:
    host_output, rewritten = _validate_and_rewrite_upstream_arguments(arguments)
    host_output.unlink(missing_ok=True)
    invocation_root = Path(tempfile.mkdtemp(prefix=_ROOT_PREFIX, dir=config["temporary_parent"]))
    invocation_root.chmod(0o700)
    layout = _create_layout(invocation_root)
    status = ReflectorBoundaryStatusV2.INVALID_INVOCATION
    event_stream = ""
    event_counts: dict[str, int] = {}
    codex_returncode: int | None = None
    stderr = ""
    last_message_sha256: str | None = None
    source_last_message_sha256: str | None = None
    projected_prompt_sha256: str | None = None
    output_normalization_id = _NO_OUTPUT_NORMALIZATION_ID
    output_normalization_applied = False
    return_code = _WRAPPER_EXIT_INVALID
    try:
        _copy_private_file(
            Path(config["auth_source"]),
            layout["codex_home"] / "auth.json",
            mode=0o600,
        )
        _copy_private_file(
            Path(config["dev_artifact_source"]),
            layout["inputs"] / "dev_loo_dataset.jsonl",
            mode=0o400,
        )
        records, records_digest = _read_and_validate_records(
            layout["inputs"] / "dev_loo_dataset.jsonl",
            expected_record_count=int(config["record_count"]),
            expected_source_split=str(config["source_split"]),
        )
        if (
            len(records) != config["record_count"]
            or records_digest != config["ordered_records_sha256"]
            or _sha256_file(layout["inputs"] / "dev_loo_dataset.jsonl")
            != config["dev_artifact_sha256"]
        ):
            raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_BINDING_INVALID")
        rewritten = _replace_upstream_paths(rewritten)
        transport_environment = _materialize_reflector_transport_environment(layout)
        command = _bubblewrap_base_command(
            bwrap_binary=Path(config["bwrap_binary"]),
            layout=layout,
            executable_source=Path(config["real_codex_binary"]),
            executable_command=_sandboxed_codex_command(
                Path(config["real_codex_binary"]),
                rewritten,
            ),
            include_codex=True,
        )
        prompt = _project_reflector_prompt(
            sys.stdin.read(),
            source_split=str(config["source_split"]),
        )
        projected_prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        completed = _run_process_group(
            command,
            input_text=prompt,
            timeout_seconds=float(config["timeout_seconds"]),
            environment=transport_environment,
        )
        codex_returncode = completed.returncode
        event_stream = completed.stdout
        stderr = completed.stderr
        if len(event_stream.encode("utf-8", errors="replace")) > _MAX_EVENT_BYTES:
            event_counts = {"unknown_tool": 1}
        else:
            event_counts = _classify_reflector_event_stream(event_stream)
        if event_counts:
            status = ReflectorBoundaryStatusV2.SECURITY_TOOL_USE_VIOLATION
            return_code = _WRAPPER_EXIT_TOOL
            host_output.unlink(missing_ok=True)
        elif completed.returncode != 0:
            status = ReflectorBoundaryStatusV2.CODEX_FAILED
            return_code = _WRAPPER_EXIT_CODEX
            host_output.unlink(missing_ok=True)
        else:
            try:
                event_response, _usage, _event_digest = _parse_jsonl_transcript(event_stream)
            except LocalCodexExecutionError as exc:
                raise ReflectorBoundaryError("REFLECTOR_EVENT_STREAM_INVALID") from exc
            isolated_output = layout["output"] / "last-message.md"
            content = _read_bounded_regular_file(
                isolated_output,
                maximum=_MAX_LAST_MESSAGE_BYTES,
            )
            if not content.strip():
                raise ReflectorBoundaryError("REFLECTOR_LAST_MESSAGE_MISSING")
            try:
                output_text = content.decode("utf-8")
            except UnicodeError as exc:
                raise ReflectorBoundaryError("REFLECTOR_LAST_MESSAGE_INVALID") from exc
            if output_text.strip() != event_response.strip():
                raise ReflectorBoundaryError("REFLECTOR_LAST_MESSAGE_INVALID")
            if config["source_split"] == SUPERVISED_TRAIN_SOURCE_SPLIT:
                output_text = output_text.strip() + "\n"
                source_last_message_sha256 = hashlib.sha256(
                    output_text.encode("utf-8")
                ).hexdigest()
                output_text, output_normalization_applied = (
                    _normalize_supervised_memory_sections(output_text)
                )
                output_normalization_id = _SUPERVISED_SECTION_NORMALIZATION_ID
                content = output_text.encode("utf-8")
            else:
                source_last_message_sha256 = hashlib.sha256(content).hexdigest()
            _exclusive_write(host_output, content, mode=0o600)
            last_message_sha256 = hashlib.sha256(content).hexdigest()
            status = ReflectorBoundaryStatusV2.COMPLETED
            return_code = 0
            sys.stdout.write(event_stream)
            sys.stdout.flush()
    except ReflectorBoundaryError:
        host_output.unlink(missing_ok=True)
        raise
    finally:
        event_digest = hashlib.sha256(event_stream.encode("utf-8")).hexdigest()
        audit_directory = Path(config["private_audit_directory"])
        audit_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        audit_directory.chmod(0o700)
        event_path = audit_directory / "events.jsonl"
        if not event_path.exists():
            _exclusive_write(
                event_path,
                event_stream.encode("utf-8", errors="replace"),
                mode=0o600,
            )
        cleanup_complete = _cleanup_invocation_root(invocation_root)
        if not cleanup_complete:
            status = ReflectorBoundaryStatusV2.CLEANUP_FAILED
            return_code = _WRAPPER_EXIT_CLEANUP
            host_output.unlink(missing_ok=True)
        receipt = ReflectorExecutionReceiptV2(
            invocation_id=str(config["invocation_id"]),
            status=status,
            mechanism="bubblewrap",
            wrapper_invoked=True,
            real_codex_sha256=str(config["real_codex_sha256"]),
            dev_artifact_sha256=str(config["dev_artifact_sha256"]),
            ordered_records_sha256=str(config["ordered_records_sha256"]),
            record_count=int(config["record_count"]),
            event_stream_sha256=event_digest,
            event_counts=tuple(sorted(event_counts.items())),
            private_event_reference=f"{config['invocation_id']}/events.jsonl",
            last_message_sha256=last_message_sha256,
            cleanup_complete=cleanup_complete,
            retry_allowed=False,
            resume_allowed=False,
            replacement_completion_allowed=False,
            codex_returncode=codex_returncode,
            stderr_sha256=hashlib.sha256(stderr.encode("utf-8", errors="replace")).hexdigest(),
            stderr_tail_codes=_stderr_tail_codes(stderr),
            protocol_id=str(config["protocol_id"]),
            source_split=str(config["source_split"]),
            projected_prompt_sha256=projected_prompt_sha256,
            source_last_message_sha256=source_last_message_sha256,
            output_normalization_id=output_normalization_id,
            output_normalization_applied=output_normalization_applied,
        )
        _exclusive_write(
            Path(config["receipt_path"]),
            (_canonical_json(receipt.to_payload()) + "\n").encode("utf-8"),
            mode=0o600,
        )
    if status is ReflectorBoundaryStatusV2.SECURITY_TOOL_USE_VIOLATION:
        print(TOOL_VIOLATION_STATUS, file=sys.stderr)
    elif status is not ReflectorBoundaryStatusV2.COMPLETED:
        print(status.value, file=sys.stderr)
    return return_code


def _load_wrapper_config(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise OSError
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReflectorBoundaryError("REFLECTOR_WRAPPER_CONFIG_INVALID") from exc
    expected = {
        "schema_version",
        "protocol_id",
        "invocation_id",
        "bwrap_binary",
        "real_codex_binary",
        "real_codex_sha256",
        "auth_source",
        "dev_artifact_source",
        "dev_artifact_sha256",
        "ordered_records_sha256",
        "record_count",
        "source_split",
        "private_audit_directory",
        "receipt_path",
        "timeout_seconds",
        "temporary_parent",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload["schema_version"] != _CONFIG_SCHEMA
        or payload["protocol_id"] not in _PROTOCOL_BY_SOURCE_SPLIT.values()
        or isinstance(payload["record_count"], bool)
        or not isinstance(payload["record_count"], int)
        or not 1 <= payload["record_count"] <= MAX_BOUNDARY_RECORDS
        or payload["source_split"] not in _ALLOWED_SOURCE_SPLITS
        or payload["protocol_id"] != _PROTOCOL_BY_SOURCE_SPLIT[payload["source_split"]]
    ):
        raise ReflectorBoundaryError("REFLECTOR_WRAPPER_CONFIG_INVALID")
    for key in ("real_codex_sha256", "dev_artifact_sha256", "ordered_records_sha256"):
        if type(payload[key]) is not str or _SHA256_RE.fullmatch(payload[key]) is None:
            raise ReflectorBoundaryError("REFLECTOR_WRAPPER_CONFIG_INVALID")
    for key in (
        "bwrap_binary",
        "real_codex_binary",
        "auth_source",
        "dev_artifact_source",
        "private_audit_directory",
        "receipt_path",
    ):
        if type(payload[key]) is not str or not Path(payload[key]).is_absolute():
            raise ReflectorBoundaryError("REFLECTOR_WRAPPER_CONFIG_INVALID")
    if payload["temporary_parent"] is not None and (
        type(payload["temporary_parent"]) is not str
        or not Path(payload["temporary_parent"]).is_absolute()
    ):
        raise ReflectorBoundaryError("REFLECTOR_WRAPPER_CONFIG_INVALID")
    if _sha256_file(Path(payload["real_codex_binary"])) != payload["real_codex_sha256"]:
        raise ReflectorBoundaryError("REFLECTOR_CODEX_BINARY_DRIFT")
    return payload


def _validate_and_rewrite_upstream_arguments(
    arguments: list[str],
) -> tuple[Path, list[str]]:
    if any(type(value) is not str for value in arguments):
        raise ReflectorBoundaryError("REFLECTOR_UPSTREAM_ARGUMENTS_INVALID")
    if (
        len(arguments) != 16
        or arguments[:10]
        != [
            "exec",
            "--json",
            "--ignore-user-config",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--disable",
            "shell_tool",
            "--skip-git-repo-check",
            "--cd",
        ]
        or arguments[11] != "--output-last-message"
        or arguments[13:17] != ["--model", "gpt-5.5", "-"]
    ):
        raise ReflectorBoundaryError("REFLECTOR_UPSTREAM_ARGUMENTS_INVALID")
    host_cwd = Path(arguments[10]).resolve()
    host_output = Path(arguments[12]).resolve()
    if host_output.parent != host_cwd or not host_cwd.is_dir():
        raise ReflectorBoundaryError("REFLECTOR_UPSTREAM_PATH_INVALID")
    try:
        if any(host_cwd.iterdir()):
            raise ReflectorBoundaryError("REFLECTOR_UPSTREAM_WORKDIR_NOT_EMPTY")
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_UPSTREAM_PATH_INVALID") from exc
    return host_output, list(arguments)


def _replace_upstream_paths(arguments: list[str]) -> list[str]:
    replaced = list(arguments)
    replaced[replaced.index("--output-last-message") + 1] = "/output/last-message.md"
    replaced[replaced.index("--cd") + 1] = "/work"
    insertion = len(replaced) - 1
    existing_disabled = {
        replaced[index + 1] for index, value in enumerate(replaced[:-1]) if value == "--disable"
    }
    replaced[insertion:insertion] = _reflector_hardening_arguments(existing_disabled)
    return replaced


def _project_reflector_prompt(prompt: str, *, source_split: str) -> str:
    """Remove operational identifiers from taskwise prompts and bind exact output shape."""

    if type(prompt) is not str or source_split not in _ALLOWED_SOURCE_SPLITS:
        raise ReflectorBoundaryError("REFLECTOR_WRAPPER_CONFIG_INVALID")
    if source_split == "dev":
        return prompt
    retained = [
        line for line in prompt.splitlines() if _TASKWISE_OPERATIONAL_LINE_RE.match(line) is None
    ]
    projected = "\n".join(retained)
    projected = _TASKWISE_OPERATIONAL_ID_RE.sub("[sealed]", projected).rstrip()
    contract = (
        _TASKWISE_PROMPT_CONTRACT
        if source_split == TASKWISE_SOURCE_SPLIT
        else _SUPERVISED_PROMPT_CONTRACT
    )
    return f"{projected}\n\n{contract}\n"


def _normalize_supervised_memory_sections(memory: str) -> tuple[str, bool]:
    """Merge duplicate exact supervised sections without changing their bodies.

    This is deliberately narrower than the validator.  It only handles the observed
    syntactic failure mode: every required section is present in first-occurrence
    order, but one or more exact allowed headings were repeated.  Missing, unknown,
    or reordered headings remain untouched and therefore fail closed downstream.
    """

    if type(memory) is not str:
        raise TypeError("supervised memory must be text")
    lines = memory.splitlines()
    first_nonempty_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_nonempty_index is None:
        return memory, False
    h1 = lines[first_nonempty_index]
    if not h1.startswith("# Category Memory: ") or not h1.removeprefix(
        "# Category Memory: "
    ).strip():
        return memory, False

    sections: dict[str, list[list[str]]] = {name: [] for name in _SUPERVISED_EXACT_SECTIONS}
    headings: list[str] = []
    current: list[str] | None = None
    for line in lines[first_nonempty_index + 1 :]:
        match = _H2_LINE_RE.fullmatch(line)
        if match is not None:
            section = match.group(1)
            if section not in sections:
                return memory, False
            headings.append(section)
            current = []
            sections[section].append(current)
            continue
        if current is None:
            if line.strip():
                return memory, False
        else:
            current.append(line)

    first_occurrences = tuple(dict.fromkeys(headings))
    if first_occurrences != _SUPERVISED_EXACT_SECTIONS or len(headings) == len(
        _SUPERVISED_EXACT_SECTIONS
    ):
        return memory, False

    normalized = [h1, ""]
    for index, section in enumerate(_SUPERVISED_EXACT_SECTIONS):
        normalized.append(f"## {section}")
        bodies = sections[section]
        for body_index, body in enumerate(bodies):
            trimmed = list(body)
            while trimmed and not trimmed[0].strip():
                trimmed.pop(0)
            while trimmed and not trimmed[-1].strip():
                trimmed.pop()
            if body_index and trimmed and normalized[-1].strip():
                normalized.append("")
            normalized.extend(trimmed)
        if index != len(_SUPERVISED_EXACT_SECTIONS) - 1:
            normalized.append("")
    return "\n".join(normalized).rstrip() + "\n", True


def _reflector_hardening_arguments(
    existing_disabled: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Return the single audited source of reflector Codex hardening args."""

    if not isinstance(existing_disabled, (set, frozenset)) or any(
        type(value) is not str for value in existing_disabled
    ):
        raise TypeError("existing_disabled must be a set of feature names")
    hardening: list[str] = []
    for feature in _DISABLED_CODEX_FEATURES:
        if feature not in existing_disabled:
            hardening.extend(("--disable", feature))
    for config in _REFLECTOR_HARDENING_CONFIG:
        key = config.partition("=")[0]
        if key == "agents.enabled":
            raise ReflectorBoundaryError("REFLECTOR_CODEX_CONFIG_AGENTS_ENABLED_FORBIDDEN")
        hardening.extend(("--config", config))
    return hardening


def inspect_reflector_codex_policy_v2(
    real_codex_binary: str | Path,
    *,
    probe_runner: ReflectorCodexPolicyProbeRunnerV2 | None = None,
    hardening_arguments: Sequence[str] | None = None,
) -> dict[str, object]:
    """Run a zero-model Codex config-parser probe in an isolated empty home."""

    executable = Path(real_codex_binary).resolve()
    arguments = tuple(
        _reflector_hardening_arguments() if hardening_arguments is None else hardening_arguments
    )
    findings: set[str] = set()
    config_values = _flag_values(arguments, "--config")
    disabled_values = _flag_values(arguments, "--disable")
    if (
        any(value.partition("=")[0] == "agents.enabled" for value in config_values)
        or "agents.enabled=false" in arguments
    ):
        findings.add("REFLECTOR_CODEX_CONFIG_AGENTS_ENABLED_FORBIDDEN")
    expected_arguments = tuple(_reflector_hardening_arguments())
    if (
        arguments != expected_arguments
        or "exec" in arguments
        or "--model" in arguments
        or tuple(config_values) != _REFLECTOR_HARDENING_CONFIG
        or tuple(disabled_values) != _DISABLED_CODEX_FEATURES
    ):
        findings.add("REFLECTOR_CODEX_CONFIG_POLICY_INVALID")

    runner = probe_runner or _default_reflector_codex_policy_probe_runner_v2
    version_returncode: int | None = None
    version_output = ""
    config_returncode: int | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=".reflector-codex-policy-preflight-") as temporary:
            root = Path(temporary)
            paths = {
                name: root / name
                for name in (
                    "home",
                    "codex_home",
                    "xdg_config",
                    "xdg_cache",
                    "xdg_state",
                    "tmp",
                    "work",
                )
            }
            for path in paths.values():
                path.mkdir(mode=0o700)
            environment = {
                "CODEX_HOME": os.fspath(paths["codex_home"]),
                "HOME": os.fspath(paths["home"]),
                "LANG": "C.UTF-8",
                "PATH": os.environ.get("PATH", os.defpath),
                "TMP": os.fspath(paths["tmp"]),
                "TMPDIR": os.fspath(paths["tmp"]),
                "XDG_CACHE_HOME": os.fspath(paths["xdg_cache"]),
                "XDG_CONFIG_HOME": os.fspath(paths["xdg_config"]),
                "XDG_STATE_HOME": os.fspath(paths["xdg_state"]),
            }
            version_returncode, version_output = runner(
                (os.fspath(executable), "--version"),
                paths["work"],
                environment,
                _CODEX_POLICY_PROBE_TIMEOUT_SECONDS,
            )
            config_returncode, _discarded_output = runner(
                (
                    os.fspath(executable),
                    "debug",
                    "prompt-input",
                    *arguments,
                    "REFLECTOR_CONFIG_POLICY_PREFLIGHT_NO_MODEL",
                ),
                paths["work"],
                environment,
                _CODEX_POLICY_PROBE_TIMEOUT_SECONDS,
            )
    except (OSError, RuntimeError, TypeError, ValueError):
        findings.add("REFLECTOR_CODEX_CONFIG_PROBE_UNAVAILABLE")
    else:
        if version_returncode != 0 or version_output.strip() != _EXPECTED_CODEX_VERSION:
            findings.add("REFLECTOR_CODEX_VERSION_MISMATCH")
        if config_returncode != 0:
            findings.add("REFLECTOR_CODEX_CONFIG_PROBE_REJECTED")

    receipt = {
        "schema_version": "chembench4k_reflector_codex_policy_preflight_v1",
        "status": "PASS" if not findings else "BLOCKED",
        "finding_codes": sorted(findings),
        "codex_cli_version": _EXPECTED_CODEX_VERSION.removeprefix("codex-cli "),
        "hardening_sha256": _canonical_sha256(list(arguments)),
        "model_calls": 0,
        "stderr_included": False,
    }
    return receipt


def verify_reflector_codex_policy_v2(
    real_codex_binary: str | Path,
    *,
    probe_runner: ReflectorCodexPolicyProbeRunnerV2 | None = None,
) -> dict[str, object]:
    receipt = inspect_reflector_codex_policy_v2(
        real_codex_binary,
        probe_runner=probe_runner,
    )
    findings = receipt["finding_codes"]
    if type(findings) is not list or any(
        code not in _REFLECTOR_POLICY_FINDINGS for code in findings
    ):
        raise ReflectorBoundaryError("REFLECTOR_CODEX_CONFIG_POLICY_INVALID")
    if findings:
        raise ReflectorBoundaryError(findings[0])
    return receipt


def _flag_values(arguments: Sequence[str], flag: str) -> tuple[str, ...]:
    if (
        isinstance(arguments, (str, bytes))
        or not isinstance(arguments, Sequence)
        or any(type(value) is not str for value in arguments)
    ):
        raise ReflectorBoundaryError("REFLECTOR_CODEX_CONFIG_POLICY_INVALID")
    values: list[str] = []
    for index, value in enumerate(arguments):
        if value == flag:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("--"):
                raise ReflectorBoundaryError("REFLECTOR_CODEX_CONFIG_POLICY_INVALID")
            values.append(arguments[index + 1])
    return tuple(values)


def _default_reflector_codex_policy_probe_runner_v2(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> tuple[int, str]:
    """Execute only version/debug commands and discard stderr completely."""

    if "exec" in command or "--model" in command:
        raise ReflectorBoundaryError("REFLECTOR_CODEX_CONFIG_POLICY_INVALID")
    try:
        completed = subprocess.run(
            tuple(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout_seconds,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""
    return completed.returncode, completed.stdout


def _resolve_native_codex_binary(candidate: Path) -> Path:
    """Resolve the npm launcher to the static Codex binary actually executed."""

    resolved = candidate.resolve()
    try:
        first_line = resolved.open("rb").readline(128)
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_CODEX_BINARY_MISSING") from exc
    if not first_line.startswith(b"#!/usr/bin/env node"):
        return resolved
    package_root = resolved.parent.parent
    native_candidates = tuple(
        sorted(package_root.glob("node_modules/@openai/codex-linux-*/vendor/*/bin/codex"))
    )
    if len(native_candidates) != 1:
        raise ReflectorBoundaryError("REFLECTOR_CODEX_INSTALLATION_INVALID")
    native = native_candidates[0].resolve()
    _require_owned_regular_file(native, executable=True)
    return native


def _sandboxed_codex_command(
    real_codex_binary: Path,
    arguments: Sequence[str],
) -> tuple[str, ...]:
    del real_codex_binary
    return ("/opt/codex-bin", *arguments)


def _materialize_reflector_transport_environment(
    layout: Mapping[str, Path],
) -> dict[str, str]:
    """Copy approved CA material and return the exact transport-only environment."""

    try:
        transport_root = layout["transport_ca"]
        metadata = transport_root.lstat()
    except (KeyError, OSError) as exc:
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")
    try:
        if any(transport_root.iterdir()):
            raise OSError
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
    try:
        source_environment = _sanitized_model_transport_environment()
    except ValueError as exc:
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
    environment = {
        key: value
        for key, value in source_environment.items()
        if key not in {*_TRANSPORT_CA_FILE_KEYS, _TRANSPORT_CA_DIRECTORY_KEY}
    }
    for key in _TRANSPORT_CA_FILE_KEYS:
        value = source_environment.get(key)
        if value is None:
            continue
        destination_name = f"{key}.pem"
        content = _read_safe_transport_ca_file(Path(value), allow_symlink=False)
        try:
            _exclusive_write(
                transport_root / destination_name,
                content,
                mode=0o400,
            )
        except OSError as exc:
            raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
        environment[key] = f"/transport-ca/{destination_name}"
    directory_value = source_environment.get(_TRANSPORT_CA_DIRECTORY_KEY)
    if directory_value is not None:
        destination = transport_root / _TRANSPORT_CA_DIRECTORY_KEY
        try:
            destination.mkdir(mode=0o700)
            _copy_safe_transport_ca_directory(
                Path(directory_value),
                destination,
            )
        except OSError as exc:
            raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
        environment[_TRANSPORT_CA_DIRECTORY_KEY] = f"/transport-ca/{_TRANSPORT_CA_DIRECTORY_KEY}"
    return {key: environment[key] for key in _MODEL_TRANSPORT_ENV_KEYS if key in environment}


def _read_safe_transport_ca_file(
    source: Path,
    *,
    allow_symlink: bool,
) -> bytes:
    if not source.is_absolute():
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")
    try:
        source_metadata = source.lstat()
        if stat.S_ISLNK(source_metadata.st_mode):
            if not allow_symlink:
                raise OSError
            resolved = source.resolve(strict=True)
        else:
            resolved = source.resolve(strict=True)
        metadata = resolved.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > _MAX_TRANSPORT_CA_FILE_BYTES
            or _path_is_inside_any_protected_root(resolved)
        ):
            raise OSError
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if (
                opened_metadata.st_dev != metadata.st_dev
                or opened_metadata.st_ino != metadata.st_ino
                or not stat.S_ISREG(opened_metadata.st_mode)
            ):
                raise OSError
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(_MAX_TRANSPORT_CA_FILE_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
    if (
        not content
        or len(content) > _MAX_TRANSPORT_CA_FILE_BYTES
        or b"-----BEGIN CERTIFICATE-----" not in content
        or b"-----END CERTIFICATE-----" not in content
    ):
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")
    return content


def _copy_safe_transport_ca_directory(source: Path, destination: Path) -> None:
    if not source.is_absolute():
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")
    try:
        source_metadata = source.lstat()
        resolved = source.resolve(strict=True)
        metadata = resolved.lstat()
        if (
            stat.S_ISLNK(source_metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or _path_is_inside_any_protected_root(resolved)
        ):
            raise OSError
        entries = tuple(sorted(resolved.iterdir(), key=lambda path: path.name))
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
    if not entries or len(entries) > _MAX_TRANSPORT_CA_DIRECTORY_ENTRIES:
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")
    total_bytes = 0
    copied = 0
    for entry in entries:
        try:
            entry_metadata = entry.lstat()
        except OSError as exc:
            raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID") from exc
        if stat.S_ISDIR(entry_metadata.st_mode):
            continue
        content = _read_safe_transport_ca_file(entry, allow_symlink=True)
        total_bytes += len(content)
        copied += 1
        if total_bytes > _MAX_TRANSPORT_CA_DIRECTORY_BYTES:
            raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")
        _exclusive_write(destination / entry.name, content, mode=0o400)
    if copied == 0:
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")


def _bubblewrap_base_command(
    *,
    bwrap_binary: Path,
    layout: Mapping[str, Path],
    executable_source: Path,
    executable_command: Sequence[str],
    include_codex: bool,
) -> list[str]:
    command = [
        os.fspath(bwrap_binary),
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
    ]
    if not include_codex:
        command.append("--clearenv")
    try:
        executable_prefix = executable_source.resolve().open("rb").read(128)
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_CODEX_BINARY_MISSING") from exc
    needs_helper_runtime = not include_codex or executable_prefix.startswith(b"#!")
    sandbox_path = "/usr/bin:/bin" if needs_helper_runtime else "/nonexistent"
    if needs_helper_runtime:
        for source in _HELPER_SYSTEM_DIRS:
            if source.exists():
                command.extend(("--ro-bind", os.fspath(source), os.fspath(source)))
    command.extend(("--dir", "/etc"))
    for source in _SAFE_ETC_PATHS:
        if source.exists():
            command.extend(("--ro-bind", os.fspath(source), os.fspath(source)))
    command.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            "/home",
            "--bind",
            os.fspath(layout["home"]),
            "/home/codex",
            "--bind",
            os.fspath(layout["codex_home"]),
            "/codex_home",
            "--bind",
            os.fspath(layout["xdg_config"]),
            "/xdg/config",
            "--bind",
            os.fspath(layout["xdg_cache"]),
            "/xdg/cache",
            "--bind",
            os.fspath(layout["xdg_state"]),
            "/xdg/state",
            "--bind",
            os.fspath(layout["tmp"]),
            "/tmp",
            "--bind",
            os.fspath(layout["work"]),
            "/work",
            "--ro-bind",
            os.fspath(layout["inputs"] / "dev_loo_dataset.jsonl"),
            "/inputs/dev_loo_dataset.jsonl",
            "--bind",
            os.fspath(layout["output"]),
            "/output",
            "--setenv",
            "HOME",
            "/home/codex",
            "--setenv",
            "CODEX_HOME",
            "/codex_home",
            "--setenv",
            "XDG_CONFIG_HOME",
            "/xdg/config",
            "--setenv",
            "XDG_CACHE_HOME",
            "/xdg/cache",
            "--setenv",
            "XDG_STATE_HOME",
            "/xdg/state",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "PATH",
            sandbox_path,
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
            "--chdir",
            "/work",
        )
    )
    if include_codex:
        resolved = executable_source.resolve()
        command.extend(
            (
                "--ro-bind",
                os.fspath(layout["transport_ca"]),
                "/transport-ca",
            )
        )
        command.extend(("--ro-bind", os.fspath(resolved), "/opt/codex-bin"))
    command.extend(("--", *executable_command))
    return command


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _run_process_group(
    command: Sequence[str],
    *,
    input_text: str,
    timeout_seconds: float,
    environment: Mapping[str, str],
) -> _ProcessResult:
    if not isinstance(environment, Mapping) or any(
        type(key) is not str or type(value) is not str for key, value in environment.items()
    ):
        raise TypeError("environment must be a string mapping")
    if (
        not set(environment).issubset(_MODEL_TRANSPORT_ENV_KEYS)
        or any(not value for value in environment.values())
        or any("\x00" in value for value in environment.values())
    ):
        raise ReflectorBoundaryError("REFLECTOR_MODEL_TRANSPORT_ENV_INVALID")
    process = subprocess.Popen(
        list(command),
        env=dict(environment),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        quiescent = _terminate_invocation_processes(
            process,
            process_group_id=process.pid,
        )
        try:
            stdout, _stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            _close_process_pipes(process)
            raise ReflectorBoundaryError("REFLECTOR_PROCESS_TERMINATION_FAILED") from exc
        if not quiescent:
            raise ReflectorBoundaryError("REFLECTOR_PROCESS_TERMINATION_FAILED") from exc
        return _ProcessResult(returncode=124, stdout=stdout, stderr=_stderr)
    except BaseException as exc:
        quiescent = _terminate_invocation_processes(
            process,
            process_group_id=process.pid,
        )
        _close_process_pipes(process)
        if not quiescent:
            raise ReflectorBoundaryError("REFLECTOR_PROCESS_TERMINATION_FAILED") from exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ReflectorBoundaryError("REFLECTOR_PROCESS_IO_FAILED") from exc
    quiescent = _terminate_invocation_processes(
        process,
        process_group_id=process.pid,
    )
    if not quiescent:
        raise ReflectorBoundaryError("REFLECTOR_PROCESS_TERMINATION_FAILED")
    return _ProcessResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=_stderr,
    )


def _stderr_tail_codes(stderr: str) -> tuple[str, ...]:
    """Map at most four stderr tail lines into a non-text closed taxonomy."""

    if type(stderr) is not str:
        raise TypeError("stderr must be text")
    codes: list[str] = []
    for line in tuple(item for item in stderr.splitlines() if item.strip())[-4:]:
        normalized = line.casefold()
        if "config.toml" in normalized or "invalid type" in normalized:
            code = "CODEX_CONFIG_PARSE_ERROR"
        elif "auth" in normalized or "login" in normalized:
            code = "CODEX_AUTH_ERROR"
        elif "429" in normalized or "rate limit" in normalized:
            code = "CODEX_RATE_LIMIT"
        elif "timeout" in normalized or "timed out" in normalized:
            code = "CODEX_TIMEOUT"
        elif any(
            marker in normalized for marker in ("connection", "transport", "websocket", "network")
        ):
            code = "CODEX_TRANSPORT_ERROR"
        else:
            code = "CODEX_STDERR_REDACTED"
        codes.append(code)
    return tuple(codes)


def _create_layout(root: Path) -> dict[str, Path]:
    root.chmod(0o700)
    layout = {"root": root}
    for name in (
        "home",
        "codex_home",
        "xdg_config",
        "xdg_cache",
        "xdg_state",
        "tmp",
        "work",
        "inputs",
        "output",
        "private_events",
        "transport_ca",
    ):
        path = root / name
        path.mkdir(mode=0o700)
        layout[name] = path
    return layout


def _read_and_validate_records(
    path: Path,
    *,
    expected_record_count: int = EXPECTED_RECORDS,
    expected_source_split: str = "dev",
) -> tuple[list[dict[str, Any]], str]:
    if (
        isinstance(expected_record_count, bool)
        or not isinstance(expected_record_count, int)
        or not 1 <= expected_record_count <= MAX_BOUNDARY_RECORDS
        or expected_source_split not in _ALLOWED_SOURCE_SPLITS
    ):
        raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_INVALID")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_INVALID") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_INVALID") from exc
        if not isinstance(value, dict) or value.get("source_split") != expected_source_split:
            raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_INVALID")
        records.append(value)
    uids = [record.get("uid") for record in records]
    if (
        len(records) != expected_record_count
        or any(type(uid) is not str for uid in uids)
        or len(set(uids)) != expected_record_count
    ):
        raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_INVALID")
    return records, _canonical_sha256(records)


def _copy_private_file(source: Path, destination: Path, *, mode: int) -> None:
    _require_owned_regular_file(source)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_PRIVATE_FILE_COPY_FAILED") from exc
    _exclusive_write(destination, data, mode=mode)


def _require_owned_regular_file(
    path: Path,
    *,
    exact_mode: int | None = None,
    executable: bool = False,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_REQUIRED_FILE_MISSING") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or (exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode)
        or (executable and not os.access(path, os.X_OK))
    ):
        raise ReflectorBoundaryError("REFLECTOR_REQUIRED_FILE_UNSAFE")


def _read_bounded_regular_file(path: Path, *, maximum: int) -> bytes:
    _require_owned_regular_file(path)
    try:
        metadata = path.stat()
        if metadata.st_size > maximum:
            raise OSError
        return path.read_bytes()
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_LAST_MESSAGE_INVALID") from exc


def _exclusive_write(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _cleanup_invocation_root(root: Path) -> bool:
    if root.name.startswith(_ROOT_PREFIX) is False:
        return False
    for delay in (0.0, 0.05, 0.1, 0.2, 0.4):
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            return True
        except OSError:
            continue
        return not root.exists()
    return False


def _restore_environment(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReflectorBoundaryError("REFLECTOR_REQUIRED_FILE_MISSING") from exc


if __name__ == "__main__":
    raise SystemExit(reflector_codex_wrapper_main())
