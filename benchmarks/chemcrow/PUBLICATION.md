# ChemCrow 发布导航

2026-09-10 整理。最新公开结果来自 2026-08-31 完成的 full-v8 实验，覆盖 14 个任务。
本次仅整理既有代码和结果，没有重新调用模型或重跑科学实验。

## 核心内容

每个任务执行 `G1 → 内部反馈 → 三个独立 Reflector → 三类产物 → G2 → 封存与重置`。
三类产物为 `text_memory`、`skill_bundle` 和 `agent_system`，通过 OpenEvo Core
Rollout/Gateway 的受管执行路线产生并注入，同一任务内配对比较，任务之间隔离。
full-v8 使用 Core 的完整 Reflector 提示构造与输出审核。

| 内容 | 入口 |
| --- | --- |
| 最新汇总、统计口径、原件哈希 | [full-v8 结果](reports/full_v8_public_20260831/README.md) |
| 两套实验共 84 份进化产物 | [产物索引与下载](artifacts/README.md) |
| 适配包和命令入口 | [src/openevo_chemcrow](src/openevo_chemcrow/)、[cli.py](src/openevo_chemcrow/cli.py) |
| 三产物协议 | [three_artifact_protocol.py](src/openevo_chemcrow/three_artifact_protocol.py) |
| 封存代际延续与恢复 | [generation_continuation.py](src/openevo_chemcrow/generation_continuation.py)、[paper_recovery.py](src/openevo_chemcrow/paper_recovery.py) |
| 配置与环境定义 | [configs](configs/)、[docker](docker/)、[pyproject.toml](pyproject.toml)、[uv.lock](uv.lock) |
| 离线测试 | [tests](tests/) |
| 产物相似度分析 | [artifact_similarity.py](src/openevo_chemcrow/artifact_similarity.py)、[独立依赖锁](analysis/artifact_similarity/) |
| 结果整理脚本 | [scripts](scripts/) |
| Core 配套实现 | [src/openevo/evolution/methods.py](../../src/openevo/evolution/methods.py) |

## 结果解读

| 评分来源 | G1 | G2 | G2 − G1 | 满分 |
| --- | ---: | ---: | ---: | ---: |
| 内部 evolution evaluator | 9.786 | 9.929 | +0.143 | 12 |
| 内部盲评 | 9.929 | 10.286 | +0.357 | 12 |
| 校准 GPT-4 Judge | 9.357 | 9.214 | −0.143 | 10 |

内部评分改善没有得到校准 GPT-4 Judge 的确认。GPT-4 配对均分差的 bootstrap 95% CI
为 `[-0.429, +0.143]`；样本小且平局多，不能据此声称显著提升或退化。
G1/G2 的 GPT-4 比较来自独立 Judge 调用后按任务配对，不是同一 prompt 中的直接对局。
历史论文评分、校准重评、内部评分分别列示，不合并成一个榜单分数。
费用 `$2.65887` 仅为该轮 GPT-4 paper evaluator，不包含整个实验的全部成本。

## 复现与验证

这是维护者使用的独立 benchmark 自动化包。安装开发环境后可运行离线测试：

```bash
uv sync --project benchmarks/chemcrow --python 3.11 --extra dev
export CHEMCROW_TEST_RUNS_ROOT=/absolute/path/to/chemcrow-runs
export CHEMCROW_TEST_PUBLIC_ROOT=/absolute/path/to/chemcrow-public
uv run --project benchmarks/chemcrow pytest benchmarks/chemcrow/tests -q
```

两个 fixture 根目录分别来自历史 ChemCrow notebooks 仓库和工具源码仓库；前者应包含
`tasks/`，后者应包含 `chemcrow/data/chem_wep_smi.csv`。测试使用本地 fixture 和 mock。
2026-09-10 对来源代码及筛选后的发布快照执行 ChemCrow 测试，均得到 `286 passed, 2 skipped`，随后使用独立
analysis 环境补跑两个缺少 sklearn/scipy 的测试，得到 `2 passed`；Core 相关方法测试
为 `47 passed`。快照验证显式绑定其 import 路径，183 个代码、配置、测试、脚本和
Docker 文件与来源提交逐字节一致。这些验证没有调用真实模型。
复现真实实验另需受管 Docker runtime、独立凭据、工具服务和显式付费授权。
`configs/` 中的绝对路径、端口、镜像、来源哈希是历史环境记录，使用前须配置新的独立
运行目录并完成零模型 preflight；仅克隆分支不能恢复原有运行状态。

已公开 full-v8 与 full-v5 replication 的 84 份真实进化产物，附原始哈希与来源清单。
产物相似度与历史报告脚本还需要对应的完整封存审计和人工注释输入；公开正文与汇总
不能替代这些原件，也不足以独立重建全部结果。

## 发布来源与边界

代码源提交：`845eb3228378e08d02a9233eaa38dc5da3574fef`。
本次以已公开 `chemcrow` 提交 `318f1c522fab44357e6bda4b6823ec3c391cb80c` 为基础，
按明确路径复制当前代码、配置、脚本、测试和所需 Core 实现。原始本地提交链保留在实验工作区。

评分结果包位于 `reports/full_v8_public_20260831/`，包含汇总与来源哈希。
进化产物正文位于 `artifacts/`，按两套独立实验分别归档并逐字节核验。
逐题最终答案、审阅工作表、轨迹、运行数据库、凭据、缓存和第三方依赖目录未新增上传。
旧分支中已经公开的任务、历史报告和校准材料保持原样；它们属于各自历史协议。
历史文档中的准备阶段、legacy/full-v3/full-v4 描述不能作为 full-v8 的当前状态。

其他实验见 [fork 分支总览](https://github.com/Ardorry/OpenEvo/blob/stable/BENCHMARKS.md)。
