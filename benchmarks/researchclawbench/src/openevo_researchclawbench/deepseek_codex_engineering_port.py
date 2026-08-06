"""Engineering-only DeepSeek Codex port for the minimal per-item canary.

This port intentionally does *not* claim the OpenEvo native Core route.  It
invokes the local ``codex`` executable with the DeepSeek profile so the
minimal per-item runner can be validated end to end before any Core provider
work is approved.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .delivery_contract import analyze_delivery_evidence


DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_PROFILE = "~/.codex-deepseek"
CODEX_EXECUTABLE = "codex"

_ENV_ALLOWLIST = {
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PYTHONUNBUFFERED",
    "CODEX_HOME",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
}


class DeepSeekEngineeringError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CodexExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _extract_final_assistant_message(stdout: str) -> dict[str, Any] | None:
    """Extract the last assistant message from a Codex JSONL event stream."""
    last: dict[str, Any] | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("message") or item.get("text") or ""
        if isinstance(message, str) and message.strip():
            last = {
                "type": event.get("type"),
                "message": message,
                "timestamp": event.get("timestamp"),
            }
    return last


def _extract_usage(stdout: str) -> dict[str, Any] | None:
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        candidate = payload.get("usage") or payload.get("token_usage")
        if isinstance(candidate, dict):
            for key, value in candidate.items():
                if isinstance(value, (int, float)) and key not in usage:
                    usage[key] = value
    return usage or None


def _output_manifest(root: Path) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    from .hashing import iter_regular_files

    for path in iter_regular_files(root):
        relative = path.relative_to(root).as_posix()
        metadata = path.stat()
        entries[relative] = {
            "size_bytes": int(metadata.st_size),
            "sha256": _sha256_file(path),
        }
    return dict(sorted(entries.items()))


def _evidence_root_sha256(root: Path) -> str:
    entries = _output_manifest(root)
    return _sha256_text(
        json.dumps(entries, sort_keys=True, separators=(",", ":"))
    )


def _run_codex(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str | Path,
    prompt: str,
    timeout_seconds: int,
) -> CodexExecutionResult:
    import time

    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            input=prompt,
            text=True,
            capture_output=True,
            env=dict(env),
            cwd=str(cwd),
            timeout=timeout_seconds,
            check=False,
            close_fds=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeepSeekEngineeringError(
            "BLOCKED_CANDIDATE_TIMEOUT", "DeepSeek Codex exceeded the engineering timeout"
        ) from exc
    return CodexExecutionResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=time.monotonic() - started,
    )


class DeepSeekCodexEngineeringPort:
    """Candidate + Evolution port backed by the local DeepSeek Codex profile."""

    def __init__(
        self,
        *,
        model: str = DEEPSEEK_MODEL,
        profile: str = DEEPSEEK_PROFILE,
        timeout_seconds: int = 1800,
        sandbox: str = "workspace-write",
        executor: Callable[..., CodexExecutionResult] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.model = model
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.sandbox = sandbox
        self._executor = executor or _run_codex
        self.dry_run = dry_run
        self.real_calls = 0
        self.dry_run_operations: list[dict[str, Any]] = []

    @property
    def deepseek_key_present(self) -> bool:
        return bool(os.environ.get("DEEPSEEK_API_KEY"))

    def _environment(self) -> dict[str, str]:
        source = os.environ
        env = {
            key: source[key]
            for key in _ENV_ALLOWLIST
            if key in source
        }
        env["CODEX_HOME"] = str(Path(self.profile).expanduser())
        if not self.dry_run and not self.deepseek_key_present:
            raise DeepSeekEngineeringError(
                "BLOCKED_DEEPSEEK_CREDENTIAL", "DEEPSEEK_API_KEY is not present"
            )
        if self.deepseek_key_present:
            env["DEEPSEEK_API_KEY"] = source["DEEPSEEK_API_KEY"]
        return env

    def command_argv(self, workspace: str | Path) -> list[str]:
        workdir = str(workspace)
        return [
            CODEX_EXECUTABLE,
            "exec",
            "--model",
            self.model,
            "--cd",
            workdir,
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            self.sandbox,
        ]

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """CandidatePort contract: one candidate pass per invocation."""

        return self.candidate(request)

    def candidate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        workspace = Path(str(request["workspace"])).resolve(strict=True)
        prompt = str(
            request.get("prompt")
            or (workspace / "INSTRUCTIONS.md").read_text(encoding="utf-8")
        )
        argv = self.command_argv(workspace)
        env = self._environment()
        self.real_calls += 1
        started_at = datetime.now(UTC).isoformat()
        result = self._executor(
            argv,
            env=env,
            cwd=workspace,
            prompt=prompt,
            timeout_seconds=self.timeout_seconds,
        )
        ended_at = datetime.now(UTC).isoformat()
        secret_values = tuple(
            value
            for key, value in os.environ.items()
            if key
            in {
                "DEEPSEEK_API_KEY",
                "RCB_JUDGE_API_KEY",
                "OPENAI_API_KEY",
            }
            and value
        )
        evidence_root = None
        if request.get("evidence_root"):
            evidence_root = Path(str(request["evidence_root"])).resolve(strict=False)
            self._persist_candidate_evidence(
                workspace=workspace,
                evidence_root=evidence_root,
                argv=argv,
                result=result,
                prompt=prompt,
                secret_values=secret_values,
                started_at=started_at,
                ended_at=ended_at,
                env_keys=sorted(env),
            )
        if result.returncode != 0:
            lowered = (result.stdout + result.stderr).casefold()
            if "authentication" in lowered or "unauthorized" in lowered:
                raise DeepSeekEngineeringError(
                    "BLOCKED_CANDIDATE_AUTH", "DeepSeek Codex authentication failed"
                )
            if "quota" in lowered or "insufficient" in lowered or "402" in lowered:
                raise DeepSeekEngineeringError(
                    "BLOCKED_CANDIDATE_QUOTA", "DeepSeek Codex quota exceeded"
                )
            raise DeepSeekEngineeringError(
                "BLOCKED_CANDIDATE_RUNTIME",
                f"DeepSeek Codex exited with {result.returncode}",
            )
        meta = {
            "status": "completed",
            "exit_code": 0,
            "task_id": request["task_id"],
            "run_id": request["run_id"],
        }
        injected_paths = request.get("injected_artifact_paths")
        injected_sha256 = request.get("injected_artifact_sha256")
        if injected_paths is not None or injected_sha256 is not None:
            meta["injected_artifact_paths"] = injected_paths
            meta["injected_artifact_sha256"] = injected_sha256
            meta["artifact_read_requested"] = True
        (workspace / "_meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        delivery_evidence = None
        if evidence_root is not None:
            delivery_evidence = analyze_delivery_evidence(
                evidence_root=evidence_root,
                workspace=workspace,
            )
        receipt: dict[str, Any] = {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "session_id": f"ds-{uuid.uuid4().hex[:12]}",
            "candidate_output_root": str(workspace),
            "exit_status": result.returncode,
            "stdout_sha256": _sha256_text(
                _redact_text(result.stdout, secret_values)
            ),
            "stderr_sha256": _sha256_text(
                _redact_text(result.stderr, secret_values)
            ),
            "duration_seconds": result.duration_seconds,
            "provider": "deepseek",
            "model": self.model,
            "profile_summary": "codex-deepseek-profile",
            "codex_version": "host-codex",
            "deepseek_key_present": self.deepseek_key_present,
            "secret_recorded": False,
        }
        if evidence_root is not None:
            receipt["evidence_root"] = str(evidence_root)
            receipt["evidence_sha256"] = _evidence_root_sha256(evidence_root)
            receipt["delivery_evidence"] = delivery_evidence
        if injected_paths is not None or injected_sha256 is not None:
            receipt["injected_artifact_paths"] = injected_paths
            receipt["injected_artifact_sha256"] = injected_sha256
            receipt["artifact_read_requested"] = True
        return receipt

    def _persist_candidate_evidence(
        self,
        *,
        workspace: Path,
        evidence_root: Path,
        argv: list[str],
        result: CodexExecutionResult,
        prompt: str,
        secret_values: tuple[str, ...],
        started_at: str,
        ended_at: str,
        env_keys: list[str],
    ) -> None:
        """Persist redacted Candidate evidence under the run namespace."""
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        redacted_stdout = _redact_text(result.stdout, secret_values)
        redacted_stderr = _redact_text(result.stderr, secret_values)
        (evidence_root / "stdout.log").write_text(
            redacted_stdout, encoding="utf-8"
        )
        (evidence_root / "stderr.log").write_text(
            redacted_stderr, encoding="utf-8"
        )
        (evidence_root / "codex_events.jsonl").write_text(
            redacted_stdout, encoding="utf-8"
        )
        final_message = _extract_final_assistant_message(redacted_stdout)
        if final_message is not None:
            (evidence_root / "final_assistant_message.json").write_text(
                json.dumps(final_message, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        command = {
            "argv": list(argv),
            "cwd": str(workspace),
            "exit_code": result.returncode,
            "model": self.model,
            "provider": "deepseek",
            "env_keys": env_keys,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": result.duration_seconds,
            "usage": _extract_usage(redacted_stdout),
            "prompt_sha256": _sha256_text(prompt),
            "secret_recorded": False,
        }
        (evidence_root / "command.json").write_text(
            json.dumps(command, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (evidence_root / "output_manifest.json").write_text(
            json.dumps(_output_manifest(workspace), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def evolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        artifact_root = Path(str(request["artifact_root"])).resolve(strict=False)
        artifact_root.mkdir(parents=True, exist_ok=True)
        skill_root = artifact_root / "skill"
        agent_root = artifact_root / "agent_system"
        skill_root.mkdir(exist_ok=True)
        agent_root.mkdir(exist_ok=True)
        memory_path = artifact_root / "memory.md"
        skill_path = skill_root / "SKILL.md"
        agent_path = agent_root / "AGENTS.md"

        workspace = artifact_root
        argv = self.command_argv(workspace)
        env = self._environment()
        artifacts: dict[str, dict[str, str]] = {}
        baseline_root = Path(str(request["baseline_sealed_root"])).resolve(strict=True)
        gt_path = Path(str(request["gt_path"])).resolve(strict=True)
        for artifact_type, path in (
            ("memory", memory_path),
            ("skill", skill_path),
            ("agent_system", agent_path),
        ):
            prompt = _evolution_prompt(
                baseline_root=baseline_root,
                gt_path=gt_path,
                artifact_type=artifact_type,
                destination=path,
            )
            self.real_calls += 1
            result = self._executor(
                argv,
                env=env,
                cwd=workspace,
                prompt=prompt,
                timeout_seconds=self.timeout_seconds,
            )
            if result.returncode != 0:
                raise DeepSeekEngineeringError(
                    "BLOCKED_EVOLUTION_PROVIDER",
                    f"DeepSeek evolution exited with {result.returncode}",
                )
            if not path.is_file():
                raise DeepSeekEngineeringError(
                    "BLOCKED_EVOLUTION_PROVIDER",
                    f"DeepSeek evolution did not produce {artifact_type}",
                )
            artifacts[artifact_type] = {
                "task_id": str(request["task_id"]),
                "artifact_type": artifact_type,
                "source_baseline_hash": str(request["baseline_sealed_hash"]),
                "gt_sha256": str(request["gt_sha256"]),
                "generation": "1",
                "content_sha256": _sha256_file(path),
                "created_at": datetime.now(UTC).isoformat(),
            }
        manifest = {
            "schema_version": "openevo.researchclawbench.minimal_engineering_artifact.v1",
            "task_id": request["task_id"],
            "baseline_run_id": request["baseline_run_id"],
            "generation": 1,
            "artifacts": artifacts,
        }
        (artifact_root / "injection_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "task_id": request["task_id"],
            "baseline_run_id": request["baseline_run_id"],
            "generation": 1,
            "gt_sha256": request["gt_sha256"],
            "artifact_root": str(artifact_root),
            "artifacts": {
                key: value["content_sha256"] for key, value in artifacts.items()
            },
            "artifact_details": artifacts,
            "secret_recorded": False,
        }


class CodexEngineeringPort(DeepSeekCodexEngineeringPort):
    """Native Codex Candidate/Evolution port (e.g. GPT-5.5, profile ~/.codex).

    Provider/profile/model are configuration parameters.  Credentials live in
    the native profile (CODEX_HOME) and are never copied into evidence or
    recorded in receipts.
    """

    def __init__(
        self,
        *,
        model: str = "gpt-5.5",
        profile: str = "~/.codex",
        timeout_seconds: int = 1800,
        sandbox: str = "workspace-write",
        executor: Callable[..., CodexExecutionResult] | None = None,
        dry_run: bool = False,
    ) -> None:
        super().__init__(
            model=model,
            profile=profile,
            timeout_seconds=timeout_seconds,
            sandbox=sandbox,
            executor=executor,
            dry_run=dry_run,
        )

    @property
    def deepseek_key_present(self) -> bool:
        return False

    def _environment(self) -> dict[str, str]:
        source = os.environ
        env = {
            key: source[key]
            for key in _ENV_ALLOWLIST
            if key in source
        }
        env["CODEX_HOME"] = str(Path(self.profile).expanduser())
        return env

    def candidate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        receipt = super().candidate(request)
        receipt["profile"] = self.profile
        receipt["credential_present"] = True
        receipt["credential_recorded"] = False
        receipt.pop("deepseek_key_present", None)
        return receipt


def _evolution_prompt(
    *,
    baseline_root: Path,
    gt_path: Path,
    artifact_type: str,
    destination: Path,
) -> str:
    baseline_text: list[str] = []
    for path in sorted(baseline_root.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".csv", ".txt", ".py"}:
            try:
                baseline_text.append(f"### {path.relative_to(baseline_root)}\n{path.read_text(encoding='utf-8', errors='replace')[:8000]}")
            except OSError:
                continue
    gt_text = gt_path.read_text(encoding="utf-8", errors="replace")
    return (
        "You are the evolution step of a research benchmark. "
        "Using ONLY the current task's baseline output and ground-truth checklist, "
        "produce one artifact file.\n"
        f"Write ONLY the {artifact_type} artifact to: {destination}\n\n"
        "Do not include Judge scores, Judge reasoning, other tasks, or secrets.\n\n"
        "## Baseline sealed output\n"
        + "\n".join(baseline_text)
        + "\n\n## Ground-truth checklist\n"
        + gt_text
        + "\n\nWrite ONLY the requested artifact and finish."
    )
