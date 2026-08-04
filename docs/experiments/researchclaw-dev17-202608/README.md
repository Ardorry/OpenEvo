# ResearchClawBench Dev17 Experiment Record

## Status

This directory records the final audited state of the local ResearchClawBench
Dev17 campaign completed during July–August 2026.

Overall publication status:

`PARTIAL_BLOCKED_WITH_VALID_INTERMEDIATE_EVIDENCE`

The complete Dev17 run did not reach a valid final completion state.

## Valid results

1. The isolated `Energy_004` v39 end-to-end gate passed.
2. The v40 formal run made valid progress and then failed closed during the
   Energy transition.
3. The repaired completed-prefix v41 passed formal identity and integrity
   verification, but continuation was rejected by Core admission rules.
4. The fresh v42 run completed the Astronomy and Chemistry task attempts and
   entered Earth.
5. The final recorded v42 state was `BLOCKED`; formal verification still
   returned `PASS`, with zero failed side effects and one pending side effect.

## Claims that must not be made

- The complete Dev17 experiment succeeded.
- Official40 was completed.
- v40, v41, or v42 produced a valid final benchmark score.
- Historical recovery state can be treated as a clean independent run.

## Repository contents

This public record contains:

- experiment summary;
- code and branch identity;
- run chronology;
- evidence index;
- integrity checksums.

The full supervisor databases, workspaces, model outputs, credentials, runtime
copies, evaluator-private data, and raw status logs remain in local archival
storage and are intentionally excluded from Git.
