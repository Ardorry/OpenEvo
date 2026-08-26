"""Prepare, run, and report the sealed ChemCrow historical judge calibration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import yaml

from .hashing import canonical_sha256, file_sha256
from .paper_calibration import (
    ARXIV_SOURCE_SHA256,
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    CHEMCROW_RUNS_COMMIT,
    CHEMCROW_RUNS_REPOSITORY,
    CHEMCROW_RUNS_V1_COMMIT,
    CHEMCROW_RUNS_ZENODO_ARCHIVE_SHA256,
    CHEMCROW_RUNS_ZENODO_DOI,
    NATURE_SOURCE_DATA_SHA256,
    NATURE_SUPPLEMENT_SHA256,
    PROMPT_CANDIDATE_IDS,
    analyze_calibration_results,
    assert_calibration_ledger_separation,
    build_calibration_plan,
    build_prompt_candidate_manifest,
    build_repeatability_plan,
    calibration_cost_ceiling,
    extract_historical_calibration_dataset,
    run_calibration_plan,
    selected_prompt_from_results,
)


def _load_config(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "chemcrow_paper_calibration_config_v1"
    ):
        raise ValueError("paper calibration config authority is invalid")
    return payload


def _path(config_path: Path, value: str) -> Path:
    return (config_path.parent / value).resolve()


def _paths(config_path: Path, config: dict[str, Any]) -> dict[str, Path]:
    root = _path(config_path, str(config["calibration_root"]))
    reports = _path(config_path, str(config["reports_root"]))
    return {
        "runs": _path(config_path, str(config["historical_runs_root"])),
        "root": root,
        "production": _path(config_path, str(config["production_root"])),
        "dataset": _path(config_path, str(config["dataset_path"])),
        "reports": reports,
        "candidates": reports / "PAPER_EVALUATOR_PROMPT_CANDIDATES.json",
        "initial_plan": root / "private" / "initial-plan.json",
        "initial_results": root / "results" / "initial",
        "initial_receipts": root / "openrouter-receipts" / "initial",
        "repeat_plan": root / "private" / "repeatability-plan.json",
        "repeat_results": root / "results" / "repeatability",
        "repeat_receipts": root / "openrouter-receipts" / "repeatability",
        "analysis": reports / "PAPER_EVALUATOR_CALIBRATION_RESULTS.json",
        "selected": reports / "PAPER_EVALUATOR_SELECTED_PROMPT.json",
    }


def _write_json(path: Path, payload: dict[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError(f"existing frozen artifact differs: {path}")
        return
    if private:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    else:
        path.write_text(content, encoding="utf-8")


def _write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.rstrip() + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != normalized:
        path.write_text(normalized, encoding="utf-8")


def _archaeology_report(dataset: dict[str, Any]) -> str:
    return f"""# ChemCrow EvaluatorGPT Prompt Archaeology

## Verdict

`EXACT_PROMPT_RECOVERED=false`

No author-controlled artifact inspected contains the implementation of `Evaluator`, its
complete prompt, or a byte-equivalent prompt template. The current calibration therefore
uses the compatible-prompt branch and does not claim an official or verbatim reproduction.

## Authoritative sources searched

- [official chemcrow-runs repository]({CHEMCROW_RUNS_REPOSITORY}), complete reachable Git
  history, `main` `{CHEMCROW_RUNS_COMMIT}`, tag `v1` `{CHEMCROW_RUNS_V1_COMMIT}`. Searches
  covered `class Evaluator`, `def run`, `chemcrow.evaluation`, teacher calls, and every
  released evaluator-output heading. Notebooks contain imports, calls, and outputs, but not
  the implementation.
- [official chemcrow-public repository](https://github.com/ur-whitelab/chemcrow-public),
  complete reachable Git history, tags, and releases. No Evaluator implementation found.
- [official Zenodo release](https://doi.org/{CHEMCROW_RUNS_ZENODO_DOI}); downloaded archive
  SHA256 `{CHEMCROW_RUNS_ZENODO_ARCHIVE_SHA256}`. It reproduces the v1 run repository and
  does not add the implementation.
- [official PyPI project](https://pypi.org/project/chemcrow/): all 27 published source
  distributions from 0.1.0 through 0.3.24 were downloaded and searched. None contains the
  historical Evaluator implementation.
- [Nature Machine Intelligence article](https://www.nature.com/articles/s42256-024-00832-8),
  Supplementary Information SHA256 `{NATURE_SUPPLEMENT_SHA256}`, and Source Data SHA256
  `{NATURE_SOURCE_DATA_SHA256}`. They specify evaluation semantics and released scores, not
  the exact prompt.
- [official arXiv source](https://arxiv.org/abs/2304.05376), archive SHA256
  `{ARXIV_SOURCE_SHA256}`. It repeats the published semantics and contains no prompt.

## Recovered authoritative semantics

The paper describes a teacher comparing two student answers to the same chemistry task,
grading whether the task was addressed and whether the overall thought process was correct,
then reporting grades, strengths, weaknesses, and improvement feedback. The experiment used
GPT-4 at temperature 0.1. These statements constrain the candidates but do not determine a
unique verbatim prompt.

## Environment evidence and limits

The notebooks import `Evaluator` from `chemcrow.agents`, but notebook metadata does not pin a
package version or source revision. Most kernels report Python 3.8.16; tasks 08 and 15 report
3.11.3. No lockfile or wheel hash binds the missing implementation. Inferring prompt wording
from released prose or output shape would therefore not be exact recovery.

## Non-authoritative clues

Public code search was used only to locate potential sources. No third-party prompt was
accepted as authority and no non-authoritative wording was copied into the frozen candidates.

Dataset binding: `{canonical_sha256(dataset)}`.
"""


def _extraction_report(dataset: dict[str, Any]) -> str:
    rows = []
    for item in dataset["tasks"]:
        rows.append(
            "| {task} | {label} | {a} | {b} | {discrepancy} | `{notebook}` |".format(
                task=item["repo_task_id"],
                label=str(item["historical_evaluator_label_available"]).lower(),
                a=item["historical_evaluator"]["student_a"]["grade"],
                b=item["historical_evaluator"]["student_b"]["grade"],
                discrepancy=str(item["supplement_discrepancy"]).lower(),
                notebook=item["source_notebook"],
            )
        )
    return """# Paper Evaluator Historical Extraction

The dataset was extracted by tracing the last executed assignment visible before each
`teacher.run(task, result_tools, result_notools)` call. Notebook outputs are the primary
runtime-reproduction labels; official Source Data Figure 4c is an independent cross-check.

- task count: {task_count}
- labeled task count: {labeled}
- missing tasks: {missing}
- source commit: `{commit}`
- source tag/commit: `v1` / `{tag_commit}`
- dataset canonical SHA256: `{digest}`
- notebook versus Supplement discrepancies: {discrepancies}

Task 12 is explicitly resolved from the later literal overwrite of `result_notools`
(execution count 6). Task 13 passes `prompt`, rather than `task`, to the evaluator. Historical
outputs provide grade, strengths, weaknesses, and grade justification; a separate improvement
feedback field is not recoverable and is not fabricated.

| task | label | notebook A | notebook B | discrepancy | source |
|---|---:|---:|---:|---:|---|
{rows}

Extraction was run twice in clean Python processes; both canonical hashes were `{digest}`.
""".format(
        task_count=dataset["task_count"],
        labeled=dataset["labeled_task_count"],
        missing=dataset["missing_label_task_ids"],
        commit=dataset["source_commit"],
        tag_commit=dataset["source_tag_commit"],
        digest=canonical_sha256(dataset),
        discrepancies=sum(item["supplement_discrepancy"] for item in dataset["tasks"]),
        rows="\n".join(rows),
    )


def _calibration_report(results: dict[str, Any], candidates_path: Path) -> str:
    result_rows = []
    for candidate_id in PROMPT_CANDIDATE_IDS:
        metric = results["metrics_by_candidate"][candidate_id]
        result_rows.append(
            "| {candidate} | {mae:.3f} | {rmse:.3f} | {bias:.3f} | {pearson} | "
            "{spearman} | {pair:.3f} | {delta_mae:.3f} | {delta_rmse:.3f} | "
            "{delta_spearman} | {parse} | {schema} |".format(
                candidate=candidate_id,
                mae=metric["score_mae"],
                rmse=metric["score_rmse"],
                bias=metric["score_bias"],
                pearson=_format_optional(metric["score_pearson"]),
                spearman=_format_optional(metric["score_spearman"]),
                pair=metric["pairwise_preference_agreement"],
                delta_mae=metric["delta_mae"],
                delta_rmse=metric["delta_rmse"],
                delta_spearman=_format_optional(metric["delta_spearman"]),
                parse=metric["json_parse_failures"],
                schema=metric["pydantic_schema_failures"],
            )
        )
    selected = results["selected_candidate_id"]
    selected_metrics = results["metrics_by_candidate"][selected]
    selected_bootstrap = results["bootstrap_by_candidate"][selected]
    return """# ChemCrow EvaluatorGPT-Compatible Historical Calibration

## Prompt recovery

- `EXACT_PROMPT_RECOVERED=false`
- authoritative search result: no verbatim implementation recovered
- prompt uncertainty remains; see `PAPER_EVALUATOR_PROMPT_ARCHAEOLOGY.md`

## Historical dataset

- task count: {task_count}
- labeled task count: {labeled}
- missing tasks: none
- source commit: `{commit}`
- dataset SHA256: `{dataset_hash}`

## Candidate prompts

Exactly three candidates and their hashes were frozen before paid results existed. Full text
and paper-grounding evidence are in [{candidates}]({candidates}).

## Results

| candidate | MAE | RMSE | bias | Pearson | Spearman | pair agreement | delta MAE | delta RMSE | delta Spearman | parse failures | schema failures |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{result_rows}

Proven OpenRouter-reported cost: `${cost:.6f}` across {calls} paid calls.

## Selection

Selected `{selected}` by the preregistered lexicographic rule: pairwise preference agreement,
delta MAE, absolute-score MAE, Spearman, parse/schema/provider reliability, then simplicity.
The complete ranking and numeric selection keys are preserved in
`PAPER_EVALUATOR_CALIBRATION_RESULTS.json`.

- LOO folds won: `{loo_wins}`
- LOO held-out pairwise agreement: {loo_pair:.3f}
- LOO held-out score error: {loo_score:.3f}
- LOO held-out delta error: {loo_delta:.3f}
- bootstrap seed/samples: {seed} / {samples}
- selected-prompt 95% CIs: `{bootstrap}`

## Drift interpretation

- Prompt uncertainty: nonzero because the authoritative exact prompt was not recovered.
- Model/API drift: inseparable from prompt uncertainty in the compatible-prompt branch.
- Judge stochasticity: `{repeatability}`. Repeat calls are reported separately and are not
  averaged into primary calibration metrics.

## Final label

`{final_label}`

Project diagnostic agreement category: `{agreement}`. Even HIGH agreement would not prove
exact prompt recovery. Historical controls must report original and modern judge values before
cross-era absolute-score interpretation.

Selected score MAE {mae:.3f}; pairwise agreement {pair:.3f}; delta MAE {delta_mae:.3f}.
""".format(
        task_count=results["historical_task_count"],
        labeled=results["historical_labeled_task_count"],
        commit=CHEMCROW_RUNS_COMMIT,
        dataset_hash=results["dataset_sha256"],
        candidates=candidates_path.name,
        result_rows="\n".join(result_rows),
        cost=results["proven_openrouter_reported_cost_usd"],
        calls=results["paid_calls"],
        selected=selected,
        loo_wins=results["leave_one_task_out"]["folds_won_by_candidate"],
        loo_pair=results["leave_one_task_out"]["held_out_pairwise_agreement"],
        loo_score=results["leave_one_task_out"]["held_out_score_error"],
        loo_delta=results["leave_one_task_out"]["held_out_delta_error"],
        seed=BOOTSTRAP_SEED,
        samples=BOOTSTRAP_SAMPLES,
        bootstrap=selected_bootstrap["confidence_intervals_95"],
        repeatability=results["repeatability"],
        final_label=results["final_label"],
        agreement=results["historical_agreement"],
        mae=selected_metrics["score_mae"],
        pair=selected_metrics["pairwise_preference_agreement"],
        delta_mae=selected_metrics["delta_mae"],
    )


def _format_optional(value: float | None) -> str:
    return "NA" if value is None else f"{value:.3f}"


def prepare(config_path: Path) -> dict[str, Any]:
    config = _load_config(config_path)
    paths = _paths(config_path, config)
    assert_calibration_ledger_separation(paths["root"], paths["production"])
    dataset = extract_historical_calibration_dataset(runs_root=paths["runs"])
    second = extract_historical_calibration_dataset(runs_root=paths["runs"])
    if canonical_sha256(dataset) != canonical_sha256(second):
        raise RuntimeError("historical extraction is not deterministic")
    candidates = build_prompt_candidate_manifest(dataset_sha256=canonical_sha256(dataset))
    plan = build_calibration_plan(dataset=dataset, candidate_manifest=candidates)
    ceiling = calibration_cost_ceiling()
    if ceiling["list_price_ceiling_usd_total"] > ceiling["authorized_max_usd"]:
        raise PermissionError("calibration cost ceiling exceeds authorization")
    _write_json(paths["dataset"], dataset)
    _write_json(paths["candidates"], candidates)
    _write_json(paths["initial_plan"], plan, private=True)
    _write_report(paths["reports"] / "PAPER_EVALUATOR_PROMPT_ARCHAEOLOGY.md", _archaeology_report(dataset))
    _write_report(paths["reports"] / "PAPER_EVALUATOR_HISTORICAL_EXTRACTION.md", _extraction_report(dataset))
    return {
        "status": "READY_FOR_CALIBRATION_PAID_AUTHORIZATION",
        "dataset_sha256": canonical_sha256(dataset),
        "candidate_manifest_sha256": canonical_sha256(candidates),
        "initial_plan_sha256": file_sha256(paths["initial_plan"]),
        "labeled_task_count": dataset["labeled_task_count"],
        "initial_call_count": plan["call_count"],
        "worst_case_cost_ceiling": ceiling,
        "exact_prompt_recovered": False,
        "production_ledger_included": False,
    }


def run(config_path: Path, *, phase: str, allow_paid: bool) -> dict[str, Any]:
    config = _load_config(config_path)
    paths = _paths(config_path, config)
    if phase == "initial":
        plan_path = paths["initial_plan"]
        result_root = paths["initial_results"]
        receipt_root = paths["initial_receipts"]
    elif phase == "repeatability":
        plan_path = paths["repeat_plan"]
        result_root = paths["repeat_results"]
        receipt_root = paths["repeat_receipts"]
    else:
        raise ValueError("calibration phase must be initial or repeatability")
    return run_calibration_plan(
        plan_path=plan_path,
        rollout_base_url=str(config["rollout_base_url"]),
        runtime=dict(config["runtime"]),
        result_root=result_root,
        shim_receipt_root=receipt_root,
        calibration_root=paths["root"],
        production_root=paths["production"],
        allow_paid=allow_paid,
    )


def analyze(config_path: Path) -> dict[str, Any]:
    config = _load_config(config_path)
    paths = _paths(config_path, config)
    dataset = json.loads(paths["dataset"].read_text(encoding="utf-8"))
    candidates = json.loads(paths["candidates"].read_text(encoding="utf-8"))
    initial_plan = json.loads(paths["initial_plan"].read_text(encoding="utf-8"))
    repeat_plan = None
    repeat_root = None
    if paths["repeat_plan"].is_file():
        repeat_plan = json.loads(paths["repeat_plan"].read_text(encoding="utf-8"))
        repeat_root = paths["repeat_results"]
    results = analyze_calibration_results(
        dataset=dataset,
        candidate_manifest=candidates,
        initial_plan=initial_plan,
        initial_result_root=paths["initial_results"],
        repeatability_plan=repeat_plan,
        repeatability_result_root=repeat_root,
    )
    selected = selected_prompt_from_results(results)
    selected_payload = {
        "schema_version": "chemcrow_paper_evaluator_selected_prompt_v1",
        **selected.model_dump(mode="json"),
        "historical_agreement": results["historical_agreement"],
        "final_label": results["final_label"],
        "calibration_results_sha256": canonical_sha256(results),
        "model": results["model"],
        "temperature": results["temperature"],
        "provider_only": results["provider_only"],
        "allow_fallbacks": results["allow_fallbacks"],
        "output_schema": "DualStudentAssessment",
        "historically_calibrated_compatible_prompt": True,
        "official_or_verbatim_prompt_claimed": False,
    }
    paths["analysis"].parent.mkdir(parents=True, exist_ok=True)
    paths["analysis"].write_text(
        json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    paths["selected"].write_text(
        json.dumps(selected_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_report(
        paths["reports"] / "PAPER_EVALUATOR_CALIBRATION.md",
        _calibration_report(results, paths["candidates"]),
    )
    if repeat_plan is None:
        repeat_plan = build_repeatability_plan(
            dataset=dataset,
            candidate_manifest=candidates,
            selected_candidate_id=selected.candidate_id,
        )
        _write_json(paths["repeat_plan"], repeat_plan, private=True)
    return {
        "status": results["status"],
        "selected_candidate_id": selected.candidate_id,
        "selected_prompt_sha256": selected.prompt_sha256,
        "historical_agreement": results["historical_agreement"],
        "paid_calls": results["paid_calls"],
        "proven_cost_usd": results["proven_openrouter_reported_cost_usd"],
        "repeatability_plan_sha256": file_sha256(paths["repeat_plan"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chemcrow-paper-calibration")
    parser.add_argument("command", choices=("prepare", "run-initial", "analyze", "run-repeat"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--allow-paid", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = args.config.resolve()
    if args.command == "prepare":
        payload = prepare(config)
    elif args.command == "run-initial":
        payload = run(config, phase="initial", allow_paid=args.allow_paid)
    elif args.command == "run-repeat":
        payload = run(config, phase="repeatability", allow_paid=args.allow_paid)
    else:
        payload = analyze(config)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
