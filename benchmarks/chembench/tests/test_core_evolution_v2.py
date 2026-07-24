from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile
from datetime import UTC, datetime, timedelta
import itertools
import stat

import pytest

from openevo import __version__
import openevo.evolution.methods as core_methods
import openevo.evolution.store as core_store
from openevo.evolution.framework import DistributionArtifactExpectation
from openevo.evolution.framework import builtins as core_builtins
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
    load_verified_builtin_registry,
)
from openevo.evolution.framework.loading import _verify_distribution_install
from openevo.evolution.framework.runtime import load_framework_distribution_lock

from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    PrivateChemBench4KTask,
)
from openevo_chembench.chembench4k_prompt import (
    build_all_dev_leave_one_out_tasks,
)
from openevo_chembench.core_evolution_v2 import (
    CoreArtifactValidationReceiptV2,
    CoreDevTrajectoryV2,
    CoreEvolutionV2Error,
    DATASET_REVISION,
    EXPECTED_DEV_TRAJECTORIES,
    METHOD_ID,
    OpenEvoTextMemoryLifecycleV2,
    build_maintainer_framework_bundle_v2,
    build_core_dev_trajectories_v2,
)
import openevo_chembench.core_evolution_v2 as core_evolution_v2
from openevo_chembench.frozen_runtime_v2 import CoreResolvedTextMemoryV2
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorExecutionBoundaryV2,
)


_DATASET_SHA256 = "d" * 64
_SAFE_MEMORY = """# General Chemistry Reasoning Memory

## Do
- Classify the chemistry task before comparing options.
- Check structures, reaction conditions, units, and conservation constraints.

## Avoid
- Avoid selecting an option before completing an independent verification.

## Validate
- Return exactly one uppercase choice label after the reasoning checks.

## When Applicable
- Use dimensional and stoichiometric checks for numerical questions.

## Retired Or Superseded
- No retired guidance.
"""


class _SourceDistribution:
    metadata = {"Name": "openevo"}
    version = __version__

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, _filename: str) -> None:
        return None


def _verified_builtin_registry(temp_root: Path):
    """Test-only real verifier path; production loads an external framework lock."""

    temp_root.mkdir(parents=True, exist_ok=True)
    install_root = Path(core_builtins.__file__).resolve().parents[3]
    artifact = temp_root / f"openevo-{__version__}-py3-none-any.whl"
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith(
                (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")
            ):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            f"openevo-{__version__}.dist-info/METADATA",
            f"Name: openevo\nVersion: {__version__}\n",
        )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    verified = _verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=__version__,
            distribution_digest=digest,
        ),
        artifact,
        metadata_provider=lambda _name: _SourceDistribution(install_root),
    )
    return load_verified_builtin_registry(verified)


@pytest.fixture(scope="module")
def executable_registry(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return _verified_builtin_registry(tmp_path_factory.mktemp("chembench4k-v2-core-registry"))


@pytest.fixture(autouse=True)
def deterministic_core_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = itertools.count()
    start = datetime(2090, 1, 1, tzinfo=UTC)

    def _clock() -> str:
        value = start + timedelta(seconds=next(counter))
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    monkeypatch.setattr(core_store, "utc_now_iso", _clock)
    monkeypatch.setattr(
        core_evolution_v2,
        "_wait_for_core_ingest_tick",
        lambda _previous: None,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _records() -> tuple[CoreDevTrajectoryV2, ...]:
    records: list[CoreDevTrajectoryV2] = []
    for category_index, category in enumerate(CHEMBENCH4K_CATEGORIES):
        for source_index in range(5):
            target = ("A", "B", "C", "D")[(category_index + source_index) % 4]
            prediction = (
                target
                if source_index % 2 == 0
                else ("A", "B", "C", "D")[(category_index + source_index + 1) % 4]
            )
            correct = prediction == target
            ordinal = len(records)
            records.append(
                CoreDevTrajectoryV2(
                    uid=_sha256(f"{category}:{source_index}"),
                    category=category,
                    source_index=source_index,
                    public_prompt_sha256=_sha256(f"loo-prompt:{category}:{source_index}"),
                    raw_completion=f"record-{ordinal:02d}-{prediction.lower()}",
                    parsed_prediction=prediction,
                    target=target,
                    correct=correct,
                    private_error_taxonomy=(() if correct else (f"failure_marker_{ordinal:02d}",)),
                    dataset_sha256=_DATASET_SHA256,
                )
            )
    assert len(records) == EXPECTED_DEV_TRAJECTORIES
    return tuple(records)


def _write_synthetic_reflector_codex(
    path: Path,
    *,
    item_type: str,
) -> None:
    event = json.dumps(
        {
            "type": "item.completed" if item_type == "agent_message" else "item.started",
            "item": {"type": item_type, "text": "synthetic"},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    path.write_text(
        f"""#!/bin/sh
set -eu
if [ "${{1:-}}" = "--version" ]; then
  printf '%s\\n' 'codex-cli 0.144.6'
  exit 0
fi
if [ "${{1:-}}" = "debug" ] && [ "${{2:-}}" = "prompt-input" ]; then
  printf '%s\\n' '{{}}'
  exit 0
fi
output=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
printf '%s\\n' '{event}'
cat > "$output" <<'EOF'
# General Chemistry Reasoning Memory

## Do
- Classify the chemistry task before comparing options.

## Avoid
- Avoid selecting an option before independent verification.

## Validate
- Check structures, units, and conservation constraints.

## When Applicable
- Use stoichiometric checks for numerical questions.

## Retired Or Superseded
- No retired guidance.
EOF
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _loo_tasks_and_evaluations():
    dev_by_category: dict[str, tuple[PrivateChemBench4KTask, ...]] = {}
    for category_index, category in enumerate(CHEMBENCH4K_CATEGORIES):
        tasks = tuple(
            PrivateChemBench4KTask(
                uid=_sha256(f"private:{category}:{source_index}"),
                category=category,
                source_split="dev",
                source_index=source_index,
                question=f"Private dev question {category_index}-{source_index}?",
                A="choice-a",
                B="choice-b",
                C="choice-c",
                D="choice-d",
                target=("A", "B", "C", "D")[(category_index + source_index) % 4],
                dataset_revision=DATASET_REVISION,
                dataset_sha256=_DATASET_SHA256,
            )
            for source_index in range(5)
        )
        dev_by_category[category] = tasks
    loo_tasks = build_all_dev_leave_one_out_tasks(dev_by_category)
    evaluator = ChemBench4KPrivateEvaluator()
    evaluations = tuple(
        evaluator.evaluate(
            task=loo_task.evaluation_task,
            raw_completion=(
                loo_task.evaluation_task.target
                if loo_task.evaluation_task.source_index % 2 == 0
                else "A"
            ),
        )
        for loo_task in loo_tasks
    )
    return loo_tasks, evaluations


class _AcceptingValidator:
    def validate(self, *, artifact, payload, payload_sha256, lineage):
        assert lineage["dev_record_count"] == 45
        assert artifact.promoted is False
        assert payload.decode("utf-8") == _SAFE_MEMORY
        return CoreArtifactValidationReceiptV2(
            validator_id="chembench4k_text_memory_validator_v2",
            artifact_id=artifact.artifact_id,
            artifact_payload_sha256=payload_sha256,
            passed=True,
            finding_codes=(),
        )


class _RejectingValidator:
    def validate(self, *, artifact, payload, payload_sha256, lineage):
        assert lineage["dev_record_count"] == 45
        assert payload
        return CoreArtifactValidationReceiptV2(
            validator_id="chembench4k_text_memory_validator_v2",
            artifact_id=artifact.artifact_id,
            artifact_payload_sha256=payload_sha256,
            passed=False,
            finding_codes=("leak_question_ngram",),
        )


def test_core_record_builder_consumes_exact_45_private_loo_evaluations() -> None:
    loo_tasks, evaluations = _loo_tasks_and_evaluations()

    records = build_core_dev_trajectories_v2(loo_tasks, evaluations)

    assert len(records) == 45
    assert {record.uid for record in records} == {task.evaluation_task.uid for task in loo_tasks}
    assert {record.category for record in records} == set(CHEMBENCH4K_CATEGORIES)
    assert all(record.source_split == "dev" for record in records)
    assert all(record.dataset_revision == DATASET_REVISION for record in records)
    assert all(
        record.public_prompt_sha256 == _sha256(loo_tasks[index].prompt.text)
        for index, record in enumerate(records)
    )


def test_lifecycle_rejects_unverified_registry(tmp_path: Path) -> None:
    unverified_snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo-test",
            distribution_version="1.0.0",
            distribution_digest="a" * 64,
        )
    )

    with pytest.raises(TypeError, match="verified registry loader"):
        OpenEvoTextMemoryLifecycleV2(
            db_path=tmp_path / "evolution.db",
            artifact_root=tmp_path / "artifacts",
            executable_registry=unverified_snapshot,  # type: ignore[arg-type]
        )


def test_maintainer_framework_bundle_builds_pinned_wheel_and_canonical_lock(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    output = tmp_path / "framework"

    bundle = build_maintainer_framework_bundle_v2(
        repository_root=repository_root,
        output_directory=output,
    )

    wheel_path = Path(bundle.wheel_path)
    lock_path = Path(bundle.framework_lock_path)
    assert wheel_path.parent == output
    assert lock_path == output / "framework-lock.json"
    assert hashlib.sha256(wheel_path.read_bytes()).hexdigest() == bundle.distribution_digest
    lock, locked_wheel = load_framework_distribution_lock(lock_path)
    assert locked_wheel == wheel_path
    assert lock.distribution == "openevo"
    assert lock.distribution_version == bundle.distribution_version
    assert lock.distribution_digest == bundle.distribution_digest
    encoded = lock_path.read_text(encoding="utf-8")
    assert encoded.endswith("\n")
    assert json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":")) == (
        encoded.rstrip("\n")
    )


def test_verified_registry_discovers_protected_text_memory_method(
    tmp_path: Path,
    executable_registry,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )

    evidence = lifecycle.method_evidence

    assert evidence.method_id == METHOD_ID
    assert evidence.registry_source == "openevo.evolution.framework.builtins"
    assert evidence.output_schema == ("text_memory",)
    assert evidence.input_schema_value()["bindings"][0] == {
        "binding_id": "dataset_inputs",
        "source": "explicit_inputs",
        "artifact_type": "dataset",
        "min_count": 1,
        "max_count": 128,
    }
    assert (
        executable_registry.method_handles[METHOD_ID] is core_methods.text_memory_expel_reflector
    )


def test_dev_dataset_plan_and_job_are_core_owned_and_immutable(
    tmp_path: Path,
    executable_registry,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )

    prepared = lifecycle.prepare_dev_job(
        _records(),
        configured_max_records=45,
        plan_id="chembench4k.frozen.v2.immutable",
    )

    assert prepared.record_count == 45
    assert prepared.configured_max_records == 45
    assert prepared.records_visible_to_reflector == 45
    assert prepared.reflector_input_digest == prepared.dataset_records_digest
    assert (
        prepared.ordered_loo_uid_sha256
        == hashlib.sha256(
            "\n".join(record.uid for record in _records()).encode("ascii")
        ).hexdigest()
    )
    private_reflector_input = lifecycle.private_reflector_input_path(prepared)
    assert stat.S_IMODE(private_reflector_input.stat().st_mode) == 0o600
    private_rows = [
        json.loads(line)
        for line in private_reflector_input.read_text(encoding="utf-8").splitlines()
    ]
    assert len(private_rows) == 45
    assert [row["uid"] for row in private_rows] == [record.uid for record in _records()]
    assert prepared.method.method_id == METHOD_ID
    with lifecycle._store.connect() as connection:  # noqa: SLF001
        dataset_row = connection.execute(
            "SELECT event_count, trace_count, artifact_id FROM datasets WHERE dataset_id = ?",
            (prepared.dataset_id,),
        ).fetchone()
        plan_row = connection.execute(
            "SELECT plan_digest, registry_snapshot_digest FROM evolution_plans WHERE plan_id = ?",
            (prepared.plan_id,),
        ).fetchone()
        job_row = connection.execute(
            "SELECT method, plan_id, target_id, declared_output_artifact_types_json, "
            "config_json "
            "FROM jobs WHERE job_id = ?",
            (prepared.job_id,),
        ).fetchone()
    assert tuple(dataset_row[:2]) == (45, 45)
    assert dataset_row["artifact_id"] == prepared.dataset_artifact_id
    assert plan_row["plan_digest"] == prepared.plan_digest
    assert plan_row["registry_snapshot_digest"] == prepared.method.reachable_registry_digest
    assert job_row["method"] == METHOD_ID
    assert job_row["plan_id"] == prepared.plan_id
    assert job_row["target_id"] == "text_memory"
    assert json.loads(job_row["declared_output_artifact_types_json"]) == ["text_memory"]
    assert json.loads(job_row["config_json"])["max_records"] == 45

    with pytest.raises(ValueError, match="different job request"):
        lifecycle.prepare_dev_job(
            _records(),
            configured_max_records=45,
            plan_id="chembench4k.frozen.v2.immutable",
        )


def test_lifecycle_refuses_upstream_default_twenty_record_fallback(
    tmp_path: Path,
    executable_registry,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )

    with pytest.raises(ValueError, match="explicitly equal 45"):
        lifecycle.prepare_dev_job(
            _records(),
            configured_max_records=20,
        )


def test_registered_method_requires_os_isolated_wrapper(
    tmp_path: Path,
    executable_registry,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )
    prepared = lifecycle.prepare_dev_job(
        _records(),
        configured_max_records=45,
    )

    with pytest.raises(CoreEvolutionV2Error, match="OS-isolated wrapper"):
        lifecycle.execute_registered_method(prepared)


def test_real_core_dispatch_is_intercepted_by_reflector_wrapper(
    tmp_path: Path,
    executable_registry,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )
    prepared = lifecycle.prepare_dev_job(
        _records(),
        configured_max_records=45,
        plan_id="chembench4k.frozen.v2.isolated-wrapper",
    )
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    fake_codex = tmp_path / "synthetic-codex"
    _write_synthetic_reflector_codex(
        fake_codex,
        item_type="agent_message",
    )
    temporary_parent = tmp_path / "reflector-temporary"
    temporary_parent.mkdir(mode=0o700)
    audit_root = tmp_path / "reflector-audit"
    boundary = ReflectorExecutionBoundaryV2(
        dev_artifact_path=lifecycle.private_reflector_input_path(prepared),
        expected_records_sha256=prepared.reflector_input_digest,
        private_audit_root=audit_root,
        real_codex_binary=fake_codex,
        auth_source=auth,
        temporary_parent=temporary_parent,
        timeout_seconds=30,
    )

    execution = lifecycle.execute_registered_method(
        prepared,
        reflector_boundary=boundary,
        lease_seconds=60,
    )

    assert execution.reflector_execution_receipt_sha256 is not None
    assert execution.reflector_event_stream_sha256 is not None
    receipts = tuple(audit_root.glob("*/receipt.json"))
    event_logs = tuple(audit_root.glob("*/events.jsonl"))
    assert len(receipts) == len(event_logs) == 1
    assert stat.S_IMODE(event_logs[0].stat().st_mode) == 0o600
    assert not list(temporary_parent.iterdir())


def test_reflector_tool_violation_is_terminal_and_not_retryable(
    tmp_path: Path,
    executable_registry,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )
    prepared = lifecycle.prepare_dev_job(
        _records(),
        configured_max_records=45,
        plan_id="chembench4k.frozen.v2.tool-violation",
    )
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    fake_codex = tmp_path / "synthetic-codex"
    _write_synthetic_reflector_codex(
        fake_codex,
        item_type="file_read",
    )
    temporary_parent = tmp_path / "reflector-temporary"
    temporary_parent.mkdir(mode=0o700)
    boundary = ReflectorExecutionBoundaryV2(
        dev_artifact_path=lifecycle.private_reflector_input_path(prepared),
        expected_records_sha256=prepared.reflector_input_digest,
        private_audit_root=tmp_path / "reflector-audit",
        real_codex_binary=fake_codex,
        auth_source=auth,
        temporary_parent=temporary_parent,
        timeout_seconds=30,
    )

    with pytest.raises(
        CoreEvolutionV2Error,
        match="REFLECTOR_SECURITY_TOOL_USE_VIOLATION",
    ):
        lifecycle.execute_registered_method(
            prepared,
            reflector_boundary=boundary,
            lease_seconds=60,
        )

    with lifecycle._store.connect() as connection:  # noqa: SLF001
        state = connection.execute(
            "SELECT state, attempt_count FROM jobs WHERE job_id = ?",
            (prepared.job_id,),
        ).fetchone()
    assert state["state"] == "failed"
    assert state["attempt_count"] == 1
    assert not list(temporary_parent.iterdir())


def test_verified_worker_typed_artifact_validation_promotion_and_core_context(
    tmp_path: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )
    prepared = lifecycle.prepare_dev_job(
        _records(),
        configured_max_records=45,
        plan_id="chembench4k.frozen.v2.full-core-flow",
    )
    reflector_prompts: list[str] = []

    def _dummy_reflector(prompt, *_args, **_kwargs):
        reflector_prompts.append(prompt)
        return _SAFE_MEMORY

    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        _dummy_reflector,
    )
    execution = lifecycle.execute_registered_method(
        prepared,
        lease_seconds=60,
        test_only_allow_unisolated_reflector=True,
    )
    assert len(reflector_prompts) == 1
    assert "- record_count: 45" in reflector_prompts[0]
    assert "- reflected_record_count: 45" in reflector_prompts[0]
    assert "private_evaluation" not in reflector_prompts[0]
    assert '"target"' not in reflector_prompts[0]
    assert "verifier_summary:" in reflector_prompts[0]
    assert all(record.uid not in reflector_prompts[0] for record in _records())
    assert all(record.uid[:24] not in reflector_prompts[0] for record in _records())
    assert all(record.public_prompt_sha256 not in reflector_prompts[0] for record in _records())
    expected_records = _records()
    success_markers = [record.raw_completion for record in expected_records if record.correct]
    failure_markers = [
        record.private_error_taxonomy[0] for record in expected_records if not record.correct
    ]
    assert all(marker in reflector_prompts[0] for marker in success_markers + failure_markers)
    assert [reflector_prompts[0].index(marker) for marker in success_markers] == sorted(
        reflector_prompts[0].index(marker) for marker in success_markers
    )
    assert [reflector_prompts[0].index(marker) for marker in failure_markers] == sorted(
        reflector_prompts[0].index(marker) for marker in failure_markers
    )

    artifact = lifecycle._store.get_artifact(  # noqa: SLF001
        execution.core_artifact_id
    )
    assert artifact.type.value == "text_memory"
    assert artifact.state.value == "active"
    assert artifact.promoted is False
    with lifecycle._store.connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT lineage_json FROM artifacts WHERE artifact_id = ?",
            (execution.core_artifact_id,),
        ).fetchone()
    artifact_lineage = json.loads(row["lineage_json"])
    assert artifact_lineage["protocol_id"] == "chembench4k_frozen_generalization_v2"
    assert artifact_lineage["dataset_combined_sha256"] == prepared.dataset_combined_sha256
    assert artifact_lineage["dev_record_count"] == 45
    assert artifact_lineage["dev_uid_set_sha256"] == prepared.dev_uid_set_sha256
    assert artifact_lineage["ordered_loo_uid_sha256"] == prepared.ordered_loo_uid_sha256
    assert artifact_lineage["reflector_input_digest"] == prepared.reflector_input_digest
    assert artifact_lineage["configured_max_records"] == 45
    assert artifact_lineage["records_visible_to_reflector"] == 45
    execution_lineage = artifact_lineage["openevo_execution"]
    assert execution_lineage["job_id"] == prepared.job_id
    assert execution_lineage["plan_id"] == prepared.plan_id
    assert execution_lineage["method_id"] == METHOD_ID
    assert execution_lineage["method_identity_digest"] == prepared.method.method_identity_digest

    with pytest.raises(CoreEvolutionV2Error, match="failed validation"):
        lifecycle.validate_promote_and_resolve(
            execution,
            validator=_RejectingValidator(),
        )
    assert (
        lifecycle._store.get_artifact(execution.core_artifact_id).promoted  # noqa: SLF001
        is False
    )

    frozen = lifecycle.validate_promote_and_resolve(
        execution,
        validator=_AcceptingValidator(),
    )

    assert (
        lifecycle._store.get_artifact(execution.core_artifact_id).promoted  # noqa: SLF001
        is True
    )
    assert frozen.resolved_memory == _SAFE_MEMORY
    assert frozen.execution.core_artifact_id == execution.core_artifact_id
    assert frozen.validation_receipt.passed is True
    assert frozen.audit_payload()["core_artifact_id"] == execution.core_artifact_id
    assert "resolved_memory" not in frozen.audit_payload()
    assert not any(record.uid in frozen.resolved_memory for record in _records())

    verified = lifecycle.verify_frozen_record(
        frozen,
        validator=_AcceptingValidator(),
    )
    runtime_memory = lifecycle.issue_runtime_memory(
        frozen,
        validator=_AcceptingValidator(),
    )
    assert verified["verified_from_core_state"] is True
    assert verified["job_state"] == "succeeded"
    assert verified["artifact_promoted"] is True
    assert type(runtime_memory) is CoreResolvedTextMemoryV2
    assert runtime_memory.core_artifact_id == execution.core_artifact_id
    assert runtime_memory.markdown == frozen.resolved_memory
    assert runtime_memory.context_resolution_digest == frozen.context_resolution_digest

    class _DifferentPassingValidator:
        def validate(self, *, artifact, payload, payload_sha256, lineage):
            assert lineage["dev_record_count"] == 45
            del payload
            return CoreArtifactValidationReceiptV2(
                validator_id="different_validator",
                artifact_id=artifact.artifact_id,
                artifact_payload_sha256=payload_sha256,
                passed=True,
                finding_codes=(),
            )

    with pytest.raises(CoreEvolutionV2Error, match="receipt is not valid"):
        lifecycle.verify_frozen_record(
            frozen,
            validator=_DifferentPassingValidator(),
        )

    tampered = frozen.model_copy(update={"context_resolution_digest": "f" * 64})
    with pytest.raises(CoreEvolutionV2Error, match="context identity"):
        lifecycle.verify_frozen_record(
            tampered,
            validator=_AcceptingValidator(),
        )


def test_caller_cannot_spoof_validation_binding(
    tmp_path: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = OpenEvoTextMemoryLifecycleV2(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        executable_registry=executable_registry,
    )
    prepared = lifecycle.prepare_dev_job(
        _records(),
        configured_max_records=45,
        plan_id="chembench4k.frozen.v2.validation-binding",
    )
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _SAFE_MEMORY,
    )
    execution = lifecycle.execute_registered_method(
        prepared,
        lease_seconds=60,
        test_only_allow_unisolated_reflector=True,
    )

    class _SpoofingValidator:
        def validate(self, *, artifact, payload, payload_sha256, lineage):
            assert lineage["dev_record_count"] == 45
            del artifact, payload, payload_sha256
            return CoreArtifactValidationReceiptV2(
                validator_id="chembench4k_text_memory_validator_v2",
                artifact_id="art_spoofed",
                artifact_payload_sha256="f" * 64,
                passed=True,
                finding_codes=(),
            )

    with pytest.raises(CoreEvolutionV2Error, match="not bound"):
        lifecycle.validate_promote_and_resolve(
            execution,
            validator=_SpoofingValidator(),
        )
    assert (
        lifecycle._store.get_artifact(execution.core_artifact_id).promoted  # noqa: SLF001
        is False
    )
