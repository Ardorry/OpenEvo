# 给学长的指标与提交证据

## 两个统计范围

| 用途 | 数据范围 | 通过率 | 费用 | 平均每次执行 |
|---|---|---:|---:|---:|
| 完整来源记录／PR 使用范围 | 两个完整 job，共 623 条，89 题各七次 | 541/623 = **86.84%** | $128.16 | $0.2057 |
| 原五轮研究展示 | 执行后选定的 445 条，89 题各五次 | 394/445 = **88.54%** | $89.74 | $0.2017 |

这两个范围不能混用：官方工具按 source_jobs 读取全部匹配记录，不按本地展示清单任意删 trial。原始记录的判分、异常、配置和 UUID 没有改写。

## 文件

- `full-source-job-metrics.png` / `.pdf`：完整来源记录指标截图／PDF。
- `full-source-job-METRICS.md`、`full-source-job-metrics.json`、`full-source-job-metrics.csv`：该范围的可读报告及机器可读指标。
- `metrics.png` / `.pdf`、`METRICS.md`、`metrics.json`、`metrics.csv`：原五轮研究展示指标，明确标为非官方成绩。
- `all-source-trials.csv`：完整 623 条的 UUID、判分、异常、用量和耗时数据。
- `consult-issue-255*.png`：官方咨询 Issue 的实际网页截图，不能当作成绩 PR 或入榜证明。
- `github-submission-pr*.png`：官方仓库实际草稿 PR #256 截图；`github-ci-status*.png`：首次 CI 运行截图。
- `harbor-source-a*.png`、`harbor-source-b*.png`：两个已公开 Hub job 的实际页面。
- `submission-receipt.json`：真实 PR、Hub 链接及首次 CI 状态；`official-ci-log.txt`：官方公开运行日志。
- `task-digest-discrepancy.json`：官方 Git 镜像与 Registry 唯一不一致任务的逐文件比对及测试 AST 等价证据。

## 指标口径

主指标为通过 trial 数除以全部 trial 数。零分、异常和未判分保留在分母；Pass@k 是逐题的组合估计，不替代 Pass@1。标准误按核对的官方 per-task 公式计算，只描述当前记录集合，不能消除选集或实验设计偏差。

输入 token 已含缓存 token，total tokens = input + output，缓存不可重复相加。费用来自 harness 的逐次记录，不含前期进化和服务器成本；完整来源缺三条用量回执，五轮展示缺两条。

九份 AGENTS 来自同题旧轨迹和判分反馈。官方接收性与配置兼容性待审；本材料不证明通用进化因果收益，也不声称已经获得官方榜单认可。

## 已公开的入口

- 代码及冻结产物：https://github.com/Ardorry/OpenEvo/tree/bae1b900e043685d5ebc3a93dc6edd2ee5c9ddfd/benchmarks/terminal_bench/releases/tb21-sol-high-agent9-20261109
- 官方评估／协议咨询：https://github.com/harbor-framework/terminal-bench-2-1/issues/255
- 实际成绩草稿 PR：https://github.com/harbor-framework/terminal-bench-2-1/pull/256
- 完整来源 A（445 条）：https://hub.harborframework.com/jobs/862a2581-7850-46f9-b955-58aa3e8cc58f
- 完整来源 B（178 条）：https://hub.harborframework.com/jobs/1f474dd2-013a-48a4-ab8e-1f90289ff85c
- 首次官方 CI：https://github.com/harbor-framework/terminal-bench-2-1/actions/runs/43887991663

## 当前审核状态

Hub 已上传、逐条核验并公开。PR #256 为 Draft，官方已自动标为 lb-submission；尚未入榜、未运行官方 judge。

本地执行未经修改的官方静态检查：8 项通过、2 项失败。失败项为历史 import_path 自定义 agent 的名称兼容，以及 sanitize-git-repo 七条记录的任务 digest 不同。后者仅有 task 根目录 .gitignore 和五个测试假 token 的等价字符串写法差异；其他文件相同，折叠常量字符串后的完整测试 AST 相同。原始 config/ref/reward/日志未修改，兼容认可需要维护者处理。

首次官方 CI 另在读取 Hub job 时提前退出，未生成静态报告，随后空报告评论步骤失败。使用 CI 相同的 Harbor 0.20.0 在本机隔离复现，同一命令退出 0；匿名 job 头读取也成功。已在 PR 披露并请求维护者核查 CI 的 Harbor 访问环境；日志不足以断言其具体凭据或网络根因。

网页截图使用 Chromium 直接访问页面生成，不修改远程页面文字或成绩。对应 `.capture.json` 保存 URL、HTTP 状态、标题和拍摄时间。截图没有出现入榜条目，就不能称为入榜截图。
