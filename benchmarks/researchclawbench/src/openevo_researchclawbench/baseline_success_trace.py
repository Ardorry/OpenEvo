"""Deterministic, candidate-grounded preservation inputs for R3 evolution.

The extractor deliberately turns a sealed Candidate workspace and transcript
into a small set of successful evidence-producing steps.  It never reads
evaluation authority and it does not replay a command or call a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SUCCESS_TRACE_SCHEMA = "openevo.researchclawbench.baseline_success_trace.v1"
ACHIEVEMENT_LEDGER_SCHEMA = "openevo.researchclawbench.baseline_achievement_ledger.v2"

_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_PATH_TOKEN = re.compile(r"(?<![\w.-])([\w./-]+\.(?:py|ipynb|r|jl|csv|tsv|json|png|jpg|jpeg|svg|md))(?![\w.-])", re.IGNORECASE)
_CODE_SUFFIXES = frozenset({".py", ".ipynb", ".r", ".jl"})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".svg"})
_NUMERIC_SUFFIXES = frozenset({".csv", ".tsv", ".json"})
_NOISE_COMMAND = re.compile(r"^(?:pwd|ls|find|rg|grep|which|env|printenv|cat|sed|head|tail)\b", re.IGNORECASE)
_SUCCESS_WORDS = frozenset(
    {"created", "wrote", "generated", "executed", "ran", "computed", "built", "validated", "produced", "saved", "rendered"}
)
_LOW_SIGNAL = frozenset(
    {"analysis", "artifact", "candidate", "data", "file", "figure", "image", "output", "report", "result", "run", "script", "summary", "the", "with"}
)


class BaselineSuccessTraceError(RuntimeError):
    """The baseline's candidate-owned evidence cannot be safely summarized."""


def _normalize(value: str) -> str:
    return " ".join(_TOKEN.findall(value.casefold()))


def _candidate_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= 4096:
            raise BaselineSuccessTraceError("SUCCESS_TRACE_EVIDENCE_INVENTORY_TOO_LARGE")
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
    return files


def _safe_ref(root: Path, relative: str) -> bool:
    try:
        if relative.startswith("/") or ".." in Path(relative).parts:
            return False
        path = (root / relative).resolve(strict=True)
        return path.is_relative_to(root) and not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _short(value: str, *, limit: int = 300) -> str:
    compact = " ".join(value.split())
    return compact[:limit].rstrip()


def _refs_in_text(value: str, files: set[str]) -> list[str]:
    refs: list[str] = []
    by_name = {Path(item).name: item for item in files}
    for match in _PATH_TOKEN.finditer(value):
        observed = match.group(1).lstrip("./")
        if observed in files:
            refs.append(observed)
        elif Path(observed).name in by_name:
            refs.append(by_name[Path(observed).name])
    return list(dict.fromkeys(refs))


def _transcript_items(root: Path, files: set[str]) -> list[dict[str, Any]]:
    transcript = root / "_agent_output.jsonl"
    if not transcript.is_file() or transcript.is_symlink():
        return []
    raw = transcript.read_text(encoding="utf-8", errors="replace")[:1_500_000]
    items: list[dict[str, Any]] = []
    for outer_line in raw.splitlines()[:256]:
        try:
            outer = json.loads(outer_line)
        except json.JSONDecodeError:
            continue
        nested = outer.get("metadata", {}).get("transcript") if isinstance(outer, dict) else None
        records: list[Any] = []
        if isinstance(nested, str):
            for line in nested.splitlines()[:2048]:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        else:
            records.append(outer)
        for record in records:
            if not isinstance(record, dict):
                continue
            item = record.get("item") if isinstance(record.get("item"), dict) else record
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "command_execution":
                if item.get("status") != "completed" or item.get("exit_code") not in {0, "0"}:
                    continue
                command = item.get("command")
                if not isinstance(command, str):
                    continue
                normalized = _normalize(command)
                if not normalized or _NOISE_COMMAND.match(normalized):
                    continue
                if not (
                    {"python", "python3", "rscript", "jupyter", "make", "bash"}.intersection(normalized.split())
                    or _refs_in_text(command, files)
                    or any(word in normalized.split() for word in _SUCCESS_WORDS)
                ):
                    continue
                refs = _refs_in_text(command, files)
                items.append(
                    {
                        "decision_context": "Candidate executed a successful evidence-producing command.",
                        "action": f"Execute candidate command: {_short(command, limit=240)}",
                        "candidate_artifact_refs": refs,
                        "result_summary": "The command completed successfully in the baseline trajectory.",
                        "verification": "Transcript records completed command execution with exit code 0.",
                    }
                )
            elif kind == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                normalized = _normalize(text)
                if not set(normalized.split()).intersection(_SUCCESS_WORDS):
                    continue
                refs = _refs_in_text(text, files)
                items.append(
                    {
                        "decision_context": "Candidate documented a completed analysis decision.",
                        "action": _short(text, limit=300),
                        "candidate_artifact_refs": refs,
                        "result_summary": "Candidate reported a completed evidence-producing action.",
                        "verification": "The action is corroborated by sealed candidate-owned files where referenced.",
                    }
                )
            else:
                # Small synthetic fixtures commonly place the useful response at
                # the outer JSON level rather than inside a Codex transcript.
                for text in _walk_strings(record):
                    normalized = _normalize(text)
                    if (
                        len(normalized.split()) >= 4
                        and set(normalized.split()).intersection(_SUCCESS_WORDS)
                    ):
                        items.append(
                            {
                                "decision_context": "Candidate documented a completed analysis decision.",
                                "action": _short(text, limit=300),
                                "candidate_artifact_refs": _refs_in_text(text, files),
                                "result_summary": "Candidate reported a completed evidence-producing action.",
                                "verification": "Candidate transcript records the completed action.",
                            }
                        )
                        break
    return items


def _classify_outputs(refs: Iterable[str]) -> list[str]:
    output_classes: set[str] = set()
    for ref in refs:
        path = Path(ref)
        if path.suffix.casefold() in _CODE_SUFFIXES:
            output_classes.add("script")
        elif path.suffix.casefold() in _IMAGE_SUFFIXES:
            output_classes.add("figure")
        elif path.suffix.casefold() in _NUMERIC_SUFFIXES:
            output_classes.add("numeric_output")
        elif ref == "report/report.md":
            output_classes.add("report_section")
    return sorted(output_classes)


def _file_events(root: Path, files: list[str]) -> list[dict[str, Any]]:
    public_inputs = [item for item in files if item.startswith(("data/", "related_work/", "inputs/"))][:6]
    reports = [item for item in files if item == "report/report.md"]
    outputs = [item for item in files if Path(item).suffix.casefold() in (_IMAGE_SUFFIXES | _NUMERIC_SUFFIXES)]
    events: list[dict[str, Any]] = []
    for script in [item for item in files if Path(item).suffix.casefold() in _CODE_SUFFIXES]:
        stem = Path(script).stem.replace("_", " ")
        linked = [item for item in outputs if any(token in Path(item).stem.casefold() for token in _TOKEN.findall(stem.casefold()))]
        refs = list(dict.fromkeys([script, *linked[:4], *reports]))
        events.append(
            {
                "decision_context": "Candidate created a concrete analysis route from the visible task inputs.",
                "action": f"Reconstruct and execute the candidate-created `{script}` analysis route.",
                "public_input_refs": public_inputs,
                "candidate_artifact_refs": refs,
                "result_summary": f"The route produced candidate-owned evidence associated with {stem}.",
                "verification": "The sealed workspace contains the script, produced outputs, and report evidence.",
            }
        )
    for output in outputs:
        if len(events) >= 28:
            break
        path = Path(output)
        kind = "figure" if path.suffix.casefold() in _IMAGE_SUFFIXES else "numeric output"
        events.append(
            {
                "decision_context": "Candidate produced an output used to support its submitted analysis.",
                "action": f"Rebuild the candidate-produced {kind} `{output}` from the reconstructed method.",
                "public_input_refs": public_inputs,
                "candidate_artifact_refs": [output, *reports],
                "result_summary": f"Candidate-owned {kind} evidence was available for report use.",
                "verification": "The sealed workspace contains the output and report artifact.",
            }
        )
    return events


def build_baseline_success_trace(*, candidate_root: str | Path) -> dict[str, Any]:
    """Extract 10--30 high-information successful steps when evidence permits."""

    root = Path(candidate_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise BaselineSuccessTraceError("SUCCESS_TRACE_CANDIDATE_ROOT_UNSAFE")
    files = _candidate_files(root)
    if "report/report.md" not in files:
        raise BaselineSuccessTraceError("SUCCESS_TRACE_REPORT_EVIDENCE_ABSENT")
    file_set = set(files)
    public_inputs = [item for item in files if item.startswith(("data/", "related_work/", "inputs/"))][:6]
    candidates = _transcript_items(root, file_set) + _file_events(root, files)
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        refs = [ref for ref in item.get("candidate_artifact_refs", []) if ref in file_set]
        public_refs = [ref for ref in item.get("public_input_refs", public_inputs) if ref in file_set]
        if not refs:
            # A successful transcript-only item is useful only when it can be
            # tied to a concrete candidate artifact, rather than its prose.
            continue
        key = _normalize(item["action"]) + "|" + "|".join(refs)
        if key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "trace_id": f"trace_{len(events) + 1:02d}",
                "decision_context": _short(str(item["decision_context"])),
                "action": _short(str(item["action"])),
                "public_input_refs": public_refs,
                "candidate_artifact_refs": refs,
                "result_summary": _short(str(item["result_summary"])),
                "verification": _short(str(item["verification"])),
                "status": "successful",
            }
        )
        if len(events) == 30:
            break
    if not events:
        raise BaselineSuccessTraceError("SUCCESS_TRACE_NO_SUCCESSFUL_EVIDENCE")
    return {
        "schema_version": SUCCESS_TRACE_SCHEMA,
        "source": "sealed_candidate_trajectory_and_workspace",
        "projector_model_calls": 0,
        "event_count": len(events),
        "events": events,
    }


def _signature(value: str) -> list[str]:
    return [
        token
        for token in dict.fromkeys(_TOKEN.findall(value.casefold()))
        if token not in _LOW_SIGNAL and not token.isdigit()
    ][:8]


def build_baseline_achievement_ledger(
    *,
    trace: Mapping[str, Any],
    candidate_root: str | Path,
) -> dict[str, Any]:
    """Derive required capabilities from execution/evidence links, not headings."""

    if trace.get("schema_version") != SUCCESS_TRACE_SCHEMA:
        raise BaselineSuccessTraceError("ACHIEVEMENT_LEDGER_TRACE_SCHEMA_INVALID")
    root = Path(candidate_root).resolve(strict=True)
    files = set(_candidate_files(root))
    raw_events = trace.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise BaselineSuccessTraceError("ACHIEVEMENT_LEDGER_TRACE_INVALID")
    achievements: list[dict[str, Any]] = []
    seen_methods: set[str] = set()
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        refs = [ref for ref in event.get("candidate_artifact_refs", []) if isinstance(ref, str) and ref in files]
        classes = _classify_outputs(refs)
        if not classes:
            continue
        method_refs = [ref for ref in refs if Path(ref).suffix.casefold() in _CODE_SUFFIXES]
        method_ref = method_refs[0] if method_refs else refs[0]
        method_key = method_ref.casefold()
        if method_key in seen_methods:
            continue
        seen_methods.add(method_key)
        action = str(event.get("action", ""))
        signature = _signature(f"{method_ref} {action} {' '.join(refs)}")
        if len(signature) < 2:
            continue
        achievement_id = f"achievement_{len(achievements) + 1:02d}"
        capability = (
            f"Use candidate-created method `{method_ref}` to produce the linked "
            f"{', '.join(classes)} evidence path."
        )
        reconstruction_steps = [
            "Start from the original public inputs in a fresh workspace.",
            f"Reconstruct and execute the capability represented by `{method_ref}`.",
            "Regenerate the required evidence outputs and connect them to the report discussion.",
            "Verify that the regenerated method and evidence chain are present before adding improvements.",
        ]
        achievements.append(
            {
                "achievement_id": achievement_id,
                "capability": capability,
                "scientific_role": (
                    "Candidate-produced analysis and evidence chain used in the submitted scientific report."
                ),
                "decision_rationale": str(event.get("decision_context", "")),
                "public_inputs": list(event.get("public_input_refs", [])),
                "trajectory_refs": [event.get("trace_id")],
                "reconstruction_steps": reconstruction_steps,
                "method_signature": signature,
                "required_output_classes": classes,
                "candidate_evidence_refs": list(dict.fromkeys([*refs, "report/report.md"])),
                "verification_assertions": [
                    str(event.get("verification", "")),
                    "Fresh output must include the reconstructed method, evidence, and report discussion.",
                ],
                "candidate_observations": [
                    {
                        "provenance": "candidate_observation",
                        "summary": str(event.get("result_summary", "")),
                    }
                ],
                "preservation_priority": "required",
            }
        )
        if len(achievements) == 8:
            break
    if not achievements:
        raise BaselineSuccessTraceError("ACHIEVEMENT_LEDGER_NO_CONCRETE_CAPABILITY")
    return {
        "schema_version": ACHIEVEMENT_LEDGER_SCHEMA,
        "source": "baseline_success_trace",
        "projector_model_calls": 0,
        "achievement_count": len(achievements),
        "required_achievement_count": len(achievements),
        "achievements": achievements,
    }


__all__ = [
    "ACHIEVEMENT_LEDGER_SCHEMA",
    "SUCCESS_TRACE_SCHEMA",
    "BaselineSuccessTraceError",
    "build_baseline_achievement_ledger",
    "build_baseline_success_trace",
]
