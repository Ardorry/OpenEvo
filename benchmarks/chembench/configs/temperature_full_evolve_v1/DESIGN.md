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
`state/chembench_temperature_full_evolve_v1/formal_runtime_v6`，并验证两个 distribution 均为
non-editable、framework registry 与 wheel/lock 匹配。旧 STV2 runtime 只作为依赖版本来源；其中
旧 Core、可编辑 ChemBench 包、数据库、completion 和 artifact 均不进入新的正式解释器。所有
正式入口以 `-I` 启动且重新核验 formal-runtime receipt、源码提交和安装 inventory。

`formal_runtime_v6` 与 `formal_runtime_v6_failures` 是不可覆盖的新 namespace；已有 v2/v3/v4/v5
runtime 及其 failure evidence 保持只读。v3 已把 Candidate formal execution 从并行准入改为全局串行，
并修正宿主 Rollout 到 Gateway 容器的 callback route。正式运行随后暴露出另一条彼此独立的
Gateway 终态竞态：subscription 主 POSTRUN 在注册 durable cleanup ownership 后等待 runtime
停止，后台 cleanup reconciler 可在这段窗口先构造终态；主路径随后又以略晚的 timer snapshot
构造第二份终态，从而触发严格 result identity mismatch。v4 不放宽 digest、CAS 或终态校验，
而是让主路径在 runtime absence 后也只能通过同一个 cleanup reconciler lock 执行终态构造；
确定性并发回归要求 primary 与 background 同时到达时 finalizer 恰好执行一次。该修复只改变
cleanup 编排与幂等性，不改变 split、prompt、artifact、模型、reasoning、parser、evaluator、
retry 选择或 feedback 可见性；v3 completion 不导入 v4 的 fresh C0 run。

v4 的首次 fresh run 证明上述终态修复有效：terminal identity、cleanup CAS 与 callback connection
错误均为 0。但在立即串接正式 session 时，前 8 个 runtime 结果稳定交替为 4 个 credential-isolation
no-completion 与 4 个 completion；所有失败都发生在下一次 back-to-back admission 的 readiness
阶段，Train batch 尚未闭合且 accepted completion 为 0。现有持久化证据不能区分 provider cooldown、
Codex readiness refusal 或其他外部瞬态，因此报告不得把具体 provider 根因写成已证明事实。v5 在
每个 durable terminal outcome 与下一次正式 admission 之间固定等待 15 秒。对于 terminal
no-completion，executor 先完成双重 durable proof 和等待，再写入允许 successor retry 的 failure
ledger event；若等待期间 crash，恢复只能重审同一 task 并保守地重新等待，不能提前发出替代调用。
crash recovery 读取尚未 accepted 的既有 completion 时也会再次执行一次 bounded wait，但不会重复
submit；已经存在 `CALL_ACCEPTED` 的 closed call 证明先前 cooldown 已完成，resume 重审时不再次等待，
避免长阶段恢复被监控误判为 stall。
Candidate phase 每次只准入一个逻辑 UID，成功后立即 accepted/evaluated；基础设施失败只在当前
UID 上执行闭集内的 bounded retry，不会继续消耗该批其他 UID。该 pacing
值进入 closed config 和 config digest，对 Candidate、Reflector、evolved Test 与 baseline Test
使用同一 executor，不改变任何模型可见内容、parser/evaluator 或 arm 间公平性。定向回归证明
cooldown 只出现在前一 terminal outcome 已 durable audit、下一 claim 尚未写入的边界；v4
completion、artifact 与数据库不导入 v5 的 fresh C0 run。

v5 的 fresh C0 健康门在 4 个逻辑 Candidate 调用内记录了 3 个 accepted completion 和 3 个
credential-isolation no-completion physical outcome；Train 的首个 25 题批次未闭合，Reflector、
Core evolution 和 Test 均未开始。为避免扩大失败，runner 在当前逻辑 UID 的 bounded retry 闭合后
停止，服务也完成无 active session 的受控清理。v5 canary 会在运行结构化 evidence validator 前
先检查 Codex CLI return code；因此“完整 no-tool turn 且 CLI 非零”无法进入唯一允许的 bounded
retry。v5 的临时 event/stderr 已按安全 cleanup contract 删除，不能从聚合日志反推每个历史失败
是否恰好命中该分支；报告必须把它表述为代码审查确认的确定性可靠性 bug，而不是伪造历史 provider
根因。

v6 保持 canary prompt、模型、reasoning、split、Candidate/Reflector prompt、parser、evaluator、
artifact 规则和反馈边界不变，只收紧 readiness 编排：先验证结构化 evidence；仅当 validator 证明
完整 turn、零 tool side effect 且 refusal inventory 精确时，才在 15 秒后允许一次重试，即使该
no-tool attempt 的 CLI return code 非零。其他 evidence、exact command evidence 与非零 CLI 的
矛盾、inventory 异常全部 fail closed。独立 EXIT cleanup 与 signal handler 不再吞掉终止信号；
cleanup 失败也不能发布 readiness；对外错误只保留 allowlisted code，不泄露 provider stderr。
v5 的 run、completion、数据库和任何中间状态均不导入 v6 的 fresh C0 run。

订阅 transport 需要容器网络连接 provider，因此不能把 `allow_internet=true` 误写成模型拥有
网页或 shell 能力。本实验在通用 Codex subscription harness 上选择 closed
`tool_policy=disabled`：正式 Candidate/Reflector 推理命令固定为 `web_search=disabled`、
`features.shell_tool=false`、`features.unified_exec=false`，同时保留 provider transport。MCP、
plugins、apps、browser、computer-use 与 subagent 继续由既有 closed profile 禁用；transcript
零工具审计提供独立的事后完整性检查。credential-isolation readiness canary 是 harness setup
的基础设施证明，不属于 Candidate/Reflector 任务指令，使用受控、最多两次且只对零工具拒绝重试的
shell canary。
Candidate formal executor 的 closed 配置固定 `candidate_max_workers=1`；executor 拒绝任何
大于 1 的值，runner 使用同一权威常量。每个 Candidate 必须完成 submit、poll 和 durable audit
后才准入下一 Candidate；任一 durable terminal outcome 后还必须满足 fixed 15-second pacing
guard。

Rollout 仍只在宿主 `127.0.0.1:8080` 监听，宿主 runner 也只使用该地址；但它写入 Gateway
session 的 terminal callback origin 固定为容器可达的
`http://host.docker.internal:8080`。Gateway bootstrap、host/effective topology validator 与服务
receipt 共同绑定这一映射，且 Docker argv 必须保留 `host.docker.internal:host-gateway`。因此
容器不会把 callback 误发到自身 loopback，Rollout 也不会扩大为对外监听。
新增 callback-route 字段使用 RuntimeServices receipt V2；旧 V1 receipt 文件保持只读，不以新
schema 重新解释或覆盖。

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

输入 dataset 的 `manifest.json` 与 `records.jsonl` 固定写入本 run 的
`core/artifacts/input_datasets/chembench_temperature_full_evolve_v1/batch_N`，以满足 Core payload
scanner 的受管根约束；private evidence 目录只保存注册 intent 与 binding。intent 在写 payload
和登记 artifact 前持久化，并绑定 artifact-root inode、请求 digest、URI 与 payload digest。
若进程在 `register_artifact` 已提交而 binding 尚未写入的窗口崩溃，恢复只接受数据库、Core
managed manifest 与 DTO 全字段精确一致的唯一既有 artifact；任何重复、URI/root/manifest 或
lineage 漂移都 fail closed，不会再次登记同一批 dataset。

私有 append-only ledger 在每次写入后 flush + fsync，并覆盖：

```text
RUN_CREATED
SPLIT_FROZEN
CALL_CLAIMED
CALL_NO_COMPLETION_FAILURE
CALL_REJECTED_COMPLETION
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

确定性 session/task ID 在模型提交前持久化。本地 Rollout client 必须先构造成功，再执行紧邻的
health check、fsync claim 与 submit；client 构造失败不会留下 owned claim。claim 之后若 transport
结果不明确，只能按 exact task identity 恢复；若既不能证明既有 task/result，也不能证明
no-completion，则 fail closed，不把缺失观察解释为重调权限。恢复时先查询 Rollout 的既有 task：
已有 terminal completion 必须接续入账，不得重调。只有两类闭合 predecessor 允许固定的
attempt+1（每个
logical call 最多三次）：一是有 Rollout/Gateway 双重证据的 no-completion；二是仅限
`train_reflector` 的 durable completion 在 response schema、evidence scope 或 packet sequence
校验失败。第二类事件只保存 response/task-result/transcript/completion identity SHA-256 和闭集
rejection code，不保存 response 或 transcript；Candidate parser invalid 仍是已接受 completion
的评估结果，不能借此重调。已有 accepted synthesis 后禁止 retry，每批仍只有一个最终
`REFLECTOR_ACCEPTED`。每个 claim 还绑定 `retry_semantics_sha256`：只规范化 attempt-specific task/
call/logical ID 与 session-bound context receipt，prompt、context artifact/target IDs、runtime、agent、
tool policy 和其他 metadata 全部参与 canonical digest；checkpoint readback 重新从 TaskRequest 计算，
successor digest 不同则在提交前 fail closed。
split、模型、reasoning、prompt、parser、evaluator、runtime、source commit、evolution schema 或
artifact policy digest 漂移时禁止 resume。

## Test 与报告

`C4` 冻结后关闭 feedback 和 evolution，先运行 evolved Test，再运行独立 baseline Test。
baseline workspace 不挂载任何 evolved target。两臂除 context 和 arm identity 外的
TaskRequest 语义必须逐字段一致。

逐题任务、GT、prediction、completion、transcript、UID 和 secret 仅存在于 owner-private run
evidence。公开结果包只包含 aggregate metrics、redacted manifests、图表和 SHA-256。由于本次
协议允许历史题复用，主比较是新 generation-zero 状态下的配对复现实验，不应表述为严格
never-exposed 泛化证明。
