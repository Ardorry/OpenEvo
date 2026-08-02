"""Aggregate-only hard-stop report package for Temperature full-evolve v1."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.temperature_full_evolve_v1.capacity_preflight import (
    FINDING_CODE,
    PROTOCOL_ID,
    build_capacity_preflight,
)

REPORT_SCHEMA = "TemperatureFullEvolveBlockedReportPackageV1"


def build_managed_runtime_receipt(
    *,
    candidate_identity: Mapping[str, object],
    reflector_identity: Mapping[str, object],
    framework_lock_present: bool,
    runtime_services_receipt_present: bool,
    disk_total_bytes: int,
    disk_free_bytes: int,
) -> dict[str, object]:
    """Project verified runtime DTOs into an aggregate, path-free public receipt."""

    required = {
        "source",
        "codex_cli_version",
        "npm_package",
        "executable_sha256",
        "receipt_sha256",
    }
    if not required <= set(candidate_identity) or not required <= set(reflector_identity):
        raise ValueError("managed runtime public identity is incomplete")
    if "image_id" not in candidate_identity or "image_authority" not in candidate_identity:
        raise ValueError("candidate managed image identity is incomplete")
    return {
        "schema_version": "TemperatureFullEvolveManagedRuntimeReceiptV1",
        "status": "PASS_STATIC_IDENTITY_NO_FORMAL_SERVICE_STARTED",
        "candidate_source": candidate_identity["source"],
        "candidate_image_id": candidate_identity["image_id"],
        "candidate_image_authority": candidate_identity["image_authority"],
        "candidate_codex_cli_version": candidate_identity["codex_cli_version"],
        "candidate_npm_package": candidate_identity["npm_package"],
        "candidate_executable_sha256": candidate_identity["executable_sha256"],
        "candidate_receipt_sha256": candidate_identity["receipt_sha256"],
        "reflector_source": reflector_identity["source"],
        "reflector_codex_cli_version": reflector_identity["codex_cli_version"],
        "reflector_npm_package": reflector_identity["npm_package"],
        "reflector_executable_sha256": reflector_identity["executable_sha256"],
        "reflector_receipt_sha256": reflector_identity["receipt_sha256"],
        "candidate_reflector_executable_equal": (
            candidate_identity["executable_sha256"] == reflector_identity["executable_sha256"]
        ),
        "framework_lock_present": framework_lock_present,
        "runtime_services_current_receipt_present": runtime_services_receipt_present,
        "runtime_services_started_for_this_audit": False,
        "credential_content_read": False,
        "disk_total_bytes": disk_total_bytes,
        "disk_free_bytes": disk_free_bytes,
    }


def write_blocked_report_package(
    *,
    repository_root: Path,
    destination: Path,
    audit_id: str,
    generated_at_utc: str,
    source_code_commit: str,
    branch: str,
    model_identity: Mapping[str, object],
    managed_runtime_identity: Mapping[str, object],
    preflight_tool_failures: int = 0,
    preflight_tool_repairs: int = 0,
    regression_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Write the closed aggregate package after the capacity gate blocks launch."""

    repository = repository_root.resolve(strict=True)
    receipt = build_capacity_preflight(repository)
    if receipt["status"] != "BLOCKED" or receipt["finding_code"] != FINDING_CODE:
        raise RuntimeError("BLOCKED_REPORT_REQUIRES_FAILED_CAPACITY_GATE")
    if receipt["side_effects"]["total_model_calls"] != 0:
        raise RuntimeError("BLOCKED_REPORT_REQUIRES_ZERO_MODEL_CALLS")
    if (
        isinstance(preflight_tool_failures, bool)
        or not isinstance(preflight_tool_failures, int)
        or preflight_tool_failures < 0
        or isinstance(preflight_tool_repairs, bool)
        or not isinstance(preflight_tool_repairs, int)
        or preflight_tool_repairs < 0
    ):
        raise ValueError("preflight tool failure and repair counts must be non-negative")
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True, mode=0o755)

    protocol_source = (
        repository
        / "benchmarks/chembench/configs/temperature_full_evolve_v1/EXPERIMENT_PROTOCOL.md"
    ).read_bytes()
    write_public_file(destination / "EXPERIMENT_PROTOCOL.md", protocol_source)

    historical = receipt["historical_exposure"]
    capacity = receipt["capacity_gate"]
    partition = receipt["v2_partition_crosscheck"]
    near_duplicate = receipt["near_duplicate_precheck"]

    historical_manifest = {
        "schema_version": "TemperatureHistoricalExclusionAggregateV1",
        "status": "CLOSED_FOR_CAPACITY_HARD_STOP",
        "category": "Temperature_Prediction",
        "taxonomy_actual_labels": historical["taxonomy_actual_labels"],
        "prior_snapshot_actual_count": historical["prior_snapshot_actual_count"],
        "current_actual_exposed_union_count": historical["actual_exposed_union_count"],
        "actual_exposed_uid_set_sha256": historical["actual_exposed_uid_set_sha256"],
        "maximum_remaining_count": capacity[
            "maximum_provable_never_exposed_count_before_near_duplicate_grouping"
        ],
        "maximum_remaining_uid_set_sha256": capacity[
            "maximum_provable_never_exposed_uid_set_sha256"
        ],
        "event_scan": historical["event_scan"],
        "v2_partition_crosscheck": partition,
        "contains_uid_values": False,
        "contains_private_item_content": False,
    }
    _write_json(destination / "historical_exclusion_manifest.json", historical_manifest)

    split_manifest = {
        "schema_version": "TemperatureFullEvolveSplitManifestV1",
        "status": "NOT_CREATED",
        "finding_code": FINDING_CODE,
        "train_count": 0,
        "test_count": 0,
        "train_uid_order_sha256": None,
        "test_uid_order_sha256": None,
        "split_sha256": None,
        "reason": "The capacity gate failed before deterministic split generation.",
    }
    _write_json(destination / "split_manifest.json", split_manifest)

    duplicate_manifest = {
        "schema_version": "TemperatureNearDuplicateGroupAggregateV1",
        "status": "NOT_USED_FOR_SPLIT",
        "finding_code": FINDING_CODE,
        **near_duplicate,
        "contains_group_members": False,
    }
    _write_json(destination / "near_duplicate_group_manifest.json", duplicate_manifest)

    config_manifest = {
        "schema_version": "TemperatureFullEvolveConfigIntentV1",
        "status": "NOT_FROZEN_FOR_FORMAL_RUN",
        "protocol_id": PROTOCOL_ID,
        "audit_id": audit_id,
        "source_code_commit": source_code_commit,
        "branch": branch,
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "batch_size": 25,
        "targets": ["text_memory", "skill_bundle", "agent_system"],
        "target_context_budget_bytes": 6144,
        "hard_context_budget_bytes": 8192,
        "candidate_tool_policy": "zero_tool_transcript_audit",
        "feedback_on_test": False,
        "model_calls_before_gate": 0,
        "finding_code": FINDING_CODE,
    }
    _write_json(destination / "config_manifest.json", config_manifest)

    _write_json(destination / "model_identity_receipt.json", dict(model_identity))
    _write_json(destination / "managed_runtime_receipt.json", dict(managed_runtime_identity))
    _write_json(destination / "capacity_preflight_receipt.json", receipt)
    public_regression_summary = (
        dict(regression_summary)
        if regression_summary is not None
        else {
            "schema_version": "TemperatureFullEvolveRegressionSummaryV1",
            "status": "NOT_EXECUTED",
        }
    )
    _write_json(destination / "TEST_REPORT.json", public_regression_summary)

    not_run = {
        "status": "NOT_RUN",
        "finding_code": FINDING_CODE,
        "N": 0,
        "accepted_completion_count": 0,
        "parser_success_count": 0,
        "model_call_count": 0,
        "reflector_call_count": 0,
        "core_job_count": 0,
    }
    _write_json(destination / "evolved_test_metrics.json", not_run)
    _write_json(destination / "baseline_test_metrics.json", not_run)
    _write_json(
        destination / "paired_test_comparison.json",
        {
            **not_run,
            "baseline_correct": None,
            "evolved_correct": None,
            "paired_delta_percentage_points": None,
            "both_correct": None,
            "baseline_only_correct": None,
            "evolved_only_correct": None,
            "both_wrong": None,
            "mcnemar_exact_p": None,
            "paired_95_percent_ci": None,
        },
    )
    _write_json(
        destination / "call_and_failure_summary.json",
        {
            "schema_version": "TemperatureFullEvolveCallSummaryV1",
            "status": "ZERO_CALL_HARD_STOP",
            "candidate_calls": 0,
            "reflector_calls": 0,
            "core_jobs": 0,
            "baseline_calls": 0,
            "failed_attempts": 0,
            "retries": 0,
            "recoveries": 0,
            "preflight_tool_failures": preflight_tool_failures,
            "preflight_tool_repairs": preflight_tool_repairs,
            "preflight_tool_failure_model_side_effects": 0,
        },
    )

    _write_csv(
        destination / "train_batch_metrics.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "pre_correct",
            "post_correct",
            "delta_percentage_points",
            "mcnemar_exact_p",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )
    _write_csv(
        destination / "train_transition_metrics.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "both_correct",
            "pre_only_correct",
            "post_only_correct",
            "both_wrong",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )
    _write_csv(
        destination / "artifact_size_by_batch.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "text_memory_bytes",
            "skill_bundle_bytes",
            "agent_system_bytes",
            "combined_injected_bytes",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )
    _write_csv(
        destination / "rule_evidence_summary.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "active_rules",
            "supported_rules",
            "conflicted_rules",
            "retired_rules",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )

    implementation_audit = _implementation_audit_payload()
    _write_json(destination / "IMPLEMENTATION_AUDIT.json", implementation_audit)
    _write_json(
        destination / "chart_inventory.json",
        {
            "schema_version": "TemperatureFullEvolveChartInventoryV1",
            "generated": ["capacity_gate.svg"],
            "not_generated": [
                "train_batch_pre_post_accuracy",
                "train_cumulative_pre_post_curve",
                "train_correctness_transitions",
                "artifact_size_curve",
                "test_baseline_vs_evolved_accuracy",
                "test_paired_flips",
                "call_and_failure_chart",
            ],
            "reason": FINDING_CODE,
            "fabricated_zero-valued_result_charts": False,
        },
    )
    write_public_file(destination / "capacity_gate.svg", _capacity_svg(receipt).encode("utf-8"))

    precheck_markdown = _precheck_markdown(receipt, audit_id, generated_at_utc)
    final_markdown = _final_report_markdown(
        receipt=receipt,
        implementation_audit=implementation_audit,
        audit_id=audit_id,
        generated_at_utc=generated_at_utc,
        source_code_commit=source_code_commit,
        branch=branch,
        model_identity=model_identity,
        managed_runtime_identity=managed_runtime_identity,
        preflight_tool_failures=preflight_tool_failures,
        preflight_tool_repairs=preflight_tool_repairs,
        regression_summary=public_regression_summary,
    )
    handoff_markdown = _handoff_markdown(
        receipt=receipt,
        audit_id=audit_id,
        source_code_commit=source_code_commit,
        branch=branch,
        preflight_tool_failures=preflight_tool_failures,
        preflight_tool_repairs=preflight_tool_repairs,
    )
    write_public_file(destination / "PRECHECK_REPORT.md", precheck_markdown.encode("utf-8"))
    write_public_file(destination / "FINAL_REPORT.md", final_markdown.encode("utf-8"))
    write_public_file(destination / "HANDOFF.md", handoff_markdown.encode("utf-8"))

    expected_files = tuple(
        sorted(
            {
                "EXPERIMENT_PROTOCOL.md",
                "PRECHECK_REPORT.md",
                "TEST_REPORT.json",
                "split_manifest.json",
                "historical_exclusion_manifest.json",
                "near_duplicate_group_manifest.json",
                "config_manifest.json",
                "model_identity_receipt.json",
                "managed_runtime_receipt.json",
                "capacity_preflight_receipt.json",
                "train_batch_metrics.csv",
                "train_transition_metrics.csv",
                "artifact_size_by_batch.csv",
                "rule_evidence_summary.csv",
                "evolved_test_metrics.json",
                "baseline_test_metrics.json",
                "paired_test_comparison.json",
                "call_and_failure_summary.json",
                "IMPLEMENTATION_AUDIT.json",
                "chart_inventory.json",
                "capacity_gate.svg",
                "FINAL_REPORT.md",
                "HANDOFF.md",
                "package_manifest.json",
            }
        )
    )
    package_manifest = {
        "schema_version": REPORT_SCHEMA,
        "audit_id": audit_id,
        "generated_at_utc": generated_at_utc,
        "status": "HARD_BLOCKED",
        "finding_code": FINDING_CODE,
        "source_code_commit": source_code_commit,
        "branch": branch,
        "formal_run_ids": [],
        "regression_status": public_regression_summary.get("status"),
        "expected_files_excluding_sha256sums": list(expected_files),
        "public_scope": "AGGREGATE_ONLY",
    }
    _write_json(destination / "package_manifest.json", package_manifest)

    actual_files = tuple(sorted(path.name for path in destination.iterdir() if path.is_file()))
    if actual_files != expected_files:
        raise RuntimeError("BLOCKED_REPORT_FILE_CLOSURE_INVALID")
    sums = "".join(
        f"{_file_sha256(destination / name)}  {name}\n" for name in expected_files
    ).encode("utf-8")
    write_public_file(destination / "SHA256SUMS.txt", sums)
    return {
        "schema_version": REPORT_SCHEMA,
        "status": "HARD_BLOCKED",
        "finding_code": FINDING_CODE,
        "audit_id": audit_id,
        "report_directory": destination.as_posix(),
        "source_code_commit": source_code_commit,
        "file_count": len(expected_files) + 1,
        "sha256sums_sha256": sha256_bytes(sums),
    }


def _implementation_audit_payload() -> dict[str, object]:
    return {
        "schema_version": "TemperatureFullEvolveImplementationAuditV1",
        "status": "NO_GO_IN_ADDITION_TO_DATA_GATE",
        "src_openevo_modification_required": False,
        "reusable": {
            "candidate_harness": "supervised_transfer_v2/executor.py",
            "core_planned_jobs_and_typed_artifacts": "supervised_transfer_v2/core.py",
            "reflector_schema_and_sandbox": "supervised_transfer_v2/reflector_boundary.py",
            "parser_and_evaluator": "chembench4k_evaluation.py",
            "paired_statistics": "paired_statistics_v2.py",
            "baseline_empty_context": "supervised_transfer_v3/composed_control_test_baseline.py",
        },
        "blocking_gaps_if_new_data_is_acquired": [
            "Current STV3 evolves per item instead of once per 25-item batch.",
            "Current formal V3 marks resume_allowed=false and lacks an exactly-once batch ledger.",
            "Reflector uses an attested managed CLI inside a Core job but not the formal TaskRequest-Rollout-Gateway-CodexHarness chain required here.",
            "Current artifact limits are materially above the requested combined 8 KiB hard limit and do not enforce cross-target responsibility separation.",
            "Current reporting can emit per-item CSV into the report root instead of a separate aggregate-only public package.",
            "No current runtime_services/current.json binds live services to this branch and commit.",
        ],
        "required_future_modules": [
            "closed Batch25 supervised packet",
            "canonical cumulative rule evidence index",
            "deterministic three-target projection and responsibility validator",
            "atomic batch state and exactly-once recovery ledger",
            "harness-backed Reflector provider",
            "combined serialized context budget validator",
            "600-second monitor and recovery supervisor",
            "aggregate-only paired reporting",
        ],
    }


def _precheck_markdown(receipt: Mapping[str, object], audit_id: str, generated_at_utc: str) -> str:
    dataset = receipt["dataset"]
    historical = receipt["historical_exposure"]
    capacity = receipt["capacity_gate"]
    return f"""# Temperature Full-Evolve v1 零模型预检

- 审计 ID：`{audit_id}`
- 生成时间：`{generated_at_utc}`
- 状态：**{receipt["finding_code"]}**
- 模型调用：**0**

冻结的 Temperature Test 池只有 {dataset["temperature_test_count"]} 题；当前权威历史暴露闭包
包含 {historical["actual_exposed_union_count"]} 题，因此在语义近重复分组前最多剩
{capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]} 题。协议要求至少
{capacity["minimum_required_total"]} 题以构造 100 Train + 100 Test，缺口为
{capacity["capacity_shortfall_before_near_duplicate_grouping"]} 题。

5 道 Dev 题会连同答案被官方 renderer 注入为类别内 demonstrations，不能作为 never-exposed
候选。本次没有创建 split、run root、数据库、artifact lineage、Core generation、tmux、凭据
暂存或模型调用。

标准化 question/options 的四种精确检查没有发现字面重复；但数据没有 reaction、paper、
template 或 source-group 标识。语义分组只可能进一步缩小候选上界，不能修复容量失败。
"""


def _final_report_markdown(
    *,
    receipt: Mapping[str, object],
    implementation_audit: Mapping[str, object],
    audit_id: str,
    generated_at_utc: str,
    source_code_commit: str,
    branch: str,
    model_identity: Mapping[str, object],
    managed_runtime_identity: Mapping[str, object],
    preflight_tool_failures: int,
    preflight_tool_repairs: int,
    regression_summary: Mapping[str, object],
) -> str:
    dataset = receipt["dataset"]
    historical = receipt["historical_exposure"]
    capacity = receipt["capacity_gate"]
    partition = receipt["v2_partition_crosscheck"]
    event_scan = historical["event_scan"]
    return f"""# ChemBench Temperature Full-Evolve v1 最终报告

## 最终判定

**HARD BLOCKED — `{FINDING_CODE}`**

这是零模型调用 preflight 的合规终态，不是半途退出。用户协议明确规定：少于 100 道从未暴露
的 Train 加 100 道从未暴露的 Test 时必须停止正式实验。

## 容量证据

| 证据 | 数量 |
|---|---:|
| 冻结 Temperature Test 全集 | {dataset["temperature_test_count"]} |
| Dev demonstrations，排除 | {dataset["temperature_dev_count"]} |
| 旧权威 exposure snapshot 已确认实际暴露 | {historical["prior_snapshot_actual_count"]} |
| 当前实际暴露并集 | {historical["actual_exposed_union_count"]} |
| 可证明的 never-exposed 最大上界 | {capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]} |
| 最低总需求 | {capacity["minimum_required_total"]} |
| 近重复分组前的缺口 | {capacity["capacity_shortfall_before_near_duplicate_grouping"]} |

72 份私有事件账本共 {event_scan["byte_count"]} bytes，inventory SHA-256 为
`{event_scan["inventory_sha256"]}`。全部记录成功解析，未知 Temperature UID 为零。当前暴露并集
在旧 v2 分区中为：Train {partition["exposed_union_by_partition"]["train"]}、Test
{partition["exposed_union_by_partition"]["test"]}、Reserve
{partition["exposed_union_by_partition"]["reserve"]}；剩余上界为：Train
{partition["remaining_by_partition"]["train"]}、Test
{partition["remaining_by_partition"]["test"]}、Reserve
{partition["remaining_by_partition"]["reserve"]}。

集合 digest 使用“唯一 UID 排序、LF 连接并保留末尾 LF 后计算 SHA-256”的固定算法，不披露
UID 明文：

- Temperature 全集：`{dataset["temperature_uid_set_sha256"]}`
- 实际暴露并集：`{historical["actual_exposed_uid_set_sha256"]}`
- 最大剩余集合：`{capacity["maximum_provable_never_exposed_uid_set_sha256"]}`

旧 tracked v2/v3 receipt 中“历史 Test 暴露为零”只是当时的快照，不能作为本次隔离证明。
当前 v1 账本与后来 v2 Test 相交 9 题、与 Reserve 相交 21 题；v3 账本已覆盖 Temperature 的
Train 50 与 Test 50 全部题目。

## 零副作用与身份审计

- 正式 run ID：无。
- Candidate、Reflector、baseline 及总模型调用：0。
- Core job、模型重试、正式 recovery：0。
- 报告工具故障/修复：{preflight_tool_failures}/{preflight_tool_repairs}，模型副作用为 0。
- Split SHA-256：未生成，因为容量门槛先失败。
- 审计源码 commit：`{source_code_commit}`，分支 `{branch}`。
- 模型配置：`{model_identity.get("model")}`，reasoning
  `{model_identity.get("reasoning_effort")}`。
- Managed native executable SHA-256：
  `{managed_runtime_identity.get("candidate_executable_sha256")}`。
- Managed candidate image：
  `{managed_runtime_identity.get("candidate_image_id")}`。
- 凭据检查只验证 owner/type/mode 元数据，没有读取或复制凭据内容。

## 测试审计

- 新协议聚焦测试：{regression_summary.get("focused_passed", "未执行")} passed，
  {regression_summary.get("focused_failed", "未执行")} failed。
- ChemBench 完整测试：{regression_summary.get("full_passed", "未执行")} passed，
  {regression_summary.get("full_failed", "未执行")} failed；另有
  {regression_summary.get("full_subtests_passed", "未执行")} subtests passed。
- 完整测试中的失败已在 `TEST_REPORT.json` 分类；没有一项来自新 preflight 的聚焦测试，且
  测试过程模型调用为 0。旧 source-manifest、legacy recovery scope 与缺失旧 checkout/private
  fixture 保持 fail closed，没有为追求全绿而改写历史证据。

## 代码审查

Candidate 与 baseline 可复用正式 `TaskRequest -> Rollout -> Gateway -> CodexHarness`
executor、同一 parser/evaluator、managed runtime identity、空上下文 baseline、Core planned
jobs、typed artifacts 和 paired statistics。本轮不需要修改 `src/openevo/**`。

但现有 STV3 不是本实验：它逐题更新，记录 `resume_allowed=false`，没有 batch-25 原子
exactly-once ledger，artifact 上限远高于本轮 8 KiB，报告路径还可能含逐题 CSV。现有
Reflector 虽由 managed Codex 与 Core planned job 承载，却没有经过用户要求的正式 Candidate
harness 调用链。新数据通过门槛后，必须在独立 benchmark package 中修复这些问题；实现审查共
列出 {len(implementation_audit["blocking_gaps_if_new_data_is_acquired"])} 项具体缺口。

## 为什么上一轮 baseline 反而更好

Train 同题从 38/50 升至 48/50 是监督后 replay 证据，不是独立泛化。Generation-zero baseline
已经达到 47/50，向上空间很小，少数 negative flips 就足以主导类别差值。上一轮逐题 updater
容易记录近期样本和局部相关性；三个 target 内容重叠、总上下文约 17.1 KiB，又会稀释原题信号。
在没有 forward unseen validation 的情况下固定使用最终累计状态，会把这些错误保留下来。
Parser 成功只代表输出可被评分，不代表化学答案正确。

因此，更合理的解释是过拟合与上下文干扰，而不是“演化天然有害”。

## 下一步设计建议

1. 获取并 pin 新的权威 Temperature 数据源：在现有 81 题上至少新增 119 道从未暴露题，并为
   source-group/语义近重复损耗预留余量；不得伪造或改写题目。
2. 在写 runner 或调用模型前，针对新 revision 重建历史暴露闭包，再冻结 group-aware split
   与 source closure。
3. 每 25 题只做一次 batch synthesis；使用 canonical evidence index、support/contradiction、
   条件化规则和确定性退役。不得把同题 Post 提升当作泛化。
4. 三目标严格分工：事实进 text memory，流程进 skill，长期行为进 agent system；按包含 framing
   的真实序列化大小执行 6 KiB 目标与 8 KiB 硬门。
5. 增加 forward-only Train 诊断：截至 batch i 学到的规则，先在 batch i+1 的反馈前评估，再
   用 Train 证据校准或退役；Test 永不用于选择。
6. 预注册独立的 Train validation slice，或获取足够额外数据单独留出；只用这部分 guard
   negative flips，仍固定在 Test 前冻结 Ck。
7. 让 Reflector synthesis 也经过正式 managed harness，同时保留 Core 对 planned job 的所有权；
   对每个 accepted completion 和 artifact commit 边界做 crash-injection 测试。
8. 在多个预注册 split 或更大的外部 holdout 上重复后，再讨论 full-evolve 是否有效；同时报告
   effect size、paired flips 和置信区间，不能只看一次 p 值。

## 未产生的实验指标

Train 曲线、artifact、evolved Test、baseline Test、paired delta、flips、McNemar p 和置信区间
均明确为 `NOT_RUN`。生成全零结果图会造成误导，因此只生成 `capacity_gate.svg`。

审计 ID：`{audit_id}`。生成时间：`{generated_at_utc}`。
"""


def _handoff_markdown(
    *,
    receipt: Mapping[str, object],
    audit_id: str,
    source_code_commit: str,
    branch: str,
    preflight_tool_failures: int,
    preflight_tool_repairs: int,
) -> str:
    capacity = receipt["capacity_gate"]
    return f"""# 交接说明

- 状态：`{FINDING_CODE}`
- 审计 ID：`{audit_id}`
- 正式 run ID：无
- 选定 Train/Test：0/0
- never-exposed 最大上界：{capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]}
- 最低总需求：{capacity["minimum_required_total"]}
- Split SHA-256：未生成
- 源码 commit：`{source_code_commit}`
- 分支：`{branch}`
- 模型调用 / Core job / 模型重试 / 正式 recovery：0 / 0 / 0 / 0
- 报告工具失败 / 修复：{preflight_tool_failures} / {preflight_tool_repairs}
- tmux attach/status/stop：不适用；未创建 session

解除阻塞需要 pin 新的权威数据 revision，至少增加 119 道从未暴露的 Temperature 题，并为
group isolation 保留余量。随后重新执行零模型门槛；不得降低 100+100 下限或导入旧 artifact。
"""


def _capacity_svg(receipt: Mapping[str, object]) -> str:
    dataset = receipt["dataset"]
    capacity = receipt["capacity_gate"]
    historical = receipt["historical_exposure"]
    total = int(dataset["temperature_test_count"])
    exposed = int(historical["actual_exposed_union_count"])
    remaining = int(
        capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]
    )
    scale = 650 / total
    exposed_width = round(exposed * scale, 2)
    remaining_width = round(remaining * scale, 2)
    required_x = round(150 + int(capacity["minimum_required_total"]) * scale, 2)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="420" viewBox="0 0 900 420">
  <rect width="900" height="420" fill="#ffffff"/>
  <text x="40" y="46" font-family="sans-serif" font-size="24" font-weight="700">Temperature capacity gate</text>
  <text x="40" y="78" font-family="sans-serif" font-size="15" fill="#374151">Aggregate-only preflight; no model calls</text>
  <text x="40" y="145" font-family="sans-serif" font-size="16">Frozen pool</text>
  <rect x="150" y="120" width="650" height="32" rx="4" fill="#d1d5db"/>
  <text x="810" y="143" font-family="sans-serif" font-size="15">{total}</text>
  <text x="40" y="215" font-family="sans-serif" font-size="16">Exposed</text>
  <rect x="150" y="190" width="{exposed_width}" height="32" rx="4" fill="#dc2626"/>
  <text x="{160 + exposed_width}" y="213" font-family="sans-serif" font-size="15">{exposed}</text>
  <text x="40" y="285" font-family="sans-serif" font-size="16">Eligible upper bound</text>
  <rect x="150" y="260" width="{remaining_width}" height="32" rx="4" fill="#2563eb"/>
  <text x="{160 + remaining_width}" y="283" font-family="sans-serif" font-size="15">{remaining}</text>
  <line x1="{required_x}" y1="245" x2="{required_x}" y2="315" stroke="#111827" stroke-width="3"/>
  <text x="{required_x - 35}" y="338" font-family="sans-serif" font-size="14">required 200</text>
  <text x="40" y="390" font-family="sans-serif" font-size="16" font-weight="700" fill="#991b1b">BLOCKED: shortfall {capacity["capacity_shortfall_before_near_duplicate_grouping"]} before near-duplicate grouping</text>
</svg>
"""


def _write_json(path: Path, payload: object) -> None:
    write_public_file(path, canonical_pretty_json_bytes(payload))


def _write_csv(path: Path, header: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    write_public_file(path, buffer.getvalue().encode("utf-8"))


def _file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


__all__ = [
    "REPORT_SCHEMA",
    "build_managed_runtime_receipt",
    "write_blocked_report_package",
]
