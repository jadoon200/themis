"""`themis agent` on the command line: one question, or a session over one loaded review.

The session exists because loading a review means compiling both revisions, which takes
minutes on a real project. Asking three questions must load it once, not three times.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from themis.cli import app
from themis.llm.provider import Response, Usage


def _manifest(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "target").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    manifest = project / "target" / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "metadata": {},
                "nodes": {
                    "model.demo.stg_orders": {
                        "resource_type": "model",
                        "name": "stg_orders",
                        "original_file_path": "models/stg_orders.sql",
                        "raw_code": "select id, amount from raw",
                        "compiled_code": "select id, amount from raw",
                        "config": {"materialized": "view"},
                        "depends_on": {"nodes": [], "macros": []},
                    }
                },
                "child_map": {"model.demo.stg_orders": []},
            }
        )
    )
    return project


class _Details:
    """Fetches the model's details, then quotes its materialization."""

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any], model: str) -> Response:
        properties = schema.get("properties", {})
        if "next" in properties:
            fetched = "[1] model_details" in prompt
            payload: dict[str, Any] = {
                "thought": "",
                "next": "answer" if fetched else "model_details",
            }
        elif "can_answer" in properties:
            payload = {
                "can_answer": True,
                "answer": "It is a view.",
                "citations": [{"result": 1, "quote": "materialization: view"}],
            }
        else:
            payload = {"model": "stg_orders"}
        return Response(payload=payload, usage=Usage(calls=1))


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from themis.agent import workspace as workspace_module
    from themis.llm import provider as provider_module

    loads: list[int] = []
    original = workspace_module.Workspace.from_manifest.__func__  # type: ignore[attr-defined]

    def counting(cls: Any, *args: Any, **kwargs: Any) -> Any:
        loads.append(1)
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(workspace_module.Workspace, "from_manifest", classmethod(counting))
    monkeypatch.setattr(provider_module, "build_provider", lambda settings: _Details())
    return loads


def test_one_question_answers_and_exits_zero(tmp_path: Path, scripted: list[int]) -> None:
    project = _manifest(tmp_path)
    result = CliRunner().invoke(
        app, ["agent", "How is stg_orders built?", "--project", str(project), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["grounded"] is True
    assert payload["citations"][0]["tool"] == "model_details"


def test_a_session_loads_the_workspace_once_for_every_question(
    tmp_path: Path, scripted: list[int]
) -> None:
    project = _manifest(tmp_path)
    result = CliRunner().invoke(
        app,
        ["agent", "--project", str(project)],
        input="How is stg_orders built?\nAnd again?\nOnce more?\n\n",
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.count("It is a view.") == 3
    assert len(scripted) == 1


def test_a_missing_manifest_cannot_start(tmp_path: Path, scripted: list[int]) -> None:
    result = CliRunner().invoke(app, ["agent", "anything", "--project", str(tmp_path)])
    assert result.exit_code == 2
