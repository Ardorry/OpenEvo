# ResearchClaw 分支导航

本次整理以仓库已有 `researchclaw` 分支为准，基于提交
`3462954e688b75522daee21b92ca7e507fbeab2f`（2026-08-10）。
这里提供现有代码、设计与公开历史记录的入口。

## 代码与布局

| 内容 | 入口 |
| --- | --- |
| 顶层原生适配包说明 | [researchclawbench/README.md](../researchclawbench/README.md) |
| 顶层适配实现 | [openevo_researchclawbench](../researchclawbench/src/openevo_researchclawbench/) |
| 每题独立重置协议 | [per-item-reset-protocol.md](../researchclawbench/docs/per-item-reset-protocol.md) |
| 维护者运行说明 | [runner-cli-demo.md](../researchclawbench/docs/runner-cli-demo.md) |
| 顶层包测试 | [tests](../researchclawbench/tests/) |
| 顶层协议配置 | [configs/researchclawbench](../../configs/researchclawbench/) |
| 调查与适配设计 | [design](design/) |
| 历史及实验配置 | [configs](configs/) |
| 原有嵌套 OpenEvo 树 | [OpenEvo](OpenEvo/) |
| 嵌套树的适配说明 | [历史 README](OpenEvo/benchmarks/researchclawbench/README.md) |

本快照同时保留顶层适配包和 `benchmarks/researchclaw/OpenEvo/` 嵌套树。
阅读当前分支实现时先从顶层包进入；它的 README 描述每题两次 Candidate、
一次当前题监督演化和独立 Judge 的重置协议。嵌套树的 README 仍描述每题
三次尝试、两次演化的旧协议，并记录当时的原生能力缺口。
两份说明对应不同历史状态，命令与配置需要按各自协议配套使用。

本次保留全部原有布局，只添加导航；没有移动、合并或重命名上述目录。

## 已有公开记录

| 内容 | 入口 |
| --- | --- |
| Dev17 归档状态与发布边界 | [归档 README](../../docs/experiments/researchclaw-dev17-202608/README.md) |
| 实验经过 | [EXPERIMENT_SUMMARY.md](../../docs/experiments/researchclaw-dev17-202608/EXPERIMENT_SUMMARY.md) |
| 可机读历史摘要 | [summary.json](../../docs/experiments/researchclaw-dev17-202608/summary.json) |
| 原代码身份 | [CODE_IDENTITY.txt](../../docs/experiments/researchclaw-dev17-202608/CODE_IDENTITY.txt) |
| 证据索引 | [EVIDENCE_INDEX.md](../../docs/experiments/researchclaw-dev17-202608/EVIDENCE_INDEX.md) |
| 归档校验清单 | [SHA256SUMS.txt](../../docs/experiments/researchclaw-dev17-202608/SHA256SUMS.txt) |

Dev17 公开摘要生成于 2026-08-04，绑定历史提交
`d8a3826a802314cc26b14704ecc8b03be12e3980`，发布状态为
`PARTIAL_BLOCKED_WITH_VALID_INTERMEDIATE_EVIDENCE`。
它保存有效中间证据，但没有完整 Dev17 或 Official40 的最终有效成绩。
其中的 `verification=PASS` 是身份与完整性检查结果，不代表整场实验完成。
这份历史记录也不能作为顶层较新每题重置协议的完成结果。

2026-09-10 按本分支基准提交的 Git 文件内容校验，归档 SHA-256 清单
5/5 项一致。本次核验的是已发布文件完整性，没有重新执行实验、复核私有账本
或生成新的成绩。原归档、代码和配置均保持原样。

公开材料保留汇总与证据索引；原始模型输出、私有评测数据、完整工作空间、
运行数据库、凭据与运行时副本不纳入本次整理。

[返回 Fork 实验分支总览](https://github.com/Ardorry/OpenEvo/blob/stable/BENCHMARKS.md)
