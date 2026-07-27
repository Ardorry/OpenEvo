# ChemBench supervised-transfer OpenEvo-managed Codex audit

Audit date: 2026-07-28

Repository: `/home/lhy-h/work/compare2`

Protocol: `chembench_supervised_transfer_v1`

## Finding

Before this correction, the supervised experiment obtained both task and
reflector Codex executables from host `PATH` via `shutil.which("codex")` or a
bare `codex --version` command. On the audited WSL host that resolves to a
user-global `codex-cli 0.145.0` installation.

OpenEvo does not commit a Codex executable blob to Git. Its authoritative
managed runtime contract instead pins:

- package: `@openai/codex@0.144.1`;
- managed package root: `/opt/codex` inside the Daemon runtime;
- managed executable: `/opt/codex/bin/codex`;
- default managed model: `gpt-5.5`.

The current WSL host has no `/opt/codex`, so the earlier experiment was not
using the Codex version built into OpenEvo's managed-runtime definition.

## Correction

The benchmark runtime preparation now materializes the exact Core-declared NPM
package under the experiment-private state root. It does not update the
user-global Codex and does not modify `src/openevo/**`.

The installation validator requires:

- the exact `MANAGED_CODEX_NPM_PACKAGE` and `MANAGED_CODEX_VERSION` exported by
  the installed OpenEvo Core distribution;
- one expected package and one platform package with matching versions;
- the expected relative launcher target;
- exactly one owned native Codex executable;
- an exact `codex --version` result;
- deterministic launcher, package metadata, platform metadata, and native
  executable SHA-256 values;
- a canonical owner-private receipt.

The task executor and supervised reflector are both passed the same explicit
verified native executable. The protocol no longer uses host `PATH` discovery.
Task success receipts, reflector receipts, preflight authority, run state,
dry-run identity, and final frozen-transfer receipt bind its SHA-256 and the
managed-runtime identity digest.

Any missing installation, unexpected package version, launcher/path escape,
native-binary drift, receipt mismatch, task/reflector digest divergence, or
mid-run change fails closed.

## Experimental impact

This is an inference-runtime identity correction. It does not change:

- the frozen Train/Probe/Test/Reserve split;
- prompts or five-shot demonstrations;
- the model name or reasoning effort;
- memory/skill/agent-system evolution semantics;
- Probe/Test isolation;
- model-call budgets.

Because source and Codex runtime identity change, every earlier preflight
authority is invalid. A fresh paid preflight is required before any new formal
run. No real model call is part of this correction and validation task.

## Validation status

Implementation and non-paid validation are complete:

- the exact Core-declared `@openai/codex@0.144.1` package was materialized in
  the private experiment state root;
- its native executable reports `codex-cli 0.144.1` and has SHA-256
  `a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902`;
- the managed-runtime identity digest is
  `6a19688253de7e71e37eeab0a4eb1e831c621cf3d11334ae4055636a6d49167d`;
- 53 focused supervised/config/orchestration tests passed;
- 179 reflector and Core-lifecycle tests passed;
- 123 shared executor/security/stability/research-compatibility tests passed;
- runtime preparation, framework verification, source verification, and the
  zero-model-call complete dry-run passed;
- `src/openevo/**` remained pristine and the split identities were unchanged.

The repository-wide `ruff check benchmarks/chembench` command still reports
267 pre-existing findings across legacy benchmark files. The new managed-Codex
module, supervised config/experiment/preflight/source-manifest changes, and
their new tests pass targeted Ruff. No unrelated legacy lint cleanup was made.

The dry-run deliberately reports `ready_for_paid_smoke=false` while benchmark
source edits are uncommitted. A clean post-commit dry-run is required before a
new paid preflight.
