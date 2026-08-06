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
        result = self._executor(
            argv,
            env=env,
            cwd=workspace,
            prompt=prompt,
            timeout_seconds=self.timeout_seconds,
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
        (workspace / "_meta.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "task_id": request["task_id"],
                    "run_id": request["run_id"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "pass_name": request["pass_name"],
            "session_id": f"ds-{uuid.uuid4().hex[:12]}",
            "candidate_output_root": str(workspace),
            "exit_status": result.returncode,
            "stdout_sha256": _sha256_text(result.stdout),
            "stderr_sha256": _sha256_text(result.stderr),
            "duration_seconds": result.duration_seconds,
            "provider": "deepseek",
            "model": self.model,
            "profile_summary": "codex-deepseek-profile",
            "codex_version": "host-codex",
            "deepseek_key_present": self.deepseek_key_present,
            "secret_recorded": False,
        }

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
