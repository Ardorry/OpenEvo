from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    CanonicalEntryV2,
    SafeArtifactStateV2,
)
from openevo_chembench.temperature_safe_evolve_v2.core_evolution import (
    create_safe_core_store_v2,
    execute_provisional_update_v2,
    promote_safe_state_v2,
    read_safe_core_store_identity_v2,
)

RUN_ID = "stv3-temperature-safe-evolve-v2-20990101T000000Z-r0"


def _entry(target: str) -> CanonicalEntryV2:
    content = (
        "Explicit cryogenic reagent signals generally favor a low-temperature regime."
        if target == "text_memory"
        else "Extract explicit thermal signals before comparing option temperature ranges."
    )
    return CanonicalEntryV2(
        entry_id="entry-" + ("a" if target == "text_memory" else "b") * 24,
        entry_type="category_rule" if target == "text_memory" else "reasoning_step",
        content=content,
        applicability="Use when the prompt exposes the corresponding thermal signal.",
        exclusion_conditions="Skip when an explicit contradictory operating condition is present.",
        support_uid_refs=("1" * 64, "2" * 64),
        supporting_batches=(1,),
        first_seen_batch=1,
        last_evaluated_block=1,
        support_count=2,
        contradiction_count=0,
        positive_flip_count=0,
        negative_flip_count=0,
        confidence=0.8,
        status="provisional",
        source_packet_sha256="3" * 64,
    )


@pytest.mark.parametrize("target", ("text_memory", "skill_bundle"))
def test_selected_target_core_update_is_plan_bound_unpromoted_then_promotable(
    tmp_path: Path, executable_registry, target: str
) -> None:
    store = create_safe_core_store_v2(
        db_path=(tmp_path / "core/evolution.sqlite3").resolve(),
        artifact_root=(tmp_path / "core/artifacts").resolve(),
        registry=executable_registry,
    )
    assert read_safe_core_store_identity_v2(
        db_path=(tmp_path / "core/evolution.sqlite3").resolve()
    ).startswith("store_")
    g0 = SafeArtifactStateV2.generation_zero(run_id=RUN_ID, fold_id="R0", selected_target=target)
    result = execute_provisional_update_v2(
        store=store,
        registry=executable_registry,
        run_id=RUN_ID,
        fold_id="R0",
        batch_index=1,
        selected_target=target,
        predecessor=g0,
        entries=(_entry(target),),
        source_packet_sha256="3" * 64,
        prior_evidence_sha256="4" * 64,
        reflector_receipt_sha256="5" * 64,
        forbidden_normalized_questions=(
            "private synthetic benchmark question that must not enter the artifact",
        ),
        evidence_root=(tmp_path / f"private/{target}").resolve(),
    )
    assert result.state.state_kind == "provisional"
    assert not result.state.promoted
    assert not store.get_artifact(result.state.core_artifact_id).promoted
    assert (
        result.state.core_payload_sha256
        == hashlib.sha256(_payload(store, result.state.core_artifact_id, target)).hexdigest()
    )

    active = promote_safe_state_v2(
        store=store,
        state=result.state,
        evidence_path=(tmp_path / f"promotion/{target}.json").resolve(),
    )
    assert active.state_kind == "active"
    assert active.promoted
    assert all(entry.status != "provisional" for entry in active.entries)
    assert store.get_artifact(active.core_artifact_id).promoted


@pytest.mark.parametrize("target", ("text_memory", "skill_bundle"))
def test_selected_target_core_update_can_represent_empty_runtime_projection(
    tmp_path: Path, executable_registry, target: str
) -> None:
    store = create_safe_core_store_v2(
        db_path=(tmp_path / f"core-empty-{target}/evolution.sqlite3").resolve(),
        artifact_root=(tmp_path / f"core-empty-{target}/artifacts").resolve(),
        registry=executable_registry,
    )
    g0 = SafeArtifactStateV2.generation_zero(run_id=RUN_ID, fold_id="R0", selected_target=target)
    result = execute_provisional_update_v2(
        store=store,
        registry=executable_registry,
        run_id=RUN_ID,
        fold_id="R0",
        batch_index=1,
        selected_target=target,
        predecessor=g0,
        entries=(),
        source_packet_sha256="3" * 64,
        prior_evidence_sha256="4" * 64,
        reflector_receipt_sha256="5" * 64,
        forbidden_normalized_questions=("private synthetic benchmark question",),
        evidence_root=(tmp_path / f"private-empty/{target}").resolve(),
    )

    assert result.state.projection == ""
    assert result.state.projection_utf8_bytes == 0
    assert result.state.entries == ()
    assert result.state.core_payload_utf8_bytes > 0
    active = promote_safe_state_v2(
        store=store,
        state=result.state,
        evidence_path=(tmp_path / f"promotion-empty/{target}.json").resolve(),
    )
    assert active.projection == ""
    assert active.entries == ()


def _payload(store, artifact_id: str, target: str) -> bytes:
    artifact = store.get_artifact(artifact_id)
    root = Path(artifact.uri.removeprefix("file://"))
    path = (
        root if root.is_file() else root / ("memory.md" if target == "text_memory" else "SKILL.md")
    )
    return path.read_bytes()
