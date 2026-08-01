# ResearchClaw V48 Codex recovered-error terminal debugging

## Scope and evidence boundary

- Workspace: `/home/lhy-h/work/researchclaw_openevo`
- Immutable failed namespace: `rcb_oe_v0_community17_dev17_fresh_v48`
- Failed unit: `Astronomy_004`, attempt `a0`
- The V48 supervisor state, Core records, receipts, and model-call evidence are
  immutable. This repair does not resume or rewrite V48.
- Private benchmark instructions, credentials, evaluator evidence, and model
  output are excluded from this document.

## Observed call chain

```text
Codex CLI starts one Candidate turn
  -> the response stream disconnects transiently
  -> Codex emits an `error` event and reconnects
  -> the same turn reaches `turn.completed` with valid usage
  -> the subscription transcript validator rejects the earlier `error`
  -> the harness returns code 1 despite the authoritative completed terminal
  -> Rollout records SessionResult(ERROR)
  -> Supervisor stops at CANDIDATE_SETUP_BLOCKED
```

V48 recorded one Candidate call and zero Judge and Reflector calls. The Judge
API key and Judge endpoint were therefore not involved in this failure.

## Terminal authority rule

For a normal Codex subscription execution transcript, terminal success is
accepted only when all of the following hold:

1. exactly one valid `thread.started`, `turn.started`, and `turn.completed`
   event is present in order;
2. `turn.completed` is the final event and contains the exact nonnegative usage
   schema;
3. no `turn.failed` event occurs;
4. every intermediate `error` event occurs while the turn is active and has a
   nonempty string message;
5. all item events and file identity/ownership/size checks remain valid.

This makes the final Codex terminal event authoritative while retaining
fail-closed behavior for errors before a turn, errors after completion,
incomplete transcripts, unknown events, and explicit `turn.failed` terminals.
The stricter credential-isolation canary validator is unchanged because its
purpose is to prove an exact, side-effect-bounded readiness transcript.

## Verification plan

- Reproduce a recoverable in-turn `error` followed by a valid
  `turn.completed`, including a nonzero Codex process exit, and require the
  pipeline to accept the completed terminal.
- Continue rejecting pre-turn errors, empty error messages, post-completion
  events, unknown events, invalid usage, and `turn.failed`.
- Run the focused harness tests, adjacent subscription/isolation tests, the
  broader test suite, compilation, lint, and `git diff --check`.
- Build and deploy a new lifecycle release, prove managed readiness without a
  model call, and use a fresh run namespace for paid validation.

## Lifecycle 96 deployment follow-up

Lifecycle 96 preserved the recovered Codex terminal rule, but its first remote
deployment could not finish Core startup inside the existing 300-second hard
limit. Read-only process sampling proved that startup was making continuous
CPU-bound progress through 170 owned workspace uploads totaling about 12.8 GB.
The process did not deadlock, write an invalid ledger, or start a model. Both
the initial 240-second attempt and one 300-second recovery attempt cleaned up
to the prior lifecycle-95 stopped floor with no live Daemon.

Lifecycle 97 retains the complete startup integrity scan and all default
timeouts. It only raises the maximum for an explicitly requested Daemon
`ensure` operation from 300 to 1800 seconds. Daemon staging, observation, and
stop operations keep their 300-second ceiling. This makes startup cost scale
with the already bounded durable workspace inventory without weakening file,
chunk, database, snapshot, release, predecessor, or host-key verification.

The first lifecycle-97 deployment attempt then failed locally before remote
startup: the transport forwarded the 1200-second overall ensure budget to its
short identity-probe substep, whose independent ceiling correctly remained 300
seconds. Lifecycle 98 caps that internal identity probe at 300 seconds while
passing the remaining long budget only to `service ensure`. A transport test
covers both the accepted 1200-second composition and rejection above the
1800-second overall bound.
