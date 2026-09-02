"""Answering a question about a stored review.

The design constraint is the one that matters most in this whole system: **a question
the artifact cannot answer gets a refusal, not an inference.** A reviewer who asks
"was the FX rate table checked for duplicates" and receives a confident, invented yes
is worse off than one who received nothing.

So the model is given only facts retrieved from the store, told explicitly it may use
nothing else, and required to say when the facts do not settle the question. The
self-check then verifies the answer quotes something it was actually shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from themis.ask.retrieval import RetrievedFacts
from themis.config import Settings
from themis.llm.provider import LLMError, Provider
from themis.logging import get_logger

log = get_logger(__name__)

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "can_answer": {"type": "boolean"},
        "answer": {"type": "string"},
        "evidence_quote": {"type": "string"},
    },
    "required": ["can_answer", "answer", "evidence_quote"],
}

SYSTEM = """You answer questions about a completed automated review of dbt SQL models.

You are given the facts that review recorded. Rules:
- Use ONLY those facts. Do not use general knowledge about dbt, SQL, or what a model
  with a given name probably contains.
- If the facts do not answer the question, set can_answer to false and say what is
  missing. Do not guess. An invented answer is worse than no answer, because it will
  be believed.
- `evidence_quote` must be copied verbatim from the facts you were given.
- Answer in two or three sentences. Quote specific numbers where they are present.
- "Nothing was found about X" is a real and useful answer when the facts show no
  finding for X. It is not a failure to answer."""


@dataclass
class Answer:
    text: str
    grounded: bool
    evidence_quote: str = ""
    refusal_reason: str | None = None


def render_facts(facts: RetrievedFacts) -> str:
    """Lay out the retrieved facts for the model. Nothing is computed here."""
    run = facts.run
    lines = [
        "## The review",
        f"project: {run.project}",
        f"comparing: {run.base_ref} -> {run.head_ref}",
        f"models reviewed: {run.models_reviewed}",
        f"both revisions were built and compared: {'yes' if run.executed else 'no'}",
    ]
    if run.degraded_reason:
        lines.append(f"grounding was degraded: {run.degraded_reason}")

    if facts.unknown_entities:
        lines += [
            "",
            "## Named in the question but absent from this review",
            ", ".join(facts.unknown_entities),
            "This review has no record of them at all — it did not examine them, so "
            "nothing here can say anything about them.",
        ]

    lines += ["", f"## Findings recorded ({len(facts.findings)})"]
    if facts.findings:
        for finding in facts.findings:
            status = ""
            if finding.suppressed_reason:
                status = f" [suppressed: {finding.suppressed_reason}]"
            elif finding.disposition:
                status = f" [a human marked this {finding.disposition}]"
            lines.append(
                f"- {finding.rule_id} on {finding.model_name}: {finding.title} "
                f"(severity {finding.severity}, confidence {finding.confidence}){status}"
            )
            if finding.evidence_note:
                lines.append(f"    evidence: {finding.evidence_note}")
            if finding.consequence:
                lines.append(f"    consequence: {finding.consequence}")
    else:
        # Stated positively. An empty list is the answer to "why was X not flagged",
        # and phrasing it as an absence of data would invite the model to fill it.
        scope = ", ".join(facts.models_mentioned) or "the models asked about"
        lines.append(f"- No findings were recorded for {scope} in this review.")

    if facts.deltas:
        lines += ["", "## What changed when both revisions were built"]
        for delta in facts.deltas:
            if delta.build_error:
                lines.append(f"- {delta.model_name}: build failed")
                continue
            piece = f"- {delta.model_name}: rows {delta.rows_before} -> {delta.rows_after}"
            for column, pair in (delta.sum_deltas or {}).items():
                if isinstance(pair, list) and len(pair) == 2 and pair[0] != pair[1]:
                    piece += f"; sum({column}) {pair[0]:,.2f} -> {pair[1]:,.2f}"
            lines.append(piece)

    if facts.grains:
        lines += ["", "## Grain of each model, and how it was established"]
        for grain in facts.grains:
            columns = ", ".join(grain.columns or []) or "could not be established"
            piece = f"- {grain.model_name}: ({columns}) via {grain.source}"
            if grain.rows_per_key is not None:
                piece += f", measured at {grain.rows_per_key:.2f} rows per key"
            lines.append(piece)

    return "\n".join(lines)


def answer_question(
    question: str,
    facts: RetrievedFacts,
    *,
    provider: Provider,
    settings: Settings,
) -> Answer:
    """Answer from the stored facts, or refuse."""
    from themis.review.selfcheck import quote_is_grounded

    context = render_facts(facts)
    prompt = f"{context}\n\n## The question\n{question}"

    try:
        response = provider.complete(
            system=SYSTEM,
            prompt=prompt,
            schema=ANSWER_SCHEMA,
            model=settings.llm_supervisor_model,
        )
    except LLMError as exc:
        return Answer(
            text="",
            grounded=False,
            refusal_reason=f"the model could not be reached: {exc}",
        )

    payload = response.payload
    text = str(payload.get("answer", "")).strip()
    quote = str(payload.get("evidence_quote", "")).strip()

    if not payload.get("can_answer", False):
        return Answer(
            text=text,
            grounded=False,
            refusal_reason=text or "the stored review does not contain enough to answer",
        )

    # The same verification the specialists get. An answer citing something absent from
    # the facts is the failure this whole lane exists to prevent.
    if quote and not quote_is_grounded(quote, context):
        log.warning("ask.ungrounded", quote=quote[:120])
        return Answer(
            text="",
            grounded=False,
            refusal_reason=(
                "the answer cited evidence that is not in the stored review, so it was "
                "discarded rather than shown"
            ),
        )

    return Answer(text=text, grounded=True, evidence_quote=quote)
