# ChemBench Temperature Full-Evolve v1 最终报告

## 结论

实验已完整闭合。Test 主比较为固定最终状态 C4 减 generation-zero baseline：baseline
93/100（93.00%），
evolved 83/100（83.00%），
配对差值 -10.00 percentage points，方向为**下降**。配对 bootstrap 95% CI
为 [-17.00,
-4.00] pp；McNemar exact p =
0.00634766。

这是一次单类别探索性实验；一次点估计或 p 值都不能证明 full-evolve 普遍有效。

## 数据与合规边界

- Train/Test: 100/100；batch size 25，共 4 批。
- split SHA-256: `779e1b60098a958519bfe61296b5cb6c28afa2a2db300238876dcb693ccb1100`。
- UID、标准化题面和选项集合的 Train/Test 交集均为 0。上游数据未提供可核验的
  source/paper/template group 字段，因此 source-group overlap 为 `NOT_ASSESSED`，不能虚报为 0；
  本实验仅报告基于可用题面与选项特征的确定性近重复分组。
- Test 在 Train 闭合前保持 sealed；Test feedback、Reflector、Core job、artifact update 均为 0。
- Candidate 与 baseline 使用相同模型、reasoning、prompt/harness policy、parser/evaluator 与 retry
  语义；managed Codex SHA-256 为 `a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902`。
- 历史暴露没有作为 eligibility exclusion，且没有用于本次固定池内的题目选择；
  因此被选项目可能曾在其他实验中暴露。与此同时，旧 artifact、completion、database、workspace
  的导入数量都为 0，C0 注入上下文为 0 bytes。该 provenance 限制不能被表述为 never-exposed
  验证。

## Train 同题诊断

四批 Pre 合计 88/100，监督演化后同题 Post 合计 90/100，差值
+2 题。Post 是看过本批 GT 的 Reflector 更新后的同题 replay，只衡量训练
适配，**不是独立泛化证据**。每批 transitions、McNemar、parser、调用、artifact 大小和规则
support/conflict 见 CSV 与图表。

最终 artifacts：text memory 3455 bytes
(`0cd3998a0f773977b05803b4c482ad70009c3a7027593cb7501e0ba06af23a57`)，skill bundle
784 bytes (`128d771e8399a12bf6c967da8006d252ebb6262ef00de21d15550ca903e62f50`)，
agent system 492 bytes
(`ebf98e210c2c025b9c1f9ebb30b54057823e02ddbfdaab00538c5d5c6bc2a903`)；组合注入
4731 bytes。最终 Test 固定使用 C4，没有 sweep 或按 Test
挑选 checkpoint。

## Test 配对审查

| 配对状态 | 数量 |
|---|---:|
| Both correct | 82 |
| Baseline only | 11 |
| Evolved only | 1 |
| Both wrong | 6 |

Negative flip rate 为 11.00%。Baseline/Evolved Wilson 95% CI
分别为 [86.25%,
96.57%] 和
[74.45%,
89.11%]。Official parser 成功数为
100/100，
strict parser 为 100/
100；retry 为
0/0（baseline/evolved）。

预注册的五项成功方向同时满足 2/5：evolved 点估计更高；evolved-only 多于
baseline-only；增益不依赖 parser/retry 差异；Test 隔离且无反馈/演化；Train 同题提升与独立
Test 提升方向一致。未同时满足时应按未证实或负结果汇报，不能改用中间 checkpoint。

## 为什么 baseline 可能反而更好

Generation-zero 已有较强先验时，新增规则的上行空间有限，而少数错误规则即可制造更多 negative
flips。Batch supervision 虽比逐题更新更稳定，仍可能把局部反应模式、选项相对关系或本批分布
固化成过宽规则。三个 target 即使职责分开，额外上下文也会竞争注意力、改变原模型原本正确的
判断路径。Train Post 又是监督后的同题重跑，容易高估可迁移性；累计使用最终 C4 会保留后期
错误，不能靠一次 Test 后选择 C1-C3 来补救。Parser 100% 成功也只说明格式有效，不等于化学
判断正确。

## 下一步设计建议

1. 预注册独立 forward validation：batch i 的规则先在 batch i+1 反馈前评估，只用该 validation
   guard 退役高 negative-flip 规则；Test 仍不可参与选择。
2. 把规则晋升门槛从“两个支持”扩展为跨 source-group 支持、最低效应和反例覆盖；对高 baseline
   置信题设置 conservative override，只有多证据一致才允许改变判断。
3. 做 target ablation（memory only、skill only、agent-system only、三者组合）及固定多 split
   重复；所有 arms 预先注册并校正多重比较，避免事后挑最好。
4. 继续压缩重复上下文并记录 rule-to-flip 归因；优先退役高冲突、低覆盖、引发 negative flip 的
   规则，但只能依据 Train/validation 证据。
5. 增加与现有 Temperature 分布不同的外部 held-out 数据，并把历史复用实验定位为机制探索，
   不把它当作 never-exposed 泛化证明。
6. 用多次独立 managed runs 估计 sampling variance；同时报告 effect size、paired CI、McNemar 和
   parser/retry 完整性，不以一次 p 值作为 go/no-go 的唯一依据。

## 调用、故障与恢复

Candidate/Reflector/Core logical calls/jobs 为 400/
4/12；实验模型逻辑调用为
404，managed runtime session attempts 为
404。404 个 accepted experimental calls 均已通过
credential-readiness；readiness canary 自身的原始 provider attempt 精确总数未被权威采集，状态为
`NOT_MEASURED`，只能报告保守下界
404。因此不能把 404 解释为本实验全部 provider
调用的精确总数。Failed attempts/retries/rejected attempts/recoveries/invalidated runs 为
0/0/0/
0/
0。完整闭集分类见 `call_and_failure_summary.json`。

Run IDs: `stv3-temperature-full-evolve-v1-20260802T191250Z`, `stv3-temperature-full-evolve-v1-20260802T191250Z-evolved-test`, `stv3-temperature-full-evolve-v1-20260802T191250Z-baseline-test`, `stv3-temperature-full-evolve-v1-20260802T191250Z-core`, `stv3-temperature-services-20260802T190719Z-b625a8ce`。
生成时间：2026-08-02T23:37:47Z。
