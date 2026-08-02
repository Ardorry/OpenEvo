# ChemBench Temperature Full-Evolve v1 实验协议

## 协议身份与数据范围

这是独立的、仅针对 `Temperature_Prediction` 的探索性监督演化实验。本协议固定使用
Temperature 官方 test pool 构造 100 Train + 100 Test，不按历史暴露筛选。因此本实验不声称
Test 是相对于全部历史运行的 never-exposed holdout；它检验的是
本次新 split 内、从全新 generation-zero 状态学习后相对于同栈 C0 baseline 的配对收益。

这一数据范围不允许继承旧状态。本次 run 必须使用新的 run root、workspace、
数据库、artifact lineage、Core generation 和 run ID，并从以下完全空的 C0 开始：

- 无 text memory；
- 无 skill artifact；
- 无 agent-system artifact；
- 无旧 completion、workspace、database、cache 或人工总结导入。

历史目录只读。公开报告必须如实标注
`historical_exposure_policy=fixed_official_temperature_pool_fresh_c0`。

## 固定数据与调用栈

- 数据源：仓库 pin 的 ChemBench4K revision。
- 类别：`Temperature_Prediction`。
- split：100 Train / 100 Test / 2 reserve；固定 namespace 下 group-aware SHA-256 排序。
- batch：Train 固定顺序连续四批，每批 25 题。
- 模型：`gpt-5.5`，reasoning `medium`。
- Candidate、Reflector、evolved Test 与 baseline 全部走正式
  `TaskRequest -> Rollout -> Gateway -> CodexHarness -> managed_science` 路径。
- Candidate 与 Reflector 的正式推理命令使用 completion-only subscription policy：容器保留
  provider transport 所需网络，但 Codex 的 `web_search`、`shell_tool`、`unified_exec`、MCP、
  插件、apps 与 subagent 能力在模型可见层关闭；transcript 的零工具审计作为第二道 fail-closed
  约束，任何工具事件都会使该 immutable completion 无效。
- Candidate 与 baseline 使用同一 prompt renderer、parser、evaluator、timeout 与 retry policy。

任何模型调用前必须冻结 split、配置、源码 commit、managed executable/image、framework
registry、runtime-services identity 和 owner-private持久化根；零调用 preflight 必须通过。

正式解释器来自本实验独立、不可编辑且内容寻址的 runtime bundle。当前提交的 OpenEvo Core 与
ChemBench wheel 必须在无索引模式下构建，wheel Python 源与冻结源码逐文件一致，安装 inventory、
两个 wheel、framework lock、解释器和 runtime receipt 全部计算 SHA-256。正式 precheck、service
lifecycle 与 runner 只能由该解释器以 `-I` 运行；旧实验解释器中的项目包不得直接承担本次调用。

## 每批 Full-Evolve 状态机

设 `C0` 为空状态，`Ci` 为第 i 批完整提交后的三目标状态。每批严格执行：

1. 使用 `C(i-1)` 对当前 25 题各做一次 pre-evolve Candidate 推理。
2. 25 个 accepted completion 与私有评估全部闭合后，构造一个 Train-only batch packet。
3. 只做一次逻辑 Reflector synthesis；它可看本批 GT、pre completion、累计 evidence、当前三
   artifact 和此前 aggregate diagnostics，但看不到 Test。
   Controller 必须在 canonical prompt 中显式给出动态 `required_response_bindings`，其中
   `batch_index` 等于当前批次，`prior_evidence_sha256` 在 C0 时为 `null`、其后为前一批
   evidence index 的精确 SHA-256。Reflector 只能把这两个值逐字复制到 response 同名字段；
   不得要求模型自行计算哈希，也不得由 Controller 在 completion 后补写或纠正。Prompt 接受层
   与 evidence merge 层分别做 exact equality 校验，binding 随 prompt hash 和 retry semantics
   一同封存。
4. Controller 将 closed JSON 合并为 canonical rule/evidence index。普通长期规则至少需两个
   独立 Train UID 支持；反例、适用范围、置信度与 retired reason 必须保留。单题答案、选项
   字母、题面映射不得投影。
5. 确定性地按职责投影三个 candidate artifact：类别知识进 `text_memory`，可执行流程进
   `skill_bundle`，长期行为和输出纪律进 `agent_system`。
6. 通过 verified registry 创建并执行三个正式 plan-bound Core jobs。只有三个 Core-owned
   payload 全部通过 lineage、schema、职责、泄漏与实际字节预算验证，才能原子提交 `Ci`。
7. 使用 `Ci` 对同一 25 题各重跑一次 post-evolve Candidate；该结果仅为同题训练诊断，不再
   触发本批 evolution。

上下文预算按最终实际注入 UTF-8 bytes 验证：text memory 4096、skill 1536、agent system
1024，三目标目标上限 6144、硬上限 8192。只能按规则支持度确定性压缩，禁止字节截断或生成
不完整 schema。

## 冻结 Test 与 baseline

四批完成后固定冻结 `C4`、Test 顺序、源码、配置和 runtime identity，并关闭 feedback、
Reflector 和 Core job。先以 `C4` 单次运行全部 100 道 evolved Test，再以独立空上下文 C0 按
同一顺序单次运行 100 道 baseline Test。Test 不得在线读取 GT、重跑、checkpoint sweep 或据
结果修改任何 inference/evaluation 语义。

若两臂之间出现可能影响 prompt、harness、timeout、retry、parser、evaluator、artifact
injection 或 accepted-completion 选择的代码修复，则旧 evolved Test 作废，保留失败证据，并在
同一新源码版本上从新 run ID 重跑两个完整 Test arm。

## Exactly-once、恢复与监控

每个逻辑调用在提交前写入 fsync-backed `CALL_CLAIMED`。同步持久化的 Rollout
`SessionResult` 是 crash recovery 的权威 completion 证据；Gateway completion record 只作辅助。
已存在 accepted completion 的逻辑调用永不替换。仅在 terminal no-completion 和相关持久化
闭包均被证明时，或 `train_reflector` 的 durable completion 因闭集 response schema、evidence
scope、packet sequence 校验失败并写入内容脱敏 rejection receipt 时，允许固定 attempt+1；每个
logical call 最多三次。Candidate parser invalid 不授权 retry。缺失、部分、多个或其他不合法
结果一律记为 ambiguous 并停止新副作用。所有 successor 还必须匹配 attempt-independent
`retry_semantics_sha256`；除 task/call/logical ID 与 session-bound context receipt 外，完整 prompt、
context artifact/target identity、runtime、agent、tool policy 和 metadata 的任何漂移都在提交前拒绝；
checkpoint 恢复同样从持久化 TaskRequest 重算该 digest。

正式 runner 使用持久 tmux。独立 monitor 每 600 秒写一个结构化 snapshot，检查 phase、batch、
题号、accepted/call/Reflector/Core 数量、lease/staged/failed side effects、最近进度、PID、tmux、
数据库、磁盘、credential 元数据和 runtime health。连续两个周期无进度自动触发保全、分类、
回归测试、最小修复和闭合 checkpoint recovery。

## 统计与公开输出

Train 每批报告 pre/post accuracy、paired transitions、negative flips、McNemar exact p、累计曲线、
rule evidence、artifact bytes、parser、调用与失败；必须注明 post 是同题监督后诊断。

Test 主比较固定为 `C4 evolved - C0 baseline`，报告 Wilson 95% CI、paired delta、四格 flips、
negative-flip rate、McNemar exact p 与 deterministic paired bootstrap 95% CI。一次单类别探索性
结果不能被表述为已证明 full-evolve 普遍有效。

公开包只含 aggregate metrics、redacted manifest、图表和 SHA-256；题目、选项、GT、UID、
逐题 prediction/completion/transcript 和凭据只保存在 owner-private run evidence 中，不进入 Git
或桌面报告包。
