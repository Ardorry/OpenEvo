"""Exactly-once unified Judge authority for the frozen official 40-task run."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .community_evaluator import (
    ProductionCommunityEvaluatorBundle,
    build_production_community_evaluator,
    run_official_evaluator_detailed,
)
from .config import ExperimentConfig
from .durable_evaluator_operation import (
    DurableEvaluatorOperation,
    FeedbackClass,
    FeedbackPartition,
    JudgeExecutionOutcome,
    JudgeInvocation,
    JudgePolicyIdentity,
)
from .formal_v11 import OFFICIAL_FROZEN_POLICY, OFFICIAL_V11_BUDGET
from .training_state_store import canonical_bytes, canonical_sha256

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_TASK_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*_[0-9]{3}\Z")


class OfficialUnifiedScorerError(RuntimeError):
    """The frozen official scorer authority failed closed."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise OfficialUnifiedScorerError("official private receipt conflicts")
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, data)
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
            raise OfficialUnifiedScorerError("official private receipt raced")
    finally:
        temporary.unlink(missing_ok=True)


def official_withheld_partitioner(
    _request: dict[str, Any], _outcome: JudgeExecutionOutcome
) -> FeedbackPartition:
    """Keep every per-task score inside the evaluator-only authority."""

    return FeedbackPartition(
        feedback_class=FeedbackClass.SOFT_JUDGE,
        global_feedback={},
        task_local_feedback={},
    )


@dataclass(frozen=True)
class OfficialJudgeExecutor:
    project_root: Path
    evaluator_private_root: Path
    policy: JudgePolicyIdentity
    official_task_ids: tuple[str, ...]
    judge_env_file: Path | None
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
            raise OfficialUnifiedScorerError("official Judge invocation identity drifted")
        execution = run_official_evaluator_detailed(
            project_root=self.project_root,
            workspace=request["candidate_output_root"],
            evaluator_private_root=self.evaluator_private_root,
            task_id=request["task_id"],
            attempt_id=request["attempt_id"],
            expected_model=self.policy.model,
            expected_api_base=self.policy.api_base,
            expected_provider=self.policy.provider,
            official_task_ids=self.official_task_ids,
            expected_artifact_root_sha256=request["artifact_root_sha256"],
            timeout_seconds=self.timeout_seconds,
            judge_env_file=self.judge_env_file,
        )
        execution.judge_outcome.validate(self.policy)
        return execution.judge_outcome


class OfficialUnifiedScorerStore:
    """Durable aggregate journal; per-task invocation fences live separately."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.stat(self.root, follow_symlinks=False)
        if (
            self.root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise OfficialUnifiedScorerError("official scorer root is unsafe")
        os.chmod(self.root, 0o700)
        self.db_path = self.root / "official-unified-scorer.sqlite3"
        descriptor = os.open(
            self.db_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS official_scores ("
                "idempotency_key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, "
                "phase TEXT NOT NULL CHECK(phase IN ('RUNNING','SUCCEEDED')), "
                "result_json BLOB, result_sha256 TEXT, private_path TEXT, "
                "private_sha256 TEXT)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def begin(self, key: str, request_sha256: str) -> dict[str, Any]:
        if _IDENTITY.fullmatch(key) is None or _SHA256.fullmatch(request_sha256) is None:
            raise OfficialUnifiedScorerError("official scorer identity is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM official_scores WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO official_scores VALUES (?, ?, 'RUNNING', NULL, NULL, NULL, NULL)",
                    (key, request_sha256),
                )
                row = connection.execute(
                    "SELECT * FROM official_scores WHERE idempotency_key = ?", (key,)
                ).fetchone()
            connection.commit()
        assert row is not None
        if row["request_sha256"] != request_sha256:
            raise OfficialUnifiedScorerError("official scorer request identity conflicts")
        return dict(row)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM official_scores WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return None if row is None else dict(row)

    def complete(
        self,
        *,
        key: str,
        result: dict[str, Any],
        private_path: Path,
        private_sha256: str,
    ) -> None:
        result_bytes = canonical_bytes(result)
        result_sha256 = hashlib.sha256(result_bytes).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM official_scores WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is None:
                raise OfficialUnifiedScorerError("official scorer intent is absent")
            if row["phase"] == "SUCCEEDED":
                if (
                    row["result_sha256"] != result_sha256
                    or row["private_sha256"] != private_sha256
                    or row["private_path"] != str(private_path)
                ):
                    raise OfficialUnifiedScorerError("official score completion conflicts")
                connection.commit()
                return
            connection.execute(
                "UPDATE official_scores SET phase='SUCCEEDED', result_json=?, "
                "result_sha256=?, private_path=?, private_sha256=? "
                "WHERE idempotency_key=? AND phase='RUNNING'",
                (result_bytes, result_sha256, str(private_path), private_sha256, key),
            )
            connection.commit()


class DurableOfficialUnifiedScorerAuthority:
    """Concrete exactly-once implementation of ``OfficialUnifiedScorerAuthority``."""

    def __init__(
        self,
        *,
        root: str | Path,
        official_run_root: str | Path,
        official_task_ids: tuple[str, ...],
        policy: JudgePolicyIdentity,
        evaluator_operation: DurableEvaluatorOperation,
        official_budget: dict[str, Any],
    ) -> None:
        if (
            len(official_task_ids) != 40
            or len(set(official_task_ids)) != 40
            or any(_TASK_ID.fullmatch(task) is None for task in official_task_ids)
        ):
            raise ValueError("official scorer requires an exact 40-task allowlist")
        if official_budget != OFFICIAL_V11_BUDGET:
            raise ValueError("official scorer budget differs from the closed v11 budget")
        self.root = Path(root).resolve(strict=False)
        self.official_run_root = Path(official_run_root).resolve(strict=False)
        self.official_task_ids = official_task_ids
        self.official_allowlist_sha256 = canonical_sha256(list(official_task_ids))
        self.policy = policy
        self.official_budget = dict(official_budget)
        self.evaluator_operation = evaluator_operation
        self.store = OfficialUnifiedScorerStore(self.root)
        self.private_root = self.root / "private"
        self.private_root.mkdir(mode=0o700, exist_ok=True)

    def _closed_request(self, request: dict[str, Any]) -> dict[str, Any]:
        manifests = request.get("run_manifests")
        if (
            not isinstance(manifests, list)
            or len(manifests) != 40
            or tuple(item.get("task_id") for item in manifests)
            != self.official_task_ids
            or len({item.get("task_id") for item in manifests}) != 40
            or request.get("official_policy") != OFFICIAL_FROZEN_POLICY
            or request.get("runs_per_attempt") != 1
            or request.get("pass_at_k") != 1
            or request.get("scoring_mode") != "unified_after_all_40_sealed"
            or request.get("official_run_set_sha256") != canonical_sha256(manifests)
        ):
            raise OfficialUnifiedScorerError("official sealed run set is incomplete")
        for index, manifest in enumerate(manifests):
            if (
                not isinstance(manifest, dict)
                or type(manifest.get("task_index")) is not int
                or manifest["task_index"] != index
                or type(manifest.get("attempt_index")) is not int
                or manifest["attempt_index"] != 0
                or manifest.get("sealed") is not True
                or type(manifest.get("completed")) is not bool
                or type(manifest.get("artifact_valid")) is not bool
                or any(
                    not isinstance(manifest.get(key), str)
                    or _IDENTITY.fullmatch(manifest[key]) is None
                    for key in (
                        "run_id",
                        "session_id",
                        "dataset_id",
                        "dataset_revision",
                    )
                )
                or _SHA256.fullmatch(str(manifest.get("validator_receipt_sha256")))
                is None
                or any(
                    manifest.get(key) is not expected
                    for key, expected in OFFICIAL_FROZEN_POLICY.items()
                )
            ):
                raise OfficialUnifiedScorerError("official task manifest is invalid")
            if manifest["artifact_valid"]:
                output = Path(str(manifest.get("candidate_output_root"))).resolve(strict=True)
                expected = self.official_run_root / "runs" / manifest["run_id"]
                if (
                    output != expected.resolve(strict=True)
                    or output.is_symlink()
                    or not output.is_dir()
                    or _SHA256.fullmatch(str(manifest.get("artifact_root_sha256")))
                    is None
                    or type(manifest.get("runtime_seconds")) not in {int, float}
                    or float(manifest["runtime_seconds"]) < 0
                    or float(manifest["runtime_seconds"])
                    > self.official_budget["candidate_runtime_seconds_per_task"]
                ):
                    raise OfficialUnifiedScorerError(
                        "official valid artifact authority is incomplete"
                    )
        return json.loads(canonical_bytes(request))

    def recover_unified_score(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        closed = self._closed_request(request)
        row = self.store.get(idempotency_key)
        if row is None:
            return None
        self.store.begin(idempotency_key, canonical_sha256(closed))
        if row["phase"] != "SUCCEEDED":
            return None
        result_bytes = bytes(row["result_json"])
        if hashlib.sha256(result_bytes).hexdigest() != row["result_sha256"]:
            raise OfficialUnifiedScorerError("official aggregate result hash drifted")
        private_path = Path(row["private_path"]).resolve(strict=True)
        if (
            not private_path.is_relative_to(self.private_root)
            or _file_sha256(private_path) != row["private_sha256"]
        ):
            raise OfficialUnifiedScorerError("official private result hash drifted")
        result = json.loads(result_bytes)
        if not isinstance(result, dict):
            raise OfficialUnifiedScorerError("official aggregate result is invalid")
        return result

    def execute_unified_score(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        closed = self._closed_request(request)
        self.store.begin(idempotency_key, canonical_sha256(closed))
        recovered = self.recover_unified_score(closed, idempotency_key)
        if recovered is not None:
            return recovered
        task_results: list[dict[str, Any]] = []
        judge_operations = 0
        for manifest in closed["run_manifests"]:
            if not manifest["completed"] or not manifest["artifact_valid"]:
                task_results.append(
                    {
                        "task_id": manifest["task_id"],
                        "run_id": manifest["run_id"],
                        "score": 0.0,
                        "judge_invoked": False,
                        "terminal_reason": (
                            "CANDIDATE_FAILED"
                            if not manifest["completed"]
                            else "ARTIFACT_INVALID"
                        ),
                    }
                )
                continue
            task_request = {
                "task_id": manifest["task_id"],
                "attempt_id": manifest["run_id"],
                "session_id": manifest["session_id"],
                "completed_dataset_id": manifest["dataset_id"],
                "dataset_revision": manifest["dataset_revision"],
                "validator_receipt_sha256": manifest["validator_receipt_sha256"],
                "artifact_root_sha256": manifest["artifact_root_sha256"],
                "candidate_runtime_seconds": manifest["runtime_seconds"],
                "candidate_output_root": manifest["candidate_output_root"],
                "generic_failure_tags": [],
            }
            task_key = f"{idempotency_key}:{manifest['task_id']}"
            result = self.evaluator_operation.recover(
                request=task_request,
                idempotency_key=task_key,
                policy=self.policy,
            )
            if result is None:
                result = self.evaluator_operation.execute(
                    request=task_request,
                    idempotency_key=task_key,
                    policy=self.policy,
                )
            judge_operations += 1
            task_results.append(
                {
                    "task_id": manifest["task_id"],
                    "run_id": manifest["run_id"],
                    "score": result.public_receipt["total_score"],
                    "judge_invoked": True,
                    "evaluation_receipt_id": result.operation_id,
                    "evaluation_receipt_sha256": result.public_receipt_sha256,
                    "raw_response_sha256": result.raw_response_sha256,
                }
            )
        total_score = sum(float(item["score"]) for item in task_results) / 40
        private = {
            "schema_version": "openevo.researchclawbench.official_scores_private.v1",
            "official_task_ids": list(self.official_task_ids),
            "official_allowlist_sha256": self.official_allowlist_sha256,
            "official_run_set_sha256": closed["official_run_set_sha256"],
            "judge_model": self.policy.model,
            "judge_provider": self.policy.provider,
            "task_results": task_results,
            "intermediate_feedback_withheld": True,
            "attachments_created": 0,
            "artifact_updates_performed": 0,
        }
        aggregate_id = "official-score-" + hashlib.sha256(
            idempotency_key.encode()
        ).hexdigest()[:24]
        private_path = self.private_root / f"{aggregate_id}.json"
        _write_immutable_json(private_path, private)
        private_sha256 = _file_sha256(private_path)
        result = {
            "scorer_receipt_id": aggregate_id,
            "private_receipt_path": str(private_path),
            "private_receipt_sha256": private_sha256,
            "official_task_count": 40,
            "official_run_set_sha256": closed["official_run_set_sha256"],
            "official_allowlist_sha256": self.official_allowlist_sha256,
            "runs_per_attempt": 1,
            "pass_at_k": 1,
            "feedback_released": False,
            "intermediate_feedback_withheld": True,
            "artifact_updates_performed": 0,
            "attachments_created": 0,
            "scoring_complete": True,
            "raw_judge_outputs_private": True,
            "judge_model": self.policy.model,
            "judge_provider": self.policy.provider,
            "judge_task_operations": judge_operations,
            "scored_task_operations": 40,
            "total_score": round(total_score, 6),
        }
        if not math.isfinite(result["total_score"]):
            raise OfficialUnifiedScorerError("official aggregate score is invalid")
        self.store.complete(
            key=idempotency_key,
            result=result,
            private_path=private_path,
            private_sha256=private_sha256,
        )
        return result


def build_production_official_unified_scorer(
    *,
    config: ExperimentConfig,
    authority_root: str | Path,
    official_run_root: str | Path,
    official_task_ids: tuple[str, ...],
) -> DurableOfficialUnifiedScorerAuthority:
    """Construct the concrete production authority without reading secrets."""

    root = Path(authority_root).resolve(strict=False)
    formal = config.formal_runs_v11
    if formal is None:
        raise OfficialUnifiedScorerError("official scorer requires a formal v11 protocol")
    budget = formal["official"]["budget"]
    base: ProductionCommunityEvaluatorBundle = build_production_community_evaluator(
        config=config,
        authority_root=root / "policy_bootstrap",
        partitioner=official_withheld_partitioner,
    )
    executor = OfficialJudgeExecutor(
        project_root=config.project_root,
        evaluator_private_root=root / "per_task_private",
        policy=base.policy,
        official_task_ids=official_task_ids,
        judge_env_file=base.executor.judge_env_file,
        timeout_seconds=budget["judge_runtime_seconds_per_task"],
    )
    operation = DurableEvaluatorOperation(
        root / "per_task_authority",
        executor=executor,
        partitioner=official_withheld_partitioner,
    )
    return DurableOfficialUnifiedScorerAuthority(
        root=root / "aggregate_authority",
        official_run_root=official_run_root,
        official_task_ids=official_task_ids,
        policy=base.policy,
        evaluator_operation=operation,
        official_budget=budget,
    )


__all__ = [
    "DurableOfficialUnifiedScorerAuthority",
    "OfficialJudgeExecutor",
    "OfficialUnifiedScorerError",
    "build_production_official_unified_scorer",
    "official_withheld_partitioner",
]
