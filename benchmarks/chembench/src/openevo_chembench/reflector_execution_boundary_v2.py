"""OS-isolated, zero-tool execution boundary for the v2 Core reflector.

The registered OpenEvo method invokes an executable named ``codex``.  Formal
ChemBench4K execution temporarily prepends a single-use launcher to ``PATH``;
that launcher enters this module and runs the real Codex installation inside a
minimal bubblewrap filesystem.  The model transport network remains shared,
but the repository, benchmark test data, results, user home, and prior
transcripts are not mounted.

No function in this module invokes Codex during preflight.  The visibility
probe and unit tests execute only deterministic helper processes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

from openevo_chembench.local_codex_executor import (
    _DISABLED_CODEX_FEATURES,
    _find_security_tool_use,
)


PROTOCOL_ID = "chembench4k_frozen_generalization_v2"
EXPECTED_RECORDS = 45
CONFIG_ENV = "OPENEVO_CHEMBENCH_REFLECTOR_BOUNDARY_CONFIG_V2"
WRAPPER_STATUS_ENV = "OPENEVO_CHEMBENCH_REFLECTOR_WRAPPER_V2"
ISOLATION_FINDING = "REFLECTOR_FILESYSTEM_ISOLATION_MISSING"
TOOL_VIOLATION_STATUS = "REFLECTOR_SECURITY_TOOL_USE_VIOLATION"
_CONFIG_SCHEMA = "chembench4k_reflector_wrapper_config_v2"
_RECEIPT_SCHEMA = "chembench4k_reflector_execution_receipt_v2"
_ROOT_PREFIX = "openevo-chembench-reflector-v2-"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_EVENT_BYTES = 16 * 1024 * 1024
_MAX_LAST_MESSAGE_BYTES = 64 * 1024
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

    @property
    def digest(self) -> str:
        return _canonical_sha256(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _RECEIPT_SCHEMA,
            "protocol_id": PROTOCOL_ID,
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
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReflectorExecutionReceiptV2:
        expected = {
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
        if set(payload) != expected:
            raise ReflectorBoundaryError("REFLECTOR_RECEIPT_SCHEMA_INVALID")
        if (
            payload["schema_version"] != _RECEIPT_SCHEMA
            or payload["protocol_id"] != PROTOCOL_ID
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
        )


@dataclass(frozen=True, slots=True)
class ReflectorBoundaryActivationV2:
    invocation_id: str
    wrapper_path: Path
    receipt_path: Path
    expected_records_sha256: str
    expected_record_count: int
    expected_real_codex_sha256: str

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
        if (
            receipt.invocation_id != self.invocation_id
            or receipt.record_count != self.expected_record_count
            or receipt.ordered_records_sha256 != self.expected_records_sha256
            or receipt.real_codex_sha256 != self.expected_real_codex_sha256
            or not receipt.wrapper_invoked
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
        private_audit_root: str | Path,
        real_codex_binary: str | Path | None = None,
        auth_source: str | Path | None = None,
        bwrap_binary: str | Path | None = None,
        timeout_seconds: float = 900.0,
        temporary_parent: str | Path | None = None,
    ) -> None:
        self.dev_artifact_path = Path(dev_artifact_path).resolve()
        self.expected_records_sha256 = expected_records_sha256
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
        if _SHA256_RE.fullmatch(expected_records_sha256) is None:
            raise ValueError("expected_records_sha256 must be a lowercase SHA-256")
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
        records, digest = _read_and_validate_records(self.dev_artifact_path)
        if len(records) != EXPECTED_RECORDS or digest != self.expected_records_sha256:
            raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_BINDING_INVALID")
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
                "protocol_id": PROTOCOL_ID,
                "invocation_id": invocation_id,
                "bwrap_binary": os.fspath(self.bwrap_binary),
                "real_codex_binary": os.fspath(self.real_codex_binary),
                "real_codex_sha256": real_codex_sha256,
                "auth_source": os.fspath(self.auth_source),
                "dev_artifact_source": os.fspath(self.dev_artifact_path),
                "dev_artifact_sha256": dev_artifact_sha256,
                "ordered_records_sha256": self.expected_records_sha256,
                "record_count": EXPECTED_RECORDS,
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
                expected_record_count=EXPECTED_RECORDS,
                expected_real_codex_sha256=real_codex_sha256,
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
    last_message_sha256: str | None = None
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
            layout["inputs"] / "dev_loo_dataset.jsonl"
        )
        if (
            len(records) != config["record_count"]
            or records_digest != config["ordered_records_sha256"]
            or _sha256_file(layout["inputs"] / "dev_loo_dataset.jsonl")
            != config["dev_artifact_sha256"]
        ):
            raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_BINDING_INVALID")
        rewritten = _replace_upstream_paths(rewritten)
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
        prompt = sys.stdin.read()
        completed = _run_process_group(
            command,
            input_text=prompt,
            timeout_seconds=float(config["timeout_seconds"]),
        )
        event_stream = completed.stdout
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
            isolated_output = layout["output"] / "last-message.md"
            content = _read_bounded_regular_file(
                isolated_output,
                maximum=_MAX_LAST_MESSAGE_BYTES,
            )
            if not content.strip():
                raise ReflectorBoundaryError("REFLECTOR_LAST_MESSAGE_MISSING")
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
        "private_audit_directory",
        "receipt_path",
        "timeout_seconds",
        "temporary_parent",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload["schema_version"] != _CONFIG_SCHEMA
        or payload["protocol_id"] != PROTOCOL_ID
        or payload["record_count"] != EXPECTED_RECORDS
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
    required = {
        "--json",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
    }
    if (
        not arguments
        or arguments[0] != "exec"
        or not required.issubset(arguments)
        or arguments[-1] != "-"
        or arguments.count("--output-last-message") != 1
        or arguments.count("--cd") != 1
        or arguments.count("--model") != 1
    ):
        raise ReflectorBoundaryError("REFLECTOR_UPSTREAM_ARGUMENTS_INVALID")
    output_index = arguments.index("--output-last-message") + 1
    cwd_index = arguments.index("--cd") + 1
    model_index = arguments.index("--model") + 1
    if max(output_index, cwd_index, model_index) >= len(arguments):
        raise ReflectorBoundaryError("REFLECTOR_UPSTREAM_ARGUMENTS_INVALID")
    if arguments[model_index] != "gpt-5.5":
        raise ReflectorBoundaryError("REFLECTOR_MODEL_IDENTITY_INVALID")
    host_output = Path(arguments[output_index]).resolve()
    host_cwd = Path(arguments[cwd_index]).resolve()
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
    hardening: list[str] = []
    for feature in _DISABLED_CODEX_FEATURES:
        if feature not in existing_disabled:
            hardening.extend(("--disable", feature))
    for config in (
        'model_reasoning_effort="medium"',
        'web_search="disabled"',
        'approval_policy="never"',
        'forced_login_method="chatgpt"',
        "check_for_update_on_startup=false",
        "allow_login_shell=false",
        'shell_environment_policy.inherit="none"',
        "agents.enabled=false",
    ):
        hardening.extend(("--config", config))
    replaced[insertion:insertion] = hardening
    return replaced


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
        "--clearenv",
    ]
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
        command.extend(("--ro-bind", os.fspath(resolved), "/opt/codex-bin"))
    command.extend(("--", *executable_command))
    return command


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: str


def _run_process_group(
    command: Sequence[str],
    *,
    input_text: str,
    timeout_seconds: float,
) -> _ProcessResult:
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, _stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, _stderr = process.communicate()
        return _ProcessResult(returncode=124, stdout=stdout)
    return _ProcessResult(returncode=process.returncode, stdout=stdout)


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
    ):
        path = root / name
        path.mkdir(mode=0o700)
        layout[name] = path
    return layout


def _read_and_validate_records(path: Path) -> tuple[list[dict[str, Any]], str]:
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
        if not isinstance(value, dict) or value.get("source_split") != "dev":
            raise ReflectorBoundaryError("REFLECTOR_DEV_ARTIFACT_INVALID")
        records.append(value)
    uids = [record.get("uid") for record in records]
    if (
        len(records) != EXPECTED_RECORDS
        or any(type(uid) is not str for uid in uids)
        or len(set(uids)) != EXPECTED_RECORDS
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
