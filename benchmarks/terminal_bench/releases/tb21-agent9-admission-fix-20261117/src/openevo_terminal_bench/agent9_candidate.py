"""Frozen, disclosed task-local AGENTS routing for the nine-task candidate.

This selects artifacts before execution; it neither reads rewards nor changes
the task, verifier, budget, model, or native Codex session.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from openevo_terminal_bench.seven_arm_agent import OpenEvoSevenArmCodexAgent


AGENT_TASKS = frozenset((
    'configure-git-webserver', 'extract-elf', 'filter-js-from-html',
    'headless-terminal', 'largest-eigenval', 'mteb-retrieve',
    'pytorch-model-cli', 'raman-fitting', 'sanitize-git-repo',
))


def select_agents(manifest_path: Path, expected_sha256: str, task_name: str):
    payload = manifest_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError('Frozen injection manifest changed')
    manifest = json.loads(payload)
    entries = manifest['tasks']
    if set(entries) != AGENT_TASKS:
        raise ValueError('Injection cohort must be the exact nine authorized tasks')
    task = task_name.removeprefix('terminal-bench/')
    if task not in entries:
        return None
    entry = entries[task]
    path = Path(entry['path']).resolve(strict=True)
    if path.name != 'AGENTS.md' or not path.is_relative_to(manifest_path.parent.resolve()):
        raise ValueError('Artifact escaped the frozen candidate directory')
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError('Frozen AGENTS artifact changed')
    return path


class AgentNineCandidate(OpenEvoSevenArmCodexAgent):
    @staticmethod
    def name():
        return 'openevo-agent9-codex'

    def __init__(self, *args, injection_manifest_path: str,
                 injection_manifest_sha256: str, public_uv_dir: str | None = None,
                 public_uv_hashes: dict | None = None, **kwargs):
        if (kwargs.get('memory_path') or kwargs.get('agent_system_path')
                or kwargs.get('skills_dir') or kwargs.get('load_trajectory')):
            raise ValueError('Candidate accepts only the frozen AGENTS selection')
        super().__init__(*args, **kwargs)
        self._injection_manifest = Path(injection_manifest_path).resolve(strict=True)
        self._injection_digest = injection_manifest_sha256
        self._uv_dir = Path(public_uv_dir).resolve(strict=True) if public_uv_dir else None
        self._uv_hashes = public_uv_hashes or {}
        if self._uv_dir:
            for command in ('uv', 'uvx'):
                if hashlib.sha256((self._uv_dir / command).read_bytes()).hexdigest() != self._uv_hashes.get(command):
                    raise ValueError('Public uv binary hash mismatch')

    async def install(self, environment):
        await super().install(environment)
        if self._uv_dir:
            # Public tool bootstrap only. Original verifier scripts/assertions,
            # their dependency pins and their time budgets remain unchanged.
            for command in ('uv', 'uvx'):
                source = self._uv_dir / command
                if hashlib.sha256(source.read_bytes()).hexdigest() != self._uv_hashes[command]:
                    raise ValueError('Public uv binary changed')
                await environment.upload_file(source, '/usr/local/bin/' + command)
            await self.exec_as_root(environment, command=(
                'chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx; '
                'mkdir -p /root/.local/bin; '
                "printf '%s\\n' 'export PATH=/usr/local/bin:$PATH' > /root/.local/bin/env; "
                '/usr/local/bin/uv --version'))

    async def exec_as_agent(self, environment, command, **kwargs):
        try:
            return await super().exec_as_agent(environment, command, **kwargs)
        except asyncio.CancelledError:
            if 'codex exec ' in command:
                # Native execution cancellation can leave the remote process
                # alive. Stop only Codex inside this trial's own environment
                # before native recovery/verification continues.
                cleanup = (
                    'for f in /proc/[0-9]*/comm; do '
                    'read -r n < "$f" 2>/dev/null || continue; '
                    'case "$n" in codex|codex-real) p=${f#/proc/}; p=${p%/comm}; '
                    'kill -TERM "$p" 2>/dev/null || true;; esac; done'
                )
                await asyncio.shield(environment.exec(command=cleanup, timeout_sec=10))
            raise

    async def setup(self, environment):
        self._agent_system_path = select_agents(
            self._injection_manifest, self._injection_digest,
            environment.environment_name,
        )
        await super().setup(environment)
        receipt = {
            'task': environment.environment_name,
            'injected': self._agent_system_path is not None,
            'manifest_sha256': self._injection_digest,
            'artifact': self.injection_receipt()['agent_system'],
            'provenance': 'same-task prior execution trajectory and grading feedback',
        }
        path = environment.trial_paths.agent_dir / 'injection-receipt.json'
        path.write_text(json.dumps(receipt, indent=2) + '\n')
