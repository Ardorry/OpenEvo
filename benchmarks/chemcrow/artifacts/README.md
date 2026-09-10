# ChemCrow 进化产物

本目录公开两套已封存实验的真实产物，共 84 份 Markdown：每套包含 14 个任务，
每题各有一份 `text_memory`、`skill_bundle` 和 `agent_system`。

| 集合 | 生成方式 | 产物数 | 入口 |
| --- | --- | ---: | --- |
| full-v8，2026-08-30 | 完整 Core Reflector，`core_full_worker_v1` | 42 | [逐任务文件索引](full_v8_20260830/README.md) |
| full-v5 replication，run 创建于 2026-08-29 | Core role wrapper，`core_role_wrapper_v1` | 42 | [逐任务文件索引](full_v5_replication_20260830/README.md) |

full-v8 产物由 G1 生成阶段封存，并由随后完成的 full-v8 G2 continuation 使用。
full-v5 replication 是另一套独立历史实验；不能将两套产物混成一次运行的 84 个产物，
也不能把目录日期理解为新的模型运行日期。

## 文件与核验

每个任务目录提供 `memory.md`、`skill_bundle/SKILL.md` 和 `agent_system.md`。
原生 skill bundle 均只有一个 `SKILL.md`，没有遗漏辅助文件。
Agent-system 使用便于浏览的归档文件名，其正文保持不变。

本次从已封存语义审计的 `content` 字段导出 UTF-8 原文，未改写、脱敏、加前言或重新生成。
84/84 份文件的字节数与 SHA-256 均匹配审计记录、注册哈希和现存原生 payload；
总正文大小为 226,632 字节。来源审计、run ID、artifact/job ID 及逐文件哈希见
[manifest.json](manifest.json)。
正文保留原有 Markdown 行尾换行空格；目录内的 Git attributes 仅对这些原文禁用
行尾规范化和行尾空格检查，以保持注册时的字节哈希，其他说明文件照常检查。

在本目录执行以下命令可检查全部正文文件：

```bash
sha256sum -c SHA256SUMS
```

[SHA256SUMS](SHA256SUMS) 只校验产物正文；来源审计的完整 JSON、Judge 请求、
轨迹、运行数据库和凭据未随此目录发布。产物正文可能包含任务背景及演化中形成的规则，
不是已验证可用于其他任务的通用规则，也不能代替完整运行账本。
文件按实验数据归档；本次发布没有执行这些指令、重新调用模型或改变历史成绩。

[返回代码与结果导航](../PUBLICATION.md) ·
[full-v8 评分汇总](../reports/full_v8_public_20260831/README.md)
