from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import openevo_chembench.taskwise_cli_v1 as cli
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.frozen_runtime_v2 import (
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.local_codex_executor import (
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
)
from openevo_chembench.taskwise_config_v1 import (
    TASKWISE_MEMORY_LIMITS_V1,
    load_taskwise_config_v1,
)
from openevo_chembench.taskwise_context_binding_v1 import (
    TaskwiseContextBindingReceiptV1,
    TaskwiseSessionContextBindingV1,
    issue_taskwise_context_binding_receipt_v1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    CoreMemoryReferenceV1,
    TaskwiseCoreUpdateOutcomeV1,
    TaskwiseEpisodeV1,
    TaskwiseExecutionFailureV1,
    TaskwiseMemoryPublicMetricsV1,
)
from openevo_chembench.taskwise_sampling_v1 import TaskwiseManifestSet
from openevo_chembench.taskwise_sampling_v1 import PILOT500_STREAM_SCOPES
from openevo_chembench.source_identity_v2 import SourceManifestError


_DATASET_SHA256 = "d" * 64
_SOURCE_COMMIT = "a" * 40
_GENERATION_ID = f"gen_{'1' * 64}"


def _allow_test_pilot_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "require_taskwise_pilot_authorization_v1",
        lambda: SimpleNamespace(
            paired_canary_generation_id=_GENERATION_ID,
            receipt_sha256="2" * 64,
            pilot_binding_sha256="3" * 64,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_authorized_pilot_generation_v1",
        lambda _authorization, requested: requested or _GENERATION_ID,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _episode(index: int = 0) -> TaskwiseEpisodeV1:
    uid = _sha256(f"taskwise-cli-task-{index}")
    task = PrivateChemBench4KTask(
        uid=uid,
        category="Name_Conversion",
        source_split="test",
        source_index=index,
        question=f"Public question {index}?",
        A="choice-a",
        B="choice-b",
        C="choice-c",
        D="choice-d",
        target="A",
        dataset_revision=CHEMBENCH4K_REVISION,
        dataset_sha256=_DATASET_SHA256,
    )
    return TaskwiseEpisodeV1(
        task=task,
        prompt=RenderedChemBench4KPrompt(
            uid=uid,
            category=task.category,
            dataset_revision=CHEMBENCH4K_REVISION,
            demonstration_uids=(),
            text=f"Public prompt {index}\nAnswer:",
        ),
    )


class _Executor:
    def __init__(self) -> None:
        self.requests = []
        self.closed = False
        self.context_receipts: dict[str, TaskwiseContextBindingReceiptV1] = {}

    def execute_taskwise(self, request):
        self.requests.append(request)
        binding = TaskwiseSessionContextBindingV1.from_memory(
            session_id=request.session_id,
            memory=request.resolved_text_memory,
        )
        self.context_receipts[request.session_id] = issue_taskwise_context_binding_receipt_v1(
            expected=binding,
            actual=binding,
        )
        return RawAttempt(
            response=("B" if request.round_index == 0 else "A"),
            transcript_reference=TranscriptReference(reference=f"transcript-{request.session_id}"),
        )

    def consume_taskwise_context_receipt(
        self,
        session_id: str,
    ) -> TaskwiseContextBindingReceiptV1:
        return self.context_receipts.pop(session_id)

    def close(self) -> None:
        self.closed = True


class _Core:
    def __init__(self) -> None:
        self.requests = []
        self.memories = {}

    def update_text_memory(self, request):
        self.requests.append(request)
        version = f"{request.task_index}-{request.update_index}"
        markdown = f"""# General Chemistry Memory

## Do
- Apply a general reasoning checklist, revision {version}.

## Avoid
- Avoid choosing before verifying the governing chemistry.

## Validate
- Verify units, conservation, structure, and output format.

## When Applicable
- Apply dimensional checks to numerical reasoning.

## Retired Or Superseded
- Retire a rule only when a safer general rule supersedes it.
"""
        memory = _issue_core_resolved_text_memory_v2(
            core_artifact_id=f"artifact_{request.task_index}_{request.update_index}",
            artifact_payload_sha256=_sha256(
                f"payload:{request.task_index}:{request.update_index}"
            ),
            context_resolution_digest=_sha256(
                f"context:{request.task_index}:{request.update_index}"
            ),
            resolved_memory_sha256=_sha256(markdown),
            markdown=markdown,
        )
        self.memories[CoreMemoryReferenceV1.from_memory(memory)] = memory
        return TaskwiseCoreUpdateOutcomeV1(
            job_id=f"core_job_{request.task_index}_{request.update_index}",
            job_state="COMPLETED",
            core_context_id=f"context_{request.task_index}_{request.update_index}",
            validation_receipt_sha256=_sha256(
                f"receipt:{request.task_index}:{request.update_index}"
            ),
            memory_metrics=TaskwiseMemoryPublicMetricsV1(
                memory_limits_sha256=TASKWISE_MEMORY_LIMITS_V1.digest,
                inspection_sha256=_sha256(f"inspection:{version}"),
                token_estimator_id=TASKWISE_MEMORY_LIMITS_V1.token_estimator_id,
                parser_id=TASKWISE_MEMORY_LIMITS_V1.parser_id,
                utf8_byte_count=len(markdown.encode("utf-8")),
                estimated_token_count=64,
                section_item_counts=(1, 1, 1, 1, 1),
                total_section_items=5,
                max_section_items=1,
            ),
            resolved_text_memory=memory,
        )

    def resolve_text_memory(self, reference):
        return self.memories[reference]


def _paired_configs(tmp_path: Path, *, item_count: int = 1):
    control = replace(
        load_taskwise_config_v1(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml"),
        source_commit=_SOURCE_COMMIT,
        output_directory="control",
        run_name="control_synthetic_cli",
    )
    online = replace(
        load_taskwise_config_v1(cli.CONFIG_ROOT / "online_canary9_taskwise_online_v1.yaml"),
        source_commit=_SOURCE_COMMIT,
        output_directory="online",
        run_name="online_synthetic_cli",
    )
    manifest = TaskwiseManifestSet(
        public_path=tmp_path / "public.jsonl",
        private_path=tmp_path / "private.jsonl",
        summary_path=tmp_path / "summary.json",
        public_sha256="1" * 64,
        private_sha256="2" * 64,
        summary_sha256="3" * 64,
        ordered_uid_sha256="4" * 64,
        item_count=item_count,
    )
    loader = SimpleNamespace(manifest=SimpleNamespace(combined_sha256=_DATASET_SHA256))
    return control, online, loader, manifest


def test_dirty_uncommitted_source_rejected_before_paid_objects() -> None:
    calls = {"executor": 0, "core": 0}

    def executor_factory(_config):
        calls["executor"] += 1
        raise AssertionError("executor must not be instantiated")

    def core_factory(_config):
        calls["core"] += 1
        raise AssertionError("Core must not be instantiated")

    def dirty_source_gate(_config):
        raise cli.TaskwiseCLIError("TASKWISE_PACKAGE_SOURCE_DIRTY")

    with pytest.raises(cli.TaskwiseCLIError, match="TASKWISE_PACKAGE_SOURCE_DIRTY"):
        cli.run_arm(
            cli.CONFIG_ROOT / "online_canary9_taskwise_online_v1.yaml",
            executor_factory=executor_factory,
            core_port_factory=core_factory,
            source_gate=dirty_source_gate,
        )
    assert calls == {"executor": 0, "core": 0}


def test_source_gate_recomputes_the_source_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_taskwise_config_v1(cli.CONFIG_ROOT / "online_canary9_taskwise_online_v1.yaml")

    def fake_git(*arguments: str) -> str:
        return "a" * 40 + "\n" if arguments == ("rev-parse", "HEAD") else ""

    monkeypatch.setattr(cli, "_git", fake_git)
    monkeypatch.setattr(
        cli,
        "verify_source_manifest",
        lambda *_args: (_ for _ in ()).throw(SourceManifestError("drift")),
    )

    with pytest.raises(cli.TaskwiseCLIError, match="TASKWISE_SOURCE_MANIFEST_MISMATCH"):
        cli.verify_taskwise_source_gate_v1(config)


def test_no_model_codex_policy_probe_accepts_the_exact_01446_policy() -> None:
    config = load_taskwise_config_v1(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml")
    command = cli._codex_command(
        executable=Path("/opt/codex"),
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        workspace=Path("/empty-work"),
        output_last_message=Path("/private/last-message"),
    )
    calls: list[tuple[str, ...]] = []

    def fake_probe(
        arguments,
        _cwd: Path,
        _environment,
        _timeout_seconds: float,
    ) -> tuple[int, str]:
        received = tuple(arguments)
        calls.append(received)
        if received[1:] == ("--version",):
            return 0, "codex-cli 0.144.6\n"
        assert "exec" not in received
        assert "--model" not in received
        assert received[1:3] == ("debug", "prompt-input")
        return 0, "discarded parser output"

    receipt = cli.verify_taskwise_codex_policy_v1(
        config,
        command=command,
        probe_runner=fake_probe,
    )

    assert receipt["status"] == "PASS"
    assert receipt["finding_codes"] == []
    assert receipt["model_calls"] == 0
    assert receipt["stderr_included"] is False
    assert "agents.enabled" not in receipt["config_keys"]
    assert len(calls) == 2


def test_no_model_codex_policy_probe_rejects_agents_enabled_without_stderr() -> None:
    config = load_taskwise_config_v1(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml")
    command = cli._codex_command(
        executable=Path("/opt/codex"),
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        workspace=Path("/empty-work"),
        output_last_message=Path("/private/last-message"),
    )
    insertion = command.index("--output-last-message")
    invalid_command = (
        command[:insertion] + ("--config", "agents.enabled=false") + command[insertion:]
    )

    def fake_probe(
        arguments,
        _cwd: Path,
        _environment,
        _timeout_seconds: float,
    ) -> tuple[int, str]:
        received = tuple(arguments)
        if received[1:] == ("--version",):
            return 0, "codex-cli 0.144.6\n"
        assert "exec" not in received
        return 1, "PRIVATE STDERR MUST NOT SURFACE"

    receipt = cli.inspect_taskwise_codex_policy_v1(
        config,
        command=invalid_command,
        probe_runner=fake_probe,
    )

    assert receipt["status"] == "BLOCKED"
    assert "EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN" in receipt["finding_codes"]
    assert "EXECUTOR_CONFIG_POLICY_PROBE_REJECTED" in receipt["finding_codes"]
    serialized = json.dumps(receipt, sort_keys=True)
    assert "stderr" not in serialized.casefold() or receipt["stderr_included"] is False
    assert "PRIVATE STDERR" not in serialized
    with pytest.raises(
        cli.TaskwiseCLIError,
        match="EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN",
    ):
        cli.verify_taskwise_codex_policy_v1(
            config,
            command=invalid_command,
            probe_runner=fake_probe,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_output", "EXECUTOR_CONFIG_POLICY_COMMAND_INVALID"),
        ("duplicate_output", "EXECUTOR_CONFIG_POLICY_COMMAND_INVALID"),
        ("extra_flag", "EXECUTOR_CONFIG_POLICY_COMMAND_INVALID"),
        ("extra_config", "EXECUTOR_CONFIG_POLICY_CONFIG_MISMATCH"),
        ("extra_feature", "EXECUTOR_CONFIG_POLICY_FEATURES_MISMATCH"),
    ],
)
def test_codex_policy_is_an_exact_command_attestation(
    mutation: str,
    expected_code: str,
) -> None:
    config = load_taskwise_config_v1(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml")
    command = cli._codex_command(
        executable=Path("/opt/codex"),
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        workspace=Path("/empty-work"),
        output_last_message=Path("/private/last-message"),
    )
    output_index = command.index("--output-last-message")
    if mutation == "missing_output":
        mutated = command[:output_index] + command[output_index + 2 :]
    elif mutation == "duplicate_output":
        mutated = (
            command[:output_index]
            + ("--output-last-message", "/private/other-message")
            + command[output_index:]
        )
    elif mutation == "extra_flag":
        mutated = command[:-1] + ("--oss", command[-1])
    elif mutation == "extra_config":
        mutated = (
            command[:output_index] + ("--config", 'model_verbosity="low"') + command[output_index:]
        )
    else:
        mutated = (
            command[:output_index] + ("--disable", "experimental_feature") + command[output_index:]
        )

    receipt = cli.inspect_taskwise_codex_policy_v1(
        config,
        command=mutated,
        probe_runner=lambda arguments, *_args: (
            (0, "codex-cli 0.144.6\n") if tuple(arguments)[1:] == ("--version",) else (0, "")
        ),
    )

    assert receipt["status"] == "BLOCKED"
    assert expected_code in receipt["finding_codes"]


def test_closed_executor_adapter_preserves_sanitized_failure_metadata() -> None:
    class FailingExecutor:
        def execute_taskwise(self, _request):
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.CLI_FAILED,
                diagnostic_receipt=f"receipt_{'a' * 32}.json",
                taskwise_failure_code="EXECUTOR_NONZERO_EXIT",
                executor_stage="CODEX_STARTUP",
                completion_observed=False,
            )

        def consume_taskwise_context_receipt(self, _session_id: str):
            raise AssertionError("failed invocation has no context receipt")

        def close(self) -> None:
            pass

    adapter = cli._ClosedExecutorFailureAdapterV1(FailingExecutor())
    with pytest.raises(TaskwiseExecutionFailureV1) as captured:
        adapter.execute_taskwise(object())

    failure = captured.value
    assert failure.code == "EXECUTOR_NONZERO_EXIT"
    assert failure.completion_observed is False
    assert failure.resume_allowed is False
    assert failure.diagnostic_receipt == f"receipt_{'a' * 32}.json"
    assert failure.executor_stage == "CODEX_STARTUP"
    assert "stderr" not in str(failure).casefold()


def test_default_executor_diagnostics_are_namespaced_by_run_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_taskwise_config_v1(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml")
    captured: dict[str, object] = {}

    def fake_executor(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "LocalCodexCLIExecutor", fake_executor)
    cli.build_default_taskwise_executor_v1(config)

    diagnostic_root = Path(captured["diagnostic_root"])
    assert diagnostic_root.parts[-3:] == (
        config.scope,
        config.run_name,
        config.arm,
    )


def test_dry_run_recomputes_manifest_binding_without_paid_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "build_default_taskwise_executor_v1",
        lambda _config: pytest.fail("dry-run instantiated executor"),
    )
    monkeypatch.setattr(
        cli,
        "build_default_taskwise_core_port_v1",
        lambda _config: pytest.fail("dry-run instantiated Core"),
    )

    policy_calls = 0

    def policy_gate(_config):
        nonlocal policy_calls
        policy_calls += 1
        return {
            "schema_version": "taskwise_codex_policy_preflight_v1",
            "status": "PASS",
            "finding_codes": [],
            "model_calls": 0,
            "stderr_included": False,
        }

    receipt = cli.dry_run(executor_policy_gate=policy_gate)

    assert receipt["status"] == "PASS"
    assert policy_calls == 1
    assert receipt["model_calls"] == 0
    assert receipt["executor_instantiated"] is False
    assert receipt["core_port_instantiated"] is False
    assert receipt["codex_policy_preflight"]["status"] == "PASS"
    assert receipt["scopes"]["canary9"]["item_count"] == 9
    assert all(receipt["scopes"][scope]["item_count"] == 50 for scope in PILOT500_STREAM_SCOPES)
    assert receipt["pilot500_stream_design"] == {
        "stream_count": 10,
        "tasks_per_stream": 50,
        "total_item_count": 500,
        "generation_zero_reset_between_streams": True,
        "legacy_single_chain_scope": "pilot500",
    }


def test_cli_main_projects_arbitrary_exception_to_closed_public_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "run_arm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private path /secret and stderr payload")
        ),
    )

    status = cli.main(
        [
            "run-arm",
            "--config",
            os.fspath(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml"),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert status == 2
    assert payload == {
        "error_type": "TASKWISE_CLI_INTERNAL_ERROR",
        "model_calls": 0,
        "status": "BLOCKED",
    }
    assert "secret" not in captured.err
    assert "stderr payload" not in captured.err


def test_manifest_is_bound_to_config_scope_and_path() -> None:
    path = cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml"
    config = load_taskwise_config_v1(path)
    loader, manifest, paired = cli._verify_static_inputs(config, path)

    assert manifest.item_count == 9
    assert (
        manifest.public_sha256
        == hashlib.sha256(
            cli._resolve_workspace_path(
                config.task_manifest,
                field_name="task manifest",
                must_exist=True,
            ).read_bytes()
        ).hexdigest()
    )
    assert (
        loader.manifest.combined_sha256
        == "cb6c17c54d4c0cf103b38f12e9ce05663515b05bfdc342d83e3245d39f3a4b3a"
    )
    assert paired["control"].task_manifest == paired["online"].task_manifest

    wrong_scope = replace(
        config,
        task_manifest=paired["control"].task_manifest.replace(
            "canary9",
            "pilot500",
        ),
    )
    with pytest.raises(cli.TaskwiseCLIError, match="CONFIG_PATH_BINDING"):
        cli._verify_static_inputs(wrong_scope, path)


def test_canary_fix7_uses_fresh_paired_run_namespaces() -> None:
    control = load_taskwise_config_v1(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml")
    online = load_taskwise_config_v1(cli.CONFIG_ROOT / "online_canary9_taskwise_online_v1.yaml")

    assert control.run_name == "control_canary9_repeated_session_v1_fix8"
    assert online.run_name == "online_canary9_taskwise_evolution_v1_fix8"
    assert control.output_directory.endswith("/canary9/control_fix8")
    assert online.output_directory.endswith("/canary9/online_fix8")
    assert "fix1" not in control.output_directory
    assert "fix1" not in online.output_directory


def test_control_online_dispatch_and_private_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, online, loader, manifest = _paired_configs(tmp_path, item_count=9)
    paired = {"control": control, "online": online}
    executors: dict[str, _Executor] = {}
    core = _Core()

    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(
        cli,
        "load_taskwise_config_v1",
        lambda path: online if path.name.startswith("online_") else control,
    )
    monkeypatch.setattr(
        cli,
        "_verify_static_inputs",
        lambda _config, _path: (loader, manifest, paired),
    )
    monkeypatch.setattr(
        cli,
        "_build_episodes",
        lambda _loader, scope: tuple(_episode(index) for index in range(9)),
    )
    monkeypatch.setattr(
        cli,
        "current_taskwise_canary_generation_id_v1",
        lambda _inputs: _GENERATION_ID,
    )

    def executor_factory(config):
        executor = _Executor()
        executors[config.arm] = executor
        return executor

    for config, path in (
        (control, tmp_path / "control_canary9_taskwise_online_v1.yaml"),
        (online, tmp_path / "online_canary9_taskwise_online_v1.yaml"),
    ):
        receipt = cli.run_arm(
            path,
            executor_factory=executor_factory,
            core_port_factory=lambda _config: core,
            source_gate=lambda _config: _SOURCE_COMMIT,
        )
        assert receipt["status"] == "COMPLETED"
        assert receipt["completion_count"] == 27
        assert receipt["update_count"] == (0 if config.arm == "control" else 18)
        assert receipt["core_job_count"] == (0 if config.arm == "control" else 18)
        assert receipt["core_artifact_count"] == (0 if config.arm == "control" else 18)

    assert len(executors["control"].requests) == 27
    assert all(request.resolved_text_memory is None for request in executors["control"].requests)
    assert len(executors["online"].requests) == 27
    assert len(core.requests) == 18
    assert executors["control"].closed is True
    assert executors["online"].closed is True

    report = cli.compare(
        scope="canary9",
        persist=False,
        source_gate=lambda _config: _SOURCE_COMMIT,
    )
    assert report["task_count"] == 9
    assert report["protocol_id"] == "taskwise_online_evolution_v1"
    assert report["paired_final_round"]["comparison"] == ("online_round_2_vs_control_round_2")
    assert report["canary9_gate"]["passed"] is True
    assert report["canary9_gate"]["performance_gate_applied"] is False
    assert report["canary9_gate"]["performance_tuning_permitted"] is False
    assert report["canary9_gate"]["labels"] == [
        "NON_PERFORMANCE_SAFETY_CANARY",
        "MECHANISM_AND_SECURITY_VALIDATION_ONLY",
        "DO_NOT_USE_FOR_ARTIFACT_OR_PROMPT_TUNING",
    ]
    assert report["canary_labels"] == report["canary9_gate"]["labels"]
    assert report["canary9_gate"]["observed"] == {
        "control_completion_count": 27,
        "online_completion_count": 27,
        "control_core_job_count": 0,
        "control_core_artifact_count": 0,
        "online_core_job_count": 18,
        "online_core_artifact_count": 18,
        "control_context_binding_violations": 0,
        "online_context_binding_violations": 0,
        "control_security_violations": 0,
        "online_security_violations": 0,
        "online_artifact_validation_failures": 0,
    }
    safe_payload = cli._safe_cli_payload("compare", report)
    assert safe_payload["canary9_gate"] == {
        "labels": report["canary_labels"],
        "passed": True,
        "finding_codes": [],
    }

    for directory in (tmp_path / "control", tmp_path / "online"):
        state_path = directory / "run_state.json"
        state = json.loads(state_path.read_bytes())
        state["binding"]["model_identity_sha256"] = "f" * 64
        state_path.write_bytes(cli._canonical_bytes(state))
    with pytest.raises(cli.TaskwiseCLIError, match="PAIRED_RUN_BINDING_MISMATCH"):
        cli.compare(
            scope="canary9",
            persist=False,
            source_gate=lambda _config: _SOURCE_COMMIT,
        )


def test_stream_public_evidence_binds_context_injection_and_memory_metrics(
    tmp_path: Path,
) -> None:
    output = tmp_path / "online"
    public = output / "public"
    public.mkdir(parents=True)
    rows: list[dict[str, object]] = []
    prior: dict[str, str] | None = None
    byte_counts: list[int] = []
    for task_index in range(50):
        rows.append(
            {
                "kind": "completion",
                "arm": "online",
                "task_ordinal": task_index,
                "round_index": 0,
                "memory": prior,
            }
        )
        for update_index in (1, 2):
            context = _sha256(f"context-{task_index}-{update_index}")
            memory = {
                "core_artifact_id": f"artifact_{task_index}_{update_index}",
                "artifact_payload_sha256": _sha256(f"payload-{task_index}-{update_index}"),
                "context_resolution_digest": context,
                "resolved_memory_sha256": _sha256(f"memory-{task_index}-{update_index}"),
            }
            byte_count = 100 + task_index + update_index
            byte_counts.append(byte_count)
            metrics = TaskwiseMemoryPublicMetricsV1(
                memory_limits_sha256=TASKWISE_MEMORY_LIMITS_V1.digest,
                inspection_sha256=_sha256(f"inspection-{task_index}-{update_index}"),
                token_estimator_id=TASKWISE_MEMORY_LIMITS_V1.token_estimator_id,
                parser_id=TASKWISE_MEMORY_LIMITS_V1.parser_id,
                utf8_byte_count=byte_count,
                estimated_token_count=50,
                section_item_counts=(1, 1, 1, 1, 1),
                total_section_items=5,
                max_section_items=1,
            )
            rows.append(
                {
                    "kind": "core_update",
                    "arm": "online",
                    "task_ordinal": task_index,
                    "update_index": update_index,
                    "core_job_state": "COMPLETED",
                    "context_resolution_digest": context,
                    "output_memory": memory,
                    "memory_metrics": metrics.to_dict(),
                }
            )
            rows.append(
                {
                    "kind": "completion",
                    "arm": "online",
                    "task_ordinal": task_index,
                    "round_index": update_index,
                    "memory": memory,
                }
            )
            prior = memory
    (public / "events.jsonl").write_bytes(b"".join(cli._canonical_bytes(row) for row in rows))
    state = {
        "memory_aggregate": {
            "approved_artifact_count": 100,
            "max_utf8_byte_count": max(byte_counts),
        }
    }

    observed, findings = cli._stream_public_evidence(
        output,
        expected_arm="online",
        state=state,
    )
    assert observed == tuple(byte_counts)
    assert findings == 0

    rows[1]["context_resolution_digest"] = "f" * 64
    (public / "events.jsonl").write_bytes(b"".join(cli._canonical_bytes(row) for row in rows))
    _observed, findings = cli._stream_public_evidence(
        output,
        expected_arm="online",
        state=state,
    )
    assert findings == 1


def test_each_stream_constructs_a_fresh_scope_specific_core_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework_lock = tmp_path / "framework-lock.json"
    framework_lock.write_text("{}\n", encoding="utf-8")
    captured: list[dict[str, object]] = []

    def fake_builder(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(cli, "FRAMEWORK_LOCK", framework_lock)
    monkeypatch.setattr(cli, "TASKWISE_STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "build_taskwise_core_port_at_roots_v1", fake_builder)

    for index in (0, 1):
        scope = f"pilot500_stream_{index:02d}"
        config = replace(
            load_taskwise_config_v1(cli.CONFIG_ROOT / f"online_{scope}_taskwise_online_v1.yaml"),
            output_directory=f"results/{scope}",
        )
        cli.build_default_taskwise_core_port_v1(config)

    assert len(captured) == 2
    roots = [Path(item["state_root"]) for item in captured]
    assert roots[0] != roots[1]
    assert roots[0].parts[-2:] == (
        "pilot500_stream_00",
        "online_pilot500_stream_00_taskwise_evolution_v1",
    )
    assert roots[1].parts[-2:] == (
        "pilot500_stream_01",
        "online_pilot500_stream_01_taskwise_evolution_v1",
    )
    assert all(item["framework_lock"] == framework_lock for item in captured)
    assert all(not (root / "private_lineage_checkpoints.jsonl").exists() for root in roots)


def _write_public_run_state(
    output: Path,
    *,
    arm: str,
    status: str,
    resume_allowed: bool = False,
    pending_invocation: object = None,
    failure_code: str | None = None,
    context_binding_violations: int = 0,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "taskwise_online_run_state_v1",
        "arm": arm,
        "status": status,
        "standard_chembench4k_score_claimed": False,
        "resume_allowed": resume_allowed,
        "pending_invocation": pending_invocation,
        "failure": (
            None
            if failure_code is None
            else {
                "code": failure_code,
                "completion_observed": not resume_allowed,
            }
        ),
        "context_binding_violation_count": context_binding_violations,
    }
    (output / "run_state.json").write_bytes(cli._canonical_bytes(payload))


def test_suite_orchestrator_rejects_existing_stream_without_resume_or_stitch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_verify_static_inputs", lambda *_args: None)
    _allow_test_pilot_generation(monkeypatch)
    suite_state = tmp_path / "suite" / "control.json"
    calls: list[tuple[str, bool, str | None]] = []

    template = load_taskwise_config_v1(
        cli.CONFIG_ROOT / "control_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    completed_config = cli.taskwise_pilot_runtime_config_v1(template, _GENERATION_ID)
    _write_public_run_state(
        tmp_path / completed_config.output_directory,
        arm="control",
        status="COMPLETED",
    )

    def fake_runner(
        config_path: Path,
        *,
        resume: bool,
        generation_id: str | None,
    ) -> dict[str, object]:
        calls.append((config_path.name, resume, generation_id))
        pytest.fail("an existing stream must stop the whole fresh generation")

    with pytest.raises(cli.TaskwiseCLIError, match="SUITE_TERMINAL_FAILURE"):
        cli.run_pilot500_stream_suite(
            arm="control",
            arm_runner=fake_runner,
            suite_state_path=suite_state,
            source_gate=lambda _config: _SOURCE_COMMIT,
        )

    assert calls == []
    state = json.loads(suite_state.read_text(encoding="utf-8"))
    assert state["status"] == "FAILED"
    assert state["generation_id"] == _GENERATION_ID
    assert state["streams"]["pilot500_stream_00"]["action"] == "TERMINAL_FAILURE"


def test_direct_pilot_run_arm_cannot_bypass_paired_attempt_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_test_pilot_generation(monkeypatch)
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    control_path = cli.CONFIG_ROOT / "control_pilot500_stream_00_taskwise_online_v1.yaml"
    paired = {
        arm: load_taskwise_config_v1(
            cli.CONFIG_ROOT / f"{arm}_pilot500_stream_00_taskwise_online_v1.yaml"
        )
        for arm in ("control", "online")
    }
    monkeypatch.setattr(
        cli,
        "_verify_static_inputs",
        lambda *_args: (SimpleNamespace(), SimpleNamespace(), paired),
    )

    with pytest.raises(cli.TaskwiseCLIError, match="ATTEMPT_AUTHORITY_INVALID"):
        cli.run_arm(
            control_path,
            generation_id=_GENERATION_ID,
            source_gate=lambda _config: _SOURCE_COMMIT,
            executor_factory=lambda _config: pytest.fail(
                "executor constructed without paired attempt authority"
            ),
        )


@pytest.mark.parametrize(
    ("run_status", "failure_code", "counter_name"),
    [
        (
            "SECURITY_TOOL_USE_VIOLATION",
            "SECURITY_TOOL_USE_VIOLATION",
            "security_violations",
        ),
        (
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            "evolution_update_failures",
        ),
        (
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
            "context_binding_violations",
        ),
    ],
)
def test_suite_orchestrator_stops_entire_suite_on_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
    failure_code: str,
    counter_name: str,
) -> None:
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_verify_static_inputs", lambda *_args: None)
    _allow_test_pilot_generation(monkeypatch)
    suite_state = tmp_path / "suite" / "online.json"
    template = load_taskwise_config_v1(
        cli.CONFIG_ROOT / "online_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    config = cli.taskwise_pilot_runtime_config_v1(template, _GENERATION_ID)
    _write_public_run_state(
        tmp_path / config.output_directory,
        arm="online",
        status=run_status,
        failure_code=failure_code,
        context_binding_violations=int(run_status == "TASKWISE_CONTEXT_BINDING_VIOLATION"),
    )

    with pytest.raises(cli.TaskwiseCLIError, match="SUITE_TERMINAL_FAILURE"):
        cli.run_pilot500_stream_suite(
            arm="online",
            arm_runner=lambda *_args, **_kwargs: pytest.fail(
                "terminal suite invoked another stream"
            ),
            suite_state_path=suite_state,
            source_gate=lambda _config: _SOURCE_COMMIT,
        )

    state = json.loads(suite_state.read_text(encoding="utf-8"))
    assert state["status"] == "FAILED"
    assert state[counter_name] == 1
    assert state["completed_streams"] == 0
    assert state["infrastructure_failures"] == 0


@pytest.mark.parametrize(
    ("status", "failure_code", "expected_class"),
    [
        ("EXECUTION_FAILED", "EXECUTOR_NONZERO_EXIT", "infrastructure"),
        (
            "EXECUTION_FAILED",
            "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
            "security",
        ),
        (
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            "EXECUTOR_NONZERO_EXIT",
            "evolution_update",
        ),
        (
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
            "EXECUTOR_NONZERO_EXIT",
            "context",
        ),
    ],
)
def test_terminal_failure_class_precedes_executor_infrastructure_class(
    status: str,
    failure_code: str,
    expected_class: str,
) -> None:
    assert (
        cli._stream_failure_class(
            {
                "run_status": status,
                "failure_code": failure_code,
            }
        )
        == expected_class
    )


def test_legacy_unclassified_executor_failure_is_closed_at_suite_boundary() -> None:
    assert (
        cli._closed_suite_failure_code(
            "EXECUTION_FAILED",
            "UNCLASSIFIED_EXECUTOR_FAILURE",
        )
        == "EXECUTOR_INTERNAL_ERROR"
    )


def test_suite_records_config_policy_failure_without_arbitrary_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_verify_static_inputs", lambda *_args: None)
    _allow_test_pilot_generation(monkeypatch)
    suite_state = tmp_path / "suite" / "control.json"

    def blocked_runner(
        _config_path: Path,
        *,
        resume: bool,
        generation_id: str | None,
    ) -> dict[str, object]:
        assert resume is False
        assert generation_id == _GENERATION_ID
        raise cli.TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN")

    with pytest.raises(
        cli.TaskwiseCLIError,
        match="EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN",
    ):
        cli.run_pilot500_stream_suite(
            arm="control",
            arm_runner=blocked_runner,
            suite_state_path=suite_state,
            source_gate=lambda _config: _SOURCE_COMMIT,
        )

    state = json.loads(suite_state.read_text(encoding="utf-8"))
    stream = state["streams"]["pilot500_stream_00"]
    assert stream["failure_code"] == ("EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN")
    assert state["executor_failures"] == 1
    assert state["executor_failure_codes"] == {
        "EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN": 1
    }
    assert state["infrastructure_failures"] == 1
    assert "stderr" not in json.dumps(state).casefold()


def test_suite_records_infrastructure_failure_but_never_resumes_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_verify_static_inputs", lambda *_args: None)
    _allow_test_pilot_generation(monkeypatch)
    calls = 0

    def fake_runner(
        _config_path: Path,
        *,
        resume: bool,
        generation_id: str | None,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert resume is False
        assert generation_id == _GENERATION_ID
        return {
            "status": "EXECUTION_FAILED",
            "resume_allowed": True,
            "finding_codes": ["INFRASTRUCTURE_TRANSPORT_FAILURE"],
        }

    suite_path = tmp_path / "suite" / "control.json"
    with pytest.raises(cli.TaskwiseCLIError, match="SUITE_TERMINAL_FAILURE"):
        cli.run_pilot500_stream_suite(
            arm="control",
            arm_runner=fake_runner,
            suite_state_path=suite_path,
            source_gate=lambda _config: _SOURCE_COMMIT,
        )

    result = json.loads(suite_path.read_text(encoding="utf-8"))
    assert calls == 1
    assert result["status"] == "FAILED"
    assert result["infrastructure_failures"] == 1
    assert result["executor_failures"] == 1
    assert result["executor_failure_codes"] == {"EXECUTOR_MODEL_TRANSPORT_FAILED": 1}
    assert (
        result["streams"]["pilot500_stream_00"]["failure_code"]
        == "EXECUTOR_MODEL_TRANSPORT_FAILED"
    )
    assert result["streams"]["pilot500_stream_01"]["action"] == "NOT_STARTED"
    with pytest.raises(cli.TaskwiseCLIError, match="SUITE_STATE_EXISTS"):
        cli.run_pilot500_stream_suite(
            arm="control",
            arm_runner=fake_runner,
            suite_state_path=suite_path,
            source_gate=lambda _config: _SOURCE_COMMIT,
        )
    assert calls == 1


def test_incomplete_stream_comparison_uses_only_public_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_verify_static_inputs", lambda *_args: None)
    _allow_test_pilot_generation(monkeypatch)
    monkeypatch.setattr(
        cli,
        "load_private_taskwise_results_v1",
        lambda *_args: pytest.fail("incomplete comparison read private results"),
    )

    report = cli.compare_pilot500_streams(
        persist=False,
        source_gate=lambda _config: _SOURCE_COMMIT,
    )

    assert report["status"] == "INCOMPLETE"
    assert report["decision"] == "NO_GO"
    assert report["completed_streams"] == 0
    assert report["infrastructure_failures"] == 20
    assert report["executor_failures"] == 0
    assert report["executor_failure_codes"] == {}
    assert report["private_results_read"] is False


def test_failed_stream_comparison_reports_all_public_failure_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_verify_static_inputs", lambda *_args: None)
    _allow_test_pilot_generation(monkeypatch)
    monkeypatch.setattr(
        cli,
        "load_private_taskwise_results_v1",
        lambda *_args: pytest.fail("failed comparison read private results"),
    )
    failures = (
        (
            0,
            "SECURITY_TOOL_USE_VIOLATION",
            "SECURITY_TOOL_USE_VIOLATION",
            0,
        ),
        (
            1,
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            0,
        ),
        (
            2,
            "EXECUTION_FAILED",
            "TASKWISE_ARTIFACT_VALIDATION_FAILED",
            0,
        ),
        (
            3,
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
            1,
        ),
        (
            4,
            "EXECUTION_FAILED",
            "EXECUTOR_NONZERO_EXIT",
            0,
        ),
    )
    for stream_index, status, failure_code, context_count in failures:
        template = load_taskwise_config_v1(
            cli.CONFIG_ROOT / f"online_pilot500_stream_{stream_index:02d}_taskwise_online_v1.yaml"
        )
        config = cli.taskwise_pilot_runtime_config_v1(template, _GENERATION_ID)
        _write_public_run_state(
            tmp_path / config.output_directory,
            arm="online",
            status=status,
            failure_code=failure_code,
            context_binding_violations=context_count,
        )

    report = cli.compare_pilot500_streams(
        persist=False,
        source_gate=lambda _config: _SOURCE_COMMIT,
    )

    assert report["status"] == "FAILED"
    assert report["decision"] == "NO_GO"
    assert report["security_violations"] == 1
    assert report["evolution_update_failures"] == 1
    assert report["artifact_validation_failures"] == 1
    assert report["context_binding_violations"] == 1
    assert report["infrastructure_failures"] == 16
    assert report["executor_failures"] == 1
    assert report["executor_failure_codes"] == {"EXECUTOR_NONZERO_EXIT": 1}
    assert report["private_results_read"] is False
