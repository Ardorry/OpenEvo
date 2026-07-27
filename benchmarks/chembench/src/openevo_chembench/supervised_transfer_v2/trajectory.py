"""Answer-free trajectory records for supervised v2 Core evolution jobs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REVISION,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.models import RawAttempt
from openevo_chembench.supervised_transfer_v2.config import PROTOCOL_ID
from openevo_chembench.taskwise_feedback_v1 import (
    TaskwiseSafeEvolutionSignalV1,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}\Z", re.ASCII)
_OPENEVO_ROLLOUT_TRANSCRIPT_PREFIX = "openevo-rollout-jsonl:sha256:"
_FORBIDDEN_KEYS = frozenset(
    {
        "answer_mapping",
        "correct_answer",
        "correct_option",
        "score",
        "target",
        "target_letter",
        "target_scores",
        "verifier_rationale",
    }
)


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class SupervisedTrajectoryV2:
    """One public task attempt plus answer-free evaluator taxonomy."""

    trajectory_id: str
    task_uid: str
    task_index: int
    category: str
    round_index: Literal[0, 1, 2]
    session_id: str
    source_transcript_sha256: str
    public_prompt_sha256: str
    public_prompt: str
    raw_completion: str
    safe_feedback: TaskwiseSafeEvolutionSignalV1
    dataset_revision: str
    dataset_sha256: str

    def __repr__(self) -> str:
        return "SupervisedTrajectoryV2(<public-attempt-safe-feedback>)"

    __str__ = __repr__

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.trajectory_id, "trajectory_id"),
            (self.task_uid, "task_uid"),
            (self.source_transcript_sha256, "source_transcript_sha256"),
            (self.public_prompt_sha256, "public_prompt_sha256"),
            (self.dataset_sha256, "dataset_sha256"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        if (
            isinstance(self.task_index, bool)
            or not isinstance(self.task_index, int)
            or self.task_index < 0
        ):
            raise ValueError("task_index must be non-negative")
        if self.category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("category is not a frozen ChemBench4K category")
        if self.round_index not in (0, 1, 2):
            raise ValueError("only rounds 0, 1, and 2 may enter an evolution update")
        if type(self.session_id) is not str or _SESSION_ID.fullmatch(self.session_id) is None:
            raise ValueError("session_id must be a bounded runtime identifier")
        if type(self.public_prompt) is not str or not self.public_prompt:
            raise ValueError("public_prompt must be non-empty text")
        if _sha256(self.public_prompt.encode("utf-8")) != self.public_prompt_sha256:
            raise ValueError("public prompt digest mismatch")
        if type(self.raw_completion) is not str:
            raise TypeError("raw_completion must be text")
        if type(self.safe_feedback) is not TaskwiseSafeEvolutionSignalV1:
            raise TypeError("safe_feedback must be exact TaskwiseSafeEvolutionSignalV1")
        if self.dataset_revision != CHEMBENCH4K_REVISION:
            raise ValueError("dataset revision is not frozen")
        if self.trajectory_id != self._expected_trajectory_id():
            raise ValueError("trajectory_id does not bind the complete trajectory")

    @classmethod
    def from_attempt(
        cls,
        *,
        task_uid: str,
        task_index: int,
        category: str,
        round_index: Literal[0, 1, 2],
        session_id: str,
        prompt: RenderedChemBench4KPrompt,
        attempt: RawAttempt,
        safe_feedback: TaskwiseSafeEvolutionSignalV1,
        dataset_sha256: str,
    ) -> SupervisedTrajectoryV2:
        if type(prompt) is not RenderedChemBench4KPrompt:
            raise TypeError("prompt must be exact RenderedChemBench4KPrompt")
        if type(attempt) is not RawAttempt:
            raise TypeError("attempt must be exact RawAttempt")
        if prompt.uid != task_uid or prompt.category != category:
            raise ValueError("prompt identity does not match the trajectory task")
        source_transcript_sha256 = _source_transcript_sha256(attempt)
        prompt_sha256 = _sha256(prompt.text.encode("utf-8"))
        identity = {
            "schema_version": "supervised_trajectory_identity_v2",
            "protocol_id": PROTOCOL_ID,
            "task_uid": task_uid,
            "task_index": task_index,
            "category": category,
            "round_index": round_index,
            "session_id": session_id,
            "source_transcript_sha256": source_transcript_sha256,
            "public_prompt_sha256": prompt_sha256,
            "raw_completion_sha256": _sha256(attempt.response.encode("utf-8")),
            "safe_feedback_digest": safe_feedback.digest,
            "dataset_revision": prompt.dataset_revision,
            "dataset_sha256": dataset_sha256,
        }
        return cls(
            trajectory_id=_sha256(_canonical_bytes(identity)),
            task_uid=task_uid,
            task_index=task_index,
            category=category,
            round_index=round_index,
            session_id=session_id,
            source_transcript_sha256=source_transcript_sha256,
            public_prompt_sha256=prompt_sha256,
            public_prompt=prompt.text,
            raw_completion=attempt.response,
            safe_feedback=safe_feedback,
            dataset_revision=prompt.dataset_revision,
            dataset_sha256=dataset_sha256,
        )

    def _expected_trajectory_id(self) -> str:
        identity = {
            "schema_version": "supervised_trajectory_identity_v2",
            "protocol_id": PROTOCOL_ID,
            "task_uid": self.task_uid,
            "task_index": self.task_index,
            "category": self.category,
            "round_index": self.round_index,
            "session_id": self.session_id,
            "source_transcript_sha256": self.source_transcript_sha256,
            "public_prompt_sha256": self.public_prompt_sha256,
            "raw_completion_sha256": _sha256(self.raw_completion.encode("utf-8")),
            "safe_feedback_digest": self.safe_feedback.digest,
            "dataset_revision": self.dataset_revision,
            "dataset_sha256": self.dataset_sha256,
        }
        return _sha256(_canonical_bytes(identity))

    def to_core_record(self) -> dict[str, object]:
        """Return the private controller binding used to digest this trajectory."""

        payload: dict[str, object] = {
            # Kept for private-checkpoint compatibility. The reflector-visible
            # Core dataset uses ``to_reflector_projection`` instead.
            "schema_version": "supervised_core_trajectory_v2",
            "protocol_id": PROTOCOL_ID,
            "trajectory_id": self.trajectory_id,
            "task_uid": self.task_uid,
            "task_index": self.task_index,
            "category": self.category,
            "round_index": self.round_index,
            "session_id": self.session_id,
            "source_transcript_sha256": self.source_transcript_sha256,
            "public_prompt_sha256": self.public_prompt_sha256,
            "public_prompt": self.public_prompt,
            "raw_completion": self.raw_completion,
            "safe_feedback": self.safe_feedback.to_evolution_payload(),
            "safe_feedback_digest": self.safe_feedback.digest,
            "dataset_revision": self.dataset_revision,
            "dataset_sha256": self.dataset_sha256,
        }
        _assert_no_private_keys(payload)
        return payload

    def to_reflector_projection(self) -> dict[str, object]:
        """Project only taxonomy-level evidence into the Core dataset artifact."""

        payload: dict[str, object] = {
            "schema_version": "supervised_reflector_trajectory_projection_v2",
            "protocol_id": PROTOCOL_ID,
            "category": self.category,
            "round_index": self.round_index,
            "safe_feedback": self.safe_feedback.to_evolution_payload(),
            "safe_feedback_digest": self.safe_feedback.digest,
        }
        _assert_no_private_keys(payload)
        return payload

    @property
    def digest(self) -> str:
        return _sha256(_canonical_bytes(self.to_core_record()))


def ordered_supervised_trajectory_digest_v2(
    trajectories: tuple[SupervisedTrajectoryV2, ...],
) -> str:
    if (
        not isinstance(trajectories, tuple)
        or not trajectories
        or any(type(item) is not SupervisedTrajectoryV2 for item in trajectories)
    ):
        raise ValueError("trajectories must be a non-empty exact tuple")
    first = trajectories[0]
    if (
        len(trajectories) not in {1, 2, 3}
        or tuple(item.round_index for item in trajectories) != tuple(range(len(trajectories)))
        or any(
            item.task_uid != first.task_uid
            or item.task_index != first.task_index
            or item.category != first.category
            or item.dataset_revision != first.dataset_revision
            or item.dataset_sha256 != first.dataset_sha256
            for item in trajectories
        )
    ):
        raise ValueError("trajectories must be the ordered current-task prefix")
    return _sha256(_canonical_bytes([item.to_core_record() for item in trajectories]))


def ordered_supervised_safe_feedback_digest_v2(
    trajectories: tuple[SupervisedTrajectoryV2, ...],
) -> str:
    ordered_supervised_trajectory_digest_v2(trajectories)
    return _sha256(
        _canonical_bytes([item.safe_feedback.to_evolution_payload() for item in trajectories])
    )


def ordered_source_execution_provenance_digest_v2(
    trajectories: tuple[SupervisedTrajectoryV2, ...],
) -> str:
    """Bind each controller trajectory to its official Rollout transcript identity."""

    ordered_supervised_trajectory_digest_v2(trajectories)
    return _sha256(
        _canonical_bytes(
            [
                {
                    "trajectory_id": item.trajectory_id,
                    "session_id": item.session_id,
                    "source_transcript_sha256": item.source_transcript_sha256,
                }
                for item in trajectories
            ]
        )
    )


def _source_transcript_sha256(attempt: RawAttempt) -> str:
    reference = attempt.transcript_reference.reference
    if not reference.startswith(_OPENEVO_ROLLOUT_TRANSCRIPT_PREFIX):
        raise ValueError("attempt is not bound to an OpenEvo Rollout transcript")
    digest = reference.removeprefix(_OPENEVO_ROLLOUT_TRANSCRIPT_PREFIX)
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("OpenEvo Rollout transcript reference digest is invalid")
    return digest


def _assert_no_private_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("Core trajectory keys must be strings")
            if key.casefold().replace("-", "_") in _FORBIDDEN_KEYS:
                raise ValueError("private evaluator field reached Core trajectory")
            _assert_no_private_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_private_keys(item)


__all__ = [
    "SupervisedTrajectoryV2",
    "ordered_source_execution_provenance_digest_v2",
    "ordered_supervised_safe_feedback_digest_v2",
    "ordered_supervised_trajectory_digest_v2",
]
