"""Exactly-once evaluator authority for billed Community Judge operations.

The scorer is an external side effect.  A local receipt written only after the
scorer returns cannot distinguish "not called" from "called, response lost".
This module therefore commits an invocation fence *before* calling the scorer.
An interrupted invocation is terminally ambiguous and is never repeated.

The authoritative raw/private response and the closed public receipt are
committed in one SQLite transaction.  JSON files are immutable read-through
mirrors of that transaction, not completion authorities.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
UNAVAILABLE = "unavailable"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODEL_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SECRET_KEYS = {
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "judge_api_key",
    "api_key",
    "secret",
    "token",
}
_GLOBAL_PRIVATE_FRAGMENTS = {
    "checklist",
    "checklist_keywords",
    "checklist_weights",
    "judge_reasoning",
    "per_item_score",
    "raw_judge_request",
    "raw_judge_response",
    "rubric_mode",
    "target_images",
    "target_paper",
    "task_answer",
}


class DurableEvaluatorError(RuntimeError):
    """A durable evaluator contract failed closed."""


class EvaluationIdentityConflict(DurableEvaluatorError):
    """An idempotency key was reused with different immutable input."""


class AmbiguousJudgeInvocation(DurableEvaluatorError):
    """The Judge may have run; exactly-once policy forbids another call."""


class EvaluationIntegrityError(DurableEvaluatorError):
    """Persisted evaluator authority or a result failed integrity checks."""


class JudgeConfigurationError(DurableEvaluatorError):
    """The non-secret Judge runtime identity differs from protocol."""


class EvaluationPhase(str, Enum):
    PLANNED = "PLANNED"
    INVOCATION_COMMITTED = "INVOCATION_COMMITTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED_AMBIGUOUS = "FAILED_AMBIGUOUS"
    FAILED_TERMINAL = "FAILED_TERMINAL"


class FeedbackClass(str, Enum):
    HARD_GT = "HARD_GT"
    SOFT_JUDGE = "SOFT_JUDGE"
    MIXED = "MIXED"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationIntegrityError("evaluator value is not closed JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise JudgeConfigurationError(f"{label} must be a SHA-256 digest")
    return value


def _walk_json(value: Any, *, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise EvaluationIntegrityError("evaluator mappings require string keys")
            yield from _walk_json(child, path=(*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, path=(*path, str(index)))


def _reject_secret_keys(value: Any) -> None:
    for path, _child in _walk_json(value):
        if path and path[-1].casefold() in _SECRET_KEYS:
            raise EvaluationIntegrityError("evaluator payload contains a secret field")


def _reject_secret_values(value: Any, secret_values: Iterable[str]) -> None:
    encoded = _canonical(value)
    for secret in secret_values:
        if not isinstance(secret, str) or not secret:
            continue
        if secret.encode("utf-8") in encoded:
            raise EvaluationIntegrityError("evaluator payload contains secret material")


def redact_text(value: str, secret_values: Iterable[str]) -> str:
    """Redact exact evaluator-only secret values without logging identities."""

    result = value
    for secret in secret_values:
        if isinstance(secret, str) and secret:
            result = result.replace(secret, "[REDACTED]")
    return result


@dataclass(frozen=True)
class JudgePolicyIdentity:
    provider: str
    api_base: str
    model: str
    model_source: str
    runs_per_attempt: int
    scorer_commit: str
    scorer_sha256: str

    @classmethod
    def from_protocol(
        cls,
        judge: Mapping[str, Any],
        *,
        scorer_sha256: str,
    ) -> JudgePolicyIdentity:
        if not isinstance(judge, Mapping):
            raise JudgeConfigurationError("judge protocol must be an object")
        provider = judge.get("provider")
        model = judge.get("model")
        model_source = judge.get("model_source")
        api_base = judge.get("api_base", OPENROUTER_API_BASE)
        runs = judge.get("runs_per_attempt")
        scorer_commit = judge.get("scorer_commit")
        if (
            judge.get("api_key_env") != "JUDGE_API_KEY"
            or judge.get("api_base_env") != "JUDGE_API_BASE"
            or judge.get("model_env") != "JUDGE_MODEL_NAME"
        ):
            raise JudgeConfigurationError("Judge environment variable contract differs")
        if provider != "openai_compatible":
            raise JudgeConfigurationError("Judge provider must be openai_compatible")
        if model_source != "OpenRouter_model_catalog":
            raise JudgeConfigurationError("Judge model source must be OpenRouter_model_catalog")
        if not isinstance(model, str) or not _MODEL_SLUG.fullmatch(model):
            raise JudgeConfigurationError("Judge model must preserve the OpenRouter slug")
        if api_base != OPENROUTER_API_BASE:
            raise JudgeConfigurationError("Judge API base differs from the OpenRouter pin")
        if runs != 1:
            raise JudgeConfigurationError("Judge runs_per_attempt must be exactly one")
        if not isinstance(scorer_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", scorer_commit):
            raise JudgeConfigurationError("Judge scorer commit is invalid")
        return cls(
            provider=provider,
            api_base=api_base,
            model=model,
            model_source=model_source,
            runs_per_attempt=runs,
            scorer_commit=scorer_commit,
            scorer_sha256=_require_sha256(scorer_sha256, "scorer_sha256"),
        )

    @property
    def policy_sha256(self) -> str:
        return _digest(
            {
                "api_base": self.api_base,
                "model": self.model,
                "model_source": self.model_source,
                "provider": self.provider,
                "runs_per_attempt": self.runs_per_attempt,
                "scorer_commit": self.scorer_commit,
                "scorer_sha256": self.scorer_sha256,
            }
        )

    @property
    def model_sha256(self) -> str:
        return _digest({"model": self.model, "model_source": self.model_source})

    @property
    def provider_sha256(self) -> str:
        return _digest({"api_base": self.api_base, "provider": self.provider})

    def validate_runtime_environment(self, environ: Mapping[str, str]) -> dict[str, Any]:
        """Return a non-secret equality receipt for the evaluator-only process."""

        key_present = bool(environ.get("JUDGE_API_KEY"))
        base_present = bool(environ.get("JUDGE_API_BASE"))
        model_present = bool(environ.get("JUDGE_MODEL_NAME"))
        if not (key_present and base_present and model_present):
            raise JudgeConfigurationError("Judge environment is incomplete")
        if environ["JUDGE_API_BASE"] != self.api_base:
            raise JudgeConfigurationError("Judge API base differs from protocol")
        if environ["JUDGE_MODEL_NAME"] != self.model:
            raise JudgeConfigurationError("Judge model differs from protocol")
        return {
            "api_key_present": True,
            "api_base_present": True,
            "model_present": True,
            "api_base": self.api_base,
            "model": self.model,
            "provider": self.provider,
            "model_slug_preserved": True,
            "secret_recorded": False,
        }


@dataclass(frozen=True)
class JudgeRuntimeIdentity:
    provider: str
    api_base: str
    model: str
    api_key_present: bool

    def validate(self, policy: JudgePolicyIdentity) -> None:
        if (
            self.provider != policy.provider
            or self.api_base != policy.api_base
            or self.model != policy.model
            or self.api_key_present is not True
        ):
            raise JudgeConfigurationError("Judge runtime identity differs from protocol")


@dataclass(frozen=True)
class JudgeExecutionOutcome:
    raw_score: dict[str, Any]
    runtime_identity: JudgeRuntimeIdentity
    request_count: int | str
    usage: dict[str, Any] | str
    cost_total_usd: float | str

    def validate(self, policy: JudgePolicyIdentity) -> None:
        self.runtime_identity.validate(policy)
        if not isinstance(self.raw_score, dict):
            raise EvaluationIntegrityError("Judge raw score must be an object")
        if "error" in self.raw_score:
            raise EvaluationIntegrityError("Judge raw score reports an error")
        score = self.raw_score.get("total_score")
        if type(score) not in {int, float} or not 0 <= float(score) <= 100:
            raise EvaluationIntegrityError("Judge total_score is invalid")
        if not (
            (type(self.request_count) is int and self.request_count >= 0)
            or self.request_count == UNAVAILABLE
        ):
            raise EvaluationIntegrityError("Judge request_count must be measured or unavailable")
        if not (isinstance(self.usage, dict) or self.usage == UNAVAILABLE):
            raise EvaluationIntegrityError("Judge usage must be measured or unavailable")
        if isinstance(self.usage, dict):
            _reject_secret_keys(self.usage)
        if not (
            (type(self.cost_total_usd) in {int, float} and float(self.cost_total_usd) >= 0)
            or self.cost_total_usd == UNAVAILABLE
        ):
            raise EvaluationIntegrityError("Judge cost must be measured or unavailable")


@dataclass(frozen=True)
class FeedbackPartition:
    feedback_class: FeedbackClass
    global_feedback: dict[str, Any]
    task_local_feedback: dict[str, Any]

    def validate(self, *, task_specific_literals: Iterable[str] = ()) -> None:
        if not isinstance(self.global_feedback, dict) or not isinstance(
            self.task_local_feedback, dict
        ):
            raise EvaluationIntegrityError("feedback partitions must be objects")
        _reject_secret_keys(self.global_feedback)
        _reject_secret_keys(self.task_local_feedback)
        for path, _value in _walk_json(self.global_feedback):
            normalized = path[-1].casefold().replace("-", "_") if path else ""
            if path and any(fragment in normalized for fragment in _GLOBAL_PRIVATE_FRAGMENTS):
                raise EvaluationIntegrityError("private evaluator content entered global feedback")
        # Closed routing metadata may identify the task/attempt.  It is not
        # artifact learning content and is removed before the task-literal
        # admission scan.
        learned_global = {
            key: value
            for key, value in self.global_feedback.items()
            if key not in {"task_id", "attempt_id"}
        }
        global_bytes = _canonical(learned_global)
        for literal in task_specific_literals:
            if isinstance(literal, str) and literal and literal.encode("utf-8") in global_bytes:
                raise EvaluationIntegrityError("task-specific content entered global feedback")
        if self.feedback_class is FeedbackClass.SOFT_JUDGE and self.task_local_feedback:
            raise EvaluationIntegrityError("SOFT_JUDGE task-local feedback must be empty")
        if (
            self.feedback_class in {FeedbackClass.HARD_GT, FeedbackClass.MIXED}
            and not self.task_local_feedback
        ):
            raise EvaluationIntegrityError("hard feedback requires a task-local partition")


@dataclass(frozen=True)
class JudgeInvocation:
    operation_id: str
    idempotency_key: str
    request_sha256: str
    policy_sha256: str
    model: str
    provider: str
    api_base: str
    scorer_commit: str
    scorer_sha256: str


class JudgeExecutor(Protocol):
    def __call__(
        self, request: dict[str, Any], invocation: JudgeInvocation
    ) -> JudgeExecutionOutcome: ...


class FeedbackPartitioner(Protocol):
    def __call__(
        self,
        request: dict[str, Any],
        outcome: JudgeExecutionOutcome,
    ) -> FeedbackPartition: ...


@dataclass(frozen=True)
class DurableEvaluationResult:
    operation_id: str
    idempotency_key: str
    public_receipt: dict[str, Any]
    public_receipt_sha256: str
    raw_response_sha256: str
    private_feedback_authority_id: str
    private_feedback_sha256: str
    recovered: bool


def soft_judge_partitioner(
    request: dict[str, Any], outcome: JudgeExecutionOutcome
) -> FeedbackPartition:
    """Build the strict public Community score boundary."""

    runtime_seconds = request.get("candidate_runtime_seconds")
    if type(runtime_seconds) not in {int, float} or float(runtime_seconds) < 0:
        raise EvaluationIntegrityError("candidate runtime is invalid")
    tags = request.get("generic_failure_tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise EvaluationIntegrityError("generic failure tags are invalid")
    return FeedbackPartition(
        feedback_class=FeedbackClass.SOFT_JUDGE,
        global_feedback={
            "task_id": request["task_id"],
            "attempt_id": request["attempt_id"],
            "completed": True,
            "exit_code": 0,
            "artifact_valid": True,
            "total_score": float(outcome.raw_score["total_score"]),
            "generic_failure_tags": tags,
            "runtime_bucket_seconds": ((int(runtime_seconds) + 59) // 60) * 60,
            "cost_total_usd": outcome.cost_total_usd,
            "artifact_root_sha256": request["artifact_root_sha256"],
        },
        task_local_feedback={},
    )


class DurableEvaluatorStore:
    """SQLite evaluator authority shared safely across supervisor processes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.stat(self.root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or self.root.is_symlink()
        ):
            raise EvaluationIntegrityError("evaluator authority root is unsafe")
        os.chmod(self.root, 0o700)
        self.private_root = self.root / "private"
        self.public_root = self.root / "public"
        for child in (self.private_root, self.public_root):
            child.mkdir(mode=0o700, exist_ok=True)
            os.chmod(child, 0o700)
        self.db_path = self.root / "durable-evaluator.sqlite3"
        descriptor = os.open(
            self.db_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
        ):
            os.close(descriptor)
            raise EvaluationIntegrityError("evaluator database is unsafe")
        os.close(descriptor)
        os.chmod(self.db_path, 0o600)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evaluator_operations (
                    idempotency_key TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    model_sha256 TEXT NOT NULL,
                    provider_sha256 TEXT NOT NULL,
                    scorer_sha256 TEXT NOT NULL,
                    request_json BLOB NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN (
                        'PLANNED', 'INVOCATION_COMMITTED', 'SUCCEEDED',
                        'FAILED_AMBIGUOUS', 'FAILED_TERMINAL'
                    )),
                    invocation_committed_at TEXT,
                    completed_at TEXT,
                    raw_response_json BLOB,
                    raw_response_sha256 TEXT,
                    private_feedback_json BLOB,
                    private_feedback_sha256 TEXT,
                    public_receipt_json BLOB,
                    public_receipt_sha256 TEXT,
                    failure_code TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def begin(
        self,
        *,
        idempotency_key: str,
        operation_id: str,
        request: dict[str, Any],
        policy: JudgePolicyIdentity,
    ) -> dict[str, Any]:
        if not _IDENTITY.fullmatch(idempotency_key) or not _IDENTITY.fullmatch(operation_id):
            raise EvaluationIdentityConflict("evaluator operation identity is invalid")
        identities = {
            "request_sha256": _digest(request),
            "policy_sha256": policy.policy_sha256,
            "model_sha256": policy.model_sha256,
            "provider_sha256": policy.provider_sha256,
            "scorer_sha256": policy.scorer_sha256,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM evaluator_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO evaluator_operations("
                    "idempotency_key, operation_id, request_sha256, policy_sha256, "
                    "model_sha256, provider_sha256, scorer_sha256, request_json, phase, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?)",
                    (
                        idempotency_key,
                        operation_id,
                        identities["request_sha256"],
                        identities["policy_sha256"],
                        identities["model_sha256"],
                        identities["provider_sha256"],
                        identities["scorer_sha256"],
                        _canonical(request),
                        _now(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM evaluator_operations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
            connection.commit()
        assert row is not None
        if row["operation_id"] != operation_id or any(
            row[key] != expected for key, expected in identities.items()
        ):
            raise EvaluationIdentityConflict("evaluator idempotency identity conflicts")
        if bytes(row["request_json"]) != _canonical(request):
            raise EvaluationIdentityConflict("evaluator request content conflicts")
        return dict(row)

    def commit_invocation(self, idempotency_key: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM evaluator_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise EvaluationIntegrityError("evaluator intent is missing")
            if row["phase"] == EvaluationPhase.PLANNED.value:
                connection.execute(
                    "UPDATE evaluator_operations SET phase = ?, invocation_committed_at = ? "
                    "WHERE idempotency_key = ? AND phase = ?",
                    (
                        EvaluationPhase.INVOCATION_COMMITTED.value,
                        _now(),
                        idempotency_key,
                        EvaluationPhase.PLANNED.value,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM evaluator_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return dict(row)

    def complete(
        self,
        *,
        idempotency_key: str,
        raw_response: dict[str, Any],
        private_feedback: dict[str, Any],
        public_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        raw_bytes = _canonical(raw_response)
        feedback_bytes = _canonical(private_feedback)
        public_bytes = _canonical(public_receipt)
        hashes = {
            "raw_response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "private_feedback_sha256": hashlib.sha256(feedback_bytes).hexdigest(),
            "public_receipt_sha256": hashlib.sha256(public_bytes).hexdigest(),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM evaluator_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise EvaluationIntegrityError("evaluator intent is missing")
            if row["phase"] == EvaluationPhase.SUCCEEDED.value:
                observed = {
                    "raw_response_sha256": row["raw_response_sha256"],
                    "private_feedback_sha256": row["private_feedback_sha256"],
                    "public_receipt_sha256": row["public_receipt_sha256"],
                }
                if observed != hashes:
                    raise EvaluationIdentityConflict("evaluator completion conflicts")
                connection.commit()
                return dict(row)
            if row["phase"] != EvaluationPhase.INVOCATION_COMMITTED.value:
                raise EvaluationIntegrityError("evaluator invocation is not committed")
            connection.execute(
                "UPDATE evaluator_operations SET phase = ?, completed_at = ?, "
                "raw_response_json = ?, raw_response_sha256 = ?, "
                "private_feedback_json = ?, private_feedback_sha256 = ?, "
                "public_receipt_json = ?, public_receipt_sha256 = ? "
                "WHERE idempotency_key = ? AND phase = ?",
                (
                    EvaluationPhase.SUCCEEDED.value,
                    _now(),
                    raw_bytes,
                    hashes["raw_response_sha256"],
                    feedback_bytes,
                    hashes["private_feedback_sha256"],
                    public_bytes,
                    hashes["public_receipt_sha256"],
                    idempotency_key,
                    EvaluationPhase.INVOCATION_COMMITTED.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM evaluator_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return dict(row)

    def fail_ambiguous(self, idempotency_key: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE evaluator_operations SET phase = ?, completed_at = ?, failure_code = ? "
                "WHERE idempotency_key = ? AND phase = ?",
                (
                    EvaluationPhase.FAILED_AMBIGUOUS.value,
                    _now(),
                    "JUDGE_INVOCATION_OUTCOME_AMBIGUOUS",
                    idempotency_key,
                    EvaluationPhase.INVOCATION_COMMITTED.value,
                ),
            )
            connection.commit()

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evaluator_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _decoded(row: Mapping[str, Any], key: str, hash_key: str) -> dict[str, Any]:
        value = row.get(key)
        expected = row.get(hash_key)
        if value is None or not isinstance(expected, str):
            raise EvaluationIntegrityError("evaluator completion is incomplete")
        raw = bytes(value)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise EvaluationIntegrityError("evaluator database content hash drifted")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise EvaluationIntegrityError("evaluator database content is malformed")
        return decoded

    def completed_payloads(
        self, idempotency_key: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        row = self.get(idempotency_key)
        if row is None or row["phase"] != EvaluationPhase.SUCCEEDED.value:
            raise EvaluationIntegrityError("evaluator operation is not complete")
        raw = self._decoded(row, "raw_response_json", "raw_response_sha256")
        feedback = self._decoded(row, "private_feedback_json", "private_feedback_sha256")
        public = self._decoded(row, "public_receipt_json", "public_receipt_sha256")
        return row, raw, feedback, public


class DurableEvaluatorOperation:
    """Evaluator-only exactly-once operation over a durable phase journal."""

    def __init__(
        self,
        root: str | Path,
        *,
        executor: JudgeExecutor,
        partitioner: FeedbackPartitioner = soft_judge_partitioner,
    ) -> None:
        self.store = DurableEvaluatorStore(root)
        self.executor = executor
        self.partitioner = partitioner

    @staticmethod
    def _operation_id(idempotency_key: str) -> str:
        return "evaluation-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _validate_request(request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise EvaluationIntegrityError("evaluator request must be an object")
        required = {
            "task_id",
            "attempt_id",
            "session_id",
            "completed_dataset_id",
            "dataset_revision",
            "validator_receipt_sha256",
            "artifact_root_sha256",
            "candidate_runtime_seconds",
        }
        if not required.issubset(request):
            raise EvaluationIntegrityError("evaluator request is incomplete")
        for key in (
            "task_id",
            "attempt_id",
            "session_id",
            "completed_dataset_id",
            "dataset_revision",
        ):
            if not isinstance(request[key], str) or not request[key]:
                raise EvaluationIntegrityError(f"evaluator request {key} is invalid")
        for key in ("validator_receipt_sha256", "artifact_root_sha256"):
            if not isinstance(request[key], str) or not _SHA256.fullmatch(request[key]):
                raise EvaluationIntegrityError(f"evaluator request {key} is invalid")
        _reject_secret_keys(request)
        return json.loads(_canonical(request))

    @staticmethod
    def _public_receipt(
        *,
        operation_id: str,
        idempotency_key: str,
        request: dict[str, Any],
        policy: JudgePolicyIdentity,
        outcome: JudgeExecutionOutcome,
        feedback: FeedbackPartition,
        raw_sha256: str,
        private_feedback_sha256: str,
        evaluator_runtime_seconds: float,
    ) -> dict[str, Any]:
        receipt = {
            "schema_version": "openevo.researchclawbench.durable_evaluation.v1",
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "task_id": request["task_id"],
            "attempt_id": request["attempt_id"],
            "session_id": request["session_id"],
            "completed_dataset_id": request["completed_dataset_id"],
            "dataset_revision": request["dataset_revision"],
            "feedback_class": feedback.feedback_class.value,
            "total_score": float(outcome.raw_score["total_score"]),
            "judge_model": policy.model,
            "judge_provider": policy.provider,
            "judge_api_base": policy.api_base,
            "request_count": outcome.request_count,
            "usage": outcome.usage,
            "cost_total_usd": outcome.cost_total_usd,
            "evaluator_runtime_seconds": evaluator_runtime_seconds,
            "request_sha256": _digest(request),
            "policy_sha256": policy.policy_sha256,
            "model_sha256": policy.model_sha256,
            "provider_sha256": policy.provider_sha256,
            "scorer_commit": policy.scorer_commit,
            "scorer_sha256": policy.scorer_sha256,
            "raw_response_sha256": raw_sha256,
            "private_feedback_authority_id": f"evaluator-feedback-{private_feedback_sha256[:24]}",
            "private_feedback_sha256": private_feedback_sha256,
            "secret_recorded": False,
        }
        for key in (
            "artifact_root_sha256",
            "artifact_valid",
            "completed",
            "cost_total_usd",
            "exit_code",
            "generic_failure_tags",
            "runtime_bucket_seconds",
        ):
            if key in feedback.global_feedback:
                receipt[key] = feedback.global_feedback[key]
        receipt["evaluation_receipt_id"] = operation_id
        return receipt

    def execute(
        self,
        *,
        request: dict[str, Any],
        idempotency_key: str,
        policy: JudgePolicyIdentity,
        task_specific_literals: Iterable[str] = (),
        secret_values: Iterable[str] = (),
        after_judge_hook: Callable[[], None] | None = None,
    ) -> DurableEvaluationResult:
        closed_request = self._validate_request(request)
        closed_task_literals = tuple(task_specific_literals)
        closed_secret_values = tuple(secret_values)
        operation_id = self._operation_id(idempotency_key)
        row = self.store.begin(
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            request=closed_request,
            policy=policy,
        )
        phase = EvaluationPhase(row["phase"])
        if phase is EvaluationPhase.SUCCEEDED:
            return self._recover(idempotency_key, recovered=True)
        if phase in {
            EvaluationPhase.INVOCATION_COMMITTED,
            EvaluationPhase.FAILED_AMBIGUOUS,
        }:
            raise AmbiguousJudgeInvocation(
                "Judge invocation was committed without recoverable completion; refusing replay"
            )
        if phase is EvaluationPhase.FAILED_TERMINAL:
            raise DurableEvaluatorError("evaluator operation is terminally failed")
        row = self.store.commit_invocation(idempotency_key)
        if row["phase"] != EvaluationPhase.INVOCATION_COMMITTED.value:
            raise EvaluationIntegrityError("evaluator invocation fence was not committed")
        invocation = JudgeInvocation(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            request_sha256=_digest(closed_request),
            policy_sha256=policy.policy_sha256,
            model=policy.model,
            provider=policy.provider,
            api_base=policy.api_base,
            scorer_commit=policy.scorer_commit,
            scorer_sha256=policy.scorer_sha256,
        )
        try:
            evaluator_started = time.monotonic()
            outcome = self.executor(closed_request, invocation)
            evaluator_runtime_seconds = time.monotonic() - evaluator_started
            if after_judge_hook is not None:
                after_judge_hook()
            outcome.validate(policy)
            feedback = self.partitioner(closed_request, outcome)
            feedback.validate(task_specific_literals=closed_task_literals)
            private_feedback = {
                "feedback_class": feedback.feedback_class.value,
                "global_feedback": feedback.global_feedback,
                "task_local_feedback": feedback.task_local_feedback,
            }
            _reject_secret_keys(outcome.raw_score)
            _reject_secret_values(outcome.raw_score, closed_secret_values)
            _reject_secret_values(private_feedback, closed_secret_values)
            raw_sha256 = _digest(outcome.raw_score)
            private_feedback_sha256 = _digest(private_feedback)
            public = self._public_receipt(
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                request=closed_request,
                policy=policy,
                outcome=outcome,
                feedback=feedback,
                raw_sha256=raw_sha256,
                private_feedback_sha256=private_feedback_sha256,
                evaluator_runtime_seconds=evaluator_runtime_seconds,
            )
            _reject_secret_values(public, closed_secret_values)
            self.store.complete(
                idempotency_key=idempotency_key,
                raw_response=outcome.raw_score,
                private_feedback=private_feedback,
                public_receipt=public,
            )
        except Exception:
            # The invocation fence was durable before entering the executor.
            # Any missing completion is ambiguous, even if the local exception
            # appears to precede a network call inside an opaque scorer.
            self.store.fail_ambiguous(idempotency_key)
            raise
        return self._recover(idempotency_key, recovered=False)

    def recover(
        self,
        *,
        request: dict[str, Any],
        idempotency_key: str,
        policy: JudgePolicyIdentity,
    ) -> DurableEvaluationResult | None:
        closed_request = self._validate_request(request)
        row = self.store.get(idempotency_key)
        if row is None:
            return None
        self.store.begin(
            idempotency_key=idempotency_key,
            operation_id=self._operation_id(idempotency_key),
            request=closed_request,
            policy=policy,
        )
        phase = EvaluationPhase(row["phase"])
        if phase is EvaluationPhase.SUCCEEDED:
            return self._recover(idempotency_key, recovered=True)
        if phase in {
            EvaluationPhase.INVOCATION_COMMITTED,
            EvaluationPhase.FAILED_AMBIGUOUS,
        }:
            raise AmbiguousJudgeInvocation(
                "Judge invocation has no durable response; refusing duplicate call"
            )
        return None

    def read_feedback_for_attachment(
        self,
        *,
        idempotency_key: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        """Evaluator-only read used to create the Core feedback attachment."""

        row, _raw, feedback, _public = self.store.completed_payloads(idempotency_key)
        if row["private_feedback_sha256"] != expected_sha256:
            raise EvaluationIntegrityError("private feedback authority hash differs")
        return feedback

    def _recover(self, idempotency_key: str, *, recovered: bool) -> DurableEvaluationResult:
        row, raw, feedback, public = self.store.completed_payloads(idempotency_key)
        raw_path = self.store.private_root / row["operation_id"] / "raw_score.json"
        feedback_path = (
            self.store.private_root / row["operation_id"] / "feedback_attachment_input.json"
        )
        public_path = self.store.public_root / f"{row['operation_id']}.json"
        self._publish_mirror(raw_path, raw)
        self._publish_mirror(feedback_path, feedback)
        self._publish_mirror(public_path, public)
        return DurableEvaluationResult(
            operation_id=row["operation_id"],
            idempotency_key=idempotency_key,
            public_receipt=public,
            public_receipt_sha256=row["public_receipt_sha256"],
            raw_response_sha256=row["raw_response_sha256"],
            private_feedback_authority_id=public["private_feedback_authority_id"],
            private_feedback_sha256=row["private_feedback_sha256"],
            recovered=recovered,
        )

    @staticmethod
    def _publish_mirror(path: Path, payload: dict[str, Any]) -> None:
        data = (
            json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise EvaluationIntegrityError("evaluator mirror differs from durable authority")
            return
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short evaluator mirror write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise EvaluationIntegrityError("concurrent evaluator mirror conflict")
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "OPENROUTER_API_BASE",
    "UNAVAILABLE",
    "AmbiguousJudgeInvocation",
    "DurableEvaluationResult",
    "DurableEvaluatorError",
    "DurableEvaluatorOperation",
    "DurableEvaluatorStore",
    "EvaluationIdentityConflict",
    "EvaluationIntegrityError",
    "EvaluationPhase",
    "FeedbackClass",
    "FeedbackPartition",
    "FeedbackPartitioner",
    "JudgeConfigurationError",
    "JudgeExecutionOutcome",
    "JudgeInvocation",
    "JudgePolicyIdentity",
    "JudgeRuntimeIdentity",
    "redact_text",
    "soft_judge_partitioner",
]
