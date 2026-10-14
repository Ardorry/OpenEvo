# ChemCrow full-v8 规范化复现

本文给出从全新 clone 到 full-v8 G1/G2、盲评、审计和成绩导出的完整流程。命令分为
零模型检查和付费实验两组。安装、fixture 获取、静态检查、测试、preflight、已有公开证据
验证和 submission 验证均不会调用模型。

## 1. 固定版本与复现边界

- 代码：先选择一个包含本文件的完整 40 位 Git commit，并在整个 run 中保持 checkout
  clean。submission 会记录并校验该 commit。
- Python：`3.11.15`；uv：`0.12.5`；Python 依赖由
  [`uv.lock`](uv.lock) 固定。
- Docker CLI/Engine：`29.5.2`；Linux runtime：`linux-amd64`，OCI index
  `sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`。
  完整身份和归档哈希见 [`reproduction/toolchain.lock.json`](reproduction/toolchain.lock.json)。
- 外部源码和数据由
  [`reproduction/fixtures.lock.json`](reproduction/fixtures.lock.json) 固定到 commit、Git tree、
  必需对象以及可用时的文件 SHA-256。
- 任务是 `tasks.jsonl` 中按顺序排列的 14 题：`chemcrow-01` 至 `chemcrow-10`，以及
  `chemcrow-12` 至 `chemcrow-15`。任务清单 SHA-256 为
  `8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604`。

外部模型和服务可能更新，因此不承诺逐题文本完全一致。成绩文件必须记录实际 provider、
模型标识、运行起止时间、代码 commit、配置哈希和随机种子，才能进行协议兼容性比较。

## 2. 从 clone 安装

```bash
git clone https://github.com/Ardorry/OpenEvo.git
cd OpenEvo
git checkout <FULL_40_CHARACTER_RELEASE_COMMIT>
test -z "$(git status --porcelain=v1)"

uv self update 0.12.5
uv sync --frozen --project benchmarks/chemcrow --python 3.11.15 --extra dev
uv run --frozen --project benchmarks/chemcrow python --version
```

`uv self update` 可替换为发行方推荐的固定版本安装方式。继续前应确认输出为 Python
`3.11.15` 和 uv `0.12.5`。

## 3. 获取并验证 fixture

```bash
export CHEMCROW_FIXTURE_ROOT="$PWD/.reproduction-fixtures"
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow \
  reproduction-fixtures \
  --lock benchmarks/chemcrow/reproduction/fixtures.lock.json \
  --destination-root "$CHEMCROW_FIXTURE_ROOT" \
  --no-model-calls
```

工具只创建不存在的目标目录，不覆盖已有 checkout。以后使用 `--check-only` 复核。锁中有四个
上游仓库：

| 资源 | 用途 | 公开与再分发状态 |
| --- | --- | --- |
| `chemcrow-runs` | 测试任务和 notebook fixture | 可匿名读取；上游未发现许可证，不能把其内容重新打包发布 |
| `chemcrow-public` | controlled-chemical CSV | MIT，可按许可证再分发 |
| `rxn-sandbox` | 本地 RXN forward/retro 服务和 LFS 权重 | OpenMDW-1.1，发布时须保留许可证、通知并履行其条件 |
| `rxn4chemistry` | 可选 hosted RXN client | MIT |

`rxn-sandbox` 的两个权重各约 155 MB，需要 Git LFS。默认 fixture 命令会验证真实 LFS payload，
而不是 pointer 文件。只准备离线测试时可添加两次
`--required-for tests --required-for controlled_chemical_data`，避免下载 RXN 权重。

## 4. 零模型验收

```bash
export CHEMCROW_CONTROLLED_CSV="$CHEMCROW_FIXTURE_ROOT/chemcrow-public/chemcrow/data/chem_wep_smi.csv"
export CHEMCROW_EXPERIMENT_ID="chemcrow-third-party-$(date -u +%Y%m%dT%H%M%SZ)"
export CHEMCROW_RUN_ROOT="$PWD/.reproduction-state/runs/$CHEMCROW_EXPERIMENT_ID"
export CHEMCROW_CACHE_ROOT="$PWD/.reproduction-state/caches/$CHEMCROW_EXPERIMENT_ID"
export CHEMCROW_LEDGER_ROOT="$PWD/.reproduction-state/ledgers/$CHEMCROW_EXPERIMENT_ID"
export CHEMCROW_EVOLUTION_DB="$PWD/.reproduction-state/evolution/$CHEMCROW_EXPERIMENT_ID.sqlite3"
export CHEMCROW_EVOLUTION_ARTIFACT_ROOT="$PWD/.reproduction-state/evolution/$CHEMCROW_EXPERIMENT_ID-artifacts"
export OPENEVO_ROLLOUT_BASE_URL=http://127.0.0.1:8001
export OPENEVO_EVOLUTION_BASE_URL=http://127.0.0.1:8002

uv run --frozen --project benchmarks/chemcrow ruff check \
  benchmarks/chemcrow/src benchmarks/chemcrow/tests benchmarks/chemcrow/scripts
CHEMCROW_TEST_RUNS_ROOT="$CHEMCROW_FIXTURE_ROOT/chemcrow-runs" \
CHEMCROW_TEST_PUBLIC_ROOT="$CHEMCROW_FIXTURE_ROOT/chemcrow-public" \
  uv run --frozen --project benchmarks/chemcrow pytest benchmarks/chemcrow/tests -q

uv run --frozen --project benchmarks/chemcrow openevo-chemcrow \
  verify-publication --package-root benchmarks/chemcrow --no-model-calls
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow \
  reproduction-check \
  --package-root benchmarks/chemcrow \
  --config benchmarks/chemcrow/configs/full-v8.reproduction.example.yaml \
  --fixture-lock benchmarks/chemcrow/reproduction/fixtures.lock.json \
  --fixture-root "$CHEMCROW_FIXTURE_ROOT" \
  --toolchain-lock benchmarks/chemcrow/reproduction/toolchain.lock.json \
  --no-model-calls
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow \
  preflight --config benchmarks/chemcrow/configs/full-v8.reproduction.example.yaml \
  --output "$CHEMCROW_RUN_ROOT/preflight.json" --no-model-calls
```

`reproduction-check` 的 `offline_ready=true` 表示依赖、fixture、配置与公开 artifact 已通过；
完整状态只有 Docker daemon 和精确 runtime image 也可用时才是 `READY`。`preflight` 还会检查
Core Rollout/Evolution 健康状态、健康 worker、Codex subscription 凭据、RXN 配置、模型身份和
runtime 标签；任何缺项均返回 `BLOCKED_HUMAN_ACTION_REQUIRED`，且 `model_calls=0`。

## 5. Docker 与工具服务

运行 full-v8 前须把 toolchain lock 中的 runtime archive 导入 Docker，并由现有 OpenEvo managed
runtime 校验器确认 archive 大小、SHA-256、config ID、OCI index ID 和 label。当前公开仓库没有
已验证可匿名下载该精确 archive 的 URL；2026-10-14 对
`openevo/science-runtime:0.1.1` 的匿名 registry 查询被拒绝并要求认证，仓库中也没有经验证能重建
相同 OCI 身份的 source Dockerfile。维护者需要发布 lock 中的归档，或提供按相同来源构建且产生
相同 OCI 身份的可审计流程。若只有不同镜像 digest，该 run 与本协议不兼容。

RXN 上游源码和权重可以从锁定的 `rxn-sandbox` Git/LFS checkout 获取，但历史 compose 文件含本地
image ID 和个人 volume 路径。第三方需要按上游 Dockerfile 构建 forward/retro 服务，并把 endpoint
通过 `CHEMCROW_RXN_PREDICT_URL`、`CHEMCROW_RXN_RETRO_URL` 和
`CHEMCROW_RXN_TIMEOUT_SECONDS` 提供给 ChemCrow 工具桥。WebSearch 另需 `SERP_API_KEY`；
可用 `openevo-chemcrow credential-check --output <PATH>` 做只报告 presence、不读取 secret value 的
检查。`reproduction-check` 不把“源码可取”误报成“运行时已就绪”。

## 6. 标准实验协议

每题独立执行：

1. 从 bare S0 运行 G1 Candidate；
2. 用 evolution evaluator 生成内部反馈；
3. 从同一冻结输入分别运行 memory、skill bundle、agent system 三个 Reflector；
4. 注册并验证三个不同的 Core-native artifact；
5. 注入三个 artifact，凭回执运行 G2 Candidate；
6. 对匿名映射的 G1/G2 答案运行 final blind evaluator；
7. 封存 pair，销毁 task-local artifact 状态并验证 bare-S0 reset 后才进入下一题。

六个角色都固定为配置中的 `gpt-5.5`、Codex subscription、transcript capture。Candidate 和
Reflector timeout 为 900 秒，两个 evaluator 为 600 秒。种子是 `20260825`。完整运行预计
112 次模型调用，即每题 G1、G1 内评、三个 Reflector、G2、G2 内评和盲评各一次。自动 retry 为
0；失败调用只有在生成带哈希的显式 replacement authority 后才能替换，旧 call ID 不得重用，
不得按分数重试。

Codex subscription harness 的 admitted config 不暴露 `temperature` 或 `top_p`，因此协议不虚构
这些参数；配置固定 reasoning effort/summary 和本地随机种子，但该种子不能控制 provider 端采样。
每个 submission 必须记录实际 provider、模型标识和时间，重复 run 应作为随机实现比较，不能要求
答案正文逐字节一致。

盲评使用 `chemcrow-final-blind-evaluator-v1`，三个维度分别为 chemical correctness、reasoning
quality、task completion，每项 0–4，总分 0–12。人工专家评价未完成时必须保持
`PROVISIONAL_NOT_COMPLETED`。历史 calibrated GPT-4 paper evaluator 是另一个研究比较协议，不是
本 submission 的标准 Judge。

出现以下任一情况不得作为完整 submission：少于或多于 14 题；任务顺序/哈希变化；任意 pair、
run 或 42 个 artifact ID 重复；少一个 Reflector；G2 无注入回执；题间 reset 无回执；fixture/mock
观测进入真实 run；审计非 PASS；自动 retry；未经 authority 的 replacement；配置、模型或 Judge
协议不兼容；答案、密钥或 host path 被写入 submission。

## 7. 付费运行入口

只有在前两项检查均就绪、服务已启动，并得到实验负责人明确授权后执行：

```bash
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow run \
  --config benchmarks/chemcrow/configs/full-v8.reproduction.example.yaml \
  --allow-paid
```

这一个命令覆盖 G1、逐题演化、G2 和盲评。它有双重付费门：环境变量和 `--allow-paid` 缺一不可。
中断后只能在核查 ledger 后使用同样的配置执行 `resume`；不能把部分 run 当作完整结果。

完成后执行零模型审计和汇总：

```bash
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow audit-run \
  --config benchmarks/chemcrow/configs/full-v8.reproduction.example.yaml \
  --core-completions <CORE_COMPLETION_LEDGER> \
  --output "$CHEMCROW_RUN_ROOT/completed-run.audit.json"
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow aggregate-run \
  --config benchmarks/chemcrow/configs/full-v8.reproduction.example.yaml \
  --output "$CHEMCROW_RUN_ROOT/aggregate.json" --no-model-calls
```

## 8. 导出、验证和汇总 submission

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow export-submission \
  --config benchmarks/chemcrow/configs/full-v8.reproduction.example.yaml \
  --audit "$CHEMCROW_RUN_ROOT/completed-run.audit.json" \
  --aggregate "$CHEMCROW_RUN_ROOT/aggregate.json" \
  --repository-root "$PWD" --source-commit "$SOURCE_COMMIT" \
  --provider <ACTUAL_PROVIDER> \
  --started-at <ISO8601_WITH_TIMEZONE> --completed-at <ISO8601_WITH_TIMEZONE> \
  --output "$CHEMCROW_RUN_ROOT/submission.json" --no-model-calls
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow validate-submission \
  --submission "$CHEMCROW_RUN_ROOT/submission.json" --no-model-calls
```

格式权威是 [`reproduction/submission.schema.json`](reproduction/submission.schema.json)。导出器要求
clean checkout、完整 14 题 PASS audit，并重新从 sealed pair rows 计算 aggregate。它只导出答案
哈希和审计关联，不导出答案正文。多个兼容成绩可离线汇总：

```bash
uv run --frozen --project benchmarks/chemcrow openevo-chemcrow aggregate-submissions \
  --submission run-a.json --submission run-b.json --output ranking.json --no-model-calls
```

## 9. 已有结论的 L1 复核

[`reports/full_v8_public_20260831/per_task_evidence.json`](reports/full_v8_public_20260831/per_task_evidence.json)
是原始 full-v8 的脱敏逐题索引，连接 run identity、答案哈希、42 个 artifact 哈希、Reflector
job/run、注入回执、reset 回执和评分。它与公开 aggregate 和完成审计的哈希绑定。公开 84 个
artifact 可用 `verify-publication` 逐字节验证。
[`paper_judge_evidence.json`](reports/full_v8_public_20260831/paper_judge_evidence.json)
提供 42 个校准 GPT-4 Judge 分数及其证据哈希，可独立重算 G1/G2 均值、差值和胜平负。

公开材料仍不含 prompt、答案正文、trajectory、tool observation、provider 原始响应、私有账本和
人工专家标注，因此第三方可验证已公开字节与关联结构，但不能仅靠公开包重建历史 provider 调用或
完成全部语义审计。原始权威结果、该公开证据投影和新的第三方 submission 在字段中明确区分。
