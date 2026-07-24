"""Deterministic source identity for the standalone ChemBench4K v2 package."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SOURCE_MANIFEST_SCHEMA = "chembench_source_manifest_v2"
DEFAULT_SOURCE_MANIFEST = Path("manifests/chembench_source_manifest_v2.json")

# This is an explicit allowlist, not a documentation-only classification.  The
# source manifest builder below admits only these inputs.
SOURCE_IDENTITY_INPUTS = (
    ".gitignore",
    "README.md",
    "pyproject.toml",
    "configs/**",
    "scripts/**",
    "src/**",
    "tests/**",
    "manifests/v2/*_public_manifest.jsonl",
    "manifests/v2/*_manifest_summary.json",
    "manifests/v2/public_artifact_metadata_v2.json",
)

# These are generated evidence, private data, runtime state, or local tooling
# products.  They are deliberately outside source identity so creating or
# recreating them cannot make the source manifest self-referential.
DERIVED_RUNTIME_OUTPUTS = (
    "manifests/chembench_source_manifest_v2.json",
    "manifests/v2/benchmark_execution_receipt_v2.json",
    "manifests/v2/source_manifest_acceptance_v2.json",
    "private_manifests/**",
    "state/**",
    "results/**",
    "data/**",
    "datasets/**",
    "logs/**",
    "auth/**",
    "artifacts/**",
    "cache/**",
    "runtime/**",
    "tmp/**",
    ".venv/**",
    "venv/**",
    "**/__pycache__/**",
    "**/*.egg-info/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/*.log",
    "**/*.py[cod]",
    "**/*.sqlite*",
    "**/*.tmp",
)

_DERIVED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "auth",
        "cache",
        "data",
        "datasets",
        "logs",
        "private_manifests",
        "results",
        "runtime",
        "state",
        "tmp",
        "venv",
    }
)
_EXCLUDED_SUFFIXES = frozenset({".log", ".pyc", ".pyo", ".sqlite", ".sqlite3"})
_EXCLUDED_EXACT_PATHS = frozenset(
    {
        DEFAULT_SOURCE_MANIFEST.as_posix(),
        "manifests/v2/benchmark_execution_receipt_v2.json",
        "manifests/v2/source_manifest_acceptance_v2.json",
    }
)


class SourceManifestError(RuntimeError):
    """Raised when the benchmark source identity cannot be established."""


@dataclass(frozen=True, slots=True)
class SourceFileIdentity:
    """One immutable source-file inventory entry."""

    path: str
    size: int
    sha256: str

    def to_payload(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


def sha256_file(path: Path) -> str:
    """Hash a regular file without following a final symlink."""

    if not isinstance(path, Path):
        raise TypeError("path must be pathlib.Path")
    if path.is_symlink() or not path.is_file():
        raise SourceManifestError("source inventory member must be a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_derived_runtime_output(relative: PurePosixPath) -> bool:
    if not relative.parts:
        return True
    if relative.as_posix() in _EXCLUDED_EXACT_PATHS:
        return True
    directory_parts = relative.parts[:-1]
    if any(part in _DERIVED_DIRECTORY_NAMES for part in directory_parts):
        return True
    if any(part.endswith(".egg-info") for part in directory_parts):
        return True
    if relative.suffix in _EXCLUDED_SUFFIXES:
        return True
    if relative.name.endswith(
        (
            ".sqlite-shm",
            ".sqlite-wal",
            ".sqlite3-shm",
            ".sqlite3-wal",
            ".tmp",
        )
    ):
        return True
    if relative.name in {".DS_Store"}:
        return True
    return False


def _matches_source_identity_input(relative: PurePosixPath) -> bool:
    if relative.as_posix() in {".gitignore", "README.md", "pyproject.toml"}:
        return True
    if relative.parts[0] in {"configs", "scripts", "src", "tests"}:
        return True
    if len(relative.parts) != 3 or relative.parts[:2] != ("manifests", "v2"):
        return False
    return (
        relative.name.endswith("_public_manifest.jsonl")
        or relative.name.endswith("_manifest_summary.json")
        or relative.name == "public_artifact_metadata_v2.json"
    )


def _is_source_file(relative: PurePosixPath) -> bool:
    return not _is_derived_runtime_output(relative) and _matches_source_identity_input(relative)


def build_source_manifest(
    package_root: Path,
    *,
    output_relative_path: Path = DEFAULT_SOURCE_MANIFEST,
) -> dict[str, object]:
    """Build a byte-stable inventory of all formal benchmark source files.

    The generated manifest excludes itself to avoid a self-referential digest.
    Dataset snapshots are identified by their independent dataset manifest.
    """

    if not isinstance(package_root, Path):
        raise TypeError("package_root must be pathlib.Path")
    if not package_root.is_dir() or package_root.is_symlink():
        raise SourceManifestError("package_root must be an existing real directory")
    if not isinstance(output_relative_path, Path) or output_relative_path.is_absolute():
        raise ValueError("output_relative_path must be a relative pathlib.Path")
    if ".." in output_relative_path.parts:
        raise ValueError("output_relative_path must remain under package_root")

    excluded_output = output_relative_path.as_posix()
    entries: list[SourceFileIdentity] = []
    for candidate in sorted(package_root.rglob("*")):
        try:
            relative = PurePosixPath(candidate.relative_to(package_root).as_posix())
        except ValueError as exc:  # pragma: no cover - rglob guarantees containment
            raise SourceManifestError("source path escaped package root") from exc
        if relative.as_posix() == excluded_output or not _is_source_file(relative):
            continue
        if candidate.is_symlink():
            raise SourceManifestError("source tree contains a symlink")
        if not candidate.is_file():
            continue
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise SourceManifestError("source file metadata is unavailable") from exc
        entries.append(
            SourceFileIdentity(
                path=relative.as_posix(),
                size=size,
                sha256=sha256_file(candidate),
            )
        )

    if not entries:
        raise SourceManifestError("source inventory is empty")
    files_payload = [entry.to_payload() for entry in entries]
    inventory_bytes = json.dumps(
        files_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "root": "benchmarks/chembench",
        "source_identity_inputs": list(SOURCE_IDENTITY_INPUTS),
        "derived_runtime_outputs": list(DERIVED_RUNTIME_OUTPUTS),
        "files": files_payload,
        "file_count": len(files_payload),
        "combined_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "excluded_generated_manifest": excluded_output,
        "dataset_identity_is_external": True,
    }


def canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    """Serialize a source manifest deterministically."""

    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_source_manifest(
    package_root: Path,
    output_path: Path | None = None,
) -> tuple[Path, str]:
    """Atomically write the current source manifest and return its SHA-256."""

    output = package_root / (output_path or DEFAULT_SOURCE_MANIFEST)
    relative = output.relative_to(package_root)
    payload = canonical_manifest_bytes(
        build_source_manifest(package_root, output_relative_path=relative)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(output)
    return output, hashlib.sha256(payload).hexdigest()


def verify_source_manifest(package_root: Path, manifest_path: Path) -> str:
    """Recompute and validate an existing source manifest."""

    if not isinstance(manifest_path, Path):
        raise TypeError("manifest_path must be pathlib.Path")
    try:
        relative = manifest_path.relative_to(package_root)
        stored_bytes = manifest_path.read_bytes()
        stored = json.loads(stored_bytes)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SourceManifestError("source manifest is unavailable or invalid") from exc
    expected = canonical_manifest_bytes(
        build_source_manifest(package_root, output_relative_path=relative)
    )
    if stored_bytes != expected or stored != json.loads(expected):
        raise SourceManifestError("source manifest does not match the current source tree")
    return hashlib.sha256(stored_bytes).hexdigest()


__all__ = [
    "DEFAULT_SOURCE_MANIFEST",
    "DERIVED_RUNTIME_OUTPUTS",
    "SOURCE_MANIFEST_SCHEMA",
    "SOURCE_IDENTITY_INPUTS",
    "SourceFileIdentity",
    "SourceManifestError",
    "build_source_manifest",
    "canonical_manifest_bytes",
    "sha256_file",
    "verify_source_manifest",
    "write_source_manifest",
]
