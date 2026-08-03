"""Owner-private fsync ledger for Safe-Evolve V2 exactly-once calls and gates."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes

LEDGER_SCHEMA = "TemperatureSafeEvolveLedgerEventV2"
GENESIS_DIGEST = "0" * 64
MAX_EVENT_BYTES = 1024 * 1024
MAX_LEDGER_BYTES = 512 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RUN_ID = re.compile(r"stv3-temperature-safe-evolve-v2-[A-Za-z0-9._:-]{8,160}\Z")
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,191}\Z", re.ASCII)
_RECORDED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")

CALL_KINDS = frozenset(
    {
        "CALL_CLAIMED",
        "CALL_NO_COMPLETION_FAILURE",
        "CALL_REJECTED_COMPLETION",
        "CALL_ACCEPTED",
        "CALL_EVALUATED",
    }
)
PROTOCOL_KINDS = frozenset(
    {
        "CAMPAIGN_CREATED",
        "PREFLIGHT_CLOSED",
        "RUNTIME_SERVICES_STARTED",
        "RUNTIME_SERVICES_STOPPED",
        "PHASE_A_ARM_CREATED",
        "LOGICAL_CONTEXT_BOUND",
        "BLOCK_INFERENCE_CLOSED",
        "GT_RELEASED",
        "PHASE_A_CLOSED",
        "TARGET_SELECTION_CLOSED",
        "FOLD_RUN_CREATED",
        "REFLECTOR_ACCEPTED",
        "CORE_JOB_COMPLETED",
        "PROVISIONAL_STATE_FROZEN",
        "PROMOTION_DECIDED",
        "V1_CLOSED",
        "V2_CLOSED",
        "FINAL_STATE_FROZEN",
        "TEST_INFERENCE_CLOSED",
        "TEST_GT_RELEASED",
        "FOLD_TEST_CLOSED",
        "R0_CONTINUATION_DECIDED",
        "CAMPAIGN_CLOSED",
        "INCIDENT_OPENED",
        "RECOVERY_COMPLETED",
        "RUN_INVALIDATED",
        "RUN_FAILED",
    }
)
ALLOWED_KINDS = CALL_KINDS | PROTOCOL_KINDS
REFLECTOR_REJECTION_CODES = frozenset(
    {
        "SAFE_REFLECTOR_RESPONSE_SCHEMA_INVALID",
        "SAFE_REFLECTOR_RESPONSE_BINDING_INVALID",
        "SAFE_REFLECTOR_RESPONSE_LEAKAGE_INVALID",
        "SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID",
    }
)


class SafeLedgerError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


class SafeExperimentLedgerV2:
    """One process-exclusive append-only SHA-256 chained ledger."""

    def __init__(self, *, path: Path, run_id: str) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise TypeError("ledger path must be absolute")
        if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("run ID is invalid")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                raise SafeLedgerError("SAFE_LEDGER_FILE_UNSAFE")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SafeLedgerError("SAFE_LEDGER_WRITER_ALREADY_ACTIVE") from exc
            self._descriptor = descriptor
            self._path = path
            self._run_id = run_id
            self._events = self._read_events()
            self._validate(self._events)
            self._closed = False
        except BaseException:
            os.close(descriptor)
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        descriptor = self._descriptor
        self._descriptor = -1
        self._closed = True
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def head_digest(self) -> str:
        return GENESIS_DIGEST if not self._events else str(self._events[-1]["event_sha256"])

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise SafeLedgerError("SAFE_LEDGER_CLOSED")
        if kind not in ALLOWED_KINDS or type(payload) is not dict:
            raise SafeLedgerError("SAFE_LEDGER_EVENT_INVALID")
        body = {
            "schema_version": LEDGER_SCHEMA,
            "sequence": len(self._events) + 1,
            "previous_event_sha256": self.head_digest,
            "run_id": self._run_id,
            "kind": kind,
            "recorded_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "payload": payload,
        }
        event = {**body, "event_sha256": _digest(body)}
        candidate = (*self._events, event)
        self._validate(candidate)
        encoded = canonical_json_bytes(event)
        if len(encoded) > MAX_EVENT_BYTES:
            raise SafeLedgerError("SAFE_LEDGER_EVENT_TOO_LARGE")
        if os.fstat(self._descriptor).st_size + len(encoded) > MAX_LEDGER_BYTES:
            raise SafeLedgerError("SAFE_LEDGER_CAPACITY_EXCEEDED")
        try:
            if os.write(self._descriptor, encoded) != len(encoded):
                raise OSError("short append")
            os.fsync(self._descriptor)
        except OSError as exc:
            raise SafeLedgerError("SAFE_LEDGER_APPEND_FAILED") from exc
        self._events.append(event)
        return event

    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None:
        values = [
            event["payload"]
            for event in self._events
            if event["kind"] == "CALL_ACCEPTED"
            and event["payload"].get("logical_call_id") == logical_call_id
        ]
        return None if not values else dict(values[0])

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None:
        values = [
            event["payload"]
            for event in self._events
            if event["kind"] == "CALL_CLAIMED"
            and event["payload"].get("logical_call_id") == logical_call_id
        ]
        return None if not values else dict(values[-1])

    def failure_has_no_completion(self, call_id: str) -> bool:
        return any(
            event["kind"] == "CALL_NO_COMPLETION_FAILURE"
            and event["payload"].get("call_id") == call_id
            for event in self._events
        )

    def completion_was_rejected(self, call_id: str) -> bool:
        return any(
            event["kind"] == "CALL_REJECTED_COMPLETION"
            and event["payload"].get("call_id") == call_id
            for event in self._events
        )

    def accepted_logical_count(self, logical_arm: str) -> int:
        """Count accepted logical calls for one closed harness role."""

        claims = {
            str(event["payload"]["logical_call_id"]): event["payload"]
            for event in self._events
            if event["kind"] == "CALL_CLAIMED"
        }
        return sum(
            claims.get(str(event["payload"].get("logical_call_id")), {}).get("logical_arm")
            == logical_arm
            for event in self._events
            if event["kind"] == "CALL_ACCEPTED"
        )

    def _read_events(self) -> list[dict[str, Any]]:
        size = os.fstat(self._descriptor).st_size
        if size > MAX_LEDGER_BYTES:
            raise SafeLedgerError("SAFE_LEDGER_CAPACITY_EXCEEDED")
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        raw = b""
        while len(raw) < size:
            chunk = os.read(self._descriptor, min(1024 * 1024, size - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) != size:
            raise SafeLedgerError("SAFE_LEDGER_READ_FAILED")
        return _decode(raw)

    def _validate(self, events: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
        previous = GENESIS_DIGEST
        claims_by_call: dict[str, dict[str, Any]] = {}
        claims_by_logical: dict[str, list[dict[str, Any]]] = {}
        accepted: set[str] = set()
        no_completion: set[str] = set()
        rejected: set[str] = set()
        evaluated: set[str] = set()
        invalidated = False
        for sequence, event in enumerate(events, start=1):
            expected = {
                "schema_version",
                "sequence",
                "previous_event_sha256",
                "run_id",
                "kind",
                "recorded_at_utc",
                "payload",
                "event_sha256",
            }
            if (
                type(event) is not dict
                or set(event) != expected
                or event["schema_version"] != LEDGER_SCHEMA
                or event["sequence"] != sequence
                or event["previous_event_sha256"] != previous
                or event["run_id"] != self._run_id
                or event["kind"] not in ALLOWED_KINDS
                or type(event["payload"]) is not dict
                or _RECORDED.fullmatch(str(event["recorded_at_utc"])) is None
                or _digest({key: event[key] for key in event if key != "event_sha256"})
                != event["event_sha256"]
            ):
                raise SafeLedgerError("SAFE_LEDGER_CHAIN_INVALID")
            previous = str(event["event_sha256"])
            kind = str(event["kind"])
            payload = event["payload"]
            if invalidated and kind not in {"INCIDENT_OPENED", "RECOVERY_COMPLETED"}:
                raise SafeLedgerError("SAFE_LEDGER_EVENT_AFTER_TERMINAL")
            if kind == "CALL_CLAIMED":
                logical, call_id = _claim(payload)
                attempts = claims_by_logical.setdefault(logical, [])
                if logical in accepted or call_id in claims_by_call:
                    raise SafeLedgerError("SAFE_LEDGER_DUPLICATE_CALL_CLAIM")
                if payload["attempt_number"] != len(attempts) + 1:
                    raise SafeLedgerError("SAFE_LEDGER_CALL_RETRY_INVALID")
                if attempts:
                    prior = attempts[-1]
                    if (
                        prior["call_id"] not in no_completion | rejected
                        or prior.get("retry_semantics_sha256")
                        != payload.get("retry_semantics_sha256")
                        or any(
                            prior.get(field) != payload.get(field)
                            for field in (
                                "logical_call_id",
                                "phase",
                                "logical_arm",
                                "service_identity_sha256",
                            )
                        )
                    ):
                        raise SafeLedgerError("SAFE_LEDGER_CALL_RETRY_INVALID")
                attempts.append(payload)
                claims_by_call[call_id] = payload
            elif kind == "CALL_NO_COMPLETION_FAILURE":
                logical, call_id = _call_ref(payload)
                claim = claims_by_call.get(call_id)
                if (
                    set(payload)
                    != {
                        "logical_call_id",
                        "call_id",
                        "failure_class",
                        "terminal_task_status",
                        "durable_rollout_no_completion",
                        "durable_gateway_absent",
                        "no_completion_evidence_sha256",
                    }
                    or claim is None
                    or claim["logical_call_id"] != logical
                    or logical in accepted
                    or call_id in no_completion | rejected
                    or payload.get("durable_rollout_no_completion") is not True
                    or payload.get("durable_gateway_absent") is not True
                    or _SHA256.fullmatch(str(payload.get("no_completion_evidence_sha256"))) is None
                ):
                    raise SafeLedgerError("SAFE_LEDGER_NO_COMPLETION_INVALID")
                no_completion.add(call_id)
            elif kind == "CALL_REJECTED_COMPLETION":
                logical, call_id = _call_ref(payload)
                claim = claims_by_call.get(call_id)
                if (
                    set(payload)
                    != {
                        "logical_call_id",
                        "call_id",
                        "rejection_code",
                        "response_sha256",
                        "task_result_sha256",
                        "transcript_sha256",
                        "completion_identity_sha256",
                        "durable_completion",
                        "tool_event_count",
                        "tool_policy_validated",
                    }
                    or claim is None
                    or claim["logical_call_id"] != logical
                    or claim.get("logical_arm") != "safe_reflector"
                    or payload.get("rejection_code") not in REFLECTOR_REJECTION_CODES
                    or logical in accepted
                    or call_id in no_completion | rejected
                    or any(
                        _SHA256.fullmatch(str(payload.get(field))) is None
                        for field in (
                            "response_sha256",
                            "task_result_sha256",
                            "transcript_sha256",
                            "completion_identity_sha256",
                        )
                    )
                    or payload.get("durable_completion") is not True
                    or payload.get("tool_event_count") != 0
                    or payload.get("tool_policy_validated") is not True
                ):
                    raise SafeLedgerError("SAFE_LEDGER_REJECTED_COMPLETION_INVALID")
                rejected.add(call_id)
            elif kind == "CALL_ACCEPTED":
                logical, call_id = _call_ref(payload)
                claim = claims_by_call.get(call_id)
                response = payload.get("response")
                if (
                    set(payload)
                    != {
                        "logical_call_id",
                        "call_id",
                        "response",
                        "response_sha256",
                        "task_result_sha256",
                        "transcript_sha256",
                        "completion_identity_sha256",
                        "tool_event_count",
                        "tool_policy_validated",
                    }
                    or claim is None
                    or claim["logical_call_id"] != logical
                    or logical in accepted
                    or call_id in no_completion | rejected
                    or type(response) is not str
                    or hashlib.sha256(response.encode()).hexdigest()
                    != payload.get("response_sha256")
                    or any(
                        _SHA256.fullmatch(str(payload.get(field))) is None
                        for field in (
                            "response_sha256",
                            "task_result_sha256",
                            "transcript_sha256",
                            "completion_identity_sha256",
                        )
                    )
                    or payload.get("tool_event_count") != 0
                    or payload.get("tool_policy_validated") is not True
                ):
                    raise SafeLedgerError("SAFE_LEDGER_ACCEPTED_CALL_INVALID")
                accepted.add(logical)
            elif kind == "CALL_EVALUATED":
                logical = payload.get("logical_call_id")
                evaluation = payload.get("evaluation_json")
                if (
                    set(payload) != {"logical_call_id", "evaluation_json", "evaluation_sha256"}
                    or type(logical) is not str
                    or logical not in accepted
                    or logical in evaluated
                    or type(evaluation) is not str
                    or hashlib.sha256(evaluation.encode()).hexdigest()
                    != payload.get("evaluation_sha256")
                ):
                    raise SafeLedgerError("SAFE_LEDGER_EVALUATION_INVALID")
                evaluated.add(logical)
            elif kind in {"RUN_INVALIDATED", "RUN_FAILED"}:
                if not payload.get("finding_code"):
                    raise SafeLedgerError("SAFE_LEDGER_TERMINAL_EVENT_INVALID")
                invalidated = True


def read_ledger_progress_v2(*, path: Path, run_id: str) -> dict[str, object]:
    """Read and validate an aggregate-only snapshot without taking the writer lock."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("ledger path must be absolute")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_size > MAX_LEDGER_BYTES
    ):
        raise SafeLedgerError("SAFE_LEDGER_OBSERVER_FILE_UNSAFE")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.fstat(descriptor)
        raw = os.pread(descriptor, before.st_size, 0)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) != before.st_size or (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise SafeLedgerError("SAFE_LEDGER_OBSERVER_CHANGED")
    events = _decode(raw)
    validator = object.__new__(SafeExperimentLedgerV2)
    validator._run_id = run_id
    validator._validate(events)
    counts: dict[str, int] = {}
    claims: set[str] = set()
    closed: set[str] = set()
    for event in events:
        kind = str(event["kind"])
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "CALL_CLAIMED":
            claims.add(str(event["payload"]["call_id"]))
        elif kind in {"CALL_NO_COMPLETION_FAILURE", "CALL_REJECTED_COMPLETION", "CALL_ACCEPTED"}:
            closed.add(str(event["payload"]["call_id"]))
    return {
        "schema_version": "TemperatureSafeLedgerProgressV2",
        "sequence": len(events),
        "head_sha256": GENESIS_DIGEST if not events else events[-1]["event_sha256"],
        "kind_counts": dict(sorted(counts.items())),
        "open_claim_count": len(claims - closed),
    }


def _claim(payload: dict[str, Any]) -> tuple[str, str]:
    logical, call_id = _call_ref(payload)
    if (
        set(payload)
        != {
            "logical_call_id",
            "call_id",
            "task_id",
            "phase",
            "logical_arm",
            "attempt_number",
            "task_request_sha256",
            "retry_semantics_sha256",
            "service_identity_sha256",
        }
        or _SAFE_ID.fullmatch(str(payload.get("task_id"))) is None
        or _SAFE_ID.fullmatch(str(payload.get("phase"))) is None
        or _SAFE_ID.fullmatch(str(payload.get("logical_arm"))) is None
        or type(payload.get("attempt_number")) is not int
        or not 1 <= payload["attempt_number"] <= 3
        or any(
            _SHA256.fullmatch(str(payload.get(field))) is None
            for field in (
                "task_request_sha256",
                "retry_semantics_sha256",
                "service_identity_sha256",
            )
        )
    ):
        raise SafeLedgerError("SAFE_LEDGER_CALL_CLAIM_INVALID")
    return logical, call_id


def _call_ref(payload: dict[str, Any]) -> tuple[str, str]:
    logical = payload.get("logical_call_id")
    call_id = payload.get("call_id")
    if (
        type(logical) is not str
        or _SAFE_ID.fullmatch(logical) is None
        or type(call_id) is not str
        or _SAFE_ID.fullmatch(call_id) is None
    ):
        raise SafeLedgerError("SAFE_LEDGER_CALL_REFERENCE_INVALID")
    return logical, call_id


def _decode(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise SafeLedgerError("SAFE_LEDGER_PARTIAL_RECORD")
    result: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if len(line) + 1 > MAX_EVENT_BYTES:
            raise SafeLedgerError("SAFE_LEDGER_EVENT_TOO_LARGE")
        try:
            value = json.loads(
                line, object_pairs_hook=_reject_duplicates, parse_constant=_reject_nonfinite
            )
        except (UnicodeError, json.JSONDecodeError, SafeLedgerError) as exc:
            raise SafeLedgerError("SAFE_LEDGER_RECORD_MALFORMED") from exc
        if type(value) is not dict:
            raise SafeLedgerError("SAFE_LEDGER_RECORD_INVALID")
        result.append(value)
    return result


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SafeLedgerError("SAFE_LEDGER_DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise SafeLedgerError(f"SAFE_LEDGER_NONFINITE_{value}")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "ALLOWED_KINDS",
    "CALL_KINDS",
    "GENESIS_DIGEST",
    "LEDGER_SCHEMA",
    "MAX_EVENT_BYTES",
    "MAX_LEDGER_BYTES",
    "PROTOCOL_KINDS",
    "REFLECTOR_REJECTION_CODES",
    "SafeExperimentLedgerV2",
    "SafeLedgerError",
    "read_ledger_progress_v2",
]
