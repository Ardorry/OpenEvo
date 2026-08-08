from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openevo.evolution.training_feedback import (
    FeedbackClass,
    SealedSessionEvidence,
    TrainingFeedbackAttachmentStore,
    TrainingFeedbackEvolutionService,
    build_training_feedback_dataset_view,
    validate_training_feedback_dataset_view,
)
from openevo.evolution.models import (
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    DatasetCreateResponse,
)


def _canonical_pretty(value: object) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode()


def _source_dataset(tmp_path: Path) -> tuple[Path, SealedSessionEvidence]:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    record = {
        "event_id": "evt_1",
        "source": "openevo",
        "event_type": "openevo.session_completed",
        "source_event_id": "source_1",
        "task_id": "task_1",
        "session_id": "session_1",
        "status": "completed",
        "traces": [{"messages": [{"role": "assistant", "content": "done"}]}],
        "payload": {"session_result": {"metadata": {}}},
    }
    records = json.dumps(record, sort_keys=True).encode() + b"\n"
    (source / "records.jsonl").write_bytes(records)
    session_sha = "a" * 64
    manifest = {
        "dataset_id": "dataset_1",
        "query": {
            "event_types": ["openevo.session_completed"],
            "status": ["COMPLETED"],
            "task_id": "task_1",
            "session_id": "session_1",
        },
        "records_path": "records.jsonl",
        "records_uri": (source / "records.jsonl").as_uri(),
        "records_byte_size": len(records),
        "records_sha256": hashlib.sha256(records).hexdigest(),
        "source_event_evidence": {
            "event_id": "evt_1",
            "session_id": "session_1",
            "task_id": "task_1",
            "session_result_sha256": session_sha,
        },
    }
    manifest_bytes = _canonical_pretty(manifest)
    (source / "manifest.json").write_bytes(manifest_bytes)
    return source / "manifest.json", SealedSessionEvidence(
        event_id="evt_1",
        session_id="session_1",
        task_id="task_1",
        dataset_id="dataset_1",
        dataset_artifact_id="artifact_1",
        session_result_sha256=session_sha,
        dataset_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        completed=True,
        sealed=True,
    )


def _store(tmp_path: Path) -> tuple[TrainingFeedbackAttachmentStore, object]:
    store = TrainingFeedbackAttachmentStore(tmp_path / "attachments")
    return store, store.issue_evaluator_authority(producer="trusted_evaluator")


def test_sealed_session_attachment_and_hash_round_trip(tmp_path: Path) -> None:
    _, session = _source_dataset(tmp_path)
    store, authority = _store(tmp_path)
    attachment = store.attach(
        authority=authority,
        session=session,
        dataset_revision="rev_1",
        feedback_class=FeedbackClass.SOFT_JUDGE,
        global_feedback={"total_score": 0.5, "completed": True},
        task_local_feedback={},
    )
    assert store.get(attachment.attachment_id) == attachment
    assert attachment.authority.value == "evaluator_only"
    assert len(attachment.content_sha256) == 64


def test_uncompleted_session_attachment_is_rejected(tmp_path: Path) -> None:
    _, session = _source_dataset(tmp_path)
    with pytest.raises(ValueError, match="completed and sealed"):
        SealedSessionEvidence.model_validate(
            {**session.model_dump(), "completed": False}
        )


def test_non_evaluator_authority_and_overwrite_are_rejected(tmp_path: Path) -> None:
    _, session = _source_dataset(tmp_path)
    store, authority = _store(tmp_path)
    with pytest.raises(PermissionError, match="trusted evaluator"):
        store.attach(
            authority=object(),
            session=session,
            dataset_revision="rev_1",
            feedback_class=FeedbackClass.SOFT_JUDGE,
            global_feedback={"total_score": 0.5},
            task_local_feedback={},
        )
    attachment = store.attach(
        authority=authority,
        session=session,
        dataset_revision="rev_1",
        feedback_class=FeedbackClass.SOFT_JUDGE,
        global_feedback={"total_score": 0.5},
        task_local_feedback={},
    )
    path = store.root / f"{attachment.attachment_id}.json"
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        store._publish(attachment)
    assert path.read_bytes() == before


def test_soft_judge_raw_reasoning_and_unknown_fields_are_rejected(tmp_path: Path) -> None:
    _, session = _source_dataset(tmp_path)
    store, authority = _store(tmp_path)
    with pytest.raises(ValueError, match="evaluator-private"):
        store.attach(
            authority=authority,
            session=session,
            dataset_revision="rev_1",
            feedback_class=FeedbackClass.SOFT_JUDGE,
            global_feedback={"judge_reasoning": "private"},
            task_local_feedback={},
        )
    with pytest.raises(ValueError, match="non-allowlisted"):
        store.attach(
            authority=authority,
            session=session,
            dataset_revision="rev_1",
            feedback_class=FeedbackClass.SOFT_JUDGE,
            global_feedback={"per_item_score": 1},
            task_local_feedback={},
        )


@pytest.mark.parametrize("feedback_class", [FeedbackClass.HARD_GT, FeedbackClass.MIXED])
def test_hard_feedback_stays_task_local(
    tmp_path: Path, feedback_class: FeedbackClass
) -> None:
    manifest_path, session = _source_dataset(tmp_path)
    store, authority = _store(tmp_path)
    global_feedback = (
        {"completed": True, "total_score": 1.0}
        if feedback_class is FeedbackClass.MIXED
        else {"generic_failure_tags": ["wrong_structure"]}
    )
    attachment = store.attach(
        authority=authority,
        session=session,
        dataset_revision="rev_1",
        feedback_class=feedback_class,
        global_feedback=global_feedback,
        task_local_feedback={"correct_answer": "task-only-secret", "tolerance": 0.01},
    )
    receipt = build_training_feedback_dataset_view(
        source_manifest_path=manifest_path,
        attachments=(attachment,),
        output_dir=tmp_path / "view",
        task_id="task_1",
    )
    records = (tmp_path / "view" / "records.jsonl").read_text()
    assert "task-only-secret" not in records
    assert attachment.attachment_id in records
    assert receipt.attachment_ids == (attachment.attachment_id,)
    assert store.task_local_overlay(
        attachment.attachment_id, next_task_scope_id="task_1"
    )["correct_answer"] == "task-only-secret"
    with pytest.raises(ValueError, match="cannot cross"):
        store.task_local_overlay(
            attachment.attachment_id,
            next_task_scope_id="task_2",
        )


def test_hard_feedback_validation_ignores_matching_public_scalar_values(
    tmp_path: Path,
) -> None:
    manifest_path, session = _source_dataset(tmp_path)
    source_record_path = manifest_path.with_name("records.jsonl")
    source_record = json.loads(source_record_path.read_text())
    source_record["payload"]["token_level_metrics_available"] = False
    source_bytes = json.dumps(source_record, sort_keys=True).encode() + b"\n"
    source_record_path.write_bytes(source_bytes)
    source_manifest = json.loads(manifest_path.read_text())
    source_manifest["records_byte_size"] = len(source_bytes)
    source_manifest["records_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    updated_manifest_bytes = _canonical_pretty(source_manifest)
    manifest_path.write_bytes(updated_manifest_bytes)
    session = session.model_copy(
        update={
            "dataset_manifest_sha256": hashlib.sha256(
                updated_manifest_bytes
            ).hexdigest()
        }
    )
    store, authority = _store(tmp_path)
    attachment = store.attach(
        authority=authority,
        session=session,
        dataset_revision="rev_1",
        feedback_class=FeedbackClass.HARD_GT,
        global_feedback={},
        task_local_feedback={
            "feedback_source": "current_task_gt",
            "ground_truth_sha256": "b" * 64,
            "ground_truth_entries": [{"criterion": "private-ground-truth"}],
            "judge_feedback_included": False,
        },
    )
    output_dir = tmp_path / "view-common-false"
    built = build_training_feedback_dataset_view(
        source_manifest_path=manifest_path,
        attachments=(attachment,),
        output_dir=output_dir,
        task_id="task_1",
        resolution_id="resolution_common_false",
    )

    validated = validate_training_feedback_dataset_view(
        source_manifest_path=manifest_path,
        attachments=(attachment,),
        output_dir=output_dir,
        task_id="task_1",
        resolution_id="resolution_common_false",
    )

    assert validated == built
    assert "private-ground-truth" not in (output_dir / "records.jsonl").read_text()


def test_hard_feedback_validation_rejects_self_attested_task_local_injection(
    tmp_path: Path,
) -> None:
    manifest_path, session = _source_dataset(tmp_path)
    store, authority = _store(tmp_path)
    attachment = store.attach(
        authority=authority,
        session=session,
        dataset_revision="rev_1",
        feedback_class=FeedbackClass.HARD_GT,
        global_feedback={},
        task_local_feedback={"ground_truth_entries": ["private-ground-truth"]},
    )
    output_dir = tmp_path / "view-tampered"
    build_training_feedback_dataset_view(
        source_manifest_path=manifest_path,
        attachments=(attachment,),
        output_dir=output_dir,
        task_id="task_1",
        resolution_id="resolution_tampered",
    )
    records_path = output_dir / "records.jsonl"
    record = json.loads(records_path.read_text())
    record["payload"]["raw_gt"] = "private-ground-truth"
    tampered_bytes = json.dumps(
        record,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    records_path.write_bytes(tampered_bytes)
    manifest_out = output_dir / "manifest.json"
    manifest = json.loads(manifest_out.read_text())
    manifest["records_byte_size"] = len(tampered_bytes)
    manifest["records_sha256"] = hashlib.sha256(tampered_bytes).hexdigest()
    manifest_out.write_bytes(_canonical_pretty(manifest))

    with pytest.raises(
        ValueError,
        match="published training feedback view authority is inconsistent",
    ):
        validate_training_feedback_dataset_view(
            source_manifest_path=manifest_path,
            attachments=(attachment,),
            output_dir=output_dir,
            task_id="task_1",
            resolution_id="resolution_tampered",
        )


def test_wrong_task_and_missing_attachment_fail_closed(tmp_path: Path) -> None:
    manifest_path, session = _source_dataset(tmp_path)
    store, authority = _store(tmp_path)
    attachment = store.attach(
        authority=authority,
        session=session,
        dataset_revision="rev_1",
        feedback_class=FeedbackClass.SOFT_JUDGE,
        global_feedback={"total_score": 0.5},
        task_local_feedback={},
    )
    with pytest.raises(ValueError, match="task does not match"):
        build_training_feedback_dataset_view(
            source_manifest_path=manifest_path,
            attachments=(attachment,),
            output_dir=tmp_path / "view-wrong",
            task_id="task_2",
        )
    with pytest.raises(ValueError, match="requires an attachment"):
        build_training_feedback_dataset_view(
            source_manifest_path=manifest_path,
            attachments=(),
            output_dir=tmp_path / "view-empty",
            task_id="task_1",
        )


def test_official_mode_forbids_authority_attachment_and_view(tmp_path: Path) -> None:
    manifest_path, _ = _source_dataset(tmp_path)
    store = TrainingFeedbackAttachmentStore(tmp_path / "official", official_mode=True)
    with pytest.raises(ValueError, match="official frozen"):
        store.issue_evaluator_authority(producer="trusted_evaluator")
    with pytest.raises(ValueError, match="official frozen"):
        build_training_feedback_dataset_view(
            source_manifest_path=manifest_path,
            attachments=(),
            output_dir=tmp_path / "official-view",
            task_id="task_1",
            official_mode=True,
        )


def test_source_session_object_hash_is_unchanged(tmp_path: Path) -> None:
    manifest_path, session = _source_dataset(tmp_path)
    before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    store, authority = _store(tmp_path)
    attachment = store.attach(
        authority=authority,
        session=session,
        dataset_revision="rev_1",
        feedback_class=FeedbackClass.SOFT_JUDGE,
        global_feedback={"total_score": 0.5},
        task_local_feedback={},
    )
    build_training_feedback_dataset_view(
        source_manifest_path=manifest_path,
        attachments=(attachment,),
        output_dir=tmp_path / "derived",
        task_id="task_1",
    )
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == before


def test_trusted_service_registers_derived_dataset_with_attachment_receipt(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _source_dataset(tmp_path)

    class Registry:
        def __init__(self) -> None:
            self.source = ArtifactResponse(
                artifact_id="artifact_1",
                type=ArtifactType.DATASET,
                name="source",
                version=1,
                state=ArtifactState.ACTIVE,
                uri=manifest_path.as_uri(),
                manifest=json.loads(manifest_path.read_text()),
            )
            self.registered = []

        def get_dataset(self, dataset_id: str) -> DatasetCreateResponse:
            assert dataset_id == "dataset_1"
            return DatasetCreateResponse(
                dataset_id=dataset_id,
                artifact_id=self.source.artifact_id,
                event_count=1,
                trace_count=1,
            )

        def get_artifact(self, artifact_id: str) -> ArtifactResponse:
            assert artifact_id == self.source.artifact_id
            return self.source

        def register_artifact(self, request):
            self.registered.append(request)
            return ArtifactResponse(
                artifact_id="artifact_feedback_1",
                type=request.type,
                name=request.name,
                version=1,
                state=ArtifactState.ACTIVE,
                uri=request.uri,
                manifest=request.manifest,
                compatibility=request.compatibility,
                tags=request.tags,
                promoted=request.promoted,
            )

    registry = Registry()
    service = TrainingFeedbackEvolutionService(
        registry=registry,
        root=tmp_path / "service",
    )
    authority = service.issue_evaluator_authority(producer="trusted_evaluator")
    receipt = service.attach_and_register(
        authority=authority,
        dataset_id="dataset_1",
        session_id="session_1",
        task_id="task_1",
        task_scope_id="benchmark_task_1",
        feedback_class=FeedbackClass.MIXED,
        global_feedback={"completed": True, "total_score": 0.75},
        task_local_feedback={"correct_answer": "private-answer"},
    )
    assert receipt.dataset_artifact.artifact_id == "artifact_feedback_1"
    assert receipt.attachment.attachment_id in (
        receipt.dataset_artifact.manifest["training_feedback_attachment_ids"]
    )
    assert "private-answer" not in Path(receipt.dataset_artifact.uri.removeprefix("file://")).with_name(
        "records.jsonl"
    ).read_text()
    assert service.task_local_overlay(
        receipt.attachment.attachment_id,
        next_task_scope_id="benchmark_task_1",
    ) == {"correct_answer": "private-answer"}
