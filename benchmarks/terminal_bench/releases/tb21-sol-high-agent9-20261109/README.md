# OpenEvo Agent9 · Terminal-Bench 2.1

GPT-6.1 Sol high，基于 Codex 0.166.0 的 OpenEvo adapter。此仓库保存可审计的固定 AGENTS 注入实现、复现配置和**选定五轮结果集**。

**本地研究结果：394 / 445 = 88.54%。** 89 道题各五次；44 次零分、7 次未判分均保留在分母中。

| 批次 | 通过 | 零分 | 未判分 |
|---|---:|---:|---:|
| A | 81 | 7 | 1 |
| B | 79 | 9 | 1 |
| C | 80 | 8 | 1 |
| D | 79 | 8 | 2 |
| E | 75 | 12 | 2 |

这五次是执行完成后确定的展示集合，非一次新启动或独立预注册的五轮实验。批次标签不改变真实 trial ID；结果表见 `data/`。本仓库不声称该数值已获得官方接收或榜单排名。

## 实现

- 九道固定题目只注入一份 AGENTS.md；Doom 和其余题目不注入进化产物。
- 原始任务内容、原始 verifier、默认时限与资源设置；并发 4。
- 同题旧轨迹与判分反馈生成的 AGENTS 在评估前冻结。详细来源见 [方法说明](docs/METHOD.md)。这份结果本身不构成无产物基线对照。
- 固定官方源码镜像经逐题 digest 验证后作为任务传输缓存，原生 Harbor Job/SingleStepTrial 执行 agent 和 verifier。
- 无自动补跑、无 best-of 替换。完整依赖缓存修复未应用。

`src/openevo_terminal_bench/` 中的六个 adapter 模块与实际执行源码逐字节相同，校验表见 `docs/executed-source-files.json`。`scripts/prepare_run.py` 是为此交付包新建的可移植启动入口，尚未用于上表的执行。

## 使用

需要 Linux x86_64、Python 3.12+、Docker/Compose，以及自行提供的 Codex 访问凭据。凭据文件必须留在仓库外。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-observed.txt
.venv/bin/pip install --no-deps -e .
git clone https://github.com/harbor-framework/terminal-bench-2-1.git /path/to/tb21
git -C /path/to/tb21 checkout dde3cd95b80ff25af5abd99a80b6513a018ad3b4

# 默认只生成/核对新运行配置，不调用模型
.venv/bin/python scripts/prepare_run.py \
  --dataset-path /path/to/tb21/tasks \
  --assets-dir /path/to/private/runtime-assets \
  --jobs-dir /path/to/private/jobs
```

只有显式增加 `--execute --auth-path /path/outside/repo/auth.json` 才会启动新的付费评估。不会继续或覆盖已有运行。

工具链、CA 和 uv 文件的精确 SHA256 在 `docs/runtime-assets.json`。大体积二进制和本地网络 CA 不在代码仓库中；本地交付包另有对应资产。新运行有模型采样和外部环境变动，不能承诺复现完全相同的逐题分数。

## 发布范围

本 repo 不含原始 session、认证文件、完整题库、隐藏测试或参考解答。原始证据和提交草稿在本地交付包的仓库目录之外。官方提交还涉及同题产物来源、结果集选择和维护者接收性；见 [提交说明](docs/SUBMISSION.md)。

2026-11-09 核对的官方入口、命令与待确认事项见 [官方提交流程](docs/OFFICIAL_SUBMISSION_FLOW.md)。GitHub 上传本身不构成官方提交。

```bash
.venv/bin/python -m pytest -q
```

许可与来源见 `LICENSE`、`NOTICE.md`。本文的分数、用量和费用均为选定记录的本地统计，费用估算不等于账单。
