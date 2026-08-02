"""Immutable formal Python/wheel authority for Temperature full-evolve v1.

Previous STV2 and Temperature formal runtimes remain immutable evidence.  This
module creates a new experiment-local environment from the audited dependency installation,
then replaces both editable/old project distributions with wheels built from
the exact clean Git commit.  No model, credential content, or benchmark item is
read while preparing or validating this bundle.
"""

from __future__ import annotations

import base64
import csv
import ctypes
import errno
import hashlib
import importlib.metadata
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
)

FORMAL_RUNTIME_SCHEMA: Final[str] = "TemperatureFormalRuntimeReceiptV1"
FORMAL_RUNTIME_ROOT_RELATIVE: Final[str] = (
    "state/chembench_temperature_full_evolve_v1/formal_runtime_v7"
)
FORMAL_RUNTIME_PYTHON_RELATIVE: Final[str] = (
    f"{FORMAL_RUNTIME_ROOT_RELATIVE}/runtime_venv/bin/python"
)
FORMAL_FRAMEWORK_LOCK_RELATIVE: Final[str] = (
    f"{FORMAL_RUNTIME_ROOT_RELATIVE}/framework/framework-lock.json"
)
FORMAL_RUNTIME_RECEIPT_RELATIVE: Final[str] = (
    f"{FORMAL_RUNTIME_ROOT_RELATIVE}/formal_runtime_receipt_v1.json"
)
FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE: Final[str] = (
    "state/chembench_temperature_full_evolve_v1/formal_runtime_v7_failures"
)
BASE_RUNTIME_ROOT_RELATIVE: Final[str] = (
    "state/chembench_supervised_transfer_v2/runtime_venv"
)

_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_WHEEL_CORE = re.compile(
    r"openevo-(?P<version>[A-Za-z0-9][A-Za-z0-9._+]*)-py3-none-any\.whl\Z",
    re.ASCII,
)
_WHEEL_CHEMBENCH = re.compile(
    r"openevo_chembench-(?P<version>[A-Za-z0-9][A-Za-z0-9._+]*)-py3-none-any\.whl\Z",
    re.ASCII,
)
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}\Z", re.ASCII)
_RENAME_NOREPLACE = 1
_OFFLINE_PREFIX = (
    "/usr/bin/unshare",
    "--user",
    "--map-root-user",
    "--net",
    "--",
)
_SAFE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_CONFIG_FILE": "/dev/null",
    "PIP_NO_INPUT": "1",
    "PYTHONNOUSERSITE": "1",
}
_FORMAL_SOURCE_PATHS = (
    "src/openevo",
    "benchmarks/chembench/src",
    "benchmarks/chembench/configs/temperature_full_evolve_v1",
    "benchmarks/chembench/scripts/temperature_full_evolve_v1",
    "benchmarks/chembench/tests/temperature_full_evolve_v1",
)


class TemperatureFormalRuntimeError(RuntimeError):
    """Content-free formal runtime preparation or identity finding."""

    def __init__(self, finding_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", finding_code) is None:
            raise ValueError("formal runtime finding code is invalid")
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class TemperatureFormalRuntimeIdentityV1:
    repository_root: Path
    root: Path
    runtime_python: Path
    framework_lock: Path
    core_wheel: Path
    chembench_wheel: Path
    receipt_path: Path
    receipt: dict[str, Any]
    receipt_sha256: str

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.receipt))

    @property
    def public_receipt(self) -> dict[str, Any]:
        return {
            **self.receipt,
            "formal_runtime_identity_sha256": self.digest,
            "formal_runtime_receipt_sha256": self.receipt_sha256,
        }


def prepare_temperature_formal_runtime_v1(
    *,
    repository_root: Path,
    bootstrap_python: Path,
) -> TemperatureFormalRuntimeIdentityV1:
    """Build and atomically publish the one immutable v1 formal runtime."""

    repository = _repository(repository_root)
    _require_clean_source(repository)
    bootstrap = _validated_bootstrap_python(repository, bootstrap_python)
    final = repository / FORMAL_RUNTIME_ROOT_RELATIVE
    if _path_entry_exists(final):
        return load_temperature_formal_runtime_v1(repository_root=repository)
    state_root = final.parent
    state_root.mkdir(parents=True, exist_ok=True)
    _require_owned_directory(state_root, exact_mode=None)
    failures = repository / FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE
    failures.mkdir(mode=0o700, exist_ok=True)
    failures.chmod(0o700)
    _require_owned_directory(failures, exact_mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".formal-runtime-", dir=state_root))
    temporary.chmod(0o700)
    try:
        framework = temporary / "framework"
        core_wheel, lock = _build_core_framework_bundle(
            repository_root=repository,
            output_directory=framework,
            bootstrap_python=bootstrap,
        )
        chembench_wheel = _build_chembench_wheel(
            repository=repository,
            bootstrap_python=bootstrap,
            framework_root=framework,
        )
        core_source_sha256 = _wheel_source_digest(
            source_root=repository / "src/openevo",
            wheel=core_wheel,
            wheel_prefix="openevo",
        )
        chembench_source_sha256 = _wheel_source_digest(
            source_root=repository / "benchmarks/chembench/src/openevo_chembench",
            wheel=chembench_wheel,
            wheel_prefix="openevo_chembench",
        )
        runtime_root = temporary / "runtime_venv"
        _materialize_runtime_dependencies(
            repository=repository,
            bootstrap_python=bootstrap,
            destination=runtime_root,
        )
        runtime_python = runtime_root / "bin/python"
        _install_project_wheels(
            runtime_python=runtime_python,
            core_wheel=core_wheel,
            chembench_wheel=chembench_wheel,
        )
        _normalize_project_direct_urls(
            runtime_root=runtime_root,
            core_wheel=core_wheel,
            chembench_wheel=chembench_wheel,
            published_core_wheel=final / "framework" / core_wheel.name,
            published_chembench_wheel=final / "framework" / chembench_wheel.name,
        )
        probe = _probe_runtime_installation(
            runtime_python=runtime_python,
            runtime_root=runtime_root,
            framework_lock=lock,
            core_wheel_sha256=_file_sha256(core_wheel),
            chembench_wheel_sha256=_file_sha256(chembench_wheel),
            expected_core_wheel_uri=(final / "framework" / core_wheel.name).as_uri(),
            expected_chembench_wheel_uri=(
                final / "framework" / chembench_wheel.name
            ).as_uri(),
        )
        source_commit = _git(repository, "rev-parse", "HEAD")
        receipt = {
            "schema_version": FORMAL_RUNTIME_SCHEMA,
            "source_commit": source_commit,
            "source_tree_sha256": sha256_bytes(
                canonical_json_bytes(
                    {
                        "openevo_python_tree_sha256": core_source_sha256,
                        "chembench_python_tree_sha256": chembench_source_sha256,
                    }
                )
            ),
            "openevo_python_tree_sha256": core_source_sha256,
            "chembench_python_tree_sha256": chembench_source_sha256,
            "core_wheel_relative": (
                f"{FORMAL_RUNTIME_ROOT_RELATIVE}/framework/{core_wheel.name}"
            ),
            "core_wheel_sha256": _file_sha256(core_wheel),
            "chembench_wheel_relative": (
                f"{FORMAL_RUNTIME_ROOT_RELATIVE}/framework/{chembench_wheel.name}"
            ),
            "chembench_wheel_sha256": _file_sha256(chembench_wheel),
            "framework_lock_relative": FORMAL_FRAMEWORK_LOCK_RELATIVE,
            "framework_lock_sha256": _file_sha256(lock),
            "runtime_python_relative": FORMAL_RUNTIME_PYTHON_RELATIVE,
            "runtime_python_sha256": _file_sha256(runtime_python),
            "base_dependency_runtime_relative": BASE_RUNTIME_ROOT_RELATIVE,
            "base_dependency_inventory_sha256": _installed_inventory_digest(
                repository / BASE_RUNTIME_ROOT_RELATIVE / "bin/python"
            ),
            "installed_inventory_sha256": probe["installed_inventory_sha256"],
            "openevo_version": probe["openevo_version"],
            "chembench_version": probe["chembench_version"],
            "openevo_origin_relative": probe["openevo_origin_relative"],
            "chembench_origin_relative": probe["chembench_origin_relative"],
            "framework_registry_digest": probe["framework_registry_digest"],
            "core_editable": False,
            "chembench_editable": False,
            "pip_check_passed": True,
            "model_visible_tools_enabled": False,
            "provider_transport_network_enabled": True,
            "model_calls": 0,
        }
        receipt_path = temporary / "formal_runtime_receipt_v1.json"
        write_private_file(
            receipt_path,
            canonical_pretty_json_bytes(receipt),
            replace=False,
        )
        os.sync()
        _publish_directory_noreplace(
            source=temporary,
            destination=final,
            parent=state_root,
        )
        _fsync_directory(state_root)
        return load_temperature_formal_runtime_v1(repository_root=repository)
    except BaseException as exc:
        _quarantine_temporary(
            temporary=temporary,
            state_root=state_root,
            failures=failures,
        )
        if isinstance(exc, TemperatureFormalRuntimeError) or not isinstance(
            exc, Exception
        ):
            raise
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PREPARATION_FAILED") from exc


def load_temperature_formal_runtime_v1(
    *, repository_root: Path
) -> TemperatureFormalRuntimeIdentityV1:
    """Revalidate the immutable runtime against its exact current Git source."""

    repository = _repository(repository_root)
    root = repository / FORMAL_RUNTIME_ROOT_RELATIVE
    _require_owned_directory(root, exact_mode=0o700)
    receipt_path = repository / FORMAL_RUNTIME_RECEIPT_RELATIVE
    receipt = _read_private_receipt(receipt_path)
    _validate_receipt_shape(receipt)
    if receipt["source_commit"] != _git(repository, "rev-parse", "HEAD"):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_SOURCE_COMMIT_DRIFT")
    core_wheel = repository / str(receipt["core_wheel_relative"])
    chembench_wheel = repository / str(receipt["chembench_wheel_relative"])
    framework_lock = repository / str(receipt["framework_lock_relative"])
    runtime_python = repository / str(receipt["runtime_python_relative"])
    paths = {
        core_wheel: receipt["core_wheel_sha256"],
        chembench_wheel: receipt["chembench_wheel_sha256"],
        framework_lock: receipt["framework_lock_sha256"],
    }
    for path, expected in paths.items():
        if _owned_regular_file_sha256(path, exact_mode=0o600) != expected:
            raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_FILE_DIGEST_DRIFT")
    if _runtime_python_sha256(runtime_python) != receipt["runtime_python_sha256"]:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_FILE_DIGEST_DRIFT")
    core_source_sha256 = _wheel_source_digest(
        source_root=repository / "src/openevo",
        wheel=core_wheel,
        wheel_prefix="openevo",
    )
    chembench_source_sha256 = _wheel_source_digest(
        source_root=repository / "benchmarks/chembench/src/openevo_chembench",
        wheel=chembench_wheel,
        wheel_prefix="openevo_chembench",
    )
    source_tree_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "openevo_python_tree_sha256": core_source_sha256,
                "chembench_python_tree_sha256": chembench_source_sha256,
            }
        )
    )
    if (
        receipt["openevo_python_tree_sha256"] != core_source_sha256
        or receipt["chembench_python_tree_sha256"] != chembench_source_sha256
        or receipt["source_tree_sha256"] != source_tree_sha256
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_SOURCE_TREE_DRIFT")
    probe = _probe_runtime_installation(
        runtime_python=runtime_python,
        runtime_root=root / "runtime_venv",
        framework_lock=framework_lock,
        core_wheel_sha256=str(receipt["core_wheel_sha256"]),
        chembench_wheel_sha256=str(receipt["chembench_wheel_sha256"]),
        expected_core_wheel_uri=core_wheel.as_uri(),
        expected_chembench_wheel_uri=chembench_wheel.as_uri(),
    )
    for key in (
        "installed_inventory_sha256",
        "openevo_version",
        "chembench_version",
        "openevo_origin_relative",
        "chembench_origin_relative",
        "framework_registry_digest",
    ):
        if receipt[key] != probe[key]:
            raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_INSTALLATION_DRIFT")
    return TemperatureFormalRuntimeIdentityV1(
        repository_root=repository,
        root=root,
        runtime_python=runtime_python,
        framework_lock=framework_lock,
        core_wheel=core_wheel,
        chembench_wheel=chembench_wheel,
        receipt_path=receipt_path,
        receipt=receipt,
        receipt_sha256=_file_sha256(receipt_path),
    )


def require_temperature_formal_runtime_python_v1(*, repository_root: Path) -> None:
    identity = load_temperature_formal_runtime_v1(repository_root=repository_root)
    actual = Path(os.path.abspath(sys.executable))
    expected = identity.runtime_python.absolute()
    if (
        actual != expected
        or Path(sys.prefix).resolve(strict=True)
        != (identity.root / "runtime_venv").resolve(strict=True)
        or sys.flags.isolated != 1
        or sys.flags.no_user_site != 1
        or sys.flags.ignore_environment != 1
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PYTHON_INVALID")
    try:
        core = importlib.metadata.distribution("openevo")
        chembench = importlib.metadata.distribution("openevo-chembench")
        core_origin = Path(core.locate_file("openevo/__init__.py")).resolve(strict=True)
        chembench_origin = Path(
            chembench.locate_file("openevo_chembench/__init__.py")
        ).resolve(strict=True)
        if (
            core_origin.relative_to(identity.root / "runtime_venv").as_posix()
            != str(identity.receipt["openevo_origin_relative"])
            or chembench_origin.relative_to(
                identity.root / "runtime_venv"
            ).as_posix()
            != str(identity.receipt["chembench_origin_relative"])
        ):
            raise ValueError("project distribution origin drift")
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError) as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PYTHON_INVALID") from exc


_CORE_BUILD_SCRIPT = r"""
import sys
from openevo_chembench.core_evolution_v2 import build_maintainer_framework_bundle_v2

build_maintainer_framework_bundle_v2(
    repository_root=sys.argv[1],
    output_directory=sys.argv[2],
    python_executable=sys.executable,
)
"""


def _build_core_framework_bundle(
    *,
    repository_root: Path,
    output_directory: Path,
    bootstrap_python: Path,
) -> tuple[Path, Path]:
    completed = _run_offline(
        (
            os.fspath(bootstrap_python),
            "-I",
            "-c",
            _CORE_BUILD_SCRIPT,
            os.fspath(repository_root),
            os.fspath(output_directory),
        ),
        cwd=repository_root,
        timeout=900,
    )
    wheels = tuple(sorted(output_directory.glob("openevo-*.whl")))
    lock = output_directory / "framework-lock.json"
    if (
        completed.returncode
        or len(wheels) != 1
        or _WHEEL_CORE.fullmatch(wheels[0].name) is None
        or not lock.is_file()
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_CORE_WHEEL_FAILED")
    return wheels[0], lock


def _build_chembench_wheel(
    *,
    repository: Path,
    bootstrap_python: Path,
    framework_root: Path,
) -> Path:
    completed = _run_offline(
        (
            os.fspath(bootstrap_python),
            "-I",
            "-m",
            "pip",
            "--disable-pip-version-check",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            os.fspath(framework_root),
            os.fspath(repository / "benchmarks/chembench"),
        ),
        cwd=repository,
        timeout=600,
    )
    wheels = tuple(sorted(framework_root.glob("openevo_chembench-*.whl")))
    if (
        completed.returncode
        or len(wheels) != 1
        or _WHEEL_CHEMBENCH.fullmatch(wheels[0].name) is None
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_CHEMBENCH_WHEEL_FAILED")
    wheels[0].chmod(0o600)
    return wheels[0]


def _materialize_runtime_dependencies(
    *,
    repository: Path,
    bootstrap_python: Path,
    destination: Path,
) -> None:
    base = repository / BASE_RUNTIME_ROOT_RELATIVE
    base_python = base / "bin/python"
    base_site = base / "lib/python3.11/site-packages"
    if not base_python.is_file() or not base_site.is_dir():
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_BASE_DEPENDENCIES_MISSING")
    created = _run_offline(
        (os.fspath(bootstrap_python), "-m", "venv", os.fspath(destination)),
        cwd=repository,
        timeout=300,
    )
    if created.returncode:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_VENV_CREATE_FAILED")
    target_site = destination / "lib/python3.11/site-packages"
    copied = subprocess.run(
        (
            "/bin/cp",
            "--archive",
            "--reflink=auto",
            f"{base_site}/.",
            os.fspath(target_site),
        ),
        cwd=repository,
        env=_SAFE_ENV,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=900,
    )
    if copied.returncode:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_DEPENDENCY_COPY_FAILED")
    _remove_copied_project_distributions(target_site)
    for path in target_site.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1:
            raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_DEPENDENCY_HARDLINK")


def _install_project_wheels(
    *, runtime_python: Path, core_wheel: Path, chembench_wheel: Path
) -> None:
    completed = _run_offline(
        (
            os.fspath(runtime_python),
            "-I",
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            os.fspath(core_wheel),
            os.fspath(chembench_wheel),
        ),
        timeout=600,
    )
    if completed.returncode:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_WHEEL_INSTALL_FAILED")
    checked = _run_offline(
        (os.fspath(runtime_python), "-I", "-m", "pip", "check"),
        timeout=300,
    )
    if checked.returncode:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PIP_CHECK_FAILED")


def _remove_copied_project_distributions(site_packages: Path) -> None:
    """Remove only copied project-owned installs before wheel installation."""

    patterns = (
        "openevo",
        "openevo-*.dist-info",
        "openevo_chembench",
        "openevo_chembench-*.dist-info",
        "openevo_chembench*.egg-info",
        "__editable__.openevo*.pth",
        "openevo*.egg-link",
    )
    targets = {candidate for pattern in patterns for candidate in site_packages.glob(pattern)}
    for candidate in sorted(targets):
        if candidate.parent != site_packages:
            raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PROJECT_CLEANUP_INVALID")
        try:
            mode = candidate.lstat().st_mode
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError as exc:
            raise TemperatureFormalRuntimeError(
                "FORMAL_RUNTIME_PROJECT_CLEANUP_FAILED"
            ) from exc


def _normalize_project_direct_urls(
    *,
    runtime_root: Path,
    core_wheel: Path,
    chembench_wheel: Path,
    published_core_wheel: Path,
    published_chembench_wheel: Path,
) -> None:
    site = runtime_root / "lib/python3.11/site-packages"
    _normalize_distribution_direct_url(
        site=site,
        dist_info_pattern="openevo-*.dist-info",
        wheel_sha256=_file_sha256(core_wheel),
        published_wheel=published_core_wheel,
    )
    _normalize_distribution_direct_url(
        site=site,
        dist_info_pattern="openevo_chembench-*.dist-info",
        wheel_sha256=_file_sha256(chembench_wheel),
        published_wheel=published_chembench_wheel,
    )


def _normalize_distribution_direct_url(
    *,
    site: Path,
    dist_info_pattern: str,
    wheel_sha256: str,
    published_wheel: Path,
) -> None:
    matches = tuple(sorted(site.glob(dist_info_pattern)))
    if len(matches) != 1 or matches[0].is_symlink() or not matches[0].is_dir():
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_DISTRIBUTION_METADATA_INVALID")
    dist_info = matches[0]
    direct_path = dist_info / "direct_url.json"
    record_path = dist_info / "RECORD"
    try:
        current = json.loads(
            direct_path.read_bytes(),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemperatureFormalRuntimeError(
            "FORMAL_RUNTIME_DISTRIBUTION_METADATA_INVALID"
        ) from exc
    archive = current.get("archive_info") if isinstance(current, dict) else None
    hashes = archive.get("hashes") if isinstance(archive, dict) else None
    if (
        not isinstance(current, dict)
        or "dir_info" in current
        or not isinstance(archive, dict)
        or not isinstance(hashes, dict)
        or hashes.get("sha256") != wheel_sha256
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_DISTRIBUTION_METADATA_INVALID")
    direct_payload = json.dumps(
        {
            "archive_info": {
                "hash": f"sha256={wheel_sha256}",
                "hashes": {"sha256": wheel_sha256},
            },
            "url": published_wheel.as_uri(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with record_path.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise TemperatureFormalRuntimeError(
            "FORMAL_RUNTIME_DISTRIBUTION_METADATA_INVALID"
        ) from exc
    direct_relative = f"{dist_info.name}/direct_url.json"
    if (
        not rows
        or any(len(row) != 3 for row in rows)
        or len({row[0] for row in rows}) != len(rows)
        or sum(row[0] == direct_relative for row in rows) != 1
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_DISTRIBUTION_METADATA_INVALID")
    digest = base64.urlsafe_b64encode(hashlib.sha256(direct_payload).digest()).rstrip(b"=")
    updated_rows = [
        [
            row[0],
            f"sha256={digest.decode('ascii')}",
            str(len(direct_payload)),
        ]
        if row[0] == direct_relative
        else row
        for row in rows
    ]
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(updated_rows)
    _write_regular_file_atomic(direct_path, direct_payload, mode=0o644)
    _write_regular_file_atomic(
        record_path,
        record_buffer.getvalue().encode("utf-8"),
        mode=0o644,
    )


_PROBE_SCRIPT = r"""
import hashlib
import importlib
import importlib.metadata as metadata
import json
import re
import sys
from pathlib import Path

from openevo.evolution.framework import load_verified_framework_registry
from openevo.runtime.codex_isolation import codex_subscription_cli_overrides

runtime_root = Path(sys.argv[1]).resolve(strict=True)
lock = Path(sys.argv[2]).resolve(strict=True)
core_sha = sys.argv[3]
chembench_sha = sys.argv[4]
expected_core_uri = sys.argv[5]
expected_chembench_uri = sys.argv[6]
site = runtime_root / "lib/python3.11/site-packages"

def closed_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).casefold()

all_distributions = tuple(metadata.distributions(path=[str(site)]))

def distribution_record(name, expected_sha, expected_uri):
    matches = tuple(
        dist for dist in all_distributions
        if canonical_name(str(dist.metadata.get("Name") or "")) == name
    )
    if len(matches) != 1:
        raise SystemExit(30)
    dist = matches[0]
    direct_text = dist.read_text("direct_url.json") or "{}"
    direct = json.loads(direct_text, object_pairs_hook=closed_object)
    if (
        not isinstance(direct, dict)
        or set(direct) != {"archive_info", "url"}
        or direct.get("url") != expected_uri
        or "dir_info" in direct
    ):
        raise SystemExit(31)
    archive = direct.get("archive_info")
    hashes = archive.get("hashes") if isinstance(archive, dict) else None
    if (
        not isinstance(archive, dict)
        or set(archive) != {"hash", "hashes"}
        or archive.get("hash") != f"sha256={expected_sha}"
        or not isinstance(hashes, dict)
        or set(hashes) != {"sha256"}
        or hashes.get("sha256") != expected_sha
    ):
        raise SystemExit(32)
    return dist

for marker in site.iterdir():
    lower_name = marker.name.casefold()
    if marker.suffix.casefold() not in {".pth", ".egg-link"}:
        continue
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 1024 * 1024:
        raise SystemExit(34)
    marker_text = marker.read_text(encoding="utf-8")
    if (
        lower_name.startswith("__editable__")
        or marker.suffix.casefold() == ".egg-link"
        or "openevo" in lower_name
        or "openevo" in marker_text.casefold()
        or any(
            line.strip()
            and not line.lstrip().startswith(("#", "import ", "import\t"))
            for line in marker_text.splitlines()
        )
    ):
        raise SystemExit(35)

core = distribution_record("openevo", core_sha, expected_core_uri)
chembench = distribution_record(
    "openevo-chembench", chembench_sha, expected_chembench_uri
)
origins = {}
for module_name in ("openevo", "openevo_chembench"):
    path = Path(importlib.import_module(module_name).__file__).resolve(strict=True)
    origins[module_name] = path.relative_to(runtime_root).as_posix()
if origins != {
    "openevo": "lib/python3.11/site-packages/openevo/__init__.py",
    "openevo_chembench": "lib/python3.11/site-packages/openevo_chembench/__init__.py",
}:
    raise SystemExit(36)

inventory = []
for dist in metadata.distributions():
    name = (dist.metadata.get("Name") or "").casefold().replace("_", "-")
    record = dist.read_text("RECORD") or ""
    direct = dist.read_text("direct_url.json") or ""
    inventory.append({
        "name": name,
        "version": dist.version,
        "record_sha256": hashlib.sha256(record.encode("utf-8")).hexdigest(),
        "direct_url_sha256": hashlib.sha256(direct.encode("utf-8")).hexdigest(),
    })
inventory.sort(key=lambda value: (value["name"], value["version"], value["record_sha256"]))
overrides = set(codex_subscription_cli_overrides(allow_internet=True, tools_enabled=False))
required = {
    'web_search="disabled"',
    "features.shell_tool=false",
    "features.unified_exec=false",
    "permissions.openevo_codex_subscription_v1.network.enabled=true",
}
if not required <= overrides:
    raise SystemExit(33)
registry = load_verified_framework_registry(lock)
print(json.dumps({
    "installed_inventory_sha256": hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest(),
    "openevo_version": core.version,
    "chembench_version": chembench.version,
    "openevo_origin_relative": origins["openevo"],
    "chembench_origin_relative": origins["openevo_chembench"],
    "framework_registry_digest": registry.snapshot.registry_digest,
}, sort_keys=True, separators=(",", ":")))
"""


def _probe_runtime_installation(
    *,
    runtime_python: Path,
    runtime_root: Path,
    framework_lock: Path,
    core_wheel_sha256: str,
    chembench_wheel_sha256: str,
    expected_core_wheel_uri: str,
    expected_chembench_wheel_uri: str,
) -> dict[str, str]:
    completed = _run_offline(
        (
            os.fspath(runtime_python),
            "-I",
            "-c",
            _PROBE_SCRIPT,
            os.fspath(runtime_root),
            os.fspath(framework_lock),
            core_wheel_sha256,
            chembench_wheel_sha256,
            expected_core_wheel_uri,
            expected_chembench_wheel_uri,
        ),
        timeout=300,
    )
    try:
        payload = json.loads(
            completed.stdout,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PROBE_FAILED") from exc
    expected = {
        "installed_inventory_sha256",
        "openevo_version",
        "chembench_version",
        "openevo_origin_relative",
        "chembench_origin_relative",
        "framework_registry_digest",
    }
    if completed.returncode or not isinstance(payload, dict) or set(payload) != expected:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PROBE_FAILED")
    if (
        any(type(payload[key]) is not str for key in expected)
        or _SHA256.fullmatch(payload["installed_inventory_sha256"]) is None
        or _SHA256.fullmatch(payload["framework_registry_digest"]) is None
        or payload["openevo_origin_relative"]
        != "lib/python3.11/site-packages/openevo/__init__.py"
        or payload["chembench_origin_relative"]
        != "lib/python3.11/site-packages/openevo_chembench/__init__.py"
        or _VERSION.fullmatch(payload["openevo_version"]) is None
        or _VERSION.fullmatch(payload["chembench_version"]) is None
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PROBE_FAILED")
    return payload


_INVENTORY_SCRIPT = r"""
import hashlib
import importlib.metadata as metadata
import json
items = []
for dist in metadata.distributions():
    name = (dist.metadata.get("Name") or "").casefold().replace("_", "-")
    record = dist.read_text("RECORD") or ""
    direct = dist.read_text("direct_url.json") or ""
    items.append({"name": name, "version": dist.version,
                  "record_sha256": hashlib.sha256(record.encode()).hexdigest(),
                  "direct_url_sha256": hashlib.sha256(direct.encode()).hexdigest()})
items.sort(key=lambda value: (value["name"], value["version"], value["record_sha256"]))
print(hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
"""


def _installed_inventory_digest(python: Path) -> str:
    completed = _run_offline(
        (os.fspath(python), "-I", "-c", _INVENTORY_SCRIPT),
        timeout=300,
    )
    value = completed.stdout.decode("ascii", errors="ignore").strip()
    if completed.returncode or _SHA256.fullmatch(value) is None:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_INVENTORY_FAILED")
    return value


def _wheel_source_digest(*, source_root: Path, wheel: Path, wheel_prefix: str) -> str:
    source: dict[str, str] = {}
    for path in sorted(source_root.rglob("*.py")):
        if not path.is_file() or path.is_symlink():
            raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_SOURCE_TREE_INVALID")
        relative = path.relative_to(source_root).as_posix()
        source[f"{wheel_prefix}/{relative}"] = _file_sha256(path)
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_WHEEL_INVALID")
            packaged = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in names
                if name.startswith(f"{wheel_prefix}/") and name.endswith(".py")
            }
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_WHEEL_INVALID") from exc
    if not source or source != packaged:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_WHEEL_SOURCE_MISMATCH")
    return sha256_bytes(canonical_json_bytes(source))


def _validate_receipt_shape(receipt: dict[str, Any]) -> None:
    expected = {
        "schema_version",
        "source_commit",
        "source_tree_sha256",
        "openevo_python_tree_sha256",
        "chembench_python_tree_sha256",
        "core_wheel_relative",
        "core_wheel_sha256",
        "chembench_wheel_relative",
        "chembench_wheel_sha256",
        "framework_lock_relative",
        "framework_lock_sha256",
        "runtime_python_relative",
        "runtime_python_sha256",
        "base_dependency_runtime_relative",
        "base_dependency_inventory_sha256",
        "installed_inventory_sha256",
        "openevo_version",
        "chembench_version",
        "openevo_origin_relative",
        "chembench_origin_relative",
        "framework_registry_digest",
        "core_editable",
        "chembench_editable",
        "pip_check_passed",
        "model_visible_tools_enabled",
        "provider_transport_network_enabled",
        "model_calls",
    }
    digest_keys = {
        "source_tree_sha256",
        "openevo_python_tree_sha256",
        "chembench_python_tree_sha256",
        "core_wheel_sha256",
        "chembench_wheel_sha256",
        "framework_lock_sha256",
        "runtime_python_sha256",
        "base_dependency_inventory_sha256",
        "installed_inventory_sha256",
        "framework_registry_digest",
    }
    core_relative = receipt.get("core_wheel_relative")
    chembench_relative = receipt.get("chembench_wheel_relative")
    core_match = (
        _WHEEL_CORE.fullmatch(Path(core_relative).name)
        if type(core_relative) is str
        else None
    )
    chembench_match = (
        _WHEEL_CHEMBENCH.fullmatch(Path(chembench_relative).name)
        if type(chembench_relative) is str
        else None
    )
    expected_source_tree = None
    if all(type(receipt.get(key)) is str for key in digest_keys):
        expected_source_tree = sha256_bytes(
            canonical_json_bytes(
                {
                    "openevo_python_tree_sha256": receipt[
                        "openevo_python_tree_sha256"
                    ],
                    "chembench_python_tree_sha256": receipt[
                        "chembench_python_tree_sha256"
                    ],
                }
            )
        )
    if (
        set(receipt) != expected
        or receipt.get("schema_version") != FORMAL_RUNTIME_SCHEMA
        or type(receipt.get("source_commit")) is not str
        or _COMMIT.fullmatch(receipt["source_commit"]) is None
        or any(
            type(receipt.get(key)) is not str
            or _SHA256.fullmatch(receipt[key]) is None
            for key in digest_keys
        )
        or receipt.get("source_tree_sha256") != expected_source_tree
        or core_match is None
        or core_relative
        != f"{FORMAL_RUNTIME_ROOT_RELATIVE}/framework/{core_match.group(0)}"
        or chembench_match is None
        or chembench_relative
        != f"{FORMAL_RUNTIME_ROOT_RELATIVE}/framework/{chembench_match.group(0)}"
        or type(receipt.get("openevo_version")) is not str
        or type(receipt.get("chembench_version")) is not str
        or receipt.get("openevo_version") != core_match.group("version")
        or receipt.get("chembench_version") != chembench_match.group("version")
        or _VERSION.fullmatch(receipt["openevo_version"]) is None
        or _VERSION.fullmatch(receipt["chembench_version"]) is None
        or receipt.get("openevo_origin_relative")
        != "lib/python3.11/site-packages/openevo/__init__.py"
        or receipt.get("chembench_origin_relative")
        != "lib/python3.11/site-packages/openevo_chembench/__init__.py"
        or receipt.get("core_editable") is not False
        or receipt.get("chembench_editable") is not False
        or receipt.get("pip_check_passed") is not True
        or receipt.get("model_visible_tools_enabled") is not False
        or receipt.get("provider_transport_network_enabled") is not True
        or type(receipt.get("model_calls")) is not int
        or receipt.get("model_calls") != 0
        or receipt.get("runtime_python_relative") != FORMAL_RUNTIME_PYTHON_RELATIVE
        or receipt.get("framework_lock_relative") != FORMAL_FRAMEWORK_LOCK_RELATIVE
        or receipt.get("base_dependency_runtime_relative")
        != BASE_RUNTIME_ROOT_RELATIVE
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_RECEIPT_INVALID")


def _read_private_receipt(path: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 2 <= metadata.st_size <= 128 * 1024
        ):
            raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_RECEIPT_INVALID")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            encoded = stream.read(128 * 1024 + 1)
        payload = json.loads(
            encoded,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_RECEIPT_INVALID") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_RECEIPT_INVALID")
    return payload


def _require_clean_source(repository: Path) -> None:
    if _git(repository, "status", "--porcelain", "--untracked-files=no"):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_SOURCE_DIRTY")
    scoped = _git(
        repository,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        *_FORMAL_SOURCE_PATHS,
    )
    if scoped:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_SOURCE_DIRTY")


def _validated_bootstrap_python(repository: Path, path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_BOOTSTRAP_INVALID")
    expected_path = repository / ".venv/bin/python"
    if path != expected_path:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_BOOTSTRAP_INVALID")
    try:
        _require_owned_directory(repository / ".venv", exact_mode=None)
        _require_owned_directory(repository / ".venv/bin", exact_mode=None)
        expected = expected_path.resolve(strict=True)
        actual = path.resolve(strict=True)
        link_metadata = expected_path.lstat()
        metadata = expected.stat()
    except (OSError, RuntimeError) as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_BOOTSTRAP_INVALID") from exc
    if (
        actual != expected
        or not stat.S_ISLNK(link_metadata.st_mode)
        or link_metadata.st_uid != os.getuid()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_BOOTSTRAP_INVALID")
    return expected_path


def _repository(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("repository root must be an absolute Path")
    try:
        repository = path.resolve(strict=True)
    except OSError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_REPOSITORY_INVALID") from exc
    if not (repository / ".git").exists():
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_REPOSITORY_INVALID")
    return repository


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_FILE_MISSING") from exc
    return digest.hexdigest()


def _owned_regular_file_sha256(path: Path, *, exact_mode: int) -> str:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != exact_mode
        ):
            raise OSError(errno.EPERM, "formal runtime file identity mismatch")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_FILE_INVALID") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _runtime_python_sha256(path: Path) -> str:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        target = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PYTHON_INVALID") from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or os.readlink(path) != "python3.11"
        or not stat.S_ISREG(target.st_mode)
        or not target.st_mode & stat.S_IXUSR
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PYTHON_INVALID")
    return _file_sha256(resolved)


def _require_owned_directory(path: Path, *, exact_mode: int | None) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_DIRECTORY_INVALID") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or mode & 0o022
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_DIRECTORY_INVALID")


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PATH_INVALID") from exc
    return True


def _write_regular_file_atomic(path: Path, payload: bytes, *, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise TemperatureFormalRuntimeError(
            "FORMAL_RUNTIME_METADATA_WRITE_FAILED"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _run_offline(
    command: tuple[str, ...],
    *,
    timeout: int,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            (*_OFFLINE_PREFIX, *command),
            cwd=cwd,
            env=_SAFE_ENV,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_OFFLINE_COMMAND_FAILED") from exc


def _rename_noreplace(
    source_name: str,
    destination_name: str,
    *,
    source_directory_fd: int,
    destination_directory_fd: int,
) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "formal runtime publication requires Linux renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "formal runtime publication requires renameat2")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_directory_fd,
        os.fsencode(source_name),
        destination_directory_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_name)


def _publish_directory_noreplace(
    *, source: Path, destination: Path, parent: Path
) -> None:
    if source.parent != parent or destination.parent != parent:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PUBLISH_INVALID")
    descriptor = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        _rename_noreplace(
            source.name,
            destination.name,
            source_directory_fd=descriptor,
            destination_directory_fd=descriptor,
        )
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PUBLISH_COLLISION") from exc
    except OSError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_PUBLISH_FAILED") from exc
    finally:
        os.close(descriptor)


def _quarantine_temporary(
    *, temporary: Path, state_root: Path, failures: Path
) -> None:
    if not _path_entry_exists(temporary):
        return
    source_fd = os.open(
        state_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    destination_fd = os.open(
        failures,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for _ in range(128):
            name = f"failed-{secrets.token_hex(24)}"
            try:
                _rename_noreplace(
                    temporary.name,
                    name,
                    source_directory_fd=source_fd,
                    destination_directory_fd=destination_fd,
                )
                os.fsync(source_fd)
                os.fsync(destination_fd)
                return
            except FileExistsError:
                continue
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_QUARANTINE_FAILED")
    except OSError as exc:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_QUARANTINE_FAILED") from exc
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env=_SAFE_ENV,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode:
        raise TemperatureFormalRuntimeError("FORMAL_RUNTIME_GIT_FAILED")
    return completed.stdout.strip()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "FORMAL_FRAMEWORK_LOCK_RELATIVE",
    "FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE",
    "FORMAL_RUNTIME_PYTHON_RELATIVE",
    "FORMAL_RUNTIME_RECEIPT_RELATIVE",
    "FORMAL_RUNTIME_ROOT_RELATIVE",
    "TemperatureFormalRuntimeError",
    "TemperatureFormalRuntimeIdentityV1",
    "load_temperature_formal_runtime_v1",
    "prepare_temperature_formal_runtime_v1",
    "require_temperature_formal_runtime_python_v1",
]
