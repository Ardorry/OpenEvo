# ChemBench supervised three-target frozen transfer v2

Protocol: `chembench_supervised_transfer_v2`

Classification: research-only supervised agent evolution; not a standard
ChemBench4K leaderboard run.

## Isolation

V2 uses new config, manifest, source, test, result, state, report, run-ID, Core
database, artifact and Test-ledger namespaces. V1 is read-only and is bound by
an aggregate file snapshot receipt. No V1 artifact, completion, partial Train
row or Test prediction can enter V2.

The only shared inputs are the revision-pinned dataset, content-free historical
exposure evidence, benchmark/Core utility code, and the OpenEvo-managed Codex
package contract.

## Managed candidate and reflector execution

Candidate sessions do not invoke Codex from the adapter. The benchmark builds
an official `TaskRequest`, pins `agent.harness=codex`, transcript capture and
the Core `managed_science` runtime, then submits it through the official local
Rollout/Gateway services. Those services run from the v2 non-editable OpenEvo
wheel environment. A private service receipt binds the source commit, topology,
wheel, runtime Python, Rollout/Gateway process identities and health state; a
formal executor refuses to submit without that live binding.

The managed subscription runtime needs provider transport, so the candidate
container permits model network transport. Tool use remains fail closed: MCP is
empty, approval is `never`, the instruction prohibits tools, and any Codex tool
event invalidates the transcript. The candidate binary is the Codex executable
inside the pinned Core managed-runtime image and is re-attested before a paid
run.

Reflector calls are initiated only while an OpenEvo plan-bound job is leased by
the Core worker. The benchmark-local provider runs the same pinned Codex native
binary in a separate Bubblewrap boundary. It stages an explicitly validated
subscription-auth source into a private temporary home, exposes no repository,
dataset or future task path, disables MCP and rejects every tool event. Raw
reflector output cannot become runtime context: all three projections must pass
their independent Core job, typed-artifact, validator, promotion and context
resolution paths first.

## Data protocol

The formal experiment has exactly two model-visible sets:

- Train: 50 items per category, 450 total;
- Test: 50 never-actually-exposed items per category, 450 total.

The other 3109 rows are Reserve and never enter a task or reflector call.
There is no Probe and Test is not evaluated during Train.

Exposure uses `ExposureTaxonomyV2`. Task attempt, completion, private
evaluation, supervised reflector input, or item-level human review excludes a
UID from Test. `MANIFEST_LISTED_ONLY`, deterministic prompt rendering, and
aggregate-only review do not represent execution exposure. Treating a
pre-generated full-stream manifest as execution would incorrectly mark all
4009 dataset rows as model-exposed.

## Train mechanism

Control uses four fresh sessions per item and no cross-session state.

Online uses:

```text
Round 0 -> Cycle 1 -> Round 1 -> Cycle 2 -> Round 2 -> Cycle 3 -> Round 3
```

Every cycle performs one isolated structured reflector call. That response has
three independent fields and is materialized through three separate verified
Core jobs, validators, promotions and context resolutions:

```text
text_memory + skill_bundle + agent_system
```

The three approved targets form one category-local context head. The Round-3
head is the predecessor for the next Train item in the same category. No state
is shared between categories.

## Paid-call plan

The selected one-call/three-artifact implementation requires:

- Control Train task calls: 1800;
- Online Train task calls: 1800;
- reflector calls: 1350;
- Core jobs/artifacts/context resolutions: 4050 each;
- paired Final Test task calls: 900;
- total model calls: 5850.

The alternative 8550-call estimate applies only when each target uses a
separate reflector call. V2 deliberately uses one schema-bound reflector call
and still validates every target independently.

## Test boundary

Before Test, V2 freezes 27 artifacts and their combined digest. Each Test UID
may produce exactly one completion in each arm. The control arm has no evolved
context; the evolved arm injects the matching category's frozen three-target
context. Test cannot create an evolution job or mutate an artifact, prompt,
split, config or source identity.
