"""Command line entrypoints for the frozen ChemBench4K v2 benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from openevo_chembench.artifact_validator_v2 import (
    ChemBench4KTextMemoryArtifactValidatorV2,
)
from openevo_chembench.benchmark_receipt_v2 import (
    BenchmarkReceiptInputsV2,
    recompute_benchmark_receipt_v2,
    verify_receipt_and_issue_authorization_v2,
    write_benchmark_receipt_v2,
)
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import (
    ChemBench4KPrivateEvaluator,
    PrivateChemBench4KEvaluation,
)
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    PrivateChemBench4KTask,
)
from openevo_chembench.chembench4k_prompt import build_all_dev_leave_one_out_tasks
from openevo_chembench.core_evolution_v2 import (
    EXPECTED_DEV_TRAJECTORIES,
    FrozenCoreTextMemoryV2,
    OpenEvoTextMemoryLifecycleV2,
    build_maintainer_framework_bundle_v2,
)
from openevo_chembench.dev_trajectory_v2 import (
    collect_dev_loo_trajectories_v2,
    load_private_dev_trajectories_v2,
)
from openevo_chembench.frozen_runner_v2 import ChemBench4KFrozenRunnerV2
from openevo_chembench.local_codex_executor import LocalCodexCLIExecutor
from openevo_chembench.paired_statistics_v2 import (
    PairedItemResult,
    compare_paired_results,
)
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorExecutionBoundaryV2,
)
from openevo_chembench.sampling_v2 import (
    generate_task_manifests,
    verify_task_manifests,
)
from openevo_chembench.source_identity_v2 import (
    sha256_file,
    verify_source_manifest,
    write_source_manifest,
)
from openevo_chembench.v2_config import (
    PLACEHOLDER,
    arm_parity_findings,
    load_frozen_config_v2,
)


_MODULE = Path(__file__).resolve()
PACKAGE_ROOT = _MODULE.parents[2]
REPOSITORY_ROOT = _MODULE.parents[4]
WORKSPACE_ROOT = REPOSITORY_ROOT
DATASET_ROOT = (
    WORKSPACE_ROOT
    / "data"
    / "chembench4k"
    / "AI4Chem_ChemBench4K"
    / "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
)
MANIFEST_ROOT = PACKAGE_ROOT / "manifests" / "v2"
PRIVATE_MANIFEST_ROOT = PACKAGE_ROOT / "private_manifests" / "v2"
STATE_ROOT = PACKAGE_ROOT / "state" / "v2"
FRAMEWORK_ROOT = STATE_ROOT / "framework"
CORE_ROOT = STATE_ROOT / "core"
FROZEN_RECORD = STATE_ROOT / "frozen_text_memory_v2.json"
FINAL_RECEIPT = MANIFEST_ROOT / "benchmark_execution_receipt_v2.json"
SOURCE_MANIFEST = PACKAGE_ROOT / "manifests" / "chembench_source_manifest_v2.json"
SOURCE_ACCEPTANCE = MANIFEST_ROOT / "source_manifest_acceptance_v2.json"
TEXT_MEMORY_EVOLUTION_CONFIG = PACKAGE_ROOT / "configs" / "text_memory_evolution_v2.yaml"
_EMPTY_RESULT_CHAIN_SHA256 = hashlib.sha256(b"").hexdigest()
_PRIVATE_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_id",
        "arm",
        "scope",
        "ordinal",
        "uid",
        "category",
        "target",
        "raw_completion",
        "official_prediction",
        "strict_prediction",
        "official_parse_status",
        "strict_parse_status",
        "correct",
    }
)
_PUBLIC_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_id",
        "arm",
        "scope",
        "ordinal",
        "uid",
        "category",
        "raw_completion",
        "parsed_prediction",
        "score",
        "official_parse_status",
        "strict_parse_status",
        "strict_parse_success",
        "transcript_reference",
        "runtime_metadata",
    }
)


def _loader() -> ChemBench4KDatasetLoader:
    return ChemBench4KDatasetLoader(snapshot_root=DATASET_ROOT)


def _scope_paths(scope: str) -> tuple[Path, Path, Path]:
    return (
        MANIFEST_ROOT / f"{scope}_public_manifest.jsonl",
        PRIVATE_MANIFEST_ROOT / f"{scope}_private_manifest.jsonl",
        MANIFEST_ROOT / f"{scope}_manifest_summary.json",
    )


def _canonical_config_path(*, arm: str, scope: str) -> Path:
    return (PACKAGE_ROOT / "configs" / f"{arm}_{scope}_frozen_v2.yaml").resolve()


def _load_requested_config(config_path: Path):
    resolved = config_path.resolve()
    config = load_frozen_config_v2(resolved)
    if resolved != _canonical_config_path(arm=config.arm, scope=config.scope):
        raise RuntimeError("run-arm config is not a registered v2 protocol config")
    return config


def _refuse_if_receipt_is_frozen(action: str) -> None:
    if FINAL_RECEIPT.exists():
        raise RuntimeError(f"{action} is forbidden after the execution receipt is frozen")


def _assert_v2_result_roots_absent() -> None:
    for scope in ("canary18", "pilot500", "full"):
        for arm in ("baseline", "evolved"):
            output = WORKSPACE_ROOT / "OpenEvo" / "results" / "chembench4k_frozen_v2" / scope / arm
            if output.exists():
                raise RuntimeError("cannot freeze configs after a v2 result exists")


def _verify_config_scope_manifests(
    *,
    config: Any,
    loader: ChemBench4KDatasetLoader,
) -> None:
    canonical_public, canonical_private, canonical_summary = _scope_paths(config.scope)
    configured_public = (WORKSPACE_ROOT / config.task_manifest).resolve()
    configured_private = (WORKSPACE_ROOT / config.private_task_manifest).resolve()
    if (
        configured_public != canonical_public.resolve()
        or configured_private != canonical_private.resolve()
    ):
        raise RuntimeError("run-arm config does not bind canonical v2 task manifests")
    verify_task_manifests(
        loader,
        scope=config.scope,
        public_path=canonical_public,
        private_path=canonical_private,
        summary_path=canonical_summary,
    )


def _receipt_inputs() -> BenchmarkReceiptInputsV2:
    pilot_public, pilot_private, pilot_summary = _scope_paths("pilot500")
    return BenchmarkReceiptInputsV2(
        workspace_root=WORKSPACE_ROOT,
        repository_root=REPOSITORY_ROOT,
        package_root=PACKAGE_ROOT,
        dataset_root=DATASET_ROOT,
        source_manifest_path=SOURCE_MANIFEST,
        pilot_public_manifest_path=pilot_public,
        pilot_private_manifest_path=pilot_private,
        pilot_summary_path=pilot_summary,
        baseline_config_path=PACKAGE_ROOT / "configs" / "baseline_pilot500_frozen_v2.yaml",
        evolved_config_path=PACKAGE_ROOT / "configs" / "evolved_pilot500_frozen_v2.yaml",
        framework_lock_path=FRAMEWORK_ROOT / "framework-lock.json",
        core_database_path=CORE_ROOT / "evolution.sqlite3",
        core_artifact_root=CORE_ROOT / "artifacts",
        frozen_record_path=FROZEN_RECORD,
        reflector_private_audit_root=STATE_ROOT / "private_reflector_events",
        source_acceptance_path=SOURCE_ACCEPTANCE if SOURCE_ACCEPTANCE.exists() else None,
    )


def _print_json(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    )


def generate_manifests() -> dict[str, object]:
    loader = _loader()
    generated: dict[str, object] = {}
    for scope in ("canary18", "pilot500", "full"):
        public, private, summary = _scope_paths(scope)
        result = generate_task_manifests(
            loader,
            scope=scope,
            public_path=public,
            private_path=private,
            summary_path=summary,
        )
        generated[scope] = {
            "items": result.item_count,
            "public_sha256": result.public_sha256,
            "private_sha256": result.private_sha256,
            "summary_sha256": result.summary_sha256,
            "ordered_uid_hash": result.ordered_uid_hash,
        }
    return generated


def validate_static_state() -> dict[str, object]:
    loader = _loader()
    configured_max_records = _configured_reflector_max_records()
    reflector_capability = ReflectorExecutionBoundaryV2.detect_capability()
    if not reflector_capability.available:
        raise RuntimeError("REFLECTOR_FILESYSTEM_ISOLATION_MISSING")
    loo_tasks = build_all_dev_leave_one_out_tasks(
        {
            category: loader.load_category(category, split="dev")
            for category in CHEMBENCH4K_CATEGORIES
        }
    )
    loo_uids = tuple(task.evaluation_task.uid for task in loo_tasks)
    if len(loo_uids) != EXPECTED_DEV_TRAJECTORIES or len(set(loo_uids)) != len(loo_uids):
        raise RuntimeError("dev LOO task set is not exactly 45 unique records")
    ordered_loo_uid_sha256 = hashlib.sha256("\n".join(loo_uids).encode("ascii")).hexdigest()
    scopes: dict[str, object] = {}
    for scope in ("canary18", "pilot500", "full"):
        public, private, summary = _scope_paths(scope)
        manifest = verify_task_manifests(
            loader,
            scope=scope,
            public_path=public,
            private_path=private,
            summary_path=summary,
        )
        baseline = load_frozen_config_v2(
            PACKAGE_ROOT / "configs" / f"baseline_{scope}_frozen_v2.yaml"
        )
        evolved = load_frozen_config_v2(
            PACKAGE_ROOT / "configs" / f"evolved_{scope}_frozen_v2.yaml"
        )
        parity = arm_parity_findings(baseline, evolved)
        if parity:
            raise RuntimeError("paired arm parity validation failed")
        scopes[scope] = {
            "items": manifest.item_count,
            "manifest_sha256": manifest.public_sha256,
            "baseline_config_sha256": baseline.config_sha256(),
            "evolved_config_sha256": evolved.config_sha256(),
            "artifact_frozen": evolved.artifact.is_frozen,
        }
    return {
        "status": "PASS",
        "dataset_repository": loader.manifest.repository,
        "dataset_revision": loader.manifest.revision,
        "dataset_sha256": loader.manifest.combined_sha256,
        "dev_count": loader.manifest.dev_count,
        "test_count": loader.manifest.test_count,
        "loo_record_count": len(loo_tasks),
        "configured_max_records": configured_max_records,
        "ordered_loo_uid_sha256": ordered_loo_uid_sha256,
        "records_visible_to_reflector": min(
            len(loo_tasks),
            configured_max_records,
        ),
        "reflector_filesystem_isolation": reflector_capability.mechanism,
        "reflector_event_wrapper": "required_for_registered_method",
        "scopes": scopes,
    }


def _configured_reflector_max_records() -> int:
    try:
        payload = yaml.safe_load(TEXT_MEMORY_EVOLUTION_CONFIG.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError("text-memory evolution config is unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("text-memory evolution config is not an object")
    dev = payload.get("dev_trajectory")
    reflector = payload.get("reflector")
    if not isinstance(dev, dict) or not isinstance(reflector, dict):
        raise RuntimeError("text-memory evolution record config is incomplete")
    expected = dev.get("expected_records")
    maximum = reflector.get("max_records")
    if (
        isinstance(expected, bool)
        or isinstance(maximum, bool)
        or expected != EXPECTED_DEV_TRAJECTORIES
        or maximum != EXPECTED_DEV_TRAJECTORIES
    ):
        raise RuntimeError("text-memory evolution must explicitly configure all 45 records")
    return maximum


def _configured_reflector_timeout_seconds() -> float:
    try:
        payload = yaml.safe_load(TEXT_MEMORY_EVOLUTION_CONFIG.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError("text-memory evolution config is unavailable") from exc
    reflector = payload.get("reflector") if isinstance(payload, dict) else None
    timeout = reflector.get("timeout_seconds") if isinstance(reflector, dict) else None
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise RuntimeError("reflector timeout must be explicitly configured")
    if not 0 < float(timeout) <= 86_400:
        raise RuntimeError("reflector timeout is outside the allowed range")
    return float(timeout)


def _load_frozen_record() -> FrozenCoreTextMemoryV2:
    try:
        return FrozenCoreTextMemoryV2.model_validate_json(
            FROZEN_RECORD.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("frozen Core text-memory record is unavailable") from exc


def _lifecycle() -> OpenEvoTextMemoryLifecycleV2:
    return OpenEvoTextMemoryLifecycleV2.from_framework_lock(
        db_path=CORE_ROOT / "evolution.sqlite3",
        artifact_root=CORE_ROOT / "artifacts",
        framework_lock=FRAMEWORK_ROOT / "framework-lock.json",
    )


def _git_commit() -> str:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=REPOSITORY_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _protocol_binding_hash(
    frozen: FrozenCoreTextMemoryV2,
) -> str:
    pilot_public, _pilot_private, pilot_summary = _scope_paths("pilot500")
    payload = {
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "openevo_commit": _git_commit(),
        "dataset_sha256": _loader().manifest.combined_sha256,
        "pilot_manifest_sha256": sha256_file(pilot_public),
        "pilot_summary_sha256": sha256_file(pilot_summary),
        "prompt_renderer_sha256": sha256_file(
            PACKAGE_ROOT / "src" / "openevo_chembench" / "chembench4k_prompt.py"
        ),
        "parser_evaluator_sha256": sha256_file(
            PACKAGE_ROOT / "src" / "openevo_chembench" / "chembench4k_evaluation.py"
        ),
        "executor_sha256": sha256_file(
            PACKAGE_ROOT / "src" / "openevo_chembench" / "local_codex_executor.py"
        ),
        "core_artifact_id": frozen.execution.core_artifact_id,
        "artifact_payload_sha256": frozen.execution.artifact_payload_sha256,
        "context_resolution_digest": frozen.context_resolution_digest,
        "resolved_memory_sha256": frozen.resolved_memory_sha256,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_v2_configs(frozen: FrozenCoreTextMemoryV2) -> dict[str, str]:
    """Bind all six new configs before any paid test execution."""

    _assert_v2_result_roots_absent()
    protocol_hash = _protocol_binding_hash(frozen)
    hashes: dict[str, str] = {}
    for scope in ("canary18", "pilot500", "full"):
        for arm in ("baseline", "evolved"):
            path = PACKAGE_ROOT / "configs" / f"{arm}_{scope}_frozen_v2.yaml"
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            payload["pilot_protocol_hash"] = protocol_hash
            if arm == "evolved":
                evolution = payload["evolution"]
                evolution["frozen_artifact_id"] = frozen.execution.core_artifact_id
                evolution["frozen_artifact_sha256"] = frozen.execution.artifact_payload_sha256
                evolution["context_resolution_digest"] = frozen.context_resolution_digest
                evolution["resolved_memory_sha256"] = frozen.resolved_memory_sha256
            encoded = yaml.safe_dump(
                payload,
                allow_unicode=True,
                sort_keys=False,
            ).encode("utf-8")
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_bytes(encoded)
            temporary.replace(path)
            hashes[path.name] = hashlib.sha256(encoded).hexdigest()
    return hashes


def collect_dev() -> dict[str, object]:
    _refuse_if_receipt_is_frozen("dev trajectory collection")
    config = load_frozen_config_v2(PACKAGE_ROOT / "configs" / "baseline_canary18_frozen_v2.yaml")
    output_root = STATE_ROOT / "dev_loo"
    diagnostic_root = STATE_ROOT / "private_executor_events" / "dev_loo"
    with LocalCodexCLIExecutor(
        config=config,
        task_timeout_seconds=config.executor.timeout_seconds,
        diagnostic_root=diagnostic_root,
    ) as executor:
        collection = collect_dev_loo_trajectories_v2(
            loader=_loader(),
            executor=executor,
            output_root=output_root,
        )
    return {
        "status": "COMPLETED",
        "record_count": collection.record_count,
        "records_sha256": collection.records_sha256,
        "private_records_path": os.fspath(collection.private_records_path),
    }


def run_evolution() -> dict[str, object]:
    _refuse_if_receipt_is_frozen("evolution execution")
    if FROZEN_RECORD.exists():
        raise RuntimeError("frozen text-memory record already exists")
    _assert_v2_result_roots_absent()
    records = load_private_dev_trajectories_v2(
        STATE_ROOT / "dev_loo" / "private" / "private_dev_trajectories.jsonl"
    )
    lifecycle = _lifecycle()
    prepared = lifecycle.prepare_dev_job(
        records,
        configured_max_records=_configured_reflector_max_records(),
        reflector_timeout_seconds=_configured_reflector_timeout_seconds(),
    )
    reflector_boundary = ReflectorExecutionBoundaryV2(
        dev_artifact_path=lifecycle.private_reflector_input_path(prepared),
        expected_records_sha256=prepared.reflector_input_digest,
        private_audit_root=STATE_ROOT / "private_reflector_events",
        timeout_seconds=_configured_reflector_timeout_seconds(),
    )
    execution = lifecycle.execute_registered_method(
        prepared,
        reflector_boundary=reflector_boundary,
    )
    loader = _loader()
    validator = ChemBench4KTextMemoryArtifactValidatorV2(
        dev_tasks=loader.load_split("dev"),
        test_uids=frozenset(task.uid for task in loader.load_split("test")),
        expected_dev_uid_set_sha256=prepared.dev_uid_set_sha256,
    )
    frozen = lifecycle.validate_promote_and_resolve(
        execution,
        validator=validator,
    )
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (frozen.model_dump_json(indent=2, exclude_none=False) + "\n").encode("utf-8")
    descriptor = os.open(
        FROZEN_RECORD,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    config_hashes = freeze_v2_configs(frozen)
    write_source_manifest(PACKAGE_ROOT)
    return {
        "status": "COMPLETED",
        **frozen.audit_payload(),
        "frozen_record_sha256": hashlib.sha256(encoded).hexdigest(),
        "config_file_sha256": config_hashes,
    }


def validate_artifact() -> dict[str, object]:
    frozen = _load_frozen_record()
    loader = _loader()
    validator = ChemBench4KTextMemoryArtifactValidatorV2(
        dev_tasks=loader.load_split("dev"),
        test_uids=frozenset(task.uid for task in loader.load_split("test")),
        expected_dev_uid_set_sha256=(frozen.execution.prepared_job.dev_uid_set_sha256),
    )
    evidence = _lifecycle().verify_frozen_record(
        frozen,
        validator=validator,
    )
    if not frozen.validation_receipt.passed:
        raise RuntimeError("frozen artifact validator receipt is not passing")
    return evidence


def run_arm(config_path: Path, *, resume: bool = False) -> dict[str, object]:
    config = _load_requested_config(config_path)
    frozen = _load_frozen_record()
    if (
        config.pilot_protocol_hash == PLACEHOLDER
        or config.pilot_protocol_hash != _protocol_binding_hash(frozen)
    ):
        raise RuntimeError("run-arm config is not bound to the frozen v2 protocol")
    loader = _loader()
    _verify_config_scope_manifests(config=config, loader=loader)
    authorization = verify_receipt_and_issue_authorization_v2(
        _receipt_inputs(),
        FINAL_RECEIPT,
    )
    if config.scope == "full":
        pilot_report = _compute_comparison(
            "pilot500",
            expected_receipt_sha256=authorization.receipt_sha256,
        )
        stored_pilot_report = _read_json(
            WORKSPACE_ROOT
            / "OpenEvo"
            / "results"
            / "chembench4k_frozen_v2"
            / "pilot500"
            / "paired_comparison.json"
        )
        if stored_pilot_report != pilot_report or pilot_report.get("decision") != "GO":
            raise RuntimeError("full run requires an unchanged passing pilot500 report")
    memory = None
    if config.arm == "evolved":
        validator = ChemBench4KTextMemoryArtifactValidatorV2(
            dev_tasks=loader.load_split("dev"),
            test_uids=frozenset(task.uid for task in loader.load_split("test")),
            expected_dev_uid_set_sha256=(frozen.execution.prepared_job.dev_uid_set_sha256),
        )
        memory = _lifecycle().issue_runtime_memory(
            frozen,
            validator=validator,
        )
    diagnostic_root = STATE_ROOT / "private_executor_events" / config.scope / config.arm
    with LocalCodexCLIExecutor(
        config=config,
        task_timeout_seconds=config.executor.timeout_seconds,
        diagnostic_root=diagnostic_root,
    ) as executor:
        result = ChemBench4KFrozenRunnerV2(
            config=config,
            loader=loader,
            executor=executor,
            workspace_root=WORKSPACE_ROOT,
            authorization=authorization,
            resolved_text_memory=memory,
            resume=resume,
        ).run()
    return result.to_public_dict()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("paired result input is unavailable or invalid") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("frozen JSON evidence is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("frozen JSON evidence must be an object")
    return payload


def _canonical_json_line(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _read_canonical_result_jsonl(
    path: Path,
) -> tuple[list[dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("paired result input is unavailable or invalid") from exc
    if raw and not raw.endswith(b"\n"):
        raise RuntimeError("paired result input is truncated")
    rows: list[dict[str, Any]] = []
    chain = _EMPTY_RESULT_CHAIN_SHA256
    for encoded in raw.splitlines(keepends=True):
        try:
            payload = json.loads(encoded)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("paired result input is unavailable or invalid") from exc
        if type(payload) is not dict or _canonical_json_line(payload) != encoded:
            raise RuntimeError("paired result input is not canonical")
        rows.append(payload)
        chain = hashlib.sha256(bytes.fromhex(chain) + encoded).hexdigest()
    return rows, chain


def _index_unique_rows(
    rows: list[dict[str, Any]],
    *,
    boundary: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row.get("uid")
        if type(uid) is not str or not uid or uid in indexed:
            raise RuntimeError(f"{boundary} result identity is invalid")
        indexed[uid] = row
    return indexed


def _validate_run_state(
    *,
    config: Any,
    state: dict[str, Any],
    expected_receipt_sha256: str,
    planned_tasks: int,
    private_rows: int,
    public_chain: str,
    private_chain: str,
) -> None:
    if (
        state.get("protocol_id") != "chembench4k_frozen_generalization_v2"
        or state.get("execution_mode") != "standalone_openevo_maintainer_benchmark"
        or state.get("run_name") != config.run_name
        or state.get("arm") != config.arm
        or state.get("scope") != config.scope
        or state.get("config_sha256") != config.config_sha256()
        or state.get("execution_receipt_sha256") != expected_receipt_sha256
        or state.get("planned_tasks") != planned_tasks
        or state.get("completed_tasks") != private_rows
        or state.get("public_result_chain_sha256") != public_chain
        or state.get("private_result_chain_sha256") != private_chain
    ):
        raise RuntimeError("paired run state no longer matches frozen evidence")
    status = state.get("status")
    if status == "COMPLETED":
        resume_count = state.get("resume_count")
        if (
            private_rows != planned_tasks
            or isinstance(resume_count, bool)
            or not isinstance(resume_count, int)
            or resume_count < 0
            or state.get("model_calls") != planned_tasks + resume_count
            or state.get("finding_codes") != []
            or state.get("resume_allowed") is not False
        ):
            raise RuntimeError("completed paired run state is internally inconsistent")
        return
    if status == "SECURITY_TOOL_USE_VIOLATION":
        if (
            state.get("resume_allowed") is not False
            or state.get("failure_code") != "SECURITY_TOOL_USE_VIOLATION"
        ):
            raise RuntimeError("security-terminated run state is internally inconsistent")
        return
    if status == "EXECUTION_FAILED" and state.get("resume_allowed") is False:
        return
    raise RuntimeError("paired run has not reached a comparable terminal state")


def _verify_private_result(
    *,
    row: dict[str, Any],
    task: PrivateChemBench4KTask,
    arm: str,
    scope: str,
    ordinal: int,
) -> PrivateChemBench4KEvaluation:
    if set(row) != _PRIVATE_RESULT_KEYS:
        raise RuntimeError("private paired result schema is invalid")
    raw_completion = row.get("raw_completion")
    if type(raw_completion) is not str:
        raise RuntimeError("private paired completion is invalid")
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion=raw_completion,
    )
    expected = {
        "schema_version": "chembench4k_private_item_result_v2",
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "arm": arm,
        "scope": scope,
        "ordinal": ordinal,
        "uid": task.uid,
        "category": task.category,
        "target": task.target,
        "raw_completion": raw_completion,
        "official_prediction": evaluation.official.prediction,
        "strict_prediction": evaluation.strict.prediction,
        "official_parse_status": evaluation.official.status.value,
        "strict_parse_status": evaluation.strict.status.value,
        "correct": evaluation.correct,
    }
    if row != expected:
        raise RuntimeError("private paired result no longer matches evaluator output")
    return evaluation


def _verify_public_completed_result(
    *,
    row: dict[str, Any],
    task: PrivateChemBench4KTask,
    evaluation: PrivateChemBench4KEvaluation,
    arm: str,
    scope: str,
    ordinal: int,
) -> None:
    if set(row) != _PUBLIC_RESULT_KEYS:
        raise RuntimeError("public paired result schema is invalid")
    transcript_reference = row.get("transcript_reference")
    runtime_metadata = row.get("runtime_metadata")
    expected = {
        "schema_version": "chembench4k_public_item_result_v2",
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "arm": arm,
        "scope": scope,
        "ordinal": ordinal,
        "uid": task.uid,
        "category": task.category,
        "raw_completion": evaluation.raw_completion,
        "parsed_prediction": evaluation.official.prediction,
        "score": 1.0 if evaluation.correct else 0.0,
        "official_parse_status": evaluation.official.status.value,
        "strict_parse_status": evaluation.strict.status.value,
        "strict_parse_success": evaluation.strict.parsed,
        "transcript_reference": transcript_reference,
        "runtime_metadata": runtime_metadata,
    }
    if (
        type(transcript_reference) is not str
        or not transcript_reference
        or (runtime_metadata is not None and not isinstance(runtime_metadata, dict))
        or row != expected
    ):
        raise RuntimeError("public paired result no longer matches evaluator output")


def _compute_comparison(
    scope: str,
    *,
    expected_receipt_sha256: str,
) -> dict[str, Any]:
    baseline_config = load_frozen_config_v2(
        PACKAGE_ROOT / "configs" / f"baseline_{scope}_frozen_v2.yaml"
    )
    evolved_config = load_frozen_config_v2(
        PACKAGE_ROOT / "configs" / f"evolved_{scope}_frozen_v2.yaml"
    )
    if (
        arm_parity_findings(baseline_config, evolved_config)
        or baseline_config.pilot_protocol_hash == PLACEHOLDER
        or evolved_config.pilot_protocol_hash == PLACEHOLDER
    ):
        raise RuntimeError("paired arms no longer satisfy parity")
    loader = _loader()
    public_manifest_path = WORKSPACE_ROOT / baseline_config.task_manifest
    private_manifest_path = WORKSPACE_ROOT / baseline_config.private_task_manifest
    _public, _private, summary_path = _scope_paths(scope)
    manifest_evidence = verify_task_manifests(
        loader,
        scope=scope,
        public_path=public_manifest_path,
        private_path=private_manifest_path,
        summary_path=summary_path,
    )
    private_manifest = _index_unique_rows(
        _read_jsonl(private_manifest_path),
        boundary="private manifest",
    )
    tasks = {task.uid: task for task in loader.load_split("test")}
    if len(private_manifest) != manifest_evidence.item_count or not set(private_manifest).issubset(
        tasks
    ):
        raise RuntimeError("paired private manifest does not match the frozen test split")
    baseline_root = WORKSPACE_ROOT / baseline_config.output_directory
    evolved_root = WORKSPACE_ROOT / evolved_config.output_directory
    baseline_state = _read_json(baseline_root / "run_state.json")
    evolved_state = _read_json(evolved_root / "run_state.json")
    baseline_rows, baseline_private_chain = _read_canonical_result_jsonl(
        baseline_root / "private" / "results.jsonl"
    )
    evolved_rows, evolved_private_chain = _read_canonical_result_jsonl(
        evolved_root / "private" / "results.jsonl"
    )
    baseline_public_rows, baseline_public_chain = _read_canonical_result_jsonl(
        baseline_root / "public" / "results.jsonl"
    )
    evolved_public_rows, evolved_public_chain = _read_canonical_result_jsonl(
        evolved_root / "public" / "results.jsonl"
    )
    baseline = _index_unique_rows(baseline_rows, boundary="baseline private")
    evolved = _index_unique_rows(evolved_rows, boundary="evolved private")
    baseline_public = _index_unique_rows(
        baseline_public_rows,
        boundary="baseline public",
    )
    evolved_public = _index_unique_rows(
        evolved_public_rows,
        boundary="evolved public",
    )
    known_uids = set(private_manifest)
    if (
        not set(baseline).issubset(known_uids)
        or not set(evolved).issubset(known_uids)
        or not set(baseline_public).issubset(known_uids)
        or not set(evolved_public).issubset(known_uids)
    ):
        raise RuntimeError("paired results contain an unknown task identity")
    _validate_run_state(
        config=baseline_config,
        state=baseline_state,
        expected_receipt_sha256=expected_receipt_sha256,
        planned_tasks=len(private_manifest),
        private_rows=len(baseline),
        public_chain=baseline_public_chain,
        private_chain=baseline_private_chain,
    )
    _validate_run_state(
        config=evolved_config,
        state=evolved_state,
        expected_receipt_sha256=expected_receipt_sha256,
        planned_tasks=len(private_manifest),
        private_rows=len(evolved),
        public_chain=evolved_public_chain,
        private_chain=evolved_private_chain,
    )
    records: list[PairedItemResult] = []
    for ordinal, (uid, item) in enumerate(private_manifest.items()):
        left = baseline.get(uid)
        right = evolved.get(uid)
        left_public = baseline_public.get(uid)
        right_public = evolved_public.get(uid)
        left_security = (
            left_public is not None
            and left_public.get("run_status") == "SECURITY_TOOL_USE_VIOLATION"
        )
        right_security = (
            right_public is not None
            and right_public.get("run_status") == "SECURITY_TOOL_USE_VIOLATION"
        )
        if (left is not None and left_security) or (right is not None and right_security):
            raise RuntimeError("paired item has conflicting completion evidence")
        left_evaluation = (
            None
            if left is None
            else _verify_private_result(
                row=left,
                task=tasks[uid],
                arm="baseline",
                scope=scope,
                ordinal=ordinal,
            )
        )
        right_evaluation = (
            None
            if right is None
            else _verify_private_result(
                row=right,
                task=tasks[uid],
                arm="evolved",
                scope=scope,
                ordinal=ordinal,
            )
        )
        if left_evaluation is not None:
            if left_public is None:
                raise RuntimeError("baseline public paired result is missing")
            _verify_public_completed_result(
                row=left_public,
                task=tasks[uid],
                evaluation=left_evaluation,
                arm="baseline",
                scope=scope,
                ordinal=ordinal,
            )
        if right_evaluation is not None:
            if right_public is None:
                raise RuntimeError("evolved public paired result is missing")
            _verify_public_completed_result(
                row=right_public,
                task=tasks[uid],
                evaluation=right_evaluation,
                arm="evolved",
                scope=scope,
                ordinal=ordinal,
            )
        records.append(
            PairedItemResult(
                uid=uid,
                category=item["category"],
                target=item["target"],
                baseline_prediction=(
                    None if left_evaluation is None else left_evaluation.official.prediction
                ),
                evolved_prediction=(
                    None if right_evaluation is None else right_evaluation.official.prediction
                ),
                baseline_official_parsed=(
                    left_evaluation is not None and left_evaluation.official.parsed
                ),
                evolved_official_parsed=(
                    right_evaluation is not None and right_evaluation.official.parsed
                ),
                baseline_strict_parsed=(
                    left_evaluation is not None and left_evaluation.strict.parsed
                ),
                evolved_strict_parsed=(
                    right_evaluation is not None and right_evaluation.strict.parsed
                ),
                baseline_infrastructure_failure=left is None and not left_security,
                evolved_infrastructure_failure=right is None and not right_security,
                baseline_security_violation=left_security,
                evolved_security_violation=right_security,
            )
        )
    report = compare_paired_results(records, planned_pairs=len(private_manifest))
    report["evidence"] = {
        "receipt_sha256": expected_receipt_sha256,
        "manifest_sha256": manifest_evidence.public_sha256,
        "ordered_uid_hash": manifest_evidence.ordered_uid_hash,
        "baseline_config_sha256": baseline_config.config_sha256(),
        "evolved_config_sha256": evolved_config.config_sha256(),
        "baseline_public_result_chain_sha256": baseline_public_chain,
        "baseline_private_result_chain_sha256": baseline_private_chain,
        "evolved_public_result_chain_sha256": evolved_public_chain,
        "evolved_private_result_chain_sha256": evolved_private_chain,
    }
    if scope == "canary18":
        report["decision"] = "INFRASTRUCTURE_CANARY_ONLY"
    elif scope == "full":
        report["decision"] = "FINAL_PAIRED_REPORT"
    return report


def compare_scope(scope: str) -> dict[str, Any]:
    authorization = verify_receipt_and_issue_authorization_v2(
        _receipt_inputs(),
        FINAL_RECEIPT,
    )
    report = _compute_comparison(
        scope,
        expected_receipt_sha256=authorization.receipt_sha256,
    )
    destination = (
        WORKSPACE_ROOT
        / "OpenEvo"
        / "results"
        / "chembench4k_frozen_v2"
        / scope
        / "paired_comparison.json"
    )
    if destination.exists():
        raise RuntimeError("paired comparison output already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chembench4k-frozen-v2")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "generate-manifests",
        "source-manifest",
        "validate-static",
        "build-framework",
        "collect-dev-loo",
        "run-evolution",
        "validate-artifact",
        "freeze-receipt",
        "dry-run",
    ):
        subparsers.add_parser(name)
    run_parser = subparsers.add_parser("run-arm")
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--resume", action="store_true")
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument(
        "--scope",
        choices=("canary18", "pilot500", "full"),
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate-manifests":
        _refuse_if_receipt_is_frozen("task manifest regeneration")
        payload = generate_manifests()
    elif args.command == "source-manifest":
        _refuse_if_receipt_is_frozen("source manifest regeneration")
        path, digest = write_source_manifest(PACKAGE_ROOT)
        payload = {"status": "PASS", "path": os.fspath(path), "sha256": digest}
    elif args.command == "validate-static":
        verify_source_manifest(PACKAGE_ROOT, SOURCE_MANIFEST)
        payload = validate_static_state()
    elif args.command == "build-framework":
        payload = build_maintainer_framework_bundle_v2(
            repository_root=REPOSITORY_ROOT,
            output_directory=FRAMEWORK_ROOT,
        ).model_dump(mode="json")
    elif args.command == "collect-dev-loo":
        payload = collect_dev()
    elif args.command == "run-evolution":
        payload = run_evolution()
    elif args.command == "validate-artifact":
        payload = validate_artifact()
    elif args.command == "freeze-receipt":
        if FINAL_RECEIPT.exists():
            authorization = verify_receipt_and_issue_authorization_v2(
                _receipt_inputs(),
                FINAL_RECEIPT,
            )
            payload = {
                "status": "ALREADY_FROZEN",
                "path": os.fspath(FINAL_RECEIPT),
                "sha256": authorization.receipt_sha256,
                "paid_execution_allowed": True,
            }
            _print_json(payload)
            return 0
        receipt = recompute_benchmark_receipt_v2(_receipt_inputs())
        if not receipt.paid_execution_allowed:
            _print_json(receipt.to_payload())
            return 2
        receipt = write_benchmark_receipt_v2(_receipt_inputs(), FINAL_RECEIPT)
        payload = receipt.to_payload()
        _print_json(payload)
        return 0
    elif args.command == "dry-run":
        payload = {
            "source_manifest_sha256": verify_source_manifest(
                PACKAGE_ROOT,
                SOURCE_MANIFEST,
            ),
            "static": validate_static_state(),
            "receipt_preview": recompute_benchmark_receipt_v2(_receipt_inputs()).to_payload(),
            "paid_calls_made": 0,
        }
    elif args.command == "run-arm":
        payload = run_arm(args.config.resolve(), resume=args.resume)
    elif args.command == "compare":
        payload = compare_scope(args.scope)
    else:  # pragma: no cover
        raise AssertionError("unreachable command")
    _print_json(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
