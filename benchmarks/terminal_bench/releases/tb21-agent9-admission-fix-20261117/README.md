# OpenEvo Agent9: corrected TB2.1 admission package

This revision fixes the preparation of **new runs**. The published historical jobs retain their original configs, task refs, rewards and trajectories.

Changes:

- AgentConfig.name is openevo-agent9-codex; import_path remains the same custom class.
- AgentConfig.model_name is openai/gpt-6.1-sol. Native Codex still receives gpt-6.1-sol with high effort.
- Task admission uses the 89 pinned Registry refs in docs/registry-task-refs.json. Each actual task directory is hashed before admission and execution.
- The canonical sanitize-git-repo archive replaces the mismatching Git task as a cache source for new runs. The original Git checkout is unchanged.
- Runtime source hashes are separate from the original executed-source record. The nine frozen AGENTS files are unchanged.

## Prepare an exact content cache

```sh
python scripts/prepare_registry_cache.py \
  --source-root /path/to/pinned-git-checkout/tasks \
  --source-root /path/to/registry-export \
  --output /private/path/canonical-cache
```

With network access and Harbor login, --download-missing downloads unresolved canonical task archives without model execution. Every downloaded directory is verified against its pinned ref.

## Validate the native run setup

```sh
python scripts/prepare_run.py \
  --dataset-path /private/path/canonical-cache \
  --assets-dir /private/path/runtime-assets \
  --jobs-dir /private/path/new-jobs \
  --attempts 5
```

Default preparation performs no model calls. It creates fresh config/lock files and checks all 89 native task budgets. Actual execution is a separate --execute operation with an external --auth-path.

Recorded validation: 89 task hashes verified; 445 trial locks resolve to canonical task refs; the corrected config passes the official matching_agent function; original job config/result hashes remain unchanged. Six admission tests and nine upstream diagnostic regression tests passed.

Historical submission: https://github.com/harbor-framework/terminal-bench-2-1/pull/256
Diagnostic repair: https://github.com/harbor-framework/terminal-bench-2-1/pull/257
