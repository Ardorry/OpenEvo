"""Minimal Candidate-grounded context for R4 per-item evolution.

This module does not interpret evaluator scores or generate feedback.  It keeps
the existing sanitized feedback authority intact and contributes only a small,
deterministic view of scientific work that the baseline Candidate actually
executed.  No command is replayed and no model is called.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .run_manifest import atomic_write_json
from .training_state_store import canonical_sha256

MINIMAL_TRACE_SCHEMA = "openevo.researchclawbench.minimal_baseline_trace.v1"
MINIMAL_CONTEXT_SCHEMA = "openevo.researchclawbench.minimal_semantic_evolution.r4"
MINIMAL_QUALITY_SCHEMA = "openevo.researchclawbench.minimal_semantic_artifact_quality.v1"
RETENTION_FEEDBACK_CLASS = "minimal_semantic_evolution_r4"
RETENTION_FEEDBACK_SCHEMA = MINIMAL_CONTEXT_SCHEMA

_ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
_CODE_SUFFIXES = frozenset({".py", ".r", ".jl", ".ipynb"})
_OUTPUT_SUFFIXES = frozenset({".csv", ".tsv", ".json", ".png", ".jpg", ".jpeg", ".svg", ".md"})
_PATH_LITERAL = re.compile(
    r"(?<![\w.-])([\w./-]+\.(?:py|ipynb|r|jl|csv|tsv|json|png|jpg|jpeg|svg|md|pdf))(?![\w.-])",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+\.\d+|\d+)(?![A-Za-z_])")
_IDENTIFIER = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_PRIVATE_MARKERS = (
    "ground_truth_entries",
    "criterion.content",
    "criterion.keywords",
    "criterion.path",
    "criterion.weight",
    "target image",
    "judge reasoning",
    "judge raw response",
)
_METHOD_NOISE = frozenset(
    {
        "main",
        "write",
        "write_csv",
        "write_json",
        "save",
        "mkdir",
        "sorted",
        "list",
        "dict",
        "set",
        "min",
        "max",
        "sum",
        "len",
        "print",
        "open",
    }
)
_TERM_NOISE = frozenset(
    {
        "candidate",
        "baseline",
        "analysis",
        "artifact",
        "data",
        "evidence",
        "figure",
        "output",
        "report",
        "result",
        "task",
        "the",
        "this",
        "with",
    }
)


class MinimalSemanticEvolutionError(RuntimeError):
    """The sealed Candidate evidence cannot support a safe minimal trace."""


def _compact(value: str, *, limit: int = 360) -> str:
    return " ".join(value.split())[:limit].rstrip()


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _candidate_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= 4096:
            raise MinimalSemanticEvolutionError("MINIMAL_TRACE_INVENTORY_TOO_LARGE")
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
    return files


def _public_inputs(files: Iterable[str]) -> list[str]:
    return [ref for ref in files if ref.startswith(("data/", "related_work/", "inputs/"))][:16]


def _candidate_outputs(files: Iterable[str]) -> list[str]:
    return [
        ref
        for ref in files
        if (
            ref == "report/report.md"
            or ref.startswith(("outputs/", "report/images/"))
            and Path(ref).suffix.casefold() in _OUTPUT_SUFFIXES
        )
    ][:64]


def _called_function_names(node: ast.AST, known: set[str]) -> list[str]:
    calls: list[tuple[int, int, str]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.id if isinstance(func, ast.Name) else None
        if name in known:
            calls.append((child.lineno, child.col_offset, name))
    return list(dict.fromkeys(name for _line, _column, name in sorted(calls)))


def _function_source(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


def _literal_refs(source: str, known_files: set[str]) -> list[str]:
    by_name = {Path(item).name: item for item in known_files}
    refs: list[str] = []
    for match in _PATH_LITERAL.finditer(source):
        observed = match.group(1).replace("\\", "/").lstrip("./")
        if observed in known_files:
            refs.append(observed)
        elif Path(observed).name in by_name:
            refs.append(by_name[Path(observed).name])
    return list(dict.fromkeys(refs))


def _parameter_expressions(source: str, nodes: Iterable[ast.AST]) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            candidate: ast.AST | None = None
            if (
                isinstance(child, ast.BoolOp)
                and any(isinstance(part, ast.Compare) for part in child.values)
            ) or isinstance(child, ast.Compare):
                candidate = child
            elif isinstance(child, ast.arguments):
                positional = [*child.posonlyargs, *child.args]
                defaults = [None] * (len(positional) - len(child.defaults)) + list(child.defaults)
                for argument, default in zip(positional, defaults, strict=True):
                    if default is None:
                        continue
                    rendered = _function_source(source, default)
                    if _NUMBER.search(rendered):
                        value = f"{argument.arg}={rendered}"
                        if value not in seen:
                            seen.add(value)
                            ranked.append((1, getattr(default, "lineno", 0), value))
                continue
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                rendered = _function_source(source, child)
                if re.search(
                    r"(?i)\b(?:threshold|cutoff|margin|epoch|alpha|beta|bins?|lr|l2|width)\b",
                    rendered,
                ) and _NUMBER.search(rendered):
                    candidate = child
            if candidate is None:
                continue
            rendered = _compact(_function_source(source, candidate), limit=220)
            if not rendered or not _NUMBER.search(rendered) or rendered in seen:
                continue
            seen.add(rendered)
            signal = len(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rendered)))
            ranked.append((-signal, getattr(candidate, "lineno", 0), rendered))
    return [value for _signal, _line, value in sorted(ranked)[:16]]


def _report_sections(report: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    title = "submitted report"
    body: list[str] = []
    for line in report.splitlines():
        if line.startswith("#"):
            if body:
                sections.append((title, body))
            title = line.lstrip("# ").strip() or title
            body = []
        else:
            body.append(line)
    if body:
        sections.append((title, body))
    return [(heading, "\n".join(lines)) for heading, lines in sections]


def _report_role(report: str, outputs: list[str], methods: list[str]) -> str:
    names = [Path(ref).name.casefold() for ref in outputs]
    method_tokens = {
        token for method in methods for token in method.casefold().split("_") if len(token) >= 5
    }
    matches: list[str] = []
    for heading, body in _report_sections(report):
        corpus = (heading + "\n" + body).casefold()
        if (
            any(name in corpus for name in names)
            or len(method_tokens.intersection(re.findall(r"[a-z0-9]+", corpus))) >= 2
        ):
            matches.append(heading)
    return (
        "; ".join(dict.fromkeys(matches))[:240] or "Executable evidence used by report/report.md"
    )


def _transcript_executed_scripts(root: Path, scripts: list[str]) -> set[str]:
    transcript = root / "_agent_output.jsonl"
    if not transcript.is_file() or transcript.is_symlink():
        return set()
    executed: set[str] = set()
    try:
        outer_records = [
            json.loads(line)
            for line in transcript.read_text(encoding="utf-8", errors="replace")[
                :1_500_000
            ].splitlines()[:256]
        ]
    except (OSError, json.JSONDecodeError):
        return set()
    for outer in outer_records:
        nested = outer.get("metadata", {}).get("transcript") if isinstance(outer, dict) else None
        if not isinstance(nested, str):
            continue
        for line in nested.splitlines()[:2048]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = record.get("item") if isinstance(record.get("item"), dict) else record
            if (
                item.get("type") != "command_execution"
                or item.get("status") != "completed"
                or item.get("exit_code") not in {0, "0"}
                or not isinstance(item.get("command"), str)
            ):
                continue
            for script in scripts:
                if script in item["command"] or Path(script).name in item["command"]:
                    executed.add(script)
    return executed


def _route_groups(functions: Mapping[str, ast.AST]) -> list[tuple[str, list[str]]]:
    names = list(functions)

    def selected(pattern: str) -> list[str]:
        return [name for name in names if re.search(pattern, name, re.IGNORECASE)]

    input_methods = selected(r"read|load|parse|extract|pdf|catalog|input")
    validation_methods = selected(
        r"valid|confusion|auc|roc|precision|recall|metric|test|margin|threshold|predict|model|logistic|correl"
    )
    output_methods = selected(r"plot|chart|heatmap|figure|render|png|write|save")
    claimed = set(input_methods) | set(validation_methods) | set(output_methods)
    analysis_methods = [
        name
        for name in names
        if name not in claimed
        and name not in _METHOD_NOISE
        and not re.search(r"canvas|pixel|rect|line|text|color|font|chunk|blank", name)
    ]
    groups = [
        ("Input and extraction path", input_methods),
        ("Scientific analysis and decision path", analysis_methods),
        ("Validation and comparison path", validation_methods),
        ("Evidence production path", output_methods),
    ]
    return [(title, methods) for title, methods in groups if methods]


def build_minimal_baseline_trace(*, candidate_root: str | Path) -> dict[str, Any]:
    """Build 3--8 dense paths from sealed Candidate code, transcript, and report."""

    root = Path(candidate_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise MinimalSemanticEvolutionError("MINIMAL_TRACE_CANDIDATE_ROOT_UNSAFE")
    files = _candidate_files(root)
    if "report/report.md" not in files:
        raise MinimalSemanticEvolutionError("MINIMAL_TRACE_REPORT_ABSENT")
    scripts = [ref for ref in files if Path(ref).suffix.casefold() in _CODE_SUFFIXES]
    if not scripts:
        raise MinimalSemanticEvolutionError("MINIMAL_TRACE_CODE_ABSENT")
    report = (root / "report/report.md").read_text(encoding="utf-8", errors="replace")[:262_144]
    public_inputs = _public_inputs(files)
    all_outputs = _candidate_outputs(files)
    executed = _transcript_executed_scripts(root, scripts)
    selected_scripts = [script for script in scripts if script in executed] or scripts
    paths: list[dict[str, Any]] = []
    known_files = set(files)

    for script in selected_scripts[:3]:
        source = (root / script).read_text(encoding="utf-8", errors="replace")[:1_000_000]
        try:
            tree = ast.parse(source, filename=script)
        except SyntaxError as exc:
            raise MinimalSemanticEvolutionError("MINIMAL_TRACE_CODE_UNPARSEABLE") from exc
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if not functions:
            continue
        known = set(functions)
        main = functions.get("main")
        main_calls = _called_function_names(main, known) if main is not None else []
        groups = _route_groups(functions)
        for title, methods in groups:
            ordered = [name for name in main_calls if name in methods]
            ordered.extend(name for name in methods if name not in ordered)
            ordered = list(dict.fromkeys(ordered))[:12]
            nodes = [functions[name] for name in ordered]
            source_block = "\n".join(_function_source(source, node) for node in nodes)
            refs = _literal_refs(source_block, known_files)
            outputs = [ref for ref in refs if ref in all_outputs]
            if title == "Evidence production path":
                outputs = list(dict.fromkeys([*outputs, *all_outputs]))[:16]
            elif not outputs:
                output_names = {Path(ref).name.casefold(): ref for ref in all_outputs}
                for name, ref in output_names.items():
                    if name in report.casefold() and any(
                        token in name
                        for method in ordered
                        for token in method.casefold().split("_")
                    ):
                        outputs.append(ref)
            inputs = [ref for ref in refs if ref in public_inputs] or public_inputs
            parameters = (
                []
                if title == "Evidence production path"
                else _parameter_expressions(source, nodes)
            )
            steps = [
                f"Call `{name}` from `{script}`." for name in ordered if name not in _METHOD_NOISE
            ][:12]
            if not steps:
                continue
            transcript_status = (
                "The baseline transcript records successful execution of this script."
                if script in executed
                else (
                    "The sealed baseline contains the executable script and its "
                    "report-linked outputs."
                )
            )
            paths.append(
                {
                    "summary": f"{title}: execute `{script}` with " + ", ".join(ordered[:8]),
                    "why": transcript_status,
                    "inputs": list(dict.fromkeys(inputs))[:8],
                    "steps": steps,
                    "parameters": parameters,
                    "outputs": list(dict.fromkeys(outputs))[:16],
                    "report_role": _report_role(report, outputs, ordered),
                }
            )
            if len(paths) == 8:
                break
        if len(paths) == 8:
            break
    if not paths:
        raise MinimalSemanticEvolutionError("MINIMAL_TRACE_SUCCESSFUL_PATHS_ABSENT")
    body = {
        "schema_version": MINIMAL_TRACE_SCHEMA,
        "successful_paths": paths,
        "projector_model_calls": 0,
    }
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 65_536:
        raise MinimalSemanticEvolutionError("MINIMAL_TRACE_TOO_LARGE")
    return body


def admit_minimal_baseline_trace(
    trace: Mapping[str, Any], *, candidate_root: str | Path
) -> dict[str, Any]:
    root = Path(candidate_root).resolve(strict=True)
    files = set(_candidate_files(root))
    paths = trace.get("successful_paths")
    if (
        set(trace) != {"schema_version", "successful_paths", "projector_model_calls"}
        or trace.get("schema_version") != MINIMAL_TRACE_SCHEMA
        or trace.get("projector_model_calls") != 0
        or not isinstance(paths, list)
        or not paths
        or len(paths) > 8
    ):
        raise MinimalSemanticEvolutionError("MINIMAL_TRACE_SCHEMA_INVALID")
    expected = {"summary", "why", "inputs", "steps", "parameters", "outputs", "report_role"}
    for path in paths:
        if (
            not isinstance(path, dict)
            or set(path) != expected
            or not all(
                isinstance(path.get(key), str) and path[key].strip()
                for key in ("summary", "why", "report_role")
            )
            or not all(
                isinstance(path.get(key), list)
                for key in ("inputs", "steps", "parameters", "outputs")
            )
            or not path["steps"]
            or any(
                not isinstance(value, str) or not value.strip()
                for key in ("inputs", "steps", "parameters", "outputs")
                for value in path[key]
            )
            or any(ref not in files for ref in [*path["inputs"], *path["outputs"]])
        ):
            raise MinimalSemanticEvolutionError("MINIMAL_TRACE_SCHEMA_INVALID")
    serialized = _normalize(json.dumps(trace, ensure_ascii=False))
    if any(_normalize(marker) in serialized for marker in _PRIVATE_MARKERS):
        raise MinimalSemanticEvolutionError("MINIMAL_TRACE_PRIVATE_MARKER")
    body = {
        "schema_version": "openevo.researchclawbench.minimal_baseline_trace_admission.v1",
        "status": "ADMITTED",
        "trace_sha256": canonical_sha256(dict(trace)),
        "successful_path_count": len(paths),
        "candidate_grounded": True,
        "raw_gt_projected": False,
        "judge_reasoning_projected": False,
        "target_image_projected": False,
        "provider_calls": 0,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def build_minimal_evolution_context(
    *, trace: Mapping[str, Any], sanitized_feedback: Mapping[str, Any]
) -> dict[str, Any]:
    paths = trace.get("successful_paths")
    diagnoses = sanitized_feedback.get("diagnoses")
    if (
        not isinstance(paths, list)
        or not paths
        or not isinstance(diagnoses, list)
        or not diagnoses
    ):
        raise MinimalSemanticEvolutionError("MINIMAL_EVOLUTION_CONTEXT_INCOMPLETE")
    return {
        "schema_version": MINIMAL_CONTEXT_SCHEMA,
        "what_already_worked": dict(trace),
        "what_needs_improvement": {
            "feedback_class": sanitized_feedback.get("feedback_class"),
            "preserve_strengths": sanitized_feedback.get("preserve_strengths", []),
            "diagnoses": diagnoses,
        },
        "instruction": (
            "The baseline trace records Candidate-produced work that succeeded operationally. "
            "Preserve these scientific paths by default. Evaluator feedback identifies additive "
            "improvements: first reconstruct the baseline strategy, then improve it. Do not "
            "replace a successful path merely because another weakness is highlighted. A method "
            "or parameter may change only when public or Candidate-produced evidence justifies "
            "the change; record the prior value, new value, evidence, and reason."
        ),
        "artifact_roles": {
            "text_memory": "Remember the scientific methods and experience that already worked.",
            "skill_bundle": (
                "Explain how to reproduce those methods and improve what feedback identified."
            ),
            "agent_system": "Require improvements to retain effective baseline analysis.",
        },
    }


def _terms(value: str) -> set[str]:
    return {
        token
        for token in _normalize(value).split()
        if len(token) >= 3 and token not in _TERM_NOISE and not token.isdigit()
    }


def _canonical_number(value: str) -> str:
    try:
        rendered = f"{float(value):.12g}"
    except ValueError:
        return value
    return "0" if rendered in {"-0", "+0"} else rendered


def _important_parameter_values(parameters: Iterable[str]) -> list[str]:
    """Select values that define a Candidate method, not parser/loop mechanics."""

    values: list[str] = []
    for parameter in parameters:
        rendered = str(parameter)
        numbers = [_canonical_number(value) for value in _NUMBER.findall(rendered)]
        if re.search(r"\[['\"][^'\"]+['\"]\]", rendered):
            values.extend(value for value in numbers if value not in {"0", "1", "-1"})
            continue
        if re.fullmatch(
            r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*[-+]?(?:\d+\.\d+|\d+)\s*",
            rendered,
        ):
            values.extend(value for value in numbers if value not in {"0", "1", "-1"})
    return list(dict.fromkeys(values))[:4]


def _path_fidelity(path: Mapping[str, Any], artifact_text: str) -> dict[str, Any]:
    normalized = _normalize(artifact_text)
    methods = [
        name
        for step in path.get("steps", [])
        for name in _IDENTIFIER.findall(str(step))
        if name not in _METHOD_NOISE
    ]
    methods = list(dict.fromkeys(methods))[:8]
    matched_methods = [
        method for method in methods if _normalize(method.replace("_", " ")) in normalized
    ]
    method_terms = {
        token
        for method in methods
        for token in _terms(method.replace("_", " "))
        if token not in {"make", "write", "read"}
    }
    matched_method_terms = method_terms.intersection(_terms(artifact_text))
    parameter_values = _important_parameter_values(path.get("parameters", []))
    observed_numbers = {_canonical_number(value) for value in _NUMBER.findall(artifact_text)}
    matched_parameters = [value for value in parameter_values if value in observed_numbers]
    output_names = [Path(ref).name for ref in path.get("outputs", [])]
    matched_outputs = [
        name for name in output_names if name.casefold() in artifact_text.casefold()
    ]
    summary_overlap = len(_terms(str(path.get("summary", ""))).intersection(_terms(artifact_text)))
    method_required = min(2, len(methods))
    method_present = (
        len(matched_methods) >= method_required if method_required else summary_overlap >= 2
    )
    method_present = method_present or (
        bool(method_terms) and len(matched_method_terms) >= min(2, len(method_terms))
    )
    parameter_present = len(matched_parameters) == len(parameter_values)
    output_present = bool(matched_outputs) or not output_names
    passed = method_present and parameter_present and (output_present or summary_overlap >= 3)
    return {
        "summary_sha256": canonical_sha256(str(path.get("summary", ""))),
        "method_anchor_count": len(methods),
        "matched_method_anchor_count": len(matched_methods),
        "matched_method_term_count": len(matched_method_terms),
        "parameter_anchor_count": len(parameter_values),
        "matched_parameter_anchor_count": len(matched_parameters),
        "output_anchor_count": len(output_names),
        "matched_output_anchor_count": len(matched_outputs),
        "status": "PASS" if passed else "FAIL",
    }


def assess_minimal_semantic_artifact_quality(
    *,
    trace: Mapping[str, Any],
    sanitized_feedback: Mapping[str, Any],
    artifact_texts: Mapping[str, str],
    content_admission_findings: list[str],
    provenance_violations: list[str],
) -> dict[str, Any]:
    if (
        trace.get("schema_version") != MINIMAL_TRACE_SCHEMA
        or set(artifact_texts) != set(_ARTIFACT_TYPES)
        or any(
            not isinstance(value, str) or not value.strip() for value in artifact_texts.values()
        )
        or any(not isinstance(item, str) or not item for item in content_admission_findings)
        or any(not isinstance(item, str) or not item for item in provenance_violations)
    ):
        raise MinimalSemanticEvolutionError("MINIMAL_ARTIFACT_QUALITY_INPUT_INVALID")
    paths = trace.get("successful_paths")
    diagnoses = sanitized_feedback.get("diagnoses")
    if (
        not isinstance(paths, list)
        or not paths
        or not isinstance(diagnoses, list)
        or not diagnoses
    ):
        raise MinimalSemanticEvolutionError("MINIMAL_ARTIFACT_QUALITY_INPUT_INVALID")
    preservation_text = artifact_texts["text_memory"] + "\n" + artifact_texts["skill_bundle"]
    path_findings = [_path_fidelity(path, preservation_text) for path in paths]
    combined = "\n".join(artifact_texts.values())
    feedback_findings: list[dict[str, Any]] = []
    for diagnosis in diagnoses:
        if not isinstance(diagnosis, dict):
            continue
        terms = _terms(
            str(diagnosis.get("dimension", ""))
            + " "
            + str(diagnosis.get("improvement_direction", ""))
        )
        overlap = len(terms.intersection(_terms(combined)))
        action = any(
            marker in _normalize(combined)
            for marker in ("add", "improve", "extend", "cross check", "compare", "validate")
        )
        feedback_findings.append(
            {
                "dimension": diagnosis.get("dimension"),
                "matched_term_count": overlap,
                "explicit_improvement_action": action,
                "status": "PASS" if action and overlap >= min(2, len(terms)) else "FAIL",
            }
        )
    leakage = sorted(set(content_admission_findings))
    paths_present = all(item["status"] == "PASS" for item in path_findings)
    feedback_addressed = any(item["status"] == "PASS" for item in feedback_findings)
    passed = paths_present and feedback_addressed and not leakage and not provenance_violations
    body = {
        "schema_version": MINIMAL_QUALITY_SCHEMA,
        "status": "PASS" if passed else "MINIMAL_SEMANTIC_ARTIFACT_QUALITY_FAILED",
        "checks": {
            "baseline_scientific_paths_present": paths_present,
            "sanitized_feedback_addressed": feedback_addressed,
            "gt_and_judge_leakage_absent": not leakage,
        },
        "path_findings": path_findings,
        "feedback_findings": feedback_findings,
        "gt_leakage_findings": leakage,
        "provenance_violations": sorted(set(provenance_violations)),
        "artifact_text_sha256": {
            kind: canonical_sha256(text) for kind, text in artifact_texts.items()
        },
        "provider_calls": 0,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


class MinimalSemanticFeedbackProjectionPort:
    """Compose R4 around the frozen sanitized-feedback projector."""

    def __init__(self, *, root: str | Path, frozen_projector: Any) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.frozen_projector = frozen_projector

    def _path(self, idempotency_key: str) -> Path:
        return self.root / f"{hashlib.sha256(idempotency_key.encode()).hexdigest()}.json"

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        if not path.is_file() or path.is_symlink():
            return None
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("request_sha256") != canonical_sha256(request):
            raise MinimalSemanticEvolutionError("R4_PROJECTION_REQUEST_DRIFT")
        result = stored.get("result")
        if not isinstance(result, dict):
            raise MinimalSemanticEvolutionError("R4_PROJECTION_RECEIPT_INVALID")
        return result

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        prior = self.recover(request, idempotency_key)
        if prior is not None:
            return prior
        frozen = self.frozen_projector.execute(request, idempotency_key)
        candidate = request.get("candidate")
        root_value = (
            candidate.get("candidate_output_root") if isinstance(candidate, dict) else None
        )
        sanitized = frozen.get("reflector_sanitized_feedback")
        if not isinstance(root_value, str) or not isinstance(sanitized, dict):
            raise MinimalSemanticEvolutionError("R4_PROJECTION_AUTHORITY_INCOMPLETE")
        trace = build_minimal_baseline_trace(candidate_root=root_value)
        admission = admit_minimal_baseline_trace(trace, candidate_root=root_value)
        context = build_minimal_evolution_context(trace=trace, sanitized_feedback=sanitized)
        reflector_feedback = {
            "a00_minimal_semantic_context": context,
            "schema_version": RETENTION_FEEDBACK_SCHEMA,
            "status": "available_for_evolution",
            "feedback_class": RETENTION_FEEDBACK_CLASS,
            "task_id": request.get("task_id"),
            "policy": {
                "candidate_grounded": True,
                "public_or_candidate_provenance_required": True,
                "answer_reconstruction_allowed": False,
            },
        }
        result_body = {
            **frozen,
            "schema_version": "openevo.researchclawbench.feedback_projection_receipt.r4",
            "feedback_projection_id": (
                "feedback-projection-r4-" + canonical_sha256(reflector_feedback)[:24]
            ),
            "minimal_baseline_trace": trace,
            "minimal_baseline_trace_sha256": canonical_sha256(trace),
            "minimal_baseline_trace_admission": admission,
            "reflector_feedback": reflector_feedback,
            "reflector_feedback_sha256": canonical_sha256(reflector_feedback),
            "r3_diagnostic_reflector_feedback_sha256": frozen.get("reflector_feedback_sha256"),
            "quality_contract": {
                "baseline_scientific_paths_required": True,
                "sanitized_feedback_improvement_required": True,
                "gt_leakage": False,
                "all_sanitized_diagnoses_visible": True,
                "minimal_baseline_trace_present": True,
                "legacy_achievement_metrics_diagnostic_only": True,
                "baseline_equivalence_diagnostic_only": True,
            },
        }
        result_body.pop("content_sha256", None)
        result = {
            **result_body,
            "content_sha256": canonical_sha256(result_body),
        }
        atomic_write_json(
            self._path(idempotency_key),
            {"request_sha256": canonical_sha256(request), "result": result},
        )
        return result


__all__ = [
    "MINIMAL_CONTEXT_SCHEMA",
    "MINIMAL_QUALITY_SCHEMA",
    "MINIMAL_TRACE_SCHEMA",
    "RETENTION_FEEDBACK_CLASS",
    "RETENTION_FEEDBACK_SCHEMA",
    "MinimalSemanticEvolutionError",
    "MinimalSemanticFeedbackProjectionPort",
    "admit_minimal_baseline_trace",
    "assess_minimal_semantic_artifact_quality",
    "build_minimal_baseline_trace",
    "build_minimal_evolution_context",
]
