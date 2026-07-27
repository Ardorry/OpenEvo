from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
)
from openevo_chembench.reflector_execution_boundary_v2 import (
    _normalize_supervised_memory_sections,
)
from openevo_chembench.supervised_transfer_v1 import (
    source_manifest as source_manifest_module,
)
from openevo_chembench.supervised_transfer_v1.common import canonical_pretty_json_bytes
from openevo_chembench.supervised_transfer_v1.config import (
    TOTAL_MODEL_CALLS,
    load_supervised_transfer_config_v1,
)
from openevo_chembench.supervised_transfer_v1.desktop_export import (
    export_desktop_audit_v1,
)
from openevo_chembench.supervised_transfer_v1.exposure import (
    HistoricalExposureManifestV1,
    load_historical_exposure_manifest,
)
from openevo_chembench.supervised_transfer_v1.memory import (
    inspect_supervised_category_memory_v1,
)
from openevo_chembench.supervised_transfer_v1.packet import (
    PACKET_INPUT_SCHEMA_DIGEST,
    REFLECTOR_PROMPT_DIGEST,
    SupervisedEvolutionPacketV1,
)
from openevo_chembench.supervised_transfer_v1.reporting import (
    _mcnemar_exact,
    _write_charts,
)
from openevo_chembench.supervised_transfer_v1.source_manifest import (
    render_source_import_manifest_v1,
)
from openevo_chembench.supervised_transfer_v1.split import (
    SupervisedSplitError,
    build_supervised_dataset_manifest,
    generate_balanced_split_v1,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_ROOT = (
    WORKSPACE_ROOT
    / "data"
    / "chembench4k"
    / "AI4Chem_ChemBench4K"
    / CHEMBENCH4K_REVISION
)
MANIFEST_ROOT = (
    WORKSPACE_ROOT / "benchmarks" / "chembench" / "manifests" / "supervised_transfer_v1"
)


def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(
        snapshot_root=SNAPSHOT_ROOT,
        manifest_path=SNAPSHOT_ROOT / "chembench4k_dataset_manifest_v2.json",
    )


def test_config_freezes_roots_model_and_exact_call_budget() -> None:
    config = load_supervised_transfer_config_v1(
        WORKSPACE_ROOT
        / "benchmarks"
        / "chembench"
        / "configs"
        / "chembench_supervised_transfer_v1.yaml"
    )
    assert config.result_root == "results/chembench_supervised_transfer_v1"
    assert config.state_root == "state/chembench_supervised_transfer_v1"
    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "medium"
    assert config.call_budget["total_model_calls"] == TOTAL_MODEL_CALLS == 5580


def test_supervised_dataset_manifest_is_complete_and_byte_stable() -> None:
    manifest = build_supervised_dataset_manifest(_loader())
    path = MANIFEST_ROOT / "chembench4k_dataset_manifest_supervised_v1.json"

    assert manifest["schema_version"] == "chembench4k_dataset_manifest_supervised_v1"
    assert manifest["revision"] == CHEMBENCH4K_REVISION
    assert len(manifest["files"]) == 18
    assert manifest["dev_count"] == 45
    assert manifest["test_count"] == 4009
    assert manifest["answer_domain"] == ["A", "B", "C", "D"]
    assert path.read_bytes() == canonical_pretty_json_bytes(manifest)


def test_packet_exposes_complete_long_option_in_bounded_core_records() -> None:
    long_option = "C[C@@H](N)" * 2_400
    task = PrivateChemBench4KTask(
        uid="a" * 64,
        category="Retrosynthesis",
        source_split="test",
        source_index=1,
        question="Which precursor set is chemically appropriate?",
        A="short A",
        B="short B",
        C=long_option,
        D="short D",
        target="C",
        dataset_revision=CHEMBENCH4K_REVISION,
        dataset_sha256="b" * 64,
    )
    evaluation = ChemBench4KPrivateEvaluator().evaluate(task=task, raw_completion="B")
    packet = SupervisedEvolutionPacketV1.from_evaluation(
        task=task,
        training_task_ordinal=1,
        round_index=0,
        evaluation=evaluation,
        predecessor_memory=None,
        predecessor_artifact_id=None,
        session_id="supervised-session-0001",
    )
    records = packet.reflector_records()
    assert 1 < len(records) <= 512
    assert all(len(" ".join(record["content"].split())) <= 236 for record in records)
    option_chunks = [record for record in records if record["field"] == "option_C"]
    assert "".join(
        record["content"].partition(" value=")[2] for record in option_chunks
    ) == " ".join(long_option.split())
    assert PACKET_INPUT_SCHEMA_DIGEST in {
        record["content"].partition(" value=")[2]
        for record in records
        if record["field"] == "input_schema_sha256"
    }
    assert REFLECTOR_PROMPT_DIGEST in {
        record["content"].partition(" value=")[2]
        for record in records
        if record["field"] == "reflector_prompt_sha256"
    }


def test_all_historically_exposed_pool_blocks_probe_and_test_split() -> None:
    loader = _loader()
    tasks = loader.load_split("test")
    exposure = HistoricalExposureManifestV1(
        source_repository_commit="d" * 40,
        scanned_file_count=10,
        exposed_uid_count=len(tasks),
        items=tuple(
            {
                "uid": task.uid,
                "category": task.category,
                "exposure_source_type": ["public_task_manifest"],
                "source_run_id_hash": ["e" * 64],
                "first_exposed_protocol": "full_streams",
                "exposure_count": 1,
            }
            for task in tasks
        ),
    )
    with pytest.raises(SupervisedSplitError) as captured:
        generate_balanced_split_v1(loader, exposure_manifest=exposure)
    assert captured.value.finding_code == "INSUFFICIENT_NEVER_EXPOSED_POOL"
    assert set(captured.value.category_counts.values()) == {0}


def test_historical_manifest_contains_only_closed_content_free_items() -> None:
    exposure = load_historical_exposure_manifest(
        MANIFEST_ROOT / "historical_exposed_uid_manifest.json"
    )
    assert exposure.exposed_uid_count == _loader().manifest.test_count == 4009
    allowed = {
        "uid",
        "category",
        "exposure_source_type",
        "source_run_id_hash",
        "first_exposed_protocol",
        "exposure_count",
    }
    assert all(set(item) == allowed for item in exposure.items)
    serialized = exposure.canonical_bytes().decode("utf-8")
    for task in _loader().load_split("test")[:9]:
        assert task.question not in serialized
        assert task.A not in serialized


def test_non_paid_dry_run_uses_v2_taxonomy_and_preserves_old_blocker() -> None:
    completed = subprocess.run(
        (
            str(
                WORKSPACE_ROOT
                / "benchmarks"
                / "chembench"
                / "scripts"
                / "run_chembench_supervised_transfer_v1.sh"
            ),
            "dry-run",
        ),
        cwd=WORKSPACE_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "PASS"
    assert payload["historical_actual_exposed_uid_count"] == 508
    assert payload["historical_strict_never_executed_uid_count"] == 3501
    assert payload["train_count"] == 450
    assert payload["probe_count"] == 90
    assert payload["primary_test_count"] == 450
    assert payload["recovery_test_count"] == 1
    assert payload["reserve_count"] == 2569
    assert payload["model_calls_made"] == 0
    assert payload["paid_calls_started"] is False
    assert payload["src_openevo_pristine"] is True
    assert payload["old_blocked_receipt_sha256"] == (
        "43e64dce6a4c357b5811042140b7a908820fa13e9fe0aefc0f9eab4da11ab18e"
    )


def test_preflight_authority_verifier_uses_verified_runtime_environment() -> None:
    wrapper = (
        WORKSPACE_ROOT
        / "benchmarks"
        / "chembench"
        / "scripts"
        / "run_chembench_supervised_transfer_v1.sh"
    ).read_text(encoding="utf-8")
    runtime_branch = wrapper.split('elif [[ ! -x "${python_executable}" ]]', 1)[0]
    assert '"${1:-}" == "run-preflight"' in runtime_branch
    assert '"${1:-}" == "verify-preflight"' in runtime_branch
    assert '"${1:-}" == "run-formal"' in runtime_branch
    assert 'python_executable="${runtime_python}"' in runtime_branch


def test_source_import_manifest_is_content_free_and_byte_stable() -> None:
    rendered = render_source_import_manifest_v1(
        repository_root=WORKSPACE_ROOT,
        old_repository=Path("/home/lhy-h/work/openevo_chembench/OpenEvo"),
    )
    assert (
        MANIFEST_ROOT / "source_import_manifest_supervised_v1.json"
    ).read_bytes() == rendered
    payload = json.loads(rendered)
    assert len(payload["records"]) == 12
    assert all(
        "source_sha256" in row and "destination_sha256" in row
        for row in payload["records"]
    )


def test_source_import_manifest_reads_committed_source_not_dirty_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / "old"
    repository = tmp_path / "repository"
    relative = "benchmarks/chembench/src/openevo_chembench/example.py"
    source = old / relative
    destination = repository / relative
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    committed = b"committed source\n"
    source.write_bytes(committed)
    destination.write_bytes(b"adapted destination\n")
    subprocess.run(("git", "init", str(old)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(old), "add", relative), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(old),
            "-c",
            "user.name=ChemBench Test",
            "-c",
            "user.email=chembench-test@example.invalid",
            "commit",
            "-m",
            "source snapshot",
        ),
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        source_manifest_module,
        "_IMPORTED_FILES",
        ((relative, "test adaptation"),),
    )
    before = render_source_import_manifest_v1(
        repository_root=repository,
        old_repository=old,
    )
    source.write_bytes(b"dirty mutable worktree\n")
    after = render_source_import_manifest_v1(
        repository_root=repository,
        old_repository=old,
    )
    assert after == before
    assert json.loads(after)["records"][0]["source_sha256"] == hashlib.sha256(
        committed
    ).hexdigest()


def test_category_memory_requires_confirmed_evidence_and_all_sections() -> None:
    memory = """# Category Memory: Name_Conversion
## Confirmed Principles
- Rule ID: N1; Status: confirmed; Category: Name_Conversion; Trigger: ionic name; Principle: balance formal charges; Action: infer stoichiometric subscripts; Validation: recompute net charge; Evidence Count: 2; Supporting Evidence: digest-a,digest-b
## Provisional Principles
- None.
## Common Failure Modes
- Do not ignore ionic charge balance.
## Option Elimination Checks
- Reject candidates with nonzero net formal charge.
## Retired Or Contradicted
- None.
## Output Discipline
- Return exactly one answer letter.
## Do
- Apply charge balance before matching a candidate.
## Avoid
- Avoid lexical matching without chemistry checks.
## Validate
- Recompute the final net charge.
## When Applicable
- Apply to ionic name conversion.
## Retired Or Superseded
- None.
"""
    valid = inspect_supervised_category_memory_v1(
        memory.encode(), category="Name_Conversion"
    )
    assert valid.passed
    assert valid.confirmed_rule_count == 1
    invalid = inspect_supervised_category_memory_v1(
        memory.replace("Evidence Count: 2", "Evidence Count: 1").encode(),
        category="Name_Conversion",
    )
    assert "memory_confirmed_evidence_insufficient" in invalid.finding_codes

    duplicated = memory.replace(
        "## Common Failure Modes",
        "## Provisional Principles\n- None.\n## Common Failure Modes",
        1,
    )
    rejected = inspect_supervised_category_memory_v1(
        duplicated.encode(), category="Name_Conversion"
    )
    assert "memory_required_sections_invalid" in rejected.finding_codes
    normalized, applied = _normalize_supervised_memory_sections(duplicated)
    accepted = inspect_supervised_category_memory_v1(
        normalized.encode(), category="Name_Conversion"
    )
    assert applied is True
    assert accepted.passed


def test_mcnemar_exact_is_two_sided_and_closed() -> None:
    assert _mcnemar_exact(0, 0) == 1.0
    assert _mcnemar_exact(5, 5) == 1.0
    assert _mcnemar_exact(0, 8) == pytest.approx(0.0078125)


def test_desktop_export_excludes_item_level_paired_csv(
    tmp_path: Path,
) -> None:
    result_root = tmp_path / "results"
    state_root = tmp_path / "state"
    reports = result_root / "reports"
    charts = reports / "charts"
    charts.mkdir(parents=True)
    (reports / "test_paired_results.csv").write_text(
        "uid,control_prediction\nsecret-uid,A\n", encoding="utf-8"
    )
    (reports / "per_category_results.csv").write_text(
        "category,accuracy\noverall,0.5\n", encoding="utf-8"
    )
    (charts / "test_overall.png").write_bytes(b"not-a-real-png")
    memory = state_root / "private/checkpoint_memory/Name_Conversion/checkpoint_00.md"
    memory.parent.mkdir(parents=True)
    memory.write_text(
        "# Category Memory: Name_Conversion\n\n## Confirmed Principles\n- None.\n",
        encoding="utf-8",
    )
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    receipt = export_desktop_audit_v1(
        repository_root=WORKSPACE_ROOT,
        run_result_root=result_root,
        run_state_root=state_root,
        run_id="unit-test-run",
        phase="final",
        desktop_root=desktop,
    )
    exported = Path(str(receipt["path"]))
    assert not (exported / "charts/test_paired_results.csv").exists()
    assert (exported / "charts/per_category_results.csv").is_file()
    assert (exported / "charts/test_overall.png").is_file()
    assert "secret-uid" not in "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in exported.rglob("*")
        if path.is_file()
    )


def test_reporting_writes_all_nine_preregistered_charts(tmp_path: Path) -> None:
    train = [
        {"arm": arm, "round": round_index, "accuracy": 0.5}
        for arm in ("control_train", "online_train")
        for round_index in range(3)
    ]
    probe = [
        {
            "checkpoint": checkpoint,
            "category": "overall",
            "control_accuracy": 0.5,
            "online_accuracy": 0.6,
            "delta": 0.1,
        }
        for checkpoint in (0, 10, 20, 30, 40, 50)
    ]
    category_rows = [
        {"category": category, "control_accuracy": 0.5, "online_accuracy": 0.6}
        for category in (
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
    ]
    memory = [
        {"category": row["category"], "training_items": 1, "utf8_bytes": 100}
        for row in category_rows
    ]
    rules = [
        {
            "category": row["category"],
            "training_items": 1,
            "confirmed": 1,
            "provisional": 1,
        }
        for row in category_rows
    ]
    _write_charts(
        tmp_path,
        train,
        probe,
        {"control_accuracy": 0.5, "online_accuracy": 0.6},
        category_rows,
        memory,
        rules,
        [
            {
                "stage": "final_test",
                "wrong_to_correct": 3,
                "correct_to_wrong": 1,
            }
        ],
        [{"evidence_count": 1, "rule_count": 4}],
    )
    assert len(tuple(tmp_path.glob("*.png"))) == 9
