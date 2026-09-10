"""OpenEvo Core harness for one sealed ChemCrow paper-evaluator call."""

from __future__ import annotations

import re
import shlex

from openevo.harness.base import BaseHarness
from openevo.runtime.models import ExecInput

from .paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_TEMPERATURE,
)

_RUNTIME_CLIENT = r"""
import json
import os
import urllib.request

def emit_transcript(content):
    print(json.dumps({"role": "assistant", "content": content}), flush=True)


try:
    base_url = os.environ["OPENAI_BASE_URL"].rstrip("/")
    payload = {
        "model": os.environ["PAPER_EVALUATOR_MODEL"],
        "temperature": float(os.environ["PAPER_EVALUATOR_TEMPERATURE"]),
        "max_tokens": int(os.environ["PAPER_EVALUATOR_MAX_TOKENS"]),
        "stream": False,
        "user": os.environ["PAPER_EVALUATOR_CALL_ID"],
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
            },
            {"role": "user", "content": os.environ["PAPER_EVALUATOR_PROMPT"]},
        ],
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise TypeError("paper evaluator response content is not text")
except Exception as exc:
    # Core's transcript builder requires a stable stdout authority even when
    # transport or response-envelope validation fails.  Emit only a closed,
    # value-free category before retaining the non-zero process exit.
    emit_transcript(
        json.dumps(
            {
                "schema_version": "paper_evaluator_transport_error_v1",
                "error_type": type(exc).__name__,
            },
            sort_keys=True,
        )
    )
    raise

# Preserve the exact model text before strict downstream JSON validation.  If
# validation fails, the process remains failed, but Gateway postrun still has
# the verified stdout transcript needed to report the real agent failure.
emit_transcript(content)
json.loads(content)
""".strip()

PAPER_RUNTIME_GATEWAY_BASE_URL = "http://host.docker.internal:8110/v1"


class PaperEvaluatorHarness(BaseHarness):
    """Execute the judge inside Core's runtime and through Core's Gateway.

    The OpenRouter key is never present in this runtime.  Core injects a
    session-scoped Gateway credential, and the dedicated local auth shim owns
    the actual upstream credential.
    """

    runtime_gateway_base_url = PAPER_RUNTIME_GATEWAY_BASE_URL

    def _validate_call_id(self, call_id: str) -> None:
        match = re.fullmatch(
            r"(paper(?:-[a-z0-9]+)*)-(chemcrow-[0-9]{2})-"
            r"(historical_control|baseline|evolved|direct|blind)"
            r"(?:-replacement-01)?",
            call_id,
        )
        if (
            match is None
            or "-cal-" in match.group(1)
            or match.group(2) not in FROZEN_PAPER_TASK_IDS
        ):
            raise ValueError("paper evaluator call ID is absent or invalid")

    def run_steps(self, instruction: str) -> list[ExecInput]:
        call_id = self.env.get("PAPER_EVALUATOR_CALL_ID", "")
        self._validate_call_id(call_id)
        if self.model_name != PAPER_EVALUATOR_MODEL:
            raise ValueError("paper evaluator model is not the frozen openai/gpt-4 model")
        if float(self.settings.get("temperature", -1)) != PAPER_EVALUATOR_TEMPERATURE:
            raise ValueError("paper evaluator temperature must be exactly 0.1")
        if int(self.settings.get("max_tokens", -1)) != PAPER_EVALUATOR_MAX_OUTPUT_TOKENS:
            raise ValueError("paper evaluator max_tokens differs from the frozen limit")
        runtime_gateway = str(self.settings.get("runtime_gateway_base_url") or "")
        if runtime_gateway != self.runtime_gateway_base_url:
            raise ValueError("paper runtime Gateway address differs from the frozen Core route")
        if any(name.startswith("OPENEVO_") and "ARTIFACT" in name for name in self.env):
            raise ValueError("paper evaluator must not consume evolution artifacts")
        return [
            ExecInput(
                command=f"python3 -c {shlex.quote(_RUNTIME_CLIENT)}",
                env={
                    # Docker Desktop's container loopback is not the WSL host.
                    # Only the address is adapted; OPENAI_API_KEY remains the
                    # Core-injected, session-scoped Gateway credential.
                    "OPENAI_BASE_URL": self.runtime_gateway_base_url,
                    "PAPER_EVALUATOR_CALL_ID": call_id,
                    "PAPER_EVALUATOR_MODEL": PAPER_EVALUATOR_MODEL,
                    "PAPER_EVALUATOR_TEMPERATURE": str(PAPER_EVALUATOR_TEMPERATURE),
                    "PAPER_EVALUATOR_MAX_TOKENS": str(PAPER_EVALUATOR_MAX_OUTPUT_TOKENS),
                    "PAPER_EVALUATOR_PROMPT": instruction,
                },
            )
        ]


class PaperCalibrationHarness(PaperEvaluatorHarness):
    """Calibration-only harness with a distinct call namespace and Core node."""

    runtime_gateway_base_url = "http://host.docker.internal:8210/v1"

    def _validate_call_id(self, call_id: str) -> None:
        if not call_id.startswith("paper-chemcrow-cal-v1-") or not all(
            character.isalnum() or character in "-_" for character in call_id
        ):
            raise ValueError("paper calibration call ID is absent or invalid")
