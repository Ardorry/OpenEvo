from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
)
from openevo_chembench.temperature_full_evolve_v1 import formal_runtime as runtime


def _valid_receipt() -> dict[str, object]:
    core_tree = "1" * 64
    chembench_tree = "2" * 64
    return {
        "schema_version": runtime.FORMAL_RUNTIME_SCHEMA,
        "source_commit": "a" * 40,
        "source_tree_sha256": sha256_bytes(
            canonical_json_bytes(
                {
                    "openevo_python_tree_sha256": core_tree,
                    "chembench_python_tree_sha256": chembench_tree,
                }
            )
        ),
        "openevo_python_tree_sha256": core_tree,
        "chembench_python_tree_sha256": chembench_tree,
        "core_wheel_relative": (
            f"{runtime.FORMAL_RUNTIME_ROOT_RELATIVE}/framework/"
            "openevo-0.1.8-py3-none-any.whl"
        ),
        "core_wheel_sha256": "3" * 64,
        "chembench_wheel_relative": (
            f"{runtime.FORMAL_RUNTIME_ROOT_RELATIVE}/framework/"
            "openevo_chembench-0.1.0-py3-none-any.whl"
        ),
        "chembench_wheel_sha256": "4" * 64,
        "framework_lock_relative": runtime.FORMAL_FRAMEWORK_LOCK_RELATIVE,
        "framework_lock_sha256": "5" * 64,
        "runtime_python_relative": runtime.FORMAL_RUNTIME_PYTHON_RELATIVE,
        "runtime_python_sha256": "6" * 64,
        "base_dependency_runtime_relative": runtime.BASE_RUNTIME_ROOT_RELATIVE,
        "base_dependency_inventory_sha256": "7" * 64,
        "installed_inventory_sha256": "8" * 64,
        "openevo_version": "0.1.8",
        "chembench_version": "0.1.0",
        "openevo_origin_relative": (
            "lib/python3.11/site-packages/openevo/__init__.py"
        ),
        "chembench_origin_relative": (
            "lib/python3.11/site-packages/openevo_chembench/__init__.py"
        ),
        "framework_registry_digest": "9" * 64,
        "core_editable": False,
        "chembench_editable": False,
        "pip_check_passed": True,
        "model_visible_tools_enabled": False,
        "provider_transport_network_enabled": True,
        "model_calls": 0,
    }


def test_receipt_shape_is_closed_and_binds_exact_paths() -> None:
    receipt = _valid_receipt()
    runtime._validate_receipt_shape(receipt)

    for key, invalid in (
        ("core_wheel_relative", "/tmp/openevo-0.1.8-py3-none-any.whl"),
        ("chembench_origin_relative", "../../source/openevo_chembench/__init__.py"),
        ("model_calls", False),
    ):
        changed = {**receipt, key: invalid}
        with pytest.raises(runtime.TemperatureFormalRuntimeError) as caught:
            runtime._validate_receipt_shape(changed)
        assert caught.value.finding_code == "FORMAL_RUNTIME_RECEIPT_INVALID"


def test_private_receipt_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text('{"schema_version":"first","schema_version":"second"}')
    path.chmod(0o600)

    with pytest.raises(runtime.TemperatureFormalRuntimeError) as caught:
        runtime._read_private_receipt(path)
    assert caught.value.finding_code == "FORMAL_RUNTIME_RECEIPT_INVALID"


def test_bootstrap_python_binds_raw_repository_venv_path(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    expected = repository / ".venv/bin/python"
    expected.parent.mkdir(parents=True)
    expected.symlink_to(Path(sys.executable).resolve(strict=True))
    alias = repository / "alias-python"
    alias.symlink_to(Path(sys.executable).resolve(strict=True))

    assert runtime._validated_bootstrap_python(repository, expected) == expected
    with pytest.raises(runtime.TemperatureFormalRuntimeError) as caught:
        runtime._validated_bootstrap_python(repository, alias)
    assert caught.value.finding_code == "FORMAL_RUNTIME_BOOTSTRAP_INVALID"


def test_wheel_source_digest_requires_exact_unique_python_members(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "__init__.py").write_text("VALUE = 1\n")
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.write(source / "__init__.py", "example/__init__.py")

    assert len(runtime._wheel_source_digest(
        source_root=source,
        wheel=wheel,
        wheel_prefix="example",
    )) == 64
    (source / "__init__.py").write_text("VALUE = 2\n")
    with pytest.raises(runtime.TemperatureFormalRuntimeError) as caught:
        runtime._wheel_source_digest(
            source_root=source,
            wheel=wheel,
            wheel_prefix="example",
        )
    assert caught.value.finding_code == "FORMAL_RUNTIME_WHEEL_SOURCE_MISMATCH"


def test_copied_editable_project_markers_are_removed(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "openevo").mkdir()
    (site / "openevo-0.1.8.dist-info").mkdir()
    marker = site / "__editable__.openevo_chembench-0.1.0.pth"
    marker.write_text("/outside/source\n")
    unrelated = site / "distutils-precedence.pth"
    unrelated.write_text("import os\n")

    runtime._remove_copied_project_distributions(site)

    assert not (site / "openevo").exists()
    assert not (site / "openevo-0.1.8.dist-info").exists()
    assert not marker.exists()
    assert unrelated.is_file()


def test_direct_url_is_rebound_to_published_wheel_and_record(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    dist_info = site / "openevo-0.1.8.dist-info"
    dist_info.mkdir(parents=True)
    wheel_digest = "a" * 64
    direct = dist_info / "direct_url.json"
    direct.write_text(
        json.dumps(
            {
                "archive_info": {
                    "hash": f"sha256={wheel_digest}",
                    "hashes": {"sha256": wheel_digest},
                },
                "url": "file:///temporary/wheel.whl",
            }
        )
    )
    record = dist_info / "RECORD"
    record.write_text(
        "openevo-0.1.8.dist-info/RECORD,,\n"
        "openevo-0.1.8.dist-info/direct_url.json,old,1\n"
    )
    published = tmp_path / "formal/framework/openevo-0.1.8-py3-none-any.whl"

    runtime._normalize_distribution_direct_url(
        site=site,
        dist_info_pattern="openevo-*.dist-info",
        wheel_sha256=wheel_digest,
        published_wheel=published,
    )

    payload = direct.read_bytes()
    assert json.loads(payload)["url"] == published.as_uri()
    encoded_digest = base64.urlsafe_b64encode(
        hashlib.sha256(payload).digest()
    ).rstrip(b"=").decode("ascii")
    assert f"sha256={encoded_digest},{len(payload)}" in record.read_text()


def test_no_replace_publication_preserves_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()

    with pytest.raises(runtime.TemperatureFormalRuntimeError) as caught:
        runtime._publish_directory_noreplace(
            source=source,
            destination=destination,
            parent=tmp_path,
        )
    assert caught.value.finding_code == "FORMAL_RUNTIME_PUBLISH_COLLISION"
    assert source.is_dir()
    assert destination.is_dir()


def test_v4_runtime_and_failure_namespaces_do_not_alias_prior_state() -> None:
    assert runtime.FORMAL_RUNTIME_ROOT_RELATIVE.endswith("/formal_runtime_v4")
    assert runtime.FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE.endswith(
        "/formal_runtime_v4_failures"
    )
    assert runtime.FORMAL_RUNTIME_ROOT_RELATIVE not in {
        "state/chembench_temperature_full_evolve_v1/formal_runtime",
        "state/chembench_temperature_full_evolve_v1/formal_runtime_v2",
        "state/chembench_temperature_full_evolve_v1/formal_runtime_v3",
    }
    assert runtime.FORMAL_RUNTIME_FAILURE_ROOT_RELATIVE not in {
        "state/chembench_temperature_full_evolve_v1/formal_runtime_failures",
        "state/chembench_temperature_full_evolve_v1/formal_runtime_v2_failures",
        "state/chembench_temperature_full_evolve_v1/formal_runtime_v3_failures",
    }


def test_formal_python_guard_rejects_repository_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = SimpleNamespace(
        runtime_python=tmp_path / "formal/runtime_venv/bin/python",
        root=tmp_path / "formal",
        receipt={},
    )
    monkeypatch.setattr(
        runtime,
        "load_temperature_formal_runtime_v1",
        lambda **_values: identity,
    )

    with pytest.raises(runtime.TemperatureFormalRuntimeError) as caught:
        runtime.require_temperature_formal_runtime_python_v1(
            repository_root=tmp_path
        )
    assert caught.value.finding_code == "FORMAL_RUNTIME_PYTHON_INVALID"
