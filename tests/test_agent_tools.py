"""The agent's tools: the facts THEMIS establishes, fetchable by name.

What is protected here is less the happy path than the unhappy ones, because a small model
acts on whatever comes back. A model name that does not exist returns the closest real
names; a question the workspace cannot answer says why; a failing tool returns a result
rather than raising out of the loop. Silence is never the answer to "is there anything".
"""

from __future__ import annotations

import pytest

from themis.agent.tools import registry
from themis.agent.workspace import Workspace
from themis.conventions import Convention
from themis.eval import synthetic
from themis.models import Confidence, Evidence, Finding, Severity


@pytest.fixture(scope="module")
def project() -> Workspace:
    return Workspace(after=synthetic.project(40))


@pytest.fixture(scope="module")
def review() -> Workspace:
    acquired = synthetic.changed(synthetic.project(40), count=1)
    finding = Finding(
        rule_id="F2001",
        family="F2",
        title="Filter added: amount > 0",
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        evidence=Evidence(model_name=acquired.changed_models[0], note="WHERE conjunct added"),
        consequence="rows are excluded",
    )
    return Workspace(
        after=acquired.after,
        before=acquired.before,
        changed_models=acquired.changed_models,
        findings=(finding,),
    )


def _run(workspace: Workspace, tool: str, **arguments: object):
    return registry()[tool].run(workspace, dict(arguments))


def test_every_tool_has_a_schema_that_forbids_unknown_arguments() -> None:
    for tool in registry().values():
        assert tool.parameters["type"] == "object"
        assert tool.parameters["additionalProperties"] is False
        assert tool.description


def test_search_finds_models_by_part_of_a_name(project: Workspace) -> None:
    result = _run(project, "search_models", query="fct_")
    assert result.ok and "fct_0" in result.text
    assert "seed" not in result.text


def test_an_unknown_model_gets_the_closest_real_names(project: Workspace) -> None:
    result = _run(project, "model_details", model="stg_0x")
    assert not result.ok
    assert "There is no model named stg_0x" in result.text
    assert "Closest names: stg_0" in result.text


def test_model_sql_is_the_compiled_sql(project: Workspace) -> None:
    result = _run(project, "model_sql", model="stg_0")
    assert result.ok
    assert "compiled SQL of stg_0 (after)" in result.text and "cast(" in result.text


def test_a_review_only_question_on_a_project_says_why(project: Workspace) -> None:
    result = _run(project, "sql_diff", model="stg_0")
    assert not result.ok
    assert "needs a review of a change" in result.text


def test_grain_reports_how_it_was_established(project: Workspace) -> None:
    grouped = next(
        name for name, grain in project.grains.items() if name.startswith("fct_") and grain.columns
    )
    result = _run(project, "grain", model=grouped)
    assert "established by" in result.text


def test_lineage_into_a_seed_says_the_source_is_untraced_not_absent(project: Workspace) -> None:
    """ "No upstream column" would read as a constant. It is a seed nobody traces."""
    result = _run(project, "column_lineage", model="stg_0", column="amount")
    assert result.ok, result.text
    assert "a seed or source whose columns THEMIS does not trace" in result.text


def test_lineage_between_models_names_the_upstream_columns(project: Workspace) -> None:
    intermediate = next(name for name in project.after.models if name.startswith("int_"))
    graph = project.lineage(intermediate)
    column = graph.outputs[intermediate][0]
    result = _run(project, "column_lineage", model=intermediate, column=column)
    assert result.ok, result.text
    assert "stg_" in result.text


def test_lineage_of_a_column_that_does_not_exist_lists_the_real_ones(project: Workspace) -> None:
    result = _run(project, "column_lineage", model="stg_0", column="no_such_column")
    assert not result.ok and "amount" in result.text


def test_downstream_models_carry_their_tags(project: Workspace) -> None:
    result = _run(project, "downstream_models", model="stg_0")
    assert result.ok


def test_rules_are_explained_including_the_safety_nets(project: Workspace) -> None:
    assert "F1001" in _run(project, "explain_rule", rule_id="f1001").text
    assert "safety net" in _run(project, "explain_rule", rule_id="X0001").text
    assert not _run(project, "explain_rule", rule_id="Z9999").ok


def test_review_tools_see_the_change(review: Workspace) -> None:
    changed = review.changed_models[0]
    assert changed in _run(review, "changed_models").text
    assert "#1 F2001" in _run(review, "findings", model=changed).text
    diff = _run(review, "sql_diff", model=changed)
    assert diff.ok and "+where status is not null and amount > 0" in diff.text


def test_no_findings_is_said_not_implied(review: Workspace) -> None:
    result = _run(review, "findings", rule="F9999")
    assert "recorded no findings" in result.text


def test_nothing_measured_is_said_when_execution_did_not_run(review: Workspace) -> None:
    result = _run(review, "measured_change", model=review.changed_models[0])
    assert not result.ok and "did not build both revisions" in result.text


def test_conventions_are_filtered_by_model_and_rule() -> None:
    workspace = Workspace(
        after=synthetic.project(10),
        conventions=(
            Convention(
                id="fx",
                condition="c",
                guidance="g",
                implication="i",
                rules=("F1001",),
                models=("int_*",),
            ),
            Convention(id="everywhere", condition="c", guidance="g", implication="i"),
        ),
    )
    assert "fx" in _run(workspace, "conventions", model="int_3", rule="F1001").text
    assert "fx —" not in _run(workspace, "conventions", model="stg_1").text
    assert "everywhere" in _run(workspace, "conventions", model="stg_1").text


def test_a_tool_that_fails_returns_a_result_rather_than_raising(project: Workspace) -> None:
    from themis.agent.tools import Tool, ToolResult

    def broken(workspace: Workspace, arguments: dict[str, object]) -> ToolResult:
        raise RuntimeError("boom")

    tool = Tool("broken", "fails", {"type": "object", "properties": {}}, broken)
    result = tool.run(project, {})
    assert not result.ok and "boom" in result.text


def test_every_example_is_a_valid_call_of_its_own_tool() -> None:
    """An example the schema would reject teaches the model a call it cannot make."""
    for tool in registry().values():
        properties = tool.parameters.get("properties", {})
        if not properties:
            continue
        assert tool.example, tool.name
        assert set(tool.example) <= set(properties), tool.name
        assert set(tool.parameters.get("required", [])) <= set(tool.example), tool.name
        for name, value in tool.example.items():
            enum = properties[name].get("enum")
            assert enum is None or value in enum, (tool.name, name)
