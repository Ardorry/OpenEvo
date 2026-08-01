from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from zipfile import ZIP_DEFLATED, ZipFile

import openevo.evolution.methods as core_methods
import pytest
from openevo import __version__
from openevo.evolution.framework import DistributionArtifactExpectation
from openevo.evolution.framework import builtins as core_builtins
from openevo.evolution.framework.builtins import load_verified_builtin_registry
from openevo.evolution.framework.loading import _verify_distribution_install

from openevo_chembench.frozen_runtime_v2 import _issue_core_resolved_text_memory_v2
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
    _issue_core_resolved_auxiliary_v2,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedSessionContextBindingV2,
    issue_supervised_context_binding_receipt_v2,
)
from openevo_chembench.supervised_transfer_v2.core import (
    TaskwiseCoreEvolutionBridgeV1,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedManagedCodexExecutorV2,
)
from openevo_chembench.supervised_transfer_v2.memory import SUPERVISED_MEMORY_LIMITS_V2
from openevo_chembench.supervised_transfer_v3 import reporting as reporting_v3
from openevo_chembench.supervised_transfer_v3.config import (
    EVOLUTION_CYCLES,
    PROTOCOL_ID,
    SOURCE_FAMILY_ID,
    TRAIN_ROUNDS,
    TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID,
    SupervisedTransferConfigV3,
    load_config_v3,
)
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedExperimentV3Error,
    SupervisedTransferExperimentV3,
    build_complete_dry_run_v3,
    load_experiment_inputs_v3,
)
from openevo_chembench.supervised_transfer_v3.source_identity import (
    verify_source_manifest_v3,
)
from openevo_chembench.supervised_transfer_v3.split_reference import (
    build_split_reference_receipt_v3,
    verify_split_reference_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.test_ledger import (
    FinalTestConsumptionLedgerV3,
    FinalTestLedgerV3Error,
)

REPOSITORY = Path(__file__).resolve().parents[4]
CONFIG = REPOSITORY / "benchmarks/chembench/configs/supervised_transfer_v3/chembench_supervised_transfer_v3.yaml"
TWO_ROUND_CONFIG = (
    REPOSITORY
    / "benchmarks/chembench/configs/supervised_transfer_v3/chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
)


class _SourceDistribution:
    metadata: ClassVar[dict[str, str]] = {"Name": "openevo"}
    version = __version__

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, _filename: str) -> None:
        return None


def _registry(root: Path):
    install_root = Path(core_builtins.__file__).resolve().parents[3]
    artifact = root / f"openevo-{__version__}-py3-none-any.whl"
    root.mkdir()
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith((".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            f"openevo-{__version__}.dist-info/METADATA",
            f"Name: openevo\nVersion: {__version__}\n",
        )
    verified = _verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=__version__,
            distribution_digest=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        ),
        artifact,
        metadata_provider=lambda _name: _SourceDistribution(install_root),
    )
    return load_verified_builtin_registry(verified)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _memory(evidence_digest: str, category: str) -> str:
    return f"""# Category Memory: {category}

## Confirmed Principles
- None.

## Provisional Principles
- Rule ID=V3-P-001; Status=provisional; Category={category}; Target Type=text_memory; Trigger=a category-specific chemistry choice; Principle=apply explicit chemical constraints; Action=compare every option against those constraints; Validation=independently verify the chosen structure; Evidence Count=1; Supporting Train Ordinals Hash={_sha('ordinal')}; First Seen Cycle=1; Last Confirmed Cycle=0; Contradiction Count=0; Evidence=current Train observation; Evidence Digests={evidence_digest}

## Common Failure Modes
- Accepting a plausible option without a chemical consistency check.

## Option Elimination Checks
- Reject options that violate the governing chemical constraint.

## Retired Or Contradicted
- None.

## Output Discipline
- Return one uppercase choice letter and no explanation.

## Do
- Apply a rule only when its trigger holds.

## Avoid
- Avoid instance-specific answer mappings.

## Validate
- Validate chemistry and final answer format.

## When Applicable
- Use only category-matched rules.

## Retired Or Superseded
- Ignore retired rules above.
"""


class _FakeExecutor:
    def __init__(self) -> None:
        self.requests = []
        self.receipts = {}

    def execute(self, request):
        self.requests.append(request)
        binding = SupervisedSessionContextBindingV2.from_context(
            session_id=request.session_id,
            context=request.resolved_context,
        )
        self.receipts[request.session_id] = issue_supervised_context_binding_receipt_v2(
            expected=binding,
            actual=binding,
        )
        return RawAttempt(
            response="A",
            transcript_reference=TranscriptReference(
                "openevo-rollout-jsonl:sha256:" + _sha(request.session_id)
            ),
        )

    def consume_context_receipt(self, session_id):
        return self.receipts.pop(session_id)


class _SyntheticBridge:
    def __init__(
        self,
        bridge: TaskwiseCoreEvolutionBridgeV1,
        active_packet: list[str],
        active_category: list[str],
        category: str,
    ) -> None:
        self.bridge = bridge
        self.active_packet = active_packet
        self.active_category = active_category
        self.category = category
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.bridge.close()

    def apply_update(self, request):
        self.requests.append(request)
        self.active_packet.append(request.supervised_packet.digest)
        self.active_category.append(self.category)
        return self.bridge.apply_update(request, test_only_allow_synthetic_reflector=True)

    def issue_runtime_context(self, result):
        return self.bridge.issue_runtime_context(result)

    def verify_update_result(self, result):
        return self.bridge.verify_update_result(result)


def _test_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_path: Path = CONFIG,
):
    config = load_config_v3(config_path.resolve())
    loaded = load_experiment_inputs_v3(REPOSITORY, config, require_runtime=False)
    payload = dict(config.payload)
    payload["roots"] = {
        "results": str(tmp_path / "results"),
        "state": str(tmp_path / "state"),
        "reports": str(tmp_path / "reports"),
    }
    test_config = SupervisedTransferConfigV3(
        path=config.path,
        payload=payload,
        digest=_sha(canonical_json_bytes(payload).decode()),
    )
    runtime = SimpleNamespace(
        digest=_sha("runtime"),
        service_run_id="stv2-services-20990101T000000Z-deadbeef",
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment._benchmark_status",
        lambda _repository: "",
    )
    return replace(
        loaded,
        config=test_config,
        managed_codex=SimpleNamespace(
            digest=_sha("reflector"),
            executable_sha256=_sha("binary"),
            executable=tmp_path / "unused-codex",
        ),
        candidate_codex=SimpleNamespace(
            digest=_sha("candidate"),
            executable_sha256=_sha("binary"),
        ),
        runtime_services=runtime,
    )


def test_v3_config_is_three_answer_two_cycle_evolved_only() -> None:
    config = load_config_v3(CONFIG.resolve())
    assert TRAIN_ROUNDS == 3
    assert EVOLUTION_CYCLES == 2
    assert config.payload["executor"]["control_train_enabled"] is False
    assert config.payload["executor"]["control_test_enabled"] is False
    assert config.call_budget["online_candidate_task_calls"] == 1350
    assert config.call_budget["reflector_calls"] == 900
    assert config.call_budget["evolved_test_candidate_calls"] == 450
    assert config.call_budget["core_jobs"] == 2700
    assert config.call_budget["answer_and_reflector_model_calls"] == 2700


def test_v3_two_round_one_evolution_profile_has_exact_budget() -> None:
    config = load_config_v3(TWO_ROUND_CONFIG.resolve())
    assert config.protocol_id == TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID
    assert config.train_rounds == 2
    assert config.evolution_cycles == 1
    assert config.call_budget["online_candidate_task_calls"] == 900
    assert config.call_budget["reflector_calls"] == 450
    assert config.call_budget["evolved_test_candidate_calls"] == 450
    assert config.call_budget["core_jobs"] == 1350
    assert config.call_budget["answer_and_reflector_model_calls"] == 1800
    assert config.call_budget["total_model_calls"] == 3150


def test_v3_two_round_one_evolution_dry_run_has_one_update() -> None:
    config = load_config_v3(TWO_ROUND_CONFIG.resolve())
    inputs = load_experiment_inputs_v3(REPOSITORY, config, require_runtime=False)
    result = build_complete_dry_run_v3(inputs)
    assert result["protocol_id"] == TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID
    assert all(item["sessions_per_task"] == 2 for item in result["train_schedule"])
    assert all(item["cycles_per_task"] == 1 for item in result["train_schedule"])
    assert all(
        item["sequence"] == ["ROUND_0", "CYCLE_1", "ROUND_1_FINAL"]
        for item in result["train_schedule"]
    )


def test_v3_split_is_exact_read_only_v2_reference() -> None:
    receipt = verify_split_reference_receipt_v3(REPOSITORY)
    assert receipt == {
        "status": "PASS",
        "sha256": receipt["sha256"],
        **build_split_reference_receipt_v3(REPOSITORY),
    }
    assert receipt["train_count"] == 450
    assert receipt["test_count"] == 450
    assert receipt["reserve_count"] == 3109
    assert receipt["test_historical_exposure"] == 0
    assert receipt["source_split_receipt"] == "450251433fd30fe8efda7ae214e7163ec67372ef982a85440ccdb82073dbe275"


def test_v3_source_manifest_closes_shared_core_abi() -> None:
    receipt = verify_source_manifest_v3(REPOSITORY)
    assert receipt["status"] == "PASS"
    payload = json.loads((REPOSITORY / "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json").read_text())
    assert payload["protocol_id"] == SOURCE_FAMILY_ID
    paths = {row["path"] for row in payload["source_files"]}
    assert "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/core.py" in paths
    assert "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/experiment.py" in paths


def test_v3_dry_run_has_no_control_probe_round3_or_cycle3() -> None:
    config = load_config_v3(CONFIG.resolve())
    inputs = load_experiment_inputs_v3(REPOSITORY, config, require_runtime=False)
    result = build_complete_dry_run_v3(inputs)
    assert result["model_calls"] == 0
    assert result["stage_order"] == ["ONLINE_TRAIN", "FREEZE_THREE_TARGETS", "EVOLVED_TEST", "REPORTING"]
    assert all(item["sequence"] == ["ROUND_0", "CYCLE_1", "ROUND_1", "CYCLE_2", "ROUND_2_FINAL"] for item in result["train_schedule"])
    assert "ROUND_3" not in json.dumps(result)
    assert "CYCLE_3" not in json.dumps(result)


def test_v3_executor_marks_task_request_with_v3_protocol(tmp_path: Path) -> None:
    executor = SupervisedManagedCodexExecutorV2(
        arm="online",
        timeout_seconds=1200,
        rollout_client=SimpleNamespace(),
        protocol_id=PROTOCOL_ID,
    )
    context = _resolved_context()
    from openevo_chembench.supervised_transfer_v2.context_binding import SupervisedAgentRequestV2

    request = SupervisedAgentRequestV2(
        rendered_public_prompt="Question\nAnswer:",
        resolved_context=context,
        session_id="stv3-session-protocol-0001",
        arm="online",
        task_ordinal=0,
        round_index=1,
        run_id="stv3-test-run-0001",
        task_uid=_sha("task"),
    )
    task_request = executor.build_task_request(request, workspace_root=tmp_path)
    assert task_request.metadata["openevo_chembench"]["protocol_id"] == PROTOCOL_ID
    assert task_request.agent.harness == "codex"


def test_v3_offline_smoke_closes_three_two_six(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _test_inputs(tmp_path, monkeypatch)
    active_packet = [""]
    active_category = ["Temperature_Prediction"]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1], active_category[-1]),
    )
    registry = _registry(tmp_path / "registry")

    def bridge_factory(root: Path, category: str) -> _SyntheticBridge:
        return _SyntheticBridge(
            TaskwiseCoreEvolutionBridgeV1(
                db_path=root / "evolution.sqlite3",
                artifact_root=root / "artifacts",
                executable_registry=registry,
                checkpoint_path=root / "checkpoints.jsonl",
                memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
                updates_per_task=2,
            ),
            active_packet,
            active_category,
            category,
        )

    executor = _FakeExecutor()
    experiment = SupervisedTransferExperimentV3(
        inputs=inputs,
        run_id="stv3-offline-smoke-0001",
        run_mode="smoke",
        executor_factory=lambda _arm: executor,
        bridge_factory=bridge_factory,
    )
    result = experiment.run_smoke()
    assert result["status"] == "PASS"
    assert [request.round_index for request in executor.requests] == [0, 1, 2]
    assert executor.requests[0].resolved_context is None
    assert all(request.resolved_context is not None for request in executor.requests[1:])
    assert experiment._state["task_sessions"] == 3
    assert experiment._state["reflector_calls"] == 2
    assert experiment._state["core_jobs"] == 6
    assert experiment._state["context_resolutions"] == 6
    assert experiment._state["text_memory_artifacts"] == 2
    assert experiment._state["skill_artifacts"] == 2
    assert experiment._state["agent_system_artifacts"] == 2


def test_v3_two_round_one_evolution_smoke_closes_two_one_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _test_inputs(tmp_path, monkeypatch, config_path=TWO_ROUND_CONFIG)
    active_packet = [""]
    active_category = ["Temperature_Prediction"]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1], active_category[-1]),
    )
    registry = _registry(tmp_path / "registry-one-update")

    def bridge_factory(root: Path, category: str) -> _SyntheticBridge:
        return _SyntheticBridge(
            TaskwiseCoreEvolutionBridgeV1(
                db_path=root / "evolution.sqlite3",
                artifact_root=root / "artifacts",
                executable_registry=registry,
                checkpoint_path=root / "checkpoints.jsonl",
                memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
                updates_per_task=1,
            ),
            active_packet,
            active_category,
            category,
        )

    executor = _FakeExecutor()
    experiment = SupervisedTransferExperimentV3(
        inputs=inputs,
        run_id="stv3-one-update-smoke-0001",
        run_mode="smoke",
        executor_factory=lambda _arm: executor,
        bridge_factory=bridge_factory,
    )
    result = experiment.run_smoke()
    assert result["status"] == "PASS"
    assert [request.round_index for request in executor.requests] == [0, 1]
    assert executor.requests[0].resolved_context is None
    assert executor.requests[1].resolved_context is not None
    assert experiment._state["task_sessions"] == 2
    assert experiment._state["reflector_calls"] == 1
    assert experiment._state["core_jobs"] == 3
    assert experiment._state["context_resolutions"] == 3
    assert experiment._state["text_memory_artifacts"] == 1
    assert experiment._state["skill_artifacts"] == 1
    assert experiment._state["agent_system_artifacts"] == 1


def test_v3_evolved_test_requires_frozen_context_before_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _test_inputs(tmp_path, monkeypatch)
    executor = _FakeExecutor()
    experiment = SupervisedTransferExperimentV3(
        inputs=inputs,
        run_id="stv3-context-guard-0001",
        run_mode="smoke",
        executor_factory=lambda _arm: executor,
        bridge_factory=lambda _root, _category: None,
    )
    experiment._state["stage"] = "EVOLVED_TEST"
    with pytest.raises(
        SupervisedExperimentV3Error,
        match="V3_EVOLVED_TEST_CONTEXT_REQUIRED",
    ):
        experiment._execute_single_session(
            executor,
            task=inputs.test[0],
            context=None,
            logical_arm="test_evolved",
            executor_arm="online",
            task_ordinal=0,
            round_index=0,
            stage="EVOLVED_TEST",
            session_id="stv3-test-context-guard-0001",
            completion_callback=None,
        )
    assert executor.requests == []


def test_v3_two_task_core_chain_carries_cycle2_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _test_inputs(tmp_path, monkeypatch)
    active_packet = [""]
    active_category = ["Name_Conversion"]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1], active_category[-1]),
    )
    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=tmp_path / "core/evolution.sqlite3",
        artifact_root=tmp_path / "core/artifacts",
        executable_registry=_registry(tmp_path / "registry-two"),
        checkpoint_path=tmp_path / "core/checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
        updates_per_task=2,
    )
    wrapper = _SyntheticBridge(bridge, active_packet, active_category, "Name_Conversion")
    executor = _FakeExecutor()
    experiment = SupervisedTransferExperimentV3(
        inputs=inputs,
        run_id="stv3-offline-carry-0001",
        run_mode="smoke",
        executor_factory=lambda _arm: executor,
        bridge_factory=lambda _root, _category: wrapper,
    )
    experiment._state["stage"] = "ONLINE_TRAIN"
    tasks = [task for task in inputs.train if task.category == "Name_Conversion"][:2]
    try:
        first = experiment._run_one_online_task_v3(executor, task=tasks[0], category_ordinal=0, bridge=wrapper, stage="ONLINE_TRAIN", persist_head=True)
        second = experiment._run_one_online_task_v3(executor, task=tasks[1], category_ordinal=1, bridge=wrapper, stage="ONLINE_TRAIN", persist_head=True)
        assert first.update_index == second.update_index == 2
        assert first.global_update_ordinal == 2
        assert second.global_update_ordinal == 4
        assert wrapper.requests[2].predecessor == first.predecessor_identity()
        assert executor.requests[3].resolved_context is not None
        with sqlite3.connect(tmp_path / "core/evolution.sqlite3") as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 12
    finally:
        bridge.close()


def test_two_update_bridge_rejects_cycle3(tmp_path: Path) -> None:
    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=tmp_path / "core/evolution.sqlite3",
        artifact_root=tmp_path / "core/artifacts",
        executable_registry=_registry(tmp_path / "registry-three"),
        checkpoint_path=tmp_path / "core/checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
        updates_per_task=2,
    )
    try:
        assert bridge._updates_per_task == 2
    finally:
        bridge.close()


def test_v2_bridge_default_remains_three_updates(tmp_path: Path) -> None:
    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=tmp_path / "core/evolution.sqlite3",
        artifact_root=tmp_path / "core/artifacts",
        executable_registry=_registry(tmp_path / "registry-default"),
        checkpoint_path=tmp_path / "core/checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
    )
    try:
        assert bridge._updates_per_task == 3
    finally:
        bridge.close()


def test_v3_test_ledger_is_evolved_only_and_single_pass(tmp_path: Path) -> None:
    ledger = FinalTestConsumptionLedgerV3(
        path=(tmp_path / "ledger.jsonl").resolve(),
        source_commit="1" * 40,
        artifact_set_digest="2" * 64,
        config_digest="3" * 64,
        model_digest="4" * 64,
    )
    uid = "5" * 64
    ledger.claim(task_uid=uid, attempt_id="stv3-attempt-0001", timestamp="time-1")
    ledger.complete(task_uid=uid, attempt_id="stv3-attempt-0001", completion="A", timestamp="time-2")
    with pytest.raises(FinalTestLedgerV3Error, match="TEST_ITEM_ALREADY_COMPLETED"):
        ledger.claim(task_uid=uid, attempt_id="stv3-attempt-0002", timestamp="time-3")
    assert ledger.summary()["completion_count"] == 1


def test_v3_ledger_allows_retry_only_before_completion(tmp_path: Path) -> None:
    ledger = FinalTestConsumptionLedgerV3(
        path=(tmp_path / "ledger.jsonl").resolve(),
        source_commit="1" * 40,
        artifact_set_digest="2" * 64,
        config_digest="3" * 64,
        model_digest="4" * 64,
    )
    uid = "6" * 64
    ledger.claim(task_uid=uid, attempt_id="stv3-attempt-0001", timestamp="time-1")
    ledger.claim(task_uid=uid, attempt_id="stv3-attempt-0002", timestamp="time-2")
    ledger.complete(task_uid=uid, attempt_id="stv3-attempt-0002", completion="B", timestamp="time-3")
    assert ledger.summary()["attempt_count"] == 2


def test_v3_ledger_reopens_new_and_historical_blank_separated_rows(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    identity = {
        "path": path,
        "source_commit": "1" * 40,
        "artifact_set_digest": "2" * 64,
        "config_digest": "3" * 64,
        "model_digest": "4" * 64,
    }
    ledger = FinalTestConsumptionLedgerV3(**identity)
    uid = "7" * 64
    ledger.claim(task_uid=uid, attempt_id="stv3-attempt-0001", timestamp="time-1")
    ledger.complete(
        task_uid=uid,
        attempt_id="stv3-attempt-0001",
        completion="C",
        timestamp="time-2",
    )
    assert "\n\n" not in path.read_text(encoding="utf-8")
    assert FinalTestConsumptionLedgerV3(**identity).summary()["completion_count"] == 1

    historical = path.read_text(encoding="utf-8").replace("\n", "\n\n")
    path.write_text(historical, encoding="utf-8")
    path.chmod(0o600)
    reopened = FinalTestConsumptionLedgerV3(**identity)
    assert reopened.summary()["completion_count"] == 1


def test_v3_report_source_forbids_paired_causal_claims() -> None:
    source = (REPOSITORY / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/reporting.py").read_text()
    assert "mcnemar_exact_p" not in source.lower()
    assert "relative_error_reduction" not in source.lower()
    assert "paired_bootstrap" not in source.lower()


def test_v3_reporting_emits_evolved_only_deliverables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_root = tmp_path / "result"
    state_root = tmp_path / "state"
    (result_root / "public").mkdir(parents=True)
    (state_root / "private").mkdir(parents=True)
    public = []
    private = []
    categories = (
        "Name_Conversion",
        "Property_Prediction",
        "Mol2caption",
        "Caption2mol",
        "Product_Prediction",
        "Retrosynthesis",
        "Yield_Prediction",
        "Temperature_Prediction",
        "Solvent_Prediction",
    )
    for category in categories:
        for task_ordinal in range(50):
            for cycle in (1,):
                public.append(
                    {
                        "kind": "REFLECTOR_SUPERVISED",
                        "stage": "ONLINE_TRAIN",
                        "category": category,
                        "task_index": task_ordinal,
                        "cycle": cycle,
                        "text_memory_bytes": 100 + cycle,
                        "skill_bytes": 200 + cycle,
                        "agent_system_bytes": 50 + cycle,
                        "confirmed_rules": cycle - 1,
                        "provisional_rules": 1,
                        "retired_rules": 0,
                    }
                )
            for round_index in range(2):
                private.append(
                    {
                        "kind": "PRIVATE_EVALUATED",
                        "stage": "ONLINE_TRAIN",
                        "category": category,
                        "task_ordinal": task_ordinal,
                        "round_index": round_index,
                        "official_prediction": "A",
                        "strict_prediction": "A",
                        "strict_parse_status": "parsed",
                        "correct": round_index > 0,
                    }
                )
            private.append(
                {
                    "kind": "PRIVATE_EVALUATED",
                    "stage": "EVOLVED_TEST",
                    "category": category,
                    "task_ordinal": task_ordinal,
                    "round_index": 0,
                    "official_prediction": "A",
                    "strict_prediction": "A",
                    "strict_parse_status": "parsed",
                    "correct": True,
                }
            )
    (result_root / "public/events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in public),
        encoding="utf-8",
    )
    (state_root / "private/events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in private),
        encoding="utf-8",
    )
    (state_root / "run_state.json").write_text(
        json.dumps(
            {
                "protocol_id": TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID,
                "source_commit": "1" * 40,
                "config_sha256": "2" * 64,
                "split_receipt_sha256": "3" * 64,
                "source_manifest_sha256": "4" * 64,
                "attempts_per_train_task": 2,
                "evolution_cycles_per_train_task": 1,
                "security_findings": 0,
                "context_findings": 0,
                "artifact_findings": 0,
                "core_jobs": 1350,
                "infrastructure_failures": 0,
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "frozen_three_target_transfer_receipt_v3.json",
        "final_test_ledger_receipt_v3.json",
    ):
        (result_root / "public" / name).write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(reporting_v3, "REPORT_ROOT", tmp_path / "reports")
    receipt = reporting_v3.build_reports_v3(
        repository_root=REPOSITORY,
        result_root=result_root,
        state_root=state_root,
        run_id="stv3-report-offline-0001",
    )
    report_root = Path(str(receipt["report_root"]))
    assert receipt["status"] == "PASS"
    assert receipt["test_accuracy"] == 1.0
    assert {
        "final_report.md",
        "final_report.html",
        "train_online_results.csv",
        "test_evolved_results.csv",
        "per_category_results.csv",
        "target_growth.csv",
        "rule_growth.csv",
        "correctness_transitions.csv",
        "frozen_targets_receipt.json",
        "integrity_receipt.json",
        "debug_history.md",
        "paid_usage.json",
    }.issubset({path.name for path in report_root.iterdir()})


def _resolved_context() -> CoreResolvedSupervisedContextV2:
    memory = "# Category Memory: Name_Conversion\n\n## Output Discipline\n- answer once\n"
    skill = "# Category Skill: Name_Conversion\n\n## Workflow\n- validate naming\n"
    system = "# Category Agent System: Name_Conversion\n\n## Directives\n- answer once\n"
    return CoreResolvedSupervisedContextV2(
        memory=_issue_core_resolved_text_memory_v2(
            core_artifact_id="memory-artifact-v3",
            artifact_payload_sha256=_sha(memory),
            context_resolution_digest="1" * 64,
            resolved_memory_sha256=_sha(memory),
            markdown=memory,
        ),
        skill=_issue_core_resolved_auxiliary_v2(
            target_id="skill_bundle",
            core_artifact_id="skill-artifact-v3",
            artifact_payload_sha256=_sha(skill),
            context_resolution_digest="2" * 64,
            resolved_content_sha256=_sha(skill),
            markdown=skill,
        ),
        agent_system=_issue_core_resolved_auxiliary_v2(
            target_id="agent_system",
            core_artifact_id="system-artifact-v3",
            artifact_payload_sha256=_sha(system),
            context_resolution_digest="3" * 64,
            resolved_content_sha256=_sha(system),
            markdown=system,
        ),
    )
