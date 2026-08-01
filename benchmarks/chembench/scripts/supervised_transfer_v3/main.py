#!/usr/bin/env python3
"""ChemBench config-bound evolved-only supervised transfer v3 entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    require_paid_runtime_python_v2,
)
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    runtime_services_status_v2,
    start_runtime_services_v2,
    stop_runtime_services_v2,
)
from openevo_chembench.supervised_transfer_v3.category_shard_recovery import (
    PARENT_RUN_ID,
    START_CATEGORY_INDEX,
    SupervisedTransferCategoryShardRecoveryV3,
    audit_parent_category_shards_v3,
    build_category_shard_recovery_dry_run_v3,
    build_source_compatibility_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.composed_final_test import (
    COMPOSITION_RUN_ID,
    ComposedFinalTestExperimentV3,
    audit_composed_train_shards_v3,
    build_composed_final_test_dry_run_v3,
    build_composed_source_compatibility_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.composed_final_test_recovery import (
    DEFAULT_PARENT_FINAL_TEST_RUN_ID,
    ComposedFinalTestSuffixRecoveryV3,
    audit_failed_final_test_prefix_v3,
    build_final_test_recovery_dry_run_v3,
    build_final_test_recovery_source_compatibility_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.continued_category_shard_recovery import (
    CONTINUED_PARENT_RUN_ID,
    CONTINUED_START_CATEGORY_INDEX,
    ContinuedCategoryShardRecoveryV3,
    audit_continued_parent_category_shards_v3,
    build_continued_category_recovery_dry_run_v3,
    build_continued_source_compatibility_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedTransferExperimentV3,
    build_complete_dry_run_v3,
    load_experiment_inputs_v3,
)
from openevo_chembench.supervised_transfer_v3.same_profile_category_recovery import (
    SAME_PROFILE_PARENT_RUN_ID,
    SAME_PROFILE_START_CATEGORY_INDEX,
    SameProfileCategoryShardRecoveryV3,
    audit_same_profile_parent_category_shards_v3,
    build_same_profile_category_recovery_dry_run_v3,
    build_same_profile_source_compatibility_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.source_identity import (
    verify_source_manifest_v3,
    write_source_manifest_v3,
)
from openevo_chembench.supervised_transfer_v3.split_reference import (
    verify_split_reference_receipt_v3,
    write_split_reference_receipt_v3,
)

DEFAULT_CONFIG = (
    PACKAGE_ROOT / "configs/supervised_transfer_v3/chembench_supervised_transfer_v3.yaml"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-id")
    parser.add_argument("--smoke-run-id")
    parser.add_argument("--service-run-id")
    parser.add_argument("--parent-run-id")
    parser.add_argument("--composition-run-id")
    parser.add_argument("--start-category-index", type=int)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "verify",
            "source-manifest",
            "dry-run",
            "services-start",
            "services-status",
            "services-stop",
            "smoke",
            "formal-online",
            "category-recovery-verify",
            "category-recovery-dry-run",
            "category-recover-online",
            "same-profile-category-recovery-verify",
            "same-profile-category-recovery-dry-run",
            "same-profile-category-recover-online",
            "continued-category-recovery-verify",
            "continued-category-recovery-dry-run",
            "continued-category-recover-online",
            "composed-final-test-verify",
            "composed-final-test-dry-run",
            "composed-final-test",
            "composed-final-test-recovery-verify",
            "composed-final-test-recovery-dry-run",
            "composed-final-test-recover",
        ),
    )
    arguments = parser.parse_args()
    config = load_config_v3(arguments.config.resolve(strict=True))
    if arguments.command == "prepare":
        payload = write_split_reference_receipt_v3(REPOSITORY_ROOT)
    elif arguments.command == "source-manifest":
        payload = write_source_manifest_v3(REPOSITORY_ROOT)
    elif arguments.command == "verify":
        payload = {
            "status": "PASS",
            "split": verify_split_reference_receipt_v3(REPOSITORY_ROOT),
            "source": verify_source_manifest_v3(REPOSITORY_ROOT),
        }
    elif arguments.command == "dry-run":
        inputs = load_experiment_inputs_v3(REPOSITORY_ROOT, config, require_runtime=False)
        payload = build_complete_dry_run_v3(inputs)
    elif arguments.command in {
        "category-recovery-verify",
        "category-recovery-dry-run",
    }:
        start_category_index = (
            START_CATEGORY_INDEX
            if arguments.start_category_index is None
            else arguments.start_category_index
        )
        if start_category_index != START_CATEGORY_INDEX:
            parser.error("category recovery requires --start-category-index 1")
        audit = audit_parent_category_shards_v3(
            repository_root=REPOSITORY_ROOT,
            parent_run_id=arguments.parent_run_id or PARENT_RUN_ID,
        )
        if arguments.command == "category-recovery-verify":
            payload = {
                "status": "PASS",
                "parent_run_id": audit.parent_run_id,
                "accepted_closed_shard_sha256": audit.accepted.digest,
                "discarded_partial_shard_sha256": audit.discarded.digest,
                "parent_tree_sha256": audit.parent_tree_sha256,
                "parent_artifacts_imported": False,
                "parent_database_imported": False,
                "first_category": "Property_Prediction",
                "first_task": 0,
                "generation_zero_targets": True,
                "model_calls": 0,
            }
        else:
            if not arguments.run_id:
                parser.error("category-recovery-dry-run requires --run-id")
            inputs = load_experiment_inputs_v3(
                REPOSITORY_ROOT,
                config,
                require_runtime=False,
            )
            payload = build_category_shard_recovery_dry_run_v3(
                inputs=inputs,
                audit=audit,
                recovery_run_id=arguments.run_id,
            )
    elif arguments.command in {
        "same-profile-category-recovery-verify",
        "same-profile-category-recovery-dry-run",
    }:
        start_category_index = (
            SAME_PROFILE_START_CATEGORY_INDEX
            if arguments.start_category_index is None
            else arguments.start_category_index
        )
        if start_category_index != SAME_PROFILE_START_CATEGORY_INDEX:
            parser.error("same-profile category recovery requires --start-category-index 5")
        audit = audit_same_profile_parent_category_shards_v3(
            repository_root=REPOSITORY_ROOT,
            parent_run_id=arguments.parent_run_id or SAME_PROFILE_PARENT_RUN_ID,
        )
        if arguments.command == "same-profile-category-recovery-verify":
            payload = {
                "status": "PASS",
                "parent_run_id": audit.parent_run_id,
                "accepted_closed_shards_sha256": audit.accepted.digest,
                "accepted_categories": [shard.category for shard in audit.accepted.shards],
                "discarded_partial_shard_sha256": audit.discarded.digest,
                "discarded_category": audit.discarded.category,
                "parent_tree_sha256": audit.parent_tree_sha256,
                "parent_artifacts_imported": False,
                "parent_database_imported": False,
                "first_category": "Retrosynthesis",
                "first_task": 0,
                "generation_zero_targets": True,
                "model_calls": 0,
            }
        else:
            if not arguments.run_id:
                parser.error("same-profile-category-recovery-dry-run requires --run-id")
            inputs = load_experiment_inputs_v3(
                REPOSITORY_ROOT,
                config,
                require_runtime=False,
            )
            payload = build_same_profile_category_recovery_dry_run_v3(
                inputs=inputs,
                audit=audit,
                recovery_run_id=arguments.run_id,
            )
    elif arguments.command in {
        "continued-category-recovery-verify",
        "continued-category-recovery-dry-run",
    }:
        start_category_index = (
            CONTINUED_START_CATEGORY_INDEX
            if arguments.start_category_index is None
            else arguments.start_category_index
        )
        audit = audit_continued_parent_category_shards_v3(
            repository_root=REPOSITORY_ROOT,
            parent_run_id=arguments.parent_run_id or CONTINUED_PARENT_RUN_ID,
            start_category_index=start_category_index,
        )
        if arguments.command == "continued-category-recovery-verify":
            payload = {
                "status": "PASS",
                "parent_run_id": audit.parent_run_id,
                "accepted_closed_shards_sha256": audit.accepted.digest,
                "accepted_categories": [shard.category for shard in audit.accepted.shards],
                "discarded_partial_shard_sha256": audit.discarded.digest,
                "discarded_category": audit.discarded.category,
                "parent_tree_sha256": audit.parent_tree_sha256,
                "parent_artifacts_imported": False,
                "parent_database_imported": False,
                "first_category": audit.discarded.category,
                "first_task": 0,
                "generation_zero_targets": True,
                "model_calls": 0,
            }
        else:
            if not arguments.run_id:
                parser.error("continued-category-recovery-dry-run requires --run-id")
            inputs = load_experiment_inputs_v3(
                REPOSITORY_ROOT,
                config,
                require_runtime=False,
            )
            payload = build_continued_category_recovery_dry_run_v3(
                inputs=inputs,
                audit=audit,
                recovery_run_id=arguments.run_id,
            )
    elif arguments.command == "services-start":
        if not arguments.service_run_id:
            parser.error("services-start requires --service-run-id")
        identity = start_runtime_services_v2(
            repository_root=REPOSITORY_ROOT,
            service_run_id=arguments.service_run_id,
        )
        payload = {
            "status": "PASS",
            "service_run_id": identity.service_run_id,
            "source_commit": identity.source_commit,
            "runtime_services_identity_sha256": identity.digest,
            "shared_verified_runtime_abi": "chembench_supervised_transfer_v2",
        }
    elif arguments.command == "services-status":
        payload = runtime_services_status_v2(repository_root=REPOSITORY_ROOT)
    elif arguments.command == "services-stop":
        payload = stop_runtime_services_v2(repository_root=REPOSITORY_ROOT)
    elif arguments.command in {"smoke", "formal-online"}:
        if not arguments.allow_paid:
            parser.error("smoke/formal-online requires --allow-paid")
        if not arguments.run_id:
            parser.error("smoke/formal-online requires --run-id")
        if arguments.command == "formal-online" and not arguments.smoke_run_id:
            parser.error("formal-online requires --smoke-run-id")
        if arguments.command == "smoke" and arguments.smoke_run_id:
            parser.error("smoke cannot consume --smoke-run-id")
        require_paid_runtime_python_v2(repository_root=REPOSITORY_ROOT)
        inputs = load_experiment_inputs_v3(REPOSITORY_ROOT, config, require_runtime=True)
        experiment = SupervisedTransferExperimentV3(
            inputs=inputs,
            run_id=arguments.run_id,
            run_mode=("smoke" if arguments.command == "smoke" else "formal_online"),
            smoke_run_id=arguments.smoke_run_id,
        )
        payload = (
            experiment.run_smoke()
            if arguments.command == "smoke"
            else experiment.run_formal_online()
        )
    elif arguments.command == "category-recover-online":
        if not arguments.allow_paid:
            parser.error("category-recover-online requires --allow-paid")
        if not arguments.run_id:
            parser.error("category-recover-online requires --run-id")
        start_category_index = (
            START_CATEGORY_INDEX
            if arguments.start_category_index is None
            else arguments.start_category_index
        )
        if start_category_index != START_CATEGORY_INDEX:
            parser.error("category recovery requires --start-category-index 1")
        require_paid_runtime_python_v2(repository_root=REPOSITORY_ROOT)
        audit = audit_parent_category_shards_v3(
            repository_root=REPOSITORY_ROOT,
            parent_run_id=arguments.parent_run_id or PARENT_RUN_ID,
        )
        compatibility = build_source_compatibility_receipt_v3(
            audit=audit,
            recovery_run_id=arguments.run_id,
        )
        if compatibility["semantically_compatible"] is not True:
            raise RuntimeError("RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE")
        inputs = load_experiment_inputs_v3(REPOSITORY_ROOT, config, require_runtime=True)
        experiment = SupervisedTransferCategoryShardRecoveryV3(
            inputs=inputs,
            run_id=arguments.run_id,
            parent_audit=audit,
        )
        write_path = (
            experiment.result_root / "public/category_shard_source_compatibility_receipt_v3.json"
        )
        write_public_file(write_path, canonical_pretty_json_bytes(compatibility))
        payload = experiment.run_category_shard_recovery()
    elif arguments.command == "same-profile-category-recover-online":
        if not arguments.allow_paid:
            parser.error("same-profile-category-recover-online requires --allow-paid")
        if not arguments.run_id:
            parser.error("same-profile-category-recover-online requires --run-id")
        if not arguments.smoke_run_id:
            parser.error("same-profile-category-recover-online requires --smoke-run-id")
        start_category_index = (
            SAME_PROFILE_START_CATEGORY_INDEX
            if arguments.start_category_index is None
            else arguments.start_category_index
        )
        if start_category_index != SAME_PROFILE_START_CATEGORY_INDEX:
            parser.error("same-profile category recovery requires --start-category-index 5")
        require_paid_runtime_python_v2(repository_root=REPOSITORY_ROOT)
        audit = audit_same_profile_parent_category_shards_v3(
            repository_root=REPOSITORY_ROOT,
            parent_run_id=arguments.parent_run_id or SAME_PROFILE_PARENT_RUN_ID,
        )
        compatibility = build_same_profile_source_compatibility_receipt_v3(
            audit=audit,
            recovery_run_id=arguments.run_id,
        )
        if compatibility["semantically_compatible"] is not True:
            raise RuntimeError("RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE")
        inputs = load_experiment_inputs_v3(
            REPOSITORY_ROOT,
            config,
            require_runtime=True,
        )
        experiment = SameProfileCategoryShardRecoveryV3(
            inputs=inputs,
            run_id=arguments.run_id,
            smoke_run_id=arguments.smoke_run_id,
            parent_audit=audit,
        )
        write_path = (
            experiment.result_root
            / "public/same_profile_category_shard_source_compatibility_receipt_v3.json"
        )
        write_public_file(write_path, canonical_pretty_json_bytes(compatibility))
        payload = experiment.run_category_shard_recovery()
    elif arguments.command == "continued-category-recover-online":
        if not arguments.allow_paid:
            parser.error("continued-category-recover-online requires --allow-paid")
        if not arguments.run_id:
            parser.error("continued-category-recover-online requires --run-id")
        if not arguments.smoke_run_id:
            parser.error("continued-category-recover-online requires --smoke-run-id")
        start_category_index = (
            CONTINUED_START_CATEGORY_INDEX
            if arguments.start_category_index is None
            else arguments.start_category_index
        )
        require_paid_runtime_python_v2(repository_root=REPOSITORY_ROOT)
        audit = audit_continued_parent_category_shards_v3(
            repository_root=REPOSITORY_ROOT,
            parent_run_id=arguments.parent_run_id or CONTINUED_PARENT_RUN_ID,
            start_category_index=start_category_index,
        )
        compatibility = build_continued_source_compatibility_receipt_v3(
            audit=audit,
            recovery_run_id=arguments.run_id,
        )
        if compatibility["semantically_compatible"] is not True:
            raise RuntimeError("RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE")
        inputs = load_experiment_inputs_v3(
            REPOSITORY_ROOT,
            config,
            require_runtime=True,
        )
        experiment = ContinuedCategoryShardRecoveryV3(
            inputs=inputs,
            run_id=arguments.run_id,
            smoke_run_id=arguments.smoke_run_id,
            parent_audit=audit,
        )
        write_path = (
            experiment.result_root
            / "public/continued_category_shard_source_compatibility_receipt_v3.json"
        )
        write_public_file(write_path, canonical_pretty_json_bytes(compatibility))
        payload = experiment.run_category_shard_recovery()
    elif arguments.command in {
        "composed-final-test-verify",
        "composed-final-test-dry-run",
    }:
        audit = audit_composed_train_shards_v3(
            repository_root=REPOSITORY_ROOT,
            composition_run_id=arguments.composition_run_id or COMPOSITION_RUN_ID,
        )
        inputs = load_experiment_inputs_v3(
            REPOSITORY_ROOT,
            config,
            require_runtime=False,
        )
        if arguments.command == "composed-final-test-verify":
            payload = {
                "status": "PASS",
                "composition_run_id": audit.composition_run_id,
                "composition_receipt_sha256": audit.composition_receipt_sha256,
                "composition_audit_sha256": audit.digest,
                "category_count": len(audit.categories),
                "target_count": len(audit.categories) * 3,
                "source_shards_opened_read_only": True,
                "source_artifacts_imported": False,
                "source_databases_imported": False,
                "model_calls": 0,
            }
        else:
            if not arguments.run_id:
                parser.error("composed-final-test-dry-run requires --run-id")
            payload = build_composed_final_test_dry_run_v3(
                inputs=inputs,
                audit=audit,
                run_id=arguments.run_id,
            )
    elif arguments.command in {
        "composed-final-test-recovery-verify",
        "composed-final-test-recovery-dry-run",
    }:
        audit = audit_composed_train_shards_v3(
            repository_root=REPOSITORY_ROOT,
            composition_run_id=arguments.composition_run_id or COMPOSITION_RUN_ID,
        )
        inputs = load_experiment_inputs_v3(
            REPOSITORY_ROOT,
            config,
            require_runtime=False,
        )
        prefix = audit_failed_final_test_prefix_v3(
            repository_root=REPOSITORY_ROOT,
            inputs=inputs,
            composition_audit=audit,
            parent_run_id=(
                arguments.parent_run_id or DEFAULT_PARENT_FINAL_TEST_RUN_ID
            ),
        )
        if arguments.command == "composed-final-test-recovery-verify":
            payload = {
                "status": "PASS",
                "parent_run_id": prefix.parent_run_id,
                "parent_prefix_receipt_sha256": prefix.accepted.digest,
                "discarded_incomplete_task_receipt_sha256": prefix.discarded.digest,
                "accepted_completion_count": prefix.accepted.completion_count,
                "start_task_ordinal": prefix.start_task_ordinal,
                "first_category": prefix.discarded.category,
                "first_category_task_ordinal": prefix.start_task_ordinal % 50,
                "parent_results_or_state_modified": False,
                "parent_ledger_modified": False,
                "model_calls": 0,
            }
        else:
            if not arguments.run_id:
                parser.error("composed-final-test-recovery-dry-run requires --run-id")
            payload = build_final_test_recovery_dry_run_v3(
                inputs=inputs,
                composition_audit=audit,
                prefix_audit=prefix,
                recovery_run_id=arguments.run_id,
            )
    elif arguments.command == "composed-final-test-recover":
        if not arguments.allow_paid:
            parser.error("composed-final-test-recover requires --allow-paid")
        if not arguments.run_id:
            parser.error("composed-final-test-recover requires --run-id")
        if not arguments.smoke_run_id:
            parser.error("composed-final-test-recover requires --smoke-run-id")
        require_paid_runtime_python_v2(repository_root=REPOSITORY_ROOT)
        audit = audit_composed_train_shards_v3(
            repository_root=REPOSITORY_ROOT,
            composition_run_id=arguments.composition_run_id or COMPOSITION_RUN_ID,
        )
        inputs = load_experiment_inputs_v3(
            REPOSITORY_ROOT,
            config,
            require_runtime=True,
        )
        prefix = audit_failed_final_test_prefix_v3(
            repository_root=REPOSITORY_ROOT,
            inputs=inputs,
            composition_audit=audit,
            parent_run_id=(
                arguments.parent_run_id or DEFAULT_PARENT_FINAL_TEST_RUN_ID
            ),
        )
        compatibility = build_final_test_recovery_source_compatibility_receipt_v3(
            inputs=inputs,
            composition_audit=audit,
            prefix_audit=prefix,
        )
        if compatibility["semantically_compatible"] is not True:
            raise RuntimeError("FINAL_TEST_RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE")
        experiment = ComposedFinalTestSuffixRecoveryV3(
            inputs=inputs,
            run_id=arguments.run_id,
            composition_audit=audit,
            prefix_audit=prefix,
            runtime_smoke_run_id=arguments.smoke_run_id,
        )
        write_public_file(
            experiment.result_root
            / "public/final_test_recovery_source_compatibility_receipt_v3.json",
            canonical_pretty_json_bytes(compatibility),
        )
        payload = experiment.run_final_test_recovery()
    else:
        if not arguments.allow_paid:
            parser.error("composed-final-test requires --allow-paid")
        if not arguments.run_id:
            parser.error("composed-final-test requires --run-id")
        if not arguments.smoke_run_id:
            parser.error("composed-final-test requires --smoke-run-id")
        require_paid_runtime_python_v2(repository_root=REPOSITORY_ROOT)
        audit = audit_composed_train_shards_v3(
            repository_root=REPOSITORY_ROOT,
            composition_run_id=arguments.composition_run_id or COMPOSITION_RUN_ID,
        )
        inputs = load_experiment_inputs_v3(
            REPOSITORY_ROOT,
            config,
            require_runtime=True,
        )
        compatibility = build_composed_source_compatibility_receipt_v3(
            inputs=inputs,
            audit=audit,
        )
        if compatibility["semantically_compatible"] is not True:
            raise RuntimeError("COMPOSED_TEST_SOURCE_NOT_COMPATIBLE")
        experiment = ComposedFinalTestExperimentV3(
            inputs=inputs,
            run_id=arguments.run_id,
            composition_audit=audit,
            runtime_smoke_run_id=arguments.smoke_run_id,
        )
        write_public_file(
            experiment.result_root
            / "public/composed_final_test_source_compatibility_receipt_v3.json",
            canonical_pretty_json_bytes(compatibility),
        )
        payload = experiment.run_composed_final_test()
    payload["config_sha256"] = config.digest
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
