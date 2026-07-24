#!/usr/bin/env python3
"""Run one revision-pinned full ChemBench mode after fail-closed preflight."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys

from openevo_chembench.dataset import ChemBenchDatasetLoader
from openevo_chembench.evaluator import ChemBenchEvaluator
from openevo_chembench.formal_config import LoadedFormalConfig, load_formal_config
from openevo_chembench.local_preflight import run_local_codex_preflight
from openevo_chembench.preflight import run_real_execution_preflight
from openevo_chembench.protocol_guard import audit_current_adapter_for_frozen_v2
from openevo_chembench.reporting import EpisodeResult
from openevo_chembench.runner import ChemBenchOnlineEvolutionRunner


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate launch gates without loading ChemBench or writing results.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_formal_config(args.config.resolve())
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
        output_target = REPOSITORY_ROOT / loaded.execution.result_root
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
        _emit({"status": "preflight_error", "error_type": type(exc).__name__})
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
        summary = _run_formal(loaded)
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


def _run_formal(loaded: LoadedFormalConfig) -> dict[str, object]:
    experiment = loaded.experiment
    execution = loaded.execution
    result_root = REPOSITORY_ROOT / execution.result_root
    if result_root.exists():
        raise FileExistsError("formal result root already exists")

    loader = ChemBenchDatasetLoader(
        revision=experiment.chembench_revision,
        config=experiment.dataset,
    )
    runner, executor = ChemBenchOnlineEvolutionRunner.with_configured_executor(
        config=experiment,
        result_root=result_root,
        rollout_url=execution.rollout_url,
        task_timeout_seconds=execution.task_timeout_seconds,
        poll_interval_seconds=execution.poll_interval_seconds,
        max_poll_attempts=execution.max_poll_attempts,
    )
    accumulator = _RunAccumulator()
    evaluator = ChemBenchEvaluator()
    config_summaries: list[dict[str, object]] = []
    try:
        for config_name in experiment.dataset.configurations:
            loaded_tasks = loader.load_config(config_name)
            eligible_tasks = tuple(
                task
                for task in loaded_tasks.tasks
                if evaluator.supports_task(task)
            )
            config_accumulator = _RunAccumulator()
            for task in eligible_tasks:
                episode = runner.run_episode(task, config_name=config_name)
                accumulator.add(episode)
                config_accumulator.add(episode)
            config_summaries.append(
                {
                    "config_name": config_name,
                    "schema_hash": loaded_tasks.provenance.schema_hash,
                    "source_atomic_task_count": len(loaded_tasks.tasks),
                    "eligible_atomic_task_count": len(eligible_tasks),
                    "excluded_atomic_task_count": (
                        len(loaded_tasks.tasks) - len(eligible_tasks)
                    ),
                    **config_accumulator.to_payload(),
                }
            )
    finally:
        executor.close()

    summary: dict[str, object] = {
        "schema_version": 1,
        "run_id": runner.run_id,
        "mode": runner.mode.value,
        "dataset_revision": experiment.chembench_revision,
        "configurations": config_summaries,
        **accumulator.to_payload(),
    }
    _write_json_exclusive(result_root / "formal_summary.json", summary)
    return summary


class _RunAccumulator:
    __slots__ = (
        "artifact_count",
        "episode_count",
        "final_score_sum",
        "round_count",
        "round_zero_score_sum",
        "runtime_ms",
        "token_usage",
        "token_usage_rounds",
        "validator_passed",
        "validator_rejected",
    )

    def __init__(self) -> None:
        self.episode_count = 0
        self.round_count = 0
        self.round_zero_score_sum = 0.0
        self.final_score_sum = 0.0
        self.runtime_ms = 0.0
        self.token_usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }
        self.token_usage_rounds = 0
        self.artifact_count = 0
        self.validator_passed = 0
        self.validator_rejected = 0

    def add(self, episode: EpisodeResult) -> None:
        if type(episode) is not EpisodeResult:
            raise TypeError("formal accumulator requires exact EpisodeResult")
        self.episode_count += 1
        self.round_count += len(episode.rounds)
        self.round_zero_score_sum += float(episode.rounds[0].score)
        self.final_score_sum += float(episode.final_score)
        self.artifact_count += sum(
            record.approved_artifact_hash is not None
            for record in episode.artifact_history
        )
        for round_result in episode.rounds:
            for receipt in round_result.validator_receipts:
                if receipt.passed:
                    self.validator_passed += 1
                else:
                    self.validator_rejected += 1
            metadata = round_result.runtime_metadata
            if metadata is None:
                continue
            self.runtime_ms += float(metadata.duration_ms)
            values = {
                field: getattr(metadata, field)
                for field in self.token_usage
            }
            if all(value is not None for value in values.values()):
                self.token_usage_rounds += 1
            for field, value in values.items():
                if value is not None:
                    self.token_usage[field] += int(value)

    def to_payload(self) -> dict[str, object]:
        if self.episode_count == 0:
            return {
                "episode_count": 0,
                "round_count": 0,
                "round_zero_accuracy": None,
                "final_round_accuracy": None,
                "runtime_ms": 0.0,
                "token_usage": dict(self.token_usage),
                "token_usage_rounds": 0,
                "artifact_count": 0,
                "validator": {"passed": 0, "rejected": 0},
            }
        return {
            "episode_count": self.episode_count,
            "round_count": self.round_count,
            "round_zero_accuracy": self.round_zero_score_sum / self.episode_count,
            "final_round_accuracy": self.final_score_sum / self.episode_count,
            "runtime_ms": self.runtime_ms,
            "token_usage": dict(self.token_usage),
            "token_usage_rounds": self.token_usage_rounds,
            "artifact_count": self.artifact_count,
            "validator": {
                "passed": self.validator_passed,
                "rejected": self.validator_rejected,
            },
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
