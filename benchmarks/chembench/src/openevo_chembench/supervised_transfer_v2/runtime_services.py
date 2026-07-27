"""Pinned local OpenEvo Rollout/Gateway lifecycle for supervised transfer v2.

The benchmark does not execute Codex itself.  It launches the official OpenEvo
Rollout and Gateway modules from the non-editable v2 runtime environment, binds
their process identities, and requires both services to remain healthy before
and after every candidate session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
)
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
    load_managed_candidate_codex_v2,
)

SERVICE_RECEIPT_SCHEMA = "OpenEvoChemBenchRuntimeServicesReceiptV2"
SERVICE_STOP_RECEIPT_SCHEMA = "OpenEvoChemBenchRuntimeServicesStopReceiptV2"
SERVICE_POINTER_SCHEMA = "OpenEvoChemBenchRuntimeServicesPointerV2"
SERVICE_ROOT_RELATIVE = "state/chembench_supervised_transfer_v2/runtime_services"
RUNTIME_PYTHON_RELATIVE = "state/chembench_supervised_transfer_v2/runtime_venv/bin/python"
FRAMEWORK_LOCK_RELATIVE = "state/chembench_supervised_transfer_v2/framework/framework-lock.json"
TOPOLOGY_RELATIVE = (
    "benchmarks/chembench/configs/supervised_transfer_v2/openevo_runtime_services_v2.yaml"
)
ROLLOUT_URL = "http://127.0.0.1:8080"
GATEWAY_URL = "http://127.0.0.1:8100"
GATEWAY_BOOTSTRAP_RELATIVE = (
    "benchmarks/chembench/scripts/supervised_transfer_v2/gateway_service_bootstrap_v2.py"
)
GATEWAY_RUNTIME_MOUNT = "/openevo-v2-runtime"
GATEWAY_AUTH_SOURCE_MOUNT = "/openevo-auth-source"
GATEWAY_DOCKER_SOURCE_MOUNT = "/openevo-host-docker"
GATEWAY_EFFECTIVE_TOPOLOGY = f"{GATEWAY_RUNTIME_MOUNT}/effective_topology.yaml"
GATEWAY_BOOTSTRAP_RECEIPT = f"{GATEWAY_RUNTIME_MOUNT}/bootstrap_receipt.json"

_RUN_ID_RE = re.compile(r"stv2-services-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,16}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EXPECTED_MODULES = {
    "rollout": "openevo.rollout.server",
    "gateway": "openevo.gateway.server",
}
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_HEALTH_TIMEOUT_SECONDS = 2.0
_START_TIMEOUT_SECONDS = 30.0
_STOP_GRACE_SECONDS = 15.0
_GATEWAY_BOOTSTRAP_RECEIPT_SCHEMA = "OpenEvoChemBenchGatewayBootstrapReceiptV2"
_GATEWAY_CONTAINER_LABEL_PROTOCOL = "openevo.chembench.protocol"
_GATEWAY_CONTAINER_LABEL_RUN = "openevo.chembench.service_run_id"
_GATEWAY_CONTAINER_LABEL_SOURCE = "openevo.chembench.source_commit"


class RuntimeServicesV2Error(RuntimeError):
    """Closed infrastructure error for the official local Core services."""

    def __init__(self, finding_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", finding_code) is None:
            raise ValueError("runtime services finding code is invalid")
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class RuntimeProcessIdentityV2:
    service: Literal["rollout", "gateway"]
    pid: int
    process_group_id: int
    session_id: int
    start_time_ticks: int
    executable_sha256: str
    cmdline_sha256: str

    @classmethod
    def from_payload(cls, payload: object) -> RuntimeProcessIdentityV2:
        if type(payload) is not dict or set(payload) != {
            "service",
            "pid",
            "process_group_id",
            "session_id",
            "start_time_ticks",
            "executable_sha256",
            "cmdline_sha256",
        }:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
        service = payload["service"]
        if service not in _EXPECTED_MODULES:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
        for key in ("pid", "process_group_id", "session_id", "start_time_ticks"):
            if type(payload[key]) is not int or payload[key] < 1:
                raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
        for key in ("executable_sha256", "cmdline_sha256"):
            if type(payload[key]) is not str or _SHA256_RE.fullmatch(payload[key]) is None:
                raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
        return cls(
            service=service,
            pid=payload["pid"],
            process_group_id=payload["process_group_id"],
            session_id=payload["session_id"],
            start_time_ticks=payload["start_time_ticks"],
            executable_sha256=payload["executable_sha256"],
            cmdline_sha256=payload["cmdline_sha256"],
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "service": self.service,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "session_id": self.session_id,
            "start_time_ticks": self.start_time_ticks,
            "executable_sha256": self.executable_sha256,
            "cmdline_sha256": self.cmdline_sha256,
        }


@dataclass(frozen=True, slots=True)
class GatewayContainerIdentityV2:
    container_name: str
    container_id: str
    image_id: str
    effective_topology_sha256: str
    docker_host_path_identity_sha256: str
    docker_engine_identity_sha256: str
    docker_launcher_sha256: str

    @classmethod
    def from_payload(cls, payload: object) -> GatewayContainerIdentityV2:
        expected = {
            "container_name",
            "container_id",
            "image_id",
            "effective_topology_sha256",
            "docker_host_path_identity_sha256",
            "docker_engine_identity_sha256",
            "docker_launcher_sha256",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
        if (
            type(payload["container_name"]) is not str
            or not payload["container_name"].startswith("openevo-stv2-gateway-")
            or type(payload["image_id"]) is not str
            or not payload["image_id"].startswith("sha256:")
        ):
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
        for key in (
            "container_id",
            "effective_topology_sha256",
            "docker_host_path_identity_sha256",
            "docker_engine_identity_sha256",
            "docker_launcher_sha256",
        ):
            if type(payload[key]) is not str or _SHA256_RE.fullmatch(payload[key]) is None:
                raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
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
        }


@dataclass(frozen=True, slots=True)
class OpenEvoRuntimeServicesIdentityV2:
    repository_root: Path
    service_run_id: str
    source_commit: str
    framework_lock_sha256: str
    framework_wheel_sha256: str
    topology_sha256: str
    gateway_bootstrap_sha256: str
    runtime_python_sha256: str
    openevo_version: str
    rollout: RuntimeProcessIdentityV2
    gateway: RuntimeProcessIdentityV2
    gateway_container: GatewayContainerIdentityV2
    receipt_path: Path
    receipt_sha256: str

    @property
    def rollout_url(self) -> str:
        return ROLLOUT_URL

    @property
    def public_identity(self) -> dict[str, object]:
        return {
            "service_run_id": self.service_run_id,
            "source_commit": self.source_commit,
            "framework_lock_sha256": self.framework_lock_sha256,
            "framework_wheel_sha256": self.framework_wheel_sha256,
            "topology_sha256": self.topology_sha256,
            "gateway_bootstrap_sha256": self.gateway_bootstrap_sha256,
            "runtime_python_sha256": self.runtime_python_sha256,
            "openevo_version": self.openevo_version,
            "rollout_process": self.rollout.payload,
            "gateway_process": self.gateway.payload,
            "gateway_container": self.gateway_container.payload,
            "receipt_sha256": self.receipt_sha256,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_pretty_json_bytes(self.public_identity))

    def require_current(self) -> dict[str, object]:
        _require_process_identity(self.rollout)
        _require_process_identity(
            self.gateway,
            gateway_container_name=self.gateway_container.container_name,
        )
        _require_gateway_container_identity(
            self.gateway_container,
            repository_root=self.repository_root,
            source_commit=self.source_commit,
            service_run_id=self.service_run_id,
        )
        health = _require_service_health()
        return {
            "runtime_services_identity_sha256": self.digest,
            "rollout_health_sha256": sha256_bytes(
                canonical_pretty_json_bytes(health["rollout"])
            ),
            "gateway_health_sha256": sha256_bytes(
                canonical_pretty_json_bytes(health["gateway"])
            ),
        }


def start_runtime_services_v2(
    *,
    repository_root: Path,
    service_run_id: str,
) -> OpenEvoRuntimeServicesIdentityV2:
    repository = _repository(repository_root)
    if _RUN_ID_RE.fullmatch(service_run_id) is None:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RUN_ID_INVALID")
    paths = _runtime_paths(repository)
    service_root = paths["service_root"]
    service_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    service_root.chmod(0o700)
    pointer = service_root / "current.json"
    if pointer.exists():
        try:
            existing = load_runtime_services_v2(repository_root=repository)
            existing.require_current()
        except RuntimeServicesV2Error:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_EXISTING_STATE_UNRESOLVED") from None
        raise RuntimeServicesV2Error("RUNTIME_SERVICES_ALREADY_RUNNING")

    run_root = service_root / "runs" / service_run_id
    if run_root.exists():
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RUN_ID_ALREADY_EXISTS")
    run_root.mkdir(mode=0o700, parents=True)
    run_root.chmod(0o700)
    _validate_runtime_inputs(paths)
    metadata = _runtime_metadata(paths)
    source_commit = _git(repository, "rev-parse", "HEAD")
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
                    os.fspath(paths["topology"]),
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
            gateway_runtime_root=gateway_runtime_root,
            base_topology=paths["topology"],
            container_name=gateway_container_name,
            expected_image_id=candidate.image_id,
            docker_launcher=docker_launcher,
            source_commit=source_commit,
            service_run_id=service_run_id,
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
        receipt_path = run_root / "runtime_services_receipt_v2.json"
        _exclusive_private_write(receipt_path, canonical_pretty_json_bytes(receipt))
        _exclusive_private_write(
            pointer,
            canonical_pretty_json_bytes(
                {
                    "schema_version": SERVICE_POINTER_SCHEMA,
                    "service_run_id": service_run_id,
                    "receipt_sha256": sha256_bytes(receipt_path.read_bytes()),
                }
            ),
        )
        return load_runtime_services_v2(repository_root=repository)
    except BaseException:
        _terminate_processes(
            processes,
            gateway_container_name=gateway_container_name,
            source_commit=source_commit,
            service_run_id=service_run_id,
        )
        raise
    finally:
        for stream in log_streams:
            stream.close()


def load_runtime_services_v2(
    *,
    repository_root: Path,
) -> OpenEvoRuntimeServicesIdentityV2:
    repository = _repository(repository_root)
    paths = _runtime_paths(repository)
    pointer_path = paths["service_root"] / "current.json"
    pointer = _read_private_json(pointer_path, maximum=4096)
    if type(pointer) is not dict or set(pointer) != {
        "schema_version",
        "service_run_id",
        "receipt_sha256",
    }:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_POINTER_INVALID")
    service_run_id = pointer["service_run_id"]
    receipt_digest = pointer["receipt_sha256"]
    if (
        pointer["schema_version"] != SERVICE_POINTER_SCHEMA
        or type(service_run_id) is not str
        or _RUN_ID_RE.fullmatch(service_run_id) is None
        or type(receipt_digest) is not str
        or _SHA256_RE.fullmatch(receipt_digest) is None
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_POINTER_INVALID")
    receipt_path = paths["service_root"] / "runs" / service_run_id / "runtime_services_receipt_v2.json"
    encoded = _read_private_bytes(receipt_path, maximum=64 * 1024)
    if sha256_bytes(encoded) != receipt_digest:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_DIGEST_MISMATCH")
    try:
        receipt = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID") from exc
    expected_keys = {
        "schema_version",
        "service_run_id",
        "source_commit",
        "framework_lock_sha256",
        "framework_wheel_sha256",
        "topology_sha256",
        "gateway_bootstrap_sha256",
        "runtime_python_sha256",
        "openevo_version",
        "rollout_url",
        "gateway_url",
        "rollout_process",
        "gateway_process",
        "gateway_container",
        "rollout_health_sha256",
        "gateway_health_sha256",
    }
    if type(receipt) is not dict or set(receipt) != expected_keys:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
    if (
        receipt["schema_version"] != SERVICE_RECEIPT_SCHEMA
        or receipt["service_run_id"] != service_run_id
        or receipt["rollout_url"] != ROLLOUT_URL
        or receipt["gateway_url"] != GATEWAY_URL
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
    _validate_runtime_inputs(paths)
    metadata = _runtime_metadata(paths)
    if any(receipt.get(key) != value for key, value in metadata.items()):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_SOURCE_DRIFT")
    if receipt["source_commit"] != _git(repository, "rev-parse", "HEAD"):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_SOURCE_DRIFT")
    for key in ("rollout_health_sha256", "gateway_health_sha256"):
        if type(receipt[key]) is not str or _SHA256_RE.fullmatch(receipt[key]) is None:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_RECEIPT_INVALID")
    identity = OpenEvoRuntimeServicesIdentityV2(
        repository_root=repository,
        service_run_id=service_run_id,
        source_commit=receipt["source_commit"],
        framework_lock_sha256=receipt["framework_lock_sha256"],
        framework_wheel_sha256=receipt["framework_wheel_sha256"],
        topology_sha256=receipt["topology_sha256"],
        gateway_bootstrap_sha256=receipt["gateway_bootstrap_sha256"],
        runtime_python_sha256=receipt["runtime_python_sha256"],
        openevo_version=receipt["openevo_version"],
        rollout=RuntimeProcessIdentityV2.from_payload(receipt["rollout_process"]),
        gateway=RuntimeProcessIdentityV2.from_payload(receipt["gateway_process"]),
        gateway_container=GatewayContainerIdentityV2.from_payload(
            receipt["gateway_container"]
        ),
        receipt_path=receipt_path,
        receipt_sha256=receipt_digest,
    )
    identity.require_current()
    return identity


def stop_runtime_services_v2(*, repository_root: Path) -> dict[str, object]:
    repository = _repository(repository_root)
    identity = load_runtime_services_v2(repository_root=repository)
    processes = {identity.rollout.service: identity.rollout, identity.gateway.service: identity.gateway}
    _require_process_identity(identity.rollout)
    _require_process_identity(
        identity.gateway,
        gateway_container_name=identity.gateway_container.container_name,
    )
    for service in ("gateway", "rollout"):
        process = processes[service]
        try:
            os.killpg(process.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    while time.monotonic() < deadline and any(_process_exists(p.pid) for p in processes.values()):
        time.sleep(0.1)
    for process in processes.values():
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
    while time.monotonic() < deadline and any(_process_exists(p.pid) for p in processes.values()):
        time.sleep(0.1)
    if any(_process_exists(p.pid) for p in processes.values()):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_CLEANUP_INCOMPLETE")
    _remove_gateway_container_if_owned(
        container_name=identity.gateway_container.container_name,
        expected_container_id=identity.gateway_container.container_id,
        source_commit=identity.source_commit,
        service_run_id=identity.service_run_id,
    )
    stop_receipt = {
        "schema_version": SERVICE_STOP_RECEIPT_SCHEMA,
        "service_run_id": identity.service_run_id,
        "runtime_services_identity_sha256": identity.digest,
        "cleanup_complete": True,
    }
    _exclusive_private_write(
        identity.receipt_path.parent / "runtime_services_stop_receipt_v2.json",
        canonical_pretty_json_bytes(stop_receipt),
    )
    (identity.repository_root / SERVICE_ROOT_RELATIVE / "current.json").unlink()
    return stop_receipt


def runtime_services_status_v2(*, repository_root: Path) -> dict[str, object]:
    identity = load_runtime_services_v2(repository_root=repository_root)
    evidence = identity.require_current()
    return {
        "status": "PASS",
        "service_run_id": identity.service_run_id,
        "source_commit": identity.source_commit,
        "runtime_services_identity_sha256": identity.digest,
        **evidence,
    }


def _runtime_paths(repository: Path) -> dict[str, Path]:
    framework_root = repository / "state/chembench_supervised_transfer_v2/framework"
    wheels = tuple(framework_root.glob("openevo-*.whl")) if framework_root.is_dir() else ()
    if len(wheels) != 1:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_FRAMEWORK_WHEEL_INVALID")
    return {
        "service_root": repository / SERVICE_ROOT_RELATIVE,
        "runtime_python": repository / RUNTIME_PYTHON_RELATIVE,
        "framework_lock": repository / FRAMEWORK_LOCK_RELATIVE,
        "framework_wheel": wheels[0],
        "topology": repository / TOPOLOGY_RELATIVE,
        "gateway_bootstrap": repository / GATEWAY_BOOTSTRAP_RELATIVE,
    }


def _validate_runtime_inputs(paths: dict[str, Path]) -> None:
    runtime_python = paths["runtime_python"]
    try:
        resolved_python = runtime_python.resolve(strict=True)
        python_metadata = resolved_python.lstat()
    except OSError as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_INPUT_MISSING") from exc
    if not stat.S_ISREG(python_metadata.st_mode) or not os.access(runtime_python, os.X_OK):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RUNTIME_INVALID")
    for key in ("framework_lock", "framework_wheel", "topology", "gateway_bootstrap"):
        path = paths[key]
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_INPUT_MISSING") from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_INPUT_UNSAFE")


def _runtime_metadata(paths: dict[str, Path]) -> dict[str, str]:
    completed = subprocess.run(
        [
            os.fspath(paths["runtime_python"]),
            "-I",
            "-c",
            "import importlib.metadata as m; print(m.version('openevo'))",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env={"PATH": _SAFE_PATH, "HOME": os.environ.get("HOME", "/nonexistent")},
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_RUNTIME_INVALID")
    return {
        "framework_lock_sha256": _sha256_file(paths["framework_lock"]),
        "framework_wheel_sha256": _sha256_file(paths["framework_wheel"]),
        "topology_sha256": _sha256_file(paths["topology"]),
        "gateway_bootstrap_sha256": _sha256_file(paths["gateway_bootstrap"]),
        "runtime_python_sha256": _sha256_file(paths["runtime_python"].resolve(strict=True)),
        "openevo_version": version,
    }


def _resolve_docker_launcher() -> Path:
    selected = shutil.which("docker", path=os.defpath)
    if selected is None:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_DOCKER_UNAVAILABLE")
    try:
        resolved = Path(selected).resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_DOCKER_UNAVAILABLE") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or not stat.S_IMODE(metadata.st_mode) & 0o111
        or metadata.st_size <= 0
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_DOCKER_UNAVAILABLE")
    return resolved


def _gateway_container_name(service_run_id: str) -> str:
    suffix = hashlib.sha256(service_run_id.encode("ascii")).hexdigest()[:24]
    return f"openevo-stv2-gateway-{suffix}"


def _gateway_container_command(
    *,
    repository: Path,
    paths: dict[str, Path],
    source_commit: str,
    service_run_id: str,
    container_name: str,
    gateway_runtime_root: Path,
    image_id: str,
    auth_source: Path,
    docker_launcher: Path,
) -> list[str]:
    socket_path = Path("/var/run/docker.sock")
    try:
        socket_metadata = socket_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_DOCKER_UNAVAILABLE") from exc
    if not stat.S_ISSOCK(socket_metadata.st_mode):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_DOCKER_UNAVAILABLE")
    namespace = f"stv2-{hashlib.sha256(service_run_id.encode('ascii')).hexdigest()[:24]}"
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
                f"{os.fspath(paths['runtime_python'])} -I "
                f"{os.fspath(paths['gateway_bootstrap'])} "
                f"--base-config {os.fspath(paths['topology'])} "
                f"--effective-config {GATEWAY_EFFECTIVE_TOPOLOGY} "
                f"--receipt {GATEWAY_BOOTSTRAP_RECEIPT} "
                f"--runtime-python {os.fspath(paths['runtime_python'])} "
                f"--namespace {namespace}"
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
        f"{_GATEWAY_CONTAINER_LABEL_PROTOCOL}=chembench_supervised_transfer_v2",
        "--label",
        f"{_GATEWAY_CONTAINER_LABEL_RUN}={service_run_id}",
        "--label",
        f"{_GATEWAY_CONTAINER_LABEL_SOURCE}={source_commit}",
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
    gateway_runtime_root: Path,
    base_topology: Path,
    container_name: str,
    expected_image_id: str,
    docker_launcher: Path,
    source_commit: str,
    service_run_id: str,
) -> GatewayContainerIdentityV2:
    receipt_path = gateway_runtime_root / "bootstrap_receipt.json"
    receipt = _read_private_json(receipt_path, maximum=16 * 1024)
    expected_keys = {
        "schema_version",
        "base_topology_sha256",
        "effective_topology_sha256",
        "docker_engine_identity_sha256",
        "docker_host_path_identity_sha256",
        "container_id",
    }
    if type(receipt) is not dict or set(receipt) != expected_keys:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_BOOTSTRAP_INVALID")
    for key in expected_keys - {"schema_version"}:
        if type(receipt[key]) is not str or _SHA256_RE.fullmatch(receipt[key]) is None:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_BOOTSTRAP_INVALID")
    effective = gateway_runtime_root / "effective_topology.yaml"
    if (
        receipt["schema_version"] != _GATEWAY_BOOTSTRAP_RECEIPT_SCHEMA
        or receipt["base_topology_sha256"] != _sha256_file(base_topology)
        or receipt["effective_topology_sha256"] != _sha256_file(effective)
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_BOOTSTRAP_INVALID")
    inspected = _inspect_gateway_container(
        container_name=container_name,
        source_commit=source_commit,
        service_run_id=service_run_id,
    )
    if inspected["container_id"] != receipt["container_id"]:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_CONTAINER_INVALID")
    if inspected["image_id"] != expected_image_id:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_CONTAINER_INVALID")
    return GatewayContainerIdentityV2(
        container_name=container_name,
        container_id=str(receipt["container_id"]),
        image_id=expected_image_id,
        effective_topology_sha256=str(receipt["effective_topology_sha256"]),
        docker_host_path_identity_sha256=str(receipt["docker_host_path_identity_sha256"]),
        docker_engine_identity_sha256=str(receipt["docker_engine_identity_sha256"]),
        docker_launcher_sha256=_sha256_file(docker_launcher),
    )


def _inspect_gateway_container(
    *,
    container_name: str,
    source_commit: str,
    service_run_id: str,
) -> dict[str, str]:
    docker = _resolve_docker_launcher()
    completed = subprocess.run(
        (os.fspath(docker), "container", "inspect", container_name),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": _SAFE_PATH, "HOME": "/proc/self", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        cwd="/",
    )
    try:
        rows = json.loads(completed.stdout)
        row = rows[0]
        labels = row["Config"]["Labels"]
        state = row["State"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_CONTAINER_INVALID") from exc
    expected_labels = {
        _GATEWAY_CONTAINER_LABEL_PROTOCOL: "chembench_supervised_transfer_v2",
        _GATEWAY_CONTAINER_LABEL_RUN: service_run_id,
        _GATEWAY_CONTAINER_LABEL_SOURCE: source_commit,
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
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_CONTAINER_INVALID")
    return {"container_id": row["Id"], "image_id": row["Image"]}


def _require_gateway_container_identity(
    expected: GatewayContainerIdentityV2,
    *,
    repository_root: Path,
    source_commit: str,
    service_run_id: str,
) -> None:
    current = _inspect_gateway_container(
        container_name=expected.container_name,
        source_commit=source_commit,
        service_run_id=service_run_id,
    )
    effective = (
        repository_root
        / SERVICE_ROOT_RELATIVE
        / "runs"
        / service_run_id
        / "gateway_runtime/effective_topology.yaml"
    )
    if (
        current["container_id"] != expected.container_id
        or current["image_id"] != expected.image_id
        or _sha256_file(_resolve_docker_launcher()) != expected.docker_launcher_sha256
        or _sha256_file(effective) != expected.effective_topology_sha256
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_CONTAINER_DRIFT")


def _service_environment(repository: Path) -> dict[str, str]:
    home = os.environ.get("HOME")
    if not home or not Path(home).is_absolute():
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_HOME_INVALID")
    return {
        "HOME": home,
        "PATH": _SAFE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "OPENEVO_CHEMBENCH_V2_SOURCE_COMMIT": _git(repository, "rev-parse", "HEAD"),
    }


def _wait_for_service_start(*, process: subprocess.Popen[bytes], url: str) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_START_FAILED")
        try:
            response = httpx.get(url, timeout=_HEALTH_TIMEOUT_SECONDS, trust_env=False)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeServicesV2Error("RUNTIME_SERVICE_START_TIMEOUT")


def _wait_for_schedulable_rollout(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeServicesV2Error("RUNTIME_SERVICE_START_FAILED")
        try:
            payload = _get_json(f"{ROLLOUT_URL}/health")
            registration = payload.get("gateway_registration")
            if (
                payload.get("status") == "ok"
                and payload.get("internal_identity") is None
                and isinstance(registration, dict)
                and registration.get("node_id") == "core-gateway"
                and registration.get("registered") is True
                and registration.get("schedulable") is True
                and registration.get("gateway_url") == GATEWAY_URL
            ):
                return
        except RuntimeServicesV2Error:
            pass
        time.sleep(0.2)
    raise RuntimeServicesV2Error("RUNTIME_SERVICE_GATEWAY_NOT_SCHEDULABLE")


def _require_service_health() -> dict[str, dict[str, Any]]:
    rollout = _get_json(f"{ROLLOUT_URL}/health")
    registration = rollout.get("gateway_registration")
    if (
        rollout.get("status") != "ok"
        or rollout.get("internal_identity") is not None
        or not isinstance(registration, dict)
        or registration.get("node_id") != "core-gateway"
        or registration.get("registered") is not True
        or registration.get("schedulable") is not True
        or registration.get("gateway_url") != GATEWAY_URL
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_HEALTH_INVALID")
    gateway = _get_json(f"{GATEWAY_URL}/health")
    if (
        gateway.get("status") != "ok"
        or gateway.get("node_id") != "core-gateway"
        or gateway.get("gateway_url") != GATEWAY_URL
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_HEALTH_INVALID")
    return {"rollout": rollout, "gateway": gateway}


def _get_json(url: str) -> dict[str, Any]:
    try:
        response = httpx.get(url, timeout=_HEALTH_TIMEOUT_SECONDS, trust_env=False)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_UNAVAILABLE") from exc
    if not isinstance(payload, dict):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_HEALTH_INVALID")
    return payload


def _capture_process_identity(
    service: Literal["rollout", "gateway"],
    pid: int,
    *,
    gateway_container_name: str | None = None,
) -> RuntimeProcessIdentityV2:
    proc = Path("/proc") / str(pid)
    try:
        start_time_ticks = _proc_start_time_ticks(proc / "stat")
        cmdline = (proc / "cmdline").read_bytes()
        executable = (proc / "exe").resolve(strict=True)
        process_group_id = os.getpgid(pid)
        session_id = os.getsid(pid)
    except (OSError, ValueError) as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_PROCESS_IDENTITY_INVALID") from exc
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
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_PROCESS_IDENTITY_INVALID")
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
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_PROCESS_IDENTITY_DRIFT")


def _proc_start_time_ticks(path: Path) -> int:
    value = path.read_text(encoding="ascii")
    end = value.rfind(")")
    if end < 1:
        raise ValueError("invalid proc stat")
    fields = value[end + 2 :].split()
    if len(fields) < 20:
        raise ValueError("invalid proc stat")
    return int(fields[19], 10)


def _terminate_processes(
    processes: dict[str, subprocess.Popen[bytes]],
    *,
    gateway_container_name: str,
    source_commit: str,
    service_run_id: str,
) -> None:
    for process in reversed(tuple(processes.values())):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(p.poll() is None for p in processes.values()):
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
    )


def _remove_gateway_container_if_owned(
    *,
    container_name: str,
    expected_container_id: str | None,
    source_commit: str,
    service_run_id: str,
) -> None:
    docker = _resolve_docker_launcher()
    inspected = subprocess.run(
        (os.fspath(docker), "container", "inspect", container_name),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": _SAFE_PATH, "HOME": "/proc/self", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
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
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_CLEANUP_OWNERSHIP_INVALID") from exc
    if (
        not isinstance(labels, dict)
        or labels.get(_GATEWAY_CONTAINER_LABEL_PROTOCOL)
        != "chembench_supervised_transfer_v2"
        or labels.get(_GATEWAY_CONTAINER_LABEL_RUN) != service_run_id
        or labels.get(_GATEWAY_CONTAINER_LABEL_SOURCE) != source_commit
        or type(container_id) is not str
        or _SHA256_RE.fullmatch(container_id) is None
        or (expected_container_id is not None and container_id != expected_container_id)
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_CLEANUP_OWNERSHIP_INVALID")
    removed = subprocess.run(
        (os.fspath(docker), "container", "rm", "--force", container_id),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        env={"PATH": _SAFE_PATH, "HOME": "/proc/self", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        cwd="/",
    )
    if removed.returncode != 0:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_CLEANUP_INCOMPLETE")


def _read_private_json(path: Path, *, maximum: int) -> object:
    encoded = _read_private_bytes(path, maximum=maximum)
    try:
        return json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_POINTER_INVALID") from exc


def _read_private_bytes(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_STATE_MISSING") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_STATE_UNSAFE")
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
        raise RuntimeServicesV2Error("RUNTIME_SERVICE_REPOSITORY_INVALID")
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
    "GatewayContainerIdentityV2",
    "OpenEvoRuntimeServicesIdentityV2",
    "RuntimeServicesV2Error",
    "load_runtime_services_v2",
    "runtime_services_status_v2",
    "start_runtime_services_v2",
    "stop_runtime_services_v2",
]
