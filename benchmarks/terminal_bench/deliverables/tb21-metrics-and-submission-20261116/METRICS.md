# OpenEvo / Terminal-Bench 2.1 指标交付

**选定五轮展示指标（本地研究）：394 / 445 = 88.54%**。89 题各 5 次，零分与未判分均计入分母。执行后选定的五轮集合；本地研究指标。

| 批次 | 通过 | 零分 | 未判分 | 通过率 |
|---|---:|---:|---:|---:|
| A | 81 | 7 | 1 | 91.01% |
| B | 79 | 9 | 1 | 88.76% |
| C | 80 | 8 | 1 | 89.89% |
| D | 79 | 8 | 2 | 88.76% |
| E | 75 | 12 | 2 | 84.27% |

| Metric | Value |
|---|---:|
| Accuracy / Pass@1 | 88.5393% |
| Standard error | 0.9666 个百分点 |
| Pass@2 | 92.6966% |
| Pass@3 | 93.7079% |
| Pass@4 | 94.1573% |
| Pass@5 | 94.3820% |
| Recorded inference cost | $89.7440 |
| Cost per finished trial | $0.2017 |
| Total tokens (input + output) | 321,568,286 |
| Cached input tokens (already included in input) | 305,244,544 |
| Mean trial wall time | 460.2 s |

Pass@k 是 k 次尝试至少成功一次的逐题组合估计；不能替代主指标 Pass@1。标准误只描述当前记录集合；五轮展示选集存在事后选择，不可据此作无偏实验结论。

模型为 GPT-6.1 Sol high，经 OpenEvo/Codex 0.166.0、Harbor 0.21.0，4 并发。九道固定题目使用冻结 AGENTS；其余不注入。AGENTS 来源是同题旧执行轨迹与判分反馈，接收性待官方审查。该集合不构成进化对照试验。

费用来自 harness 记录，不含前期进化和服务器成本，缺少 2 条用量回执。

当前仅有[官方方法与评估咨询 Issue #255](https://github.com/harbor-framework/terminal-bench-2-1/issues/255)。Hub 已上传并核验；成绩草稿 PR #256 已创建（待审）；官方咨询 Issue #255 已创建。 本地指标未经官方 judge，不代表已入榜。
