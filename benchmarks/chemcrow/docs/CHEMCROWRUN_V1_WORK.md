# ChemCrowRun v1.0 implementation record (local issue)

User-authorized scope: formal naming with immutable legacy provenance, exact historical runtime acquisition and review, isolated service lifecycle, fresh real smoke/full runs, calibrated evaluation, submission/statistics, and publication preparation.

This local issue records work before implementation. External issue/PR creation and publication are deferred to the user's single final publication approval. Base: codex/chemcrow-reproducibility at 7375f5ec4. Working branch: codex/chemcrowrun-v1.

Acceptance: clean local clone; public fixture acquisition; verified original runtime; isolated services; genuine one-task and 14-task runs; audit and submission; publication-ready evidence. Pending steps must never be reported as passed. Do not change historical payloads, scores, protocol thresholds or raw data. No automatic generation retries.

Initial observations: successful OpenRouter probe used the configured US proxy, GPT-4/OpenAI, one request, 44 tokens, USD 0.00147. It did not test Core, subscription roles or a complete Judge harness. A previous direct probe bypassed that proxy and returned a region-related 403. The original 352236726-byte runtime archive verifies against Core authority. No benchmark services were running at task start.

Follow-up issue: current subscription catalog excludes historical gpt-5.5. Preserve its canonical configuration and add an explicit, validated model substitution variant across all six roles. A real managed gpt-5.6-sol probe passed unchanged credential isolation. Pin source/config/topology/environment per workspace; reject changed identity at dispatch/export. Keep original submission canonical payloads valid and prevent pooling different model conditions.
