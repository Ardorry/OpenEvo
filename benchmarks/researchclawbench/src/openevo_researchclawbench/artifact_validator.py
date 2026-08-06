"""Target-independent, fail-closed candidate deliverable validator."""

from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .hashing import (
    UnsafePathError,
    canonical_json_sha256,
    iter_regular_files,
    tree_entries,
    write_sha256_manifest,
)


ABSOLUTE_REFERENCE = re.compile(r"(?:!\[[^\]]*\]|\[[^\]]*\])\((/[^)]+|[A-Za-z]:\\[^)]+)\)")
IMAGE_REFERENCE = re.compile(r"!\[[^\]]*\]\(([^)]+\.png)\)", re.I)
UNFINISHED = re.compile(r"\b(?:TODO|TBD|FIXME|PLACEHOLDER|INSERT (?:RESULT|FIGURE)|NOT YET IMPLEMENTED)\b", re.I)
NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _has_required_report_sections(report_text: str) -> bool:
    """Recognize standard scientific heading variants without inspecting prose.

    ResearchClawBench tasks require the semantic method/results/discussion
    structure, but do not prescribe one exact Markdown title.  In particular,
    a report may close its analysis under ``Limitations`` and ``Conclusion``.
    Requiring the literal word ``discussion`` incorrectly rejects that standard
    form before the trusted Judge can evaluate it.
    """

    headings = [match.casefold() for match in MARKDOWN_HEADING.findall(report_text)]
    required = (
        re.compile(
            r"\b(?:methods?|methodology|approach|study design|experimental design|"
            r"experimental setup|experiments?|pipeline|workflow|procedure|implementation)\b"
        ),
        re.compile(r"\b(?:results?|findings?|analysis)\b"),
        re.compile(
            r"\b(?:discussion|limitations?|conclusions?|implications?|interpretation)\b"
        ),
    )
    return all(any(pattern.search(heading) for heading in headings) for pattern in required)


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    checks: dict[str, bool]
    artifact_root_sha256: str | None
    validator_completeness: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def candidate_artifact_root_sha256(workspace: str | Path) -> str:
    """Hash candidate-owned content, excluding the adapter-owned receipt.

    ``LocalValidationPort`` publishes the root ``validator.json`` only after
    validation has bound the candidate artifact.  Later evaluator checks must
    therefore ignore exactly that control-plane file while continuing to bind
    every candidate deliverable and any nested file with the same basename.
    """

    root = Path(workspace).resolve(strict=True)
    entries = [
        entry for entry in tree_entries(root) if entry["path"] != "validator.json"
    ]
    return canonical_json_sha256(entries)


def _valid_png(path: Path) -> bool:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    saw_ihdr = saw_idat = saw_iend = False
    idat = bytearray()
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        crc_at = offset + 8 + length
        if crc_at + 4 > len(data):
            return False
        expected_crc = struct.unpack(">I", data[crc_at : crc_at + 4])[0]
        if zlib.crc32(kind + chunk) & 0xFFFFFFFF != expected_crc:
            return False
        if kind == b"IHDR":
            if length != 13 or struct.unpack(">II", chunk[:8]) == (0, 0):
                return False
            width, height = struct.unpack(">II", chunk[:8])
            if width == 0 or height == 0:
                return False
            saw_ihdr = True
        elif kind == b"IDAT":
            idat.extend(chunk)
            saw_idat = True
        elif kind == b"IEND":
            saw_iend = True
            break
        offset = crc_at + 4
    if not (saw_ihdr and saw_idat and saw_iend):
        return False
    try:
        zlib.decompress(bytes(idat))
    except zlib.error:
        return False
    return True


def validate_workspace(workspace: str | Path, *, timed_out: bool = False) -> ValidationResult:
    root = Path(workspace).resolve(strict=True)
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}
    try:
        list(iter_regular_files(root))
        checks["safe_regular_files_only"] = True
    except UnsafePathError:
        checks["safe_regular_files_only"] = False
        errors.append("UNSAFE_FILESYSTEM_ENTRY")
    for name in ("code", "outputs", "report"):
        path = root / name
        valid = path.is_dir() and not path.is_symlink()
        checks[f"{name}_directory"] = valid
        if not valid:
            errors.append(f"{name.upper()}_DIRECTORY_MISSING")
    report_path = root / "report" / "report.md"
    report_text = report_path.read_text(encoding="utf-8", errors="replace") if report_path.is_file() else ""
    checks["report_nonempty"] = bool(report_text.strip())
    if not checks["report_nonempty"]:
        errors.append("REPORT_MISSING")
    checks["report_substantive"] = len(report_text) >= 1500 and len(report_text.split()) >= 250
    if report_text and not checks["report_substantive"]:
        errors.append("REPORT_INSUFFICIENT_SUBSTANCE")
    checks["report_sections"] = _has_required_report_sections(report_text)
    if report_text and not checks["report_sections"]:
        errors.append("REPORT_REQUIRED_SECTIONS_MISSING")
    checks["no_unfinished_markers"] = not bool(UNFINISHED.search(report_text))
    if not checks["no_unfinished_markers"]:
        errors.append("UNFINISHED_MARKER")
    checks["no_absolute_references"] = not bool(ABSOLUTE_REFERENCE.search(report_text))
    if not checks["no_absolute_references"]:
        errors.append("ABSOLUTE_PATH_REFERENCE")

    pngs = sorted((root / "report" / "images").glob("*.png")) if (root / "report" / "images").is_dir() else []
    checks["png_present"] = bool(pngs)
    if not pngs:
        errors.append("PNG_MISSING")
    checks["pngs_valid"] = bool(pngs) and all(_valid_png(path) for path in pngs)
    if pngs and not checks["pngs_valid"]:
        errors.append("PNG_INVALID")
    refs = IMAGE_REFERENCE.findall(report_text)
    safe_refs = True
    for ref in refs:
        if Path(ref).is_absolute() or ".." in Path(ref).parts:
            safe_refs = False
            break
        candidate = (report_path.parent / ref).resolve(strict=False)
        if not candidate.is_relative_to(root) or not candidate.is_file():
            safe_refs = False
            break
    checks["image_references_resolve"] = bool(refs) and safe_refs
    if report_text and not checks["image_references_resolve"]:
        errors.append("IMAGE_REFERENCE_INVALID")

    try:
        code_files = list(iter_regular_files(root / "code")) if (root / "code").is_dir() else []
        output_files = list(iter_regular_files(root / "outputs")) if (root / "outputs").is_dir() else []
    except UnsafePathError:
        code_files = []
        output_files = []
        errors.append("UNSAFE_FILESYSTEM_ENTRY")
    checks["code_present"] = bool(code_files)
    checks["outputs_present"] = bool(output_files)
    if not code_files:
        errors.append("CODE_MISSING")
    if not output_files:
        errors.append("OUTPUTS_MISSING")
    output_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")[:200000]
        for path in output_files
        if path.stat().st_size <= 2 * 1024 * 1024
    )
    report_numbers = {match.group(0) for match in NUMBER.finditer(report_text) if len(match.group(0)) >= 2}
    checks["numeric_traceability"] = bool(report_numbers) and any(number in output_text for number in report_numbers)
    if report_text and not checks["numeric_traceability"]:
        errors.append("TRACEABILITY_MISSING")

    meta_path = root / "_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {}
    status_consistent = meta.get("status") == "completed" and meta.get("exit_code") == 0 and not timed_out
    checks["exit_status_consistent"] = status_consistent
    if not status_consistent:
        errors.append("EXIT_STATUS_INCONSISTENT")
    if timed_out:
        errors.append("TIMEOUT")
    manifest_path = root / "file_manifest_sha256.tsv"
    digest: str | None = None
    if not errors:
        write_sha256_manifest(root, manifest_path)
        digest = candidate_artifact_root_sha256(root)
        checks["hash_manifest_written"] = True
    else:
        checks["hash_manifest_written"] = False
    return ValidationResult(
        passed=not errors,
        errors=tuple(sorted(set(errors))),
        warnings=tuple(sorted(set(warnings))),
        checks=checks,
        artifact_root_sha256=digest,
        validator_completeness=sum(1 for value in checks.values() if value),
    )


def freeze_candidate_outputs(workspace: str | Path) -> None:
    root = Path(workspace).resolve(strict=True)
    for name in ("code", "outputs", "report"):
        directory = root / name
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise UnsafePathError(f"cannot freeze unsafe artifact directory: {directory}")
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_symlink():
                raise UnsafePathError(f"cannot freeze symlink: {path}")
            path.chmod(0o444 if path.is_file() else 0o555)
        directory.chmod(0o555)
