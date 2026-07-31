"""Compose only benchmark instructions and a task-local teacher overlay.

Global agent-system, text-memory, and skill artifacts are intentionally absent
from this module.  OpenEvo Core materializes those registered artifacts into
the managed runtime from ``metadata.evolution.context_artifact_ids``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .hashing import sha256_bytes


@dataclass(frozen=True)
class CandidateInstruction:
    text: str
    sha256: str
    sections: tuple[str, ...]


def compose_native_instruction(
    official_instructions: str | Path,
    *,
    task_local_overlay: str | None = None,
) -> CandidateInstruction:
    path = Path(official_instructions)
    original = path.read_text(encoding="utf-8") if path.exists() else str(official_instructions)
    sections: list[str] = ["official_instructions"]
    parts = [original.rstrip()]
    if task_local_overlay is not None and task_local_overlay.strip():
        # The overlay is Community-training-only and never becomes an OpenEvo
        # artifact.  It follows the immutable benchmark prompt so the original
        # text is preserved byte-for-byte as the first section.
        parts.append(
            "## Community training task-local teacher overlay\n\n"
            "This overlay applies only to the current task and current follow-up "
            "attempt. It must not be retained in global memory or skills.\n\n"
            + task_local_overlay.strip()
        )
        sections.append("task_local_overlay")
    text = "\n\n---\n\n".join(parts).rstrip() + "\n"
    return CandidateInstruction(
        text=text,
        sha256=sha256_bytes(text.encode("utf-8")),
        sections=tuple(sections),
    )
