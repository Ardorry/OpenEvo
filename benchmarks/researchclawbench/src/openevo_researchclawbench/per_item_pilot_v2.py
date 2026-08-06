"""Per-item pilot v2: three community tasks with explicit artifact consumption.

Protocol ``per_item_minimal_deepseek_pilot_v2`` runs a fixed task order with:

- baseline Candidate without artifacts, baseline Judge;
- one Evolution round from baseline + task GT into memory/skill/agent-system;
- evolved Candidate that must explicitly read the three injected artifacts;
- evolved Judge and a paired result;
- durable HTTP budget reservations before every billed call.

Life_005 v1-v4 evidence is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import yaml

from .artifact_validator import (
    candidate_artifact_root_sha256,
    freeze_candidate_outputs,
    validate_workspace,
)
from .community_evaluator import (
    REQUESTED_JUDGE_PROVIDER,
    run_community_evaluator_detailed,
)
from .hashing import canonical_json_sha256, sha256_file
from .run_manifest import atomic_write_json
from .training_state_store import TrainingStateStore, canonical_sha256
from .workspace import WorkspaceReceipt, build_official_workspace

PILOT_V2_PROTOCOL_NAME = "per_item_minimal_deepseek_pilot_v2"
PILOT_TASKS = ("Astronomy_004", "Chemistry_004", "Information_005")
DEFAULT_CANDIDATE_MODEL = "deepseek-v4-flash"
DEFAULT_JUDGE_MODEL = "openai/gpt-5.1"
# Two Judge passes per task and one HTTP request per rubric item, with the
# locked community manifests at 6+6+5 items: 2 * (6+6+5) = 34 requests.
MAX_JUDGE_HTTP_REQUESTS = 34
MAX_CANDIDATE_JOBS = 6
MAX_EVOLUTION_JOBS = 9

ARTIFACT_CONSUMPTION_BLOCK = """\
## Required artifact consumption

Before starting the task you MUST read the following files inside your
workspace and use their content as context for your work:

1. AGENTS.md
2. memory.md
3. skills/skill/SKILL.md

Record the following fields in your final run metadata (`_meta.json`):
`injected_artifact_paths`, `injected_artifact_sha256`, and
`artifact_read_requested=true`.

Do not copy the ground-truth checklist or artifact contents verbatim into the
report, and do not mention Judge outputs.
"""


class PilotV2Error(RuntimeError):
    """A pilot v2 step failed closed with a typed code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PilotBudgetExhausted(PilotV2Error):
    """The durable billed-call budget cannot cover the next request."""


class PilotV2Stage(StrEnum):
    PILOT_INIT = "PILOT_INIT"
    TASK_PREPARED = "TASK_PREPARED"
    TASK_BASELINE_RUNNING = "TASK_BASELINE_RUNNING"
    TASK_BASELINE_SEALED = "TASK_BASELINE_SEALED"
    TASK_BASELINE_SCORED = "TASK_BASELINE_SCORED"
    TASK_EVOLUTION_RUNNING = "TASK_EVOLUTION_RUNNING"
    TASK_EVOLUTION_SEALED = "TASK_EVOLUTION_SEALED"
    TASK_EVOLVED_RUNNING = "TASK_EVOLVED_RUNNING"
    TASK_EVOLVED_SEALED = "TASK_EVOLVED_SEALED"
    TASK_EVOLVED_SCORED = "TASK_EVOLVED_SCORED"
    TASK_CLOSED = "TASK_CLOSED"
    PILOT_CLOSED = "PILOT_CLOSED"
    PILOT_BLOCKED = "PILOT_BLOCKED"
    PILOT_BUDGET_EXHAUSTED = "PILOT_BUDGET_EXHAUSTED"


_ALLOWED: dict[PilotV2Stage, frozenset[PilotV2Stage]] = {
    PilotV2Stage.PILOT_INIT: frozenset(
        {
            PilotV2Stage.TASK_PREPARED,
            PilotV2Stage.PILOT_CLOSED,
            PilotV2Stage.PILOT_BLOCKED,
        }
    ),
    PilotV2Stage.TASK_PREPARED: frozenset({PilotV2Stage.TASK_BASELINE_RUNNING, PilotV2Stage.PILOT_BLOCKED, PilotV2Stage.PILOT_BUDGET_EXHAUSTED}),
    PilotV2Stage.TASK_BASELINE_RUNNING: frozenset({PilotV2Stage.TASK_BASELINE_SEALED, PilotV2Stage.PILOT_BLOCKED}),
    PilotV2Stage.TASK_BASELINE_SEALED: frozenset({PilotV2Stage.TASK_BASELINE_SCORED, PilotV2Stage.PILOT_BLOCKED, PilotV2Stage.PILOT_BUDGET_EXHAUSTED}),
    PilotV2Stage.TASK_BASELINE_SCORED: frozenset({PilotV2Stage.TASK_EVOLUTION_RUNNING, PilotV2Stage.PILOT_BLOCKED}),
    PilotV2Stage.TASK_EVOLUTION_RUNNING: frozenset({PilotV2Stage.TASK_EVOLUTION_SEALED, PilotV2Stage.PILOT_BLOCKED, PilotV2Stage.PILOT_BUDGET_EXHAUSTED}),
    PilotV2Stage.TASK_EVOLUTION_SEALED: frozenset({PilotV2Stage.TASK_EVOLVED_RUNNING, PilotV2Stage.PILOT_BLOCKED}),
    PilotV2Stage.TASK_EVOLVED_RUNNING: frozenset({PilotV2Stage.TASK_EVOLVED_SEALED, PilotV2Stage.PILOT_BLOCKED}),
    PilotV2Stage.TASK_EVOLVED_SEALED: frozenset({PilotV2Stage.TASK_EVOLVED_SCORED, PilotV2Stage.PILOT_BLOCKED, PilotV2Stage.PILOT_BUDGET_EXHAUSTED}),
    PilotV2Stage.TASK_EVOLVED_SCORED: frozenset({PilotV2Stage.TASK_CLOSED, PilotV2Stage.PILOT_BLOCKED}),
    PilotV2Stage.TASK_CLOSED: frozenset({PilotV2Stage.TASK_PREPARED, PilotV2Stage.PILOT_CLOSED, PilotV2Stage.PILOT_BLOCKED}),
    PilotV2Stage.PILOT_CLOSED: frozenset(),
    PilotV2Stage.PILOT_BLOCKED: frozenset(),
    PilotV2Stage.PILOT_BUDGET_EXHAUSTED: frozenset(),
}


def require_pilot_v2_transition(source: PilotV2Stage, target: PilotV2Stage) -> None:
    if PilotV2Stage(target) not in _ALLOWED[PilotV2Stage(source)]:
        raise ValueError(f"invalid pilot v2 transition: {source.value}->{target.value}")


def append_artifact_consumption_instructions(instructions: str) -> str:
    """Append the generic artifact consumption protocol to evolved prompt."""
    return instructions.rstrip() + "\n\n" + ARTIFACT_CONSUMPTION_BLOCK


@dataclass(frozen=True)
class PilotV2Config:
    path: Path
    raw: dict[str, Any]

    def __post_init__(self) -> None:
        if self.raw.get("protocol_name") != PILOT_V2_PROTOCOL_NAME:
            raise ValueError("unexpected pilot v2 protocol_name")
        if self.raw.get("split") != "community":
            raise ValueError("pilot v2 split must be community")
        if tuple(self.raw.get("task_ids", ())) != PILOT_TASKS:
            raise ValueError("pilot v2 task order differs from the manifest")
        if self.raw.get("candidate", {}).get("model") != DEFAULT_CANDIDATE_MODEL:
            raise ValueError("pilot v2 candidate model differs")
        if self.raw.get("evolution", {}).get("model") != DEFAULT_CANDIDATE_MODEL:
            raise ValueError("pilot v2 evolution model differs")
        judge = self.raw.get("judge", {})
        if (
            judge.get("model") != DEFAULT_JUDGE_MODEL
            or judge.get("provider") != "openai_compatible"
            or judge.get("provider_only") != "azure"
            or judge.get("allow_fallbacks") is not False
        ):
            raise ValueError("pilot v2 judge identity differs")
        execution = self.raw.get("execution", {})
        if (
            int(execution.get("max_tasks", 0)) != len(PILOT_TASKS)
            or int(execution.get("max_retries", -1)) != 0
            or execution.get("stop_on_infrastructure_failure") is not True
            or execution.get("leaderboard_eligible", True) is not False
            or execution.get("official_protocol", True) is not False
        ):
            raise ValueError("pilot v2 execution constraints differ")
        if int(execution.get("max_judge_http_requests", 0)) != MAX_JUDGE_HTTP_REQUESTS:
            raise ValueError("pilot v2 judge HTTP budget differs")

    @classmethod
    def load(cls, path: str | Path) -> "PilotV2Config":
        protocol_path = Path(path).resolve(strict=True)
        payload = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("pilot v2 protocol must be a YAML object")
        return cls(protocol_path, payload)

    def require(self, key: str) -> Any:
        value: Any = self.raw
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise KeyError(key)
            value = value[part]
        return value

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


class PilotCandidatePort(Protocol):
    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class PilotEvolutionPort(Protocol):
    def evolve(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class PilotJudgePort(Protocol):
    def evaluate(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class PilotV2JudgePort:
    """Judge adapter using the durable evaluator subprocess and canonical scorer."""

    def __init__(
        self,
        *,
        researchclawbench_root: Path,
        evaluator_private_root: Path,
        timeout_seconds: int = 1800,
    ) -> None:
        self.researchclawbench_root = researchclawbench_root
        self.evaluator_private_root = evaluator_private_root
        self.timeout_seconds = timeout_seconds
        self.real_calls = 0

    def evaluate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.evaluator_private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        rcb_base = os.environ.get("RCB_JUDGE_BASE_URL") or os.environ.get(
            "JUDGE_API_BASE"
        )
        if not rcb_base:
            raise PilotV2Error("BLOCKED_JUDGE_AUTH", "Judge API base is missing")
        model = os.environ.get("RCB_JUDGE_MODEL") or os.environ.get("JUDGE_MODEL_NAME")
        if model != DEFAULT_JUDGE_MODEL:
            raise PilotV2Error("BLOCKED_JUDGE_AUTH", "Judge model differs from protocol")
        self.real_calls += 1
        execution = run_community_evaluator_detailed(
            project_root=self.researchclawbench_root.parent,
            workspace=request["candidate_output_root"],
            evaluator_private_root=self.evaluator_private_root,
            task_id=request["task_id"],
            attempt_id=request["run_id"],
            expected_model=model,
            expected_api_base=rcb_base,
            expected_provider="openai_compatible",
            expected_requested_provider=REQUESTED_JUDGE_PROVIDER,
            timeout_seconds=self.timeout_seconds,
        )
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "payload": execution.raw_score,
            "score_valid": execution.score_valid,
            "judge_completed": execution.judge_completed,
            "failure_category": execution.failure_category,
            "http_requests": int(
                execution.raw_score.get("http_requests") or 0
            ),
        }


def _gt_sha256(task_dir: Path) -> str:
    checklist = task_dir / "target_study" / "checklist.json"
    if not checklist.is_file() or checklist.is_symlink():
        raise PilotV2Error("BLOCKED_GT_UNAVAILABLE", "checklist is unavailable")
    return sha256_file(checklist)


def _rubric_count(task_dir: Path) -> int:
    checklist = task_dir / "target_study" / "checklist.json"
    items = json.loads(checklist.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        raise PilotV2Error("BLOCKED_GT_UNAVAILABLE", "checklist is empty")
    return len(items)


def _seal_workspace(workspace: Path) -> str:
    result = validate_workspace(workspace)
    if not result.passed:
        raise PilotV2Error(
            "WORKSPACE_VALIDATION_FAILED",
            "workspace validation failed: " + ",".join(result.errors),
        )
    freeze_candidate_outputs(workspace)
    return str(result.artifact_root_sha256)


def _verify_sealed_hash(root: Path, expected_hash: str, label: str) -> str:
    actual = candidate_artifact_root_sha256(root)
    if actual != expected_hash:
        raise PilotV2Error(
            f"{label}_SEALED_HASH_MISMATCH",
            f"{label} sealed hash differs",
        )
    return actual


class PilotV2DryRunCandidate:
    """Deterministic zero-call Candidate used only for tests and dry-run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.real_calls = 0
        self.provider_calls = 0

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        self.real_calls += 1
        workspace = Path(str(request["workspace"]))
        (workspace / "code").mkdir(exist_ok=True)
        (workspace / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "outputs").mkdir(exist_ok=True)
        (workspace / "outputs" / "summary.json").write_text(
            json.dumps({"dry_run": True, "estimated_effect": 0.42}),
            encoding="utf-8",
        )
        report = workspace / "report"
        (report / "images").mkdir(parents=True, exist_ok=True)
        (report / "report.md").write_text(
            _dry_report(task_id=request["task_id"]),
            encoding="utf-8",
        )
        (report / "images" / "f.png").write_bytes(_PILOT_PNG)
        meta = {
            "status": "completed",
            "exit_code": 0,
            "task_id": request["task_id"],
            "run_id": request["run_id"],
        }
        if request.get("injected_artifact_sha256"):
            meta["injected_artifact_paths"] = request.get("injected_artifact_paths")
            meta["injected_artifact_sha256"] = request.get("injected_artifact_sha256")
            meta["artifact_read_requested"] = True
        (workspace / "_meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "session_id": f"dry-{request['run_id']}",
            "candidate_output_root": str(workspace),
            "exit_status": 0,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "deepseek_key_present": False,
            "secret_recorded": False,
        }


class PilotV2DryRunEvolution:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.real_calls = 0
        self.provider_calls = 0

    def evolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        self.real_calls += 1
        artifact_root = Path(str(request["artifact_root"]))
        (artifact_root / "skill").mkdir(parents=True, exist_ok=True)
        (artifact_root / "agent_system").mkdir(parents=True, exist_ok=True)
        contents = {
            "memory": f"# Memory\nItem-scoped {request['task_id']}.",
            "skill": f"# Skill\nItem-scoped {request['task_id']}.",
            "agent_system": f"# Agent system\nItem-scoped {request['task_id']}.",
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
        (artifact_root / "injection_manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "openevo.researchclawbench.pilot_v2_artifact.v1",
                    "task_id": request["task_id"],
                    "baseline_run_id": request["baseline_run_id"],
                    "generation": 1,
                    "artifacts": artifacts,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "task_id": request["task_id"],
            "baseline_run_id": request["baseline_run_id"],
            "generation": 1,
            "gt_sha256": request["gt_sha256"],
            "artifact_root": str(artifact_root),
            "artifacts": {
                key: value["content_sha256"] for key, value in artifacts.items()
            },
            "artifact_details": artifacts,
            "secret_recorded": False,
        }


class PilotV2DryRunJudge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.real_calls = 0
        self.provider_calls = 0

    def evaluate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        self.real_calls += 1
        count = int(request["rubric_count"])
        score = 62.0 if request["pass_name"] == "baseline" else 66.0
        items = []
        for index in range(count):
            items.append(
                {
                    "index": index,
                    "type": "text",
                    "content": "criterion",
                    "weight": 1.0 / count,
                    "score": score,
                    "reasoning": "deterministic dry-run reasoning",
                    "score_valid": True,
                    "judge_completed": True,
                    "failure_category": None,
                    "rubric_id": f"rubric_{index}",
                    "requested_model": "openai/gpt-5.1",
                    "requested_provider": "azure",
                    "returned_model": "openai/gpt-5.1",
                    "returned_provider": "Azure",
                    "http_status": 200,
                    "request_id": "dry",
                    "response_hash": "0" * 64,
                    "parse_status": "ok",
                    "latency_ms": 0,
                    "retry_count": 1,
                    "http_requests": 1,
                    "response_body_present": True,
                    "content_present": True,
                    "json_parse_success": True,
                    "schema_valid": True,
                    "usage": None,
                }
            )
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "payload": {
                "run_id": request["run_id"],
                "task_id": request["task_id"],
                "agent_name": "Unknown",
                "items": items,
                "total_score": score,
                "total_weight": 1.0,
                "score_valid": True,
                "judge_completed": True,
                "failure_category": None,
                "requested_model": "openai/gpt-5.1",
                "requested_provider": "azure",
                "returned_provider": "Azure",
                "http_requests": count,
                "usage": None,
            },
            "score_valid": True,
            "judge_completed": True,
            "failure_category": None,
            "http_requests": count,
        }


def _dry_report(*, task_id: str) -> str:
    return "\n".join(
        (
            f"# Dry-run report for {task_id}",
            "",
            "## Methodology",
            "This deterministic dry run exercises the pilot v2 wiring without "
            "calling any model. The baseline workspace is staged from the "
            "official public task snapshot; the evolved workspace adds memory, "
            "skill, and agent-system artifacts that must be explicitly read "
            "before the analysis starts. The experimental design applies the "
            "published methodology to the provided input tables and computes "
            "the required summary statistics. The pipeline is implemented in "
            "Python and follows the procedure described in the related work, "
            "including the standard aggregation steps and the strand-count "
            "sweeps. Every input file is read from the read-only data "
            "directory, and every intermediate result is written into the "
            "outputs directory so that the full pipeline is reproducible from "
            "the sealed workspace alone.",
            "",
            "## Results",
            "The primary quantitative result is an estimated effect of 0.42 "
            "with 128 synthetic observations across the designed comparison "
            "groups. The summary table reports the group means, the standard "
            "deviations, and the paired differences, all of which are "
            "consistent with the synthetic fixture used for this wiring test. "
            "The figure was generated from the same output tables and is "
            "referenced directly from the results section. Additional "
            "sensitivity checks confirm that the deterministic pipeline "
            "produces stable values across repeated runs and that the "
            "reported statistics remain within the expected ranges.",
            "",
            "## Discussion",
            "The findings are consistent with the task expectations and the "
            "interpretation follows the published discussion. The main "
            "limitation is that this content is a deterministic fixture used "
            "only to exercise the runner wiring, so no biological conclusion "
            "should be drawn from it. The important conclusion for "
            "engineering purposes is that the candidate executor contract, "
            "the workspace sealing step, and the judge input boundary all "
            "accept the same sealed artifact format, which validates the full "
            "pilot path before any live model invocation is attempted. The "
            "same protocol applies to the evolved pass, where the injected "
            "artifacts are explicitly consumed before the analysis runs.",
            "",
            "![figure](images/f.png)",
        )
    )


_PILOT_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class PilotV2Runner:
    """Ordered three-task baseline/evolution/evolved pilot runner."""

    def __init__(
        self,
        *,
        config: PilotV2Config,
        batch_id: str,
        state_root: str | Path,
        candidate_port: PilotCandidatePort,
        evolution_port: PilotEvolutionPort,
        judge_port: PilotJudgePort,
        workspace_builder: Callable[..., WorkspaceReceipt] = build_official_workspace,
        dry_run: bool = False,
        adopt_from: str | None = None,
        mark_nondelivery: tuple[str, ...] = (),
    ) -> None:
        self.config = config
        self.batch_id = batch_id
        self.candidate_port = candidate_port
        self.evolution_port = evolution_port
        self.judge_port = judge_port
        self.workspace_builder = workspace_builder
        self.dry_run = dry_run
        self.adopt_from = adopt_from
        self.mark_nondelivery = tuple(mark_nondelivery)
        self.store = TrainingStateStore(
            state_root,
            transition_fn=require_pilot_v2_transition,
        )
        self._judge_http_limit = (
            10_000 if dry_run else MAX_JUDGE_HTTP_REQUESTS
        )

    def initialize(self) -> dict[str, Any]:
        if self.mark_nondelivery and self.adopt_from is None:
            raise PilotV2Error(
                "NONDELIVERY_REQUIRES_ADOPT",
                "marking a task as Candidate nondelivery requires --adopt-batch",
            )
        for task_id in self.mark_nondelivery:
            if task_id not in PILOT_TASKS:
                raise PilotV2Error(
                    "NONDELIVERY_TASK_OUTSIDE_PILOT",
                    f"{task_id} is outside the fixed pilot task list",
                )
        for task_id in PILOT_TASKS:
            task_dir = self.config.researchclawbench_root / "tasks" / task_id
            if not (task_dir / "task_info.json").is_file():
                raise PilotV2Error("MANIFEST_MISSING", f"{task_id} manifest missing")
            if not (task_dir / "target_study" / "checklist.json").is_file():
                raise PilotV2Error("GT_MISSING", f"{task_id} GT missing")
        needed_judge_http = 2 * sum(
            _rubric_count(self.config.researchclawbench_root / "tasks" / task_id)
            for task_id in PILOT_TASKS
        )
        if needed_judge_http > MAX_JUDGE_HTTP_REQUESTS:
            raise PilotV2Error(
                "JUDGE_HTTP_BUDGET_INSUFFICIENT",
                f"locked manifests need {needed_judge_http} Judge HTTP requests "
                f"but the ceiling is {MAX_JUDGE_HTTP_REQUESTS}",
            )
        adopted_tasks: dict[str, Any] = {}
        adopted_counters: dict[str, int] = {
            "candidate_jobs": 0,
            "evolution_jobs": 0,
            "judge_logical_jobs": 0,
            "judge_http_requests": 0,
        }
        task_index = 0
        if self.adopt_from is not None:
            (
                adopted_tasks,
                adopted_counters,
                task_index,
            ) = self._adopt_prior_batch(self.adopt_from, self.mark_nondelivery)
        self.store.initialize_experiment(
            experiment_id=self.batch_id,
            protocol_sha256=canonical_sha256(self.config.raw),
            core_identity_sha256="engineering-pilot-v2",
            adapter_identity_sha256=canonical_sha256({"adapter": "per-item-pilot-v2"}),
            initial_state={
                "batch_id": self.batch_id,
                "task_index": task_index,
                "tasks": adopted_tasks,
                "candidate_jobs": adopted_counters["candidate_jobs"],
                "evolution_jobs": adopted_counters["evolution_jobs"],
                "judge_logical_jobs": adopted_counters["judge_logical_jobs"],
                "judge_http_requests": adopted_counters["judge_http_requests"],
            },
            initial_stage=PilotV2Stage.PILOT_INIT,
        )
        if self.adopt_from is not None:
            self._reserve(
                category="candidate_model_calls",
                units=adopted_counters["candidate_jobs"],
                limit_units=MAX_CANDIDATE_JOBS,
                key="adopt:candidate",
            )
            self._reserve(
                category="reflector_model_calls",
                units=adopted_counters["evolution_jobs"],
                limit_units=MAX_EVOLUTION_JOBS,
                key="adopt:evolution",
            )
            self._reserve(
                category="judge_operations",
                units=adopted_counters["judge_http_requests"],
                limit_units=self._judge_http_limit,
                key="adopt:judge",
            )
        return self.status()

    def _adopt_prior_batch(
        self,
        prior_batch_id: str,
        mark_nondelivery: tuple[str, ...],
    ) -> tuple[dict[str, Any], dict[str, int], int]:
        """Adopt fully sealed prior-batch task results without re-running them."""

        prior_state_root = self.config.output_root / "supervisor" / prior_batch_id
        if not (prior_state_root / "training-supervisor.sqlite3").is_file():
            raise PilotV2Error("ADOPT_SOURCE_MISSING", "prior batch state is missing")
        prior_store = TrainingStateStore(
            prior_state_root,
            transition_fn=require_pilot_v2_transition,
        )
        prior = prior_store.load(prior_batch_id)
        if prior.get("stage") not in {
            PilotV2Stage.PILOT_BLOCKED.value,
            PilotV2Stage.PILOT_BUDGET_EXHAUSTED.value,
            PilotV2Stage.PILOT_CLOSED.value,
        }:
            raise PilotV2Error(
                "ADOPT_SOURCE_NOT_TERMINAL",
                "prior batch is not terminal",
            )
        if prior.get("_adapter_identity_sha256") != canonical_sha256(
            {"adapter": "per-item-pilot-v2"}
        ):
            raise PilotV2Error(
                "ADOPT_ADAPTER_IDENTITY_MISMATCH",
                "prior batch adapter identity differs",
            )
        prior_tasks: dict[str, Any] = prior.get("tasks", {})
        adopted: dict[str, Any] = {}
        first_pending = 0
        nondelivery_candidate_jobs = 0
        prior_effects = prior_store.side_effects_for_experiment(prior_batch_id)
        for index, task_id in enumerate(PILOT_TASKS):
            if task_id in mark_nondelivery:
                task_state = prior_tasks.get(task_id)
                if (
                    isinstance(task_state, dict)
                    and task_state.get("status") == "CANDIDATE_NONDELIVERY"
                ):
                    adopted[task_id] = task_state
                    namespace = (
                        self.config.output_root
                        / "items"
                        / task_id
                        / "sealed"
                        / self.batch_id
                    )
                    namespace.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(namespace / "task_result.json", task_state)
                    first_pending = index + 1
                    continue
                if not isinstance(task_state, dict) or "evolved_judge" in task_state:
                    raise PilotV2Error(
                        "NONDELIVERY_MARK_INVALID",
                        f"{task_id} cannot be marked as Candidate nondelivery",
                    )
                if prior.get("stage") != PilotV2Stage.PILOT_BLOCKED.value or prior.get(
                    "failure_code"
                ) != "WORKSPACE_VALIDATION_FAILED":
                    raise PilotV2Error(
                        "NONDELIVERY_SOURCE_NOT_SEAL_FAILURE",
                        f"{task_id} prior block is not a workspace validation failure",
                    )
                candidate_effect = next(
                    (
                        effect
                        for effect in prior_effects
                        if effect["kind"] == "candidate-baseline"
                        and (effect.get("receipt") or {}).get("task_id") == task_id
                        and effect["status"] == "completed"
                    ),
                    None,
                )
                if candidate_effect is None:
                    raise PilotV2Error(
                        "NONDELIVERY_CANDIDATE_EFFECT_MISSING",
                        f"{task_id} prior Candidate side effect is missing",
                    )
                record = {
                    "status": "CANDIDATE_NONDELIVERY",
                    "task_id": task_id,
                    "score_valid": False,
                    "paired_result_valid": False,
                    "candidate_failure_count": 1,
                    "failure_code": str(prior.get("failure_code")),
                    "failure_message": str(prior.get("failure_message")),
                    "source_batch_id": prior_batch_id,
                }
                adopted[task_id] = record
                namespace = (
                    self.config.output_root
                    / "items"
                    / task_id
                    / "sealed"
                    / self.batch_id
                )
                namespace.mkdir(parents=True, exist_ok=True)
                atomic_write_json(namespace / "task_result.json", record)
                nondelivery_candidate_jobs += 1
                first_pending = index + 1
                continue
            task_state = prior_tasks.get(task_id)
            if not isinstance(task_state, dict):
                first_pending = index
                break
            baseline_judge = task_state.get("baseline_judge")
            evolved_judge = task_state.get("evolved_judge")
            evolution_receipt = task_state.get("evolution_receipt")
            if not (
                isinstance(baseline_judge, dict)
                and baseline_judge.get("score_valid") is True
                and isinstance(evolved_judge, dict)
                and evolved_judge.get("score_valid") is True
                and isinstance(evolution_receipt, dict)
                and isinstance(task_state.get("artifact_hashes"), dict)
            ):
                first_pending = index
                break
            baseline_root = Path(
                str(task_state["baseline_receipt"]["candidate_output_root"])
            ).resolve(strict=True)
            evolved_root = Path(
                str(task_state["evolved_receipt"]["candidate_output_root"])
            ).resolve(strict=True)
            if candidate_artifact_root_sha256(baseline_root) != task_state[
                "baseline_sealed_hash"
            ] or candidate_artifact_root_sha256(evolved_root) != task_state[
                "evolved_sealed_hash"
            ]:
                raise PilotV2Error(
                    "ADOPT_SEALED_HASH_MISMATCH",
                    f"{task_id} prior sealed evidence drifted",
                )
            for payload_name in (
                "baseline_score.json",
                "evolved_score.json",
                "task_result.json",
            ):
                source = (
                    self.config.output_root
                    / "items"
                    / task_id
                    / "sealed"
                    / prior_batch_id
                    / payload_name
                )
                if not source.is_file():
                    raise PilotV2Error(
                        "ADOPT_SEALED_FILE_MISSING",
                        f"{task_id} prior sealed file {payload_name} is missing",
                    )
            for payload_name in (
                "baseline_score.json",
                "evolved_score.json",
                "task_result.json",
            ):
                source = (
                    self.config.output_root
                    / "items"
                    / task_id
                    / "sealed"
                    / prior_batch_id
                    / payload_name
                )
                target = (
                    self.config.output_root
                    / "items"
                    / task_id
                    / "sealed"
                    / self.batch_id
                    / payload_name
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(
                    target,
                    json.loads(source.read_text(encoding="utf-8")),
                )
            adopted[task_id] = task_state
            first_pending = index + 1
        counters = {
            "candidate_jobs": int(prior.get("candidate_jobs", 0)),
            "evolution_jobs": int(prior.get("evolution_jobs", 0)),
            "judge_logical_jobs": int(prior.get("judge_logical_jobs", 0)),
            "judge_http_requests": int(prior.get("judge_http_requests", 0)),
        }
        counters["candidate_jobs"] += nondelivery_candidate_jobs
        if first_pending > 0 and not adopted:
            raise PilotV2Error(
                "ADOPT_PARTIAL_INVALID",
                "prior batch has no adoptable completed task",
            )
        return adopted, counters, first_pending

    def status(self) -> dict[str, Any]:
        return self.store.load(self.batch_id)

    def _transition(
        self,
        source: PilotV2Stage,
        target: PilotV2Stage,
        suffix: str,
        *,
        updates: dict[str, Any] | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.status()
        key = f"{self.batch_id}:{state['task_index']}:{suffix}"
        return self.store.transition(
            experiment_id=self.batch_id,
            idempotency_key=key,
            source=source,
            target=target,
            updates=updates or {},
            receipt=receipt or {},
        )

    def _reserve(
        self,
        *,
        category: str,
        units: int,
        limit_units: int,
        key: str,
    ) -> None:
        try:
            self.store.reserve_budget(
                experiment_id=self.batch_id,
                idempotency_key=f"{self.batch_id}:{key}",
                category=category,
                units=units,
                limit_units=limit_units,
            )
        except ValueError as exc:
            raise PilotBudgetExhausted(
                "JUDGE_HTTP_BUDGET_EXHAUSTED"
                if category == "judge_operations"
                else "MODEL_BUDGET_EXHAUSTED",
                str(exc),
            ) from exc

    def _effect(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        execute: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        state = self.status()
        key = f"{self.batch_id}:{state['task_index']}:{kind}:{request.get('run_id', '')}"
        observed = self.store.plan_side_effect(
            experiment_id=self.batch_id,
            idempotency_key=key,
            kind=kind,
            request=request,
        )
        if observed["status"] == "completed":
            return observed["receipt"]
        receipt = execute(request)
        return self.store.complete_side_effect(idempotency_key=key, receipt=receipt)

    def _task_dir(self, task_id: str) -> Path:
        return self.config.researchclawbench_root / "tasks" / task_id

    def _task_namespace(self, task_id: str) -> Path:
        return self.config.output_root / "items" / task_id / "runs" / self.batch_id

    def _stage_workspace(
        self,
        task_id: str,
        run_id: str,
        pass_name: str,
    ) -> WorkspaceReceipt:
        run_root = self._task_namespace(task_id)
        receipt = self.workspace_builder(
            self.config.researchclawbench_root,
            task_id,
            run_root,
            run_id,
            allowed_task_ids=PILOT_TASKS,
        )
        destination = run_root / f"{pass_name}_candidate"
        if destination.exists():
            if not destination.is_dir():
                raise PilotV2Error("WORKSPACE_COLLISION", "candidate destination unsafe")
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

    def _validate_evolved_artifacts(self, artifact_root: Path, task_id: str) -> dict[str, str]:
        paths = {
            "memory": artifact_root / "memory.md",
            "skill": artifact_root / "skill" / "SKILL.md",
            "agent_system": artifact_root / "agent_system" / "AGENTS.md",
        }
        hashes: dict[str, str] = {}
        for name, path in paths.items():
            if not path.is_file() or path.is_symlink():
                raise PilotV2Error(
                    "ARTIFACT_MISSING",
                    f"{task_id} artifact {name} is missing",
                )
            hashes[name] = sha256_file(path)
        return hashes

    def _inject_artifacts(
        self,
        *,
        artifact_root: Path,
        context_root: Path,
        candidate_workspace: Path,
    ) -> dict[str, str]:
        context_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact_root / "memory.md", context_root / "memory.md")
        shutil.copytree(
            artifact_root / "skill",
            context_root / "skill",
            dirs_exist_ok=True,
        )
        shutil.copytree(
            artifact_root / "agent_system",
            context_root / "agent_system",
            dirs_exist_ok=True,
        )
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
        return {
            "AGENTS.md": sha256_file(candidate_workspace / "AGENTS.md"),
            "memory.md": sha256_file(candidate_workspace / "memory.md"),
            "skills/skill/SKILL.md": sha256_file(
                candidate_workspace / "skills" / "skill" / "SKILL.md"
            ),
        }

    def _judge_pass(
        self,
        *,
        task_id: str,
        run_id: str,
        pass_name: str,
        candidate_root: Path,
        rubric_count: int,
    ) -> dict[str, Any]:
        self._reserve(
            category="judge_operations",
            units=rubric_count,
            limit_units=self._judge_http_limit,
            key=f"{task_id}:{pass_name}:judge",
        )
        request = {
            "task_id": task_id,
            "run_id": run_id,
            "pass_name": pass_name,
            "candidate_output_root": str(candidate_root),
            "rubric_count": rubric_count,
        }
        result = self._effect(
            kind=f"judge-{pass_name}",
            request=request,
            execute=self.judge_port.evaluate,
        )
        if result.get("score_valid") is not True:
            category = result.get("failure_category") or "JUDGE_RESPONSE_INVALID"
            raise PilotV2Error(category, f"{pass_name} Judge failed closed")
        payload = result["payload"]
        http_requests = int(payload.get("http_requests") or 0)
        if http_requests != rubric_count:
            raise PilotV2Error(
                "JUDGE_HTTP_COUNT_MISMATCH",
                f"{pass_name} Judge HTTP count differs from rubric count",
            )
        return result

    def _blocked(
        self,
        source: PilotV2Stage,
        exc: Exception,
    ) -> dict[str, Any]:
        code = getattr(exc, "code", "PILOT_INFRASTRUCTURE_FAILURE")
        if isinstance(exc, PilotBudgetExhausted):
            target = PilotV2Stage.PILOT_BUDGET_EXHAUSTED
        else:
            target = PilotV2Stage.PILOT_BLOCKED
        return self._transition(
            source,
            target,
            "blocked",
            updates={"failure_code": code, "failure_message": str(exc)},
            receipt={"failure_classification": code},
        )

    def run_next(self) -> dict[str, Any]:
        state = self.status()
        stage = PilotV2Stage(state["stage"])
        task_index = int(state["task_index"])
        if stage is PilotV2Stage.PILOT_INIT and task_index >= len(PILOT_TASKS):
            result = self._pilot_summary(state)
            atomic_write_json(
                self.config.output_root / "pilot_batch_result.json",
                result,
            )
            return self._transition(
                stage,
                PilotV2Stage.PILOT_CLOSED,
                "pilot-closed",
                updates={"pilot_result": result},
                receipt=result,
            )
        task_id = PILOT_TASKS[task_index]
        task_dir = self._task_dir(task_id)
        task_state = state.setdefault("tasks", {}).setdefault(task_id, {})
        task_key = f"{task_id}_{task_index}"

        if stage is PilotV2Stage.PILOT_INIT:
            _gt_sha256(task_dir)
            baseline = self._stage_workspace(
                task_id, f"{task_id}_a0_baseline", "baseline"
            )
            return self._transition(
                stage,
                PilotV2Stage.TASK_PREPARED,
                f"{task_key}-prepared",
                updates={
                    "task_index": task_index,
                    "tasks": {
                        **state["tasks"],
                        task_id: {
                            "baseline_workspace": str(baseline.workspace),
                            "baseline_candidate_workspace": str(
                                baseline.candidate_workspace
                            ),
                            "baseline_run_id": baseline.run_id,
                            "rubric_count": _rubric_count(task_dir),
                            "gt_sha256": _gt_sha256(task_dir),
                        },
                    },
                },
                receipt={"task_id": task_id, "prepared": True},
            )

        if stage is PilotV2Stage.TASK_PREPARED:
            try:
                if not self.dry_run:
                    judge_used = self.store.budget_usage(self.batch_id)[
                        "judge_operations"
                    ]
                    needed = int(task_state["rubric_count"]) * 2
                    if judge_used + needed > self._judge_http_limit:
                        raise PilotBudgetExhausted(
                            "JUDGE_HTTP_BUDGET_EXHAUSTED",
                            f"task {task_id} needs {needed} Judge HTTP requests, "
                            f"remaining budget {self._judge_http_limit - judge_used}",
                        )
                self._reserve(
                    category="candidate_model_calls",
                    units=1,
                    limit_units=MAX_CANDIDATE_JOBS,
                    key=f"{task_key}:baseline-candidate",
                )
                request = {
                    "task_id": task_id,
                    "run_id": task_state["baseline_run_id"],
                    "pass_name": "baseline",
                    "workspace": task_state["baseline_candidate_workspace"],
                    "evidence_root": str(
                        self._task_namespace(task_id) / "evidence" / "baseline"
                    ),
                }
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                PilotV2Stage.TASK_BASELINE_RUNNING,
                f"{task_key}-baseline-start",
                updates={"active_baseline_request": request},
                receipt={"candidate_intent_persisted": True},
            )

        if stage is PilotV2Stage.TASK_BASELINE_RUNNING:
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
                PilotV2Stage.TASK_BASELINE_SEALED,
                f"{task_key}-baseline-sealed",
                updates={
                    "tasks": {
                        **state["tasks"],
                        task_id: {
                            **task_state,
                            "baseline_receipt": candidate,
                            "baseline_sealed_hash": sealed_hash,
                        },
                    },
                    "candidate_jobs": int(state.get("candidate_jobs", 0)) + 1,
                },
                receipt=candidate,
            )

        if stage is PilotV2Stage.TASK_BASELINE_SEALED:
            try:
                judge = self._judge_pass(
                    task_id=task_id,
                    run_id=task_state["baseline_run_id"],
                    pass_name="baseline",
                    candidate_root=Path(
                        task_state["baseline_receipt"]["candidate_output_root"]
                    ),
                    rubric_count=int(task_state["rubric_count"]),
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                PilotV2Stage.TASK_BASELINE_SCORED,
                f"{task_key}-baseline-scored",
                updates={
                    "tasks": {
                        **state["tasks"],
                        task_id: {**task_state, "baseline_judge": judge},
                    },
                    "judge_logical_jobs": int(state.get("judge_logical_jobs", 0)) + 1,
                    "judge_http_requests": int(state.get("judge_http_requests", 0))
                    + int(judge.get("http_requests") or 0),
                },
                receipt=judge,
            )

        if stage is PilotV2Stage.TASK_BASELINE_SCORED:
            request = {
                "task_id": task_id,
                "baseline_run_id": task_state["baseline_run_id"],
                "baseline_sealed_root": task_state["baseline_receipt"][
                    "candidate_output_root"
                ],
                "baseline_sealed_hash": task_state["baseline_sealed_hash"],
                "gt_path": str(task_dir / "target_study" / "checklist.json"),
                "gt_sha256": task_state["gt_sha256"],
                "artifact_root": str(
                    self.config.output_root
                    / "items"
                    / task_id
                    / "evolution_artifacts"
                    / self.batch_id
                ),
            }
            return self._transition(
                stage,
                PilotV2Stage.TASK_EVOLUTION_RUNNING,
                f"{task_key}-evolution-start",
                updates={"active_evolution_request": request},
                receipt={"evolution_intent_persisted": True},
            )

        if stage is PilotV2Stage.TASK_EVOLUTION_RUNNING:
            self._reserve(
                category="reflector_model_calls",
                units=3,
                limit_units=MAX_EVOLUTION_JOBS,
                key=f"{task_key}:evolution",
            )
            try:
                evolution = self._effect(
                    kind="evolution",
                    request=state["active_evolution_request"],
                    execute=self.evolution_port.evolve,
                )
                artifact_root = Path(evolution["artifact_root"])
                artifact_hashes = self._validate_evolved_artifacts(
                    artifact_root, task_id
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                PilotV2Stage.TASK_EVOLUTION_SEALED,
                f"{task_key}-evolution-sealed",
                updates={
                    "tasks": {
                        **state["tasks"],
                        task_id: {
                            **task_state,
                            "evolution_receipt": evolution,
                            "artifact_hashes": artifact_hashes,
                        },
                    },
                    "evolution_jobs": int(state.get("evolution_jobs", 0)) + 3,
                },
                receipt=evolution,
            )

        if stage is PilotV2Stage.TASK_EVOLUTION_SEALED:
            try:
                evolved = self._stage_workspace(
                    task_id, f"{task_id}_a0_evolved", "evolved"
                )
                context_root = (
                    self.config.output_root
                    / "items"
                    / task_id
                    / "evolved_context"
                    / self.batch_id
                )
                artifact_root = Path(task_state["evolution_receipt"]["artifact_root"])
                injected = self._inject_artifacts(
                    artifact_root=artifact_root,
                    context_root=context_root,
                    candidate_workspace=Path(evolved.candidate_workspace),
                )
                injected_artifact_sha256 = canonical_json_sha256(
                    {key: value for key, value in sorted(injected.items())}
                )
                instructions_path = (
                    Path(evolved.candidate_workspace) / "INSTRUCTIONS.md"
                )
                instructions = instructions_path.read_text(encoding="utf-8")
                instructions_path.write_text(
                    append_artifact_consumption_instructions(instructions),
                    encoding="utf-8",
                )
                injected_paths = sorted(injected)
                request = {
                    "task_id": task_id,
                    "run_id": evolved.run_id,
                    "pass_name": "evolved",
                    "workspace": str(evolved.candidate_workspace),
                    "evidence_root": str(
                        self._task_namespace(task_id) / "evidence" / "evolved"
                    ),
                    "injected_artifact_paths": injected_paths,
                    "injected_artifact_sha256": injected_artifact_sha256,
                    "artifact_read_requested": True,
                }
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                PilotV2Stage.TASK_EVOLVED_RUNNING,
                f"{task_key}-evolved-start",
                updates={
                    "tasks": {
                        **state["tasks"],
                        task_id: {
                            **task_state,
                            "evolved_candidate_workspace": str(
                                evolved.candidate_workspace
                            ),
                            "evolved_run_id": evolved.run_id,
                            "evolved_context": str(context_root),
                            "injected_artifact_paths": injected_paths,
                            "injected_artifact_sha256": injected_artifact_sha256,
                        },
                    },
                    "active_evolved_request": request,
                },
                receipt={"evolved_intent_persisted": True},
            )

        if stage is PilotV2Stage.TASK_EVOLVED_RUNNING:
            self._reserve(
                category="candidate_model_calls",
                units=1,
                limit_units=MAX_CANDIDATE_JOBS,
                key=f"{task_key}:evolved-candidate",
            )
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
                PilotV2Stage.TASK_EVOLVED_SEALED,
                f"{task_key}-evolved-sealed",
                updates={
                    "tasks": {
                        **state["tasks"],
                        task_id: {
                            **task_state,
                            "evolved_receipt": candidate,
                            "evolved_sealed_hash": sealed_hash,
                        },
                    },
                    "candidate_jobs": int(state.get("candidate_jobs", 0)) + 1,
                },
                receipt=candidate,
            )

        if stage is PilotV2Stage.TASK_EVOLVED_SEALED:
            try:
                judge = self._judge_pass(
                    task_id=task_id,
                    run_id=task_state["evolved_run_id"],
                    pass_name="evolved",
                    candidate_root=Path(
                        task_state["evolved_receipt"]["candidate_output_root"]
                    ),
                    rubric_count=int(task_state["rubric_count"]),
                )
            except Exception as exc:
                return self._blocked(stage, exc)
            return self._transition(
                stage,
                PilotV2Stage.TASK_EVOLVED_SCORED,
                f"{task_key}-evolved-scored",
                updates={
                    "tasks": {
                        **state["tasks"],
                        task_id: {**task_state, "evolved_judge": judge},
                    },
                    "judge_logical_jobs": int(state.get("judge_logical_jobs", 0)) + 1,
                    "judge_http_requests": int(state.get("judge_http_requests", 0))
                    + int(judge.get("http_requests") or 0),
                },
                receipt=judge,
            )

        if stage is PilotV2Stage.TASK_EVOLVED_SCORED:
            try:
                baseline_score = float(
                    task_state["baseline_judge"]["payload"]["total_score"]
                )
                evolved_score = float(
                    task_state["evolved_judge"]["payload"]["total_score"]
                )
                result = {
                    "task_id": task_id,
                    "baseline_score": baseline_score,
                    "evolved_score": evolved_score,
                    "delta": round(evolved_score - baseline_score, 2),
                    "score_valid": True,
                    "paired_score_valid": True,
                    "baseline_http_requests": int(
                        task_state["baseline_judge"].get("http_requests") or 0
                    ),
                    "evolved_http_requests": int(
                        task_state["evolved_judge"].get("http_requests") or 0
                    ),
                    "baseline_sealed_hash": task_state["baseline_sealed_hash"],
                    "evolved_sealed_hash": task_state["evolved_sealed_hash"],
                    "artifact_hashes": task_state["artifact_hashes"],
                    "injected_artifact_sha256": task_state[
                        "injected_artifact_sha256"
                    ],
                }
                namespace = (
                    self.config.output_root
                    / "items"
                    / task_id
                    / "sealed"
                    / self.batch_id
                )
                namespace.mkdir(parents=True, exist_ok=True)
                atomic_write_json(namespace / "task_result.json", result)
                atomic_write_json(
                    namespace / "baseline_score.json",
                    task_state["baseline_judge"]["payload"],
                )
                atomic_write_json(
                    namespace / "evolved_score.json",
                    task_state["evolved_judge"]["payload"],
                )
                next_index = task_index + 1
                updates: dict[str, Any] = {}
                if next_index < len(PILOT_TASKS):
                    next_task = PILOT_TASKS[next_index]
                    baseline = self._stage_workspace(
                        next_task, f"{next_task}_a0_baseline", "baseline"
                    )
                    next_dir = self._task_dir(next_task)
                    updates = {
                        "task_index": next_index,
                        "tasks": {
                            **state["tasks"],
                            next_task: {
                                "baseline_workspace": str(baseline.workspace),
                                "baseline_candidate_workspace": str(
                                    baseline.candidate_workspace
                                ),
                                "baseline_run_id": baseline.run_id,
                                "rubric_count": _rubric_count(next_dir),
                                "gt_sha256": _gt_sha256(next_dir),
                            },
                        },
                    }
            except Exception as exc:
                return self._blocked(stage, exc)
            if updates:
                return self._transition(
                    stage,
                    PilotV2Stage.TASK_CLOSED,
                    f"{task_key}-closed",
                    updates=updates,
                    receipt=result,
                )
            return self._transition(
                stage,
                PilotV2Stage.TASK_CLOSED,
                f"{task_key}-closed",
                updates={"task_index": task_index},
                receipt=result,
            )

        if stage is PilotV2Stage.TASK_CLOSED:
            next_task = PILOT_TASKS[task_index]
            if "evolved_judge" not in state["tasks"].get(next_task, {}):
                return self._transition(
                    stage,
                    PilotV2Stage.TASK_PREPARED,
                    f"{task_key}-next-prepared",
                    receipt={"next_task": next_task},
                )
            result = self._pilot_summary(state)
            atomic_write_json(
                self.config.output_root / "pilot_batch_result.json",
                result,
            )
            return self._transition(
                stage,
                PilotV2Stage.PILOT_CLOSED,
                "pilot-closed",
                updates={"pilot_result": result},
                receipt=result,
            )
        raise RuntimeError(f"unhandled pilot v2 stage: {stage.value}")

    def _pilot_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        rows = []
        completed_tasks: list[str] = []
        for task_id in PILOT_TASKS:
            task = state["tasks"].get(task_id, {})
            if "baseline_judge" in task and "evolved_judge" in task:
                completed_tasks.append(task_id)
                rows.append(
                    {
                        "task_id": task_id,
                        "baseline_score": float(
                            task["baseline_judge"]["payload"]["total_score"]
                        ),
                        "evolved_score": float(
                            task["evolved_judge"]["payload"]["total_score"]
                        ),
                        "delta": round(
                            float(task["evolved_judge"]["payload"]["total_score"])
                            - float(
                                task["baseline_judge"]["payload"]["total_score"]
                            ),
                            2,
                        ),
                        "valid": True,
                    }
                )
        deltas = [row["delta"] for row in rows]
        mean_delta = (
            round(sum(deltas) / len(deltas), 2) if deltas else None
        )
        sorted_deltas = sorted(deltas)
        median_delta = (
            round(
                (
                    sorted_deltas[len(sorted_deltas) // 2]
                    if len(sorted_deltas) % 2 == 1
                    else (
                        sorted_deltas[len(sorted_deltas) // 2 - 1]
                        + sorted_deltas[len(sorted_deltas) // 2]
                    )
                    / 2
                ),
                2,
            )
            if deltas
            else None
        )
        wins = sum(1 for delta in deltas if delta > 0)
        ties = sum(1 for delta in deltas if delta == 0)
        losses = sum(1 for delta in deltas if delta < 0)
        candidate_jobs = int(state.get("candidate_jobs", 0))
        candidate_nondelivery_count = sum(
            1
            for task_id in PILOT_TASKS
            if state["tasks"].get(task_id, {}).get("status")
            == "CANDIDATE_NONDELIVERY"
        )
        summary_status = (
            "PILOT_V2_CLOSED_WITH_CANDIDATE_FAILURE"
            if candidate_nondelivery_count > 0
            else "PILOT_V2_CLOSED"
        )
        return {
            "schema_version": "openevo.researchclawbench.pilot_v2_batch.v1",
            "status": summary_status,
            "batch_id": self.batch_id,
            "task_ids": list(PILOT_TASKS),
            "rows": rows,
            "mean_delta": mean_delta,
            "median_delta": median_delta,
            "wins": wins,
            "ties": ties,
            "losses": losses,
            "task_completion_rate": round(
                len(completed_tasks) / len(PILOT_TASKS), 3
            ),
            "candidate_delivery_success_rate": (
                round(
                    (candidate_jobs - candidate_nondelivery_count)
                    / candidate_jobs,
                    3,
                )
                if candidate_jobs > 0
                else None
            ),
            "candidate_nondelivery_count": candidate_nondelivery_count,
            "infrastructure_failure_count": 0,
            "blocked_count": 0,
            "candidate_jobs": int(state.get("candidate_jobs", 0)),
            "evolution_jobs": int(state.get("evolution_jobs", 0)),
            "judge_logical_jobs": int(state.get("judge_logical_jobs", 0)),
            "judge_http_requests": int(state.get("judge_http_requests", 0)),
            "budget_usage": self.store.budget_usage(self.batch_id),
            "created_at": datetime.now(UTC).isoformat(),
        }

    def run_until_terminal(self, *, max_transitions: int = 200) -> dict[str, Any]:
        for _ in range(max_transitions):
            state = self.status()
            if state["stage"] in {
                PilotV2Stage.PILOT_CLOSED.value,
                PilotV2Stage.PILOT_BLOCKED.value,
                PilotV2Stage.PILOT_BUDGET_EXHAUSTED.value,
            }:
                return state
            self.run_next()
        raise RuntimeError("pilot v2 transition budget exhausted")
