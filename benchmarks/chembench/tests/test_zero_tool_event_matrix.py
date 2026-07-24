from __future__ import annotations

import json

import pytest

from openevo_chembench.local_codex_executor import (
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
    _parse_jsonl_transcript,
)


@pytest.mark.parametrize(
    "item_type",
    (
        "command_execution",
        "file_read",
        "mcp_tool_call",
        "web_search",
        "network_call",
        "browser_action",
        "computer_action",
        "plugin_call",
        "app_call",
        "subagent_call",
    ),
)
def test_every_non_message_item_fails_closed(item_type: str) -> None:
    transcript = "\n".join(
        json.dumps(event, sort_keys=True)
        for event in (
            {"type": "thread.started", "thread_id": "thread-safe"},
            {"type": "turn.started"},
            {
                "type": "item.started",
                "item": {"id": "disallowed", "type": item_type},
            },
        )
    )

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(transcript)

    assert raised.value.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
    assert raised.value.run_status == "SECURITY_TOOL_USE_VIOLATION"
    assert raised.value.retry_allowed is False
    assert raised.value.resume_allowed is False
    assert raised.value.replacement_completion_allowed is False


def test_unknown_top_level_tool_like_event_fails_closed() -> None:
    transcript = "\n".join(
        json.dumps(event, sort_keys=True)
        for event in (
            {"type": "thread.started", "thread_id": "thread-safe"},
            {"type": "turn.started"},
            {"type": "plugin.activated"},
        )
    )

    with pytest.raises(LocalCodexExecutionError) as raised:
        _parse_jsonl_transcript(transcript)

    assert raised.value.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
    assert raised.value.event_counts == {"plugin": 1}
