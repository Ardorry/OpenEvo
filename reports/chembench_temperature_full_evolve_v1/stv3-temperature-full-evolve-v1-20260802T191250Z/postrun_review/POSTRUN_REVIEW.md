# ChemBench Temperature Full-Evolve v1 终版审查补充

## 结论先行

本次 100 Train + 100 Test 的正式配对实验完整闭合，但 full-evolve 明确没有达到预期：

- generation-zero baseline：93/100，93.00%；
- frozen C4 evolved：83/100，83.00%；
- evolved − baseline：−10.00 percentage points；
- paired transitions：both correct 82、baseline-only 11、evolved-only 1、both wrong 6；
- paired bootstrap 95% CI：[-17, -4] pp；
- McNemar exact p：0.00634765625；
- 两臂 official/strict parser 均为 100/100，retry、失败和 rejected attempt 均为 0。

因此，下降不是格式解析、重试选择、调用失败或 Test 在线反馈造成的。它来自冻结 artifacts 改变了模型判断：每产生 1 个正向 flip，同时产生了 11 个负向 flip。单次探索性实验不能证明 full-evolve 普遍无效，但对当前设计应给出明确的 no-go 结论，不能继续把同题 Train Post 提升当作泛化代理。

## 完整性与合规审查

成功 run 使用 4 个连续 25 题 Train batch。每批只有一次逻辑 Reflector synthesis 和三个正式 Core jobs；总计 200 个 Train Candidate 调用、4 个 Reflector、12 个 Core jobs。C4 在 Test 前冻结，随后顺序执行 100 个 evolved Test，再执行独立空上下文的 100 个 baseline Test。Test 阶段新增 Reflector、Core job 和 artifact update 均为 0。

成功 run 共 400 个 Candidate 和 4 个 Reflector logical calls，404 个 managed runtime session attempts 全部 accepted，失败、retry、rejected attempt 和 recovery 均为 0。公开包不含题目、选项、GT、逐题预测、completion、transcript、UID 或凭据信息。正式报告包的 31 个内容文件均通过 `SHA256SUMS.txt` readback 校验。

数据 provenance 仍有一个必须保留的限制：本次按用户授权使用固定官方 Temperature pool 的 fresh-C0 策略，而不是严格 never-exposed eligibility。Train/Test 在本次 split 内的 UID、规范化题面和选项交集为 0；但上游没有可核验的 source/paper/template group 字段，且选中项可能在其他历史实验中出现。因此结果适合机制探索，不应宣传为严格未暴露外部泛化证明。

## Train 证据为什么没有支持 Test 泛化

| Batch | Pre | Post | Delta | Negative flips | Active/conflicted rules | Context bytes |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 21/25 | 21/25 | 0 pp | 0 | 5 / 3 | 5368 |
| 2 | 21/25 | 23/25 | +8 pp | 0 | 9 / 5 | 5699 |
| 3 | 23/25 | 23/25 | 0 pp | 1 | 10 / 6 | 4824 |
| 4 | 23/25 | 23/25 | 0 pp | 0 | 10 / 6 | 4731 |

合计 Pre 88/100、Post 90/100，只提升 2 题，而且全部来自 Batch 2 的同题 replay。Batch 1、3、4 没有净提升；Batch 3 已出现一次负向 flip。Post 在 Reflector 看过本批 GT 后重跑相同题目，只能证明监督适配，不是 forward generalization。

规则证据也显示了风险积累：到 C4 共有 10 条 active rules，其中 6 条带冲突；累计 support reference 为 95，contradiction reference 已达 53，但 retired rules 始终为 0。也就是说，设计会记录冲突，却没有把高冲突规则有效退出最终注入。最终 context 虽只有 4731 bytes、低于 6 KiB 目标和 8 KiB 硬上限，但“预算合法”不等于“信息有益”。text memory 独占 3455 bytes，约占组合正文 73%，全局类别规则很容易覆盖模型原有的强先验。

## baseline 反而更好的主要机制

1. **高基线造成收益不对称。** C0 已有 93% 准确率，只剩 7 个潜在上行题，却有 93 个可被错误规则破坏的题。更新若没有极强安全门，负向空间天然远大于正向空间。
2. **两样本支持门槛太弱。** 两个相似 Train 例子足以把局部模式晋升为全局 Temperature 规则；缺少跨 batch、跨来源或 forward validation 支持。
3. **冲突被累计但没有退役。** C4 的 10 条规则中 6 条 conflicted、矛盾引用 53 条、retired 为 0，说明 evidence index 的审计能力强于实际治理能力。
4. **三目标同时改变放大了干预。** text memory、skill 和 agent system 同时注入会改变知识、推理流程和行为纪律。职责分离减少了文本重复，却没有提供因果归因；任何一个 target 的错误都可能污染全部 Test。
5. **最终 C4 被强制使用，但没有独立晋升门。** 当前协议正确地禁止 Test 后挑 C1-C3，但 Train 内也没有独立 validation 来判断 C4 是否比 C0 更安全。
6. **上下文稀释仍可能存在。** 4.7 KiB 没有超预算，但对本来已能正确作答的模型，额外的广义温度规则可能把注意力从题目中的显式试剂、溶剂、低温/回流信号移开。
7. **Train 目标没有惩罚 negative flip。** 同题 Post 只看净正确数；对于 93% 高基线，更合理的目标应对“把原本正确改错”施加更高代价。

上一轮 50 题 Temperature 配对曾出现 baseline 94%、evolved 82%；本轮是 93% 对 83%。由于历史暴露与样本独立性不能严格证明，不能把两轮合并做正式显著性推断，但方向高度一致，提示问题更可能来自设计的系统性干预，而不是一次偶然波动。

## 建议的下一版实验设计

### P0：加入 Train 内 forward-validation promotion gate

把 100 个 Train 项重新定义为 75 个 evolution + 25 个内部 validation，或采用一批延迟部署：Batch i 只生成候选 `D_i`，在 Batch i+1 的 GT 揭示前，分别用 `D_i` 和 C0 对同一批做 paired shadow inference。只有预注册 safety utility 通过，`D_i` 才成为 active state。最后一个没有 forward validation 的候选不得进入最终 Test。

建议的 promotion utility 为：

`positive_flips - 2 × negative_flips`

同时要求 negative flips 不多于 positive flips、parser/retry 差异为 0，并报告 paired CI。系数必须在实验前固定，不能看到 validation/Test 后调整。

### P0：保留 generation-zero shadow arm

当前 Batch 2–4 的 Pre 只运行当前 evolved state，无法知道它在新题上相对 C0 的净变化。下一版应在每个新 batch、GT 揭示前增加相同配置的 C0 shadow prediction。这样每批都能产生真正的 forward paired flips，而不是只比较同题 Pre/Post。

### P1：提高规则晋升和退役门槛

- 普通知识至少需要来自两个不同 batch 的支持，而不仅是两个样本；
- 若能获得 source/reaction/template group，要求跨 group 支持；否则先做确定性反应近重复聚类；
- provisional rule 不进入 runtime injection；
- contradiction/support 比超过预注册阈值，或在 forward validation 中产生净负 flip，必须降权或退役；
- retirement 规则要有可执行门槛，避免再次出现 6 条 conflicted、0 条 retired。

### P1：先做 target ablation，再测试三目标组合

在独立内部 validation 上预注册比较：memory-only、skill-only、agent-system-only、memory+skill、三目标。不能在最终 Test 上挑最好。当前 text memory 占最终 context 的大部分，应优先审查；agent system 建议先固定为最小稳定协议，不要与类别事实一起频繁演化。

### P1：改为任务相关的稀疏注入

不要给每道题注入完整 4.7 KiB context。根据题面可见反应/试剂信号做确定性、无 GT 的 top-k rule retrieval，只注入最相关且高支持、低冲突的少量规则；skill 保留短流程，agent system 保持固定 parser-compatible 行为。检索器及 k 值必须在 Test 前冻结。

### P2：扩大真正独立的数据和重复次数

当前官方池只有 202 项且历史严格未暴露无法证明。要回答“进化是否泛化”，需要新增带 source group 的独立 Temperature 数据，或至少进行预注册的多 split/多 managed-run 重复，并对多臂比较校正。一次 p 值不能作为算法普遍有效或无效的最终证明。

## 建议的决策

当前三目标 continuous full-evolve 不应直接进入更大 Test 或其他类别。先实现 C0 shadow、forward-validation delayed promotion、冲突退役和 target ablation，再用不参与设计选择的新 Test 做一次确认性实验。成功门应至少要求 evolved 点估计高于 baseline、positive flips 多于 negative flips、paired CI 不显示实质性下降、parser/retry 完整一致，并在多个独立重复中方向一致。

## 运行与兼容性说明

正式推理与报告绑定源码提交 `b625a8ceabc512636a474502d84a93ece1f50929`。报告 closure 前发现 fresh run root 本身因隐式 parent mkdir/umask 为 `0755`，敏感子目录均已是 `0700`；只把 completed run root mode 收紧为 `0700` 后 closure 通过，没有修改任何结果字节或实验语义。后续提交 `2cf53173a20d0a275ccf5b008d05af2b5c5409d2` 已让未来 run root 和 `arms/` 显式以 `0700` 创建，并有聚焦回归。

正式 `call_and_failure_summary.json` 中 `invalidated_formal_runs=0` 的作用域是成功 controller run 本身；整个开发/正式运行 campaign 在成功前另有 7 个未完成的 immutable attempts，其中 2 个有显式 invalidation receipt。完整 aggregate run history 见 `COMPATIBILITY_RECEIPT.json`，不得把该字段误读为“整个 campaign 从未失败”。
