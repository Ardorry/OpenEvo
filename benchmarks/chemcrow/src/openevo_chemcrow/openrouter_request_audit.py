"""Sanitized descriptors for actual outgoing OpenRouter HTTP requests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx


def sanitized_request_descriptor(request: httpx.Request) -> dict[str, Any]:
    """Describe transmitted bytes without retaining prompt, response, or auth values."""

    body = bytes(request.content)
    payload = json.loads(body)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("OpenRouter request messages are invalid")
    prompt_bytes = b"\n".join(
        str(message.get("content") or "").encode("utf-8") for message in messages
    )
    return {
        "request_body_sha256": hashlib.sha256(body).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "model": payload.get("model"),
        "temperature": payload.get("temperature"),
        "max_tokens": payload.get("max_tokens"),
        "stream": payload.get("stream"),
        "user_present": "user" in payload,
        "response_format_present": "response_format" in payload,
        "provider": payload.get("provider"),
        "top_level_field_names_sorted": sorted(payload),
        "http_header_names_sorted": sorted(name.casefold() for name in request.headers),
        "body_byte_length": len(body),
        "prompt_byte_length": len(prompt_bytes),
        "message_count": len(messages),
        "message_roles": [message.get("role") for message in messages],
    }
