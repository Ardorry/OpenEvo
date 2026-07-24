"""Trust-boundary DTOs for the frozen ChemBench4K v2 protocol.

This module intentionally has no dependency on the legacy
``jablonkagroup/ChemBench`` adapter.  Private task values are redacted from
``repr`` and expose no generic serialization method.  Public values provide
only explicit allowlist serializers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


CHEMBENCH4K_REPOSITORY = "AI4Chem/ChemBench4K"
CHEMBENCH4K_REVISION = "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
CHEMBENCH4K_CATEGORIES = (
    "Name_Conversion",
    "Property_Prediction",
    "Mol2caption",
    "Caption2mol",
    "Product_Prediction",
    "Retrosynthesis",
    "Yield_Prediction",
    "Temperature_Prediction",
    "Solvent_Prediction",
)
CHOICE_LABELS = ("A", "B", "C", "D")


def _require_text(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_string(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")


class _PrivateDTO:
    """Prevent private task/result values from leaking through casual logging."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: int) -> Any:
        del protocol
        raise TypeError(f"{type(self).__name__} cannot cross a trust boundary")


@dataclass(frozen=True, slots=True, repr=False)
class PrivateChemBench4KTask(_PrivateDTO):
    """One trusted dataset row, including its private benchmark label."""

    uid: str
    category: str
    source_split: Literal["dev", "test"]
    source_index: int
    question: str
    A: str
    B: str
    C: str
    D: str
    target: Literal["A", "B", "C", "D"]
    dataset_revision: str
    dataset_sha256: str

    def __post_init__(self) -> None:
        if len(self.uid) != 64:
            raise ValueError("PrivateChemBench4KTask.uid must be a SHA-256 digest")
        if self.category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("PrivateChemBench4KTask.category is unsupported")
        if self.source_split not in {"dev", "test"}:
            raise ValueError("PrivateChemBench4KTask.source_split must be dev or test")
        if (
            isinstance(self.source_index, bool)
            or not isinstance(self.source_index, int)
            or self.source_index < 0
        ):
            raise ValueError("PrivateChemBench4KTask.source_index must be non-negative")
        _require_text(self.question, "question")
        for field_name in ("A", "B", "C", "D"):
            _require_string(getattr(self, field_name), field_name)
        if self.target not in CHOICE_LABELS:
            raise ValueError("PrivateChemBench4KTask.target must be A, B, C, or D")
        if self.dataset_revision != CHEMBENCH4K_REVISION:
            raise ValueError("PrivateChemBench4KTask.dataset_revision is not frozen")
        if len(self.dataset_sha256) != 64:
            raise ValueError("PrivateChemBench4KTask.dataset_sha256 must be a SHA-256 digest")

    def to_public(self) -> PublicChemBench4KTask:
        """Project through the only permitted private-to-public allowlist."""

        return PublicChemBench4KTask(
            uid=self.uid,
            category=self.category,
            question=self.question,
            A=self.A,
            B=self.B,
            C=self.C,
            D=self.D,
            dataset_revision=self.dataset_revision,
        )


@dataclass(frozen=True, slots=True)
class PublicChemBench4KTask:
    """The complete task payload permitted outside the trusted data boundary."""

    uid: str
    category: str
    question: str
    A: str
    B: str
    C: str
    D: str
    dataset_revision: str

    def __post_init__(self) -> None:
        if len(self.uid) != 64:
            raise ValueError("PublicChemBench4KTask.uid must be a SHA-256 digest")
        if self.category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("PublicChemBench4KTask.category is unsupported")
        _require_text(self.question, "question")
        for field_name in ("A", "B", "C", "D"):
            _require_string(getattr(self, field_name), field_name)
        if self.dataset_revision != CHEMBENCH4K_REVISION:
            raise ValueError("PublicChemBench4KTask.dataset_revision is not frozen")

    def to_public_dict(self) -> dict[str, str]:
        """Serialize exactly the public fields, without derived private metadata."""

        return {
            "uid": self.uid,
            "category": self.category,
            "question": self.question,
            "A": self.A,
            "B": self.B,
            "C": self.C,
            "D": self.D,
            "dataset_revision": self.dataset_revision,
        }


@dataclass(frozen=True, slots=True)
class RenderedChemBench4KPrompt:
    """Public prompt text plus answer-free prompt identity."""

    uid: str
    category: str
    dataset_revision: str
    demonstration_uids: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        if len(self.uid) != 64:
            raise ValueError("RenderedChemBench4KPrompt.uid must be a SHA-256 digest")
        if self.category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("RenderedChemBench4KPrompt.category is unsupported")
        if self.dataset_revision != CHEMBENCH4K_REVISION:
            raise ValueError("RenderedChemBench4KPrompt.dataset_revision is not frozen")
        if not isinstance(self.demonstration_uids, tuple):
            raise TypeError("demonstration_uids must be an immutable tuple")
        if any(len(uid) != 64 for uid in self.demonstration_uids):
            raise ValueError("demonstration_uids must contain SHA-256 digests")
        _require_text(self.text, "RenderedChemBench4KPrompt.text")

    def to_agent_payload(self) -> dict[str, str]:
        """Return the only fields that may be supplied to the executor."""

        return {"instruction": self.text}


__all__ = [
    "CHEMBENCH4K_CATEGORIES",
    "CHEMBENCH4K_REPOSITORY",
    "CHEMBENCH4K_REVISION",
    "CHOICE_LABELS",
    "PrivateChemBench4KTask",
    "PublicChemBench4KTask",
    "RenderedChemBench4KPrompt",
]
