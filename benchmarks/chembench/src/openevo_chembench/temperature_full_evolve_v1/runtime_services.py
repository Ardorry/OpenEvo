"""Durable, experiment-local Rollout/Gateway lifecycle.

This wrapper uses the experiment-local, immutable non-editable wheel runtime
and the audited managed Codex image, plus an independent service namespace and
a run-scoped owner-private completion root.  The host Rollout writes session
results to the host spelling of that root; the Gateway bootstrap rewrites the
same topology field to a fixed container spelling backed by an explicit
read-write bind mount.

Subscription transcript sessions normally bypass the Gateway LLM proxy, so a
Gateway ``CompletionWriter`` file is only auxiliary evidence.  The primary
recovery authority is Rollout's synchronously persisted ``SessionResult``.
``audit_persisted_rollout_result_v1`` strictly enumerates and validates that
record and reports one of three states.  Missing, multiple, partial, or invalid
records are ``AMBIGUOUS`` and are never authority to repeat a model call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from openevo.config import TopologyConfig
from openevo.rollout.models import SessionResult, SessionStatus
from pydantic import ValidationError

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
)
from openevo_chembench.supervised_transfer_v2 import runtime_services as v2_services
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
    load_managed_candidate_codex_v2,
)
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    RuntimeProcessIdentityV2,
)
from openevo_chembench.temperature_full_evolve_v1.formal_runtime import (
    FORMAL_FRAMEWORK_LOCK_RELATIVE,
    FORMAL_RUNTIME_PYTHON_RELATIVE,
    TemperatureFormalRuntimeError,
    load_temperature_formal_runtime_v1,
)

SERVICE_RECEIPT_SCHEMA = "TemperatureFullEvolveRuntimeServicesReceiptV1"
SERVICE_STOP_RECEIPT_SCHEMA = "TemperatureFullEvolveRuntimeServicesStopReceiptV1"
SERVICE_POINTER_SCHEMA = "TemperatureFullEvolveRuntimeServicesPointerV1"
COMPLETION_ROOT_SCHEMA = "TemperatureFullEvolveCompletionRootV1"
COMPLETION_AUDIT_SCHEMA = "TemperatureFullEvolvePersistedCompletionAuditV1"
ROLLOUT_RESULT_AUDIT_SCHEMA = "TemperatureFullEvolvePersistedRolloutResultAuditV1"
NO_COMPLETION_EVIDENCE_SCHEMA = "TemperatureFullEvolveNoCompletionEvidenceV1"
EMPTY_RUNTIME_INVENTORY_SCHEMA = "TemperatureFullEvolveEmptyRuntimeInventoryV1"

SERVICE_ROOT_RELATIVE = "state/chembench_temperature_full_evolve_v1/runtime_services"
RUNTIME_PYTHON_RELATIVE = FORMAL_RUNTIME_PYTHON_RELATIVE
FRAMEWORK_LOCK_RELATIVE = FORMAL_FRAMEWORK_LOCK_RELATIVE
TOPOLOGY_RELATIVE = (
    "benchmarks/chembench/configs/temperature_full_evolve_v1/"
    "runtime_services_topology.yaml"
)
GATEWAY_BOOTSTRAP_RELATIVE = (
    "benchmarks/chembench/scripts/temperature_full_evolve_v1/"
    "gateway_service_bootstrap.py"
)

ROLLOUT_URL = "http://127.0.0.1:8080"
GATEWAY_URL = "http://127.0.0.1:8100"
GATEWAY_RUNTIME_MOUNT = "/openevo-temperature-runtime"
GATEWAY_COMPLETION_MOUNT = "/openevo-temperature-completions"
GATEWAY_AUTH_SOURCE_MOUNT = "/openevo-auth-source"
GATEWAY_DOCKER_SOURCE_MOUNT = "/openevo-host-docker"
GATEWAY_EFFECTIVE_TOPOLOGY = f"{GATEWAY_RUNTIME_MOUNT}/effective_topology.yaml"
GATEWAY_BOOTSTRAP_RECEIPT = f"{GATEWAY_RUNTIME_MOUNT}/bootstrap_receipt.json"
COMPLETION_ROOT_MARKER = ".openevo-completion-root.json"
HOST_TOPOLOGY_FILENAME = "host_topology.yaml"

COMPLETION_MAX_FIELD_BYTES = 16 * 1024 * 1024
COMPLETION_QUEUE_SIZE = 128
MAX_COMPLETION_RECORD_BYTES = 80 * 1024 * 1024
MAX_COMPLETION_FILES_PER_SESSION = 16

_RUN_ID_RE = re.compile(
    r"stv3-temperature-services-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,16}\Z"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_COMPLETION_FILENAME_RE = re.compile(
    r"(?P<sequence>[0-9]{4})-(?P<completion_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json\Z"
)
_ROLLOUT_RESULT_FILENAME_RE = re.compile(
    r"ses_(?P<session_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json\Z"
)
_EXPECTED_MODULES = {
    "rollout": "openevo.rollout.server",
    "gateway": "openevo.gateway.server",
}
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_STOP_GRACE_SECONDS = 15.0
_TOPOLOGY_HOST_MARKER = "__TEMPERATURE_COMPLETION_ROOT_HOST__"
_GATEWAY_BOOTSTRAP_RECEIPT_SCHEMA = "TemperatureFullEvolveGatewayBootstrapReceiptV1"
_GATEWAY_CONTAINER_LABEL_PROTOCOL = "openevo.chembench.protocol"
_GATEWAY_CONTAINER_LABEL_RUN = "openevo.chembench.service_run_id"
_GATEWAY_CONTAINER_LABEL_SOURCE = "openevo.chembench.source_commit"
_GATEWAY_CONTAINER_LABEL_COMPLETION = "openevo.chembench.completion_root_marker_sha256"
_GATEWAY_PROTOCOL_LABEL = "temperature_full_evolve_v1"


class TemperatureRuntimeServicesError(RuntimeError):
    """Closed infrastructure or completion-evidence finding."""

    def __init__(self, finding_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", finding_code) is None:
            raise ValueError("runtime services finding code is invalid")
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class CompletionRootIdentityV1:
    root_relative: str
    host_topology_relative: str
    host_topology_sha256: str
    marker_sha256: str
    device: int
    inode: int
    owner_uid: int
    mode: int
    container_path: str
    max_field_bytes: int
    queue_size: int

    @classmethod
    def from_payload(cls, payload: object) -> CompletionRootIdentityV1:
        expected = {
            "root_relative",
            "host_topology_relative",
            "host_topology_sha256",
            "marker_sha256",
            "device",
            "inode",
            "owner_uid",
            "mode",
            "container_path",
            "max_field_bytes",
            "queue_size",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
        if (
            type(payload["root_relative"]) is not str
            or type(payload["host_topology_relative"]) is not str
            or Path(payload["root_relative"]).is_absolute()
            or Path(payload["host_topology_relative"]).is_absolute()
            or ".." in Path(payload["root_relative"]).parts
            or ".." in Path(payload["host_topology_relative"]).parts
            or payload["container_path"] != GATEWAY_COMPLETION_MOUNT
            or payload["max_field_bytes"] != COMPLETION_MAX_FIELD_BYTES
            or payload["queue_size"] != COMPLETION_QUEUE_SIZE
            or payload["mode"] != 0o700
        ):
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
        for key in ("host_topology_sha256", "marker_sha256"):
            if type(payload[key]) is not str or _SHA256_RE.fullmatch(payload[key]) is None:
                raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
        for key in ("device", "inode", "owner_uid", "mode", "max_field_bytes", "queue_size"):
            if type(payload[key]) is not int or payload[key] < 0:
                raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
        return cls(**payload)

    @property
    def payload(self) -> dict[str, object]:
        return {
            "root_relative": self.root_relative,
            "host_topology_relative": self.host_topology_relative,
            "host_topology_sha256": self.host_topology_sha256,
            "marker_sha256": self.marker_sha256,
            "device": self.device,
            "inode": self.inode,
            "owner_uid": self.owner_uid,
            "mode": self.mode,
            "container_path": self.container_path,
            "max_field_bytes": self.max_field_bytes,
            "queue_size": self.queue_size,
        }


@dataclass(frozen=True, slots=True)
class TemperatureGatewayContainerIdentityV1:
    container_name: str
    container_id: str
    image_id: str
    effective_topology_sha256: str
    docker_host_path_identity_sha256: str
    docker_engine_identity_sha256: str
    docker_launcher_sha256: str
    completion_mount_sha256: str
    completion_root_marker_sha256: str
    container_completion_root_identity_sha256: str

    @classmethod
    def from_payload(cls, payload: object) -> TemperatureGatewayContainerIdentityV1:
        expected = {
            "container_name",
            "container_id",
            "image_id",
            "effective_topology_sha256",
            "docker_host_path_identity_sha256",
            "docker_engine_identity_sha256",
            "docker_launcher_sha256",
            "completion_mount_sha256",
            "completion_root_marker_sha256",
            "container_completion_root_identity_sha256",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
        if (
            type(payload["container_name"]) is not str
            or not payload["container_name"].startswith("openevo-temperature-gateway-")
            or type(payload["image_id"]) is not str
            or not payload["image_id"].startswith("sha256:")
        ):
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
        for key in expected - {"container_name", "image_id"}:
            if type(payload[key]) is not str or _SHA256_RE.fullmatch(payload[key]) is None:
                raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
        return cls(**payload)

    @property
    def payload(self) -> dict[str, str]:
        return {
            "container_name": self.container_name,
            "container_id": self.container_id,
            "image_id": self.image_id,
            "effective_topology_sha256": self.effective_topology_sha256,
            "docker_host_path_identity_sha256": self.docker_host_path_identity_sha256,
            "docker_engine_identity_sha256": self.docker_engine_identity_sha256,
            "docker_launcher_sha256": self.docker_launcher_sha256,
            "completion_mount_sha256": self.completion_mount_sha256,
            "completion_root_marker_sha256": self.completion_root_marker_sha256,
            "container_completion_root_identity_sha256": (
                self.container_completion_root_identity_sha256
            ),
        }


@dataclass(frozen=True, slots=True)
class TemperatureRuntimeServicesIdentityV1:
    repository_root: Path
    service_run_id: str
    source_commit: str
    framework_lock_sha256: str
    framework_wheel_sha256: str
    topology_template_sha256: str
    gateway_bootstrap_sha256: str
    runtime_python_sha256: str
    openevo_version: str
    completion_root: CompletionRootIdentityV1
    rollout: RuntimeProcessIdentityV2
    gateway: RuntimeProcessIdentityV2
    gateway_container: TemperatureGatewayContainerIdentityV1
    receipt_path: Path
    receipt_sha256: str

    @property
    def rollout_url(self) -> str:
        return ROLLOUT_URL

    @property
    def public_identity(self) -> dict[str, object]:
        return {
            "schema_version": SERVICE_RECEIPT_SCHEMA,
            "service_run_id": self.service_run_id,
            "source_commit": self.source_commit,
            "framework_lock_sha256": self.framework_lock_sha256,
            "framework_wheel_sha256": self.framework_wheel_sha256,
            "topology_template_sha256": self.topology_template_sha256,
            "gateway_bootstrap_sha256": self.gateway_bootstrap_sha256,
            "runtime_python_sha256": self.runtime_python_sha256,
            "openevo_version": self.openevo_version,
            "completion_root": self.completion_root.payload,
            "rollout_process": self.rollout.payload,
            "gateway_process": self.gateway.payload,
            "gateway_container": self.gateway_container.payload,
            "receipt_sha256": self.receipt_sha256,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_pretty_json_bytes(self.public_identity))

    def require_identity_current(self) -> None:
        _require_completion_root_identity(self)
        _require_process_identity(self.rollout)
        _require_process_identity(
            self.gateway,
            gateway_container_name=self.gateway_container.container_name,
        )
        _require_gateway_container_identity(self)

    def require_current(self) -> dict[str, object]:
        self.require_identity_current()
        health = _require_service_health()
        return {
            "runtime_services_identity_sha256": self.digest,
            "rollout_health_sha256": sha256_bytes(
                canonical_pretty_json_bytes(health["rollout"])
            ),
            "gateway_health_sha256": sha256_bytes(
                canonical_pretty_json_bytes(health["gateway"])
            ),
            "completion_root_identity_sha256": sha256_bytes(
                canonical_pretty_json_bytes(self.completion_root.payload)
            ),
        }


@dataclass(frozen=True, slots=True)
class PersistedCompletionAuditV1:
    service_run_id: str
    completion_id: str
    record_sha256: str
    response_sha256: str
    record_size_bytes: int
    task_id_sha256: str
    session_id_sha256: str
    record: Mapping[str, Any]

    @property
    def public_identity(self) -> dict[str, object]:
        return {
            "schema_version": COMPLETION_AUDIT_SCHEMA,
            "status": "DURABLE_COMPLETE_RECORD_PROVEN",
            "service_run_id": self.service_run_id,
            "completion_id": self.completion_id,
            "record_sha256": self.record_sha256,
            "response_sha256": self.response_sha256,
            "record_size_bytes": self.record_size_bytes,
            "task_id_sha256": self.task_id_sha256,
            "session_id_sha256": self.session_id_sha256,
            "safe_to_repeat_model_call": False,
        }


@dataclass(frozen=True, slots=True)
class PersistedRolloutResultAuditV1:
    state: Literal["PROVEN_COMPLETE", "PROVEN_TERMINAL_NO_COMPLETION", "AMBIGUOUS"]
    service_run_id: str
    task_id_sha256: str
    session_id_sha256: str | None
    result_sha256: str | None
    result_size_bytes: int | None
    terminal_status: str | None
    completion_exists: bool | None
    completion_sha256: str | None
    finding_code: str | None
    result: SessionResult | None

    @property
    def public_identity(self) -> dict[str, object]:
        return {
            "schema_version": ROLLOUT_RESULT_AUDIT_SCHEMA,
            "state": self.state,
            "service_run_id": self.service_run_id,
            "task_id_sha256": self.task_id_sha256,
            "session_id_sha256": self.session_id_sha256,
            "result_sha256": self.result_sha256,
            "result_size_bytes": self.result_size_bytes,
            "terminal_status": self.terminal_status,
            "completion_exists": self.completion_exists,
            "completion_sha256": self.completion_sha256,
            "finding_code": self.finding_code,
            # Even a proven no-completion record needs the controller's
            # infrastructure classification and retry policy.  Evidence alone
            # never authorizes another paid call.
            "safe_to_repeat_model_call": False,
        }


@dataclass(frozen=True, slots=True)
class DurableNoCompletionEvidenceV1:
    state: Literal["PROVEN_NO_COMPLETION", "AMBIGUOUS"]
    service_run_id: str
    task_id_sha256: str
    session_id_sha256: str | None
    rollout_result_sha256: str | None
    rollout_terminal_status: str | None
    durable_rollout_no_completion: bool
    durable_gateway_absent: bool
    gateway_absence_basis: str | None
    finding_code: str | None

    @property
    def evidence_body(self) -> dict[str, object]:
        return {
            "schema_version": NO_COMPLETION_EVIDENCE_SCHEMA,
            "state": self.state,
            "service_run_id": self.service_run_id,
            "task_id_sha256": self.task_id_sha256,
            "session_id_sha256": self.session_id_sha256,
            "rollout_result_sha256": self.rollout_result_sha256,
            "rollout_terminal_status": self.rollout_terminal_status,
            "durable_rollout_no_completion": self.durable_rollout_no_completion,
            "durable_gateway_absent": self.durable_gateway_absent,
            "gateway_absence_basis": self.gateway_absence_basis,
            "finding_code": self.finding_code,
            "safe_to_repeat_model_call": False,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_pretty_json_bytes(self.evidence_body))

    @property
    def receipt(self) -> dict[str, object]:
        return {
            **self.evidence_body,
            "no_completion_evidence_sha256": self.digest,
        }

    @property
    def ledger_fields(self) -> dict[str, object]:
        """Return the exact dual-proof fields accepted by the experiment ledger."""

        if (
            self.state != "PROVEN_NO_COMPLETION"
            or not self.durable_rollout_no_completion
            or not self.durable_gateway_absent
        ):
            raise TemperatureRuntimeServicesError("DURABLE_NO_COMPLETION_NOT_PROVEN")
        return {
            "durable_rollout_no_completion": True,
            "durable_gateway_absent": True,
            "no_completion_evidence_sha256": self.digest,
        }


def start_temperature_runtime_services_v1(
    *,
    repository_root: Path,
    service_run_id: str,
) -> TemperatureRuntimeServicesIdentityV1:
    """Start the shared services without submitting any model work."""

    repository = _repository(repository_root)
    if _RUN_ID_RE.fullmatch(service_run_id) is None:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RUN_ID_INVALID")
    paths = _runtime_paths(repository)
    service_root = paths["service_root"]
    service_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    service_root.chmod(0o700)
    runs_root = service_root / "runs"
    runs_root.mkdir(mode=0o700, exist_ok=True)
    runs_root.chmod(0o700)
    pointer = service_root / "current.json"
    if pointer.exists():
        try:
            existing = load_temperature_runtime_services_v1(repository_root=repository)
            existing.require_current()
        except TemperatureRuntimeServicesError:
            raise TemperatureRuntimeServicesError(
                "TEMPERATURE_RUNTIME_EXISTING_STATE_UNRESOLVED"
            ) from None
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_ALREADY_RUNNING")

    run_root = runs_root / service_run_id
    if run_root.exists():
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RUN_ID_ALREADY_EXISTS")
    run_root.mkdir(mode=0o700)
    _validate_runtime_inputs(paths)
    metadata = _runtime_metadata(paths)
    source_commit = _git(repository, "rev-parse", "HEAD")

    completion_root_path = run_root / "completion_persistence"
    completion_root_path.mkdir(mode=0o700)
    completion_root_path.chmod(0o700)
    marker_payload = {
        "schema_version": COMPLETION_ROOT_SCHEMA,
        "service_run_id": service_run_id,
        "source_commit": source_commit,
        "nonce": secrets.token_hex(32),
    }
    marker_path = completion_root_path / COMPLETION_ROOT_MARKER
    _exclusive_private_write(marker_path, canonical_pretty_json_bytes(marker_payload))
    marker_sha256 = _sha256_file(marker_path)

    host_topology_path = run_root / HOST_TOPOLOGY_FILENAME
    host_topology_bytes = _render_host_topology(
        paths["topology"],
        completion_root=completion_root_path,
    )
    _exclusive_private_write(host_topology_path, host_topology_bytes)
    completion_identity = _capture_completion_root_identity(
        repository=repository,
        completion_root=completion_root_path,
        host_topology=host_topology_path,
        marker_sha256=marker_sha256,
    )

    environment = _service_environment(repository)
    candidate = load_managed_candidate_codex_v2(repository_root=repository)
    auth_source = codex_subscription_auth_source_v2()
    docker_launcher = _resolve_docker_launcher()
    gateway_container_name = _gateway_container_name(service_run_id)
    gateway_runtime_root = run_root / "gateway_runtime"
    gateway_runtime_root.mkdir(mode=0o700)
    gateway_runtime_root.chmod(0o700)
    processes: dict[str, subprocess.Popen[bytes]] = {}
    log_streams: list[Any] = []
    try:
        for service in ("rollout", "gateway"):
            log_path = run_root / f"{service}.log"
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stream = os.fdopen(descriptor, "wb", buffering=0)
            log_streams.append(stream)
            command = (
                [
                    os.fspath(paths["runtime_python"]),
                    "-I",
                    "-m",
                    _EXPECTED_MODULES[service],
                    "--config",
                    os.fspath(host_topology_path),
                    "--log-level",
                    "warning",
                ]
                if service == "rollout"
                else _gateway_container_command(
                    repository=repository,
                    paths=paths,
                    source_commit=source_commit,
                    service_run_id=service_run_id,
                    container_name=gateway_container_name,
                    gateway_runtime_root=gateway_runtime_root,
                    completion_root=completion_root_path,
                    host_topology=host_topology_path,
                    completion_root_marker_sha256=marker_sha256,
                    image_id=candidate.image_id,
                    auth_source=auth_source,
                    docker_launcher=docker_launcher,
                )
            )
            processes[service] = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                cwd=repository,
                env=environment,
                start_new_session=True,
            )
            _wait_for_service_start(
                process=processes[service],
                url=f"{ROLLOUT_URL if service == 'rollout' else GATEWAY_URL}/health",
            )
        _wait_for_schedulable_rollout(processes["rollout"])
        gateway_container = _load_gateway_container_identity(
            repository=repository,
            gateway_runtime_root=gateway_runtime_root,
            host_topology=host_topology_path,
            completion_root=completion_root_path,
            container_name=gateway_container_name,
            expected_image_id=candidate.image_id,
            docker_launcher=docker_launcher,
            auth_source=auth_source,
            source_commit=source_commit,
            service_run_id=service_run_id,
            marker_sha256=marker_sha256,
        )
        identities = {
            service: _capture_process_identity(
                service,
                process.pid,
                gateway_container_name=(gateway_container_name if service == "gateway" else None),
            )
            for service, process in processes.items()
        }
        health = _require_service_health()
        receipt = {
            "schema_version": SERVICE_RECEIPT_SCHEMA,
            "service_run_id": service_run_id,
            "source_commit": source_commit,
            **metadata,
            "rollout_url": ROLLOUT_URL,
            "gateway_url": GATEWAY_URL,
            "completion_root": completion_identity.payload,
            "rollout_process": identities["rollout"].payload,
            "gateway_process": identities["gateway"].payload,
            "gateway_container": gateway_container.payload,
            "rollout_health_sha256": sha256_bytes(
                canonical_pretty_json_bytes(health["rollout"])
            ),
            "gateway_health_sha256": sha256_bytes(
                canonical_pretty_json_bytes(health["gateway"])
            ),
        }
        receipt_path = run_root / "runtime_services_receipt_v1.json"
        _exclusive_private_write(receipt_path, canonical_pretty_json_bytes(receipt))
        _exclusive_private_write(
            pointer,
            canonical_pretty_json_bytes(
                {
                    "schema_version": SERVICE_POINTER_SCHEMA,
                    "service_run_id": service_run_id,
                    "receipt_sha256": _sha256_file(receipt_path),
                }
            ),
        )
        return load_temperature_runtime_services_v1(repository_root=repository)
    except BaseException:
        _terminate_processes(
            processes,
            gateway_container_name=gateway_container_name,
            source_commit=source_commit,
            service_run_id=service_run_id,
            marker_sha256=marker_sha256,
        )
        raise
    finally:
        for stream in log_streams:
            stream.close()


def load_temperature_runtime_services_v1(
    *, repository_root: Path
) -> TemperatureRuntimeServicesIdentityV1:
    repository = _repository(repository_root)
    identity = _load_runtime_identity(
        repository,
        service_run_id=None,
        require_current_source=True,
    )
    identity.require_current()
    return identity


def load_temperature_runtime_evidence_v1(
    *,
    repository_root: Path,
    service_run_id: str,
) -> TemperatureRuntimeServicesIdentityV1:
    """Load immutable completion-root evidence without requiring live processes."""

    repository = _repository(repository_root)
    identity = _load_runtime_identity(
        repository,
        service_run_id=service_run_id,
        require_current_source=False,
    )
    _require_completion_root_identity(identity)
    return identity


def temperature_runtime_services_status_v1(*, repository_root: Path) -> dict[str, object]:
    identity = load_temperature_runtime_services_v1(repository_root=repository_root)
    evidence = identity.require_current()
    return {
        "status": "PASS",
        "service_run_id": identity.service_run_id,
        "source_commit": identity.source_commit,
        "runtime_services_identity_sha256": identity.digest,
        **evidence,
    }


def audit_empty_temperature_runtime_services_v1(
    *, repository_root: Path
) -> dict[str, object]:
    """Prove that the fresh service owns no Rollout task or persisted result.

    This is a zero-model-call admission check.  It combines the in-memory
    Rollout task inventory with the no-follow, identity-bound completion root;
    either surface being non-empty fails closed.
    """

    identity = load_temperature_runtime_services_v1(repository_root=repository_root)
    identity.require_current()
    try:
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            response = client.get(f"{identity.rollout_url}/tasks", params={"limit": 1})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_EMPTY_INVENTORY_UNAVAILABLE"
        ) from exc
    if type(payload) is not dict or set(payload) != {"tasks"} or payload["tasks"] != []:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_NOT_EMPTY")

    root = identity.repository_root / identity.completion_root.root_relative
    root_fd = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _require_open_root_identity(root_fd, identity.completion_root)
        entries = sorted(os.listdir(root_fd))
        if entries != [COMPLETION_ROOT_MARKER]:
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_NOT_EMPTY")
        marker = _read_private_record_at(root_fd, COMPLETION_ROOT_MARKER)
        if hashlib.sha256(marker).hexdigest() != identity.completion_root.marker_sha256:
            raise TemperatureRuntimeServicesError(
                "TEMPERATURE_RUNTIME_COMPLETION_ROOT_MARKER_DRIFT"
            )
        if sorted(os.listdir(root_fd)) != entries:
            raise TemperatureRuntimeServicesError(
                "TEMPERATURE_RUNTIME_EMPTY_INVENTORY_CHANGED"
            )
    finally:
        os.close(root_fd)
    receipt: dict[str, object] = {
        "schema_version": EMPTY_RUNTIME_INVENTORY_SCHEMA,
        "service_run_id": identity.service_run_id,
        "runtime_services_identity_sha256": identity.digest,
        "rollout_task_count": 0,
        "persisted_task_directory_count": 0,
        "completion_root_marker_sha256": identity.completion_root.marker_sha256,
        "model_calls_observed": 0,
    }
    receipt["inventory_sha256"] = sha256_bytes(canonical_pretty_json_bytes(receipt))
    return receipt


def stop_temperature_runtime_services_v1(*, repository_root: Path) -> dict[str, object]:
    """Stop only the processes and container bound by the current receipt."""

    repository = _repository(repository_root)
    identity = _load_runtime_identity(
        repository,
        service_run_id=None,
        require_current_source=False,
    )
    _require_completion_root_identity(identity)
    active_processes: dict[str, RuntimeProcessIdentityV2] = {}
    if _process_exists(identity.rollout.pid):
        _require_process_identity(identity.rollout)
        active_processes[identity.rollout.service] = identity.rollout
    if _process_exists(identity.gateway.pid):
        _require_process_identity(
            identity.gateway,
            gateway_container_name=identity.gateway_container.container_name,
        )
        active_processes[identity.gateway.service] = identity.gateway
    for service in ("gateway", "rollout"):
        process = active_processes.get(service)
        if process is None:
            continue
        try:
            os.killpg(process.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    while time.monotonic() < deadline and any(
        _process_exists(process.pid) for process in active_processes.values()
    ):
        time.sleep(0.1)
    for process in active_processes.values():
        if _process_exists(process.pid):
            _require_process_identity(
                process,
                gateway_container_name=(
                    identity.gateway_container.container_name
                    if process.service == "gateway"
                    else None
                ),
            )
            os.killpg(process.process_group_id, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(
        _process_exists(process.pid) for process in active_processes.values()
    ):
        time.sleep(0.1)
    if any(_process_exists(process.pid) for process in active_processes.values()):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_CLEANUP_INCOMPLETE")
    _remove_gateway_container_if_owned(
        container_name=identity.gateway_container.container_name,
        expected_container_id=identity.gateway_container.container_id,
        source_commit=identity.source_commit,
        service_run_id=identity.service_run_id,
        marker_sha256=identity.completion_root.marker_sha256,
    )
    stop_receipt = {
        "schema_version": SERVICE_STOP_RECEIPT_SCHEMA,
        "service_run_id": identity.service_run_id,
        "runtime_services_identity_sha256": identity.digest,
        "completion_root_marker_sha256": identity.completion_root.marker_sha256,
        "completion_evidence_preserved": True,
        "cleanup_complete": True,
    }
    _exclusive_private_write(
        identity.receipt_path.parent / "runtime_services_stop_receipt_v1.json",
        canonical_pretty_json_bytes(stop_receipt),
    )
    (identity.repository_root / SERVICE_ROOT_RELATIVE / "current.json").unlink()
    return stop_receipt


def audit_persisted_rollout_result_v1(
    *,
    repository_root: Path,
    service_run_id: str,
    task_id: str,
) -> PersistedRolloutResultAuditV1:
    """Audit the single synchronous Rollout result for a claimed task.

    The manager-generated session id is learned from the one durable result
    filename and then checked against the validated body.  ``AMBIGUOUS`` is a
    fail-closed state, not evidence that no completion occurred.
    """

    if _RUN_ID_RE.fullmatch(service_run_id) is None:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RUN_ID_INVALID")
    if _SAFE_ID_RE.fullmatch(task_id) is None:
        raise TemperatureRuntimeServicesError("PERSISTED_ROLLOUT_RESULT_ID_INVALID")
    task_id_sha256 = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    try:
        identity = load_temperature_runtime_evidence_v1(
            repository_root=repository_root,
            service_run_id=service_run_id,
        )
        root = identity.repository_root / identity.completion_root.root_relative
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors = [root_fd]
        try:
            _require_open_root_identity(root_fd, identity.completion_root)
            task_fd = _open_private_directory(root_fd, f"task_{task_id}")
            descriptors.append(task_fd)
            names = sorted(os.listdir(task_fd))
            result_names = [
                name for name in names if _ROLLOUT_RESULT_FILENAME_RE.fullmatch(name)
            ]
            allowed = set(result_names)
            if "sessions" in names:
                sessions_fd = _open_private_directory(task_fd, "sessions")
                os.close(sessions_fd)
                allowed.add("sessions")
            if len(result_names) != 1 or set(names) != allowed:
                raise TemperatureRuntimeServicesError(
                    "PERSISTED_ROLLOUT_RESULT_AMBIGUOUS"
                )
            result_name = result_names[0]
            match = _ROLLOUT_RESULT_FILENAME_RE.fullmatch(result_name)
            assert match is not None
            session_id = match.group("session_id")
            encoded = _read_private_record_at(task_fd, result_name)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        result = _decode_rollout_storage_result(encoded)
        if result.task_id != task_id or result.session_id != session_id:
            raise TemperatureRuntimeServicesError("PERSISTED_ROLLOUT_RESULT_AMBIGUOUS")
    except (OSError, TemperatureRuntimeServicesError, ValueError) as exc:
        del exc
        return _ambiguous_rollout_result(
            service_run_id=service_run_id,
            task_id_sha256=task_id_sha256,
        )

    responses = [
        str(message["content"])
        for trace in result.trajectory.traces
        for message in trace.response_messages
        if isinstance(message, Mapping)
        and message.get("role") == "assistant"
        and isinstance(message.get("content"), str)
        and str(message["content"]).strip()
    ]
    completion_exists = bool(responses)
    completion_sha256 = (
        hashlib.sha256("\n".join(responses).encode("utf-8")).hexdigest()
        if responses
        else None
    )
    state: Literal["PROVEN_COMPLETE", "PROVEN_TERMINAL_NO_COMPLETION"] = (
        "PROVEN_COMPLETE" if completion_exists else "PROVEN_TERMINAL_NO_COMPLETION"
    )
    return PersistedRolloutResultAuditV1(
        state=state,
        service_run_id=service_run_id,
        task_id_sha256=task_id_sha256,
        session_id_sha256=hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
        result_sha256=hashlib.sha256(encoded).hexdigest(),
        result_size_bytes=len(encoded),
        terminal_status=result.status.value,
        completion_exists=completion_exists,
        completion_sha256=completion_sha256,
        finding_code=None,
        result=result,
    )


def audit_persisted_completion_v1(
    *,
    repository_root: Path,
    service_run_id: str,
    task_id: str,
    session_id: str,
    expected_completion_id: str | None = None,
    expected_response_sha256: str | None = None,
) -> PersistedCompletionAuditV1:
    """Prove one auxiliary Gateway-proxy completion after infrastructure loss.

    The returned ``record`` is private recovery material.  Only
    ``public_identity`` is suitable for an aggregate receipt.  Failure to prove
    exactly one complete record raises ``PERSISTED_COMPLETION_NOT_PROVEN``.
    Subscription transcript recovery must use
    :func:`audit_persisted_rollout_result_v1`; this auxiliary file is never a
    required success condition.
    """

    if _SAFE_ID_RE.fullmatch(task_id) is None or _SAFE_ID_RE.fullmatch(session_id) is None:
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_ID_INVALID")
    if expected_completion_id is not None and _SAFE_ID_RE.fullmatch(expected_completion_id) is None:
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_ID_INVALID")
    if (
        expected_response_sha256 is not None
        and _SHA256_RE.fullmatch(expected_response_sha256) is None
    ):
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_DIGEST_INVALID")

    identity = load_temperature_runtime_evidence_v1(
        repository_root=repository_root,
        service_run_id=service_run_id,
    )
    root = identity.repository_root / identity.completion_root.root_relative
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN") from exc
    descriptors = [root_fd]
    try:
        _require_open_root_identity(root_fd, identity.completion_root)
        task_fd = _open_private_directory(root_fd, f"task_{task_id}")
        descriptors.append(task_fd)
        sessions_fd = _open_private_directory(task_fd, "sessions")
        descriptors.append(sessions_fd)
        session_fd = _open_private_directory(sessions_fd, session_id)
        descriptors.append(session_fd)
        completions_fd = _open_private_directory(session_fd, "completions")
        descriptors.append(completions_fd)
        names = sorted(os.listdir(completions_fd))
        if (
            len(names) != 1
            or len(names) > MAX_COMPLETION_FILES_PER_SESSION
            or _COMPLETION_FILENAME_RE.fullmatch(names[0]) is None
        ):
            raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
        match = _COMPLETION_FILENAME_RE.fullmatch(names[0])
        assert match is not None
        completion_id = match.group("completion_id")
        if match.group("sequence") != "0001" or (
            expected_completion_id is not None and completion_id != expected_completion_id
        ):
            raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
        encoded = _read_private_record_at(completions_fd, names[0])
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    record = _decode_completion_record(encoded)
    if (
        record.get("task_id") != task_id
        or record.get("session_id") != session_id
        or record.get("completion_id") != completion_id
    ):
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
    _require_untruncated_record(record)
    response = record.get("response")
    if not isinstance(response, dict):
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
    response_sha256 = sha256_bytes(canonical_pretty_json_bytes(response))
    if expected_response_sha256 is not None and response_sha256 != expected_response_sha256:
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_DIGEST_MISMATCH")
    return PersistedCompletionAuditV1(
        service_run_id=identity.service_run_id,
        completion_id=completion_id,
        record_sha256=hashlib.sha256(encoded).hexdigest(),
        response_sha256=response_sha256,
        record_size_bytes=len(encoded),
        task_id_sha256=hashlib.sha256(task_id.encode("utf-8")).hexdigest(),
        session_id_sha256=hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
        record=record,
    )


def audit_durable_no_completion_v1(
    *,
    repository_root: Path,
    service_run_id: str,
    task_id: str,
) -> DurableNoCompletionEvidenceV1:
    """Aggregate the two durable proofs required before an infrastructure retry.

    A terminal Rollout result without an assistant response is necessary but
    insufficient.  The exact Gateway completion path derived from the
    manager-generated session id must also be absent in a stable, no-follow
    inventory.  Any malformed, unexpected, or populated path is ambiguous and
    cannot produce ledger retry fields.
    """

    rollout = audit_persisted_rollout_result_v1(
        repository_root=repository_root,
        service_run_id=service_run_id,
        task_id=task_id,
    )
    if rollout.state != "PROVEN_TERMINAL_NO_COMPLETION" or rollout.result is None:
        return _ambiguous_no_completion_evidence(
            rollout=rollout,
            durable_rollout_no_completion=False,
        )
    try:
        identity = load_temperature_runtime_evidence_v1(
            repository_root=repository_root,
            service_run_id=service_run_id,
        )
        basis = _prove_gateway_completion_path_absent(
            identity=identity,
            task_id=task_id,
            session_id=rollout.result.session_id,
            expected_rollout_result_sha256=rollout.result_sha256,
        )
    except (OSError, TemperatureRuntimeServicesError, ValueError):
        return _ambiguous_no_completion_evidence(
            rollout=rollout,
            durable_rollout_no_completion=True,
        )
    return DurableNoCompletionEvidenceV1(
        state="PROVEN_NO_COMPLETION",
        service_run_id=service_run_id,
        task_id_sha256=rollout.task_id_sha256,
        session_id_sha256=rollout.session_id_sha256,
        rollout_result_sha256=rollout.result_sha256,
        rollout_terminal_status=rollout.terminal_status,
        durable_rollout_no_completion=True,
        durable_gateway_absent=True,
        gateway_absence_basis=basis,
        finding_code=None,
    )


def _load_runtime_identity(
    repository: Path,
    *,
    service_run_id: str | None,
    require_current_source: bool,
) -> TemperatureRuntimeServicesIdentityV1:
    service_root = repository / SERVICE_ROOT_RELATIVE
    pointer_digest: str | None = None
    if service_run_id is None:
        pointer_path = service_root / "current.json"
        pointer = _read_private_json(pointer_path, maximum=4096)
        if type(pointer) is not dict or set(pointer) != {
            "schema_version",
            "service_run_id",
            "receipt_sha256",
        }:
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_POINTER_INVALID")
        service_run_id = pointer["service_run_id"]
        pointer_digest = pointer["receipt_sha256"]
        if (
            pointer["schema_version"] != SERVICE_POINTER_SCHEMA
            or type(service_run_id) is not str
            or _RUN_ID_RE.fullmatch(service_run_id) is None
            or type(pointer_digest) is not str
            or _SHA256_RE.fullmatch(pointer_digest) is None
        ):
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_POINTER_INVALID")
    elif _RUN_ID_RE.fullmatch(service_run_id) is None:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RUN_ID_INVALID")

    receipt_path = service_root / "runs" / service_run_id / "runtime_services_receipt_v1.json"
    encoded = _read_private_bytes(receipt_path, maximum=128 * 1024)
    receipt_sha256 = hashlib.sha256(encoded).hexdigest()
    if pointer_digest is not None and receipt_sha256 != pointer_digest:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_DIGEST_MISMATCH")
    receipt = _decode_json_object(encoded, "TEMPERATURE_RUNTIME_RECEIPT_INVALID")
    expected_keys = {
        "schema_version",
        "service_run_id",
        "source_commit",
        "framework_lock_sha256",
        "framework_wheel_sha256",
        "topology_template_sha256",
        "gateway_bootstrap_sha256",
        "runtime_python_sha256",
        "openevo_version",
        "rollout_url",
        "gateway_url",
        "completion_root",
        "rollout_process",
        "gateway_process",
        "gateway_container",
        "rollout_health_sha256",
        "gateway_health_sha256",
    }
    if set(receipt) != expected_keys or (
        receipt["schema_version"] != SERVICE_RECEIPT_SCHEMA
        or receipt["service_run_id"] != service_run_id
        or receipt["rollout_url"] != ROLLOUT_URL
        or receipt["gateway_url"] != GATEWAY_URL
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
    digest_fields = (
        "framework_lock_sha256",
        "framework_wheel_sha256",
        "topology_template_sha256",
        "gateway_bootstrap_sha256",
        "runtime_python_sha256",
        "rollout_health_sha256",
        "gateway_health_sha256",
    )
    if any(
        type(receipt[key]) is not str or _SHA256_RE.fullmatch(receipt[key]) is None
        for key in digest_fields
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
    if (
        type(receipt["source_commit"]) is not str
        or re.fullmatch(r"[0-9a-f]{40}", receipt["source_commit"]) is None
        or type(receipt["openevo_version"]) is not str
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", receipt["openevo_version"]) is None
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")

    completion_root = CompletionRootIdentityV1.from_payload(receipt["completion_root"])
    expected_root_relative = (
        f"{SERVICE_ROOT_RELATIVE}/runs/{service_run_id}/completion_persistence"
    )
    expected_topology_relative = (
        f"{SERVICE_ROOT_RELATIVE}/runs/{service_run_id}/{HOST_TOPOLOGY_FILENAME}"
    )
    if (
        completion_root.root_relative != expected_root_relative
        or completion_root.host_topology_relative != expected_topology_relative
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")
    try:
        rollout = RuntimeProcessIdentityV2.from_payload(receipt["rollout_process"])
        gateway = RuntimeProcessIdentityV2.from_payload(receipt["gateway_process"])
    except v2_services.RuntimeServicesV2Error as exc:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID") from exc
    gateway_container = TemperatureGatewayContainerIdentityV1.from_payload(
        receipt["gateway_container"]
    )
    if gateway_container.completion_root_marker_sha256 != completion_root.marker_sha256:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_RECEIPT_INVALID")

    if require_current_source:
        metadata = _runtime_metadata(_runtime_paths(repository))
        if any(receipt.get(key) != value for key, value in metadata.items()) or (
            receipt["source_commit"] != _git(repository, "rev-parse", "HEAD")
        ):
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_SOURCE_DRIFT")
    identity = TemperatureRuntimeServicesIdentityV1(
        repository_root=repository,
        service_run_id=service_run_id,
        source_commit=receipt["source_commit"],
        framework_lock_sha256=receipt["framework_lock_sha256"],
        framework_wheel_sha256=receipt["framework_wheel_sha256"],
        topology_template_sha256=receipt["topology_template_sha256"],
        gateway_bootstrap_sha256=receipt["gateway_bootstrap_sha256"],
        runtime_python_sha256=receipt["runtime_python_sha256"],
        openevo_version=receipt["openevo_version"],
        completion_root=completion_root,
        rollout=rollout,
        gateway=gateway,
        gateway_container=gateway_container,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
    )
    _require_completion_root_identity(identity)
    return identity


def _runtime_paths(repository: Path) -> dict[str, Path]:
    try:
        formal = load_temperature_formal_runtime_v1(repository_root=repository)
    except TemperatureFormalRuntimeError as exc:
        raise TemperatureRuntimeServicesError(exc.finding_code) from exc
    return {
        "service_root": repository / SERVICE_ROOT_RELATIVE,
        "runtime_python": formal.runtime_python,
        "framework_lock": formal.framework_lock,
        "framework_wheel": formal.core_wheel,
        "topology": repository / TOPOLOGY_RELATIVE,
        "gateway_bootstrap": repository / GATEWAY_BOOTSTRAP_RELATIVE,
    }


def _validate_runtime_inputs(paths: dict[str, Path]) -> None:
    try:
        v2_services._validate_runtime_inputs(paths)
    except v2_services.RuntimeServicesV2Error as exc:
        raise TemperatureRuntimeServicesError(exc.finding_code) from exc


def _runtime_metadata(paths: dict[str, Path]) -> dict[str, str]:
    try:
        metadata = v2_services._runtime_metadata(paths)
    except v2_services.RuntimeServicesV2Error as exc:
        raise TemperatureRuntimeServicesError(exc.finding_code) from exc
    return {
        "framework_lock_sha256": metadata["framework_lock_sha256"],
        "framework_wheel_sha256": metadata["framework_wheel_sha256"],
        "topology_template_sha256": metadata["topology_sha256"],
        "gateway_bootstrap_sha256": metadata["gateway_bootstrap_sha256"],
        "runtime_python_sha256": metadata["runtime_python_sha256"],
        "openevo_version": metadata["openevo_version"],
    }


def _render_host_topology(template: Path, *, completion_root: Path) -> bytes:
    if not completion_root.is_absolute():
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_COMPLETION_ROOT_INVALID")
    try:
        loaded = yaml.safe_load(template.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_TOPOLOGY_INVALID") from exc
    if not isinstance(loaded, dict):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_TOPOLOGY_INVALID")
    rollout = loaded.get("rollout")
    gateway = loaded.get("gateway")
    persistence = gateway.get("completion_persistence") if isinstance(gateway, dict) else None
    if (
        not isinstance(rollout, dict)
        or rollout.get("save_dir") != _TOPOLOGY_HOST_MARKER
        or not isinstance(persistence, dict)
        or persistence != {
            "enabled": True,
            "max_field_bytes": COMPLETION_MAX_FIELD_BYTES,
            "queue_size": COMPLETION_QUEUE_SIZE,
        }
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_TOPOLOGY_INVALID")
    rollout["save_dir"] = os.fspath(completion_root)
    try:
        topology = TopologyConfig.model_validate(loaded)
    except Exception as exc:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_TOPOLOGY_INVALID") from exc
    if (
        topology.rollout.host != "127.0.0.1"
        or topology.rollout.port != 8080
        or topology.rollout.public_url != ROLLOUT_URL
        or topology.rollout.save_dir != os.fspath(completion_root)
        or len(topology.gateway.nodes) != 1
        or topology.gateway.nodes[0].id != "core-gateway"
        or topology.gateway.nodes[0].public_url != GATEWAY_URL
        or topology.gateway.nodes[0].max_run_workers != 1
        or not topology.gateway.completion_persistence.enabled
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_TOPOLOGY_INVALID")
    return yaml.safe_dump(
        loaded,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    ).encode("utf-8")


def _capture_completion_root_identity(
    *,
    repository: Path,
    completion_root: Path,
    host_topology: Path,
    marker_sha256: str,
) -> CompletionRootIdentityV1:
    try:
        root_resolved = completion_root.resolve(strict=True)
        root_metadata = completion_root.stat(follow_symlinks=False)
        marker = completion_root / COMPLETION_ROOT_MARKER
        marker_metadata = marker.stat(follow_symlinks=False)
        topology_metadata = host_topology.stat(follow_symlinks=False)
        root_relative = root_resolved.relative_to(repository).as_posix()
        topology_relative = host_topology.resolve(strict=True).relative_to(repository).as_posix()
    except (OSError, ValueError) as exc:
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_COMPLETION_ROOT_INVALID"
        ) from exc
    if (
        root_resolved != completion_root
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or not stat.S_ISREG(marker_metadata.st_mode)
        or marker_metadata.st_uid != os.getuid()
        or marker_metadata.st_nlink != 1
        or stat.S_IMODE(marker_metadata.st_mode) != 0o600
        or _sha256_file(marker) != marker_sha256
        or not stat.S_ISREG(topology_metadata.st_mode)
        or topology_metadata.st_uid != os.getuid()
        or topology_metadata.st_nlink != 1
        or stat.S_IMODE(topology_metadata.st_mode) != 0o600
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_COMPLETION_ROOT_INVALID")
    return CompletionRootIdentityV1(
        root_relative=root_relative,
        host_topology_relative=topology_relative,
        host_topology_sha256=_sha256_file(host_topology),
        marker_sha256=marker_sha256,
        device=root_metadata.st_dev,
        inode=root_metadata.st_ino,
        owner_uid=root_metadata.st_uid,
        mode=stat.S_IMODE(root_metadata.st_mode),
        container_path=GATEWAY_COMPLETION_MOUNT,
        max_field_bytes=COMPLETION_MAX_FIELD_BYTES,
        queue_size=COMPLETION_QUEUE_SIZE,
    )


def _require_completion_root_identity(identity: TemperatureRuntimeServicesIdentityV1) -> None:
    root = identity.repository_root / identity.completion_root.root_relative
    topology = identity.repository_root / identity.completion_root.host_topology_relative
    current = _capture_completion_root_identity(
        repository=identity.repository_root,
        completion_root=root,
        host_topology=topology,
        marker_sha256=identity.completion_root.marker_sha256,
    )
    if current != identity.completion_root:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_COMPLETION_ROOT_DRIFT")
    try:
        loaded = TopologyConfig.load(topology)
    except Exception as exc:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_TOPOLOGY_DRIFT") from exc
    if (
        loaded.rollout.save_dir != os.fspath(root)
        or not loaded.gateway.completion_persistence.enabled
        or loaded.gateway.completion_persistence.max_field_bytes
        != identity.completion_root.max_field_bytes
        or loaded.gateway.completion_persistence.queue_size != identity.completion_root.queue_size
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_TOPOLOGY_DRIFT")


def _resolve_docker_launcher() -> Path:
    try:
        return v2_services._resolve_docker_launcher()
    except v2_services.RuntimeServicesV2Error as exc:
        raise TemperatureRuntimeServicesError(exc.finding_code) from exc


def _gateway_container_name(service_run_id: str) -> str:
    suffix = hashlib.sha256(service_run_id.encode("ascii")).hexdigest()[:24]
    return f"openevo-temperature-gateway-{suffix}"


def _gateway_container_command(
    *,
    repository: Path,
    paths: dict[str, Path],
    source_commit: str,
    service_run_id: str,
    container_name: str,
    gateway_runtime_root: Path,
    completion_root: Path,
    host_topology: Path,
    completion_root_marker_sha256: str,
    image_id: str,
    auth_source: Path,
    docker_launcher: Path,
) -> list[str]:
    socket_path = Path("/var/run/docker.sock")
    try:
        socket_metadata = socket_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_DOCKER_UNAVAILABLE") from exc
    if not stat.S_ISSOCK(socket_metadata.st_mode):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_DOCKER_UNAVAILABLE")
    for path in (
        repository,
        gateway_runtime_root,
        completion_root,
        host_topology,
        auth_source,
        docker_launcher,
    ):
        if any(character in os.fspath(path) for character in (",", "\n", "\x00")):
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_MOUNT_PATH_INVALID")
    namespace = f"stv3-temperature-{hashlib.sha256(service_run_id.encode('ascii')).hexdigest()[:24]}"
    bootstrap_arguments = (
        f"--base-config {shlex.quote(os.fspath(host_topology))} "
        f"--effective-config {shlex.quote(GATEWAY_EFFECTIVE_TOPOLOGY)} "
        f"--receipt {shlex.quote(GATEWAY_BOOTSTRAP_RECEIPT)} "
        f"--runtime-python {shlex.quote(os.fspath(paths['runtime_python']))} "
        f"--namespace {shlex.quote(namespace)} "
        f"--host-completion-root {shlex.quote(os.fspath(completion_root))} "
        f"--container-completion-root {shlex.quote(GATEWAY_COMPLETION_MOUNT)} "
        f"--completion-root-marker-sha256 {completion_root_marker_sha256}"
    )
    shell = "\n".join(
        (
            "set -eu",
            "umask 077",
            f"cp {GATEWAY_DOCKER_SOURCE_MOUNT} /usr/bin/docker",
            "chmod 0755 /usr/bin/docker",
            "install -d -m 0700 -o 1000 -g 1000 /home/openevo/.codex",
            (
                f"install -m 0600 -o 1000 -g 1000 {GATEWAY_AUTH_SOURCE_MOUNT} "
                "/home/openevo/.codex/auth.json"
            ),
            (
                "exec setpriv --reuid 1000 --regid 1000 "
                f"--groups {socket_metadata.st_gid} -- "
                f"{shlex.quote(os.fspath(paths['runtime_python']))} -I "
                f"{shlex.quote(os.fspath(paths['gateway_bootstrap']))} "
                f"{bootstrap_arguments}"
            ),
        )
    )
    return [
        os.fspath(docker_launcher),
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--user",
        "0:0",
        "--group-add",
        str(socket_metadata.st_gid),
        "--security-opt",
        "no-new-privileges:true",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        "127.0.0.1:8100:8100",
        "--label",
        f"{_GATEWAY_CONTAINER_LABEL_PROTOCOL}={_GATEWAY_PROTOCOL_LABEL}",
        "--label",
        f"{_GATEWAY_CONTAINER_LABEL_RUN}={service_run_id}",
        "--label",
        f"{_GATEWAY_CONTAINER_LABEL_SOURCE}={source_commit}",
        "--label",
        f"{_GATEWAY_CONTAINER_LABEL_COMPLETION}={completion_root_marker_sha256}",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--mount",
        (
            f"type=bind,source={os.fspath(docker_launcher)},"
            f"target={GATEWAY_DOCKER_SOURCE_MOUNT},readonly"
        ),
        "--mount",
        (
            f"type=bind,source={os.fspath(repository)},"
            f"target={os.fspath(repository)},readonly"
        ),
        "--mount",
        (
            f"type=bind,source={os.fspath(gateway_runtime_root)},"
            f"target={GATEWAY_RUNTIME_MOUNT}"
        ),
        "--mount",
        (
            f"type=bind,source={os.fspath(completion_root)},"
            f"target={GATEWAY_COMPLETION_MOUNT}"
        ),
        "--mount",
        (
            f"type=bind,source={os.fspath(auth_source)},"
            f"target={GATEWAY_AUTH_SOURCE_MOUNT},readonly"
        ),
        "--entrypoint",
        "/bin/sh",
        image_id,
        "-ceu",
        shell,
    ]


def _load_gateway_container_identity(
    *,
    repository: Path,
    gateway_runtime_root: Path,
    host_topology: Path,
    completion_root: Path,
    container_name: str,
    expected_image_id: str,
    docker_launcher: Path,
    auth_source: Path,
    source_commit: str,
    service_run_id: str,
    marker_sha256: str,
) -> TemperatureGatewayContainerIdentityV1:
    receipt_path = gateway_runtime_root / "bootstrap_receipt.json"
    receipt = _read_private_json(receipt_path, maximum=32 * 1024)
    expected_keys = {
        "schema_version",
        "host_topology_sha256",
        "effective_topology_sha256",
        "docker_engine_identity_sha256",
        "docker_host_path_identity_sha256",
        "container_id",
        "completion_root_marker_sha256",
        "container_completion_root",
        "container_completion_root_identity_sha256",
    }
    if type(receipt) is not dict or set(receipt) != expected_keys:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_GATEWAY_BOOTSTRAP_INVALID")
    digest_keys = expected_keys - {
        "schema_version",
        "container_completion_root",
    }
    if any(
        type(receipt[key]) is not str or _SHA256_RE.fullmatch(receipt[key]) is None
        for key in digest_keys
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_GATEWAY_BOOTSTRAP_INVALID")
    effective = gateway_runtime_root / "effective_topology.yaml"
    if (
        receipt["schema_version"] != _GATEWAY_BOOTSTRAP_RECEIPT_SCHEMA
        or receipt["host_topology_sha256"] != _sha256_file(host_topology)
        or receipt["effective_topology_sha256"] != _sha256_file(effective)
        or receipt["completion_root_marker_sha256"] != marker_sha256
        or receipt["container_completion_root"] != GATEWAY_COMPLETION_MOUNT
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_GATEWAY_BOOTSTRAP_INVALID")
    try:
        effective_topology = TopologyConfig.load(effective)
    except Exception as exc:
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_GATEWAY_BOOTSTRAP_INVALID"
        ) from exc
    if (
        effective_topology.rollout.save_dir != GATEWAY_COMPLETION_MOUNT
        or not effective_topology.gateway.completion_persistence.enabled
        or effective_topology.gateway.completion_persistence.max_field_bytes
        != COMPLETION_MAX_FIELD_BYTES
        or effective_topology.gateway.completion_persistence.queue_size
        != COMPLETION_QUEUE_SIZE
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_GATEWAY_BOOTSTRAP_INVALID")
    inspected = _inspect_gateway_container(
        repository=repository,
        container_name=container_name,
        source_commit=source_commit,
        service_run_id=service_run_id,
        marker_sha256=marker_sha256,
        gateway_runtime_root=gateway_runtime_root,
        completion_root=completion_root,
        auth_source=auth_source,
        docker_launcher=docker_launcher,
    )
    if inspected["container_id"] != receipt["container_id"]:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_GATEWAY_CONTAINER_INVALID")
    if inspected["image_id"] != expected_image_id:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_GATEWAY_CONTAINER_INVALID")
    return TemperatureGatewayContainerIdentityV1(
        container_name=container_name,
        container_id=str(receipt["container_id"]),
        image_id=expected_image_id,
        effective_topology_sha256=str(receipt["effective_topology_sha256"]),
        docker_host_path_identity_sha256=str(
            receipt["docker_host_path_identity_sha256"]
        ),
        docker_engine_identity_sha256=str(receipt["docker_engine_identity_sha256"]),
        docker_launcher_sha256=_sha256_file(docker_launcher),
        completion_mount_sha256=inspected["completion_mount_sha256"],
        completion_root_marker_sha256=marker_sha256,
        container_completion_root_identity_sha256=str(
            receipt["container_completion_root_identity_sha256"]
        ),
    )


def _inspect_gateway_container(
    *,
    repository: Path,
    container_name: str,
    source_commit: str,
    service_run_id: str,
    marker_sha256: str,
    gateway_runtime_root: Path,
    completion_root: Path,
    auth_source: Path,
    docker_launcher: Path,
) -> dict[str, str]:
    try:
        completed = subprocess.run(
            (os.fspath(docker_launcher), "container", "inspect", container_name),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={
                "PATH": _SAFE_PATH,
                "HOME": "/proc/self",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            cwd="/",
        )
    except subprocess.TimeoutExpired as exc:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_UNAVAILABLE") from exc
    try:
        rows = json.loads(completed.stdout)
        row = rows[0]
        labels = row["Config"]["Labels"]
        state = row["State"]
        mounts = row["Mounts"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_GATEWAY_CONTAINER_INVALID"
        ) from exc
    expected_labels = {
        _GATEWAY_CONTAINER_LABEL_PROTOCOL: _GATEWAY_PROTOCOL_LABEL,
        _GATEWAY_CONTAINER_LABEL_RUN: service_run_id,
        _GATEWAY_CONTAINER_LABEL_SOURCE: source_commit,
        _GATEWAY_CONTAINER_LABEL_COMPLETION: marker_sha256,
    }
    if (
        completed.returncode != 0
        or not isinstance(row, dict)
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected_labels.items())
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or type(row.get("Id")) is not str
        or _SHA256_RE.fullmatch(row["Id"]) is None
        or type(row.get("Image")) is not str
        or not row["Image"].startswith("sha256:")
    ):
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_GATEWAY_CONTAINER_INVALID"
        )
    completion_mount_sha256 = _validate_mount_inventory(
        mounts,
        repository=repository,
        gateway_runtime_root=gateway_runtime_root,
        completion_root=completion_root,
        auth_source=auth_source,
        docker_launcher=docker_launcher,
    )
    return {
        "container_id": row["Id"],
        "image_id": row["Image"],
        "completion_mount_sha256": completion_mount_sha256,
    }


def _validate_mount_inventory(
    mounts: object,
    *,
    repository: Path,
    gateway_runtime_root: Path,
    completion_root: Path,
    auth_source: Path,
    docker_launcher: Path,
) -> str:
    if not isinstance(mounts, list):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_MOUNT_IDENTITY_INVALID")
    expected = {
        "/var/run/docker.sock": ("/var/run/docker.sock", True),
        GATEWAY_DOCKER_SOURCE_MOUNT: (os.fspath(docker_launcher), False),
        os.fspath(repository): (os.fspath(repository), False),
        GATEWAY_RUNTIME_MOUNT: (os.fspath(gateway_runtime_root), True),
        GATEWAY_COMPLETION_MOUNT: (os.fspath(completion_root), True),
        GATEWAY_AUTH_SOURCE_MOUNT: (os.fspath(auth_source), False),
    }
    observed: list[dict[str, object]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_MOUNT_IDENTITY_INVALID")
        destination = mount.get("Destination")
        if destination not in expected:
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_MOUNT_IDENTITY_INVALID")
        expected_source, expected_rw = expected[destination]
        if (
            mount.get("Type") != "bind"
            or mount.get("Source") != expected_source
            or mount.get("RW") is not expected_rw
        ):
            raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_MOUNT_IDENTITY_INVALID")
        observed.append(
            {
                "type": "bind",
                "source": expected_source,
                "destination": destination,
                "rw": expected_rw,
                "propagation": mount.get("Propagation", ""),
            }
        )
    if len(observed) != len(expected) or {row["destination"] for row in observed} != set(
        expected
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_MOUNT_IDENTITY_INVALID")
    observed.sort(key=lambda row: str(row["destination"]))
    return sha256_bytes(canonical_pretty_json_bytes(observed))


def _require_gateway_container_identity(identity: TemperatureRuntimeServicesIdentityV1) -> None:
    run_root = identity.receipt_path.parent
    current = _inspect_gateway_container(
        repository=identity.repository_root,
        container_name=identity.gateway_container.container_name,
        source_commit=identity.source_commit,
        service_run_id=identity.service_run_id,
        marker_sha256=identity.completion_root.marker_sha256,
        gateway_runtime_root=run_root / "gateway_runtime",
        completion_root=identity.repository_root / identity.completion_root.root_relative,
        auth_source=codex_subscription_auth_source_v2(),
        docker_launcher=_resolve_docker_launcher(),
    )
    effective = run_root / "gateway_runtime/effective_topology.yaml"
    if (
        current["container_id"] != identity.gateway_container.container_id
        or current["image_id"] != identity.gateway_container.image_id
        or current["completion_mount_sha256"]
        != identity.gateway_container.completion_mount_sha256
        or _sha256_file(_resolve_docker_launcher())
        != identity.gateway_container.docker_launcher_sha256
        or _sha256_file(effective)
        != identity.gateway_container.effective_topology_sha256
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_GATEWAY_CONTAINER_DRIFT")


def _service_environment(repository: Path) -> dict[str, str]:
    home = os.environ.get("HOME")
    if not home or not Path(home).is_absolute():
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_HOME_INVALID")
    return {
        "HOME": home,
        "PATH": _SAFE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "OPENEVO_CHEMBENCH_TEMPERATURE_SOURCE_COMMIT": _git(
            repository, "rev-parse", "HEAD"
        ),
    }


def _wait_for_service_start(*, process: subprocess.Popen[bytes], url: str) -> None:
    try:
        v2_services._wait_for_service_start(process=process, url=url)
    except v2_services.RuntimeServicesV2Error as exc:
        raise TemperatureRuntimeServicesError(exc.finding_code) from exc


def _wait_for_schedulable_rollout(process: subprocess.Popen[bytes]) -> None:
    try:
        v2_services._wait_for_schedulable_rollout(process)
    except v2_services.RuntimeServicesV2Error as exc:
        raise TemperatureRuntimeServicesError(exc.finding_code) from exc


def _require_service_health() -> dict[str, dict[str, Any]]:
    try:
        return v2_services._require_service_health()
    except v2_services.RuntimeServicesV2Error as exc:
        raise TemperatureRuntimeServicesError(exc.finding_code) from exc


def _capture_process_identity(
    service: Literal["rollout", "gateway"],
    pid: int,
    *,
    gateway_container_name: str | None = None,
) -> RuntimeProcessIdentityV2:
    proc = Path("/proc") / str(pid)
    try:
        start_time_ticks = v2_services._proc_start_time_ticks(proc / "stat")
        cmdline = (proc / "cmdline").read_bytes()
        executable = (proc / "exe").resolve(strict=True)
        process_group_id = os.getpgid(pid)
        session_id = os.getsid(pid)
    except (OSError, ValueError) as exc:
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_PROCESS_IDENTITY_INVALID"
        ) from exc
    expected_command = (
        f"-m\x00{_EXPECTED_MODULES[service]}\x00".encode()
        if service == "rollout"
        else GATEWAY_BOOTSTRAP_RELATIVE.encode()
    )
    expected_container = (
        None
        if service == "rollout"
        else f"--name\x00{gateway_container_name}\x00".encode()
    )
    if (
        process_group_id != pid
        or session_id != pid
        or expected_command not in cmdline
        or (expected_container is not None and expected_container not in cmdline)
    ):
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_PROCESS_IDENTITY_INVALID"
        )
    return RuntimeProcessIdentityV2(
        service=service,
        pid=pid,
        process_group_id=process_group_id,
        session_id=session_id,
        start_time_ticks=start_time_ticks,
        executable_sha256=_sha256_file(executable),
        cmdline_sha256=hashlib.sha256(cmdline).hexdigest(),
    )


def _require_process_identity(
    expected: RuntimeProcessIdentityV2,
    *,
    gateway_container_name: str | None = None,
) -> None:
    current = _capture_process_identity(
        expected.service,
        expected.pid,
        gateway_container_name=gateway_container_name,
    )
    if current != expected:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_PROCESS_IDENTITY_DRIFT")


def _terminate_processes(
    processes: dict[str, subprocess.Popen[bytes]],
    *,
    gateway_container_name: str,
    source_commit: str,
    service_run_id: str,
    marker_sha256: str,
) -> None:
    for process in reversed(tuple(processes.values())):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(
        process.poll() is None for process in processes.values()
    ):
        time.sleep(0.1)
    for process in processes.values():
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    _remove_gateway_container_if_owned(
        container_name=gateway_container_name,
        expected_container_id=None,
        source_commit=source_commit,
        service_run_id=service_run_id,
        marker_sha256=marker_sha256,
    )


def _remove_gateway_container_if_owned(
    *,
    container_name: str,
    expected_container_id: str | None,
    source_commit: str,
    service_run_id: str,
    marker_sha256: str,
) -> None:
    docker = _resolve_docker_launcher()
    inspected = subprocess.run(
        (os.fspath(docker), "container", "inspect", container_name),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": _SAFE_PATH,
            "HOME": "/proc/self",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
        cwd="/",
    )
    if inspected.returncode != 0:
        return
    try:
        rows = json.loads(inspected.stdout)
        row = rows[0]
        labels = row["Config"]["Labels"]
        container_id = row["Id"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_CLEANUP_OWNERSHIP_INVALID"
        ) from exc
    if (
        not isinstance(labels, dict)
        or labels.get(_GATEWAY_CONTAINER_LABEL_PROTOCOL) != _GATEWAY_PROTOCOL_LABEL
        or labels.get(_GATEWAY_CONTAINER_LABEL_RUN) != service_run_id
        or labels.get(_GATEWAY_CONTAINER_LABEL_SOURCE) != source_commit
        or labels.get(_GATEWAY_CONTAINER_LABEL_COMPLETION) != marker_sha256
        or type(container_id) is not str
        or _SHA256_RE.fullmatch(container_id) is None
        or (expected_container_id is not None and container_id != expected_container_id)
    ):
        raise TemperatureRuntimeServicesError(
            "TEMPERATURE_RUNTIME_CLEANUP_OWNERSHIP_INVALID"
        )
    removed = subprocess.run(
        (os.fspath(docker), "container", "rm", "--force", container_id),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        env={
            "PATH": _SAFE_PATH,
            "HOME": "/proc/self",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
        cwd="/",
    )
    if removed.returncode != 0:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_CLEANUP_INCOMPLETE")


def _open_private_directory(parent_fd: int, name: str) -> int:
    if _SAFE_ID_RE.fullmatch(name.removeprefix("task_")) is None and name not in {
        "sessions",
        "completions",
    }:
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        # Rollout currently creates task directories with the process umask,
        # commonly 0755.  The exact 0700 completion root is the access-control
        # boundary, so readable child modes remain private while any
        # group/other write permission is unsafe.
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
    return descriptor


def _require_open_root_identity(descriptor: int, expected: CompletionRootIdentityV1) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != expected.device
        or metadata.st_ino != expected.inode
        or metadata.st_uid != expected.owner_uid
        or stat.S_IMODE(metadata.st_mode) != expected.mode
    ):
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")


def _read_private_record_at(parent_fd: int, name: str) -> bytes:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            # Pipeline._persist_result uses Path.write_text and commonly
            # produces 0644.  This is safe below the verified 0700 root, but a
            # group/other-writable result is not authoritative evidence.
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size < 2
            or before.st_size > MAX_COMPLETION_RECORD_BYTES
        ):
            raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
        after = os.fstat(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_rollout_storage_result(encoded: bytes) -> SessionResult:
    payload = _decode_json_object(encoded, "PERSISTED_ROLLOUT_RESULT_AMBIGUOUS")
    expected = {
        "session_id",
        "task_id",
        "status",
        "trajectory",
        "timing",
        "node_id",
        "error",
        "metadata",
    }
    if set(payload) != expected:
        raise TemperatureRuntimeServicesError("PERSISTED_ROLLOUT_RESULT_AMBIGUOUS")
    if (
        type(payload["session_id"]) is not str
        or _SAFE_ID_RE.fullmatch(payload["session_id"]) is None
        or type(payload["task_id"]) is not str
        or _SAFE_ID_RE.fullmatch(payload["task_id"]) is None
        or type(payload["status"]) is not str
        or payload["status"] not in {value.value for value in SessionStatus.terminal()}
        or type(payload["trajectory"]) is not dict
        or set(payload["trajectory"]) != {"metadata", "traces"}
    ):
        raise TemperatureRuntimeServicesError("PERSISTED_ROLLOUT_RESULT_AMBIGUOUS")

    # Rollout's storage ABI deliberately removes the duplicated trajectory
    # status/error fields.  Restore those fields from the authoritative
    # session-level values, then validate the complete public SessionResult
    # model rather than trusting an ad-hoc subset of the JSON body.
    restored = dict(payload)
    restored_trajectory = dict(payload["trajectory"])
    restored_trajectory["status"] = payload["status"]
    restored_trajectory["error"] = payload["error"]
    restored["trajectory"] = restored_trajectory
    try:
        result = SessionResult.model_validate(restored)
    except ValidationError as exc:
        raise TemperatureRuntimeServicesError(
            "PERSISTED_ROLLOUT_RESULT_AMBIGUOUS"
        ) from exc
    if result.status not in SessionStatus.terminal():
        raise TemperatureRuntimeServicesError("PERSISTED_ROLLOUT_RESULT_AMBIGUOUS")
    return result


def _ambiguous_rollout_result(
    *,
    service_run_id: str,
    task_id_sha256: str,
) -> PersistedRolloutResultAuditV1:
    return PersistedRolloutResultAuditV1(
        state="AMBIGUOUS",
        service_run_id=service_run_id,
        task_id_sha256=task_id_sha256,
        session_id_sha256=None,
        result_sha256=None,
        result_size_bytes=None,
        terminal_status=None,
        completion_exists=None,
        completion_sha256=None,
        finding_code="PERSISTED_ROLLOUT_RESULT_AMBIGUOUS",
        result=None,
    )


def _prove_gateway_completion_path_absent(
    *,
    identity: TemperatureRuntimeServicesIdentityV1,
    task_id: str,
    session_id: str,
    expected_rollout_result_sha256: str | None,
) -> str:
    if (
        _SAFE_ID_RE.fullmatch(task_id) is None
        or _SAFE_ID_RE.fullmatch(session_id) is None
        or expected_rollout_result_sha256 is None
        or _SHA256_RE.fullmatch(expected_rollout_result_sha256) is None
    ):
        raise TemperatureRuntimeServicesError("DURABLE_NO_COMPLETION_NOT_PROVEN")
    root = identity.repository_root / identity.completion_root.root_relative
    root_fd = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptors = [root_fd]
    inventory_checks: list[tuple[int, list[str]]] = []
    try:
        _require_open_root_identity(root_fd, identity.completion_root)
        task_fd = _open_private_directory(root_fd, f"task_{task_id}")
        descriptors.append(task_fd)
        rollout_name = f"ses_{session_id}.json"
        task_names = sorted(os.listdir(task_fd))
        if set(task_names) not in ({rollout_name}, {rollout_name, "sessions"}):
            raise TemperatureRuntimeServicesError("DURABLE_NO_COMPLETION_NOT_PROVEN")
        encoded = _read_private_record_at(task_fd, rollout_name)
        if hashlib.sha256(encoded).hexdigest() != expected_rollout_result_sha256:
            raise TemperatureRuntimeServicesError("DURABLE_NO_COMPLETION_NOT_PROVEN")
        inventory_checks.append((task_fd, task_names))

        if "sessions" not in task_names:
            basis = "GATEWAY_SESSIONS_DIRECTORY_ABSENT"
        else:
            sessions_fd = _open_private_directory(task_fd, "sessions")
            descriptors.append(sessions_fd)
            session_names = sorted(os.listdir(sessions_fd))
            if session_names not in ([], [session_id]):
                raise TemperatureRuntimeServicesError(
                    "DURABLE_NO_COMPLETION_NOT_PROVEN"
                )
            inventory_checks.append((sessions_fd, session_names))
            if not session_names:
                basis = "GATEWAY_SESSION_DIRECTORY_ABSENT"
            else:
                session_fd = _open_private_directory(sessions_fd, session_id)
                descriptors.append(session_fd)
                session_entries = sorted(os.listdir(session_fd))
                if session_entries not in ([], ["completions"]):
                    raise TemperatureRuntimeServicesError(
                        "DURABLE_NO_COMPLETION_NOT_PROVEN"
                    )
                inventory_checks.append((session_fd, session_entries))
                if not session_entries:
                    basis = "GATEWAY_COMPLETIONS_DIRECTORY_ABSENT"
                else:
                    completions_fd = _open_private_directory(session_fd, "completions")
                    descriptors.append(completions_fd)
                    completion_names = sorted(os.listdir(completions_fd))
                    if completion_names:
                        raise TemperatureRuntimeServicesError(
                            "DURABLE_NO_COMPLETION_NOT_PROVEN"
                        )
                    inventory_checks.append((completions_fd, completion_names))
                    basis = "GATEWAY_COMPLETIONS_DIRECTORY_EMPTY"

        # Detect directory or result mutation during the no-follow inventory.
        if any(sorted(os.listdir(fd)) != expected for fd, expected in inventory_checks):
            raise TemperatureRuntimeServicesError("DURABLE_NO_COMPLETION_NOT_PROVEN")
        if (
            hashlib.sha256(_read_private_record_at(task_fd, rollout_name)).hexdigest()
            != expected_rollout_result_sha256
        ):
            raise TemperatureRuntimeServicesError("DURABLE_NO_COMPLETION_NOT_PROVEN")
        return basis
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _ambiguous_no_completion_evidence(
    *,
    rollout: PersistedRolloutResultAuditV1,
    durable_rollout_no_completion: bool,
) -> DurableNoCompletionEvidenceV1:
    return DurableNoCompletionEvidenceV1(
        state="AMBIGUOUS",
        service_run_id=rollout.service_run_id,
        task_id_sha256=rollout.task_id_sha256,
        session_id_sha256=rollout.session_id_sha256,
        rollout_result_sha256=rollout.result_sha256,
        rollout_terminal_status=rollout.terminal_status,
        durable_rollout_no_completion=durable_rollout_no_completion,
        durable_gateway_absent=False,
        gateway_absence_basis=None,
        finding_code="DURABLE_NO_COMPLETION_NOT_PROVEN",
    )


def _decode_completion_record(encoded: bytes) -> dict[str, Any]:
    record = _decode_json_object(encoded, "PERSISTED_COMPLETION_NOT_PROVEN")
    expected = {
        "completion_id",
        "timestamp",
        "session_id",
        "task_id",
        "api_type",
        "model_requested",
        "model_used",
        "original_request",
        "transformed_request",
        "response",
        "metadata",
        "__written_at",
    }
    if set(record) != expected:
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
    if (
        type(record["completion_id"]) is not str
        or _SAFE_ID_RE.fullmatch(record["completion_id"]) is None
        or type(record["timestamp"]) is not str
        or type(record["__written_at"]) is not str
        or not isinstance(record["original_request"], dict)
        or not isinstance(record["transformed_request"], dict)
        or not isinstance(record["response"], dict)
        or not isinstance(record["metadata"], dict)
    ):
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_NOT_PROVEN")
    return record


def _require_untruncated_record(record: Mapping[str, Any]) -> None:
    if _contains_truncation_marker(record):
        raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_TRUNCATED")
    for value in record.values():
        if _looks_like_truncated_string(value):
            raise TemperatureRuntimeServicesError("PERSISTED_COMPLETION_TRUNCATED")


def _contains_truncation_marker(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("__truncated") is True or "_truncated_keys_omitted" in value:
            return True
        return any(_contains_truncation_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_truncation_marker(item) for item in value)
    return False


def _looks_like_truncated_string(value: Any) -> bool:
    if isinstance(value, str):
        size = len(value.encode("utf-8", errors="ignore"))
        return value.endswith("…") and size >= COMPLETION_MAX_FIELD_BYTES - 8
    if isinstance(value, dict):
        return any(_looks_like_truncated_string(item) for item in value.values())
    if isinstance(value, list):
        return any(_looks_like_truncated_string(item) for item in value)
    return False


def _decode_json_object(encoded: bytes, finding_code: str) -> dict[str, Any]:
    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise TemperatureRuntimeServicesError(finding_code) from exc
    if not isinstance(payload, dict):
        raise TemperatureRuntimeServicesError(finding_code)
    return payload


def _read_private_json(path: Path, *, maximum: int) -> object:
    encoded = _read_private_bytes(path, maximum=maximum)
    return _decode_json_object(encoded, "TEMPERATURE_RUNTIME_POINTER_INVALID")


def _read_private_bytes(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_STATE_MISSING") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
        or metadata.st_uid != os.getuid()
    ):
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_STATE_UNSAFE")
    return path.read_bytes()


def _exclusive_private_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed.stdout.strip()


def _repository(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise TypeError("repository_root must be an absolute Path")
    repository = value.resolve(strict=True)
    if not (repository / ".git").is_dir():
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_REPOSITORY_INVALID")
    return repository


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = [
    "COMPLETION_AUDIT_SCHEMA",
    "COMPLETION_MAX_FIELD_BYTES",
    "COMPLETION_QUEUE_SIZE",
    "NO_COMPLETION_EVIDENCE_SCHEMA",
    "ROLLOUT_RESULT_AUDIT_SCHEMA",
    "SERVICE_ROOT_RELATIVE",
    "CompletionRootIdentityV1",
    "DurableNoCompletionEvidenceV1",
    "PersistedCompletionAuditV1",
    "PersistedRolloutResultAuditV1",
    "TemperatureGatewayContainerIdentityV1",
    "TemperatureRuntimeServicesError",
    "TemperatureRuntimeServicesIdentityV1",
    "audit_durable_no_completion_v1",
    "audit_empty_temperature_runtime_services_v1",
    "audit_persisted_completion_v1",
    "audit_persisted_rollout_result_v1",
    "load_temperature_runtime_evidence_v1",
    "load_temperature_runtime_services_v1",
    "start_temperature_runtime_services_v1",
    "stop_temperature_runtime_services_v1",
    "temperature_runtime_services_status_v1",
]
