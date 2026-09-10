#!/usr/bin/env python3
"""Build the sealed full-v8 G2/GPT-4 analysis tables and figures."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import binomtest, wilcoxon

TASK_IDS = [f"chemcrow-{number:02d}" for number in (*range(1, 11), *range(12, 16))]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def score_total(scores: dict[str, float]) -> float:
    return sum(float(scores[key]) for key in (
        "chemical_correctness",
        "reasoning_quality",
        "task_completion",
    ))


def comparison(label: str, source: str, former: list[float], latter: list[float]) -> dict:
    deltas = np.asarray(former, dtype=float) - np.asarray(latter, dtype=float)
    return {
        "comparison": label,
        "source": source,
        "former_mean": float(np.mean(former)),
        "latter_mean": float(np.mean(latter)),
        "delta": float(np.mean(deltas)),
        "wins": int(np.sum(deltas > 0)),
        "ties": int(np.sum(deltas == 0)),
        "losses": int(np.sum(deltas < 0)),
        "count": len(deltas),
    }


def paired_stats(deltas: list[float], *, seed: int = 20260831) -> dict:
    values = np.asarray(deltas, dtype=float)
    rng = np.random.default_rng(seed)
    bootstrap = np.mean(
        rng.choice(values, size=(100_000, len(values)), replace=True),
        axis=1,
    )
    nonzero = values[values != 0]
    if len(nonzero):
        signed = binomtest(int(np.sum(nonzero > 0)), len(nonzero), 0.5)
        signed_payload = {
            "non_tied_count": len(nonzero),
            "positive_count": int(np.sum(nonzero > 0)),
            "pvalue_two_sided": float(signed.pvalue),
        }
        try:
            ranked = wilcoxon(values, zero_method="wilcox", alternative="two-sided")
            wilcoxon_payload = {
                "statistic": float(ranked.statistic),
                "pvalue_two_sided": float(ranked.pvalue),
                "zero_method": "wilcox",
            }
        except ValueError:
            wilcoxon_payload = None
    else:
        signed_payload = None
        wilcoxon_payload = None
    return {
        "count": len(values),
        "mean_delta": float(np.mean(values)),
        "median_delta": float(np.median(values)),
        "wins": int(np.sum(values > 0)),
        "ties": int(np.sum(values == 0)),
        "losses": int(np.sum(values < 0)),
        "paired_bootstrap_95_ci_mean_delta": [
            float(np.quantile(bootstrap, 0.025)),
            float(np.quantile(bootstrap, 0.975)),
        ],
        "wilcoxon_signed_rank": wilcoxon_payload,
        "exact_sign_test": signed_payload,
    }


def historical_notebook_grades(root: Path) -> dict[str, tuple[float, float]]:
    pattern = re.compile(
        r"Student\s*([12])(?:'s)?\s+Grade\s*:\s*([0-9]+(?:\.[0-9]+)?)",
        re.IGNORECASE,
    )
    output: dict[str, tuple[float, float]] = {}
    for task_id in TASK_IDS:
        number = task_id.rsplit("-", 1)[-1]
        matches = sorted((root / "tasks").glob(f"{number}_*.ipynb"))
        if len(matches) != 1:
            raise ValueError(f"historical notebook is not unique: {task_id}")
        notebook = json.loads(matches[0].read_text(encoding="utf-8"))
        text = "\n".join(
            "".join(cell.get("source", []))
            + "\n"
            + "\n".join(
                "".join(item.get("text", []))
                for item in cell.get("outputs", [])
                if isinstance(item, dict)
            )
            for cell in notebook.get("cells", [])
            if isinstance(cell, dict) and "Evaluator(" in "".join(cell.get("source", []))
        )
        observed = {key: float(value) for key, value in pattern.findall(text)}
        if set(observed) != {"1", "2"}:
            raise ValueError(f"historical grades are incomplete: {task_id}")
        output[task_id] = (observed["1"], observed["2"])
    return output


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def draw_table(path: Path, rows: list[dict], columns: list[tuple[str, str]], *, title: str,
               width: float, height: float, font_size: float) -> None:
    figure, axis = plt.subplots(figsize=(width, height), dpi=180)
    figure.patch.set_facecolor("#111315")
    axis.set_facecolor("#111315")
    axis.axis("off")
    values = [[row[key] for key, _ in columns] for row in rows]
    table = axis.table(
        cellText=values,
        colLabels=[label for _, label in columns],
        cellLoc="center",
        colLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(font_size)
    table.scale(1.0, 1.55)
    for (row_index, _), cell in table.get_celld().items():
        cell.set_edgecolor("#40454a")
        cell.set_linewidth(0.6)
        if row_index == 0:
            cell.set_facecolor("#24292e")
            cell.get_text().set_color("#f1c75b")
            cell.get_text().set_weight("bold")
        else:
            cell.set_facecolor("#171a1d" if row_index % 2 else "#1d2125")
            cell.get_text().set_color("#e6e6e6")
    axis.set_title(title, color="#f1c75b", fontsize=14, pad=18, weight="bold")
    figure.tight_layout(pad=1.2)
    figure.savefig(path, bbox_inches="tight", facecolor=figure.get_facecolor())
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--paper-root", type=Path, required=True)
    parser.add_argument("--historical-runs-root", type=Path, required=True)
    parser.add_argument("--artifact-index", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)

    aggregate_path = args.run_root / "aggregate.json"
    run_audit_path = args.run_root / "completed_run.audit.json"
    paper_aggregate_path = args.paper_root / "results" / "aggregate.json"
    plan_path = args.paper_root / "private" / "plan.json"
    internal_aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    run_audit = json.loads(run_audit_path.read_text(encoding="utf-8"))
    paper_aggregate = json.loads(paper_aggregate_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    pair_paths = sorted(args.run_root.glob("*--chemcrow-*/pair.result.json"))
    pairs = {json.loads(path.read_text(encoding="utf-8"))["task_id"]:
             json.loads(path.read_text(encoding="utf-8")) for path in pair_paths}
    if sorted(pairs) != TASK_IDS:
        raise ValueError("pair inventory differs")
    result_paths = sorted((args.paper_root / "results").glob("*.result.json"))
    paper_results = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
    by_task_paper: dict[str, dict[str, dict]] = {task_id: {} for task_id in TASK_IDS}
    for result in paper_results:
        by_task_paper[result["task_id"]][result["comparison"]] = result
    if len(paper_results) != 42 or any(len(group) != 3 for group in by_task_paper.values()):
        raise ValueError("paper result inventory differs")

    official = historical_notebook_grades(args.historical_runs_root)
    artifact_rows = list(csv.DictReader(args.artifact_index.open(encoding="utf-8", newline="")))
    artifacts_by_task: dict[str, list[dict]] = {task_id: [] for task_id in TASK_IDS}
    for row in artifact_rows:
        artifacts_by_task[row["task_id"]].append(row)

    task_rows = []
    for task_id in TASK_IDS:
        pair = pairs[task_id]
        paper = by_task_paper[task_id]
        internal_g1 = score_total(pair["baseline_internal_evaluation"]["scores"])
        internal_g2 = score_total(pair["evolved_internal_evaluation"]["scores"])
        blind_g1 = score_total(pair["final_evaluation"]["baseline_scores"])
        blind_g2 = score_total(pair["final_evaluation"]["evolved_scores"])
        grades = {
            comparison_name: (
                float(result["assessment"]["student_a"]["grade"]),
                float(result["assessment"]["student_b"]["grade"]),
            )
            for comparison_name, result in paper.items()
        }
        artifact_summary = sorted(
            (
                {
                    "type": artifact["artifact_type"],
                    "artifact_id": artifact["artifact_id"],
                    "job_id": artifact["job_id"],
                    "task_conflict": artifact["TASK_CONFLICT"] == "True",
                }
                for artifact in artifacts_by_task[task_id]
            ),
            key=lambda item: item["type"],
        )
        task_rows.append({
            "task_id": task_id,
            "internal_g1": internal_g1,
            "internal_g2": internal_g2,
            "internal_delta": internal_g2 - internal_g1,
            "blind_g1": blind_g1,
            "blind_g2": blind_g2,
            "blind_delta": blind_g2 - blind_g1,
            "blind_winner": pair["final_evaluation"]["winner"],
            "official_historical_chemcrow": official[task_id][0],
            "official_historical_gpt4": official[task_id][1],
            "judge_historical_chemcrow": grades["historical_control"][0],
            "judge_historical_gpt4_control": grades["historical_control"][1],
            "judge_g1": grades["baseline"][0],
            "judge_g1_gpt4_control": grades["baseline"][1],
            "judge_g2": grades["evolved"][0],
            "judge_g2_gpt4_control": grades["evolved"][1],
            "judge_g2_minus_g1": grades["evolved"][0] - grades["baseline"][0],
            "artifact_ids": ";".join(item["artifact_id"] for item in artifact_summary),
            "reflector_job_ids": ";".join(item["job_id"] for item in artifact_summary),
            "task_conflict_artifact_count": sum(item["task_conflict"] for item in artifact_summary),
        })

    column = lambda key: [float(row[key]) for row in task_rows]
    summaries = [
        comparison("Historical ChemCrow - GPT-4", "Official notebook EvaluatorGPT",
                   column("official_historical_chemcrow"), column("official_historical_gpt4")),
        comparison("Re-evaluated ChemCrow - GPT-4", "Calibrated GPT-4 Judge",
                   column("judge_historical_chemcrow"), column("judge_historical_gpt4_control")),
        comparison("G1 - GPT-4", "Calibrated GPT-4 Judge",
                   column("judge_g1"), column("judge_g1_gpt4_control")),
        comparison("G2 - GPT-4", "Calibrated GPT-4 Judge",
                   column("judge_g2"), column("judge_g2_gpt4_control")),
        comparison("G1 - historical ChemCrow", "Same judge, independent calls",
                   column("judge_g1"), column("judge_historical_chemcrow")),
        comparison("G2 - historical ChemCrow", "Same judge, independent calls",
                   column("judge_g2"), column("judge_historical_chemcrow")),
        comparison("G2 - G1", "Same calibrated judge, independent calls",
                   column("judge_g2"), column("judge_g1")),
        comparison("G2 - G1", "Internal blind evaluator /12",
                   column("blind_g2"), column("blind_g1")),
        comparison("G2 - G1", "Internal evolution evaluator /12",
                   column("internal_g2"), column("internal_g1")),
    ]
    blind_summary = next(
        row for row in summaries if row["source"] == "Internal blind evaluator /12"
    )
    blind_summary.update({
        "wins": sum(row["blind_winner"] == "evolved" for row in task_rows),
        "ties": sum(row["blind_winner"] == "tie" for row in task_rows),
        "losses": sum(row["blind_winner"] == "baseline" for row in task_rows),
    })
    for row in summaries:
        row["wtl"] = f"{row['wins']} / {row['ties']} / {row['losses']}"

    statistics = {
        "paper_g2_minus_g1": paired_stats(column("judge_g2_minus_g1")),
        "internal_blind_g2_minus_g1": paired_stats(column("blind_delta")),
        "internal_evolution_g2_minus_g1": paired_stats(column("internal_delta")),
    }
    analysis = {
        "schema_version": "chemcrow_full_v8_g2_gpt4_analysis_v1",
        "status": "PASS",
        "task_count": 14,
        "paper_valid_result_count": paper_aggregate["valid_target_result_count"],
        "paper_attempt_count": paper_aggregate["actual_upstream_call_count"],
        "paper_provider_response_count": paper_aggregate["provider_response_count"],
        "paper_actual_cost_usd": paper_aggregate["actual_openrouter_reported_cost_usd"],
        "paper_aggregate_status": paper_aggregate["status"],
        "paper_prompt_sha256": plan["prompt_candidate_sha256"],
        "paper_model": plan["model"],
        "paper_provider_only": plan["provider_only"],
        "paper_allow_fallbacks": plan["allow_fallbacks"],
        "excluded_invalid_assessment_count": paper_aggregate[
            "excluded_invalid_assessment_count"
        ],
        "excluded_pre_response_attempt_count": paper_aggregate[
            "excluded_pre_response_attempt_count"
        ],
        "run_audit_status": run_audit["status"],
        "run_aggregate_sha256": sha256(aggregate_path),
        "completed_run_audit_sha256": sha256(run_audit_path),
        "paper_plan_sha256": sha256(plan_path),
        "paper_aggregate_sha256": sha256(paper_aggregate_path),
        "summary_comparisons": summaries,
        "paired_statistics": statistics,
        "tasks": task_rows,
        "internal_aggregate": internal_aggregate,
    }
    (args.output_root / "analysis.json").write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_csv(args.output_root / "summary_comparisons.csv", summaries)
    write_csv(args.output_root / "per_task_results.csv", task_rows)

    summary_image_rows = [{
        "comparison": row["comparison"],
        "source": row["source"],
        "former": f"{row['former_mean']:.3f}",
        "latter": f"{row['latter_mean']:.3f}",
        "delta": f"{row['delta']:+.3f}",
        "wtl": row["wtl"],
        "count": str(row["count"]),
    } for row in summaries]
    draw_table(
        args.output_root / "summary_table.png",
        summary_image_rows,
        [("comparison", "Comparison"), ("source", "Scoring source"),
         ("former", "Former mean"), ("latter", "Latter mean"),
         ("delta", "Delta"), ("wtl", "Former W / T / L"), ("count", "N")],
        title="Full-v8 Core-full-worker: G2 and GPT-4 final summary",
        width=17.0,
        height=5.8,
        font_size=7.8,
    )
    per_task_image_rows = [{
        "task": row["task_id"].removeprefix("chemcrow-"),
        "ig1": f"{row['internal_g1']:.0f}", "ig2": f"{row['internal_g2']:.0f}",
        "id": f"{row['internal_delta']:+.0f}",
        "bg1": f"{row['blind_g1']:.0f}", "bg2": f"{row['blind_g2']:.0f}",
        "winner": row["blind_winner"],
        "hc": f"{row['judge_historical_chemcrow']:.0f}",
        "g1": f"{row['judge_g1']:.0f}", "g2": f"{row['judge_g2']:.0f}",
        "gd": f"{row['judge_g2_minus_g1']:+.0f}",
    } for row in task_rows]
    draw_table(
        args.output_root / "per_task_table.png",
        per_task_image_rows,
        [("task", "Task"), ("ig1", "Int G1"), ("ig2", "Int G2"),
         ("id", "Int Δ"), ("bg1", "Blind G1"), ("bg2", "Blind G2"),
         ("winner", "Blind winner"), ("hc", "GPT4 HC"),
         ("g1", "GPT4 G1"), ("g2", "GPT4 G2"), ("gd", "GPT4 Δ")],
        title="Per-task full-v8 results (internal /12; GPT-4 /10)",
        width=13.5,
        height=7.0,
        font_size=8.0,
    )

    summary_lines = [
        "# Full-v8 Core-full-worker G2 与 GPT-4 最终结果",
        "",
        "## 结论",
        "",
        (
            f"- 内部 evolution evaluator：G1 `{np.mean(column('internal_g1')):.3f}/12`，"
            f"G2 `{np.mean(column('internal_g2')):.3f}/12`，"
            f"变化 `{np.mean(column('internal_delta')):+.3f}`。"
        ),
        (
            f"- 内部盲评：G1 `{np.mean(column('blind_g1')):.3f}/12`，"
            f"G2 `{np.mean(column('blind_g2')):.3f}/12`，"
            f"变化 `{np.mean(column('blind_delta')):+.3f}`。"
        ),
        (
            f"- GPT-4：G1 `{np.mean(column('judge_g1')):.3f}/10`，"
            f"G2 `{np.mean(column('judge_g2')):.3f}/10`，"
            f"变化 `{np.mean(column('judge_g2_minus_g1')):+.3f}`，"
            f"G2 胜/平/负 `{statistics['paper_g2_minus_g1']['wins']}/"
            f"{statistics['paper_g2_minus_g1']['ties']}/"
            f"{statistics['paper_g2_minus_g1']['losses']}`。"
        ),
        (
            "- 因此长模板 G2 在内部指标上改善，但 paper-compatible GPT-4 没有确认该改善；"
            "在 N=14 下更合适的表述是基本持平、略向下。"
        ),
        "",
        "## 汇总表",
        "",
        "| 比较 | 评分来源 | 前者均分 | 后者均分 | 差值 | 前者胜/平/负 | N |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    summary_lines.extend(
        f"| {row['comparison']} | {row['source']} | {row['former_mean']:.3f} | "
        f"{row['latter_mean']:.3f} | {row['delta']:+.3f} | {row['wtl']} | {row['count']} |"
        for row in summaries
    )
    summary_lines.extend([
        "",
        (
            "`G1/G2 - historical ChemCrow` 两行是同一冻结 Judge 的独立调用后按任务配对，"
            "不是把两份答案放进同一个 prompt 的直接对局。`G2 - G1` 的 GPT-4 行同理。"
        ),
        (
            "盲评行的胜/平/负使用 evaluator 显式 winner 字段；任务 13 总分相同"
            "但 winner 为 evolved，因此与单纯按总分差计算的 `8/2/4` 不同。"
        ),
        "",
        "## 逐任务",
        "",
        "| 任务 | 内部 G1 | 内部 G2 | Δ | 盲评 G1 | 盲评 G2 | 盲评胜者 | GPT-4 历史 ChemCrow | GPT-4 G1 | GPT-4 G2 | G2-G1 | 冲突产物 |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|",
    ])
    summary_lines.extend(
        f"| {row['task_id'][-2:]} | {row['internal_g1']:.0f} | {row['internal_g2']:.0f} | "
        f"{row['internal_delta']:+.0f} | {row['blind_g1']:.0f} | {row['blind_g2']:.0f} | "
        f"{row['blind_winner']} | {row['judge_historical_chemcrow']:.0f} | "
        f"{row['judge_g1']:.0f} | {row['judge_g2']:.0f} | "
        f"{row['judge_g2_minus_g1']:+.0f} | {row['task_conflict_artifact_count']} |"
        for row in task_rows
    )
    paper_stats = statistics["paper_g2_minus_g1"]
    summary_lines.extend([
        "",
        "## 配对统计",
        "",
        (
            f"GPT-4 G2-G1 的 mean/median delta 为 `{paper_stats['mean_delta']:+.3f}` / "
            f"`{paper_stats['median_delta']:+.3f}`；paired bootstrap 95% CI "
            f"`[{paper_stats['paired_bootstrap_95_ci_mean_delta'][0]:+.3f}, "
            f"{paper_stats['paired_bootstrap_95_ci_mean_delta'][1]:+.3f}]`。"
        ),
        (
            f"Wilcoxon p=`{paper_stats['wilcoxon_signed_rank']['pvalue_two_sided']:.4f}`；"
            f"排除平局后的 exact sign-test p=`{paper_stats['exact_sign_test']['pvalue_two_sided']:.4f}`。"
            " 分数离散且平局很多，检验功效很低，不能据此声称显著退化。"
        ),
        "",
        "## 产物机制与任务 12/13",
        "",
        "- 长模板产物人工审核判定 2/14 个任务、6/42 个产物存在字面任务冲突，全部集中在任务 12/13。",
        "- 任务 12 的冲突来自化学武器/神经毒剂拒绝；这是字面上不完成请求，但安全机制本身合理。",
        (
            "- 任务 13 的三类产物把受监管药物的安全限制推广为拒绝具体路线、价格和 make-vs-buy 比较，"
            "与题目明确义务发生实质冲突，更接近过度保守。"
        ),
        (
            "- 尽管存在这些冲突，本轮 GPT-4 对任务 12/13 的 G2 分数不能单独证明冲突造成总体回归；"
            "总体 paper delta 主要由逐任务离散变化决定。"
        ),
        "",
        "## 账本与成本",
        "",
        "- 有效结果 `42/42`；实际 attempt `44`；provider response `43`。",
        "- 排除 1 个 schema 无效的付费输出，以及 1 个没有 provider response、没有费用字段的 HTTP 402。",
        (
            "- fresh-ID replacement `2`；原 call ID 复用 `false`；自动 provider retry `false`；"
            "score-driven retry `false`。"
        ),
        f"- 模型 `openai/gpt-4`；OpenAI-only；fallback `false`；实际成本 `${paper_aggregate['actual_openrouter_reported_cost_usd']:.5f}`。",
        (
            f"- 账本状态 `{paper_aggregate['status']}`：本文的“最终”表示已授权的 "
            "LLM Judge 调用全部完成，不代表已经人类评审确认。"
        ),
        f"- 冻结提示词 SHA256 `{plan['prompt_candidate_sha256']}`。",
        "",
        "## 文件",
        "",
        "- `summary_table.png`：与用户示例同结构的汇总图。",
        "- `per_task_table.png`：14 个任务逐项结果图。",
        "- `analysis.json`：全部原始派生值、统计和哈希。",
        "- `per_task_results.csv`：逐任务机器可读表。",
    ])
    (args.output_root / "README.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
