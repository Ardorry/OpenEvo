# OpenEvo benchmark 分支总览

整理日期：2026-09-10。每个实验使用独立分支；本页是 fork 首页的导航索引。

| 分支 | 核心内容与结果 | 核验范围 |
| --- | --- | --- |
| [chemcrow](https://github.com/Ardorry/OpenEvo/tree/chemcrow) | [发布说明](https://github.com/Ardorry/OpenEvo/blob/chemcrow/benchmarks/chemcrow/PUBLICATION.md) · [full-v8 汇总](https://github.com/Ardorry/OpenEvo/blob/chemcrow/benchmarks/chemcrow/reports/full_v8_public_20260831/README.md) | 当前本地代码、已封存结果和来源哈希 |
| [chembench](https://github.com/Ardorry/OpenEvo/tree/chembench) | [发布说明](https://github.com/Ardorry/OpenEvo/blob/chembench/benchmarks/chembench/PUBLICATION.md) | 已公开 STV3 历史快照；无法确认等于缺失的 readiness-v4 本地最新版本 |
| CompChemBench（待恢复） | 当前没有可核验、可上传的本地代码和结果 | 工作区仅剩 15 个目录，普通文件为 0；本次未创建占位实验分支 |
| [researchclaw](https://github.com/Ardorry/OpenEvo/tree/researchclaw) | [历史代码与报告导航](https://github.com/Ardorry/OpenEvo/blob/researchclaw/benchmarks/researchclaw/PUBLICATION.md) | Dev17 归档为部分完成状态；归档哈希 5/5 一致，本次未重算成绩 |

## 结果索引

| 实验与口径 | 样本 | 基线 | 演化后 | 差值 |
| --- | ---: | ---: | ---: | ---: |
| ChemCrow full-v8，校准 GPT-4 Judge，满分 10 | 14 | G1 9.357 | G2 9.214 | −0.143 |
| ChemBench 历史 STV3，配对 Test 正确率 | 450 对 | 388/450，86.22% | 382/450，84.89% | −1.33 个百分点 |

两行任务、评分尺度和协议不同，不能横向排名。
ChemCrow 是 `PROVISIONAL_LLM_JUDGED_RESULT`；GPT-4 配对比较来自独立调用，
95% bootstrap CI 跨零，内部 evaluator 的改善没有得到外部校准 Judge 确认。
ChemBench 属于归档的配对描述性恢复比较，结论为
`ENGINEERING_FEASIBLE_EFFECTIVENESS_NOT_DEMONSTRATED`，不是标准 leaderboard 成绩。
详细限制以各分支发布说明和来源报告为准。

## 本地材料缺口

本次检查的三个工作区为 `chemcrowrun`、`chembench-lamalab-fullrun-readiness-v4` 和
`compchem-bench`。后两者分别仅有 30 / 15 个目录、0 / 0 个普通文件，
没有 Git 仓库、代码或结果文件，也没有独立挂载或符号链接可供追踪。

ChemBench 暂以 fork 中已有的 `f7aebe8adade4170537186873258ae308c1a1731` 历史快照
整理入口。CompChemBench 待恢复源码、依赖与许可证，以及具有运行身份和完整性证据的
汇总结果后，再发布到独立 `compchembench` 分支。历史记忆中的分数未用作本次发布证据。

## 归档约定

- 每个实验分支提供代码入口、环境配置、结果摘要及协议限制。
- 新增公开结果使用汇总数据和来源哈希，原始运行凭据、数据库、逐题输出、私有演化产物
  和依赖缓存保留在各自实验存储中。
- 历史报告保留原本的实验口径，不能把选择后的分数当作 raw evolved 成绩。
- 本轮没有重新启动实验、创建官方投稿或改写已有分支历史。
