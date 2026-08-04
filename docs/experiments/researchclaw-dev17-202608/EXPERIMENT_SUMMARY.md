# ResearchClaw Dev17 Summary

## v39 Energy gate

The isolated `Energy_004` end-to-end gate passed. This result validates the
repaired Candidate, Judge, Reflector, successor-transition, and Core-control
path for the isolated task.

This gate is intermediate infrastructure and method evidence. It is not a
complete Dev17 benchmark result.

## v40 successor-recovery run

The v40 run completed Astronomy, Chemistry, Earth tasks and reached the Energy
transition. The transition later failed after method execution.

The run was preserved fail-closed. No manual database editing or unsupported
state rebinding was used to turn the run into a success.

## v41 completed-prefix recovery

The supported recovery path created a completed-prefix namespace with:

- 13 attempt receipts;
- 9 completed Reflector cycles;
- 27 Core evolution jobs;
- 13 Candidate calls;
- 13 Judge calls;
- 45 Reflector calls.

Formal identity and integrity verification passed. Continuation was later
blocked because the recovered committed head was tied to a historical
executable registry and was not admissible under the current Core release.

## v42 fresh formal run

The fresh v42 namespace imported no legacy run artifacts.

Recorded progress:

- Astronomy: all three attempts closed;
- Chemistry: all three attempts closed;
- Earth: attempt 0 admission began;
- Astronomy and Chemistry task-best attempts: attempt 2.

Final recorded state:

- stage: `BLOCKED`;
- verify status: `PASS`;
- failed side effects: `0`;
- pending side effects: `1`.

The experiment therefore remains a partial blocked run.
