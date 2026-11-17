import hashlib
import json
import asyncio

import pytest

from openevo_terminal_bench.agent9_candidate import AGENT_TASKS, select_agents
from openevo_terminal_bench.agent9_candidate import AgentNineCandidate
from openevo_terminal_bench.seven_arm_agent import OpenEvoSevenArmCodexAgent


def manifest(tmp_path):
    entries = {}
    for task in AGENT_TASKS:
        path = tmp_path / task / 'AGENTS.md'
        path.parent.mkdir()
        path.write_text('frozen ' + task)
        entries[task] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    path = tmp_path / 'injections.json'
    path.write_text(json.dumps({'tasks': entries}))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_only_authorized_tasks_receive_artifact(tmp_path):
    path, digest = manifest(tmp_path)
    for task in AGENT_TASKS:
        assert select_agents(path, digest, 'terminal-bench/' + task).parent.name == task
    assert select_agents(path, digest, 'make-doom-for-mips') is None
    assert select_agents(path, digest, 'build-cython-ext') is None


def test_modified_manifest_rejected(tmp_path):
    path, digest = manifest(tmp_path)
    path.write_text('{}')
    with pytest.raises(ValueError, match='manifest changed'):
        select_agents(path, digest, 'make-doom-for-mips')


def test_modified_artifact_rejected(tmp_path):
    path, digest = manifest(tmp_path)
    (tmp_path / 'extract-elf/AGENTS.md').write_text('changed')
    with pytest.raises(ValueError, match='artifact changed'):
        select_agents(path, digest, 'extract-elf')


def test_extra_cohort_entry_rejected(tmp_path):
    path, _ = manifest(tmp_path)
    value = json.loads(path.read_text())
    value['tasks']['make-doom-for-mips'] = {}
    path.write_text(json.dumps(value))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='exact nine'):
        select_agents(path, digest, 'make-doom-for-mips')


def test_cancelled_solver_stops_remote_codex(monkeypatch):
    calls = []

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    class Environment:
        async def exec(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(OpenEvoSevenArmCodexAgent, 'exec_as_agent', cancelled)
    agent = object.__new__(AgentNineCandidate)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent.exec_as_agent(Environment(), 'codex exec --json -- instruction'))
    assert len(calls) == 1 and 'kill -TERM' in calls[0]['command']
    assert calls[0]['timeout_sec'] == 10


def test_cancelled_setup_does_not_kill_solver(monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    class Environment:
        async def exec(self, **kwargs):
            pytest.fail('Setup cancellation must not invoke solver cleanup')

    monkeypatch.setattr(OpenEvoSevenArmCodexAgent, 'exec_as_agent', cancelled)
    agent = object.__new__(AgentNineCandidate)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent.exec_as_agent(Environment(), 'mkdir -p /tmp/setup'))
