# ChemBench supervised-transfer multi-target evolution design

Status: implemented; fresh paid preflight authority pending

Decision date: 2026-07-27

Protocol namespace: `chembench_supervised_transfer_v1`
Frozen split: unchanged

## Audit conclusion

The implementation at commit
`cd319dc5f5528ff1ad60f381e44ac9c0fa095aa7` evolves only the Core
`text_memory` target. The configured method is
`text_memory_expel_reflector`; every supervised update creates one text-memory
job, one text-memory artifact, and one text-memory context resolution. The
task executor, Probe freeze, and Final Test accept only
`CoreResolvedTextMemoryV2`.

It does not create, promote, resolve, freeze, or inject `skill_bundle` or
`agent_system` artifacts. This is a treatment-mechanism mismatch with the
maintainer's clarified requirement.

Formal run `st-v1-formal-20260727-15` was stopped after 355 Control sessions.
Its immutable administrative pause receipt is
`71a7da00fd34cad6beeb39d593ace108e329ac4924a3c457dbf87bf0fa2bdcb3`.
The run is non-resumable and none of its rows may be spliced into a replacement
run.

## Required treatment

Every category owns one ordered composite evolution chain:

```text
Category head 0
  -> supervised update 1
     -> text_memory artifact
     -> skill_bundle artifact
     -> agent_system artifact
  -> supervised update 2
     -> text_memory artifact
     -> skill_bundle artifact
     -> agent_system artifact
  -> next Train item
```

The nine categories remain independent. No target artifact, predecessor, Core
state, checkpoint, or runtime instruction crosses category boundaries.

Control receives none of the three targets. Online Train Round 1 and Round 2,
Online Probe, and Online Test receive the exact three-artifact context approved
for that category. Therefore the treatment difference is the complete frozen
evolved context, not text memory alone.

## One reflector call, three typed artifacts

The model-facing supervised reflector remains one call per update so the
registered call budget remains 900 reflector calls for formal Online Train.
Its closed structured response contains three independently rendered
components:

1. category text memory;
2. category skill entrypoint;
3. category agent-system instructions.

The existing verified Core `text_memory_expel_reflector` job owns the one
reflector invocation. The isolated benchmark wrapper verifies and binds the
complete structured response, then returns only the memory projection to that
Core method. The two other projections are not adapter-side fake artifacts:
they are supplied to the existing verified Core `skill_bundle` and
`agent_system` materializer methods, each through its own immutable plan-bound
job and typed `ArtifactRegisterRequest` lifecycle.

This design avoids three independent model calls that could learn inconsistent
heads and would increase paid calls by 1,800. It also avoids a benchmark-local
unverified method or any `src/openevo/**` modification.

Per formal update the expected Core counts become:

- reflector calls: 1;
- plan-bound jobs: 3;
- approved artifacts: 3;
- context resolutions: 3.

Formal Online Train totals become 900 reflectors, 2,700 Core jobs, 2,700
approved artifacts, and 2,700 context resolutions. Task-model and total model
call budgets remain unchanged.

## Core lifecycle

The memory target continues to use:

```text
SupervisedEvolutionPacketV1
  -> event ingestion
  -> Core dataset artifact
  -> immutable text_memory plan/job
  -> isolated reflector
  -> typed text_memory artifact
  -> validator
  -> promotion
  -> text_memory context resolution
```

The verified structured reflector receipt additionally binds canonical skill
and agent-system projections. For each projection the same update then uses:

```text
bound structured projection
  -> immutable target plan/job
  -> verified built-in materializer method
  -> typed target artifact
  -> target validator
  -> promotion
  -> target context resolution
```

The composite category head advances only after all three target lifecycles
pass. If any target fails, the category/run fails closed and no later round or
task may consume a partial head.

## Structured output contracts

### Memory

The existing confirmed/provisional/retired rule schema, evidence binding,
capacity limits, and leakage checks remain unchanged.

### Skill

The skill projection contains closed arrays for:

- applicability triggers;
- ordered reasoning workflow;
- validation checks;
- failure guards.

It renders to one bounded `SKILL.md`. It must be category-specific, actionable,
free of item literals and answer maps, and contain no paths, UID, source index,
or benchmark lookup instruction.

### Agent system

The agent-system projection contains bounded directive objects. Each directive
has a trigger, instruction, validation check, and supporting Train evidence
digests. It renders to one bounded `AGENTS.md`-compatible instruction payload.
It may specify reasoning and output discipline but may not contain task/answer
mappings or operational filesystem instructions.

## Validators

All three artifact payloads independently undergo:

- exact UTF-8 and byte/token capacity checks;
- closed heading/schema checks;
- exact, normalized, and Unicode token-aligned Train literal checks;
- answer-map detection;
- UID, path, source-index, and dataset-lookup checks;
- Train-only evidence/provenance checks;
- predecessor artifact and payload binding;
- typed Core artifact and method identity checks;
- Core context-selection and payload digest checks.

Findings remain content-free: code, length, digest, category, target, and
artifact identity only.

## Runtime context and prompt parity

A benchmark-local capability DTO binds the three approved Core artifacts and
their content/context digests. The executor independently reconstructs the
actual injected sections and emits one context-binding receipt covering all
three targets.

The official ChemBench prompt and demonstrations remain byte-identical between
arms and remain the final prompt suffix. Online adds three fixed internal
sections before that prompt:

1. approved Core-resolved category memory;
2. approved Core-resolved category skill;
3. approved Core-resolved category agent system.

Control adds none. No target, correctness, Probe result, or Test result enters
these sections.

## Probe and Test freeze

Every checkpoint freezes a complete nine-category artifact set for all three
targets. Probe reads but never updates it. The final receipt binds 27 artifact
IDs, payload digests, component content digests, and context-resolution
digests. Final Test claims the existing single-use Primary Test ledger only
after this complete receipt exists.

Test remains single-pass and creates zero evolution jobs/artifacts/context
updates. A missing or mismatched component fails closed; it never falls back to
memory-only execution.

## State and receipt changes

Public state counters retain `reflector_completions` as model reflector calls
and change Core expectations from one to three jobs/artifacts per update.
Receipts explicitly record per-target counts, method identities, artifact IDs,
payload hashes, validation hashes, and context hashes.

Preflight authority must bind the new source and target set. Every authority
created before this change is invalid. The replacement sequence is:

1. focused unit and synthetic Core lifecycle tests;
2. complete zero-call dry-run;
3. relevant Ruff and full benchmark regression;
4. local source commit;
5. new preflight run ID;
6. independent preflight authority verification;
7. new formal run ID from Control task 0.

## Non-goals and preserved boundaries

- No split/manifests are regenerated or modified.
- No historical result directory is deleted or overwritten.
- No failed/paused run is resumed.
- No `src/openevo/**` change is permitted.
- No skill/tool is allowed to call shell, filesystem, web, MCP, plugins,
  subagents, or external search during task execution.
- The experiment remains research-only and is not an official leaderboard
  result.

## Implementation and non-paid validation

The benchmark-local implementation now matches this design. No frozen split,
private manifest, historical result, or `src/openevo/**` file was changed.

Validation completed on 2026-07-27:

- 229 related supervised-transfer, executor, reflector, and Core lifecycle
  tests passed as one combined regression;
- the full 132-test Core lifecycle file passed once, and one later isolated
  retry confirmed that a transient `/tmp` identity race was environmental;
- supervised-transfer Ruff checks and focused `E/F/I` checks passed;
- Python compilation and supervised shell syntax checks passed;
- frozen-identity `verify` and complete `dry-run` both passed with zero model
  calls;
- dry-run recorded 900 formal reflector calls, 2,700 Core jobs/artifacts,
  complete three-target checkpoint sets, and zero Probe/Test evolution jobs.

Every pre-existing paid authority predates this mechanism and is invalid for
reuse. A new source-bound preflight run ID is required before a new formal run
may start at Control Train task 0.
