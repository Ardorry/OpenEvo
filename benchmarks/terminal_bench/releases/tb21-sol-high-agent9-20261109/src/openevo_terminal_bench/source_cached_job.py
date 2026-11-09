"""Native Harbor lifecycle using a verified official source mirror as cache.

Registry availability is not fabricated. Canonical identities are accepted only
when Harbor's own content hasher proves the source bytes have those identities.
This module replaces task transport only, not agent or verifier execution.
"""
from __future__ import annotations

import json
from pathlib import Path

from harbor.environments.factory import EnvironmentFactory
from harbor.job import Job
from harbor.metrics.mean import Mean
from harbor.models.job.config import JobConfig
from harbor.models.task.task import Task
from harbor.models.trial.config import TaskConfig
from harbor.publisher.packager import Packager
from harbor.tasks.client import TaskDownloadResult
from harbor.trial.queue import TrialQueue
from harbor.trial.single_step import SingleStepTrial

from openevo_terminal_bench.hub_manifest import (
    OFFICIAL_DATASET, OFFICIAL_DATASET_REF, verify_local_tb21_mirror,
)


class SourceCacheQueue(TrialQueue):
    def __init__(self, *, downloads, **kwargs):
        super().__init__(**kwargs)
        self.downloads = downloads

    async def _execute_trial_with_retries(self, trial_config):
        if self._retry_config.max_retries:
            raise ValueError('This candidate forbids replacement of admitted trials')
        download = self.downloads[trial_config.task.get_task_id()]
        actual, _ = Packager.compute_content_hash(download.path)
        if 'sha256:' + actual != trial_config.task.ref:
            raise ValueError('Task source changed since admission')
        trial = SingleStepTrial(
            trial_config, _task=Task(download.path), _task_download_result=download,
        )
        self._setup_hooks(trial)
        receipt = {
            'transport': 'verified-official-git-source-cache',
            'registry_verified': False, 'task_name': trial_config.task.name,
            'task_ref': trial_config.task.ref, 'source_path': str(download.path),
            'content_hash_verified_before_execution': True,
            'agent_and_verifier_lifecycle': 'native Harbor SingleStepTrial',
        }
        (trial.paths.trial_dir / 'task-transport.json').write_text(json.dumps(receipt, indent=2) + '\n')
        return await trial.run()


def create_source_cached_job(config: JobConfig, dataset_path: Path) -> Job:
    metadata = verify_local_tb21_mirror(dataset_path)
    if len(config.datasets) != 1 or config.tasks:
        raise ValueError('Exactly one canonical dataset is required')
    dataset = config.datasets[0]
    if dataset.name != OFFICIAL_DATASET or dataset.ref != OFFICIAL_DATASET_REF:
        raise ValueError('Canonical immutable dataset identity is required')
    if dataset.path or dataset.n_tasks or dataset.exclude_task_names:
        raise ValueError('Only explicit smoke task selection is allowed')
    EnvironmentFactory.validate_resource_policies(config.environment)
    selected = set(dataset.task_names or (item['name'] for item in metadata['tasks']))
    tasks = []
    downloads = {}
    for item in metadata['tasks']:
        short = item['name'].removeprefix('terminal-bench/')
        if item['name'] not in selected and short not in selected:
            continue
        task = TaskConfig(name=item['name'], ref=item['ref'], source=OFFICIAL_DATASET)
        path = dataset_path / short
        instance = Task(path)
        if instance.has_steps:
            raise ValueError('This source cache runner supports single-step TB2.1 only')
        tasks.append(task)
        downloads[task.get_task_id()] = TaskDownloadResult(
            path=path, download_time_sec=0, cached=True,
            content_hash=item['ref'],
        )
    if len(tasks) != len(selected):
        raise ValueError('Task selection did not resolve exactly')
    job = Job(config, _task_configs=tasks,
              _metrics={OFFICIAL_DATASET: [Mean()], 'adhoc': [Mean()]},
              _task_download_results=downloads)
    native_queue = job._trial_queue
    job._trial_queue = SourceCacheQueue(
        downloads=downloads, n_concurrent=config.n_concurrent_trials,
        retry_config=config.retry, hooks=native_queue._hooks,
    )
    (job.job_dir / 'source-cache-provenance.json').write_text(
        json.dumps(metadata, indent=2) + '\n')
    return job
