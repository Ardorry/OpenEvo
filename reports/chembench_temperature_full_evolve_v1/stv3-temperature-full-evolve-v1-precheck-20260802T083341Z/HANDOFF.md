# 交接说明

- 状态：`INSUFFICIENT_NEVER_EXPOSED_TEMPERATURE_DATA`
- 审计 ID：`stv3-temperature-full-evolve-v1-precheck-20260802T083341Z`
- 正式 run ID：无
- 选定 Train/Test：0/0
- never-exposed 最大上界：81
- 最低总需求：200
- Split SHA-256：未生成
- 源码 commit：`273d0be384abe0075eef92f2a897c045259c328f`
- 分支：`chembench-temperature-full-evolve-v1`
- 模型调用 / Core job / 模型重试 / 正式 recovery：0 / 0 / 0 / 0
- 报告工具失败 / 修复：1 / 1
- tmux attach/status/stop：不适用；未创建 session

解除阻塞需要 pin 新的权威数据 revision，至少增加 119 道从未暴露的 Temperature 题，并为
group isolation 保留余量。随后重新执行零模型门槛；不得降低 100+100 下限或导入旧 artifact。
