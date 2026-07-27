from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v2 import managed_codex
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    ManagedCodexError,
    load_managed_candidate_codex_v2,
    write_managed_candidate_codex_receipt_v2,
)
from openevo_chembench.supervised_transfer_v2.reflector_boundary import (
    _stage_core_managed_codex_auth,
)


def test_reflector_stages_nontrivial_auth_bytes_through_core_primitive(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-auth.json"
    source.write_bytes(b'{"auth":"synthetic-non-secret-fixture"}\n')
    source.chmod(0o600)
    destination = tmp_path / "codex-home"
    destination.mkdir(mode=0o700)

    _stage_core_managed_codex_auth(source=source, destination_root=destination)

    staged = destination / "auth.json"
    assert staged.read_bytes() == source.read_bytes()
    assert staged.stat().st_size > 1
    assert staged.stat().st_mode & 0o777 == 0o600


def _candidate_payload(*, executable_sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "schema_version": "OpenEvoManagedCandidateCodexReceiptV2",
        "source": "openevo_core_managed_science_runtime_v1",
        "image_id": "sha256:" + "b" * 64,
        "image_authority": "sha256:" + "b" * 64,
        "launcher_path": "/opt/codex/bin/codex",
        "native_executable_path": "/opt/codex/native/codex",
        "npm_package": "@openai/codex@0.144.1",
        "codex_cli_version": "codex-cli 0.144.1",
        "executable_sha256": executable_sha256,
    }


def test_candidate_runtime_receipt_is_reverified_and_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state/chembench_supervised_transfer_v2/managed_codex"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    repository = tmp_path.resolve()
    monkeypatch.setattr(
        managed_codex,
        "_inspect_candidate_runtime",
        lambda: _candidate_payload(),
    )

    payload, receipt_sha256 = write_managed_candidate_codex_receipt_v2(repository_root=repository)
    identity = load_managed_candidate_codex_v2(repository_root=repository)
    assert identity.executable_sha256 == payload["executable_sha256"]
    assert identity.receipt_sha256 == receipt_sha256
    assert identity.codex_cli_version == "codex-cli 0.144.1"

    monkeypatch.setattr(
        managed_codex,
        "_inspect_candidate_runtime",
        lambda: _candidate_payload(executable_sha256="c" * 64),
    )
    with pytest.raises(ManagedCodexError, match="CANDIDATE_CODEX_IDENTITY_DRIFT"):
        load_managed_candidate_codex_v2(repository_root=repository)
