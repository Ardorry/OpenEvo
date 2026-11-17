"""Validate new TB2.1 admissions without rewriting historical records."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from harbor.models.job.config import JobConfig
from harbor.publisher.packager import Packager

DATASET = 'terminal-bench/terminal-bench-2-1'
DATASET_REF = 'sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a'
AGENT = 'openevo-agent9-codex'
IMPORT = 'openevo_terminal_bench.agent9_candidate:AgentNineCandidate'
MODEL = 'openai/gpt-6.1-sol'
REF_RE = re.compile(r'sha256:[0-9a-f]{64}\Z')


def prepare_new_config(config: dict) -> dict:
    """Return a new-run config; caller retains the original input unchanged."""
    result = deepcopy(config)
    agents = result.get('agents', [])
    if len(agents) != 1 or agents[0].get('import_path') != IMPORT:
        raise ValueError('Expected exactly the frozen AgentNineCandidate import path')
    agent = agents[0]
    if agent.get('model_name') not in ('gpt-6.1-sol', MODEL):
        raise ValueError('Unexpected model; no implicit model substitution')
    if agent.get('name') not in (None, AGENT):
        raise ValueError('Unexpected agent identity')
    if agent.get('kwargs', {}).get('reasoning_effort') != 'high':
        raise ValueError('Expected high reasoning effort')
    agent.update(name=AGENT, model_name=MODEL)
    JobConfig.model_validate(result)
    return result


def verify_sources(metadata: dict, paths: dict[str, Path], *, expected_count=89) -> dict:
    """Use Registry refs as authority; hash every chosen on-disk task."""
    if metadata.get('dataset_ref') != DATASET_REF:
        raise ValueError('Wrong Registry dataset version')
    tasks = metadata.get('tasks', [])
    refs = {t['name']: t['ref'] for t in tasks}
    if len(refs) != expected_count or len(tasks) != expected_count:
        raise ValueError('Duplicate or missing Registry tasks')
    if set(refs) != set(paths):
        raise ValueError('Task paths differ from the Registry task set')
    records = []
    for name, ref in sorted(refs.items()):
        if not REF_RE.fullmatch(ref):
            raise ValueError(f'Invalid task ref: {name}')
        path = Path(paths[name]).resolve(strict=True)
        digest, _ = Packager.compute_content_hash(path)
        actual = 'sha256:' + digest
        if actual != ref:
            raise ValueError(f'Canonical task mismatch: {name}: {actual} != {ref}')
        records.append({'name': name, 'ref': ref, 'path': str(path)})
    return {'dataset': DATASET, 'dataset_ref': DATASET_REF,
            'verified_task_count': len(records), 'tasks': records}


def write_new_config(source: Path, destination: Path) -> None:
    """Never overwrite a config, especially one belonging to an existing job."""
    result = prepare_new_config(json.loads(source.read_text()))
    with destination.open('x') as out:
        out.write(json.dumps(result, indent=2) + '\n')
