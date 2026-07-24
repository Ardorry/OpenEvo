from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import openevo_chembench.benchmark_receipt_v2 as receipt_module
from openevo_chembench.benchmark_receipt_v2 import BenchmarkAuthorizationV2
from openevo_chembench.chembench4k_dataset import (
    ChemBench4KDatasetLoader,
    write_dataset_manifest,
)
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REVISION,
)
from openevo_chembench.frozen_runner_v2 import (
    ChemBench4KFrozenRunnerV2,
    FrozenManifestError,
    FrozenProtocolTripwire,
    FrozenRunnerError,
    FrozenRunStatusV2,
)
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    FrozenAgentRequestV2,
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.local_codex_executor import (
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
)
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.sampling_v2 import generate_task_manifests
from openevo_chembench.v2_config import (
    ExecutorPolicyV2,
    FrozenArtifactBinding,
    FrozenExperimentConfigV2,
)


_MEMORY = "Check chemical constraints, then return exactly one uppercase choice letter."
_MEMORY_SHA256 = hashlib.sha256(_MEMORY.encode("utf-8")).hexdigest()
_RECEIPT_SHA256 = "e" * 64


def _authorization(
    *,
    receipt_sha256: str = _RECEIPT_SHA256,
    evidence_digest: str = "d" * 64,
) -> BenchmarkAuthorizationV2:
    return receipt_module._issue_benchmark_authorization_v2(
        receipt_sha256=receipt_sha256,
        evidence_digest=evidence_digest,
    )


class _DummyExecutor:
    def __init__(
        self,
        *,
        response: str = "A",
        tripwire: FrozenProtocolTripwire | None = None,
        security_violation: bool = False,
        fail_on_call: int | None = None,
        failure_code: LocalCodexExecutionErrorCode = (
            LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE
        ),
    ) -> None:
        self.response = response
        self.tripwire = tripwire
        self.security_violation = security_violation
        self.fail_on_call = fail_on_call
        self.failure_code = failure_code
        self.requests: list[FrozenAgentRequestV2] = []

    def execute_frozen(self, request: FrozenAgentRequestV2) -> RawAttempt:
        assert type(request) is FrozenAgentRequestV2
        self.requests.append(request)
        if self.tripwire is not None:
            self.tripwire.record_event("reflector")
        if self.security_violation:
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION,
                event_counts={"file_read": 1},
                event_digest="f" * 64,
                private_event_reference="private-audit-reference",
            )
        if self.fail_on_call == len(self.requests):
            raise LocalCodexExecutionError(self.failure_code)
        return RawAttempt(
            response=self.response,
            transcript_reference=TranscriptReference(
                reference=f"opaque-transcript-{len(self.requests)}"
            ),
        )


def _write_snapshot(workspace: Path) -> ChemBench4KDatasetLoader:
    snapshot = workspace / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
    for split, count in (("dev", 5), ("test", 2)):
        directory = snapshot / split
        directory.mkdir(parents=True, exist_ok=True)
        for category in CHEMBENCH4K_CATEGORIES:
            rows = [
                {
                    "question": f"{split} {category} unique question {index}?",
                    "A": f"{split} {category} option A {index}",
                    "B": f"{split} {category} option B {index}",
                    "C": f"{split} {category} option C {index}",
                    "D": f"{split} {category} option D {index}",
                    "answer": ("A", "B", "C", "D")[index % 4],
                }
                for index in range(count)
            ]
            (directory / f"{category}_benchmark.json").write_text(
                json.dumps(rows, ensure_ascii=False),
                encoding="utf-8",
            )
    write_dataset_manifest(snapshot)
    return ChemBench4KDatasetLoader(snapshot_root=snapshot)


def _generate_canary_manifests(
    workspace: Path,
    loader: ChemBench4KDatasetLoader,
) -> None:
    generate_task_manifests(
        loader,
        scope="canary18",
        public_path=workspace / "manifests" / "canary18_public.jsonl",
        private_path=workspace / "private_manifests" / "canary18_private.jsonl",
        summary_path=workspace / "manifests" / "canary18_summary.json",
    )


def _config(*, arm: str) -> FrozenExperimentConfigV2:
    enabled = arm == "evolved"
    return FrozenExperimentConfigV2(
        arm=arm,
        scope="canary18",
        run_name=f"{arm}_canary18_frozen_v2",
        output_directory=f"results/v2/canary18/{arm}",
        dataset_root=("data/chembench4k/AI4Chem_ChemBench4K/" + CHEMBENCH4K_REVISION),
        dataset_manifest=(
            "data/chembench4k/AI4Chem_ChemBench4K/"
            + CHEMBENCH4K_REVISION
            + "/chembench4k_dataset_manifest_v2.json"
        ),
        task_manifest="manifests/canary18_public.jsonl",
        private_task_manifest="private_manifests/canary18_private.jsonl",
        receipt_path="manifests/benchmark_execution_receipt_v2.json",
        pilot_protocol_hash="d" * 64,
        codex_cli_version="0.144.6",
        prompt_renderer_id="chembench4k_official_five_shot_v2",
        parser_id="official_first_capital_parser_v2",
        evaluator_id="chembench4k_accuracy_v2",
        artifact=FrozenArtifactBinding(
            enabled=enabled,
            frozen_artifact_id="core-text-memory-v2" if enabled else None,
            frozen_artifact_sha256="a" * 64 if enabled else None,
            context_resolution_digest="b" * 64 if enabled else None,
            resolved_memory_sha256=_MEMORY_SHA256 if enabled else None,
        ),
        executor=ExecutorPolicyV2(
            backend="local_codex_cli",
            harness="codex_cli",
            timeout_seconds=600,
            concurrency=1,
            infrastructure_retries_before_completion=0,
            tools_enabled=False,
            mcp_enabled=False,
            web_enabled=False,
            network_enabled=False,
            subagents_enabled=False,
        ),
    )


def _resolved_memory() -> CoreResolvedTextMemoryV2:
    return _issue_core_resolved_text_memory_v2(
        core_artifact_id="core-text-memory-v2",
        artifact_payload_sha256="a" * 64,
        context_resolution_digest="b" * 64,
        resolved_memory_sha256=_MEMORY_SHA256,
        markdown=_MEMORY,
    )


@pytest.fixture
def frozen_workspace(
    tmp_path: Path,
) -> tuple[Path, ChemBench4KDatasetLoader]:
    loader = _write_snapshot(tmp_path)
    _generate_canary_manifests(tmp_path, loader)
    return tmp_path, loader


def _runner(
    workspace: Path,
    loader: ChemBench4KDatasetLoader,
    executor: _DummyExecutor,
    *,
    arm: str,
    tripwire: FrozenProtocolTripwire | None = None,
    resume: bool = False,
    authorization: BenchmarkAuthorizationV2 | None = None,
) -> ChemBench4KFrozenRunnerV2:
    return ChemBench4KFrozenRunnerV2(
        config=_config(arm=arm),
        loader=loader,
        executor=executor,
        workspace_root=workspace,
        authorization=authorization or _authorization(),
        resolved_text_memory=_resolved_memory() if arm == "evolved" else None,
        tripwire=tripwire,
        resume=resume,
    )


def test_baseline_runs_once_per_item_without_memory(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    executor = _DummyExecutor(response="B")
    runner = _runner(workspace, loader, executor, arm="baseline")

    result = runner.run()

    assert result.status is FrozenRunStatusV2.COMPLETED
    assert result.planned_tasks == result.completed_tasks == 18
    assert result.model_calls == len(executor.requests) == 18
    assert all(request.resolved_text_memory is None for request in executor.requests)
    assert not hasattr(runner, "_reflector")
    assert not hasattr(runner, "_artifact_writer")
    assert not hasattr(runner, "_evolution_job")


def test_evolved_uses_one_identical_core_resolved_memory_for_every_item(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    executor = _DummyExecutor()
    memory = _resolved_memory()
    runner = ChemBench4KFrozenRunnerV2(
        config=_config(arm="evolved"),
        loader=loader,
        executor=executor,
        workspace_root=workspace,
        authorization=_authorization(),
        resolved_text_memory=memory,
    )

    result = runner.run()

    assert result.status is FrozenRunStatusV2.COMPLETED
    assert len(executor.requests) == 18
    assert all(request.resolved_text_memory is memory for request in executor.requests)


def test_results_are_public_private_split_and_private_is_mode_0600(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    result = _runner(
        workspace,
        loader,
        _DummyExecutor(response=" A "),
        arm="baseline",
    ).run()

    public_path = result.output_directory / "public" / "results.jsonl"
    private_path = result.output_directory / "private" / "results.jsonl"
    public_text = public_path.read_text(encoding="utf-8")
    private_rows = [
        json.loads(line) for line in private_path.read_text(encoding="utf-8").splitlines()
    ]
    public_rows = [json.loads(line) for line in public_text.splitlines()]

    assert len(public_rows) == len(private_rows) == 18
    assert all("target" not in row and "source_index" not in row for row in public_rows)
    assert all(row["raw_completion"] == " A " for row in public_rows)
    assert all(row["parsed_prediction"] == "A" for row in public_rows)
    assert all("score" in row and "transcript_reference" in row for row in public_rows)
    assert all(row["target"] in {"A", "B", "C", "D"} for row in private_rows)
    assert private_path.stat().st_mode & 0o077 == 0


def test_wrong_or_parse_failed_completion_is_never_retried(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    executor = _DummyExecutor(response="not parseable")
    result = _runner(workspace, loader, executor, arm="baseline").run()

    assert result.status is FrozenRunStatusV2.COMPLETED
    assert result.model_calls == len(executor.requests) == 18
    rows = [
        json.loads(line)
        for line in (result.output_directory / "public" / "results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert all(row["parsed_prediction"] == "" for row in rows)


def test_manifest_public_private_mismatch_fails_before_executor(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    private_path = workspace / "private_manifests" / "canary18_private.jsonl"
    rows = private_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(rows[0])
    first["uid"] = "0" * 64
    rows[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    private_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    private_path.chmod(0o600)
    executor = _DummyExecutor()

    with pytest.raises(FrozenManifestError):
        _runner(workspace, loader, executor, arm="baseline")

    assert executor.requests == []


def test_tripwire_stops_run_on_forbidden_evolution_event(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    tripwire = FrozenProtocolTripwire()
    executor = _DummyExecutor(tripwire=tripwire)
    runner = _runner(
        workspace,
        loader,
        executor,
        arm="baseline",
        tripwire=tripwire,
    )

    result = runner.run()

    assert result.status is FrozenRunStatusV2.FROZEN_PROTOCOL_VIOLATION
    assert result.completed_tasks == 0
    assert result.model_calls == len(executor.requests) == 1
    state = json.loads((result.output_directory / "run_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "FROZEN_PROTOCOL_VIOLATION"
    assert state["resume_allowed"] is False


def test_security_tool_violation_propagates_to_run_and_forbids_retry_resume(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    executor = _DummyExecutor(security_violation=True)

    result = _runner(workspace, loader, executor, arm="baseline").run()

    assert result.status is FrozenRunStatusV2.SECURITY_TOOL_USE_VIOLATION
    assert result.completed_tasks == 0
    assert result.model_calls == len(executor.requests) == 1
    state = json.loads((result.output_directory / "run_state.json").read_text(encoding="utf-8"))
    assert state["resume_allowed"] is False
    public_failure = json.loads(
        (result.output_directory / "public" / "results.jsonl").read_text(encoding="utf-8")
    )
    assert public_failure["event_counts"] == {"file_read": 1}
    assert "private_event_reference" not in public_failure
    assert public_failure["retry_allowed"] is False
    assert public_failure["replacement_completion_allowed"] is False


def test_existing_output_directory_is_never_overwritten(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    first = _runner(workspace, loader, _DummyExecutor(), arm="baseline")
    first.run()
    second_executor = _DummyExecutor()
    second = _runner(workspace, loader, second_executor, arm="baseline")

    with pytest.raises(FrozenRunnerError, match="output target already exists"):
        second.run()

    assert second_executor.requests == []


def test_safe_resume_validates_prefix_and_continues_after_no_completion_failure(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    initial_executor = _DummyExecutor(
        response="A",
        fail_on_call=4,
        failure_code=LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE,
    )
    failed = _runner(
        workspace,
        loader,
        initial_executor,
        arm="baseline",
    ).run()

    assert failed.status is FrozenRunStatusV2.EXECUTION_FAILED
    assert failed.completed_tasks == 3
    assert failed.model_calls == 4
    failed_state = json.loads(
        (failed.output_directory / "run_state.json").read_text(encoding="utf-8")
    )
    assert failed_state["resume_allowed"] is True
    assert failed_state["failed_ordinal"] == 3
    assert failed_state["failed_item_completion_observed"] is False
    public_prefix = (failed.output_directory / "public" / "results.jsonl").read_bytes()
    private_prefix = (failed.output_directory / "private" / "results.jsonl").read_bytes()

    resumed_executor = _DummyExecutor(response="B")
    resumed = _runner(
        workspace,
        loader,
        resumed_executor,
        arm="baseline",
        resume=True,
    ).run()

    assert resumed.status is FrozenRunStatusV2.COMPLETED
    assert resumed.completed_tasks == 18
    assert resumed.model_calls == 19
    assert len(resumed_executor.requests) == 15
    assert (
        (resumed.output_directory / "public" / "results.jsonl")
        .read_bytes()
        .startswith(public_prefix)
    )
    assert (
        (resumed.output_directory / "private" / "results.jsonl")
        .read_bytes()
        .startswith(private_prefix)
    )
    resumed_state = json.loads(
        (resumed.output_directory / "run_state.json").read_text(encoding="utf-8")
    )
    assert resumed_state["resume_count"] == 1
    assert resumed_state["resume_allowed"] is False


def test_resume_rejects_receipt_or_evidence_hash_mismatch(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    failed = _runner(
        workspace,
        loader,
        _DummyExecutor(fail_on_call=1),
        arm="baseline",
    ).run()
    assert failed.status is FrozenRunStatusV2.EXECUTION_FAILED
    resumed = _runner(
        workspace,
        loader,
        _DummyExecutor(),
        arm="baseline",
        resume=True,
        authorization=_authorization(
            receipt_sha256="1" * 64,
            evidence_digest="2" * 64,
        ),
    )

    with pytest.raises(FrozenRunnerError, match="evidence hash mismatch"):
        resumed.run()


def test_resume_recomputes_dataset_artifact_model_and_manifest_hashes(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    failed = _runner(
        workspace,
        loader,
        _DummyExecutor(fail_on_call=1),
        arm="baseline",
    ).run()
    state_path = failed.output_directory / "run_state.json"
    original = json.loads(state_path.read_text(encoding="utf-8"))

    for field_name in (
        "dataset_combined_sha256",
        "artifact_identity_sha256",
        "model_identity_sha256",
        "public_task_manifest_sha256",
        "private_task_manifest_sha256",
    ):
        changed = json.loads(json.dumps(original))
        changed["evidence_binding"][field_name] = "0" * 64
        state_path.write_text(
            json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(FrozenRunnerError, match="evidence hash mismatch"):
            _runner(
                workspace,
                loader,
                _DummyExecutor(),
                arm="baseline",
                resume=True,
            ).run()
        state_path.write_text(
            json.dumps(original, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


def test_resume_rejects_modified_completed_result_prefix(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    failed = _runner(
        workspace,
        loader,
        _DummyExecutor(fail_on_call=3),
        arm="baseline",
    ).run()
    assert failed.completed_tasks == 2
    public_path = failed.output_directory / "public" / "results.jsonl"
    rows = public_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(rows[0])
    first["score"] = 1.0 - first["score"]
    rows[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    public_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    resumed = _runner(
        workspace,
        loader,
        _DummyExecutor(),
        arm="baseline",
        resume=True,
    )

    with pytest.raises(FrozenRunnerError, match="prefix digest mismatch"):
        resumed.run()


def test_resume_rejects_security_and_frozen_protocol_terminal_states(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    security = _runner(
        workspace,
        loader,
        _DummyExecutor(security_violation=True),
        arm="baseline",
    ).run()
    assert security.status is FrozenRunStatusV2.SECURITY_TOOL_USE_VIOLATION

    with pytest.raises(FrozenRunnerError, match="protocol identity mismatch"):
        _runner(
            workspace,
            loader,
            _DummyExecutor(),
            arm="baseline",
            resume=True,
        ).run()

    frozen_workspace_root = workspace / "frozen-protocol-case"
    frozen_loader = _write_snapshot(frozen_workspace_root)
    _generate_canary_manifests(frozen_workspace_root, frozen_loader)
    tripwire = FrozenProtocolTripwire()
    frozen = _runner(
        frozen_workspace_root,
        frozen_loader,
        _DummyExecutor(tripwire=tripwire),
        arm="baseline",
        tripwire=tripwire,
    ).run()
    assert frozen.status is FrozenRunStatusV2.FROZEN_PROTOCOL_VIOLATION
    with pytest.raises(FrozenRunnerError, match="protocol identity mismatch"):
        _runner(
            frozen_workspace_root,
            frozen_loader,
            _DummyExecutor(),
            arm="baseline",
            resume=True,
        ).run()


def test_resume_rejects_failure_that_might_have_produced_completion(
    frozen_workspace: tuple[Path, ChemBench4KDatasetLoader],
) -> None:
    workspace, loader = frozen_workspace
    failed = _runner(
        workspace,
        loader,
        _DummyExecutor(
            fail_on_call=1,
            failure_code=LocalCodexExecutionErrorCode.CLI_TIMEOUT,
        ),
        arm="baseline",
    ).run()
    assert failed.status is FrozenRunStatusV2.EXECUTION_FAILED
    state = json.loads((failed.output_directory / "run_state.json").read_text(encoding="utf-8"))
    assert state["resume_allowed"] is False

    with pytest.raises(FrozenRunnerError, match="resume is forbidden"):
        _runner(
            workspace,
            loader,
            _DummyExecutor(),
            arm="baseline",
            resume=True,
        ).run()
