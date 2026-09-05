"""Proposed fixes — and the two checks that make them safe to print.

A rule can name a defect and give general advice. It cannot write the corrected ON
clause, because that depends on which columns the key really needs. This is the one job
where a model writing text is the right tool and being wrong is cheap: a reviewer reads
a diff. What it must never do is print something that is not SQL.
"""

from __future__ import annotations

from typing import Any

from themis.config import Settings
from themis.llm.context_pack import ContextPack
from themis.llm.provider import Response, Usage
from themis.models import Confidence, Evidence, Finding, Severity
from themis.review.fix import propose


class _Provider:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls = 0

    def complete(self, **_: Any) -> Response:
        self.calls += 1
        return Response(payload=self._payload, usage=Usage(calls=1))


def _finding() -> Finding:
    return Finding(
        rule_id="F1001",
        family="F1",
        title="join may fan out",
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        evidence=Evidence(
            model_name="fct",
            sql_after="inner join rates on e.ccy = rates.ccy",
        ),
        consequence="amounts duplicate",
    )


def _pack() -> ContextPack:
    return ContextPack(text="## context\n\nsome sql")


def _propose(payload: dict[str, Any]) -> tuple[str | None, _Provider]:
    provider = _Provider(payload)
    fixed = propose(_finding(), _pack(), provider=provider, settings=Settings(), usage=Usage())
    return fixed, provider


def test_a_valid_fix_is_returned() -> None:
    fixed, _ = _propose(
        {
            "can_fix": True,
            "fixed_sql": "inner join rates on e.ccy = rates.ccy and e.period = rates.period",
            "explanation": "carry the period predicate",
        }
    )
    assert fixed is not None and "period" in fixed


def test_a_fix_that_does_not_parse_is_discarded() -> None:
    """The one failure this feature cannot afford: printing something that is not SQL."""
    fixed, _ = _propose(
        {"can_fix": True, "fixed_sql": "join rates ON WHERE ((", "explanation": "x"}
    )
    assert fixed is None


def test_a_fix_identical_to_the_original_is_discarded() -> None:
    """Echoing the input back is not a proposal, and printing it teaches people to skip them."""
    fixed, _ = _propose(
        {
            "can_fix": True,
            "fixed_sql": "INNER JOIN rates ON e.ccy = rates.ccy",
            "explanation": "unchanged",
        }
    )
    assert fixed is None


def test_declining_is_respected() -> None:
    """A guess dressed as a fix is worse than the generic advice already attached."""
    fixed, _ = _propose(
        {"can_fix": False, "fixed_sql": "", "explanation": "need to know the real key"}
    )
    assert fixed is None


def test_an_empty_answer_is_discarded() -> None:
    fixed, _ = _propose({"can_fix": True, "fixed_sql": "   ", "explanation": ""})
    assert fixed is None


def test_a_fragment_is_checked_in_the_shape_it_was_given() -> None:
    """A join clause does not parse alone, and neither does a predicate.

    Checking the proposal in isolation would reject every correct answer, because a
    finding's evidence is almost always a fragment. It is checked in whatever context
    makes the original parse.
    """
    fixed, _ = _propose(
        {
            "can_fix": True,
            "fixed_sql": "inner join rates on e.ccy = rates.ccy and e.period = rates.period",
            "explanation": "carry the period predicate",
        }
    )
    assert fixed is not None


def test_a_fragment_that_is_broken_in_that_shape_is_still_rejected() -> None:
    """The guard must not have been bought by accepting anything."""
    fixed, _ = _propose(
        {"can_fix": True, "fixed_sql": "inner join rates on and and", "explanation": "x"}
    )
    assert fixed is None
