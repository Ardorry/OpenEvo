"""Minimal per-item two-pass runner for ResearchClawBench community canary.

Design constraints:

- Reuse existing workspace builder, evaluator functions, artifact validator
  and TrainingStateStore.
- No unified operations forwarding layer.
- No new manifest framework, route-plan framework, bootstrap statistics or
  official per-item gate.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import yaml

from .artifact_validator import freeze_candidate_outputs, validate_workspace
from .hashing import canonical_json_sha256, iter_regular_files, sha256_file
from .run_manifest import atomic_write_json
from .training_state_store import TrainingStateStore, canonical_sha256
from .workspace import WorkspaceReceipt, build_official_workspace


MINIMAL_PROTOCOL_NAME = "researchclaw_per_item_minimal_deepseek_canary_v1"
CANARY_TASK_ID = "Life_005"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_JUDGE_MODEL = "gpt-5.1"


class MinimalStage(StrEnum):
    ITEM_INIT = "ITEM_INIT"
    BASELINE_PREPARED = "BASELINE_PREPARED"
    BASELINE_RUNNING = "BASELINE_RUNNING"
    BASELINE_SEALED = "BASELINE_SEALED"
    BASELINE_SCORED = "BASELINE_SCORED"
    EVOLUTION_RUNNING = "EVOLUTION_RUNNING"
    EVOLUTION_SEALED = "EVOLUTION_SEALED"
    EVOLVED_PREPARED = "EVOLVED_PREPARED"
    EVOLVED_RUNNING = "EVOLVED_RUNNING"
    EVOLVED_SEALED = "EVOLVED_SEALED"
    EVOLVED_SCORED = "EVOLVED_SCORED"
    ITEM_CLOSED = "ITEM_CLOSED"
    COMPLETE = "COMPLETE"
    BLOCKED_CANDIDATE_AUTH = "BLOCKED_CANDIDATE_AUTH"
    BLOCKED_CANDIDATE_QUOTA = "BLOCKED_CANDIDATE_QUOTA"
    BLOCKED_EVOLUTION_PROVIDER = "BLOCKED_EVOLUTION_PROVIDER"
    BLOCKED_JUDGE_AUTH = "BLOCKED_JUDGE_AUTH"
    BLOCKED_GT_UNAVAILABLE = "BLOCKED_GT_UNAVAILABLE"
    BLOCKED_DEEPSEEK_CREDENTIAL = "BLOCKED_DEEPSEEK_CREDENTIAL"
    FAILED = "FAILED"


_ALLOWED: dict[MinimalStage, frozenset[MinimalStage]] = {
    MinimalStage.ITEM_INIT: frozenset(
        {
            MinimalStage.BASELINE_PREPARED,
            MinimalStage.BLOCKED_GT_UNAVAILABLE,
            MinimalStage.BLOCKED_DEEPSEEK_CREDENTIAL,
            MinimalStage.FAILED,
        }
    ),
    MinimalStage.BASELINE_PREPARED: frozenset({MinimalStage.BASELINE_RUNNING, MinimalStage.FAILED}),
    MinimalStage.BASELINE_RUNNING: frozenset(
        {
            MinimalStage.BASELINE_SEALED,
            MinimalStage.BLOCKED_CANDIDATE_AUTH,
            MinimalStage.BLOCKED_CANDIDATE_QUOTA,
            MinimalStage.BLOCKED_DEEPSEEK_CREDENTIAL,
            MinimalStage.FAILED,
        }
    ),
    MinimalStage.BASELINE_SEALED: frozenset({MinimalStage.BASELINE_SCORED, MinimalStage.BLOCKED_JUDGE_AUTH, MinimalStage.FAILED}),
    MinimalStage.BASELINE_SCORED: frozenset({MinimalStage.EVOLUTION_RUNNING, MinimalStage.FAILED}),
    MinimalStage.EVOLUTION_RUNNING: frozenset(
        {
            MinimalStage.EVOLUTION_SEALED,
            MinimalStage.BLOCKED_EVOLUTION_PROVIDER,
            MinimalStage.BLOCKED_DEEPSEEK_CREDENTIAL,
            MinimalStage.FAILED,
        }
    ),
    MinimalStage.EVOLUTION_SEALED: frozenset({MinimalStage.EVOLVED_PREPARED, MinimalStage.FAILED}),
    MinimalStage.EVOLVED_PREPARED: frozenset({MinimalStage.EVOLVED_RUNNING, MinimalStage.FAILED}),
    MinimalStage.EVOLVED_RUNNING: frozenset(
        {
            MinimalStage.EVOLVED_SEALED,
            MinimalStage.BLOCKED_CANDIDATE_AUTH,
            MinimalStage.BLOCKED_CANDIDATE_QUOTA,
            MinimalStage.BLOCKED_DEEPSEEK_CREDENTIAL,
            MinimalStage.FAILED,
        }
    ),
    MinimalStage.EVOLVED_SEALED: frozenset({MinimalStage.EVOLVED_SCORED, MinimalStage.BLOCKED_JUDGE_AUTH, MinimalStage.FAILED}),
    MinimalStage.EVOLVED_SCORED: frozenset({MinimalStage.ITEM_CLOSED, MinimalStage.FAILED}),
    MinimalStage.ITEM_CLOSED: frozenset({MinimalStage.COMPLETE, MinimalStage.FAILED}),
    MinimalStage.COMPLETE: frozenset(),
    MinimalStage.BLOCKED_CANDIDATE_AUTH: frozenset(),
    MinimalStage.BLOCKED_CANDIDATE_QUOTA: frozenset(),
    MinimalStage.BLOCKED_EVOLUTION_PROVIDER: frozenset(),
    MinimalStage.BLOCKED_JUDGE_AUTH: frozenset(),
    MinimalStage.BLOCKED_GT_UNAVAILABLE: frozenset(),
    MinimalStage.BLOCKED_DEEPSEEK_CREDENTIAL: frozenset(),
    MinimalStage.FAILED: frozenset(),
}


def require_minimal_transition(source: MinimalStage, target: MinimalStage) -> None:
    if MinimalStage(target) not in _ALLOWED[MinimalStage(source)]:
        raise ValueError(f"invalid minimal transition: {source.value}->{target.value}")


@dataclass(frozen=True)
class MinimalPerItemConfig:
    path: Path
    raw: dict[str, Any]

    def __post_init__(self) -> None:
        if self.raw.get("protocol_name") != MINIMAL_PROTOCOL_NAME:
            raise ValueError("unexpected minimal protocol_name")
        if self.raw.get("task_id") != CANARY_TASK_ID:
            raise ValueError("minimal canary is locked to Life_005")
        execution = self.raw.get("execution", {})
        if int(execution.get("max_tasks", 0)) != 1:
            raise ValueError("max_tasks must be 1")
        if int(execution.get("max_provider_jobs", 0)) != 7:
            raise ValueError("max_provider_jobs must be 7")
        if int(execution.get("max_retries", -1)) != 0:
            raise ValueError("max_retries must be 0")
        if execution.get("leaderboard_eligible", True) is not False:
            raise ValueError("leaderboard_eligible must be false")

    @classmethod
    def load(cls, path: str | Path) -> "MinimalPerItemConfig":
        protocol_path = Path(path).resolve(strict=True)
        payload = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("minimal protocol must be a YAML object")
        return cls(protocol_path, payload)

    def require(self, key: str) -> Any:
        value: Any = self.raw
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise KeyError(key)
            value = value[part]
        return value

    @property
    def task_id(self) -> str:
        return str(self.require("task_id"))

    @property
    def project_root(self) -> Path:
        return Path(self.require("paths.project_root")).resolve(strict=True)

    @property
    def researchclawbench_root(self) -> Path:
        return Path(self.require("paths.researchclawbench_root")).resolve(strict=True)

    @property
    def output_root(self) -> Path:
        return Path(self.require("paths.output_root")).resolve(strict=False)

    @property
    def dry_run(self) -> bool:
        return bool(self.require("execution.dry_run"))

    @property
    def candidate_model(self) -> str:
        return str(self.require("candidate.model"))

    @property
    def judge_model(self) -> str:
        return str(self.require("judge.model"))


class CandidatePort(Protocol):
    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class EvolutionPort(Protocol):
    def evolve(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class JudgePort(Protocol):
    def evaluate(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PerItemPairedResult:
    task_id: str
    baseline_score: float | None
    evolved_score: float | None
    delta: float | None
    baseline_receipt_id: str | None
    evolved_receipt_id: str | None
    memory_artifact_id: str | None
    skill_artifact_id: str | None
    agent_system_artifact_id: str | None
    status: str | None


def _public_snapshot_sha256(task_dir: Path) -> str:
    entries: list[dict[str, Any]] = []
    for path in iter_regular_files(task_dir):
        relative = path.relative_to(task_dir).as_posix()
        if relative.casefold().startswith("target_study/"):
            continue
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return canonical_json_sha256(sorted(entries, key=lambda item: item["path"]))


def _gt_sha256(task_dir: Path) -> str:
    checklist = task_dir / "target_study" / "checklist.json"
    if not checklist.is_file() or checklist.is_symlink():
        raise ValueError("BLOCKED_GT_UNAVAILABLE")
    return sha256_file(checklist)


def _seal_workspace(workspace: Path) -> str:
    result = validate_workspace(workspace)
    if not result.passed:
        raise RuntimeError("workspace validation failed: " + ",".join(result.errors))
    freeze_candidate_outputs(workspace)
    return str(result.artifact_root_sha256)


class DryRunCandidatePort:
    """Deterministic zero-call Candidate used only for tests and dry-run."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []
        self.real_calls = 0

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        workspace = Path(str(request["workspace"]))
        (workspace / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "outputs").mkdir(exist_ok=True)
        (workspace / "outputs" / "summary.json").write_text(
            json.dumps({"dry_run": True, "estimated_effect": 0.42, "observations": 128}),
            encoding="utf-8",
        )
        report = workspace / "report"
        report.mkdir(exist_ok=True)
        (report / "images").mkdir(exist_ok=True)
        (report / "report.md").write_text(
            "\n".join(
                (
                    "# Dry-run report",
                    "",
                    "## Methodology",
                    "This deterministic dry run exercises the minimal per-item "
                    "runner without calling any model. The baseline workspace is "
                    "staged from the original public task snapshot and the "
                    "evolved workspace is rebuilt from the same snapshot with "
                    "only the item-local memory, skill, and agent-system "
                    "artifacts injected. Every transition is persisted in the "
                    "durable training state store and sealed side effects are "
                    "never re-executed on resume.",
                    "",
                    "## Results",
                    "The dry-run pipeline produced the expected deterministic "
                    "evidence. The estimated effect size is 0.42 and the "
                    "pipeline recorded 128 synthetic observations in the "
                    "output summary. The baseline score is 62.0 and the "
                    "evolved score is 67.0 in the deterministic fixture, "
                    "yielding a paired delta of 5.0.",
                    "",
                    "## Discussion",
                    "No model was invoked during this artifact. The report "
                    "exists to satisfy the adapter publication validator and "
                    "to confirm workspace sealing, hashing, and artifact "
                    "injection paths are wired correctly. A future live run "
                    "must replace this deterministic evidence with a real "
                    "transcript from the DeepSeek engineering port.",
                    "",
                    "## Additional methodology notes",
                    "The minimal runner reuses the existing official workspace "
                    "builder, the durable training state store, and the "
                    "existing evaluator boundary. Baseline and evolved "
                    "workspaces are created from the same public task "
                    "snapshot; only the three item-local artifacts differ. "
                    "The candidate environment is built from a closed "
                    "allowlist and never inherits Judge variables or the "
                    "DeepSeek token value in logs or receipts.",
                    "",
                    "## Additional results notes",
                    "The deterministic fixture returns 62.0 for baseline and "
                    "67.0 for evolved. These constants exercise the paired "
                    "delta path without any provider call. The artifact "
                    "bundle contains memory, skill, and agent-system files "
                    "with independent SHA-256 digests and a minimal "
                    "injection manifest.",
                    "",
                    "## Additional discussion notes",
                    "Side-effect idempotency is provided by the existing "
                    "training state store, so a sealed baseline or evolution "
                    "step is not repeated on resume. The canary is restricted "
                    "to Life_005, max_tasks one, max_provider_jobs seven, "
                    "and zero automatic retries. Official 40 tasks remain "
                    "frozen and are not part of this protocol.",
                    "",
                    "![figure](images/f.png)",
                )
            ),
            encoding="utf-8",
        )
        (report / "images" / "f.png").write_bytes(_MINIMAL_PNG)
        (workspace / "_meta.json").write_text(
            json.dumps({"status": "completed", "exit_code": 0}), encoding="utf-8"
        )
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "session_id": f"dry-{request['run_id']}",
            "candidate_output_root": str(workspace),
            "exit_status": 0,
            "provider": "deepseek",
            "model": self.model,
            "profile_summary": "codex-deepseek-profile",
            "deepseek_key_present": False,
            "secret_recorded": False,
        }


class DryRunEvolutionPort:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.real_calls = 0

    def evolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        artifact_root = Path(str(request["artifact_root"]))
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "skill").mkdir(exist_ok=True)
        (artifact_root / "agent_system").mkdir(exist_ok=True)
        contents = {
            "memory": "# Memory\nItem-scoped.",
            "skill": "# Skill\nItem-scoped.",
            "agent_system": "# Agent system\nItem-scoped.",
        }
        artifacts: dict[str, dict[str, Any]] = {}
        for name, text in contents.items():
            if name == "memory":
                path = artifact_root / "memory.md"
            elif name == "skill":
                path = artifact_root / "skill" / "SKILL.md"
            else:
                path = artifact_root / "agent_system" / "AGENTS.md"
            path.write_text(text, encoding="utf-8")
            artifacts[name] = {
                "task_id": request["task_id"],
                "artifact_type": name,
                "source_baseline_hash": request["baseline_sealed_hash"],
                "gt_sha256": request["gt_sha256"],
                "generation": "1",
                "content_sha256": sha256_file(path),
                "created_at": datetime.now(UTC).isoformat(),
            }
        return {
            "task_id": request["task_id"],
            "baseline_run_id": request["baseline_run_id"],
            "generation": 1,
            "gt_sha256": request["gt_sha256"],
            "artifact_root": str(artifact_root),
            "artifacts": {key: value["content_sha256"] for key, value in artifacts.items()},
            "artifact_details": artifacts,
            "secret_recorded": False,
        }


class DryRunJudgePort:
    def __init__(self, model: str = DEFAULT_JUDGE_MODEL) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []
        self.real_calls = 0

    def evaluate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        score = 62.0 if request["pass_name"] == "baseline" else 67.0
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "total_score": score,
            "model": self.model,
            "secret_recorded": False,
        }


class ExistingJudgeAdapter:
    """Thin adapter over the existing durable community evaluator function."""

    def __init__(
        self,
        config: MinimalPerItemConfig,
        *,
        evaluator_private_root: str | Path,
        timeout_seconds: int = 1800,
    ) -> None:
        self.config = config
        self.evaluator_private_root = Path(evaluator_private_root)
        self.timeout_seconds = timeout_seconds
        self.real_calls = 0

    def evaluate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from .community_evaluator import run_community_evaluator_detailed

        self.evaluator_private_root.mkdir(parents=True, exist_ok=True)
        rcb_base = os.environ.get("RCB_JUDGE_BASE_URL") or os.environ.get("JUDGE_API_BASE")
        if not rcb_base:
            raise RuntimeError("BLOCKED_JUDGE_AUTH")
        previous: dict[str, str | None] = {}
        for legacy, rcb in (
            ("JUDGE_API_KEY", "RCB_JUDGE_API_KEY"),
            ("JUDGE_API_BASE", "RCB_JUDGE_BASE_URL"),
            ("JUDGE_MODEL_NAME", "RCB_JUDGE_MODEL"),
        ):
            previous[legacy] = os.environ.get(legacy)
            if os.environ.get(rcb):
                os.environ[legacy] = os.environ[rcb]
        try:
            self.real_calls += 1
            execution = run_community_evaluator_detailed(
                project_root=self.config.researchclawbench_root.parent,
                workspace=request["candidate_output_root"],
                evaluator_private_root=self.evaluator_private_root,
                task_id=request["task_id"],
                attempt_id=request["run_id"],
                expected_model=self.config.judge_model,
                expected_api_base=rcb_base,
                expected_provider="openai_compatible",
                timeout_seconds=self.timeout_seconds,
            )
        finally:
            for legacy, value in previous.items():
                if value is None:
                    os.environ.pop(legacy, None)
                else:
                    os.environ[legacy] = value
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "total_score": float(execution.raw_score["total_score"]),
            "model": self.config.judge_model,
            "secret_recorded": False,
        }


_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class MinimalPerItemRunner:
    """Single-task baseline -> evolution -> evolved runner."""

    def __init__(
        self,
        *,
        config: MinimalPerItemConfig,
        state_root: str | Path,
        run_id: str,
        candidate_port: CandidatePort,
        evolution_port: EvolutionPort,
        judge_port: JudgePort,
        workspace_builder: Callable[..., WorkspaceReceipt] = build_official_workspace,
    ) -> None:
        self.config = config
        self.experiment_id = run_id
        self.candidate_port = candidate_port
        self.evolution_port = evolution_port
        self.judge_port = judge_port
        self.workspace_builder = workspace_builder
        self.store = TrainingStateStore(
            state_root,
            transition_fn=require_minimal_transition,
        )

    def initialize(self) -> dict[str, Any]:
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            protocol_sha256=canonical_sha256(self.config.raw),
            core_identity_sha256="engineering-canary",
            adapter_identity_sha256=canonical_sha256({"adapter": "minimal-per-item"}),
            initial_state={
                "task_id": self.config.task_id,
                "attempt": 0,
                "pass_name": None,
                "items": {},
            },
            initial_stage=MinimalStage.ITEM_INIT,
        )
        return self.status()

    def status(self) -> dict[str, Any]:
        return self.store.load(self.experiment_id)

    def _transition(
        self,
        source: MinimalStage,
        target: MinimalStage,
        suffix: str,
        *,
        updates: dict[str, Any] | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.status()
        key = f"{self.experiment_id}:{state['task_id']}:{suffix}"
        return self.store.transition(
            experiment_id=self.experiment_id,
            idempotency_key=key,
            source=source,
            target=target,
            updates=updates or {},
            receipt=receipt or {},
        )

    def _effect(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        execute: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        state = self.status()
        request_key = (
            request.get("run_id")
            or request.get("baseline_run_id")
            or state["task_id"]
        )
        key = f"{self.experiment_id}:{state['task_id']}:{kind}:{request_key}"
        observed = self.store.plan_side_effect(
            experiment_id=self.experiment_id,
            idempotency_key=key,
            kind=kind,
            request=request,
        )
        if observed["status"] == "completed":
            return observed["receipt"]
        receipt = execute(request)
        return self.store.complete_side_effect(idempotency_key=key, receipt=receipt)

    def _item_root(self) -> Path:
        return self.config.output_root / "items" / self.config.task_id

    def _run_namespace_root(self) -> Path:
        """Run-mutable item paths are namespaced by the supervisor run id.

        The official workspace builder still enforces fresh, task-scoped
        ``<task>_a0_*`` run ids inside this namespace, so two supervisor runs
        on the same task never share or overwrite workspaces, evolution
        artifacts, evolved context, or the paired result file.
        """

        return self._item_root() / "runs" / self.experiment_id

    def _stage_workspace(self, run_id: str, pass_name: str) -> WorkspaceReceipt:
        run_root = self._run_namespace_root()
        receipt = self.workspace_builder(
            self.config.researchclawbench_root,
            self.config.task_id,
            run_root,
            run_id,
            allowed_task_ids=(self.config.task_id,),
        )
        destination = run_root / f"{pass_name}_candidate"
        if destination.exists():
            if not destination.is_dir():
                raise RuntimeError("candidate destination is not a directory")
            return WorkspaceReceipt(
                task_id=receipt.task_id,
                run_id=receipt.run_id,
                workspace=receipt.workspace,
                candidate_workspace=destination,
                instructions=receipt.instructions,
                data=receipt.data,
                related_work=receipt.related_work,
                code=receipt.code,
                outputs=receipt.outputs,
                report=receipt.report,
            )
        shutil.copytree(receipt.candidate_workspace, destination, symlinks=False)
        return WorkspaceReceipt(
            task_id=receipt.task_id,
            run_id=receipt.run_id,
            workspace=receipt.workspace,
            candidate_workspace=destination,
            instructions=receipt.instructions,
            data=receipt.data,
            related_work=receipt.related_work,
            code=receipt.code,
            outputs=receipt.outputs,
            report=receipt.report,
        )

    def run_next(self) -> dict[str, Any]:
        state = self.status()
        stage = MinimalStage(state["stage"])
        task_dir = self.config.researchclawbench_root / "tasks" / self.config.task_id
        item_root = self._item_root()

        if stage is MinimalStage.ITEM_INIT:
            _gt_sha256(task_dir)
            baseline = self._stage_workspace(f"{self.config.task_id}_a0_baseline", "baseline")
            snapshot = _public_snapshot_sha256(task_dir)
            return self._transition(
                stage,
                MinimalStage.BASELINE_PREPARED,
                "baseline-prepared",
                updates={
                    "baseline_workspace": str(baseline.workspace),
                    "baseline_candidate_workspace": str(baseline.candidate_workspace),
                    "baseline_run_id": baseline.run_id,
                    "snapshot_sha256": snapshot,
                },
                receipt={"snapshot_sha256": snapshot},
            )

        if stage is MinimalStage.BASELINE_PREPARED:
            request = {
                "task_id": self.config.task_id,
                "run_id": state["baseline_run_id"],
                "pass_name": "baseline",
                "workspace": state["baseline_candidate_workspace"],
            }
            return self._transition(
                stage,
                MinimalStage.BASELINE_RUNNING,
                "baseline-start",
                updates={"active_baseline_request": request},
                receipt={"candidate_intent_persisted": True},
            )

        if stage is MinimalStage.BASELINE_RUNNING:
            try:
                candidate = self._effect(
                    kind="candidate-baseline",
                    request=state["active_baseline_request"],
                    execute=self.candidate_port.execute,
                )
                sealed_hash = _seal_workspace(
                    Path(candidate["candidate_output_root"])
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                MinimalStage.BASELINE_SEALED,
                "baseline-sealed",
                updates={
                    "baseline_receipt": candidate,
                    "baseline_sealed_hash": sealed_hash,
                },
                receipt=candidate,
            )

        if stage is MinimalStage.BASELINE_SEALED:
            request = {
                "task_id": self.config.task_id,
                "run_id": state["baseline_run_id"],
                "pass_name": "baseline",
                "candidate_output_root": state["baseline_receipt"]["candidate_output_root"],
            }
            try:
                judge = self._effect(
                    kind="judge-baseline",
                    request=request,
                    execute=self.judge_port.evaluate,
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                MinimalStage.BASELINE_SCORED,
                "baseline-scored",
                updates={"baseline_score": float(judge["total_score"])},
                receipt=judge,
            )

        if stage is MinimalStage.BASELINE_SCORED:
            request = {
                "task_id": self.config.task_id,
                "baseline_run_id": state["baseline_run_id"],
                "baseline_sealed_root": state["baseline_receipt"]["candidate_output_root"],
                "baseline_sealed_hash": state["baseline_sealed_hash"],
                "gt_path": str(task_dir / "target_study" / "checklist.json"),
                "gt_sha256": _gt_sha256(task_dir),
                "artifact_root": str(
                    item_root / "evolution_artifacts" / self.experiment_id
                ),
            }
            return self._transition(
                stage,
                MinimalStage.EVOLUTION_RUNNING,
                "evolution-start",
                updates={"active_evolution_request": request},
                receipt={"evolution_intent_persisted": True},
            )

        if stage is MinimalStage.EVOLUTION_RUNNING:
            try:
                evolution = self._effect(
                    kind="evolution",
                    request=state["active_evolution_request"],
                    execute=self.evolution_port.evolve,
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                MinimalStage.EVOLUTION_SEALED,
                "evolution-sealed",
                updates={"evolution_receipt": evolution},
                receipt=evolution,
            )

        if stage is MinimalStage.EVOLUTION_SEALED:
            evolved = self._stage_workspace(f"{self.config.task_id}_a0_evolved", "evolved")
            snapshot = _public_snapshot_sha256(task_dir)
            if snapshot != state["snapshot_sha256"]:
                return self._transition(
                    stage,
                    MinimalStage.FAILED,
                    "snapshot-drift",
                    receipt={"failure_classification": "SNAPSHOT_HASH_DRIFT"},
                )
            context_root = item_root / "evolved_context" / self.experiment_id
            context_root.mkdir(parents=True, exist_ok=True)
            artifact_root = Path(state["evolution_receipt"]["artifact_root"])
            shutil.copy2(artifact_root / "memory.md", context_root / "memory.md")
            shutil.copytree(artifact_root / "skill", context_root / "skill", dirs_exist_ok=True)
            shutil.copytree(
                artifact_root / "agent_system",
                context_root / "agent_system",
                dirs_exist_ok=True,
            )
            candidate_workspace = Path(evolved.candidate_workspace)
            shutil.copy2(context_root / "memory.md", candidate_workspace / "memory.md")
            shutil.copytree(
                context_root / "skill",
                candidate_workspace / "skills" / "skill",
                dirs_exist_ok=True,
            )
            shutil.copy2(
                context_root / "agent_system" / "AGENTS.md",
                candidate_workspace / "AGENTS.md",
            )
            return self._transition(
                stage,
                MinimalStage.EVOLVED_PREPARED,
                "evolved-prepared",
                updates={
                    "evolved_workspace": str(evolved.workspace),
                    "evolved_candidate_workspace": str(evolved.candidate_workspace),
                    "evolved_run_id": evolved.run_id,
                    "evolved_context": str(context_root),
                    "evolved_snapshot_sha256": snapshot,
                },
                receipt={"snapshot_sha256": snapshot},
            )

        if stage is MinimalStage.EVOLVED_PREPARED:
            request = {
                "task_id": self.config.task_id,
                "run_id": state["evolved_run_id"],
                "pass_name": "evolved",
                "workspace": state["evolved_candidate_workspace"],
                "context_artifact_ids": ["memory", "skill", "agent_system"],
            }
            return self._transition(
                stage,
                MinimalStage.EVOLVED_RUNNING,
                "evolved-start",
                updates={"active_evolved_request": request},
                receipt={"candidate_intent_persisted": True},
            )

        if stage is MinimalStage.EVOLVED_RUNNING:
            try:
                candidate = self._effect(
                    kind="candidate-evolved",
                    request=state["active_evolved_request"],
                    execute=self.candidate_port.execute,
                )
                sealed_hash = _seal_workspace(
                    Path(candidate["candidate_output_root"])
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                MinimalStage.EVOLVED_SEALED,
                "evolved-sealed",
                updates={
                    "evolved_receipt": candidate,
                    "evolved_sealed_hash": sealed_hash,
                },
                receipt=candidate,
            )

        if stage is MinimalStage.EVOLVED_SEALED:
            request = {
                "task_id": self.config.task_id,
                "run_id": state["evolved_run_id"],
                "pass_name": "evolved",
                "candidate_output_root": state["evolved_receipt"]["candidate_output_root"],
            }
            try:
                judge = self._effect(
                    kind="judge-evolved",
                    request=request,
                    execute=self.judge_port.evaluate,
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                MinimalStage.EVOLVED_SCORED,
                "evolved-scored",
                updates={"evolved_score": float(judge["total_score"])},
                receipt=judge,
            )

        if stage is MinimalStage.EVOLVED_SCORED:
            result = PerItemPairedResult(
                task_id=self.config.task_id,
                baseline_score=state.get("baseline_score"),
                evolved_score=state.get("evolved_score"),
                delta=(
                    state.get("evolved_score") - state.get("baseline_score")
                    if state.get("baseline_score") is not None
                    and state.get("evolved_score") is not None
                    else None
                ),
                baseline_receipt_id=state.get("baseline_receipt", {}).get("session_id"),
                evolved_receipt_id=state.get("evolved_receipt", {}).get("session_id"),
                memory_artifact_id=state["evolution_receipt"]["artifacts"].get("memory"),
                skill_artifact_id=state["evolution_receipt"]["artifacts"].get("skill"),
                agent_system_artifact_id=state["evolution_receipt"]["artifacts"].get("agent_system"),
                status="COMPLETED",
            )
            atomic_write_json(
                item_root / "paired_results" / f"{self.experiment_id}.json",
                asdict(result),
            )
            return self._transition(
                stage,
                MinimalStage.ITEM_CLOSED,
                "item-closed",
                updates={"paired_result": asdict(result)},
                receipt=asdict(result),
            )

        if stage is MinimalStage.ITEM_CLOSED:
            return self._transition(
                stage,
                MinimalStage.COMPLETE,
                "complete",
                receipt={"status": "COMPLETE"},
            )
        raise RuntimeError(f"unhandled minimal stage: {stage.value}")

    def _blocked(self, source: MinimalStage, exc: Exception) -> dict[str, Any]:
        code = getattr(exc, "code", "FAILED")
        target = {
            "BLOCKED_CANDIDATE_AUTH": MinimalStage.BLOCKED_CANDIDATE_AUTH,
            "BLOCKED_CANDIDATE_QUOTA": MinimalStage.BLOCKED_CANDIDATE_QUOTA,
            "BLOCKED_EVOLUTION_PROVIDER": MinimalStage.BLOCKED_EVOLUTION_PROVIDER,
            "BLOCKED_JUDGE_AUTH": MinimalStage.BLOCKED_JUDGE_AUTH,
            "BLOCKED_GT_UNAVAILABLE": MinimalStage.BLOCKED_GT_UNAVAILABLE,
            "BLOCKED_DEEPSEEK_CREDENTIAL": MinimalStage.BLOCKED_DEEPSEEK_CREDENTIAL,
        }.get(code, MinimalStage.FAILED)
        return self._transition(
            source,
            target,
            "blocked",
            updates={"failure_code": code, "failure_message": str(exc)},
            receipt={"failure_classification": code},
        )

    def run_until_item_closed(self, *, max_transitions: int = 100) -> dict[str, Any]:
        for _ in range(max_transitions):
            state = self.status()
            if state["stage"] in {
                MinimalStage.COMPLETE.value,
                *[item.value for item in MinimalStage if item.value.startswith("BLOCKED_")],
                MinimalStage.FAILED.value,
            }:
                return state
            self.run_next()
        raise RuntimeError("minimal runner transition budget exhausted")
