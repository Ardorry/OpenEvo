# ChemCrowRun v1.0 工程验收记录（2026-10-15）

**真实 14 题实验、内部审计、42 次 GPT-4 校准和标准 submission 均已完成。公开 runtime 分发与真实 GitHub fresh clone 尚未通过。** 本记录对应新运行 `reproduction-v1-sol-20261015`，不是旧 full-v8 成绩的重新包装。ChemCrowRun 是 OpenEvo 中对 ChemCrow benchmark 的独立复现与评价实现，不代表原作者或官方排行榜认证。验收更新于 2026-10-15 09:56 UTC。

## A. 工程完成情况

工作分支 `codex/chemcrowrun-v1` 从 `codex/chemcrow-reproducibility` 的 `7375f5ec4` 建立。原三个规范化提交 `bda442414`、`405f7b91e`、`7375f5ec4` 保留。完整生成源码为 `1bf37ce0b386be4abccbcaed8daf5a5f34d1f54b`；其独立 clone 在生成、Judge、导出完成后仍保持干净。最终离线分析/导出代码为 `6e03aef26bc9e9c6e89f18c04df7282f3dca7edd`，文档/证据提交随后追加。

| 功能 | 实现与对应验证 |
| --- | --- |
| 正式命名与历史兼容 | 根 README、REPRODUCE.md、`reproduction/legacy-aliases.json`、`configs/chemcrowrun.reproduction.yaml`；旧路径、identity 和 payload 保留 |
| 完整实验入口 | `src/openevo_chemcrow/chemcrowrun.py`：init/up/status/logs/down/runtime/experiment/report/judge/compare；`tests/test_chemcrowrun.py` |
| 原始 OCI 获取 | `runtime_assets.py`：下载、大小/SHA/OCI/平台校验、原子缓存、Docker 导入与启动；本地与匿名下载分开记录 |
| 服务/状态隔离 | 独立端口、PID start ticks、session/state/cache/claim root、Compose project；Core 可选 session_base_dir 保留默认行为 |
| 化学服务部署 | `deployment/` 中 Dockerfile/Compose/约束；worker 97 项、MCP 58 项依赖实际版本核验 |
| 审计与真实评价 | `workflow.py`：封存审计、内部结果、42 次 GPT-4、当前价格/余额/预算保护与账单 |
| 模型替代 | `submission.py`、`cli.py`：六角色统一模型、显式 variant，拒绝混合条件；`tests/test_submission.py` |
| 配对统计/历史对照 | `comparison.py`：14 题、10,000 次 bootstrap、精确双侧 sign-flip，先验证新 submission 后读历史分数 |
| 真实用量 | `usage.py`：只读解析 turn.completed、去重重复 transcript 附件、逐字段计数、源文件/分析模块哈希；`tests/test_usage.py` |
| 恢复记录导出 | `submission.py`：验证 terminal ledger 与审计恢复次数一致、输出失败/替换调用及恢复凭证哈希；`tests/test_submission_replacements.py` |
| 实际发现并修复的问题 | 配置 ledger 目录、Judge 审计文件名连字符/下划线不一致、submission 遗漏 replacements；均未改评分规则 |

生成源码独立 clone：**313 passed、2 skipped、2 warnings**（33.49 秒）；最终代码在另一个干净 clone：**328 passed、2 skipped、2 warnings**（15.46 秒）。跳过项为可选 scipy/sklearn 的 artifact similarity 测试；两项 warning 保留在日志中。Core 配置/Gateway/session 隔离相关 33 项测试通过。Benchmark lint、历史哈希及差异格式检查通过。

证据：[生成版本测试](benchmark-tests.txt)、[最终候选测试](candidate-tests.txt)、[最终报告](chemcrowrun-report.json)、[标准 submission](chemcrowrun-submission.json)、[导出来源与修正范围](export-provenance.json)、[脱敏检查](source-and-document-checks.json)。原 OpenEvo 产品说明保留在根 OPENEVO.md；本工程没有修改 Desktop 产品契约。

## B. Docker 环境

| 项目 | 身份 / 结果 |
| --- | --- |
| 归档 | `openevo-science-runtime-0.1.1-linux-amd64.tar.gz`，352236726 bytes |
| SHA-256 | `ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149` |
| OCI index | `sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b` |
| Config | `sha256:0e5783e7839fe06d2df14d7a431c90f0982ca2099ef33bfa4c9e5933149bf5f2` |
| 平台/环境 | Linux amd64；Debian 12、Python 3.12.13、Codex 0.144.1、Node 22.23.1 |
| 本地验证 | 历史锁、归档、OCI identity、Docker import、无网络只读启动：PASS；真实实验使用相同原始镜像 |
| 匿名下载 | PENDING：两个已知 GitHub release tag 的匿名查询均返回 404；不能据此证明上游从未发布，也不能声称匿名下载成功 |
| 再分发审查 | EVIDENCE_SUPPORTS_REDISTRIBUTION_WITH_COMPANIONS：实际 Bazel/静态库/ELF 证据及适用的重链接验证通过；尚未获发布批准 |

[全部 layer 扫描](runtime-layer-scan-summary.json)覆盖 10 层、17,480 个成员（含后续删除的文件）；[OCI 元数据扫描](runtime-metadata-scan.json)核验 config/hash、环境变量名称及 19 条 history 元数据。规定范围内未命中敏感文件或高置信度凭据模式。文本扫描有 2 MiB 上限，不是对任意秘密的绝对排除。352 项是文件名匹配的 license/notice 候选，不是 352 份审定许可。

本地 staging 已准备候选资产与 SHA256SUMS（数量及哈希以更新后的清单为准）：[资产清单](release-assets.json)。包括原始归档、180 项 Debian binary/source inventory、104/104 精确 Debian 源码包、原镜像 notices、Codex 0.144.1 对应 LICENSE/NOTICE/源码、1193 个匹配 Cargo.lock 校验和的 registry 源码包和 7 个精确 Git 依赖。最终校验清单检查通过。

本轮补充审查已核验 104 个 Debian 源码版本的 223 个源文件、Codex npm 发布包与镜像中的五个原生可执行文件及 layout manifest、pip 的 431 个源文件；这些比对全部通过。bubblewrap 使用所提供源码修改版本标识后重新链接成功；这不是历史 musl 二进制逐字节重建，也不是 V8 重链接验证。

43 个原始文件名候选已逐项记录：14 个取得精确源码树许可文本，2 个含嵌入式授权文本，27 个保留声明、源码及其实际版权/许可头；文件名缺失不自动等于违规。补齐了 V8 的 23 份精确源码树许可和 22 份子模块根许可、pip 内嵌依赖许可、两版 Rust 编译器版权清单。Cargo.lock 仍不是精确 Linux 链接 SBOM。

后续审查纠正了 GN 默认值无法代表实际 Bazel 发布构建的问题：21 个 fdlibm 数学函数在原始 Codex 和 code-mode-host 中分别获得唯一机器码匹配，实际发布静态库及 Bazel 源码表未包含 glibc 数学路径。fdlibm 为 SunPro 许可，条件编译的 glibc 文件为 LGPL-2.1-or-later，不能混为一谈。整个 10,822,192-byte ICU 数据块与两个原始程序、锁定 crate 及实际 Bazel ICU 版本完全匹配，原始许可已附齐。

**V8-LGPL-RELINK 现已关闭**。从空构建目录重链接修改版 bubblewrap，并由原始 Codex 通过正常 PATH 选择它执行沙箱命令，strace 确认成功；原镜像断网只读 smoke 验证了 Codex、code-mode-host/V8/ICU 和修改版 bwrap。没有执行完整 V8/Codex 重建，也不再对被排除的数学路径虚设应用重链接要求。原有 53 MB 许可补充包保持不变，新材料独立附加。见[实际链接证据](native-link-verification.json)、[干净重链接与替换](bubblewrap-clean-relink.json)、[原镜像 smoke](original-image-native-smoke.json)及[独立验收](native-independent-acceptance.json)。

附带 zsh 仍因 GLIBC 2.38 要求不能在镜像 glibc 2.36 上启动，明确保留此可选组件限制。再分发已有技术和许可材料支持，但不代表获得律师法律意见；仍需维护完整源码/notices 同发并取得用户正式发布批准。匿名公开下载与真实远端 clone 验收仍 PENDING。历史审查可在 a2e8c4021 查阅，历史实验 payload 未改动。

上游依据：[Codex LICENSE](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/LICENSE)、[Codex NOTICE](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/NOTICE)、[Debian licenses](https://www.debian.org/legal/licenses/)。Fixture 从公开上游获取；未重新分发未发现许可的原始 notebook 仓库或冒称近似镜像为历史镜像。

## C. 服务部署与执行路径

| 服务 | 本次端口 / 网络 | 实测 |
| --- | --- | --- |
| Core Rollout | 19480 | 健康、真实派发、114 条终态记录 |
| Core Gateway | 19481 | 节点健康、原始 managed runtime、凭据隔离 |
| Evolution | 19482 | 本次数据库/artifact root、42 个独立 artifact 提交与注入 |
| RXN/MCP | 19483 loopback | OpenAPI、真实产品/逆合成预测 |
| Celery worker | Compose 内网 | live ping、固定依赖版本 |
| Redis / RabbitMQ | Compose 内网 | healthcheck、无宿主机外部暴露端口 |
| Managed runtime | 独立会话容器 | 原始 OCI、session/credential 隔离、轨迹捕获 |
| GPT-4 Judge | 8110 / 8180 / 8400 | 42 个 Core managed Python harness 调用，经显式代理访问 OpenRouter/OpenAI |

宿主机运行 Python 调度服务；**Codex 在 Core 管理的容器中运行，没有直接调用宿主机 Codex**。容器 `/proc` 实测二进制路径为：

```text
/opt/codex/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
```

容器挂载仅为本次 session、隔离凭据目录与只读 auth.json；没有挂载宿主机 Codex 或历史实验数据库/答案。Core 记录的 `openevo_core_rollout_gateway_managed_codex_v1` 路由及 `host_codex_exec_forbidden=true` 与进程证据一致。外部订阅认证仅作为合法凭据来源。

WSL2/Docker Desktop/Linux amd64 已实测。RXN 为依赖固定的新构建，不声称字节等同旧 RXN 环境。`up/status/logs/down` 覆盖依赖启动、诊断、日志与所有权校验后的清理；实验和 Judge 均已停止，七个服务端口释放，日志与数据保留：[清理记录](service-cleanup.json)。

工具能力限制：本环境没有 `SERP_API_KEY`，WebSearch 不可用；因此不能称为全部第三方工具都可用。真实工具错误未被隐藏或用 mock 替换，详见 E。

## D. 真实模型链路、时间与费用

| 阶段 | 实际结果 |
| --- | --- |
| 原错误路径 probe | 绕过代理直连得到 403；HKG 是服务路径中的边缘位置，不能据此断定用户的美国节点无效 |
| 正确付费 probe | 显式美国代理 → OpenRouter → OpenAI-only GPT-4，HTTP 200；39 input + 5 output，USD 0.00147，无 retry/fallback |
| 模型身份 | 原 gpt-5.5 不在当前订阅 catalog；gpt-5.6-sol 通过原始容器和不变的凭据隔离检查 |
| Stage A | Core/Evolution/RXN/模型路由实测通过；WebSearch 凭据缺失单独记录 |
| Stage B | 独立 task01，8/8 调用成功，三路 Reflector、注入、盲评与审计 PASS |
| Stage C | 新 14 题全部完成：112 COMPLETED + 2 ERROR，42 个 artifact，零自动重试、两次显式验证恢复 |
| Stage D | 42/42 GPT-4 有效封存、42 OpenAI provider、42 禁止 fallback，零无效/排除/替换评价；完整报告 CALIBRATED_EXPERIMENT_PASS |

单题 smoke：03:40:02–03:54:28 UTC，865.75 秒。生成源码 `95df3151b0a483694de65f83f9113f072e85ef62`；使用 `6791d6bbed553520b759e71fb13e50778d79551e` 修复 ledger 路径后离线重审，原科学数据/claim/completion 哈希保持，未重复调用。见 [smoke 报告](smoke-report.json)。

完整生成：04:07:09–09:40:58 UTC，三个执行区间合计 **7210.03 秒（约 2 小时）**；墙钟 **20029.63 秒（约 5 小时 34 分钟）**，其中 **12819.59 秒**为停机等待与恢复间隔。任务会自动推进；早期按用户当时要求暂停轮询，后续按最新要求恢复轮询。

两次停止分别发生于 task08 baseline_internal_evaluator 和 task13 baseline_candidate 的凭据隔离 canary。失败记录为零 trajectory records/traces、无 workspace result；原有 no-effect 恢复全部谓词通过后各显式替换一次，未放宽隔离规则。前 7/11 个封存 pair 和已有 completion 哈希保持，task08 已成功 G1 从本次记录恢复而非重新生成。见 [task08](task08-evaluator-recovery.json)、[task13](task13-candidate-recovery.json)。canary 可能消耗订阅用量；不能把失败称为零费用，也不能在缺乏细节时推断余额/地区原因。

Judge 初次 preflight 因审计文件名拼写不一致停止，model_calls=0、paid_operations=0。保留原文件并建立相同哈希的 `completed_run.audit.json` 副本，原函数仅替换该文件名的一次性启动脚本继续执行；原生成 checkout 不变，所有价格/预算/余额保护保留。见 [修正证据](judge-canonical-audit-repair.json)。未来 CLI 已修复并通过回归测试。

GPT-4 校准于 **09:44:44–09:52:48 UTC**完成，约 **483.53 秒**。42 次记录 **68,622 prompt tokens + 15,599 completion tokens**，费用 **USD 2.99460**；加正确路径 probe 共 **USD 2.99607**，不含无法归因的订阅费用。42-call 冻结 list-price 最大费用为 11.83266 美元，执行预算 12 美元，实际价格/余额检查均通过。[账单摘要](judge-receipts-summary.json)只公开白名单字段和来源哈希，去除私人代理/认证信息。

本次 112 个成功订阅终态均有 turn.completed usage：[只读用量汇总](recorded-codex-usage.json)。input=5,960,169，cached_input=4,537,728，output=246,044，reasoning_output=122,061；各字段按原值独立求和，不能再把重叠字段相加。未计入基础设施 canary，provider account total 与 subscription dollar cost 均为 null。

六种生成/反思/内部评价角色统一使用 gpt-5.6-sol，明确声明 `chemcrowrun_v1_model_substitution_v1`。14 题顺序、三路独立性、artifact 注入、超时、恢复规则和评分协议不变。订阅及 GPT-4 服务端权重快照无法保证固定，不能称为完全同模型条件复现。

## E. 实验成绩、统计与异常

| 运行 | 内部 G1 /12 | 内部 G2 /12 | GPT-4 G1 /10 | GPT-4 G2 /10 |
| --- | ---: | ---: | ---: | ---: |
| 权威历史 full-v8（14 题） | 9.9286 | 10.2857 | 9.3571 | 9.2143 |
| 新独立 smoke（仅 task01） | 11 | 10 | 未执行 | 未执行 |
| 本次新 14 题 | **10.6286** | **11.0143** | **9.6071** | **9.5714** |

| 本次配对统计 | G2−G1 | Bootstrap 95% CI | 双侧精确 sign-flip p | 胜 / 平 / 负 |
| --- | ---: | --- | ---: | --- |
| 内部盲评 | +0.3857 | [-0.5071, 1.1214] | 0.4336 | 8 / 3 / 3 |
| GPT-4 校准 | -0.0357 | [-0.3214, 0.2500] | 1.0000 | 2 / 9 / 3 |

两种评价均未证明 G2 显著提升。Bootstrap 10,000 次、seed=20260825；sign-flip 的前提是零假设下配对差值可交换符号。n=14 的结果是此任务集上的描述，仍为 provisional LLM-judged，未完成人类裁定或官方认证。

| 题目 | 内部 G1 | 内部 G2 | 内部差值 | GPT-4 G1 | GPT-4 G2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| chemcrow-01 | 9 | 10 | +1.0 | 9.5 | 9 |
| chemcrow-02 | 11 | 12 | +1.0 | 9 | 10 |
| chemcrow-03 | 12 | 12 | +0.0 | 10 | 10 |
| chemcrow-04 | 8 | 11 | +3.0 | 9 | 9 |
| chemcrow-05 | 9.5 | 10.4 | +0.9 | 10 | 10 |
| chemcrow-06 | 12 | 12 | +0.0 | 10 | 10 |
| chemcrow-07 | 10 | 11 | +1.0 | 10 | 9 |
| chemcrow-08 | 11.3 | 12 | +0.7 | 9 | 10 |
| chemcrow-09 | 12 | 11.8 | -0.2 | 10 | 10 |
| chemcrow-10 | 11 | 10 | -1.0 | 9 | 9 |
| chemcrow-12 | 12 | 12 | +0.0 | 10 | 10 |
| chemcrow-13 | 8 | 10 | +2.0 | 9 | 9 |
| chemcrow-14 | 12 | 8 | -4.0 | 10 | 9 |
| chemcrow-15 | 11 | 12 | +1.0 | 10 | 10 |

最大内部下降为 task14（12→8），任务完成度下降 2 分、化学正确性和推理各下降 1 分；其 GPT-4 评价为 10→9。task10 内部下降 1 分，task09 下降 0.2 分。这里只陈述记录中的评分分解，不凭分数推断某个工具故障具有因果作用。

共 686 次工具观测，161 次错误（23.47%）：71 次 WebSearch 缺少 SERP_API_KEY、29 次输入校验、34 次 HTTP 404、16 次 HTTP 503、9 次 HTTP 400、2 次 TLS EOF。 [工具审计](tool-observation-audit.json)保留逐题/逐代计数及 pair 哈希，无原始参数、答案或 URL；mock/fixture observation=0。缺失 WebSearch、上游可用性和新固定 RXN 版本均限制与历史条件的可比性。

[新旧比较](chemcrowrun-comparison.json)包含完整逐题差值。新相对历史的内部 G1/G2 平均差为 +0.7000/+0.7286；GPT-4 为 +0.2500/+0.3571。跨运行检验是未校正的探索性分析，模型/服务/工具变化构成混杂，不能解释为演化方法的因果改进。当前 Judge 对历史对照的评分也有漂移，见完整报告的校准对照项。

## F. Fresh Clone 验收与命令

生成实验使用独立 `git clone --no-local`，未复制旧数据库、trajectory、答案或私有 cache。依赖独立安装；公开 fixture 从上游取得并锁校验，可共享已校验的公开下载缓存。模型凭据来自合法外部配置；历史 notebook 只在完整生成封存后的 Judge 对照阶段访问。使用本地原始 OCI，不冒称公开下载验收。

实际执行的等价命令如下（本地源路径和私人凭据路径使用占位符）：

```bash
git clone --no-local --branch codex/chemcrowrun-v1 /path/to/candidate ChemCrowRun
cd ChemCrowRun
git checkout 1bf37ce0b
uv sync --frozen --project benchmarks/chemcrow --python 3.11.15 --extra dev
uv run --frozen --project benchmarks/chemcrow chemcrowrun reproduction-fixtures \
  --lock benchmarks/chemcrow/reproduction/fixtures.lock.json \
  --destination-root /path/to/public-fixture-cache --no-model-calls
uv run --frozen --project benchmarks/chemcrow chemcrowrun runtime \
  --archive /path/to/original/openevo-science-runtime-0.1.1-linux-amd64.tar.gz \
  --output .reproduction-cache/runtime-verification.json
uv run --frozen --project benchmarks/chemcrow chemcrowrun init \
  --run-id reproduction-v1-sol-20261015 --port-base 19480 \
  --fixture-root /path/to/public-fixture-cache --model gpt-5.6-sol \
  --substitution-reason "Historical gpt-5.5 absent from current subscription catalog; unchanged Core isolation passes with gpt-5.6-sol"
uv run --frozen --project benchmarks/chemcrow chemcrowrun up \
  --workspace outputs/chemcrowrun/reproduction-v1-sol-20261015
uv run --frozen --project benchmarks/chemcrow chemcrowrun status \
  --workspace outputs/chemcrowrun/reproduction-v1-sol-20261015
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --frozen --project benchmarks/chemcrow chemcrowrun experiment \
  --workspace outputs/chemcrowrun/reproduction-v1-sol-20261015 --allow-paid
uv run --frozen --project benchmarks/chemcrow chemcrowrun report \
  --workspace outputs/chemcrowrun/reproduction-v1-sol-20261015
```

本次在两次明确验证的 no-effect 恢复后使用 `experiment --resume --allow-paid` 续跑。Judge 首次 CLI preflight 停止，随后按 D 所述的单路径修正脚本完成，而不是未经修改的 `1bf37ce0b judge` 一次通过。最终 `report`、`logs`、`down` 已实测。原始 source1bf 导出遗漏 replacements，现由独立分析代码离线修正并保留原文件：[导出证明](export-provenance.json)。

最终候选 `6e03aef26` 在另一个干净 clone 完成独立安装、328 项测试和 L1 校验；这些后处理修复没有再花费模型费用重复整场实验。新用户使用最终候选的常规 `judge --env-file /private/judge.env --budget-usd 12 --allow-paid` 和 `report` 流程，完整步骤见 [REPRODUCE.md](../../REPRODUCE.md)。这不等于已经在最终 commit 从头再次付费运行。

发布后仍需从真实 GitHub commit clone、匿名下载 runtime 并完成远端复验。当前两项为 PENDING，无法声称“第三方仅靠已公开资产即可完成”的最终交付已验收。

## G. L1 / L2 / L3

| 层级 | 状态与边界 |
| --- | --- |
| L1 历史公开证据 | PASS：84 artifacts、226632 bytes；原 manifest/payload 哈希保持 |
| L2 离线/本地环境 | PASS：独立安装、fixture 锁、原始 OCI、最终 328 项测试与 preflight |
| 新真实单题 | PASS：8 调用、三路反思、注入、盲评及封存审计 |
| L3 新 14 题内部结果 | PASS：112 成功逻辑调用、42 artifacts、14 pair、审计与标准 schema；两次经验证恢复公开记录 |
| GPT-4 校准 | PASS：42 有效封存调用；独立账单、配对统计和历史对照 |
| 全工具可用性 | PARTIAL：WebSearch 缺 SERP_API_KEY，上游错误保留 |
| 原 runtime 再分发材料 | EVIDENCE_SUPPORTS_REDISTRIBUTION_WITH_COMPANIONS；待用户发布批准 |
| 真实公开下载/远端 fresh clone | PENDING_PUBLICATION_AND_REMOTE_ACCEPTANCE |

## H. Git 与发布准备

没有执行 push、创建正式 PR、Release Asset、公开镜像或排行榜提交。09:54 UTC 只读核对远端：`Ardorry/OpenEvo` 的 `chemcrow` 仍为 `4bfe8f797a2026ebbcd95fb64b6ea32646c827dd`；两个候选分支尚未存在远端。原 chemcrow 分支和历史数据未覆盖。

本地提交沿安全分支追加；[发布计划](PUBLICATION_PLAN.md)列出拟推送目标、PR 标题/正文、资产状态和批准范围。最终提交及 diff 清单见同目录的 publication-git-state.json、publication-commits.txt 和 publication-diff-stat.txt。实际 Git HEAD 的完整值以提交后只读核对为准。

当前可集中申请批准 **候选分支 push、面向 chemcrow 的未合并正式 PR，以及 release-assets.json 所列原始镜像和完整 source/notice companions 的 GitHub Release**。拟定 tag 为 chemcrowrun-v1.0，实际目标提交与远端状态在发布前复核；不包含 merge、公开 registry 镜像或排行榜提交。原始日志、模型回答、数据库、私有配置和凭据不进入公开提交。
