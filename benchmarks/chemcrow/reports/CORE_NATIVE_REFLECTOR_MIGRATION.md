# ChemCrow Reflector contract migration

Generated: 2026-08-27 (Asia/Shanghai)

## Finding

The frozen Memory/Skill/AgentSystem responsibility prose and its keyword-marker validator were
ChemCrow benchmark-layer additions, not OpenEvo Core contracts. Git history places their benchmark
implementation in `5089d18e3` (`feat: isolate ChemCrow artifact evolution pipelines`) and
`021fda10d` (`Prepare ChemCrow three-pipeline full-run protocol`).

OpenEvo Core independently owns the built-in `text_memory_reflector`,
`skill_bundle_reflector`, and `agent_system_reflector` methods and their Codex prompt renderers in
`src/openevo/evolution/methods.py`. The ChemCrow adapter now calls one public Core renderer for
those exact method IDs.

## Removed from active execution

- ChemCrow-specific `_SYSTEM_CONTRACTS` role prose.
- ChemCrow-specific responsibility keyword sets and minimum-token responsibility check.
- Type-specific ChemCrow JSON output wrappers.
- Runtime rejection based on missing `observation`, `checklist`, `policy`, or similar keywords.

The adapter still enforces scientific boundaries that are independent of those role definitions:
three distinct jobs/invocations/artifact IDs, sibling isolation, allowed-evidence checks, byte and
normalized duplicate rejection, frozen near-duplicate rejection, baseline-answer copy rejection,
lineage, exact-three injection, and task reset.

## Protocol identity and historical evidence

Historical `three_isolated_v1` is read-only and remains parseable without altering sealed files.
New execution uses `three_isolated_core_native_v2`, external label
`chemcrow-three-isolated-core-native-artifacts-v2`, and fresh config
`full.v5-core-native-three-pipeline.yaml`. It uses fresh run, cache, and ledger roots.

No model or paid call was made for this migration. A new live Core-native preflight is required
before treating v2 as ready for another authoritative paid run.

## Verification

- Focused three-artifact protocol tests: 20 passed.
- Full ChemCrow suite: 113 passed, 2 unchanged dependency warnings.
- All 14 sealed full-v4 pair/artifact receipts parse and round-trip to the same canonical JSON.
- ChemCrow Ruff: pass.
- `git diff --check`: pass.
