"""Artifact versioning, admission, and composite construction."""

from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .hashing import UnsafePathError, sha256_bytes, sha256_file, tree_entries, tree_sha256


TASK_LITERAL = re.compile(r"\b[A-Za-z]+_[0-9]{3}\b")
LONG_RESULT_LITERAL = re.compile(r"(?<![A-Za-z0-9])[-+]?\d{5,}(?:\.\d+)?(?:[eE][-+]?\d+)?")
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:/[A-Za-z0-9_.-]+){2,}")
EVALUATOR_TERMS = re.compile(
    r"\b(?:checklist|rubric\s*mode|judge\s*reasoning|target[_ -]?study|target\s+paper|"
    r"per[- ]item\s+score|_score\.json|checklist\s+keyword|checklist\s+weight)\b",
    re.I,
)
NETWORK_LOGIC = re.compile(r"\b(?:curl|wget|requests\.(?:get|post)|urllib\.request|httpx\.|https?://)", re.I)
DESTRUCTIVE_SHELL = re.compile(
    r"\b(?:pkill|killall|docker\s+(?:system|container|volume|network)\s+prune|rm\s+-rf|shutdown|reboot)\b",
    re.I,
)


class AdmissionError(ValueError):
    """Candidate artifact cannot be safely admitted."""


@dataclass(frozen=True)
class ArtifactRef:
    artifact_type: str
    revision: str
    path: Path
    sha256: str
    registry_id: str | None = None


@dataclass(frozen=True)
class AdmissionResult:
    accepted: bool
    artifact_type: str
    proposed_revision: str
    reasons: tuple[str, ...]
    sha256: str | None
    diff: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "artifact_type": self.artifact_type,
            "proposed_revision": self.proposed_revision,
            "reasons": list(self.reasons),
            "sha256": self.sha256,
            "diff": self.diff,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class CompositeArtifact:
    revision: str
    root: Path
    manifest: dict[str, Any]

    @property
    def sha256(self) -> str:
        return str(self.manifest["composite_sha256"])


def _text_reasons(text: str, forbidden_literals: Iterable[str]) -> list[str]:
    reasons: list[str] = []
    if TASK_LITERAL.search(text):
        reasons.append("TASK_ID_LITERAL")
    if EVALUATOR_TERMS.search(text):
        reasons.append("EVALUATOR_PRIVATE_TERM")
    if ABSOLUTE_PATH.search(text):
        reasons.append("ABSOLUTE_PATH")
    if LONG_RESULT_LITERAL.search(text):
        reasons.append("LONG_NUMERIC_LITERAL")
    lowered = text.casefold()
    if any(literal and literal.casefold() in lowered for literal in forbidden_literals):
        reasons.append("SOURCE_TASK_LITERAL")
    return sorted(set(reasons))


def _unified_diff(previous: str, proposed: str, artifact_type: str) -> str:
    return "".join(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"{artifact_type}:parent",
            tofile=f"{artifact_type}:proposal",
        )
    )


def admit_text_artifact(
    artifact_type: str,
    proposed_revision: str,
    proposed: str,
    *,
    previous: str = "",
    forbidden_literals: Iterable[str] = (),
    max_chars: int = 16000,
) -> AdmissionResult:
    if artifact_type not in {"agent_system", "text_memory"}:
        raise ValueError("unsupported text artifact type")
    reasons = _text_reasons(proposed, forbidden_literals)
    if len(proposed) > max_chars:
        reasons.append("LENGTH_LIMIT")
    if proposed and len(proposed.split()) < 8:
        reasons.append("INSUFFICIENT_SUBSTANCE")
    duplicate_lines = [line.strip() for line in proposed.splitlines() if len(line.strip()) > 24]
    if duplicate_lines and len(set(duplicate_lines)) / len(duplicate_lines) < 0.75:
        reasons.append("REPETITIVE_EXPANSION")
    if artifact_type == "text_memory" and re.search(
        r"\b(?:(?:correct|final|expected)\s+answer|final\s+result|observed\s+value)\s*(?:is|=|:)",
        proposed,
        re.I,
    ):
        reasons.append("ANSWER_LIKE_MEMORY")
    diff = _unified_diff(previous, proposed, artifact_type)
    digest = sha256_bytes(proposed.encode("utf-8")) if not reasons else None
    return AdmissionResult(
        not reasons,
        artifact_type,
        proposed_revision,
        tuple(sorted(set(reasons))),
        digest,
        diff,
        len(proposed.encode("utf-8")),
    )


def admit_skill_bundle(
    proposed_revision: str,
    source: str | Path,
    *,
    forbidden_literals: Iterable[str] = (),
    max_files: int = 32,
    max_bytes: int = 512 * 1024,
) -> AdmissionResult:
    root = Path(source).resolve(strict=True)
    reasons: list[str] = []
    try:
        entries = tree_entries(root)
    except UnsafePathError as exc:
        return AdmissionResult(False, "skill_bundle", proposed_revision, ("UNSAFE_FILESYSTEM_ENTRY", str(exc)), None, "", 0)
    total = sum(int(entry["size_bytes"]) for entry in entries)
    if len(entries) > max_files:
        reasons.append("FILE_COUNT_LIMIT")
    if total > max_bytes:
        reasons.append("SIZE_LIMIT")
    if entries and not any(Path(entry["path"]).name.upper() in {"SKILL.MD", "README.MD"} for entry in entries):
        reasons.append("PURPOSE_DOCUMENTATION_MISSING")
    allowed_suffixes = {".md", ".py", ".json", ".yaml", ".yml", ".toml", ".txt"}
    for entry in entries:
        path = root / entry["path"]
        if path.suffix.lower() not in allowed_suffixes:
            reasons.append("UNSUPPORTED_SKILL_FILE_TYPE")
            continue
        text = path.read_text(encoding="utf-8", errors="strict")
        reasons.extend(_text_reasons(text, forbidden_literals))
        if NETWORK_LOGIC.search(text):
            reasons.append("NETWORK_DOWNLOAD_LOGIC")
        if DESTRUCTIVE_SHELL.search(text):
            reasons.append("DESTRUCTIVE_COMMAND")
        if re.search(r"(?:target_study|\.\./|/home/|/mnt/|docker\.sock)", text, re.I):
            reasons.append("HIDDEN_PATH_ACCESS_LOGIC")
    digest = tree_sha256(root) if not reasons else None
    return AdmissionResult(
        not reasons,
        "skill_bundle",
        proposed_revision,
        tuple(sorted(set(reasons))),
        digest,
        "",
        total,
    )


def persist_admitted_text(result: AdmissionResult, text: str, destination: str | Path) -> ArtifactRef:
    del result, text, destination
    raise AdmissionError("adapter artifact persistence is disabled; use the OpenEvo native registry")


def persist_admitted_skills(result: AdmissionResult, source: str | Path, destination: str | Path) -> ArtifactRef:
    del result, source, destination
    raise AdmissionError("adapter artifact persistence is disabled; use the OpenEvo native registry")


def create_composite(
    destination: str | Path,
    *,
    composite_revision: str,
    parent_revision: str | None,
    source_task_id: str | None,
    source_attempt_id: str | None,
    agent_system: ArtifactRef,
    text_memory: ArtifactRef,
    skill_bundle: ArtifactRef,
) -> CompositeArtifact:
    root = Path(destination)
    if root.exists():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    for artifact in (agent_system, text_memory, skill_bundle):
        baseline = artifact.revision.endswith("000")
        if not baseline and not artifact.registry_id:
            raise AdmissionError(
                f"non-baseline {artifact.artifact_type} lacks an OpenEvo registry identifier"
            )
    shutil.copy2(agent_system.path, root / "agent_system.md")
    shutil.copy2(text_memory.path, root / "text_memory.md")
    shutil.copytree(skill_bundle.path, root / "skills", symlinks=False)
    identity = {
        "composite_revision": composite_revision,
        "parent_revision": parent_revision,
        "source_task_id": source_task_id,
        "source_attempt_id": source_attempt_id,
        "agent_system_revision": agent_system.revision,
        "text_memory_revision": text_memory.revision,
        "skill_bundle_revision": skill_bundle.revision,
        "agent_system_sha256": sha256_file(root / "agent_system.md"),
        "text_memory_sha256": sha256_file(root / "text_memory.md"),
        "skill_bundle_sha256": tree_sha256(root / "skills"),
        "agent_system_registry_id": agent_system.registry_id,
        "text_memory_registry_id": text_memory.registry_id,
        "skill_bundle_registry_id": skill_bundle.registry_id,
    }
    composite_sha = sha256_bytes(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    manifest = {
        **identity,
        "composite_sha256": composite_sha,
        "admission_status": "accepted",
        "registry_owner": "OpenEvo evolution worker",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return CompositeArtifact(composite_revision, root, manifest)


def load_composite(root: str | Path) -> CompositeArtifact:
    path = Path(root).resolve(strict=True)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    required = {
        "composite_revision", "agent_system_revision", "text_memory_revision",
        "skill_bundle_revision", "agent_system_sha256", "text_memory_sha256",
        "skill_bundle_sha256", "composite_sha256", "admission_status",
        "agent_system_registry_id", "text_memory_registry_id", "skill_bundle_registry_id",
    }
    if not required.issubset(manifest) or manifest["admission_status"] != "accepted":
        raise AdmissionError("invalid composite manifest")
    if sha256_file(path / "agent_system.md") != manifest["agent_system_sha256"]:
        raise AdmissionError("agent system hash mismatch")
    if sha256_file(path / "text_memory.md") != manifest["text_memory_sha256"]:
        raise AdmissionError("text memory hash mismatch")
    if tree_sha256(path / "skills") != manifest["skill_bundle_sha256"]:
        raise AdmissionError("skill bundle hash mismatch")
    identity = {
        "composite_revision": manifest["composite_revision"],
        "parent_revision": manifest.get("parent_revision"),
        "source_task_id": manifest.get("source_task_id"),
        "source_attempt_id": manifest.get("source_attempt_id"),
        "agent_system_revision": manifest["agent_system_revision"],
        "text_memory_revision": manifest["text_memory_revision"],
        "skill_bundle_revision": manifest["skill_bundle_revision"],
        "agent_system_sha256": manifest["agent_system_sha256"],
        "text_memory_sha256": manifest["text_memory_sha256"],
        "skill_bundle_sha256": manifest["skill_bundle_sha256"],
        "agent_system_registry_id": manifest["agent_system_registry_id"],
        "text_memory_registry_id": manifest["text_memory_registry_id"],
        "skill_bundle_registry_id": manifest["skill_bundle_registry_id"],
    }
    expected = sha256_bytes(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if expected != manifest["composite_sha256"]:
        raise AdmissionError("composite identity hash mismatch")
    return CompositeArtifact(str(manifest["composite_revision"]), path, manifest)
