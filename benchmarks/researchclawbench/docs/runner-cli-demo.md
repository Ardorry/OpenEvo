# ResearchClawBench maintainer CLI meeting demo

This package-local maintainer command uses OpenEvo Core, `CodexHarness`, and the
managed runtime. It never falls back to host `codex exec`; it is not a separate
OpenEvo end-user CLI product.

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

## 4. Fresh baseline plus native evolution command

Run only after the dry-run reports `managed_runtime_ready=true`:

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --task Life_005 \
  --run-id rcb_oe_v0_native_life005_meeting
```

This is the real fresh engineering command. It consumes model calls, uses a new
namespace, attaches only Life_005 GT, and stops before evolved Candidate/Judge.
Do not run it during a zero-call meeting demo.

The completed architecture validation used the sealed baseline and Core's
native recovery API:

```bash
.venv/bin/python -m openevo_researchclawbench.cli recover-native-evolution \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --source-run-id rcb_oe_v0_native_life005_arch_20260808T084500Z \
  --run-id rcb_oe_v0_native_life005_evolution_recovery_20260808T094500Z
```

It is now idempotent: Core reads the three completed target operations and the
existing Project Head seed without issuing another reflector call.

## 5. Status

```bash
python -m json.tool \
  /home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/successor_recovery/rcb_oe_v0_native_life005_evolution_recovery_20260808T094500Z/closed_receipt.json
```

## 6. Target operation receipts

```bash
find /home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/successor_recovery/rcb_oe_v0_native_life005_evolution_recovery_20260808T094500Z/targets \
  -type f -name '*.json' -print | sort
```

## 7. Verify/result

```bash
.venv/bin/pytest -q \
  benchmarks/researchclawbench/tests/test_native_evolution_recovery.py \
  benchmarks/researchclawbench/tests/test_runner_cli_demo.py
```

The closed receipt records the three registry artifact IDs/hashes and successor
Project Head. The source baseline remains under the original Supervisor
namespace; no result is copied into this source checkout.

## 8. Stop owned resources

```bash
.venv/bin/python -m openevo_researchclawbench.cli stop-owned \
  --protocol configs/researchclawbench/native_life005_engineering.yaml \
  --run-id rcb_oe_v0_native_life005_meeting
```

This targets only resources whose ownership is recorded by the fresh meeting
Supervisor. It is not needed for the already closed recovery validation.
