from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v2.artifacts import (
    inspect_supervised_auxiliary_artifact_v2,
)
from openevo_chembench.supervised_transfer_v2.readiness import (
    _contract_matrix_markdown,
    _failure_audit_markdown,
)
from openevo_chembench.supervised_transfer_v2.reflector_boundary import (
    _LAST_MESSAGE_OUTPUT_FILE,
    _LAST_MESSAGE_TERMINAL_TRANSCRIPT_RECOVERY,
    _SUPERVISED_MULTITARGET_RENDER_ID,
    _SUPERVISED_OUTPUT_SCHEMA,
    ReflectorBoundaryError,
    ReflectorBoundaryStatusV2,
    ReflectorExecutionReceiptV2,
    _constrained_supervised_output_schema_bytes,
    _parse_reflector_jsonl_transcript_v2,
    _read_last_message_with_terminal_recovery,
    _render_supervised_structured_auxiliary,
    _render_supervised_structured_memory,
    _supervised_structured_normalization_id,
    _validate_supervised_evidence_allowlist,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _valid_payload(*, category: str = "Temperature_Prediction") -> dict[str, object]:
    evidence = _sha(f"packet:{category}")
    return {
        "category": category,
        "confirmed_principles": [],
        "provisional_principles": [],
        "common_failure_modes": ["Do not confuse reaction feasibility with temperature."],
        "option_elimination_checks": ["Reject values outside chemically plausible bounds."],
        "retired_or_contradicted": [],
        "output_discipline": ["Return exactly one uppercase option."],
        "do": ["Check the dominant physical constraint."],
        "avoid": ["Avoid unsupported numerical interpolation."],
        "validate": ["Recheck units and qualitative trend."],
        "when_applicable": ["Apply only within the matching chemistry category."],
        "retired_or_superseded": [],
        "skill_when_to_use": ["When a temperature-sensitive chemistry choice is posed."],
        "skill_workflow": ["Identify the dominant thermodynamic or kinetic constraint."],
        "skill_validation_checks": ["Check that the selected range is chemically plausible."],
        "skill_failure_guards": ["Do not infer a precise value without supporting constraints."],
        "skill_evidence_digests": [evidence],
        "agent_system_directives": [
            {
                "trigger": "solving this chemistry category",
                "instruction": "prioritize explicit chemical constraints",
                "validation": "verify the final option against those constraints",
                "evidence_digests": [evidence],
            }
        ],
        "agent_system_output_discipline": ["Return exactly one uppercase option."],
    }


def _response(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _assert_rejected(response: str) -> None:
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        _supervised_structured_normalization_id(response)


def test_contract_schema_parser_and_artifact_validator_are_aligned() -> None:
    properties = _SUPERVISED_OUTPUT_SCHEMA["properties"]
    required = set(_SUPERVISED_OUTPUT_SCHEMA["required"])
    assert _SUPERVISED_OUTPUT_SCHEMA["additionalProperties"] is False
    assert required == set(properties)

    for field in (
        "skill_when_to_use",
        "skill_workflow",
        "skill_validation_checks",
        "skill_failure_guards",
        "agent_system_output_discipline",
    ):
        assert properties[field]["minItems"] == 1
        assert properties[field]["items"]["minLength"] == 1
        assert properties[field]["items"]["maxLength"] == 192
        assert properties[field]["maxItems"] == 4
        assert "default" not in properties[field]
    assert properties["agent_system_directives"]["minItems"] == 1
    assert properties["agent_system_directives"]["maxItems"] == 4
    for field in ("trigger", "instruction", "validation"):
        scalar = properties["agent_system_directives"]["items"]["properties"][field]
        assert scalar == {"type": "string", "minLength": 1, "maxLength": 192}

    payload = _valid_payload()
    response = _response(payload)
    assert _supervised_structured_normalization_id(response).endswith("render_v3")
    memory = _render_supervised_structured_memory(response)
    auxiliary = _render_supervised_structured_auxiliary(response)
    evidence = tuple(payload["skill_evidence_digests"])
    assert inspect_supervised_auxiliary_artifact_v2(
        auxiliary.skill_markdown.encode(),
        target_id="skill_bundle",
        category="Temperature_Prediction",
        source_evidence_digests=auxiliary.skill_evidence_digests,
        allowed_evidence_digests=frozenset(evidence),
        forbidden_literals=(),
    ).passed
    assert inspect_supervised_auxiliary_artifact_v2(
        auxiliary.agent_system_markdown.encode(),
        target_id="agent_system",
        category="Temperature_Prediction",
        source_evidence_digests=auxiliary.agent_system_evidence_digests,
        allowed_evidence_digests=frozenset(evidence),
        forbidden_literals=(),
    ).passed
    assert "# Category Memory: Temperature_Prediction" in memory


def test_runtime_schema_enumerates_only_core_authorized_evidence_digests() -> None:
    first = _sha("authorized-packet-1")
    second = _sha("authorized-packet-2")
    near_copy = first[:-12] + ("0" * 12)
    assert near_copy != first

    schema = json.loads(
        _constrained_supervised_output_schema_bytes(frozenset({first, second}))
    )
    properties = schema["properties"]
    expected = [first, second]
    for field in (
        "confirmed_principles",
        "provisional_principles",
        "retired_or_contradicted",
    ):
        items = properties[field]["items"]["properties"]["evidence_digests"][
            "items"
        ]
        assert items == {"type": "string", "enum": expected}
        assert near_copy not in items["enum"]
    assert properties["skill_evidence_digests"]["items"] == {
        "type": "string",
        "enum": expected,
    }
    assert properties["agent_system_directives"]["items"]["properties"][
        "evidence_digests"
    ]["items"] == {"type": "string", "enum": expected}
    records = [{"packet_sha256": second}]
    _validate_supervised_evidence_allowlist(records, frozenset({first, second}))
    with pytest.raises(
        ReflectorBoundaryError,
        match="REFLECTOR_OUTPUT_SCHEMA_BINDING_INVALID",
    ):
        _validate_supervised_evidence_allowlist(
            [{"packet_sha256": near_copy}],
            frozenset({first, second}),
        )


def test_auxiliary_renderer_is_bounded_by_core_method_config_contract() -> None:
    payload = _valid_payload()
    maximum = "x" * 192
    for field in (
        "skill_when_to_use",
        "skill_workflow",
        "skill_validation_checks",
        "skill_failure_guards",
        "agent_system_output_discipline",
    ):
        payload[field] = [maximum] * 4
    payload["agent_system_directives"] = [
        {
            "trigger": maximum,
            "instruction": maximum,
            "validation": maximum,
            "evidence_digests": payload["skill_evidence_digests"],
        }
        for _index in range(4)
    ]
    rendered = _render_supervised_structured_auxiliary(_response(payload))
    assert len(rendered.skill_markdown) <= 4096
    assert len(rendered.agent_system_markdown) <= 4096

    payload["skill_workflow"] = ["x" * 193]
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        _render_supervised_structured_auxiliary(_response(payload))


@pytest.mark.parametrize(
    ("mutation", "field"),
    (
        ("missing", "skill_workflow"),
        ("null", "skill_workflow"),
        ("empty_array", "skill_workflow"),
        ("empty_string", "skill_workflow"),
        ("wrong_type", "skill_workflow"),
        ("missing", "agent_system_directives"),
        ("null", "agent_system_directives"),
        ("empty_array", "agent_system_directives"),
        ("empty_string", "agent_system_output_discipline"),
    ),
)
def test_historical_empty_and_adjacent_required_field_shapes_fail_closed(
    mutation: str,
    field: str,
) -> None:
    payload = _valid_payload()
    if mutation == "missing":
        del payload[field]
    elif mutation == "null":
        payload[field] = None
    elif mutation == "empty_array":
        payload[field] = []
    elif mutation == "empty_string":
        payload[field] = [""]
    else:
        payload[field] = {"unexpected": True}
    response = _response(payload)
    if mutation == "missing":
        _assert_rejected(response)
    else:
        _supervised_structured_normalization_id(response)
        with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
            _render_supervised_structured_auxiliary(response)


@pytest.mark.parametrize(
    "response_factory",
    (
        lambda value: f"```json\n{value}\n```",
        lambda value: f"explanation\n{value}",
        lambda value: f"{value}\nexplanation",
        lambda value: f"{value}\n{value}",
        lambda value: value[:-1],
    ),
)
def test_non_exact_json_envelopes_fail_closed(response_factory) -> None:
    _assert_rejected(response_factory(_response(_valid_payload())))


def test_unknown_duplicate_and_nonfinite_json_fail_closed() -> None:
    payload = _valid_payload()
    payload["unknown_field"] = "forbidden"
    _assert_rejected(_response(payload))

    response = _response(_valid_payload())
    duplicate = response.replace(
        '"category":"Temperature_Prediction"',
        '"category":"Temperature_Prediction","category":"Temperature_Prediction"',
        1,
    )
    _assert_rejected(duplicate)

    nested_duplicate = response.replace(
        '"instruction":"prioritize explicit chemical constraints"',
        '"instruction":"prioritize explicit chemical constraints",'
        '"instruction":"prioritize explicit chemical constraints"',
        1,
    )
    _assert_rejected(nested_duplicate)

    _assert_rejected(response.replace('"category":"Temperature_Prediction"', '"category":NaN'))


def test_unicode_is_preserved_but_empty_and_oversized_scalars_fail_closed() -> None:
    payload = _valid_payload()
    payload["skill_workflow"] = ["先核对反应约束，再检查单位与数量级。"]
    auxiliary = _render_supervised_structured_auxiliary(_response(payload))
    assert "先核对反应约束" in auxiliary.skill_markdown

    payload["skill_workflow"] = [" "]
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        _render_supervised_structured_auxiliary(_response(payload))
    payload["skill_workflow"] = ["x" * 4097]
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        _render_supervised_structured_auxiliary(_response(payload))


def test_rule_semantics_reject_unsupported_confirmation_and_target_mismatch() -> None:
    payload = _valid_payload()
    evidence = _sha("one-packet")
    rule = {
        "rule_id": "temperature-constraint-v2",
        "target_type": "text_memory",
        "trigger": "when temperature bounds decide feasibility",
        "principle": "a feasible range must respect physical constraints",
        "action": "eliminate choices outside the feasible range",
        "validation": "check units and limiting assumptions",
        "evidence_count": 1,
        "evidence_digests": [evidence],
        "first_seen_cycle": 1,
        "last_confirmed_cycle": 1,
        "contradiction_count": 0,
    }
    payload["confirmed_principles"] = [rule]
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        _render_supervised_structured_memory(_response(payload))

    payload["confirmed_principles"] = []
    payload["provisional_principles"] = [rule]
    rule["target_type"] = "skill_bundle"
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        _render_supervised_structured_memory(_response(payload))


def _event_stream(*messages: str, terminal_last: bool = True) -> str:
    events: list[dict[str, object]] = [
        {"type": "thread.started"},
        {"type": "turn.started"},
        *(
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}}
            for message in messages
        ),
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        },
    ]
    if not terminal_last:
        events.append({"type": "item.completed", "item": {"type": "reasoning", "text": "late"}})
    return "\n".join(json.dumps(event, separators=(",", ":")) for event in events) + "\n"


def test_reflector_event_extraction_uses_last_assistant_and_requires_terminal_event() -> None:
    response, usage, _digest = _parse_reflector_jsonl_transcript_v2(
        _event_stream("first", "second")
    )
    assert response == "second"
    assert usage["output_tokens"] == 1
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_EVENT_STREAM_INVALID"):
        _parse_reflector_jsonl_transcript_v2(_event_stream("valid", terminal_last=False))


def test_missing_redundant_last_message_recovers_only_from_terminal_transcript(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "last-message.md"
    terminal = _response(_valid_payload())
    content, transport = _read_last_message_with_terminal_recovery(
        missing,
        terminal_message=terminal,
        maximum=65536,
        allow_recovery=True,
    )
    assert content.decode() == terminal
    assert transport == _LAST_MESSAGE_TERMINAL_TRANSCRIPT_RECOVERY
    assert not missing.exists()
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_REQUIRED_FILE_MISSING"):
        _read_last_message_with_terminal_recovery(
            missing,
            terminal_message=terminal,
            maximum=65536,
            allow_recovery=False,
        )


def test_existing_last_message_remains_authoritative(tmp_path: Path) -> None:
    path = tmp_path / "last-message.md"
    path.write_text("exact", encoding="utf-8")
    content, transport = _read_last_message_with_terminal_recovery(
        path,
        terminal_message="different",
        maximum=65536,
        allow_recovery=True,
    )
    assert content == b"exact"
    assert transport == _LAST_MESSAGE_OUTPUT_FILE


def test_recovered_transport_is_explicit_in_bound_receipt() -> None:
    source = (_response(_valid_payload()).strip() + "\n").encode()
    rendered = _render_supervised_structured_memory(source.decode()).encode()
    receipt = ReflectorExecutionReceiptV2(
        invocation_id="a" * 32,
        status=ReflectorBoundaryStatusV2.COMPLETED,
        mechanism="bubblewrap",
        wrapper_invoked=True,
        real_codex_sha256=_sha("codex"),
        dev_artifact_sha256=_sha("dataset"),
        ordered_records_sha256=_sha("records"),
        record_count=1,
        event_stream_sha256=_sha("events"),
        event_counts=(),
        private_event_reference=f"{'a' * 32}/events.jsonl",
        last_message_sha256=hashlib.sha256(rendered).hexdigest(),
        cleanup_complete=True,
        retry_allowed=False,
        resume_allowed=False,
        replacement_completion_allowed=False,
        codex_returncode=0,
        protocol_id="chembench_supervised_transfer_v2",
        source_split="supervised_train",
        projected_prompt_sha256=_sha("prompt"),
        source_last_message_sha256=hashlib.sha256(source).hexdigest(),
        output_normalization_id=_SUPERVISED_MULTITARGET_RENDER_ID,
        output_normalization_applied=True,
        last_message_transport=_LAST_MESSAGE_TERMINAL_TRANSCRIPT_RECOVERY,
    )
    payload = receipt.to_payload()
    assert payload["schema_version"] == "chembench4k_reflector_execution_receipt_v6"
    assert payload["last_message_transport"] == "terminal_transcript_recovery"
    assert ReflectorExecutionReceiptV2.from_payload(payload) == receipt


def test_historical_fixture_is_content_free_and_digest_bound() -> None:
    fixture = Path(__file__).with_name("fixtures") / "historical_failure_shapes_v2.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert payload["privacy"] == "content_free_digest_bound"
    assert len(payload["real_run_failures"]) >= 7
    for failure in payload["real_run_failures"]:
        assert set(failure) == {
            "run_id",
            "stage",
            "failure_code",
            "shape",
            "expected_replay",
            "run_state_sha256",
        }
        assert len(failure["run_state_sha256"]) == 64
        assert failure["expected_replay"] == "REJECTED_FAIL_CLOSED_WITH_EXPECTED_CODE"


def test_all_six_nonempty_auxiliary_fields_replay_the_015_failure_shape() -> None:
    fields = (
        "skill_when_to_use",
        "skill_workflow",
        "skill_validation_checks",
        "skill_failure_guards",
        "agent_system_directives",
        "agent_system_output_discipline",
    )
    for field in fields:
        payload = deepcopy(_valid_payload())
        payload[field] = []
        with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
            _render_supervised_structured_auxiliary(_response(payload))


def test_machine_readable_contract_and_failure_audit_cover_the_paid_boundaries() -> None:
    matrix = _contract_matrix_markdown()
    assert (
        "Round 0 -> Cycle 1 -> Round 1 -> Cycle 2 -> Round 2 -> Cycle 3 -> Round 3 Final"
        in matrix
    )
    for field in (
        "skill_when_to_use",
        "skill_workflow",
        "skill_validation_checks",
        "skill_failure_guards",
        "agent_system_directives",
        "agent_system_output_discipline",
    ):
        assert f"| {field} | yes |" in matrix
    audit = _failure_audit_markdown()
    for finding in (
        "REFLECTOR_LAST_MESSAGE_INVALID",
        "REFLECTOR_EVENT_STREAM_INVALID",
        "TASKWISE_CORE_JOB_FAILED",
        "TASKWISE_ARTIFACT_VALIDATION_FAILED",
        "TASKWISE_ARTIFACT_PROMOTION_FAILED",
        "TASKWISE_CONTEXT_RESOLUTION_FAILED",
        "EXECUTOR_STALLED",
        "FINAL_TEST_LEDGER_DUPLICATE_COMPLETION",
    ):
        assert finding in audit
