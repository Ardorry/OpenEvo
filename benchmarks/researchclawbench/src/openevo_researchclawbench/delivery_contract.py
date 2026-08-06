"""Generic Protocol v3 Candidate delivery contract and evidence analysis.

The contract is task-agnostic: it never names a task, domain, rubric or
dataset.  The analyzer derives delivery evidence from the persisted Codex
JSONL and the sealed workspace; it never invents model behavior.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DELIVERY_CONTRACT_BLOCK = """\
## Delivery contract

After reading INSTRUCTIONS.md, before large-scale exploration, dependency
installation, or long analysis, create the workspace delivery skeleton:

- code/
- outputs/
- report/
- report/images/

and at least create `report/report.md` with your task understanding, plan,
current progress, and remaining work.  Keep updating it into the final report.

Use /tmp only for temporary caches.  Reproducible scripts must live in
`code/`, key intermediate data in `outputs/`, final figures in
`report/images/`, and conclusions in `report/report.md`.  Do not complete the
whole analysis only inside /tmp.

One of your first tool calls (within the first 5 effective tool calls) must
perform a real workspace write.

Before finishing, run a delivery self-check:

    find code outputs report images -maxdepth 3 -type f -print

and confirm that: `code/` has at least one non-empty implementation file,
`outputs/` has at least one non-empty result file, `report/report.md` is
non-empty and substantive, `report/images/` contains at least one valid PNG,
and every deliverable is inside the current workspace.  If any core
deliverable is missing, continue working instead of declaring completion.

Your final assistant message must state: the task is complete, the report
path, the main code path, the main output path, the main image path, and
whether the delivery self-check passed.
"""


def append_delivery_contract(instructions: str) -> str:
    """Append the generic delivery contract to any Candidate instruction set."""

    return instructions.rstrip() + "\n\n" + DELIVERY_CONTRACT_BLOCK


_TOOL_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "web_search", "todo_list"}
)
_WORKSPACE_RELATIVE_WRITE = re.compile(
    r"(?:^|[\s\"'`(])(?:code|outputs|report|report/images)/", re.I
)
_TMP_PATH = re.compile(r"/tmp(?:[^\s\"'`)]*)", re.I)
_WRITE_TOKEN = re.compile(
    r"(?:cat\s*>|tee\s+|>>|touch\s+|mkdir\s+-p|cp\s+|mv\s+|"
    r"write_text|open\(|to_csv|savefig|put\(|write\(|install\s+--target)",
    re.I,
)
_SELF_CHECK = re.compile(
    r"find\s+code\s+outputs\s+report\s+images", re.I
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _valid_png(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(len(_PNG_MAGIC)) == _PNG_MAGIC
    except OSError:
        return False


def _workspace_deliverables_valid(workspace: Path) -> bool:
    """Independent minimum delivery check (the seal remains the strict gate)."""

    code_ok = any(
        path.is_file() and path.stat().st_size > 0
        for path in (workspace / "code").glob("*")
        if not path.is_symlink()
    ) if (workspace / "code").is_dir() else False
    outputs_ok = any(
        path.is_file() and path.stat().st_size > 0
        for path in (workspace / "outputs").glob("*")
        if not path.is_symlink()
    ) if (workspace / "outputs").is_dir() else False
    report_md = workspace / "report" / "report.md"
    report_ok = (
        report_md.is_file()
        and not report_md.is_symlink()
        and report_md.stat().st_size > 0
    )
    images_dir = workspace / "report" / "images"
    images_ok = (
        images_dir.is_dir()
        and any(_valid_png(path) for path in images_dir.iterdir())
    )
    return code_ok and outputs_ok and report_ok and images_ok


def analyze_delivery_evidence(
    *,
    evidence_root: Path,
    workspace: Path,
) -> dict[str, Any]:
    """Derive delivery evidence from persisted JSONL and workspace state."""

    evidence: dict[str, Any] = {
        "first_workspace_write_at": None,
        "first_workspace_write_event_index": None,
        "workspace_write_event_count": 0,
        "tmp_write_event_count": 0,
        "delivery_scaffold_created": False,
        "delivery_self_check_executed": False,
        "final_message_present": False,
        "required_deliverables_valid": _workspace_deliverables_valid(workspace),
        "delivery_classification": "CANDIDATE_DELIVERY_VALID",
    }
    events_path = evidence_root / "codex_events.jsonl"
    if not events_path.is_file():
        evidence["delivery_classification"] = "CANDIDATE_NONDELIVERY_NO_WORKSPACE_WRITE"
        return evidence

    tool_index = 0
    first_write_line: int | None = None
    first_write_tool: int | None = None
    report_md_written = False
    self_check = False
    workspace_writes = 0
    tmp_writes = 0
    for line_index, line in enumerate(
        events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.started":
            continue
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type not in _TOOL_ITEM_TYPES:
            continue
        tool_index += 1
        if item_type == "file_change":
            workspace_writes += 1
            if first_write_line is None:
                first_write_line = line_index
                first_write_tool = tool_index
            for change in item.get("changes") or []:
                path = str(change.get("path") or "")
                if path.rstrip("/").endswith("report/report.md"):
                    report_md_written = True
            continue
        if item_type != "command_execution":
            continue
        command = str(item.get("command") or "")
        if _SELF_CHECK.search(command):
            self_check = True
        if _TMP_PATH.search(command) and _WRITE_TOKEN.search(command):
            tmp_writes += 1
        if _WORKSPACE_RELATIVE_WRITE.search(command) and _WRITE_TOKEN.search(
            command
        ):
            workspace_writes += 1
            if first_write_line is None:
                first_write_line = line_index
                first_write_tool = tool_index
            if "report/report.md" in command:
                report_md_written = True

    evidence["first_workspace_write_at"] = first_write_line
    evidence["first_workspace_write_event_index"] = first_write_tool
    evidence["workspace_write_event_count"] = workspace_writes
    evidence["tmp_write_event_count"] = tmp_writes
    evidence["delivery_scaffold_created"] = report_md_written
    evidence["delivery_self_check_executed"] = self_check
    evidence["final_message_present"] = (
        evidence_root / "final_assistant_message.json"
    ).is_file()
    if workspace_writes == 0:
        evidence["delivery_classification"] = (
            "CANDIDATE_NONDELIVERY_NO_WORKSPACE_WRITE"
        )
    elif not evidence["required_deliverables_valid"]:
        evidence["delivery_classification"] = (
            "CANDIDATE_NONDELIVERY_INCOMPLETE_SCAFFOLD"
        )
    elif not evidence["final_message_present"]:
        evidence["delivery_classification"] = (
            "CANDIDATE_NONDELIVERY_NO_FINAL_MESSAGE"
        )
    else:
        evidence["delivery_classification"] = "CANDIDATE_DELIVERY_VALID"
    return evidence
