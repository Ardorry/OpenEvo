# ChemBench supervised-transfer mechanism audit

Audit date: 2026-07-27

Repository: `/home/lhy-h/work/compare2`

Protocol: `chembench_supervised_transfer_v1`
Frozen split: unchanged

## Finding

At baseline commit `cd319dc5f5528ff1ad60f381e44ac9c0fa095aa7`, supervised
evolution updated only `text_memory`. Online Train, Probe, and Test accepted a
single memory context; no `skill_bundle` or `agent_system` Core lifecycle was
present. That was a mechanism mismatch with the clarified three-target
treatment and has been corrected in benchmark-local code.

## Current mechanism

Each category owns an independent composite head containing exactly:

1. one approved `text_memory` artifact;
2. one approved `skill_bundle` artifact;
3. one approved `agent_system` artifact.

Each Online Train update invokes the supervised reflector once. Its closed
structured response supplies the three projections. Memory is produced by the
verified text-memory reflector job; skill and agent-system projections each
pass through a separate immutable plan-bound Core job, typed artifact
registration, validator, promotion, and context resolution. A category head
advances only when all three targets pass.

The full predecessor memory, skill, and agent-system content and identities are
bound into the next same-category supervised packet. The reflector must merge,
refine, confirm, contradict, or retire prior content; it cannot silently reset
the auxiliary chains from the current item alone.

## Arm and phase behavior

- Control Train: no memory, skill, or agent-system injection; zero evolution.
- Online Train: the exact current same-category three-target head is injected.
- Probe: the checkpoint three-target heads are read-only; zero evolution.
- Final Test: the final 27-artifact set (nine categories times three targets)
  is frozen before the first Test call; Test creates zero evolution jobs and
  cannot update or reselect any component.
- Probe/Test targets and results never enter the supervised packet.

The official task prompt and demonstrations remain identical between arms.
The only treatment difference is injection of the approved frozen/evolving
three-target category context.

## Counts

Formal model-call counts remain unchanged:

- task-model calls: 4,500;
- reflector calls: 900;
- total model calls: 5,580.

Core lifecycle counts increase from one to three per Online update:

- Core plan-bound jobs: 2,700;
- approved artifacts: 2,700;
- context resolutions: 2,700.

## Evidence and current gate

The related 229-test regression passed. Frozen-identity verification and the
complete dry-run passed with zero model calls, an unchanged split, zero
holdout exposure overlap, and pristine `src/openevo/**`.

The mechanism is implemented, but all older paid preflight receipts are
invalid because they bind the earlier source. The next permitted paid action
is one new source-bound preflight run. Formal execution may begin under a
separate new run ID only after that authority verifies; its first formal
session must be Control Train task 0, round 0.
