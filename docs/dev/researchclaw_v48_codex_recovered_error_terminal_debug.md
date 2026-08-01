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
- Build and deploy lifecycle 96 (the first replaceable identity above deployed
  lifecycle 95), prove managed readiness without a model call, and use a fresh
  run namespace for paid validation.
