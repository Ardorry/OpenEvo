from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

CONTROL_ENVIRONMENT_NAMES = (
    "OPENEVO_ROLLOUT_BASE_URL",
    "OPENEVO_CANDIDATE_MODEL",
    "OPENEVO_REFLECTOR_MODEL",
    "OPENEVO_EVOLUTION_EVALUATOR_MODEL",
    "OPENEVO_FINAL_EVALUATOR_MODEL",
    "CHEMCROW_RXN_PREDICT_URL",
    "CHEMCROW_RXN_RETRO_URL",
    "CHEMCROW_RXN_TIMEOUT_SECONDS",
)

OPTIONAL_PROVIDER_CREDENTIAL_NAMES = (
    "RXN4CHEM_API_KEY",
    "RXN4CHEM_PROJECT_ID",
    "SERP_API_KEY",
)

PAPER_EVALUATOR_ENVIRONMENT_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "CHEMCROW_PAPER_EVALUATOR_MODEL",
    "CHEMCROW_PAPER_EVALUATOR_MAX_USD",
    "CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION",
)

LEGACY_OR_EXCLUDED_CREDENTIAL_NAMES = (
    "OPENAI_API_KEY",
    "CHEMSPACE_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
)


def _presence(names: tuple[str, ...]) -> dict[str, bool]:
    return {name: bool(os.environ.get(name)) for name in names}


def credential_report(*, codex_auth_file: Path | None = None) -> dict[str, Any]:
    """Return a value-free credential/configuration report.

    Secret values are deliberately never included, hashed, measured, or read
    back. Only environment-variable names and non-empty presence flags leave
    this function.
    """
    auth_file = codex_auth_file or (Path.home() / ".codex" / "auth.json")
    controls = _presence(CONTROL_ENVIRONMENT_NAMES)
    optional = _presence(OPTIONAL_PROVIDER_CREDENTIAL_NAMES)
    paper = _presence(PAPER_EVALUATOR_ENVIRONMENT_NAMES)
    excluded = _presence(LEGACY_OR_EXCLUDED_CREDENTIAL_NAMES)
    local_rxn = controls["CHEMCROW_RXN_PREDICT_URL"] and controls[
        "CHEMCROW_RXN_RETRO_URL"
    ]
    hosted_rxn_credentials = optional["RXN4CHEM_API_KEY"] and optional[
        "RXN4CHEM_PROJECT_ID"
    ]
    hosted_rxn_package = importlib.util.find_spec("rxn4chemistry") is not None
    missing_controls = [name for name, present in controls.items() if not present]
    missing_core_model_controls = [
        name for name in CONTROL_ENVIRONMENT_NAMES[:5] if not controls[name]
    ]
    missing_paper = [name for name, present in paper.items() if not present]
    unexpected_excluded = [name for name, present in excluded.items() if present]
    codex_auth_present = auth_file.is_file()
    reduced_profile_ready = not missing_controls and codex_auth_present and local_rxn
    return {
        "schema_version": "chemcrow_credential_check_v1",
        "status": "READY_REDUCED_PROFILE" if reduced_profile_ready else "BLOCKED",
        "model_calls": 0,
        "paid_operations": 0,
        "secret_values_included": False,
        "core_model_authentication": {
            "method": "codex_subscription_auth_file",
            "credential_name": "Codex subscription auth.json",
            "present": codex_auth_present,
            "openai_api_key_required": False,
        },
        "control_environment_presence": controls,
        "optional_provider_credential_presence": optional,
        "paper_evaluator_environment_presence": paper,
        "legacy_or_excluded_credential_presence": excluded,
        "provider_modes": {
            "local_rxn_configured": local_rxn,
            "hosted_rxn_credentials_configured": hosted_rxn_credentials,
            "hosted_rxn_package_installed": hosted_rxn_package,
            "serp_web_search_configured": optional["SERP_API_KEY"],
            "local_rxn_takes_precedence": local_rxn,
        },
        "capability_status": {
            "core_candidate_reflector_evaluators": (
                "ready"
                if codex_auth_present and not missing_core_model_controls
                else "blocked"
            ),
            "paper_evaluator_openrouter": (
                "configured_not_authorized_or_tested"
                if not missing_paper
                else "blocked"
            ),
            "reaction_prediction_and_retrosynthesis": (
                "ready_local"
                if local_rxn
                else "ready_hosted_unverified"
                if hosted_rxn_credentials and hosted_rxn_package
                else "blocked"
            ),
            "web_search": "ready_unverified" if optional["SERP_API_KEY"] else "unavailable",
            "literature_search": "excluded",
            "molecule_procurement_price": "excluded",
        },
        "missing_required_control_names": missing_controls,
        "missing_paper_evaluator_names": missing_paper,
        "unexpected_legacy_or_excluded_key_names": unexpected_excluded,
        "full_run_authorization_present": bool(
            os.environ.get("CHEMCROW_FULL_RUN_AUTHORIZATION")
        ),
        "ready_for_reduced_profile_setting_checks": reduced_profile_ready,
    }
