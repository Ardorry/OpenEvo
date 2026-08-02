# Temperature Full-Evolve v1 设计

## 研究身份与数据范围

本实验是独立的 `Temperature_Prediction`、generation-zero、research-only 实验。它不读取或
导入任何旧 run 的 completion、workspace、database、artifact、text memory、skill、agent
system 或缓存。

本协议固定从 202 道 Temperature 官方 test-pool 题目中构造 100 Train + 100 Test，且不按
历史暴露筛选。这一数据范围只改变题目资格，
不改变本次 Train/Test 互斥、Test 盲化或 C0 空状态。内部审计与最终方法说明必须记录
`historical_exposure_policy=fixed_official_temperature_pool_fresh_c0`；不得把 Test 描述为严格
never-exposed holdout。

## 边界与数据流

```text
frozen dataset + historical ledgers
  -> deterministic group-aware 100/100 split (private UID manifests)
  -> C0 empty context
  -> [25 Candidate pre sessions]
  -> one batch Reflector session through TaskRequest/Rollout/Gateway/CodexHarness
  -> canonical structured rule/evidence index
  -> deterministic target projections
  -> three verified plan-bound Core jobs
  -> validate all targets and publish one controller checkpoint Ci
  -> [25 Candidate post sessions]
  -> next batch
  -> freeze C4
  -> evolved Test (100, feedback disabled)
  -> baseline Test (same 100/order, C0, feedback disabled)
  -> paired aggregate audit and report
```

Candidate、Reflector、evolved Test 与 baseline 的模型推理都必须通过正式
`TaskRequest -> Rollout -> Gateway -> CodexHarness` 路径，并使用相同的 pinned managed Codex、
`gpt-5.5` 和 `medium` reasoning。Core jobs 只通过 verified executable registry、immutable
plan、worker claim、typed artifact registration、validation、promotion 和 context resolution
执行；benchmark controller 不直接登记伪造 artifact。

正式服务和付费 runner 使用本实验独立的固定 Python bundle。该 bundle 在源码提交冻结后离线
构建当前 OpenEvo Core 与 ChemBench wheel，逐个核对 wheel 中 Python 文件与源码，安装到
`state/chembench_temperature_full_evolve_v1/formal_runtime`，并验证两个 distribution 均为
non-editable、framework registry 与 wheel/lock 匹配。旧 STV2 runtime 只作为依赖版本来源；其中
旧 Core、可编辑 ChemBench 包、数据库、completion 和 artifact 均不进入新的正式解释器。所有
正式入口以 `-I` 启动且重新核验 formal-runtime receipt、源码提交和安装 inventory。

订阅 transport 需要容器网络连接 provider，因此不能把 `allow_internet=true` 误写成模型拥有
网页或 shell 能力。本实验在通用 Codex subscription harness 上选择 closed
`tool_policy=disabled`：正式 Candidate/Reflector 推理命令固定为 `web_search=disabled`、
`features.shell_tool=false`、`features.unified_exec=false`，同时保留 provider transport。MCP、
plugins、apps、browser、computer-use 与 subagent 继续由既有 closed profile 禁用；transcript
零工具审计提供独立的事后完整性检查。credential-isolation readiness canary 是 harness setup
的基础设施证明，不属于 Candidate/Reflector 任务指令，仍使用既有受控单次 shell canary。

## Split

- 数据源固定为仓库已验证的 ChemBench4K revision。
- 5 道 Dev demonstrations 不进入 Train/Test；它们只按官方 five-shot renderer 的既有语义
  出现在 Candidate prompt 中。
- 对 question、ordered/unordered options 和完整规范化内容做 exact closure 检查。
- 缺少 upstream source/reaction/template 字段时，使用固定的规范化字符-shingle 相似度规则
  构造保守的 connected components；group 不跨 Train/Test。
- 在固定 namespace 下按 SHA-256 排序 group。确定性装箱得到恰好 100 Train、100 Test；未选
  题进入本次 reserve。冻结后不得重采样。
- 私有 manifest 保存 UID 与 source index，权限 `0600`；公开 receipt 只保存数量、策略和集合
  digest。

## Batch Reflector 与 canonical evidence

每批 25 个 Train pre completion 闭合后，只允许一个最终 Reflector synthesis。输入只包含：

- 当前批的 Train 问题、选项、GT、completion、parsed prediction 与 correctness；
- 当前 C(i-1) 三目标内容；
- 前序 canonical evidence index；
- 已闭合前序批次 aggregate pre/post diagnostics。

Test manifest、Test UID、内容、GT 与统计不进入该请求。Reflector 返回 closed JSON。每条 rule
包含 stable rule ID、kind、content、applicability、support/counterexample UID references、
first-seen/last-validated batch、confidence、status 与 target projection。Controller 重新计算
support/conflict/count/digest，拒绝引用非 Train UID、重复引用、字段漂移或跨 target 职责错误。

普通类别知识只有至少两个独立支持时才能投影到 `text_memory`。单题规则可留在私有 evidence
index 中作为 provisional，但不得进入 Candidate 注入上下文。Post-evolve 同题结果只作为下一批
diagnostic metadata，不增加规则的独立支持数。

## 三目标投影和预算

投影由确定性 renderer 完成，不允许模型同时维护三份互相复制的自然语言摘要：

- `text_memory`：仅 category knowledge、适用条件、反例、置信度和验证方法；硬上限 4096 bytes。
- `skill_bundle`：仅可执行 workflow；硬上限 1536 bytes。
- `agent_system`：仅稳定行为和 parser-compatible 输出纪律；硬上限 1024 bytes。
- 三目标实际 UTF-8 注入 bytes 总和硬上限 8192，目标上限 6144。

超预算时只做确定性压缩：删除未投影 provisional、合并同 ID 更新、按
`status -> support -> confidence -> conflict -> rule_id` 排序保留。禁止字节截断。若仍不合法，
批次 fail closed；正式语义代码修改后必须使用新 run ID 从 C0 重启。

## Core successor 与恢复

每批创建一个不可变 dataset artifact 和三个 plan-bound target jobs。三个输出在 controller 侧
全部验证通过之前均不得成为 `Ci`。Core 当前没有跨三个 artifact 的公开原子 promotion API，
因此 controller checkpoint 是本实验的原子可消费边界：部分 staged/promoted artifact 保留为
不可消费审计 side effect，只有包含三目标完整 identity 的 `BATCH_ARTIFACT_SET_COMMITTED`
记录才能被下一 session 使用。

私有 append-only ledger 在每次写入后 flush + fsync，并覆盖：

```text
RUN_CREATED
SPLIT_FROZEN
CALL_CLAIMED
CALL_ACCEPTED
CALL_EVALUATED
BATCH_PRE_CLOSED
REFLECTOR_ACCEPTED
EVIDENCE_VALIDATED
TARGET_JOB_COMPLETED
BATCH_ARTIFACT_SET_COMMITTED
BATCH_POST_CLOSED
FINAL_STATE_FROZEN
EVOLVED_TEST_CLOSED
BASELINE_TEST_CLOSED
AUDIT_CLOSED
```

确定性 session/task ID 在模型提交前持久化。恢复时先查询 Rollout 的既有 task：已有 terminal
completion 必须接续入账，不得重调；只有明确不存在 completion 的 infrastructure attempt 才能
按相同输入重试。split、模型、reasoning、prompt、parser、evaluator、runtime、source commit、
evolution schema 或 artifact policy digest 漂移时禁止 resume。

## Test 与报告

`C4` 冻结后关闭 feedback 和 evolution，先运行 evolved Test，再运行独立 baseline Test。
baseline workspace 不挂载任何 evolved target。两臂除 context 和 arm identity 外的
TaskRequest 语义必须逐字段一致。

逐题任务、GT、prediction、completion、transcript、UID 和 secret 仅存在于 owner-private run
evidence。公开结果包只包含 aggregate metrics、redacted manifests、图表和 SHA-256。由于本次
协议允许历史题复用，主比较是新 generation-zero 状态下的配对复现实验，不应表述为严格
never-exposed 泛化证明。
