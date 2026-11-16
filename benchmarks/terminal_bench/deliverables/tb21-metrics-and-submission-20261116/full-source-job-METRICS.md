# OpenEvo / Terminal-Bench 2.1 指标交付

**完整来源记录指标（提交来源口径）：541 / 623 = 86.84%**。89 题各 7 次，零分与未判分均计入分母。两个完整来源 job，全部七次重复；提交来源口径，尚未获官方接收。

| 批次 | 通过 | 零分 | 未判分 | 通过率 |
|---|---:|---:|---:|---:|
| 来源 job a（445 条） | 387 | 44 | 14 | 86.97% |
| 来源 job b（178 条） | 154 | 20 | 4 | 86.52% |

| Metric | Value |
|---|---:|
| Accuracy / Pass@1 | 86.8379% |
| Standard error | 0.9496 个百分点 |
| Pass@2 | 92.4559% |
| Pass@3 | 93.8363% |
| Pass@4 | 94.2857% |
| Pass@5 | 94.3820% |
| Recorded inference cost | $128.1577 |
| Cost per finished trial | $0.2057 |
| Total tokens (input + output) | 461,013,808 |
| Cached input tokens (already included in input) | 437,493,376 |
| Mean trial wall time | 531.6 s |

Pass@k 是 k 次尝试至少成功一次的逐题组合估计；不能替代主指标 Pass@1。标准误只描述当前记录集合；五轮展示选集存在事后选择，不可据此作无偏实验结论。

模型为 GPT-6.1 Sol high，经 OpenEvo/Codex 0.166.0、Harbor 0.21.0，4 并发。九道固定题目使用冻结 AGENTS；其余不注入。AGENTS 来源是同题旧执行轨迹与判分反馈，接收性待官方审查。该集合不构成进化对照试验。

费用来自 harness 记录，不含前期进化和服务器成本，缺少 3 条用量回执。

当前仅有[官方方法与评估咨询 Issue #255](https://github.com/harbor-framework/terminal-bench-2-1/issues/255)。Hub 已上传并核验；成绩草稿 PR #256 已创建（待审）；官方咨询 Issue #255 已创建。 本地指标未经官方 judge，不代表已入榜。
