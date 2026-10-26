"""The graph, as one principal sees it, through the warehouse-graph MCP tools (task 5.5).

Retrievers never touch the graph file: they call the five MCP tools over an in-process MCP
client, with the server bound to the asking user's identity. Role filtering is therefore the
server's job, so a retriever bug can't surface a hidden node. Calls are memoised per session
(the graph is read-only at runtime), which keeps parameter sweeps fast.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client
from mcp.types import CallToolResult, TextContent
from warehouse_graph.governance.policies import PolicyFile
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder
from warehouse_graph.mcp_server import create_server

NOT_FOUND = "unknown node id"


def identity_for(policies: PolicyFile, role: str) -> str:
    """The first identity that maps to `role` (the eval asks as a representative user)."""
    for identity, principal in sorted(policies.identities.items()):
        if principal == role:
            return identity
    raise KeyError(f"no identity maps to principal {role}")


class ToolError(RuntimeError):
    pass


class GraphTools:
    def __init__(self, client: Client) -> None:
        self._client = client
        self._memo: dict[str, Any] = {}
        self.calls = 0

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        key = json.dumps([tool, args], sort_keys=True)
        if key in self._memo:
            cached: dict[str, Any] | None = self._memo[key]
            return cached
        self.calls += 1
        result: CallToolResult = await self._client.call_tool(tool, args)
        if result.is_error:
            text = result.content[0].text if isinstance(result.content[0], TextContent) else ""
            if NOT_FOUND in text:
                self._memo[key] = None
                return None
            raise ToolError(f"{tool} failed: {text}")
        data: dict[str, Any] = result.structured_content or {}
        self._memo[key] = data
        return data

    async def search(
        self,
        query: str,
        *,
        k: int = 10,
        types: list[str] | None = None,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"query": query, "k": k, "mode": mode}
        if types:
            args["types"] = types
        data = await self._call("search_nodes", args)
        return list((data or {}).get("hits", []))

    async def node(self, node_id: str) -> dict[str, Any] | None:
        """Full record, or None when the node doesn't exist *for this principal*."""
        return await self._call("get_node", {"id": node_id})

    async def lineage(
        self, node_id: str, direction: str = "upstream", depth: int = 3, level: str = "model"
    ) -> dict[str, Any] | None:
        args = {"id": node_id, "direction": direction, "depth": depth, "level": level}
        return await self._call("get_lineage", args)

    async def metric(self, name: str) -> dict[str, Any] | None:
        return await self._call("get_metric", {"name": name})


@asynccontextmanager
async def open_tools(
    graph: GraphArtifact, policies: PolicyFile, role: str, embedder: Embedder
) -> AsyncIterator[GraphTools]:
    server = create_server(
        graph, policies, identity=identity_for(policies, role), embedder=embedder
    )
    async with Client(server) as client:
        yield GraphTools(client)
