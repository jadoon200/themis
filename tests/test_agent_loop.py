"""The investigation loop, driven by a scripted model so every path is deterministic.

The properties that matter: an answer is only returned when every citation quotes a tool
result verbatim; a fabricated quote gets one retry with the failure named and is then
refused; a repeated call is not re-run and eventually forces an answer; an unreachable
model is a refusal, not a crash; and every exchange is kept for the dataset.
"""

from __future__ import annotations

from typing import Any

from themis.agent.loop import investigate
from themis.agent.workspace import Workspace
from themis.config import Settings
from themis.eval import synthetic
from themis.llm.provider import LLMError, Response, Usage


class ScriptedModel:
    """Answers each kind of step from its own queue, recognised by the schema asked for."""

    def __init__(
        self, choices: list[str], arguments: list[dict[str, Any]], answers: list[dict[str, Any]]
    ):
        self.choices = list(choices)
        self.arguments = list(arguments)
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any], model: str) -> Response:
        self.prompts.append(prompt)
        properties = schema.get("properties", {})
        if "next" in properties:
            payload: dict[str, Any] = {
                "thought": "",
                "next": self.choices.pop(0) if self.choices else "answer",
            }
        elif "can_answer" in properties:
            payload = self.answers.pop(0)
        else:
            payload = self.arguments.pop(0)
        return Response(payload=payload, usage=Usage(calls=1))


class DownModel:
    def complete(self, **_: Any) -> Response:
        raise LLMError("connection refused")


def _workspace() -> Workspace:
    return Workspace(after=synthetic.project(30))


def _grounded_answer(quote: str, result: int = 1) -> dict[str, Any]:
    return {
        "can_answer": True,
        "answer": "It is a view.",
        "citations": [{"result": result, "quote": quote}],
    }


def test_an_answer_quoting_a_tool_result_is_returned() -> None:
    model = ScriptedModel(
        choices=["model_details", "answer"],
        arguments=[{"model": "stg_0"}],
        answers=[_grounded_answer("materialization: view")],
    )
    outcome = investigate(
        "How is stg_0 materialized?", _workspace(), provider=model, settings=Settings()
    )
    assert outcome.grounded, outcome.refusal_reason
    assert outcome.answer == "It is a view."
    assert [step.tool for step in outcome.steps] == ["model_details"]
    assert outcome.citations[0].result == 1


def test_a_fabricated_quote_is_retried_once_then_refused() -> None:
    model = ScriptedModel(
        choices=["model_details", "answer"],
        arguments=[{"model": "stg_0"}],
        answers=[
            _grounded_answer("materialization: incremental with merge"),
            _grounded_answer("materialization: incremental with merge"),
        ],
    )
    outcome = investigate("How is stg_0 built?", _workspace(), provider=model, settings=Settings())
    assert not outcome.grounded
    assert outcome.refusal_reason is not None and "not in that result" in outcome.refusal_reason
    assert "Your previous answer was rejected" in model.prompts[-1]
    assert outcome.calls[-1].accepted is False


def test_a_quote_fixed_on_the_retry_is_accepted() -> None:
    model = ScriptedModel(
        choices=["model_details", "answer"],
        arguments=[{"model": "stg_0"}],
        answers=[
            _grounded_answer("materialization: table"),
            _grounded_answer("materialization: view"),
        ],
    )
    outcome = investigate("How is stg_0 built?", _workspace(), provider=model, settings=Settings())
    assert outcome.grounded


def test_a_true_quote_filed_under_the_wrong_result_is_corrected_not_refused() -> None:
    """A small model quoted a tool result word for word and gave it the wrong number. The
    quote is really in what it was shown, so the answer stands and the citation is fixed."""
    model = ScriptedModel(
        choices=["search_models", "model_details", "answer"],
        arguments=[{"query": "stg_0"}, {"model": "stg_0"}],
        answers=[_grounded_answer("materialization: view", result=1)],
    )
    outcome = investigate("How is stg_0 built?", _workspace(), provider=model, settings=Settings())
    assert outcome.grounded
    assert outcome.citations[0].result == 2


def test_a_quote_in_no_result_at_all_is_still_refused() -> None:
    model = ScriptedModel(
        choices=["model_details", "answer"],
        arguments=[{"model": "stg_0"}],
        answers=[
            _grounded_answer("materialization: incremental", result=1),
            _grounded_answer("materialization: incremental", result=1),
        ],
    )
    outcome = investigate("How is stg_0 built?", _workspace(), provider=model, settings=Settings())
    assert not outcome.grounded


def test_a_citation_of_a_result_that_does_not_exist_is_refused() -> None:
    model = ScriptedModel(
        choices=["answer"],
        arguments=[],
        answers=[_grounded_answer("anything", result=3), _grounded_answer("anything", result=3)],
    )
    outcome = investigate("Anything?", _workspace(), provider=model, settings=Settings())
    assert not outcome.grounded
    assert "does not exist" in (outcome.refusal_reason or "")


def test_an_honest_cannot_answer_is_a_refusal_with_its_reason() -> None:
    model = ScriptedModel(
        choices=["answer"],
        arguments=[],
        answers=[{"can_answer": False, "answer": "No tool shows ownership.", "citations": []}],
    )
    outcome = investigate("Who owns stg_0?", _workspace(), provider=model, settings=Settings())
    assert not outcome.grounded
    assert outcome.refusal_reason == "No tool shows ownership."


def test_repeating_a_call_does_not_rerun_it_and_ends_the_search() -> None:
    model = ScriptedModel(
        choices=["model_details"] * 5,
        arguments=[{"model": "stg_0"}] * 5,
        answers=[_grounded_answer("materialization: view")],
    )
    outcome = investigate("How is stg_0 built?", _workspace(), provider=model, settings=Settings())
    repeats = [step for step in outcome.steps if step.repeated_from is not None]
    assert len(repeats) == 2
    assert len(outcome.steps) == 3
    assert outcome.grounded


def test_the_step_budget_forces_an_answer() -> None:
    model = ScriptedModel(
        choices=["search_models"] * 10,
        arguments=[{"query": f"stg_{i}"} for i in range(10)],
        answers=[{"can_answer": False, "answer": "Ran out of steps.", "citations": []}],
    )
    outcome = investigate(
        "Find everything", _workspace(), provider=model, settings=Settings(), max_steps=3
    )
    assert len(outcome.steps) == 3
    assert outcome.refusal_reason == "Ran out of steps."


def test_an_unreachable_model_is_a_refusal_not_a_crash() -> None:
    outcome = investigate("Anything?", _workspace(), provider=DownModel(), settings=Settings())
    assert not outcome.grounded
    assert "could not be used" in (outcome.refusal_reason or "")


def test_every_exchange_is_kept_for_the_dataset() -> None:
    model = ScriptedModel(
        choices=["model_details", "answer"],
        arguments=[{"model": "stg_0"}],
        answers=[_grounded_answer("materialization: view")],
    )
    outcome = investigate("How is stg_0 built?", _workspace(), provider=model, settings=Settings())
    seats = [call.seat for call in outcome.calls]
    assert seats == [
        "agent.choose",
        "agent.arguments.model_details",
        "agent.choose",
        "agent.answer",
    ]
    assert all(call.context and call.system for call in outcome.calls)
