"""Append-only one-completion-per-Test-item ledger for evolved-only v3."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)


class FinalTestLedgerV3Error(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class FinalTestAttemptV3:
    task_uid: str
    attempt_id: str
    completion_exists: bool
    completion_sha256: str | None
    source_commit: str
    artifact_set_digest: str
    config_digest: str
    model_digest: str
    timestamp: str

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.task_uid) is None:
            raise ValueError("Test task UID is invalid")
        if _ID.fullmatch(self.attempt_id) is None:
            raise ValueError("Test attempt ID is invalid")
        if _COMMIT.fullmatch(self.source_commit) is None:
            raise ValueError("Test source commit is invalid")
        if any(
            _SHA256.fullmatch(value) is None
            for value in (self.artifact_set_digest, self.config_digest, self.model_digest)
        ):
            raise ValueError("Test identity digest is invalid")
        if self.completion_exists != (self.completion_sha256 is not None):
            raise ValueError("Test completion flag and digest differ")
        if self.completion_sha256 is not None and _SHA256.fullmatch(self.completion_sha256) is None:
            raise ValueError("Test completion digest is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "FinalTestConsumptionLedgerEntryV3",
            "arm": "evolved",
            "task_uid": self.task_uid,
            "attempt_id": self.attempt_id,
            "completion_exists": self.completion_exists,
            "completion_sha256": self.completion_sha256,
            "source_commit": self.source_commit,
            "artifact_set_digest": self.artifact_set_digest,
            "config_digest": self.config_digest,
            "model_digest": self.model_digest,
            "timestamp": self.timestamp,
        }


class FinalTestConsumptionLedgerV3:
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
        self.identity = (source_commit, artifact_set_digest, config_digest, model_digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise FinalTestLedgerV3Error("TEST_LEDGER_FILE_UNSAFE")
        self._entries = self._load()

    def claim(self, *, task_uid: str, attempt_id: str, timestamp: str) -> FinalTestAttemptV3:
        prior = self._entries.get(task_uid, [])
        if any(item.completion_exists for item in prior):
            raise FinalTestLedgerV3Error("TEST_ITEM_ALREADY_COMPLETED")
        if any(item.attempt_id == attempt_id for item in prior):
            raise FinalTestLedgerV3Error("TEST_ATTEMPT_ID_REUSED")
        entry = FinalTestAttemptV3(
            task_uid=task_uid,
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
        attempt_id: str,
        completion: str,
        timestamp: str,
    ) -> FinalTestAttemptV3:
        return self.complete_digest(
            task_uid=task_uid,
            attempt_id=attempt_id,
            completion_sha256=hashlib.sha256(completion.encode("utf-8")).hexdigest(),
            timestamp=timestamp,
        )

    def complete_digest(
        self,
        *,
        task_uid: str,
        attempt_id: str,
        completion_sha256: str,
        timestamp: str,
    ) -> FinalTestAttemptV3:
        if _SHA256.fullmatch(completion_sha256) is None:
            raise ValueError("Test completion digest is invalid")
        prior = self._entries.get(task_uid, [])
        if not prior or prior[-1].attempt_id != attempt_id or prior[-1].completion_exists:
            raise FinalTestLedgerV3Error("TEST_COMPLETION_WITHOUT_ACTIVE_ATTEMPT")
        if any(item.completion_exists for item in prior):
            raise FinalTestLedgerV3Error("TEST_ITEM_ALREADY_COMPLETED")
        entry = FinalTestAttemptV3(
            task_uid=task_uid,
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

    def summary(self) -> dict[str, object]:
        entries = [item for values in self._entries.values() for item in values]
        return {
            "schema_version": "FinalTestConsumptionLedgerSummaryV3",
            "arm": "evolved",
            "attempt_count": sum(not item.completion_exists for item in entries),
            "completion_count": sum(item.completion_exists for item in entries),
            "duplicate_completion_count": 0,
            "ledger_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }

    def _load(self) -> dict[str, list[FinalTestAttemptV3]]:
        result: dict[str, list[FinalTestAttemptV3]] = {}
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            # Historical v3 writers appended a newline to canonical_json_bytes(),
            # which already ends in a newline.  Accept only the resulting empty
            # separator lines so an immutable historical ledger can be reopened.
            if raw == "":
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise FinalTestLedgerV3Error("TEST_LEDGER_SCHEMA_INVALID") from exc
            expected = {
                "schema_version",
                "arm",
                "task_uid",
                "attempt_id",
                "completion_exists",
                "completion_sha256",
                "source_commit",
                "artifact_set_digest",
                "config_digest",
                "model_digest",
                "timestamp",
            }
            if type(payload) is not dict or set(payload) != expected or payload["schema_version"] != "FinalTestConsumptionLedgerEntryV3" or payload["arm"] != "evolved":
                raise FinalTestLedgerV3Error("TEST_LEDGER_SCHEMA_INVALID")
            entry = FinalTestAttemptV3(**{key: value for key, value in payload.items() if key not in {"schema_version", "arm"}})
            if (entry.source_commit, entry.artifact_set_digest, entry.config_digest, entry.model_digest) != self.identity:
                raise FinalTestLedgerV3Error("TEST_LEDGER_IDENTITY_MISMATCH")
            values = result.setdefault(entry.task_uid, [])
            if entry.completion_exists:
                if not values or values[-1].attempt_id != entry.attempt_id or values[-1].completion_exists:
                    raise FinalTestLedgerV3Error("TEST_LEDGER_SEQUENCE_INVALID")
                if any(item.completion_exists for item in values):
                    raise FinalTestLedgerV3Error("TEST_DUPLICATE_COMPLETION")
            elif any(item.attempt_id == entry.attempt_id for item in values):
                raise FinalTestLedgerV3Error("TEST_ATTEMPT_ID_REUSED")
            values.append(entry)
        return result

    def _append(self, entry: FinalTestAttemptV3) -> None:
        encoded = canonical_json_bytes(entry.to_payload())
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "ab", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        self._entries.setdefault(entry.task_uid, []).append(entry)


__all__ = ["FinalTestAttemptV3", "FinalTestConsumptionLedgerV3", "FinalTestLedgerV3Error"]
