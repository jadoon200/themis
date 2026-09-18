"""The MCP adapter against the real SDK, over a real stdio connection.

`test_agent_mcp.py` covers the conversion without the SDK installed. This file covers the
part that file cannot: that `serve()` still matches the protocol library it calls. A
handler wired to a keyword the SDK has renamed, or a result field it no longer carries,
fails here and nowhere else — the adapter's own functions would keep passing.

Skipped when the optional extra is absent (`pip install 'themis[mcp]'`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="the optional MCP SDK is not installed")

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from themis.agent.tools import registry

# The server the test talks to: the real `serve()`, on a synthetic project, so the test
# needs no compiled manifest and no warehouse.
_SERVER = """
import sys

# THEMIS_MCP_AUDIT: record every connection the server process attempts, so a test can
# assert that serving the tools sends nothing anywhere. The SDK carries OpenTelemetry;
# this is what says it stays a no-op rather than an egress path.
audit = __import__("os").environ.get("THEMIS_MCP_AUDIT")
if audit:
    def hook(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "urllib.Request"):
            with open(audit, "a") as handle:
                handle.write(f"{event} {args!r}\\n")
    sys.addaudithook(hook)

from themis.agent.mcp_server import serve
from themis.agent.workspace import Workspace
from themis.eval import synthetic

serve(Workspace(after=synthetic.project(12)))
"""


async def _with_session(work: Any, env: dict[str, str] | None = None) -> Any:
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", _SERVER], env={**os.environ, **(env or {})}
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        with anyio.fail_after(60):
            await session.initialize()
            return await work(session)


def _run(work: Any, env: dict[str, str] | None = None) -> Any:
    return anyio.run(_with_session, work, env)


def test_a_client_sees_every_tool_with_a_usable_schema() -> None:
    async def work(session: ClientSession) -> Any:
        return await session.list_tools()

    listed = _run(work)
    assert {tool.name for tool in listed.tools} == set(registry())
    for tool in listed.tools:
        assert tool.description
        assert tool.input_schema["type"] == "object"
        assert "properties" in tool.input_schema


def test_a_client_gets_the_same_answer_the_agent_would() -> None:
    async def work(session: ClientSession) -> Any:
        return await session.call_tool("model_details", {"model": "stg_0"})

    result = _run(work)
    assert not result.is_error
    assert "materialization: view" in result.content[0].text
    assert result.structured_content is not None
    assert result.structured_content["model"] == "stg_0"


def test_a_bad_call_comes_back_as_an_error_the_client_can_show() -> None:
    async def work(session: ClientSession) -> Any:
        unknown = await session.call_tool("run_sql", {"sql": "drop table x"})
        wrong_arguments = await session.call_tool("column_lineage", {"model": "stg_0", "col": "x"})
        return unknown, wrong_arguments

    unknown, wrong_arguments = _run(work)
    assert unknown.is_error and "No tool named run_sql" in unknown.content[0].text
    assert wrong_arguments.is_error
    assert "missing column" in wrong_arguments.content[0].text


def test_serving_the_tools_sends_nothing_anywhere(tmp_path: Path) -> None:
    """The SQL under review leaves this process only down the client's own pipe.

    The SDK depends on OpenTelemetry, so the question is worth answering with a test
    rather than with a reading of the dependency tree: a server process that resolved a
    host or opened a connection while answering would be caught here.
    """
    audit = tmp_path / "connections.log"

    async def work(session: ClientSession) -> Any:
        await session.list_tools()
        return await session.call_tool("model_details", {"model": "stg_0"})

    result = _run(work, {"THEMIS_MCP_AUDIT": str(audit)})
    assert not result.is_error
    attempts = audit.read_text() if audit.exists() else ""
    assert attempts == "", f"the MCP server tried to reach the network:\n{attempts}"
