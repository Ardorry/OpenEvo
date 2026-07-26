"""Windows Desktop export of a hash-verified, item-content-free audit package."""

from __future__ import annotations

import difflib
import hashlib
import html
import re
import shutil
import subprocess
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)

_SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")
_PATH_RE = re.compile(r"(?:/home/|/mnt/|[A-Za-z]:\\|(?:data|results|state)/)\S*", re.IGNORECASE)
_QUESTION_RE = re.compile(r"(?im)^\s*(?:question|[ABCD]\.)\s*:\s*.+$")
_DESKTOP_SAFE_CSVS = frozenset(
    {
        "correctness_transitions.csv",
        "memory_evidence_count_distribution.csv",
        "memory_growth.csv",
        "per_category_results.csv",
        "probe_learning_curve.csv",
        "rule_status_counts.csv",
        "training_round_accuracy.csv",
    }
)


def export_desktop_audit_v1(
    *,
    repository_root: Path,
    run_result_root: Path,
    run_state_root: Path,
    run_id: str,
    phase: str,
    desktop_root: Path | None = None,
) -> dict[str, object]:
    if phase not in {"post_train", "final"}:
        raise ValueError("desktop export phase is invalid")
    desktop = _desktop_path() if desktop_root is None else desktop_root.resolve()
    if not desktop.is_dir():
        raise RuntimeError("WINDOWS_DESKTOP_UNAVAILABLE")
    commit = _git_commit(repository_root)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = desktop / f"OpenEvo_ChemBench_Supervised_Transfer_Audit_{timestamp}_{commit[:12]}"
    if root.exists():
        raise RuntimeError("DESKTOP_AUDIT_DIRECTORY_EXISTS")
    root.mkdir(mode=0o755)

    memory_root = run_state_root / "private/checkpoint_memory"
    exported_memories: dict[tuple[str, int], str] = {}
    for category in CHEMBENCH4K_CATEGORIES:
        for checkpoint in (0, 10, 20, 30, 40, 50):
            source = memory_root / category / f"checkpoint_{checkpoint:02d}.md"
            if not source.is_file():
                continue
            sanitized = _sanitize_memory(source.read_text(encoding="utf-8"))
            destination = root / "memory" / category / f"checkpoint_{checkpoint:02d}.md"
            write_public_file(destination, sanitized.encode("utf-8"))
            exported_memories[(category, checkpoint)] = sanitized
    for category in CHEMBENCH4K_CATEGORIES:
        checkpoints = sorted(
            checkpoint for candidate, checkpoint in exported_memories if candidate == category
        )
        for left, right in pairwise(checkpoints):
            diff = "".join(
                difflib.unified_diff(
                    exported_memories[(category, left)].splitlines(keepends=True),
                    exported_memories[(category, right)].splitlines(keepends=True),
                    fromfile=f"checkpoint_{left:02d}",
                    tofile=f"checkpoint_{right:02d}",
                )
            )
            write_public_file(
                root / "diffs" / category / f"checkpoint_{left:02d}_to_{right:02d}.diff",
                diff.encode("utf-8"),
            )

    rules = _collect_rules(exported_memories.values())
    for filename, values in rules.items():
        write_public_file(
            root / "rules" / filename,
            ("\n".join(values).rstrip() + "\n").encode("utf-8"),
        )
    report_names = {
        "final_report.md": "final_test_report.md",
        "probe_learning_curve.md": "probe_learning_curve.md",
        "training_analysis.md": "training_analysis.md",
    }
    for source_name, target_name in report_names.items():
        source = run_result_root / "reports" / source_name
        content = (
            source.read_text(encoding="utf-8")
            if source.is_file()
            else f"# {target_name}\n\nNot available during {phase} export.\n"
        )
        write_public_file(root / "reports" / target_name, _sanitize_report(content).encode())
    charts_source = run_result_root / "reports"
    if charts_source.is_dir():
        safe_sources = [
            source
            for source in sorted(charts_source.rglob("*"))
            if source.is_file()
            and (
                source.name in _DESKTOP_SAFE_CSVS
                or (
                    source.parent.name == "charts"
                    and source.suffix.lower() in {".png", ".html"}
                )
            )
        ]
        for source in safe_sources:
            destination = root / "charts" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o644)

    readme = (
        "# OpenEvo ChemBench Supervised Transfer Audit\n\n"
        f"Phase: {phase}\n\n"
        "Research-only supervised evolution. Train answers were visible only to the "
        "Train reflector. Probe and Test never evolved memory. This package excludes "
        "questions, options, targets, UID mappings, auth, proxy settings, private packets, "
        "feedback, and event streams.\n"
    )
    write_public_file(root / "README_FIRST.md", readme.encode("utf-8"))
    index = _index_markdown(root, phase=phase, run_id=run_id)
    write_public_file(root / "index.md", index.encode("utf-8"))
    write_public_file(
        root / "index.html",
        (
            "<!doctype html><html><head><meta charset='utf-8'><title>Audit</title>"
            "<style>body{font-family:system-ui;max-width:1000px;margin:2rem auto;"
            "white-space:pre-wrap}</style></head><body>"
            f"{html.escape(index)}</body></html>"
        ).encode(),
    )
    manifest = _export_manifest(root, phase=phase, run_id=run_id, commit=commit)
    write_public_file(root / "export_manifest.json", canonical_pretty_json_bytes(manifest))
    _verify_manifest(root, manifest)
    manifest_sha256 = sha256_bytes((root / "export_manifest.json").read_bytes())
    return {
        "status": "PASS",
        "phase": phase,
        "directory_name": root.name,
        "path": str(root),
        "file_count": len(manifest["files"]) + 1,
        "total_bytes": sum(int(item["size_bytes"]) for item in manifest["files"])
        + (root / "export_manifest.json").stat().st_size,
        "manifest_sha256": manifest_sha256,
        "hash_verification": "PASS",
    }


def _sanitize_memory(text: str) -> str:
    findings = 0

    def redact(match: re.Match[str], code: str) -> str:
        nonlocal findings
        findings += 1
        digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:12]
        return f"[REDACTED_SUSPECTED_TRAIN_LITERAL:{code}:{digest}]"

    text = _PATH_RE.sub(lambda match: redact(match, "PATH"), text)
    text = _QUESTION_RE.sub(lambda match: redact(match, "ITEM_BLOCK"), text)
    # A full SHA-256 in an exported rule could be mistaken for a UID mapping.
    text = _SHA256_RE.sub(lambda match: redact(match, "OPAQUE_SHA256"), text)
    return text


def _sanitize_report(text: str) -> str:
    return _PATH_RE.sub("[REDACTED_PATH]", text)


def _collect_rules(memories: object) -> dict[str, list[str]]:
    result = {
        "confirmed_rules.md": ["# Confirmed Rules", ""],
        "provisional_rules.md": ["# Provisional Rules", ""],
        "retired_rules.md": ["# Retired Rules", ""],
        "contradictions.md": ["# Contradictions", ""],
    }
    for memory in memories:
        if not isinstance(memory, str):
            continue
        current = ""
        for line in memory.splitlines():
            if line.startswith("## "):
                current = line[3:]
                continue
            if not line.startswith("- "):
                continue
            if current == "Confirmed Principles":
                result["confirmed_rules.md"].append(line)
            elif current == "Provisional Principles":
                result["provisional_rules.md"].append(line)
            elif current == "Retired Or Contradicted":
                result["retired_rules.md"].append(line)
                if "contradict" in line.casefold():
                    result["contradictions.md"].append(line)
    for values in result.values():
        if len(values) == 2:
            values.append("- None.")
    return result


def _desktop_path() -> Path:
    completed = subprocess.run(
        (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Environment]::GetFolderPath('Desktop')",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    windows = completed.stdout.replace("\r", "").strip()
    if completed.returncode != 0 or not windows:
        raise RuntimeError("WINDOWS_DESKTOP_UNAVAILABLE")
    converted = subprocess.run(
        ("wslpath", "-u", windows),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if converted.returncode != 0 or not converted.stdout.strip():
        raise RuntimeError("WINDOWS_DESKTOP_UNAVAILABLE")
    return Path(converted.stdout.strip()).resolve()


def _git_commit(repository: Path) -> str:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise RuntimeError("GIT_COMMIT_UNAVAILABLE")
    return value


def _index_markdown(root: Path, *, phase: str, run_id: str) -> str:
    paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )
    return "\n".join(
        [
            "# Audit Package Index",
            "",
            f"Run: {run_id}",
            f"Phase: {phase}",
            "",
            *[f"- `{path}`" for path in paths],
            "",
        ]
    )


def _export_manifest(root: Path, *, phase: str, run_id: str, commit: str) -> dict[str, object]:
    files = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "export_manifest.json":
            continue
        payload = path.read_bytes()
        files.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "sha256": sha256_bytes(payload),
            }
        )
    return {
        "schema_version": "chembench_supervised_transfer_desktop_export_v1",
        "run_id": run_id,
        "phase": phase,
        "source_commit": commit,
        "files": files,
    }


def _verify_manifest(root: Path, manifest: dict[str, object]) -> None:
    rows = manifest["files"]
    if not isinstance(rows, list):
        raise TypeError("DESKTOP_EXPORT_MANIFEST_INVALID")
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("DESKTOP_EXPORT_MANIFEST_INVALID")
        path = root / str(row["path"])
        payload = path.read_bytes()
        if len(payload) != row["size_bytes"] or sha256_bytes(payload) != row["sha256"]:
            raise RuntimeError("DESKTOP_EXPORT_HASH_MISMATCH")


__all__ = ["export_desktop_audit_v1"]
