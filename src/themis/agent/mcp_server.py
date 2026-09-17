"""The same tools, served over the Model Context Protocol.

An external agent — an IDE assistant, a desktop client — can then investigate a dbt change
with exactly the evidence the built-in agent uses, through exactly the same functions.

**Read this before connecting a client.** Tool results contain the SQL under review. MCP
itself sends nothing anywhere; the client decides where results go, and a client backed by
a hosted model sends them to that model's provider. For proprietary SQL, connect only a
client that runs a local model. THEMIS cannot enforce that from the server side, so it is
said here, in `themis mcp --help`, and on startup.

The SDK is an optional extra (`pip install 'themis[mcp]'`): it brings its own dependency
tree, and a deployment that never serves MCP should not have to review it. The conversion
between the registry and MCP lives in plain functions below, testable without the SDK.
"""

from __future__ import annotations

import json
from typing import Any

from themis.agent.tools import Tool, registry
from themis.agent.workspace import Workspace

PRIVACY_NOTICE = (
    "Tool results include the SQL under review. A client backed by a hosted model sends "
    "them to that provider; connect only a local-model client to proprietary projects."
)


def listing(tools: dict[str, Tool]) -> list[dict[str, Any]]:
    """Tool definitions as MCP describes them: name, description, input schema."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
        for tool in sorted(tools.values(), key=lambda t: t.name)
    ]


def call(
    tools: dict[str, Tool], workspace: Workspace, name: str, arguments: dict[str, Any] | None
) -> tuple[str, dict[str, Any], bool]:
    """Run one tool for a client: (text, structured data, is_error).

    Arguments are checked against the schema's declared properties here, because unlike the
    built-in agent an MCP client's arguments are not produced under constrained decoding.
    """
    tool = tools.get(name)
    if tool is None:
        return f"No tool named {name}. Tools: {', '.join(sorted(tools))}.", {}, True
    given = dict(arguments or {})
    properties = tool.parameters.get("properties", {})
    unknown = sorted(set(given) - set(properties))
    missing = sorted(set(tool.parameters.get("required", [])) - set(given))
    if unknown or missing:
        problems = []
        if missing:
            problems.append(f"missing {', '.join(missing)}")
        if unknown:
            problems.append(f"unknown {', '.join(unknown)}")
        example = f" Example: {json.dumps(tool.example)}" if tool.example else ""
        return f"Invalid arguments for {name}: {'; '.join(problems)}.{example}", {}, True
    result = tool.run(workspace, given)
    return result.text, result.data, not result.ok


def serve(workspace: Workspace) -> None:  # pragma: no cover - exercised only with the SDK
    """Serve the registry over stdio until the client disconnects."""
    try:
        import anyio
        import mcp.server.stdio
        import mcp.types as types
        from mcp.server import Server
    except ImportError as exc:
        raise RuntimeError(
            "the MCP server needs the optional SDK: pip install 'themis[mcp]'"
        ) from exc

    tools = registry()

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=entry["name"],
                    description=entry["description"],
                    input_schema=entry["input_schema"],
                )
                for entry in listing(tools)
            ]
        )

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        text, data, is_error = call(tools, workspace, params.name, params.arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=data or None,
            is_error=is_error,
        )

    server = Server("themis", on_list_tools=on_list_tools, on_call_tool=on_call_tool)

    async def run() -> None:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(run)
