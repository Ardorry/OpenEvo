#!/usr/bin/env python3
"""Generate or verify non-standard taskwise stream manifests without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.taskwise_sampling_v1 import (
    LEGACY_SINGLE_CHAIN_CLASSIFICATION,
    PILOT500_SCOPE,
    PILOT500_STREAM_SCOPES,
    generate_taskwise_manifests,
    legacy_single_chain_provenance_bytes,
    pilot500_stream_suite_summary_bytes,
    verify_taskwise_manifests,
)
from openevo_chembench.taskwise_config_v1 import (
    SOURCE_COMMIT_PLACEHOLDER,
    load_taskwise_config_v1,
    taskwise_arm_parity_findings,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
WORKSPACE_ROOT = REPOSITORY_ROOT.parent
DATASET_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)
PUBLIC_ROOT = PACKAGE_ROOT / "manifests" / "taskwise_online_v1"
PRIVATE_ROOT = PACKAGE_ROOT / "private_manifests" / "taskwise_online_v1"
PUBLIC_STREAM_ROOT = PUBLIC_ROOT / "streams"
PRIVATE_STREAM_ROOT = PRIVATE_ROOT / "streams"
LEGACY_PUBLIC_ROOT = PUBLIC_ROOT / "legacy_single_stream"
LEGACY_PRIVATE_ROOT = PRIVATE_ROOT / "legacy_single_stream"
STREAM_SUITE_SUMMARY = PUBLIC_ROOT / "online_pilot500_summary.json"
LEGACY_PROVENANCE = LEGACY_PUBLIC_ROOT / "provenance.json"


def _paths(scope: str) -> tuple[Path, Path, Path]:
    if scope == PILOT500_SCOPE:
        return (
            LEGACY_PUBLIC_ROOT / "online_pilot500_public_manifest.jsonl",
            LEGACY_PRIVATE_ROOT / "online_pilot500_private_manifest.jsonl",
            LEGACY_PUBLIC_ROOT / "online_pilot500_summary.json",
        )
    if scope in PILOT500_STREAM_SCOPES:
        stream_id = scope.removeprefix("pilot500_")
        return (
            PUBLIC_STREAM_ROOT / f"{stream_id}_public_manifest.jsonl",
            PRIVATE_STREAM_ROOT / f"{stream_id}_private_manifest.jsonl",
            PUBLIC_STREAM_ROOT / f"{stream_id}_summary.json",
        )
    return (
        PUBLIC_ROOT / f"online_{scope}_public_manifest.jsonl",
        PRIVATE_ROOT / f"online_{scope}_private_manifest.jsonl",
        PUBLIC_ROOT / f"online_{scope}_summary.json",
    )


def _validate_configs(scope: str) -> dict[str, object]:
    control = load_taskwise_config_v1(
        PACKAGE_ROOT / "configs" / f"control_{scope}_taskwise_online_v1.yaml"
    )
    online = load_taskwise_config_v1(
        PACKAGE_ROOT / "configs" / f"online_{scope}_taskwise_online_v1.yaml"
    )
    findings = taskwise_arm_parity_findings(control, online)
    if findings:
        raise RuntimeError(f"taskwise arm parity failed: {findings}")
    public, private, _summary = _paths(scope)
    expected_public = public.relative_to(WORKSPACE_ROOT).as_posix()
    expected_private = private.relative_to(WORKSPACE_ROOT).as_posix()
    for config in (control, online):
        if (
            config.task_manifest != expected_public
            or config.private_task_manifest != expected_private
        ):
            raise RuntimeError("taskwise config does not bind the canonical manifest set")
        if config.source_commit != SOURCE_COMMIT_PLACEHOLDER:
            raise RuntimeError("pre-commit dry-run requires the source commit placeholder")
    return {
        "control_protocol_id": control.protocol_id,
        "online_protocol_id": online.protocol_id,
        "control_config_sha256": control.config_sha256(),
        "online_config_sha256": online.config_sha256(),
        "parity_findings": list(findings),
        "attempts_per_task": control.attempts_per_task,
        "control_evolution_updates_per_task": control.evolution_updates_per_task,
        "online_evolution_updates_per_task": online.evolution_updates_per_task,
        "fixed_round_budget": control.fixed_round_budget,
        "stop_when_correct": control.stop_when_correct,
        "source_commit": SOURCE_COMMIT_PLACEHOLDER,
    }


def _validate_suite_configs() -> dict[str, object]:
    expected_keys = {
        "schema_version",
        "protocol_id",
        "arm",
        "stream_design",
        "stream_count",
        "tasks_per_stream",
        "total_item_count",
        "reset_memory_between_streams",
        "suite_summary",
        "source_commit",
        "stream_configs",
    }
    loaded: dict[str, dict[str, object]] = {}
    for arm in ("control", "online"):
        path = PACKAGE_ROOT / "configs" / f"{arm}_pilot500_stream_suite_taskwise_online_v1.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if type(payload) is not dict or set(payload) != expected_keys:
            raise RuntimeError("pilot500 stream suite config schema is invalid")
        expected_configs = [
            f"{arm}_{scope}_taskwise_online_v1.yaml" for scope in PILOT500_STREAM_SCOPES
        ]
        if (
            payload["arm"] != arm
            or payload["stream_count"] != 10
            or payload["tasks_per_stream"] != 50
            or payload["total_item_count"] != 500
            or payload["reset_memory_between_streams"] is not True
            or payload["stream_configs"] != expected_configs
            or payload["source_commit"] != SOURCE_COMMIT_PLACEHOLDER
        ):
            raise RuntimeError("pilot500 stream suite config binding is invalid")
        for scope, config_name in zip(
            PILOT500_STREAM_SCOPES,
            expected_configs,
            strict=True,
        ):
            config = load_taskwise_config_v1(PACKAGE_ROOT / "configs" / config_name)
            if config.scope != scope or config.arm != arm:
                raise RuntimeError("pilot500 expanded stream config is invalid")
        loaded[arm] = payload
    for key in (
        "schema_version",
        "stream_design",
        "stream_count",
        "tasks_per_stream",
        "total_item_count",
        "reset_memory_between_streams",
        "suite_summary",
        "source_commit",
    ):
        if loaded["control"][key] != loaded["online"][key]:
            raise RuntimeError("pilot500 suite arm parity is invalid")
    return {
        "stream_count": 10,
        "tasks_per_stream": 50,
        "total_item_count": 500,
        "reset_memory_between_streams": True,
        "arm_parity": "PASS",
    }


def _run(command: str) -> dict[str, object]:
    loader = ChemBench4KDatasetLoader(snapshot_root=DATASET_ROOT)
    output: dict[str, object] = {}
    function = generate_taskwise_manifests if command == "generate" else verify_taskwise_manifests
    for scope in ("canary9", *PILOT500_STREAM_SCOPES):
        public, private, summary = _paths(scope)
        result = function(
            loader,
            scope=scope,
            public_path=public,
            private_path=private,
            summary_path=summary,
        )
        output[scope] = {
            "item_count": result.item_count,
            "public_manifest_sha256": result.public_sha256,
            "private_manifest_sha256": result.private_sha256,
            "summary_sha256": result.summary_sha256,
            "ordered_uid_sha256": result.ordered_uid_sha256,
        }
        if command == "dry-run":
            output[scope]["config"] = _validate_configs(scope)
    legacy_public, legacy_private, legacy_summary = _paths(PILOT500_SCOPE)
    legacy = verify_taskwise_manifests(
        loader,
        scope=PILOT500_SCOPE,
        public_path=legacy_public,
        private_path=legacy_private,
        summary_path=legacy_summary,
    )
    legacy_bytes = legacy_single_chain_provenance_bytes(legacy)
    manifests = tuple(
        verify_taskwise_manifests(
            loader,
            scope=scope,
            public_path=_paths(scope)[0],
            private_path=_paths(scope)[1],
            summary_path=_paths(scope)[2],
        )
        for scope in PILOT500_STREAM_SCOPES
    )
    suite_bytes = pilot500_stream_suite_summary_bytes(
        loader,
        stream_manifests=manifests,
    )
    if command == "generate":
        STREAM_SUITE_SUMMARY.write_bytes(suite_bytes)
        LEGACY_PROVENANCE.write_bytes(legacy_bytes)
    else:
        if STREAM_SUITE_SUMMARY.read_bytes() != suite_bytes:
            raise RuntimeError("pilot500 stream suite summary is not frozen")
        if LEGACY_PROVENANCE.read_bytes() != legacy_bytes:
            raise RuntimeError("legacy single-chain provenance is not frozen")
    output["pilot500_stream_suite"] = {
        "stream_count": len(PILOT500_STREAM_SCOPES),
        "tasks_per_stream": 50,
        "total_item_count": sum(manifest.item_count for manifest in manifests),
        "summary_sha256": hashlib.sha256(suite_bytes).hexdigest(),
    }
    output["legacy_single_chain_pilot500"] = {
        "classification": list(LEGACY_SINGLE_CHAIN_CLASSIFICATION),
        "item_count": legacy.item_count,
        "public_manifest_sha256": legacy.public_sha256,
        "private_manifest_sha256": legacy.private_sha256,
        "summary_sha256": legacy.summary_sha256,
        "provenance_sha256": hashlib.sha256(legacy_bytes).hexdigest(),
    }
    output["pilot500_stream_suite"]["config"] = _validate_suite_configs()
    return {
        "status": "PASS",
        "operation": command,
        "model_calls": 0,
        "scopes": output,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate-taskwise-manifests-v1")
    parser.add_argument("command", choices=("generate", "verify", "dry-run"))
    args = parser.parse_args(argv)
    print(json.dumps(_run(args.command), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
