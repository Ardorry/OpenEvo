from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
)
from openevo_chembench.temperature_full_evolve_v1.core_evolution import CoreEvolutionStateV1
from openevo_chembench.temperature_full_evolve_v1.evidence import RuleEvidenceIndexV1
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)
from openevo_chembench.temperature_full_evolve_v1.packet import BatchAggregateDiagnosticV1
from openevo_chembench.temperature_full_evolve_v1.runner import (
    TemperatureFormalRunnerError,
    TemperatureFullEvolveFormalRunnerV1,
    TemperatureRunLayoutV1,
    _batch_diagnostic_from_ledger,
    _candidate_logical_call_id,
    _require_exact_preflight_payloads,
    _stage_and_close_aggregate_input,
    _validated_admission_roots,
    _write_private_once,
)


class _Ledger:
    def __init__(self, events: list[dict[str, object]] | None = None) -> None:
        self._events = list(events or [])

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        return tuple(self._events)


class _Controller:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.calls = 0

    def append_protocol_event(self, kind: str, payload: dict[str, object]) -> None:
        self.calls += 1
        self.ledger._events.append(
            {
                "kind": kind,
                "payload": dict(payload),
                "event_sha256": hashlib.sha256(str(payload).encode()).hexdigest(),
            }
        )


def _layout(tmp_path: Path) -> TemperatureRunLayoutV1:
    return TemperatureRunLayoutV1.build(
        repository_root=tmp_path,
        controller_run_id="stv3-temperature-full-evolve-v1-20260802T010203Z",
    )


def test_layout_has_independent_evolved_baseline_roots_and_ids(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert layout.evolved_run_id != layout.baseline_run_id
    assert layout.evolved_root != layout.baseline_root
    assert layout.core_root not in {layout.evolved_root, layout.baseline_root}
    assert len(set(layout.all_run_ids)) == 4


@pytest.mark.parametrize(
    "tampered_key",
    (
        "regression_receipt",
        "historical_manifest",
        "config_manifest",
        "model_identity_receipt",
    ),
)
def test_preflight_exact_binding_rejects_tampered_payload(tampered_key: str) -> None:
    keys = {
        "preflight_report",
        "split_manifest",
        "historical_manifest",
        "grouping_manifest",
        "config_manifest",
        "model_identity_receipt",
        "runtime_identity_receipt",
        "regression_receipt",
    }
    expected = {key: {"identity": key} for key in keys}
    loaded = {key: dict(value) for key, value in expected.items()}
    loaded[tampered_key]["tampered"] = True
    with pytest.raises(
        TemperatureFormalRunnerError, match="RUNNER_PREFLIGHT_BUNDLE_DRIFT"
    ):
        _require_exact_preflight_payloads(loaded, expected)


def test_immutable_private_evidence_never_overwrites(tmp_path: Path) -> None:
    target = (tmp_path / "private/evidence.json").resolve()
    _write_private_once(target, b'{"value":1}\n')
    _write_private_once(target, b'{"value":1}\n')
    with pytest.raises(
        TemperatureFormalRunnerError, match="RUNNER_IMMUTABLE_EVIDENCE_DRIFT"
    ):
        _write_private_once(target, b'{"value":2}\n')
    assert target.read_bytes() == b'{"value":1}\n'
    assert target.stat().st_mode & 0o777 == 0o600


def test_admission_roots_are_same_owner_private_preflight_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state/chembench_temperature_full_evolve_v1/preflights"
    public = root / "preflight-12345678/public"
    split = root / "preflight-12345678/split"
    private = split / "private"
    for path, mode in ((root, 0o700), (public, 0o755), (split, 0o755), (private, 0o700)):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
    assert _validated_admission_roots(
        repository=tmp_path.resolve(),
        preflight_root=public.resolve(),
        private_split_root=split.resolve(),
    ) == (public.resolve(), split.resolve())
    with pytest.raises(
        TemperatureFormalRunnerError, match="RUNNER_ADMISSION_ROOT_OUTSIDE_STATE"
    ):
        _validated_admission_roots(
            repository=tmp_path.resolve(),
            preflight_root=public.resolve(),
            private_split_root=tmp_path.resolve(),
        )


def test_audit_staging_recovers_crash_before_event(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    aggregate = (tmp_path / "run/private/aggregate.json").resolve()
    receipt = aggregate.with_name("receipt.json")
    encoded = canonical_pretty_json_bytes({"status": "READY_FOR_AUDIT"})
    digest = hashlib.sha256(encoded).hexdigest()
    _write_private_once(aggregate, encoded)  # crash immediately after staging
    ledger = _Ledger()
    controller = _Controller(ledger)

    result = _stage_and_close_aggregate_input(
        encoded=encoded,
        aggregate_sha256=digest,
        aggregate_path=aggregate,
        receipt_path=receipt,
        completed_at_utc="2026-08-02T01:02:03Z",
        layout=layout,
        controller=controller,
        ledger=ledger,
    )

    assert result == aggregate
    assert controller.calls == 1
    assert receipt.exists()
    assert ledger.events[-1]["payload"]["aggregate_report_input_sha256"] == digest


def test_audit_staging_recovers_crash_after_event_before_receipt(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    aggregate = (tmp_path / "run/private/aggregate.json").resolve()
    receipt = aggregate.with_name("receipt.json")
    encoded = canonical_pretty_json_bytes({"status": "READY_FOR_AUDIT"})
    digest = hashlib.sha256(encoded).hexdigest()
    _write_private_once(aggregate, encoded)
    audit = {
        "kind": "AUDIT_CLOSED",
        "payload": {
            "checksums_verified": True,
            "aggregate_report_input_sha256": digest,
        },
        "event_sha256": "a" * 64,
    }
    ledger = _Ledger([audit])  # crash before runner-side receipt
    controller = _Controller(ledger)

    _stage_and_close_aggregate_input(
        encoded=encoded,
        aggregate_sha256=digest,
        aggregate_path=aggregate,
        receipt_path=receipt,
        completed_at_utc="2026-08-02T01:02:03Z",
        layout=layout,
        controller=controller,
        ledger=ledger,
    )

    assert controller.calls == 0
    assert json.loads(receipt.read_bytes())["ledger_audit_event_sha256"] == "a" * 64


def test_batch_diagnostic_recovers_from_closed_ledger_without_old_context() -> None:
    run_id = "stv3-temperature-full-evolve-v1-20260802T010203Z"
    events: list[dict[str, object]] = []
    for phase in ("train_pre", "train_post"):
        for ordinal in range(25):
            logical = _candidate_logical_call_id(
                run_id=run_id,
                phase=phase,
                batch_index=1,
                task_ordinal=ordinal,
            )
            correct = ordinal < (10 if phase == "train_pre" else 12)
            evaluation = {
                "phase": phase,
                "batch_index": 1,
                "task_ordinal": ordinal,
                "correct": correct,
                "official_parse_status": "parsed",
            }
            events.append(
                {
                    "kind": "CALL_EVALUATED",
                    "payload": {
                        "logical_call_id": logical,
                        "evaluation_json": json.dumps(
                            evaluation, sort_keys=True, separators=(",", ":")
                        ),
                    },
                }
            )

    diagnostic = _batch_diagnostic_from_ledger(
        run_id=run_id,
        batch_index=1,
        events=tuple(events),
        artifact_total_utf8_bytes=512,
    )
    assert diagnostic.pre_correct == 10
    assert diagnostic.post_correct == 12
    assert diagnostic.post_only_correct == 2
    assert diagnostic.pre_parser_success == diagnostic.post_parser_success == 25


def test_formal_cli_rejects_noncanonical_run_id_without_side_effects(tmp_path: Path) -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/temperature_full_evolve_v1/run.py"
    )
    environment = dict(os.environ)
    environment.update(
        {"OPENEVO_ALLOW_PAID_CALLS": "0", "CHEMBENCH_ALLOW_PAID_CALLS": "0"}
    )
    completed = subprocess.run(
        (
            sys.executable,
            os.fspath(script),
            "fresh",
            "--run-id",
            "invalid",
            "--preflight-root",
            os.fspath(tmp_path),
            "--private-split-root",
            os.fspath(tmp_path),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 2
    assert "--run-id must use" in completed.stderr


def test_formal_entrypoints_do_not_inject_source_import_paths() -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts/temperature_full_evolve_v1"
    for name in ("run.py", "runtime_services.py"):
        source = (scripts / name).read_text(encoding="utf-8")
        assert "sys.path.insert" not in source
        assert "PYTHONPATH" not in source
        assert "flags.isolated" in source
    precheck = (scripts / "precheck.py").read_text(encoding="utf-8")
    assert "sys.path.insert" not in precheck
    assert '"PYTHONPATH": "benchmarks/chembench/src:src"' in precheck
    assert '"OPENEVO_ALLOW_PAID_CALLS": "0"' in precheck
    assert '"CHEMBENCH_ALLOW_PAID_CALLS": "0"' in precheck
    assert "flags.isolated" in precheck


def test_formal_cli_redacts_boundary_failure(tmp_path: Path) -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/temperature_full_evolve_v1/run.py"
    )
    environment = dict(os.environ)
    environment.update(
        {"OPENEVO_ALLOW_PAID_CALLS": "0", "CHEMBENCH_ALLOW_PAID_CALLS": "0"}
    )
    completed = subprocess.run(
        (
            sys.executable,
            os.fspath(script),
            "fresh",
            "--run-id",
            "stv3-temperature-full-evolve-v1-20260802T010203Z",
            "--preflight-root",
            os.fspath(tmp_path),
            "--private-split-root",
            os.fspath(tmp_path),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 3
    assert json.loads(completed.stderr) == {
        "status": "FAIL_CLOSED",
        "finding_code": "FORMAL_RUNNER_BOUNDARY_FAILURE",
    }
    assert "Traceback" not in completed.stderr


def test_fresh_protocol_initialization_writes_run_then_split_once() -> None:
    runner = object.__new__(TemperatureFullEvolveFormalRunnerV1)
    ledger = _Ledger()
    runner._ledger = ledger
    runner._controller = _Controller(ledger)
    runner._admission = SimpleNamespace(
        split=SimpleNamespace(plan=SimpleNamespace(split_sha256="a" * 64))
    )

    runner._ensure_run_and_split_events()
    runner._ensure_run_and_split_events()

    assert [event["kind"] for event in ledger.events] == ["RUN_CREATED", "SPLIT_FROZEN"]


class _RunLedger(_Ledger):
    pass


class _RunController(_Controller):
    def __init__(self, ledger: _RunLedger, trace: list[tuple[object, ...]]) -> None:
        super().__init__(ledger)
        self.trace = trace

    def append_protocol_event(self, kind: str, payload: dict[str, object]) -> None:
        self.trace.append(("event", kind, payload.get("batch_index")))
        super().append_protocol_event(kind, payload)

    def write_status(self) -> None:
        self.trace.append(("status",))


class _State:
    def __init__(self, batch_index: int) -> None:
        self.batch_index = batch_index
        self.digest = hashlib.sha256(f"C{batch_index}".encode()).hexdigest()
        self.total_artifact_utf8_bytes = 100 * batch_index


def _diagnostic(batch_index: int) -> BatchAggregateDiagnosticV1:
    return BatchAggregateDiagnosticV1(
        batch_index=batch_index,
        pre_correct=0,
        post_correct=0,
        both_correct=0,
        pre_only_correct=0,
        post_only_correct=0,
        both_wrong=25,
        pre_parser_success=25,
        post_parser_success=25,
        accuracy_delta_percentage_points=0.0,
        mcnemar_exact_p=1.0,
        artifact_total_utf8_bytes=100 * batch_index,
    )


def _orchestration_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    committed_batch_one: bool,
) -> tuple[TemperatureFullEvolveFormalRunnerV1, list[tuple[object, ...]]]:
    import openevo_chembench.temperature_full_evolve_v1.runner as module

    trace: list[tuple[object, ...]] = []
    runner = object.__new__(TemperatureFullEvolveFormalRunnerV1)
    runner.layout = _layout(tmp_path)
    runner._resume = False
    runner._recovery_recorded = False
    train = tuple(object() for _ in range(100))
    test = tuple(object() for _ in range(100))
    runner._admission = SimpleNamespace(
        split=SimpleNamespace(
            train=train,
            test=test,
            plan=SimpleNamespace(
                train_uids=tuple(f"train-{index}" for index in range(100)),
                test_uids=tuple(f"test-{index}" for index in range(100)),
            ),
        )
    )
    initial_events: list[dict[str, object]] = []
    if committed_batch_one:
        initial_events.append(
            {
                "kind": "BATCH_ARTIFACT_SET_COMMITTED",
                "payload": {"batch_index": 1},
                "event_sha256": "1" * 64,
            }
        )
    runner._ledger = _RunLedger(initial_events)
    runner._controller = _RunController(runner._ledger, trace)
    runner._core = SimpleNamespace(head=_State(1 if committed_batch_one else 0))
    runner._forbidden_questions = ()
    runner._ensure_runtime_inventory_admission = lambda: None
    runner._ensure_core_state_checkpoint_chain = lambda: None
    runner._ensure_run_and_split_events = lambda: None
    runner._load_closed_diagnostics = list
    evidences = {
        index: RuleEvidenceIndexV1(batch_index=index, rules=())
        for index in range(5)
    }
    runner._load_evidence = lambda batch: evidences[batch]
    runner._load_batch_report = lambda _batch: {}
    runner._context_for_state = (
        lambda state: None if state.batch_index == 0 else f"context-C{state.batch_index}"
    )
    frozen_targets = [
        {
            "target_id": target,
            "core_artifact_id": f"artifact-{target}",
            "artifact_payload_sha256": hashlib.sha256(
                f"payload-{target}".encode()
            ).hexdigest(),
            "resolved_content_sha256": hashlib.sha256(
                f"resolved-{target}".encode()
            ).hexdigest(),
            "context_resolution_digest": hashlib.sha256(
                b"frozen-context"
            ).hexdigest(),
        }
        for target in ("text_memory", "skill_bundle", "agent_system")
    ]
    monkeypatch.setattr(
        module,
        "_frozen_context_target_payload",
        lambda _context: {
            "frozen_context_targets": frozen_targets,
            "frozen_context_targets_sha256": hashlib.sha256(
                canonical_json_bytes(frozen_targets)
            ).hexdigest(),
        },
    )

    def candidate(**kwargs: object) -> tuple[object, ...]:
        trace.append(
            (
                "candidate",
                kwargs["phase"],
                kwargs["batch_index"],
                kwargs["context"],
                kwargs["run_id"],
            )
        )
        return tuple(object() for _ in kwargs["tasks"])  # type: ignore[arg-type]

    runner._run_candidate_phase = candidate
    runner._build_packet = lambda **kwargs: SimpleNamespace(
        digest=hashlib.sha256(f"packet-{kwargs['batch_index']}".encode()).hexdigest(),
        batch_index=kwargs["batch_index"],
    )
    monkeypatch.setattr(
        module,
        "seal_batch_supervised_packet_v1",
        lambda packet, **_kwargs: SimpleNamespace(
            packet=SimpleNamespace(batch_index=packet.batch_index)
        ),
    )

    def reflector(sealed: object) -> object:
        batch = sealed.packet.batch_index  # type: ignore[attr-defined]
        trace.append(("reflector", batch))
        return SimpleNamespace(next_evidence=evidences[batch])

    runner._run_reflector = reflector
    monkeypatch.setattr(module, "project_artifacts", lambda *_args, **_kwargs: object())

    def core_batch(**kwargs: object) -> _State:
        batch = int(kwargs["batch_index"])
        trace.append(("core", batch))
        runner._core.head = _State(batch)
        return runner._core.head

    runner._run_core_batch = core_batch
    monkeypatch.setattr(
        module,
        "_batch_diagnostic_from_ledger",
        lambda **kwargs: _diagnostic(int(kwargs["batch_index"])),
    )

    def checkpoints(**kwargs: object) -> None:
        trace.append(("checkpoints", kwargs["batch_index"]))

    runner._write_batch_checkpoints = checkpoints
    runner._close_audit_and_write_aggregate_input = lambda **_kwargs: (
        tmp_path / "aggregate.json"
    )
    return runner, trace


def test_runner_fresh_c0_and_evolved_before_independent_empty_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, trace = _orchestration_runner(
        tmp_path, monkeypatch, committed_batch_one=False
    )
    runner.run()
    candidate = [item for item in trace if item[0] == "candidate"]
    assert candidate[0][1:4] == ("train_pre", 1, None)
    evolved_index = next(i for i, item in enumerate(candidate) if item[1] == "evolved_test")
    baseline_index = next(i for i, item in enumerate(candidate) if item[1] == "baseline_test")
    assert evolved_index < baseline_index
    assert candidate[evolved_index][3] == "context-C4"
    assert candidate[baseline_index][3] is None
    assert candidate[evolved_index][4] == runner.layout.evolved_run_id
    assert candidate[baseline_index][4] == runner.layout.baseline_run_id
    assert [item for item in trace if item[0] == "reflector"] == [
        ("reflector", 1),
        ("reflector", 2),
        ("reflector", 3),
        ("reflector", 4),
    ]


def test_runner_recovers_after_core_commit_by_starting_with_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, trace = _orchestration_runner(
        tmp_path, monkeypatch, committed_batch_one=True
    )
    runner.run()
    candidate = [item for item in trace if item[0] == "candidate"]
    assert candidate[0][1:4] == ("train_post", 1, "context-C1")
    assert ("reflector", 1) not in trace
    assert ("core", 1) not in trace


def test_runner_seals_batch_checkpoints_before_post_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, trace = _orchestration_runner(
        tmp_path, monkeypatch, committed_batch_one=False
    )
    runner.run()
    checkpoint_index = trace.index(("checkpoints", 1))
    closure_index = trace.index(("event", "BATCH_POST_CLOSED", 1))
    assert checkpoint_index < closure_index


def test_generation_zero_core_checkpoint_is_empty_and_immutable(tmp_path: Path) -> None:
    runner = object.__new__(TemperatureFullEvolveFormalRunnerV1)
    runner.layout = _layout(tmp_path)
    state = CoreEvolutionStateV1.generation_zero(full_registry_digest="f" * 64)
    runner._core = SimpleNamespace(head=state)

    runner._ensure_core_state_checkpoint_chain()
    checkpoint = runner.layout.private_root / "core_checkpoints/C0.json"
    recovered = CoreEvolutionStateV1.from_private_checkpoint_bytes(checkpoint.read_bytes())
    assert recovered.batch_index == 0
    assert recovered.target_receipts == ()
    assert recovered.committed_batch is None

    checkpoint.write_bytes(b'{"tampered":true}')
    with pytest.raises(TemperatureFormalRunnerError):
        runner._ensure_core_state_checkpoint_chain()


def test_successful_resume_is_recorded_once_with_checkpoint_binding(
    tmp_path: Path,
) -> None:
    runner = object.__new__(TemperatureFullEvolveFormalRunnerV1)
    runner.layout = _layout(tmp_path)
    runner._resume = True
    runner._recovery_recorded = False
    state = CoreEvolutionStateV1.generation_zero(full_registry_digest="f" * 64)
    runner._core = SimpleNamespace(head=state)
    runner._write_core_state_checkpoint(state)
    ledger = TemperatureExperimentLedgerV1(
        path=runner.layout.ledger_path,
        run_id=runner.layout.controller_run_id,
    )
    runner._ledger = ledger
    try:
        prior_head = ledger.head_digest
        runner._record_successful_resume()
        runner._record_successful_resume()
        recoveries = [
            event for event in ledger.events if event["kind"] == "RECOVERY_COMPLETED"
        ]
        assert len(recoveries) == 1
        assert recoveries[0]["payload"] == {
            "recovery_index": 1,
            "recovery_mode": "resume",
            "recovered_ledger_head_sha256": prior_head,
            "core_checkpoint_sha256": hashlib.sha256(
                runner._core_state_checkpoint_path(0).read_bytes()
            ).hexdigest(),
            "core_batch_index": 0,
        }
    finally:
        ledger.close()


def test_core_checkpoint_chain_rejects_nonadjacent_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = object.__new__(TemperatureFullEvolveFormalRunnerV1)
    runner.layout = _layout(tmp_path)
    for batch in (0, 1):
        path = runner.layout.private_root / f"core_checkpoints/C{batch}.json"
        _write_private_once(path, b'{"placeholder":true}\n')
    states = {
        0: SimpleNamespace(
            batch_index=0,
            digest="0" * 64,
            predecessor_state_sha256=None,
            target_receipts=(),
            committed_batch=None,
        ),
        1: SimpleNamespace(
            batch_index=1,
            digest="1" * 64,
            predecessor_state_sha256="e" * 64,
            target_receipts=(object(),),
            committed_batch=object(),
        ),
    }
    runner._core = SimpleNamespace(head=states[1])
    monkeypatch.setattr(runner, "_load_core_state_checkpoint", states.__getitem__)
    with pytest.raises(
        TemperatureFormalRunnerError, match="RUNNER_CORE_STATE_PREDECESSOR_DRIFT"
    ):
        runner._ensure_core_state_checkpoint_chain()


def test_evidence_checkpoint_must_match_immutable_core_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = object.__new__(TemperatureFullEvolveFormalRunnerV1)
    runner.layout = _layout(tmp_path)
    evidence = RuleEvidenceIndexV1(batch_index=1, rules=())
    evidence_path = runner.layout.private_root / "evidence/C1.json"
    state_path = runner.layout.private_root / "core_checkpoints/C1.json"
    _write_private_once(
        evidence_path,
        json.dumps(evidence.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(),
    )
    _write_private_once(state_path, b'{"placeholder":true}\n')
    monkeypatch.setattr(
        runner,
        "_load_core_state_checkpoint",
        lambda _batch: SimpleNamespace(
            committed_batch=SimpleNamespace(evidence_sha256="0" * 64)
        ),
    )
    with pytest.raises(
        TemperatureFormalRunnerError,
        match="RUNNER_EVIDENCE_CORE_STATE_BINDING_DRIFT",
    ):
        runner._load_evidence(1)
