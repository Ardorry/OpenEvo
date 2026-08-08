# ResearchClawBench runner CLI meeting demo

This reuses the production control/ports, Core v2, and managed Codex harness;
it does not use the engineering `CodexEngineeringPort` subprocess route.

## 1. Enter the repository

```bash
cd /home/lhy-h/work/researchclaw_per_item_minimal
```

## 2. Check environment without displaying secrets

Set a fully pinned native ResearchClaw protocol and a fresh namespace:

```bash
export RCB_PROTOCOL=/absolute/path/to/native-protocol.yaml
export RCB_RUN_ID=rcb_oe_v0_meeting_demo_earth_004
export RCB_TASK=Earth_004
test -f "$RCB_PROTOCOL"
test -n "${RCB_JUDGE_API_KEY:+set}"
test -n "${RCB_JUDGE_BASE_URL:+set}"
test -n "${RCB_JUDGE_MODEL:+set}"
```

Do not use `env`, `printenv`, or shell tracing in the meeting.

## 3. Zero-call demo

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item \
  --protocol "$RCB_PROTOCOL" \
  --task "$RCB_TASK" \
  --run-id "$RCB_RUN_ID" \
  --dry-run \
  --no-model-calls
```

This prints discovery, route, stages, output, readiness, and `provider_calls=0`;
it creates no namespace and issues no Candidate, Evolution, or Judge call.

## 4. Real single-task command

Run only after `managed_runtime_ready=true` and all normal preflights pass:

```bash
.venv/bin/python -m openevo_researchclawbench.cli run-per-item \
  --protocol "$RCB_PROTOCOL" \
  --task "$RCB_TASK" \
  --run-id "$RCB_RUN_ID"
```

The live command rejects existing namespaces and host `codex exec` fallback.

## 5. Startup fields

- Candidate identifies the Core-owned Codex harness and GPT-5.5.
- Evolution identifies the managed reflector and native artifact registry.
- Judge identifies OpenRouter, GPT-5.1, and Azure-only routing.
- Passes reflect the existing two-round production supervisor.
- Core attaches closed evaluator feedback while keeping credentials private.

## 6. Status

```bash
.venv/bin/python -m openevo_researchclawbench.cli status \
  --protocol "$RCB_PROTOCOL" \
  --run-id "$RCB_RUN_ID"
```

## 7. Logs and receipts

Use the `Output` path printed at startup as `RCB_STATE_ROOT`:

```bash
export RCB_STATE_ROOT=/absolute/output/path/from/startup
find "$RCB_STATE_ROOT/operations" -type f -name '*.json' -print | sort
```

## 8. Result verification

```bash
.venv/bin/python -m openevo_researchclawbench.cli verify \
  --protocol "$RCB_PROTOCOL" \
  --run-id "$RCB_RUN_ID"
```

State is in `training-supervisor.sqlite3`; receipts are under `operations/`.

## 9. Stop

```bash
.venv/bin/python -m openevo_researchclawbench.cli stop-owned \
  --protocol "$RCB_PROTOCOL" \
  --run-id "$RCB_RUN_ID"
```

Only provably owned processes are stopped; Core resources fail closed. Never
display API keys, OAuth files, tokens, or credential paths.
