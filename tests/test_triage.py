"""Stage 4 — the half that pays for recall-first flagging.

Rules over-flag deliberately. That bargain is only honest if something ranks the
result, and until this stage existed the reviewer got the over-flagging and none of
the suppression: a change with one real problem arrived as seven findings with the one
that mattered last.

Demotion, never deletion. A finding removed here is one the reviewer could not have
asked about afterwards, and "why didn't you flag X" is a question the follow-up lane
is supposed to be able to answer.
"""

from __future__ import annotations

from themis.models import Confidence, Evidence, Finding, Severity
from themis.triage.rubric import triage


def _finding(
    rule_id: str,
    *,
    model: str = "fct",
    severity: Severity = Severity.HIGH,
    confidence: Confidence = Confidence.LIKELY,
    blast: tuple[str, ...] = (),
) -> Finding:
    return Finding(
        rule_id=rule_id,
        family=rule_id[:2],
        title=f"{rule_id} fired",
        severity=severity,
        confidence=confidence,
        evidence=Evidence(model_name=model),
        consequence="something",
        blast_radius=blast,
    )


def test_a_general_finding_is_demoted_when_a_specific_one_names_the_same_edit() -> None:
    """`F2001` says a predicate changed; `F5001` says which, and why it matters."""
    result = triage([_finding("F2001"), _finding("F5001")])
    by_rule = {t.finding.rule_id: t for t in result}
    assert by_rule["F2001"].demoted
    assert by_rule["F2001"].subsumed_by == "F5001"
    assert not by_rule["F5001"].demoted


def test_a_demoted_finding_is_kept_not_dropped() -> None:
    """Recall-first is paid for in ranking, never in silence."""
    result = triage([_finding("F2001"), _finding("F5001")])
    assert len(result) == 2


def test_demotion_needs_the_specific_finding_on_the_same_model() -> None:
    """A guard removed in one model does not explain a predicate change in another."""
    result = triage([_finding("F2001", model="a"), _finding("F5001", model="b")])
    assert not any(t.demoted for t in result)


def test_a_general_finding_alone_is_not_demoted() -> None:
    """With nothing more specific in the report, it is the best statement available."""
    result = triage([_finding("F2001")])
    assert not result[0].demoted


def test_measured_evidence_outranks_an_unsubstantiated_critical() -> None:
    """A critical nobody can substantiate should not sit above a demonstrated high."""
    result = triage(
        [
            _finding("F3001", severity=Severity.CRITICAL, confidence=Confidence.POSSIBLE),
            _finding("F1001", severity=Severity.HIGH, confidence=Confidence.MEASURED),
        ]
    )
    assert result[0].finding.rule_id == "F1001"


def test_reach_raises_the_score_with_diminishing_returns() -> None:
    """One downstream model to four is the interesting step; thirty to forty is not."""
    narrow = triage([_finding("F1001", blast=("a",))])[0].score
    wide = triage([_finding("F1001", blast=tuple(f"m{i}" for i in range(6)))])[0].score
    wider = triage([_finding("F1001", blast=tuple(f"m{i}" for i in range(25)))])[0].score
    assert narrow < wide < wider
    assert (wider - wide) < (wide - narrow)


def test_landing_in_a_governed_model_raises_the_score() -> None:
    plain = triage([_finding("F1001", blast=("mart",))])[0].score
    governed = triage([_finding("F1001", blast=("mart",))], governed_models=frozenset({"mart"}))[
        0
    ].score
    assert governed > plain


def test_the_score_says_what_produced_it() -> None:
    """An opaque number gating a merge is not a reviewable statement."""
    result = triage([_finding("F1001", blast=("mart",))], governed_models=frozenset({"mart"}))[0]
    assert "high" in result.reason
    assert "reaches 1 model(s)" in result.reason
    assert "governed" in result.reason
