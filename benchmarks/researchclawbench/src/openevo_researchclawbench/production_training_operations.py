"""Production side-effect driver for durable Community training.

The supervisor owns state and transition ordering.  This module owns only the
execution/recovery of already-planned operations.  Every external side effect
is delegated to a typed Core/evaluator port with the supervisor idempotency
key, recovered before creation, and sealed into an immutable local receipt.

No implementation in this module executes Codex, a reflector, or a scorer
directly.  The production ports are responsible for invoking the existing
OpenEvo run owner, trusted evaluator process, Core feedback control API, and
native successor/evolution services respectively.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
)

from .config import FROZEN_TASKS, ExperimentConfig
from .durable_evaluator_operation import OPENROUTER_API_BASE
from .evaluator_dependency_lock import validate_evaluator_dependency_lock
from .gt_supervision import load_current_task_gt_supervision
from .training_state_store import canonical_bytes, canonical_sha256

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OperationStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    RECOVERED = "RECOVERED"


class FailureClass(str, Enum):
    NONE = "NONE"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    RESEARCH = "RESEARCH"
    POLICY = "POLICY"
    CREDENTIALS = "CREDENTIALS"
    AUTHORITY = "AUTHORITY"
    INTEGRITY = "INTEGRITY"


class JudgeCredentialsRequired(RuntimeError):
    """The evaluator boundary has no complete credential reference."""


class ProductionOperationError(RuntimeError):
    """A formal operation failed closed before a valid receipt existed."""


class SuccessorRecoveryRequired(ProductionOperationError):
    """Core proved that a source successor needs append-only recovery."""

    _SCHEMA_VERSION = "openevo.researchclawbench.successor_recovery_checkpoint.v1"

    def __init__(self, checkpoint: dict[str, Any]) -> None:
        if not isinstance(checkpoint, dict):
            raise TypeError("successor recovery checkpoint must be an object")
        closed = json.loads(canonical_bytes(checkpoint))
        content = closed.get("content_sha256")
        body = {key: value for key, value in closed.items() if key != "content_sha256"}
        if (
            closed.get("schema_version") != self._SCHEMA_VERSION
            or not isinstance(content, str)
            or _SHA256.fullmatch(content) is None
            or canonical_sha256(body) != content
            or closed.get("commit_absent") is not True
            or closed.get("successor_artifact_count") != 0
            or closed.get("source_mutation_allowed") is not False
            or closed.get("candidate_reexecution_allowed") is not False
            or closed.get("judge_reexecution_allowed") is not False
            or closed.get("reflector_reexecution_allowed") is not False
        ):
            raise ValueError("successor recovery checkpoint is invalid")
        self.checkpoint = closed
        super().__init__("SUCCESSOR_RECOVERY_REQUIRED")


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    idempotency_key: str
    status: OperationStatus
    started_at: str
    completed_at: str | None
    input_identity: str
    output_identity: str | None
    receipt_path: str
    content_sha256: str
    owned_resource_ids: tuple[str, ...] = ()
    retryable: bool = False
    failure_class: FailureClass = FailureClass.NONE
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input_identity": self.input_identity,
            "output_identity": self.output_identity,
            "receipt_path": self.receipt_path,
            "content_sha256": self.content_sha256,
            "owned_resource_ids": list(self.owned_resource_ids),
            "retryable": self.retryable,
            "failure_class": self.failure_class.value,
            **self.payload,
        }


class ProductionOperationPort(Protocol):
    """One external authority used by the production driver.

    ``recover`` must query durable authority using the supplied key.  It must
    never infer completion from logs.  ``execute`` must pass the same key to
    the external service so a lost response cannot duplicate a billed call.
    """

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None: ...

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def reconcile_terminal_successor(
        self,
        request: dict[str, Any],
        idempotency_key: str,
        checkpoint: dict[str, Any],
        reconciliation_id: str,
    ) -> dict[str, Any]: ...


class TrainingOperations(Protocol):
    def reconcile_candidate_authority(
        self, request: dict[str, Any], idempotency_key: str
    ) -> OperationResult: ...

    def candidate_readiness(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def evolution_readiness(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def create_candidate_workspace(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def start_candidate(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def recover_candidate(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def validate_candidate_artifacts(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def evaluate_candidate(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def create_feedback_attachment(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def prepare_successor(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def collect_artifact_jobs(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def construct_composite(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def prepare_per_item_evolved_workspace(
        self, request: dict[str, Any], idempotency_key: str
    ) -> OperationResult: ...

    def sanitize_cross_task(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...

    def freeze_final_artifact(self, request: dict[str, Any], idempotency_key: str) -> OperationResult: ...


@dataclass(frozen=True)
class ProductionPorts:
    candidate: ProductionOperationPort
    validation: ProductionOperationPort
    evaluation: ProductionOperationPort
    attachment: ProductionOperationPort
    evolution: ProductionOperationPort
    composite: ProductionOperationPort
    task_local: ProductionOperationPort
    sanitizer: ProductionOperationPort
    freeze: ProductionOperationPort
    reconciliation: ProductionOperationPort | None = None
    per_item_evolved: ProductionOperationPort | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class OperationReceiptStore:
    """Immutable, content-addressed operation receipts.

    External services remain the authority for side-effect recovery.  These
    files are an audit cache and are never used to invent external completion.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("operation receipt root is unsafe")
        os.chmod(self.root, 0o700)

    def _path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("operation receipt entry is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("idempotency_key") != idempotency_key:
            raise ValueError("operation receipt identity changed")
        observed = value.get("content_sha256")
        body = {key: item for key, item in value.items() if key != "content_sha256"}
        if observed != canonical_sha256(body):
            raise ValueError("operation receipt hash is invalid")
        return value

    def put(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or not key:
            raise ValueError("operation receipt lacks idempotency key")
        closed = dict(payload)
        closed.pop("content_sha256", None)
        closed["content_sha256"] = canonical_sha256(closed)
        path = self._path(key)
        prior = self.get(key)
        if prior is not None:
            if canonical_bytes(prior) != canonical_bytes(closed):
                raise ValueError("operation receipt idempotency conflict")
            return prior
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            data = json.dumps(closed, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short operation receipt write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            prior = self.get(key)
            if prior is None or canonical_bytes(prior) != canonical_bytes(closed):
                raise ValueError("concurrent operation receipt conflict")
        finally:
            temporary.unlink(missing_ok=True)
        return self.get(key) or closed

    def get_reconciliation(
        self,
        storage_key: str,
        *,
        supervisor_idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Read one superseding receipt without touching its source receipt."""

        path = self._path(storage_key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("operation reconciliation receipt entry is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("receipt_storage_key") != storage_key
            or value.get("idempotency_key") != supervisor_idempotency_key
        ):
            raise ValueError("operation reconciliation receipt identity changed")
        observed = value.get("content_sha256")
        body = {key: item for key, item in value.items() if key != "content_sha256"}
        if observed != canonical_sha256(body):
            raise ValueError("operation reconciliation receipt hash is invalid")
        return value

    def put_reconciliation(
        self,
        storage_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a distinct append-only receipt for a terminal source."""

        supervisor_key = payload.get("idempotency_key")
        if (
            not isinstance(storage_key, str)
            or not storage_key
            or not isinstance(supervisor_key, str)
            or not supervisor_key
        ):
            raise ValueError("operation reconciliation identity is invalid")
        closed = dict(payload)
        closed["receipt_storage_key"] = storage_key
        closed.pop("content_sha256", None)
        closed["content_sha256"] = canonical_sha256(closed)
        path = self._path(storage_key)
        prior = self.get_reconciliation(
            storage_key,
            supervisor_idempotency_key=supervisor_key,
        )
        if prior is not None:
            if canonical_bytes(prior) != canonical_bytes(closed):
                raise ValueError("operation reconciliation idempotency conflict")
            return prior
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            data = (
                json.dumps(closed, indent=2, sort_keys=True, allow_nan=False).encode(
                    "utf-8"
                )
                + b"\n"
            )
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short operation reconciliation receipt write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
            directory = os.open(
                self.root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            prior = self.get_reconciliation(
                storage_key,
                supervisor_idempotency_key=supervisor_key,
            )
            if prior is None or canonical_bytes(prior) != canonical_bytes(closed):
                raise ValueError("concurrent operation reconciliation conflict")
        finally:
            temporary.unlink(missing_ok=True)
        return (
            self.get_reconciliation(
                storage_key,
                supervisor_idempotency_key=supervisor_key,
            )
            or closed
        )


class ProductionTrainingOperations:
    """Concrete supervisor driver over native OpenEvo/evaluator authorities."""

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        experiment_run_id: str,
        receipt_root: str | Path,
        ports: ProductionPorts,
    ) -> None:
        if not _IDENTITY.fullmatch(experiment_run_id):
            raise ValueError("unsafe production experiment run ID")
        self.config = config
        self.experiment_run_id = experiment_run_id
        self.ports = ports
        self.receipts = OperationReceiptStore(receipt_root)

    def candidate_readiness(self, request: dict[str, Any]) -> dict[str, Any]:
        """Authenticate Core before the supervisor persists candidate intent."""

        request = self._require_request(request)
        preflight = getattr(self.ports.candidate, "preflight", None)
        if not callable(preflight):
            raise ProductionOperationError(
                "candidate authority has no pre-intent readiness boundary"
            )
        result = preflight(request)
        isolation = (
            result.get("managed_candidate_subscription_isolation")
            if isinstance(result, dict)
            else None
        )
        required_true = (
            "service_reachable",
            "bearer_present",
            "bearer_valid",
            "generation_matches",
            "candidate_port_authorized",
        )
        if (
            not isinstance(result, dict)
            or any(result.get(field) is not True for field in required_true)
            or result.get("secret_recorded") is not False
            or result.get("environment_fallback_used") is not False
            or result.get("managed_candidate_runtime_ready") is not True
            or result.get("managed_candidate_credential_mount_adopted") is not True
            or result.get("managed_candidate_codex_cli_version") != "0.144.1"
            or result.get("managed_candidate_model_started") is not False
            or not isinstance(isolation, dict)
            or any(
                isolation.get(field) is not expected
                for field, expected in {
                    "authority_issued": True,
                    "mount_adopted": True,
                    "codex_cli_auth_visible": True,
                    "host_source_hidden": True,
                    "tool_sandbox_credential_hidden": True,
                    "tool_environment_clean": True,
                    "parent_process_secret_unreadable": True,
                    "generation_matches": True,
                    "release_identity_matches": True,
                    "image_digest_matches": True,
                    "cli_version_matches": True,
                    "cleanup_verified": True,
                    "model_started": False,
                }.items()
            )
            or isolation.get("policy_id") != CODEX_SUBSCRIPTION_POLICY_ID
            or isolation.get("policy_sha256")
            != CODEX_SUBSCRIPTION_POLICY_SHA256
        ):
            raise ProductionOperationError(
                "candidate Core-control readiness failed closed"
            )
        return json.loads(canonical_bytes(result))

    def current_task_gt_supervision(self, task_id: str) -> dict[str, Any]:
        """Load one bounded current-task GT authority without invoking Judge."""

        return load_current_task_gt_supervision(self.config, task_id=task_id)

    def evolution_readiness(self, request: dict[str, Any]) -> dict[str, Any]:
        """Fail closed before an evolution operation intent can be persisted."""

        request = self._require_request(request)
        preflight = getattr(self.ports.evolution, "preflight", None)
        if not callable(preflight):
            raise ProductionOperationError(
                "REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"
            )
        result = preflight(request)
        local_pin = self.config.reflector_credential_mount_readiness()
        required_true = (
            "authority_issued",
            "docker_mount_created",
            "container_path_visible",
            "container_user_can_read",
            "generation_matches",
            "release_identity_matches",
            "adoption_receipt_valid",
            "cleanup_verified",
        )
        if (
            not isinstance(result, dict)
            or result.get("ready") is not True
            or result.get("identity_matches") is not True
            or any(result.get(field) is not True for field in required_true)
            or result.get("secret_recorded") is not False
            or result.get("codex_cli_started") is not False
            or result.get("model_started") is not False
        ):
            raise ProductionOperationError(
                "REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"
            )
        if local_pin.get("required") is True and (
            local_pin.get("ready") is not True
            or any(
                local_pin.get(key) != result.get(key)
                for key in (
                    "authority_id",
                    "worker_launch_id",
                    "generation_digest",
                    "release_install_digest",
                    "release_registry_digest",
                    "docker_host_path_identity",
                )
            )
        ):
            raise ProductionOperationError(
                "REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"
            )
        return json.loads(canonical_bytes(result))

    def reconcile_candidate_authority(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """Import a sealed Core authority without invoking a candidate."""

        port = self.ports.reconciliation
        if port is None:
            raise ProductionOperationError(
                "candidate reconciliation authority is unavailable"
            )
        return self._run(
            kind="candidate_reconciliation",
            port=port,
            request=request,
            idempotency_key=idempotency_key,
        ).to_dict()

    @staticmethod
    def _require_request(request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise TypeError("operation request must be an object")
        task = request.get("task_id")
        if task is not None and task not in FROZEN_TASKS:
            raise ValueError("operation task is outside the frozen Community inventory")
        return json.loads(canonical_bytes(request))

    def _run(
        self,
        *,
        kind: str,
        port: ProductionOperationPort,
        request: dict[str, Any],
        idempotency_key: str,
        recover_only: bool = False,
    ) -> OperationResult:
        request = self._require_request(request)
        if not _IDENTITY.fullmatch(kind.replace("_", "-")):
            raise ValueError("invalid production operation kind")
        input_identity = canonical_sha256(request)
        existing = self.receipts.get(idempotency_key)
        if existing is not None:
            if existing.get("input_identity") != input_identity:
                raise ValueError("operation receipt request drifted")
            if existing.get("status") == OperationStatus.FAILED_TERMINAL.value:
                checkpoint = existing.get("successor_recovery_checkpoint")
                if checkpoint is None:
                    raise ProductionOperationError(
                        "terminal operation receipt cannot prove recovery authority"
                    )
                reconcile = getattr(
                    port,
                    "reconcile_terminal_successor",
                    None,
                )
                if not callable(reconcile):
                    raise SuccessorRecoveryRequired(checkpoint)
                return self._reconcile_terminal_successor_operation(
                    kind=kind,
                    port=port,
                    request=request,
                    idempotency_key=idempotency_key,
                    input_identity=input_identity,
                    source_receipt=existing,
                    checkpoint=checkpoint,
                )
            return _operation_result_from_dict(existing)
        started = _utc_now()
        try:
            recovered = port.recover(request, idempotency_key)
            if recovered is None:
                if recover_only:
                    raise ProductionOperationError("external authority cannot prove operation completion")
                output = port.execute(request, idempotency_key)
                status = OperationStatus.SUCCEEDED
            else:
                output = recovered
                status = OperationStatus.RECOVERED
        except SuccessorRecoveryRequired as exc:
            operation_id = (
                "op-"
                + hashlib.sha256(
                    (kind + ":" + idempotency_key).encode()
                ).hexdigest()[:24]
            )
            closed = self.receipts.put(
                {
                    "operation_id": operation_id,
                    "operation_kind": kind,
                    "idempotency_key": idempotency_key,
                    "status": OperationStatus.FAILED_TERMINAL.value,
                    "started_at": started,
                    "completed_at": _utc_now(),
                    "input_identity": input_identity,
                    "output_identity": None,
                    "receipt_path": os.fspath(self.receipts._path(idempotency_key)),
                    "owned_resource_ids": [],
                    "retryable": False,
                    "failure_class": FailureClass.AUTHORITY.value,
                    "successor_recovery_checkpoint": exc.checkpoint,
                }
            )
            raise SuccessorRecoveryRequired(
                closed["successor_recovery_checkpoint"]
            ) from None
        if not isinstance(output, dict):
            raise ProductionOperationError(f"{kind} authority returned a non-object receipt")
        output = json.loads(canonical_bytes(output))
        owned = output.pop("owned_resource_ids", [])
        if not isinstance(owned, list) or not all(isinstance(item, str) and _IDENTITY.fullmatch(item) for item in owned):
            raise ProductionOperationError(f"{kind} returned invalid owned resources")
        output_identity = canonical_sha256(output)
        operation_id = f"op-{hashlib.sha256((kind + ':' + idempotency_key).encode()).hexdigest()[:24]}"
        receipt_path = os.fspath(self.receipts._path(idempotency_key))
        closed = self.receipts.put(
            {
                "operation_id": operation_id,
                "operation_kind": kind,
                "idempotency_key": idempotency_key,
                "status": status.value,
                "started_at": started,
                "completed_at": _utc_now(),
                "input_identity": input_identity,
                "output_identity": output_identity,
                "receipt_path": receipt_path,
                "owned_resource_ids": owned,
                "retryable": False,
                "failure_class": FailureClass.NONE.value,
                **output,
            }
        )
        return _operation_result_from_dict(closed)

    def _reconcile_terminal_successor_operation(
        self,
        *,
        kind: str,
        port: ProductionOperationPort,
        request: dict[str, Any],
        idempotency_key: str,
        input_identity: str,
        source_receipt: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> OperationResult:
        """Append a recovered result while preserving the failed receipt."""

        checkpoint = SuccessorRecoveryRequired(checkpoint).checkpoint
        source_sha256 = source_receipt.get("content_sha256")
        if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
            raise ProductionOperationError(
                "terminal successor receipt identity is invalid"
            )
        seed = canonical_sha256(
            {
                "checkpoint_sha256": checkpoint["content_sha256"],
                "input_identity": input_identity,
                "source_receipt_sha256": source_sha256,
                "supervisor_idempotency_key": idempotency_key,
            }
        )
        reconciliation_id = f"successor-reconcile-{seed[:40]}"
        prior = self.receipts.get_reconciliation(
            reconciliation_id,
            supervisor_idempotency_key=idempotency_key,
        )
        if prior is not None:
            if (
                prior.get("source_terminal_receipt_sha256") != source_sha256
                or prior.get("successor_recovery_checkpoint_sha256")
                != checkpoint["content_sha256"]
                or prior.get("input_identity") != input_identity
                or prior.get("status") != OperationStatus.RECOVERED.value
            ):
                raise ProductionOperationError(
                    "terminal successor reconciliation receipt drifted"
                )
            return _operation_result_from_dict(prior)
        reconcile = getattr(port, "reconcile_terminal_successor", None)
        if not callable(reconcile):
            raise SuccessorRecoveryRequired(checkpoint)
        started = _utc_now()
        output = reconcile(
            request,
            idempotency_key,
            checkpoint,
            reconciliation_id,
        )
        if not isinstance(output, dict):
            raise ProductionOperationError(
                "terminal successor reconciliation returned a non-object receipt"
            )
        output = json.loads(canonical_bytes(output))
        owned = output.pop("owned_resource_ids", [])
        if not isinstance(owned, list) or not all(
            isinstance(item, str) and _IDENTITY.fullmatch(item)
            for item in owned
        ):
            raise ProductionOperationError(
                "terminal successor reconciliation returned invalid resources"
            )
        output_identity = canonical_sha256(output)
        operation_id = (
            "op-"
            + hashlib.sha256(
                (kind + ":" + idempotency_key + ":" + reconciliation_id).encode()
            ).hexdigest()[:24]
        )
        closed = self.receipts.put_reconciliation(
            reconciliation_id,
            {
                "operation_id": operation_id,
                "operation_kind": kind,
                "idempotency_key": idempotency_key,
                "status": OperationStatus.RECOVERED.value,
                "started_at": started,
                "completed_at": _utc_now(),
                "input_identity": input_identity,
                "output_identity": output_identity,
                "receipt_path": os.fspath(
                    self.receipts._path(reconciliation_id)
                ),
                "owned_resource_ids": owned,
                "retryable": False,
                "failure_class": FailureClass.NONE.value,
                "source_terminal_receipt_sha256": source_sha256,
                "successor_recovery_checkpoint_sha256": (
                    checkpoint["content_sha256"]
                ),
                **output,
            },
        )
        return _operation_result_from_dict(closed)

    def create_candidate_workspace(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="candidate_workspace", port=self.ports.candidate, request={**request, "operation_phase": "workspace"}, idempotency_key=idempotency_key)

    def start_candidate(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="candidate", port=self.ports.candidate, request={**request, "operation_phase": "execute"}, idempotency_key=idempotency_key)

    def recover_candidate(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="candidate", port=self.ports.candidate, request={**request, "operation_phase": "execute"}, idempotency_key=idempotency_key, recover_only=True)

    def validate_candidate_artifacts(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="validation", port=self.ports.validation, request=request, idempotency_key=idempotency_key)

    def evaluate_candidate(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        readiness = judge_credential_readiness(self.config)
        if not readiness["ready"]:
            raise JudgeCredentialsRequired("JUDGE_CREDENTIALS_REQUIRED")
        return self._run(kind="evaluation", port=self.ports.evaluation, request=request, idempotency_key=idempotency_key)

    def create_feedback_attachment(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="feedback_attachment", port=self.ports.attachment, request=request, idempotency_key=idempotency_key)

    def prepare_successor(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="successor", port=self.ports.evolution, request={**request, "operation_phase": "prepare"}, idempotency_key=idempotency_key)

    def collect_artifact_jobs(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="evolution", port=self.ports.evolution, request={**request, "operation_phase": "collect"}, idempotency_key=idempotency_key)

    def construct_composite(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="composite", port=self.ports.composite, request=request, idempotency_key=idempotency_key)

    def prepare_per_item_evolved_workspace(
        self, request: dict[str, Any], idempotency_key: str
    ) -> OperationResult:
        if self.ports.per_item_evolved is None:
            raise ProductionOperationError(
                "per-item evolved workspace authority is unavailable"
            )
        return self._run(
            kind="per_item_evolved_workspace",
            port=self.ports.per_item_evolved,
            request=request,
            idempotency_key=idempotency_key,
        )

    def sanitize_cross_task(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="sanitizer", port=self.ports.sanitizer, request=request, idempotency_key=idempotency_key)

    def freeze_final_artifact(self, request: dict[str, Any], idempotency_key: str) -> OperationResult:
        return self._run(kind="freeze", port=self.ports.freeze, request=request, idempotency_key=idempotency_key)

    # Compatibility surface consumed by CommunityTrainingSupervisor.  The
    # public typed methods above remain the formal Operations protocol.
    def ensure_candidate(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.start_candidate(request, idempotency_key).to_dict()

    def validate_artifact(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.validate_candidate_artifacts(request, idempotency_key).to_dict()

    def evaluate(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.evaluate_candidate(request, idempotency_key).to_dict()

    def attach_feedback(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.create_feedback_attachment(request, idempotency_key).to_dict()

    def evolve_artifacts(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.collect_artifact_jobs(request, idempotency_key).to_dict()

    def admit_composite(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.construct_composite(request, idempotency_key).to_dict()

    def prepare_evolved_workspace(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        return self.prepare_per_item_evolved_workspace(
            request, idempotency_key
        ).to_dict()

    def destroy_task_local(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._run(kind="task_local_destroy", port=self.ports.task_local, request=request, idempotency_key=idempotency_key).to_dict()

    def sanitize_composite(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.sanitize_cross_task(request, idempotency_key).to_dict()

    def freeze_final(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.freeze_final_artifact(request, idempotency_key).to_dict()


def _operation_result_from_dict(value: dict[str, Any]) -> OperationResult:
    required = {
        "operation_id", "idempotency_key", "status", "started_at", "completed_at",
        "input_identity", "output_identity", "receipt_path", "content_sha256",
        "owned_resource_ids", "retryable", "failure_class",
    }
    if not required.issubset(value):
        raise ValueError("operation result is incomplete")
    content = value.get("content_sha256")
    body = {key: item for key, item in value.items() if key != "content_sha256"}
    if not isinstance(content, str) or not _SHA256.fullmatch(content) or canonical_sha256(body) != content:
        raise ValueError("operation result hash is invalid")
    return OperationResult(
        operation_id=str(value["operation_id"]),
        idempotency_key=str(value["idempotency_key"]),
        status=OperationStatus(value["status"]),
        started_at=str(value["started_at"]),
        completed_at=None if value["completed_at"] is None else str(value["completed_at"]),
        input_identity=str(value["input_identity"]),
        output_identity=None if value["output_identity"] is None else str(value["output_identity"]),
        receipt_path=str(value["receipt_path"]),
        content_sha256=content,
        owned_resource_ids=tuple(value["owned_resource_ids"]),
        retryable=bool(value["retryable"]),
        failure_class=FailureClass(value["failure_class"]),
        payload={key: item for key, item in value.items() if key not in required and key != "operation_kind"},
    )


def judge_credential_readiness(config: ExperimentConfig) -> dict[str, Any]:
    names = ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")
    environment_present = [name for name in names if bool(os.environ.get(name))]
    secret_dir = config.experiment_root / "secrets"
    secret_file = secret_dir / "judge.env"
    file_ready = False
    file_error: str | None = None
    if secret_file.exists():
        try:
            if secret_dir.is_symlink() or secret_file.is_symlink():
                raise ValueError("judge secret path may not be a symlink")
            if (os.stat(secret_dir).st_mode & 0o777) != 0o700:
                raise ValueError("judge secret directory must be 0700")
            if (os.stat(secret_file).st_mode & 0o777) != 0o600:
                raise ValueError("judge secret file must be 0600")
            # The supervisor deliberately does not read the file.  The
            # evaluator-only child validates the closed variable set.
            file_ready = True
        except (OSError, ValueError) as exc:
            file_error = str(exc)
    return {
        "ready": len(environment_present) == len(names) or file_ready,
        "environment_names_present": environment_present,
        "missing_environment_names": [name for name in names if name not in environment_present],
        "secret_file_present": secret_file.exists(),
        "secret_file_permissions_valid": file_ready,
        "secret_file_error": file_error,
        "secret_values_read": False,
    }


def judge_identity_preflight(
    config: ExperimentConfig,
    *,
    receipt_path: str | Path,
) -> dict[str, Any]:
    """Run an evaluator-only, no-request Judge identity check.

    This is intentionally stronger than :func:`judge_credential_readiness`:
    file permissions alone cannot prove that the configured model and API base
    match the frozen protocol.  Secret contents are loaded only inside the
    short-lived evaluator subprocess and are never returned or logged.
    """

    destination = Path(os.path.abspath(receipt_path))
    experiment_root = config.experiment_root
    if not destination.is_relative_to(experiment_root):
        raise JudgeCredentialsRequired("Judge preflight receipt escapes experiment")
    dependency_claim = config.require("judge.evaluator_dependency_lock")
    if not isinstance(dependency_claim, dict):
        raise JudgeCredentialsRequired("evaluator dependency lock is absent")
    dependency_path = Path(str(dependency_claim.get("path", "")))
    try:
        dependency_lock = validate_evaluator_dependency_lock(dependency_path)
    except RuntimeError as exc:
        raise JudgeCredentialsRequired("evaluator dependency lock drifted") from exc
    dependency_file_sha256 = hashlib.sha256(dependency_path.read_bytes()).hexdigest()
    if (
        dependency_file_sha256 != dependency_claim.get("sha256")
        or dependency_lock.get("content_sha256")
        != dependency_claim.get("content_sha256")
    ):
        raise JudgeCredentialsRequired("evaluator dependency lock identity drifted")
    expected = {
        "schema_version": "openevo.researchclawbench.judge_scorer_preflight.v2",
        "api_base": OPENROUTER_API_BASE,
        "api_key_present": True,
        "model": config.require("judge.model"),
        "provider": config.require("judge.provider"),
        "scorer_import_ready": True,
        "scorer_module_origin": "ResearchClawBench/evaluation/score.py",
        "structai_version": "0.1.23",
        "evaluator_dependency_lock_sha256": dependency_file_sha256,
        "evaluator_dependency_content_sha256": dependency_lock["content_sha256"],
        "tasks_dir_authority_matches": True,
        "tasks_dir_relative": "ResearchClawBench/tasks",
        "model_slug_preserved": True,
        "judge_request_started": False,
        "model_started": False,
        "secret_recorded": False,
    }
    if destination.exists():
        metadata = os.stat(destination, follow_symlinks=False)
        if (
            destination.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise JudgeCredentialsRequired("Judge preflight receipt is unsafe")
        try:
            observed = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JudgeCredentialsRequired("Judge preflight receipt is invalid") from exc
        if observed != expected:
            raise JudgeCredentialsRequired("Judge preflight identity drifted")
        return observed

    readiness = judge_credential_readiness(config)
    if not readiness["ready"]:
        raise JudgeCredentialsRequired("JUDGE_CREDENTIALS_REQUIRED")
    openevo_root = getattr(config, "openevo_root", config.project_root / "OpenEvo")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(
            (
                os.fspath(openevo_root / "src"),
                os.fspath(
                    openevo_root
                    / "benchmarks"
                    / "researchclawbench"
                    / "src"
                ),
            )
        ),
    }
    names = ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")
    if all(os.environ.get(name) for name in names):
        environment.update({name: os.environ[name] for name in names})
    else:
        secret_file = config.experiment_root / "secrets" / "judge.env"
        if readiness["secret_file_permissions_valid"] is not True:
            raise JudgeCredentialsRequired("JUDGE_CREDENTIALS_REQUIRED")
        environment["OPENEVO_JUDGE_ENV_FILE"] = os.fspath(secret_file)
    command = (
        sys.executable,
        "-m",
        "openevo_researchclawbench.community_evaluator",
        "--credential-preflight-output",
        os.fspath(destination),
        "--project-root",
        os.fspath(config.project_root),
        "--expected-judge-model",
        str(expected["model"]),
        "--expected-judge-api-base",
        OPENROUTER_API_BASE,
        "--expected-judge-provider",
        str(expected["provider"]),
        "--evaluator-dependency-lock",
        os.fspath(dependency_path),
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    if completed.returncode != 0 or completed.stdout or completed.stderr:
        raise JudgeCredentialsRequired("Judge identity preflight failed closed")
    try:
        observed = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgeCredentialsRequired("Judge preflight receipt is unavailable") from exc
    if observed != expected:
        raise JudgeCredentialsRequired("Judge preflight identity drifted")
    return observed


__all__ = [
    "FailureClass",
    "JudgeCredentialsRequired",
    "OperationReceiptStore",
    "OperationResult",
    "OperationStatus",
    "ProductionOperationError",
    "ProductionOperationPort",
    "ProductionPorts",
    "ProductionTrainingOperations",
    "SuccessorRecoveryRequired",
    "TrainingOperations",
    "judge_credential_readiness",
    "judge_identity_preflight",
]
