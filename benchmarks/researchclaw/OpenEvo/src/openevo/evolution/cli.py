from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from openevo.evolution.framework import load_verified_framework_registry
from openevo.evolution.framework.execution import MethodExecutionServices
from openevo.evolution.managed_reflector import (
    ManagedCodexReflectorService,
    default_managed_reflector_runtime,
)
from openevo.evolution.methods import METHOD_REGISTRY
from openevo.evolution.server import create_app
from openevo.evolution.worker import EvolutionWorkerClient, run_once
from openevo.gateway.session_files import PreparedCodexCredentialSnapshot
from openevo.internal_auth import (
    inherited_listen_fd,
    read_internal_service_identity,
    verified_private_file_sha256,
)
from openevo.runtime.managed_reflector_mount import (
    MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV,
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorMountAuthority,
    ManagedReflectorMountRegistrationExpectation,
    ManagedReflectorRuntimeReadiness,
)

_MAX_FRAMEWORK_LOCK_BYTES = 4 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m openevo.evolution.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the Evolution Backend.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8200)
    serve.add_argument("--db", default=".openevo/evolution/evolution.db")
    serve.add_argument("--artifact-root", default=".openevo/evolution")
    serve.add_argument("--framework-lock", type=Path, required=True)
    serve.add_argument("--managed-reflector-mount-expectation")

    worker = subparsers.add_parser("worker", help="Run an Evolution reference worker.")
    worker.add_argument("--base-url", default="http://127.0.0.1:8200")
    worker.add_argument("--worker-id", default="reference-worker")
    worker.add_argument("--capability", action="append", default=[])
    worker.add_argument("--artifact-root", default=".openevo/evolution")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--sleep-seconds", type=float, default=5.0)
    worker.add_argument("--lease-seconds", type=int, default=600)
    worker.add_argument("--framework-lock", type=Path)
    worker.add_argument("--managed-reflector-root", type=Path)
    worker.add_argument("--managed-reflector-mount-launch-id")
    worker.add_argument("--managed-reflector-docker-host-path-identity")
    worker.add_argument("--managed-reflector-daemon-release-identity")
    worker.add_argument("--managed-reflector-release-install-identity")
    worker.add_argument("--managed-reflector-runtime-profile")
    worker.add_argument("--managed-reflector-runtime-image")
    worker.add_argument("--managed-reflector-runtime-identity")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "serve":
        import uvicorn

        registry = load_verified_framework_registry(args.framework_lock)
        internal_identity = read_internal_service_identity(
            required=False,
            expected_service_id="evolution-backend",
        )
        if (
            internal_identity is not None
            and (
                internal_identity.registry_digest != registry.snapshot.registry_digest
                or internal_identity.framework_lock_digest
                != verified_private_file_sha256(
                    args.framework_lock,
                    max_bytes=_MAX_FRAMEWORK_LOCK_BYTES,
                )
            )
        ):
            raise RuntimeError(
                "loaded framework lock/registry does not match the service generation"
            )
        app_kwargs = {
            "db_path": Path(args.db),
            "artifact_root": Path(args.artifact_root),
            "executable_registry": registry,
        }
        if internal_identity is not None:
            app_kwargs["internal_identity"] = internal_identity
            if args.managed_reflector_mount_expectation is not None:
                app_kwargs["managed_reflector_mount_expectation"] = (
                    ManagedReflectorMountRegistrationExpectation.model_validate_json(
                        args.managed_reflector_mount_expectation
                    )
                )
        elif args.managed_reflector_mount_expectation is not None:
            raise RuntimeError(
                "managed reflector mount expectation requires release service identity"
            )
        app = create_app(**app_kwargs)
        listen_fd = inherited_listen_fd()
        if internal_identity is not None and listen_fd is None:
            raise RuntimeError("release-owned evolution backend requires an inherited listener")
        if listen_fd is None:
            uvicorn.run(app, host=args.host, port=args.port)
        else:
            uvicorn.run(app, fd=listen_fd)
        return 0

    capabilities = _parse_capabilities(args.capability)
    artifact_root = Path(args.artifact_root)
    registry = (
        load_verified_framework_registry(args.framework_lock)
        if args.framework_lock is not None
        else None
    )
    internal_identity = read_internal_service_identity(
        required=False,
        expected_service_id="evolution-worker",
        actual_registry_digest=(
            registry.snapshot.registry_digest if registry is not None else None
        ),
    )
    if internal_identity is not None:
        if args.framework_lock is None:
            raise RuntimeError("release-owned worker requires a framework lock")
        if (
            internal_identity.framework_lock_digest
            != verified_private_file_sha256(
                args.framework_lock,
                max_bytes=_MAX_FRAMEWORK_LOCK_BYTES,
            )
        ):
            raise RuntimeError("worker framework lock does not match the service generation")
    credential_snapshot = PreparedCodexCredentialSnapshot.from_inherited_environment(
        required=internal_identity is not None,
    )
    reflector_service: ManagedCodexReflectorService | None = None
    try:
        if internal_identity is not None and args.managed_reflector_root is None:
            raise RuntimeError("release-owned worker requires a managed reflector root")
        mount_launch_id = args.managed_reflector_mount_launch_id
        docker_host_path_identity = args.managed_reflector_docker_host_path_identity
        daemon_release_identity = args.managed_reflector_daemon_release_identity
        release_install_identity = args.managed_reflector_release_install_identity
        runtime_profile = args.managed_reflector_runtime_profile
        runtime_image = args.managed_reflector_runtime_image
        runtime_identity = args.managed_reflector_runtime_identity
        mount_binding_values = (
            mount_launch_id,
            docker_host_path_identity,
            daemon_release_identity,
            release_install_identity,
            runtime_profile,
            runtime_image,
            runtime_identity,
        )
        if any(value is not None for value in mount_binding_values) and not all(
            value is not None for value in mount_binding_values
        ):
            raise RuntimeError(
                "managed reflector mount binding identities must form a closed set"
            )
        inherited_mount_authority = (
            MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV in os.environ
        )
        if (mount_launch_id is not None) != inherited_mount_authority:
            raise RuntimeError(
                "managed reflector mount authority does not match the worker launch"
            )
        if mount_launch_id is not None and internal_identity is None:
            raise RuntimeError(
                "managed reflector mount authority requires release service identity"
            )
        mount_authority = (
            ManagedReflectorMountAuthority.from_inherited_environment(
                internal_identity,
                expected_worker_launch_id=mount_launch_id,
                expected_docker_host_path_identity=docker_host_path_identity,
                expected_daemon_release_identity=daemon_release_identity,
                expected_release_install_digest=release_install_identity,
                expected_runtime_profile=runtime_profile,
                expected_runtime_image=runtime_image,
                expected_runtime_identity_digest=runtime_identity,
                required=True,
            )
            if internal_identity is not None
            and mount_launch_id is not None
            and docker_host_path_identity is not None
            and daemon_release_identity is not None
            and release_install_identity is not None
            and runtime_profile is not None
            and runtime_image is not None
            and runtime_identity is not None
            else None
        )
        reflector_service = (
            ManagedCodexReflectorService(
                root=args.managed_reflector_root,
                credential_snapshot=credential_snapshot,
                docker_host_path=(
                    None if mount_authority is None else mount_authority.docker_host_path
                ),
                mount_authority=mount_authority,
                require_docker_host_path=mount_authority is not None,
            )
            if credential_snapshot is not None and args.managed_reflector_root is not None
            else None
        )
        method_services = MethodExecutionServices(
            harness=_UnavailableCliHarnessService(),
            reflector=reflector_service,
        )
        mount_readiness: dict[str, object] | None = None
        runtime_readiness: dict[str, object] | None = None
        if mount_authority is not None:
            if reflector_service is None:
                raise RuntimeError("managed reflector mount readiness service is unavailable")
            mount_readiness = reflector_service.credential_mount_readiness(
                default_managed_reflector_runtime()
            )
            required_readiness = {
                "authority_issued",
                "docker_mount_created",
                "container_path_visible",
                "container_user_can_read",
                "auth_file_read_only",
                "generation_matches",
                "release_identity_matches",
                "adoption_receipt_valid",
                "container_authority_verified",
                "cleanup_verified",
            }
            if any(mount_readiness.get(key) is not True for key in required_readiness):
                raise RuntimeError("managed reflector credential mount readiness failed")
            if (
                mount_readiness.get("authority_id") != mount_authority.authority_id
                or mount_readiness.get("worker_launch_id")
                != mount_authority.grant.worker_launch_id
                or mount_readiness.get("codex_cli_started") is not False
                or mount_readiness.get("model_started") is not False
            ):
                raise RuntimeError("managed reflector credential mount receipt is invalid")
            validated_mount = ManagedReflectorCredentialMountReadiness.model_validate(
                mount_readiness
            )
            runtime_readiness = reflector_service.readiness(
                default_managed_reflector_runtime(),
                credential_mount_readiness=validated_mount,
            )
            validated_runtime = ManagedReflectorRuntimeReadiness.model_validate(
                runtime_readiness
            )
            ManagedReflectorMountRegistrationExpectation.from_authority(
                mount_authority
            ).verify_runtime_readiness(
                validated_runtime,
                credential_mount=validated_mount,
            )
        with EvolutionWorkerClient(
            args.base_url,
            headers=(
                internal_identity.request_headers()
                if internal_identity is not None
                else None
            ),
        ) as client:
            if internal_identity is not None:
                client.register_internal_worker(
                    worker_id=args.worker_id,
                    framework_lock_digest=internal_identity.framework_lock_digest,
                    generation_digest=internal_identity.generation_digest,
                    registry_digest=internal_identity.registry_digest,
                    managed_reflector_credential_mount=mount_readiness,
                    managed_reflector_runtime_readiness=runtime_readiness,
                )
            while True:
                claimed = run_once(
                    client,
                    worker_id=args.worker_id,
                    capabilities=capabilities,
                    artifact_root=artifact_root,
                    lease_seconds=args.lease_seconds,
                    executable_registry=registry,
                    method_services=method_services,
                )
                if args.once:
                    return 0
                if not claimed:
                    time.sleep(args.sleep_seconds)
    finally:
        if reflector_service is not None:
            reflector_service.close()
        if credential_snapshot is not None:
            credential_snapshot.close()


class _UnavailableCliHarnessService:
    def infer(self, request):
        del request
        raise ValueError("evolution worker harness inference is unavailable")


def _parse_capabilities(values: list[str]) -> list[str]:
    capabilities: list[str] = []
    for value in values:
        capabilities.extend(part.strip() for part in value.split(",") if part.strip())
    return capabilities or list(METHOD_REGISTRY)


if __name__ == "__main__":
    raise SystemExit(main())
