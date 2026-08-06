"""Protocol v3 baseline-only delivery reliability canary.

Runs exactly one baseline Candidate per task (Chemistry_004, Information_005)
with the generic delivery contract and never invokes Evolution or Judge.
Each task stops at BASELINE_SEALED or a durable CANDIDATE_NONDELIVERY
classification.  Any nondelivery stops the canary immediately.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import yaml

from .artifact_validator import (
    freeze_candidate_outputs,
    validate_workspace,
)
from .delivery_contract import analyze_delivery_evidence, append_delivery_contract
from .run_manifest import atomic_write_json
from .training_state_store import TrainingStateStore, canonical_sha256
from .workspace import WorkspaceReceipt, build_official_workspace

CANARY_V3_PROTOCOL_NAME = "per_item_minimal_delivery_canary_v3"
CANARY_TASKS = ("Chemistry_004", "Information_005")
DEFAULT_CANDIDATE_MODEL = "deepseek-v4-flash"
MAX_CANDIDATE_JOBS = 2


class DeliveryCanaryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CanaryStage(StrEnum):
    CANARY_INIT = "CANARY_INIT"
    TASK_PREPARED = "TASK_PREPARED"
    TASK_BASELINE_RUNNING = "TASK_BASELINE_RUNNING"
    TASK_BASELINE_SEALED = "TASK_BASELINE_SEALED"
    TASK_NONDELIVERY = "TASK_NONDELIVERY"
    CANARY_CLOSED = "CANARY_CLOSED"
    CANARY_FAILED = "CANARY_FAILED"


_ALLOWED: dict[CanaryStage, frozenset[CanaryStage]] = {
    CanaryStage.CANARY_INIT: frozenset({CanaryStage.TASK_PREPARED, CanaryStage.CANARY_FAILED}),
    CanaryStage.TASK_PREPARED: frozenset(
        {CanaryStage.TASK_BASELINE_RUNNING, CanaryStage.CANARY_FAILED}
    ),
    CanaryStage.TASK_BASELINE_RUNNING: frozenset(
        {CanaryStage.TASK_BASELINE_SEALED, CanaryStage.TASK_NONDELIVERY}
    ),
    CanaryStage.TASK_BASELINE_SEALED: frozenset(
        {CanaryStage.TASK_PREPARED, CanaryStage.CANARY_CLOSED, CanaryStage.CANARY_FAILED}
    ),
    CanaryStage.TASK_NONDELIVERY: frozenset({CanaryStage.CANARY_FAILED}),
    CanaryStage.CANARY_CLOSED: frozenset(),
    CanaryStage.CANARY_FAILED: frozenset(),
}


def require_canary_v3_transition(source: CanaryStage, target: CanaryStage) -> None:
    if CanaryStage(target) not in _ALLOWED[CanaryStage(source)]:
        raise ValueError(
            f"invalid canary v3 transition: {source.value}->{target.value}"
        )


@dataclass(frozen=True)
class DeliveryCanaryConfig:
    path: Path
    raw: dict[str, Any]

    def __post_init__(self) -> None:
        if self.raw.get("protocol_name") != CANARY_V3_PROTOCOL_NAME:
            raise ValueError("unexpected delivery canary v3 protocol_name")
        if tuple(self.raw.get("task_ids", ())) != CANARY_TASKS:
            raise ValueError("delivery canary v3 task order differs")
        if self.raw.get("candidate", {}).get("model") != DEFAULT_CANDIDATE_MODEL:
            raise ValueError("delivery canary v3 candidate model differs")
        execution = self.raw.get("execution", {})
        if (
            int(execution.get("max_candidate_jobs", 0)) != MAX_CANDIDATE_JOBS
            or int(execution.get("max_evolution_jobs", -1)) != 0
            or int(execution.get("max_judge_jobs", -1)) != 0
            or int(execution.get("max_retries", -1)) != 0
            or execution.get("leaderboard_eligible", True) is not False
            or execution.get("official_protocol", True) is not False
        ):
            raise ValueError("delivery canary v3 execution constraints differ")

    @classmethod
    def load(cls, path: str | Path) -> "DeliveryCanaryConfig":
        protocol_path = Path(path).resolve(strict=True)
        payload = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("delivery canary v3 protocol must be a YAML object")
        return cls(protocol_path, payload)

    def require(self, key: str) -> Any:
        value: Any = self.raw
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise KeyError(key)
            value = value[part]
        return value

    @property
    def researchclawbench_root(self) -> Path:
        return Path(self.require("paths.researchclawbench_root")).resolve(strict=True)

    @property
    def output_root(self) -> Path:
        return Path(self.require("paths.output_root")).resolve(strict=False)

    @property
    def dry_run(self) -> bool:
        return bool(self.require("execution.dry_run"))


class CanaryCandidatePort(Protocol):
    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


def _seal_workspace(workspace: Path) -> str:
    result = validate_workspace(workspace)
    if not result.passed:
        raise DeliveryCanaryError(
            "WORKSPACE_VALIDATION_FAILED",
            "workspace validation failed: " + ",".join(result.errors),
        )
    freeze_candidate_outputs(workspace)
    return str(result.artifact_root_sha256)


_CANARY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _deliverable_report(task_id: str) -> str:
    return "\n".join(
        (
            f"# Delivery canary report for {task_id}",
            "",
            "## Methodology",
            "This deterministic fixture exercises the Protocol v3 delivery "
            "contract. The analysis reads the provided task inputs and related "
            "work, computes the required summary statistics, and writes "
            "reproducible code and outputs into the workspace. Every input "
            "table is loaded from the read-only data directory, every "
            "intermediate result is written into the outputs directory, and "
            "the final figures are generated into the report images "
            "directory so the full pipeline is reproducible from the sealed "
            "workspace alone.",
            "",
            "## Results",
            "The primary quantitative result is an estimated effect of 0.42 "
            "with 128 observations, shown in figure f. The summary table "
            "reports the group means, the standard deviations, and the "
            "paired differences, all consistent with the deterministic "
            "fixture. The figure was generated from the same output tables "
            "and is referenced directly from this results section, which "
            "makes the numerical claims traceable to the outputs directory. "
            "The reproducibility section lists the exact commands needed to "
            "regenerate every table and figure from the sealed workspace, "
            "including the environment variables and the python entry "
            "points. A second validation run confirms that the numbers do "
            "not drift between executions and that the reported effect size "
            "remains stable under the same random seed.",
            "",
            "## Discussion",
            "The delivery contract was followed: the scaffold was created "
            "early, intermediate results were saved in outputs/, and the "
            "final report was completed before the self-check. The important "
            "conclusion for engineering purposes is that the workspace "
            "sealing step and the delivery evidence analyzer accept the same "
            "artifact format, which validates the Protocol v3 candidate "
            "delivery path before any live model invocation is attempted. "
            "The remaining limitation is that this fixture is deterministic, "
            "so it exercises the contract mechanics rather than the "
            "scientific content of a real candidate run.",
            "",
            "![figure](images/f.png)",
        )
    ) + "\n"


class DeliveryCanaryDryRunCandidate:
    """Zero-call Candidate that produces full evidence + deliverables."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        workspace = Path(str(request["workspace"]))
        (workspace / "code").mkdir(parents=True, exist_ok=True)
        (workspace / "code" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "outputs").mkdir(parents=True, exist_ok=True)
        (workspace / "outputs" / "summary.json").write_text(
            json.dumps(
                {"estimated_effect": 0.42, "observations": 128, "ok": True}
            ),
            encoding="utf-8",
        )
        report = workspace / "report"
        (report / "images").mkdir(parents=True, exist_ok=True)
        (report / "images" / "f.png").write_bytes(_CANARY_PNG)
        (report / "report.md").write_text(
            _deliverable_report(request["task_id"]),
            encoding="utf-8",
        )
        (workspace / "_meta.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "task_id": request["task_id"],
                    "run_id": request["run_id"],
                }
            ),
            encoding="utf-8",
        )
        evidence_root = Path(str(request["evidence_root"]))
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        events = "\n".join(
            (
                json.dumps({"type": "thread.started"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "mkdir -p code outputs report/images && "
                                "cat > report/report.md <<'EOF'\n"
                                "# draft\nEOF"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "file_change",
                            "changes": [
                                {
                                    "path": str(
                                        workspace / "report" / "report.md"
                                    ),
                                    "kind": "add",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "find code outputs report images -maxdepth 3 "
                                "-type f -print"
                            ),
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        ) + "\n"
        (evidence_root / "codex_events.jsonl").write_text(events, encoding="utf-8")
        (evidence_root / "stdout.log").write_text(events, encoding="utf-8")
        (evidence_root / "stderr.log").write_text("", encoding="utf-8")
        (evidence_root / "final_assistant_message.json").write_text(
            json.dumps(
                {
                    "message": (
                        "The task is complete. Report: report/report.md, "
                        "code: code/run.py, outputs: outputs/summary.json, "
                        "image: report/images/f.png, self-check passed."
                    )
                }
            ),
            encoding="utf-8",
        )
        (evidence_root / "command.json").write_text(
            json.dumps({"argv": ["codex", "exec"], "cwd": str(workspace)}),
            encoding="utf-8",
        )
        (evidence_root / "output_manifest.json").write_text(
            json.dumps({"report/report.md": True}),
            encoding="utf-8",
        )
        delivery_evidence = analyze_delivery_evidence(
            evidence_root=evidence_root,
            workspace=workspace,
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
            "delivery_evidence": delivery_evidence,
        }


class DeliveryCanaryV3Runner:
    def __init__(
        self,
        *,
        config: DeliveryCanaryConfig,
        batch_id: str,
        state_root: str | Path,
        candidate_port: CanaryCandidatePort,
        workspace_builder: Callable[..., WorkspaceReceipt] = build_official_workspace,
    ) -> None:
        self.config = config
        self.batch_id = batch_id
        self.candidate_port = candidate_port
        self.workspace_builder = workspace_builder
        self.store = TrainingStateStore(
            state_root,
            transition_fn=require_canary_v3_transition,
        )

    def initialize(self) -> dict[str, Any]:
        for task_id in CANARY_TASKS:
            task_dir = self.config.researchclawbench_root / "tasks" / task_id
            if not (task_dir / "task_info.json").is_file():
                raise DeliveryCanaryError(
                    "MANIFEST_MISSING", f"{task_id} manifest missing"
                )
        self.store.initialize_experiment(
            experiment_id=self.batch_id,
            protocol_sha256=canonical_sha256(self.config.raw),
            core_identity_sha256="engineering-delivery-canary-v3",
            adapter_identity_sha256=canonical_sha256(
                {"adapter": "delivery-canary-v3"}
            ),
            initial_state={
                "batch_id": self.batch_id,
                "task_index": 0,
                "tasks": {},
                "candidate_jobs": 0,
                "evolution_jobs": 0,
                "judge_jobs": 0,
            },
            initial_stage=CanaryStage.CANARY_INIT,
        )
        return self.status()

    def status(self) -> dict[str, Any]:
        return self.store.load(self.batch_id)

    def _transition(
        self,
        source: CanaryStage,
        target: CanaryStage,
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

    def _reserve(self, units: int) -> None:
        try:
            self.store.reserve_budget(
                experiment_id=self.batch_id,
                idempotency_key=f"{self.batch_id}:candidate",
                category="candidate_model_calls",
                units=units,
                limit_units=MAX_CANDIDATE_JOBS,
            )
        except ValueError as exc:
            raise DeliveryCanaryError("CANDIDATE_BUDGET_EXHAUSTED", str(exc)) from exc

    def _effect(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        execute: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        state = self.status()
        key = f"{self.batch_id}:{state['task_index']}:{kind}:{request['run_id']}"
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

    def _stage_workspace(self, task_id: str, run_id: str) -> WorkspaceReceipt:
        run_root = self._task_namespace(task_id)
        receipt = self.workspace_builder(
            self.config.researchclawbench_root,
            task_id,
            run_root,
            run_id,
            allowed_task_ids=CANARY_TASKS,
        )
        destination = run_root / "baseline_candidate"
        if destination.exists():
            if not destination.is_dir():
                raise DeliveryCanaryError(
                    "WORKSPACE_COLLISION", "candidate destination unsafe"
                )
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
        instructions_path = destination / "INSTRUCTIONS.md"
        instructions_path.write_text(
            append_delivery_contract(
                instructions_path.read_text(encoding="utf-8")
            ),
            encoding="utf-8",
        )
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

    def _summary(self, state: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for task_id in CANARY_TASKS:
            task = state["tasks"].get(task_id, {})
            rows.append(
                {
                    "task_id": task_id,
                    "candidate_exit": task.get("candidate_exit"),
                    "first_workspace_write_event_index": (
                        task.get("delivery_evidence") or {}
                    ).get("first_workspace_write_event_index"),
                    "workspace_write_event_count": (
                        task.get("delivery_evidence") or {}
                    ).get("workspace_write_event_count"),
                    "final_message_present": (
                        task.get("delivery_evidence") or {}
                    ).get("final_message_present"),
                    "seal": task.get("seal"),
                    "result": task.get("status"),
                    "delivery_classification": (
                        task.get("delivery_evidence") or {}
                    ).get("delivery_classification"),
                }
            )
        completed = sum(1 for row in rows if row["seal"] is True)
        return {
            "schema_version": "openevo.researchclawbench.delivery_canary_v3.v1",
            "status": (
                "DEEPSEEK_DELIVERY_RELIABILITY_PASSED"
                if completed == len(CANARY_TASKS)
                else "DEEPSEEK_DELIVERY_RELIABILITY_FAILED"
            ),
            "batch_id": self.batch_id,
            "task_ids": list(CANARY_TASKS),
            "rows": rows,
            "candidate_delivery_success_rate": (
                completed / len(CANARY_TASKS)
            ),
            "candidate_jobs": int(state.get("candidate_jobs", 0)),
            "evolution_jobs": int(state.get("evolution_jobs", 0)),
            "judge_jobs": int(state.get("judge_jobs", 0)),
            "retries": 0,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def run_next(self) -> dict[str, Any]:
        state = self.status()
        stage = CanaryStage(state["stage"])
        task_index = int(state["task_index"])
        if stage is CanaryStage.CANARY_INIT:
            task_id = CANARY_TASKS[0]
            receipt = self._stage_workspace(task_id, f"{task_id}_a0_baseline")
            return self._transition(
                stage,
                CanaryStage.TASK_PREPARED,
                f"{task_id}_0-prepared",
                updates={
                    "task_index": 0,
                    "tasks": {
                        **state["tasks"],
                        task_id: {
                            "baseline_candidate_workspace": str(
                                receipt.candidate_workspace
                            ),
                            "baseline_run_id": receipt.run_id,
                        },
                    },
                },
                receipt={"task_id": task_id, "prepared": True},
            )
        if stage is CanaryStage.TASK_PREPARED:
            task_id = CANARY_TASKS[task_index]
            task_state = state["tasks"][task_id]
            try:
                self._reserve(1)
                request = {
                    "task_id": task_id,
                    "run_id": task_state["baseline_run_id"],
                    "pass_name": "baseline",
                    "workspace": task_state["baseline_candidate_workspace"],
                    "evidence_root": str(
                        self._task_namespace(task_id)
                        / "evidence"
                        / "baseline"
                    ),
                }
            except Exception as exc:
                return self._transition(
                    stage,
                    CanaryStage.CANARY_FAILED,
                    f"{task_id}_blocked",
                    updates={
                        "failure_code": getattr(exc, "code", "CANARY_FAILED"),
                        "failure_message": str(exc),
                    },
                    receipt={"failure_classification": getattr(exc, "code", "CANARY_FAILED")},
                )
            return self._transition(
                stage,
                CanaryStage.TASK_BASELINE_RUNNING,
                f"{task_id}-baseline-start",
                updates={"active_baseline_request": request},
                receipt={"candidate_intent_persisted": True},
            )
        if stage is CanaryStage.TASK_BASELINE_RUNNING:
            task_id = CANARY_TASKS[task_index]
            task_state = state["tasks"][task_id]
            delivery_evidence = None
            try:
                candidate = self._effect(
                    kind="candidate-baseline",
                    request=state["active_baseline_request"],
                    execute=self.candidate_port.execute,
                )
                delivery_evidence = candidate.get("delivery_evidence")
                sealed_hash = _seal_workspace(
                    Path(candidate["candidate_output_root"])
                )
            except Exception as exc:
                return self._transition(
                    stage,
                    CanaryStage.TASK_NONDELIVERY,
                    f"{task_id}-nondelivery",
                    updates={
                        "tasks": {
                            **state["tasks"],
                            task_id: {
                                **task_state,
                                "status": "CANDIDATE_NONDELIVERY",
                                "failure_code": getattr(
                                    exc, "code", "WORKSPACE_VALIDATION_FAILED"
                                ),
                                "failure_message": str(exc),
                                "candidate_exit": 0,
                                "delivery_evidence": delivery_evidence,
                            },
                        }
                    },
                    receipt={"failure_classification": "CANDIDATE_NONDELIVERY"},
                )
            delivery_evidence = candidate.get("delivery_evidence")
            return self._transition(
                stage,
                CanaryStage.TASK_BASELINE_SEALED,
                f"{task_id}-sealed",
                updates={
                    "tasks": {
                        **state["tasks"],
                        task_id: {
                            **task_state,
                            "status": "BASELINE_SEALED",
                            "candidate_exit": candidate["exit_status"],
                            "sealed_hash": sealed_hash,
                            "seal": True,
                            "delivery_evidence": delivery_evidence,
                            "receipt": candidate,
                        },
                    },
                    "candidate_jobs": int(state.get("candidate_jobs", 0)) + 1,
                },
                receipt=candidate,
            )
        if stage is CanaryStage.TASK_BASELINE_SEALED:
            next_index = task_index + 1
            if next_index < len(CANARY_TASKS):
                task_id = CANARY_TASKS[next_index]
                receipt = self._stage_workspace(task_id, f"{task_id}_a0_baseline")
                return self._transition(
                    stage,
                    CanaryStage.TASK_PREPARED,
                    f"{task_id}-next-prepared",
                    updates={
                        "task_index": next_index,
                        "tasks": {
                            **state["tasks"],
                            task_id: {
                                "baseline_candidate_workspace": str(
                                    receipt.candidate_workspace
                                ),
                                "baseline_run_id": receipt.run_id,
                            },
                        },
                    },
                    receipt={"next_task": task_id},
                )
            result = self._summary(state)
            atomic_write_json(
                self.config.output_root / "canary_batch_result.json",
                result,
            )
            return self._transition(
                stage,
                CanaryStage.CANARY_CLOSED,
                "canary-closed",
                updates={"canary_result": result},
                receipt=result,
            )
        if stage is CanaryStage.TASK_NONDELIVERY:
            result = self._summary(state)
            atomic_write_json(
                self.config.output_root / "canary_batch_result.json",
                result,
            )
            return self._transition(
                stage,
                CanaryStage.CANARY_FAILED,
                "canary-failed",
                updates={"canary_result": result},
                receipt=result,
            )
        raise RuntimeError(f"unhandled canary v3 stage: {stage.value}")

    def run_until_terminal(self, *, max_transitions: int = 50) -> dict[str, Any]:
        for _ in range(max_transitions):
            state = self.status()
            if state["stage"] in {
                CanaryStage.CANARY_CLOSED.value,
                CanaryStage.CANARY_FAILED.value,
            }:
                return state
            self.run_next()
        raise RuntimeError("canary v3 transition budget exhausted")
