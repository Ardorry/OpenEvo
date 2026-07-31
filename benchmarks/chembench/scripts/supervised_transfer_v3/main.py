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
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
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
    else:
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
    payload["config_sha256"] = config.digest
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
