"""Explaining a measured change that no rule accounts for.

This is the one job in the system that genuinely needs a language model, and the
division of labour is worth stating precisely: **measurement decides whether something
changed; the model only proposes why.**

X0001 fires when building both revisions produced different results and no rule
accounts for it. The number is certain and the cause is unknown — which is exactly the
shape of question a model is good at and a rule cannot be written for, because if the
cause were anticipable there would already be a rule.

The output is explicitly a hypothesis. It is never allowed to change severity, suppress
the finding, or claim the change is safe: the measurement stands regardless of whether
the explanation is right.
"""

from __future__ import annotations

from typing import Any

from themis.config import Settings
from themis.llm.provider import LLMError, Provider, Usage
from themis.logging import get_logger
from themis.models import Finding
from themis.review.selfcheck import quote_is_grounded
from themis.snapshot import ProjectSnapshot

log = get_logger(__name__)

EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "confidence": {"type": "string", "enum": ["likely", "possible", "unclear"]},
        "evidence_quote": {"type": "string"},
    },
    "required": ["hypothesis", "confidence", "evidence_quote"],
}

SYSTEM = """A dbt model's results changed when both revisions were built, and no
automated check explains why. You are given the SQL diff and the measured change.

Propose what in the diff caused the measured difference.

Rules:
- The measurement is certain. Do not dispute it, and do not say the change is safe.
- Point at something specific in the diff. "The logic changed" is not an answer.
- If the diff does not explain the measurement, say so and set confidence to
  "unclear". That is a useful answer — it means the cause is upstream or in data.
- `evidence_quote` must be copied verbatim from what you were given.
- Two sentences at most."""


def _diff_excerpt(before: str | None, after: str | None, *, max_lines: int = 60) -> str:
    """A unified diff of the two compiled versions, bounded."""
    import difflib

    if before is None or after is None:
        return "(compiled SQL not available for both revisions)"
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2))
    changed = [line for line in lines if line.startswith(("+", "-", "@@"))]
    if len(changed) > max_lines:
        changed = [*changed[:max_lines], f"... {len(changed) - max_lines} more lines ..."]
    return "\n".join(changed) or "(no textual difference in the compiled SQL)"


def explain(
    finding: Finding,
    *,
    provider: Provider,
    settings: Settings,
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    usage: Usage,
) -> str | None:
    """Propose a cause for an unexplained measured change. None if it cannot."""
    delta = finding.execution_delta
    if delta is None:
        return None

    name = finding.evidence.model_name
    before_model = before.models.get(name)
    after_model = after.models.get(name)

    measured = [f"Model: {name}"]
    if delta.rows_before is not None and delta.rows_after is not None:
        measured.append(f"rows: {delta.rows_before:,} -> {delta.rows_after:,}")
    for column, (was, now) in sorted(delta.sum_deltas.items()):
        if was != now:
            shift = ((now - was) / was * 100) if was else 0.0
            measured.append(f"sum({column}): {was:,.2f} -> {now:,.2f} ({shift:+.1f}%)")

    context = "\n".join(
        [
            "## What was measured",
            *measured,
            "",
            "## What changed in the compiled SQL",
            "```diff",
            _diff_excerpt(
                before_model.analysable_sql if before_model else None,
                after_model.analysable_sql if after_model else None,
            ),
            "```",
        ]
    )

    try:
        response = provider.complete(
            system=SYSTEM,
            prompt=context,
            schema=EXPLAIN_SCHEMA,
            model=settings.llm_supervisor_model,
        )
    except LLMError as exc:
        log.warning("explain.failed", model=name, error=str(exc)[:200])
        return None

    usage.add(response.usage)
    hypothesis = str(response.payload.get("hypothesis", "")).strip()
    quote = str(response.payload.get("evidence_quote", "")).strip()
    confidence = str(response.payload.get("confidence", "unclear"))

    if not hypothesis:
        return None
    if quote and not quote_is_grounded(quote, context):
        log.warning("explain.ungrounded", model=name)
        return None

    # Labelled as a hypothesis in the text itself, so it can never be read as a
    # finding the tool stands behind.
    return f"Possible cause ({confidence}): {hypothesis}"
