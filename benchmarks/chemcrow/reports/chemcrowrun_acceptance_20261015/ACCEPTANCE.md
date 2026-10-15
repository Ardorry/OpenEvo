# ChemCrowRun v1.0 工程验收记录（2026-10-15）

本记录区分已观察到的真实结果、离线验证和待执行步骤。完整实验正在进行，当前文件为工作快照；最终状态以本目录的验收状态和封存报告为准。ChemCrowRun 是 OpenEvo 中对 ChemCrow benchmark 的复现与评价实现，不代表原作者或官方排行榜认证。

快照时间：2026-10-15 08:37 UTC。根据用户要求，实验自主逐题运行，不轮询。08:30 UTC 后检查发现前 7 题已封存，第 8 题内部评价的凭据隔离 canary 校验失败并安全停止。既有 no-effect 恢复证明通过后，已显式替换该评价并续跑。下一次检查为 10:00 UTC（北京时间 18:00）；仅在 14 题完成、审计和 submission 均通过时，才启动一次预算 USD 12 的 GPT-4 校准。

## A. 工程完成情况

候选分支为 `codex/chemcrowrun-v1`，从 `codex/chemcrow-reproducibility` 的 `7375f5ec4` 安全分支开发。原有三个规范化提交 `bda442414`、`405f7b91e`、`7375f5ec4` 均保留。完整实验锁定源码 `1bf37ce0b`；实验期间其独立 clone 不变更 HEAD 或已跟踪文件。

| 功能 | 实现与主要验证 |
| --- | --- |
| 正式名称、兼容映射 | 根 README、REPRODUCE.md、`reproduction/legacy-aliases.json`、`chemcrowrun.reproduction.yaml`；旧 import、CLI、full-v8 identity 和 payload 保留 |
| 独立运行入口 | `src/openevo_chemcrow/chemcrowrun.py`：init/up/status/logs/down/runtime/experiment/report/judge/compare；`tests/test_chemcrowrun.py` |
| 原始 OCI 获取 | `runtime_assets.py`：HTTPS 下载、大小/哈希/OCI 身份校验、原子缓存、Docker 导入和启动；本地归档与公开匿名下载分别记录 |
| 服务与数据隔离 | 独立端口、PID start ticks、会话目录、状态库、claim ledger、Compose project；Core 的可选 session_base_dir 保留原默认行为 |
| RXN 环境固定 | `deployment/` 中 Dockerfile、Compose 和约束文件；worker 97 项、MCP 58 项依赖实际安装结果与约束一致 |
| 完整结果与评价 | `workflow.py`：封存审计后导出、GPT-4 预算/实时价格检查、真实 token/cost 汇总 |
| 模型替代真实性 | `submission.py`、`cli.py`：六种角色一致替代、显式 variant、拒绝混合不相容模型条件；`tests/test_submission.py` |
| 配对统计与历史对照 | `comparison.py`：14 题配对、10,000 次 bootstrap、精确双侧 sign-flip；先验证新 submission，再读取不可变历史分数 |
| ledger 修复 | `three_artifact_audit.py` 使用配置声明的 ledger；保留历史默认目录；单题结果离线重审，无模型重放 |

独立 clone 在完整实验源码上测试：**313 passed、2 skipped、2 warnings，33.49 秒**。跳过项为未安装可选 scipy/sklearn 的 artifact similarity 测试。另有 Core 配置、Gateway 和会话隔离相关测试 33 项通过。Benchmark lint 和差异格式检查通过；没有因此扩大改动到全仓库格式化。

本目录保存 [测试日志](benchmark-tests.txt)、[单题封存报告](smoke-report.json)、[付费 probe 摘要](paid-probe-summary.json)、[fixture 校验](fixture-verification.json)、[runtime manifest](runtime-manifest.json)、[发布资产清单](release-assets.json)、[扫描摘要](runtime-layer-scan-summary.json) 和 [源代码/文档检查](source-and-document-checks.json)。它们没有包含凭据、私有网络配置、实验答案或 transcript。根 README 现集中到 ChemCrowRun；原有 OpenEvo 产品说明逐字保留于根目录 OPENEVO.md。

## B. Docker 环境

| 项目 | 身份 / 结果 |
| --- | --- |
| 原始归档 | `openevo-science-runtime-0.1.1-linux-amd64.tar.gz`，352236726 bytes |
| SHA-256 | `ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149` |
| OCI index | `sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b` |
| Config ID | `sha256:0e5783e7839fe06d2df14d7a431c90f0982ca2099ef33bfa4c9e5933149bf5f2` |
| 平台 | Linux amd64；Debian 12、Python 3.12.13、Codex 0.144.1、Node 22.23.1 |
| 本地获取验证 | 原始归档、历史锁、OCI identity、Docker 导入、无网络只读启动：PASS |
| 公开下载 | PENDING：尚未发布资产，不能把本地缓存验证称为公开下载成功 |
| 再分发审查 | REVIEW_REQUIRED：已收集大量对应源码和 notices，仍不能确认原生二进制的全部依赖归属与 notice 覆盖 |

对全部 10 个 OCI layer、17,480 个成员进行了有边界的扫描，包括后续删除的文件；未命中规定的敏感路径或高置信度密钥模式。文本大小限制为 2 MiB，不构成对所有秘密的绝对排除。352 项是文件名命中的 license/notice 候选，不是 352 份已审定许可。

已准备 180 项 Debian binary/source inventory、104/104 精确 Debian 源码版本、对应源码归档、镜像内 Debian 与非 Debian notices，以及匹配 Codex 0.144.1 tag 的 LICENSE/NOTICE 和源码。另取得 Cargo.lock 中 1193 个校验和匹配的 registry 源码包、7 个精确 Git 依赖源码仓库。该 workspace lock 包含多平台和开发依赖，是过量收集，并非该 Linux 二进制的精确链接 SBOM。未达到再分发审核条件的镜像不会列入可直接批准发布的资产。

上游依据：[Codex LICENSE](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/LICENSE)、[Codex NOTICE](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/NOTICE)、[Debian licenses](https://www.debian.org/legal/licenses/)。第三方 fixture 直接从其公开源取得；未将缺乏已发现再分发许可的原始 notebook 仓库打包发布。

## C. 服务部署与实际执行路径

| 服务 | 完整实验端口 / 网络 | 实际验证 |
| --- | --- | --- |
| Core Rollout | 19480 | 健康响应、真实派发和 completion |
| Core Gateway | 19481 | 当前 node 健康、managed runtime 调用 |
| Evolution | 19482 | 数据库与本次 artifact root 检查、真实 artifact 提交与注入 |
| RXN/MCP | 19483，loopback | OpenAPI 路由、真实 ReactionPredict 与 ReactionRetrosynthesis 返回 |
| Celery worker | Compose 内部网络 | live ping、约束版本校验 |
| Redis / RabbitMQ | Compose 内部网络 | healthcheck，不向宿主机开放服务端口 |
| Managed runtime | 每个会话独立容器 | 原始 OCI identity、凭据隔离 receipt、轨迹捕获 |
| GPT-4 Judge 服务 | 8110 / 8180 / 8400 | 由 judge 阶段单独启动，禁止并存冲突；完成状态见 D |

宿主机负责 Python 调度服务；**Codex 在 Core 创建的 managed runtime 容器中运行**。当前完整实验的容器 `/proc` 实测可执行文件为：

```text
/opt/codex/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
```

容器仅挂载本次会话目录、隔离凭据目录与只读 auth.json。没有挂载宿主机 Codex 程序、历史实验数据库、历史答案或历史 trajectory。终态记录声明 `execution_route=openevo_core_rollout_gateway_managed_codex_v1` 和 `host_codex_exec_forbidden=true`；实测进程证据与声明相符。原始宿主机订阅认证只是已授权的凭据来源。

`up` 按依赖启动，`status` 检查就绪；`logs` 收集私有 Core/Compose/Celery 日志与镜像身份；`down` 检查进程所有权并停止本项目资源，保留数据。WSL2 + Docker Desktop 的 Linux amd64 路径已测试，其他平台未宣称通过。RXN 是新建且固定依赖的环境，不能冒称其字节身份等同历史 RXN 镜像。

## D. 真实模型链路

| 阶段 | 真实结果 |
| --- | --- |
| 旧 OpenRouter 失败探针 | 绕过代理的直连路径，HTTP 403，未返回 token/cost；没有把它判成用户美国节点故障 |
| 正确路径付费 probe | 2026-10-15，显式美国代理 → OpenRouter → OpenAI-only GPT-4；HTTP 200，39 input + 5 output tokens，USD 0.00147，无 fallback/retry |
| 历史订阅模型检查 | 当前 catalog 不含 gpt-5.5；原模型 managed readiness 失败，未伪造为成功 |
| 替代模型 managed probe | gpt-5.6-sol，原始容器与凭据隔离不变，真实 Core completion 成功 |
| Stage A | 服务健康、真实工具调用、Candidate/Reflector managed 路由 PASS；probe 不等于完整 Judge harness 验收 |
| Stage B | 独立真实 task01，8 次逻辑调用，三路 Reflector、G2 注入、最终盲评全部完成，审计 PASS |
| Stage C | `reproduction-v1-sol-20261015`，前 7 题封存；task08 评价前 canary 失败，经原协议显式恢复后续跑，尚未完成 14 题 |
| Stage D | PENDING，完整实验审计通过后才能执行 42 次 GPT-4 校准及最终导出 |

单题 smoke：2026-10-15 03:40:02–03:54:28 UTC，865.75 秒；8/8 Core terminal COMPLETED、0 failed、0 自动 retry/replacement。生成源码 `95df3151b0a483694de65f83f9113f072e85ef62`；发现 ledger 路径审计缺陷后，用 `6791d6bbed553520b759e71fb13e50778d79551e` 离线重审。对所有既有 run/claim/completion JSON 做前后哈希验证，科学证据未改动，增加模型调用数为 0；保留初次失败报告。

完整实验首次运行于 04:07:09–05:03:55 UTC，3406.25 秒后停止；当时共有 57 条 COMPLETED 和 1 条 ERROR 终态记录。task08 的 G1 已成功，失败的内部评价没有轨迹记录、没有 trace、没有 workspace result。原有 `recover-baseline-evaluator-no-effect` 命令的全部谓词通过，生成 [恢复凭证](task08-evaluator-recovery.json)，保留原失败 claim 和 completion，前 7 题及既有 completion 的哈希不变。此次离线恢复调用模型 0 次；随后显式安排一次评价替换，而非重跑 Candidate 或放宽隔离校验。基础设施 canary 可能已消耗订阅调用；不能把本次失败说成完全无费用。凭据隔离 canary 未通过是可证实的失败类别，缺乏保留的 canary 细节，不能进一步断言是余额、地区或某个具体校验谓词导致。

新实验六种角色全部显式使用 gpt-5.6-sol，variant 为 `chemcrowrun_v1_model_substitution_v1`。task 顺序、三个独立 Reflector、artifact 注入、超时、零自动重试和评分协议保持不变。订阅服务无法保证服务端权重快照固定，不能宣称与历史 gpt-5.5 完全同条件。

完整运行计划 112 次逻辑订阅调用，隔离 canary 和 agent 内部 turns 另计。订阅 transcript 不提供完整 provider token 总数或逐次美元账单，未知值明确为 null。GPT-4 校准独立计费：42 次冻结 list-price 上界 USD 11.83266，预算 USD 12，执行前检查当前价格、上下文上限及余额；余额不足或限额失败时停止并保留状态，不自动重复昂贵实验。

## E. 已产生的成绩与比较限制

| 记录 | 内部 G1 /12 | 内部 G2 /12 | GPT-4 G1 /10 | GPT-4 G2 /10 |
| --- | ---: | ---: | ---: | ---: |
| 历史权威 full-v8（14 题） | 9.9286 | 10.2857 | 9.3571 | 9.2143 |
| 新独立 smoke（仅 task01） | 11 | 10 | 未执行 | 未执行 |
| 新完整复现 | 运行中 | 运行中 | 待执行 | 待执行 |

单题差值为 -1，n=1，不用于推断完整任务集改进。历史 full-v8 没有证明 G2 显著提升。新完整结果将给出每题配对差值、bootstrap 95% CI、精确双侧 sign-flip p 值、异常与失败、时间和可观测费用；不会预设应优于历史。跨运行差异受模型和工具环境变化影响，仅作描述及未校正的探索性比较，不解释为演化方法的因果效应。

## F. Fresh Clone 验收

独立目录使用 `git clone --no-local` 从候选本地 Git 仓库建立，不复制 `.venv`、运行数据库、答案或缓存。安装依赖、公开 fixture 下载/锁校验、原始 runtime 导入、服务构建、健康检查、真实 smoke 均实际完成。完整实验在同一独立 clone 的全新 run ID/状态目录进行。允许复用的是经验证的公开 fixture 和合法外部凭据；fixture 不挂载给 Candidate/Reflector。

公开用户流程见 [REPRODUCE.md](../../REPRODUCE.md)。本次本地候选验证的等价命令如下；第一行的本地 clone 明确不冒充 GitHub fresh clone：

```bash
git clone --no-local --branch codex/chemcrowrun-v1 /path/to/candidate ChemCrowRun
cd ChemCrowRun
git checkout 1bf37ce0b
uv sync --frozen --project benchmarks/chemcrow --python 3.11.15 --extra dev
uv run --frozen --project benchmarks/chemcrow chemcrowrun reproduction-fixtures \
  --lock benchmarks/chemcrow/reproduction/fixtures.lock.json \
  --destination-root "$PWD/.reproduction-fixtures" --no-model-calls
uv run --frozen --project benchmarks/chemcrow chemcrowrun runtime \
  --archive /path/to/original/openevo-science-runtime-0.1.1-linux-amd64.tar.gz \
  --output .reproduction-cache/runtime-verification.json
uv run --frozen --project benchmarks/chemcrow chemcrowrun init \
  --run-id reproduction-v1-sol-20261015 --port-base 19480 \
  --fixture-root "$PWD/.reproduction-fixtures" --model gpt-5.6-sol \
  --substitution-reason "Historical gpt-5.5 absent from current subscription catalog; unchanged Core isolation passes with gpt-5.6-sol"
uv run --frozen --project benchmarks/chemcrow chemcrowrun up \
  --workspace outputs/chemcrowrun/reproduction-v1-sol-20261015
uv run --frozen --project benchmarks/chemcrow chemcrowrun status \
  --workspace outputs/chemcrowrun/reproduction-v1-sol-20261015
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --frozen --project benchmarks/chemcrow chemcrowrun experiment \
  --workspace outputs/chemcrowrun/reproduction-v1-sol-20261015 --allow-paid
```

报告、Judge、submission 的命令已经实现和测试，但其本次完整真实执行状态见 D；未执行时不列为通过。发布后还必须从真实 GitHub commit clone，并匿名下载公开 runtime 后复验；两项当前均 PENDING。

## G. L1 / L2 / L3

| 层级 | 状态与边界 |
| --- | --- |
| L1 公开历史证据 | PASS：84 个 artifact、226632 bytes 的公开验证；原 manifest/payload 哈希不变 |
| L2 离线/环境 | PASS（本地候选）：独立安装、fixture 锁、原始 OCI、313 项测试和 zero-model preflight |
| 真实单题 | PASS：新生成、完整三路反思、注入、盲评与封存审计 |
| L3 schema 基础设施 | PASS：导出/验证/模型条件/统计测试，不等价于完整实验通过 |
| L3 新 14 题结果 | RUNNING：仅封存完成且审计通过才认定通过 |
| GPT-4 完整校准 | PENDING |
| 公开 runtime 与远端 fresh clone | PENDING：资产发布与后续实测尚未发生 |

## H. Git 与发布准备

当前没有 push、正式 PR、Release、公开镜像或排行榜提交。原 chemcrow 分支和原历史数据保留。发布前将再次读取远端分支，审阅候选 diff、安全扫描与本地提交记录，并集中请求一次明确确认。

可审阅内容包括源代码、配置/环境模板、部署脚本、schema、FORMAT_ONLY 示例、历史映射、说明和本次已完成的脱敏验收证据。实验未完成时不会生成冒充成功的 submission。镜像和对应源码/notice companions 在本地外部资产 staging 中，尚未发布；仍需完成依赖归属审查的资产不得被称为已经具备发布许可。
