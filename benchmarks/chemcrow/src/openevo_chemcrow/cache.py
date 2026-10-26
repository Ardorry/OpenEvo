from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256
from .models import ObservationSource, ToolObservation


class PairObservationCache:
    """Replay identical observations only inside one baseline/evolved pair."""

    def __init__(self, root: Path, *, pair_id: str) -> None:
        if not pair_id or "/" in pair_id or ".." in pair_id:
            raise ValueError("invalid pair cache identity")
        self.root = root / pair_id
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(tool_name: str, arguments: dict[str, Any]) -> str:
        return canonical_sha256({"tool_name": tool_name, "arguments": arguments})

    def execute(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        live: Callable[[], ToolObservation],
    ) -> ToolObservation:
        key = self.key(tool_name, arguments)
        path = self.root / f"{key}.json"
        if path.is_file():
            prior = ToolObservation.model_validate_json(path.read_text(encoding="utf-8"))
            if prior.source in {ObservationSource.FIXTURE, ObservationSource.MOCK}:
                source = prior.source
            else:
                source = ObservationSource.CACHE_REPLAY
            return prior.model_copy(update={"call_id": call_id, "source": source})
        observation = live()
        if observation.canonical_arguments_sha256 != canonical_sha256(arguments):
            raise ValueError("tool observation arguments do not match cache authority")
        path.write_text(observation.model_dump_json(indent=2), encoding="utf-8")
        return observation


def assert_real_metric_observations(observations: list[ToolObservation]) -> None:
    contaminated = [
        item.call_id
        for item in observations
        if item.source in {ObservationSource.FIXTURE, ObservationSource.MOCK}
    ]
    if contaminated:
        raise ValueError(f"real benchmark metrics contain fixture/mock calls: {contaminated}")
