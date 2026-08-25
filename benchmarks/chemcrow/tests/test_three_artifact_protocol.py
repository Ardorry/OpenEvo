from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo_chemcrow.cli import _bounded_task_prefix
from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.models import (
    ArtifactKind,
    EvaluatorFeedback,
    FeedbackMode,
    RubricScores,
    TaskItem,
    Trajectory,
)
from openevo_chemcrow.protocol import BlindJudgeResult
from openevo_chemcrow.three_artifact_evolution import (
    ThreeIsolatedEvolutionEngine,
    _strict_reflector_content,
    detect_artifact_duplicates,
)
from openevo_chemcrow.three_artifact_models import (
    THREE_ARTIFACT_ORDER,
    ArtifactSeparationPolicy,
    CoreInjectionReceiptSummary,
    ThreeArtifactBundleReceipt,
    ThreeArtifactReceipt,
)
from openevo_chemcrow.three_artifact_protocol import (
    ThreeArtifactTaskLocalProtocolRunner,
)
from openevo_chemcrow.three_artifact_runtime import (
    _three_artifact_injection_receipt,
    candidate_pair_request_parity,
)


class FakeThreeCandidate:
    config_sha256 = "s0"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[ArtifactKind, str]]] = []

    def run_candidate_with_receipt(self, *, task, role, artifact_ids_by_type, pair_id, mcp_url):
        mapping = dict(artifact_ids_by_type)
        self.calls.append((task.task_id, role, mapping))
        ids = [mapping[kind] for kind in THREE_ARTIFACT_ORDER] if mapping else []
        trajectory = Trajectory(
            run_id=f"{pair_id}-{role}",
            task_id=task.task_id,
            role=role,
            status="COMPLETED",
            answer=f"{role} answer for {task.task_id}",
            candidate_config_sha256=self.config_sha256,
            artifact_ids=ids,
        )
        receipt = None
        if role == "evolved":
            receipt = CoreInjectionReceiptSummary(
                schema_version="3",
                receipt_sha256="9" * 64,
                memory_artifact_id=mapping[ArtifactKind.TEXT_MEMORY],
                skill_artifact_id=mapping[ArtifactKind.SKILL_BUNDLE],
                agent_system_artifact_id=mapping[ArtifactKind.AGENT_SYSTEM],
            )
        return trajectory, receipt


class FakeThreeEvolution:
    def __init__(self) -> None:
        self.inputs: list[dict] = []

    def evolve_all(self, *, task, baseline, feedback_payload, pair_id):
        self.inputs.append(json.loads(json.dumps(feedback_payload)))
        evidence_hash = canonical_sha256(
            {
                "task": {
                    "task_id": task.task_id,
                    "prompt": task.prompt,
                    "category": task.broad_category,
                    "allowed_tool_metadata": task.allowed_tool_metadata,
                    "safety_metadata": task.safety_metadata,
                    "sanitized_item_sha256": task.sanitized_item_sha256,
                },
                "baseline": baseline.model_dump(mode="json"),
                "feedback": feedback_payload,
            }
        )
        artifacts = []
        for index, kind in enumerate(THREE_ARTIFACT_ORDER):
            artifacts.append(
                ThreeArtifactReceipt(
                    task_id=task.task_id,
                    pair_id=pair_id,
                    parent_run_id=baseline.run_id,
                    reflector_job_id=f"job-{pair_id}-{index}",
                    reflector_run_id=f"run-{pair_id}-{index}",
                    model="gpt-5.5",
                    system_prompt_hash=f"{index + 1}" * 64,
                    prompt_hash=f"{index + 4}" * 64,
                    input_evidence_hash=evidence_hash,
                    artifact_type=kind,
                    artifact_id=f"art-{pair_id}-{kind.value}",
                    artifact_hash=f"{index + 7}" * 64,
                    normalized_text_hash=f"{index + 3}" * 64,
                    size_bytes=100,
                    generation_time_seconds=0.1,
                    registration_receipt_sha256=f"{index + 6}" * 64,
                )
            )
        return ThreeArtifactBundleReceipt(
            task_id=task.task_id,
            pair_id=pair_id,
            parent_run_id=baseline.run_id,
            input_evidence_hash=evidence_hash,
            separation_policy=ArtifactSeparationPolicy(),
            artifacts=artifacts,
        )


class FakeInternalEvaluator:
    evaluator_id = "internal-gpt-5.5-config"

    def __init__(self) -> None:
        self.roles: list[str] = []

    def evaluate(self, *, task, trajectory):
        self.roles.append(trajectory.role)
        return EvaluatorFeedback(
            evaluator_role="evolution_evaluator",
            evaluator_run_id=f"internal-{trajectory.run_id}",
            scores=RubricScores(
                chemical_correctness=2,
                reasoning_quality=2,
                task_completion=2,
            ),
            actionable_critique=["verify observable evidence"],
        )


class FakeFinalEvaluator:
    evaluator_id = "final-gpt-5.5-independent-config"

    def compare(self, *, task, answer_a, answer_b):
        return BlindJudgeResult(
            scores_a=RubricScores(
                chemical_correctness=2,
                reasoning_quality=2,
                task_completion=2,
            ),
            scores_b=RubricScores(
                chemical_correctness=3,
                reasoning_quality=3,
                task_completion=3,
            ),
            winner="B",
            confidence=0.7,
        )


class FakeCoreReflector:
    def __init__(self, kind: ArtifactKind, content: str) -> None:
        self.kind = kind
        self.content = content
        self.config_sha256 = f"config-{kind.value}"
        self.candidate = {"agent": {"model_name": "gpt-5.5"}}
        self.prompts: list[str] = []

    def run_candidate(self, *, task, role, artifact_ids, pair_id, mcp_url):
        self.prompts.append(task.prompt)
        key = {
            ArtifactKind.TEXT_MEMORY: "memory",
            ArtifactKind.SKILL_BUNDLE: "skill_markdown",
            ArtifactKind.AGENT_SYSTEM: "agent_system_markdown",
        }[self.kind]
        return Trajectory(
            run_id=f"{pair_id}-{self.kind.value}",
            task_id=task.task_id,
            role="baseline",
            status="COMPLETED",
            answer=json.dumps(
                {"artifact_type": self.kind.value, key: self.content}, sort_keys=True
            ),
            candidate_config_sha256=self.config_sha256,
        )


def _another_task(task_item: TaskItem) -> TaskItem:
    payload = task_item.model_dump(mode="json")
    payload["task_id"] = "chemcrow-test-02"
    payload["prompt"] = "A second independent chemistry task."
    payload["sanitized_item_sha256"] = canonical_sha256(
        {"task_id": payload["task_id"], "prompt": payload["prompt"]}
    )
    return TaskItem.model_validate(payload)


def test_planned_task14_prefix_does_not_start_task15(task_item):
    second = _another_task(task_item)
    selected = _bounded_task_prefix([task_item, second], stop_after_task_id=task_item.task_id)
    assert [item.task_id for item in selected] == [task_item.task_id]
    with pytest.raises(ValueError, match="exactly one configured task"):
        _bounded_task_prefix([task_item, second], stop_after_task_id="missing")


def test_three_pipeline_protocol_reset_lineage_and_scoring_order(tmp_path, task_item):
    candidate = FakeThreeCandidate()
    evolution = FakeThreeEvolution()
    internal = FakeInternalEvaluator()
    runner = ThreeArtifactTaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=candidate,
        evolution=evolution,
        evolution_evaluator=internal,
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=tmp_path / "claims",
    )
    first = runner.run_item(task_item, pair_id="pair-1")
    second = runner.run_item(_another_task(task_item), pair_id="pair-2")

    assert internal.roles == ["baseline", "evolved", "baseline", "evolved"]
    assert [call[1] for call in candidate.calls] == [
        "baseline",
        "evolved",
        "baseline",
        "evolved",
    ]
    assert candidate.calls[0][2] == candidate.calls[2][2] == {}
    assert set(candidate.calls[1][2]) == set(candidate.calls[3][2]) == set(THREE_ARTIFACT_ORDER)
    assert not set(candidate.calls[1][2].values()) & set(candidate.calls[3][2].values())
    for result in (first, second):
        assert len(result.artifact_bundle.artifacts) == 3
        assert len({item.reflector_job_id for item in result.artifact_bundle.artifacts}) == 3
        assert len({item.prompt_hash for item in result.artifact_bundle.artifacts}) == 3
        reset = json.loads((tmp_path / "runs" / result.pair_id / "reset.receipt.json").read_text())
        assert reset["artifact_inventory_after"] == []
        assert reset["runtime_context_after"] == "bare_s0"
        assert reset["memory_after"] == reset["skill_bundle_after"] == []
        assert reset["agent_system_after"] == []
    assert all("paper" not in json.dumps(payload).lower() for payload in evolution.inputs)


def test_native_three_reflectors_are_independent_and_sibling_blind(tmp_path, task_item):
    contents = {
        ArtifactKind.TEXT_MEMORY: (
            "Observed fact: the baseline omitted a returned molecular-weight observation. "
            "Remember the verified observation and its units for this retry."
        ),
        ArtifactKind.SKILL_BUNDLE: (
            "# Verification workflow\n1. Parse the question.\n2. Select the declared tool.\n"
            "3. Compare the observation with the drafted claim.\n4. Report uncertainty."
        ),
        ArtifactKind.AGENT_SYSTEM: (
            "# Same-task behavior\n- Ground claims in visible tool observations.\n"
            "- Suppress unsupported chemical details.\n- Verify units before composing the answer."
        ),
    }
    ports = {kind: FakeCoreReflector(kind, content) for kind, content in contents.items()}
    engine = ThreeIsolatedEvolutionEngine(
        run_root=tmp_path / "runs",
        reflector_rollouts=ports,
        evolution_db_path=tmp_path / "evolution.sqlite3",
        evolution_artifact_root=tmp_path / "artifacts",
        separation_policy=ArtifactSeparationPolicy(),
    )
    baseline = Trajectory(
        run_id="baseline-run",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="A short baseline answer.",
        candidate_config_sha256="s0",
    )
    bundle = engine.evolve_all(
        task=task_item,
        baseline=baseline,
        feedback_payload={
            "mode": "F0",
            "observable_trajectory": baseline.model_dump(mode="json"),
        },
        pair_id="pair-native-three",
    )
    assert len(bundle.artifacts) == 3
    assert len({item.reflector_job_id for item in bundle.artifacts}) == 3
    assert len({item.reflector_run_id for item in bundle.artifacts}) == 3
    assert len({item.prompt_hash for item in bundle.artifacts}) == 3
    assert {item.model for item in bundle.artifacts} == {"gpt-5.5"}
    assert {item.input_evidence_hash for item in bundle.artifacts} == {bundle.input_evidence_hash}
    for kind, port in ports.items():
        assert len(port.prompts) == 1
        sibling_contents = [text for sibling, text in contents.items() if sibling != kind]
        assert all(content not in port.prompts[0] for content in sibling_contents)
        assert "paper EvaluatorGPT grades" in port.prompts[0]
    manifests = list((tmp_path / "artifacts" / "artifacts").rglob("*.json"))
    assert len(manifests) >= 3


@pytest.mark.parametrize(
    "artifacts, expected_key",
    [
        (
            {
                ArtifactKind.TEXT_MEMORY: "same bytes",
                ArtifactKind.SKILL_BUNDLE: "same bytes",
                ArtifactKind.AGENT_SYSTEM: "different policy",
            },
            "byte_identical_pairs",
        ),
        (
            {
                ArtifactKind.TEXT_MEMORY: "Same, normalized TEXT!",
                ArtifactKind.SKILL_BUNDLE: "same normalized text",
                ArtifactKind.AGENT_SYSTEM: "different policy",
            },
            "normalized_identical_pairs",
        ),
        (
            {
                ArtifactKind.TEXT_MEMORY: "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour twentyfive twentysix twentyseven twentyeight twentynine thirty",
                ArtifactKind.SKILL_BUNDLE: "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour twentyfive twentysix twentyseven twentyeight twentynine changed",
                ArtifactKind.AGENT_SYSTEM: "ground every claim and verify all chemical units before answering",
            },
            "near_duplicate_pairs",
        ),
    ],
)
def test_duplicate_guards_fail_closed(artifacts, expected_key):
    findings = detect_artifact_duplicates(
        artifacts,
        baseline_answer="unrelated baseline answer",
        policy=ArtifactSeparationPolicy(),
    )
    assert findings[expected_key]


def test_reflector_strict_type_specific_schemas():
    assert (
        _strict_reflector_content(
            '{"artifact_type":"text_memory","memory":"remember this"}',
            kind=ArtifactKind.TEXT_MEMORY,
        )
        == "remember this"
    )
    with pytest.raises(ValueError, match="schema"):
        _strict_reflector_content(
            '{"artifact_type":"text_memory","skill_markdown":"wrong"}',
            kind=ArtifactKind.TEXT_MEMORY,
        )
    with pytest.raises(ValueError, match="Markdown fence"):
        _strict_reflector_content(
            '```json\n{"artifact_type":"text_memory","memory":"x"}\n```',
            kind=ArtifactKind.TEXT_MEMORY,
        )


def test_core_injection_receipt_exact_type_mapping_and_no_fourth_artifact():
    mapping = {
        ArtifactKind.TEXT_MEMORY: "art-memory",
        ArtifactKind.SKILL_BUNDLE: "art-skill",
        ArtifactKind.AGENT_SYSTEM: "art-agent",
    }
    raw = {
        "schema_version": "3",
        "artifacts": [
            {
                "artifact_id": mapping[kind],
                "artifact_type": kind.value,
                "runtime_paths": [f"evolution/{kind.value}"],
            }
            for kind in THREE_ARTIFACT_ORDER
        ],
    }
    payload = {
        "results": [
            {
                "trajectory": {
                    "metadata": {
                        "task_metadata": {
                            "evolution": {
                                "context_injected": True,
                                "context_artifact_ids": [
                                    mapping[kind] for kind in THREE_ARTIFACT_ORDER
                                ],
                                "runtime_injection_receipt": raw,
                            }
                        }
                    }
                }
            }
        ]
    }
    summary = _three_artifact_injection_receipt(payload, expected=mapping, role="evolved")
    assert summary is not None and summary.artifact_count == 3
    raw["artifacts"].append(
        {
            "artifact_id": "art-fourth",
            "artifact_type": "text_memory",
            "runtime_paths": ["evolution/fourth"],
        }
    )
    with pytest.raises(Exception, match="exactly three"):
        _three_artifact_injection_receipt(payload, expected=mapping, role="evolved")


def test_candidate_pair_parity_changes_only_artifact_authority(task_item, core_candidate_config):
    receipt = candidate_pair_request_parity(
        task=task_item,
        candidate=core_candidate_config,
        mcp_url="http://127.0.0.1:9/mcp",
    )
    assert receipt["all_invariant_fields_equal"] is True
    assert receipt["only_allowed_metadata_drift"] is True
    assert receipt["baseline_context_artifact_ids"] == []
    assert receipt["evolved_artifact_types"] == [
        "text_memory",
        "skill_bundle",
        "agent_system",
    ]


def test_three_artifact_source_has_no_host_codex_or_shell_bypass():
    sources = []
    module_root = Path(__file__).resolve().parents[1] / "src" / "openevo_chemcrow"
    for name in (
        "three_artifact_evolution.py",
        "three_artifact_runtime.py",
        "three_artifact_protocol.py",
    ):
        sources.append((module_root / name).read_text(encoding="utf-8"))
    source = "\n".join(sources)
    assert "codex exec" not in source
    assert "subprocess" not in source
    assert "mcp_servers" not in source
