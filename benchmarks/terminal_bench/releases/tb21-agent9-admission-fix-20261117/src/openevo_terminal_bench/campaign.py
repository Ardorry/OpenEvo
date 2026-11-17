"""Audited G1 -> Evolution -> seven-arm campaign planning and local bookkeeping."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
from typing import Any, Iterable

from openevo_terminal_bench.codex_toolchain import validate_codex_toolchain_bundle


ARMS = {
    "M": ("memory",), "S": ("skill",), "A": ("agent_system",),
    "MS": ("memory", "skill"), "MA": ("memory", "agent_system"),
    "SA": ("skill", "agent_system"),
    "MSA": ("memory", "skill", "agent_system"),
}

# Keep model admission explicit so a new baseline cannot silently change only
# one half of the model/effort/version contract.  The 5.6 profile is the
# original campaign; the 6.1 profile is the matching high-effort G1 baseline.
SUPPORTED_SOLVER_PROFILES = {
    ("gpt-5.6-terra", "medium", "0.144.1"),
    ("gpt-6.1-sol", "high", "0.166.0"),
}
TERMINAL_STATUSES = {
    "pass", "fail", "timeout", "infrastructure_error", "interrupted"
}
AGENT_IMPORT = (
    "openevo_terminal_bench.seven_arm_agent:OpenEvoSevenArmCodexAgent"
)
SHA256_REF_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_codex_auth_path(path: Path) -> Path:
    """Validate a caller-selected auth file without opening or logging it."""
    if not path.is_absolute():
        raise ValueError("Codex auth path must be absolute")
    if path.is_symlink():
        raise ValueError("Codex auth path must not be a symbolic link")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise ValueError(f"Codex auth file does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Codex auth path must name a regular file")
    if metadata.st_uid != os.getuid():
        raise ValueError("Codex auth file must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Codex auth file must not be accessible by group or other")
    return path.resolve(strict=True)


def build_task_manifest(metadata: dict[str, Any], *, historical_failed: Iterable[str]) -> dict[str, Any]:
    """Validate an audited metadata snapshot and emit an immutable 89-task manifest."""
    version = metadata.get("dataset_version")
    tasks = metadata.get("tasks")
    if not isinstance(version, str) or SHA256_REF_RE.fullmatch(version) is None:
        raise ValueError("dataset_version must be an immutable sha256 ref")
    if not isinstance(tasks, list) or len(tasks) != 89:
        raise ValueError("Terminal-Bench 2.1 manifest must contain exactly 89 tasks")
    normalized = []
    for item in tasks:
        if not isinstance(item, dict):
            raise ValueError("task metadata must be objects")
        name, ref = item.get("name"), item.get("ref")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(ref, str)
            or SHA256_REF_RE.fullmatch(ref) is None
        ):
            raise ValueError("every task requires a name and immutable sha256 ref")
        normalized.append({"task_id": name.removeprefix("terminal-bench/"), "package": name, "ref": ref})
    normalized.sort(key=lambda item: item["task_id"])
    ids = [item["task_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("task IDs are not unique")
    missing = sorted(set(historical_failed) - set(ids))
    if missing:
        raise ValueError(f"historical task IDs missing from fixed dataset: {missing}")
    registry_verified = metadata.get("registry_verified", True)
    local_mirror_verified = metadata.get("local_mirror_verified", False)
    if registry_verified is not True and local_mirror_verified is not True:
        raise ValueError("task metadata must be Registry-verified or local-mirror-verified")
    body = {
        "schema_version": 1,
        "dataset": "terminal-bench/terminal-bench-2-1",
        "dataset_version": version,
        "dataset_version_id": metadata.get("dataset_version_id"),
        "task_count": 89,
        "tasks": normalized,
        "historical_failed_25_covered": True,
        "registry_verified": registry_verified is True,
        "local_mirror_verified": local_mirror_verified is True,
    }
    if isinstance(metadata.get("provenance"), dict):
        body["provenance"] = metadata["provenance"]
    body["manifest_sha256"] = hashlib.sha256(canonical_json(body)).hexdigest()
    return body


def verify_task_manifest(manifest: dict[str, Any]) -> bool:
    """Verify the complete canonical manifest rather than trusting its flags."""
    if (
        manifest.get("schema_version") != 1
        or manifest.get("dataset") != "terminal-bench/terminal-bench-2-1"
        or manifest.get("task_count") != 89
        or manifest.get("historical_failed_25_covered") is not True
    ):
        return False
    registry_verified = manifest.get("registry_verified") is True
    local_mirror_verified = manifest.get("local_mirror_verified") is True
    if not registry_verified and not local_mirror_verified:
        return False
    if local_mirror_verified:
        provenance = manifest.get("provenance")
        if not isinstance(provenance, dict) or not all(
            (
                provenance.get("source_commit"),
                provenance.get("source_manifest_sha256"),
                provenance.get("local_dataset_path"),
                provenance.get("all_task_digests_match") is True,
                provenance.get("git_clean") is True,
            )
        ):
            return False
    version = manifest.get("dataset_version")
    if not isinstance(version, str) or SHA256_REF_RE.fullmatch(version) is None:
        return False
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 89:
        return False
    task_ids: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            return False
        task_id, package, ref = item.get("task_id"), item.get("package"), item.get("ref")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(package, str)
            or not package
            or not isinstance(ref, str)
            or SHA256_REF_RE.fullmatch(ref) is None
        ):
            return False
        task_ids.append(task_id)
    if task_ids != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
        return False
    recorded = manifest.get("manifest_sha256")
    if not isinstance(recorded, str) or re.fullmatch(r"[0-9a-f]{64}", recorded) is None:
        return False
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    return hashlib.sha256(canonical_json(body)).hexdigest() == recorded


def validate_experiment_contract(
    config: dict[str, Any], *, dataset_version: str
) -> None:
    """Fail closed if the fixed MSA-only protocol drifts before planning."""
    benchmark = config.get("benchmark") or {}
    solver = config.get("solver") or {}
    authentication = config.get("authentication") or {}
    evolution = config.get("evolution") or {}
    dataset_source = benchmark.get("dataset_source", "registry")
    expected_arms = {key: list(value) for key, value in ARMS.items()}
    selected_arms = config.get("selected_arms")
    selected_arms_valid = (
        isinstance(selected_arms, list)
        and bool(selected_arms)
        and len(selected_arms) == len(set(selected_arms))
        and all(arm in ARMS for arm in selected_arms)
        and selected_arms == ["MSA"]
    )
    expected_solver_attempts = (
        benchmark.get("expected_task_count", 0)
        * benchmark.get("attempts_per_task", 0)
        * (1 + len(selected_arms))
        if selected_arms_valid
        else None
    )
    checks = {
        "formal_run_authorized": config.get("formal_run_authorized") is False,
        "expected_solver_attempts": (
            config.get("expected_solver_attempts") == expected_solver_attempts
        ),
        "arms": config.get("arms") == expected_arms,
        "selected_arms": selected_arms_valid,
        "expected_task_count": benchmark.get("expected_task_count") == 89,
        "attempts_per_task": benchmark.get("attempts_per_task") == 1,
        "concurrency": (
            isinstance(benchmark.get("concurrency"), int)
            and not isinstance(benchmark.get("concurrency"), bool)
            and 1 <= benchmark["concurrency"] <= 4
        ),
        "dataset_digest": benchmark.get("hub_digest") == dataset_version,
        "dataset_source": dataset_source in {"registry", "local_mirror"},
        "local_dataset_path": (
            dataset_source != "local_mirror"
            or isinstance(benchmark.get("local_dataset_path"), str)
        ),
        "solver_harness": solver.get("harness") == "codex_cli",
        "solver_profile": (
            solver.get("model"),
            solver.get("reasoning_effort"),
            solver.get("codex_version"),
        ) in SUPPORTED_SOLVER_PROFILES,
        "harbor_version": (config.get("harbor") or {}).get("version") == "0.21.0",
        "agent_setup_timeout_multiplier": (
            (config.get("harbor") or {}).get("agent_setup_timeout_multiplier") == 4.0
        ),
        "evolution_scope": evolution.get("scope") == "task_local",
        "evolution_input_mode": (
            evolution.get("input_mode") == "full_trajectory_no_chunking"
        ),
        "evolution_artifact_kinds": evolution.get("artifact_kinds")
        == ["memory", "skill", "agent_system"],
        "evolution_artifact_count": evolution.get("artifact_count_per_task") == 3,
        "evolution_g2_routing": (
            evolution.get("g2_artifact_routing") == "same_task_only"
        ),
        "reflector_matches_solver": (
            evolution.get("reflector_model") == solver.get("model")
            and evolution.get("reasoning_effort") == solver.get("reasoning_effort")
        ),
        "auth_mode": authentication.get("mode") == "explicit_codex_auth_json",
        "no_ambient_codex_home": authentication.get("forward_host_codex_home") is False,
        "no_default_auth": authentication.get("force_default_auth_json") is False,
    }
    drift = sorted(name for name, passed in checks.items() if not passed)
    if drift:
        raise ValueError(f"fixed MSA-only experiment contract drifted: {drift}")


@dataclass(frozen=True)
class FrozenArtifacts:
    memory: Path | None = None
    skill: Path | None = None
    agent_system: Path | None = None
    freeze_manifest: Path | None = None

    def selected(self, arm: str) -> dict[str, Path]:
        if arm not in ARMS:
            raise ValueError(f"unknown arm: {arm}")
        if self.freeze_manifest is None or not self.freeze_manifest.is_file():
            raise ValueError("G2 requires an artifact freeze manifest")
        freeze_root = self.freeze_manifest.parent.resolve()
        if not verify_frozen_tree(freeze_root, self.freeze_manifest):
            raise ValueError("frozen Evolution artifacts have drifted")
        available = {"memory": self.memory, "skill": self.skill, "agent_system": self.agent_system}
        selected: dict[str, Path] = {}
        for kind in ARMS[arm]:
            path = available[kind]
            valid = (
                path is not None
                and (
                    (kind == "skill" and path.is_dir() and (path / "SKILL.md").is_file())
                    or (kind != "skill" and path.is_file())
                )
            )
            if not valid:
                raise ValueError(f"arm {arm} requires frozen {kind}")
            assert path is not None
            try:
                path.resolve().relative_to(freeze_root)
            except ValueError as exc:
                raise ValueError(f"arm {arm} {kind} is outside the frozen artifact root") from exc
            selected[kind] = path
        return selected


def build_harbor_phase_command(
    *, dataset_ref: str | None = None, dataset_path: Path | None = None,
    jobs_dir: Path, job_name: str, model: str,
    reasoning_effort: str, codex_version: str, arm: str | None = None,
    artifacts: FrozenArtifacts | None = None, codex_auth_json_path: Path,
    toolchain_bundle_path: Path | None = None,
    toolchain_bundle_sha256: str | None = None,
    ca_bundle_path: Path | None = None,
    ca_bundle_sha256: str | None = None,
) -> list[str]:
    if (dataset_ref is None) == (dataset_path is None):
        raise ValueError("exactly one of dataset_ref or dataset_path is required")
    dataset_args: list[str]
    if dataset_ref is not None:
        prefix = "terminal-bench/terminal-bench-2-1@"
        if (
            not dataset_ref.startswith(prefix)
            or SHA256_REF_RE.fullmatch(dataset_ref[len(prefix):]) is None
        ):
            raise ValueError("remote plans require an immutable Hub dataset digest")
        dataset_args = ["--dataset", dataset_ref]
    else:
        assert dataset_path is not None
        if not dataset_path.is_absolute() or dataset_path.is_symlink():
            raise ValueError("local dataset path must be an absolute non-symlink directory")
        resolved_dataset_path = dataset_path.resolve(strict=True)
        if not resolved_dataset_path.is_dir() or not (
            resolved_dataset_path / "dataset.toml"
        ).is_file():
            raise ValueError("local dataset path must contain dataset.toml")
        dataset_args = ["--path", str(resolved_dataset_path)]
    auth_path = validate_codex_auth_path(codex_auth_json_path)
    toolchain_args: list[str] = []
    if toolchain_bundle_path is not None or toolchain_bundle_sha256 is not None:
        if toolchain_bundle_path is None or toolchain_bundle_sha256 is None:
            raise ValueError("toolchain bundle path and SHA256 must be provided together")
        receipt = validate_codex_toolchain_bundle(
            toolchain_bundle_path,
            expected_version=codex_version,
            expected_sha256=toolchain_bundle_sha256,
        )
        toolchain_args = [
            "--ak",
            f"toolchain_bundle_path={receipt['path']}",
            "--ak",
            f"toolchain_bundle_sha256={receipt['bundle_sha256']}",
        ]
    ca_args: list[str] = []
    if ca_bundle_path is not None or ca_bundle_sha256 is not None:
        if ca_bundle_path is None or ca_bundle_sha256 is None:
            raise ValueError("CA bundle path and SHA256 must be provided together")
        resolved_ca = ca_bundle_path.resolve(strict=True)
        if ca_bundle_path.is_symlink() or not resolved_ca.is_file():
            raise ValueError("CA bundle must be a non-symlink file")
        actual_ca_sha256 = sha256_file(resolved_ca)
        if actual_ca_sha256 != ca_bundle_sha256:
            raise ValueError("CA bundle SHA256 mismatch")
        ca_args = [
            "--ak",
            f"ca_bundle_path={resolved_ca}",
            "--ak",
            f"ca_bundle_sha256={actual_ca_sha256}",
        ]
    command = [
        "harbor", "run", *dataset_args, "--job-name", job_name,
        "--jobs-dir", str(jobs_dir), "--n-attempts", "1", "--n-concurrent", "1",
        "--max-retries", "0", "--agent-setup-timeout-multiplier", "4.0",
        "--agent", AGENT_IMPORT, "--model", model,
        "--ak", f"version={codex_version}", "--ak", f"reasoning_effort={reasoning_effort}",
        *toolchain_args,
        *ca_args,
        "--ae", f"CODEX_AUTH_JSON_PATH={auth_path}",
    ]
    if arm is None:
        return command
    if artifacts is None:
        raise ValueError("G2 arm requires frozen artifacts")
    selected = artifacts.selected(arm)
    if "memory" in selected:
        command.extend(["--ak", f"memory_path={selected['memory']}"])
    if "agent_system" in selected:
        command.extend(["--ak", f"agent_system_path={selected['agent_system']}"])
    if "skill" in selected:
        command.extend(["--skill", str(selected["skill"])])
    return command


class StatusLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return latest
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid status ledger line {number}") from exc
            if item.get("status") not in TERMINAL_STATUSES:
                raise ValueError(f"invalid status on ledger line {number}")
            latest[item["task_id"]] = item
        return latest

    def append(self, record: dict[str, Any]) -> None:
        if record.get("status") not in TERMINAL_STATUSES or not record.get("task_id"):
            raise ValueError("status record requires task_id and a closed status")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, canonical_json(record))
            os.fsync(fd)
        finally:
            os.close(fd)

    def pending(self, task_ids: Iterable[str]) -> list[str]:
        completed = self.read()
        return [task_id for task_id in task_ids if task_id not in completed]


def _replace_option(command: list[str], option: str, value: str) -> list[str]:
    result = list(command)
    if option in result:
        index = result.index(option)
        result[index + 1] = value
    else:
        result.extend([option, value])
    return result


def build_per_task_command(base_command: list[str], task_id: str) -> list[str]:
    """Derive an isolated one-task command without changing solver settings."""
    if not task_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for character in task_id):
        raise ValueError(f"unsafe task ID: {task_id!r}")
    command = list(base_command)
    if "--include-task-name" in command:
        raise ValueError("base phase command must not already filter tasks")
    job_index = command.index("--job-name") + 1
    jobs_index = command.index("--jobs-dir") + 1
    base_name = command[job_index]
    base_jobs = Path(command[jobs_index])
    command = _replace_option(command, "--job-name", f"{base_name}--{task_id}")
    command = _replace_option(
        command,
        "--jobs-dir",
        str(base_jobs.parent / "tasks" / task_id / "jobs"),
    )
    command.extend(["--include-task-name", task_id])
    return command


def _command_option(command: list[str], option: str) -> str:
    try:
        return command[command.index(option) + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"command is missing {option}") from exc


def _single_result_for_command(command: list[str]) -> tuple[Path | None, dict[str, Any] | None]:
    job_dir = Path(_command_option(command, "--jobs-dir")) / _command_option(
        command, "--job-name"
    )
    # Harbor 0.21 writes both a job-level aggregate at ``job_dir/result.json``
    # and one authoritative trial result below ``job_dir/<trial>/result.json``.
    # A per-task command must have exactly one direct trial child.
    results = sorted(job_dir.glob("*/result.json")) if job_dir.is_dir() else []
    if len(results) != 1:
        return None, None
    try:
        payload = json.loads(results[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return results[0], None
    return results[0], payload if isinstance(payload, dict) else None


class _ManagedSubprocessRunner:
    """Run Harbor in isolated process groups that can all be stopped on abort."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[Any]] = set()
        self._stopping = False

    def __call__(self, command: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
        with self._lock:
            if self._stopping:
                raise InterruptedError("campaign subprocess runner is stopping")
            process = subprocess.Popen(command, start_new_session=True)
            self._processes.add(process)
        try:
            returncode = process.wait()
        finally:
            with self._lock:
                self._processes.discard(process)
        completed = subprocess.CompletedProcess(command, returncode)
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, command)
        return completed

    @staticmethod
    def _signal(process: subprocess.Popen[Any], signum: signal.Signals) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def terminate_all(self, *, grace_seconds: float = 15.0) -> None:
        """Give Harbor SIGINT cleanup time, then ensure no paid child remains."""
        with self._lock:
            self._stopping = True
            processes = tuple(self._processes)
        for process in processes:
            self._signal(process, signal.SIGINT)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and any(
            process.poll() is None for process in processes
        ):
            time.sleep(0.05)
        for process in processes:
            self._signal(process, signal.SIGTERM)
        deadline = time.monotonic() + min(grace_seconds, 5.0)
        while time.monotonic() < deadline and any(
            process.poll() is None for process in processes
        ):
            time.sleep(0.05)
        for process in processes:
            self._signal(process, signal.SIGKILL)


def _run_task_command(
    *,
    task_id: str,
    command: list[str],
    campaign_concurrency: int,
    command_runner,
) -> dict[str, Any]:
    try:
        completed = command_runner(command, check=False)
        returncode = int(getattr(completed, "returncode", 1))
        result_path, result = _single_result_for_command(command)
        verifier_stdout = None
        if result_path is not None:
            log_path = result_path.parent / "verifier" / "test-stdout.txt"
            if log_path.is_file():
                verifier_stdout = log_path.read_text(encoding="utf-8", errors="replace")
        status = classify_result(result, verifier_stdout=verifier_stdout)
        if result is None:
            status = "infrastructure_error"
        return {
            "task_id": task_id,
            "status": status,
            "returncode": returncode,
            "result_path": str(result_path) if result_path else None,
            "command_sha256": hashlib.sha256(canonical_json(command)).hexdigest(),
            "campaign_concurrency": campaign_concurrency,
        }
    except Exception as exc:
        return {
            "task_id": task_id,
            "status": "infrastructure_error",
            "returncode": None,
            "result_path": None,
            "command_sha256": hashlib.sha256(canonical_json(command)).hexdigest(),
            "campaign_concurrency": campaign_concurrency,
            "runner_error_type": type(exc).__name__,
        }


def _interrupted_task_record(
    task_id: str, command: list[str], *, campaign_concurrency: int
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "interrupted",
        "returncode": None,
        "result_path": None,
        "command_sha256": hashlib.sha256(canonical_json(command)).hexdigest(),
        "campaign_concurrency": campaign_concurrency,
    }


def run_campaign_phase(
    *,
    base_command: list[str],
    task_ids: Iterable[str],
    ledger: StatusLedger,
    formal_run_authorized: bool,
    concurrency: int = 1,
    command_runner=None,
) -> list[dict[str, Any]]:
    """Run isolated task commands with bounded, interruptible outer concurrency."""
    if not formal_run_authorized:
        raise PermissionError("formal campaign execution is not authorized")
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not 1 <= concurrency <= 4
    ):
        raise ValueError("campaign concurrency must be an integer from 1 through 4")

    pending = ledger.pending(task_ids)
    if not pending:
        return []
    commands = {
        task_id: build_per_task_command(base_command, task_id) for task_id in pending
    }
    managed_runner = _ManagedSubprocessRunner() if command_runner is None else None
    runner = managed_runner if managed_runner is not None else command_runner
    assert runner is not None

    records: dict[str, dict[str, Any]] = {}
    submitted: dict[Future, str] = {}
    next_index = 0
    executor = ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="tb21-campaign"
    )

    def submit_available() -> None:
        nonlocal next_index
        while len(submitted) < concurrency and next_index < len(pending):
            task_id = pending[next_index]
            next_index += 1
            future = executor.submit(
                _run_task_command,
                task_id=task_id,
                command=commands[task_id],
                campaign_concurrency=concurrency,
                command_runner=runner,
            )
            submitted[future] = task_id

    try:
        submit_available()
        while submitted:
            completed, _ = wait(tuple(submitted), return_when=FIRST_COMPLETED)
            for future in completed:
                task_id = submitted[future]
                record = future.result()
                submitted.pop(future)
                ledger.append(record)
                records[task_id] = record
            submit_available()
    except BaseException:
        if managed_runner is not None:
            managed_runner.terminate_all()
        for future in submitted:
            future.cancel()
        for task_id in submitted.values():
            if task_id not in records:
                record = _interrupted_task_record(
                    task_id,
                    commands[task_id],
                    campaign_concurrency=concurrency,
                )
                ledger.append(record)
                records[task_id] = record
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return [records[task_id] for task_id in pending if task_id in records]


def g1_evolution_sources(
    *, task_ids: Iterable[str], ledger: StatusLedger
) -> list[Path]:
    """Return complete raw trajectory/verifier evidence in manifest task order."""
    latest = ledger.read()
    ordered_ids = list(task_ids)
    missing = [task_id for task_id in ordered_ids if task_id not in latest]
    if missing:
        raise ValueError(f"G1 ledger is incomplete for {len(missing)} task(s)")
    sources: list[Path] = []
    evidence_paths = (
        Path("result.json"),
        Path("agent/trajectory.json"),
        Path("agent/codex.txt"),
        Path("agent/stdout.txt"),
        Path("agent/stderr.txt"),
        Path("verifier/ctrf.json"),
        Path("verifier/test-stdout.txt"),
        Path("verifier/reward.txt"),
    )
    for task_id in ordered_ids:
        result_value = latest[task_id].get("result_path")
        if not isinstance(result_value, str) or not result_value:
            raise ValueError(f"G1 task lacks a result path: {task_id}")
        result_path = Path(result_value).resolve(strict=True)
        trial_dir = result_path.parent
        trajectory = trial_dir / "agent" / "trajectory.json"
        if not trajectory.is_file():
            raise ValueError(f"G1 task lacks a complete ATIF trajectory: {task_id}")
        task_sources = [trial_dir / relative for relative in evidence_paths]
        sources.extend(path for path in task_sources if path.is_file())
    if len(sources) != len(set(sources)):
        raise ValueError("G1 Evolution source paths are not unique")
    return sources


def classify_result(
    result: dict[str, Any] | None, *, verifier_stdout: str | None = None
) -> str:
    if result is None:
        return "interrupted"
    exception = str((result.get("exception_info") or {}).get("exception_type") or "")
    if "Timeout" in exception:
        return "timeout"
    if exception:
        infrastructure = (
            "Docker",
            "Environment",
            "Image",
            "Setup",
            "Connection",
            "NonZeroAgentExitCode",
            "VerifierInfrastructure",
        )
        return "infrastructure_error" if any(token in exception for token in infrastructure) else "fail"
    if verifier_stdout and "test session starts" not in verifier_stdout:
        if re.search(r"(?m)^/tests/test\.sh: line \d+: uvx: command not found\s*$", verifier_stdout):
            return "infrastructure_error"
    verifier_result = result.get("verifier_result")
    rewards = (
        verifier_result.get("rewards")
        if isinstance(verifier_result, dict)
        else None
    )
    if rewards is None:
        rewards = result.get("rewards")
    if rewards is None and "reward" in result:
        rewards = result.get("reward")
    if isinstance(rewards, dict):
        values = list(rewards.values())
        return "pass" if any(value == 1 or value == 1.0 for value in values) else "fail"
    return "pass" if rewards == 1 or rewards == 1.0 else "fail"


def make_trajectory_chunks(source: Path, output_dir: Path, *, chunk_bytes: int) -> dict[str, Any]:
    """Copy every source byte into deterministic chunks with auditable coverage."""
    if chunk_bytes < 1:
        raise ValueError("chunk_bytes must be positive")
    output_dir.mkdir(parents=True, exist_ok=False)
    source_hash = sha256_file(source)
    chunks = []
    offset = 0
    with source.open("rb") as handle:
        while payload := handle.read(chunk_bytes):
            index = len(chunks)
            path = output_dir / f"chunk-{index:06d}.bin"
            path.write_bytes(payload)
            chunks.append({
                "index": index, "source_start": offset,
                "source_end_exclusive": offset + len(payload), "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(), "path": path.name,
                "processing_status": "pending",
            })
            offset += len(payload)
    manifest = {
        "schema_version": 1, "source_path": str(source), "source_sha256": source_hash,
        "source_byte_count": source.stat().st_size, "chunk_bytes": chunk_bytes,
        "chunk_count": len(chunks), "coverage_complete": offset == source.stat().st_size,
        "chunks": chunks, "unprocessed_chunk_count": len(chunks),
    }
    (output_dir / "manifest.json").write_bytes(canonical_json(manifest))
    return manifest


def verify_trajectory_chunks(output_dir: Path) -> bool:
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    cursor = 0
    digest = hashlib.sha256()
    for item in manifest["chunks"]:
        if item["source_start"] != cursor:
            return False
        payload = (output_dir / item["path"]).read_bytes()
        if len(payload) != item["byte_count"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
            return False
        digest.update(payload)
        cursor = item["source_end_exclusive"]
    return (
        cursor == manifest["source_byte_count"]
        and digest.hexdigest() == manifest["source_sha256"]
        and manifest["coverage_complete"] is True
    )


def _tree_files(root: Path, *, excluded: set[Path]) -> list[dict[str, Any]]:
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item not in excluded):
        files.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    return files


def freeze_tree(root: Path, output: Path) -> dict[str, Any]:
    files = _tree_files(root, excluded={output})
    manifest = {"schema_version": 1, "root": str(root), "files": files}
    manifest["tree_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    output.write_bytes(canonical_json(manifest))
    return manifest


def verify_frozen_tree(root: Path, manifest_path: Path) -> bool:
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = _tree_files(root, excluded={manifest_path})
    actual = {"schema_version": 1, "root": str(root), "files": files}
    actual["tree_sha256"] = hashlib.sha256(canonical_json(actual)).hexdigest()
    return actual == expected


__all__ = [
    "ARMS", "FrozenArtifacts", "StatusLedger", "build_harbor_phase_command",
    "build_per_task_command", "g1_evolution_sources", "run_campaign_phase",
    "build_task_manifest", "classify_result", "freeze_tree", "make_trajectory_chunks",
    "validate_codex_auth_path", "validate_experiment_contract",
    "verify_frozen_tree", "verify_task_manifest",
    "verify_trajectory_chunks",
]
