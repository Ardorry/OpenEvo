from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    write_private_file,
)
from openevo_chembench.temperature_full_evolve_v1.execution import FormalCallEnvelopeV1
from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    CanonicalEntryV2,
    SafeArtifactStateV2,
)
from openevo_chembench.temperature_safe_evolve_v2.candidate import (
    CandidateContextV2,
    prepare_candidate_call_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.execution import (
    SafeExecutionError,
    SafeFormalExecutionV2,
)
from openevo_chembench.temperature_safe_evolve_v2.ledger import SafeExperimentLedgerV2
from openevo_chembench.temperature_safe_evolve_v2.packet import (
    ReflectorArmRecordV2,
    ReflectorTrainItemV2,
    build_reflector_packet_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.reflector import (
    EntryProposalV2,
    SafeReflectionV2,
    SafeReflectorError,
    _merge_reflection,
    prepare_reflector_call_v2,
)

RUN_ID = "stv3-temperature-safe-evolve-v2-20990101T000000Z-r0"


class _EmptyLedger:
    def accepted_call(self, _logical: str) -> None:
        return None

    def latest_claim(self, _logical: str) -> None:
        return None

    def failure_has_no_completion(self, _call_id: str) -> bool:
        return False


@dataclass
class _Runtime:
    repository_root: Path
    digest: str = "d" * 64
    service_run_id: str = "stv3-temperature-safe-services-20990101T000000Z-12345678"
    rollout_url: str = "http://127.0.0.1:8080"

    def require_current(self) -> dict[str, object]:
        return {"runtime_services_identity_sha256": self.digest}


def _tasks(repository_root: Path):
    root = (
        repository_root / "data/chembench4k/AI4Chem_ChemBench4K/"
        "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
    )
    loader = ChemBench4KDatasetLoader(snapshot_root=root)
    return (
        tuple(loader.load_category("Temperature_Prediction", split="test")),
        tuple(loader.load_category("Temperature_Prediction", split="dev")),
    )


def _packet(repository_root: Path):
    tasks, _dev = _tasks(repository_root)
    train = tasks[:25]
    g0 = SafeArtifactStateV2.generation_zero(
        run_id=RUN_ID, fold_id="R0", selected_target="text_memory"
    )
    items = tuple(
        ReflectorTrainItemV2(
            task=task,
            arms=(
                ReflectorArmRecordV2(
                    logical_label="g0",
                    response=task.target,
                    response_sha256=hashlib.sha256(task.target.encode()).hexdigest(),
                    parsed_prediction=task.target,
                    parser_status="parsed",
                    correct=True,
                    context_hash="c" * 64,
                    selected_entry_ids=(),
                ),
            ),
        )
        for task in train
    )
    packet = build_reflector_packet_v2(
        run_id=RUN_ID,
        fold_id="R0",
        batch_index=1,
        selected_target="text_memory",
        active_state=g0,
        items=items,
        cumulative_evidence={
            "schema_version": "TemperatureSafeCumulativeEvidenceV2",
            "entry_evidence": {},
            "candidate_history": [],
            "mandatory_retirement_entry_ids": [],
        },
        promotion_history=(),
        aggregate_forward_history=(),
        seen_train_uids=frozenset(task.uid for task in train),
        seen_train_tasks=train,
        test_tasks=tasks[25:75],
    )
    return packet, train


def test_candidate_generation_zero_context_hash_deduplicates_logical_labels(
    repository_root: Path,
) -> None:
    tasks, dev = _tasks(repository_root)
    task = tasks[0]
    prompt = render_official_five_shot_prompt(task.to_public(), category_dev=dev)
    plans = [
        prepare_candidate_call_v2(
            task=task.to_public(),
            prompt=prompt,
            context=CandidateContextV2.generation_zero(),
            context_workspace=None,
            phase="train",
            block_id="b1",
            task_ordinal=0,
            ledger=_EmptyLedger(),
            run_id=RUN_ID,
            service_identity_sha256="d" * 64,
        )
        for _ in ("g0", "active")
    ]
    assert plans[0].logical_call_id == plans[1].logical_call_id
    assert plans[0].logical_call_id.endswith(f"-c{plans[0].context_hash}")
    assert plans[0].task_request_sha256 == plans[1].task_request_sha256
    assert plans[0].task_request.agent.settings["tool_policy"] == "disabled"
    assert plans[0].task_request.agent.model_name == "gpt-5.5"
    assert plans[0].task_request.agent.settings["reasoning_effort"] == "medium"


def test_accepted_reflector_recovers_synthesis_without_second_model_call(
    tmp_path: Path, repository_root: Path
) -> None:
    packet, train = _packet(repository_root)
    ledger_path = (tmp_path / "events.jsonl").resolve()
    checkpoint_root = (tmp_path / "calls").resolve()
    synthesis = (tmp_path / "reflection/accepted.json").resolve()
    with SafeExperimentLedgerV2(path=ledger_path, run_id=RUN_ID) as ledger:
        plan = prepare_reflector_call_v2(
            packet=packet,
            ledger=ledger,
            service_identity_sha256="d" * 64,
        )
        ledger.append("CALL_CLAIMED", plan.claim_payload)
        envelope = FormalCallEnvelopeV1(
            logical_call_id=plan.logical_call_id,
            call_id=plan.call_id,
            task_request=plan.task_request,
            task_request_sha256=plan.task_request_sha256,
            claim_payload=dict(plan.claim_payload),
        )
        checkpoint_root.mkdir(mode=0o700)
        write_private_file(
            checkpoint_root / f"{plan.call_id}.json",
            envelope.to_private_checkpoint_bytes(),
            replace=False,
        )
        supports = tuple(sorted((train[0].uid, train[1].uid)))
        entry = CanonicalEntryV2(
            entry_id="entry-" + "a" * 24,
            entry_type="category_rule",
            content="Explicit cryogenic reagent signals usually favor a low-temperature regime.",
            applicability="Use only when a cryogenic reagent signal is explicit in the prompt.",
            exclusion_conditions="Skip when heating or reflux is explicitly required.",
            support_uid_refs=supports,
            supporting_batches=(1,),
            first_seen_batch=1,
            last_evaluated_block=1,
            support_count=2,
            contradiction_count=0,
            positive_flip_count=0,
            negative_flip_count=0,
            confidence=0.8,
            status="provisional",
            source_packet_sha256=packet.packet_sha256,
        )
        response = (
            canonical_json_bytes(
                {
                    "schema_version": "TemperatureSafeReflectionV2",
                    **packet.required_response_bindings,
                    "proposals": [{"action": "add", "entry": entry.model_dump(mode="json")}],
                }
            )
            .decode()
            .strip()
        )
        accepted = {
            "logical_call_id": plan.logical_call_id,
            "call_id": plan.call_id,
            "response": response,
            "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
            "task_result_sha256": "1" * 64,
            "transcript_sha256": "2" * 64,
            "completion_identity_sha256": "3" * 64,
            "tool_event_count": 0,
            "tool_policy_validated": True,
        }
        ledger.append("CALL_ACCEPTED", accepted)
        engine = SafeFormalExecutionV2(
            runtime=_Runtime(repository_root),  # type: ignore[arg-type]
            ledger=ledger,
            checkpoint_root=checkpoint_root,
            cooldown_seconds=0,
            candidate_logical_limit=500,
            reflector_logical_limit=4,
        )
        engine._executor = SimpleNamespace(  # type: ignore[assignment]
            run_many_to_durable_terminal=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("accepted reflection must not call the model again")
            )
        )
        recovered = engine.run_reflector(packet=packet, synthesis_path=synthesis)
        repeated = engine.run_reflector(packet=packet, synthesis_path=synthesis)

        assert recovered.synthesis_receipt == repeated.synthesis_receipt
        assert len(recovered.merged_entries_payload) == 1
        assert sum(event["kind"] == "CALL_ACCEPTED" for event in ledger.events) == 1
        assert sum(event["kind"] == "REFLECTOR_ACCEPTED" for event in ledger.events) == 1
        assert json.loads(synthesis.read_bytes())["packet_sha256"] == packet.packet_sha256

        tampered = json.loads(synthesis.read_bytes())
        tampered["synthesis_receipt"]["proposal_count"] = 99
        write_private_file(synthesis, canonical_json_bytes(tampered), replace=True)
        with pytest.raises(SafeExecutionError, match="SAFE_REFLECTOR_SYNTHESIS_RECEIPT_DRIFT"):
            engine.run_reflector(packet=packet, synthesis_path=synthesis)


def test_reflector_rejects_entry_level_source_packet_binding_mismatch(
    repository_root: Path,
) -> None:
    packet, train = _packet(repository_root)
    supports = tuple(sorted((train[0].uid, train[1].uid)))
    entry = CanonicalEntryV2(
        entry_id="entry-" + "b" * 24,
        entry_type="category_rule",
        content="Explicit cryogenic reagent signals usually favor a low-temperature regime.",
        applicability="Use only when a cryogenic reagent signal is explicit in the prompt.",
        exclusion_conditions="Skip when heating or reflux is explicitly required.",
        support_uid_refs=supports,
        supporting_batches=(1,),
        first_seen_batch=1,
        last_evaluated_block=1,
        support_count=2,
        contradiction_count=0,
        positive_flip_count=0,
        negative_flip_count=0,
        confidence=0.8,
        status="provisional",
        source_packet_sha256="f" * 64,
    )
    reflection = SafeReflectionV2(
        **packet.required_response_bindings,
        proposals=(EntryProposalV2(action="add", entry=entry),),
    )

    with pytest.raises(SafeReflectorError, match="SAFE_REFLECTOR_RESPONSE_BINDING_INVALID"):
        _merge_reflection(reflection, packet)
