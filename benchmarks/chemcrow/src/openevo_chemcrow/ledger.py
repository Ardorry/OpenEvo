from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256


class AmbiguousPhaseClaimError(RuntimeError):
    pass


class PhaseLedger:
    """Durable fail-closed dispatch claims; ambiguous effects are never replayed."""

    def __init__(self, root: Path, *, pair_id: str) -> None:
        self.root = root / pair_id
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, phase: str) -> Path:
        return self.root / f"{phase}.json"

    def claim(self, phase: str, authority: dict[str, Any]) -> None:
        path = self._path(phase)
        if path.is_file():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior.get("status") == "terminal" and prior.get("authority_sha256") == canonical_sha256(authority):
                raise ValueError(f"phase already terminal: {phase}")
            raise AmbiguousPhaseClaimError(
                f"phase {phase} has a non-terminal prior claim; provider-side effects require audit"
            )
        payload = {
            "schema_version": "chemcrow_phase_claim_v1",
            "phase": phase,
            "status": "claimed",
            "authority": authority,
            "authority_sha256": canonical_sha256(authority),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def terminal(self, phase: str, receipt: dict[str, Any]) -> None:
        path = self._path(phase)
        if not path.is_file():
            raise ValueError(f"phase has no durable claim: {phase}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "claimed":
            raise ValueError(f"phase is not claimable: {phase}")
        payload["status"] = "terminal"
        payload["receipt"] = receipt
        payload["receipt_sha256"] = canonical_sha256(receipt)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def audit_resume(self) -> dict[str, str]:
        statuses: dict[str, str] = {}
        for path in sorted(self.root.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            phase = str(payload.get("phase") or path.stem)
            status = str(payload.get("status") or "invalid")
            statuses[phase] = status
            if status != "terminal":
                raise AmbiguousPhaseClaimError(
                    f"resume refused: {phase} is {status}; do not duplicate the external call"
                )
        return statuses
