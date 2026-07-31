"""Compile one native triple-artifact cycle from the verified Core registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import MANAGED_CODEX_MODEL, MANAGED_RUNTIME_IMAGE, ExperimentConfig
from .reflector_runner import NATIVE_METHODS


def native_experiment_payload(
    config: ExperimentConfig,
    *,
    task_id: str,
    instruction: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "experiment": {"name": config.require("experiment_id")},
        "agent": {
            "preset": "codex",
            "model": MANAGED_CODEX_MODEL,
            "auth": "subscription",
            "provider": "codex_cli",
            "settings": {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "reasoning_effort": config.require("candidate.reasoning_level"),
            },
            "env": {},
        },
        "tasks": [{"id": task_id, "instruction": instruction, "metadata": {}}],
        "runtime": {
            "kind": "docker",
            "profile": "managed_science",
            "container_user": "host",
            "workdir": "/openevo/session/workspace",
            "image": MANAGED_RUNTIME_IMAGE,
            "env": {
                "HOME": "/openevo/session/home",
                "PATH": (
                    "/opt/codex/bin:/usr/local/sbin:/usr/local/bin:"
                    "/usr/sbin:/usr/bin:/sbin:/bin"
                ),
            },
            "prepare": [],
        },
        "rollout": {"url": config.rollout_url},
        "evolution": {
            "backend_url": config.evolution_url,
            "rounds": 1,
            "worker": {"mode": "local_once"},
            "promotion_gate": {
                "mode": "human",
                "human_input": "file",
                "artifact_types": list(NATIVE_METHODS),
                "review_dir": str(config.experiment_root / "evaluator_private/native_admission"),
                "decision_dir": str(config.experiment_root / "manifests/native_admission"),
                "require_support": True,
            },
            "targets": {
                "agent_system": {
                    "enabled": True,
                    "method": NATIVE_METHODS["agent_system"],
                    "config": {},
                },
                "text_memory": {
                    "enabled": True,
                    "method": NATIVE_METHODS["text_memory"],
                    "config": {},
                },
                "skill_bundle": {
                    "enabled": True,
                    "method": NATIVE_METHODS["skill_bundle"],
                    "config": {},
                },
                "parametric_memory": {"enabled": False, "method": None, "config": {}},
            },
        },
    }


def compile_native_cycle(
    config: ExperimentConfig,
    *,
    task_id: str,
    instruction: str,
    run_id: str,
    framework_lock: str | Path | None = None,
) -> Any:
    """Use only a wheel-verified registry; never invent a registry snapshot."""

    from openevo.evolution.framework.profiles import execution_profile_for_release_mode
    from openevo.evolution.framework.runtime import load_verified_framework_registry
    from openevo.experiments.compiler import compile_experiment
    from openevo.experiments.models import ExperimentConfig as NativeExperimentConfig

    lock = Path(framework_lock or config.require("native_openevo.framework_lock"))
    verified = load_verified_framework_registry(lock)
    native = NativeExperimentConfig.model_validate(native_experiment_payload(
        config,
        task_id=task_id,
        instruction=instruction,
    ))
    compiled = compile_experiment(
        native,
        task_ids=[task_id],
        rounds_override=1,
        run_id=run_id,
        registry_snapshot=verified.snapshot,
        execution_profile=execution_profile_for_release_mode(
            "codex_subscription_transcript"
        ),
    )
    methods = compiled.evolution_methods_for_round(
        0,
        prior_dataset_artifact_ids=(),
        task_id=task_id,
    )
    actual = {method.artifact_type: method.method for method in methods}
    if actual != NATIVE_METHODS:
        raise ValueError(f"verified native method plan drifted: {actual}")
    return compiled
