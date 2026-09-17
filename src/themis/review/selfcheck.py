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

# Models quote selectively as well as elliptically: they join lines of context into one
# sentence, skipping the lines between. Treating the punctuation they join with as a
# possible split point and verifying each piece contiguously accepts that, while still
# requiring every substantial phrase to be genuinely present and in order.
#
# The colon earns its place separately from the comma. A pack is markdown, so a
# specialist quoting a section and the block beneath it writes
# "The SQL of `x`, the model being joined to: select ...", joining a heading to its
# own code fence. Every word is genuinely there; only the fence sits between them.
_JOIN = re.compile(r"\s*[,:]\s*")
_MIN_SEGMENT_CHARS = 12


def _normalise(text: str) -> str:
    """Collapse whitespace so a reflowed quote still matches."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _words(text: str) -> list[str]:
    """Lowercased word tokens, with punctuation discarded.

    Comparison happens on words rather than characters because models re-punctuate
    when they quote: joining three lines of context into one sentence with commas is
    the commonest form, and rejecting it discards a correct answer. What must survive
    is that the words are really there, in order — a fabricated claim fails that
    whether or not it is punctuated tidily.
    """
    # Alphanumerics and underscores only. Including the dot glued trailing periods to
    # words, so "month" and "month..." stopped matching each other.
    return re.findall(r"[a-z0-9_]+", text.lower())


def _contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i] == first and haystack[i : i + len(needle)] == needle:
            return True
    return False


# How many context words may separate a short piece of a quote from its neighbour. None:
# punctuation is already stripped, so a real quote has no gap, and a gap of even one word
# let "materialization: incremental" borrow its value across "view" from the next line.
_SHORT_PIECE_WINDOW = 0


def _occurrences(haystack: list[str], needle: list[str]) -> list[tuple[int, int]]:
    """Every (start, end) at which ``needle`` appears contiguously in ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return []
    return [
        (i, i + len(needle))
        for i in range(len(haystack) - len(needle) + 1)
        if haystack[i : i + len(needle)] == needle
    ]


def quote_is_grounded(quote: str, context: str) -> bool:
    """Whether every part of a quote appears in the context.

    Shared by the specialists, the follow-up lane and the agent, so there is exactly one
    definition of what counts as grounded — two implementations would drift, and the weaker
    one would decide.

    A quote is split where models join lines — elisions, commas, colons — and each piece
    checked on its own. Pieces shorter than a phrase used to be skipped as insubstantial,
    and that was a hole exactly where facts live: "materialization: incremental" passed
    against a context saying "materialization: view", because "incremental" was never
    checked. Now every piece must be present, and a short one must sit beside its
    neighbour in the context — a value is only grounded next to the name it belongs to.
    At least one piece must still be phrase-length: a quote of two words proves nothing.
    """
    context_words = _words(context)
    pieces: list[str] = []
    for part in _ELISION.split(quote):
        pieces.extend(piece.strip() for piece in _JOIN.split(part))
    pieces = [piece for piece in pieces if _words(piece)]
    if not any(len(_normalise(piece)) >= _MIN_SEGMENT_CHARS for piece in pieces):
        return False

    spans = [_occurrences(context_words, _words(piece)) for piece in pieces]
    if not all(spans):
        return False
    for index, piece in enumerate(pieces):
        if len(_normalise(piece)) >= _MIN_SEGMENT_CHARS:
            continue
        before = spans[index - 1] if index > 0 else []
        after = spans[index + 1] if index + 1 < len(pieces) else []
        beside = any(
            any(0 <= start - end_before <= _SHORT_PIECE_WINDOW for _, end_before in before)
            or any(0 <= start_after - end <= _SHORT_PIECE_WINDOW for start_after, _ in after)
            for start, end in spans[index]
        )
        if not beside:
            return False
    return True


_LOG_QUOTE_CHARS = 600


def _for_log(quote: str) -> str:
    """A quote for the log, with any truncation stated rather than silent."""
    if len(quote) <= _LOG_QUOTE_CHARS:
        return quote
    return f"{quote[:_LOG_QUOTE_CHARS]}… [truncated for the log, {len(quote)} chars]"


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

    if not quote_is_grounded(quote, pack.evidence_text):
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
        # The quote is logged because a rejection nobody can inspect is a rejection
        # nobody can fix — it says a specialist misbehaved without saying how.
        log.warning(
            "selfcheck.rejected",
            specialist=adjudication.specialist,
            # Which verdict was thrown away matters more than that one was. Discarding
            # a confirmation costs nothing — the deterministic finding stands either
            # way — while discarding a refutation is the layer's only useful output
            # being lost to punctuation, and the two are indistinguishable without it.
            verdict=adjudication.verdict,
            reason=result.reason,
            # Logged nearly in full. At 160 characters a rejected quote was cut
            # mid-identifier, which reads exactly like the model having fabricated a
            # truncated name — the log meant to make rejections diagnosable was
            # manufacturing a second, imaginary fault to diagnose.
            quote=_for_log(adjudication.evidence_quote),
        )
        return None, result.reason
    return adjudication, None
