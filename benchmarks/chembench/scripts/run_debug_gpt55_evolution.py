#!/usr/bin/env python3
"""Run the pinned ten-task baseline/evolution debug experiment after preflight."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import sys

from openevo_chembench.config import LoadedDebugConfig, load_debug_config
from openevo_chembench.dataset import ChemBenchDatasetLoader
from openevo_chembench.evaluator import ChemBenchEvaluator
from openevo_chembench.local_preflight import run_local_codex_preflight
from openevo_chembench.preflight import run_real_execution_preflight
from openevo_chembench.protocol_guard import audit_current_adapter_for_frozen_v2
from openevo_chembench.reporting import EpisodeResult
from openevo_chembench.runner import ChemBenchOnlineEvolutionRunner


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "configs" / "debug_gpt55_evolution.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fail-closed OpenEvo/Codex/GPT-5.5 ChemBench debug.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Closed debug YAML configuration.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run read-only launch gates without loading ChemBench or writing results.",
    )
    parser.add_argument(
        "--task-count",
        type=int,
        choices=(1, 10),
        help="Run the one-task gate or the pinned ten-task debug set (default: 10).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_debug_config(args.config.resolve())
        protocol_receipt = audit_current_adapter_for_frozen_v2(loaded.experiment)
        if not protocol_receipt.passed:
            _emit(
                {
                    "status": "blocked",
                    "protocol_gate": protocol_receipt.to_audit_payload(),
                    "dataset_loaded": False,
                    "results_written": False,
                    "model_calls_made": 0,
                }
            )
            return 2
        task_count = (
            loaded.execution.task_count
            if args.task_count is None
            else args.task_count
        )
        run_label = "single_task" if task_count == 1 else "ten_task"
        output_target = (
            REPOSITORY_ROOT
            / loaded.execution.result_root
            / run_label
        )
        if loaded.experiment.execution_backend == "local_codex_cli":
            receipt = run_local_codex_preflight(
                config=loaded.experiment,
                execution=loaded.execution,
                repository_root=REPOSITORY_ROOT,
                output_target=output_target,
            )
        else:
            receipt = run_real_execution_preflight(
                config=loaded.experiment,
                execution=loaded.execution,
                repository_root=REPOSITORY_ROOT,
            )
    except Exception as exc:
        _emit(
            {
                "status": "preflight_error",
                "error_type": type(exc).__name__,
            }
        )
        return 2

    preflight_payload = receipt.to_audit_payload()
    if args.preflight_only or not receipt.passed:
        _emit(
            {
                "status": "ready" if receipt.passed else "blocked",
                "preflight": preflight_payload,
                "dataset_loaded": False,
                "results_written": False,
            }
        )
        return 0 if receipt.passed else 2

    try:
        summary = _run_debug(loaded, task_count=task_count)
    except Exception as exc:
        _emit(
            {
                "status": "execution_failed",
                "error_type": type(exc).__name__,
                "preflight": preflight_payload,
            }
        )
        return 3
    _emit(
        {
            "status": "completed",
            "preflight": preflight_payload,
            "summary": summary,
        }
    )
    return 0


def _run_debug(
    loaded: LoadedDebugConfig,
    *,
    task_count: int,
) -> dict[str, object]:
    if task_count not in {1, 10}:
        raise ValueError("debug task_count must be 1 or 10")
    experiment = loaded.experiment
    execution = loaded.execution
    run_label = "single_task" if task_count == 1 else "ten_task"
    result_root = REPOSITORY_ROOT / execution.result_root / run_label
    if result_root.exists():
        raise FileExistsError("debug result root already exists")

    loader = ChemBenchDatasetLoader(
        revision=experiment.chembench_revision,
        config=experiment.dataset,
    )
    loaded_tasks = loader.load_config(execution.config_name)
    evaluator = ChemBenchEvaluator()
    eligible_tasks = tuple(
        task for task in loaded_tasks.tasks if evaluator.supports_task(task)
    )
    if len(eligible_tasks) < task_count:
        raise RuntimeError(
            "pinned config contains fewer evaluator-compatible tasks than requested"
        )
    tasks = eligible_tasks[:task_count]

    baseline_config = replace(
        experiment,
        evolution=replace(experiment.evolution, max_rounds=0),
    )
    baseline_results = _run_mode(
        config=baseline_config,
        tasks=tasks,
        config_name=execution.config_name,
        result_root=result_root / "baseline",
        loaded=loaded,
    )
    evolution_results = _run_mode(
        config=experiment,
        tasks=tasks,
        config_name=execution.config_name,
        result_root=result_root / "evolution",
        loaded=loaded,
    )

    baseline = _summarize_episodes(baseline_results)
    evolution = _summarize_episodes(evolution_results)
    summary: dict[str, object] = {
        "schema_version": 1,
        "task_count": task_count,
        "source_atomic_task_count": len(loaded_tasks.tasks),
        "eligible_atomic_task_count": len(eligible_tasks),
        "excluded_atomic_task_count": (
            len(loaded_tasks.tasks) - len(eligible_tasks)
        ),
        "baseline": baseline,
        "evolution": evolution,
        "final_round_accuracy_improvement": (
            float(evolution["final_round_accuracy"])
            - float(baseline["final_round_accuracy"])
        ),
    }
    _write_json_exclusive(result_root / "debug_summary.json", summary)
    return summary


def _run_mode(
    *,
    config,
    tasks,
    config_name: str,
    result_root: Path,
    loaded: LoadedDebugConfig,
) -> tuple[EpisodeResult, ...]:
    execution = loaded.execution
    runner, executor = ChemBenchOnlineEvolutionRunner.with_configured_executor(
        config=config,
        result_root=result_root,
        rollout_url=execution.rollout_url,
        task_timeout_seconds=execution.task_timeout_seconds,
        poll_interval_seconds=execution.poll_interval_seconds,
        max_poll_attempts=execution.max_poll_attempts,
    )
    try:
        return tuple(
            runner.run_episode(task, config_name=config_name)
            for task in tasks
        )
    finally:
        executor.close()


def _summarize_episodes(
    episodes: tuple[EpisodeResult, ...],
) -> dict[str, object]:
    if not episodes:
        raise ValueError("debug summary requires episodes")
    final_scores = [episode.final_score for episode in episodes]
    round_zero_scores = [episode.rounds[0].score for episode in episodes]
    all_rounds = [
        round_result
        for episode in episodes
        for round_result in episode.rounds
    ]
    runtime_rows = [
        round_result.runtime_metadata
        for round_result in all_rounds
        if round_result.runtime_metadata is not None
    ]
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    token_usage = {
        field_name: sum(
            int(getattr(metadata, field_name))
            for metadata in runtime_rows
            if getattr(metadata, field_name) is not None
        )
        for field_name in token_fields
    }
    return {
        "run_id": episodes[0].run_id,
        "episode_count": len(episodes),
        "round_count": len(all_rounds),
        "round_zero_accuracy": sum(round_zero_scores) / len(round_zero_scores),
        "final_round_accuracy": sum(final_scores) / len(final_scores),
        "runtime_ms": sum(float(metadata.duration_ms) for metadata in runtime_rows),
        "runtime_metadata_rounds": len(runtime_rows),
        "token_usage": token_usage,
        "token_usage_rounds": sum(
            all(getattr(metadata, field_name) is not None for field_name in token_fields)
            for metadata in runtime_rows
        ),
    }


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
