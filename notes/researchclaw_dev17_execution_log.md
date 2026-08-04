# ResearchClawBench Dev17 execution log

## 2026-08-01T04:58:44Z - takeover and v38 audit

- Run: `rcb_oe_v0_community17_fresh_efficiency_v38`
- Task/attempt: `Energy_004/a0`
- Repository baseline: branch
  `feat/researchclawbench-community-evolution-v0`, HEAD
  `36d31848e87c486d02f0110625f22d1e0d7413a6`, clean worktree.
- Fix branch: `fix/researchclaw-dev17-successor-recovery`.
- Supervisor evidence: `EVOLUTION_RUNNING`, revision 148, one planned
  `Energy_004/a0/evolution` side effect, no active owned resource.
- Candidate: completed and sealed. Validator failed the artifact. Judge was
  skipped. Feedback attachment was sealed.
- Core transition: two failed attempts, no commit, no successor artifact, no
  successor head, predecessor head remains active.
- Core Evolution evidence: no Energy transition job, no Reflector inference
  reservation, no active lease, no retry receipt, no staged artifact, and no
  job created in the failure window. Candidate/Judge/Reflector call difference
  required for source recovery: 0/0/0.
- Core generation and release still match the source run. Current repository
  source identity differs from the frozen v38 identity, so current CLI status
  and verify correctly fail closed on source drift; they do not invalidate the
  immutable v38 evidence.
- Root defect: retry exhaustion is not persisted as a typed adapter operation
  receipt even though Core's append-only recovery protocol can recover the
  exact pre-job failure in a new generation.
- Evidence snapshot:
  `/home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/audit/dev17_takeover_20260801/core-evolution-v38-readonly.sqlite3`
  (`sha256=6368768df958fe42810e5b53a73fdc775476ed2710fa537244448afe3b776c86`).
- Safety: v38 was not mutated; no model, evaluator, or recovery target call was
  made; no runner was started; no secret was recorded.

## 2026-08-01T05:31:49Z - repaired release deployment preflight

- Code fix commit: `842186ca757247501a03cca3e2080f81f448ee05`.
- No-model checks: new adapter recovery tests 6/6 passed; existing Core
  successor recovery suite 77/77 passed; focused lint passed.
- Built immutable release
  `release-dev17-successor-recovery-20260801T051743Z` with release identity
  `3257c829...`, then attempted the official managed Core acquisition path.
- Acquisition stopped before daemon mutation with `daemon_bundle_failed`.
  The old formal Core remained healthy and unchanged.
- Root cause: v0.1.10's predecessor observer recognized only the published
  lifecycle-16 v0.1.9 profile. The live v38 predecessor is an exact, distinct
  lifecycle-85 v0.1.9 profile (release `a56fab...`, bundle `9fa598...`, source
  `3a7ee...`) proven by its private ledger and authenticated discovery.
- Repair: admit that one full predecessor profile and bind equal-lifecycle
  replacement to all frozen ledger and discovery digests. Generic v0.1.9 and
  partially matching identities remain rejected.
- Focused regression result: exact public and formal predecessor cases 2/2
  passed; any formal profile digest drift is rejected.
- Safety: no model call, no formal run mutation, and no secret output. The
  failed release/bootstrap namespace is preserved and will not be overwritten.

## 2026-08-01T05:47:08Z - repaired Core deployment and Energy gate authorization

- Core repair commit: `dc1d879bb6732c1490905d16d5b79f141ca4e2f7`.
- Full Core host-service regression: 60/60 passed. Focused adapter and Core
  successor recovery regressions remain green.
- New release namespace:
  `release-dev17-successor-recovery-v2-20260801T053532Z`.
- Release identity `e39ab71f...`, registry `8755006f...`, daemon bundle
  `b8a22c29...`, manifest `86af7fb5...`, source commit `dc1d879bb...`.
- Official deployment receipt proves predecessor generation `d58256de...` was
  stopped and replacement generation `f8c903ed...` started. Candidate, Judge,
  Reflector, and model start flags are all false.
- Fresh no-model execution/mount/runtime readiness receipts passed; pinned
  Codex CLI started only for readiness and `model_started=false`.
- Official `formal-v11-prepare` created isolated Energy gate v39 and clean
  Dev17 v40 protocols with zero model/Judge calls.
- Credential-backed formal validate passed for both namespaces. Energy v39
  live no-model preflight passed with no Candidate or evolution intent.
- Authorized next paid scope: one protocol-bounded `Energy_004` v39 gate only.
  It must close three Candidate attempts, Judge workflow, two Reflector cycles,
  six evolution jobs, and final verification before Dev17 v40 can start.

## 2026-08-01T06:51:02Z - Energy_004 isolated end-to-end gate passed

- Run: `rcb_oe_v0_energy004_task_gate_v39`; isolated from every formal run.
- Result: runner exit 0, terminal `FINAL_FROZEN`, gate passed with no blocker,
  and two independent post-run inspections both returned verify `PASS`.
- Calls/jobs: Candidate 3, Judge 3, Reflector model calls 10, Reflector cycles
  2, and Core successor jobs 6. No retry enlarged the protocol budget.
- Successor cycle 0: transition `successor-cd4fc3c674735bf25ec5508f3a5a474d`,
  predecessor `project-head-ba0391e0...`, successor
  `project-head-e8e9d075...`, workspace `workspace-80b95115...`, commit receipt
  SHA-256 `740bf791...`.
- Successor cycle 1: transition `successor-3f1ae4bfc036a69228e462c7cabff3b3`,
  predecessor `project-head-e8e9d075...`, successor
  `project-head-64935250...`, workspace `workspace-f522b0ab...`, commit receipt
  SHA-256 `d62ad2b0...`.
- Core readback: all six transition-bound jobs succeeded on attempt 1, all
  leases are cleared, artifacts are sealed, both transitions remain committed,
  and neither transition was discarded.
- Final handoff: the protocol's cross-task sanitation/historical-restore step
  produced active generation-4 head `project-head-2c7124c3...` with matching
  workspace `workspace-1b74d4f8...`; final freeze then succeeded. This explains
  why the frozen active head is later than the two evolution heads.
- Global Core audit: SQLite integrity `ok`; active jobs 0, jobs retaining a
  lease 0, started Reflector reservations 0. The two failed jobs in the store
  are terminal historical transitions with no lease, not Energy work or
  orphan jobs.
- Idempotency check: calling terminal reconciliation twice returned the same
  terminal pause; supervisor state, revision, side-effect inventory, budget
  usage, attempt inventory, and active-resource inventory were unchanged.
  Candidate/Judge/Reflector/Core deltas were all zero.
- Evidence:
  `logs/energy004_task_gate_v39_20260801T054437Z/` including
  `gate_receipt.json`, `post_gate_inspect_{1,2}.json`,
  `core_global_readonly_audit.json`, `core_energy_transition_readonly_audit.json`,
  `core_project_head_readonly_audit.json`, and the immutable read-only Core
  snapshot (`sha256=8e54f8489f5c49a5e1c3dd86f2ecc7d8835b32e886f2598a38cf8482fc671703`).
- Safety: no debug artifact was copied to v38 or v40; no supervisor/receipt was
  edited; no completed call was replayed; no secret was recorded.

## 2026-08-01T07:01:12Z - clean Dev17 v40 formal launch

- Run: `rcb_oe_v0_community17_dev17_successor_recovery_v40` from a previously
  absent supervisor namespace; this is a clean formal run, not a v38 resume.
- Official prelaunch validate: `PASS`, blockers empty, namespace available,
  model/Judge started false, frozen inventory 17 tasks / 51 Candidate / 34
  Reflector cycles / 102 Core jobs.
- Launch session: `rcb_oe_community17_dev17_v40`, runner PID 409990. Initial
  durable state was `INITIALIZED` revision 1 with zero calls and verify `PASS`.
- First execution state: `Astronomy_004/a0`, `CANDIDATE_RUNNING`, revision 3.
- First monitor poll: Candidate/Judge/Reflector budget usage 1/0/0, Core jobs
  0, one expected in-flight side effect, failed effects 0, active/orphan owned
  resources 0, Core health `PASS`, generation `f8c903ed...`, formal verify
  `PASS`, and 925490668 KiB disk available.
- Monitor session: `rcb_oe_community17_dev17_v40_monitor`, PID 410533, fixed
  1200-second interval. Two identical durable progress timestamps or a
  nonterminal runner exit cause a fail-closed anomaly exit for debugging.
- Debug v39 receipts and workspaces were not imported. No historical run state
  or receipt was copied into v40.

## 2026-08-01T10:10:00Z - v40 Energy_004 post-method failure audit

- Run/task: `rcb_oe_v0_community17_dev17_successor_recovery_v40`,
  `Energy_004/a0`; supervisor remains immutable at `EVOLUTION_RUNNING`,
  revision 150, with one planned evolution side effect.
- Candidate, evaluator attachment, and all three target jobs completed. Core
  jobs are `succeeded` on attempt 1; completed Reflector reservations total 3
  jobs for this transition and no lease or started reservation remains.
- The transition's first Core attempt failed retryably before jobs. Its second
  attempt reached `materializing` (`4/6`) and failed terminally with no commit,
  successor artifact authority, materialized-context row, project-head update,
  or workspace handoff. The exact predecessor is still active.
- The runner exited 2 and the formal runner session is down. The monitor was
  stopped before debugging; no third retry was issued.
- Failure evidence is preserved under
  `logs/community17_dev17_successor_recovery_v40_20260801T054437Z/failure_energy004_a0_20260801T093706Z/`.
  Read-only snapshots include Core control, science tasks, Evolution,
  successor recovery, and service ledger identities.
- Exact offline reproduction with the deployed v0.1.10 wheel and formal
  framework lock passed both projection and materialization using the three
  selected sealed payloads. This rules out payload/projection incompatibility;
  the original 422 detail was not durably retained by the service log window.
- Existing append-only successor recovery supports only an empty pre-job
  inventory or one failed paid job. It cannot safely encode three already
  succeeded and admitted jobs. A fresh formal run would repeat closed paid
  calls and is rejected.
- Chosen repair: add a Core-fenced, read-only proof for the exact completed job
  inventory, then append a `reconciliation_only` transition attempt that can
  execute only validate/materialize/workspace/atomic-commit. Preserve the
  terminal adapter receipt and append a separate reconciliation receipt.
- Model-call delta during audit and offline reproduction: Candidate 0, Judge 0,
  Reflector 0, other model 0, Core jobs 0.

## 2026-08-01T11:38:17Z - completed-prefix repair releases v3-v5

- Local repair commits: `331a64b92d536ded5e098a2923288502651f82df`
  adds verified completed-prefix continuation; `adeafa1a67fbe2f3d4dd32954e31304faf95231d`
  moves the completed-method contract to daemon lifecycle 86.
- Release v3 is an immutable build failure because the build environment did
  not expose `uv` on `PATH`. Release v4 built successfully but its first deploy
  used the wrong SSH port and stopped before any remote side effect. The
  corrected v4 deploy stopped the exact lifecycle-85 predecessor, then
  correctly rejected the same-lifecycle repair.
- Release v5 built from `adeafa1a...` and deployed successfully to the owned
  release-host fixture on port 50527. Generation is `a750812b...`, lifecycle
  86, release identity `e3e7e730...`, registry `d42b5f04...`; deployment and
  all execution/mount/runtime readiness receipts report Candidate, Judge,
  Reflector, and model start flags false.
- A distinct formal repair-executor protocol validated against v5. No formal
  namespace was initialized and no model call or Core job was started.

## 2026-08-01T12:09:45Z - lifecycle-86 cross-generation defect isolated

- The first completed-prefix executor call reused the exact v40 terminal
  checkpoint and stable reconciliation identity, but Core returned 409 before
  appending a reconciliation attempt. Transition attempt count stayed 2,
  commit remained absent, artifact count stayed 0, and the Supervisor retained
  its one planned effect.
- One safe, same-identity diagnostic replay was issued only after the absence
  of side effects was proved. Its closed Core detail was `successor service
  authority changed after Attempt execution`.
- Root cause: ordinary `_evolution` correctly requires the current registry and
  framework lock to equal the captured Attempt. The repair release necessarily
  has new identities (`d42b5f04...` current versus `8755006f...` captured),
  while the runtime identity remains exact. That ordinary check was
  incorrectly reused by `reconciliation_only`.
- A read-only snapshot of the idle Evolution database passed integrity check
  (`sha256=dfa2fa5f...`). It proves all three v40 jobs are still succeeded once,
  no transition materialized context exists, and the transition therefore
  needs one current-registry context publication without any model/job replay.
- Repair in progress: lifecycle 87 preserves strict ordinary behavior, reads
  historical jobs against their persisted plan/registry/envelope authority,
  and uses transition plus canonical request digest to query-before-create the
  materialized context. Response-loss and replay tests require exactly one
  publication. Model-call and Core-job delta remains zero.

## 2026-08-01T12:34:55Z - lifecycle-87 no-model validation

- Root fix: a reconciliation-only successor now validates the exact closed
  transition inventory against each persisted succeeded plan/job/result and
  admission authority. It permits registry/framework drift only when the
  captured runtime identity remains exact; ordinary successor execution still
  requires the captured service authority.
- Materialization is query-before-create under the transition plus canonical
  request digest. A lost publication response is reconciled by the same query,
  and repeated recovery returns the prior context without a second write.
- Daemon compatibility is lifecycle 87. The new internal materialization
  readback is Core-only and absence, ambiguity, and malformed authority remain
  distinct fail-closed outcomes.
- No-model focused regression: 250/250 passed in 176.43 seconds. JUnit:
  `logs/community17_dev17_completed_prefix_v41_20260801T113817Z/test_lifecycle87_focused_v2.xml`.
  Compile, undefined-name/fatal lint, new broad-exception lint, and diff checks
  all passed.
- A deliberately broad adapter-tree probe was not used as an acceptance gate:
  341 passed and 45 failed because historical asset tests read mutable frozen
  protocol/image pins, a live legacy SQLite sidecar, and old lifecycle literals.
  Its complete JUnit is retained as
  `logs/community17_dev17_completed_prefix_v41_20260801T113817Z/test_lifecycle87_adapter.xml`;
  no failure touched the lifecycle-87 Core/Successor test set.
- Model-call and Core-job delta: Candidate 0, Judge 0, Reflector 0, other model
  0, Core jobs 0. Source v40 and its terminal receipt remain unchanged.

## 2026-08-01T12:50:00Z - lifecycle-87 deployment and pre-append defect

- Code commit `ec24f69c5ab7b24d270b3114285f9efde50eb06c` produced immutable
  release v6: release `54b94ea4...`, registry `8a6cc762...`, Daemon
  `afb814af...`, lifecycle 87. The exact lifecycle-86 v5 generation was stopped
  and v6 generation `730e8a2c...` started. Deployment and all three readiness
  receipts prove no Candidate, Judge, Reflector, or model start.
- The first deployment invocation used an incorrect trust-root name and failed
  before staging or remote mutation. The existing v5 receipt identified the
  exact trust root `ssh-known-hosts-v13-50527`; the corrected append-only
  invocation then passed. The first bootstrap file is retained but unused; the
  `v6b` bootstrap binds the correct root.
- Formal repair executor v42 and its identity were generated with zero model
  and Judge calls. Its generic validate from the non-credential shell reported
  only `JUDGE_CREDENTIALS_MISSING`; the repair driver itself is no-model and
  its read-only preflight passed against v6.
- The lifecycle-87 repair POST returned 409. A read-only post-failure preflight
  proved attempt count remained 2, transition remained failed, and no commit,
  model call, or new Core job existed. No retry was issued.
- Root cause: before the ledger appends a `reconciliation_only` attempt, Core
  closes the historical dataset and jobs using the persisted ordinary failed
  Attempt. That in-memory context still triggered the ordinary release fence.
  Lifecycle 88 projects the request-bound terminal attempt/hash only for this
  read-only pre-append validation; the ledger independently verifies the same
  authority transactionally before any append.

## 2026-08-01T13:05:00Z - lifecycle-88 deployment and exact 404 isolation

- Commit `4c46ec9b07b94e09c007a37a2f4f90f12bf55a18` produced release v7,
  release identity `edde744b...`, registry `6f597685...`, lifecycle 88. The
  exact v6 predecessor was replaced by generation `07e61215...`; deployment
  and all readiness receipts prove model, Candidate, Judge, and Reflector
  starts false.
- Repair executor v43 passed read-only preflight. Its first request returned
  409 before any reconciliation attempt append. A post-failure preflight
  proved source attempt count 2, transition `failed`, commit absent, and no
  model/job delta. One same-identity diagnostic request was then permitted and
  exposed the closed cause as Evolution HTTP 404; no further POST was issued.
- The current Evolution database was copied only as a read-only diagnostic
  snapshot to
  `logs/community17_dev17_completed_prefix_v41_20260801T125439Z/diagnostic_core_v7_404_readonly/evolution.db`,
  SHA-256 `dfa2fa5f1d22dacea0e0df12e6eda7c02632d8a8ade1c91e54dddf6119d1d1d6`.
  It retains the exact dataset, three current succeeded jobs, their admissions,
  and the three sealed predecessor artifacts; no transition is discarded.

## 2026-08-01T13:23:34Z - lifecycle-89 chained historical readback fix

- Root cause: the outer historical terminal job allowed its persisted registry,
  but sealed predecessor input verification dropped that permission and
  required the repair release's current registry. A chained predecessor thus
  failed read-only authority validation and the Evolution internal endpoint
  mapped it to 404.
- Fix: propagate historical-terminal permission only inside the sealed-input
  validation of an already succeeded transition-bound job. Ordinary job
  creation, claim, retry, and nonterminal reads remain current-registry exact.
  Daemon compatibility is lifecycle 89.
- Regression first reproduced the exact nested failure, then passed together
  with the single-generation historical readback and nonterminal rejection
  controls (3/3). The full focused no-model suite passed 264/264 in 88.60
  seconds; JUnit is
  `logs/community17_dev17_completed_prefix_v41_20260801T125439Z/test_lifecycle89_focused.xml`.
- Compile, fatal E9/F lint, new-line broad-exception review, and `git diff
  --check` passed. Three pre-existing BLE findings in the job-completion cleanup
  block are outside this patch and were not changed. Model-call and Core-job
  delta remains zero.

## 2026-08-01T13:46:47Z - lifecycle-90 post-materialization validation fix

- Release v8b deployed lifecycle 89 as generation `97b6dabc...`; deployment
  and all readiness receipts prove model, Candidate, Judge, and Reflector
  starts false. Its first exact completed-prefix request passed historical job
  closure and durably appended reconciliation attempt 3.
- Attempt 3 reached `materializing` (`4/6`) and then failed closed with no
  successor commit. A read-only Science/Evolution snapshot proves exactly one
  context was published for transition
  `successor-889c006c8e8eec11bf5653c832e07cac`, the exact predecessor, and the
  three existing artifact IDs. No model job or method job was created.
- Root cause: the production preparer intentionally fenced the replacement
  release by captured runtime identity and returned its current registry in
  the runtime snapshot. The owner-level materialization receipt validator then
  unconditionally compared that snapshot to the historical Task admission
  registry and raised `ValueError` after the durable publication.
- Lifecycle 90 permits the preparer-fenced replacement registry only for a
  `reconciliation_only` attempt. Ordinary successor validation remains exact
  to the admission registry. The same transition plus request digest will
  query-before-create the already published context on recovery.
- Regression reproduced the exact failure before the fix and passed after it;
  its strict ordinary-attempt control still rejects registry drift. The full
  focused no-model suite passed 264/264 in 90.31 seconds. JUnit:
  `logs/community17_dev17_completed_prefix_v41_20260801T132334Z/test_lifecycle90_focused.xml`.
  Compile, fatal E9/F lint, and `git diff --check` passed. Existing broad-
  exception findings are outside this patch. Candidate, Judge, Reflector,
  other model, and Core-job deltas remain zero.

## 2026-08-01T14:01:00Z - lifecycle-91 repaired-tail resume gate

- Commit `2fc2a2e63fd3cac8b4ec64786a9502287f410596` packaged the lifecycle-90
  registry validation fix. An initial v9 build passed no asset because its
  supplied full HEAD suffix was wrong; the empty namespace is preserved.
  Release v9b built and deployed successfully, replacing exact lifecycle-89
  generation `97b6dabc...` with lifecycle-90 generation `00b8df42...` and
  release `0ea46853...`. Deployment and readiness report model false.
- Before issuing a mutation, code inspection proved that same-identity replay
  of attempt 3 would only return the preserved
  `successor_reconciliation_failed` state. The ledger resumed only daemon-
  interrupted reconciliation attempts; it could not enter a repaired commit
  tail after a deterministic code failure. No ineffective POST was issued.
- Lifecycle 91 re-arms the same reconciliation attempt only when the request
  identity, original source attempt/digest, failed transition/attempt,
  predecessor head, sealed dataset, Task blocker, and absent commit remain
  exact. It appends no attempt and enters only the no-model reconciliation
  path; ordinary successors remain ineligible.
- New regression failed before the fix, then proved repaired completion,
  attempt count stability, no ordinary `running_methods`, and side-effect-free
  replay. The focused no-model suite passed 265/265 in 87.75 seconds. JUnit:
  `logs/community17_dev17_completed_prefix_v41_20260801T134647Z/test_lifecycle91_focused.xml`.
  Compile, fatal E9/F lint, and diff checks passed. Candidate, Judge,
  Reflector, other model, and Core-job deltas remain zero.

## 2026-08-01T14:13:00Z - lifecycle-92 historical materialization replay

- Lifecycle-91 release v10 deployed successfully as generation `f5eb569a...`,
  release `ad35843c...`; all deployment/readiness model-start flags are false.
  Read-only preflight passed with exact source, attempt count 3, failed state,
  no commit, and model calls 0.
- One authorized same-identity execute re-armed attempt 3 but preserved another
  `successor_reconciliation_failed` at `materializing` (`4/6`). It appended no
  attempt, commit, artifact, destination namespace, model call, or Core job.
- Root cause: query-before-create returned the unique context published by
  lifecycle 89 under registry `f20ac...`. Lifecycle 91 ran under a later
  registry and accepted only the original Task admission or its current
  release registry, so it rejected this third, durable publication registry.
- Lifecycle 92 recognizes the registry only when the Core-only Evolution
  endpoint returns one unique row for the exact transition plus canonical
  request digest. It creates no second context. Fresh publications and ordinary
  successor execution retain their strict registry fences.
- Regression first reproduced the cross-two-release replay failure, then
  passed while proving materialization count remained one. The focused
  no-model suite passed 265/265 in 88.36 seconds. JUnit:
  `logs/community17_dev17_completed_prefix_v41_20260801T140100Z/test_lifecycle92_focused.xml`.
  Compile, fatal E9/F lint, and diff checks passed. Candidate, Judge,
  Reflector, other model, and Core-job deltas remain zero.

## 2026-08-01T14:28:00Z - lifecycle-93 atomic registry activation

- Lifecycle-92 release v11 deployed successfully as generation `05d74e60...`,
  release `b3acc580...`; deployment/readiness prove model false. Preflight
  remained exact with attempt count 3 and no commit.
- One same-identity execute advanced the durable transition from
  `materializing` (`4/6`) to `committing` (`5/6`) before preserving
  `successor_reconciliation_failed`. This proves historical materialization
  replay and workspace capture succeeded; attempt count stayed 3 and v41 was
  not initialized.
- Root cause: the final atomic successor closure still required every evolved
  successor registry to equal its predecessor registry. It had no access to
  the active `reconciliation_only` attempt, despite owner/preparer validation
  already binding the exact durable materialization registry.
- Lifecycle 93 passes the active attempt into atomic closure and permits the
  verified materialization registry only for reconciliation-only semantics-2
  commits. Ordinary successors remain predecessor-registry exact; all other
  dataset, artifact, workspace, runtime, plan, head, and transaction checks
  are unchanged.
- New end-to-end ledger regression reproduced the exact commit rejection and
  then proved the replacement registry is atomically activated without an
  extra attempt or ordinary job execution. The first focused run exposed a
  test-only NameError in abandon validation argument wiring; it was corrected
  to its existing failed-attempt fence. The final focused no-model suite passed
  266/266 in 92.04 seconds. JUnit:
  `logs/community17_dev17_completed_prefix_v41_20260801T141300Z/test_lifecycle93_focused_v2.xml`.
  Compile, fatal E9/F lint, and diff checks passed. Candidate, Judge,
  Reflector, other model, and Core-job deltas remain zero.

## 2026-08-01T14:39:43Z - lifecycle-93 deployment and v40 closeout

- Commit `450fd51393b7af460bf66b94c3f19a172d221410` was packaged as release v12
  and deployed with lifecycle 93. The fenced deployment replaced exact v11
  generation `05d74e60...` with generation `7b9cc863...`, release
  `66a9208e...`; deployment and readiness receipts record Candidate, Judge,
  Reflector, and model starts as false.
- Preflight bound the exact failed v40 transition `successor-889c...`, attempt
  count 3, absent commit, and zero allowed model calls. One reconciliation
  execute reused the existing three succeeded method jobs and the unique
  historical materialization, committed successor receipt `a226b079...`, and
  closed the source side effect without appending an attempt.
- Source v40 advanced to `NEXT_ATTEMPT_READY`. The official completed-prefix
  path created v41 at `NEXT_ATTEMPT_READY` with 13 attempt receipts, 9
  reflector cycles, 27 evolution jobs, and budget usage 13 Candidate / 13
  Judge / 45 Reflector. Additional Candidate, Judge, Reflector, other model,
  and Core jobs were all zero.
- v41 formal verification is `PASS` with identity and integrity verified,
  zero pending or failed side effects, and no active owned resources. The
  fresh-namespace validation surface correctly reports that the imported v41
  namespace already exists; existing-namespace status/verify are the
  continuation authority.
- Evidence: `logs/dev17_completed_prefix_release_v12_20260801T142800Z/`,
  `logs/community17_dev17_completed_prefix_v41_20260801T142800Z/completed_prefix_bootstrap_receipt.json`,
  and `supervisor/rcb_oe_v0_community17_dev17_completed_prefix_v41/`.

## 2026-08-01T14:52:00Z - v41 continuation fail-closed and fresh-run decision

- The first two continuation launches failed before mutation because runner
  and immediate monitor simultaneously entered Daemon attach control. Both
  preserved v41 revision 2 and its 13 / 13 / 45 model-operation accounting.
  A standalone attach against generation `7b9cc863...` passed, so retry2
  serialized runner attachment before monitor startup.
- Retry2 advanced only the no-model cursor to Energy attempt 1, then Candidate
  preflight failed closed before intent persistence. v41 revision 4 is
  `BLOCKED` with `CORE_CONTROL_PREFLIGHT_FAILED`, zero pending/failed side
  effects, zero active resources, 13 attempt receipts, 9 reflector cycles, and
  27 Core evolution jobs. No Candidate, Judge, Reflector, other model, or Core
  job was added.
- An isolated no-model reproduction exposed
  `CANDIDATE_EXISTING_WORKSPACE_SNAPSHOT_DRIFT`. The imported successor raw
  workspace maps exactly to Core snapshot `workspace-ec3bd889...`; the blocker
  is project state `not_ready`, not workspace content.
- The committed recovered head is bound to historical executable registry
  `f20ac9a7...`, while lifecycle-93 release v12 is registry `e04c9f76...`.
  Core therefore correctly rejects new Task admission. Rebinding an immutable
  committed formal head by hand would violate the protocol and is not used.
- v40 and v41 are preserved as failed-closed evidence. The supported Case-E
  path is a new clean formal Dev17 namespace under the current release, with
  no import from v38/v40/v41 and no manual state edits.

## 2026-08-01T15:05:30Z - managed Core attach serialization

- Root cause of the two pre-mutation runner exits was a cross-process race:
  the runner and immediate monitor both performed Daemon stage / observe /
  ensure / tunnel authentication at launch. The renderer-safe transport
  classified the losing control transaction as `daemon_bundle_failed`.
- The adapter now holds one owner-private experiment-runtime file lock only
  across attachment acquisition. The lock is released before any Candidate,
  Judge, Reflector, or successor operation. It carries no bearer or credential,
  rejects symlinks and unsafe ownership/mode, and times out fail-closed within
  the caller's attachment deadline.
- New no-model tests prove runner/monitor serialization and closed timeout.
  `test_managed_core_control.py` passed 10/10. The broader successor, Evolution,
  Daemon-build, and Core-service focused suite passed 274/274 with one
  explicitly deselected pre-existing stale assertion that still expects
  lifecycle 86 although the inherited HEAD is lifecycle 93. JUnit:
  `logs/community17_dev17_fresh_v42_20260801T145200Z/test_lifecycle94_focused_v2.xml`.
- The first broad run is preserved as
  `test_lifecycle94_focused.xml`: it passed the same 274 tests and failed only
  that stale lifecycle assertion. Compile, E9/F lint, and diff checks passed.
  Model-call and Core-job deltas remain zero.

## 2026-08-01T15:12:04Z - v13 deployment rejected by lifecycle floor

- Release v13 was built from `00876ff0df6c36754acc77cafbe86f96d64a6e48`;
  local Daemon checksums passed and its bootstrap allowed zero model
  operations. The fenced deployment stopped exact v12, then rejected v13 with
  `daemon_update_required` before writing a deployment receipt.
- Exact remote observations made after the failure report both the v12 and v13
  bundle views as `absent`. There is no active Daemon generation, runner, or
  model job to reconcile. The failed release and absent receipt are preserved.
- Root cause is the monotonic Core deployment floor: v12 and v13 both declare
  lifecycle 93, and a different release may not replace an already-published
  release at the same lifecycle. This is deterministic protocol enforcement,
  not a transient infrastructure error, so v13 is not retried.
- Lifecycle advances to 94 for the serialized managed-Core attachment release;
  the formal deployment asset assertion is updated from its stale historical
  value. No Candidate, Judge, Reflector, other model, or Core evolution job was
  started.

## 2026-08-01T15:19:00Z - lifecycle-94 deployment regression closure

- The first 138-test deployment-focused run passed 136 tests and exposed two
  failures: the expected Daemon lifecycle assertion still named 86, and a Mac
  askpass replacement-during-signature test intermittently missed unlink and
  recreation when the closed descriptor's inode and timestamps were reused.
- The lifecycle assertion now names 94. Askpass verification holds the original
  no-follow file descriptor across payload inspection and path-based signature
  verification, then compares the still-open inode with the final path. This
  makes replacement detection independent of inode reuse and timestamp
  granularity.
- The replacement regression passed five consecutive isolated runs. The same
  complete focused suite then passed 138/138 in 102.22 seconds. JUnit:
  `logs/community17_dev17_fresh_v42_20260801T145200Z/test_lifecycle94_deployment_focused_v2.xml`.
  The earlier failing JUnit is preserved without overwrite. Compile, E9/F lint,
  and diff checks pass; model and Core-evolution job deltas remain zero.

## 2026-08-01T15:35:24Z - lifecycle-94 formal Dev17 launch

- Local commits `946e2f356` and `036b651a8` separate the askpass descriptor
  fix from the lifecycle-94 release identity. No push was performed. An empty
  v14 directory from a wrong expanded commit identity and a partial v15 build
  missing activated-venv `uv` are preserved and not reused. Release v16 was
  built in a new directory from exact HEAD
  `036b651a867d9abaa1778dd8315f375996b24274`; Daemon checksums and identity
  passed.
- Lifecycle-94 deployment created generation `3e431a39...`, release
  `eb2a7c84...`, registry `215784ea...`. Its receipt proves authenticated
  tunnel and Docker self-inspection, predecessor already absent, and no
  Candidate, Judge, Reflector, or model start. Three no-model readiness
  receipts returned ready with Codex CLI started and model false.
- Fresh formal protocol v50 is bound to v16 and the unchanged Dev17 contract.
  Prelaunch validation passed with namespace available and model/judge false.
  Formal run `rcb_oe_v0_community17_dev17_fresh_v42` started in tmux; no legacy
  run artifact was imported.
- The first three monitor launches exposed only monitor-local startup/summary
  bugs: the first raced namespace creation; later runs treated an empty
  `tmux display-message` pane PID as an integer. The runner remained alive and
  durable snapshots stayed PASS with zero failed effects. The monitor now uses
  `tmux list-panes` and treats any nonnumeric PID as null.
- The first valid 1200-second monitor record at 15:35:24Z reports
  `Astronomy_004 a0 / CANDIDATE_RUNNING`, runner PID 474525, calls 1/0/0,
  Core jobs 0, pending 1, failed 0, active resources 0, Core health PASS,
  generation `3e431a39...`, and formal verify PASS with no anomaly.

## 2026-08-01T15:56:05Z - formal poll 2

- Durable progress advanced from Astronomy attempt 0 to attempt 1. Attempt 0
  closed its Candidate and Judge, consumed one five-call Reflector cycle, and
  committed the successor through three Core jobs. The active project head and
  workspace snapshot are both present.
- Current state is `Astronomy_004 a1 / CANDIDATE_RUNNING`; accounting is 2
  Candidate, 1 Judge, 5 Reflector, 3 Core jobs, pending 1, failed 0, active
  resources 0. Core health and formal verify are PASS, generation is unchanged,
  progress time advanced to 15:51:58Z, and the monitor reports no anomaly.

## 2026-08-01T16:16:42Z - formal poll 3 and Astronomy gate

- Astronomy closed all three attempts, three Judge operations, two Reflector
  cycles (10 calls), and six Core jobs. The successor is committed and the
  current project/workspace heads are present.
- The explicit Astronomy closeout gate passed with no blockers, no official40
  start, and no secret recorded. Runner advanced to
  `Chemistry_004 a0 / CANDIDATE_RUNNING`.
- Accounting is 4 Candidate, 3 Judge, 10 Reflector, 6 Core jobs, pending 1,
  failed 0, active resources 0. Core health and formal verify remain PASS,
  generation is unchanged, progress advanced to 16:12:33Z, and no monitor
  anomaly is present.

## 2026-08-01T16:37:09Z - formal poll 4

- Chemistry attempts 0 and 1 closed. Current state is
  `Chemistry_004 a2 / TASK_ATTEMPT_READY`; the latest successor is committed
  with project/workspace heads present.
- Cumulative accounting is 5 Candidate, 5 Judge, 20 Reflector, 12 Core jobs.
  Pending, failed, and active resource counts are all zero. Core health and
  formal verify remain PASS, progress advanced to 16:36:32Z, and there is no
  monitor anomaly or unchanged-progress sample.
