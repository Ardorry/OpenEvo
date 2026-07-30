# Draft — Maintainer Protocol Confirmation

Status: `NOT_SENT`

This is a draft only. It must not be sent without a separate user-authorized action.

## Email draft

**Subject:** Protocol confirmation request for a frozen OpenEvo agent-system evaluation on ResearchClawBench

Dear ResearchClawBench maintainers,

We are preparing a ResearchClawBench evaluation of OpenEvo, using OpenEvo only to evolve a general `agent_system` artifact before the official test run. Before running or submitting any official 40-task result, we would like to confirm that our protocol matches your intended leaderboard rules.

Our proposed conservative protocol is:

1. We use only the 17 Hugging Face community tasks as development data, with a fixed 11-task Dev / 6-task Validation split created before any scoring.
2. OpenEvo evolves an `agent_system` only on the 11 Dev tasks. Candidate agents cannot read `target_study`, checklists, target papers/images, judge credentials, `_score.json`, or judge reasoning.
3. Validation is used only to select one already generated candidate artifact. Validation details are not returned to the reflector and no further evolution occurs after selection.
4. Before official testing, we freeze the OpenEvo commit, adapter commit, artifact bytes/hash, Codex CLI version, exact model/provider/reasoning level, tools, network policy, dependency image/lock, time/token/cost budgets, task order, randomness policy, and scorer/judge identities.
5. The same frozen artifact and configuration are used for every official task. Candidate state, home, cache, trajectory, and memory are reset per task.
6. During all official candidate runs, no scorer is called and no completion, validity, runtime, cost, score, rubric, or judge feedback is returned to OpenEvo or later official runs. We score only after all candidate runs are sealed.
7. Baseline and treatment differ only by the presence of the frozen `agent_system`; all other execution and scoring conditions are identical.
8. We retain complete run folders, candidate transcripts, code, outputs, reports/images, hashes, configuration, cost/runtime records, and isolation receipts.

Could you please confirm the following points explicitly?

### A. Eligibility and submission channel

1. May an external team run the official 40 tasks locally under the protocol above and submit the results to the public leaderboard?
2. What is the current official submission channel: email, form/Space, pull request, or another workflow?
3. Is human review required before an entry is imported, and who updates the leaderboard JSON/data?
4. Are imported historical runs accepted under the same evidence and scoring requirements as runs produced by the current local workflow?

### B. Required run evidence

5. What exactly constitutes a “complete run folder” for submission? Please confirm whether you require each of the following: `_meta.json`, `_agent_output.jsonl`, `INSTRUCTIONS.md`, `code/`, `outputs/`, `report/report.md`, `report/images/`, `_score.json`, full agent trajectory/trace, agent configuration, model/provider version, agent version, source Git commits, dependency lock/image digest, runtime and API cost information.
6. Must agent code, wrapper code, trajectories, and reports be made public, or may some evidence be shared privately for review?
7. Do you require a specific run-folder naming convention, archive format, manifest, logo, or agent-name/version convention?

### C. Scoring authority

8. Should submitters run `evaluation/score.py` locally and submit `_score.json`, or will maintainers rerun/re-score every submitted report?
9. What exact judge model, provider, prompt revision, temperature/retry policy, and image limits should be pinned? The current source appears to use at most five generated images plus one target image per image item; is that the intended leaderboard rule?
10. If a scorer request fails, may it be retried on the identical sealed run, and how should multiple scorer attempts be recorded?

### D. Offline evolution and fixed-agent identity

11. Is development-time evolution on the separate 17 community tasks acceptable if the final artifact is frozen before any official task and all development data/lineage are disclosed?
12. Would you consider the frozen artifact plus fixed wrapper/model/configuration one agent version for leaderboard purposes?
13. Should the baseline and OpenEvo treatment appear as separate versioned agent names, and what naming format do you prefer?

### E. Within-task and cross-task adaptation

14. During one task, may a fixed autonomous agent iteratively modify its own code, working memory, or internal plan based only on the public workspace, its own tool outputs, and execution errors, without any checklist/judge access?
15. May state learned from an earlier official task be carried into a later official task if it uses only the agent's own trajectory/public outputs and no judge feedback? We currently propose **no** cross-task state unless you explicitly approve it.
16. We assume that using `_score.json`, judge reasoning, checklist-derived signals, or any hidden rubric feedback to update the agent across official tasks is not acceptable for a standard leaderboard entry. Is that correct?
17. Is repeatedly running the same official task, inspecting its judge score/reasoning, changing the agent, and submitting the best result prohibited as test-set/rubric tuning?

### F. Pass@1 and Pass@5

18. Please provide the formal Pass@1 and Pass@5 definitions used by the leaderboard.
19. For Pass@5, must all five runs use byte-identical agent artifact, wrapper commit, model/provider, reasoning level, tools, network policy, and budgets, with only provider sampling/random seed variation?
20. Must the five runs be independent with fresh home/cache/memory, and may no run read another run's trajectory, artifacts, score, or judge reasoning?
21. Is the displayed Pass@5 value the best of five, and should submissions also include/report mean, standard deviation, minimum, maximum, and all five complete run folders?
22. May the scorer be rerun independently per attempt, and must the scorer configuration remain identical?

### G. Network, tools, and resource constraints

23. Are internet access, scholarly/web search, package installation, custom tools/MCP servers, and external APIs allowed during official candidate runs? If so, what restrictions and disclosure are required?
24. How should agents prevent retrieval of the hidden target paper when internet access is allowed? Would you prefer internet disabled for comparable submissions?
25. Are there official limits for wall time, model steps/rounds, input/output/total tokens, cost, CPU, memory, storage, concurrency, or model release/cutoff date?
26. Are different dependency images or scientific packages permitted if baseline and treatment use the same pinned image?

### H. Retry and failure handling

27. Which infrastructure failures permit a fresh retry, and which outcomes must remain failed attempts?
28. Is in-place resume ever allowed, or should every launched retry receive a new run ID and preserve the failed predecessor?
29. If artifact validation fails before scoring, may the same fixed agent be run again under a predeclared retry rule, or must it remain a failed attempt?

If any part of this protocol belongs in a separate track rather than the standard leaderboard, we would appreciate guidance on the appropriate label or submission format. We will not run the official 40 tasks, invoke the official judge, claim Pass@5, or use cross-task evolution until the relevant points are confirmed.

Thank you for your guidance and for maintaining ResearchClawBench.

Best regards,

[Name]

[Affiliation / team]

[Contact information]

## Response-recording rule

When a response is eventually received, record its URL/message ID/date and quote only the minimum authoritative text needed to support each boolean. Ambiguous, conditional, or missing answers remain `false` or `PENDING_MAINTAINER_CONFIRMATION`; they are never inferred from leaderboard display behavior or an existing agent name.
