from __future__ import annotations

from pathlib import Path

import pytest

from openevo_researchclawbench.artifact_loader import (
    ArtifactRef,
    admit_skill_bundle,
    admit_text_artifact,
    create_composite,
    load_composite,
)
from openevo_researchclawbench.cross_task_sanitizer import sanitize_text
from openevo_researchclawbench.hashing import sha256_file, tree_sha256
from openevo_researchclawbench.openevo_bridge import reject_synthetic_native_inputs


GENERAL_SYSTEM = "Always inspect data schemas, establish reproducible baselines, validate results, and trace every reported conclusion to saved outputs."


def test_text_artifact_accepts_general_method_and_stable_hash() -> None:
    first = admit_text_artifact("agent_system", "as001", GENERAL_SYSTEM)
    second = admit_text_artifact("agent_system", "as001", GENERAL_SYSTEM)
    assert first.accepted and second.accepted
    assert first.sha256 == second.sha256


def test_text_artifact_rejects_task_literal_absolute_path_and_evaluator_term() -> None:
    proposed = "For Life_005 read /home/user/data and optimize against the checklist with enough procedural padding words."
    result = admit_text_artifact("text_memory", "tm001", proposed)
    assert not result.accepted
    assert {"TASK_ID_LITERAL", "ABSOLUTE_PATH", "EVALUATOR_PRIVATE_TERM"} <= set(result.reasons)


def test_text_memory_allows_generic_answer_avoidance_but_rejects_answer_claim() -> None:
    generic = admit_text_artifact(
        "text_memory",
        "tm001",
        "Avoid copying exact prior answers into reusable memory; retain only general verification procedures.",
    )
    leaked = admit_text_artifact(
        "text_memory",
        "tm002",
        "The correct answer is a task-specific secret that should be reused in later attempts.",
    )

    assert generic.accepted
    assert not leaked.accepted
    assert "ANSWER_LIKE_MEMORY" in leaked.reasons


def test_skill_bundle_rejects_symlink_task_literal_and_network_logic(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "README.md").write_text("General data audit purpose for Life_005", encoding="utf-8")
    (skills / "fetch.py").write_text("import requests\nrequests.get('https://example.com')\n", encoding="utf-8")
    result = admit_skill_bundle("sk001", skills)
    assert not result.accepted
    assert "TASK_ID_LITERAL" in result.reasons
    assert "NETWORK_DOWNLOAD_LOGIC" in result.reasons
    (skills / "link").symlink_to("README.md")
    result = admit_skill_bundle("sk002", skills)
    assert not result.accepted
    assert "UNSAFE_FILESYSTEM_ENTRY" in result.reasons


def test_composite_versions_all_three_artifacts(tmp_path: Path) -> None:
    system = tmp_path / "as.md"
    memory = tmp_path / "tm.md"
    skills = tmp_path / "skills"
    skills.mkdir()
    system.write_text(GENERAL_SYSTEM, encoding="utf-8")
    memory.write_text("Retain general debugging lessons and validate each output before reporting conclusions.", encoding="utf-8")
    (skills / "README.md").write_text("General reproducibility checks.", encoding="utf-8")
    composite = create_composite(
        tmp_path / "c001",
        composite_revision="c001",
        parent_revision="c000",
        source_task_id="Life_005",
        source_attempt_id="Life_005_a0",
        agent_system=ArtifactRef("agent_system", "as001", system, sha256_file(system), "artifact-as001"),
        text_memory=ArtifactRef("text_memory", "tm001", memory, sha256_file(memory), "artifact-tm001"),
        skill_bundle=ArtifactRef("skill_bundle", "sk001", skills, tree_sha256(skills), "artifact-sk001"),
    )
    loaded = load_composite(composite.root)
    assert loaded.manifest["agent_system_revision"] == "as001"
    assert loaded.manifest["text_memory_revision"] == "tm001"
    assert loaded.manifest["skill_bundle_revision"] == "sk001"
    assert loaded.sha256 == composite.sha256
    assert loaded.manifest["agent_system_registry_id"] == "artifact-as001"
    assert loaded.manifest["text_memory_registry_id"] == "artifact-tm001"
    assert loaded.manifest["skill_bundle_registry_id"] == "artifact-sk001"
    with pytest.raises(ValueError, match="synthesize"):
        reject_synthetic_native_inputs({"event_type": "openevo.session_completed"})


def test_cross_task_sanitizer_removes_source_specific_lines() -> None:
    text = "Generalize schema validation across formats.\nLife_005 uses fig11_mix.csv and scored 123456.\n"
    result = sanitize_text(text, source_file_names=["fig11_mix.csv"])
    assert result.accepted
    assert "Generalize schema" in result.sanitized_text
    assert "Life_005" not in result.sanitized_text
    assert result.removed
