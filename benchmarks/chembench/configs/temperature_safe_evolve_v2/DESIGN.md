# ChemBench Temperature Safe-Evolve V2 设计

## 研究身份

本实验是
`reused_official_temperature_pool_independent_state_mechanism_experiment`。它重新使用官方
202 道 `Temperature_Prediction` 题，只证明每个新 run 内的数据阶段隔离以及数据库、状态、
artifact lineage、workspace 和模型调用的独立性。它不是历史 never-exposed 或外部独立泛化实验。

Phase A 从 owner-private 的上一轮正式证据恢复 C4 的逐字节 payload，并在同一批已暴露的 100
道 Test 上运行固定 `2^3` 八臂诊断。八个 arm 在任何结果产生前冻结题序与 H0/H1；所有 800 个
Candidate completion 闭合后才统一读取 GT。Phase A 不调用 Reflector、Core evolution 或 artifact
update，也不作为新的泛化证据。

只有 memory-only 或 skill-only arm 通过预注册安全标准，Phase B 才会启动。`agent_system` 始终为空。
四个 fold run 都从独立 G0、数据库、Core generation、artifact store、workspace、ledger、completion
root 和 runtime-services identity 开始。任何 run 的 state、completion 或 evidence 均不得被其他 run
导入。

Phase A 使用 preflight 绑定的空 runtime-services identity；目标选择闭合后写入停止回执。每个 fold
由 campaign ID 与 fold ID 确定性派生一个从未使用的 service run ID，启动时审计空 Rollout inventory
和空 completion root，闭合后停止并保留不可变 completion evidence。fold 中途失败时不自动销毁该
服务，以便 exactly-once 诊断和恢复。每个 fold 的 runtime-services digest 与 Core store ID 都进入
链式 ledger 和 aggregate compatibility receipt。

## 调用边界

```text
private frozen evidence + official snapshot
  -> zero-call identity, hash, fold, leakage and state-machine preflight
  -> immutable source commit + one non-editable wheel runtime
  -> Phase A: 8 x 100 Candidate sessions
  -> preregistered single-target selection or no-go
  -> R0: four Train blocks with G0/active/provisional shadows
       -> one schema-bound Reflector session per block
       -> one selected-target verified plan-bound Core job per block
       -> delayed forward promotion
       -> V1 promotion check -> V2 deployment gate
       -> sealed G0/raw Test
  -> preregistered R0 continuation gate
  -> optional frozen R1-R3
  -> aggregate-only reports and local Git commit
```

每个模型调用都沿
`TaskRequest -> Rollout -> Gateway -> CodexHarness -> managed_science` 执行。Candidate 与
Reflector 的 model-visible tool policy 为 disabled；shell、web、MCP、apps、plugins 和 subagent
均不可用。Core update 只能由 frozen verified executable registry、plan-bound job、worker claim、
typed artifact output 和 controller validation 产生。

## Candidate context 与去重

G0 使用上一轮相同的官方 prompt renderer 和 runtime policy。非空 context 仅改变一个冻结目标：

- memory：在同一固定 framing 下加入当前题检索出的最多 2 个 active entry；
- skill：在 managed workspace 中只上传一个 `SKILL.md`，每题最多检索 1 个 active entry；
- agent-system：只在 Phase A 上传冻结 C4 的精确 `AGENTS.md`。

每个逻辑 inference context 的 hash 覆盖公开 prompt、最终 instruction、workspace 文件 inventory、
model、reasoning、runtime、parser/evaluator、timeout/retry policy、artifact identity、检索输入与输出。
Phase B 同一 run、同一 block 内完全相同的 context 可以绑定到同一个 persisted accepted completion；
不同 prompt、payload 或 metadata 不得去重。Phase A 的八个独立 arm 不跨 arm 去重，A0/A7 也必须
完整重跑。

## GT 屏障

每个 block 先只从 public task 构造并闭合所有唯一 Candidate context；之后 controller 才从私有
task 读取 target 并产生 evaluation。Train GT 仅进入该 block 完成后的 Reflector packet。V1、V2
和 Test 在 inference 闭合前同样禁止读取 GT；V1/V2 只用于固定 gate，Test 后禁止任何更新。

Test 开始前冻结 source commit、runtime、model、parser/evaluator、检索器、artifact、deployment
decision 和题序。Test 阶段机械禁用 Reflector、Core 和 artifact update。

## Canonical entries 与稀疏检索

selected target 的 canonical artifact 是 closed JSON，entry 状态仅允许 `provisional`、`active`、
`retired`。每条 entry 绑定 support/contradiction/flip 引用、batch、confidence、status、retirement
reason 和 source packet digest。Controller 在 prompt acceptance 与 merge 两层核对 Reflector 的
run/fold/batch/target/predecessor/prior-evidence/source-packet bindings，绝不在 completion 后补值。

检索器仅使用题面与选项的规范化 lexical token/shingle，entry 只使用 content、applicability 与
exclusion conditions。排序由 overlap score、support、confidence、entry ID 固定；不使用 GT、外部
embedding 或网络。active entry 总数至多 6。memory top-k=2、单题硬上限 1200 bytes；skill
top-k=1、单题硬上限 700 bytes。超限 entry 不截断，而是在确定性排序中跳过。

## Promotion、retirement 与 deployment

provisional candidate 只在下一 forward block 上与当前 active 和 G0 配对比较。promotion gate、
entry retirement gate、V2 deployment gate 以及 R0 continuation gate 完全按协议 YAML 固定。失败
candidate 保持 immutable，active state 不变，不重跑 forward block，也不降低 gate。

V2 gate 失败时 deployment-aware state 固定回退 G0；Test 的 deployment-aware prediction直接复用
同 run G0 prediction，不产生额外模型调用。raw state 仍需在 Test 上评估，以诊断机制本身。

Deployment gate 前重新机械审计四个 Reflector/Core 闭包、promotion lineage、active-entry 与稀疏
注入预算、当前 runtime identity 以及 Test 尚未开始。R0 continuation 前再次审计 freeze、双臂
inference、GT release、evaluation 的事件顺序，并拒绝 freeze 后的 Reflector/Core mutation；这些
完整性输入不得以常量真值替代。

## Exactly-once 与恢复

每次物理模型调用在 submit 前把完整 TaskRequest checkpoint 和 `CALL_CLAIMED` 追加并 fsync。唯一
权威 completion 是 Rollout 持久化 `SessionResult`。已有 accepted completion 永不替换；只有双重
证明 terminal no-completion 的基础设施失败允许固定 attempt+1。Candidate parser invalid 是结果，
不授权重跑。Reflector closed-schema rejection 最多 3 个 attempt，并保存脱敏 rejection receipt。
ambiguous 状态停止新副作用。

只有日志、进程、序列化兼容、幂等、权限和观察层修复可从闭合 checkpoint 恢复。split、prompt、
gate、retrieval、模型、parser/evaluator、retry、artifact 规则或 feedback 边界变化会把受影响 run
封存为 immutable invalid evidence，并使用新 run ID 从 G0 重启。

## 隐私与交付

run evidence root 及其敏感子目录为 owner-private。公开报告只含 aggregate count、digest、统计和
图表，不含题目、选项、GT、UID、逐题 prediction、completion、transcript、canonical entry 内容、
数据库或凭据。每个公开文件写后 readback 并进入 `SHA256SUMS.txt`。
