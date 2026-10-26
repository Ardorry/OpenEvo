from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CONTRACT_KEY,
    CODEX_SUBSCRIPTION_READINESS_KEY,
    codex_subscription_contract,
)

from .checkpoint_recovery import (
    _completion_wall_time,
    _load_core_completion,
)
from .compatible_evaluation import (
    build_evolution_judge_task,
    parse_evolution_feedback_result,
)
from .feedback import reflector_feedback_payload, runtime_feedback
from .hashing import canonical_sha256, file_sha256, text_sha256
from .models import ArtifactKind, EvaluatorFeedback, FeedbackMode, TaskItem, Trajectory
from .replacement_ledger import (
    VerifiedReplacementPhaseLedger,
    validate_evolved_candidate_no_effect_receipt,
    verified_no_effect_attempt_count,
)
from .runtime import (
    CORE_MANAGED_CODEX_ROUTE,
    _message_content,
    _normalize_rollout,
    build_task_request,
    s0_config_hash,
)
from .three_artifact_evolution import (
    _core_reflector_contract_hash,
    _strict_reflector_markdown,
    detect_artifact_duplicates,
    normalize_artifact_text,
    render_core_native_reflector_prompt,
)
from .three_artifact_models import (
    CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
    CORE_ROLE_WRAPPER_PROMPT_PROFILE,
    THREE_ARTIFACT_ORDER,
    ArtifactSeparationPolicy,
    ThreeArtifactBundleReceipt,
    ThreeArtifactReceipt,
)

_CHECKPOINT_NAME = "evolved-candidate-boundary.checkpoint.json"
_RECEIPT_NAME = "evolved-candidate-no-effect.recovery.json"
_RECOVERABLE_ERROR = (
    "agent execution failed: Codex subscription credential isolation could not be "
    "proven (validation_failed)"
)
_CLAIM_PHASES = {
    "baseline_candidate",
    "baseline_internal_evaluator",
    "reflector_memory",
    "reflector_skill_bundle",
    "reflector_agent_system",
    "evolved_candidate",
}
_REFLECTOR_PHASES = {
    ArtifactKind.TEXT_MEMORY: "reflector_memory",
    ArtifactKind.SKILL_BUNDLE: "reflector_skill_bundle",
    ArtifactKind.AGENT_SYSTEM: "reflector_agent_system",
}
_METHODS = {
    ArtifactKind.TEXT_MEMORY: "text_memory_reflector",
    ArtifactKind.SKILL_BUNDLE: "skill_bundle_reflector",
    ArtifactKind.AGENT_SYSTEM: "agent_system_reflector",
}
_CONTENT_NAMES = {
    ArtifactKind.TEXT_MEMORY: "memory.md",
    ArtifactKind.SKILL_BUNDLE: "SKILL.md",
    ArtifactKind.AGENT_SYSTEM: "AGENTS.md",
}
_CHECKPOINT_KEYS = {
    "schema_version",
    "status",
    "pair_id",
    "task_id",
    "task_authority_sha256",
    "baseline",
    "baseline_internal_evaluation",
    "artifact_bundle",
    "input_evidence_sha256",
    "artifact_ids_by_type",
    "evolved_claim_authority_sha256",
}
_RECOVERY_RECEIPT_KEYS = {
    "schema_version",
    "status",
    "pair_id",
    "task_id",
    "task_authority_sha256",
    "attempt_ordinal",
    "replacement_run_id",
    "config_sha256",
    "checkpoint_sha256",
    "phase_receipt_sha256",
    "failed_core_completion_sha256",
    "baseline_core_completion_sha256",
    "baseline_evaluator_core_completion_sha256",
    "baseline_evaluator_prompt_sha256",
    "reflector_authority",
    "artifact_ids_by_type",
    "reflector_job_ids",
    "reflector_run_ids",
    "reflector_prompt_hashes",
    "input_evidence_sha256",
    "injection_authority",
    "completed_scientific_calls_reused",
    "prior_scientific_calls_redispatched",
    "replacement_scope",
    "model_calls_during_reconciliation",
    "generation_time_reconstruction",
    "source_authority_sha256",
}


def _recovery_source_paths(repository_root: Path) -> dict[str, Path]:
    benchmark_source = repository_root / "benchmarks/chemcrow/src/openevo_chemcrow"
    return {
        "codex_harness": repository_root / "src/openevo/harness/presets/codex.py",
        "codex_isolation": repository_root / "src/openevo/runtime/codex_isolation.py",
        "checkpoint_recovery": benchmark_source / "checkpoint_recovery.py",
        "compatible_evaluation": benchmark_source / "compatible_evaluation.py",
        "cli": benchmark_source / "cli.py",
        "evolved_candidate_recovery": benchmark_source / "evolved_candidate_recovery.py",
        "feedback": benchmark_source / "feedback.py",
        "gateway_node": repository_root / "src/openevo/gateway/node.py",
        "hashing": benchmark_source / "hashing.py",
        "ledger": benchmark_source / "ledger.py",
        "models": benchmark_source / "models.py",
        "replacement_ledger": benchmark_source / "replacement_ledger.py",
        "runtime": benchmark_source / "runtime.py",
        "tasks": benchmark_source / "tasks.py",
        "three_artifact_evolution": benchmark_source / "three_artifact_evolution.py",
        "three_artifact_models": benchmark_source / "three_artifact_models.py",
        "three_artifact_protocol": benchmark_source / "three_artifact_protocol.py",
        "three_artifact_runtime": benchmark_source / "three_artifact_runtime.py",
    }


def _task_authority(task: TaskItem) -> dict[str, Any]:
    payload = task.model_dump(mode="json")
    body = dict(payload)
    stored_sha256 = str(body.pop("sanitized_item_sha256"))
    if canonical_sha256(body) != stored_sha256:
        raise ValueError("task sanitized-item hash differs from the full canonical body")
    return payload


def _reflector_task_evidence(task: TaskItem) -> dict[str, str]:
    _task_authority(task)
    return {
        "task_id": task.task_id,
        "prompt": task.prompt,
        "sanitized_item_sha256": task.sanitized_item_sha256,
    }


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != serialized:
            raise ValueError(f"existing immutable recovery evidence differs: {path.name}")
        return
    with path.open("x", encoding="utf-8") as handle:
        handle.write(serialized)


def _entry_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _recovery_receipt_path(recovery_root: Path, *, attempt_ordinal: int) -> Path:
    if attempt_ordinal < 1:
        raise ValueError("evolved Candidate recovery attempt ordinal is invalid")
    return recovery_root / (
        _RECEIPT_NAME
        if attempt_ordinal == 1
        else f"evolved-candidate-no-effect.recovery.attempt-{attempt_ordinal}.json"
    )


def _require_claim_hashes(claim: dict[str, Any], *, phase: str) -> None:
    authority = claim.get("authority")
    if (
        claim.get("phase") != phase
        or claim.get("schema_version")
        not in {"chemcrow_phase_claim_v1", "chemcrow_phase_claim_v2"}
        or not isinstance(authority, dict)
        or claim.get("authority_sha256") != canonical_sha256(authority)
    ):
        raise ValueError(f"phase claim authority differs: {phase}")
    if claim.get("status") == "terminal":
        receipt = claim.get("receipt")
        if not isinstance(receipt, dict) or claim.get("receipt_sha256") != canonical_sha256(
            receipt
        ):
            raise ValueError(f"terminal phase receipt differs: {phase}")


def _completion_path(root: Path, *, run_id: str) -> Path:
    if not root.exists():
        raise ValueError("Core completion root is absent")
    root_entry = root.lstat()
    if not stat.S_ISDIR(root_entry.st_mode) or stat.S_ISLNK(root_entry.st_mode):
        raise ValueError("Core completion root is not a no-follow directory")
    directory = root / f"task_{run_id}"
    if not directory.exists():
        raise ValueError(f"Core completion inventory differs for run: {run_id}")
    directory_entry = directory.lstat()
    if not stat.S_ISDIR(directory_entry.st_mode) or stat.S_ISLNK(directory_entry.st_mode):
        raise ValueError("Core completion task directory is not a no-follow directory")
    paths = sorted(directory.glob("*.json"))
    if len(paths) != 1:
        raise ValueError(f"Core completion inventory differs for run: {run_id}")
    _read_regular_file(paths[0], allowed_root=root)
    return paths[0]


def _evolved_completion_inventory(root: Path, *, pair_id: str) -> dict[str, Path]:
    if not root.exists():
        raise ValueError("Core completion root is absent")
    root_entry = root.lstat()
    if not stat.S_ISDIR(root_entry.st_mode) or stat.S_ISLNK(root_entry.st_mode):
        raise ValueError("Core completion root is not a no-follow directory")
    pattern = re.compile(rf"task_(?P<run_id>{re.escape(pair_id)}-evolved-[0-9a-f]{{10}})")
    inventory: dict[str, Path] = {}
    for entry in root.iterdir():
        match = pattern.fullmatch(entry.name)
        if match is None:
            continue
        run_id = match.group("run_id")
        inventory[run_id] = _completion_path(root, run_id=run_id)
    return inventory


def _new_replacement_run_id(
    *,
    pair_id: str,
    attempt_ordinal: int,
    failed_completion_sha256: str,
    claim_authority_sha256: str,
    completion_root: Path,
    forbidden_run_ids: set[str],
) -> str:
    suffix = canonical_sha256(
        {
            "schema_version": "chemcrow_evolved_candidate_replacement_run_id_v1",
            "pair_id": pair_id,
            "attempt_ordinal": attempt_ordinal,
            "failed_completion_sha256": failed_completion_sha256,
            "claim_authority_sha256": claim_authority_sha256,
        }
    )[:10]
    run_id = f"{pair_id}-evolved-{suffix}"
    if run_id in forbidden_run_ids or _entry_exists_no_follow(completion_root / f"task_{run_id}"):
        raise ValueError("preallocated evolved Candidate replacement run ID is not fresh")
    return run_id


def _single_assistant_answer(completion: dict[str, Any]) -> str:
    trajectory = completion.get("trajectory")
    traces = trajectory.get("traces") if isinstance(trajectory, dict) else None
    if not isinstance(traces, list) or len(traces) != 1 or not isinstance(traces[0], dict):
        raise ValueError("completed scientific call does not contain exactly one trace")
    answers = [
        _message_content(message).strip()
        for message in traces[0].get("response_messages", [])
        if isinstance(message, dict)
        and message.get("role") == "assistant"
        and _message_content(message).strip()
    ]
    if len(answers) != 1:
        raise ValueError("completed scientific call final answer inventory differs")
    return answers[0]


def _single_user_prompt(completion: dict[str, Any]) -> str:
    trajectory = completion.get("trajectory")
    traces = trajectory.get("traces") if isinstance(trajectory, dict) else None
    prompt_messages = (
        traces[0].get("prompt_messages")
        if isinstance(traces, list) and len(traces) == 1 and isinstance(traces[0], dict)
        else None
    )
    if (
        not isinstance(prompt_messages, list)
        or len(prompt_messages) != 1
        or not isinstance(prompt_messages[0], dict)
        or prompt_messages[0].get("role") != "user"
    ):
        raise ValueError("completed scientific call prompt inventory differs")
    return _message_content(prompt_messages[0])


def _require_exact_prompt(completion: dict[str, Any], *, expected: str) -> str:
    actual = _single_user_prompt(completion)
    if actual != expected:
        raise ValueError("completed scientific call prompt differs from frozen authority")
    return text_sha256(actual)


def _require_evaluator_prompt(completion: dict[str, Any], *, expected: str) -> str:
    """Bind the prompt while tolerating JSON object-key reordering only."""

    actual = _single_user_prompt(completion)
    marker = "OBSERVABLE TOOL EVIDENCE:\n"
    if actual.count(marker) != 1 or expected.count(marker) != 1:
        raise ValueError("evaluator prompt evidence boundary is invalid")
    actual_prefix, actual_evidence = actual.split(marker, 1)
    expected_prefix, expected_evidence = expected.split(marker, 1)
    decoder = json.JSONDecoder()
    try:
        decoded_actual, actual_end = decoder.raw_decode(actual_evidence)
        decoded_expected, expected_end = decoder.raw_decode(expected_evidence)
    except json.JSONDecodeError as exc:
        raise ValueError("evaluator prompt evidence JSON is invalid") from exc
    if (
        actual_prefix != expected_prefix
        or decoded_actual != decoded_expected
        or actual_evidence[actual_end:] != expected_evidence[expected_end:]
    ):
        raise ValueError("evaluator prompt differs from frozen semantic authority")
    return text_sha256(actual)


def _open_no_follow_regular(
    path: Path,
    *,
    allowed_root: Path,
) -> tuple[int, int, str, os.stat_result]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("no-follow filesystem verification is unavailable")
    root = Path(os.path.abspath(allowed_root))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("recovery payload escaped the Core artifact root") from exc
    if not relative.parts:
        raise ValueError("recovery payload cannot be the allowed directory root")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_descriptor = os.open("/", directory_flags)
    try:
        for component in (*root.parts[1:], *relative.parts[:-1]):
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        final_name = relative.parts[-1]
        file_descriptor = os.open(
            final_name,
            file_flags,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError as exc:
        os.close(directory_descriptor)
        raise ValueError(f"recovery payload is absent: {path.name}") from exc
    except OSError as exc:
        os.close(directory_descriptor)
        raise ValueError(
            f"recovery payload is not a no-follow link-count-one regular file: {path.name}"
        ) from exc
    entry = os.fstat(file_descriptor)
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        os.close(file_descriptor)
        os.close(directory_descriptor)
        raise ValueError(
            f"recovery payload is not a no-follow link-count-one regular file: {path.name}"
        )
    return file_descriptor, directory_descriptor, final_name, entry


def _stable_file_identity(entry: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        entry.st_dev,
        entry.st_ino,
        entry.st_nlink,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    )


@contextmanager
def _held_regular_file(
    path: Path,
    *,
    allowed_root: Path,
) -> Iterator[tuple[int, os.stat_result]]:
    descriptor, parent_descriptor, final_name, initial = _open_no_follow_regular(
        path,
        allowed_root=allowed_root,
    )
    try:
        yield descriptor, initial
        held_after = os.fstat(descriptor)
        parent_after = os.stat(
            final_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        reopened, reopened_parent, _, reopened_entry = _open_no_follow_regular(
            path,
            allowed_root=allowed_root,
        )
        try:
            identities = {
                _stable_file_identity(initial),
                _stable_file_identity(held_after),
                _stable_file_identity(parent_after),
                _stable_file_identity(reopened_entry),
            }
            if len(identities) != 1:
                raise ValueError("recovery payload identity changed during verification")
        finally:
            os.close(reopened)
            os.close(reopened_parent)
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)


def _read_regular_file(path: Path, *, allowed_root: Path) -> bytes:
    with _held_regular_file(path, allowed_root=allowed_root) as (descriptor, entry):
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        if len(payload) != entry.st_size:
            raise ValueError("recovery payload size changed during verification")
        return payload


def _read_regular_json(path: Path, *, allowed_root: Path) -> dict[str, Any]:
    payload = json.loads(_read_regular_file(path, allowed_root=allowed_root))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON object required: {path}")
    return payload


def _artifact_uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("recovered artifact URI is not a local file URI")
    return Path(unquote(parsed.path))


def _load_store_rows(db_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with _held_regular_file(db_path, allowed_root=db_path.parent):
        uri = f"file:{Path(os.path.abspath(db_path))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            jobs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT job_id, job_type, method, state, lease_id, error,
                           attempt_count, input_artifact_ids_json, config_json
                    FROM jobs
                    WHERE job_type = 'chemcrow_task_local_isolated_reflection'
                    """
                )
            ]
            artifacts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT artifact_id, type, name, version, state, uri, manifest_path,
                           manifest_json, lineage_json, compatibility_json, scores_json,
                           tags_json, promoted, staging_job_id
                    FROM artifacts
                    """
                )
            ]
    for job in jobs:
        job["config"] = json.loads(str(job.pop("config_json")))
        job["input_artifact_ids"] = json.loads(str(job.pop("input_artifact_ids_json")))
    for artifact in artifacts:
        for raw_key, parsed_key in (
            ("manifest_json", "manifest"),
            ("lineage_json", "lineage"),
            ("compatibility_json", "compatibility"),
            ("scores_json", "scores"),
            ("tags_json", "tags"),
        ):
            artifact[parsed_key] = json.loads(str(artifact.pop(raw_key)))
    return jobs, artifacts


def _manifest_file_payload(artifact: dict[str, Any], *, artifact_root: Path) -> dict[str, Any]:
    manifest_path = Path(str(artifact.get("manifest_path")))
    payload = _read_regular_json(manifest_path, allowed_root=artifact_root)
    expected = {
        "artifact_id": artifact["artifact_id"],
        "type": artifact["type"],
        "name": artifact["name"],
        "uri": artifact["uri"],
        "manifest": artifact["manifest"],
        "lineage": artifact["lineage"],
        "compatibility": artifact["compatibility"],
        "scores": artifact["scores"],
        "tags": artifact["tags"],
        "promoted": bool(artifact["promoted"]),
    }
    if payload != expected:
        raise ValueError("artifact manifest file differs from the Core store row")
    return payload


def _expected_manifest(
    *,
    kind: ArtifactKind,
    task: TaskItem,
    pair_id: str,
    parent_run_id: str,
    dataset_id: str,
    dataset_uri: str,
    job_id: str,
    reflector_run_id: str,
    model: str,
    prompt_hash: str,
    system_hash: str,
    evidence_hash: str,
    reflector_attempt_run_ids: list[str],
    reflector_output_audit: dict[str, object],
) -> dict[str, Any]:
    method = _METHODS[kind]
    payload: dict[str, Any] = {
        "protocol": CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
        "method": method,
        "task_id": task.task_id,
        "pair_id": pair_id,
        "parent_run_id": parent_run_id,
        "source_dataset_artifact_id": dataset_id,
        "source_dataset_uri": dataset_uri,
        "reflector_job_id": job_id,
        "reflector_run_id": reflector_run_id,
        "reflector_model": model,
        "reflector_prompt_sha256": prompt_hash,
        "reflector_system_sha256": system_hash,
        "reflector_prompt_profile": CORE_ROLE_WRAPPER_PROMPT_PROFILE,
        "reflector_attempt_run_ids": reflector_attempt_run_ids,
        "reflector_output_audit": reflector_output_audit,
        "input_evidence_sha256": evidence_hash,
        "execution_route": CORE_MANAGED_CODEX_ROUTE,
        "sibling_outputs_visible": False,
        "historical_answers_included": False,
        "paper_evaluator_feedback_included": False,
    }
    if kind is ArtifactKind.TEXT_MEMORY:
        return {"content_path": "memory.md", **payload}
    if kind is ArtifactKind.SKILL_BUNDLE:
        return {"entrypoint": "SKILL.md", "files": ["SKILL.md"], **payload}
    return {"content_path": "AGENTS.md", "target_path": "AGENTS.md", **payload}


def _event_evidence(
    *,
    event_path: Path,
    kind: ArtifactKind,
    task: TaskItem,
    pair_id: str,
    baseline_run_id: str,
    expected_model: str,
    artifact_root: Path,
) -> dict[str, Any]:
    event = _read_regular_json(event_path, allowed_root=artifact_root)
    if (
        event.get("source") != "openevo-chemcrow-three-isolated"
        or event.get("event_type") != "openevo.session_completed"
        or event.get("source_event_id") != f"{pair_id}-{kind.value}-baseline-feedback"
        or event.get("task_id") != task.task_id
        or event.get("session_id") != baseline_run_id
        or event.get("policy_version") != "chemcrow-task-local-three-isolated-core-native-v2"
        or event.get("rollout_step") != 0
        or event.get("status") != "COMPLETED"
        or event.get("base_model") != expected_model
        or event.get("agent") != {"harness": "chemcrow-adapter", "reflector_role": kind.value}
    ):
        raise ValueError("Core evidence event authority differs")
    session_result = event.get("payload", {}).get("session_result")
    metadata = session_result.get("metadata") if isinstance(session_result, dict) else None
    evidence = metadata.get("input_evidence") if isinstance(metadata, dict) else None
    evidence_hash = metadata.get("input_evidence_sha256") if isinstance(metadata, dict) else None
    if not isinstance(evidence, dict) or canonical_sha256(evidence) != evidence_hash:
        raise ValueError("Core evidence event payload hash differs")
    return evidence


def _load_frozen_evidence(
    *,
    evidence_hash: str,
    evolution_db_path: Path,
    evolution_artifact_root: Path,
) -> dict[str, Any]:
    jobs, artifacts = _load_store_rows(evolution_db_path)
    event_candidates: list[Path] = []
    for job in jobs:
        if job.get("config", {}).get("input_evidence_sha256") != evidence_hash:
            continue
        inputs = job.get("input_artifact_ids")
        if not isinstance(inputs, list) or len(inputs) != 1:
            continue
        dataset = next(
            (item for item in artifacts if item.get("artifact_id") == inputs[0]),
            None,
        )
        event_ids = dataset.get("manifest", {}).get("event_ids") if dataset else None
        if isinstance(event_ids, list) and len(event_ids) == 1:
            event_candidates.append(evolution_artifact_root / "events" / f"{event_ids[0]}.json")
    unique_events = set(event_candidates)
    if len(event_candidates) != 3 or len(unique_events) != 3:
        raise ValueError("Reflector evidence event inventory is not exactly three")
    first_event_path = min(unique_events)
    first_event = _read_regular_json(
        first_event_path,
        allowed_root=evolution_artifact_root,
    )
    session_result = first_event.get("payload", {}).get("session_result", {})
    evidence = session_result.get("metadata", {}).get("input_evidence")
    if not isinstance(evidence, dict) or canonical_sha256(evidence) != evidence_hash:
        raise ValueError("Reflector evidence checkpoint is invalid")
    return evidence


def _validate_baseline_and_evaluator(
    *,
    claims: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
    task: TaskItem,
    candidate_config_sha256: str,
    evaluator_config: dict[str, Any],
    evaluator_id: str,
    feedback_mode: FeedbackMode,
    core_completion_root: Path,
) -> tuple[Trajectory, EvaluatorFeedback, dict[str, Any]]:
    expected_task = _reflector_task_evidence(task)
    if evidence.get("task") != expected_task:
        raise ValueError("recovered evidence task authority differs")
    baseline = Trajectory.model_validate(evidence.get("baseline"))
    feedback_payload = evidence.get("feedback")
    if not isinstance(feedback_payload, dict):
        raise TypeError("recovered Reflector feedback payload is absent")
    evaluation = EvaluatorFeedback.model_validate(feedback_payload.get("evaluator_feedback"))
    baseline_claim = claims["baseline_candidate"]
    evaluator_claim = claims["baseline_internal_evaluator"]
    baseline_receipt = baseline_claim["receipt"]
    evaluator_receipt = evaluator_claim["receipt"]
    evaluator_authority = evaluator_claim["authority"]
    if (
        baseline.task_id != task.task_id
        or baseline.role != "baseline"
        or baseline.status != "COMPLETED"
        or not baseline.answer.strip()
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != candidate_config_sha256
        or baseline_claim["authority"].get("artifact_ids") != []
        or baseline_claim["authority"].get("artifact_inventory") != {}
        or baseline_receipt.get("run_id") != baseline.run_id
        or baseline_receipt.get("runtime_context") != "bare_s0"
        or baseline_receipt.get("trajectory_sha256")
        != canonical_sha256(baseline.model_dump(mode="json"))
        or evaluation.evaluator_role != "evolution_evaluator"
        or evaluator_authority.get("task_id") != task.task_id
        or evaluator_authority.get("run_id") != baseline.run_id
        or evaluator_authority.get("evaluator_id") != evaluator_id
        or evaluator_authority.get("paper_evaluator") is not False
        or evaluator_receipt.get("evaluator_run_id") != evaluation.evaluator_run_id
        or evaluator_receipt.get("feedback_sha256")
        != canonical_sha256(evaluation.model_dump(mode="json"))
    ):
        raise ValueError("recovered G1/evaluator checkpoint authority differs")
    expected_feedback = reflector_feedback_payload(
        mode=feedback_mode,
        trajectory=baseline,
        runtime=runtime_feedback(baseline),
        evaluator=evaluation,
    )
    if feedback_payload != expected_feedback:
        raise ValueError("recovered Reflector feedback differs from the frozen protocol")

    baseline_completion_path = _completion_path(core_completion_root, run_id=baseline.run_id)
    baseline_completion = _load_core_completion(
        baseline_completion_path, expected_run_id=baseline.run_id
    )
    if _single_assistant_answer(baseline_completion) != baseline.answer:
        raise ValueError("baseline Core completion answer differs from frozen evidence")

    evaluator_completion_path = _completion_path(
        core_completion_root, run_id=evaluation.evaluator_run_id
    )
    evaluator_completion = _load_core_completion(
        evaluator_completion_path,
        expected_run_id=evaluation.evaluator_run_id,
    )
    judge_task = build_evolution_judge_task(task=task, trajectory=baseline)
    expected_evaluator_request = build_task_request(
        task=judge_task,
        run_id=evaluation.evaluator_run_id,
        role="baseline",
        candidate=evaluator_config,
        artifact_ids=[],
        mcp_url=None,
    )
    evaluator_prompt_hash = _require_evaluator_prompt(
        evaluator_completion,
        expected=expected_evaluator_request["instruction"],
    )
    evaluator_result = _normalize_rollout(
        {"results": [evaluator_completion]},
        run_id=evaluation.evaluator_run_id,
        task_id=judge_task.task_id,
        role="baseline",
        artifact_ids=[],
        config_sha256=canonical_sha256(evaluator_config),
        wall_time=_completion_wall_time(evaluator_completion),
        tool_receipts=[],
        declared_tool_bridge=False,
        polling_retries=0,
    )
    parsed_evaluation, parse_receipt = parse_evolution_feedback_result(evaluator_result)
    if parsed_evaluation != evaluation:
        raise ValueError("evaluator Core completion differs from frozen feedback")
    stored_parse_receipt = evaluator_receipt.get("confidence_parse_receipt")
    if stored_parse_receipt is not None and stored_parse_receipt != parse_receipt:
        raise ValueError("evaluator confidence transport receipt differs")
    completion_authority = {
        "baseline": {
            "path": baseline_completion_path,
            "payload": baseline_completion,
        },
        "baseline_internal_evaluator": {
            "path": evaluator_completion_path,
            "payload": evaluator_completion,
            "prompt_sha256": evaluator_prompt_hash,
        },
    }
    return baseline, evaluation, completion_authority


def _reconstruct_artifact_bundle(
    *,
    claims: dict[str, dict[str, Any]],
    task: TaskItem,
    pair_id: str,
    baseline: Trajectory,
    evidence: dict[str, Any],
    evidence_hash: str,
    reflector_configs: dict[ArtifactKind, dict[str, Any]],
    separation_policy: ArtifactSeparationPolicy,
    evolution_db_path: Path,
    evolution_artifact_root: Path,
    core_completion_root: Path,
) -> tuple[ThreeArtifactBundleReceipt, dict[str, Any]]:
    jobs, artifacts = _load_store_rows(evolution_db_path)
    matching_jobs = [
        job
        for job in jobs
        if isinstance(job.get("config"), dict)
        and job["config"].get("input_evidence_sha256") == evidence_hash
    ]
    if len(matching_jobs) != 3:
        raise ValueError("Core Reflector job inventory is not exactly three")
    job_by_id = {str(job["job_id"]): job for job in matching_jobs}
    artifact_by_id = {str(artifact["artifact_id"]): artifact for artifact in artifacts}
    output_rows = [
        artifact
        for artifact in artifacts
        if isinstance(artifact.get("manifest"), dict)
        and artifact["manifest"].get("input_evidence_sha256") == evidence_hash
        and artifact.get("type") != "dataset"
    ]
    if len(output_rows) != 3:
        raise ValueError("Core registered artifact inventory is not exactly three")

    evidence_by_kind: dict[ArtifactKind, dict[str, Any]] = {}
    content_by_kind: dict[ArtifactKind, str] = {}
    receipts: list[ThreeArtifactReceipt] = []
    authority: dict[str, Any] = {"jobs": {}, "artifacts": {}, "completions": {}, "events": {}}
    for kind in THREE_ARTIFACT_ORDER:
        phase = _REFLECTOR_PHASES[kind]
        claim = claims[phase]
        claim_authority = claim["authority"]
        claim_receipt = claim["receipt"]
        job_id = claim_receipt.get("reflector_job_id")
        artifact_id = claim_receipt.get("artifact_id")
        reflector_run_id = claim_receipt.get("reflector_run_id")
        config = reflector_configs[kind]
        expected_model = str(config["agent"]["model_name"])
        expected_config_hash = s0_config_hash(config)
        if (
            not isinstance(job_id, str)
            or not isinstance(artifact_id, str)
            or not isinstance(reflector_run_id, str)
            or set(job_by_id)
            != {
                str(claims[item]["receipt"]["reflector_job_id"])
                for item in _REFLECTOR_PHASES.values()
            }
        ):
            raise ValueError("Reflector claim job inventory differs")
        job = job_by_id.get(job_id)
        artifact = artifact_by_id.get(artifact_id)
        method = _METHODS[kind]
        prompt = render_core_native_reflector_prompt(kind, evidence)
        prompt_hash = canonical_sha256({"prompt": prompt})
        system_hash = _core_reflector_contract_hash(kind)
        expected_job_config: dict[str, Any] = {
            "name": f"{task.task_id} {kind.value} isolated",
            "promoted": False,
            "tags": [
                "chemcrow",
                "task-local",
                "isolated",
                task.task_id,
                kind.value,
            ],
            "compatibility": {"task_tags": [task.task_id]},
            "reflector_execution_route": CORE_MANAGED_CODEX_ROUTE,
            "reflector_config_sha256": expected_config_hash,
            "reflector_prompt_sha256": prompt_hash,
            "reflector_system_sha256": system_hash,
            "reflector_prompt_authority": "openevo_core_builtin",
            "reflector_prompt_profile": CORE_ROLE_WRAPPER_PROMPT_PROFILE,
            "input_evidence_sha256": evidence_hash,
            "sibling_outputs_visible": False,
        }
        if kind is ArtifactKind.AGENT_SYSTEM:
            expected_job_config["target_path"] = "AGENTS.md"
        if (
            job is None
            or artifact is None
            or job.get("method") != method
            or job.get("state") != "succeeded"
            or job.get("lease_id") is not None
            or job.get("error") is not None
            or job.get("attempt_count") != 1
            or not isinstance(job.get("input_artifact_ids"), list)
            or len(job["input_artifact_ids"]) != 1
            or job.get("config") != expected_job_config
            or claim_authority.get("task_id") != task.task_id
            or claim_authority.get("pair_id") != pair_id
            or claim_authority.get("parent_run_id") != baseline.run_id
            or claim_authority.get("artifact_type") != kind.value
            or claim_authority.get("input_evidence_hash") != evidence_hash
            or claim_authority.get("sibling_artifact_ids") != []
            or claim_authority.get("paper_evaluator_feedback_included") is not False
            or claim_receipt.get("model") != expected_model
            or claim_receipt.get("prompt_hash") != prompt_hash
            or claim_receipt.get("input_evidence_hash") != evidence_hash
            or claim_receipt.get("artifact_type") != kind.value
        ):
            raise ValueError(f"Core Reflector authority differs: {kind.value}")

        dataset_id = str(job["input_artifact_ids"][0])
        dataset = artifact_by_id.get(dataset_id)
        if (
            dataset is None
            or dataset.get("type") != "dataset"
            or dataset.get("state") != "active"
            or dataset.get("staging_job_id") is not None
            or not isinstance(dataset.get("manifest"), dict)
        ):
            raise ValueError("Reflector source dataset authority differs")
        dataset_manifest = dataset["manifest"]
        event_ids = dataset_manifest.get("event_ids")
        query = dataset_manifest.get("query")
        if (
            dataset_manifest.get("event_count") != 1
            or dataset_manifest.get("trace_count") != 1
            or not isinstance(event_ids, list)
            or len(event_ids) != 1
            or not isinstance(query, dict)
            or query.get("source") != "openevo-chemcrow-three-isolated"
            or query.get("source_event_id") != f"{pair_id}-{kind.value}-baseline-feedback"
            or query.get("task_id") != task.task_id
            or query.get("session_id") != baseline.run_id
            or query.get("event_types") != ["openevo.session_completed"]
        ):
            raise ValueError("Reflector source dataset evidence differs")
        event_path = evolution_artifact_root / "events" / f"{event_ids[0]}.json"
        _read_regular_file(event_path, allowed_root=evolution_artifact_root)
        evidence_by_kind[kind] = _event_evidence(
            event_path=event_path,
            kind=kind,
            task=task,
            pair_id=pair_id,
            baseline_run_id=baseline.run_id,
            expected_model=expected_model,
            artifact_root=evolution_artifact_root,
        )
        if canonical_sha256(evidence_by_kind[kind]) != evidence_hash:
            raise ValueError("Reflector event evidence differs from the phase claim")

        expected_manifest = _expected_manifest(
            kind=kind,
            task=task,
            pair_id=pair_id,
            parent_run_id=baseline.run_id,
            dataset_id=dataset_id,
            dataset_uri=str(dataset["uri"]),
            job_id=job_id,
            reflector_run_id=reflector_run_id,
            model=expected_model,
            prompt_hash=prompt_hash,
            system_hash=system_hash,
            evidence_hash=evidence_hash,
            reflector_attempt_run_ids=[reflector_run_id],
            reflector_output_audit={},
        )
        expected_lineage = {
            **{
                key: value
                for key, value in expected_manifest.items()
                if key not in {"content_path", "entrypoint", "files", "target_path"}
            },
            "input_artifact_ids": [dataset_id],
        }
        expected_tags = [
            "chemcrow",
            "task-local",
            "three-isolated-core-native-v2",
            task.task_id,
            kind.value,
        ]
        if (
            artifact.get("type") != kind.value
            or artifact.get("name") != f"{task.task_id} isolated {kind.value}"
            or artifact.get("version") != 1
            or artifact.get("state") != "active"
            or artifact.get("staging_job_id") is not None
            or artifact.get("manifest") != expected_manifest
            or artifact.get("lineage") != expected_lineage
            or artifact.get("compatibility") != {"task_tags": [task.task_id]}
            or artifact.get("scores") != {}
            or artifact.get("tags") != expected_tags
            or artifact.get("promoted") != 0
        ):
            raise ValueError(f"registered artifact authority differs: {kind.value}")
        _manifest_file_payload(artifact, artifact_root=evolution_artifact_root)

        output_root = evolution_artifact_root / "workers" / job_id / method
        content_path = output_root / _CONTENT_NAMES[kind]
        expected_uri_path = output_root if kind is ArtifactKind.SKILL_BUNDLE else content_path
        if _artifact_uri_path(str(artifact["uri"])).resolve() != expected_uri_path.resolve():
            raise ValueError("registered artifact URI differs from the worker output")
        content_bytes = _read_regular_file(
            content_path,
            allowed_root=evolution_artifact_root,
        )
        content = content_bytes.decode("utf-8")

        reflector_completion_path = _completion_path(core_completion_root, run_id=reflector_run_id)
        reflector_completion = _load_core_completion(
            reflector_completion_path,
            expected_run_id=reflector_run_id,
        )
        reflected_task = task.model_copy(
            update={
                "task_id": f"{task.task_id}-{phase}",
                "prompt": prompt,
                "sanitized_item_sha256": canonical_sha256({"prompt": prompt}),
            }
        )
        expected_request = build_task_request(
            task=reflected_task,
            run_id=reflector_run_id,
            role="baseline",
            candidate=config,
            artifact_ids=[],
            mcp_url=None,
        )
        _require_exact_prompt(
            reflector_completion,
            expected=expected_request["instruction"],
        )
        reflected = _normalize_rollout(
            {"results": [reflector_completion]},
            run_id=reflector_run_id,
            task_id=reflected_task.task_id,
            role="baseline",
            artifact_ids=[],
            config_sha256=expected_config_hash,
            wall_time=_completion_wall_time(reflector_completion),
            tool_receipts=[],
            declared_tool_bridge=False,
            polling_retries=0,
        )
        reflected_content = _strict_reflector_markdown(reflected.answer).rstrip() + "\n"
        if content != reflected_content:
            raise ValueError("registered artifact bytes differ from the Reflector completion")
        registration_receipt = {
            "job_id": job_id,
            "state": "succeeded",
            "artifact_ids": [artifact_id],
        }
        artifact_hash = hashlib.sha256(content_bytes).hexdigest()
        registration_hash = canonical_sha256(registration_receipt)
        if (
            claim_receipt.get("artifact_hash") != artifact_hash
            or claim_receipt.get("registration_receipt_sha256") != registration_hash
        ):
            raise ValueError("Reflector terminal receipt differs from Core registration")
        content_by_kind[kind] = content
        receipts.append(
            ThreeArtifactReceipt(
                task_id=task.task_id,
                pair_id=pair_id,
                parent_run_id=baseline.run_id,
                reflector_job_id=job_id,
                reflector_run_id=reflector_run_id,
                model=expected_model,
                system_prompt_hash=system_hash,
                prompt_hash=prompt_hash,
                input_evidence_hash=evidence_hash,
                artifact_type=kind,
                artifact_id=artifact_id,
                artifact_hash=artifact_hash,
                normalized_text_hash=canonical_sha256(
                    {"normalized_text": normalize_artifact_text(content)}
                ),
                size_bytes=len(content_bytes),
                generation_time_seconds=_completion_wall_time(reflector_completion),
                registration_receipt_sha256=registration_hash,
            )
        )
        authority["jobs"][kind.value] = {
            "sha256": canonical_sha256(job),
            "job_id": job_id,
        }
        authority["artifacts"][kind.value] = {
            "sha256": canonical_sha256(artifact),
            "artifact_id": artifact_id,
            "content_sha256": artifact_hash,
        }
        authority["completions"][kind.value] = {
            "sha256": file_sha256(reflector_completion_path),
            "session_id_sha256": text_sha256(str(reflector_completion["session_id"])),
        }
        authority["events"][kind.value] = {
            "sha256": file_sha256(event_path),
        }

    if any(value != evidence for value in evidence_by_kind.values()):
        raise ValueError("three Reflectors do not share byte-equivalent frozen evidence")
    duplicate_findings = detect_artifact_duplicates(
        content_by_kind,
        baseline_answer=baseline.answer,
        policy=separation_policy,
    )
    if any(duplicate_findings.values()):
        raise ValueError("recovered artifact bundle fails the frozen duplicate guard")
    bundle = ThreeArtifactBundleReceipt(
        task_id=task.task_id,
        pair_id=pair_id,
        parent_run_id=baseline.run_id,
        input_evidence_hash=evidence_hash,
        separation_policy=separation_policy,
        artifacts=receipts,
        **duplicate_findings,
    )
    return bundle, authority


def _validate_failed_evolved_completion(
    *,
    completion_path: Path,
    completion_root: Path,
    pair_id: str,
    artifact_ids: list[str],
    claim_authority_sha256: str,
    expected_run_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, bool], dict[str, Any]]:
    completion = _read_regular_json(completion_path, allowed_root=completion_root)
    run_id = completion.get("task_id")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(rf"{re.escape(pair_id)}-evolved-[0-9a-f]{{10}}", run_id) is None
        or (expected_run_id is not None and run_id != expected_run_id)
        or _completion_path(completion_root, run_id=run_id).resolve() != completion_path.resolve()
    ):
        raise ValueError("failed evolved Candidate Core completion identity differs")
    trajectory = completion.get("trajectory")
    trajectory_metadata = trajectory.get("metadata") if isinstance(trajectory, dict) else None
    task_metadata = (
        trajectory_metadata.get("task_metadata") if isinstance(trajectory_metadata, dict) else None
    )
    evolution = task_metadata.get("evolution") if isinstance(task_metadata, dict) else None
    metadata = completion.get("metadata")
    top_level_evolution = metadata.get("evolution") if isinstance(metadata, dict) else None
    openevo = metadata.get("openevo") if isinstance(metadata, dict) else None
    task_openevo = task_metadata.get("openevo") if isinstance(task_metadata, dict) else None
    credential_contract = (
        openevo.get(CODEX_SUBSCRIPTION_CONTRACT_KEY) if isinstance(openevo, dict) else None
    )
    expected_revision = "chemcrow-task-local-three:" + canonical_sha256(artifact_ids)
    no_effect_predicates = {
        "core_status_error": completion.get("status") == "ERROR",
        "exact_setup_canary_validation_error": completion.get("error") == _RECOVERABLE_ERROR,
        "trajectory_has_zero_records": isinstance(trajectory_metadata, dict)
        and set(trajectory_metadata) == {"builder", "record_count", "task_metadata"}
        and trajectory_metadata.get("builder") == "agent_transcript"
        and type(trajectory_metadata.get("record_count")) is int
        and trajectory_metadata["record_count"] == 0,
        "trajectory_has_zero_traces": isinstance(trajectory, dict)
        and trajectory.get("traces") == [],
        "workspace_result_absent": completion.get("workspace_result") is None,
        "core_route_bound": isinstance(metadata, dict)
        and task_metadata == metadata
        and metadata.get("execution_route") == CORE_MANAGED_CODEX_ROUTE,
        "host_codex_exec_forbidden": isinstance(metadata, dict)
        and metadata.get("host_codex_exec_forbidden") is True,
        "context_injected_before_setup": isinstance(evolution, dict)
        and top_level_evolution == evolution
        and evolution.get("context_injected") is True,
        "exact_three_context_artifact_ids": isinstance(evolution, dict)
        and evolution.get("context_artifact_ids") == artifact_ids,
        "context_identity_present": isinstance(evolution, dict)
        and isinstance(evolution.get("context_id"), str)
        and bool(evolution.get("context_id")),
        "runtime_injection_receipt_not_published": isinstance(evolution, dict)
        and "runtime_injection_receipt" not in evolution,
        "credential_contract_present": credential_contract == codex_subscription_contract()
        and isinstance(task_openevo, dict)
        and task_openevo.get(CODEX_SUBSCRIPTION_CONTRACT_KEY) == codex_subscription_contract(),
        "credential_readiness_receipt_not_published": isinstance(openevo, dict)
        and CODEX_SUBSCRIPTION_READINESS_KEY not in openevo
        and isinstance(task_openevo, dict)
        and CODEX_SUBSCRIPTION_READINESS_KEY not in task_openevo,
        "revision_authority_exact": isinstance(openevo, dict)
        and openevo.get("revision_id") == expected_revision,
        "session_identity_present": isinstance(completion.get("session_id"), str)
        and bool(completion.get("session_id"))
        and isinstance(metadata, dict)
        and metadata.get("run_id") == run_id
        and isinstance(task_metadata, dict)
        and task_metadata.get("run_id") == run_id,
    }
    if set(no_effect_predicates.values()) != {True}:
        failed = sorted(key for key, value in no_effect_predicates.items() if not value)
        raise ValueError("evolved Candidate no-effect proof failed: " + ", ".join(failed))
    injection_authority = {
        "evolved_claim_authority_sha256": claim_authority_sha256,
        "context_id": evolution["context_id"],
        "context_artifact_ids": artifact_ids,
        "context_injected": True,
        "revision_id": expected_revision,
        "runtime_injection_receipt_published": False,
        "credential_readiness_receipt_published": False,
    }
    return completion, no_effect_predicates, injection_authority


def reconcile_evolved_candidate_no_effect_failure(
    *,
    config_path: Path,
    run_root: Path,
    ledger_root: Path,
    experiment_id: str,
    task: TaskItem,
    candidate_config_sha256: str,
    evaluator_config: dict[str, Any],
    evaluator_id: str,
    feedback_mode: FeedbackMode,
    reflector_configs: dict[ArtifactKind, dict[str, Any]],
    separation_policy: ArtifactSeparationPolicy,
    evolution_db_path: Path,
    evolution_artifact_root: Path,
    core_completion_root: Path,
    failed_core_completion_path: Path,
    repository_root: Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Seal a proven setup-only G2 failure and prepare one replacement G2."""

    pair_id = f"{experiment_id}--{task.task_id}"
    if (run_root / pair_id / "pair.result.json").exists():
        raise ValueError("sealed pair cannot be evolved-Candidate-recovered")
    claim_root = ledger_root / pair_id
    claims = {
        path.stem: _read_regular_json(path, allowed_root=claim_root)
        for path in sorted(claim_root.glob("*.json"))
    }
    if set(claims) != _CLAIM_PHASES:
        raise ValueError("evolved Candidate recovery phase inventory differs")
    for phase, claim in claims.items():
        _require_claim_hashes(claim, phase=phase)
    if (
        any(
            claims[phase].get("status") != "terminal"
            for phase in _CLAIM_PHASES - {"evolved_candidate"}
        )
        or claims["evolved_candidate"].get("status") != "claimed"
    ):
        raise ValueError("evolved Candidate recovery claim states differ")
    evolved_claim = claims["evolved_candidate"]
    prior_attempt_count = (
        verified_no_effect_attempt_count(
            evolved_claim,
            pair_id=pair_id,
            phase="evolved_candidate",
        )
        if evolved_claim.get("schema_version") == "chemcrow_phase_claim_v2"
        else 0
    )
    attempt_ordinal = prior_attempt_count + 1

    evidence_hashes = {
        claims[phase]["authority"].get("input_evidence_hash")
        for phase in _REFLECTOR_PHASES.values()
    }
    if len(evidence_hashes) != 1 or not isinstance(next(iter(evidence_hashes)), str):
        raise ValueError("Reflector input evidence hashes differ")
    evidence_hash = str(next(iter(evidence_hashes)))

    evidence = _load_frozen_evidence(
        evidence_hash=evidence_hash,
        evolution_db_path=evolution_db_path,
        evolution_artifact_root=evolution_artifact_root,
    )

    baseline, evaluation, completed_g1 = _validate_baseline_and_evaluator(
        claims=claims,
        evidence=evidence,
        task=task,
        candidate_config_sha256=candidate_config_sha256,
        evaluator_config=evaluator_config,
        evaluator_id=evaluator_id,
        feedback_mode=feedback_mode,
        core_completion_root=core_completion_root,
    )
    bundle, artifact_authority = _reconstruct_artifact_bundle(
        claims=claims,
        task=task,
        pair_id=pair_id,
        baseline=baseline,
        evidence=evidence,
        evidence_hash=evidence_hash,
        reflector_configs=reflector_configs,
        separation_policy=separation_policy,
        evolution_db_path=evolution_db_path,
        evolution_artifact_root=evolution_artifact_root,
        core_completion_root=core_completion_root,
    )
    artifact_ids = bundle.artifact_ids()
    evolved_authority = evolved_claim["authority"]
    if (
        evolved_authority.get("task_id") != task.task_id
        or evolved_authority.get("s0_hash") != candidate_config_sha256
        or evolved_authority.get("artifact_count") != 3
        or evolved_authority.get("artifact_ids_by_type") != bundle.artifact_id_by_type()
    ):
        raise ValueError("evolved Candidate claim artifact authority differs")
    active_attempt_run_id = evolved_claim.get("active_attempt_run_id")
    if prior_attempt_count > 0 and (
        not isinstance(active_attempt_run_id, str) or not active_attempt_run_id
    ):
        raise ValueError("evolved Candidate active replacement run authority is absent")
    failed_completion, no_effect_predicates, injection_authority = (
        _validate_failed_evolved_completion(
            completion_path=failed_core_completion_path,
            completion_root=core_completion_root,
            pair_id=pair_id,
            artifact_ids=artifact_ids,
            claim_authority_sha256=str(evolved_claim["authority_sha256"]),
            expected_run_id=(str(active_attempt_run_id) if prior_attempt_count > 0 else None),
        )
    )
    current_completion_sha256 = file_sha256(failed_core_completion_path)
    current_session_sha256 = text_sha256(str(failed_completion["session_id"]))
    historical_receipts = [
        attempt.get("receipt", {})
        for attempt in evolved_claim.get("failed_attempts", [])
        if isinstance(attempt, dict)
    ]
    historical_core_run_ids = [
        str(receipt["core_task_id"])
        for receipt in historical_receipts
        if isinstance(receipt.get("core_task_id"), str)
    ]
    historical_replacement_run_ids = [
        str(receipt["replacement_run_id"])
        for receipt in historical_receipts
        if isinstance(receipt.get("replacement_run_id"), str)
    ]
    if (
        failed_completion["task_id"] in historical_core_run_ids
        or (
            prior_attempt_count > 0
            and failed_completion["task_id"] != historical_replacement_run_ids[-1]
        )
        or current_completion_sha256
        in {receipt.get("core_completion_sha256") for receipt in historical_receipts}
        or current_session_sha256
        in {receipt.get("core_session_id_sha256") for receipt in historical_receipts}
    ):
        raise ValueError("evolved Candidate recovery reused historical completion evidence")
    expected_completion_run_ids = set(historical_core_run_ids) | {
        str(failed_completion["task_id"])
    }
    evolved_completion_inventory = _evolved_completion_inventory(
        core_completion_root,
        pair_id=pair_id,
    )
    if set(evolved_completion_inventory) != expected_completion_run_ids:
        raise ValueError("evolved Candidate Core completion inventory is not exhaustive")
    replacement_run_id = _new_replacement_run_id(
        pair_id=pair_id,
        attempt_ordinal=attempt_ordinal,
        failed_completion_sha256=current_completion_sha256,
        claim_authority_sha256=str(evolved_claim["authority_sha256"]),
        completion_root=core_completion_root,
        forbidden_run_ids=set(historical_core_run_ids)
        | set(historical_replacement_run_ids)
        | {str(failed_completion["task_id"])},
    )
    task_authority_sha256 = canonical_sha256(_task_authority(task))

    checkpoint = {
        "schema_version": "chemcrow_evolved_candidate_boundary_checkpoint_v1",
        "status": "READY_FOR_EVOLVED_CANDIDATE_REPLACEMENT_WITHOUT_PRIOR_REDISPATCH",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "task_authority_sha256": task_authority_sha256,
        "baseline": baseline.model_dump(mode="json"),
        "baseline_internal_evaluation": evaluation.model_dump(mode="json"),
        "artifact_bundle": bundle.model_dump(mode="json"),
        "input_evidence_sha256": evidence_hash,
        "artifact_ids_by_type": bundle.artifact_id_by_type(),
        "evolved_claim_authority_sha256": evolved_claim["authority_sha256"],
    }
    recovery_root = run_root / "recovery" / pair_id
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    _write_immutable_json(checkpoint_path, checkpoint)

    repo_root = repository_root or Path(__file__).resolve().parents[4]
    source_paths = _recovery_source_paths(repo_root)
    if any(not path.is_file() for path in source_paths.values()):
        raise ValueError("evolved Candidate recovery source authority is absent")
    phase_receipt = {
        "schema_version": "chemcrow_evolved_candidate_no_effect_recovery_v1",
        "status": "VERIFIED_NO_EVOLVED_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "task_authority_sha256": task_authority_sha256,
        "phase": "evolved_candidate",
        "attempt_ordinal": attempt_ordinal,
        "authority_sha256": evolved_claim["authority_sha256"],
        "original_claim_sha256": file_sha256(claim_root / "evolved_candidate.json"),
        "config_sha256": file_sha256(config_path),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "active_attempt_run_id": failed_completion["task_id"],
        "replacement_run_id": replacement_run_id,
        "core_task_id": failed_completion["task_id"],
        "core_session_id_sha256": current_session_sha256,
        "core_completion_sha256": current_completion_sha256,
        "core_completion_status": "ERROR",
        "error_category": "credential_isolation_validation_failed",
        "input_evidence_sha256": evidence_hash,
        "artifact_ids_by_type": bundle.artifact_id_by_type(),
        "reflector_job_ids": [receipt.reflector_job_id for receipt in bundle.artifacts],
        "reflector_run_ids": [receipt.reflector_run_id for receipt in bundle.artifacts],
        "reflector_prompt_hashes": [receipt.prompt_hash for receipt in bundle.artifacts],
        "injection_authority": injection_authority,
        "no_effect_predicates": no_effect_predicates,
        "infrastructure_canary_model_call_may_have_occurred": True,
        "evolved_candidate_model_call_proven_absent": True,
        "prior_scientific_calls_preserved": [
            "baseline_candidate",
            "baseline_internal_evaluator",
            "reflector_memory",
            "reflector_skill_bundle",
            "reflector_agent_system",
        ],
        "prior_scientific_calls_redispatched": False,
        "replacement_scope": ["evolved_candidate"],
        "duplicate_scientific_call": False,
        "recorded_before_replacement_dispatch": True,
    }
    phase_receipt = validate_evolved_candidate_no_effect_receipt(
        phase_receipt,
        pair_id=pair_id,
    )
    recovery_receipt = {
        "schema_version": "chemcrow_evolved_candidate_boundary_recovery_v1",
        "status": "VERIFIED_EVOLVED_CANDIDATE_ONLY_REPLACEMENT_READY",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "task_authority_sha256": task_authority_sha256,
        "attempt_ordinal": attempt_ordinal,
        "replacement_run_id": replacement_run_id,
        "config_sha256": file_sha256(config_path),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "phase_receipt_sha256": canonical_sha256(phase_receipt),
        "failed_core_completion_sha256": file_sha256(failed_core_completion_path),
        "baseline_core_completion_sha256": file_sha256(completed_g1["baseline"]["path"]),
        "baseline_evaluator_core_completion_sha256": file_sha256(
            completed_g1["baseline_internal_evaluator"]["path"]
        ),
        "baseline_evaluator_prompt_sha256": completed_g1["baseline_internal_evaluator"][
            "prompt_sha256"
        ],
        "reflector_authority": artifact_authority,
        "artifact_ids_by_type": bundle.artifact_id_by_type(),
        "reflector_job_ids": [receipt.reflector_job_id for receipt in bundle.artifacts],
        "reflector_run_ids": [receipt.reflector_run_id for receipt in bundle.artifacts],
        "reflector_prompt_hashes": [receipt.prompt_hash for receipt in bundle.artifacts],
        "input_evidence_sha256": evidence_hash,
        "injection_authority": injection_authority,
        "completed_scientific_calls_reused": 5,
        "prior_scientific_calls_redispatched": False,
        "replacement_scope": ["evolved_candidate"],
        "model_calls_during_reconciliation": 0,
        "generation_time_reconstruction": "core_completion_timing_sum_ms",
        "source_authority_sha256": {
            name: file_sha256(path) for name, path in sorted(source_paths.items())
        },
    }
    recovery_path = _recovery_receipt_path(
        recovery_root,
        attempt_ordinal=attempt_ordinal,
    )
    _write_immutable_json(recovery_path, recovery_receipt)
    VerifiedReplacementPhaseLedger(
        ledger_root, pair_id=pair_id
    ).reconcile_verified_no_effect_failure(
        "evolved_candidate",
        phase_receipt,
    )
    return checkpoint_path, recovery_path, recovery_receipt


def load_evolved_candidate_boundary_checkpoint(
    *,
    config_path: Path,
    run_root: Path,
    ledger_root: Path,
    pair_id: str,
    task: TaskItem,
    expected_candidate_config_sha256: str,
    evaluator_config: dict[str, Any],
    expected_evaluator_id: str,
    feedback_mode: FeedbackMode,
    reflector_configs: dict[ArtifactKind, dict[str, Any]],
    separation_policy: ArtifactSeparationPolicy,
    evolution_db_path: Path,
    evolution_artifact_root: Path,
    core_completion_root: Path | None,
) -> tuple[Trajectory, EvaluatorFeedback, ThreeArtifactBundleReceipt, str] | None:
    recovery_root = run_root / "recovery" / pair_id
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    if not _entry_exists_no_follow(checkpoint_path):
        return None
    if core_completion_root is None:
        raise ValueError("evolved Candidate recovery resume requires --core-completions authority")
    recovery_root_entry = recovery_root.lstat()
    if not stat.S_ISDIR(recovery_root_entry.st_mode) or stat.S_ISLNK(recovery_root_entry.st_mode):
        raise ValueError("evolved Candidate recovery root is not a no-follow directory")
    checkpoint = _read_regular_json(checkpoint_path, allowed_root=recovery_root)

    claim_root = ledger_root / pair_id
    if not _entry_exists_no_follow(claim_root):
        raise ValueError("evolved Candidate checkpoint ledger is absent")
    claim_root_entry = claim_root.lstat()
    if not stat.S_ISDIR(claim_root_entry.st_mode) or stat.S_ISLNK(claim_root_entry.st_mode):
        raise ValueError("evolved Candidate checkpoint ledger root is invalid")
    claims = {
        path.stem: _read_regular_json(path, allowed_root=claim_root)
        for path in sorted(claim_root.glob("*.json"))
    }
    if set(claims) != _CLAIM_PHASES:
        raise ValueError("evolved Candidate checkpoint claim inventory differs")
    for phase, claim in claims.items():
        _require_claim_hashes(claim, phase=phase)
    if (
        any(
            claims[phase].get("status") != "terminal"
            for phase in _CLAIM_PHASES - {"evolved_candidate"}
        )
        or claims["evolved_candidate"].get("status") != "replacement_ready"
    ):
        raise ValueError("evolved Candidate replacement is not ready")
    attempt_count = verified_no_effect_attempt_count(
        claims["evolved_candidate"],
        pair_id=pair_id,
        phase="evolved_candidate",
    )
    failed_attempts = claims["evolved_candidate"].get("failed_attempts")
    if not isinstance(failed_attempts, list) or len(failed_attempts) != attempt_count:
        raise ValueError("evolved Candidate failed-attempt inventory differs")
    expected_recovery_paths = [
        _recovery_receipt_path(recovery_root, attempt_ordinal=ordinal)
        for ordinal in range(1, attempt_count + 1)
    ]
    expected_recovery_names = {
        _CHECKPOINT_NAME,
        *(path.name for path in expected_recovery_paths),
    }
    if {entry.name for entry in recovery_root.iterdir()} != expected_recovery_names:
        raise ValueError("evolved Candidate recovery receipt inventory differs")
    phase_receipts = [
        validate_evolved_candidate_no_effect_receipt(
            attempt["receipt"],
            pair_id=pair_id,
        )
        for attempt in failed_attempts
    ]
    recoveries = [
        _read_regular_json(path, allowed_root=recovery_root) for path in expected_recovery_paths
    ]

    _read_regular_file(config_path, allowed_root=config_path.parent)
    current_config_sha256 = file_sha256(config_path)
    repo_root = Path(__file__).resolve().parents[4]
    source_paths = _recovery_source_paths(repo_root)
    for path in source_paths.values():
        _read_regular_file(path, allowed_root=repo_root)
    expected_source_authority = {
        name: file_sha256(path) for name, path in sorted(source_paths.items())
    }
    task_authority_sha256 = canonical_sha256(_task_authority(task))
    checkpoint_sha256 = file_sha256(checkpoint_path)
    if (
        set(checkpoint) != _CHECKPOINT_KEYS
        or checkpoint.get("schema_version") != "chemcrow_evolved_candidate_boundary_checkpoint_v1"
        or checkpoint.get("status")
        != "READY_FOR_EVOLVED_CANDIDATE_REPLACEMENT_WITHOUT_PRIOR_REDISPATCH"
        or checkpoint.get("pair_id") != pair_id
        or checkpoint.get("task_id") != task.task_id
        or checkpoint.get("task_authority_sha256") != task_authority_sha256
    ):
        raise ValueError("evolved Candidate recovery checkpoint authority differs")
    for ordinal, (phase_receipt, recovery) in enumerate(
        zip(phase_receipts, recoveries, strict=True),
        start=1,
    ):
        if (
            set(recovery) != _RECOVERY_RECEIPT_KEYS
            or recovery.get("schema_version") != "chemcrow_evolved_candidate_boundary_recovery_v1"
            or recovery.get("status") != "VERIFIED_EVOLVED_CANDIDATE_ONLY_REPLACEMENT_READY"
            or recovery.get("pair_id") != pair_id
            or recovery.get("task_id") != task.task_id
            or recovery.get("task_authority_sha256") != task_authority_sha256
            or recovery.get("attempt_ordinal") != ordinal
            or phase_receipt.get("attempt_ordinal") != ordinal
            or phase_receipt.get("task_id") != task.task_id
            or phase_receipt.get("task_authority_sha256") != task_authority_sha256
            or phase_receipt.get("authority_sha256")
            != claims["evolved_candidate"]["authority_sha256"]
            or recovery.get("replacement_run_id") != phase_receipt["replacement_run_id"]
            or recovery.get("config_sha256") != current_config_sha256
            or phase_receipt.get("config_sha256") != current_config_sha256
            or recovery.get("checkpoint_sha256") != checkpoint_sha256
            or phase_receipt.get("checkpoint_sha256") != checkpoint_sha256
            or recovery.get("phase_receipt_sha256") != canonical_sha256(phase_receipt)
            or recovery.get("failed_core_completion_sha256")
            != phase_receipt.get("core_completion_sha256")
            or recovery.get("prior_scientific_calls_redispatched") is not False
            or recovery.get("replacement_scope") != ["evolved_candidate"]
            or recovery.get("model_calls_during_reconciliation") != 0
            or recovery.get("completed_scientific_calls_reused") != 5
            or recovery.get("generation_time_reconstruction") != "core_completion_timing_sum_ms"
            or recovery.get("source_authority_sha256") != expected_source_authority
        ):
            raise ValueError(f"evolved Candidate recovery attempt {ordinal} authority differs")

    baseline = Trajectory.model_validate(checkpoint.get("baseline"))
    evaluation = EvaluatorFeedback.model_validate(checkpoint.get("baseline_internal_evaluation"))
    bundle = ThreeArtifactBundleReceipt.model_validate(checkpoint.get("artifact_bundle"))
    evolved_authority = claims["evolved_candidate"]["authority"]
    expected_revision = "chemcrow-task-local-three:" + canonical_sha256(bundle.artifact_ids())
    if (
        baseline.task_id != task.task_id
        or baseline.role != "baseline"
        or baseline.status != "COMPLETED"
        or not baseline.answer.strip()
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != expected_candidate_config_sha256
        or evaluation.evaluator_role != "evolution_evaluator"
        or claims["baseline_internal_evaluator"]["authority"].get("evaluator_id")
        != expected_evaluator_id
        or bundle.task_id != task.task_id
        or bundle.pair_id != pair_id
        or bundle.parent_run_id != baseline.run_id
        or bundle.separation_policy != separation_policy
        or any(receipt.consumed_by_evolved_run_id is not None for receipt in bundle.artifacts)
        or evolved_authority.get("artifact_ids_by_type") != bundle.artifact_id_by_type()
        or evolved_authority.get("artifact_count") != 3
        or evolved_authority.get("s0_hash") != expected_candidate_config_sha256
        or checkpoint.get("artifact_ids_by_type") != bundle.artifact_id_by_type()
        or checkpoint.get("evolved_claim_authority_sha256")
        != claims["evolved_candidate"]["authority_sha256"]
        or checkpoint.get("input_evidence_sha256") != bundle.input_evidence_hash
    ):
        raise ValueError("evolved Candidate recovery semantic authority differs")
    artifact_ids_by_type = bundle.artifact_id_by_type()
    reflector_job_ids = [receipt.reflector_job_id for receipt in bundle.artifacts]
    reflector_run_ids = [receipt.reflector_run_id for receipt in bundle.artifacts]
    reflector_prompt_hashes = [receipt.prompt_hash for receipt in bundle.artifacts]
    for phase_receipt, recovery in zip(phase_receipts, recoveries, strict=True):
        injection_authority = recovery.get("injection_authority")
        if (
            recovery.get("input_evidence_sha256") != bundle.input_evidence_hash
            or phase_receipt.get("input_evidence_sha256") != bundle.input_evidence_hash
            or recovery.get("artifact_ids_by_type") != artifact_ids_by_type
            or phase_receipt.get("artifact_ids_by_type") != artifact_ids_by_type
            or recovery.get("reflector_job_ids") != reflector_job_ids
            or recovery.get("reflector_run_ids") != reflector_run_ids
            or recovery.get("reflector_prompt_hashes") != reflector_prompt_hashes
            or phase_receipt.get("reflector_job_ids") != reflector_job_ids
            or phase_receipt.get("reflector_run_ids") != reflector_run_ids
            or phase_receipt.get("reflector_prompt_hashes") != reflector_prompt_hashes
            or not isinstance(injection_authority, dict)
            or injection_authority != phase_receipt.get("injection_authority")
            or injection_authority.get("evolved_claim_authority_sha256")
            != claims["evolved_candidate"]["authority_sha256"]
            or not isinstance(injection_authority.get("context_id"), str)
            or not injection_authority["context_id"]
            or injection_authority.get("context_artifact_ids") != bundle.artifact_ids()
            or injection_authority.get("context_injected") is not True
            or injection_authority.get("revision_id") != expected_revision
            or injection_authority.get("runtime_injection_receipt_published") is not False
            or injection_authority.get("credential_readiness_receipt_published") is not False
        ):
            raise ValueError("evolved Candidate recovery semantic authority differs")

    evidence = _load_frozen_evidence(
        evidence_hash=bundle.input_evidence_hash,
        evolution_db_path=evolution_db_path,
        evolution_artifact_root=evolution_artifact_root,
    )
    live_baseline, live_evaluation, completed_g1 = _validate_baseline_and_evaluator(
        claims=claims,
        evidence=evidence,
        task=task,
        candidate_config_sha256=expected_candidate_config_sha256,
        evaluator_config=evaluator_config,
        evaluator_id=expected_evaluator_id,
        feedback_mode=feedback_mode,
        core_completion_root=core_completion_root,
    )
    live_bundle, live_reflector_authority = _reconstruct_artifact_bundle(
        claims=claims,
        task=task,
        pair_id=pair_id,
        baseline=live_baseline,
        evidence=evidence,
        evidence_hash=bundle.input_evidence_hash,
        reflector_configs=reflector_configs,
        separation_policy=separation_policy,
        evolution_db_path=evolution_db_path,
        evolution_artifact_root=evolution_artifact_root,
        core_completion_root=core_completion_root,
    )
    if live_baseline != baseline or live_evaluation != evaluation or live_bundle != bundle:
        raise ValueError("completed pre-G2 scientific authority drifted")
    baseline_completion_sha256 = file_sha256(completed_g1["baseline"]["path"])
    evaluator_completion_sha256 = file_sha256(completed_g1["baseline_internal_evaluator"]["path"])
    evaluator_prompt_sha256 = completed_g1["baseline_internal_evaluator"]["prompt_sha256"]
    for recovery in recoveries:
        if (
            live_reflector_authority != recovery.get("reflector_authority")
            or baseline_completion_sha256 != recovery.get("baseline_core_completion_sha256")
            or evaluator_completion_sha256
            != recovery.get("baseline_evaluator_core_completion_sha256")
            or evaluator_prompt_sha256 != recovery.get("baseline_evaluator_prompt_sha256")
        ):
            raise ValueError("completed pre-G2 scientific authority drifted")

    completion_inventory = _evolved_completion_inventory(
        core_completion_root,
        pair_id=pair_id,
    )
    expected_failed_run_ids = {
        str(phase_receipt["core_task_id"]) for phase_receipt in phase_receipts
    }
    replacement_run_id = str(phase_receipts[-1]["replacement_run_id"])
    replacement_completion_directory = core_completion_root / f"task_{replacement_run_id}"
    if set(completion_inventory) != expected_failed_run_ids or _entry_exists_no_follow(
        replacement_completion_directory
    ):
        raise ValueError("evolved Candidate Core completion inventory is not exhaustive")

    for phase_receipt, recovery in zip(phase_receipts, recoveries, strict=True):
        failed_completion_path = completion_inventory[str(phase_receipt["core_task_id"])]
        failed_completion, no_effect_predicates, live_injection_authority = (
            _validate_failed_evolved_completion(
                completion_path=failed_completion_path,
                completion_root=core_completion_root,
                pair_id=pair_id,
                artifact_ids=bundle.artifact_ids(),
                claim_authority_sha256=str(claims["evolved_candidate"]["authority_sha256"]),
                expected_run_id=str(phase_receipt["core_task_id"]),
            )
        )
        if (
            file_sha256(failed_completion_path) != phase_receipt["core_completion_sha256"]
            or file_sha256(failed_completion_path) != recovery.get("failed_core_completion_sha256")
            or text_sha256(str(failed_completion["session_id"]))
            != phase_receipt["core_session_id_sha256"]
            or no_effect_predicates != phase_receipt["no_effect_predicates"]
            or live_injection_authority != phase_receipt["injection_authority"]
            or live_injection_authority != recovery.get("injection_authority")
        ):
            raise ValueError("failed evolved Candidate completion authority drifted")

    if (
        set(
            _evolved_completion_inventory(
                core_completion_root,
                pair_id=pair_id,
            )
        )
        != expected_failed_run_ids
        or _entry_exists_no_follow(replacement_completion_directory)
        or any(
            file_sha256(completion_inventory[str(receipt["core_task_id"])])
            != receipt["core_completion_sha256"]
            for receipt in phase_receipts
        )
    ):
        raise ValueError("evolved Candidate Core completion authority changed during load")
    return baseline, evaluation, bundle, replacement_run_id


__all__ = [
    "load_evolved_candidate_boundary_checkpoint",
    "reconcile_evolved_candidate_no_effect_failure",
]
