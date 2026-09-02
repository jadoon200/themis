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


def _normalise(text: str) -> str:
    """Collapse whitespace so a reflowed quote still matches."""
    return re.sub(r"\s+", " ", text).strip().lower()


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

    if _normalise(quote) not in _normalise(pack.text):
        return CheckResult(
            ok=False, reason="evidence quote does not appear in the context it was given"
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
