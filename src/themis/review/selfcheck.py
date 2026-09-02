"""Verifying that a specialist's answer is grounded in what it was shown.

This is the direct answer to the failure Recce documented: an agent inventing DAG
lineage from semantic inference. Their fix was to force the model to show its raw
mappings before rendering prose. The equivalent here is cheaper — every answer must
quote the context verbatim, and an answer whose quote is not actually in the context is
discarded.

Discarded means the deterministic finding stands unchanged. The model is only ever
allowed to *adjust* a finding the rules produced; it can never be the reason a finding
is trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from themis.llm.context_pack import ContextPack
from themis.logging import get_logger
from themis.review.specialists import Adjudication

log = get_logger(__name__)

# A quote shorter than this proves nothing — "the" appears in every context.
_MIN_QUOTE_CHARS = 12

# Models elide the middle of a long quote rather than reproducing it in full. That is
# not fabrication, so the check splits on the elision and requires every substantial
# segment to appear — which keeps the property that every part was actually shown.
_ELISION = re.compile(r"\s*(?:\[\.\.\.\]|\.\.\.|…|\[…\])\s*")
_MIN_SEGMENT_CHARS = 12


def _normalise(text: str) -> str:
    """Collapse whitespace so a reflowed quote still matches."""
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_is_grounded(quote: str, context: str) -> bool:
    """Whether every substantial part of a quote appears in the context.

    Shared by the specialists and the follow-up lane so there is exactly one definition
    of what counts as grounded — two implementations would inevitably drift, and the
    weaker one would decide.
    """
    normalised_context = _normalise(context)
    segments = [
        segment
        for segment in (_normalise(part) for part in _ELISION.split(quote))
        if len(segment) >= _MIN_SEGMENT_CHARS
    ]
    if not segments:
        return False
    return all(segment in normalised_context for segment in segments)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""


def check(adjudication: Adjudication, pack: ContextPack) -> CheckResult:
    """Whether an adjudication may be trusted to modify a finding."""
    quote = adjudication.evidence_quote.strip()

    if adjudication.verdict == "uncertain":
        # Abstaining needs no evidence, and demanding it would push the model towards
        # inventing a quote in order to abstain.
        return CheckResult(ok=True)

    if len(quote) < _MIN_QUOTE_CHARS:
        return CheckResult(
            ok=False, reason=f"evidence quote too short to prove anything ({len(quote)} chars)"
        )

    if not quote_is_grounded(quote, pack.text):
        return CheckResult(
            ok=False,
            reason="evidence quote does not appear in the context it was given",
        )

    if not adjudication.rationale.strip():
        return CheckResult(ok=False, reason="no rationale given")

    return CheckResult(ok=True)


def verified(
    adjudication: Adjudication | None, pack: ContextPack
) -> tuple[Adjudication | None, str | None]:
    """Return the adjudication if it passes, otherwise None and the reason."""
    if adjudication is None:
        return None, "the model did not answer"
    result = check(adjudication, pack)
    if not result.ok:
        log.warning(
            "selfcheck.rejected",
            specialist=adjudication.specialist,
            reason=result.reason,
        )
        return None, result.reason
    return adjudication, None
