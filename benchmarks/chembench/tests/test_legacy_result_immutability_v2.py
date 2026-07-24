from __future__ import annotations

import hashlib
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = REPOSITORY_ROOT.parent
RECEIPT_PATH = WORKSPACE_ROOT / "notes" / "chembench4k_legacy_online_protocol_provenance_v1.json"
V2_RECEIPT_PATH = WORKSPACE_ROOT / "notes" / "chembench4k_legacy_protocol_provenance_v2.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _result_tree_sha256(root: Path) -> str:
    assert not root.is_symlink()
    paths = sorted(root.rglob("*"))
    assert not any(path.is_symlink() for path in paths)
    lines: list[str] = []
    for path in (candidate for candidate in paths if candidate.is_file()):
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        lines.append(f"{_sha256(path)}  {relative_path}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def test_legacy_manifests_and_configs_match_hash_receipt() -> None:
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    for relative_path, expected_sha256 in receipt["manifest_sha256"].items():
        path = WORKSPACE_ROOT / relative_path
        assert _sha256(path) == expected_sha256
    for relative_path, expected_sha256 in receipt["source_config_sha256"].items():
        path = WORKSPACE_ROOT / relative_path
        assert _sha256(path) == expected_sha256


def test_legacy_partial_result_trees_match_hash_receipt() -> None:
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    for relative_path, expected_sha256 in receipt["result_tree_sha256"].items():
        path = WORKSPACE_ROOT / relative_path
        actual_file_count = sum(candidate.is_file() for candidate in path.rglob("*"))
        assert actual_file_count == receipt["result_tree_file_count"][relative_path]
        assert _result_tree_sha256(path) == expected_sha256


def test_legacy_receipt_has_required_nonstandard_classification() -> None:
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    assert receipt["protocol_alias"] == "task_local_online_recovery_v1"
    assert receipt["status"] == "LEGACY_PARTIAL_RESULTS_HASH_SNAPSHOTTED_UNMODIFIED"
    assert receipt["classification"] == [
        "NON_STANDARD_ONLINE_RECOVERY_PROTOCOL",
        "NOT_COMPARABLE_TO_STANDARD_CHEMBENCH4K_SCORE",
    ]


def test_v2_legacy_provenance_covers_complete_result_roots() -> None:
    receipt = json.loads(V2_RECEIPT_PATH.read_text(encoding="utf-8"))

    assert receipt["classification"] == [
        "LEGACY_JABLONKAGROUP_CHEMBENCH",
        "NON_STANDARD_TASK_LOCAL_ONLINE_RECOVERY",
        "NOT_A_CHEMBENCH4K_RESULT",
        "NOT_A_CORE_REGISTERED_OPENEVO_RESULT",
    ]
    assert set(receipt["result_tree_sha256"]) == {
        "OpenEvo/results/debug_local_codex",
        "OpenEvo/results/formal",
    }
    for relative_path, expected_sha256 in receipt["result_tree_sha256"].items():
        path = WORKSPACE_ROOT / relative_path
        actual_file_count = sum(candidate.is_file() for candidate in path.rglob("*"))
        assert actual_file_count == receipt["result_tree_file_count"][relative_path]
        assert _result_tree_sha256(path) == expected_sha256
