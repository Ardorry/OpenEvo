# ResearchClawBench per-item runner meeting demo

This package-local maintainer CLI runs the Community per-item experiment through
OpenEvo Core. It is not a public OpenEvo end-user CLI and never falls back to a
host `codex exec` subprocess.

## 1. Enter the repository

```bash
cd /home/lhy-h/work/researchclaw_per_item_minimal
export PYTHONPATH="$PWD/benchmarks/researchclawbench/src:$PWD/src"
```

## 2. Check Judge credentials without printing them

```bash
test -d /home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/secrets
test -f /home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/secrets/judge.env
test "$(stat -c %a /home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/secrets/judge.env)" = 600
```

Do not use `env`, `printenv`, shell tracing, or display `judge.env`, OAuth files,
API keys, or tokens during the meeting.

## 3. Zero-call Community 17 plan

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item-community \
  --protocol configs/researchclawbench/per_item_community17.yaml \
  --run-id rcb_oe_v0_per_item_community17_meeting_dry \
  --dry-run \
  --no-model-calls
```

The output lists all 17 tasks and the sequence `baseline -> judge -> evolve
once -> evolved -> judge -> pair -> reset`. It reports `provider_calls=0` and
does not create a run namespace.

## 4. Life_005 engineering rehearsal

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item \
  --protocol configs/researchclawbench/per_item_community17.yaml \
  --task Life_005 \
  --run-id rcb_oe_v0_per_item_life005_meeting
```

This paid command uses fresh Candidate sessions for baseline and evolved,
performs exactly one native GT-supervised evolution cycle, writes a pair, and
resets active state. Use a new run ID if the shown namespace already exists.

## 5. Community 17 launch command (prepared, not executed here)

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item-community \
  --protocol configs/researchclawbench/per_item_community17.yaml \
  --run-id rcb_oe_v0_per_item_community17_formal_01
```

## 6. Status

```bash
.venv/bin/python -m openevo_researchclawbench.cli per-item-community-status \
  --protocol configs/researchclawbench/per_item_community17.yaml \
  --run-id rcb_oe_v0_per_item_community17_formal_01
```

## 7. Logs and receipts

```bash
find /home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/per_item_community/rcb_oe_v0_per_item_community17_formal_01 \
  -type f \( -name '*.json' -o -name '*.jsonl' \) -print | sort
```

## 8. Verify and results

```bash
.venv/bin/python -m openevo_researchclawbench.cli per-item-community-verify \
  --protocol configs/researchclawbench/per_item_community17.yaml \
  --run-id rcb_oe_v0_per_item_community17_formal_01
```

The final paired result is under the printed output root as
`paired_results.json`.

## 9. Stop owned resources

```bash
.venv/bin/python -m openevo_researchclawbench.cli per-item-community-stop-owned \
  --protocol configs/researchclawbench/per_item_community17.yaml \
  --run-id rcb_oe_v0_per_item_community17_formal_01
```

The stop command uses only Supervisor-recorded ownership. It does not target
unrelated processes, containers, or archived evidence.
