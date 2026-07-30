"""Evaluator-only scorer subprocess; raw details remain in the private tree."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .config import FROZEN_TASKS, ExperimentConfig
from .durable_evaluator_operation import (
    UNAVAILABLE,
    DurableEvaluatorOperation,
    FeedbackPartitioner,
    JudgeExecutionOutcome,
    JudgeInvocation,
    JudgePolicyIdentity,
    JudgeRuntimeIdentity,
    soft_judge_partitioner,
)
from .evaluator_dependency_lock import validate_evaluator_dependency_lock
from .hashing import UnsafePathError, iter_regular_files, tree_sha256

_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_PINNED_STRUCTAI_VERSION = "0.1.23"
_EVALUATOR_FORBIDDEN_SOURCE_NAMES = {
    ".codex",
    "checklist.json",
    "evaluator_private",
    "secrets",
    "target_study",
}


class EvaluatorError(RuntimeError):
    """Community evaluator failed or returned malformed output."""


@dataclass(frozen=True)
class CommunityEvaluatorExecution:
    """Private score plus measured-or-unavailable execution accounting."""

    raw_score: dict[str, Any]
    judge_outcome: JudgeExecutionOutcome


@dataclass(frozen=True)
class DurableCommunityJudgeExecutor:
    """Production executor plugged into ``DurableEvaluatorOperation``.

    This object owns no retry logic.  Its caller must commit the durable Judge
    invocation fence first and must never invoke it again for an ambiguous
    operation.
    """

    project_root: Path
    evaluator_private_root: Path
    policy: JudgePolicyIdentity
    judge_env_file: Path | None = None
    timeout_seconds: int = 1800

    def __call__(
        self, request: dict[str, Any], invocation: JudgeInvocation
    ) -> JudgeExecutionOutcome:
        if (
            invocation.policy_sha256 != self.policy.policy_sha256
            or invocation.model != self.policy.model
            or invocation.provider != self.policy.provider
            or invocation.api_base != self.policy.api_base
            or invocation.scorer_commit != self.policy.scorer_commit
            or invocation.scorer_sha256 != self.policy.scorer_sha256
        ):
            raise EvaluatorError("durable Judge invocation identity differs")
        workspace = request.get("candidate_output_root")
        if not isinstance(workspace, str) or not workspace:
            raise EvaluatorError("durable evaluator request lacks candidate output root")
        result = run_community_evaluator_detailed(
            project_root=self.project_root,
            workspace=workspace,
            evaluator_private_root=self.evaluator_private_root,
            task_id=request["task_id"],
            attempt_id=request["attempt_id"],
            expected_model=self.policy.model,
            expected_api_base=self.policy.api_base,
            expected_provider=self.policy.provider,
            expected_artifact_root_sha256=request["artifact_root_sha256"],
            timeout_seconds=self.timeout_seconds,
            judge_env_file=self.judge_env_file,
        )
        result.judge_outcome.validate(self.policy)
        return result.judge_outcome


@dataclass(frozen=True)
class ProductionCommunityEvaluatorBundle:
    """Typed construction result consumed by the production evaluator port."""

    policy: JudgePolicyIdentity
    executor: DurableCommunityJudgeExecutor
    operation: DurableEvaluatorOperation
    scorer_tracked_tree_sha256: str


def scorer_tracked_tree_sha256(
    researchclawbench_root: str | Path,
    *,
    expected_commit: str,
) -> str:
    """Hash the clean tracked ``evaluation`` tree without untracked task data."""

    root = Path(researchclawbench_root).resolve(strict=True)

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ("git", "-C", os.fspath(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or result.stderr:
            raise EvaluatorError("ResearchClawBench scorer Git identity is unavailable")
        return result.stdout.strip()

    head = git("rev-parse", "HEAD")
    if head != expected_commit:
        raise EvaluatorError("ResearchClawBench scorer commit differs from protocol")
    if git("status", "--porcelain=v1", "--untracked-files=no", "--", "evaluation"):
        raise EvaluatorError("ResearchClawBench scorer tracked tree is dirty")
    tree_object = git("rev-parse", f"{head}:evaluation")
    if not re_full_hex(tree_object, lengths=(40, 64)):
        raise EvaluatorError("ResearchClawBench scorer tree object is invalid")
    return hashlib.sha256(
        json.dumps(
            {
                "scorer_commit": head,
                "tracked_tree_object": tree_object,
                "tree_path": "evaluation",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def re_full_hex(value: str, *, lengths: tuple[int, ...]) -> bool:
    return len(value) in lengths and all(character in "0123456789abcdef" for character in value)


def build_production_community_evaluator(
    *,
    config: ExperimentConfig,
    authority_root: str | Path,
    partitioner: FeedbackPartitioner = soft_judge_partitioner,
) -> ProductionCommunityEvaluatorBundle:
    """Build the durable scorer port dependencies from frozen protocol.

    This helper reads no secret value.  It only selects the evaluator-only
    ``judge.env`` path after checking its ownership mode; the child process
    performs the closed secret load.
    """

    root = Path(authority_root).resolve(strict=False)
    if root != config.experiment_root and not root.is_relative_to(config.experiment_root):
        raise EvaluatorError("durable evaluator authority root escapes the experiment")
    scorer_sha256 = scorer_tracked_tree_sha256(
        config.researchclawbench_root,
        expected_commit=str(config.require("judge.scorer_commit")),
    )
    policy = JudgePolicyIdentity.from_protocol(
        config.require("judge"), scorer_sha256=scorer_sha256
    )
    secret_directory = config.experiment_root / "secrets"
    secret_file = secret_directory / "judge.env"
    selected_secret: Path | None = None
    if secret_file.exists():
        directory_metadata = os.stat(secret_directory, follow_symlinks=False)
        file_metadata = os.stat(secret_file, follow_symlinks=False)
        if (
            secret_directory.is_symlink()
            or secret_file.is_symlink()
            or not stat.S_ISDIR(directory_metadata.st_mode)
            or not stat.S_ISREG(file_metadata.st_mode)
            or directory_metadata.st_uid != os.geteuid()
            or file_metadata.st_uid != os.geteuid()
            or file_metadata.st_nlink != 1
            or (directory_metadata.st_mode & 0o777) != 0o700
            or (file_metadata.st_mode & 0o777) != 0o600
        ):
            raise EvaluatorError("Judge secret path permissions are unsafe")
        selected_secret = secret_file
    scorer_private = root / "scorer_private"
    scorer_private.mkdir(parents=True, exist_ok=True, mode=0o700)
    scorer_private_metadata = os.stat(scorer_private, follow_symlinks=False)
    if (
        scorer_private.is_symlink()
        or not stat.S_ISDIR(scorer_private_metadata.st_mode)
        or scorer_private_metadata.st_uid != os.geteuid()
    ):
        raise EvaluatorError("evaluator private scorer root is unsafe")
    os.chmod(scorer_private, 0o700)
    executor = DurableCommunityJudgeExecutor(
        project_root=config.project_root,
        evaluator_private_root=scorer_private,
        policy=policy,
        judge_env_file=selected_secret,
    )
    operation = DurableEvaluatorOperation(
        root / "authority", executor=executor, partitioner=partitioner
    )
    return ProductionCommunityEvaluatorBundle(
        policy=policy,
        executor=executor,
        operation=operation,
        scorer_tracked_tree_sha256=scorer_sha256,
    )


class DurableCommunityEvaluatorPort:
    """``ProductionOperationPort``-compatible durable evaluator adapter."""

    def __init__(self, bundle: ProductionCommunityEvaluatorBundle) -> None:
        self.bundle = bundle

    @staticmethod
    def _closed_request(request: dict[str, Any]) -> dict[str, Any]:
        candidate = request.get("candidate")
        validation = request.get("validation")
        if not isinstance(candidate, dict) or not isinstance(validation, dict):
            raise EvaluatorError("production evaluator request is incomplete")
        run_id = candidate.get("run_id")
        if not isinstance(run_id, str) or "_a" not in run_id:
            raise EvaluatorError("candidate run identity is invalid")
        task_id = run_id.split("_a", 1)[0]
        if task_id not in FROZEN_TASKS:
            raise EvaluatorError("candidate task is outside Community training")
        return {
            "task_id": task_id,
            "attempt_id": run_id,
            "session_id": candidate.get("session_id"),
            "completed_dataset_id": candidate.get("dataset_id"),
            "dataset_revision": candidate.get("dataset_revision"),
            "validator_receipt_sha256": validation.get("content_sha256"),
            "artifact_root_sha256": validation.get("artifact_root_sha256"),
            "candidate_runtime_seconds": candidate.get("runtime_seconds"),
            "generic_failure_tags": [],
            "candidate_output_root": candidate.get("candidate_output_root"),
        }

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        result = self.bundle.operation.recover(
            request=self._closed_request(request),
            idempotency_key=idempotency_key,
            policy=self.bundle.policy,
        )
        return None if result is None else dict(result.public_receipt)

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        result = self.bundle.operation.execute(
            request=self._closed_request(request),
            idempotency_key=idempotency_key,
            policy=self.bundle.policy,
        )
        return dict(result.public_receipt)

    def read_feedback_for_attachment(
        self, *, idempotency_key: str, expected_sha256: str
    ) -> dict[str, Any]:
        return self.bundle.operation.read_feedback_for_attachment(
            idempotency_key=idempotency_key,
            expected_sha256=expected_sha256,
        )


def run_community_evaluator(
    *,
    project_root: str | Path,
    workspace: str | Path,
    evaluator_private_root: str | Path,
    task_id: str,
    attempt_id: str,
    timeout_seconds: int = 1800,
    judge_env_file: str | Path | None = None,
) -> dict[str, Any]:
    """Backward-compatible private scorer entrypoint.

    Production durable execution should use
    :func:`run_community_evaluator_detailed` so the protocol-pinned model and
    API base are checked in the evaluator-only worker.
    """

    return _run_community_evaluator(
        project_root=project_root,
        workspace=workspace,
        evaluator_private_root=evaluator_private_root,
        task_id=task_id,
        attempt_id=attempt_id,
        timeout_seconds=timeout_seconds,
        judge_env_file=judge_env_file,
        expected_model=None,
        expected_api_base=None,
        expected_provider="openai_compatible",
    ).raw_score


def run_community_evaluator_detailed(
    *,
    project_root: str | Path,
    workspace: str | Path,
    evaluator_private_root: str | Path,
    task_id: str,
    attempt_id: str,
    expected_model: str,
    expected_api_base: str,
    expected_provider: str,
    expected_artifact_root_sha256: str | None = None,
    timeout_seconds: int = 1800,
    judge_env_file: str | Path | None = None,
) -> CommunityEvaluatorExecution:
    if not expected_model or not expected_api_base or expected_provider != "openai_compatible":
        raise EvaluatorError("evaluator expected Judge identity is invalid")
    return _run_community_evaluator(
        project_root=project_root,
        workspace=workspace,
        evaluator_private_root=evaluator_private_root,
        task_id=task_id,
        attempt_id=attempt_id,
        timeout_seconds=timeout_seconds,
        judge_env_file=judge_env_file,
        expected_model=expected_model,
        expected_api_base=expected_api_base,
        expected_provider=expected_provider,
        expected_artifact_root_sha256=expected_artifact_root_sha256,
        allowed_task_ids=FROZEN_TASKS,
    )


def run_official_evaluator_detailed(
    *,
    project_root: str | Path,
    workspace: str | Path,
    evaluator_private_root: str | Path,
    task_id: str,
    attempt_id: str,
    expected_model: str,
    expected_api_base: str,
    expected_provider: str,
    official_task_ids: tuple[str, ...],
    expected_artifact_root_sha256: str | None = None,
    timeout_seconds: int = 1800,
    judge_env_file: str | Path | None = None,
) -> CommunityEvaluatorExecution:
    """Run the same durable Judge worker against an exact official allowlist."""

    if (
        len(official_task_ids) != 40
        or len(set(official_task_ids)) != 40
        or task_id not in official_task_ids
    ):
        raise EvaluatorError("official evaluator task allowlist is invalid")
    if not expected_model or not expected_api_base or expected_provider != "openai_compatible":
        raise EvaluatorError("evaluator expected Judge identity is invalid")
    return _run_community_evaluator(
        project_root=project_root,
        workspace=workspace,
        evaluator_private_root=evaluator_private_root,
        task_id=task_id,
        attempt_id=attempt_id,
        timeout_seconds=timeout_seconds,
        judge_env_file=judge_env_file,
        expected_model=expected_model,
        expected_api_base=expected_api_base,
        expected_provider=expected_provider,
        expected_artifact_root_sha256=expected_artifact_root_sha256,
        allowed_task_ids=official_task_ids,
    )


def _run_community_evaluator(
    *,
    project_root: str | Path,
    workspace: str | Path,
    evaluator_private_root: str | Path,
    task_id: str,
    attempt_id: str,
    timeout_seconds: int,
    judge_env_file: str | Path | None,
    expected_model: str | None,
    expected_api_base: str | None,
    expected_provider: str,
    expected_artifact_root_sha256: str | None = None,
    allowed_task_ids: tuple[str, ...] = FROZEN_TASKS,
) -> CommunityEvaluatorExecution:
    if task_id not in allowed_task_ids:
        raise EvaluatorError("evaluator task is outside the exact task allowlist")
    if (
        _ATTEMPT_ID.fullmatch(attempt_id) is None
        or not attempt_id.startswith(f"{task_id}_a")
    ):
        raise EvaluatorError("evaluator attempt identity is unsafe or mismatched")
    source_input = Path(workspace)
    source_metadata = os.stat(source_input, follow_symlinks=False)
    if source_input.is_symlink() or not stat.S_ISDIR(source_metadata.st_mode):
        raise EvaluatorError("candidate output root is unsafe")
    source = source_input.resolve(strict=True)
    try:
        _ = tuple(iter_regular_files(source))
    except UnsafePathError as error:
        raise EvaluatorError("candidate output contains an unsafe filesystem entry") from error
    if any(
        part.casefold() in _EVALUATOR_FORBIDDEN_SOURCE_NAMES
        for path in (source, *source.rglob("*"))
        for part in path.relative_to(source).parts
    ):
        raise EvaluatorError("candidate output contains evaluator-hidden content")
    if expected_artifact_root_sha256 is not None and (
        re.fullmatch(r"[0-9a-f]{64}", expected_artifact_root_sha256) is None
        or tree_sha256(source) != expected_artifact_root_sha256
    ):
        raise EvaluatorError("candidate artifact authority hash differs")
    private_input = Path(evaluator_private_root)
    private_metadata = os.stat(private_input, follow_symlinks=False)
    if (
        private_input.is_symlink()
        or not stat.S_ISDIR(private_metadata.st_mode)
        or private_metadata.st_uid != os.geteuid()
    ):
        raise EvaluatorError("evaluator private root is unsafe")
    private_root = private_input.resolve(strict=True)
    os.chmod(private_root, 0o700)
    destination = private_root / attempt_id / "workspace"
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    shutil.copytree(source, destination, symlinks=False)
    raw_path = destination.parent / "raw_community_score.json"
    metadata_path = destination.parent / "judge_execution_metadata.json"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(Path(project_root).resolve(strict=True)),
    }
    names = ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")
    if all(os.environ.get(name) for name in names):
        env.update({name: os.environ[name] for name in names})
    elif judge_env_file is not None:
        secret = Path(judge_env_file).resolve(strict=True)
        env["OPENEVO_JUDGE_ENV_FILE"] = os.fspath(secret)
    else:
        raise EvaluatorError("judge environment is incomplete")
    worker_stdout = destination.parent / "evaluator_stdout.log"
    worker_stderr = destination.parent / "evaluator_stderr.log"
    command = [
        sys.executable,
        "-m",
        "openevo_researchclawbench.community_evaluator",
        "--worker",
        "--project-root",
        str(Path(project_root).resolve(strict=True)),
        "--workspace",
        str(destination),
        "--raw-output",
        str(raw_path),
        "--worker-stdout",
        str(worker_stdout),
        "--worker-stderr",
        str(worker_stderr),
        "--execution-metadata",
        str(metadata_path),
        "--expected-judge-provider",
        expected_provider,
        "--expected-task-id",
        task_id,
        "--expected-attempt-id",
        attempt_id,
    ]
    if expected_model is not None:
        command.extend(("--expected-judge-model", expected_model))
    if expected_api_base is not None:
        command.extend(("--expected-judge-api-base", expected_api_base))
    proc = subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout_seconds,
        check=False,
    )
    # Bootstrap output must not contain secret values.  The worker captures and
    # redacts scorer output itself; non-empty bootstrap streams therefore fail
    # closed instead of being persisted.
    if proc.stdout or proc.stderr:
        raise EvaluatorError("evaluator bootstrap emitted untrusted output")
    if proc.returncode != 0 or not raw_path.is_file() or not metadata_path.is_file():
        raise EvaluatorError(f"community scorer failed with exit code {proc.returncode}")
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or "error" in payload
        or type(payload.get("total_score")) not in {int, float}
    ):
        raise EvaluatorError("community scorer output is invalid")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or set(metadata) != {
        "api_base",
        "api_key_present",
        "cost_total_usd",
        "model",
        "provider",
        "request_count",
        "secret_recorded",
        "usage",
    }:
        raise EvaluatorError("Judge execution metadata is invalid")
    if (
        metadata["provider"] != expected_provider
        or metadata["api_key_present"] is not True
        or metadata["secret_recorded"] is not False
        or (expected_model is not None and metadata["model"] != expected_model)
        or (expected_api_base is not None and metadata["api_base"] != expected_api_base)
        or metadata["request_count"] != UNAVAILABLE
        or metadata["usage"] != UNAVAILABLE
        or metadata["cost_total_usd"] != UNAVAILABLE
    ):
        raise EvaluatorError("Judge execution identity differs from protocol")
    outcome = JudgeExecutionOutcome(
        raw_score=payload,
        runtime_identity=JudgeRuntimeIdentity(
            provider=metadata["provider"],
            api_base=metadata["api_base"],
            model=metadata["model"],
            api_key_present=metadata["api_key_present"],
        ),
        # The official scorer/structai boundary currently does not expose
        # per-request accounting.  Never replace these values with a guessed
        # checklist length, one request, or zero cost.
        request_count=metadata["request_count"],
        usage=metadata["usage"],
        cost_total_usd=metadata["cost_total_usd"],
    )
    return CommunityEvaluatorExecution(raw_score=payload, judge_outcome=outcome)


def _load_judge_environment() -> tuple[str, ...]:
    names = ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")
    secret_path = os.environ.get("OPENEVO_JUDGE_ENV_FILE")
    if secret_path:
        source = Path(secret_path)
        metadata = os.stat(source, follow_symlinks=False)
        parent_metadata = os.stat(source.parent, follow_symlinks=False)
        if (
            source.is_symlink()
            or source.parent.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or parent_metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or (metadata.st_mode & 0o777) != 0o600
            or (parent_metadata.st_mode & 0o777) != 0o700
        ):
            raise EvaluatorError("judge environment file is unsafe")
        path = source.resolve(strict=True)
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            if separator != "=" or key not in names or key in values:
                raise EvaluatorError("judge environment file has an invalid closed key set")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if not value or "\x00" in value or "\n" in value or "\r" in value:
                raise EvaluatorError("judge environment file contains an invalid value")
            values[key] = value
        if set(values) != set(names):
            raise EvaluatorError("judge environment file is incomplete")
        os.environ.update(values)
    if not all(os.environ.get(name) for name in names):
        raise EvaluatorError("judge environment is incomplete")
    return tuple(os.environ[name] for name in names)


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        remaining = memoryview(value.encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short evaluator private write")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    _write_private_text(
        path,
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _credential_preflight(
    *,
    project_root: Path,
    output: Path,
    expected_judge_model: str,
    expected_judge_api_base: str,
    expected_judge_provider: str,
    evaluator_dependency_lock: Path,
) -> int:
    """Validate evaluator-only Judge identity without making a request.

    The process may read the private credential file, but the receipt exposes
    only presence and the non-secret provider/model/base identity.  This keeps
    the training supervisor from accepting a merely well-permissioned yet
    misconfigured ``judge.env`` before the first paid Candidate is started.
    """

    dependency_lock = validate_evaluator_dependency_lock(
        evaluator_dependency_lock
    )
    dependency_file_sha256 = hashlib.sha256(
        evaluator_dependency_lock.read_bytes()
    ).hexdigest()
    scorer = _scorer_installation_authority(project_root)
    _load_judge_environment()
    actual_api_base = os.environ["JUDGE_API_BASE"]
    actual_model = os.environ["JUDGE_MODEL_NAME"]
    if (
        expected_judge_provider != "openai_compatible"
        or actual_model != expected_judge_model
        or actual_api_base != expected_judge_api_base
    ):
        raise EvaluatorError("Judge identity differs from protocol")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    _write_private_json(
        output,
        {
            "schema_version": "openevo.researchclawbench.judge_scorer_preflight.v2",
            "api_base": actual_api_base,
            "api_key_present": True,
            "model": actual_model,
            "provider": expected_judge_provider,
            "scorer_import_ready": True,
            "scorer_module_origin": scorer["scorer_module_origin"],
            "structai_version": scorer["structai_version"],
            "evaluator_dependency_lock_sha256": dependency_file_sha256,
            "evaluator_dependency_content_sha256": dependency_lock[
                "content_sha256"
            ],
            "tasks_dir_authority_matches": True,
            "tasks_dir_relative": scorer["tasks_dir_relative"],
            "model_slug_preserved": "/" in actual_model,
            "judge_request_started": False,
            "model_started": False,
            "secret_recorded": False,
        },
    )
    return 0


def _scorer_installation_authority(project_root: Path) -> dict[str, Any]:
    """Attest the exact scorer package, hidden task root, and dependency pin."""

    parent = str(project_root)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        from ResearchClawBench.evaluation import config as scorer_config
        from ResearchClawBench.evaluation import score as scorer_module
    except ImportError as error:
        raise EvaluatorError("ResearchClawBench scorer dependency is unavailable") from error
    try:
        structai_version = version("structai")
    except PackageNotFoundError as error:
        raise EvaluatorError("ResearchClawBench scorer dependency is unavailable") from error
    if structai_version != _PINNED_STRUCTAI_VERSION:
        raise EvaluatorError("ResearchClawBench scorer dependency version differs")

    expected_repository = (project_root / "ResearchClawBench").resolve(strict=True)
    expected_evaluation = (expected_repository / "evaluation").resolve(strict=True)
    score_origin = Path(scorer_module.__file__).resolve(strict=True)
    config_origin = Path(scorer_config.__file__).resolve(strict=True)
    try:
        tasks_dir = Path(scorer_config.TASKS_DIR).resolve(strict=True)
        expected_tasks_dir = (expected_repository / "tasks").resolve(strict=True)
    except OSError as error:
        raise EvaluatorError("ResearchClawBench TASKS_DIR authority is unavailable") from error
    if (
        not score_origin.is_relative_to(expected_evaluation)
        or not config_origin.is_relative_to(expected_evaluation)
        or tasks_dir != expected_tasks_dir
    ):
        raise EvaluatorError("ResearchClawBench scorer or TASKS_DIR authority differs")
    return {
        "score_workspace": scorer_module.score_workspace,
        "scorer_module_origin": score_origin.relative_to(project_root).as_posix(),
        "structai_version": structai_version,
        "tasks_dir": tasks_dir,
        "tasks_dir_relative": tasks_dir.relative_to(project_root).as_posix(),
    }


def _resolve_scorer_authority(
    project_root: Path,
    workspace: Path,
    expected_task_id: str,
    expected_attempt_id: str,
) -> Any:
    """Bind the worker to the pinned scorer and evaluator-only hidden task root."""

    authority = _scorer_installation_authority(project_root)
    tasks_dir = authority["tasks_dir"]
    meta = json.loads((workspace / "_meta.json").read_text(encoding="utf-8"))
    if (
        not isinstance(meta, dict)
        or meta.get("task_id") != expected_task_id
        or meta.get("run_id") != expected_attempt_id
    ):
        raise EvaluatorError("evaluator workspace metadata differs")
    target_study = (tasks_dir / expected_task_id / "target_study").resolve(strict=True)
    checklist = target_study / "checklist.json"
    if (
        not target_study.is_relative_to(tasks_dir)
        or checklist.is_symlink()
        or not checklist.is_file()
    ):
        raise EvaluatorError("evaluator checklist authority is unavailable or unsafe")
    return authority["score_workspace"]


def _worker(
    project_root: Path,
    workspace: Path,
    raw_output: Path,
    worker_stdout: Path,
    worker_stderr: Path,
    execution_metadata: Path,
    expected_judge_model: str | None,
    expected_judge_api_base: str | None,
    expected_judge_provider: str,
    expected_task_id: str,
    expected_attempt_id: str,
) -> int:
    secrets_to_redact = _load_judge_environment()
    actual_api_base = os.environ["JUDGE_API_BASE"]
    actual_model = os.environ["JUDGE_MODEL_NAME"]
    if expected_judge_provider != "openai_compatible":
        raise EvaluatorError("Judge provider differs from the evaluator contract")
    if expected_judge_model is not None and actual_model != expected_judge_model:
        raise EvaluatorError("Judge model differs from protocol")
    if expected_judge_api_base is not None and actual_api_base != expected_judge_api_base:
        raise EvaluatorError("Judge API base differs from protocol")
    score_workspace = _resolve_scorer_authority(
        project_root,
        workspace,
        expected_task_id,
        expected_attempt_id,
    )

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        payload = score_workspace(workspace)

    def redacted(value: str) -> str:
        for secret in secrets_to_redact:
            value = value.replace(secret, "[REDACTED]")
        return value

    _write_private_text(worker_stdout, redacted(stdout_buffer.getvalue()))
    _write_private_text(worker_stderr, redacted(stderr_buffer.getvalue()))
    encoded_payload = json.dumps(payload, sort_keys=True, allow_nan=False)
    if any(secret and secret in encoded_payload for secret in secrets_to_redact):
        raise EvaluatorError("Judge result contains secret material")
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    _write_private_json(raw_output, payload)
    _write_private_json(
        execution_metadata,
        {
            "api_base": actual_api_base,
            "api_key_present": True,
            "cost_total_usd": UNAVAILABLE,
            "model": actual_model,
            "provider": expected_judge_provider,
            "request_count": UNAVAILABLE,
            "secret_recorded": False,
            "usage": UNAVAILABLE,
        },
    )
    return 0 if isinstance(payload, dict) and "error" not in payload else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--credential-preflight-output", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--raw-output", type=Path)
    parser.add_argument("--worker-stdout", type=Path)
    parser.add_argument("--worker-stderr", type=Path)
    parser.add_argument("--execution-metadata", type=Path)
    parser.add_argument("--expected-judge-model")
    parser.add_argument("--expected-judge-api-base")
    parser.add_argument("--expected-judge-provider", default="openai_compatible")
    parser.add_argument("--evaluator-dependency-lock", type=Path)
    parser.add_argument("--expected-task-id")
    parser.add_argument("--expected-attempt-id")
    args = parser.parse_args(argv)
    if args.credential_preflight_output is not None:
        if (
            args.worker
            or args.project_root is None
            or args.expected_judge_model is None
            or args.expected_judge_api_base is None
            or args.evaluator_dependency_lock is None
        ):
            parser.error("credential preflight requires only the expected Judge identity")
        return _credential_preflight(
            project_root=args.project_root.resolve(strict=True),
            output=args.credential_preflight_output.resolve(strict=False),
            expected_judge_model=args.expected_judge_model,
            expected_judge_api_base=args.expected_judge_api_base,
            expected_judge_provider=args.expected_judge_provider,
            evaluator_dependency_lock=args.evaluator_dependency_lock.resolve(
                strict=True
            ),
        )
    if not args.worker or not all(
        (
            args.project_root,
            args.workspace,
            args.raw_output,
            args.worker_stdout,
            args.worker_stderr,
            args.execution_metadata,
            args.expected_task_id,
            args.expected_attempt_id,
        )
    ):
        parser.error("worker arguments are required")
    return _worker(
        args.project_root.resolve(strict=True),
        args.workspace.resolve(strict=True),
        args.raw_output.resolve(strict=False),
        args.worker_stdout.resolve(strict=False),
        args.worker_stderr.resolve(strict=False),
        args.execution_metadata.resolve(strict=False),
        args.expected_judge_model,
        args.expected_judge_api_base,
        args.expected_judge_provider,
        args.expected_task_id,
        args.expected_attempt_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
