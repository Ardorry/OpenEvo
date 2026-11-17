# Terminal-Bench 2.1 官方提交流程核对

核对日期：2026-11-09。当前仅整理材料、上传作者自己的 GitHub；没有向官方开 PR、发送消息或上传 Harbor Hub。

## 当前入口

[官方 SUBMIT.md](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/leaderboard/SUBMIT.md) 仍声明暂停社区提交，目前只将维护者运行的结果加入榜单。因此，下面是官方技术流程说明，不表示现在直接发社区 PR 就能入榜。首先需要维护者确认接收和运行本 harness 的方式。

## 必备材料与执行条件

- 使用官方固定数据集及不可变 digest；本包保存的 digest 与当前官方 `core/hub.py` 一致。
- 所有 89 道题，每题至少五次执行。异常记录按零分计算，保留在分母中。
- 使用默认预算与资源；不能增加 agent/verifier 时限或资源。CI 会同时核对 job 和逐 trial 配置。
- Harbor Hub 上的 job、trial 必须公开可读；每条成功记录必须有标准 ATIF `trajectory_path`。
- 完整的 agent、model、组织、网址、日期和 reasoning effort 展示元数据。
- 提交工具使用 `uv`、已认证的 `gh` 和官方仓库的 fork clone；进入 `leaderboard/` 目录操作。

规则和数量常量见 [static_analysis.py](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/leaderboard/src/leaderboard/ci/static_analysis.py)。本包本地已有 89 × 5 条选定记录及 394 条可解析的成功 ATIF，但尚无 Hub 公开链接。

## 官方命令与审核顺序

以下为将来的操作示例，本次没有执行。先将获认可的运行通过 Harbor 上传至 Hub，再依次运行：

```sh
# 在官方仓库的 leaderboard/ 目录内
uv run lb filter https://hub.harborframework.com/jobs/<real-job-uuid>
uv run lb metadata <submission-file.json>
uv run lb open-prs <submission-file.json>
```

`filter` 按 agent、版本、模型和 effort 生成提交文件，同一配置的多个来源 job 可以合并；`metadata` 补齐展示字段；`open-prs` 为提交建立分支并向官方开 PR。`lb submit` 会合并执行这些步骤，不能把它当作仅本地校验的命令。

PR 的静态检查通过后，CI 将记录复制为榜单持有的版本并创建 bot PR，原 PR 随之关闭。维护者在 bot PR 上运行 `/judge` 审核成功轨迹；处理 harness 问题及 reward hacking 判定，再以 `/apply` 更新剔除记录和成绩，最后合并入榜。`/check` 会从 Hub 重取数据复核。

## 我们需要先确认的三件事

1. **固定 AGENTS 的来源。** 九份文件来自同题此前的执行轨迹和判分反馈。官方 [judge 指令](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/leaderboard/src/leaderboard/ci/analysis-task-template/instruction.md#harness_level_cheating) 会审核预加载任务专属提示和旧复盘；未获维护者确认前，不能认定这种方案可获接收。应提供真实来源和文件供其判断。
2. **所选五轮的来源范围。** 展示集合在执行后选定，真实 trial UUID 已保留。[submission_trials](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/leaderboard/src/leaderboard/core/hub.py) 从 source_jobs 拉取全部匹配记录；它不接受本地 CSV 的任意 trial 白名单。因此，直接提交来源 job 不等于提交本报告的 445 条。需要维护者认可的记录导入或选集处理方式，不能修改历史配置或伪造新运行。
3. **公开证据与元数据。** 还需审核原始轨迹的具体公开内容、取得真实 Hub 链接，并核对 source_filter 与上传后 job/agent/model 元数据完全一致；由官方工具重算分数。

当前 88.54% 是本地所选集合的研究统计。官方审核可能改变有效成绩；本仓库不将其称为已接收的榜单分数。

## 本 GitHub 上传的范围

代码、冻结 AGENTS、逐题结果表、依赖版本和复现配置上传至作者的 OpenEvo 仓库独立分支。这里只保存研究复现快照和维护者 benchmark automation，不引入普通用户产品入口，也不更改 Core/Desktop 的运行行为。原始 session、认证文件、运行二进制、本地 CA 和完整题库留在仓库外。
