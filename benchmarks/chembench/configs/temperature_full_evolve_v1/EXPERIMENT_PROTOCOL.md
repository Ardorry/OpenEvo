# ChemBench Temperature Full-Evolve v1 实验协议

## 状态与执行边界

这是一个全新的、仅针对 `Temperature_Prediction` 的探索性监督迁移协议。只有零模型调用的
容量与历史暴露门槛证明至少存在 100 道从未暴露的 Train 和 100 道从未暴露的 Test，才允许
创建正式 run。该门槛先于 split、run root、服务、凭据暂存、tmux，以及任何 Candidate、
Reflector、Core 或 baseline 模型副作用。

当前冻结数据未通过这一门槛。因此本分支有意只实现并执行 fail-closed preflight，不实现或
启动正式 runner。这是协议规定的终态，不是未完成的实验。

## 将来新数据修订通过门槛后的目标协议

- 使用全新的 generation-zero namespace，不导入历史 completion、数据库、workspace、缓存或
  artifact。
- 按 200、175、150、125、100 的顺序选择可行的最大等量 Train/Test 档位。
- 在任何模型调用前冻结确定性的 group-aware split。
- Train 按固定顺序每 25 题一个批次：25 次 pre-evolve Candidate、一次逻辑 Reflector
  synthesis、三个 Core target job、25 次 post-evolve 诊断。Post 结果不触发第二次演化。
- 保存结构化累计 rule/evidence index：事实只投影到 text memory，可执行流程只投影到
  skill，稳定行为和输出纪律只投影到 agent system。
- 按真实序列化注入字节执行 6 KiB 目标与 8 KiB 硬上限，并分别校验三个 target；禁止按字节
  截断不完整结构。
- 保留 C0 至 Ck 的全部 checkpoint；Test 前冻结 Ck，先跑 evolved Test，再用同一题序和推理栈
  跑隔离的 generation-zero baseline。
- Candidate、Reflector、evolved Test 与 baseline 都经过 OpenEvo 正式 managed harness；Test
  阶段 feedback、Reflector、Core job 和 artifact update 均为零。
- 逐题证据只保存在 owner-private run state；公开报告只含聚合指标和 digest。

## 将来正式启动前必须补齐的实现

现有 STV3 是逐题演化，不能静默冒充 batch 协议。未来通过数据门槛的分支必须在 benchmark
层新增 closed Batch25 packet、canonical rule evidence、确定性的三目标投影、原子 batch
ledger、跨 Candidate/Core 边界的 exactly-once recovery、harness-backed Reflector、8 KiB
组合预算、600 秒监控，以及 aggregate-only paired reporting。

本次审查未发现必须修改 `src/openevo/**` 的理由。所有历史 run tree 保持不可变、只读。
