from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from openevo_chembench.frozen_runtime_v2 import (
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.taskwise_context_binding_v1 import (
    CONTEXT_BINDING_VIOLATION,
    TaskwiseContextBindingError,
    TaskwiseSessionContextBindingV1,
    issue_taskwise_context_binding_receipt_v1,
)


def _memory():
    markdown = "## Do\n- Verify.\n"
    return _issue_core_resolved_text_memory_v2(
        core_artifact_id="artifact_taskwise_context_1",
        artifact_payload_sha256="a" * 64,
        context_resolution_digest="b" * 64,
        resolved_memory_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        markdown=markdown,
    )


def _binding() -> TaskwiseSessionContextBindingV1:
    return TaskwiseSessionContextBindingV1.from_memory(
        session_id="session_context_0001",
        memory=_memory(),
    )


def test_binding_records_exact_session_artifact_memory_and_context() -> None:
    binding = _binding()

    assert binding.to_dict() == {
        "session_id": "session_context_0001",
        "core_artifact_id": "artifact_taskwise_context_1",
        "artifact_payload_sha256": "a" * 64,
        "resolved_memory_sha256": hashlib.sha256("## Do\n- Verify.\n".encode()).hexdigest(),
        "context_resolution_digest": "b" * 64,
    }
    assert binding.memory_present is True
    assert len(binding.digest) == 64

    empty = TaskwiseSessionContextBindingV1.from_memory(
        session_id="session_context_0002",
        memory=None,
    )
    assert empty.memory_present is False
    assert all(value is None for key, value in empty.to_dict().items() if key != "session_id")


@pytest.mark.parametrize(
    "actual",
    (
        replace(_binding(), session_id="session_context_9999"),
        replace(_binding(), core_artifact_id="artifact_taskwise_context_2"),
        replace(_binding(), artifact_payload_sha256="c" * 64),
        replace(_binding(), resolved_memory_sha256="d" * 64),
        replace(_binding(), context_resolution_digest="e" * 64),
    ),
)
def test_every_expected_actual_context_mismatch_has_one_closed_finding(
    actual: TaskwiseSessionContextBindingV1,
) -> None:
    receipt = issue_taskwise_context_binding_receipt_v1(
        expected=_binding(),
        actual=actual,
    )

    assert receipt.passed is False
    assert receipt.finding_codes == (CONTEXT_BINDING_VIOLATION,)
    assert receipt.to_public_dict()["finding_codes"] == ["TASKWISE_CONTEXT_BINDING_VIOLATION"]
    with pytest.raises(
        TaskwiseContextBindingError,
        match="TASKWISE_CONTEXT_BINDING_VIOLATION",
    ):
        receipt.require_match()


def test_matching_receipt_is_content_free_and_cannot_accept_caller_pass_boolean() -> None:
    binding = _binding()
    receipt = issue_taskwise_context_binding_receipt_v1(
        expected=binding,
        actual=binding,
    )

    assert receipt.passed is True
    assert receipt.finding_codes == ()
    receipt.require_match()
    public = receipt.to_public_dict()
    assert public["expected_session_id"] == public["session_id"]
    assert public["expected_memory_artifact_id"] == public["actual_resolved_artifact_id"]
    assert public["expected_memory_sha256"] == public["actual_injected_memory_sha256"]
    assert public["expected_context_resolution_digest"] == public["context_resolution_digest"]
    assert "prompt" not in repr(public).casefold()
    assert "completion" not in repr(public).casefold()
    assert "target" not in repr(public).casefold()
    with pytest.raises(TypeError):
        type(receipt)(expected=binding, actual=binding, passed=True)


def test_actual_injected_memory_hash_is_computed_from_markdown_bytes() -> None:
    expected = _binding()
    actual = TaskwiseSessionContextBindingV1.from_injected_markdown(
        session_id=expected.session_id,
        resolved_artifact_id=expected.core_artifact_id,
        artifact_payload_sha256=expected.artifact_payload_sha256,
        context_resolution_digest=expected.context_resolution_digest,
        injected_markdown="## Do\n- Different bytes reached the prompt.\n",
    )
    receipt = issue_taskwise_context_binding_receipt_v1(
        expected=expected,
        actual=actual,
    )

    assert (
        receipt.actual_injected_memory_sha256
        == hashlib.sha256("## Do\n- Different bytes reached the prompt.\n".encode()).hexdigest()
    )
    assert receipt.actual_injected_memory_sha256 != receipt.expected_memory_sha256
    assert receipt.finding_codes == (CONTEXT_BINDING_VIOLATION,)
