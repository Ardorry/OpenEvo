"""Fail-closed validator for Core-produced ChemBench4K text-memory artifacts."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import Enum

from openevo.evolution.models import ArtifactResponse, ArtifactType

from openevo_chembench.chembench4k_dataset import normalize_benchmark_text
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
    r"\.(?:json|jsonl|parquet)\b)",
    flags=re.IGNORECASE | re.MULTILINE,
)
_ANSWER_MAP = re.compile(
    r"(?:the\s+)?(?:correct\s+)?answer\s*(?:is|=|:)\s*[ABCD]\b|"
    r"(?:choose|select|pick)\s+(?:option\s+)?[ABCD]\b|"
    r"(?:question|item|uid|index)\s*[^\\n]{0,48}(?:->|=|:)\s*[ABCD]\b",
    flags=re.IGNORECASE,
)


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
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _sequential_ngram_overlap(source: str, candidate_ngrams: set[str]) -> bool:
    normalized = normalize_benchmark_text(source)
    if len(normalized) < 24:
        return False
    consecutive = 0
    for index in range(len(normalized) - 23):
        gram = normalized[index : index + 24]
        if gram in candidate_ngrams:
            consecutive += 1
            if consecutive >= 4:
                return True
        else:
            consecutive = 0
    return False


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
        candidate_ngrams = {
            normalized_candidate[index : index + 24]
            for index in range(max(0, len(normalized_candidate) - 23))
        }
        for task in self._dev_tasks:
            if task.uid in text or task.uid[:24] in text:
                findings.add(ArtifactFindingV2.LEAK_UID)
            for field_name in ("question", "A", "B", "C", "D"):
                source = getattr(task, field_name)
                if len(source.strip()) >= 12 and source.casefold() in text.casefold():
                    findings.add(ArtifactFindingV2.LEAK_EXACT_DEV_TEXT)
                normalized_source = normalize_benchmark_text(source)
                if len(normalized_source) >= 12 and normalized_source in normalized_candidate:
                    findings.add(ArtifactFindingV2.LEAK_NORMALIZED_DEV_TEXT)
                if _sequential_ngram_overlap(source, candidate_ngrams):
                    findings.add(
                        ArtifactFindingV2.LEAK_QUESTION_NGRAM
                        if field_name == "question"
                        else ArtifactFindingV2.LEAK_OPTION_NGRAM
                    )
        if any(uid in text for uid in self._test_uid_set):
            findings.add(ArtifactFindingV2.LEAK_TEST_UID)
        if _PATH_OR_BENCHMARK_MARKER.search(text):
            findings.add(ArtifactFindingV2.LEAK_PATH_OR_BENCHMARK_MARKER)
        if _ANSWER_MAP.search(text):
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
