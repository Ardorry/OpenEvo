"""Append-only one-completion-per-Test-item ledger for v2."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)


class FinalTestLedgerError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class FinalTestAttemptV2:
    task_uid: str
    arm: Literal["control", "evolved"]
    attempt_id: str
    completion_exists: bool
    completion_sha256: str | None
    source_commit: str
    artifact_set_digest: str
    config_digest: str
    model_digest: str
    timestamp: str

    def __post_init__(self) -> None:
        for value in (
            self.task_uid,
            self.source_commit,
            self.artifact_set_digest,
            self.config_digest,
            self.model_digest,
        ):
            if _SHA256.fullmatch(value) is None and not (
                value == self.source_commit and re.fullmatch(r"[0-9a-f]{40}", value)
            ):
                raise ValueError("Test ledger digest identity is invalid")
        if _ID.fullmatch(self.attempt_id) is None:
            raise ValueError("Test ledger attempt ID is invalid")
        if self.completion_exists != (self.completion_sha256 is not None):
            raise ValueError("Test completion flag and digest differ")
        if (
            self.completion_sha256 is not None
            and _SHA256.fullmatch(self.completion_sha256) is None
        ):
            raise ValueError("Test completion digest is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "FinalTestConsumptionLedgerEntryV2",
            "task_uid": self.task_uid,
            "arm": self.arm,
            "attempt_id": self.attempt_id,
            "completion_exists": self.completion_exists,
            "completion_sha256": self.completion_sha256,
            "source_commit": self.source_commit,
            "artifact_set_digest": self.artifact_set_digest,
            "config_digest": self.config_digest,
            "model_digest": self.model_digest,
            "timestamp": self.timestamp,
        }


class FinalTestConsumptionLedgerV2:
    """Authority that never permits a second call after a valid completion."""

    def __init__(
        self,
        *,
        path: Path,
        source_commit: str,
        artifact_set_digest: str,
        config_digest: str,
        model_digest: str,
    ) -> None:
        if not path.is_absolute():
            raise TypeError("Test ledger path must be absolute")
        self.path = path
        self.identity = (
            source_commit,
            artifact_set_digest,
            config_digest,
            model_digest,
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise FinalTestLedgerError("TEST_LEDGER_FILE_UNSAFE")
        self._entries = self._load()

    def claim(
        self,
        *,
        task_uid: str,
        arm: Literal["control", "evolved"],
        attempt_id: str,
        timestamp: str,
    ) -> FinalTestAttemptV2:
        key = (task_uid, arm)
        prior = self._entries.get(key, [])
        if any(item.completion_exists for item in prior):
            raise FinalTestLedgerError("TEST_ITEM_ALREADY_COMPLETED")
        if any(item.attempt_id == attempt_id for item in prior):
            raise FinalTestLedgerError("TEST_ATTEMPT_ID_REUSED")
        entry = FinalTestAttemptV2(
            task_uid=task_uid,
            arm=arm,
            attempt_id=attempt_id,
            completion_exists=False,
            completion_sha256=None,
            source_commit=self.identity[0],
            artifact_set_digest=self.identity[1],
            config_digest=self.identity[2],
            model_digest=self.identity[3],
            timestamp=timestamp,
        )
        self._append(entry)
        return entry

    def complete(
        self,
        *,
        task_uid: str,
        arm: Literal["control", "evolved"],
        attempt_id: str,
        completion: str,
        timestamp: str,
    ) -> FinalTestAttemptV2:
        return self.complete_digest(
            task_uid=task_uid,
            arm=arm,
            attempt_id=attempt_id,
            completion_sha256=hashlib.sha256(completion.encode("utf-8")).hexdigest(),
            timestamp=timestamp,
        )

    def complete_digest(
        self,
        *,
        task_uid: str,
        arm: Literal["control", "evolved"],
        attempt_id: str,
        completion_sha256: str,
        timestamp: str,
    ) -> FinalTestAttemptV2:
        """Seal an item when a failed execution still produced a completion."""

        if _SHA256.fullmatch(completion_sha256) is None:
            raise ValueError("Test completion digest is invalid")
        key = (task_uid, arm)
        prior = self._entries.get(key, [])
        if not prior or prior[-1].attempt_id != attempt_id or prior[-1].completion_exists:
            raise FinalTestLedgerError("TEST_COMPLETION_WITHOUT_ACTIVE_ATTEMPT")
        if any(item.completion_exists for item in prior):
            raise FinalTestLedgerError("TEST_ITEM_ALREADY_COMPLETED")
        entry = FinalTestAttemptV2(
            task_uid=task_uid,
            arm=arm,
            attempt_id=attempt_id,
            completion_exists=True,
            completion_sha256=completion_sha256,
            source_commit=self.identity[0],
            artifact_set_digest=self.identity[1],
            config_digest=self.identity[2],
            model_digest=self.identity[3],
            timestamp=timestamp,
        )
        self._append(entry)
        return entry

    def completed(self, task_uid: str, arm: Literal["control", "evolved"]) -> bool:
        return any(item.completion_exists for item in self._entries.get((task_uid, arm), ()))

    def summary(self) -> dict[str, object]:
        entries = [item for values in self._entries.values() for item in values]
        return {
            "schema_version": "FinalTestConsumptionLedgerSummaryV2",
            "attempt_count": sum(not item.completion_exists for item in entries),
            "completion_count": sum(item.completion_exists for item in entries),
            "control_completion_count": sum(
                item.completion_exists and item.arm == "control" for item in entries
            ),
            "evolved_completion_count": sum(
                item.completion_exists and item.arm == "evolved" for item in entries
            ),
            "ledger_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }

    def _load(self) -> dict[tuple[str, str], list[FinalTestAttemptV2]]:
        result: dict[tuple[str, str], list[FinalTestAttemptV2]] = {}
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(raw)
            if (
                type(payload) is not dict
                or set(payload)
                != {
                    "schema_version",
                    "task_uid",
                    "arm",
                    "attempt_id",
                    "completion_exists",
                    "completion_sha256",
                    "source_commit",
                    "artifact_set_digest",
                    "config_digest",
                    "model_digest",
                    "timestamp",
                }
                or payload["schema_version"] != "FinalTestConsumptionLedgerEntryV2"
            ):
                raise FinalTestLedgerError("TEST_LEDGER_SCHEMA_INVALID")
            entry = FinalTestAttemptV2(
                **{key: value for key, value in payload.items() if key != "schema_version"}
            )
            if (
                entry.source_commit,
                entry.artifact_set_digest,
                entry.config_digest,
                entry.model_digest,
            ) != self.identity:
                raise FinalTestLedgerError("TEST_LEDGER_IDENTITY_MISMATCH")
            values = result.setdefault((entry.task_uid, entry.arm), [])
            if entry.completion_exists and (
                not values
                or values[-1].attempt_id != entry.attempt_id
                or values[-1].completion_exists
            ):
                raise FinalTestLedgerError("TEST_LEDGER_SEQUENCE_INVALID")
            if not entry.completion_exists and any(value.completion_exists for value in values):
                raise FinalTestLedgerError("TEST_LEDGER_SEQUENCE_INVALID")
            values.append(entry)
        return result

    def _append(self, entry: FinalTestAttemptV2) -> None:
        encoded = canonical_json_bytes(entry.to_payload())
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(descriptor, "ab", buffering=0) as stream:
            stream.write(encoded)
            os.fsync(stream.fileno())
        self._entries.setdefault((entry.task_uid, entry.arm), []).append(entry)


__all__ = [
    "FinalTestAttemptV2",
    "FinalTestConsumptionLedgerV2",
    "FinalTestLedgerError",
]
