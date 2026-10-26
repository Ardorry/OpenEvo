# ChemCrow 14 题结果：OpenEvo 与 ChemCrowRun Reproduction

本文整理原始 ChemCrow 14 题上的官方结果、OpenEvo 实验和 ChemCrowRun Reproduction（重新复现版）结果。所有分数均保留原始量纲；不同评分协议不混合计算。

本文统一使用以下名称：

- **OpenEvo G1 / G2**：使用 `gpt-5.5` 完成的历史实验。
- **ChemCrowRun Reproduction G1 / G2**：使用 `gpt-5.6-sol` 在独立状态和缓存中完成的重新复现版实验。

## 结果总览

| 系统 | 候选模型 | 阶段 | 人类专家综合分（/10） | GPT-4 评价（/10） | 内部盲评（/12） | 已记录 Judge 成本 |
|---|---|---|---:|---:|---:|---:|
| 官方 GPT-4 | GPT-4 | 无工具基线 | **7.696** | **8.750**（论文固定评分） | — | 未公开 |
| 官方 ChemCrow | GPT-4 | ChemCrow 工具增强 | **8.589** | **7.357**（论文固定评分） | — | 未公开 |
| **OpenEvo G1** | `gpt-5.5` | 进化前 | — | **9.357** | **9.929** | G1 + G2 合计 **$2.65887** |
| **OpenEvo G2** | `gpt-5.5` | 三路 Reflector 后 | — | **9.214** | **10.286** | 同上 |
| **ChemCrowRun Reproduction G1** | `gpt-5.6-sol` | 进化前 | — | **9.607** | **10.629** | G1 + G2 合计 **$2.99460** |
| **ChemCrowRun Reproduction G2** | `gpt-5.6-sol` | 三路 Reflector 后 | — | **9.571** | **11.014** | 同上 |

> **最佳复现结果：** ChemCrowRun Reproduction G2 的内部盲评为 **11.014/12**；ChemCrowRun Reproduction G1 的 GPT-4 评价为 **9.607/10**。重新复现版在 G1 和 G2 两个阶段的两套均分均高于对应的 OpenEvo 结果。

## 进化前后变化

| 实验 | 内部 G1 | 内部 G2 | 内部变化 | GPT-4 G1 | GPT-4 G2 | GPT-4 变化 |
|---|---:|---:|---:|---:|---:|---:|
| OpenEvo | 9.929 | 10.286 | **+0.357** | 9.357 | 9.214 | -0.143 |
| **ChemCrowRun Reproduction** | **10.629** | **11.014** | **+0.386** | **9.607** | **9.571** | -0.036 |

内部评价在两次实验中都记录到 G2 提升。GPT-4 评价中的变化很小：OpenEvo 为 -0.143，ChemCrowRun Reproduction 为 -0.036。重新复现版的外部评价变化接近零，同时取得了更高的 G1、G2 绝对分数。

## 逐题 GPT-4 评价

官方 ChemCrow 列采用与 ChemCrowRun Reproduction 同轮 Judge 得到的 2026-10 统一重评分数。OpenEvo 两列来自其 2026-08 冻结 Judge；因此逐题表用于展示各运行内部的成绩分布，不把两个日期的 Judge 输出视为完全相同的测量条件。

| 任务 | 官方 ChemCrow 重评 | OpenEvo G1 | OpenEvo G2 | ChemCrowRun Reproduction G1 | ChemCrowRun Reproduction G2 |
|---|---:|---:|---:|---:|---:|
| 01 Safinamide | 7.0 | 9.0 | 9.0 | 9.5 | 9.0 |
| 02 Discover catalyst | 7.0 | 9.0 | 9.0 | 9.0 | 10.0 |
| 03 Explain mechanism | 9.0 | 10.0 | 10.0 | 10.0 | 10.0 |
| 04 Insect repellent | 9.0 | 9.0 | 8.0 | 9.0 | 9.0 |
| 05 How to make | 7.0 | 9.0 | 9.0 | 10.0 | 10.0 |
| 06 Lindlar mechanism | 9.0 | 10.0 | 9.0 | 10.0 | 10.0 |
| 07 Modify and synthesize | 9.0 | 9.0 | 9.0 | 10.0 | 9.0 |
| 08 Ricci organocatalyst | 7.0 | 10.0 | 10.0 | 9.0 | 10.0 |
| 09 Predict incompatibility | 9.0 | 9.0 | 9.0 | 10.0 | 10.0 |
| 10 Product property | 6.0 | 9.0 | 8.0 | 9.0 | 9.0 |
| 12 Similar mechanism of action | 10.0 | 10.0 | 10.0 | 10.0 | 10.0 |
| 13 Atorvastatin synthesis | 8.0 | 9.0 | 9.0 | 9.0 | 9.0 |
| 14 Aspirin synthesis | 3.0 | 10.0 | 10.0 | 10.0 | 9.0 |
| 15 Takemoto catalyst | 7.0 | 9.0 | 10.0 | 10.0 | 10.0 |
| **均值** | **7.643** | **9.357** | **9.214** | **9.607** | **9.571** |

## ChemCrowRun Reproduction 相对 OpenEvo 的变化

| 指标 | G1 变化 | G2 变化 |
|---|---:|---:|
| 内部盲评（/12） | **+0.700** | **+0.729** |
| GPT-4 评价（/10） | **+0.250** | **+0.357** |

ChemCrowRun Reproduction 使用 `gpt-5.6-sol`，OpenEvo 使用 `gpt-5.5`。模型版本和服务时间构成混杂因素，上表表达重新复现实验的观察差异，不能把全部变化归因于进化系统。

## 成本与调用记录

| 项目 | OpenEvo | ChemCrowRun Reproduction |
|---|---:|---:|
| 有效 GPT-4 Judge 结果 | 42 | 42 |
| Judge 成本 | $2.65887 | **$2.99460** |
| 候选与进化模型 | `gpt-5.5` | `gpt-5.6-sol` |
| 生成侧美元成本 | 订阅成本不可归因 | 订阅成本不可归因 |
| 记录的成功终态模型调用 | — | 112 |
| 记录的 input tokens | — | 5,960,169 |
| 记录的 cached input tokens | — | 4,537,728 |
| 记录的 output tokens | — | 246,044 |
| 记录的 reasoning output tokens | — | 122,061 |

Token 字段存在包含关系，不能相加为总 token。ChemCrowRun Reproduction 的独立付费链路 probe 成本为 $0.00147，未计入 benchmark Judge 成本。

## 指标说明

- 官方人类专家综合分是化学准确性、推理质量和任务完成度的均值。
- OpenEvo 与 ChemCrowRun Reproduction 没有执行原论文式四位化学家评审，因此人类专家列不使用模型评分填充。
- GPT-4 评价采用 ChemCrow EvaluatorGPT 风格的校准协议；它是 LLM Judge 结果，不代表官方排行榜认证。
- `Pass@n` 不适用于这里的公开主指标。ChemCrow 是开放式化学任务，使用连续评分评价答案，没有可自动判定的二元测试通过条件。
- 配对统计没有建立 G2 显著优于 G1 的证据。结果应解释为内部评价有所改善、GPT-4 评价整体接近稳定。

## 数据来源

- [ChemCrow peer-reviewed paper](https://www.nature.com/articles/s42256-024-00832-8)
- [Official Nature Source Data](https://media.springernature.com/original/springer-static/esm/art%3A10.1038%2Fs42256-024-00832-8/MediaObjects/42256_2024_832_MOESM3_ESM.xlsx)
- [Official ChemCrow experiment notebooks](https://github.com/ur-whitelab/chemcrow-runs)
- [ChemCrowRun acceptance report](https://github.com/Ardorry/OpenEvo/blob/03c6e91ac/benchmarks/chemcrow/reports/chemcrowrun_acceptance_20261015/chemcrowrun-report.json)
- [ChemCrowRun comparison data](https://github.com/Ardorry/OpenEvo/blob/03c6e91ac/benchmarks/chemcrow/reports/chemcrowrun_acceptance_20261015/chemcrowrun-comparison.json)
