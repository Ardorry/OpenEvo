"""Read-only recovery and proof of the exact Full-Evolve V1 C4 artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.temperature_full_evolve_v1.core_evolution import CoreEvolutionStateV1
from openevo_chembench.temperature_safe_evolve_v2.config import FROZEN_C4

PRIOR_RUN_ID = "stv3-temperature-full-evolve-v1-20260802T191250Z"
PRIOR_RUN_ROOT_RELATIVE = f"state/chembench_temperature_full_evolve_v1/runs/{PRIOR_RUN_ID}"
PRIOR_PREFLIGHT_RELATIVE = (
    "state/chembench_temperature_full_evolve_v1/preflights/preflight-20260802T190753Z-b625a8ce"
)
EXPECTED_FINAL_MANIFEST_SHA256 = "39eea4b9295d8d40c798d376c5a434f6c99661a5beb00d9fb699d82bda2da1ee"
EXPECTED_C4_CHECKPOINT_SHA256 = "8ef9bf908e18fa94113acf302ecad4705aa2f26e6cc957a6ab787c4a0c1c7957"
EXPECTED_C4_STATE_SHA256 = "a3c7ed81b84671533f26ebd78cf91980025ea385f3e6abbf81f87ebccfaafabc"
TARGET_ORDER = ("text_memory", "skill_bundle", "agent_system")


class FrozenC4Error(RuntimeError):
    """Content-free frozen evidence finding."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class FrozenC4ArtifactV2:
    target_id: str
    artifact_id: str
    job_id: str
    payload: bytes
    payload_sha256: str
    utf8_bytes: int
    source_path: Path
    source_uri: str
    manifest_sha256: str
    lineage_sha256: str

    def __repr__(self) -> str:
        return f"FrozenC4ArtifactV2(target_id={self.target_id!r}, <payload-redacted>)"

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8")


@dataclass(frozen=True, slots=True, repr=False)
class FrozenC4BundleV2:
    run_root: Path
    preflight_root: Path
    core_db_path: Path
    checkpoint_path: Path
    checkpoint_sha256: str
    state_sha256: str
    artifacts: tuple[FrozenC4ArtifactV2, ...]
    combined_context_sha256: str
    combined_context_bytes: int
    final_manifest_sha256: str
    database_integrity: str

    def __repr__(self) -> str:
        return "FrozenC4BundleV2(<owner-private-evidence-redacted>)"

    def artifact(self, target_id: str) -> FrozenC4ArtifactV2:
        try:
            return next(value for value in self.artifacts if value.target_id == target_id)
        except StopIteration as exc:
            raise KeyError(target_id) from exc

    def public_receipt(self) -> dict[str, object]:
        return {
            "schema_version": "TemperatureSafeEvolveFrozenC4ReceiptV2",
            "status": "PASS",
            "prior_run_id": PRIOR_RUN_ID,
            "checkpoint_sha256": self.checkpoint_sha256,
            "state_sha256": self.state_sha256,
            "database_integrity": self.database_integrity,
            "artifacts": [
                {
                    "target_id": artifact.target_id,
                    "artifact_id": artifact.artifact_id,
                    "job_id": artifact.job_id,
                    "utf8_bytes": artifact.utf8_bytes,
                    "sha256": artifact.payload_sha256,
                    "manifest_sha256": artifact.manifest_sha256,
                    "lineage_sha256": artifact.lineage_sha256,
                }
                for artifact in self.artifacts
            ],
            "combined_context_bytes": self.combined_context_bytes,
            "combined_context_sha256": self.combined_context_sha256,
            "final_artifact_manifest_sha256": self.final_manifest_sha256,
            "payloads_recovered_from_formal_store": True,
            "payloads_reconstructed_from_report": False,
        }


def recover_frozen_c4_v2(repository_root: Path) -> FrozenC4BundleV2:
    """Recover exact C4 bytes through checkpoint, DB, URI and hash cross-checks."""

    repository = _repository(repository_root)
    run_root = repository / PRIOR_RUN_ROOT_RELATIVE
    preflight_root = repository / PRIOR_PREFLIGHT_RELATIVE
    _require_private_ancestor(run_root)
    if not preflight_root.is_dir():
        raise FrozenC4Error("FROZEN_C4_PREFLIGHT_UNAVAILABLE")
    core_root = run_root / "core"
    artifact_root = core_root / "artifacts"
    db_path = core_root / "evolution.sqlite3"
    checkpoint = run_root / "private/core_checkpoints/C4.json"
    checkpoint_bytes = _read_regular(checkpoint, maximum=16 * 1024 * 1024)
    checkpoint_sha256 = hashlib.sha256(checkpoint_bytes).hexdigest()
    if checkpoint_sha256 != EXPECTED_C4_CHECKPOINT_SHA256:
        raise FrozenC4Error("FROZEN_C4_CHECKPOINT_DIGEST_MISMATCH")
    try:
        state = CoreEvolutionStateV1.from_private_checkpoint_bytes(checkpoint_bytes)
    except Exception as exc:  # exact historical DTO boundary
        raise FrozenC4Error("FROZEN_C4_CHECKPOINT_INVALID") from exc
    if state.batch_index != 4 or state.digest != EXPECTED_C4_STATE_SHA256:
        raise FrozenC4Error("FROZEN_C4_STATE_IDENTITY_MISMATCH")
    if tuple(receipt.target_id for receipt in state.target_receipts) != TARGET_ORDER:
        raise FrozenC4Error("FROZEN_C4_TARGET_CLOSURE_INVALID")

    connection = _open_readonly_database(db_path)
    try:
        integrity_rows = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
        if integrity_rows != ("ok",):
            raise FrozenC4Error("FROZEN_C4_DATABASE_INTEGRITY_INVALID")
        artifacts: list[FrozenC4ArtifactV2] = []
        for receipt in state.target_receipts:
            row = connection.execute(
                "SELECT artifact_id,type,uri,manifest_json,lineage_json,state,promoted,"
                "staging_job_id FROM artifacts WHERE artifact_id=?",
                (receipt.artifact_id,),
            ).fetchone()
            if row is None or tuple(row[:2]) != (receipt.artifact_id, receipt.target_id):
                raise FrozenC4Error("FROZEN_C4_DATABASE_ARTIFACT_MISSING")
            if row[5] != "active" or row[6] != 1 or row[7] is not None:
                raise FrozenC4Error("FROZEN_C4_DATABASE_ARTIFACT_NOT_FINAL")
            manifest = _canonical_object(row[3], "FROZEN_C4_MANIFEST_INVALID")
            lineage = _canonical_object(row[4], "FROZEN_C4_LINEAGE_INVALID")
            execution = lineage.get("openevo_execution")
            if (
                type(execution) is not dict
                or execution.get("job_id") != receipt.planned_job.job_id
                or execution.get("target_id") != receipt.target_id
                or execution.get("method_id") != receipt.planned_job.method_id
            ):
                raise FrozenC4Error("FROZEN_C4_JOB_LINEAGE_INVALID")
            path = _file_uri_path(str(row[2]), artifact_root=artifact_root)
            content_path = _content_path(receipt.target_id, manifest)
            payload_path = path / content_path if path.is_dir() else path
            payload = _read_regular(payload_path, maximum=8192)
            expected_size, expected_hash = FROZEN_C4[receipt.target_id]
            if (
                len(payload) != expected_size
                or hashlib.sha256(payload).hexdigest() != expected_hash
                or payload.decode("utf-8") != receipt.artifact_content
                or receipt.core_payload_sha256 != expected_hash
                or receipt.core_payload_utf8_bytes != expected_size
                or not receipt.promoted
            ):
                raise FrozenC4Error("FROZEN_C4_PAYLOAD_IDENTITY_MISMATCH")
            artifacts.append(
                FrozenC4ArtifactV2(
                    target_id=receipt.target_id,
                    artifact_id=receipt.artifact_id,
                    job_id=receipt.planned_job.job_id,
                    payload=payload,
                    payload_sha256=expected_hash,
                    utf8_bytes=expected_size,
                    source_path=payload_path,
                    source_uri=str(row[2]),
                    manifest_sha256=sha256_bytes(canonical_json_bytes(manifest)),
                    lineage_sha256=sha256_bytes(canonical_json_bytes(lineage)),
                )
            )
    finally:
        connection.close()

    identity = [
        {
            "target_id": item.target_id,
            "sha256": item.payload_sha256,
            "utf8_bytes": item.utf8_bytes,
        }
        for item in artifacts
    ]
    combined_sha256 = sha256_bytes(canonical_json_bytes(identity))
    combined_bytes = sum(item.utf8_bytes for item in artifacts)
    if (combined_bytes, combined_sha256) != FROZEN_C4["combined"]:
        raise FrozenC4Error("FROZEN_C4_COMBINED_IDENTITY_MISMATCH")

    aggregate_path = run_root / "private/aggregate_report_input_v1.json"
    aggregate = _canonical_object(
        _read_regular(aggregate_path, maximum=4 * 1024 * 1024).decode("utf-8"),
        "FROZEN_C4_AGGREGATE_INVALID",
    )
    final = aggregate.get("final_artifacts")
    if (
        type(final) is not dict
        or final.get("final_state_id") != "C4"
        or final.get("final_manifest_sha256") != EXPECTED_FINAL_MANIFEST_SHA256
        or final.get("combined_context_sha256") != combined_sha256
        or final.get("combined_context_bytes") != combined_bytes
        or final.get("frozen_before_test") is not True
    ):
        raise FrozenC4Error("FROZEN_C4_FINAL_MANIFEST_INVALID")
    return FrozenC4BundleV2(
        run_root=run_root,
        preflight_root=preflight_root,
        core_db_path=db_path,
        checkpoint_path=checkpoint,
        checkpoint_sha256=checkpoint_sha256,
        state_sha256=state.digest,
        artifacts=tuple(artifacts),
        combined_context_sha256=combined_sha256,
        combined_context_bytes=combined_bytes,
        final_manifest_sha256=EXPECTED_FINAL_MANIFEST_SHA256,
        database_integrity="ok",
    )


def _repository(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("repository root must be absolute")
    try:
        value = path.resolve(strict=True)
    except OSError as exc:
        raise FrozenC4Error("FROZEN_C4_REPOSITORY_UNAVAILABLE") from exc
    if not (value / "benchmarks/chembench").is_dir():
        raise FrozenC4Error("FROZEN_C4_REPOSITORY_INVALID")
    return value


def _require_private_ancestor(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FrozenC4Error("FROZEN_C4_RUN_ROOT_UNAVAILABLE") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise FrozenC4Error("FROZEN_C4_RUN_ROOT_UNSAFE")


def _read_regular(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or not 0 < metadata.st_size <= maximum
        ):
            raise FrozenC4Error("FROZEN_C4_FILE_UNSAFE")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            value = os.read(descriptor, maximum + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except FrozenC4Error:
        raise
    except OSError as exc:
        raise FrozenC4Error("FROZEN_C4_FILE_UNAVAILABLE") from exc
    if (
        len(value) != before.st_size
        or len(value) > maximum
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
    ):
        raise FrozenC4Error("FROZEN_C4_FILE_CHANGED")
    return value


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    _read_regular(path, maximum=256 * 1024 * 1024)
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise FrozenC4Error("FROZEN_C4_DATABASE_UNAVAILABLE") from exc


def _canonical_object(raw: str, finding: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise FrozenC4Error(finding) from exc
    # Historical Store rows use SQLite's stable JSON renderer with spaces,
    # while private aggregate files use pretty JSON.  Their semantic digest is
    # recomputed from the parsed closed object; raw-byte canonicality was not a
    # V1 persistence contract.
    if type(value) is not dict:
        raise FrozenC4Error(finding)
    return value


def _file_uri_path(uri: str, *, artifact_root: Path) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise FrozenC4Error("FROZEN_C4_ARTIFACT_URI_INVALID")
    try:
        path = Path(unquote(parsed.path)).resolve(strict=True)
        root = artifact_root.resolve(strict=True)
    except OSError as exc:
        raise FrozenC4Error("FROZEN_C4_ARTIFACT_URI_UNAVAILABLE") from exc
    if not path.is_relative_to(root):
        raise FrozenC4Error("FROZEN_C4_ARTIFACT_URI_OUTSIDE_STORE")
    return path


def _content_path(target_id: str, manifest: dict[str, object]) -> str:
    key = "entrypoint" if target_id == "skill_bundle" else "content_path"
    value = manifest.get(key)
    if type(value) is not str or Path(value).is_absolute() or ".." in Path(value).parts:
        raise FrozenC4Error("FROZEN_C4_CONTENT_PATH_INVALID")
    return value


__all__ = [
    "EXPECTED_C4_CHECKPOINT_SHA256",
    "EXPECTED_C4_STATE_SHA256",
    "EXPECTED_FINAL_MANIFEST_SHA256",
    "PRIOR_PREFLIGHT_RELATIVE",
    "PRIOR_RUN_ID",
    "PRIOR_RUN_ROOT_RELATIVE",
    "FrozenC4ArtifactV2",
    "FrozenC4BundleV2",
    "FrozenC4Error",
    "recover_frozen_c4_v2",
]
