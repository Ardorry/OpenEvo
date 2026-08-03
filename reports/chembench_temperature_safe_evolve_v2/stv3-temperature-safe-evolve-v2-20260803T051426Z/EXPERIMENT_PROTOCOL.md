# ChemBench Temperature Safe-Evolve V2 正式协议

本目录的 `temperature_safe_evolve_v2.yaml`、`DESIGN.md` 与源码中的 closed validators 共同构成
正式协议。实验分类固定为
`reused_official_temperature_pool_independent_state_mechanism_experiment`，不得表述为历史
never-exposed 或外部独立确认。

固定执行顺序为：零模型调用 preflight；冻结 C4 八臂消融；按预注册规则选择单一
`text_memory` 或 `skill_bundle`；若有合格目标则运行 R0；只有 R0 continuation gate 通过才执行
冻结的 R1-R3；最后执行 aggregate-only 统计、审计、报告与 checksum closure。

停止状态、八臂选择规则、四折设计、forward promotion、entry retirement、V2 deployment、R0
continuation、最终分类、调用预算、context bytes 与十分钟监控策略均以 YAML 的精确值为准。正式
源码 commit 产生后，任何推理语义字段都不得修改；需要修改时封存受影响 run 并使用新 run ID。
