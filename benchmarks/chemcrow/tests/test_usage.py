import json

import pytest

from openevo_chemcrow.usage import summarize_codex_usage


def completion(root, name, events, *, status="COMPLETED", duplicate_trace=False):
    directory = root / f"task_{name}"
    directory.mkdir()
    transcript = "\n".join(json.dumps(e) for e in events)
    traces = [{"metadata": {"transcript": transcript}}]
    if duplicate_trace:
        traces *= 2
    path = directory / f"ses_{name}.json"
    path.write_text(
        json.dumps(
            {
                "task_id": name,
                "session_id": name,
                "status": status,
                "trajectory": {"traces": traces},
            }
        )
    )
    return path


def test_counts_real_turn_fields_without_double_counting_transcript_attachments(tmp_path):
    event = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 25,
            "reasoning_output_tokens": 15,
        },
    }
    completion(tmp_path, "a", [event, event], duplicate_trace=True)
    result = summarize_codex_usage(tmp_path)
    assert result["reported_turns"] == 2  # Identical counters can belong to distinct turns.
    assert result["duplicate_transcript_attachments_skipped"] == 1
    assert result["observed_totals"] == {
        "input_tokens": 200,
        "cached_input_tokens": 160,
        "output_tokens": 50,
        "reasoning_output_tokens": 30,
    }
    assert result["provider_account_token_total"] is None
    assert result["subscription_cost_usd"] is None
    assert result["infrastructure_canary_usage_included"] is False


def test_missing_usage_is_not_reported_as_zero_provider_usage(tmp_path):
    completion(tmp_path, "a", [{"type": "turn.completed"}])
    completion(tmp_path, "b", [], status="ERROR")
    result = summarize_codex_usage(tmp_path)
    assert result["observed_totals"] == {}
    assert result["completed_without_reported_usage"] == 1
    assert result["turns_without_usage"] == 1
    assert result["status"] == "NO_USAGE_RECORDED"


@pytest.mark.parametrize("bad_value", [-1, True, 1.5, "42"])
def test_invalid_counter_is_not_counted_as_a_billing_fact(tmp_path, bad_value):
    completion(
        tmp_path,
        "a",
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": bad_value,
                    "output_tokens": 5,
                },
            }
        ],
    )
    result = summarize_codex_usage(tmp_path)
    assert result["invalid_usage_events"] == 1
    assert result["observed_totals"] == {}


def test_summary_excludes_transcript_bodies_and_binds_source_bytes(tmp_path):
    path = completion(
        tmp_path,
        "a",
        [
            {"type": "item.completed", "item": {"text": "PRIVATE_ANSWER_SENTINEL"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
        ],
    )
    result = summarize_codex_usage(tmp_path)
    assert "PRIVATE_ANSWER_SENTINEL" not in json.dumps(result)
    before = result["source_inventory_sha256"]
    path.write_text(path.read_text() + "\n")
    assert summarize_codex_usage(tmp_path)["source_inventory_sha256"] != before


def test_conflicting_completion_identity_is_rejected(tmp_path):
    path = completion(tmp_path, "a", [])
    payload = json.loads(path.read_text())
    payload["status"] = "ERROR"
    (path.parent / "ses_conflict.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="conflicting completion identity"):
        summarize_codex_usage(tmp_path)
