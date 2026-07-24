from __future__ import annotations

import copy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from openevo_chembench.config import CHEMBENCH_CONFIGS
from openevo_chembench.dataset import (
    ChemBenchDatasetLoader,
    DatasetFormatError,
    DatasetSnapshot,
    HuggingFaceRowSource,
    canonical_schema_hash,
)


_REVISION = "6e1d25748952393f44e35b8e85bbe567246a6430"
_SCHEMA = {
    "canary": {"dtype": "string"},
    "examples": {
        "list": {
            "input": {"dtype": "string"},
            "target": {"dtype": "string"},
            "target_scores": {"dtype": "string"},
        }
    },
    "metrics": {"list": {"dtype": "string"}},
    "uuid": {"dtype": "string"},
}


def _synthetic_row() -> dict[str, object]:
    return {
        "canary": "private-canary",
        "description": "private-description",
        "examples": [
            {
                "input": "Synthetic question one?",
                "target": None,
                "target_scores": '{"choice alpha": 0, "choice beta": 1}',
            },
            {
                "input": "Synthetic question two?",
                "target": None,
                "target_scores": '{"choice gamma": 1, "choice delta": 0}',
            },
        ],
        "keywords": ["private-keyword"],
        "metrics": ["multiple_choice_grade"],
        "name": "private-name",
        "preferred_score": "multiple_choice_grade",
        "subfield": "private-subfield",
        "uuid": "private-row-uuid",
    }


class _FakeRowSource:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.calls: list[dict[str, str]] = []

    def load_config(
        self,
        *,
        repository: str,
        configuration: str,
        split: str,
        revision: str,
    ) -> DatasetSnapshot:
        self.calls.append(
            {
                "repository": repository,
                "configuration": configuration,
                "split": split,
                "revision": revision,
            }
        )
        return DatasetSnapshot(rows=(self.row,), schema=_SCHEMA)


class _FakeFeatures:
    def to_dict(self) -> dict[str, object]:
        return copy.deepcopy(_SCHEMA)


class _FakeDataset:
    features = _FakeFeatures()

    def __iter__(self):
        return iter((_synthetic_row(),))


class DatasetLoaderTests(unittest.TestCase):
    def test_huggingface_source_passes_exact_revision_and_config(self) -> None:
        calls: list[dict[str, object]] = []
        fake_module = types.ModuleType("datasets")

        def fake_load_dataset(**kwargs):
            calls.append(kwargs)
            return _FakeDataset()

        fake_module.load_dataset = fake_load_dataset  # type: ignore[attr-defined]
        source = HuggingFaceRowSource(cache_dir=Path("/tmp/chembench-read-cache"))

        with patch.dict(sys.modules, {"datasets": fake_module}):
            snapshot = source.load_config(
                repository="jablonkagroup/ChemBench",
                configuration="analytical_chemistry",
                split="train",
                revision=_REVISION,
            )

        self.assertIsInstance(snapshot, DatasetSnapshot)
        self.assertEqual(
            calls,
            [
                {
                    "path": "jablonkagroup/ChemBench",
                    "name": "analytical_chemistry",
                    "split": "train",
                    "revision": _REVISION,
                    "cache_dir": "/tmp/chembench-read-cache",
                }
            ],
        )

    def test_load_all_pins_revision_and_supports_all_nine_configs(self) -> None:
        raw_row = _synthetic_row()
        source = _FakeRowSource(raw_row)
        loader = ChemBenchDatasetLoader(revision=_REVISION, row_source=source)

        loaded = loader.load_all()

        self.assertEqual(
            tuple(item.provenance.config_name for item in loaded),
            CHEMBENCH_CONFIGS,
        )
        self.assertEqual(len(source.calls), 9)
        self.assertEqual(
            tuple(call["configuration"] for call in source.calls),
            CHEMBENCH_CONFIGS,
        )
        self.assertTrue(all(call["revision"] == _REVISION for call in source.calls))
        self.assertTrue(
            all(item.provenance.revision == _REVISION for item in loaded)
        )
        self.assertTrue(all(len(item.tasks) == 2 for item in loaded))

    def test_examples_expand_into_atomic_private_tasks_without_mutating_row(self) -> None:
        raw_row = _synthetic_row()
        original = copy.deepcopy(raw_row)
        source = _FakeRowSource(raw_row)
        loader = ChemBenchDatasetLoader(revision=_REVISION, row_source=source)

        loaded = loader.load_config("analytical_chemistry")

        self.assertEqual(raw_row, original)
        self.assertEqual(len(loaded.tasks), 2)
        self.assertEqual(loaded.tasks[0].question, "Synthetic question one?")
        self.assertEqual(
            loaded.tasks[0].options,
            ("choice alpha", "choice beta"),
        )
        self.assertEqual(tuple(item.score for item in loaded.tasks[0].target_scores), (0, 1))
        self.assertEqual(loaded.tasks[0].metrics, ("multiple_choice_grade",))
        self.assertEqual(loaded.tasks[0].uuid, "private-row-uuid")

    def test_provenance_records_canonical_schema_hash(self) -> None:
        source = _FakeRowSource(_synthetic_row())
        loader = ChemBenchDatasetLoader(revision=_REVISION, row_source=source)

        loaded = loader.load_config("general_chemistry")

        self.assertEqual(
            loaded.provenance.schema_hash,
            canonical_schema_hash(_SCHEMA),
        )
        self.assertEqual(len(loaded.provenance.schema_hash), 64)

    def test_invalid_target_scores_fail_closed_without_mutating_row(self) -> None:
        raw_row = _synthetic_row()
        examples = raw_row["examples"]
        assert isinstance(examples, list)
        first = examples[0]
        assert isinstance(first, dict)
        first["target_scores"] = "not-json"
        original = copy.deepcopy(raw_row)
        loader = ChemBenchDatasetLoader(
            revision=_REVISION,
            row_source=_FakeRowSource(raw_row),
        )

        with self.assertRaises(DatasetFormatError):
            loader.load_config("organic_chemistry")
        self.assertEqual(raw_row, original)

    def test_revision_must_be_a_full_commit_hash(self) -> None:
        source = _FakeRowSource(_synthetic_row())

        with self.assertRaises(ValueError):
            ChemBenchDatasetLoader(revision="main", row_source=source)
        self.assertEqual(source.calls, [])


if __name__ == "__main__":
    unittest.main()
