"""The investigation loop: a local model choosing tools, and an answer it must prove.

Shaped by two measurements and one rule.

- **Small local models call tools unreliably the native way.** Qwen3 drops to emitting
  tool calls as text once it is offered more than a handful. So a step is two constrained
  completions instead of one: first choose the next tool from an enum of names, then fill
  in that tool's own JSON schema. An unknown tool or a misspelt argument cannot be produced.
- **An answer is only as good as what it quotes.** The final answer cites, for every claim,
  the number of a tool result and a verbatim quote from it, checked with the same grounding
  test the specialists get. One retry with the failures named; then a refusal.
- **The model never produces a fact.** It decides which fact to fetch. The tools compute it.

Everything the model was shown and answered is kept as ``ModelCall`` records, so agent
runs feed the same dataset as the reviewers' calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from themis.agent.tools import Tool, ToolResult, registry
from themis.agent.workspace import Workspace
from themis.config import Settings
from themis.llm.provider import LLMError, Provider, Usage
from themis.logging import get_logger
from themis.models import ModelCall
from themis.review.selfcheck import quote_is_grounded

log = get_logger(__name__)

ANSWER = "answer"

SYSTEM = """You investigate questions about a dbt project and the change under review.

You cannot see the project. You see only what tools return. Rules:
- Choose one tool at a time. Pick the tool whose result would most directly answer the
  question; do not fetch things you already have.
- When the results so far answer the question, choose "answer".
- State nothing a tool result does not show. General knowledge about dbt or SQL is not
  evidence about this project.
- If the tools cannot answer the question, say so plainly. An honest "the results do not
  show this" is a good answer; a guess is a bad one."""

_FULL_RESULTS = 3
_RESULT_CHARS = 2500
_OLDER_RESULT_CHARS = 400
_MAX_REPEATS = 2


@dataclass(frozen=True)
class Step:
    number: int
    tool: str
    arguments: dict[str, Any]
    result: ToolResult
    repeated_from: int | None = None


@dataclass(frozen=True)
class Citation:
    result: int
    quote: str


@dataclass
class AgentAnswer:
    question: str
    answer: str = ""
    grounded: bool = False
    citations: tuple[Citation, ...] = ()
    steps: tuple[Step, ...] = ()
    refusal_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    calls: list[ModelCall] = field(default_factory=list)


def _choose_schema(tools: dict[str, Tool]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "description": "One sentence: what is still missing."},
            "next": {"type": "string", "enum": [*sorted(tools), ANSWER]},
        },
        "required": ["thought", "next"],
    }


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "can_answer": {"type": "boolean"},
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "result": {"type": "integer", "description": "The [n] of a tool result."},
                    "quote": {"type": "string", "description": "Copied exactly from it."},
                },
                "required": ["result", "quote"],
            },
        },
    },
    "required": ["can_answer", "answer", "citations"],
}


def _catalogue(tools: dict[str, Tool]) -> str:
    lines = []
    for tool in sorted(tools.values(), key=lambda t: t.name):
        properties = tool.parameters.get("properties", {})
        required = set(tool.parameters.get("required", []))
        arguments = ", ".join(f"{name}{'' if name in required else '?'}" for name in properties)
        lines.append(f"- {tool.name}({arguments}): {tool.description}")
    return "\n".join(lines)


def _transcript(steps: list[Step], *, generous: bool = False) -> str:
    if not steps:
        return "(nothing fetched yet)"
    blocks = []
    recent = {step.number for step in steps[-_FULL_RESULTS:]}
    for step in steps:
        limit = _RESULT_CHARS if (generous or step.number in recent) else _OLDER_RESULT_CHARS
        text = step.result.text
        if len(text) > limit:
            text = text[:limit] + "\n... (truncated)"
        arguments = ", ".join(f"{k}={v}" for k, v in sorted(step.arguments.items()))
        # The call and its result are marked apart. A model quoted the call line as though it
        # were part of the result, which no tool returned and no check could accept.
        blocks.append(
            f"[{step.number}] you called {step.tool}({arguments}); it returned:\n<<<\n{text}\n>>>"
        )
    return "\n\n".join(blocks)


class _Session:
    def __init__(
        self,
        question: str,
        workspace: Workspace,
        provider: Provider,
        settings: Settings,
        tools: dict[str, Tool],
    ) -> None:
        self.question = question
        self.workspace = workspace
        self.provider = provider
        self.settings = settings
        self.tools = tools
        self.steps: list[Step] = []
        self.outcome = AgentAnswer(question=question)

    def _call(self, seat: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        model = self.settings.llm_supervisor_model
        response = self.provider.complete(system=SYSTEM, prompt=prompt, schema=schema, model=model)
        self.outcome.usage.add(response.usage)
        self.outcome.calls.append(
            ModelCall(
                seat=seat,
                model=model,
                context=prompt,
                system=SYSTEM,
                response=dict(response.payload),
            )
        )
        return response.payload

    def choose(self) -> str:
        prompt = (
            f"## Question\n{self.question}\n\n## Tools\n{_catalogue(self.tools)}\n\n"
            f"## What the tools returned so far\n{_transcript(self.steps)}\n\n"
            "Choose the next tool, or answer."
        )
        payload = self._call("agent.choose", prompt, _choose_schema(self.tools))
        choice = str(payload.get("next", ANSWER))
        return choice if choice in self.tools or choice == ANSWER else ANSWER

    def arguments(self, tool: Tool) -> dict[str, Any]:
        if not tool.parameters.get("properties"):
            return {}
        prompt = (
            f"## Question\n{self.question}\n\n"
            f"## What the tools returned so far\n{_transcript(self.steps)}\n\n"
            f"## Calling {tool.name}\n{tool.description}\n"
            + (
                f"Format of a call (the shape, not the values to use): {json.dumps(tool.example)}\n"
                if tool.example
                else ""
            )
            + "Give its arguments: plain values taken from the question and the results, "
            "never a function call."
        )
        payload = self._call(f"agent.arguments.{tool.name}", prompt, tool.parameters)
        allowed = set(tool.parameters.get("properties", {}))
        return {key: value for key, value in payload.items() if key in allowed}

    def execute(self, tool: Tool, arguments: dict[str, Any]) -> bool:
        """Run a tool, or recognise a repeat. False when repeats say it is time to answer."""
        key = (tool.name, json.dumps(arguments, sort_keys=True))
        number = len(self.steps) + 1
        for step in self.steps:
            if (step.tool, json.dumps(step.arguments, sort_keys=True)) == key:
                self.steps.append(
                    Step(
                        number=number,
                        tool=tool.name,
                        arguments=arguments,
                        result=ToolResult(text=f"(the same call as [{step.number}]; see it above)"),
                        repeated_from=step.number,
                    )
                )
                repeats = sum(1 for s in self.steps if s.repeated_from is not None)
                return repeats < _MAX_REPEATS
        result = tool.run(self.workspace, arguments)
        log.debug("agent.tool", tool=tool.name, ok=result.ok, chars=len(result.text))
        self.steps.append(Step(number=number, tool=tool.name, arguments=arguments, result=result))
        return True

    def answer(self, feedback: str | None = None) -> tuple[dict[str, Any], list[str]]:
        prompt = (
            f"## Question\n{self.question}\n\n"
            f"## What the tools returned\n{_transcript(self.steps, generous=True)}\n\n"
            "Answer the question using only these results. Every claim needs a citation: the "
            "[n] of the result and a quote copied exactly from between that result's <<< and >>> "
            "— copy whole lines as they are written, including the model name that starts them. "
            "If one result already lists what the question asks for, answer from it; do not "
            "infer a relationship that no line states. If the results do not answer the "
            "question, set can_answer to false and say what is missing."
        )
        if feedback:
            prompt += f"\n\nYour previous answer was rejected: {feedback} Quote exactly."
        payload = self._call("agent.answer", prompt, ANSWER_SCHEMA)
        return payload, self._check(payload)

    def _check(self, payload: dict[str, Any]) -> list[str]:
        if not payload.get("can_answer"):
            return []
        problems: list[str] = []
        citations = payload.get("citations") or []
        if not citations:
            return ["it cited no tool result"]
        by_number = {step.number: step for step in self.steps}
        for citation in citations:
            number = citation.get("result")
            quote = str(citation.get("quote", "")).strip()
            step = by_number.get(number) if isinstance(number, int) else None
            if step is not None and quote_is_grounded(quote, step.result.text):
                continue
            # The quote may be verbatim from a result the model was shown, filed under the
            # wrong number: a small model quoting "tags: regulatory, recon" from [3] as [2].
            # The guarantee is that every quote is really in a tool result, and it still
            # holds, so the citation is corrected rather than a grounded answer discarded.
            elsewhere = next(
                (
                    other
                    for other in self.steps
                    if other.repeated_from is None and quote_is_grounded(quote, other.result.text)
                ),
                None,
            )
            if elsewhere is not None:
                log.info("agent.citation_corrected", cited=number, found=elsewhere.number)
                citation["result"] = elsewhere.number
                continue
            if step is None:
                problems.append(f"it cited result [{number}], which does not exist")
            else:
                problems.append(f"its quote from [{number}] is not in that result: {quote[:80]!r}")
        return problems


def investigate(
    question: str,
    workspace: Workspace,
    *,
    provider: Provider,
    settings: Settings,
    tools: dict[str, Tool] | None = None,
    max_steps: int = 6,
) -> AgentAnswer:
    """Answer a question by choosing tools, or refuse with the reason."""
    session = _Session(question, workspace, provider, settings, tools or registry())
    outcome = session.outcome
    try:
        for _ in range(max_steps):
            choice = session.choose()
            if choice == ANSWER:
                break
            tool = session.tools[choice]
            if not session.execute(tool, session.arguments(tool)):
                break

        payload, problems = session.answer()
        if problems:
            log.info("agent.answer_rejected", problems=problems)
            payload, problems = session.answer(feedback="; ".join(problems) + ".")
    except LLMError as exc:
        outcome.steps = tuple(session.steps)
        outcome.refusal_reason = f"the model could not be reached: {exc}"
        return outcome

    outcome.steps = tuple(session.steps)
    if outcome.calls:
        outcome.calls[-1] = outcome.calls[-1].model_copy(
            update={"accepted": not problems, "rejected_reason": "; ".join(problems) or None}
        )
    if not payload.get("can_answer"):
        outcome.refusal_reason = str(payload.get("answer") or "the tools did not answer it")
        return outcome
    if problems:
        outcome.refusal_reason = "the answer could not be verified against the tools: " + "; ".join(
            problems
        )
        return outcome

    outcome.answer = str(payload.get("answer", "")).strip()
    outcome.grounded = True
    outcome.citations = tuple(
        Citation(result=int(c["result"]), quote=str(c["quote"]).strip())
        for c in payload.get("citations") or []
    )
    return outcome
