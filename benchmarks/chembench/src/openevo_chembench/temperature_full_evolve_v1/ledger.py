"""Fsync-backed private exactly-once ledger for Temperature full-evolve v1."""

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

LEDGER_SCHEMA = "TemperatureFullEvolveLedgerEventV1"
GENESIS_DIGEST = "0" * 64
MAX_EVENT_BYTES = 1024 * 1024
MAX_LEDGER_BYTES = 128 * 1024 * 1024
LEDGER_PROGRESS_SCHEMA = "TemperatureFullEvolveLedgerProgressV1"
_READ_SNAPSHOT_ATTEMPTS = 4
REFLECTOR_REJECTED_COMPLETION_CODES = frozenset(
    {
        "REFLECTOR_RESPONSE_PACKET_SEQUENCE_INVALID",
        "REFLECTOR_RESPONSE_SCHEMA_OR_EVIDENCE_INVALID",
    }
)
REFLECTOR_ATTEMPT_RETRY_EXHAUSTED = "REFLECTOR_ATTEMPT_RETRY_EXHAUSTED"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RUNTIME_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}\Z", re.ASCII)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,191}\Z", re.ASCII)
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z", re.ASCII)
_RECORDED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z", re.ASCII)
_KINDS = frozenset(
    {
        "RUN_CREATED",
        "SPLIT_FROZEN",
        "CALL_CLAIMED",
        "CALL_NO_COMPLETION_FAILURE",
        "CALL_REJECTED_COMPLETION",
        "CALL_ACCEPTED",
        "CALL_EVALUATED",
        "BATCH_PRE_CLOSED",
        "REFLECTOR_ACCEPTED",
        "EVIDENCE_VALIDATED",
        "TARGET_JOB_COMPLETED",
        "BATCH_ARTIFACT_SET_COMMITTED",
        "BATCH_POST_CLOSED",
        "FINAL_STATE_FROZEN",
        "EVOLVED_TEST_CLOSED",
        "BASELINE_TEST_CLOSED",
        "AUDIT_CLOSED",
        "INCIDENT_OPENED",
        "RECOVERY_COMPLETED",
        "RUN_FAILED",
    }
)
_TARGETS = ("text_memory", "skill_bundle", "agent_system")


def _protocol_sequence() -> tuple[tuple[str, int | None, str | None], ...]:
    values: list[tuple[str, int | None, str | None]] = [
        ("RUN_CREATED", None, None),
        ("SPLIT_FROZEN", None, None),
    ]
    for batch in range(1, 5):
        values.extend(
            (
                ("BATCH_PRE_CLOSED", batch, None),
                ("REFLECTOR_ACCEPTED", batch, None),
                ("EVIDENCE_VALIDATED", batch, None),
                *(("TARGET_JOB_COMPLETED", batch, target) for target in _TARGETS),
                ("BATCH_ARTIFACT_SET_COMMITTED", batch, None),
                ("BATCH_POST_CLOSED", batch, None),
            )
        )
    values.extend(
        (
            ("FINAL_STATE_FROZEN", 4, None),
            ("EVOLVED_TEST_CLOSED", None, None),
            ("BASELINE_TEST_CLOSED", None, None),
            ("AUDIT_CLOSED", None, None),
        )
    )
    return tuple(values)


_PROTOCOL_SEQUENCE = _protocol_sequence()
_PROTOCOL_KINDS = frozenset(item[0] for item in _PROTOCOL_SEQUENCE)


class TemperatureLedgerError(RuntimeError):
    """Closed ledger integrity or transition failure."""


class TemperatureExperimentLedgerV1:
    """One process-exclusive append-only private ledger with a SHA-256 chain."""

    def __init__(self, *, path: Path, run_id: str) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise TypeError("ledger path must be absolute")
        if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("run ID is invalid")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise TemperatureLedgerError("LEDGER_FILE_UNSAFE")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TemperatureLedgerError("LEDGER_WRITER_ALREADY_ACTIVE") from exc
            self._descriptor = descriptor
            self._path = path
            self._run_id = run_id
            self._events = self._read_events()
            self._validate_transitions(self._events)
            self._closed = False
        except BaseException:
            os.close(descriptor)
            raise

    def __enter__(self) -> Self:
        if self._closed:
            raise TemperatureLedgerError("LEDGER_CLOSED")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        descriptor = self._descriptor
        self._closed = True
        self._descriptor = -1
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
            raise TemperatureLedgerError("LEDGER_CLOSED")
        if kind not in _KINDS or type(payload) is not dict:
            raise TemperatureLedgerError("LEDGER_EVENT_INVALID")
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
        self._validate_transitions(candidate)
        encoded = canonical_json_bytes(event)
        if len(encoded) > MAX_EVENT_BYTES:
            raise TemperatureLedgerError("LEDGER_EVENT_TOO_LARGE")
        metadata = os.fstat(self._descriptor)
        if metadata.st_size + len(encoded) > MAX_LEDGER_BYTES:
            raise TemperatureLedgerError("LEDGER_CAPACITY_EXCEEDED")
        try:
            written = os.write(self._descriptor, encoded)
            if written != len(encoded):
                raise OSError("short ledger append")
            os.fsync(self._descriptor)
        except OSError as exc:
            raise TemperatureLedgerError("LEDGER_APPEND_FAILED") from exc
        self._events.append(event)
        return event

    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None:
        for event in self._events:
            if (
                event["kind"] == "CALL_ACCEPTED"
                and event["payload"].get("logical_call_id") == logical_call_id
            ):
                return dict(event["payload"])
        return None

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
        """Return whether a durable Reflector completion was schema-rejected."""

        return any(
            event["kind"] == "CALL_REJECTED_COMPLETION"
            and event["payload"].get("call_id") == call_id
            for event in self._events
        )

    def _read_events(self) -> list[dict[str, Any]]:
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        if os.fstat(self._descriptor).st_size > MAX_LEDGER_BYTES:
            raise TemperatureLedgerError("LEDGER_CAPACITY_EXCEEDED")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(self._descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        return _decode_events(raw)

    def _validate_transitions(self, events: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
        claims_by_logical: dict[str, list[dict[str, Any]]] = {}
        claims_by_call: dict[str, dict[str, Any]] = {}
        no_completion: set[str] = set()
        rejected_completion: set[str] = set()
        accepted: dict[str, dict[str, Any]] = {}
        evaluated: set[str] = set()
        recovery_count = 0
        run_failed = False
        previous = GENESIS_DIGEST
        for sequence, event in enumerate(events, start=1):
            expected_keys = {
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
                or set(event) != expected_keys
                or event["schema_version"] != LEDGER_SCHEMA
                or event["sequence"] != sequence
                or event["previous_event_sha256"] != previous
                or event["run_id"] != self._run_id
                or event["kind"] not in _KINDS
                or type(event["recorded_at_utc"]) is not str
                or _RECORDED_AT.fullmatch(event["recorded_at_utc"]) is None
                or type(event["payload"]) is not dict
                or _SHA256.fullmatch(str(event["event_sha256"])) is None
            ):
                raise TemperatureLedgerError("LEDGER_CHAIN_INVALID")
            body = {key: event[key] for key in event if key != "event_sha256"}
            if _digest(body) != event["event_sha256"]:
                raise TemperatureLedgerError("LEDGER_CHAIN_INVALID")
            previous = str(event["event_sha256"])
            kind = str(event["kind"])
            payload = event["payload"]
            if run_failed:
                raise TemperatureLedgerError("LEDGER_EVENT_AFTER_RUN_FAILED")
            if kind == "CALL_CLAIMED":
                logical, call_id = _validate_claim(payload)
                if logical in accepted or call_id in claims_by_call:
                    raise TemperatureLedgerError("LEDGER_DUPLICATE_CALL_CLAIM")
                prior = claims_by_logical.setdefault(logical, [])
                if payload["attempt_number"] != len(prior) + 1 or (
                    prior
                    and (
                        str(prior[-1]["call_id"])
                        not in no_completion | rejected_completion
                        or payload.get("retry_semantics_sha256")
                        != prior[-1].get("retry_semantics_sha256")
                    )
                ):
                    raise TemperatureLedgerError("LEDGER_CALL_RETRY_INVALID")
                prior.append(payload)
                claims_by_call[call_id] = payload
            elif kind == "CALL_NO_COMPLETION_FAILURE":
                logical, call_id = _validate_call_reference(payload)
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
                    or call_id in no_completion
                    or call_id in rejected_completion
                    or type(payload.get("failure_class")) is not str
                    or not payload["failure_class"]
                    or type(payload.get("terminal_task_status")) is not str
                    or not payload["terminal_task_status"]
                    or payload.get("durable_rollout_no_completion") is not True
                    or payload.get("durable_gateway_absent") is not True
                    or _SHA256.fullmatch(
                        str(payload.get("no_completion_evidence_sha256"))
                    )
                    is None
                ):
                    raise TemperatureLedgerError("LEDGER_NO_COMPLETION_FAILURE_INVALID")
                no_completion.add(call_id)
            elif kind == "CALL_REJECTED_COMPLETION":
                logical, call_id = _validate_call_reference(payload)
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
                    or claim.get("phase") != "train_reflector"
                    or claim.get("logical_arm") != "batch_supervised_reflector"
                    or logical in accepted
                    or call_id in no_completion
                    or call_id in rejected_completion
                    or payload.get("rejection_code")
                    not in REFLECTOR_REJECTED_COMPLETION_CODES
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
                    or type(payload.get("tool_event_count")) is not int
                    or payload["tool_event_count"] != 0
                    or payload.get("tool_policy_validated") is not True
                ):
                    raise TemperatureLedgerError("LEDGER_REJECTED_COMPLETION_INVALID")
                rejected_completion.add(call_id)
            elif kind == "CALL_ACCEPTED":
                logical, call_id = _validate_call_reference(payload)
                claim = claims_by_call.get(call_id)
                response = payload.get("response")
                response_digest = payload.get("response_sha256")
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
                    or call_id in no_completion
                    or call_id in rejected_completion
                    or type(response) is not str
                    or _SHA256.fullmatch(str(response_digest)) is None
                    or hashlib.sha256(response.encode("utf-8")).hexdigest() != response_digest
                    or _SHA256.fullmatch(str(payload.get("task_result_sha256"))) is None
                    or _SHA256.fullmatch(str(payload.get("transcript_sha256"))) is None
                    or _SHA256.fullmatch(str(payload.get("completion_identity_sha256"))) is None
                    or type(payload.get("tool_event_count")) is not int
                    or payload["tool_event_count"] != 0
                    or payload.get("tool_policy_validated") is not True
                ):
                    raise TemperatureLedgerError("LEDGER_ACCEPTED_CALL_INVALID")
                accepted[logical] = payload
            elif kind == "CALL_EVALUATED":
                logical = payload.get("logical_call_id")
                evaluation_json = payload.get("evaluation_json")
                if (
                    set(payload)
                    != {"logical_call_id", "evaluation_json", "evaluation_sha256"}
                    or type(logical) is not str
                    or logical not in accepted
                    or logical in evaluated
                    or type(evaluation_json) is not str
                    or _SHA256.fullmatch(str(payload.get("evaluation_sha256"))) is None
                    or hashlib.sha256(evaluation_json.encode("utf-8")).hexdigest()
                    != payload.get("evaluation_sha256")
                    or not _is_canonical_json(evaluation_json)
                ):
                    raise TemperatureLedgerError("LEDGER_CALL_EVALUATION_INVALID")
                evaluated.add(logical)
            elif kind == "RECOVERY_COMPLETED":
                recovery_count += 1
                if (
                    set(payload)
                    != {
                        "recovery_index",
                        "recovery_mode",
                        "recovered_ledger_head_sha256",
                        "core_checkpoint_sha256",
                        "core_batch_index",
                    }
                    or payload.get("recovery_index") != recovery_count
                    or payload.get("recovery_mode") != "resume"
                    or payload.get("recovered_ledger_head_sha256")
                    != event["previous_event_sha256"]
                    or _SHA256.fullmatch(
                        str(payload.get("core_checkpoint_sha256"))
                    )
                    is None
                    or type(payload.get("core_batch_index")) is not int
                    or not 0 <= payload["core_batch_index"] <= 4
                ):
                    raise TemperatureLedgerError("LEDGER_RECOVERY_EVENT_INVALID")
            elif kind == "RUN_FAILED":
                _validate_reflector_retry_exhausted_failure(
                    payload=payload,
                    claims_by_logical=claims_by_logical,
                    no_completion=no_completion,
                    rejected_completion=rejected_completion,
                    accepted=accepted,
                )
                run_failed = True
        _validate_protocol_state_machine(events)


def read_validated_ledger_progress_v1(*, path: Path, run_id: str) -> dict[str, object]:
    """Read one stable live-ledger snapshot and expose aggregate progress only.

    This observer never opens the ledger for writing and never returns event
    payloads or their call/task identifiers.  It applies the same complete
    chain, retry, exactly-once, and protocol validation as writer recovery.
    """

    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("ledger path must be absolute")
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run ID is invalid")
    raw: bytes | None = None
    for _attempt in range(_READ_SNAPSHOT_ATTEMPTS):
        try:
            observed = path.lstat()
        except FileNotFoundError as exc:
            raise TemperatureLedgerError("LEDGER_OBSERVER_FILE_MISSING") from exc
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_nlink != 1
            or (hasattr(os, "getuid") and observed.st_uid != os.getuid())
            or observed.st_size > MAX_LEDGER_BYTES
        ):
            raise TemperatureLedgerError("LEDGER_OBSERVER_FILE_UNSAFE")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino):
                raise TemperatureLedgerError("LEDGER_OBSERVER_PATH_CHANGED")
            candidate = os.pread(descriptor, before.st_size, 0)
            after = os.fstat(descriptor)
            try:
                rebound = path.lstat()
            except FileNotFoundError as exc:
                raise TemperatureLedgerError("LEDGER_OBSERVER_PATH_CHANGED") from exc
            if (
                (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                or (rebound.st_dev, rebound.st_ino) != (before.st_dev, before.st_ino)
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o600
            ):
                raise TemperatureLedgerError("LEDGER_OBSERVER_PATH_CHANGED")
            if after.st_size != before.st_size or len(candidate) != before.st_size:
                continue
            raw = candidate
            break
        finally:
            os.close(descriptor)
    if raw is None:
        raise TemperatureLedgerError("LEDGER_OBSERVER_SNAPSHOT_UNSTABLE")

    events = _decode_events(raw)
    validator = object.__new__(TemperatureExperimentLedgerV1)
    validator._run_id = run_id
    validator._validate_transitions(events)
    kind_counts: dict[str, int] = {}
    claimed: set[str] = set()
    closed: set[str] = set()
    for event in events:
        kind = str(event["kind"])
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if kind == "CALL_CLAIMED":
            claimed.add(str(event["payload"]["call_id"]))
        elif kind in {
            "CALL_NO_COMPLETION_FAILURE",
            "CALL_REJECTED_COMPLETION",
            "CALL_ACCEPTED",
        }:
            closed.add(str(event["payload"]["call_id"]))
    return {
        "schema_version": LEDGER_PROGRESS_SCHEMA,
        "sequence": len(events),
        "head_sha256": GENESIS_DIGEST if not events else str(events[-1]["event_sha256"]),
        "kind_counts": dict(sorted(kind_counts.items())),
        "open_claim_count": len(claimed - closed),
    }


def _decode_events(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise TemperatureLedgerError("LEDGER_PARTIAL_RECORD")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if len(line) + 1 > MAX_EVENT_BYTES:
            raise TemperatureLedgerError("LEDGER_EVENT_TOO_LARGE")
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeError, json.JSONDecodeError, TemperatureLedgerError) as exc:
            raise TemperatureLedgerError("LEDGER_RECORD_MALFORMED") from exc
        if type(value) is not dict:
            raise TemperatureLedgerError("LEDGER_RECORD_INVALID")
        events.append(value)
    return events


def _validate_protocol_state_machine(
    events: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> None:
    """Validate the formal phase FSM when a run-creation event is present.

    Low-level transport unit tests may use a call-only ledger.  A formal ledger
    switches irreversibly into the closed protocol on ``RUN_CREATED``; from
    that point claims, closures, Test order and aggregate proof fields are all
    phase-bound and cannot fall back to the call-only structural mode.
    """

    if not any(event.get("kind") == "RUN_CREATED" for event in events):
        if any(event.get("kind") in _PROTOCOL_KINDS for event in events):
            raise TemperatureLedgerError("LEDGER_PROTOCOL_RUN_CREATION_MISSING")
        return
    semantic_index = 0
    claims: dict[str, dict[str, Any]] = {}
    accepted: set[str] = set()
    evaluated: set[str] = set()
    for event in events:
        kind = str(event["kind"])
        payload = event["payload"]
        if kind == "CALL_CLAIMED":
            if semantic_index < 2:
                raise TemperatureLedgerError("LEDGER_PROTOCOL_CALL_BEFORE_SPLIT")
            _validate_protocol_claim(payload, semantic_index=semantic_index)
            claims[str(payload["logical_call_id"])] = payload
        elif kind == "CALL_ACCEPTED":
            accepted.add(str(payload["logical_call_id"]))
        elif kind == "CALL_EVALUATED":
            evaluated.add(str(payload["logical_call_id"]))
        if kind not in _PROTOCOL_KINDS:
            continue
        if semantic_index >= len(_PROTOCOL_SEQUENCE):
            raise TemperatureLedgerError("LEDGER_PROTOCOL_EVENT_OVERFLOW")
        expected_kind, batch_index, target_id = _PROTOCOL_SEQUENCE[semantic_index]
        if kind != expected_kind:
            raise TemperatureLedgerError("LEDGER_PROTOCOL_EVENT_OUT_OF_ORDER")
        _validate_protocol_boundary_payload(
            kind=kind,
            payload=payload,
            batch_index=batch_index,
            target_id=target_id,
            claims=claims,
            accepted=accepted,
            evaluated=evaluated,
        )
        semantic_index += 1


def _validate_protocol_claim(payload: dict[str, Any], *, semantic_index: int) -> None:
    expected_kind, batch_index, _target = _PROTOCOL_SEQUENCE[semantic_index]
    phase = payload.get("phase")
    logical = str(payload.get("logical_call_id"))
    actual_batch = _logical_batch(logical)
    expected_phase: str | None
    if expected_kind == "BATCH_PRE_CLOSED":
        expected_phase = "train-pre"
    elif expected_kind == "REFLECTOR_ACCEPTED":
        expected_phase = "train_reflector"
    elif expected_kind == "BATCH_POST_CLOSED":
        expected_phase = "train-post"
    elif expected_kind == "EVOLVED_TEST_CLOSED":
        expected_phase = "evolved-test"
    elif expected_kind == "BASELINE_TEST_CLOSED":
        expected_phase = "baseline-test"
    else:
        expected_phase = None
    expected_batch = batch_index if expected_phase and expected_phase.startswith("train") else None
    if phase != expected_phase or actual_batch != expected_batch:
        raise TemperatureLedgerError("LEDGER_PROTOCOL_CALL_PHASE_INVALID")


def _validate_protocol_boundary_payload(
    *,
    kind: str,
    payload: dict[str, Any],
    batch_index: int | None,
    target_id: str | None,
    claims: dict[str, dict[str, Any]],
    accepted: set[str],
    evaluated: set[str],
) -> None:
    if batch_index is not None and payload.get("batch_index") != batch_index:
        raise TemperatureLedgerError("LEDGER_PROTOCOL_BATCH_IDENTITY_INVALID")
    if target_id is not None and payload.get("target_id") != target_id:
        raise TemperatureLedgerError("LEDGER_PROTOCOL_TARGET_IDENTITY_INVALID")
    if kind == "RUN_CREATED":
        if (
            payload.get("generation_zero") is not True
            or payload.get("old_artifact_imported") is not False
            or payload.get("old_completion_imported") is not False
            or payload.get("old_database_imported") is not False
        ):
            raise TemperatureLedgerError("LEDGER_RUN_CREATION_PROOF_INVALID")
        return
    if kind == "SPLIT_FROZEN":
        if (
            _SHA256.fullmatch(str(payload.get("split_sha256"))) is None
            or payload.get("train_count") != 100
            or payload.get("test_count") != 100
            or payload.get("batch_size") != 25
            or payload.get("test_sealed") is not True
        ):
            raise TemperatureLedgerError("LEDGER_SPLIT_FREEZE_PROOF_INVALID")
        return
    if kind in {"BATCH_PRE_CLOSED", "BATCH_POST_CLOSED"}:
        phase = "train-pre" if kind == "BATCH_PRE_CLOSED" else "train-post"
        count = _evaluated_count(
            claims=claims,
            evaluated=evaluated,
            phase=phase,
            batch_index=batch_index,
        )
        if (
            count != 25
            or payload.get("accepted_count") != 25
            or payload.get("evaluated_count") != 25
        ):
            raise TemperatureLedgerError("LEDGER_BATCH_CLOSURE_PROOF_INVALID")
        return
    if kind == "REFLECTOR_ACCEPTED":
        logical = payload.get("logical_call_id")
        if (
            type(logical) is not str
            or logical not in accepted
            or logical not in claims
            or claims[logical].get("phase") != "train_reflector"
            or _logical_batch(logical) != batch_index
            or payload.get("accepted_synthesis_count_for_batch") != 1
        ):
            raise TemperatureLedgerError("LEDGER_REFLECTOR_CLOSURE_PROOF_INVALID")
        return
    if kind == "EVIDENCE_VALIDATED":
        if _SHA256.fullmatch(str(payload.get("evidence_sha256"))) is None:
            raise TemperatureLedgerError("LEDGER_EVIDENCE_PROOF_INVALID")
        return
    if kind == "TARGET_JOB_COMPLETED":
        if (
            type(payload.get("job_id")) is not str
            or not payload["job_id"]
            or _SHA256.fullmatch(str(payload.get("execution_receipt_sha256"))) is None
        ):
            raise TemperatureLedgerError("LEDGER_CORE_JOB_PROOF_INVALID")
        return
    if kind == "BATCH_ARTIFACT_SET_COMMITTED":
        if (
            _SHA256.fullmatch(str(payload.get("state_sha256"))) is None
            or payload.get("artifact_count") != 3
            or payload.get("core_job_count") != 3
        ):
            raise TemperatureLedgerError("LEDGER_ARTIFACT_COMMIT_PROOF_INVALID")
        return
    if kind == "FINAL_STATE_FROZEN":
        if (
            _SHA256.fullmatch(str(payload.get("state_sha256"))) is None
            or payload.get("artifact_count") != 3
            or payload.get("feedback_disabled") is not True
            or payload.get("test_sealed") is not True
            or not _valid_frozen_context_targets(payload)
        ):
            raise TemperatureLedgerError("LEDGER_FINAL_FREEZE_PROOF_INVALID")
        return
    if kind in {"EVOLVED_TEST_CLOSED", "BASELINE_TEST_CLOSED"}:
        phase = "evolved-test" if kind == "EVOLVED_TEST_CLOSED" else "baseline-test"
        if (
            _evaluated_count(
                claims=claims,
                evaluated=evaluated,
                phase=phase,
                batch_index=None,
            )
            != 100
            or payload.get("accepted_count") != 100
            or payload.get("evaluated_count") != 100
            or payload.get("feedback_disabled") is not True
            or payload.get("reflector_calls") != 0
            or payload.get("core_jobs") != 0
            or payload.get("artifact_updates") != 0
            or (kind == "BASELINE_TEST_CLOSED" and payload.get("context_empty") is not True)
        ):
            raise TemperatureLedgerError("LEDGER_TEST_CLOSURE_PROOF_INVALID")
        return
    if kind == "AUDIT_CLOSED" and payload.get("checksums_verified") is not True:
        raise TemperatureLedgerError("LEDGER_AUDIT_CLOSURE_PROOF_INVALID")


def _logical_batch(logical_call_id: str) -> int | None:
    match = re.search(r"(?:^|-)b(0[1-4])(?:-|$)", logical_call_id, re.ASCII)
    return None if match is None else int(match.group(1))


def _valid_frozen_context_targets(payload: dict[str, Any]) -> bool:
    values = payload.get("frozen_context_targets")
    expected_digest = payload.get("frozen_context_targets_sha256")
    required = {
        "target_id",
        "core_artifact_id",
        "artifact_payload_sha256",
        "resolved_content_sha256",
        "context_resolution_digest",
    }
    if (
        type(values) is not list
        or len(values) != len(_TARGETS)
        or tuple(
            value.get("target_id") if type(value) is dict else None
            for value in values
        )
        != _TARGETS
        or _SHA256.fullmatch(str(expected_digest)) is None
        or hashlib.sha256(canonical_json_bytes(values)).hexdigest() != expected_digest
    ):
        return False
    resolution_digests: set[str] = set()
    for value in values:
        if (
            type(value) is not dict
            or set(value) != required
            or _ARTIFACT_ID.fullmatch(str(value.get("core_artifact_id"))) is None
            or any(
                _SHA256.fullmatch(str(value.get(field))) is None
                for field in (
                    "artifact_payload_sha256",
                    "resolved_content_sha256",
                    "context_resolution_digest",
                )
            )
        ):
            return False
        resolution_digests.add(str(value["context_resolution_digest"]))
    return len(resolution_digests) == 1


def _evaluated_count(
    *,
    claims: dict[str, dict[str, Any]],
    evaluated: set[str],
    phase: str,
    batch_index: int | None,
) -> int:
    return sum(
        logical in claims
        and claims[logical].get("phase") == phase
        and _logical_batch(logical) == batch_index
        for logical in evaluated
    )


def _validate_reflector_retry_exhausted_failure(
    *,
    payload: dict[str, Any],
    claims_by_logical: dict[str, list[dict[str, Any]]],
    no_completion: set[str],
    rejected_completion: set[str],
    accepted: dict[str, dict[str, Any]],
) -> None:
    required = {
        "failure_code",
        "phase",
        "batch_index",
        "attempt_count",
        "rejected_completion_attempt_count",
        "no_completion_attempt_count",
    }
    batch_index = payload.get("batch_index")
    matching = [
        (logical, attempts)
        for logical, attempts in claims_by_logical.items()
        if attempts
        and attempts[-1].get("phase") == "train_reflector"
        and _logical_batch(logical) == batch_index
    ]
    if len(matching) != 1:
        raise TemperatureLedgerError("LEDGER_RUN_FAILED_INVALID")
    logical, attempts = matching[0]
    call_ids = {str(attempt.get("call_id")) for attempt in attempts}
    rejected_count = len(call_ids & rejected_completion)
    no_completion_count = len(call_ids & no_completion)
    if (
        set(payload) != required
        or payload.get("failure_code") != REFLECTOR_ATTEMPT_RETRY_EXHAUSTED
        or payload.get("phase") != "train_reflector"
        or type(batch_index) is not int
        or not 1 <= batch_index <= 4
        or payload.get("attempt_count") != 3
        or payload.get("rejected_completion_attempt_count") != rejected_count
        or payload.get("no_completion_attempt_count") != no_completion_count
        or rejected_count + no_completion_count != 3
        or len(attempts) != 3
        or tuple(attempt.get("attempt_number") for attempt in attempts) != (1, 2, 3)
        or any(
            attempt.get("phase") != "train_reflector"
            or attempt.get("logical_arm") != "batch_supervised_reflector"
            for attempt in attempts
        )
        or logical in accepted
    ):
        raise TemperatureLedgerError("LEDGER_RUN_FAILED_INVALID")


def _validate_claim(payload: dict[str, Any]) -> tuple[str, str]:
    required = {
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
    logical, call_id = _validate_call_reference(payload)
    if (
        set(payload) != required
        or _RUNTIME_ID.fullmatch(str(payload.get("task_id"))) is None
        or _RUNTIME_ID.fullmatch(str(payload.get("phase"))) is None
        or _RUNTIME_ID.fullmatch(str(payload.get("logical_arm"))) is None
        or type(payload.get("attempt_number")) is not int
        or payload["attempt_number"] < 1
        or (
            payload.get("phase") == "train_reflector"
            and payload["attempt_number"] > 3
        )
        or _SHA256.fullmatch(str(payload.get("task_request_sha256"))) is None
        or _SHA256.fullmatch(str(payload.get("retry_semantics_sha256"))) is None
        or _SHA256.fullmatch(str(payload.get("service_identity_sha256"))) is None
    ):
        raise TemperatureLedgerError("LEDGER_CALL_CLAIM_INVALID")
    return logical, call_id


def _validate_call_reference(payload: dict[str, Any]) -> tuple[str, str]:
    logical = payload.get("logical_call_id")
    call_id = payload.get("call_id")
    if (
        type(logical) is not str
        or _RUNTIME_ID.fullmatch(logical) is None
        or type(call_id) is not str
        or _RUNTIME_ID.fullmatch(call_id) is None
    ):
        raise TemperatureLedgerError("LEDGER_CALL_REFERENCE_INVALID")
    return logical, call_id


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TemperatureLedgerError("LEDGER_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise TemperatureLedgerError(f"LEDGER_NONFINITE_JSON:{value}")


def _is_canonical_json(value: str) -> bool:
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, TemperatureLedgerError):
        return False
    return canonical_json_bytes(decoded) == value.encode("utf-8")


__all__ = [
    "GENESIS_DIGEST",
    "LEDGER_PROGRESS_SCHEMA",
    "LEDGER_SCHEMA",
    "MAX_EVENT_BYTES",
    "MAX_LEDGER_BYTES",
    "REFLECTOR_REJECTED_COMPLETION_CODES",
    "TemperatureExperimentLedgerV1",
    "TemperatureLedgerError",
    "read_validated_ledger_progress_v1",
]
