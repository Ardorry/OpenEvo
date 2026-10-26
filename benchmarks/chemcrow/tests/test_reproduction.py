from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from openevo_chemcrow.reproduction import load_fixture_lock, verify_publication
from openevo_chemcrow.submission import REPRODUCTION_CONTRACT

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_portable_reproduction_config_and_locks_are_closed() -> None:
    config_path = PACKAGE_ROOT / "configs/full-v8.reproduction.example.yaml"
    raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw)
    fixture_lock = load_fixture_lock(PACKAGE_ROOT / "reproduction/fixtures.lock.json")
    toolchain = json.loads(
        (PACKAGE_ROOT / "reproduction/toolchain.lock.json").read_text(encoding="utf-8")
    )

    assert re.search(r"(?<![A-Za-z0-9._-])/home/[A-Za-z0-9._-]+", raw) is None
    assert config["execution_scope"] == "full_g1_g2_pair"
    assert config["task_ids"] == "all"
    assert config["reproduction_contract"] == REPRODUCTION_CONTRACT
    assert {item["name"] for item in fixture_lock["repositories"]} == {
        "chemcrow-runs",
        "chemcrow-public",
        "rxn-sandbox",
        "rxn4chemistry",
    }
    assert toolchain["python"] == "3.11.15"
    assert toolchain["uv"] == "0.12.5"
    assert toolchain["docker"] == {
        "cli": "29.5.2",
        "engine": "29.5.2",
        "engine_verified_in_acceptance_environment": False,
    }
    assert toolchain["package_lock_sha256"] == (
        "05ed78b4ef903a51cc3158ce445d7e2f5bf5696d0c2020ad37f3fba40188582f"
    )
    assert toolchain["managed_runtime"]["oci_index_id"] == config["candidate"][
        "runtime"
    ]["image"]


def test_checked_in_publication_is_byte_verified() -> None:
    receipt = verify_publication(PACKAGE_ROOT)

    assert receipt["status"] == "PASS"
    assert receipt["artifact_count"] == 84
    assert receipt["artifact_count_by_type"] == {
        "agent_system": 28,
        "skill_bundle": 28,
        "text_memory": 28,
    }
    assert receipt["private_runtime_state_bundled"] is False
    assert receipt["independent_full_audit_possible"] is False
