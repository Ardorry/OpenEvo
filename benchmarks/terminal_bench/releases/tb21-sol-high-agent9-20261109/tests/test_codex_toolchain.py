from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from openevo_terminal_bench.codex_toolchain import (
    ROOT_NAME,
    TARGET,
    VENDOR_FILES,
    build_codex_toolchain_bundle,
    validate_codex_toolchain_bundle,
)


def _fake_package(tmp_path: Path) -> Path:
    root = tmp_path / "codex"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"version": "0.144.1"}))
    source = root / "node_modules/@openai/codex-linux-x64"
    for index, relative in enumerate(VENDOR_FILES):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"payload-{index}".encode())
    return root


def _fake_managed_package(tmp_path: Path) -> Path:
    root = tmp_path / "managed-codex"
    root.mkdir()
    (root / "codex-package.json").write_text(
        json.dumps(
            {
                "layoutVersion": 1,
                "version": "0.166.0",
                "target": TARGET,
            }
        )
    )
    prefix = f"vendor/{TARGET}/"
    for index, relative in enumerate(VENDOR_FILES):
        path = root / relative.removeprefix(prefix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"managed-payload-{index}".encode())
    return root


def test_bundle_is_deterministic_and_hash_validated(tmp_path: Path) -> None:
    package = _fake_package(tmp_path)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    receipt = build_codex_toolchain_bundle(
        codex_package_root=package, output=first, version="0.144.1"
    )
    build_codex_toolchain_bundle(
        codex_package_root=package, output=second, version="0.144.1"
    )
    assert first.read_bytes() == second.read_bytes()
    verified = validate_codex_toolchain_bundle(
        first, expected_version="0.144.1", expected_sha256=receipt["bundle_sha256"]
    )
    assert verified["version"] == "0.144.1"
    assert set(verified["files"]) == {"bin/codex", *VENDOR_FILES}
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        validate_codex_toolchain_bundle(
            first, expected_version="0.144.1", expected_sha256="0" * 64
        )


def test_bundle_rejects_unsafe_archive_member(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
        manifest = tarfile.TarInfo(f"{ROOT_NAME}/manifest.json")
        manifest.size = 2
        archive.addfile(manifest, io.BytesIO(b"{}"))
    with pytest.raises(ValueError, match="unsafe"):
        validate_codex_toolchain_bundle(path, expected_version="0.144.1")


def test_bundle_accepts_managed_standalone_package(tmp_path: Path) -> None:
    package = _fake_managed_package(tmp_path)
    output = tmp_path / "managed.tar.gz"
    receipt = build_codex_toolchain_bundle(
        codex_package_root=package, output=output, version="0.166.0"
    )
    verified = validate_codex_toolchain_bundle(
        output,
        expected_version="0.166.0",
        expected_sha256=receipt["bundle_sha256"],
    )
    assert verified["version"] == "0.166.0"
