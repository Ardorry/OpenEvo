# ChemBench 分支导航

本分支保存 OpenEvo 的 ChemBench 独立研究适配、测试、配置与历史汇总报告。
本次整理基于已发布提交 `f7aebe8adade4170537186873258ae308c1a1731`
（2026-08-04），结果证据截止于 `2026-08-02T06:44:21.965303Z`。

| 内容 | 入口 |
| --- | --- |
| 协议、环境与执行边界 | [包说明](README.md) |
| 适配与执行实现 | [代码目录](src/openevo_chembench/) |
| Supervised Transfer V3 实现 | [V3 模块](src/openevo_chembench/supervised_transfer_v3/) |
| V3 配置与恢复说明 | [配置目录](configs/supervised_transfer_v3/) |
| V3 维护者执行入口 | [main.py](scripts/supervised_transfer_v3/main.py) |
| V3 协议与恢复测试 | [测试目录](tests/supervised_transfer_v3/) |
| 公开汇总、图表与归档边界 | [结果归档](../../docs/experiments/chembench-supervised-transfer-v3-202608/README.md) |
| 终版配对报告 | [中文报告](../../docs/experiments/chembench-supervised-transfer-v3-202608/final-feasibility-package/ChemBench_STV3_最终会议汇报.md) |
| 可机读指标 | [summary_metrics.json](../../docs/experiments/chembench-supervised-transfer-v3-202608/final-feasibility-package/summary_metrics.json) |

## 历史结果

STV3 使用九类各 50 道 Train 题进行监督演化，冻结每类的
`text_memory`、`skill_bundle`、`agent_system` 三个目标，再在同一组
450 道 Test 题上分别评估 generation-zero baseline 与 frozen evolved。
归档报告记录两组模型均为 `gpt-5.5`、reasoning effort `medium`。

| Test 指标 | 归档值 |
| --- | ---: |
| Generation-zero baseline | 388/450，86.22% |
| Frozen evolved | 382/450，84.89% |
| Evolved − Baseline | −1.33 个百分点 |
| 仅 baseline 正确 / 仅 evolved 正确 | 23 / 17 |
| 配对 McNemar exact p | 0.4296 |

报告结论为“工程闭环可行；进化有效性未证明”，本设计不应作为优于 baseline
的版本晋升。Train 同题监督后从 382/450 升至 402/450，不能替代独立 Test
上的泛化证据。

本结果属于研究性监督迁移。两组均由已封存父前缀与恢复后缀组成，应标注
`PAIRED_DESCRIPTIVE_RECOVERY_COMPARISON_NOT_REPLICATED_CAUSAL_TRIAL`，
且不是标准 ChemBench4K 排行榜分数。归档复用了官方题池，不能证明严格的
历史从未暴露或外部独立泛化；完整口径以终版报告为准。

## 本次整理与完整性

2026-09-10 已直接按该提交的 Git 文件内容校验归档中的两份 SHA-256 清单：
[归档清单](../../docs/experiments/chembench-supervised-transfer-v3-202608/SHA256SUMS.txt)
21/21 项一致，
[结果包清单](../../docs/experiments/chembench-supervised-transfer-v3-202608/final-feasibility-package/SHA256SUMS.txt)
15/15 项一致。原代码身份见
[CODE_IDENTITY.txt](../../docs/experiments/chembench-supervised-transfer-v3-202608/CODE_IDENTITY.txt)。
这是对已发布文件完整性的核验；本次没有重新执行实验或复核私有运行账本。

同日检查本地 `chembench-lamalab-fullrun-readiness-v4` 工作区，发现只剩空目录
骨架，普通文件数为 0，没有 Git 元数据、源码或结果。因此，本分支保留上述
历史版本，不能视为该 readiness-v4 工作区最新内容的恢复或完整上传。

公开归档仅包含汇总、分类统计、图表与选定审计说明；数据集、逐题题目与答案、
completion、transcript、数据库、运行时状态及凭据不纳入本次整理。
复现实验仍需按包说明准备相应数据、私有状态和执行环境。

[返回 Fork 实验分支总览](https://github.com/Ardorry/OpenEvo/blob/stable/BENCHMARKS.md)
