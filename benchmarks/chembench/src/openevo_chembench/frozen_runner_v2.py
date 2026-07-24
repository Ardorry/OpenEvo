"""Frozen, one-completion-per-item ChemBench4K v2 test orchestration.

This runner is deliberately separate from the legacy task-local online
recovery runner.  It has no reflector, evolution method, job writer, artifact
writer, or feedback channel.  A baseline run receives no memory; an evolved
run receives one already-resolved immutable Core text-memory object for every
item.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from openevo_chembench.benchmark_receipt_v2 import BenchmarkAuthorizationV2
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import (
    ChemBench4KPrivateEvaluator,
    PrivateChemBench4KEvaluation,
)
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    FrozenAgentRequestV2,
)
from openevo_chembench.local_codex_executor import (
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
)
from openevo_chembench.models import RawAttempt
from openevo_chembench.v2_config import (
    PROTOCOL_ID,
    FrozenExperimentConfigV2,
)


_PUBLIC_MANIFEST_KEYS = frozenset(
    {
        "ordinal",
        "uid",
        "category",
        "question",
        "A",
        "B",
        "C",
        "D",
        "dataset_revision",
    }
)
_PRIVATE_MANIFEST_KEYS = frozenset(
    {
        "ordinal",
        "uid",
        "category",
        "source_split",
        "source_index",
        "target",
        "dataset_revision",
        "dataset_sha256",
    }
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "answer",
        "answer_mapping",
        "correct_answer",
        "private_task",
        "source_index",
        "target",
        "target_scores",
    }
)
_FORBIDDEN_EVOLUTION_EVENTS = frozenset(
    {
        "artifact_mutation",
        "artifact_reselection",
        "artifact_writer",
        "context_regeneration",
        "evaluator_feedback",
        "evolution_job",
        "evolution_method",
        "evolution_worker",
        "prompt_adjustment",
        "reflector",
    }
)
_SAFE_RESUMABLE_NO_COMPLETION_ERRORS = frozenset(
    {
        LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE,
        LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED,
        LocalCodexExecutionErrorCode.RESPONSE_MISSING,
    }
)
_PUBLIC_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_id",
        "arm",
        "scope",
        "ordinal",
        "uid",
        "category",
        "raw_completion",
        "parsed_prediction",
        "score",
        "official_parse_status",
        "strict_parse_status",
        "strict_parse_success",
        "transcript_reference",
        "runtime_metadata",
    }
)
_PRIVATE_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_id",
        "arm",
        "scope",
        "ordinal",
        "uid",
        "category",
        "target",
        "raw_completion",
        "official_prediction",
        "strict_prediction",
        "official_parse_status",
        "strict_parse_status",
        "correct",
    }
)
_RUN_STATE_KEYS = frozenset(
    {
        "schema_version",
        "protocol_id",
        "execution_mode",
        "run_name",
        "arm",
        "scope",
        "status",
        "planned_tasks",
        "completed_tasks",
        "model_calls",
        "config_sha256",
        "execution_receipt_sha256",
        "execution_evidence_digest",
        "evidence_binding",
        "frozen_artifact_id",
        "resolved_memory_sha256",
        "public_result_chain_sha256",
        "private_result_chain_sha256",
        "finding_codes",
        "retry_policy",
        "resume_count",
        "resume_allowed",
        "failed_ordinal",
        "failure_code",
        "failed_item_completion_observed",
    }
)
_EMPTY_CHAIN_SHA256 = hashlib.sha256(b"").hexdigest()


class FrozenRunnerError(RuntimeError):
    """Sanitized fail-closed runner error."""


class FrozenManifestError(FrozenRunnerError):
    """Public/private task manifests do not bind the same frozen test tasks."""


class FrozenProtocolViolation(FrozenRunnerError):
    """A forbidden evolution-plane action was attempted in the test loop."""


class FrozenRunStatusV2(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    SECURITY_TOOL_USE_VIOLATION = "SECURITY_TOOL_USE_VIOLATION"
    FROZEN_PROTOCOL_VIOLATION = "FROZEN_PROTOCOL_VIOLATION"


class FrozenExecutorV2(Protocol):
    """Minimal executor surface accepted by the frozen runner."""

    def execute_frozen(self, request: FrozenAgentRequestV2) -> RawAttempt: ...


class FrozenProtocolTripwire:
    """Latch and reject any evolution-plane activity during frozen testing."""

    __slots__ = ("_active", "_finding")

    def __init__(self) -> None:
        self._active = False
        self._finding: str | None = None

    @property
    def finding(self) -> str | None:
        return self._finding

    @property
    def active(self) -> bool:
        return self._active

    def enter_test_loop(self) -> None:
        if self._active or self._finding is not None:
            raise FrozenProtocolViolation("frozen protocol tripwire is not reusable")
        self._active = True

    def exit_test_loop(self) -> None:
        self._active = False

    def record_event(self, event_type: str) -> None:
        """Record a controller event through a closed test-loop vocabulary.

        The runner itself emits no events here.  Integrations that expose
        evolution hooks to workers must route attempted calls through this
        method.  Unknown event names fail closed as well.
        """

        if type(event_type) is not str or not event_type:
            event_type = "unknown"
        normalized = event_type.strip().casefold().replace("-", "_")
        if not self._active:
            self._finding = "EVENT_OUTSIDE_FROZEN_TEST_LOOP"
        elif normalized in _FORBIDDEN_EVOLUTION_EVENTS:
            self._finding = f"FORBIDDEN_{normalized.upper()}"
        else:
            self._finding = "UNKNOWN_FROZEN_LOOP_EVENT"
        raise FrozenProtocolViolation("FROZEN_PROTOCOL_VIOLATION")

    def assert_clean(self) -> None:
        if self._finding is not None:
            raise FrozenProtocolViolation("FROZEN_PROTOCOL_VIOLATION")


@dataclass(frozen=True, slots=True)
class FrozenRunResultV2:
    """Target-free terminal summary returned to an ordinary caller."""

    status: FrozenRunStatusV2
    arm: str
    scope: str
    planned_tasks: int
    completed_tasks: int
    model_calls: int
    output_directory: Path
    execution_receipt_sha256: str
    finding_codes: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, object]:
        payload = {
            "status": self.status.value,
            "arm": self.arm,
            "scope": self.scope,
            "planned_tasks": self.planned_tasks,
            "completed_tasks": self.completed_tasks,
            "model_calls": self.model_calls,
            "execution_receipt_sha256": self.execution_receipt_sha256,
            "finding_codes": list(self.finding_codes),
        }
        _assert_public_payload(payload)
        return payload


@dataclass(frozen=True, slots=True)
class _BoundManifestItem:
    ordinal: int
    task: PrivateChemBench4KTask


@dataclass(frozen=True, slots=True)
class _ResumeState:
    completed_tasks: int
    model_calls: int
    public_result_chain_sha256: str
    private_result_chain_sha256: str
    resume_count: int


class ChemBench4KFrozenRunnerV2:
    """Run one frozen baseline or evolved test arm without online evolution."""

    __slots__ = (
        "_authorization",
        "_config",
        "_evidence_binding",
        "_evaluator",
        "_executor",
        "_items",
        "_loader",
        "_memory",
        "_output_root",
        "_resume_requested",
        "_started",
        "_tripwire",
        "_workspace_root",
    )

    def __init__(
        self,
        *,
        config: FrozenExperimentConfigV2,
        loader: ChemBench4KDatasetLoader,
        executor: FrozenExecutorV2,
        workspace_root: Path,
        authorization: BenchmarkAuthorizationV2,
        resolved_text_memory: CoreResolvedTextMemoryV2 | None = None,
        tripwire: FrozenProtocolTripwire | None = None,
        resume: bool = False,
    ) -> None:
        if type(config) is not FrozenExperimentConfigV2:
            raise TypeError("config must be exact FrozenExperimentConfigV2")
        if type(loader) is not ChemBench4KDatasetLoader:
            raise TypeError("loader must be exact ChemBench4KDatasetLoader")
        if not callable(getattr(executor, "execute_frozen", None)):
            raise TypeError("executor must implement execute_frozen(request)")
        if not isinstance(workspace_root, Path) or not workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        if type(authorization) is not BenchmarkAuthorizationV2:
            raise TypeError("authorization must be issued by the benchmark receipt gate")
        if type(resume) is not bool:
            raise TypeError("resume must be boolean")
        if config.arm == "baseline":
            if resolved_text_memory is not None:
                raise ValueError("baseline must not receive resolved text memory")
        else:
            if type(resolved_text_memory) is not CoreResolvedTextMemoryV2:
                raise TypeError("evolved arm requires exact CoreResolvedTextMemoryV2")
            _validate_memory_binding(config, resolved_text_memory)

        self._config = config
        self._loader = loader
        self._executor = executor
        self._workspace_root = workspace_root.resolve()
        self._output_root = _resolve_inside_workspace(
            self._workspace_root,
            config.output_directory,
        )
        self._authorization = authorization
        self._memory = resolved_text_memory
        self._tripwire = tripwire or FrozenProtocolTripwire()
        if type(self._tripwire) is not FrozenProtocolTripwire:
            raise TypeError("tripwire must be exact FrozenProtocolTripwire")
        self._evaluator = ChemBench4KPrivateEvaluator()
        self._items = self._bind_manifests()
        self._evidence_binding = self._build_evidence_binding()
        self._resume_requested = resume
        self._started = False

    @property
    def output_root(self) -> Path:
        return self._output_root

    @property
    def tripwire(self) -> FrozenProtocolTripwire:
        return self._tripwire

    def _build_evidence_binding(self) -> dict[str, object]:
        public_manifest = _resolve_inside_workspace(
            self._workspace_root,
            self._config.task_manifest,
        )
        private_manifest = _resolve_inside_workspace(
            self._workspace_root,
            self._config.private_task_manifest,
        )
        model_identity = {
            "model": self._config.model,
            "reasoning_effort": self._config.reasoning_effort,
            "codex_cli_version": self._config.codex_cli_version,
            "backend": self._config.executor.backend,
            "harness": self._config.executor.harness,
        }
        artifact_identity = {
            "enabled": self._memory is not None,
            "core_artifact_id": (None if self._memory is None else self._memory.core_artifact_id),
            "artifact_payload_sha256": (
                None if self._memory is None else self._memory.artifact_payload_sha256
            ),
            "context_resolution_digest": (
                None if self._memory is None else self._memory.context_resolution_digest
            ),
            "resolved_memory_sha256": (
                None if self._memory is None else self._memory.resolved_memory_sha256
            ),
        }
        fields: dict[str, object] = {
            "protocol_id": PROTOCOL_ID,
            "execution_mode": self._config.execution_mode,
            "config_sha256": self._config.config_sha256(),
            "execution_receipt_sha256": self._authorization.receipt_sha256,
            "execution_evidence_digest": self._authorization.evidence_digest,
            "dataset_repository": self._loader.manifest.repository,
            "dataset_revision": self._loader.manifest.revision,
            "dataset_combined_sha256": self._loader.manifest.combined_sha256,
            "public_task_manifest_sha256": _sha256_file(public_manifest),
            "private_task_manifest_sha256": _sha256_file(private_manifest),
            "model_identity_sha256": hashlib.sha256(
                _canonical_json_bytes(model_identity)
            ).hexdigest(),
            "artifact_identity_sha256": hashlib.sha256(
                _canonical_json_bytes(artifact_identity)
            ).hexdigest(),
        }
        fields["binding_sha256"] = hashlib.sha256(_canonical_json_bytes(fields)).hexdigest()
        return fields

    def run(self) -> FrozenRunResultV2:
        """Execute every selected task exactly once and persist split results."""

        if self._started:
            raise FrozenRunnerError("frozen runner cannot be resumed or reused")
        if self._build_evidence_binding() != self._evidence_binding:
            raise FrozenRunnerError("bound execution evidence changed before launch")
        self._started = True

        if self._resume_requested:
            resume = self._validate_resume_state()
            public_result_path, private_result_path = self._existing_result_paths()
            completed_tasks = resume.completed_tasks
            model_calls = resume.model_calls
            public_chain = resume.public_result_chain_sha256
            private_chain = resume.private_result_chain_sha256
            resume_count = resume.resume_count + 1
        else:
            if self._output_root.exists():
                raise FrozenRunnerError("output target already exists")
            public_result_path, private_result_path = self._initialize_result_tree()
            completed_tasks = 0
            model_calls = 0
            public_chain = _EMPTY_CHAIN_SHA256
            private_chain = _EMPTY_CHAIN_SHA256
            resume_count = 0

        status = FrozenRunStatusV2.RUNNING
        finding_codes: tuple[str, ...] = ()
        self._write_run_state(
            status=status,
            completed_tasks=completed_tasks,
            model_calls=model_calls,
            finding_codes=finding_codes,
            public_result_chain_sha256=public_chain,
            private_result_chain_sha256=private_chain,
            resume_count=resume_count,
            resume_allowed=False,
            failed_ordinal=None,
            failure_code=None,
            failed_item_completion_observed=None,
        )

        self._tripwire.enter_test_loop()
        current_ordinal: int | None = None
        current_completion_observed = False
        resume_allowed = False
        failure_code: str | None = None
        try:
            for item in self._items[completed_tasks:]:
                self._tripwire.assert_clean()
                task = item.task
                current_ordinal = item.ordinal
                current_completion_observed = False
                category_dev = self._loader.load_category(task.category, split="dev")
                rendered = render_official_five_shot_prompt(
                    task.to_public(),
                    category_dev=category_dev,
                )
                request = FrozenAgentRequestV2(
                    rendered_public_prompt=rendered.text,
                    resolved_text_memory=self._memory,
                )

                # There is intentionally no retry loop.  One invocation begins
                # and either yields one completion or terminates the arm.
                model_calls += 1
                attempt = self._executor.execute_frozen(request)
                current_completion_observed = True
                if type(attempt) is not RawAttempt:
                    raise TypeError("execute_frozen must return exact RawAttempt")
                self._tripwire.assert_clean()
                evaluation = self._evaluator.evaluate(
                    task=task,
                    raw_completion=attempt.response,
                )
                public_encoded, private_encoded = self._append_item_results(
                    public_result_path=public_result_path,
                    private_result_path=private_result_path,
                    ordinal=item.ordinal,
                    task=task,
                    attempt=attempt,
                    evaluation=evaluation,
                )
                public_chain = _advance_result_chain(public_chain, public_encoded)
                private_chain = _advance_result_chain(private_chain, private_encoded)
                completed_tasks += 1
                current_ordinal = None
                current_completion_observed = False
                self._write_run_state(
                    status=status,
                    completed_tasks=completed_tasks,
                    model_calls=model_calls,
                    finding_codes=finding_codes,
                    public_result_chain_sha256=public_chain,
                    private_result_chain_sha256=private_chain,
                    resume_count=resume_count,
                    resume_allowed=False,
                    failed_ordinal=None,
                    failure_code=None,
                    failed_item_completion_observed=None,
                )
            status = FrozenRunStatusV2.COMPLETED
        except FrozenProtocolViolation:
            status = FrozenRunStatusV2.FROZEN_PROTOCOL_VIOLATION
            finding_codes = (self._tripwire.finding or "FROZEN_PROTOCOL_VIOLATION",)
            failure_code = "FROZEN_PROTOCOL_VIOLATION"
        except LocalCodexExecutionError as exc:
            if (
                exc.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
                or exc.run_status == FrozenRunStatusV2.SECURITY_TOOL_USE_VIOLATION.value
            ):
                status = FrozenRunStatusV2.SECURITY_TOOL_USE_VIOLATION
                finding_codes = ("SECURITY_TOOL_USE_VIOLATION",)
                failure_code = "SECURITY_TOOL_USE_VIOLATION"
                failure_encoded = self._append_public_failure(
                    public_result_path,
                    ordinal=self._items[completed_tasks].ordinal,
                    task=self._items[completed_tasks].task,
                    error=exc,
                )
                public_chain = _advance_result_chain(public_chain, failure_encoded)
            else:
                status = FrozenRunStatusV2.EXECUTION_FAILED
                finding_codes = (f"EXECUTOR_{exc.code.value.upper()}",)
                failure_code = exc.code.value
                resume_allowed = (
                    exc.code in _SAFE_RESUMABLE_NO_COMPLETION_ERRORS
                    and not current_completion_observed
                    and exc.replacement_completion_allowed
                )
        except BaseException:
            status = FrozenRunStatusV2.EXECUTION_FAILED
            finding_codes = ("UNCLASSIFIED_EXECUTION_FAILURE",)
            failure_code = "unclassified_execution_failure"
            self._write_run_state(
                status=status,
                completed_tasks=completed_tasks,
                model_calls=model_calls,
                finding_codes=finding_codes,
                public_result_chain_sha256=public_chain,
                private_result_chain_sha256=private_chain,
                resume_count=resume_count,
                resume_allowed=False,
                failed_ordinal=current_ordinal,
                failure_code=failure_code,
                failed_item_completion_observed=current_completion_observed,
            )
            raise
        finally:
            self._tripwire.exit_test_loop()

        self._write_run_state(
            status=status,
            completed_tasks=completed_tasks,
            model_calls=model_calls,
            finding_codes=finding_codes,
            public_result_chain_sha256=public_chain,
            private_result_chain_sha256=private_chain,
            resume_count=resume_count,
            resume_allowed=resume_allowed,
            failed_ordinal=(
                current_ordinal if status is not FrozenRunStatusV2.COMPLETED else None
            ),
            failure_code=failure_code,
            failed_item_completion_observed=(
                current_completion_observed if status is not FrozenRunStatusV2.COMPLETED else None
            ),
        )
        return FrozenRunResultV2(
            status=status,
            arm=self._config.arm,
            scope=self._config.scope,
            planned_tasks=len(self._items),
            completed_tasks=completed_tasks,
            model_calls=model_calls,
            output_directory=self._output_root,
            execution_receipt_sha256=self._authorization.receipt_sha256,
            finding_codes=finding_codes,
        )

    def _bind_manifests(self) -> tuple[_BoundManifestItem, ...]:
        public_path = _resolve_inside_workspace(
            self._workspace_root,
            self._config.task_manifest,
        )
        private_path = _resolve_inside_workspace(
            self._workspace_root,
            self._config.private_task_manifest,
        )
        try:
            private_mode = private_path.stat().st_mode & 0o777
        except OSError as exc:
            raise FrozenManifestError("private task manifest is unavailable") from exc
        if private_mode & 0o077:
            raise FrozenManifestError("private task manifest permissions are unsafe")

        public_rows = _read_json_lines(
            public_path,
            expected_keys=_PUBLIC_MANIFEST_KEYS,
            boundary="public",
        )
        private_rows = _read_json_lines(
            private_path,
            expected_keys=_PRIVATE_MANIFEST_KEYS,
            boundary="private",
        )
        expected_count = {
            "canary18": 18,
            "pilot500": 500,
            "full": self._loader.manifest.test_count,
        }[self._config.scope]
        if len(public_rows) != expected_count or len(private_rows) != expected_count:
            raise FrozenManifestError("task manifest count does not match frozen scope")

        test_by_uid = {task.uid: task for task in self._loader.load_split("test")}
        bound: list[_BoundManifestItem] = []
        seen_uids: set[str] = set()
        for ordinal, (public, private) in enumerate(zip(public_rows, private_rows, strict=True)):
            if public["ordinal"] != ordinal or private["ordinal"] != ordinal:
                raise FrozenManifestError("task manifest ordinal sequence is invalid")
            uid = public["uid"]
            if type(uid) is not str or uid in seen_uids or private["uid"] != uid:
                raise FrozenManifestError("public/private task UID sequence differs")
            seen_uids.add(uid)
            task = test_by_uid.get(uid)
            if task is None:
                raise FrozenManifestError("task manifest references a non-test UID")
            expected_public = {
                "ordinal": ordinal,
                **task.to_public().to_public_dict(),
            }
            expected_private = {
                "ordinal": ordinal,
                "uid": task.uid,
                "category": task.category,
                "source_split": task.source_split,
                "source_index": task.source_index,
                "target": task.target,
                "dataset_revision": task.dataset_revision,
                "dataset_sha256": task.dataset_sha256,
            }
            if public != expected_public or private != expected_private:
                raise FrozenManifestError("task manifest content differs from snapshot")
            bound.append(_BoundManifestItem(ordinal=ordinal, task=task))
        return tuple(bound)

    def _initialize_result_tree(self) -> tuple[Path, Path]:
        self._output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        public_root = self._output_root / "public"
        private_root = self._output_root / "private"
        public_root.mkdir(mode=0o755)
        private_root.mkdir(mode=0o700)
        public_path = public_root / "results.jsonl"
        private_path = private_root / "results.jsonl"
        _create_empty_file(public_path, mode=0o644)
        _create_empty_file(private_path, mode=0o600)
        return public_path, private_path

    def _existing_result_paths(self) -> tuple[Path, Path]:
        if (
            not self._output_root.is_dir()
            or self._output_root.is_symlink()
            or (self._output_root.stat().st_mode & 0o077)
        ):
            raise FrozenRunnerError("resume output directory is unavailable or unsafe")
        public_path = self._output_root / "public" / "results.jsonl"
        private_path = self._output_root / "private" / "results.jsonl"
        if (
            not public_path.is_file()
            or public_path.is_symlink()
            or not private_path.is_file()
            or private_path.is_symlink()
            or (private_path.stat().st_mode & 0o077)
        ):
            raise FrozenRunnerError("resume result files are unavailable or unsafe")
        return public_path, private_path

    def _validate_resume_state(self) -> _ResumeState:
        """Recompute every identity and validate the completed result prefix."""

        if not self._output_root.exists():
            raise FrozenRunnerError("resume output target does not exist")
        public_path, private_path = self._existing_result_paths()
        state_path = self._output_root / "run_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FrozenRunnerError("resume run state is unreadable") from exc
        if not isinstance(state, dict):
            raise FrozenRunnerError("resume run state is invalid")
        if (
            set(state) != _RUN_STATE_KEYS
            or state.get("retry_policy") != "one_completion_no_retry"
            or state.get("schema_version") != "chembench4k_frozen_run_state_v2"
            or state.get("protocol_id") != PROTOCOL_ID
            or state.get("execution_mode") != self._config.execution_mode
            or state.get("run_name") != self._config.run_name
            or state.get("arm") != self._config.arm
            or state.get("scope") != self._config.scope
            or state.get("status") != FrozenRunStatusV2.EXECUTION_FAILED.value
            or state.get("planned_tasks") != len(self._items)
        ):
            raise FrozenRunnerError("resume protocol identity mismatch")
        if state.get("evidence_binding") != self._evidence_binding:
            raise FrozenRunnerError("resume evidence hash mismatch")
        if (
            state.get("config_sha256") != self._config.config_sha256()
            or state.get("execution_receipt_sha256") != self._authorization.receipt_sha256
            or state.get("execution_evidence_digest") != self._authorization.evidence_digest
            or state.get("frozen_artifact_id")
            != (None if self._memory is None else self._memory.core_artifact_id)
            or state.get("resolved_memory_sha256")
            != (None if self._memory is None else self._memory.resolved_memory_sha256)
        ):
            raise FrozenRunnerError("resume config or receipt hash mismatch")
        if state.get("resume_allowed") is not True:
            raise FrozenRunnerError("resume is forbidden by terminal run state")
        if state.get("failed_item_completion_observed") is not False:
            raise FrozenRunnerError("resume cannot replace an observed completion")
        failure_code = state.get("failure_code")
        if failure_code not in {code.value for code in _SAFE_RESUMABLE_NO_COMPLETION_ERRORS}:
            raise FrozenRunnerError("resume failure type is not infrastructure-safe")

        completed_tasks = state.get("completed_tasks")
        model_calls = state.get("model_calls")
        resume_count = state.get("resume_count")
        if (
            type(completed_tasks) is not int
            or not 0 <= completed_tasks < len(self._items)
            or type(model_calls) is not int
            or model_calls < completed_tasks + 1
            or type(resume_count) is not int
            or resume_count < 0
            or state.get("failed_ordinal") != completed_tasks
        ):
            raise FrozenRunnerError("resume progress counters are invalid")

        (
            public_rows,
            public_chain,
        ) = _read_canonical_result_lines(public_path)
        (
            private_rows,
            private_chain,
        ) = _read_canonical_result_lines(private_path)
        if len(public_rows) != completed_tasks or len(private_rows) != completed_tasks:
            raise FrozenRunnerError("resume result prefix length is invalid")
        if (
            state.get("public_result_chain_sha256") != public_chain
            or state.get("private_result_chain_sha256") != private_chain
        ):
            raise FrozenRunnerError("resume result prefix digest mismatch")
        for ordinal, (public, private) in enumerate(zip(public_rows, private_rows, strict=True)):
            self._validate_completed_result(
                ordinal=ordinal,
                task=self._items[ordinal].task,
                public=public,
                private=private,
            )
        return _ResumeState(
            completed_tasks=completed_tasks,
            model_calls=model_calls,
            public_result_chain_sha256=public_chain,
            private_result_chain_sha256=private_chain,
            resume_count=resume_count,
        )

    def _validate_completed_result(
        self,
        *,
        ordinal: int,
        task: PrivateChemBench4KTask,
        public: dict[str, object],
        private: dict[str, object],
    ) -> None:
        if set(public) != _PUBLIC_RESULT_KEYS or set(private) != _PRIVATE_RESULT_KEYS:
            raise FrozenRunnerError("resume result prefix schema is invalid")
        raw_completion = public.get("raw_completion")
        if type(raw_completion) is not str or private.get("raw_completion") != raw_completion:
            raise FrozenRunnerError("resume completion prefix is invalid")
        evaluation = self._evaluator.evaluate(
            task=task,
            raw_completion=raw_completion,
        )
        public_evaluation = evaluation.to_public_result()
        expected_public = {
            "schema_version": "chembench4k_public_item_result_v2",
            "protocol_id": PROTOCOL_ID,
            "arm": self._config.arm,
            "scope": self._config.scope,
            "ordinal": ordinal,
            "uid": task.uid,
            "category": task.category,
            "raw_completion": raw_completion,
            "parsed_prediction": public_evaluation.official_prediction,
            "score": public_evaluation.official_accuracy,
            "official_parse_status": public_evaluation.official_parse_status,
            "strict_parse_status": public_evaluation.strict_parse_status,
            "strict_parse_success": public_evaluation.strict_parse_success,
            "transcript_reference": public.get("transcript_reference"),
            "runtime_metadata": public.get("runtime_metadata"),
        }
        expected_private = {
            "schema_version": "chembench4k_private_item_result_v2",
            "protocol_id": PROTOCOL_ID,
            "arm": self._config.arm,
            "scope": self._config.scope,
            "ordinal": ordinal,
            "uid": task.uid,
            "category": task.category,
            "target": task.target,
            "raw_completion": evaluation.raw_completion,
            "official_prediction": evaluation.official.prediction,
            "strict_prediction": evaluation.strict.prediction,
            "official_parse_status": evaluation.official.status.value,
            "strict_parse_status": evaluation.strict.status.value,
            "correct": evaluation.correct,
        }
        if (
            type(public.get("transcript_reference")) is not str
            or not public["transcript_reference"]
            or (
                public.get("runtime_metadata") is not None
                and not isinstance(public["runtime_metadata"], dict)
            )
            or public != expected_public
            or private != expected_private
        ):
            raise FrozenRunnerError("resume completed result prefix was modified")

    def _append_item_results(
        self,
        *,
        public_result_path: Path,
        private_result_path: Path,
        ordinal: int,
        task: PrivateChemBench4KTask,
        attempt: RawAttempt,
        evaluation: PrivateChemBench4KEvaluation,
    ) -> tuple[bytes, bytes]:
        public_evaluation = evaluation.to_public_result()
        public_payload = {
            "schema_version": "chembench4k_public_item_result_v2",
            "protocol_id": PROTOCOL_ID,
            "arm": self._config.arm,
            "scope": self._config.scope,
            "ordinal": ordinal,
            "uid": task.uid,
            "category": task.category,
            "raw_completion": attempt.response,
            "parsed_prediction": public_evaluation.official_prediction,
            "score": public_evaluation.official_accuracy,
            "official_parse_status": public_evaluation.official_parse_status,
            "strict_parse_status": public_evaluation.strict_parse_status,
            "strict_parse_success": public_evaluation.strict_parse_success,
            "transcript_reference": attempt.transcript_reference.reference,
            "runtime_metadata": (
                None
                if attempt.runtime_metadata is None
                else attempt.runtime_metadata.to_result_payload()
            ),
        }
        _assert_public_payload(public_payload)
        private_payload = {
            "schema_version": "chembench4k_private_item_result_v2",
            "protocol_id": PROTOCOL_ID,
            "arm": self._config.arm,
            "scope": self._config.scope,
            "ordinal": ordinal,
            "uid": task.uid,
            "category": task.category,
            "target": task.target,
            "raw_completion": evaluation.raw_completion,
            "official_prediction": evaluation.official.prediction,
            "strict_prediction": evaluation.strict.prediction,
            "official_parse_status": evaluation.official.status.value,
            "strict_parse_status": evaluation.strict.status.value,
            "correct": evaluation.correct,
        }
        public_encoded = _append_json_line(public_result_path, public_payload)
        private_encoded = _append_json_line(private_result_path, private_payload)
        return public_encoded, private_encoded

    def _append_public_failure(
        self,
        public_result_path: Path,
        *,
        ordinal: int,
        task: PrivateChemBench4KTask,
        error: LocalCodexExecutionError,
    ) -> bytes:
        payload = {
            "schema_version": "chembench4k_public_item_failure_v2",
            "protocol_id": PROTOCOL_ID,
            "arm": self._config.arm,
            "scope": self._config.scope,
            "ordinal": ordinal,
            "uid": task.uid,
            "category": task.category,
            "run_status": FrozenRunStatusV2.SECURITY_TOOL_USE_VIOLATION.value,
            "error_type": error.code.value,
            "event_counts": dict(error.event_counts),
            "event_types": sorted(error.event_counts),
            "event_digest": error.event_digest,
            "retry_allowed": False,
            "resume_allowed": False,
            "replacement_completion_allowed": False,
        }
        _assert_public_payload(payload)
        return _append_json_line(public_result_path, payload)

    def _write_run_state(
        self,
        *,
        status: FrozenRunStatusV2,
        completed_tasks: int,
        model_calls: int,
        finding_codes: tuple[str, ...],
        public_result_chain_sha256: str,
        private_result_chain_sha256: str,
        resume_count: int,
        resume_allowed: bool,
        failed_ordinal: int | None,
        failure_code: str | None,
        failed_item_completion_observed: bool | None,
    ) -> None:
        payload = {
            "schema_version": "chembench4k_frozen_run_state_v2",
            "protocol_id": PROTOCOL_ID,
            "execution_mode": self._config.execution_mode,
            "run_name": self._config.run_name,
            "arm": self._config.arm,
            "scope": self._config.scope,
            "status": status.value,
            "planned_tasks": len(self._items),
            "completed_tasks": completed_tasks,
            "model_calls": model_calls,
            "config_sha256": self._config.config_sha256(),
            "execution_receipt_sha256": self._authorization.receipt_sha256,
            "execution_evidence_digest": self._authorization.evidence_digest,
            "evidence_binding": self._evidence_binding,
            "frozen_artifact_id": (
                None if self._memory is None else self._memory.core_artifact_id
            ),
            "resolved_memory_sha256": (
                None if self._memory is None else self._memory.resolved_memory_sha256
            ),
            "public_result_chain_sha256": public_result_chain_sha256,
            "private_result_chain_sha256": private_result_chain_sha256,
            "finding_codes": list(finding_codes),
            "retry_policy": "one_completion_no_retry",
            "resume_count": resume_count,
            "resume_allowed": resume_allowed,
            "failed_ordinal": failed_ordinal,
            "failure_code": failure_code,
            "failed_item_completion_observed": failed_item_completion_observed,
        }
        _assert_public_payload(payload)
        _write_json_atomic(self._output_root / "run_state.json", payload)


def _validate_memory_binding(
    config: FrozenExperimentConfigV2,
    memory: CoreResolvedTextMemoryV2,
) -> None:
    binding = config.artifact
    if not binding.is_frozen:
        raise ValueError("evolved configuration artifact binding is not frozen")
    if (
        memory.core_artifact_id != binding.frozen_artifact_id
        or memory.artifact_payload_sha256 != binding.frozen_artifact_sha256
        or memory.context_resolution_digest != binding.context_resolution_digest
        or memory.resolved_memory_sha256 != binding.resolved_memory_sha256
    ):
        raise ValueError("Core-resolved memory does not match frozen configuration")


def _resolve_inside_workspace(workspace_root: Path, relative: str) -> Path:
    candidate = (workspace_root / relative).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("configured path escapes workspace_root") from exc
    return candidate


def _read_json_lines(
    path: Path,
    *,
    expected_keys: frozenset[str],
    boundary: str,
) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise FrozenManifestError(f"{boundary} task manifest is unreadable") from exc
    if not lines:
        raise FrozenManifestError(f"{boundary} task manifest is empty")
    rows: list[dict[str, object]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FrozenManifestError(f"{boundary} task manifest contains invalid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise FrozenManifestError(f"{boundary} task manifest row schema is invalid")
        rows.append(payload)
    return rows


def _assert_public_payload(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("public result keys must be strings")
            normalized = key.casefold().replace("-", "_")
            if normalized in _FORBIDDEN_PUBLIC_KEYS:
                raise FrozenRunnerError("private field reached public result boundary")
            _assert_public_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_public_payload(item)


def _canonical_json_line(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return _canonical_json_line(payload)


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise FrozenRunnerError("bound evidence file is unavailable") from exc


def _advance_result_chain(previous: str, encoded_line: bytes) -> str:
    try:
        previous_bytes = bytes.fromhex(previous)
    except ValueError as exc:
        raise FrozenRunnerError("result prefix chain is invalid") from exc
    if len(previous_bytes) != 32:
        raise FrozenRunnerError("result prefix chain is invalid")
    return hashlib.sha256(previous_bytes + encoded_line).hexdigest()


def _read_canonical_result_lines(
    path: Path,
) -> tuple[list[dict[str, object]], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FrozenRunnerError("resume result prefix is unreadable") from exc
    if raw and not raw.endswith(b"\n"):
        raise FrozenRunnerError("resume result prefix is truncated")
    rows: list[dict[str, object]] = []
    chain = _EMPTY_CHAIN_SHA256
    for encoded in raw.splitlines(keepends=True):
        try:
            payload = json.loads(encoded)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FrozenRunnerError("resume result prefix contains invalid JSON") from exc
        if not isinstance(payload, dict) or _canonical_json_line(payload) != encoded:
            raise FrozenRunnerError("resume result prefix is not canonical")
        rows.append(payload)
        chain = _advance_result_chain(chain, encoded)
    return rows, chain


def _create_empty_file(path: Path, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.close(descriptor)
    path.chmod(mode)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> bytes:
    encoded = _canonical_json_line(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        with os.fdopen(descriptor, "ab", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return encoded


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    encoded = _canonical_json_line(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o644)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "ChemBench4KFrozenRunnerV2",
    "FrozenExecutorV2",
    "FrozenManifestError",
    "FrozenProtocolTripwire",
    "FrozenProtocolViolation",
    "FrozenRunResultV2",
    "FrozenRunnerError",
    "FrozenRunStatusV2",
]
