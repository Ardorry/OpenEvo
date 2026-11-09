"""Portable launcher added for this release. Default: zero-model validation."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


async def prepare(args):
    from harbor.models.job.config import JobConfig
    from harbor.models.job.lock import build_job_lock
    from harbor.models.task.task import Task
    from harbor.trial.single_step import SingleStepTrial
    from openevo_terminal_bench.agent9_candidate import select_agents
    from openevo_terminal_bench.campaign import validate_codex_auth_path
    from openevo_terminal_bench.source_cached_job import create_source_cached_job

    if args.attempts < 1:
        raise ValueError("attempts must be positive")
    assets = args.assets_dir.resolve(strict=True)
    for item in json.loads((ROOT / "docs/runtime-assets.json").read_text()).values():
        if digest(assets / item["filename"]) != item["sha256"]:
            raise ValueError("Runtime asset changed: " + item["filename"])
    for item in json.loads((ROOT / "docs/executed-source-files.json").read_text()):
        if digest(ROOT / item["file"]) != item["sha256"]:
            raise ValueError("Frozen execution source changed: " + item["file"])

    auth = args.auth_path
    if args.execute:
        if auth is None:
            raise ValueError("--execute requires --auth-path outside the repository")
        auth = validate_codex_auth_path(auth.absolute())
        if auth.is_relative_to(ROOT):
            raise ValueError("Credentials must remain outside the repository")
    name = "openevo-tb21-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    jobs = args.jobs_dir.resolve()
    if jobs.is_relative_to(ROOT):
        raise ValueError("Keep runtime outputs outside the public repository")
    jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared = jobs / (name + "-inputs")
    prepared.mkdir(mode=0o700)
    manifest = json.loads((ROOT / "artifacts/manifest.json").read_text())
    injections = prepared / "injections"
    injections.mkdir(mode=0o700)
    for task, entry in manifest["tasks"].items():
        source = (ROOT / "artifacts" / entry["path"]).resolve(strict=True)
        if not source.is_relative_to(ROOT / "artifacts") or digest(source) != entry["sha256"]:
            raise ValueError("Frozen artifact changed: " + task)
        target = injections / task / "AGENTS.md"
        target.parent.mkdir(mode=0o700)
        shutil.copyfile(source, target)
        target.chmod(0o600)
        entry["path"] = str(target)
    manifest_path = injections / "manifest.json"
    save(manifest_path, manifest)
    manifest_sha = digest(manifest_path)
    for task in manifest["tasks"]:
        select_agents(manifest_path, manifest_sha, task)
    assert select_agents(manifest_path, manifest_sha, "make-doom-for-mips") is None

    payload = json.loads((ROOT / "config.template.json").read_text())
    payload.update(job_name=name, jobs_dir=str(jobs), n_attempts=args.attempts)
    agent = payload["agents"][0]
    kwargs = agent["kwargs"]
    kwargs.update(injection_manifest_path=str(manifest_path), injection_manifest_sha256=manifest_sha,
                  toolchain_bundle_path=str(assets / "codex-0.166.0-x86_64-linux.tar.gz"),
                  ca_bundle_path=str(assets / "ca-certificates-20261103.pem"), public_uv_dir=str(assets))
    agent["env"]["CODEX_AUTH_JSON_PATH"] = str(auth or (jobs / "AUTH_REQUIRED_FOR_EXECUTION.json"))
    config = JobConfig.model_validate(payload)
    assert config.n_concurrent_trials == 4 and config.timeout_multiplier == 1
    assert config.retry.max_retries == 0 and not config.verifier.disable
    assert not config.agents[0].skills and not config.agents[0].resume_trajectory
    config_path = prepared / "config.json"
    save(config_path, config.model_dump(mode="json"))
    job = create_source_cached_job(config, args.dataset_path.resolve(strict=True))
    try:
        lock = build_job_lock(config=config, trial_configs=job._trial_configs,
                              task_download_results=job._task_download_results)
        save(job.job_dir / "resolved-job-lock.json", lock.model_dump(mode="json"))
        if len(job._task_configs) != 89 or len(job) != 89 * args.attempts:
            raise ValueError("Expected all 89 tasks in every attempt")
        # Construct native trials and check budgets without containers or agent execution.
        for trial_config in job._trial_configs[:89]:
            downloaded = job._task_download_results[trial_config.task.get_task_id()]
            trial = SingleStepTrial(trial_config, _task=Task(downloaded.path),
                                    _task_download_result=downloaded)
            try:
                assert trial._agent_timeout_sec == trial.task.config.agent.timeout_sec
                assert trial._verifier_timeout_sec == trial.task.config.verifier.timeout_sec
            finally:
                trial._close_logger_handler()
        report = dict(job_dir=str(job.job_dir), distinct_tasks=89, planned_trials=len(job),
                      native_budget_checks=89, execute=args.execute,
                      model_calls_during_preparation=0, registry_verified=False)
        save(prepared / "validation.json", report)
        print(json.dumps(report), flush=True)
        if args.execute:
            save(job.job_dir / "admission.json", dict(config_sha256=digest(config_path), automatic_retry=False))
            await job.run()
    finally:
        job._close_logger_handlers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-path", required=True, type=Path)
    parser.add_argument("--assets-dir", required=True, type=Path)
    parser.add_argument("--jobs-dir", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--auth-path", type=Path)
    asyncio.run(prepare(parser.parse_args()))
