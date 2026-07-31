"""Compose only benchmark instructions and a task-local teacher overlay.

Global agent-system, text-memory, and skill artifacts are intentionally absent
from this module.  OpenEvo Core materializes those registered artifacts into
the managed runtime from ``metadata.evolution.context_artifact_ids``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .hashing import sha256_bytes


_RUNTIME_CAPTURE_SAFETY = """## Runtime capture safety

Keep terminal output bounded and limited to printable UTF-8 text. Never print,
dump, or decode raw binary file bytes (including PDF bytes) to stdout or stderr,
and never use Latin-1 as a binary-to-text fallback. Use a text-aware parser; if
one is unavailable, skip that extraction rather than emitting binary data. This
is an execution-integrity requirement and does not change the scientific task.
"""


_WORKSPACE_PUBLICATION_SAFETY = """## Workspace publication safety

The current workspace is a publishable scientific artifact, not a location for
runtime environments or package caches. Create virtual environments, dependency
trees, and other transient tool state outside the current workspace (for
example, under /tmp). Do not create symbolic links or other special filesystem
entries in the workspace.

Before completing, preserve all requested scientific deliverables but remove
runtime-only directories accidentally created in the workspace, including
.venv, venv, node_modules, __pycache__, .pytest_cache, .mypy_cache, and
.ruff_cache. The final workspace may contain directories and regular files only:
no symbolic links, sockets, FIFOs, or device files. This is a publication-safety
requirement and does not change the scientific task.
"""


_DELIVERABLE_VALIDATION_CONTRACT = """## Deliverable validation contract

Before completing, run a final structural check against these adapter-owned
publication requirements. Keep a substantive `report/report.md` and use
explicit Markdown section headings for all three semantic groups:

- **Method**, **Methodology**, **Approach**, **Experiments**, or **Pipeline**;
- **Results**, **Findings**, or **Analysis**; and
- **Discussion**, **Limitations**, **Conclusion**, **Implications**, or
  **Interpretation**.

Creative section titles may be added, but must not replace those explicit
scientific headings. Keep reproducible code in `code/`, derived evidence in
`outputs/`, and at least one valid PNG figure under `report/images/` that is
referenced from the report by a relative path. Ensure numerical claims in the
report are traceable to an output file. Remove TODO/TBD/placeholder markers and
do not use absolute-path links. These checks expose the existing publication
validator contract; they do not alter the scientific task or its scoring.
"""


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
    sections: list[str] = [
        "official_instructions",
        "runtime_capture_safety",
        "workspace_publication_safety",
        "deliverable_validation_contract",
    ]
    parts = [
        original.rstrip(),
        _RUNTIME_CAPTURE_SAFETY.rstrip(),
        _WORKSPACE_PUBLICATION_SAFETY.rstrip(),
        _DELIVERABLE_VALIDATION_CONTRACT.rstrip(),
    ]
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
