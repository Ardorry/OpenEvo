"""Fail-closed validator for Core-produced ChemBench4K text-memory artifacts."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from enum import Enum

from openevo.evolution.models import ArtifactResponse, ArtifactType

from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.core_evolution_v2 import CoreArtifactValidationReceiptV2


VALIDATOR_ID = "chembench4k_text_memory_validator_v2"
MAX_TEXT_MEMORY_BYTES = 32 * 1024
_REQUIRED_SECTIONS = (
    "Do",
    "Avoid",
    "Validate",
    "When Applicable",
    "Retired Or Superseded",
)
_PATH_OR_BENCHMARK_MARKER = re.compile(
    r"(?:chembench(?:4k)?|ai4chem|opencompass|"
    r"(?:^|[\\/])(?:dev|test)[\\/]|_benchmark\.json|"
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|"
    r"(?<![A-Za-z0-9_])\\\\[^\\\s]+\\[^\\\s]+|"
    r"\bfile://|"
    r"\.(?:json|jsonl|parquet|sqlite3?|ya?ml)\b)",
    flags=re.IGNORECASE | re.MULTILINE,
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"/(?:[\w.~+@%=-]+/)+[\w.~+@%=-]+",
    re.UNICODE,
)
_POSIX_PATH_OPENING_BOUNDARY = frozenset("\"'([{:=<")
_OPTION_TOKEN_PREFIX_RE = r"(?<![A-Za-z0-9_])"
_OPTION_CHEMICAL_SUFFIX_RE = r"(?![-‐‑‒–—=][A-Za-z0-9])"
_UPPER_OPTION_TOKEN_RE = rf"{_OPTION_TOKEN_PREFIX_RE}[ABCD]\b{_OPTION_CHEMICAL_SUFFIX_RE}"
_EXPLICIT_OPTION_TOKEN_RE = rf"{_OPTION_TOKEN_PREFIX_RE}[ABCDabcd]\b{_OPTION_CHEMICAL_SUFFIX_RE}"
_TERMINAL_OPTION_TOKEN_RE = (
    rf"{_OPTION_TOKEN_PREFIX_RE}[ABCD]\b{_OPTION_CHEMICAL_SUFFIX_RE}"
    r"(?=\s*(?:\Z|[.,;:!?]))"
)
_ANSWER_MAP_SEPARATOR_RE = r"(?:is|=|:|->|→)"
_ANSWER_MAP = re.compile(
    rf"(?i:(?:the\s+)?(?:correct\s+)?answer\s*{_ANSWER_MAP_SEPARATOR_RE})\s*"
    rf"{_UPPER_OPTION_TOKEN_RE}|"
    rf"(?i:(?:choose|select|pick)\s+option\s*"
    rf"(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*){_EXPLICIT_OPTION_TOKEN_RE}|"
    rf"(?i:(?:choose|select|pick)\s+){_TERMINAL_OPTION_TOKEN_RE}|"
    rf"(?i:(?:return|output)\s*(?:only\s+)?option\s*"
    rf"(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*){_EXPLICIT_OPTION_TOKEN_RE}|"
    rf"(?i:(?:return|output)\s*(?:only\s+)?"
    rf"(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*){_TERMINAL_OPTION_TOKEN_RE}|"
    rf"(?i:(?:final\s+)?(?:response|prediction|letter|choice)\s*"
    rf"{_ANSWER_MAP_SEPARATOR_RE}\s*){_TERMINAL_OPTION_TOKEN_RE}|"
    rf"(?i:\boption\s*(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*)"
    rf"{_EXPLICIT_OPTION_TOKEN_RE}|"
    r"(?i:(?:question|item|index)"
    r"(?:\s+(?:\d+|uid|[A-Za-z0-9_.-]{6,}))?|uid"
    r"(?:\s+[A-Za-z0-9_.:-]{6,})?)\s*"
    rf"(?:->|→|=|:)\s*{_UPPER_OPTION_TOKEN_RE}|"
    r"(?i:(?:question|item|index)"
    r"(?:\s+(?:\d+|uid|[A-Za-z0-9_.-]{6,}))?|uid"
    r"(?:\s+[A-Za-z0-9_.:-]{6,})?)\s+"
    rf"maps?\s+to\s+{_UPPER_OPTION_TOKEN_RE}",
)
_ANSWER_MAP_WRAPPER_RE = re.compile(r"""[*_`~()[\]{}"'“”‘’]""")


class ArtifactFindingV2(str, Enum):
    INVALID_TYPE = "INVALID_TYPE"
    ALREADY_PROMOTED = "ALREADY_PROMOTED"
    INVALID_UTF8 = "INVALID_UTF8"
    INVALID_STRUCTURE = "INVALID_STRUCTURE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    PAYLOAD_DIGEST_MISMATCH = "PAYLOAD_DIGEST_MISMATCH"
    LEAK_EXACT_DEV_TEXT = "LEAK_EXACT_DEV_TEXT"
    LEAK_NORMALIZED_DEV_TEXT = "LEAK_NORMALIZED_DEV_TEXT"
    LEAK_QUESTION_NGRAM = "LEAK_QUESTION_NGRAM"
    LEAK_OPTION_NGRAM = "LEAK_OPTION_NGRAM"
    LEAK_UID = "LEAK_UID"
    LEAK_TEST_UID = "LEAK_TEST_UID"
    LEAK_PATH_OR_BENCHMARK_MARKER = "LEAK_PATH_OR_BENCHMARK_MARKER"
    LEAK_ANSWER_MAP = "LEAK_ANSWER_MAP"
    INVALID_CORE_MANIFEST = "INVALID_CORE_MANIFEST"
    INVALID_DEV_PROVENANCE = "INVALID_DEV_PROVENANCE"


def _normalize(value: str) -> str:
    canonical = unicodedata.normalize("NFKC", value).casefold()
    normalized: list[str] = []
    pending_separator = False
    for character in canonical:
        if character.isalnum():
            if pending_separator and normalized:
                normalized.append(" ")
            normalized.append(character)
            pending_separator = False
        else:
            pending_separator = bool(normalized)
    return "".join(normalized)


def _unicode_token_spans(value: str) -> tuple[re.Match[str], ...]:
    return tuple(re.finditer(r"[^\W_]+", value))


def _contains_bounded_literal(text: str, literal: str) -> bool:
    if not literal:
        return False
    candidate = text.casefold()
    needle = literal.casefold()
    offset = 0
    while True:
        index = candidate.find(needle, offset)
        if index < 0:
            return False
        end = index + len(needle)
        starts_in_token = (
            index > 0
            and (needle[0].isalnum() or needle[0] == "_")
            and (candidate[index - 1].isalnum() or candidate[index - 1] == "_")
        )
        ends_in_token = (
            end < len(candidate)
            and (needle[-1].isalnum() or needle[-1] == "_")
            and (candidate[end].isalnum() or candidate[end] == "_")
        )
        if not starts_in_token and not ends_in_token:
            return True
        offset = index + 1


def _sequential_ngram_overlap(source: str, candidate: str, *, field_name: str) -> bool:
    normalized = _normalize(source)
    if field_name == "question":
        minimum_source_ratio = 0.30
    elif field_name in {"A", "B", "C", "D"}:
        minimum_source_ratio = 0.40
    else:
        raise ValueError("unsupported private source kind")
    minimum_characters = 32
    minimum_tokens = 4
    required_characters = max(
        minimum_characters,
        math.ceil(len(normalized) * minimum_source_ratio),
        math.ceil(len(candidate) * 0.002),
    )
    tokens = _unicode_token_spans(normalized)
    for start_index, start_token in enumerate(tokens):
        long_token = start_token.group(0)
        if (
            len(long_token) >= 48
            and len(long_token) / max(1, len(normalized)) >= minimum_source_ratio
            and len(long_token) / max(1, len(candidate)) >= 0.002
            and _contains_bounded_literal(candidate, long_token)
        ):
            return True
        for end_index in range(start_index + minimum_tokens - 1, len(tokens)):
            span = normalized[start_token.start() : tokens[end_index].end()]
            if len(span) < required_characters:
                continue
            if (
                len(span) / max(1, len(normalized)) >= minimum_source_ratio
                and len(span) / max(1, len(candidate)) >= 0.002
                and _contains_bounded_literal(candidate, span)
            ):
                return True
            break
    return False


def _contains_path_or_benchmark_marker(text: str) -> bool:
    if _PATH_OR_BENCHMARK_MARKER.search(text) is not None:
        return True
    for match in _POSIX_ABSOLUTE_PATH_RE.finditer(text):
        if match.start() > 0:
            preceding = text[match.start() - 1]
            if not preceding.isspace() and preceding not in _POSIX_PATH_OPENING_BOUNDARY:
                continue
        segments = match.group(0)[1:].split("/")
        if any(
            not segment
            or (not segment[0].isalnum() and segment[0] not in "._~")
            or (not segment[-1].isalnum() and segment[-1] not in "._~")
            for segment in segments
        ):
            continue
        return True
    return False


def _contains_answer_map(text: str) -> bool:
    scan_text = _ANSWER_MAP_WRAPPER_RE.sub(
        " ",
        unicodedata.normalize("NFKC", text),
    )
    return _ANSWER_MAP.search(scan_text) is not None


class ChemBench4KTextMemoryArtifactValidatorV2:
    """Validate one untrusted Core artifact against dev-only private material."""

    __slots__ = (
        "_dev_tasks",
        "_dev_uid_set",
        "_expected_dataset_sha256",
        "_expected_dev_uid_set_sha256",
        "_test_uid_set",
    )

    def __init__(
        self,
        *,
        dev_tasks: tuple[PrivateChemBench4KTask, ...],
        test_uids: frozenset[str],
        expected_dev_uid_set_sha256: str,
    ) -> None:
        if (
            not isinstance(dev_tasks, tuple)
            or len(dev_tasks) != 45
            or any(type(task) is not PrivateChemBench4KTask for task in dev_tasks)
            or any(task.source_split != "dev" for task in dev_tasks)
        ):
            raise ValueError("validator requires exactly 45 private dev tasks")
        dev_uids = frozenset(task.uid for task in dev_tasks)
        if len(dev_uids) != 45:
            raise ValueError("validator dev UIDs must be unique")
        if (
            not isinstance(test_uids, frozenset)
            or not test_uids
            or not all(type(uid) is str and len(uid) == 64 for uid in test_uids)
            or dev_uids.intersection(test_uids)
        ):
            raise ValueError("validator requires a disjoint frozen test UID set")
        if (
            type(expected_dev_uid_set_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_dev_uid_set_sha256) is None
        ):
            raise ValueError("expected_dev_uid_set_sha256 must be SHA-256")
        dataset_hashes = {task.dataset_sha256 for task in dev_tasks}
        if len(dataset_hashes) != 1:
            raise ValueError("validator dev tasks must bind one dataset snapshot")
        self._dev_tasks = dev_tasks
        self._dev_uid_set = dev_uids
        self._test_uid_set = test_uids
        self._expected_dataset_sha256 = next(iter(dataset_hashes))
        self._expected_dev_uid_set_sha256 = expected_dev_uid_set_sha256

    def validate(
        self,
        *,
        artifact: ArtifactResponse,
        payload: bytes,
        payload_sha256: str,
        lineage: dict[str, object],
    ) -> CoreArtifactValidationReceiptV2:
        """Return only a closed decision; no source or candidate excerpt escapes."""

        if type(artifact) is not ArtifactResponse:
            raise TypeError("artifact must be exact Core ArtifactResponse")
        if type(payload) is not bytes:
            raise TypeError("payload must be bytes")
        findings: set[ArtifactFindingV2] = set()
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != payload_sha256:
            findings.add(ArtifactFindingV2.PAYLOAD_DIGEST_MISMATCH)
        if artifact.type is not ArtifactType.TEXT_MEMORY:
            findings.add(ArtifactFindingV2.INVALID_TYPE)
        if artifact.promoted:
            findings.add(ArtifactFindingV2.ALREADY_PROMOTED)
        if len(payload) > MAX_TEXT_MEMORY_BYTES:
            findings.add(ArtifactFindingV2.PAYLOAD_TOO_LARGE)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
            findings.add(ArtifactFindingV2.INVALID_UTF8)
        if "\x00" in text or not text.strip():
            findings.add(ArtifactFindingV2.INVALID_STRUCTURE)
        for section in _REQUIRED_SECTIONS:
            if (
                re.search(
                    rf"^##\s+{re.escape(section)}\s*#*\s*$",
                    text,
                    flags=re.MULTILINE,
                )
                is None
            ):
                findings.add(ArtifactFindingV2.INVALID_STRUCTURE)
                break

        manifest = artifact.manifest
        if (
            not isinstance(manifest, dict)
            or manifest.get("method") != "text_memory_expel_reflector"
            or manifest.get("record_count") != 45
            or not isinstance(manifest.get("source_dataset_artifact_ids"), list)
            or not manifest.get("source_dataset_artifact_ids")
        ):
            findings.add(ArtifactFindingV2.INVALID_CORE_MANIFEST)
        if not isinstance(lineage, dict):
            raise TypeError("lineage must be a dict issued from the Core artifact store")
        required_lineage = {
            "protocol_id": "chembench4k_frozen_generalization_v2",
            "dataset_repository": "AI4Chem/ChemBench4K",
            "dataset_revision": "f8ad41a980170f4c5d0cc97e57722d06887c8f53",
            "dataset_combined_sha256": self._expected_dataset_sha256,
            "dev_record_count": 45,
            "dev_uid_set_sha256": self._expected_dev_uid_set_sha256,
        }
        if any(lineage.get(key) != value for key, value in required_lineage.items()):
            findings.add(ArtifactFindingV2.INVALID_DEV_PROVENANCE)
        if any(
            "test" in str(key).casefold()
            or (
                isinstance(value, str)
                and ("test/" in value.casefold() or "/test" in value.casefold())
            )
            for key, value in lineage.items()
        ):
            findings.add(ArtifactFindingV2.INVALID_DEV_PROVENANCE)

        normalized_candidate = _normalize(text)
        for task in self._dev_tasks:
            if task.uid in text or task.uid[:24] in text:
                findings.add(ArtifactFindingV2.LEAK_UID)
            for field_name in ("question", "A", "B", "C", "D"):
                source = getattr(task, field_name)
                if len(source.strip()) >= 12 and _contains_bounded_literal(text, source):
                    findings.add(ArtifactFindingV2.LEAK_EXACT_DEV_TEXT)
                normalized_source = _normalize(source)
                if len(normalized_source) >= 12 and _contains_bounded_literal(
                    normalized_candidate,
                    normalized_source,
                ):
                    findings.add(ArtifactFindingV2.LEAK_NORMALIZED_DEV_TEXT)
                if _sequential_ngram_overlap(
                    source,
                    normalized_candidate,
                    field_name=field_name,
                ):
                    findings.add(
                        ArtifactFindingV2.LEAK_QUESTION_NGRAM
                        if field_name == "question"
                        else ArtifactFindingV2.LEAK_OPTION_NGRAM
                    )
        if any(uid in text for uid in self._test_uid_set):
            findings.add(ArtifactFindingV2.LEAK_TEST_UID)
        if _contains_path_or_benchmark_marker(text):
            findings.add(ArtifactFindingV2.LEAK_PATH_OR_BENCHMARK_MARKER)
        if _contains_answer_map(text):
            findings.add(ArtifactFindingV2.LEAK_ANSWER_MAP)

        finding_codes = tuple(sorted(finding.value for finding in findings))
        return CoreArtifactValidationReceiptV2(
            validator_id=VALIDATOR_ID,
            artifact_id=artifact.artifact_id,
            artifact_payload_sha256=payload_sha256,
            passed=not finding_codes,
            finding_codes=finding_codes,
        )


__all__ = [
    "ArtifactFindingV2",
    "ChemBench4KTextMemoryArtifactValidatorV2",
    "MAX_TEXT_MEMORY_BYTES",
    "VALIDATOR_ID",
]
