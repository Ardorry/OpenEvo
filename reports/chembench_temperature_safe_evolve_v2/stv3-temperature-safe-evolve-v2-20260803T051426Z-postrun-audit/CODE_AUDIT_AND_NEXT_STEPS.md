# Safe-Evolve V2 代码审查与后续实现边界

## 已验证的边界

- Phase A 八臂全部走 `TaskRequest -> Rollout -> Gateway -> CodexHarness -> managed_science`。
- 800 个 accepted Candidate context 对应 800 个唯一 claim；没有 replacement completion。
- 全局 GT barrier 在八臂全部闭合后才释放并评分。
- Phase A 的 Reflector、Core job 和 artifact update 均为 0。
- A0–A7 使用同一模型、reasoning、parser、evaluator、timeout 和 retry 语义，仅 artifact 组合不同。
- 正式 stop rule 在 target selection 后终止，未创建任何 fold run、fold database 或 R0 模型调用。

## V1 退役根因

V1 schema 支持 `retire`，但状态转换完全依赖 Reflector 主动返回该 operation。Controller 会累计 support 和 counterexample，并把不足以投影的规则降为 provisional，但不会根据有害行为强制 retire。省略规则也不会删除旧规则。因此正式 C1–C4 中 0 retired 是机制决定，而不是日志遗漏。

## V2 退役覆盖审查

V2 已增加 mandatory retirement ID、closed-schema merge 校验、retired entry 不参与 Core projection/retrieval，以及 negative-flip utility、contradiction ratio、连续非正 block、stale-with-contradiction 四类 Controller trigger。

正式源码仍有一个未执行路径缺口：`retirement_reasons_v2` 定义了 `stronger_unconditional_conflict` 和 `schema_length_or_retrieval_safety`，但 `_mandatory_retirements` 的调用将这两个布尔值固定为 false。由于 Phase A no-go，R0 从未开始，该缺口没有影响本 campaign 的任何调用或结论；未来任何启用 fold evolution 的 commit 必须先补齐，并使用新 run ID。

建议实现方式：

1. 为 conflict 提供结构化、可绑定的 entry-to-entry relation，而不是依赖自由文本相似度。
2. Controller 只在 scope 相同、证据方向冲突且替代 entry 的支持更强、冲突更少时确认 stronger conflict。
3. 对单 entry 的 sparse-rendered bytes、target hard limit 和检索可达性做确定性预审；不可能安全注入的 entry 写入 mandatory retirement。
4. Reflector 必须退役全部 mandatory IDs；漏项、改写旧语义或伪造关系均拒绝。
5. 增加六类 trigger 的单元测试、merge 失败测试、retired entry 不可检索测试和 checkpoint recovery 测试。

## 已修复的运行问题

正式 A2 暴露了一个纯编排问题：claim 前的 `runtime.require_current()` 单次观察可能碰到短暂服务抖动。后运行补丁把重试限制在 durable claim 之前，并保持以下不变量：

- health 未恢复时不写 `CALL_CLAIMED`；
- 不调用 `submit_task`；
- 已 claim 后不走该重试；
- accepted completion 永不替换；
- 持续失败使用固定 finding code fail closed。

对应回归测试覆盖“先失败后恢复只提交一次”和“持续失败零 claim、零 submit”。正式推理仍绑定原 commit；补丁只供未来运行使用。

## 新实验准入条件

在启动新的 Safe-Evolve campaign 前，应要求：

- 六类 retirement trigger 均在 Controller 路径有测试和 receipt；
- selected target 的新 frozen ablation 在两个固定半集均不下降；
- source commit、runtime identity 和 config digest 重新冻结；
- 使用新 campaign/run IDs；
- 如果继续使用官方重复池，只能表述为 mechanism evidence；
- 外部确认必须使用未参与任何机制选择的新数据。
