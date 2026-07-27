"""Bind supervised transfer to the Codex package pinned by OpenEvo Core."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openevo.runtime.managed import (
    MANAGED_CODEX_NPM_PACKAGE,
    MANAGED_CODEX_VERSION,
)

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
)

MANAGED_CODEX_SOURCE = "openevo_core_managed_codex_v1"
MANAGED_CODEX_RECEIPT_SCHEMA = "OpenEvoManagedCodexReceiptV1"
MANAGED_CODEX_RECEIPT_FILENAME = "managed_codex_receipt_v1.json"
_EXPECTED_ROOT_RELATIVE = "state/chembench_supervised_transfer_v1/managed_codex"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class ManagedCodexError(RuntimeError):
    """Closed failure while loading the OpenEvo-managed Codex installation."""

    def __init__(self, finding_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", finding_code) is None:
            raise ValueError("managed Codex finding code is invalid")
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class OpenEvoManagedCodexIdentityV1:
    """Verified identity and executable for one Core-pinned Codex package."""

    root: Path
    executable: Path
    executable_sha256: str
    launcher_sha256: str
    package_json_sha256: str
    platform_package_json_sha256: str
    codex_cli_version: str
    npm_package: str
    openevo_distribution_version: str
    receipt_sha256: str

    @property
    def source(self) -> str:
        return MANAGED_CODEX_SOURCE

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_pretty_json_bytes(self.public_identity))

    @property
    def public_identity(self) -> dict[str, object]:
        return {
            "source": self.source,
            "npm_package": self.npm_package,
            "codex_cli_version": self.codex_cli_version,
            "openevo_distribution_version": self.openevo_distribution_version,
            "executable_sha256": self.executable_sha256,
            "launcher_sha256": self.launcher_sha256,
            "package_json_sha256": self.package_json_sha256,
            "platform_package_json_sha256": self.platform_package_json_sha256,
            "receipt_sha256": self.receipt_sha256,
        }


def write_managed_codex_receipt_v1(
    *,
    repository_root: Path,
    managed_root_relative: str = _EXPECTED_ROOT_RELATIVE,
) -> tuple[dict[str, object], str]:
    """Write one deterministic receipt after a new managed package install."""

    repository, root = _resolve_roots(repository_root, managed_root_relative)
    receipt_path = root / MANAGED_CODEX_RECEIPT_FILENAME
    if receipt_path.exists():
        raise ManagedCodexError("MANAGED_CODEX_RECEIPT_ALREADY_EXISTS")
    payload, _executable = _inspect_installation(repository, root, managed_root_relative)
    encoded = canonical_pretty_json_bytes(payload)
    descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        receipt_path.unlink(missing_ok=True)
        raise
    receipt_path.chmod(0o600)
    return payload, sha256_bytes(encoded)


def load_managed_codex_v1(
    *,
    repository_root: Path,
    managed_root_relative: str = _EXPECTED_ROOT_RELATIVE,
) -> OpenEvoManagedCodexIdentityV1:
    """Load a receipt and re-attest the exact executable before every run."""

    repository, root = _resolve_roots(repository_root, managed_root_relative)
    receipt_path = root / MANAGED_CODEX_RECEIPT_FILENAME
    _require_owned_regular_file(receipt_path, executable=False, exact_mode=0o600)
    try:
        encoded = receipt_path.read_bytes()
        loaded = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedCodexError("MANAGED_CODEX_RECEIPT_INVALID") from exc
    expected, executable = _inspect_installation(repository, root, managed_root_relative)
    if loaded != expected or encoded != canonical_pretty_json_bytes(expected):
        raise ManagedCodexError("MANAGED_CODEX_IDENTITY_DRIFT")
    receipt_sha256 = sha256_bytes(encoded)
    return OpenEvoManagedCodexIdentityV1(
        root=root,
        executable=executable,
        executable_sha256=str(expected["executable_sha256"]),
        launcher_sha256=str(expected["launcher_sha256"]),
        package_json_sha256=str(expected["package_json_sha256"]),
        platform_package_json_sha256=str(
            expected["platform_package_json_sha256"]
        ),
        codex_cli_version=str(expected["codex_cli_version"]),
        npm_package=str(expected["npm_package"]),
        openevo_distribution_version=str(expected["openevo_distribution_version"]),
        receipt_sha256=receipt_sha256,
    )


def _resolve_roots(repository_root: Path, managed_root_relative: str) -> tuple[Path, Path]:
    if not isinstance(repository_root, Path) or not repository_root.is_absolute():
        raise TypeError("repository_root must be an absolute Path")
    if managed_root_relative != _EXPECTED_ROOT_RELATIVE:
        raise ManagedCodexError("MANAGED_CODEX_ROOT_NOT_PROTOCOL_FIXED")
    repository = repository_root.resolve(strict=True)
    try:
        root = (repository / managed_root_relative).resolve(strict=True)
    except OSError as exc:
        raise ManagedCodexError("MANAGED_CODEX_INSTALLATION_MISSING") from exc
    try:
        root.relative_to(repository)
    except ValueError as exc:
        raise ManagedCodexError("MANAGED_CODEX_ROOT_OUTSIDE_REPOSITORY") from exc
    metadata = root.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ManagedCodexError("MANAGED_CODEX_ROOT_UNSAFE")
    return repository, root


def _inspect_installation(
    repository: Path,
    root: Path,
    managed_root_relative: str,
) -> tuple[dict[str, object], Path]:
    launcher = root / "bin/codex"
    try:
        launcher_metadata = launcher.lstat()
    except OSError as exc:
        raise ManagedCodexError("MANAGED_CODEX_LAUNCHER_INVALID") from exc
    if not stat.S_ISLNK(launcher_metadata.st_mode):
        raise ManagedCodexError("MANAGED_CODEX_LAUNCHER_INVALID")
    launcher_target = os.readlink(launcher)
    if launcher_target != "../lib/node_modules/@openai/codex/bin/codex.js":
        raise ManagedCodexError("MANAGED_CODEX_LAUNCHER_INVALID")
    launcher_resolved = launcher.resolve(strict=True)
    _require_inside(root, launcher_resolved)
    _require_owned_regular_file(launcher_resolved, executable=True)

    package_root = root / "lib/node_modules/@openai/codex"
    package_json = package_root / "package.json"
    _require_owned_regular_file(package_json, executable=False)
    package = _read_package_json(package_json)
    if package.get("name") != "@openai/codex" or package.get("version") != MANAGED_CODEX_VERSION:
        raise ManagedCodexError("MANAGED_CODEX_PACKAGE_IDENTITY_INVALID")

    native_candidates = tuple(
        sorted(
            package_root.glob(
                "node_modules/@openai/codex-linux-*/vendor/*/bin/codex"
            )
        )
    )
    if len(native_candidates) != 1:
        raise ManagedCodexError("MANAGED_CODEX_NATIVE_BINARY_INVALID")
    executable = native_candidates[0].resolve(strict=True)
    _require_inside(root, executable)
    _require_owned_regular_file(executable, executable=True)
    platform_root = executable.parents[3]
    platform_package_json = platform_root / "package.json"
    _require_owned_regular_file(platform_package_json, executable=False)
    platform_package = _read_package_json(platform_package_json)
    platform_label = platform_root.name.removeprefix("codex-")
    if (
        platform_root.parent.name != "@openai"
        or platform_label not in {"linux-x64", "linux-arm64"}
        or platform_package.get("name") != "@openai/codex"
        or platform_package.get("version")
        != f"{MANAGED_CODEX_VERSION}-{platform_label}"
    ):
        raise ManagedCodexError("MANAGED_CODEX_PLATFORM_PACKAGE_INVALID")

    codex_cli_version = _read_codex_version(executable, repository)
    expected_cli_version = f"codex-cli {MANAGED_CODEX_VERSION}"
    if codex_cli_version != expected_cli_version:
        raise ManagedCodexError("MANAGED_CODEX_VERSION_MISMATCH")
    payload = {
        "schema_version": MANAGED_CODEX_RECEIPT_SCHEMA,
        "source": MANAGED_CODEX_SOURCE,
        "managed_root_relative": managed_root_relative,
        "npm_package": MANAGED_CODEX_NPM_PACKAGE,
        "codex_cli_version": codex_cli_version,
        "openevo_distribution_version": importlib.metadata.version("openevo"),
        "launcher_relative": "bin/codex",
        "launcher_target": launcher_target,
        "launcher_sha256": _sha256_file(launcher_resolved),
        "package_json_sha256": _sha256_file(package_json),
        "platform_package_json_sha256": _sha256_file(platform_package_json),
        "executable_relative": executable.relative_to(root).as_posix(),
        "executable_size_bytes": executable.stat().st_size,
        "executable_sha256": _sha256_file(executable),
    }
    if any(
        _SHA256_RE.fullmatch(str(payload[key])) is None
        for key in (
            "launcher_sha256",
            "package_json_sha256",
            "platform_package_json_sha256",
            "executable_sha256",
        )
    ):
        raise ManagedCodexError("MANAGED_CODEX_DIGEST_INVALID")
    return payload, executable


def _read_codex_version(executable: Path, repository: Path) -> str:
    try:
        completed = subprocess.run(
            (os.fspath(executable), "--version"),
            cwd=repository,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedCodexError("MANAGED_CODEX_VERSION_UNAVAILABLE") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(
        r"codex-cli [0-9]+\.[0-9]+\.[0-9]+", value
    ) is None:
        raise ManagedCodexError("MANAGED_CODEX_VERSION_UNAVAILABLE")
    return value


def _read_package_json(path: Path) -> dict[str, object]:
    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=closed_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ManagedCodexError("MANAGED_CODEX_PACKAGE_IDENTITY_INVALID") from exc
    if not isinstance(payload, dict):
        raise ManagedCodexError("MANAGED_CODEX_PACKAGE_IDENTITY_INVALID")
    return payload


def _require_inside(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ManagedCodexError("MANAGED_CODEX_PATH_ESCAPE") from exc


def _require_owned_regular_file(
    path: Path,
    *,
    executable: bool,
    exact_mode: int | None = None,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ManagedCodexError("MANAGED_CODEX_FILE_INVALID") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or mode & 0o022
        or (executable and not mode & stat.S_IXUSR)
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise ManagedCodexError("MANAGED_CODEX_FILE_INVALID")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManagedCodexError("MANAGED_CODEX_FILE_INVALID") from exc
    return digest.hexdigest()


__all__ = [
    "MANAGED_CODEX_RECEIPT_FILENAME",
    "MANAGED_CODEX_RECEIPT_SCHEMA",
    "MANAGED_CODEX_SOURCE",
    "ManagedCodexError",
    "OpenEvoManagedCodexIdentityV1",
    "load_managed_codex_v1",
    "write_managed_codex_receipt_v1",
]
