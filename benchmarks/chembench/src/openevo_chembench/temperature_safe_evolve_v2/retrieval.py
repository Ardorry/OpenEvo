"""Deterministic, GT-blind lexical retrieval and sparse payload rendering."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from openevo_chembench.chembench4k_dataset import normalize_benchmark_text
from openevo_chembench.chembench4k_models import PublicChemBench4KTask
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    CanonicalEntryV2,
    SafeArtifactStateV2,
    SelectedTarget,
)

RETRIEVER_ID = "deterministic_temperature_lexical_retrieval_v1"
_TOKEN = re.compile(r"[a-z]+(?:[0-9]+)?|[0-9]+(?:\.[0-9]+)?|°c|[+\-]?[0-9]+", re.ASCII)
_LIMITS = {
    "text_memory": (2, 1000, 1200),
    "skill_bundle": (1, 500, 700),
}


class SafeRetrievalError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class RetrievedEntryV2:
    entry_id: str
    score: tuple[int, int, int, int, int]
    rendered: str

    def __repr__(self) -> str:
        return f"RetrievedEntryV2(entry_id={self.entry_id!r}, <content-redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class RetrievalReceiptV2:
    target_id: SelectedTarget
    eligible_entry_ids: tuple[str, ...]
    selected: tuple[RetrievedEntryV2, ...]
    retrieval_input_sha256: str
    injected_payload: str
    injected_payload_sha256: str
    injected_utf8_bytes: int
    context_hash_component: str

    def __post_init__(self) -> None:
        if (
            tuple(sorted(self.eligible_entry_ids)) != self.eligible_entry_ids
            or len(set(self.eligible_entry_ids)) != len(self.eligible_entry_ids)
            or any(value.entry_id not in self.eligible_entry_ids for value in self.selected)
            or len(self.injected_payload.encode("utf-8")) != self.injected_utf8_bytes
            or sha256_bytes(self.injected_payload.encode("utf-8")) != self.injected_payload_sha256
        ):
            raise ValueError("retrieval receipt is invalid")
        top_k, _target, hard = _LIMITS[self.target_id]
        if len(self.selected) > top_k or self.injected_utf8_bytes > hard:
            raise ValueError("retrieval budget is invalid")

    def __repr__(self) -> str:
        return "RetrievalReceiptV2(<question-and-content-redacted>)"

    def private_record(self) -> dict[str, object]:
        return {
            "schema_version": "TemperatureSafeRetrievalReceiptV2",
            "retriever_id": RETRIEVER_ID,
            "target_id": self.target_id,
            "eligible_active_entry_ids": list(self.eligible_entry_ids),
            "selected_entry_ids": [item.entry_id for item in self.selected],
            "selection_scores": [list(item.score) for item in self.selected],
            "retrieval_input_sha256": self.retrieval_input_sha256,
            "injected_payload_sha256": self.injected_payload_sha256,
            "injected_utf8_bytes": self.injected_utf8_bytes,
            "context_hash": self.context_hash_component,
        }


def retrieve_sparse_context_v2(
    task: PublicChemBench4KTask,
    *,
    state: SafeArtifactStateV2,
) -> RetrievalReceiptV2:
    if type(task) is not PublicChemBench4KTask or type(state) is not SafeArtifactStateV2:
        raise TypeError("retrieval requires exact public task and state")
    query_text = f"{task.question}\n{task.A}\n{task.B}\n{task.C}\n{task.D}"
    query_tokens = _tokens(query_text)
    visible_input = {
        "question": normalize_benchmark_text(task.question),
        "options": [normalize_benchmark_text(value) for value in (task.A, task.B, task.C, task.D)],
    }
    input_digest = sha256_bytes(
        canonical_json_bytes(
            {
                "retriever_id": RETRIEVER_ID,
                "visible_question_and_options_sha256": sha256_bytes(
                    canonical_json_bytes(visible_input)
                ),
                "state_sha256": state.digest,
            }
        )
    )
    eligible = tuple(sorted(state.runtime_entries, key=lambda entry: entry.entry_id))
    ranked: list[tuple[tuple[int, int, int, int, int], CanonicalEntryV2]] = []
    for entry in eligible:
        positive = _tokens(f"{entry.content} {entry.applicability}")
        exclusions = _tokens(entry.exclusion_conditions)
        overlap = len(query_tokens & positive)
        applicability_overlap = len(query_tokens & _tokens(entry.applicability))
        exclusion_overlap = len(query_tokens & exclusions)
        if overlap == 0 or (exclusion_overlap > 0 and exclusion_overlap >= overlap):
            continue
        score = (
            overlap * 100_000 // max(1, len(positive)),
            applicability_overlap,
            entry.support_count - entry.contradiction_count,
            round(entry.confidence * 10_000),
            -entry.negative_flip_count,
        )
        ranked.append((score, entry))
    ranked.sort(key=lambda value: (tuple(-x for x in value[0]), value[1].entry_id))
    top_k, target_bytes, hard_bytes = _LIMITS[state.selected_target]
    selected_entries: list[tuple[tuple[int, int, int, int, int], CanonicalEntryV2]] = []
    for score, entry in ranked:
        if len(selected_entries) >= top_k:
            break
        trial = (*selected_entries, (score, entry))
        payload = _render_sparse(state.selected_target, tuple(value[1] for value in trial))
        size = len(payload.encode("utf-8"))
        if size > hard_bytes:
            continue
        if selected_entries and size > target_bytes:
            continue
        selected_entries.append((score, entry))
    payload = _render_sparse(
        state.selected_target,
        tuple(entry for _score, entry in selected_entries),
    )
    rendered = tuple(
        RetrievedEntryV2(
            entry_id=entry.entry_id,
            score=score,
            rendered=_render_entry(state.selected_target, entry),
        )
        for score, entry in selected_entries
    )
    payload_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    context_component = sha256_bytes(
        canonical_json_bytes(
            {
                "target_id": state.selected_target,
                "state_sha256": state.digest,
                "retrieval_input_sha256": input_digest,
                "selected_entry_ids": [item.entry_id for item in rendered],
                "scores": [list(item.score) for item in rendered],
                "injected_payload_sha256": payload_sha,
                "injected_utf8_bytes": len(payload.encode("utf-8")),
            }
        )
    )
    return RetrievalReceiptV2(
        target_id=state.selected_target,
        eligible_entry_ids=tuple(entry.entry_id for entry in eligible),
        selected=rendered,
        retrieval_input_sha256=input_digest,
        injected_payload=payload,
        injected_payload_sha256=payload_sha,
        injected_utf8_bytes=len(payload.encode("utf-8")),
        context_hash_component=context_component,
    )


def _tokens(value: str) -> frozenset[str]:
    normalized = normalize_benchmark_text(value).casefold()
    return frozenset(_TOKEN.findall(normalized))


def _render_entry(target: SelectedTarget, entry: CanonicalEntryV2) -> str:
    if target == "text_memory":
        return (
            f"- {entry.content}\n"
            f"  Applies: {entry.applicability}\n"
            f"  Exclude: {entry.exclusion_conditions}"
        )
    return (
        f"1. {entry.content}\n"
        f"   Apply when: {entry.applicability}\n"
        f"   Skip when: {entry.exclusion_conditions}"
    )


def _render_sparse(target: SelectedTarget, entries: tuple[CanonicalEntryV2, ...]) -> str:
    if not entries:
        return ""
    if target == "text_memory":
        lines = ["# Retrieved Temperature Knowledge", ""]
        lines.extend(_render_entry(target, entry) for entry in entries)
    else:
        lines = ["# Temperature Prediction Skill", "", "## Workflow", ""]
        for index, entry in enumerate(entries, start=1):
            rendered = _render_entry(target, entry)
            if index != 1:
                rendered = re.sub(r"^1\.", f"{index}.", rendered)
            lines.append(rendered)
        lines.extend(
            (
                "",
                "## Validation Checks",
                "",
                "- Preserve explicit problem constraints and verify units before returning one choice.",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "RETRIEVER_ID",
    "RetrievalReceiptV2",
    "RetrievedEntryV2",
    "SafeRetrievalError",
    "retrieve_sparse_context_v2",
]
