# ChemBench Temperature Full-Evolve v1 最终报告

## 最终判定

**HARD BLOCKED — `INSUFFICIENT_NEVER_EXPOSED_TEMPERATURE_DATA`**

这是零模型调用 preflight 的合规终态，不是半途退出。用户协议明确规定：少于 100 道从未暴露
的 Train 加 100 道从未暴露的 Test 时必须停止正式实验。

## 容量证据

| 证据 | 数量 |
|---|---:|
| 冻结 Temperature Test 全集 | 202 |
| Dev demonstrations，排除 | 5 |
| 旧权威 exposure snapshot 已确认实际暴露 | 26 |
| 当前实际暴露并集 | 121 |
| 可证明的 never-exposed 最大上界 | 81 |
| 最低总需求 | 200 |
| 近重复分组前的缺口 | 119 |

72 份私有事件账本共 15027532 bytes，inventory SHA-256 为
`46d07a1e5324230a8a0c9372ec09069767516192d51530f8ddc14c2380e54be0`。全部记录成功解析，未知 Temperature UID 为零。当前暴露并集
在旧 v2 分区中为：Train 50、Test
50、Reserve
21；剩余上界为：Train
0、Test
0、Reserve
81。

集合 digest 使用“唯一 UID 排序、LF 连接并保留末尾 LF 后计算 SHA-256”的固定算法，不披露
UID 明文：

- Temperature 全集：`bf2ba97a3fe2ccc0a356441eabdd0281e34203afa2a39f5c0d97fc6ec654c707`
- 实际暴露并集：`e24f8a3413c612a7df67f15b801e4e1e850fb70bfedfab0b6c1b1e7770b57873`
- 最大剩余集合：`f5be563006b3874903c828dd1cff4b6cd4340825b205bea0cc6e78d4aae8968b`

旧 tracked v2/v3 receipt 中“历史 Test 暴露为零”只是当时的快照，不能作为本次隔离证明。
当前 v1 账本与后来 v2 Test 相交 9 题、与 Reserve 相交 21 题；v3 账本已覆盖 Temperature 的
Train 50 与 Test 50 全部题目。

## 零副作用与身份审计

- 正式 run ID：无。
- Candidate、Reflector、baseline 及总模型调用：0。
- Core job、模型重试、正式 recovery：0。
- 报告工具故障/修复：1/1，模型副作用为 0。
- Split SHA-256：未生成，因为容量门槛先失败。
- 审计源码 commit：`7dc1982f15a9f86e83f3264447bb47cc76cf78bd`，分支 `chembench-temperature-full-evolve-v1`。
- 模型配置：`gpt-5.5`，reasoning
  `medium`。
- Managed native executable SHA-256：
  `a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902`。
- Managed candidate image：
  `sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`。
- 凭据检查只验证 owner/type/mode 元数据，没有读取或复制凭据内容。

## 测试审计

- 新协议聚焦测试：17 passed，
  0 failed。
- ChemBench 完整测试：1023 passed，
  39 failed；另有
  58 subtests passed。
- 完整测试中的失败已在 `TEST_REPORT.json` 分类；没有一项来自新 preflight 的聚焦测试，且
  测试过程模型调用为 0。旧 source-manifest、legacy recovery scope 与缺失旧 checkout/private
  fixture 保持 fail closed，没有为追求全绿而改写历史证据。

## 代码审查

Candidate 与 baseline 可复用正式 `TaskRequest -> Rollout -> Gateway -> CodexHarness`
executor、同一 parser/evaluator、managed runtime identity、空上下文 baseline、Core planned
jobs、typed artifacts 和 paired statistics。本轮不需要修改 `src/openevo/**`。

但现有 STV3 不是本实验：它逐题更新，记录 `resume_allowed=false`，没有 batch-25 原子
exactly-once ledger，artifact 上限远高于本轮 8 KiB，报告路径还可能含逐题 CSV。现有
Reflector 虽由 managed Codex 与 Core planned job 承载，却没有经过用户要求的正式 Candidate
harness 调用链。新数据通过门槛后，必须在独立 benchmark package 中修复这些问题；实现审查共
列出 6 项具体缺口。

## 为什么上一轮 baseline 反而更好

Train 同题从 38/50 升至 48/50 是监督后 replay 证据，不是独立泛化。Generation-zero baseline
已经达到 47/50，向上空间很小，少数 negative flips 就足以主导类别差值。上一轮逐题 updater
容易记录近期样本和局部相关性；三个 target 内容重叠、总上下文约 17.1 KiB，又会稀释原题信号。
在没有 forward unseen validation 的情况下固定使用最终累计状态，会把这些错误保留下来。
Parser 成功只代表输出可被评分，不代表化学答案正确。

因此，更合理的解释是过拟合与上下文干扰，而不是“演化天然有害”。

## 下一步设计建议

1. 获取并 pin 新的权威 Temperature 数据源：在现有 81 题上至少新增 119 道从未暴露题，并为
   source-group/语义近重复损耗预留余量；不得伪造或改写题目。
2. 在写 runner 或调用模型前，针对新 revision 重建历史暴露闭包，再冻结 group-aware split
   与 source closure。
3. 每 25 题只做一次 batch synthesis；使用 canonical evidence index、support/contradiction、
   条件化规则和确定性退役。不得把同题 Post 提升当作泛化。
4. 三目标严格分工：事实进 text memory，流程进 skill，长期行为进 agent system；按包含 framing
   的真实序列化大小执行 6 KiB 目标与 8 KiB 硬门。
5. 增加 forward-only Train 诊断：截至 batch i 学到的规则，先在 batch i+1 的反馈前评估，再
   用 Train 证据校准或退役；Test 永不用于选择。
6. 预注册独立的 Train validation slice，或获取足够额外数据单独留出；只用这部分 guard
   negative flips，仍固定在 Test 前冻结 Ck。
7. 让 Reflector synthesis 也经过正式 managed harness，同时保留 Core 对 planned job 的所有权；
   对每个 accepted completion 和 artifact commit 边界做 crash-injection 测试。
8. 在多个预注册 split 或更大的外部 holdout 上重复后，再讨论 full-evolve 是否有效；同时报告
   effect size、paired flips 和置信区间，不能只看一次 p 值。

## 未产生的实验指标

Train 曲线、artifact、evolved Test、baseline Test、paired delta、flips、McNemar p 和置信区间
均明确为 `NOT_RUN`。生成全零结果图会造成误导，因此只生成 `capacity_gate.svg`。

审计 ID：`stv3-temperature-full-evolve-v1-precheck-20260802T084229Z`。生成时间：`2026-08-02T08:42:29Z`。
