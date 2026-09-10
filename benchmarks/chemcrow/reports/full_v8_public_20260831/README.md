# ChemCrow full-v8 公开汇总

本目录整理已完成的 14 题 full-v8 实验。结果状态为 `PROVISIONAL_LLM_JUDGED_RESULT`：模型评审已经完成，尚无人类专家确认，也不代表官方榜单认证。内部评分有所提高，但校准 GPT-4 Judge 没有确认这一改善。

| 评分方式 | G1 均分 | G2 均分 | G2−G1 | G2 胜/平/负 | N |
|---|---:|---:|---:|---:|---:|
| 内部 evolution evaluator /12 | 9.786 | 9.929 | +0.143 | 5/7/2 | 14 |
| 内部盲评 /12 | 9.929 | 10.286 | +0.357 | 9/1/4 | 14 |
| 校准 GPT-4 Judge /10 | 9.357 | 9.214 | −0.143 | 1/10/3 | 14 |

内部两类评分均由三个 0–4 分维度组成。盲评表的胜/平/负来自 evaluator 显式 winner 字段；按数值总分差计算为 8/2/4，配对统计采用后一口径。上述均为评分，不是正确率。

GPT-4 的 G2−G1 平均差为 −0.143，配对 bootstrap 95% 区间为 [−0.429, +0.143]；Wilcoxon 双侧 p=0.3173，排除平局后的 exact sign-test 双侧 p=0.6250。样本量为 14 且平局较多，不能据此声称显著改善或显著退化。

## 历史结果与本项目评估

| 比较层 | 前者 | 后者 | 前者−后者 |
|---|---:|---:|---:|
| 原始 notebook EvaluatorGPT：历史 ChemCrow 对历史 GPT-4 | 7.357 | 8.750 | −1.393 |
| 本项目校准 Judge：历史 ChemCrow 对历史 GPT-4 | 7.500 | 9.357 | −1.857 |
| 本项目校准 Judge：G1 对历史 GPT-4 | 9.357 | 7.929 | +1.429 |
| 本项目校准 Judge：G2 对历史 GPT-4 | 9.214 | 7.929 | +1.286 |

所有行均为 0–10 分、N=14。历史 GPT-4 答案在不同配对调用中重新得到评分，不能把这些不同上下文的控制组均分当作一个固定值。

本项目采用 `CHEMCROW_EVALUATORGPT_PROMPT_CALIBRATED_V2`。原论文的精确评审提示词未恢复；此处为历史校准的兼容评估，不是官方原文复现。G1/G2 对历史 ChemCrow，以及 G2 对 G1 的汇总比较，使用同一冻结 Judge 的独立调用后按任务配对，并非把两份答案放入同一个 prompt 的直接对局。原始 notebook 评分、本项目重新评估和项目新增比较应分别解释。完整汇总见 [summary_comparisons.csv](summary_comparisons.csv)。

## 实验范围与完成证据

G1 与产物生成封存于 `full-v8-core-full-worker-g1-artifacts-20260830T121136Z`，G2 从该封存结果继续执行于 `full-v8-core-full-worker-g2-continuation-20260831T052113Z`。两者绑定同一实验 ID，采用 `core_full_worker_v1` 提示词配置及 `chemcrow-three-isolated-core-native-artifacts-v2` 协议。

已有完成审计为 `PASS`：14 题对应 42 个独立 Reflector jobs、42 个唯一产物，`text_memory`、`skill_bundle`、`agent_system` 各 14 个；共有 42 份 sibling-isolation 证据、14 份 G2 Core 注入凭证和 14 份 reset 凭证，mock/fixture 观测为 0。G1 生成阶段自身没有 G2 调用或 G2 注入，G2 完成计数来自后续 continuation 审计。

这些是现存封存审计的汇总。本次整理核对了输入文件哈希及报告间绑定，未重新执行完整实验审计或科学调用。公开汇总没有逐题轨迹、答案、产物正文、Judge 请求或私人账本，读者无法仅凭本目录独立重建完整审计；哈希是对原文件的标识，不能替代原始证据。此说明针对本次结果包，不宣称仓库旧历史从未包含数据。

## GPT-4 Judge 账本与费用

Judge run 为 `paper-evaluator-full-v8-core-full-worker-20260831-r2`，模型为 `openai/gpt-4`，限定 OpenAI provider，未使用 fallback。账本记录 42 个有效结果、44 次 attempt、43 个 provider response；排除一次 schema 无效的付费输出，以及一次没有 provider response 或费用字段的 HTTP 402。

两次 replacement 使用新的 call ID；无旧 call ID 重用、自动 provider retry 或按分数重试。记录费用为 **$2.65887**，仅对应此次 paper-compatible GPT-4 evaluator 账本，包含被排除的付费输出；不代表全实验总成本。内部 aggregate 未提供可用的费用或 token 汇总。

## 文件

- [summary.json](summary.json)：白名单导出的审计计数、内部和 GPT-4 aggregate、配对统计及比较口径。
- [summary_comparisons.csv](summary_comparisons.csv)：原报告的九行汇总比较，保持原始字节内容。
- [provenance.json](provenance.json)：来源提交、三个 run 身份、来源文件相对路径与 SHA256，以及本目录其他三个文件的哈希。

来源路径以原 `chemcrowrun` 工作区为基准，仅用于来源标识，不表示这些原始文件随本结果包发布。重新开展产物分析需要另外取得封存产物与人工注释输入。
