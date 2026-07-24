"""Closed DTOs for the ChemBench trust boundaries.

Only ``PublicPrompt``, ``SealedEvaluation``, and ``SafeEvolutionSignal`` expose
boundary-specific serialization methods. Private and raw-attempt DTOs are
intentionally redacted in logs and have no generic dump method.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


_CHOICE_LABEL = re.compile(r"[A-Z]+")


class _SensitiveDTO:
    """Redact sensitive DTOs from accidental logging and pickling."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: int) -> Any:
        del protocol
        raise TypeError(f"{type(self).__name__} cannot be pickled across trust boundaries")


def _require_non_empty_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_string_tuple(
    value: object,
    field_name: str,
    *,
    allow_empty: bool,
) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be an immutable tuple")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    for item in value:
        _require_non_empty_text(item, f"{field_name}[]")


@dataclass(frozen=True, slots=True, repr=False)
class PrivateTargetScore(_SensitiveDTO):
    """A private option-to-score binding; never enters an agent payload."""

    option: str
    score: float

    def __post_init__(self) -> None:
        _require_non_empty_text(self.option, "PrivateTargetScore.option")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("PrivateTargetScore.score must be numeric")
        if not math.isfinite(float(self.score)):
            raise ValueError("PrivateTargetScore.score must be finite")


@dataclass(frozen=True, slots=True, repr=False)
class PrivateTask(_SensitiveDTO):
    """Trusted ChemBench example with public and ground-truth fields co-located.

    Construction happens only inside the trusted dataset adapter. The only
    permitted outbound conversion is the explicit prompt adapter implemented
    in Step 2.
    """

    question: str
    options: tuple[str, ...]
    target: str
    target_scores: tuple[PrivateTargetScore, ...]
    metrics: tuple[str, ...]
    uuid: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.question, "PrivateTask.question")
        _require_string_tuple(self.options, "PrivateTask.options", allow_empty=True)
        if not isinstance(self.target, str):
            raise TypeError("PrivateTask.target must be a string")
        if not isinstance(self.target_scores, tuple):
            raise TypeError("PrivateTask.target_scores must be an immutable tuple")
        if not all(isinstance(item, PrivateTargetScore) for item in self.target_scores):
            raise TypeError("PrivateTask.target_scores must contain PrivateTargetScore values")
        _require_string_tuple(self.metrics, "PrivateTask.metrics", allow_empty=False)
        _require_non_empty_text(self.uuid, "PrivateTask.uuid")

        if len(self.options) != len(set(self.options)):
            raise ValueError("PrivateTask.options must not contain duplicates")
        score_options = tuple(item.option for item in self.target_scores)
        if len(score_options) != len(set(score_options)):
            raise ValueError("PrivateTask.target_scores must not contain duplicate options")
        if self.target_scores and set(score_options) != set(self.options):
            raise ValueError(
                "PrivateTask.options must exactly match private target-score options"
            )
        if not self.target.strip() and not self.target_scores:
            raise ValueError(
                "PrivateTask requires target text or private target-score bindings"
            )


@dataclass(frozen=True, slots=True)
class PublicChoice:
    """One deterministic public answer choice."""

    label: str
    text: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.label, "PublicChoice.label")
        _require_non_empty_text(self.text, "PublicChoice.text")
        if _CHOICE_LABEL.fullmatch(self.label) is None:
            raise ValueError("PublicChoice.label must contain uppercase ASCII letters only")


@dataclass(frozen=True, slots=True)
class PublicPrompt:
    """The complete and only DTO accepted by the future Codex rollout adapter."""

    question: str
    choices: tuple[PublicChoice, ...]
    answer_format: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.question, "PublicPrompt.question")
        if not isinstance(self.choices, tuple):
            raise TypeError("PublicPrompt.choices must be an immutable tuple")
        if not all(isinstance(choice, PublicChoice) for choice in self.choices):
            raise TypeError("PublicPrompt.choices must contain PublicChoice values")
        _require_non_empty_text(self.answer_format, "PublicPrompt.answer_format")

        labels = tuple(choice.label for choice in self.choices)
        if len(labels) != len(set(labels)):
            raise ValueError("PublicPrompt choice labels must be unique")

    def to_agent_payload(self) -> dict[str, object]:
        """Return the closed public payload allowed to cross into Agent Runtime."""

        return {
            "question": self.question,
            "choices": [
                {"label": choice.label, "text": choice.text} for choice in self.choices
            ],
            "answer_format": self.answer_format,
        }


@dataclass(frozen=True, slots=True, repr=False)
class TranscriptReference(_SensitiveDTO):
    """Opaque controller-owned reference to a raw Codex transcript."""

    reference: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.reference, "TranscriptReference.reference")


@dataclass(frozen=True, slots=True)
class AgentRuntimeMetadata:
    """Closed, answer-free execution counters returned by OpenEvo."""

    duration_ms: float
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    harness: str = "codex"
    model: str = "gpt-5.5"
    capture_mode: str = "transcript"
    execution_backend: str = "managed_codex"
    codex_cli_version: str | None = None
    started_at_utc: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, (int, float))
            or not math.isfinite(float(self.duration_ms))
            or float(self.duration_ms) < 0
        ):
            raise ValueError("AgentRuntimeMetadata.duration_ms must be finite and non-negative")
        for field_name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(
                    f"AgentRuntimeMetadata.{field_name} must be a non-negative integer or None"
                )
        if self.execution_backend not in {
            "managed_codex",
            "local_codex_cli",
        }:
            raise ValueError(
                "AgentRuntimeMetadata.execution_backend is unsupported"
            )
        expected_harness = (
            "codex"
            if self.execution_backend == "managed_codex"
            else "codex_cli"
        )
        if self.harness != expected_harness:
            raise ValueError(
                "AgentRuntimeMetadata.harness must agree with execution_backend"
            )
        if self.model != "gpt-5.5":
            raise ValueError("AgentRuntimeMetadata.model must be gpt-5.5")
        if self.capture_mode != "transcript":
            raise ValueError("AgentRuntimeMetadata.capture_mode must be transcript")
        if self.codex_cli_version is not None:
            _require_non_empty_text(
                self.codex_cli_version,
                "AgentRuntimeMetadata.codex_cli_version",
            )
        if self.started_at_utc is not None:
            _require_non_empty_text(
                self.started_at_utc,
                "AgentRuntimeMetadata.started_at_utc",
            )
        if self.execution_backend == "local_codex_cli" and (
            self.codex_cli_version is None or self.started_at_utc is None
        ):
            raise ValueError(
                "local Codex metadata requires CLI version and UTC timestamp"
            )

    def to_result_payload(self) -> dict[str, object]:
        """Serialize only closed runtime identity and numeric counters."""

        return {
            "duration_ms": float(self.duration_ms),
            "token_usage": {
                "input_tokens": self.input_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "output_tokens": self.output_tokens,
                "reasoning_output_tokens": self.reasoning_output_tokens,
            },
            "harness": self.harness,
            "model": self.model,
            "capture_mode": self.capture_mode,
            "execution_backend": self.execution_backend,
            "codex_cli_version": self.codex_cli_version,
            "started_at_utc": self.started_at_utc,
        }


@dataclass(frozen=True, slots=True, repr=False)
class RawAttempt(_SensitiveDTO):
    """Agent output with no benchmark ground truth."""

    response: str
    transcript_reference: TranscriptReference
    runtime_metadata: AgentRuntimeMetadata | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.response, str):
            raise TypeError("RawAttempt.response must be a string")
        if not isinstance(self.transcript_reference, TranscriptReference):
            raise TypeError(
                "RawAttempt.transcript_reference must be a TranscriptReference"
            )
        if (
            self.runtime_metadata is not None
            and type(self.runtime_metadata) is not AgentRuntimeMetadata
        ):
            raise TypeError(
                "RawAttempt.runtime_metadata must be exact AgentRuntimeMetadata or None"
            )


class CorrectnessBucket(str, Enum):
    """Coarse private outcome used for deterministic safe-feedback mapping."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNPARSEABLE = "unparseable"


class ParseStatus(str, Enum):
    """Closed parser result vocabulary with no response or answer content."""

    PARSED = "parsed"
    MISSING_ANSWER_TAG = "missing_answer_tag"
    EMPTY_ANSWER_TAG = "empty_answer_tag"
    MULTIPLE_ANSWER_TAGS = "multiple_answer_tags"
    INVALID_CHOICE_LABEL = "invalid_choice_label"


@dataclass(frozen=True, slots=True, repr=False)
class SealedEvaluation(_SensitiveDTO):
    """Private evaluator output that must never enter evolution directly."""

    metric_name: str
    score: float
    correctness_bucket: CorrectnessBucket
    parse_status: ParseStatus

    def __post_init__(self) -> None:
        if self.metric_name != "multiple_choice_grade":
            raise ValueError(
                "SealedEvaluation.metric_name must be multiple_choice_grade"
            )
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("SealedEvaluation.score must be numeric")
        if not math.isfinite(float(self.score)):
            raise ValueError("SealedEvaluation.score must be finite")
        if not isinstance(self.correctness_bucket, CorrectnessBucket):
            raise TypeError(
                "SealedEvaluation.correctness_bucket must be a CorrectnessBucket"
            )
        if not isinstance(self.parse_status, ParseStatus):
            raise TypeError("SealedEvaluation.parse_status must be a ParseStatus")
        parsed = self.parse_status is ParseStatus.PARSED
        unparseable = self.correctness_bucket is CorrectnessBucket.UNPARSEABLE
        if parsed == unparseable:
            raise ValueError(
                "SealedEvaluation parse status and correctness bucket are inconsistent"
            )

    def to_private_payload(self) -> dict[str, object]:
        """Serialize only the closed fields permitted in private statistics."""

        return {
            "metric_name": self.metric_name,
            "score": float(self.score),
            "correctness_bucket": self.correctness_bucket.value,
            "parse_status": self.parse_status.value,
        }


class SignalOutcome(str, Enum):
    """Closed, answer-free outcome vocabulary for evolution."""

    SATISFACTORY = "satisfactory"
    NEEDS_IMPROVEMENT = "needs_improvement"
    INCOMPLETE = "incomplete"


class FailureCategory(str, Enum):
    """Closed taxonomy; values carry no task text, answer, or score."""

    INCORRECT_SELECTION = "incorrect_selection"
    FORMAT_FAILURE = "format_failure"
    INSUFFICIENT_VERIFICATION = "insufficient_verification"
    CALCULATION_VERIFICATION = "calculation_verification"
    REASONING_CHECK = "reasoning_check"
    FORMAT_ISSUE = "format_issue"
    UNIT_CONSISTENCY = "unit_consistency"
    UNCERTAINTY_MANAGEMENT = "uncertainty_management"
    TOOL_USAGE = "tool_usage"
    TIMEOUT_OR_INCOMPLETE = "timeout_or_incomplete"
    INCORRECT_RESULT_UNSPECIFIED = "incorrect_result_unspecified"


class SignalSeverity(str, Enum):
    """Closed severity vocabulary; never carries a numeric benchmark score."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class SafeEvolutionSignal:
    """The only evaluator-derived DTO allowed into the evolution plane."""

    outcome: SignalOutcome
    failure_categories: tuple[FailureCategory, ...] = ()
    severity: SignalSeverity = SignalSeverity.NONE
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, SignalOutcome):
            raise TypeError("SafeEvolutionSignal.outcome must be a SignalOutcome")
        if not isinstance(self.failure_categories, tuple):
            raise TypeError(
                "SafeEvolutionSignal.failure_categories must be an immutable tuple"
            )
        if not all(
            isinstance(category, FailureCategory)
            for category in self.failure_categories
        ):
            raise TypeError(
                "SafeEvolutionSignal.failure_categories must contain FailureCategory values"
            )
        if len(self.failure_categories) != len(set(self.failure_categories)):
            raise ValueError("SafeEvolutionSignal.failure_categories must be unique")
        if not isinstance(self.severity, SignalSeverity):
            raise TypeError("SafeEvolutionSignal.severity must be a SignalSeverity")
        if self.schema_version != "1":
            raise ValueError("SafeEvolutionSignal.schema_version must be '1'")
        if (
            self.outcome is SignalOutcome.SATISFACTORY
            and (
                self.failure_categories
                or self.severity is not SignalSeverity.NONE
            )
        ):
            raise ValueError(
                "satisfactory SafeEvolutionSignal cannot contain failures or severity"
            )
        if (
            self.outcome is not SignalOutcome.SATISFACTORY
            and (
                not self.failure_categories
                or self.severity is SignalSeverity.NONE
            )
        ):
            raise ValueError(
                "non-satisfactory SafeEvolutionSignal requires failures and severity"
            )

    def to_evolution_payload(self) -> dict[str, object]:
        """Return the closed payload allowed to cross into the evolution plane."""

        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "failure_categories": [
                category.value for category in self.failure_categories
            ],
            "severity": self.severity.value,
        }


__all__ = [
    "AgentRuntimeMetadata",
    "CorrectnessBucket",
    "FailureCategory",
    "ParseStatus",
    "PrivateTargetScore",
    "PrivateTask",
    "PublicChoice",
    "PublicPrompt",
    "RawAttempt",
    "SafeEvolutionSignal",
    "SealedEvaluation",
    "SignalSeverity",
    "SignalOutcome",
    "TranscriptReference",
]
