"""The MCP adapter's conversion, tested without the optional SDK.

An MCP client's arguments are not produced under constrained decoding the way the built-in
agent's are, so the adapter checks them against the schema and answers with the example
of a correct call rather than passing a malformed call through to a tool.
"""

from __future__ import annotations

from themis.agent.mcp_server import PRIVACY_NOTICE, call, listing
from themis.agent.tools import registry
from themis.agent.workspace import Workspace
from themis.eval import synthetic


def _workspace() -> Workspace:
    return Workspace(after=synthetic.project(20))


def test_every_tool_is_listed_with_its_schema() -> None:
    tools = registry()
    listed = listing(tools)
    assert {entry["name"] for entry in listed} == set(tools)
    assert all(entry["input_schema"]["type"] == "object" for entry in listed)


def test_a_call_runs_the_same_tool_the_agent_uses() -> None:
    text, data, is_error = call(registry(), _workspace(), "model_details", {"model": "stg_0"})
    assert not is_error
    assert "materialization: view" in text
    assert data["model"] == "stg_0"


def test_missing_and_unknown_arguments_are_refused_with_an_example() -> None:
    text, _, is_error = call(
        registry(), _workspace(), "column_lineage", {"model": "stg_0", "col": "x"}
    )
    assert is_error
    assert "missing column" in text and "unknown col" in text
    # The example shows the shape of a correct call — asserted by its arguments rather than
    # by its values, which are free to change as the tool learns to answer more.
    assert '"model":' in text and '"column":' in text


def test_an_unknown_tool_lists_the_real_ones() -> None:
    text, _, is_error = call(registry(), _workspace(), "run_sql", {"sql": "drop table x"})
    assert is_error and "No tool named run_sql" in text and "model_sql" in text


def test_the_privacy_notice_names_the_risk() -> None:
    assert "hosted model" in PRIVACY_NOTICE and "SQL under review" in PRIVACY_NOTICE
