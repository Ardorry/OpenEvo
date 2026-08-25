"""Explicit MOCK server used only for the zero-paid OpenEvo Core smoke test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import uvicorn

from openevo_chemcrow.openrouter_shim import create_openrouter_shim_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8400)
    args = parser.parse_args()

    async def mock_upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        call_id = payload["user"]
        content = json.dumps(
            {
                "student_a": {
                    "grade": 8.0,
                    "strengths": ["MOCK schema validation"],
                    "weaknesses": [],
                    "justification": "MOCK response for a zero-paid Core route smoke.",
                    "feedback": [],
                },
                "student_b": {
                    "grade": 7.0,
                    "strengths": ["MOCK schema validation"],
                    "weaknesses": [],
                    "justification": "MOCK response for a zero-paid Core route smoke.",
                    "feedback": [],
                },
            },
            sort_keys=True,
        )
        return httpx.Response(
            200,
            json={
                "id": "mock-" + call_id,
                "model": "openai/gpt-4",
                "provider": "OpenAI",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 100,
                    "total_tokens": 200,
                },
            },
        )

    app = create_openrouter_shim_app(
        api_key="MOCK_NOT_A_REAL_KEY",
        base_url="http://mock.invalid/v1",
        receipt_root=args.receipt_root,
        transport=httpx.MockTransport(mock_upstream),
        mock_mode=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
