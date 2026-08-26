# ChemCrow v5 Core-native architecture audit

## Verdict

`PASS` before paid execution.

```text
CHEMCROW_CUSTOM_ROLE_PROMPTS = 0
CHEMCROW_CUSTOM_ARTIFACT_JSON_WRAPPERS = 0
CHEMCROW_ROLE_KEYWORD_VALIDATION = 0
CHEMCROW_ROLE_MIN_LENGTH_VALIDATION = 0
```

The first audit pass found one remaining semantic confounder outside those four counters: the
ChemCrow adapter still told every Reflector never to direct physical execution or procurement and
included benchmark `safety_metadata` in its evidence object. This could directly suppress explicit
synthesis/procurement obligations in tasks 05 and 13. It was removed before any v5 model call.

The v5 evidence object now contains only the current task ID/prompt/hash, the bare-S0 G1
trajectory, and the frozen allowed feedback. The adapter adds only sibling-isolation and leakage
boundary language before passing this evidence to the selected Core renderer.

## Actual path

```text
ThreeIsolatedEvolutionEngine
  -> method ID text_memory_reflector | skill_bundle_reflector | agent_system_reflector
  -> render_codex_cli_reflector_prompt in OpenEvo Core
  -> independent Core-managed Codex rollout
  -> raw Markdown validation only
  -> typed Core ArtifactRegisterRequest
  -> duplicate/baseline-copy guards
  -> registration after all three pass
  -> exact-three G2 context injection and receipt verification
  -> pair seal and bare-S0 reset
```

No adapter code rewrites generated artifact content. Type is selected by the frozen Core method/job
binding, not inferred from output keywords.

## Historical compatibility

The legacy responsibility fields remain accepted only so immutable v4 receipts can be parsed. They
are omitted from v5 serialization and are not evaluated. All 14 sealed v4 pairs and artifact
receipts round-trip to the same canonical JSON. The 246-file v4 tree remained byte-stable; the
before/after `sha256sum` stream digest is
`12d483a81f50447263c21a4a826c68f49fcb6ccee1ca0a7f61ca8baafe23b444`.

## Frozen native template hashes

| Core method | Template-contract SHA256 |
|---|---|
| `text_memory_reflector` | `01b0ddc5fd34625e56ccc2160fd75830c087b527db40ce9495f77714dd3278ef` |
| `skill_bundle_reflector` | `b2af46079cfd2a44b033344e9b110d7aa302c63afe03cc5d83814f5c126cad1d` |
| `agent_system_reflector` | `b8a6505491057131d53c981c51bbd03a06adb8478912406ab207314b8c7797d2` |

Machine-readable evidence: `CORE_NATIVE_V5_ARCHITECTURE_AUDIT.json`.

## Free verification

- Focused protocol: 20 passed.
- Full ChemCrow: 113 passed, 2 unchanged dependency warnings.
- Core worker methods: 84 passed.
- ChemCrow Ruff: pass.
- changed Core import ordering check: pass.
- `git diff --check`: pass.
- Paid/model calls: 0.
