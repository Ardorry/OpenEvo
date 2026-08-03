# ChemBench Temperature Safe-Evolve V2 详细终审

## 1. 终态

- 正式状态：`NO_GO_ABLATION_NO_SAFE_SINGLE_TARGET`
- Campaign：`stv3-temperature-safe-evolve-v2-20260803T051426Z`
- 正式推理源码：`cae3b81b57b26bed2eed418af99869c84843b1d4`
- Phase A：完整闭合 8 个 arm、800 个 accepted Candidate context
- Reflector / Core：`0 / 0`
- R0–R3：`NOT_RUN_BY_PREREGISTERED_STOP_RULE`
- 正式结果目录：`reports/chembench_temperature_safe_evolve_v2/stv3-temperature-safe-evolve-v2-20260803T051426Z`
- 正式 `SHA256SUMS.txt` 哈希：`475a2244c5eabc3191b31ac303a9c8ccc0c710d37f12babc018f75bfab618a54`

这是对重复官方 Temperature 题池的机制诊断，不是历史 never-exposed 或外部独立泛化证据。所有比较必须以本 campaign 内 A0 的配对结果为参照；不能把不同运行的绝对分数直接当成模型能力变化。

## 2. 八臂结果

| Arm | 注入状态 | 正确率 | 相对 A0 | 正向 flip | 负向 flip | Utility |
|---|---|---:|---:|---:|---:|---:|
| A0 | generation zero | 94/100 | 0 pp | 0 | 0 | 0 |
| A1 | memory | 86/100 | -8 pp | 0 | 8 | -16 |
| A2 | skill | 94/100 | 0 pp | 2 | 2 | -2 |
| A3 | agent system | 92/100 | -2 pp | 1 | 3 | -5 |
| A4 | memory + skill | 89/100 | -5 pp | 1 | 6 | -11 |
| A5 | memory + agent | 86/100 | -8 pp | 0 | 8 | -16 |
| A6 | skill + agent | 93/100 | -1 pp | 2 | 3 | -4 |
| A7 | memory + skill + agent | 87/100 | -7 pp | 1 | 8 | -15 |

所有 arm 的 parser 均为 100/100，retry、failure 和 timeout 均为 0，因此差异不能归因于解析或基础设施不对称。

## 3. 主要结论

### 3.1 text memory 是主要损害来源

Memory-only 从 94 降至 86，产生 0 个正向 flip 和 8 个负向 flip；McNemar exact p 为 0.0078125，配对 bootstrap 95% 区间为 [-14, -3] pp。两个固定半集分别下降 12 pp 和 4 pp，方向一致。三因子分析中 memory 主效应为 -6.25 pp，远大于其他组件。

Memory 与其他组件组合后没有得到可靠补偿：memory + skill 为 -5 pp，memory + agent 为 -8 pp，三组件为 -7 pp。小幅正交互不足以抵消 memory 的大幅负主效应。

### 3.2 skill 没有表现出净收益

Skill-only 的绝对准确率与 A0 同为 94%，但发生 2 个正向和 2 个负向 flip，按预注册损失函数得到 `U = 2 - 2×2 = -2`。H0 为 +2 pp、H1 为 -2 pp，方向不稳定。因此它不满足 `U > 0`、正向多于负向、两个半集均不下降等条件。

这个结果不能解释为“skill 已经安全”。它只说明 skill 比 memory 的危害小，但仍会交换正确题，且没有净保护效用。

### 3.3 agent system 也不是无害控制项

Agent-only 为 -2 pp，1 个正向、3 个负向，utility 为 -5。虽然只有 492 bytes，仍可改变模型的证据优先级和输出前推理路径。小 payload 不等于小行为影响。

### 3.4 完整 C4 的下降可复现，但跨运行绝对分数会波动

本 campaign 中 A7 为 87/100，相对同轮 A0 的 94/100 下降 7 pp；1 个正向、8 个负向，McNemar exact p 为 0.0390625，bootstrap 95% 区间为 [-13, -2] pp。上一轮曾观察到 83 对 93。两轮绝对分数不同，说明单次模型采样存在波动；稳定结论来自两轮均显示 evolved 相对同轮 baseline 明显下降，而不是把 83、87、93、94 横向混算。

## 4. 为什么上一轮会失败

1. **大而宽的 memory 覆盖了题面证据。** 冻结 memory 为 3455 bytes，是三个组件中最大者。它包含带边界但仍较宽的温度启发式，容易在新题中压过显式反应条件。
2. **全量固定注入造成提示竞争。** V1 每题注入完整上下文，不按题面检索相关 entry；无关规则也会参与模型判断。
3. **训练诊断不是 forward validation。** 四个 Train batch 的同题 pre/post 为 21→21、21→23、23→23、23→23，看起来安全，但这些题已经向 Reflector揭示监督信息，不能估计下一批新题上的负向 flip。
4. **V1 的 retire 是可选模型动作。** Controller 只有收到显式 `operation=retire` 才退役规则，没有根据 contradiction、连续负效用或 stale evidence 自动生成 mandatory retirement。
5. **冲突只会弱化规则，不会清除规则。** C4 有 10 条非 retired 规则，其中 7 confirmed、3 provisional；6 条带 contradiction，仍然 0 retired。3 条 provisional 不再投影，但 7 条 confirmed 中仍有 4 条带 contradiction。
6. **没有 baseline shadow 和延迟晋升。** 每批合法 successor 都连续成为下一批 active state，没有先在未揭示 GT 的 forward block 上证明收益。
7. **Test 损害出现得太晚。** 最终 Test 冻结后禁止反馈，这是正确隔离；但也意味着 Test 上的负向 flip无法修复。因此安全性必须在 Train-side forward validation 阶段建立。

这些证据支持“行为性、启发式过拟合和提示稀释”的解释；它们不证明逐字记忆答案。正式 artifact 没有发现选项字母或答案映射，问题在于窄样本总结出的规则被过宽地应用。

## 5. 预注册停止是否正确

是。Memory-only 明显不合格；skill-only 虽不降准确率，却没有正 utility，且两个固定半集方向不一致。若此时为了继续 R0 而放宽阈值，会使用同一组诊断结果反向修改实验标准，构成结果后调参。停止避免了额外 500–2000 个 Candidate context，并防止在无安全单目标的前提下制造“安全演化”结论。

## 6. 下一步设计建议

### 必须先完成

1. 不复用 C4 memory、skill 或 agent state；新实验仍从 fresh C0 开始。
2. 不用本次 100 题的结果直接改写规则后再把同一题池称为独立 Test。
3. 将 V2 中 stronger-conflict 与 safety-failure 两类 retirement condition 接入 Controller 的可审计 mandatory ID 生成，并增加端到端测试；目前正式源码只机械接通了四类 evidence trigger。
4. 保留 generation-zero shadow，并把每个被检索 entry 与 forward positive/negative flip 建立归因记录。

### 推荐的最小新实验

- 只研究 `skill_bundle`，memory 和 agent system 保持空；这不是因为 skill 已通过，而是因为它是唯一接近中性的组件。
- 每题 top-1，单题硬上限 500–700 bytes；最多 3–4 个 active reasoning steps，而不是 6 个宽规则。
- 新 entry 至少两个独立 Train 支持；先进入 provisional，必须在下一未揭示 GT block 上满足 `U > 0` 且 negative flip 为 0 或最多 1 才晋升。
- 两个连续 forward block 非正 utility，或累计两个 negative flips 且正向不占优时，Controller 强制 retire。
- V2 deployment gate 继续保留；未通过时直接复用同 run 的 G0 prediction，不增加模型调用。

### 最有价值的数据改进

优先构建新的外部 Temperature 题集，并按反应/source/template group 隔离。重复官方题池的四折只能提供机制探索；真正判断 evolution 是否改善泛化，需要一次从未参与阈值、prompt、规则或 artifact 设计的新数据评估。

## 7. 不应采取的做法

- 不选择八臂中分数最高的组合后继续多目标演化。
- 不把 A2 的 94/100 描述为 skill 有效，因为其 utility 为负且半集方向不稳定。
- 不挑选历史 checkpoint 代替最终状态。
- 不降低 negative-flip 惩罚、promotion gate 或 deployment gate 来换取继续运行。
- 不将本次 no-go 描述成模型失败；这是安全机制正确拒绝了未经证明的 artifact。

## 8. 后运行基础设施修复

Phase A 在 A2 曾遇到一次 claim 前的瞬态 runtime-health 失败。权威 ledger 证明当时没有 open claim、没有 completion 歧义，恢复后重复模型调用为 0。后运行补丁仅在 `CALL_CLAIMED` 之前增加有限健康重试；一旦 claim 写入，仍然禁止自动重提。这一修复不改变本次 800 个正式 completion 或统计结果。
