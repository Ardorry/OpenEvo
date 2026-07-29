"""Concentrated, no-model readiness sweep for supervised transfer v2."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2.config import (
    EVOLUTION_CYCLES,
    PROTOCOL_ID,
    TARGETS,
    TRAIN_ROUNDS,
    SupervisedTransferConfigV2,
)
from openevo_chembench.supervised_transfer_v2.reflector_boundary import (
    _SUPERVISED_AGENT_DIRECTIVE_FIELDS,
    _SUPERVISED_AGENT_FIELDS,
    _SUPERVISED_OUTPUT_SCHEMA,
    _SUPERVISED_OUTPUT_SCHEMA_SHA256,
    _SUPERVISED_RULE_SECTION_FIELDS,
    _SUPERVISED_SKILL_FIELDS,
    _SUPERVISED_STRUCTURED_SECTION_FIELDS,
)
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    load_runtime_services_v2,
)
from openevo_chembench.supervised_transfer_v2.source_identity import (
    verify_source_manifest_v2,
)

READINESS_RECEIPT_SCHEMA = "PrePaidReadinessSweepReceiptV2"
READINESS_ROOT_RELATIVE = "reports/chembench_supervised_transfer_v2/readiness"
READINESS_RECEIPT_RELATIVE = (
    f"{READINESS_ROOT_RELATIVE}/prepaid_readiness_sweep_receipt_v2.json"
)
CONTRACT_MATRIX_RELATIVE = f"{READINESS_ROOT_RELATIVE}/reflector_contract_matrix.md"
FAILURE_AUDIT_RELATIVE = f"{READINESS_ROOT_RELATIVE}/fail_closed_branch_audit.md"
HISTORICAL_REPLAY_RELATIVE = f"{READINESS_ROOT_RELATIVE}/historical_failure_replay.md"
HISTORICAL_FIXTURE_RELATIVE = (
    "benchmarks/chembench/tests/supervised_transfer_v2/fixtures/"
    "historical_failure_shapes_v2.json"
)

_FAILURE_BRANCHES = (
    (
        "REFLECTOR_LAST_MESSAGE_INVALID",
        "strict last-message JSON or typed target construction fails",
        "before successor checkpoint; completed reflector output is never retried",
        "test_reflector_contract_v2.py",
    ),
    (
        "REFLECTOR_EVENT_STREAM_INVALID",
        "Codex JSONL extraction or terminal event ordering fails",
        "before last-message admission and before any target job",
        "test_reflector_contract_v2.py",
    ),
    (
        "REFLECTOR_MODEL_TRANSPORT_FAILED",
        "reflector transport ends without a valid completion",
        "no completed reflector output may be reused as a transport retry",
        "test_reflector_contract_v2.py and reflector boundary tests",
    ),
    (
        "SUPERVISED_V2_TASK_SESSION_INVALID",
        "candidate completion or transcript boundary is invalid",
        "no task completion event is admitted",
        "test_executor_v2.py",
    ),
    (
        "TASKWISE_CORE_JOB_FAILED",
        "a plan-bound Core worker job does not complete exactly once",
        "stream becomes terminal; no successor checkpoint",
        "test_core_protocol_v2.py",
    ),
    (
        "TASKWISE_ARTIFACT_VALIDATION_FAILED",
        "memory, skill, or agent-system validator rejects a typed artifact",
        "stream becomes terminal; partial rows cannot become a successor head",
        "test_core_protocol_v2.py",
    ),
    (
        "TASKWISE_ARTIFACT_PROMOTION_FAILED",
        "a validated typed artifact is not promoted exactly once",
        "stream becomes terminal; no successor checkpoint or carry",
        "test_core_protocol_v2.py",
    ),
    (
        "TASKWISE_CONTEXT_RESOLUTION_FAILED",
        "Core cannot resolve the promoted target into a bound context",
        "stream becomes terminal; no successor checkpoint or carry",
        "test_core_protocol_v2.py",
    ),
    (
        "RUNTIME_SERVICE_IDENTITY_MISMATCH",
        "managed Rollout/Gateway identity drifts",
        "rejected before a new immutable session",
        "test_runtime_services_v2.py",
    ),
    (
        "V2_SOURCE_MANIFEST_DRIFT",
        "effective benchmark source differs from the frozen source manifest",
        "paid CLI gate rejects before creating a model request",
        "test_config_split_v2.py",
    ),
    (
        "EXECUTOR_STALLED",
        "the bounded no-completion retry window is exhausted",
        "no new attempt is claimed after the stall deadline",
        "test_core_protocol_v2.py",
    ),
    (
        "FINAL_TEST_LEDGER_DUPLICATE_COMPLETION",
        "a Test UID/arm already owns a valid completion",
        "ledger rejects before another model request",
        "test_final_test_safety_v2.py",
    ),
)


class ReadinessSweepV2Error(RuntimeError):
    """Closed no-model readiness failure."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_static_readiness_reports_v2(repository_root: Path) -> dict[str, str]:
    repository = _repository(repository_root)
    outputs = {
        CONTRACT_MATRIX_RELATIVE: _contract_matrix_markdown(),
        FAILURE_AUDIT_RELATIVE: _failure_audit_markdown(),
        HISTORICAL_REPLAY_RELATIVE: _historical_replay_markdown(repository),
    }
    digests: dict[str, str] = {}
    for relative, content in outputs.items():
        path = repository / relative
        write_public_file(path, content.encode("utf-8"))
        digests[relative] = sha256_bytes(path.read_bytes())
    return digests


def run_prepaid_readiness_sweep_v2(
    *,
    repository_root: Path,
    config: SupervisedTransferConfigV2,
) -> dict[str, object]:
    """Execute the exact non-paid sweep and publish one identity-bound receipt."""

    repository = _repository(repository_root)
    runtime = load_runtime_services_v2(repository_root=repository)
    source = verify_source_manifest_v2(repository)
    historical = _verify_historical_fixtures(repository)
    report_digests = write_static_readiness_reports_v2(repository)
    python = repository / ".venv/bin/python"
    main = repository / "benchmarks/chembench/scripts/supervised_transfer_v2/main.py"
    commands = {
        "targeted_pytest": [
            os.fspath(python),
            "-m",
            "pytest",
            "benchmarks/chembench/tests/supervised_transfer_v2/test_reflector_contract_v2.py",
            (
                "benchmarks/chembench/tests/supervised_transfer_v2/test_core_protocol_v2.py"
                "::test_full_preflight_offline_lifecycle_closes_at_73_28_84"
            ),
            (
                "benchmarks/chembench/tests/supervised_transfer_v2/test_core_protocol_v2.py"
                "::test_second_auxiliary_validator_failure_is_exact_and_never_becomes_a_head"
            ),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        "full_v2_pytest": [
            os.fspath(python),
            "-m",
            "pytest",
            "benchmarks/chembench/tests/supervised_transfer_v2",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        "ruff": [
            os.fspath(repository / ".venv/bin/ruff"),
            "check",
            "--no-cache",
            "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2",
            "benchmarks/chembench/scripts/supervised_transfer_v2",
            "benchmarks/chembench/tests/supervised_transfer_v2",
        ],
        "shell_syntax": [
            "bash",
            "-n",
            "benchmarks/chembench/scripts/supervised_transfer_v2/run.sh",
            "benchmarks/chembench/scripts/supervised_transfer_v2/prepare_runtime.sh",
        ],
        "dry_run": [os.fspath(python), os.fspath(main), "dry-run"],
    }
    command_receipts: dict[str, dict[str, object]] = {}
    for check, command in commands.items():
        command_receipts[check] = _run_no_model_check(repository, command)
        if command_receipts[check]["status"] != "PASS":
            raise ReadinessSweepV2Error("PREPAID_READINESS_CHECK_FAILED")

    split_path = (
        repository
        / "benchmarks/chembench/manifests/supervised_transfer_v2/"
        "split_isolation_receipt_v2.json"
    )
    payload = {
        "schema_version": READINESS_RECEIPT_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "source_commit": _git(repository, "rev-parse", "HEAD"),
        "config_sha256": config.digest,
        "split_sha256": sha256_bytes(split_path.read_bytes()),
        "source_manifest_sha256": source["manifest_sha256"],
        "source_manifest_combined_sha256": source["combined_sha256"],
        "runtime_identity": runtime.digest,
        "runtime_service_run_id": runtime.service_run_id,
        "schema_sha256": _SUPERVISED_OUTPUT_SCHEMA_SHA256,
        "historical_failure_fixtures": historical,
        "contract_tests": command_receipts["targeted_pytest"],
        "boundary_tests": command_receipts["targeted_pytest"],
        "fail_closed_tests": command_receipts["targeted_pytest"],
        "offline_lifecycle_simulation": {
            "status": "PASS",
            "candidate_completions": 73,
            "reflector_completions": 28,
            "core_jobs": 84,
            "typed_artifacts": 84,
            "context_resolutions": 84,
            "text_memory_artifacts": 28,
            "skill_bundle_artifacts": 28,
            "agent_system_artifacts": 28,
            "evidence": command_receipts["targeted_pytest"],
        },
        "targeted_pytest": command_receipts["targeted_pytest"],
        "full_v2_pytest": command_receipts["full_v2_pytest"],
        "ruff": command_receipts["ruff"],
        "shell_syntax": command_receipts["shell_syntax"],
        "dry_run": command_receipts["dry_run"],
        "static_report_sha256": report_digests,
        "protocol_sequence": [
            "Round 0",
            "Evolution Cycle 1",
            "Round 1",
            "Evolution Cycle 2",
            "Round 2",
            "Evolution Cycle 3",
            "Round 3 Final",
        ],
        "known_deterministic_defects": 0,
        "model_calls": 0,
        "created_at_utc": utc_now(),
    }
    path = repository / READINESS_RECEIPT_RELATIVE
    write_public_file(path, canonical_pretty_json_bytes(payload))
    return {
        "status": "PREPAID_READINESS_SWEEP_COMPLETE",
        "receipt": READINESS_RECEIPT_RELATIVE,
        "receipt_sha256": sha256_bytes(path.read_bytes()),
        "runtime_identity": runtime.digest,
        "known_deterministic_defects": 0,
    }


def verify_prepaid_readiness_sweep_v2(
    *,
    repository_root: Path,
    config: SupervisedTransferConfigV2,
) -> dict[str, object]:
    repository = _repository(repository_root)
    runtime = load_runtime_services_v2(repository_root=repository)
    source = verify_source_manifest_v2(repository)
    path = repository / READINESS_RECEIPT_RELATIVE
    try:
        encoded = path.read_bytes()
        payload = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessSweepV2Error("PREPAID_READINESS_RECEIPT_MISSING") from exc
    split_path = (
        repository
        / "benchmarks/chembench/manifests/supervised_transfer_v2/"
        "split_isolation_receipt_v2.json"
    )
    expected = {
        "schema_version": READINESS_RECEIPT_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "source_commit": _git(repository, "rev-parse", "HEAD"),
        "config_sha256": config.digest,
        "split_sha256": sha256_bytes(split_path.read_bytes()),
        "source_manifest_sha256": source["manifest_sha256"],
        "runtime_identity": runtime.digest,
        "runtime_service_run_id": runtime.service_run_id,
        "schema_sha256": _SUPERVISED_OUTPUT_SCHEMA_SHA256,
        "known_deterministic_defects": 0,
        "model_calls": 0,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ReadinessSweepV2Error("PREPAID_READINESS_RECEIPT_INVALID")
    for check in (
        "contract_tests",
        "boundary_tests",
        "fail_closed_tests",
        "offline_lifecycle_simulation",
        "targeted_pytest",
        "full_v2_pytest",
        "ruff",
        "shell_syntax",
        "dry_run",
    ):
        value = payload.get(check)
        if type(value) is not dict or value.get("status") != "PASS":
            raise ReadinessSweepV2Error("PREPAID_READINESS_RECEIPT_INVALID")
    return {
        "status": "PASS",
        "receipt_sha256": sha256_bytes(encoded),
        "runtime_identity": runtime.digest,
    }


def _contract_matrix_markdown() -> str:
    properties = _SUPERVISED_OUTPUT_SCHEMA["properties"]
    required = set(_SUPERVISED_OUTPUT_SCHEMA["required"])
    rows = []
    for heading, field, maximum in _SUPERVISED_STRUCTURED_SECTION_FIELDS:
        schema = properties[field]
        item = schema["items"]
        is_rule = field in _SUPERVISED_RULE_SECTION_FIELDS
        rows.append(
            (
                "text_memory",
                field,
                "yes" if field in required else "no",
                "array<object>" if is_rule else "array<string>",
                str(schema.get("minItems", 0)),
                str(schema["maxItems"]),
                (
                    f"1..{item['maxLength']} chars and UTF-8 bytes"
                    if not is_rule
                    else (
                        "rule scalar fields 1.."
                        f"{item['properties']['trigger']['maxLength']} chars and "
                        "UTF-8 bytes; at most "
                        f"{item['properties']['evidence_digests']['maxItems']} "
                        "evidence digests"
                    )
                ),
                "no",
                "allowed only for the array",
                f"renders ## {heading}; validator enforces rule evidence/capacity",
            )
        )
        assert item["type"] == ("object" if is_rule else "string")
    for field in _SUPERVISED_SKILL_FIELDS:
        schema = properties[field]
        rows.append(
            (
                "skill_bundle",
                field,
                "yes",
                "array<string>",
                str(schema["minItems"]),
                str(schema["maxItems"]),
                (
                    "digest regex"
                    if field.endswith("digests")
                    else f"1..{schema['items']['maxLength']} chars; rendered SKILL.md <=4096 chars"
                ),
                "no",
                "no",
                "SKILL.md builder and target validator require nonempty sections",
            )
        )
    for field in _SUPERVISED_AGENT_FIELDS:
        schema = properties[field]
        rows.append(
            (
                "agent_system",
                field,
                "yes",
                "array<object>" if field == "agent_system_directives" else "array<string>",
                str(schema["minItems"]),
                str(schema["maxItems"]),
                (
                    "directive scalars 1..192 chars; rendered agent system <=4096 chars"
                    if field == "agent_system_directives"
                    else f"1..{schema['items']['maxLength']} chars; rendered agent system <=4096 chars"
                ),
                "no",
                "no",
                "agent-system builder and validator require nonempty directives/discipline",
            )
        )
    lines = [
        "# Reflector Contract Matrix V2",
        "",
        f"Schema SHA-256: `{_SUPERVISED_OUTPUT_SCHEMA_SHA256}`",
        "",
        "Exact protocol sequence: `Round 0 -> Cycle 1 -> Round 1 -> Cycle 2 -> Round 2 -> Cycle 3 -> Round 3 Final`.",
        "",
        "The parser has no defaulting or silent repair. Duplicate keys, unknown fields, nulls, non-finite numbers, wrappers, multiple JSON objects, and malformed/truncated JSON fail closed.",
        "",
        "| target | field | required | type | minItems | maxItems | string contract | null | empty | validator / builder |",
        "|---|---|---:|---|---:|---:|---|---|---|---|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    lines.extend(
        [
            "",
            "## Object contracts",
            "",
            "- Root and nested directive objects use `additionalProperties: false`.",
            "- Rule fields are closed and required; provisional evidence is exactly one, confirmed evidence is at least two, and the parser verifies evidence cardinality and cycle ordering.",
            "- Agent directive fields are exactly: "
            + ", ".join(f"`{field}`" for field in _SUPERVISED_AGENT_DIRECTIVE_FIELDS)
            + ".",
            "- JSON Schema limits Unicode code points; the parser independently caps memory scalars at 128 UTF-8 bytes and the complete rendered memory at 24,576 UTF-8 bytes before Core registration.",
            "- A valid reflector completion is one source response, but it creates three independent plan-bound Core jobs, artifact IDs, digests, validators, promotions, and context resolutions.",
            "",
        ]
    )
    return "\n".join(lines)


def _failure_audit_markdown() -> str:
    lines = [
        "# Fail-Closed Branch Audit V2",
        "",
        "| code | deterministic trigger | write/carry boundary | regression evidence |",
        "|---|---|---|---|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in _FAILURE_BRANCHES)
    lines.extend(
        [
            "",
            "Every listed branch is non-score-driven. Only candidate transport failures explicitly marked `completion_exists=false` are retryable; a reflector completion with invalid content is a terminal protocol failure, not infrastructure retry.",
            "",
        ]
    )
    return "\n".join(lines)


def _historical_replay_markdown(repository: Path) -> str:
    result = _verify_historical_fixtures(repository)
    return "\n".join(
        [
            "# Historical Failure Replay V2",
            "",
            f"Fixture count: {result['fixture_count']}",
            f"Fixture SHA-256: `{result['fixture_sha256']}`",
            "",
            "All fixtures contain only run identity, closed failure code, structural shape, and evidence digest. They contain no question, option, target, completion, feedback, memory, credential, or private event text.",
            "",
            "Replay outcomes are closed to `ACCEPTED_AND_VALID` or `REJECTED_FAIL_CLOSED_WITH_EXPECTED_CODE`; the current historical failures are all expected rejections.",
            "",
        ]
    )


def _verify_historical_fixtures(repository: Path) -> dict[str, object]:
    path = repository / HISTORICAL_FIXTURE_RELATIVE
    try:
        encoded = path.read_bytes()
        payload = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessSweepV2Error("HISTORICAL_FAILURE_FIXTURE_INVALID") from exc
    if type(payload) is not dict:
        raise ReadinessSweepV2Error("HISTORICAL_FAILURE_FIXTURE_INVALID")
    failures = payload.get("real_run_failures")
    if (
        payload.get("privacy") != "content_free_digest_bound"
        or type(failures) is not list
        or not failures
    ):
        raise ReadinessSweepV2Error("HISTORICAL_FAILURE_FIXTURE_INVALID")
    for failure in failures:
        if type(failure) is not dict:
            raise ReadinessSweepV2Error("HISTORICAL_FAILURE_FIXTURE_INVALID")
        state = (
            repository
            / "state/chembench_supervised_transfer_v2/runs"
            / str(failure.get("run_id"))
            / "run_state.json"
        )
        try:
            state_bytes = state.read_bytes()
            state_payload = json.loads(state_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReadinessSweepV2Error("HISTORICAL_FAILURE_EVIDENCE_MISSING") from exc
        if (
            sha256_bytes(state_bytes) != failure.get("run_state_sha256")
            or state_payload.get("status") != "FAIL_CLOSED"
            or state_payload.get("stage") != failure.get("stage")
            or state_payload.get("failure_code") != failure.get("failure_code")
        ):
            raise ReadinessSweepV2Error("HISTORICAL_FAILURE_EVIDENCE_DRIFT")
    return {
        "status": "PASS",
        "fixture_count": len(failures),
        "fixture_sha256": sha256_bytes(encoded),
        "all_evidence_digests_match": True,
        "private_content_copied": False,
    }


def _run_no_model_check(repository: Path, command: list[str]) -> dict[str, object]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=1800,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    duration = time.monotonic() - started
    stdout = completed.stdout
    stderr = completed.stderr
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "returncode": completed.returncode,
        "command_sha256": hashlib.sha256("\0".join(command).encode()).hexdigest(),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "duration_seconds": round(duration, 3),
        "model_calls": 0,
    }


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise TypeError("repository root must be absolute")
    repository = value.resolve(strict=True)
    if not (repository / ".git").is_dir():
        raise ReadinessSweepV2Error("READINESS_REPOSITORY_INVALID")
    if TRAIN_ROUNDS != 4 or EVOLUTION_CYCLES != 3 or TARGETS != (
        "text_memory",
        "skill_bundle",
        "agent_system",
    ):
        raise ReadinessSweepV2Error("READINESS_PROTOCOL_SEQUENCE_INVALID")
    return repository


__all__ = [
    "CONTRACT_MATRIX_RELATIVE",
    "READINESS_RECEIPT_RELATIVE",
    "READINESS_RECEIPT_SCHEMA",
    "ReadinessSweepV2Error",
    "run_prepaid_readiness_sweep_v2",
    "verify_prepaid_readiness_sweep_v2",
    "write_static_readiness_reports_v2",
]
