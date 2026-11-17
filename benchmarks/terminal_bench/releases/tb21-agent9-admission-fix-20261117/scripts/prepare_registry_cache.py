"""Resolve every pinned Registry task to matching local source bytes."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from harbor.publisher.packager import Packager
from openevo_terminal_bench.admission import verify_sources


async def main(args):
    pinned = json.loads((ROOT/'docs/registry-task-refs.json').read_text())
    selected, missing = {}, []
    for task in pinned['tasks']:
        short = task['name'].split('/', 1)[1]
        for root in args.source_root:
            path = root/short
            if not (path/'task.toml').is_file():
                continue
            digest, _ = Packager.compute_content_hash(path)
            if 'sha256:' + digest == task['ref']:
                selected[task['name']] = path
                break
        else:
            missing.append(task)
    if missing and args.download_missing:
        from harbor.models.task.id import PackageTaskId
        from harbor.tasks.client import TaskClient
        ids = [PackageTaskId(org=t['name'].split('/', 1)[0],
                             name=t['name'].split('/', 1)[1], ref=t['ref']) for t in missing]
        result = await TaskClient().download_tasks(ids, output_dir=args.download_dir, export=True)
        selected.update((task['name'], path) for task, path in zip(missing, result.paths, strict=True))
    elif missing:
        raise ValueError('Missing canonical task bytes: ' + ', '.join(t['name'] for t in missing))
    verified = verify_sources(pinned, selected)
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output/'registry-cache.json'
    with output.open('x') as stream:
        json.dump(verified, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'task_count':verified['verified_task_count'], 'manifest':str(output),
                      'model_calls':0}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, action='append', default=[])
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--download-missing', action='store_true')
    p.add_argument('--download-dir', type=Path)
    asyncio.run(main(p.parse_args()))
