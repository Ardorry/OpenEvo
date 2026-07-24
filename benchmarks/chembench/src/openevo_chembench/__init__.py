"""Security-isolated ChemBench automation for OpenEvo.

The package root intentionally performs no eager imports.  In particular, a
formal ChemBench4K v2 import must not initialize the legacy
``jablonkagroup/ChemBench`` loader, task-local reflector, or online runner.
Legacy public names remain available through PEP 562 lazy attribute loading.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


__version__ = "0.1.0"

_LAZY_EXPORTS = {
    "AgentExecutionError": "agent_executor",
    "AgentExecutionErrorCode": "agent_executor",
    "OpenEvoCodexExecutor": "agent_executor",
    "AgentSystemPayload": "artifacts",
    "ApprovedArtifact": "artifacts",
    "ArtifactFile": "artifacts",
    "ArtifactKind": "artifacts",
    "ArtifactLineage": "artifacts",
    "CandidateArtifact": "artifacts",
    "EvolutionArtifactSet": "artifacts",
    "SkillBundlePayload": "artifacts",
    "TextMemoryPayload": "artifacts",
    "ValidationDecision": "artifacts",
    "ValidationFinding": "artifacts",
    "ValidationReceipt": "artifacts",
    "CHEMBENCH_CONFIGS": "config",
    "AgentConfig": "config",
    "ChemBenchDatasetConfig": "config",
    "DebugExecutionConfig": "config",
    "EvolutionConfig": "config",
    "ExperimentConfig": "config",
    "LoadedDebugConfig": "config",
    "RuntimeIsolationConfig": "config",
    "load_debug_config": "config",
    "ChemBenchDatasetLoader": "dataset",
    "ChemBenchRowSource": "dataset",
    "DatasetFormatError": "dataset",
    "DatasetLoadError": "dataset",
    "DatasetProvenance": "dataset",
    "DatasetSnapshot": "dataset",
    "HuggingFaceRowSource": "dataset",
    "LoadedPrivateTasks": "dataset",
    "canonical_schema_hash": "dataset",
    "MULTIPLE_CHOICE_METRIC": "evaluator",
    "ChemBenchEvaluator": "evaluator",
    "EvaluationErrorType": "evaluator",
    "PrivateEvaluationError": "evaluator",
    "evaluate_attempt": "evaluator",
    "SafeEvolutionFeedback": "feedback",
    "generate_safe_evolution_signal": "feedback",
    "LocalCodexCLIExecutor": "local_codex_executor",
    "LocalCodexExecutionError": "local_codex_executor",
    "LocalCodexExecutionErrorCode": "local_codex_executor",
    "LocalCommandResult": "local_codex_executor",
    "LocalCodexPreflightReceipt": "local_preflight",
    "LocalPreflightFindingCode": "local_preflight",
    "run_local_codex_preflight": "local_preflight",
    "AgentRuntimeMetadata": "models",
    "CorrectnessBucket": "models",
    "FailureCategory": "models",
    "ParseStatus": "models",
    "PrivateTargetScore": "models",
    "PrivateTask": "models",
    "PublicChoice": "models",
    "PublicPrompt": "models",
    "RawAttempt": "models",
    "SafeEvolutionSignal": "models",
    "SealedEvaluation": "models",
    "SignalOutcome": "models",
    "SignalSeverity": "models",
    "TranscriptReference": "models",
    "FREE_RESPONSE_ANSWER_FORMAT": "prompt_adapter",
    "MULTIPLE_CHOICE_ANSWER_FORMAT": "prompt_adapter",
    "ChemBenchPromptAdapter": "prompt_adapter",
    "project_private_task": "prompt_adapter",
    "PreflightFindingCode": "preflight",
    "RealExecutionPreflightReceipt": "preflight",
    "run_real_execution_preflight": "preflight",
    "SafeEvolutionReflector": "reflector",
    "compute_source_signal_hash": "reflector",
    "reflect_safe_signal": "reflector",
    "AppliedArtifactReference": "reporting",
    "ArtifactHistoryRecord": "reporting",
    "EpisodeResult": "reporting",
    "EpisodeStatus": "reporting",
    "RoundResult": "reporting",
    "RoundStatus": "reporting",
    "RunManifest": "reporting",
    "RunMode": "reporting",
    "ChemBenchOnlineEvolutionRunner": "runner",
    "AgentArtifactContext": "runtime_context",
    "AgentContextFile": "runtime_context",
    "AgentExecutor": "runtime_context",
    "AgentRoundRequest": "runtime_context",
    "ArtifactContextResolver": "runtime_context",
    "VALIDATOR_VERSION": "security",
    "ArtifactValidationResult": "security",
    "EvolutionArtifactValidator": "security",
}

__all__ = sorted((*_LAZY_EXPORTS, "__version__"))


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
