# ChemBench Temperature Full-Evolve v1

## Status and execution boundary

This is a new, exploratory, Temperature-only supervised-transfer protocol. A
fresh formal run may be created only after the zero-model capacity and exposure
gate proves at least 100 never-exposed Train items and 100 never-exposed Test
items. The gate runs before split generation, run-root creation, service launch,
credential staging, tmux creation, or any Candidate, Reflector, Core, or
baseline model side effect.

The current frozen dataset does not pass that gate. The formal runner is
therefore intentionally not implemented or launched on this branch. This is a
fail-closed protocol outcome, not a partially completed experiment.

## Intended protocol after a future data revision passes the gate

- Use a fresh generation-zero namespace and never import prior completions,
  databases, workspaces, caches, or artifacts.
- Select the largest feasible equal Train/Test tier in 200, 175, 150, 125, 100.
- Freeze a deterministic group-aware split before model calls.
- Process Train in ordered batches of 25: 25 pre-evolve Candidate calls, one
  logical supervised Reflector synthesis, three Core target jobs, then 25
  post-evolve diagnostic calls. Post-evolve results never trigger a second
  update.
- Maintain a structured cumulative rule/evidence index. Project facts only to
  text memory, executable reasoning procedure only to the skill, and stable
  behavior/output discipline only to the agent system.
- Enforce an actual serialized combined injection hard limit of 8 KiB, with a
  6 KiB target and target-specific budgets. Compression must be structural;
  byte truncation is forbidden.
- Preserve all checkpoints C0 through Ck. Freeze Ck before Test, run evolved
  Test first, then an isolated generation-zero baseline on the identical Test
  order and inference stack.
- Route Candidate, Reflector, evolved Test, and baseline inference through the
  formal OpenEvo managed harness. Test feedback, Reflector calls, Core jobs, and
  artifact updates are zero.
- Store per-item evidence only in owner-private run state. Public reports contain
  aggregate metrics and digests only.

## Required implementation before any future formal launch

Existing STV3 implements per-item evolution and cannot be silently reused as a
batch protocol. A future data-qualified branch must add a benchmark-only
package with a closed Batch25 packet, canonical rule evidence, deterministic
three-target projection, an atomic batch ledger, exactly-once recovery across
all Candidate/Core boundaries, harness-backed Reflector inference, the 8 KiB
combined budget, 600-second monitoring, and aggregate-only paired reporting.

No change to `src/openevo/**` is required by the present review. Historical run
trees remain immutable and read-only.
