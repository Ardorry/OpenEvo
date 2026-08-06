"""OpenRouter Judge client used by the evaluator worker adapter.

This module owns all OpenRouter transport concerns: request construction,
Azure-only provider routing, HTTP execution, failure classification, probe
calls and per-request non-secret metadata.  The official ResearchClawBench
scorer stays transport-free and receives one rubric result through an injected
``judge_client`` callable.

GPT-5.1/Azure does not accept a ``temperature`` parameter, so it is never
included in the request body.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .hashing import canonical_json_bytes

REQUESTED_JUDGE_PROVIDER = "azure"
OPENROUTER_PROVIDER_BODY = {
    "provider": {
        "only": ["azure"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
}
JUDGE_FAILURE_CATEGORIES = frozenset(
    {
        "BLOCKED_JUDGE_REGION",
        "BLOCKED_JUDGE_AUTH",
        "BLOCKED_JUDGE_QUOTA",
        "BLOCKED_JUDGE_RATE_LIMIT",
        "JUDGE_PROVIDER_FAILED",
        "JUDGE_RESPONSE_INVALID",
    }
)

JudgeClient = Callable[[str, str, list[Path] | None], dict[str, Any]]


class JudgeTransportError(RuntimeError):
    """The Judge request failed before an HTTP status was available."""


def _judge_token_limit_kwargs(
    model_version: str, max_tokens: int | None
) -> dict[str, int]:
    """Return the Chat Completions token-limit argument for the model family."""
    if max_tokens is None:
        return {}
    model_name = (model_version or "").lower().strip().rsplit("/", 1)[-1]
    if model_name.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def build_judge_payload(
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None,
) -> dict[str, Any]:
    """Build the OpenRouter request body with Azure-only routing."""

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    payload.update(_judge_token_limit_kwargs(model, max_tokens))
    payload["provider"] = {
        "only": ["azure"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    return payload


def build_judge_messages(
    system_prompt: str,
    prompt: str,
    image_paths: list[Path] | None,
) -> list[dict[str, Any]]:
    """Build Chat Completions messages, embedding images as data URLs."""

    if image_paths:
        content: Any = [{"type": "text", "text": prompt}]
        for image_path in image_paths:
            data_url = _image_to_data_url(image_path)
            if data_url is not None:
                content.append(
                    {"type": "image_url", "image_url": {"url": data_url}}
                )
    else:
        content = prompt
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def _image_to_data_url(path: Path) -> str | None:
    """Encode a raster image as a data URL for vision scoring."""
    import base64
    import io

    try:
        from PIL import Image

        with Image.open(path) as image:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return None


def http_judge_post(
    *,
    api_key: str,
    api_base: str,
    payload: dict[str, Any],
    timeout_seconds: int = 120,
) -> tuple[int, dict[str, Any], str]:
    """POST one OpenRouter Judge request.

    Returns ``(http_status, body, raw_text)``.  HTTP error responses are
    returned as data; transport failures raise :class:`JudgeTransportError`.
    """

    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload, allow_nan=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": "ResearchClawBench Judge",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw_text = response.read().decode("utf-8")
            body = json.loads(raw_text)
            if not isinstance(body, dict):
                body = {"error": {"message": "Judge response body is not an object"}}
            return int(response.status), body, raw_text
    except urllib.error.HTTPError as exc:
        raw_text = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw_text)
        except json.JSONDecodeError:
            body = {"error": {"message": raw_text[:500]}}
        if not isinstance(body, dict):
            body = {"error": {"message": raw_text[:500]}}
        return int(exc.code), body, raw_text
    except Exception as exc:
        raise JudgeTransportError(str(exc)) from exc


def classify_judge_http_status(status: int | None, body: Any) -> str:
    """Map an OpenRouter HTTP failure to a closed failure category."""
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    message = str(error.get("message") or "")
    code = str(error.get("code") or "")
    text = f"{message} {code}".casefold()
    if status == 401:
        return "BLOCKED_JUDGE_AUTH"
    if status == 402:
        return "BLOCKED_JUDGE_QUOTA"
    if status == 429:
        return "BLOCKED_JUDGE_RATE_LIMIT"
    if status == 403:
        if "region" in text or "country" in text or "not available" in text:
            return "BLOCKED_JUDGE_REGION"
        return "JUDGE_PROVIDER_FAILED"
    if isinstance(status, int) and 500 <= status < 600:
        return "JUDGE_PROVIDER_FAILED"
    return "JUDGE_PROVIDER_FAILED"


def classify_judge_failure(exc: BaseException) -> str:
    """Classify an exception carrying OpenRouter status/body."""
    return classify_judge_http_status(
        getattr(exc, "status_code", None),
        getattr(exc, "body", None),
    )


def _coerce_judge_score(value: Any) -> float | None:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        return None
    return score


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        try:
            dumped = usage.model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, dict):
            usage = dumped
    if isinstance(usage, dict):
        return {
            key: value
            for key, value in usage.items()
            if isinstance(value, (int, float))
        }
    return None


def _failure_result(
    *,
    rubric_id: str,
    category: str,
    detail: str,
    requested_model: str,
    http_status: int | None,
    returned_model: str | None,
    returned_provider: str | None,
    request_id: str | None,
    parse_status: str,
    latency_ms: int,
    attempts: int,
) -> dict[str, Any]:
    if category not in JUDGE_FAILURE_CATEGORIES:
        raise ValueError(f"unknown Judge failure category: {category}")
    return {
        "rubric_id": rubric_id,
        "score": None,
        "reasoning": detail,
        "score_valid": False,
        "judge_completed": False,
        "failure_category": category,
        "requested_model": requested_model,
        "requested_provider": REQUESTED_JUDGE_PROVIDER,
        "returned_model": returned_model,
        "returned_provider": returned_provider,
        "http_status": http_status,
        "request_id": request_id,
        "response_hash": None,
        "parse_status": parse_status,
        "latency_ms": latency_ms,
        "retry_count": attempts,
        "http_requests": attempts,
        "response_body_present": False,
        "content_present": False,
        "json_parse_success": False,
        "schema_valid": False,
        "usage": None,
    }


def _success_result(
    *,
    rubric_id: str,
    score: float,
    reasoning: str,
    requested_model: str,
    returned_model: str,
    returned_provider: str,
    request_id: str | None,
    response_hash: str,
    latency_ms: int,
    attempts: int,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "rubric_id": rubric_id,
        "score": score,
        "reasoning": reasoning,
        "score_valid": True,
        "judge_completed": True,
        "failure_category": None,
        "requested_model": requested_model,
        "requested_provider": REQUESTED_JUDGE_PROVIDER,
        "returned_model": returned_model,
        "returned_provider": returned_provider,
        "http_status": 200,
        "request_id": request_id,
        "response_hash": response_hash,
        "parse_status": "ok",
        "latency_ms": latency_ms,
        "retry_count": attempts,
        "http_requests": attempts,
        "response_body_present": True,
        "content_present": True,
        "json_parse_success": True,
        "schema_valid": True,
        "usage": usage,
    }


def make_judge_client(
    *,
    api_key: str,
    api_base: str,
    model: str,
    max_try: int = 2,
    timeout_seconds: int = 120,
) -> JudgeClient:
    """Build the evaluator worker Judge client with Azure-only routing."""

    def judge(
        rubric_id: str,
        prompt: str,
        image_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        messages = build_judge_messages(
            "You are a strict scientific peer reviewer evaluating AI-generated "
            "research. Score the report against the criterion only - do not "
            "attempt to solve the research task yourself.",
            prompt,
            image_paths,
        )
        attempts = max(1, int(max_try or 1))
        last_category: str | None = None
        last_status: int | None = None
        last_returned_model: str | None = None
        last_returned_provider: str | None = None
        last_request_id: str | None = None
        last_parse_status = "provider_failed"
        http_requests = 0
        total_latency_ms = 0
        for _ in range(attempts):
            started = time.monotonic()
            try:
                payload = build_judge_payload(model, messages, 500)
                http_status, body, raw_text = http_judge_post(
                    api_key=api_key,
                    api_base=api_base,
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                )
            except JudgeTransportError:
                total_latency_ms += int((time.monotonic() - started) * 1000)
                http_requests += 1
                last_category = "JUDGE_PROVIDER_FAILED"
                last_status = None
                last_parse_status = "transport_error"
                continue
            total_latency_ms += int((time.monotonic() - started) * 1000)
            http_requests += 1
            last_status = http_status
            if not isinstance(body, dict):
                body = {"error": {"message": "Judge response body is not an object"}}
            last_returned_model = body.get("model")
            last_returned_provider = body.get("provider")
            last_request_id = body.get("id")
            if http_status != 200:
                last_category = classify_judge_http_status(http_status, body)
                last_parse_status = "http_error"
                continue
            response_hash = hashlib.sha256(
                canonical_json_bytes(body)
            ).hexdigest()
            choices = body.get("choices")
            content = None
            if choices:
                try:
                    message = choices[0].get("message", {})
                    content = message.get("content")
                except (IndexError, AttributeError, TypeError):
                    content = None
            if not isinstance(content, str) or not content.strip():
                last_category = "JUDGE_RESPONSE_INVALID"
                last_parse_status = "empty_content"
                continue
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                last_category = "JUDGE_RESPONSE_INVALID"
                last_parse_status = "invalid_json"
                continue
            if not isinstance(parsed, dict):
                last_category = "JUDGE_RESPONSE_INVALID"
                last_parse_status = "schema_invalid"
                continue
            score = _coerce_judge_score(parsed.get("score"))
            reasoning = parsed.get("reasoning")
            if score is None or not isinstance(reasoning, str) or not reasoning.strip():
                last_category = "JUDGE_RESPONSE_INVALID"
                last_parse_status = "schema_invalid"
                continue
            return _success_result(
                rubric_id=rubric_id,
                score=score,
                reasoning=reasoning.strip(),
                requested_model=model,
                returned_model=last_returned_model or model,
                returned_provider=last_returned_provider or "",
                request_id=last_request_id,
                response_hash=response_hash,
                latency_ms=total_latency_ms,
                attempts=http_requests,
                usage=_usage_dict(body.get("usage")),
            )
        return _failure_result(
            rubric_id=rubric_id,
            category=last_category or "JUDGE_RESPONSE_INVALID",
            detail=f"Judge request failed: {last_parse_status}",
            requested_model=model,
            http_status=last_status,
            returned_model=last_returned_model,
            returned_provider=last_returned_provider,
            request_id=last_request_id,
            parse_status=last_parse_status,
            latency_ms=total_latency_ms,
            attempts=http_requests,
        )

    return judge


def run_judge_probe(
    *,
    api_key: str,
    api_base: str,
    model: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run one minimal Azure-routed Judge request with no task content."""
    if not api_key or not api_base or not model:
        return {
            "status": "JUDGE_SUBPROCESS_FAILED",
            "failure_category": "BLOCKED_JUDGE_AUTH",
            "http_status": None,
            "requested_model": model,
            "requested_provider": REQUESTED_JUDGE_PROVIDER,
            "returned_model": None,
            "returned_provider": None,
            "content": None,
            "error_detail": "Judge credentials are incomplete",
        }
    messages = [
        {
            "role": "user",
            "content": "Reply with exactly: JUDGE_SUBPROCESS_OK",
        }
    ]
    try:
        payload = build_judge_payload(model, messages, 16)
        http_status, body, _raw = http_judge_post(
            api_key=api_key,
            api_base=api_base,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
    except JudgeTransportError as exc:
        error_detail = str(exc)
        if api_key:
            error_detail = error_detail.replace(api_key, "[REDACTED]")
        return {
            "status": "JUDGE_SUBPROCESS_FAILED",
            "failure_category": "JUDGE_PROVIDER_FAILED",
            "http_status": None,
            "requested_model": model,
            "requested_provider": REQUESTED_JUDGE_PROVIDER,
            "returned_model": None,
            "returned_provider": None,
            "content": None,
            "error_detail": error_detail,
        }
    error = body.get("error", {}) if isinstance(body, dict) else {}
    error_detail = (
        str(error.get("message") or "")
        if isinstance(error, dict)
        else str(body)[:500]
    )
    if api_key:
        error_detail = error_detail.replace(api_key, "[REDACTED]")
    if http_status != 200 or not isinstance(body, dict):
        return {
            "status": "JUDGE_SUBPROCESS_FAILED",
            "failure_category": classify_judge_http_status(http_status, body),
            "http_status": http_status,
            "requested_model": model,
            "requested_provider": REQUESTED_JUDGE_PROVIDER,
            "returned_model": body.get("model") if isinstance(body, dict) else None,
            "returned_provider": (
                body.get("provider") if isinstance(body, dict) else None
            ),
            "content": None,
            "error_detail": error_detail,
        }
    choices = body.get("choices")
    content = None
    if choices:
        try:
            message = choices[0].get("message", {})
            content = message.get("content")
        except (IndexError, AttributeError, TypeError):
            content = None
    returned_model = body.get("model")
    returned_provider = body.get("provider")
    if not isinstance(returned_model, str):
        returned_model = None
    if not isinstance(returned_provider, str):
        returned_provider = None
    stripped = content.strip() if isinstance(content, str) else ""
    ok = stripped == "JUDGE_SUBPROCESS_OK"
    return {
        "status": "JUDGE_SUBPROCESS_OK" if ok else "JUDGE_SUBPROCESS_FAILED",
        "failure_category": None if ok else "JUDGE_RESPONSE_INVALID",
        "http_status": 200,
        "requested_model": model,
        "requested_provider": REQUESTED_JUDGE_PROVIDER,
        "returned_model": returned_model,
        "returned_provider": returned_provider,
        "content": stripped if isinstance(content, str) else None,
        "error_detail": None if ok else "probe content differs",
    }
