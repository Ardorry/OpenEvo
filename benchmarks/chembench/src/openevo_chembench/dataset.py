"""Revision-pinned, trusted-boundary ChemBench dataset loading."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openevo_chembench.config import CHEMBENCH_CONFIGS, ChemBenchDatasetConfig
from openevo_chembench.models import PrivateTargetScore, PrivateTask


_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")
_SCHEMA_HASH = re.compile(r"[0-9a-f]{64}")
_REQUIRED_SCHEMA_FIELDS = frozenset({"canary", "examples", "metrics", "uuid"})


class DatasetLoadError(RuntimeError):
    """Fail-closed dataset access or schema error without raw row contents."""


class DatasetFormatError(ValueError):
    """Fail-closed row/example format error without raw row contents."""


@dataclass(frozen=True, slots=True, repr=False)
class DatasetSnapshot:
    """Trusted row source result before any private task leaves the loader frame."""

    rows: Iterable[Mapping[str, object]]
    schema: Mapping[str, object]

    def __post_init__(self) -> None:
        if isinstance(self.rows, (str, bytes)) or not isinstance(self.rows, Iterable):
            raise TypeError("DatasetSnapshot.rows must be an iterable of mappings")
        if not isinstance(self.schema, Mapping):
            raise TypeError("DatasetSnapshot.schema must be a mapping")


class ChemBenchRowSource(Protocol):
    """Read-only row-source contract used by production and offline tests."""

    def load_config(
        self,
        *,
        repository: str,
        configuration: str,
        split: str,
        revision: str,
    ) -> DatasetSnapshot:
        """Load one exact dataset configuration without transforming its rows."""


class HuggingFaceRowSource:
    """Read an exact Hugging Face dataset revision through ``datasets``."""

    __slots__ = ("_cache_dir",)

    def __init__(self, *, cache_dir: Path | None = None) -> None:
        if cache_dir is not None and not isinstance(cache_dir, Path):
            raise TypeError("HuggingFaceRowSource.cache_dir must be pathlib.Path")
        self._cache_dir = cache_dir

    def load_config(
        self,
        *,
        repository: str,
        configuration: str,
        split: str,
        revision: str,
    ) -> DatasetSnapshot:
        try:
            from datasets import load_dataset
        except ImportError as exc:
            raise DatasetLoadError(
                "Hugging Face datasets is required to load ChemBench"
            ) from exc

        kwargs: dict[str, object] = {
            "path": repository,
            "name": configuration,
            "split": split,
            "revision": revision,
        }
        if self._cache_dir is not None:
            kwargs["cache_dir"] = str(self._cache_dir)

        try:
            dataset = load_dataset(**kwargs)
        except Exception as exc:
            raise DatasetLoadError(
                f"failed to load pinned ChemBench config {configuration}"
            ) from exc

        features = getattr(dataset, "features", None)
        if features is None or not hasattr(features, "to_dict"):
            raise DatasetLoadError(
                f"ChemBench config {configuration} did not expose a feature schema"
            )
        schema = features.to_dict()
        if not isinstance(schema, Mapping):
            raise DatasetLoadError(
                f"ChemBench config {configuration} exposed a non-mapping schema"
            )
        if not isinstance(dataset, Iterable):
            raise DatasetLoadError(
                f"ChemBench config {configuration} exposed non-iterable rows"
            )
        return DatasetSnapshot(rows=dataset, schema=schema)


@dataclass(frozen=True, slots=True)
class DatasetProvenance:
    """Non-secret identity recorded for one loaded ChemBench config."""

    revision: str
    config_name: str
    schema_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or _COMMIT_HASH.fullmatch(self.revision) is None:
            raise ValueError("DatasetProvenance.revision must be a full commit hash")
        if self.config_name not in CHEMBENCH_CONFIGS:
            raise ValueError("DatasetProvenance.config_name is not a ChemBench config")
        if (
            not isinstance(self.schema_hash, str)
            or _SCHEMA_HASH.fullmatch(self.schema_hash) is None
        ):
            raise ValueError("DatasetProvenance.schema_hash must be a SHA-256 digest")


@dataclass(frozen=True, slots=True, repr=False)
class LoadedPrivateTasks:
    """Private atomic tasks plus non-secret config provenance."""

    provenance: DatasetProvenance
    tasks: tuple[PrivateTask, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, DatasetProvenance):
            raise TypeError("LoadedPrivateTasks.provenance must be DatasetProvenance")
        if not isinstance(self.tasks, tuple) or not self.tasks:
            raise ValueError("LoadedPrivateTasks.tasks must be a non-empty tuple")
        if not all(type(task) is PrivateTask for task in self.tasks):
            raise TypeError("LoadedPrivateTasks.tasks must contain exact PrivateTask values")


class ChemBenchDatasetLoader:
    """Expand every example from exact ChemBench configs into atomic private tasks."""

    __slots__ = ("_config", "_revision", "_row_source")

    def __init__(
        self,
        *,
        revision: str,
        config: ChemBenchDatasetConfig | None = None,
        row_source: ChemBenchRowSource | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        if not isinstance(revision, str) or _COMMIT_HASH.fullmatch(revision) is None:
            raise ValueError(
                "ChemBenchDatasetLoader.revision must be a full lowercase commit hash"
            )
        if config is not None and not isinstance(config, ChemBenchDatasetConfig):
            raise TypeError("config must be ChemBenchDatasetConfig")
        if row_source is not None and cache_dir is not None:
            raise ValueError("cache_dir cannot be combined with a custom row_source")
        self._revision = revision
        self._config = config if config is not None else ChemBenchDatasetConfig()
        self._row_source = (
            row_source
            if row_source is not None
            else HuggingFaceRowSource(cache_dir=cache_dir)
        )

    @property
    def revision(self) -> str:
        return self._revision

    @property
    def configurations(self) -> tuple[str, ...]:
        return self._config.configurations

    def load_all(self) -> tuple[LoadedPrivateTasks, ...]:
        """Load every configured topic in deterministic configuration order."""

        return tuple(self.load_config(name) for name in self._config.configurations)

    def load_config(self, config_name: str) -> LoadedPrivateTasks:
        """Load one selected config and expand all of its ``examples[]`` entries."""

        if config_name not in self._config.configurations:
            raise ValueError("config_name is not selected by ChemBenchDatasetConfig")
        snapshot = self._row_source.load_config(
            repository=self._config.repository,
            configuration=config_name,
            split=self._config.split,
            revision=self._revision,
        )
        schema_hash = canonical_schema_hash(snapshot.schema)
        _validate_required_schema(snapshot.schema, config_name=config_name)

        tasks: list[PrivateTask] = []
        for row_index, row in enumerate(snapshot.rows):
            tasks.extend(
                _expand_row(
                    row,
                    config_name=config_name,
                    row_index=row_index,
                )
            )
        if not tasks:
            raise DatasetLoadError(
                f"pinned ChemBench config {config_name} contained no atomic tasks"
            )
        return LoadedPrivateTasks(
            provenance=DatasetProvenance(
                revision=self._revision,
                config_name=config_name,
                schema_hash=schema_hash,
            ),
            tasks=tuple(tasks),
        )


def canonical_schema_hash(schema: Mapping[str, object]) -> str:
    """Hash the complete feature schema without including any dataset row."""

    if not isinstance(schema, Mapping):
        raise TypeError("schema must be a mapping")
    try:
        canonical = json.dumps(
            dict(schema),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DatasetLoadError("ChemBench feature schema is not canonical JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def _validate_required_schema(
    schema: Mapping[str, object],
    *,
    config_name: str,
) -> None:
    missing = sorted(_REQUIRED_SCHEMA_FIELDS - set(schema))
    if missing:
        raise DatasetLoadError(
            f"ChemBench config {config_name} schema is missing required fields: "
            f"{', '.join(missing)}"
        )


def _expand_row(
    row: object,
    *,
    config_name: str,
    row_index: int,
) -> tuple[PrivateTask, ...]:
    if not isinstance(row, Mapping):
        raise DatasetFormatError(
            f"ChemBench config {config_name} row {row_index} must be a mapping"
        )

    examples = row.get("examples")
    metrics = _parse_metrics(
        row.get("metrics"),
        config_name=config_name,
        row_index=row_index,
    )
    uuid = row.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        raise DatasetFormatError(
            f"ChemBench config {config_name} row {row_index} has invalid uuid"
        )
    if not isinstance(examples, (list, tuple)) or not examples:
        raise DatasetFormatError(
            f"ChemBench config {config_name} row {row_index} has invalid examples"
        )

    tasks: list[PrivateTask] = []
    for example_index, example in enumerate(examples):
        tasks.append(
            _private_task_from_example(
                example,
                metrics=metrics,
                uuid=uuid,
                config_name=config_name,
                row_index=row_index,
                example_index=example_index,
            )
        )
    return tuple(tasks)


def _private_task_from_example(
    example: object,
    *,
    metrics: tuple[str, ...],
    uuid: str,
    config_name: str,
    row_index: int,
    example_index: int,
) -> PrivateTask:
    context = (
        f"ChemBench config {config_name} row {row_index} example {example_index}"
    )
    if not isinstance(example, Mapping):
        raise DatasetFormatError(f"{context} must be a mapping")

    question = example.get("input")
    if not isinstance(question, str) or not question.strip():
        raise DatasetFormatError(f"{context} has invalid input")
    target_value = example.get("target")
    if target_value is None:
        target = ""
    elif isinstance(target_value, str):
        target = target_value
    else:
        raise DatasetFormatError(f"{context} has invalid target")

    target_scores = _parse_target_scores(
        example.get("target_scores"),
        context=context,
    )
    try:
        return PrivateTask(
            question=question,
            options=tuple(item.option for item in target_scores),
            target=target,
            target_scores=target_scores,
            metrics=metrics,
            uuid=uuid,
        )
    except (TypeError, ValueError) as exc:
        raise DatasetFormatError(f"{context} failed PrivateTask validation: {exc}") from None


def _parse_metrics(
    value: object,
    *,
    config_name: str,
    row_index: int,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise DatasetFormatError(
            f"ChemBench config {config_name} row {row_index} has invalid metrics"
        )
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise DatasetFormatError(
            f"ChemBench config {config_name} row {row_index} has invalid metrics"
        )
    return tuple(value)


def _parse_target_scores(
    value: object,
    *,
    context: str,
) -> tuple[PrivateTargetScore, ...]:
    parsed: object
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        if not value.strip():
            return ()
        try:
            parsed = json.loads(value, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, DatasetFormatError):
            raise DatasetFormatError(f"{context} has invalid target_scores JSON") from None
    else:
        parsed = value
    if parsed is None:
        return ()
    if not isinstance(parsed, Mapping):
        raise DatasetFormatError(f"{context} target_scores must decode to an object")

    scores: list[PrivateTargetScore] = []
    for option, score in parsed.items():
        try:
            scores.append(PrivateTargetScore(option=option, score=score))
        except (TypeError, ValueError) as exc:
            raise DatasetFormatError(
                f"{context} has invalid target_scores binding: {exc}"
            ) from None
    return tuple(scores)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DatasetFormatError("target_scores contains duplicate options")
        value[key] = item
    return value


__all__ = [
    "ChemBenchDatasetLoader",
    "ChemBenchRowSource",
    "DatasetFormatError",
    "DatasetLoadError",
    "DatasetProvenance",
    "DatasetSnapshot",
    "HuggingFaceRowSource",
    "LoadedPrivateTasks",
    "canonical_schema_hash",
]
