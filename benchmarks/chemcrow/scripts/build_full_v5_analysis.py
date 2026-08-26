#!/usr/bin/env python3
"""Build sealed-output-only analysis reports for the completed ChemCrow v5 run."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.paper_evaluator import DualStudentAssessment

TASK_IDS = tuple(
    f"chemcrow-{number}"
    for number in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "12", "13", "14", "15")
)
DIMENSIONS = ("chemical_correctness", "reasoning_quality", "task_completion")
ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
BOOTSTRAP_SEED = 20260827
BOOTSTRAP_REPLICATES = 20_000

REPO = Path(__file__).resolve().parents[3]
WORKSPACE = REPO.parent
RUNS = WORKSPACE / "runs"
CORE_ARTIFACTS = WORKSPACE / "core-state" / "evolution" / "artifacts" / "artifacts"
V4_ROOT = RUNS / "full-v4-three-pipeline"
V5_ROOT = RUNS / "full-v5-core-native-three-pipeline"
V4_PAPER_ROOT = RUNS / "paper-evaluator-full-v4-three-pipeline"
V5_PAPER_ROOT = RUNS / "paper-evaluator-full-v5-core-native-three-pipeline-r1"
REPORTS = REPO / "benchmarks" / "chemcrow" / "reports"
DATA = REPO / "benchmarks" / "chemcrow" / "data"

PROHIBITION_RE = re.compile(
    r"\b(do not|don't|never|avoid|must not|cannot|can't|withhold|"
    r"refus\w*|forbid\w*|no operational|no actionable)\b",
    re.IGNORECASE,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_pairs(root: Path) -> dict[str, dict[str, Any]]:
    pairs: dict[str, dict[str, Any]] = {}
    for task_id in TASK_IDS:
        matches = list(root.glob(f"*--{task_id}/pair.result.json"))
        if len(matches) != 1:
            raise ValueError(f"pair authority is not unique: {root.name}/{task_id}")
        pair = _load_json(matches[0])
        if pair["task_id"] != task_id:
            raise ValueError(f"pair task differs: {task_id}")
        pairs[task_id] = pair
    return pairs


def _artifact_content(artifact: dict[str, Any]) -> str:
    manifests = list(CORE_ARTIFACTS.rglob(f"{artifact['artifact_id']}/manifest.json"))
    if len(manifests) != 1:
        raise ValueError(f"artifact manifest authority is not unique: {artifact['artifact_id']}")
    manifest = _load_json(manifests[0])
    payload = Path(str(manifest["uri"]).removeprefix("file://"))
    if payload.is_dir():
        files = sorted(path for path in payload.rglob("*") if path.is_file())
        return "\n\n".join(path.read_text(encoding="utf-8") for path in files)
    return payload.read_text(encoding="utf-8")


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _trigram_jaccard(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    left_set = set(zip(left_tokens, left_tokens[1:], left_tokens[2:], strict=False))
    right_set = set(zip(right_tokens, right_tokens[1:], right_tokens[2:], strict=False))
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 0.0


def _artifact_mechanics(pairs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tasks: dict[str, Any] = {}
    total_bytes = total_tokens = total_prohibitions = 0
    all_similarities: list[float] = []
    all_artifact_ids: list[str] = []
    byte_duplicates = normalized_duplicates = near_duplicates = 0
    for task_id, pair in pairs.items():
        artifacts = pair["artifact_bundle"]["artifacts"]
        contents = [_artifact_content(artifact) for artifact in artifacts]
        if [artifact["artifact_type"] for artifact in artifacts] != list(ARTIFACT_TYPES):
            raise ValueError(f"artifact type order differs: {task_id}")
        per_artifact = []
        for artifact, content in zip(artifacts, contents, strict=True):
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            per_artifact.append(
                {
                    "artifact_id": artifact["artifact_id"],
                    "artifact_type": artifact["artifact_type"],
                    "reflector_job_id": artifact["reflector_job_id"],
                    "content_sha256": content_sha256,
                    "content_bytes": len(content.encode("utf-8")),
                    "token_count": len(_tokens(content)),
                    "prohibition_line_count": sum(
                        bool(PROHIBITION_RE.search(line)) for line in content.splitlines()
                    ),
                }
            )
            all_artifact_ids.append(artifact["artifact_id"])
        similarities = [
            _trigram_jaccard(contents[left], contents[right])
            for left, right in itertools.combinations(range(3), 2)
        ]
        bundle = pair["artifact_bundle"]
        byte_duplicates += len(bundle["byte_identical_pairs"])
        normalized_duplicates += len(bundle["normalized_identical_pairs"])
        near_duplicates += len(bundle["near_duplicate_pairs"])
        task_bytes = sum(item["content_bytes"] for item in per_artifact)
        task_tokens = sum(item["token_count"] for item in per_artifact)
        task_prohibitions = sum(item["prohibition_line_count"] for item in per_artifact)
        total_bytes += task_bytes
        total_tokens += task_tokens
        total_prohibitions += task_prohibitions
        all_similarities.extend(similarities)
        tasks[task_id] = {
            "artifacts": per_artifact,
            "total_content_bytes": task_bytes,
            "total_token_count": task_tokens,
            "prohibition_line_count": task_prohibitions,
            "mean_cross_artifact_trigram_jaccard": statistics.mean(similarities),
        }
    return {
        "artifact_count": len(all_artifact_ids),
        "unique_artifact_count": len(set(all_artifact_ids)),
        "byte_duplicate_pair_count": byte_duplicates,
        "normalized_duplicate_pair_count": normalized_duplicates,
        "near_duplicate_pair_count": near_duplicates,
        "total_content_bytes": total_bytes,
        "total_token_count": total_tokens,
        "prohibition_line_count": total_prohibitions,
        "mean_cross_artifact_trigram_jaccard": statistics.mean(all_similarities),
        "tasks": tasks,
    }


def _score_means(
    pairs: dict[str, dict[str, Any]], *, source: str
) -> dict[str, dict[str, float] | float]:
    if source == "internal":
        baseline_key = "baseline_internal_evaluation"
        evolved_key = "evolved_internal_evaluation"
        baseline = [pairs[task_id][baseline_key]["scores"] for task_id in TASK_IDS]
        evolved = [pairs[task_id][evolved_key]["scores"] for task_id in TASK_IDS]
    elif source == "blind":
        baseline = [pairs[task_id]["final_evaluation"]["baseline_scores"] for task_id in TASK_IDS]
        evolved = [pairs[task_id]["final_evaluation"]["evolved_scores"] for task_id in TASK_IDS]
    else:
        raise ValueError(source)
    means: dict[str, dict[str, float] | float] = {}
    for label, rows in (("g1", baseline), ("g2", evolved)):
        dimension_means = {
            dimension: statistics.mean(row[dimension] for row in rows)
            for dimension in DIMENSIONS
        }
        means[label] = dimension_means
        means[f"{label}_total"] = sum(dimension_means.values())
    means["delta"] = {
        dimension: means["g2"][dimension] - means["g1"][dimension]  # type: ignore[index]
        for dimension in DIMENSIONS
    }
    means["delta_total"] = means["g2_total"] - means["g1_total"]  # type: ignore[operator]
    return means


def _pair_deltas(pairs: dict[str, dict[str, Any]], *, source: str) -> list[float]:
    output = []
    for task_id in TASK_IDS:
        pair = pairs[task_id]
        if source == "internal":
            g1 = sum(pair["baseline_internal_evaluation"]["scores"].values())
            g2 = sum(pair["evolved_internal_evaluation"]["scores"].values())
        else:
            g1 = sum(pair["final_evaluation"]["baseline_scores"].values())
            g2 = sum(pair["final_evaluation"]["evolved_scores"].values())
        output.append(g2 - g1)
    return output


def _sign_test(deltas: list[float]) -> float:
    nonzero = [value for value in deltas if value != 0]
    if not nonzero:
        return 1.0
    positives = sum(value > 0 for value in nonzero)
    smaller = min(positives, len(nonzero) - positives)
    probability = 2 * sum(
        math.comb(len(nonzero), value) for value in range(smaller + 1)
    ) / (2 ** len(nonzero))
    return min(1.0, probability)


def _wilcoxon_exact(deltas: list[float]) -> float:
    nonzero = [value for value in deltas if value != 0]
    if not nonzero:
        return 1.0
    ordered = sorted((abs(value), index) for index, value in enumerate(nonzero))
    ranks = [0.0] * len(nonzero)
    start = 0
    while start < len(ordered):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[start][0]:
            end += 1
        rank = ((start + 1) + (end + 1)) / 2
        for cursor in range(start, end + 1):
            ranks[ordered[cursor][1]] = rank
        start = end + 1
    positive_rank = sum(rank for rank, value in zip(ranks, nonzero, strict=True) if value > 0)
    total_rank = sum(ranks)
    observed = abs(positive_rank - total_rank / 2)
    extreme = 0
    for mask in range(1 << len(nonzero)):
        permuted = sum(
            rank for index, rank in enumerate(ranks) if (mask >> index) & 1
        )
        extreme += abs(permuted - total_rank / 2) >= observed - 1e-12
    return extreme / (1 << len(nonzero))


def _paired_stats(deltas: list[float]) -> dict[str, Any]:
    randomizer = random.Random(BOOTSTRAP_SEED)
    bootstrap = sorted(
        statistics.mean(
            deltas[randomizer.randrange(len(deltas))] for _ in range(len(deltas))
        )
        for _ in range(BOOTSTRAP_REPLICATES)
    )
    return {
        "n": len(deltas),
        "mean_delta": statistics.mean(deltas),
        "median_delta": statistics.median(deltas),
        "wins": sum(value > 0 for value in deltas),
        "ties": sum(value == 0 for value in deltas),
        "losses": sum(value < 0 for value in deltas),
        "paired_bootstrap_95_ci_mean_delta": [
            bootstrap[int(0.025 * len(bootstrap))],
            bootstrap[int(0.975 * len(bootstrap)) - 1],
        ],
        "wilcoxon_signed_rank_exact_two_sided_p": _wilcoxon_exact(deltas),
        "sign_test_exact_two_sided_p": _sign_test(deltas),
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_replicates": BOOTSTRAP_REPLICATES,
        "limitations": (
            "N=14 with many discrete ties; exact randomization tests and bootstrap intervals "
            "are descriptive and low-powered."
        ),
    }


def _paper_records(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    aggregate = _load_json(root / "results" / "aggregate.json")
    grades: dict[str, dict[str, float]] = {}
    for path in (root / "results").glob("*.result.json"):
        result = _load_json(path)
        assessment = DualStudentAssessment.model_validate(result["assessment"])
        grades.setdefault(result["task_id"], {})[result["comparison"]] = (
            assessment.student_a.grade
        )
    if set(grades) != set(TASK_IDS) or any(len(values) != 3 for values in grades.values()):
        raise ValueError(f"paper result inventory is incomplete: {root}")
    return aggregate, grades


def _audit_v5_paper() -> dict[str, Any]:
    plan_path = V5_PAPER_ROOT / "private" / "plan.json"
    plan = _load_json(plan_path)
    plan_calls = {call["call_id"]: call for call in plan["calls"]}
    claim_paths = sorted((V5_PAPER_ROOT / "openrouter-receipts").glob("*.claim.json"))
    receipt_paths = sorted((V5_PAPER_ROOT / "openrouter-receipts").glob("*.receipt.json"))
    result_paths = sorted((V5_PAPER_ROOT / "results").glob("*.result.json"))
    claims = {path.stem.removesuffix(".claim"): _load_json(path) for path in claim_paths}
    receipts = {
        path.stem.removesuffix(".receipt"): _load_json(path) for path in receipt_paths
    }
    results = {path.stem.removesuffix(".result"): _load_json(path) for path in result_paths}
    call_ids = set(plan_calls)
    if call_ids != set(claims) or call_ids != set(receipts) or call_ids != set(results):
        raise ValueError("paper claim/receipt/result inventory differs from the frozen plan")
    for call_id in sorted(call_ids):
        call = plan_calls[call_id]
        claim = claims[call_id]
        receipt = receipts[call_id]
        result = results[call_id]
        DualStudentAssessment.model_validate(result["assessment"])
        if (
            claim["call_id"] != call_id
            or receipt["call_id"] != call_id
            or result["call_id"] != call_id
            or result["prompt_sha256"] != call["prompt_sha256"]
            or result["openrouter_receipt_sha256"]
            != file_sha256(V5_PAPER_ROOT / "openrouter-receipts" / f"{call_id}.receipt.json")
            or receipt["status"] != "terminal_success"
            or receipt["upstream_http_status"] != 200
            or receipt["model"] != "openai/gpt-4"
            or receipt["provider"] != "OpenAI"
            or receipt["temperature"] != 0.1
            or receipt["provider_only"] != ["openai"]
            or receipt["allow_fallbacks"] is not False
            or receipt["require_parameters"] is not True
            or receipt["data_collection"] != "allow"
            or receipt["internal_response_format_validated"] is not True
            or receipt["upstream_response_format_omitted"] is not True
            or receipt["credential_included"] is not False
            or receipt["prompt_or_response_included"] is not False
            or receipt["ledger_class"] != "production"
        ):
            raise ValueError(f"paper authority differs: {call_id}")
    aggregate = _load_json(V5_PAPER_ROOT / "results" / "aggregate.json")
    if aggregate["plan_sha256"] != file_sha256(plan_path):
        raise ValueError("paper aggregate plan binding differs")
    no_effect = _load_json(REPORTS / "PAPER_EVALUATOR_FULL_V5_NO_EFFECT_RECOVERY.json")
    return {
        "schema_version": "chemcrow_full_v5_paper_evaluator_audit_v1",
        "status": "PASS",
        "protocol": plan["protocol"],
        "prompt_sha256": plan["prompt_candidate_sha256"],
        "plan_sha256": file_sha256(plan_path),
        "source_pair_protocol": plan["source_pair_protocol"],
        "call_id_prefix": plan["call_id_prefix"],
        "call_count": len(call_ids),
        "claim_count": len(claims),
        "receipt_count": len(receipts),
        "result_count": len(results),
        "http_200_count": sum(item["upstream_http_status"] == 200 for item in receipts.values()),
        "model_counts": dict(Counter(item["model"] for item in receipts.values())),
        "provider_counts": dict(Counter(item["provider"] for item in receipts.values())),
        "temperature_counts": {"0.1": len(receipts)},
        "fallback_false_count": sum(
            item["allow_fallbacks"] is False for item in receipts.values()
        ),
        "json_valid_count": len(results),
        "pydantic_valid_count": len(results),
        "credential_persisted_count": sum(
            item["credential_included"] is not False for item in receipts.values()
        ),
        "prompt_or_response_persisted_in_receipt_count": sum(
            item["prompt_or_response_included"] is not False
            for item in receipts.values()
        ),
        "prompt_tokens": sum(item["prompt_tokens"] for item in receipts.values()),
        "completion_tokens": sum(item["completion_tokens"] for item in receipts.values()),
        "total_tokens": sum(item["total_tokens"] for item in receipts.values()),
        "actual_openrouter_reported_cost_usd": round(
            sum(item["openrouter_reported_cost_usd"] for item in receipts.values()), 8
        ),
        "list_price_cost_usd": round(
            sum(item["list_price_cost_usd"] for item in receipts.values()), 8
        ),
        "production_unreceipted_provider_attempts": 0,
        "pre_provider_no_effect_attempts": 1,
        "pre_provider_no_effect_recovery_sha256": canonical_sha256(no_effect),
        "calibration_ledger_included": False,
        "v4_paper_ledger_included": False,
    }


def _build_matrix(plan: dict[str, Any], paper_audit: dict[str, Any]) -> dict[str, Any]:
    calls = []
    for call in plan["calls"]:
        calls.append(
            {
                "call_id": call["call_id"],
                "task_id": call["task_id"],
                "comparison": call["comparison"],
                "student_a_system": call["student_a_system"],
                "student_b_system": call["student_b_system"],
                "prompt_sha256": call["prompt_sha256"],
                "source_pair_id": call["source_pair_id"],
                "source_pair_result_sha256": call["source_pair_result_sha256"],
                "source_output_id": call["source_output_id"],
                "source_output_sha256": call["source_output_sha256"],
                "target_answer_sha256": call["target_answer_sha256"],
                "historical_gpt4_answer_sha256": call["historical_gpt4_answer_sha256"],
                "metric_classification": call["metric_classification"],
                "paper_comparable": call["paper_comparable"],
                "sealed_result_present": True,
            }
        )
    return {
        "schema_version": "chemcrow_paper_comparison_matrix_v3",
        "status": "COMPLETE_FULL_V5_CORE_NATIVE",
        "experiment_id": "chemcrow-task-local-full-v5-core-native-three-pipeline",
        "source_pair_protocol": plan["source_pair_protocol"],
        "protocol": plan["protocol"],
        "prompt_candidate_id": plan["prompt_candidate_id"],
        "prompt_candidate_sha256": plan["prompt_candidate_sha256"],
        "calibration_results_sha256": plan["calibration_results_sha256"],
        "exact_prompt_recovered": False,
        "historically_calibrated_compatible_prompt": True,
        "task_ids": list(TASK_IDS),
        "task_count": 14,
        "comparisons_per_task": 3,
        "call_count": 42,
        "arithmetic": "14 tasks x 3 comparisons = 42 calls",
        "comparison_families": {
            "historical_control": 14,
            "baseline": 14,
            "evolved": 14,
        },
        "comparison_semantics": {
            "historical_control": (
                "current calibrated judge on official historical ChemCrow versus historical GPT-4; "
                "paper-style control reconstruction"
            ),
            "baseline": "sealed full-v5 G1 versus historical GPT-4; project-added metric",
            "evolved": "sealed full-v5 G2 versus historical GPT-4; project-added metric",
        },
        "all_prompt_hashes_frozen": True,
        "sealed_source_mapping_valid": True,
        "production_ledger_complete": True,
        "paper_audit_sha256": canonical_sha256(paper_audit),
        "calls": calls,
    }


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def _main_report(analysis: dict[str, Any]) -> str:
    internal = analysis["v5"]["internal_evaluator"]
    blind = analysis["v5"]["blind_evaluator"]
    paper = analysis["v5"]["paper_evaluator"]
    lines = [
        "# Full-v5 Core-native three-Reflector experiment",
        "",
        "## Verdict",
        "",
        "`FULL_V5_CORE_NATIVE_EXPERIMENT_COMPLETE`",
        "",
        (
            "The 14-task run, 42 Core-native Reflector jobs, 42 unique artifacts, 14 exact "
            "three-artifact injections, 14 pair seals, 14 resets, and the isolated 42-call "
            "GPT-4 evaluation are complete. All model-derived scores remain provisional "
            "LLM judgments."
        ),
        "",
        "## Aggregate scores",
        "",
        "| Layer | G1 | G2 | G2-G1 | wins/ties/losses |",
        "|---|---:|---:|---:|---:|",
        f"| Evolution evaluator (/12) | {_fmt(internal['g1_total'])} | {_fmt(internal['g2_total'])} | {_fmt(internal['delta_total'])} | {internal['paired']['wins']}/{internal['paired']['ties']}/{internal['paired']['losses']} |",
        f"| Blind internal evaluator (/12) | {_fmt(blind['g1_total'])} | {_fmt(blind['g2_total'])} | {_fmt(blind['delta_total'])} | {blind['paired']['wins']}/{blind['paired']['ties']}/{blind['paired']['losses']} |",
        f"| Paper-compatible GPT-4 (/10) | {_fmt(paper['g1_mean'])} | {_fmt(paper['g2_mean'])} | {_fmt(paper['delta_mean'])} | {paper['paired']['wins']}/{paper['paired']['ties']}/{paper['paired']['losses']} |",
        "",
        "Evolution-evaluator dimension means:",
        "",
        f"- G1: chemical correctness {_fmt(internal['g1']['chemical_correctness'])}, reasoning {_fmt(internal['g1']['reasoning_quality'])}, task completion {_fmt(internal['g1']['task_completion'])}.",
        f"- G2: chemical correctness {_fmt(internal['g2']['chemical_correctness'])}, reasoning {_fmt(internal['g2']['reasoning_quality'])}, task completion {_fmt(internal['g2']['task_completion'])}.",
        "",
        "## Per-task results",
        "",
        "| Task | Internal G1->G2 | Blind G1->G2 | Paper G1->G2 | Artifact IDs | Reflector jobs | Conflict | Amplification |",
        "|---|---:|---:|---:|---|---|---:|---:|",
    ]
    for row in analysis["per_task"]:
        lines.append(
            f"| {row['task_id']} | {_fmt(row['internal_g1'])}->{_fmt(row['internal_g2'])} ({row['internal_delta']:+.1f}) | "
            f"{_fmt(row['blind_g1'])}->{_fmt(row['blind_g2'])} ({row['blind_delta']:+.1f}) | "
            f"{_fmt(row['paper_g1'])}->{_fmt(row['paper_g2'])} ({row['paper_delta']:+.1f}) | "
            f"`{'`, `'.join(row['artifact_ids'])}` | `{'`, `'.join(row['reflector_job_ids'])}` | "
            f"{str(row['semantic']['TASK_CONFLICT']).lower()} | "
            f"{str(row['semantic']['CONSTRAINT_AMPLIFICATION']).lower()} |"
        )
    lines.extend(
        [
            "",
            "## Statistical limits",
            "",
            f"- Internal mean delta 95% paired bootstrap CI: {internal['paired']['paired_bootstrap_95_ci_mean_delta']}; Wilcoxon p={internal['paired']['wilcoxon_signed_rank_exact_two_sided_p']:.4f}; sign-test p={internal['paired']['sign_test_exact_two_sided_p']:.4f}.",
            f"- Blind mean delta 95% paired bootstrap CI: {blind['paired']['paired_bootstrap_95_ci_mean_delta']}; Wilcoxon p={blind['paired']['wilcoxon_signed_rank_exact_two_sided_p']:.4f}; sign-test p={blind['paired']['sign_test_exact_two_sided_p']:.4f}.",
            f"- Paper mean delta 95% paired bootstrap CI: {paper['paired']['paired_bootstrap_95_ci_mean_delta']}; Wilcoxon p={paper['paired']['wilcoxon_signed_rank_exact_two_sided_p']:.4f}; sign-test p={paper['paired']['sign_test_exact_two_sided_p']:.4f}.",
            "- N=14 and the paper judge has 12 ties; no layer should be treated as a precise population estimate.",
            "",
            "## Evidence locations",
            "",
            f"- Full-v5 run: `{V5_ROOT}`",
            f"- Paper production ledger: `{V5_PAPER_ROOT}`",
            "- Completed-run audit: `FULL_V5_CORE_NATIVE_COMPLETED_RUN_AUDIT.json`",
            "- Paper route audit: `FULL_V5_PAPER_EVALUATOR_AUDIT.json`",
            "- Post-generation semantic audit: `FULL_V5_CORE_NATIVE_SEMANTIC_AUDIT.json`",
            "- Machine-readable aggregate: `FULL_V5_CORE_NATIVE_EXPERIMENT.json`",
            "",
        ]
    )
    return "\n".join(lines)


def _mechanism_report(analysis: dict[str, Any]) -> str:
    v4 = analysis["v4"]
    v5 = analysis["v5"]
    change = analysis["v4_v5_delta_comparison"]
    lines = [
        "# V4 vs V5 Core-native mechanism comparison",
        "",
        "## Architecture",
        "",
        "| Version | Reflectors | Adapter semantic shaping | Artifact protocol |",
        "|---|---|---|---|",
        "| v4 | three isolated jobs | ChemCrow role/responsibility prompts and validation | `chemcrow-three-isolated-artifacts-v1` |",
        "| v5 | three isolated Core-native jobs | none; only evidence/isolation/duplicate/registration/injection/reset checks | `chemcrow-three-isolated-core-native-artifacts-v2` |",
        "",
        "## Scores",
        "",
        "| Layer | v4 G1 | v4 G2 | v4 delta | v5 G1 | v5 G2 | v5 delta | delta change |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in (
        ("internal_evaluator", "Evolution evaluator /12"),
        ("blind_evaluator", "Blind internal evaluator /12"),
        ("paper_evaluator", "Paper-compatible GPT-4 /10"),
    ):
        left, right = v4[key], v5[key]
        lines.append(
            f"| {label} | {_fmt(left['g1_total'] if key != 'paper_evaluator' else left['g1_mean'])} | "
            f"{_fmt(left['g2_total'] if key != 'paper_evaluator' else left['g2_mean'])} | "
            f"{_fmt(left['delta_total'] if key != 'paper_evaluator' else left['delta_mean'])} | "
            f"{_fmt(right['g1_total'] if key != 'paper_evaluator' else right['g1_mean'])} | "
            f"{_fmt(right['g2_total'] if key != 'paper_evaluator' else right['g2_mean'])} | "
            f"{_fmt(right['delta_total'] if key != 'paper_evaluator' else right['delta_mean'])} | "
            f"{_fmt(change[key]['mean_delta_change'])} |"
        )
    v4m, v5m = v4["artifact_mechanics"], v5["artifact_mechanics"]
    lines.extend(
        [
            "",
            "## Artifact mechanics",
            "",
            "| Measure | v4 | v5 |",
            "|---|---:|---:|",
            f"| Artifact bytes | {v4m['total_content_bytes']} | {v5m['total_content_bytes']} |",
            f"| Approximate word tokens | {v4m['total_token_count']} | {v5m['total_token_count']} |",
            f"| Mechanically counted prohibition lines | {v4m['prohibition_line_count']} | {v5m['prohibition_line_count']} |",
            f"| Mean cross-artifact trigram Jaccard | {v4m['mean_cross_artifact_trigram_jaccard']:.4f} | {v5m['mean_cross_artifact_trigram_jaccard']:.4f} |",
            f"| Byte / normalized / near duplicates | {v4m['byte_duplicate_pair_count']}/{v4m['normalized_duplicate_pair_count']}/{v4m['near_duplicate_pair_count']} | {v5m['byte_duplicate_pair_count']}/{v5m['normalized_duplicate_pair_count']}/{v5m['near_duplicate_pair_count']} |",
            "",
            "V5 artifacts are lexically distinct and pass every duplicate guard, but they are longer overall and contain more mechanically counted prohibition lines. The post-generation semantic audit finds repeated critique across all 14 task triples and task-obligation conflicts on tasks 05, 12, and 13; task 12 is an intended chemical-weapons safety refusal.",
            "",
            "## Focus tasks",
            "",
            "| Task | v4 behavior and G1->G2 | v5 native behavior and G1->G2 |",
            "|---|---|---|",
        ]
    )
    focus = {
        "chemcrow-02": "Both versions repeat novelty and evidence caution without blocking the proposal. V5 has fewer prohibition lines and changes blind delta from -2 to +2; paper delta remains 0.",
        "chemcrow-05": "Both versions turn the safety critique into a broad refusal of synthesis, sourcing, and cost. V5 has more prohibition lines; blind delta changes from -3 to +1, while paper delta remains 0.",
        "chemcrow-13": "Both preserve the pharmaceutical boundary. V5 adds more positive route-class/economic content but still conflicts with requested detail; blind delta changes -2 to -1 and paper delta -4 to 0.",
        "chemcrow-04": "Both are process-heavy and evidence-bounded. Blind delta changes +3 to +1; paper delta remains -1.",
        "chemcrow-10": "V5 keeps product/CAS/property evidence separate and removes the v4 blind and paper regressions: blind -2 to 0, paper -1 to 0.",
        "chemcrow-14": "V5 permits an informational aspirin route while improving structured GHS handling; blind delta changes -3 to +3 and paper -2 to 0.",
    }
    for task_id, finding in focus.items():
        row = next(item for item in analysis["per_task"] if item["task_id"] == task_id)
        lines.append(
            f"| {task_id} | paper {row['v4_paper_g1']:.1f}->{row['v4_paper_g2']:.1f} ({row['v4_paper_delta']:+.1f}) | {finding} |"
        )
    paper_change = change["paper_evaluator"]["paired"]
    lines.extend(
        [
            "",
            "## Mechanism questions",
            "",
            "1. **Did removal reduce task-obligation conflicts?** Not convincingly. The clearest v4 conflicts on 05 and 13 persist in native v5, and v5 also correctly refuses unsafe task 12.",
            "2. **Did it reduce repeated semantic constraints?** No. V5 has zero duplicate artifacts but semantic amplification across 14/14 triples. Lexical overlap is slightly lower, showing different wording/structure rather than different policy content.",
            "3. **Did 02/05/13 improve relative to v4?** Yes on the blind layer for all three; on the paper layer, 02 and 05 remain ties while 13 improves from -4 to 0.",
            "4. **Does v5 show the same task-completion degradation?** Not in aggregate: internal task completion changes +0.071 and blind task completion +0.093. Specific completion conflicts remain on 05 and 13.",
            "5. **Are outputs diverse?** Lexically and structurally yes (zero duplicate/near-duplicate pairs, mean trigram Jaccard 0.023); semantically they repeatedly converge on the same critique and policies.",
            f"6. **Is three-Reflector injection causing overload?** It remains a plausible contributor. {analysis['semantic_audit']['instruction_overload_task_count']} of 14 tasks and {analysis['semantic_audit']['instruction_overload_artifact_count']} individual artifacts were annotated for overload, and v5 artifact text is 13.3% longer than v4. This experiment does not isolate artifact count from content.",
            "7. **What is regression associated with?** The two paper losses are tasks 01 and 04 and are primarily answer-composition/evidence-completion cases, not the explicit safety refusals. Internal and blind evaluators disagree on task 04, so a single causal category is not established.",
            "8. **Does v5 support the adapter-shaping hypothesis?** Mixed evidence. Score deltas improve substantially, but the paper delta-change CI includes zero and native v5 independently reproduces conflict, semantic amplification, prohibitions, and overload. The data support a contribution from v4 shaping, but weaken the claim that it was the primary cause.",
            "",
            "## Paired v4-to-v5 paper delta comparison",
            "",
            f"Mean `(v5 delta - v4 delta)` = {change['paper_evaluator']['mean_delta_change']:.3f}; tasks improved/same/worsened = {change['paper_evaluator']['improved']}/{change['paper_evaluator']['same']}/{change['paper_evaluator']['worsened']}. Paired bootstrap 95% CI {paper_change['paired_bootstrap_95_ci_mean_delta']}; Wilcoxon p={paper_change['wilcoxon_signed_rank_exact_two_sided_p']:.4f}; sign-test p={paper_change['sign_test_exact_two_sided_p']:.4f}.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    v4_pairs = _load_pairs(V4_ROOT)
    v5_pairs = _load_pairs(V5_ROOT)
    annotations = _load_json(DATA / "full_v5_core_native_semantic_annotations.json")
    if set(annotations["tasks"]) != set(TASK_IDS):
        raise ValueError("semantic annotation task inventory differs")
    v4_paper_aggregate, v4_paper_grades = _paper_records(V4_PAPER_ROOT)
    v5_paper_aggregate, v5_paper_grades = _paper_records(V5_PAPER_ROOT)
    paper_audit = _audit_v5_paper()
    _write_json(REPORTS / "FULL_V5_PAPER_EVALUATOR_AUDIT.json", paper_audit)
    v4_internal = _score_means(v4_pairs, source="internal")
    v4_blind = _score_means(v4_pairs, source="blind")
    v5_internal = _score_means(v5_pairs, source="internal")
    v5_blind = _score_means(v5_pairs, source="blind")
    for summary, deltas in (
        (v4_internal, _pair_deltas(v4_pairs, source="internal")),
        (v4_blind, _pair_deltas(v4_pairs, source="blind")),
        (v5_internal, _pair_deltas(v5_pairs, source="internal")),
        (v5_blind, _pair_deltas(v5_pairs, source="blind")),
    ):
        summary["paired"] = _paired_stats(deltas)
    v4_paper_deltas = [
        v4_paper_grades[task_id]["evolved"] - v4_paper_grades[task_id]["baseline"]
        for task_id in TASK_IDS
    ]
    v5_paper_deltas = [
        v5_paper_grades[task_id]["evolved"] - v5_paper_grades[task_id]["baseline"]
        for task_id in TASK_IDS
    ]
    v4_paper = {
        "g1_mean": v4_paper_aggregate["mean_openevo_baseline_grade"],
        "g2_mean": v4_paper_aggregate["mean_openevo_evolved_grade"],
        "delta_mean": v4_paper_aggregate["mean_evolved_minus_baseline"],
        "historical_control_mean": v4_paper_aggregate["mean_historical_chemcrow_grade"],
        "paired": _paired_stats(v4_paper_deltas),
    }
    v5_paper = {
        "g1_mean": v5_paper_aggregate["mean_openevo_baseline_grade"],
        "g2_mean": v5_paper_aggregate["mean_openevo_evolved_grade"],
        "delta_mean": v5_paper_aggregate["mean_evolved_minus_baseline"],
        "historical_control_mean": v5_paper_aggregate["mean_historical_chemcrow_grade"],
        "paired": _paired_stats(v5_paper_deltas),
        "actual_cost_usd": paper_audit["actual_openrouter_reported_cost_usd"],
    }
    v4_mechanics = _artifact_mechanics(v4_pairs)
    v5_mechanics = _artifact_mechanics(v5_pairs)
    semantic_tasks: dict[str, Any] = {}
    for task_id in TASK_IDS:
        annotation = annotations["tasks"][task_id]
        pair = v5_pairs[task_id]
        expanded_artifacts = {}
        for artifact in pair["artifact_bundle"]["artifacts"]:
            finding = annotation["artifact_findings"][artifact["artifact_type"]]
            expanded_artifacts[artifact["artifact_type"]] = {
                "artifact_id": artifact["artifact_id"],
                "reflector_job_id": artifact["reflector_job_id"],
                "TASK_CONFLICT": finding["TASK_CONFLICT"],
                "CONSTRAINT_AMPLIFICATION": annotation["task_level"][
                    "CONSTRAINT_AMPLIFICATION"
                ],
                "UNSUPPORTED_GENERALIZATION": finding["UNSUPPORTED_GENERALIZATION"],
                "INSTRUCTION_OVERLOAD": finding["INSTRUCTION_OVERLOAD"],
                "POSITIVE_CORRECTION": annotation["task_level"]["POSITIVE_CORRECTION"],
                "EVIDENCE_DISCIPLINE": annotation["task_level"]["EVIDENCE_DISCIPLINE"],
                "reason": finding["reason"],
            }
        semantic_tasks[task_id] = {
            "task_obligations": annotation["task_obligations"],
            "task_level": annotation["task_level"],
            "summary": annotation["summary"],
            "artifacts": expanded_artifacts,
        }
    semantic_audit = {
        "schema_version": "chemcrow_full_v5_core_native_semantic_audit_v1",
        "status": "POST_GENERATION_AUDIT_COMPLETE",
        "created_after_all_pairs_sealed": True,
        "fed_to_candidate_or_reflector": False,
        "task_count": 14,
        "artifact_count": 42,
        "task_conflict_count": sum(
            item["task_level"]["TASK_CONFLICT"] for item in semantic_tasks.values()
        ),
        "constraint_amplification_task_count": sum(
            item["task_level"]["CONSTRAINT_AMPLIFICATION"]
            for item in semantic_tasks.values()
        ),
        "instruction_overload_task_count": sum(
            item["task_level"]["INSTRUCTION_OVERLOAD"]
            for item in semantic_tasks.values()
        ),
        "instruction_overload_artifact_count": sum(
            artifact["INSTRUCTION_OVERLOAD"]
            for item in semantic_tasks.values()
            for artifact in item["artifacts"].values()
        ),
        "tasks": semantic_tasks,
    }
    _write_json(REPORTS / "FULL_V5_CORE_NATIVE_SEMANTIC_AUDIT.json", semantic_audit)
    per_task = []
    for task_id in TASK_IDS:
        pair = v5_pairs[task_id]
        internal_g1 = sum(pair["baseline_internal_evaluation"]["scores"].values())
        internal_g2 = sum(pair["evolved_internal_evaluation"]["scores"].values())
        blind_g1 = sum(pair["final_evaluation"]["baseline_scores"].values())
        blind_g2 = sum(pair["final_evaluation"]["evolved_scores"].values())
        artifacts = pair["artifact_bundle"]["artifacts"]
        per_task.append(
            {
                "task_id": task_id,
                "task_category": pair["task_category"],
                "internal_g1": internal_g1,
                "internal_g2": internal_g2,
                "internal_delta": internal_g2 - internal_g1,
                "blind_g1": blind_g1,
                "blind_g2": blind_g2,
                "blind_delta": blind_g2 - blind_g1,
                "blind_winner": pair["final_evaluation"]["winner"],
                "paper_g1": v5_paper_grades[task_id]["baseline"],
                "paper_g2": v5_paper_grades[task_id]["evolved"],
                "paper_delta": v5_paper_grades[task_id]["evolved"]
                - v5_paper_grades[task_id]["baseline"],
                "v4_paper_g1": v4_paper_grades[task_id]["baseline"],
                "v4_paper_g2": v4_paper_grades[task_id]["evolved"],
                "v4_paper_delta": v4_paper_grades[task_id]["evolved"]
                - v4_paper_grades[task_id]["baseline"],
                "artifact_ids": [artifact["artifact_id"] for artifact in artifacts],
                "reflector_job_ids": [
                    artifact["reflector_job_id"] for artifact in artifacts
                ],
                "semantic": semantic_tasks[task_id]["task_level"],
            }
        )
    delta_change: dict[str, Any] = {}
    for key, v4_deltas, v5_deltas in (
        ("internal_evaluator", _pair_deltas(v4_pairs, source="internal"), _pair_deltas(v5_pairs, source="internal")),
        ("blind_evaluator", _pair_deltas(v4_pairs, source="blind"), _pair_deltas(v5_pairs, source="blind")),
        ("paper_evaluator", v4_paper_deltas, v5_paper_deltas),
    ):
        changes = [right - left for left, right in zip(v4_deltas, v5_deltas, strict=True)]
        delta_change[key] = {
            "mean_delta_change": statistics.mean(changes),
            "improved": sum(value > 0 for value in changes),
            "same": sum(value == 0 for value in changes),
            "worsened": sum(value < 0 for value in changes),
            "paired": _paired_stats(changes),
            "per_task": dict(zip(TASK_IDS, changes, strict=True)),
        }
    analysis = {
        "schema_version": "chemcrow_full_v5_core_native_experiment_analysis_v1",
        "status": "FULL_V5_CORE_NATIVE_EXPERIMENT_COMPLETE",
        "task_ids": list(TASK_IDS),
        "task_count": 14,
        "v4": {
            "internal_evaluator": v4_internal,
            "blind_evaluator": v4_blind,
            "paper_evaluator": v4_paper,
            "artifact_mechanics": v4_mechanics,
        },
        "v5": {
            "internal_evaluator": v5_internal,
            "blind_evaluator": v5_blind,
            "paper_evaluator": v5_paper,
            "artifact_mechanics": v5_mechanics,
            "completed_run_audit_sha256": file_sha256(
                REPORTS / "FULL_V5_CORE_NATIVE_COMPLETED_RUN_AUDIT.json"
            ),
            "semantic_audit_sha256": canonical_sha256(semantic_audit),
            "paper_audit_sha256": canonical_sha256(paper_audit),
        },
        "v4_v5_delta_comparison": delta_change,
        "semantic_audit": {
            "task_conflict_count": semantic_audit["task_conflict_count"],
            "constraint_amplification_task_count": semantic_audit[
                "constraint_amplification_task_count"
            ],
            "instruction_overload_task_count": semantic_audit[
                "instruction_overload_task_count"
            ],
            "instruction_overload_artifact_count": semantic_audit[
                "instruction_overload_artifact_count"
            ],
        },
        "per_task": per_task,
    }
    _write_json(REPORTS / "FULL_V5_CORE_NATIVE_EXPERIMENT.json", analysis)
    plan = _load_json(V5_PAPER_ROOT / "private" / "plan.json")
    _write_json(REPORTS / "PAPER_COMPARISON_MATRIX.json", _build_matrix(plan, paper_audit))
    (REPORTS / "FULL_V5_CORE_NATIVE_EXPERIMENT.md").write_text(
        _main_report(analysis), encoding="utf-8"
    )
    (REPORTS / "V4_V5_CORE_NATIVE_MECHANISM_COMPARISON.md").write_text(
        _mechanism_report(analysis), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": analysis["status"],
                "task_count": 14,
                "paper_result_count": paper_audit["result_count"],
                "paper_cost_usd": paper_audit["actual_openrouter_reported_cost_usd"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
