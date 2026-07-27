"""Protocol-global, append-only Test-manifest use ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes

if TYPE_CHECKING:
    from openevo_chembench.supervised_transfer_v1.experiment import ExperimentInputsV1


TEST_LEDGER_SCHEMA = "SupervisedTransferTestManifestLedgerEntryV1"
TEST_LEDGER_FILENAME = "test_manifest_use_ledger_v1.jsonl"
TEST_LEDGER_LOCK = ".test_manifest_use_ledger_v1.lock"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)
_MANIFESTS = ("test_primary", "test_recovery_01")


class TestManifestLedgerError(RuntimeError):
    """Closed cross-run Test-use failure."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


def claim_test_manifest_use_v1(
    *,
    inputs: ExperimentInputsV1,
    run_id: str,
    frozen_receipt_sha256: str,
    claimed_at_utc: str,
) -> dict[str, object]:
    _require_run_id(run_id)
    _require_sha256(frozen_receipt_sha256)
    manifest = inputs.test_manifest
    if manifest not in _MANIFESTS:
        raise TestManifestLedgerError("TEST_MANIFEST_NOT_PREREGISTERED")
    root = inputs.repository_root / inputs.config.state_root
    with _locked_ledger(root) as ledger:
        records = _read_ledger(ledger)
        _require_claim_allowed(records, manifest=manifest)
        manifest_path = inputs.manifest_root / f"{manifest}_private_manifest.jsonl"
        payload = {
            "schema_version": TEST_LEDGER_SCHEMA,
            "sequence": len(records) + 1,
            "previous_entry_sha256": (
                None if not records else str(records[-1]["entry_sha256"])
            ),
            "action": "CLAIMED",
            "protocol_id": inputs.config.protocol_id,
            "manifest": manifest,
            "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
            "ordered_uid_sha256": _ordered_uid_sha256(inputs.test),
            "run_id": run_id,
            "source_commit": inputs.source_commit,
            "split_receipt_sha256": inputs.split_receipt_sha256,
            "frozen_transfer_receipt_sha256": frozen_receipt_sha256,
            "reason": (
                "PRIMARY_PREREGISTERED_TEST"
                if manifest == "test_primary"
                else "RECOVERY_AFTER_IMPLEMENTATION_BUG_INVALIDATION"
            ),
            "recorded_at_utc": claimed_at_utc,
        }
        return _append_entry(ledger, records, payload)


def complete_test_manifest_use_v1(
    *,
    inputs: ExperimentInputsV1,
    run_id: str,
    claim_entry_sha256: str,
    completed_at_utc: str,
) -> dict[str, object]:
    _require_run_id(run_id)
    _require_sha256(claim_entry_sha256)
    root = inputs.repository_root / inputs.config.state_root
    with _locked_ledger(root) as ledger:
        records = _read_ledger(ledger)
        claim = next(
            (
                row
                for row in records
                if row.get("entry_sha256") == claim_entry_sha256
                and row.get("action") == "CLAIMED"
            ),
            None,
        )
        if (
            claim is None
            or claim.get("run_id") != run_id
            or claim.get("manifest") != inputs.test_manifest
            or any(
                row.get("claim_entry_sha256") == claim_entry_sha256
                for row in records
                if row.get("action") in {"COMPLETED", "INVALIDATED_BY_IMPLEMENTATION_BUG"}
            )
        ):
            raise TestManifestLedgerError("TEST_MANIFEST_CLAIM_NOT_COMPLETABLE")
        _verify_completed_test_evidence(inputs=inputs, run_id=run_id, claim=claim)
        payload = {
            "schema_version": TEST_LEDGER_SCHEMA,
            "sequence": len(records) + 1,
            "previous_entry_sha256": str(records[-1]["entry_sha256"]),
            "action": "COMPLETED",
            "protocol_id": inputs.config.protocol_id,
            "manifest": inputs.test_manifest,
            "run_id": run_id,
            "source_commit": inputs.source_commit,
            "claim_entry_sha256": claim_entry_sha256,
            "recorded_at_utc": completed_at_utc,
        }
        return _append_entry(ledger, records, payload)


def _verify_completed_test_evidence(
    *,
    inputs: ExperimentInputsV1,
    run_id: str,
    claim: dict[str, Any],
) -> None:
    result_root = (
        inputs.repository_root / inputs.config.result_root / "runs" / run_id
    )
    state_root = inputs.repository_root / inputs.config.state_root / "runs" / run_id
    try:
        state = json.loads(
            (result_root / "public/run_state.json").read_text(encoding="utf-8")
        )
        frozen_raw = (result_root / "public/frozen_transfer_receipt_v1.json").read_bytes()
        use = json.loads(
            (
                result_root
                / "public"
                / f"{inputs.test_manifest}_use_receipt.json"
            ).read_text(encoding="utf-8")
        )
        public = _read_jsonl(result_root / "public/events.jsonl")
        private = _read_jsonl(state_root / "private/events.jsonl")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TestManifestLedgerError("TEST_COMPLETION_EVIDENCE_MISSING") from exc
    public_test = [
        row
        for row in public
        if row.get("kind") == "TASK_MODEL_EXECUTED"
        and row.get("stage") == "FINAL_TEST"
    ]
    private_test = [
        row
        for row in private
        if row.get("kind") == "PRIVATE_EVALUATED" and row.get("stage") == "FINAL_TEST"
    ]
    expected_uids = {str(task.uid) for task in inputs.test}
    public_sessions = {str(row.get("session_id")) for row in public_test}
    private_sessions = {str(row.get("session_id")) for row in private_test}
    by_arm = {
        arm: {str(row.get("task_uid")) for row in public_test if row.get("logical_arm") == arm}
        for arm in ("control_test", "online_test")
    }
    arm_counts = {
        arm: sum(row.get("logical_arm") == arm for row in public_test)
        for arm in ("control_test", "online_test")
    }
    if (
        not isinstance(state, dict)
        or state.get("run_id") != run_id
        or state.get("run_mode") != "formal"
        or state.get("stage") != "FINAL_TEST"
        or state.get("task_sessions") != 4_500
        or state.get("reflector_completions") != 900
        or state.get("core_jobs") != 2_700
        or state.get("core_artifacts") != 2_700
        or len(public_test) != 900
        or len(private_test) != 900
        or len(public_sessions) != 900
        or public_sessions != private_sessions
        or by_arm["control_test"] != expected_uids
        or by_arm["online_test"] != expected_uids
        or arm_counts != {"control_test": 450, "online_test": 450}
        or any(
            row.get("logical_arm") == "control_test"
            and any(
                row.get(field) is not None
                for field in (
                    "memory_artifact_id",
                    "skill_artifact_id",
                    "agent_system_artifact_id",
                )
            )
            for row in public_test
        )
        or any(
            row.get("logical_arm") == "online_test"
            and any(
                row.get(field) is None
                for field in (
                    "memory_artifact_id",
                    "skill_artifact_id",
                    "agent_system_artifact_id",
                )
            )
            for row in public_test
        )
        or any(
            row.get("kind") == "REFLECTOR_SUPERVISED"
            and row.get("stage") == "FINAL_TEST"
            for row in public
        )
        or sha256_bytes(frozen_raw) != claim.get("frozen_transfer_receipt_sha256")
        or use.get("global_ledger_claim_sha256") != claim.get("entry_sha256")
    ):
        raise TestManifestLedgerError("TEST_COMPLETION_EVIDENCE_INVALID")


def invalidate_test_manifest_after_source_bug_v1(
    *,
    repository_root: Path,
    state_root_relative: str,
    result_root_relative: str,
    failed_run_id: str,
    current_source_commit: str,
    recorded_at_utc: str,
) -> dict[str, object]:
    """Invalidate one claimed Test only from persisted failure and Git evidence."""

    _require_run_id(failed_run_id)
    _require_commit(current_source_commit)
    repository = repository_root.resolve(strict=True)
    result_root = repository / result_root_relative / "runs" / failed_run_id
    try:
        run_state = json.loads(
            (result_root / "public/run_state.json").read_text(encoding="utf-8")
        )
        use_receipt = json.loads(
            next((result_root / "public").glob("test_*_use_receipt.json")).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, StopIteration, UnicodeError, json.JSONDecodeError) as exc:
        raise TestManifestLedgerError("TEST_INVALIDATION_EVIDENCE_MISSING") from exc
    old_source = run_state.get("source_commit")
    manifest = use_receipt.get("manifest")
    if (
        run_state.get("status") != "FAIL_CLOSED"
        or run_state.get("stage") != "FINAL_TEST"
        or type(old_source) is not str
        or re.fullmatch(r"[0-9a-f]{40}", old_source) is None
        or old_source == current_source_commit
        or manifest not in _MANIFESTS
        or use_receipt.get("reason")
        not in {
            "PRIMARY_PREREGISTERED_TEST",
            "RECOVERY_AFTER_IMPLEMENTATION_BUG_INVALIDATION",
        }
    ):
        raise TestManifestLedgerError("TEST_INVALIDATION_EVIDENCE_INVALID")
    ancestor = _git(repository, "merge-base", "--is-ancestor", old_source, current_source_commit)
    if ancestor != "":
        # merge-base --is-ancestor emits no stdout on success; _git raises on failure.
        raise AssertionError("unexpected merge-base output")
    root = repository / state_root_relative
    with _locked_ledger(root) as ledger:
        records = _read_ledger(ledger)
        claims = [
            row
            for row in records
            if row.get("action") == "CLAIMED"
            and row.get("run_id") == failed_run_id
            and row.get("manifest") == manifest
        ]
        if len(claims) != 1:
            raise TestManifestLedgerError("TEST_INVALIDATION_CLAIM_INVALID")
        claim = claims[0]
        claim_digest = str(claim["entry_sha256"])
        if any(
            row.get("claim_entry_sha256") == claim_digest
            for row in records
            if row.get("action") in {"COMPLETED", "INVALIDATED_BY_IMPLEMENTATION_BUG"}
        ):
            raise TestManifestLedgerError("TEST_INVALIDATION_CLAIM_TERMINAL")
        payload = {
            "schema_version": TEST_LEDGER_SCHEMA,
            "sequence": len(records) + 1,
            "previous_entry_sha256": str(records[-1]["entry_sha256"]),
            "action": "INVALIDATED_BY_IMPLEMENTATION_BUG",
            "protocol_id": "chembench_supervised_transfer_v1",
            "manifest": manifest,
            "run_id": failed_run_id,
            "source_commit": old_source,
            "replacement_source_commit": current_source_commit,
            "claim_entry_sha256": claim_digest,
            "reason": "SOURCE_CHANGE_AFTER_FINAL_TEST_IMPLEMENTATION_BUG",
            "recorded_at_utc": recorded_at_utc,
        }
        return _append_entry(ledger, records, payload)


def _require_claim_allowed(records: list[dict[str, Any]], *, manifest: str) -> None:
    if any(row.get("action") == "CLAIMED" and row.get("manifest") == manifest for row in records):
        raise TestManifestLedgerError("TEST_MANIFEST_ALREADY_CLAIMED")
    if manifest == "test_primary":
        if any(row.get("action") == "CLAIMED" for row in records):
            raise TestManifestLedgerError("TEST_PRIMARY_NO_LONGER_AVAILABLE")
        return
    primary_claims = [
        row
        for row in records
        if row.get("action") == "CLAIMED" and row.get("manifest") == "test_primary"
    ]
    if len(primary_claims) != 1:
        raise TestManifestLedgerError("RECOVERY_TEST_NOT_AUTHORIZED")
    primary_digest = primary_claims[0]["entry_sha256"]
    if not any(
        row.get("action") == "INVALIDATED_BY_IMPLEMENTATION_BUG"
        and row.get("claim_entry_sha256") == primary_digest
        for row in records
    ):
        raise TestManifestLedgerError("RECOVERY_TEST_NOT_AUTHORIZED")


@contextmanager
def _locked_ledger(root: Path) -> Iterator[Path]:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    lock = root / TEST_LEDGER_LOCK
    descriptor = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ledger = root / TEST_LEDGER_FILENAME
        if ledger.exists():
            metadata = ledger.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
            ):
                raise TestManifestLedgerError("TEST_LEDGER_FILE_UNSAFE")
        yield ledger
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TestManifestLedgerError("TEST_LEDGER_CORRUPT") from exc
    previous: str | None = None
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TestManifestLedgerError("TEST_LEDGER_CORRUPT")
        claimed_digest = row.get("entry_sha256")
        payload = {key: value for key, value in row.items() if key != "entry_sha256"}
        if (
            row.get("schema_version") != TEST_LEDGER_SCHEMA
            or row.get("sequence") != index
            or row.get("previous_entry_sha256") != previous
            or type(claimed_digest) is not str
            or sha256_bytes(canonical_json_bytes(payload)) != claimed_digest
        ):
            raise TestManifestLedgerError("TEST_LEDGER_CORRUPT")
        previous = claimed_digest
    return rows


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if any(not isinstance(row, dict) for row in rows):
        raise TestManifestLedgerError("TEST_COMPLETION_EVIDENCE_INVALID")
    return rows


def _append_entry(
    path: Path,
    records: list[dict[str, Any]],
    payload: dict[str, object],
) -> dict[str, object]:
    entry = {**payload, "entry_sha256": sha256_bytes(canonical_json_bytes(payload))}
    expected_sequence = len(records) + 1
    if entry["sequence"] != expected_sequence:
        raise TestManifestLedgerError("TEST_LEDGER_SEQUENCE_INVALID")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise TestManifestLedgerError("TEST_LEDGER_FILE_UNSAFE")
        with os.fdopen(descriptor, "ab", buffering=0) as stream:
            descriptor = -1
            stream.write(canonical_json_bytes(entry))
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _read_ledger(path)[-1] != entry:
        raise TestManifestLedgerError("TEST_LEDGER_READBACK_FAILED")
    return entry


def _ordered_uid_sha256(tasks: tuple[Any, ...]) -> str:
    return hashlib.sha256(
        ("\n".join(str(task.uid) for task in tasks) + "\n").encode("ascii")
    ).hexdigest()


def _require_sha256(value: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("digest must be SHA-256")


def _require_commit(value: str) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("source commit must be a Git commit")


def _require_run_id(value: str) -> None:
    if type(value) is not str or _RUN_ID_RE.fullmatch(value) is None:
        raise ValueError("run_id is invalid")


def _git(repository: Path, *arguments: str) -> str:
    import subprocess

    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise TestManifestLedgerError("TEST_INVALIDATION_GIT_EVIDENCE_INVALID")
    return completed.stdout.strip()


__all__ = [
    "TEST_LEDGER_FILENAME",
    "TEST_LEDGER_SCHEMA",
    "TestManifestLedgerError",
    "claim_test_manifest_use_v1",
    "complete_test_manifest_use_v1",
    "invalidate_test_manifest_after_source_bug_v1",
]
