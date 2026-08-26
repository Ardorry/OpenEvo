from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256
from .ledger import AmbiguousPhaseClaimError, PhaseLedger


class VerifiedReplacementPhaseLedger(PhaseLedger):
    """Three-pipeline ledger extension for a proven pre-Candidate no-effect failure."""

    def __init__(self, root: Path, *, pair_id: str) -> None:
        self.pair_id = pair_id
        super().__init__(root, pair_id=pair_id)

    def claim(
        self,
        phase: str,
        authority: dict[str, Any],
        *,
        allow_verified_replacement: bool = False,
    ) -> None:
        path = self._path(phase)
        if path.is_file():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior.get("status") == "replacement_ready":
                if not allow_verified_replacement:
                    raise AmbiguousPhaseClaimError(
                        f"phase {phase} has a verified replacement awaiting explicit resume"
                    )
                _validate_replacement_ready_claim(
                    prior,
                    pair_id=self.pair_id,
                    phase=phase,
                    authority=authority,
                )
                prior["status"] = "claimed"
                prior["active_attempt_ordinal"] = len(prior["failed_attempts"]) + 1
                prior["replacement_activated"] = True
                path.write_text(
                    json.dumps(prior, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                return
        super().claim(phase, authority)

    def reconcile_verified_no_effect_failure(
        self,
        phase: str,
        receipt: dict[str, Any],
    ) -> None:
        """Preserve a terminal pre-dispatch failure and allow one explicit replacement."""

        path = self._path(phase)
        if not path.is_file():
            raise ValueError(f"phase has no durable claim: {phase}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "claimed":
            raise ValueError(f"phase is not a claimed failed attempt: {phase}")
        if payload.get("failed_attempts") is not None:
            raise ValueError(f"phase already contains failed-attempt history: {phase}")
        if receipt.get("status") != "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY":
            raise ValueError("no-effect recovery receipt status is invalid")
        if receipt.get("pair_id") != self.pair_id or receipt.get("phase") != phase:
            raise ValueError("no-effect recovery receipt authority differs")
        if receipt.get("attempt_ordinal") != 1:
            raise ValueError("first no-effect recovery attempt ordinal must be one")
        if receipt.get("authority_sha256") != payload.get("authority_sha256"):
            raise ValueError("no-effect recovery authority hash differs")
        original_bytes = path.read_bytes()
        if receipt.get("original_claim_sha256") != hashlib.sha256(original_bytes).hexdigest():
            raise ValueError("no-effect recovery original claim hash differs")
        predicates = receipt.get("no_effect_predicates")
        if not isinstance(predicates, dict) or not predicates or set(predicates.values()) != {True}:
            raise ValueError("no-effect recovery predicates are incomplete")
        receipt_sha256 = canonical_sha256(receipt)
        payload["schema_version"] = "chemcrow_phase_claim_v2"
        payload["status"] = "replacement_ready"
        payload["failed_attempts"] = [
            {
                "attempt_ordinal": 1,
                "outcome": "terminal_pre_candidate_no_effect",
                "receipt": receipt,
                "receipt_sha256": receipt_sha256,
            }
        ]
        payload["active_attempt_ordinal"] = None
        payload["replacement_activated"] = False
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def replacement_ready(self, phase: str) -> bool:
        path = self._path(phase)
        if not path.is_file():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "replacement_ready":
            return False
        _validate_replacement_ready_claim(
            payload,
            pair_id=self.pair_id,
            phase=phase,
            authority=payload.get("authority"),
        )
        return True


def verified_no_effect_attempt_count(
    payload: dict[str, Any],
    *,
    pair_id: str,
    phase: str,
) -> int:
    """Validate and count preserved terminal pre-Candidate infrastructure attempts."""

    attempts = payload.get("failed_attempts")
    if attempts is None:
        return 0
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("failed-attempt history is invalid")
    for ordinal, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict):
            raise TypeError("failed-attempt entry is invalid")
        receipt = attempt.get("receipt")
        if (
            attempt.get("attempt_ordinal") != ordinal
            or attempt.get("outcome") != "terminal_pre_candidate_no_effect"
            or not isinstance(receipt, dict)
            or attempt.get("receipt_sha256") != canonical_sha256(receipt)
            or receipt.get("attempt_ordinal") != ordinal
            or receipt.get("schema_version")
            != "chemcrow_pre_candidate_no_effect_recovery_v1"
            or receipt.get("status") != "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY"
            or receipt.get("pair_id") != pair_id
            or receipt.get("phase") != phase
            or receipt.get("authority_sha256") != payload.get("authority_sha256")
            or receipt.get("candidate_model_call_proven_absent") is not True
            or receipt.get("duplicate_scientific_call") is not False
            or receipt.get("recorded_before_replacement_dispatch") is not True
        ):
            raise ValueError("failed-attempt receipt is invalid")
        predicates = receipt.get("no_effect_predicates")
        if not isinstance(predicates, dict) or not predicates or set(predicates.values()) != {True}:
            raise ValueError("failed-attempt no-effect proof is incomplete")
    count = len(attempts)
    if count != 1:
        raise ValueError("only one verified no-effect replacement is permitted")
    if payload.get("status") == "replacement_ready":
        if (
            payload.get("replacement_activated") is not False
            or payload.get("active_attempt_ordinal") is not None
        ):
            raise ValueError("replacement-ready claim activation state is invalid")
    elif payload.get("status") in {"claimed", "terminal"}:
        if (
            payload.get("replacement_activated") is not True
            or payload.get("active_attempt_ordinal") != 2
        ):
            raise ValueError("replacement claim activation evidence is invalid")
    else:
        raise ValueError("replacement claim status is invalid")
    return count


def _validate_replacement_ready_claim(
    payload: dict[str, Any],
    *,
    pair_id: str,
    phase: str,
    authority: object,
) -> None:
    if (
        payload.get("schema_version") != "chemcrow_phase_claim_v2"
        or payload.get("phase") != phase
        or payload.get("status") != "replacement_ready"
        or not isinstance(authority, dict)
        or payload.get("authority_sha256") != canonical_sha256(authority)
        or payload.get("active_attempt_ordinal") is not None
        or payload.get("replacement_activated") is not False
    ):
        raise ValueError("replacement-ready claim is invalid")
    count = verified_no_effect_attempt_count(payload, pair_id=pair_id, phase=phase)
    receipt = payload["failed_attempts"][-1]["receipt"]
    if receipt.get("pair_id") != pair_id or receipt.get("phase") != phase or count != 1:
        raise ValueError("replacement-ready claim recovery authority differs")


__all__ = ["VerifiedReplacementPhaseLedger", "verified_no_effect_attempt_count"]
