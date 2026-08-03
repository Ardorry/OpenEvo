"""Top-level preregistered Safe-Evolve V2 campaign orchestration."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openevo.evolution.framework.runtime import load_verified_framework_registry

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    write_private_file,
)
from openevo_chembench.temperature_safe_evolve_v2.config import (
    load_safe_evolve_config_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.fold_run import (
    FoldRunOutcomeV2,
    SafeFoldRunnerV2,
)
from openevo_chembench.temperature_safe_evolve_v2.folds import (
    FrozenSafeFoldsV2,
    build_safe_folds_v2,
    private_fold_manifest_bytes_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.ledger import SafeExperimentLedgerV2
from openevo_chembench.temperature_safe_evolve_v2.phase_a import (
    PhaseAOutcomeV2,
    PhaseARunnerV2,
    load_phase_a_inputs_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    TemperatureRuntimeServicesIdentityV1,
    audit_empty_temperature_safe_runtime_services_v2,
    fold_runtime_service_run_id_v2,
    load_temperature_safe_runtime_evidence_v2,
    load_temperature_safe_runtime_services_v2,
    start_temperature_safe_runtime_services_v2,
    stop_temperature_safe_runtime_services_v2,
)

_RUN_ID = re.compile(r"stv3-temperature-safe-evolve-v2-20[0-9]{6}T[0-9]{6}Z\Z")


class CampaignError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class CampaignOutcomeV2:
    status: str
    campaign_run_id: str
    campaign_root: Path
    selected_target: str | None
    phase_a: PhaseAOutcomeV2
    folds: tuple[FoldRunOutcomeV2, ...]
    source_commit: str
    invalidated_run_ids: tuple[str, ...] = ()


class SafeEvolveCampaignV2:
    def __init__(
        self,
        *,
        repository_root: Path,
        campaign_run_id: str,
        preflight_root: Path,
        runtime: TemperatureRuntimeServicesIdentityV1,
        resume: bool,
    ) -> None:
        if _RUN_ID.fullmatch(campaign_run_id) is None:
            raise CampaignError("SAFE_CAMPAIGN_RUN_ID_INVALID")
        self.repository = repository_root.resolve(strict=True)
        self.run_id = campaign_run_id
        self.preflight_root = preflight_root.resolve(strict=True)
        self.runtime = runtime
        self.resume = resume
        self.config = load_safe_evolve_config_v2(
            self.repository / "benchmarks/chembench/configs/temperature_safe_evolve_v2/"
            "temperature_safe_evolve_v2.yaml"
        )
        self.source_commit = _git(self.repository, "rev-parse", "HEAD")
        self.root = self.repository / self.config.state_root_relative / "runs" / campaign_run_id
        self.preflight_report = self._load_preflight_report()
        self._validate_admission()

    def run(self) -> CampaignOutcomeV2:
        phase_inputs = load_phase_a_inputs_v2(repository_root=self.repository, config=self.config)
        phase_a = PhaseARunnerV2(
            repository_root=self.repository,
            campaign_run_id=self.run_id,
            campaign_root=self.root,
            config=self.config,
            runtime=self.runtime,
            inputs=phase_inputs,
            resume=self.resume,
        ).run()
        self._record_phase_a_closure(phase_a)
        self._campaign_manifest(phase_a=phase_a)
        if phase_a.selected_target is None:
            if not self._runtime_scope_stopped("phase_a"):
                self._stop_bound_runtime(scope="phase_a", runtime=self.runtime)
            self._close_campaign(status="NO_GO_ABLATION_NO_SAFE_SINGLE_TARGET", completed_folds=0)
            return CampaignOutcomeV2(
                status="NO_GO_ABLATION_NO_SAFE_SINGLE_TARGET",
                campaign_run_id=self.run_id,
                campaign_root=self.root,
                selected_target=None,
                phase_a=phase_a,
                folds=(),
                source_commit=self.source_commit,
            )
        if not self._runtime_scope_stopped("phase_a"):
            self._stop_bound_runtime(scope="phase_a", runtime=self.runtime)
        folds = self._load_frozen_folds()
        dev = tuple(
            ChemBench4KDatasetLoader(
                snapshot_root=self.repository / str(self.config.payload["dataset"]["root"])
            ).load_category("Temperature_Prediction", split="dev")
        )
        registry = load_verified_framework_registry(
            self.repository / self.config.framework_lock_relative
        )
        outcomes: list[FoldRunOutcomeV2] = []
        for fold_id in ("R0", "R1", "R2", "R3"):
            if fold_id != "R0" and (not outcomes or outcomes[0].r0_continue is not True):
                break
            train, validation, test = folds.run_partitions(fold_id)
            run_id = f"{self.run_id}-{fold_id.lower()}"
            run_root = self.root / "folds" / fold_id.lower()
            fold_runtime, already_stopped = self._acquire_fold_runtime(fold_id=fold_id)
            outcome = SafeFoldRunnerV2(
                repository_root=self.repository,
                run_id=run_id,
                fold_id=fold_id,  # type: ignore[arg-type]
                run_root=run_root,
                selected_target=phase_a.selected_target,  # type: ignore[arg-type]
                train=train,
                validation=validation,
                test=test,
                dev=dev,
                config=self.config,
                runtime=fold_runtime,
                registry=registry,
                resume=self.resume and run_root.exists(),
            ).run()
            # Only a closed fold reaches this stop; failures preserve the exact service root.
            if not already_stopped:
                self._stop_bound_runtime(scope=fold_id.casefold(), runtime=fold_runtime)
            outcomes.append(outcome)
            if fold_id == "R0" and outcome.r0_continue is not True:
                self._close_campaign(status="NO_GO_R0_SAFE_EVOLVE_PILOT_FAILED", completed_folds=1)
                return CampaignOutcomeV2(
                    status="NO_GO_R0_SAFE_EVOLVE_PILOT_FAILED",
                    campaign_run_id=self.run_id,
                    campaign_root=self.root,
                    selected_target=phase_a.selected_target,
                    phase_a=phase_a,
                    folds=tuple(outcomes),
                    source_commit=self.source_commit,
                )
        if len(outcomes) != 4:
            raise CampaignError("SAFE_CAMPAIGN_FOLD_CLOSURE_INVALID")
        status = _classify_four_folds(tuple(outcomes))
        self._close_campaign(status=status, completed_folds=4)
        return CampaignOutcomeV2(
            status=status,
            campaign_run_id=self.run_id,
            campaign_root=self.root,
            selected_target=phase_a.selected_target,
            phase_a=phase_a,
            folds=tuple(outcomes),
            source_commit=self.source_commit,
        )

    def _record_phase_a_closure(self, phase_a: PhaseAOutcomeV2) -> None:
        with SafeExperimentLedgerV2(
            path=self.root / "private/campaign_events.jsonl", run_id=self.run_id
        ) as ledger:
            _append_once(
                ledger,
                "CAMPAIGN_CREATED",
                {
                    "campaign_run_id": self.run_id,
                    "source_commit": self.source_commit,
                    "config_sha256": self.config.digest,
                    "classification": (
                        "reused_official_temperature_pool_independent_state_mechanism_experiment"
                    ),
                },
            )
            _append_once(
                ledger,
                "PREFLIGHT_CLOSED",
                {
                    "preflight_root": self.preflight_root.relative_to(self.repository).as_posix(),
                    "runtime_services_run_id": self.preflight_report[
                        "phase_a_runtime_services_run_id"
                    ],
                    "runtime_services_identity_sha256": self.preflight_report[
                        "runtime_services_identity_sha256"
                    ],
                    "preflight_model_calls": 0,
                },
            )
            _append_once(
                ledger,
                "TARGET_SELECTION_CLOSED",
                phase_a.target_selection_receipt,
            )

    def _close_campaign(self, *, status: str, completed_folds: int) -> None:
        with SafeExperimentLedgerV2(
            path=self.root / "private/campaign_events.jsonl", run_id=self.run_id
        ) as ledger:
            _append_once(
                ledger,
                "CAMPAIGN_CLOSED",
                {
                    "status": status,
                    "completed_folds": completed_folds,
                    "selected_target_receipt_closed": True,
                },
            )

    def _start_fold_runtime(self, *, fold_id: str) -> TemperatureRuntimeServicesIdentityV1:
        service_run_id = fold_runtime_service_run_id_v2(
            campaign_run_id=self.run_id,
            fold_id=fold_id,
        )
        identity = start_temperature_safe_runtime_services_v2(
            repository_root=self.repository,
            service_run_id=service_run_id,
        )
        if identity.source_commit != self.source_commit:
            raise CampaignError("SAFE_FOLD_RUNTIME_SOURCE_COMMIT_MISMATCH")
        inventory = audit_empty_temperature_safe_runtime_services_v2(
            repository_root=self.repository
        )
        if (
            inventory.get("service_run_id") != identity.service_run_id
            or inventory.get("runtime_services_identity_sha256") != identity.digest
            or inventory.get("model_calls_observed") != 0
        ):
            raise CampaignError("SAFE_FOLD_RUNTIME_EMPTY_INVENTORY_INVALID")
        self._append_runtime_event(
            "RUNTIME_SERVICES_STARTED",
            {
                "scope": fold_id.casefold(),
                "service_run_id": identity.service_run_id,
                "runtime_services_identity_sha256": identity.digest,
                "source_commit": identity.source_commit,
                "empty_inventory_sha256": inventory["inventory_sha256"],
                "model_calls_observed_before_fold": 0,
            },
        )
        return identity

    def _acquire_fold_runtime(
        self, *, fold_id: str
    ) -> tuple[TemperatureRuntimeServicesIdentityV1, bool]:
        scope = fold_id.casefold()
        service_run_id = fold_runtime_service_run_id_v2(
            campaign_run_id=self.run_id,
            fold_id=fold_id,
        )
        started = self._runtime_events(kind="RUNTIME_SERVICES_STARTED", scope=scope)
        stopped = self._runtime_events(kind="RUNTIME_SERVICES_STOPPED", scope=scope)
        if stopped:
            if len(started) != 1 or len(stopped) != 1:
                raise CampaignError("SAFE_FOLD_RUNTIME_LEDGER_CARDINALITY_INVALID")
            identity = load_temperature_safe_runtime_evidence_v2(
                repository_root=self.repository,
                service_run_id=service_run_id,
            )
            if (
                started[0].get("service_run_id") != identity.service_run_id
                or started[0].get("runtime_services_identity_sha256") != identity.digest
                or stopped[0].get("runtime_services_identity_sha256") != identity.digest
            ):
                raise CampaignError("SAFE_FOLD_RUNTIME_EVIDENCE_DRIFT")
            return identity, True
        if started:
            if len(started) != 1:
                raise CampaignError("SAFE_FOLD_RUNTIME_LEDGER_CARDINALITY_INVALID")
            identity = load_temperature_safe_runtime_services_v2(repository_root=self.repository)
            if (
                started[0].get("service_run_id") != identity.service_run_id
                or started[0].get("runtime_services_identity_sha256") != identity.digest
                or identity.service_run_id != service_run_id
            ):
                raise CampaignError("SAFE_FOLD_RUNTIME_CURRENT_BINDING_DRIFT")
            return identity, False
        return self._start_fold_runtime(fold_id=fold_id), False

    def _stop_bound_runtime(
        self, *, scope: str, runtime: TemperatureRuntimeServicesIdentityV1
    ) -> None:
        current = runtime.require_current()
        if current.get("runtime_services_identity_sha256") != runtime.digest:
            raise CampaignError("SAFE_RUNTIME_STOP_BINDING_INVALID")
        receipt = stop_temperature_safe_runtime_services_v2(repository_root=self.repository)
        if (
            receipt.get("service_run_id") != runtime.service_run_id
            or receipt.get("runtime_services_identity_sha256") != runtime.digest
            or receipt.get("completion_evidence_preserved") is not True
            or receipt.get("cleanup_complete") is not True
        ):
            raise CampaignError("SAFE_RUNTIME_STOP_RECEIPT_INVALID")
        self._append_runtime_event(
            "RUNTIME_SERVICES_STOPPED",
            {
                "scope": scope,
                "service_run_id": runtime.service_run_id,
                "runtime_services_identity_sha256": runtime.digest,
                "completion_root_marker_sha256": receipt[
                    "completion_root_marker_sha256"
                ],
                "completion_evidence_preserved": True,
                "cleanup_complete": True,
            },
        )

    def _append_runtime_event(self, kind: str, payload: dict[str, object]) -> None:
        with SafeExperimentLedgerV2(
            path=self.root / "private/campaign_events.jsonl", run_id=self.run_id
        ) as ledger:
            existing = [
                event["payload"]
                for event in ledger.events
                if event["kind"] == kind and event["payload"].get("scope") == payload["scope"]
            ]
            if existing:
                if existing != [payload]:
                    raise CampaignError("SAFE_CAMPAIGN_RUNTIME_RECEIPT_DRIFT")
                return
            ledger.append(kind, payload)

    def _runtime_events(self, *, kind: str, scope: str) -> list[dict[str, object]]:
        path = self.root / "private/campaign_events.jsonl"
        if not path.exists():
            return []
        try:
            with SafeExperimentLedgerV2(path=path, run_id=self.run_id) as ledger:
                return [
                    dict(event["payload"])
                    for event in ledger.events
                    if event["kind"] == kind and event["payload"].get("scope") == scope
                ]
        except (OSError, ValueError) as exc:
            raise CampaignError("SAFE_CAMPAIGN_RUNTIME_LEDGER_INVALID") from exc

    def _runtime_scope_stopped(self, scope: str) -> bool:
        stopped = self._runtime_events(kind="RUNTIME_SERVICES_STOPPED", scope=scope)
        if len(stopped) > 1:
            raise CampaignError("SAFE_CAMPAIGN_RUNTIME_LEDGER_CARDINALITY_INVALID")
        return bool(stopped)

    def _load_frozen_folds(self) -> FrozenSafeFoldsV2:
        loader = ChemBench4KDatasetLoader(
            snapshot_root=self.repository / str(self.config.payload["dataset"]["root"])
        )
        folds = build_safe_folds_v2(loader)
        private_path = self.preflight_root / "private/fold_manifest.jsonl"
        public_path = self.preflight_root / "public/fold_manifest.json"
        if (
            private_path.read_bytes() != private_fold_manifest_bytes_v2(folds)
            or json.loads(public_path.read_bytes()) != folds.plan.public_receipt()
        ):
            raise CampaignError("SAFE_CAMPAIGN_FOLD_MANIFEST_DRIFT")
        return folds

    def _validate_admission(self) -> None:
        if not self.runtime.require_current():
            raise CampaignError("SAFE_CAMPAIGN_RUNTIME_NOT_CURRENT")
        if self.runtime.source_commit != self.source_commit:
            raise CampaignError("SAFE_CAMPAIGN_RUNTIME_SOURCE_COMMIT_MISMATCH")
        if _git(self.repository, "status", "--porcelain", "--untracked-files=no"):
            raise CampaignError("SAFE_CAMPAIGN_TRACKED_TREE_DIRTY")
        report = self.preflight_report
        if (
            report.get("status") != "PASS_READY_FOR_SAFE_EVOLVE_V2"
            or report.get("source_commit") != self.source_commit
            or report.get("config_sha256") != self.config.digest
            or report.get("preflight_model_calls") != 0
        ):
            raise CampaignError("SAFE_CAMPAIGN_PREFLIGHT_MISMATCH")
        if report.get("runtime_services_identity_sha256") == self.runtime.digest:
            return
        if not self.resume:
            raise CampaignError("SAFE_CAMPAIGN_PREFLIGHT_RUNTIME_MISMATCH")
        active_bindings: list[dict[str, object]] = []
        for fold_id in ("r0", "r1", "r2", "r3"):
            started = self._runtime_events(kind="RUNTIME_SERVICES_STARTED", scope=fold_id)
            stopped = self._runtime_events(kind="RUNTIME_SERVICES_STOPPED", scope=fold_id)
            if len(started) > 1 or len(stopped) > 1 or (stopped and not started):
                raise CampaignError("SAFE_CAMPAIGN_RUNTIME_LEDGER_CARDINALITY_INVALID")
            if started and not stopped:
                active_bindings.extend(started)
        if (
            len(active_bindings) != 1
            or active_bindings[0].get("service_run_id") != self.runtime.service_run_id
            or active_bindings[0].get("runtime_services_identity_sha256") != self.runtime.digest
        ):
            raise CampaignError("SAFE_CAMPAIGN_RESUME_RUNTIME_MISMATCH")

    def _load_preflight_report(self) -> dict[str, object]:
        receipt_path = self.preflight_root / "public/preflight_report.json"
        try:
            report = json.loads(receipt_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise CampaignError("SAFE_CAMPAIGN_PREFLIGHT_UNAVAILABLE") from exc
        if type(report) is not dict:
            raise CampaignError("SAFE_CAMPAIGN_PREFLIGHT_UNAVAILABLE")
        return report

    def _campaign_manifest(self, *, phase_a: PhaseAOutcomeV2) -> None:
        payload = {
            "schema_version": "TemperatureSafeCampaignManifestV2",
            "campaign_run_id": self.run_id,
            "classification": (
                "reused_official_temperature_pool_independent_state_mechanism_experiment"
            ),
            "source_commit": self.source_commit,
            "config_sha256": self.config.digest,
            "phase_a_runtime_services_run_id": self.preflight_report[
                "phase_a_runtime_services_run_id"
            ],
            "phase_a_runtime_services_identity_sha256": self.preflight_report[
                "runtime_services_identity_sha256"
            ],
            "distinct_runtime_services_identity_per_fold": True,
            "distinct_core_store_identity_per_fold": True,
            "preflight_root": self.preflight_root.relative_to(self.repository).as_posix(),
            "phase_a_status": phase_a.target_selection_receipt["status"],
            "selected_target": phase_a.selected_target,
            "fresh_database_artifact_lineage_per_run": True,
            "historically_never_exposed_claim": False,
            "external_independent_generalization_claim": False,
        }
        path = self.root / "private/campaign_manifest.json"
        if path.exists():
            if path.read_bytes() != canonical_json_bytes(payload):
                raise CampaignError("SAFE_CAMPAIGN_MANIFEST_DRIFT")
        else:
            write_private_file(path, canonical_json_bytes(payload), replace=False)


def _classify_four_folds(outcomes: tuple[FoldRunOutcomeV2, ...]) -> str:
    g0: list[bool] = []
    raw: list[bool] = []
    deployed: list[bool] = []
    fold_deltas: list[float] = []
    for outcome in outcomes:
        private = json.loads(outcome.private_result_path.read_bytes())
        g0.extend(bool(value) for value in private["test"]["g0_correctness"])
        raw.extend(bool(value) for value in private["test"]["raw_correctness"])
        deployed.extend(bool(value) for value in private["test"]["deployed_correctness"])
        fold_deltas.append(float(outcome.test_metrics["raw_vs_g0"]["delta_percentage_points"]))
    from openevo_chembench.temperature_full_evolve_v1.statistics import paired_binary_metrics_v1

    metrics = paired_binary_metrics_v1(tuple(g0), tuple(raw), bootstrap_seed=20260803)
    positive = metrics.candidate_only_correct
    negative = metrics.reference_only_correct
    infrastructure_parity = all(
        bool(value.test_metrics["parser_retry_failure_parity"]) for value in outcomes
    )
    preregistered_no_go = (
        metrics.delta_percentage_points < 0
        or positive <= negative
        or min(fold_deltas) < -4.0
        or not infrastructure_parity
    )
    if preregistered_no_go:
        return "COMPLETE_NO_GO_SAFE_EVOLVE_V2"
    go = (
        metrics.delta_percentage_points >= 2.0
        and positive >= 2 * negative
        and metrics.paired_bootstrap_delta_95_percentage_points[0] >= -2.0
        and sum(value >= 0 for value in fold_deltas) >= 3
        and min(fold_deltas) >= -4.0
        and sum(value.promotion_count > 0 for value in outcomes) >= 2
        and sum(value.deployment_real_evolved for value in outcomes) >= 2
        and infrastructure_parity
    )
    if go:
        return "COMPLETE_GO_MECHANISM_SIGNAL"
    deployed_delta = 100.0 * (sum(deployed) - sum(g0)) / len(g0)
    if (
        deployed_delta >= 0
        and sum(not value.deployment_real_evolved for value in outcomes) >= 3
        and metrics.delta_percentage_points >= 0
        and infrastructure_parity
    ):
        return "COMPLETE_SAFE_FALLBACK_ONLY"
    return "COMPLETE_NO_GO_SAFE_EVOLVE_V2"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _append_once(ledger: SafeExperimentLedgerV2, kind: str, payload: dict[str, object]) -> None:
    existing = [event["payload"] for event in ledger.events if event["kind"] == kind]
    if existing:
        if existing != [payload]:
            raise CampaignError("SAFE_CAMPAIGN_LEDGER_RECEIPT_DRIFT")
        return
    ledger.append(kind, payload)


__all__ = [
    "CampaignError",
    "CampaignOutcomeV2",
    "SafeEvolveCampaignV2",
]
