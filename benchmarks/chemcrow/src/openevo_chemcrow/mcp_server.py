from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .cache import PairObservationCache
from .tools import ChemCrowToolRegistry, ToolInputError, ToolUnavailableError

_EXPOSED_TOOLS = (
    "wikipedia",
    "Name2SMILES",
    "Mol2CAS",
    "SMILES2Name",
    "PatentCheck",
    "MolSimilarity",
    "SMILES2Weight",
    "FunctionalGroups",
    "ExplosiveCheck",
    "ControlChemCheck",
    "SimilarityToControlChem",
    "SafetySummary",
    "WebSearch",
    "ReactionPredict",
    "ReactionRetrosynthesis",
)


def build_server(
    *,
    pair_id: str,
    cache_root: Path,
    controlled_chemicals_csv: Path,
    host: str,
    port: int,
    network_enabled: bool,
) -> FastMCP:
    registry = ChemCrowToolRegistry(
        controlled_chemicals_csv=controlled_chemicals_csv,
        network_enabled=network_enabled,
        mode="real",
    )
    cache = PairObservationCache(cache_root, pair_id=pair_id)
    server = FastMCP("ChemCrow Tools", host=host, port=port, json_response=True)

    def make_tool(tool_name: str) -> Callable[[str], str]:
        def execute(query: str) -> str:
            arguments = {"query": query}
            call_id = f"mcp-{uuid.uuid4().hex}"
            try:
                observation = cache.execute(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    live=lambda: registry.execute(tool_name, arguments, call_id=call_id),
                )
            except (ToolUnavailableError, ToolInputError) as exc:
                raise RuntimeError(f"{tool_name} unavailable: {exc}") from exc
            payload: dict[str, Any] = {
                "tool": tool_name,
                "result": observation.result,
                "error": observation.error,
                "observation_source": observation.source,
                "canonical_arguments_sha256": observation.canonical_arguments_sha256,
                "elapsed_seconds": observation.elapsed_seconds,
            }
            return json.dumps(payload, ensure_ascii=True, sort_keys=True)

        execute.__name__ = f"chemcrow_{tool_name.lower()}"
        execute.__doc__ = f"Invoke ChemCrow's {tool_name} tool with a single query string."
        return execute

    for name in _EXPOSED_TOOLS:
        server.tool(name=name)(make_tool(name))
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Pair-scoped ChemCrow MCP tool server")
    parser.add_argument("--pair-id", required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--controlled-chemicals-csv", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--disable-network", action="store_true")
    args = parser.parse_args(argv)
    server = build_server(
        pair_id=args.pair_id,
        cache_root=args.cache_root,
        controlled_chemicals_csv=args.controlled_chemicals_csv,
        host=args.host,
        port=args.port,
        network_enabled=not args.disable_network,
    )
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
