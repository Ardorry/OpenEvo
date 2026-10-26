from __future__ import annotations

import argparse
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .cache import PairObservationCache
from .hashing import canonical_sha256
from .models import ObservationSource, ToolObservation
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
    receipts: list[dict[str, Any]] = []

    def record_observation(
        *, arguments: dict[str, Any], observation: ToolObservation
    ) -> dict[str, Any]:
        receipts.append(
            {
                "arguments": arguments,
                "observation": observation.model_dump(mode="json"),
            }
        )
        return {
            "tool": observation.tool_name,
            "arguments": arguments,
            "result": observation.result,
            "error": observation.error,
            "observation_source": observation.source,
            "canonical_arguments_sha256": observation.canonical_arguments_sha256,
            "elapsed_seconds": observation.elapsed_seconds,
            "call_id": observation.call_id,
        }

    def error_observation(
        *, tool_name: str, arguments: dict[str, Any], call_id: str, error: Exception
    ) -> ToolObservation:
        return ToolObservation(
            call_id=call_id,
            tool_name=tool_name,
            canonical_arguments_sha256=canonical_sha256(arguments),
            error=f"{type(error).__name__}: {error}",
            source=ObservationSource.LIVE,
            elapsed_seconds=0.0,
        )

    def execute_tool(tool_name: str, query: str) -> dict[str, Any]:
        arguments = {"query": query}
        call_id = f"chemcrow-{uuid.uuid4().hex}"
        started = time.monotonic()

        def execute_live() -> ToolObservation:
            try:
                return registry.execute(tool_name, arguments, call_id=call_id)
            except (ToolUnavailableError, ToolInputError) as exc:
                return error_observation(
                    tool_name=tool_name,
                    arguments=arguments,
                    call_id=call_id,
                    error=exc,
                ).model_copy(update={"elapsed_seconds": time.monotonic() - started})

        observation = cache.execute(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            live=execute_live,
        )
        return record_observation(arguments=arguments, observation=observation)

    def record_route_error(
        *, tool_name: str, arguments: dict[str, Any], error: Exception
    ) -> dict[str, Any]:
        call_id = f"chemcrow-{uuid.uuid4().hex}"
        return record_observation(
            arguments=arguments,
            observation=error_observation(
                tool_name=tool_name,
                arguments=arguments,
                call_id=call_id,
                error=error,
            ),
        )

    def make_tool(tool_name: str) -> Callable[[str], str]:
        def execute(query: str) -> str:
            return json.dumps(execute_tool(tool_name, query), ensure_ascii=True, sort_keys=True)

        execute.__name__ = f"chemcrow_{tool_name.lower()}"
        execute.__doc__ = f"Invoke ChemCrow's {tool_name} tool with a single query string."
        return execute

    for name in _EXPOSED_TOOLS:
        server.tool(name=name)(make_tool(name))

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "pair_id": pair_id, "tool_count": len(_EXPOSED_TOOLS)})

    @server.custom_route("/receipts", methods=["GET"])
    async def receipt_list(_: Request) -> JSONResponse:
        return JSONResponse({"pair_id": pair_id, "receipts": receipts})

    @server.custom_route("/tool/{tool_name}", methods=["POST"])
    async def rest_tool(request: Request) -> JSONResponse:
        tool_name = request.path_params["tool_name"]
        if tool_name not in _EXPOSED_TOOLS:
            payload = record_route_error(
                tool_name=tool_name,
                arguments={},
                error=ToolUnavailableError("unknown ChemCrow tool"),
            )
            return JSONResponse(payload, status_code=404)
        try:
            body = await request.json()
            query = body.get("query") if isinstance(body, dict) else None
            if not isinstance(query, str) or not query.strip():
                raise ToolInputError("tool requires a non-empty query string")
            return JSONResponse(execute_tool(tool_name, query.strip()))
        except (RuntimeError, ToolInputError, ValueError) as exc:
            arguments = {"query": query} if "query" in locals() else {}
            payload = record_route_error(
                tool_name=tool_name,
                arguments=arguments,
                error=exc,
            )
            return JSONResponse(payload, status_code=422)
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
