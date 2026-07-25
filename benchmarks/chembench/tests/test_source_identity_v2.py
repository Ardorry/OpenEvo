from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openevo_chembench.source_identity_v2 import (
    DERIVED_RUNTIME_OUTPUTS,
    SOURCE_IDENTITY_INPUTS,
    SourceManifestError,
    build_source_manifest,
    canonical_manifest_bytes,
    verify_source_manifest,
    write_source_manifest,
)


def test_source_manifest_is_deterministic_and_excludes_itself(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src" / "package.egg-info").mkdir()
    (tmp_path / "src" / "package.egg-info" / "PKG-INFO").write_text(
        "generated metadata\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_module.py").write_text(
        "def test_value(): pass\n",
        encoding="utf-8",
    )
    first = build_source_manifest(tmp_path)
    second = build_source_manifest(tmp_path)
    assert canonical_manifest_bytes(first) == canonical_manifest_bytes(second)

    path, digest = write_source_manifest(tmp_path)
    assert digest == verify_source_manifest(tmp_path, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    paths = {item["path"] for item in payload["files"]}
    assert "manifests/chembench_source_manifest_v2.json" not in paths
    assert "src/package.egg-info/PKG-INFO" not in paths
    assert payload["source_identity_inputs"] == list(SOURCE_IDENTITY_INPUTS)
    assert payload["derived_runtime_outputs"] == list(DERIVED_RUNTIME_OUTPUTS)
    assert "source_manifest_sha256" not in payload
    assert "manifest_sha256" not in payload


def test_source_manifest_binds_all_root_protocol_documentation(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    runbook = tmp_path / "TASKWISE_ONLINE_RUNBOOK_V1.md"
    readme.write_text("# Package\n", encoding="utf-8")
    runbook.write_text("# Protocol\n", encoding="utf-8")

    paths = {entry["path"] for entry in build_source_manifest(tmp_path)["files"]}

    assert paths == {"README.md", "TASKWISE_ONLINE_RUNBOOK_V1.md"}


def test_source_manifest_rejects_symlink(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    (tmp_path / "src" / "alias.py").symlink_to(source)
    with pytest.raises(SourceManifestError, match="symlink"):
        build_source_manifest(tmp_path)


def test_source_manifest_detects_change(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    path, _digest = write_source_manifest(tmp_path)
    source.write_text("raise SystemExit\n", encoding="utf-8")
    with pytest.raises(SourceManifestError, match="does not match"):
        verify_source_manifest(tmp_path, path)


def test_public_manifests_are_source_but_private_and_runtime_outputs_are_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "manifests" / "v2").mkdir(parents=True)
    public = tmp_path / "manifests" / "v2" / "pilot500_public_manifest.jsonl"
    summary = tmp_path / "manifests" / "v2" / "pilot500_manifest_summary.json"
    public.write_text('{"uid":"public"}\n', encoding="utf-8")
    summary.write_text('{"scope":"pilot500"}\n', encoding="utf-8")
    generated = {
        "manifests/v2/benchmark_execution_receipt_v2.json": '{"receipt":true}\n',
        "manifests/v2/source_manifest_acceptance_v2.json": '{"accept":true}\n',
        "private_manifests/v2/pilot500_private_manifest.jsonl": '{"target":"A"}\n',
        "state/v2/artifacts/payload.json": '{"memory":"generated"}\n',
        "state/v2/core.sqlite": "not-a-database\n",
        "results/v2/result.json": '{"prediction":"A"}\n',
        "runtime/session.json": "{}\n",
        "cache/cache.json": "{}\n",
        "auth/auth.json": "{}\n",
        "tmp/temporary.json": "{}\n",
    }
    for relative, content in generated.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    manifest = build_source_manifest(tmp_path)
    paths = {entry["path"] for entry in manifest["files"]}
    assert public.relative_to(tmp_path).as_posix() in paths
    assert summary.relative_to(tmp_path).as_posix() in paths
    assert paths.isdisjoint(generated)


def test_receipt_and_artifact_instances_do_not_change_source_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "configs").mkdir()
    config = tmp_path / "configs" / "baseline.yaml"
    config.write_text("artifact_id: PLACEHOLDER\n", encoding="utf-8")
    baseline = canonical_manifest_bytes(build_source_manifest(tmp_path))

    receipt = tmp_path / "manifests" / "v2" / "benchmark_execution_receipt_v2.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"evidence":"one"}\n', encoding="utf-8")
    artifact = tmp_path / "state" / "v2" / "artifacts" / "memory.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"payload":"generated"}\n', encoding="utf-8")
    core_db = tmp_path / "state" / "v2" / "evolution.sqlite"
    core_db.write_bytes(b"generated state")

    after_instances = canonical_manifest_bytes(build_source_manifest(tmp_path))
    assert after_instances == baseline

    receipt.unlink()
    receipt.write_text('{"evidence":"rebuilt"}\n', encoding="utf-8")
    assert canonical_manifest_bytes(build_source_manifest(tmp_path)) == baseline

    config.write_text("artifact_id: core-artifact-v2\n", encoding="utf-8")
    rebound = canonical_manifest_bytes(build_source_manifest(tmp_path))
    assert rebound != baseline
    assert hashlib.sha256(rebound).hexdigest() != hashlib.sha256(baseline).hexdigest()
