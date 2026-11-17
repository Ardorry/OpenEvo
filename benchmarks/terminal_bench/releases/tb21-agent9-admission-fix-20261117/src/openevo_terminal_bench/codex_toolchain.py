"""Build and validate a reproducible offline Codex toolchain bundle."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA = "openevo.tb21.codex-toolchain.v1"
TARGET = "x86_64-unknown-linux-musl"
ROOT_NAME = "openevo-codex-toolchain"
VENDOR_FILES = (
    f"vendor/{TARGET}/bin/codex",
    f"vendor/{TARGET}/bin/codex-code-mode-host",
    f"vendor/{TARGET}/codex-path/rg",
    f"vendor/{TARGET}/codex-resources/bwrap",
)
WRAPPER = f"""#!/usr/bin/env bash
set -euo pipefail
root="/opt/{ROOT_NAME}"
export PATH="$root/vendor/{TARGET}/codex-path:$PATH"
export CODEX_MANAGED_PACKAGE_ROOT="$root"
export CODEX_MANAGED_BY_NPM=1
exec "$root/vendor/{TARGET}/bin/codex" "$@"
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def build_codex_toolchain_bundle(
    *, codex_package_root: Path, output: Path, version: str
) -> dict[str, Any]:
    """Create a deterministic bundle from an npm or managed Codex package."""
    package_root = codex_package_root.resolve()
    package_manifest = package_root / "package.json"
    managed_manifest = package_root / "codex-package.json"
    if package_manifest.is_file():
        package_metadata = json.loads(package_manifest.read_text(encoding="utf-8"))
        platform_package = package_root / "node_modules/@openai/codex-linux-x64"
        source_paths = {
            relative: platform_package / relative for relative in VENDOR_FILES
        }
    elif managed_manifest.is_file():
        package_metadata = json.loads(managed_manifest.read_text(encoding="utf-8"))
        if (
            package_metadata.get("layoutVersion") != 1
            or package_metadata.get("target") != TARGET
        ):
            raise ValueError("unsupported managed Codex package layout")
        prefix = f"vendor/{TARGET}/"
        source_paths = {
            relative: package_root / relative.removeprefix(prefix)
            for relative in VENDOR_FILES
        }
    else:
        raise ValueError("Codex package manifest is missing")
    installed_version = package_metadata.get("version")
    if installed_version != version:
        raise ValueError(
            f"Codex package version {installed_version!r} differs from {version!r}"
        )
    missing = [rel for rel, source in source_paths.items() if not source.is_file()]
    if missing:
        raise ValueError(f"Codex package lacks required vendor files: {missing}")
    if not version or any(char.isspace() for char in version):
        raise ValueError("invalid Codex toolchain version")

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-toolchain-") as temp:
        root = Path(temp) / ROOT_NAME
        for relative in VENDOR_FILES:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_paths[relative], destination)
            destination.chmod(0o755)
        wrapper = root / "bin/codex"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(WRAPPER, encoding="utf-8")
        wrapper.chmod(0o755)

        payload_files = ("bin/codex", *VENDOR_FILES)
        hashes = {relative: _sha256(root / relative) for relative in payload_files}
        sums = root / "SHA256SUMS"
        sums.write_text(
            "".join(f"{hashes[relative]}  {relative}\n" for relative in payload_files),
            encoding="utf-8",
        )
        manifest = {
            "schema": SCHEMA,
            "version": version,
            "target": TARGET,
            "root_name": ROOT_NAME,
            "files": hashes,
            "sha256sums_sha256": _sha256(sums),
        }
        (root / "manifest.json").write_bytes(_canonical_json(manifest) + b"\n")

        temporary_output = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            with temporary_output.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
                    with tarfile.open(mode="w", fileobj=zipped) as archive:
                        archive.add(root, arcname=ROOT_NAME, filter=_normalized_tar_info)
            os.replace(temporary_output, output)
        finally:
            temporary_output.unlink(missing_ok=True)
    result = dict(manifest)
    result.update({"path": str(output), "bundle_sha256": _sha256(output)})
    return result


@lru_cache(maxsize=16)
def validate_codex_toolchain_bundle(
    path: Path,
    *,
    expected_version: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Fail closed on archive drift, unsafe members, or internal hash mismatch."""
    supplied_path = path.absolute()
    if supplied_path.is_symlink() or not supplied_path.is_file():
        raise ValueError(f"Codex toolchain bundle is not a regular file: {supplied_path}")
    bundle = supplied_path.resolve()
    bundle_sha256 = _sha256(bundle)
    if expected_sha256 is not None and bundle_sha256 != expected_sha256:
        raise ValueError("Codex toolchain bundle SHA256 mismatch")

    with tarfile.open(bundle, mode="r:gz") as archive:
        member_list = archive.getmembers()
        members = {member.name: member for member in member_list}
        if len(members) != len(member_list):
            raise ValueError("Codex toolchain archive contains duplicate members")
        for member in members.values():
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError(f"unsafe Codex toolchain member: {member.name}")
        expected_archive_files = {
            f"{ROOT_NAME}/manifest.json",
            f"{ROOT_NAME}/SHA256SUMS",
            *(f"{ROOT_NAME}/{relative}" for relative in ("bin/codex", *VENDOR_FILES)),
        }
        actual_archive_files = {
            member.name for member in members.values() if member.isfile()
        }
        if actual_archive_files != expected_archive_files:
            raise ValueError("Codex toolchain archive file set mismatch")
        manifest_name = f"{ROOT_NAME}/manifest.json"
        manifest_stream = archive.extractfile(manifest_name)
        if manifest_stream is None:
            raise ValueError("Codex toolchain manifest is missing")
        manifest = json.loads(manifest_stream.read())
        if (
            manifest.get("schema") != SCHEMA
            or manifest.get("version") != expected_version
            or manifest.get("target") != TARGET
            or manifest.get("root_name") != ROOT_NAME
        ):
            raise ValueError("Codex toolchain manifest identity mismatch")
        expected_files = {"bin/codex", *VENDOR_FILES}
        hashes = manifest.get("files")
        if not isinstance(hashes, dict) or set(hashes) != expected_files:
            raise ValueError("Codex toolchain payload set mismatch")
        for relative, expected_hash in hashes.items():
            stream = archive.extractfile(f"{ROOT_NAME}/{relative}")
            if stream is None:
                raise ValueError(f"Codex toolchain payload is missing: {relative}")
            if hashlib.sha256(stream.read()).hexdigest() != expected_hash:
                raise ValueError(f"Codex toolchain payload hash mismatch: {relative}")
        sums_stream = archive.extractfile(f"{ROOT_NAME}/SHA256SUMS")
        if (
            sums_stream is None
            or hashlib.sha256(sums_stream.read()).hexdigest()
            != manifest.get("sha256sums_sha256")
        ):
            raise ValueError("Codex toolchain SHA256SUMS mismatch")
    return {**manifest, "path": str(bundle), "bundle_sha256": bundle_sha256}


__all__ = [
    "ROOT_NAME",
    "SCHEMA",
    "TARGET",
    "build_codex_toolchain_bundle",
    "validate_codex_toolchain_bundle",
]
