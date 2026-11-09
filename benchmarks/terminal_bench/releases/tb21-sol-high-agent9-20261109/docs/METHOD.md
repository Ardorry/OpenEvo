# Method and provenance

The solver is gpt-6.1-sol, reasoning high, through Codex 0.166.0 and Harbor 0.21.0. The nine selected task names and exact AGENTS content digests are in artifacts/manifest.json. Doom receives no artifact.

The artifacts were generated from one complete, untruncated earlier execution trajectory of the same task, task information and correctness/grading feedback. Reflection produced memory, skill and agent-system candidates; this evaluation selected only the frozen agent-system/AGENTS content. No new reflection runs occur inside the reported trial set. Referenced manifests describe the full reflection output; unused memory/skill files are not injected.

This package provides the frozen A artifacts and their source hashes. The local private evidence bundle additionally retains original A-reflection input, prompt, response and receipts. The standalone package reproduces the frozen-artifact evaluation configuration; it does not claim to recreate the entire OpenEvo evolution service or deterministically regenerate its outputs.

The presented 445 records form a post-completion selected-five cohort. Batch names A–E are presentation labels, not fresh job IDs. Each task has exactly five distinct actual trial UUIDs, preserved in data/trials.csv. Full source ancestry remains in the local submission manifest and original records.

The dataset is pinned to sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a. A clean Git source checkout at dde3cd95b80ff25af5abd99a80b6513a018ad3b4 supplies the local task cache. Harbor Packager verifies all 89 task digests before admission and each task again before execution. Registry availability is not fabricated.

Original task tests and default budgets are used. Codex/CA/uv tool binaries are preprovisioned, but the original verifier still performs dependency setup. Seven verifier timeouts in this cohort remain in the accuracy denominator. Public dependency caching was not fully implemented for these runs.

There is no matched artifact-free control arm in this reported cohort. The result measures the presented fixed harness and task-local artifacts; it cannot alone establish the causal improvement from evolution or equivalence with a different model/harness on an official leaderboard.
