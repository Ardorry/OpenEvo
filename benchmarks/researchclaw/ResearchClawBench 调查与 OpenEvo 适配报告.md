# ResearchClawBench 调查与 OpenEvo 适配报告

## 一、结论摘要

ResearchClawBench 是一个长程、端到端科研代理评测框架。代理获得任务描述、原始数据和相关文献，需要自行完成数据探索、实验设计、代码编写、结果验证、图表生成和科研报告撰写；最终由多模态 LLM judge 对照目标论文的隐藏 checklist 评分。官方仓库包含 **10 个领域、40 个基础任务**，Hugging Face 数据集另有 **16 个社区任务**。

对 OpenEvo，建议采用以下主线：

1. OpenEvo Core 保持无修改，新增独立 `benchmarks/researchclawbench` adapter。
2. 继续采用现有架构：**OpenEvo 外层演化与编排 → Codex CLI 内层执行 → GPT-5.5**。
3. 只演化 `agent_system`，保持 `text_memory`、`skill_bundle` 和 `parametric_memory` 关闭。
4. 先通过 `agents.json` 接入官方 Web UI，验证单任务标准工作区。
5. 另写 OpenEvo 自有 batch runner，复用官方 workspace 构造和 `score_workspace()`，避免重写评分器。
6. 社区任务用于演化和验证，官方 40 题保留为冻结测试集。
7. evaluator 与 candidate agent 必须进程隔离；严禁向 reflector 暴露 checklist、目标论文、目标图像或逐项评分理由。
8. 最终成绩使用官方评测流程生成，打包完整 run 文件夹，邮件提交给维护者。

------

## 二、Benchmark 评测内容

### 2.1 单个任务的输入

官方任务目录大致如下：

```text
tasks/<TaskID>/
├── task_info.json
├── data/
├── related_work/
└── target_study/
    ├── paper.pdf
    ├── checklist.json
    └── images/
```

代理运行时只应获得：

- `task_info.json` 转换出的任务说明；
- `data/` 原始实验数据；
- `related_work/` 相关论文；
- 统一的自主科研执行指令。

`target_study/` 中的目标论文、评分 checklist 和目标图片属于 evaluator 侧隐藏信息。

### 2.2 代理必须生成的产物

标准 workspace 为：

```text
workspace/
├── INSTRUCTIONS.md
├── data/
├── related_work/
├── code/
├── outputs/
├── report/
│   ├── report.md
│   └── images/*.png
├── _agent_output.jsonl
└── _meta.json
```

其中：

- `code/`：分析和实验代码；
- `outputs/`：中间结果；
- `report/report.md`：最终科研报告；
- `report/images/`：PNG 图表；
- `_agent_output.jsonl`：代理标准输出和错误输出；
- `_meta.json`：任务、代理、时长、状态和模型等元数据；
- `_score.json`：完成评分后生成。

官方指令明确要求报告至少包含数据概览、方法、主要结果、讨论，以及验证或对比图表。

### 2.3 评分方式

每个任务都有若干带权重的 checklist item：

- `text`：方法、结论、机理分析或定量结果；
- `image`：将代理生成的图片与目标论文图片进行多模态比较。

每项得分为 0–100：

- 约 50 分：达到目标论文对应结果的水平；
- 低于 50：证据、方法或结果较弱；
- 高于 50：结果或分析超过参考论文。

最终分数是各项分数的加权平均。

评分器当前还有两个需要注意的实现细节：

1. 图像评分最多读取代理 workspace 中找到的前 5 张图片，因此生成大量低价值图片可能挤掉关键图片。
2. judge 会自行判断每项采用定量 Mode A 或定性 Mode B。维护者已确认，同一 checklist item 在不同运行中可能被分类成不同模式，造成额外评分噪声。

------

## 三、官方运行方式

## 3.1 安装 Benchmark

```bash
git clone --depth 1 https://github.com/InternScience/ResearchClawBench.git
cd ResearchClawBench

python -m venv .venv
source .venv/bin/activate

pip install -r evaluation/requirements.txt
```

核心依赖包括 Flask、OpenAI SDK、StructAI、PyYAML、openpyxl 和 ResearchHarness。

评分器配置写入 `evaluation/.env`：

```env
JUDGE_API_KEY=sk-xxx
JUDGE_API_BASE=https://api.openai.com/v1
JUDGE_MODEL_NAME=gpt-5.1
```

README 中的 `gpt-5.1` 是公开示例。正式提交前应固定 benchmark commit，并确认维护者当前认可的 judge 配置。

## 3.2 Web UI 单任务运行

启动：

```bash
python -m evaluation
```

打开：

```text
http://localhost:5000
```

操作流程：

1. 选择任务；
2. 选择代理；
3. 点击 `Start Run`；
4. 等待代理结束；
5. 进入 `Evaluation`；
6. 点击 `Score`。

Web UI 会创建标准 workspace，并展示代码、输出、报告、图片和逐项评分理由。

这条路径最适合 OpenEvo 适配早期的 canary 和 smoke test。

## 3.3 官方 Batch CLI

当前通用 batch CLI 实际绑定 ResearchHarness：

```bash
python3 -m evaluation.cli_eval eval_configs/my_eval.yaml
```

单任务配置示例：

```yaml
name: researchharness_example

agent_model:
  name_env: AGENT_MODEL_NAME
  api_base_env: AGENT_API_BASE
  api_key_env: AGENT_API_KEY

tasks:
  - id: Physics_003
    repeats: 1

repeats_per_task: 1
max_concurrent_runs: 1

researchharness:
  max_rounds: 500
  max_runtime_seconds: 10800
  llm_request_timeout_seconds: 1200
  webfetch_tool_timeout_seconds: 300
  readpdf_tool_timeout_seconds: 300
  max_output_tokens: 16384
  max_input_tokens: 131072
  compact_trigger_tokens: "96k"

judge_model:
  enabled: true
  name_env: JUDGE_MODEL_NAME
  api_base_env: JUDGE_API_BASE
  api_key_env: JUDGE_API_KEY
```

运行前可进行配置检查：

```bash
python3 -m evaluation.cli_eval \
  eval_configs/my_eval.yaml \
  --dry-run \
  --skip-secret-check \
  --no-score
```

Batch 结果保存在：

```text
workspaces/cli_runs/cli_<timestamp>_<random>/
```

其中包含各任务 run 文件夹和汇总报告 `eval_report_<batch_id>.md`。

由于该 CLI 的 runtime 逻辑直接面向 ResearchHarness，OpenEvo 全量运行不应强行套用这一实现。更合适的方式是复用官方 `TaskRunner` 和 `score_workspace()`，单独编写 OpenEvo batch orchestration。

------

## 四、自定义代理接入协议

### 4.1 agents.json 接口

自定义代理通过 `evaluation/agents.json` 注册：

```json
{
  "openevo": {
    "label": "OpenEvo",
    "icon": "O",
    "logo": "/static/logos/openevo.svg",
    "cmd": "python -m openevo_researchclawbench.agent --workspace <WORKSPACE> --prompt <PROMPT>"
  }
}
```

官方要求代理：

- 能接受 prompt；
- 能在指定 workspace 中运行；
- 全程无人工交互；
- 将运行日志写入 stdout；
- 生成 `report/report.md` 和 PNG 图片；
- 完成后以退出码 0 结束。

官方已有 Codex、ResearchClaw、EvoScientist 等命令模板，可直接参考其调用形式。

### 4.2 Prompt 参数需要特别处理

README 描述 `<PROMPT>` 可以代表文件路径或 prompt 内容；当前 `run_task.py` 实现会统一替换为：

```bash
"$(cat '<INSTRUCTIONS_PATH>')"
```

即将完整 prompt 内容作为命令行参数传给代理。

因此 OpenEvo adapter 的入口应支持：

```bash
--prompt "<完整长文本>"
```

不要假设接收到的是 `INSTRUCTIONS.md` 路径。后续若希望规避超长命令行和 shell quoting 问题，可以在自己的 ResearchClawBench fork 中增加 `<PROMPT_FILE>` 占位符，但用于官方提交时要说明这一补丁。

### 4.3 退出状态不等于产物完整

当前 runner 主要根据子进程退出码判断 `completed` 或 `failed`，不会在标记完成前强制校验报告和图片。

OpenEvo wrapper 应在退出前执行 postcondition：

```text
report/report.md 存在且非空
report/images/ 至少存在一张有效 PNG
报告中引用的图片均存在
关键分析脚本存在
报告不包含明显占位符
```

任一条件失败时返回非零退出码。

------

## 五、成绩和贡献提交方式

ResearchClawBench 有三类不同的“提交”。

## 5.1 提交排行榜成绩

维护者在 2026 年 7 月的公开回复中给出的流程是：

1. 使用官方 RCB evaluation workflow 运行；
2. 打包完整 run folder；
3. 联系维护者邮箱 `xu_wanghan@sjtu.edu.cn`；
4. 由维护者审核并导入公开排行榜。

建议提交包结构：

```text
openevo_rcb_submission/
├── README.md
├── manifest.json
├── benchmark_commit.txt
├── openevo_commit.txt
├── adapter_commit.txt
├── experiment_config.yaml
└── runs/
    ├── <task_run_id_1>/
    │   ├── _meta.json
    │   ├── _agent_output.jsonl
    │   ├── INSTRUCTIONS.md
    │   ├── code/
    │   ├── outputs/
    │   ├── report/
    │   └── _score.json
    └── ...
```

`manifest.json` 建议额外记录：

- OpenEvo、adapter、benchmark Git commit；
- 底模精确名称；
- Codex CLI 版本；
- reasoning level；
- token、step、时间和成本预算；
- 网络、Web、MCP、subagent 策略；
- evolution 对象；
- 每个 task 的 seed、时长、退出状态和总分；
- Baseline 与 OpenEvo treatment 的区别。

这些字段目前不全属于官方硬性要求，但会显著提升审核和复现实验的可信度。

## 5.2 提交新的代理预设

将 OpenEvo 作为官方内置 agent 时，需要：

1. 修改 `evaluation/agents.json`；
2. 添加 logo；
3. 在 Web UI 中验证；
4. 向 ResearchClawBench GitHub 仓库提交 PR。

官方贡献指南要求 PR 保持单一主题，并在本地验证运行和评分流程。

建议先完成排行榜结果，再决定是否提交 agent preset PR。这样可以用真实 run 证明 OpenEvo adapter 已可复现运行。

## 5.3 提交新的 Benchmark 任务

新的任务不直接进入基础 GitHub 仓库，需打包后上传官方 Hugging Face Submission Space。系统会验证 zip，并向数据集仓库创建 PR。

这条渠道适合后续将 OpenEvo 项目中的科学任务整理为社区任务，与 OpenEvo 成绩提交相互独立。

------

## 六、当前实现中的公平性与安全风险

### 6.1 `target_study` 泄漏风险

`TaskRunner` 只把 `data/` 和 `related_work/` 复制进 workspace。

但从源码看，子进程运行时：

- 仅将 `cwd` 设置为 workspace；
- 继承宿主环境变量；
- 使用 `shell=True`；
- 未见容器、chroot 或 mount namespace 隔离。

与此同时，`target_study/` 仍位于同一 benchmark clone 下。只依赖工作目录和 prompt 约束时，具备 shell 能力的代理理论上可以通过父目录遍历读取目标论文或 checklist。

OpenEvo 正式运行必须增加真实隔离：

```text
candidate 容器可见：
  /workspace/data            只读
  /workspace/related_work    只读
  /workspace/code            可写
  /workspace/outputs         可写
  /workspace/report          可写

candidate 容器不可见：
  ResearchClawBench/tasks/
  target_study/
  checklist.json
  judge credentials
  scorer source与结果
  其他 run workspace
```

Evaluator 应在 candidate 容器结束后，于独立进程或独立容器中挂载 `target_study/` 并评分。

### 6.2 环境变量泄漏

Runner 默认继承全部环境变量。OpenEvo adapter 应建立 allowlist，只保留：

- Codex 必需配置；
  -固定代理变量；
  -系统最小 PATH 和语言变量。

需要删除：

- judge API key；
- benchmark 管理密钥；
  -其他模型 provider key；
  -远程存储凭据；
  -GitHub、Hugging Face 等无关 token。

### 6.3 Scorer feedback 泄漏

以下信息不得进入 OpenEvo reflector：

- checklist item 内容；
- checklist keywords；
- target paper；
- target images；
  -目标论文指标；
  -逐项 judge reasoning；
- evaluator prompt；
- `_score.json` 的逐项内容。

在开发任务上可开放的反馈建议限制为：

```json
{
  "total_score": 27.4,
  "completed": true,
  "artifact_valid": true,
  "runtime_seconds": 2100,
  "generic_failure_tags": [
    "insufficient_validation",
    "missing_uncertainty_analysis"
  ]
}
```

`generic_failure_tags` 应来自事先固定的通用分类器，避免包含目标论文答案。

------

## 七、OpenEvo Adapter 推荐架构

沿用既有 ChemBench 项目的 adapter-only 边界：

```text
OpenEvo/
└── benchmarks/
    └── researchclawbench/
        ├── pyproject.toml
        ├── README.md
        ├── src/
        │   └── openevo_researchclawbench/
        │       ├── task_loader.py
        │       ├── prompt_adapter.py
        │       ├── workspace.py
        │       ├── codex_harness.py
        │       ├── agent_entrypoint.py
        │       ├── artifact_validator.py
        │       ├── official_scorer.py
        │       ├── feedback_filter.py
        │       ├── batch_runner.py
        │       ├── result_writer.py
        │       └── submission_packager.py
        └── tests/

input/
└── researchclawbench/
    ├── smoke.yaml
    ├── community_dev.yaml
    ├── community_validation.yaml
    └── official_final.yaml

scripts/
├── run_rcb_smoke.sh
├── run_rcb_evolution.sh
├── run_rcb_final.sh
└── package_rcb_submission.sh
```

保持：

```text
OpenEvo/src/openevo          无修改
OpenEvo/pyproject.toml       无修改
```

### 7.1 执行链路

```text
ResearchClawBench task
        │
        ▼
OpenEvo ResearchClawBench adapter
        │
        ├── Baseline：原始 agent_system
        │
        └── Treatment：冻结的 evolved agent_system
        │
        ▼
OpenEvo outer harness
        │
        ▼
Codex CLI inner harness
        │
        ▼
GPT-5.5
        │
        ▼
code / outputs / report / images
        │
        ▼
artifact validator
        │
        ▼
官方 ResearchClawBench scorer
```

Baseline 和 treatment 必须共享：

- GPT-5.5；
- Codex CLI 版本；
- reasoning level；
  -工具集合；
  -网络策略；
  -最大执行步数；
  -时间预算；
  -上下文和输出 token 限制；
  -预装依赖；
  -任务顺序和 seed。

唯一处理变量应是 evolved `agent_system`。

### 7.2 OpenEvo entrypoint

```python
def main() -> int:
    args = parse_args()

    workspace = validate_workspace(args.workspace)
    prompt = args.prompt

    artifact = load_frozen_agent_system(args.artifact)
    final_prompt = compose_prompt(
        benchmark_prompt=prompt,
        evolved_agent_system=artifact,
    )

    result = run_codex(
        cwd=workspace,
        prompt=final_prompt,
        model="gpt-5.5",
        network_enabled=False,
        mcp_enabled=False,
        subagents_enabled=False,
    )

    validation = validate_artifacts(workspace)
    write_run_manifest(workspace, result, validation)

    return 0 if result.success and validation.passed else 1
```

正式适配时应复用现有 `CodexHarness`，入口示例只表达责任边界。

------

## 八、演化与正式测试协议

### 8.1 数据划分

推荐：

- **社区任务 Dev**：用于 rollout、reflector 和候选筛选；
- **社区任务 Validation**：用于选择最终 `agent_system`；
- **官方基础 40 题**：冻结测试集；
- **Pass@5**：只用于最终候选，避免演化阶段成本膨胀。

当前社区任务有 16 个，可按领域分层划分为约 10 个 Dev 和 6 个 Validation。领域分布不均时，同源任务应进入同一 split，减少近重复泄漏。

官方公开的 Pass@5 数据同时保存 5 次尝试、最佳分、均值、标准差、最小值和最大值。

### 8.2 分阶段筛选

建议采用以下 gate：

1. **静态验证**
   - 配置和路径有效；
   - 不存在目标信息访问；
   - 无 judge key 泄漏。
2. **Canary**
   - 运行 1–2 个社区任务；
   - 验证 Codex 能持续执行；
   - 验证报告和图片产物。
3. **Dev rollout**
   - 演化候选 `agent_system`；
   - reflector 只接收合法反馈。
4. **Validation gate**
   - 候选在未参与演化的社区任务上评测；
   - 按完成率、RADS、稳定性和成本选择一个最终版本。
5. **Freeze**
   - 固定 adapter commit；
   - 固定 `agent_system`；
   - 固定模型和预算；
   - 禁止根据官方 40 题结果继续修改。
6. **Official final**
   - 运行 Baseline；
   - 运行 OpenEvo treatment；
   - 使用官方 scorer；
   - 生成提交包。

### 8.3 候选选择指标

建议按优先级排序：

1. 报告完成率；
2. artifact validator 通过率；
3. 平均 RADS；
4. 中位数 RADS；
5. 最差领域表现；
6. 跨任务标准差；
7. 运行失败率；
8. API 成本和运行时长。

不要只使用平均分。一个候选可能在少数任务中获得高分，同时在大量任务中无法生成有效报告。

------

## 九、针对 ResearchClawBench 的演化重点

由于评分器主要读取最终报告和生成图片，OpenEvo 的 `agent_system` 应优先优化科研工作流质量：

### 9.1 科研计划

要求代理在实验前完成：

- 数据文件审计；
  -研究问题拆解；
  -候选假设；
  -基线和对照实验；
  -评价指标；
  -验证和消融计划；
  -失败回退策略。

### 9.2 证据链

报告中的每个主要结论应能追溯到：

```text
结论
→ 图或表
→ 中间结果文件
→ 可执行代码
→ 原始数据
```

### 9.3 图像策略

建议只保留 3–5 张高信息密度图片：

1. 数据概览；
   2.主要结果；
   3.基线比较；
   4.验证或消融；
   5.误差、鲁棒性或失败分析。

所有关键图片放在 `report/images/`，使用清晰、稳定、可预测的文件名。

### 9.4 报告自检

完成前检查：

- 是否报告定量指标；
  -是否包含基线；
  -是否解释误差来源；
  -是否区分观察与推断；
  -是否存在无证据结论；
  -图片坐标、单位和图例是否完整；
  -报告中的数值是否能从输出文件重算；
  -方法和结果是否与任务目标一致。

这些通用能力比学习某个具体任务的目标答案更适合作为 OpenEvo 的演化对象。

------

## 十、实施优先级

| 优先级 | 工作                          | 验收条件                                     |
| ------ | ----------------------------- | -------------------------------------------- |
| P0     | OpenEvo agent entrypoint      | 能接收 `<PROMPT>` 和 `<WORKSPACE>`           |
| P0     | artifact validator            | 缺报告或图片时返回非零退出码                 |
| P0     | evaluator 隔离                | candidate 看不到 `target_study` 和 judge key |
| P0     | 单任务 Web UI canary          | 生成标准 run folder 并可官方评分             |
| P1     | OpenEvo batch runner          | 可按 YAML 批量运行并复用官方 scorer          |
| P1     | feedback filter               | reflector 只能收到合法反馈字段               |
| P1     | 社区任务 Dev/Validation split | 官方 40 题保持未使用                         |
| P1     | Baseline/treatment 配置锁定   | 除 `agent_system` 外条件一致                 |
| P2     | submission packager           | 自动生成 manifest 和完整压缩包               |
| P2     | Pass@5                        | 仅对冻结最终候选执行                         |
| P2     | 上游 agent preset PR          | 完成可复现结果后提交                         |

------

## 十一、最终建议

最稳妥的路线是：

1. 固定一份官方 ResearchClawBench clone；
2. 在 `agents.json` 添加 OpenEvo wrapper；
3. 用两个社区任务完成标准 workspace smoke test；
4. 建立真实文件系统隔离；
5. 在社区任务上进行 `agent_system` 演化；
6. 冻结最终 artifact；
7. 在官方 40 题上分别运行 GPT-5.5 + Codex baseline 和 OpenEvo treatment；
8. 使用官方 scorer 评分；
9. 检查异常值和运行失败，但不依据测试结果修改系统；
10. 打包所有完整 run 文件夹和实验 manifest，发送给维护者。

当前适配的最大技术难点集中在长程 Codex 会话稳定性、科研产物校验、隐藏目标隔离和演化反馈合法性。ResearchClawBench 自身接口较简单，接入 `agents.json` 的代码工作量有限；正式实验是否可信，主要取决于 evaluator/candidate 边界和冻结测试协议。