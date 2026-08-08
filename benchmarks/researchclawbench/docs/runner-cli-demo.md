# ResearchClawBench runner CLI meeting demo

This command uses the production control, OpenEvo Core v2, `CodexHarness`, and
managed runtime. It never falls back to host `codex exec`.

## 1. Enter the repository

```bash
cd /home/lhy-h/work/researchclaw_per_item_minimal
export PYTHONPATH="$PWD/benchmarks/researchclawbench/src:$PWD/src"
```

## 2. Check the closed inputs without displaying secrets

```bash
test -f configs/researchclawbench/native_life005_engineering.yaml
test -n "${CODEX_HOME:+set}"
/usr/bin/docker version --format '{{.Server.Version}} {{.Server.APIVersion}}'
```

Do not use `env`, `printenv`, shell tracing, or display `auth.json` in a meeting.
This engineering validation does not invoke the Judge.

## 3. Zero-call demo

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --task Life_005 \
  --run-id rcb_oe_v0_native_life005_meeting \
  --dry-run \
  --no-model-calls
```

This prints task discovery, the Core/CodexHarness route, planned stages, output
root, managed readiness, and `provider_calls=0`. It creates no run namespace.

## 4. Baseline plus native evolution

Run only after the dry-run reports `managed_runtime_ready=true`:

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --task Life_005 \
  --run-id rcb_oe_v0_native_life005_meeting
```

The command runs one baseline, attaches only Life_005 GT, creates native
memory/skill/agent-system artifacts, and stops before evolved Candidate/Judge.

## 5. Status

```bash
.venv/bin/python -m openevo_researchclawbench.cli status \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --run-id rcb_oe_v0_native_life005_meeting
```

## 6. Logs and receipts

```bash
find /home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/supervisor/rcb_oe_v0_native_life005_meeting/operations \
  -type f -name '*.json' -print | sort
```

## 7. Verify/result

```bash
.venv/bin/python -m openevo_researchclawbench.cli verify \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --run-id rcb_oe_v0_native_life005_meeting
```

State is in `training-supervisor.sqlite3`; immutable operation receipts are
under `operations/`; Core artifact IDs and Project Head are in successor
receipts.

## 8. Stop owned resources

```bash
.venv/bin/python -m openevo_researchclawbench.cli stop-owned \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --run-id rcb_oe_v0_native_life005_meeting
```

This targets only resources whose ownership is recorded by the supervisor.
