from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v2.runtime_services import (
    RuntimeProcessIdentityV2,
    RuntimeServicesV2Error,
    _runtime_metadata,
    _runtime_paths,
    _validate_runtime_inputs,
)

REPOSITORY = Path(__file__).resolve().parents[4]


def test_runtime_services_bind_the_isolated_noneditable_core_wheel() -> None:
    paths = _runtime_paths(REPOSITORY.resolve())
    _validate_runtime_inputs(paths)
    metadata = _runtime_metadata(paths)

    assert paths["runtime_python"] != REPOSITORY / ".venv/bin/python"
    assert metadata["openevo_version"] == "0.1.8"
    assert all(len(metadata[key]) == 64 for key in metadata if key.endswith("sha256"))


def test_runtime_process_receipt_is_closed_and_content_free() -> None:
    payload = {
        "service": "rollout",
        "pid": 100,
        "process_group_id": 100,
        "session_id": 100,
        "start_time_ticks": 200,
        "executable_sha256": "a" * 64,
        "cmdline_sha256": "b" * 64,
    }

    identity = RuntimeProcessIdentityV2.from_payload(payload)

    assert identity.payload == payload
    assert "cmdline" not in identity.payload
    assert "environment" not in identity.payload


def test_runtime_process_receipt_rejects_unknown_fields() -> None:
    payload = {
        "service": "gateway",
        "pid": 100,
        "process_group_id": 100,
        "session_id": 100,
        "start_time_ticks": 200,
        "executable_sha256": "a" * 64,
        "cmdline_sha256": "b" * 64,
        "credential": "forbidden",
    }

    with pytest.raises(RuntimeServicesV2Error, match="RUNTIME_SERVICE_RECEIPT_INVALID"):
        RuntimeProcessIdentityV2.from_payload(payload)
