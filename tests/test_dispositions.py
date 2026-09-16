"""What a reviewer decided, acting on the next review.

The loop this closes: a finding is raised, someone rules on it, and the next run that
raises the same finding knows. Two separate uses of the same fact, deliberately kept
apart — the ranking demotes what people keep dismissing, and the specialist is shown
what they said. Neither deletes anything, and both are visible in the report.

The failure to guard against is the one the roadmap names: a tool that learns to go
quiet. So a measurement is exempt from the demotion, one person's single dismissal
moves nothing, and the penalty is bounded well short of removal.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from themis.db.base import Base
from themis.db.models import Finding as FindingRow
from themis.db.models import ReviewRun, RunStatus, utcnow
from themis.db.store import enqueue_run, finding_fingerprint, history_for
from themis.models import (
    Confidence,
    Evidence,
    Finding,
    FindingHistory,
    PriorJudgement,
    Severity,
)
from themis.pipeline import attach_history
from themis.triage.rubric import triage


def _finding(
    rule_id: str = "F2001",
    *,
    model: str = "fct_revenue",
    confidence: Confidence = Confidence.LIKELY,
    history: FindingHistory | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        family=rule_id[:2],
        title=f"{rule_id} fired",
        severity=Severity.HIGH,
        confidence=confidence,
        evidence=Evidence(model_name=model, note="a predicate changed"),
        consequence="a total moves",
        history=history,
    )


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path}/history.db", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _run(session: Session, project: str = "demo") -> ReviewRun:
    run = enqueue_run(session, project=project, base_ref="main", head_ref="HEAD")
    run.status = RunStatus.SUCCEEDED
    session.flush()
    return run


def _store(
    session: Session,
    finding: Finding,
    *,
    project: str = "demo",
    disposition: str | None = None,
    note: str | None = None,
    model_name: str | None = None,
) -> FindingRow:
    run = _run(session, project)
    row = FindingRow(
        run_id=run.id,
        fingerprint=finding_fingerprint(finding, project=project),
        rule_id=finding.rule_id,
        family=finding.family,
        title=finding.title,
        severity=str(finding.severity),
        confidence=str(finding.confidence),
        verdict=str(finding.verdict),
        model_name=model_name or finding.evidence.model_name,
        consequence=finding.consequence,
        evidence_note=finding.evidence.note,
        blast_radius=[],
        disposition=disposition,
        disposition_note=note,
        disposition_at=utcnow() if disposition else None,
    )
    session.add(row)
    session.flush()
    return row


# --- the ranking ---------------------------------------------------------------


def test_a_repeatedly_dismissed_finding_ranks_below_an_identical_one() -> None:
    dismissed = _finding(
        "F2001", model="a", history=FindingHistory(occurrences=4, dismissed=3, accepted=1)
    )
    fresh = _finding("F2001", model="b")
    ranked = triage([dismissed, fresh])
    assert [t.finding.evidence.model_name for t in ranked] == ["b", "a"]
    assert ranked[1].score < ranked[0].score


def test_the_demotion_says_so_in_words() -> None:
    ranked = triage(
        [_finding(history=FindingHistory(occurrences=4, dismissed=3, accepted=1))]
    )
    assert "dismissed by a reviewer in 3 of 4" in ranked[0].reason


def test_one_dismissal_is_an_opinion_not_evidence() -> None:
    """A single judgement about a single change must not re-rank the rule."""
    once = triage([_finding(history=FindingHistory(occurrences=1, dismissed=1))])[0]
    never = triage([_finding()])[0]
    assert once.score == never.score


def test_a_minority_of_dismissals_does_not_demote() -> None:
    mostly_kept = triage(
        [_finding(history=FindingHistory(occurrences=5, dismissed=1, accepted=3))]
    )[0]
    assert mostly_kept.score == triage([_finding()])[0].score


def test_a_measured_finding_is_never_demoted_by_its_history() -> None:
    """The rows moved. That someone accepted the last move is not evidence that this
    one is fine — and a tool that learns to go quiet on measurements is worse than one
    that never learned anything."""
    history = FindingHistory(occurrences=4, dismissed=4)
    measured = triage([_finding(confidence=Confidence.MEASURED, history=history)])[0]
    plain = triage([_finding(confidence=Confidence.MEASURED)])[0]
    assert measured.score == plain.score
    assert "the ranking is unchanged" in measured.reason


def test_a_demoted_finding_is_still_in_the_report() -> None:
    history = FindingHistory(occurrences=9, dismissed=9)
    ranked = triage([_finding(history=history)])
    assert len(ranked) == 1
    assert ranked[0].score >= 1.0


def test_a_recurrence_nobody_judged_is_only_noted() -> None:
    seen = triage([_finding(history=FindingHistory(occurrences=3))])[0]
    assert seen.score == triage([_finding()])[0].score
    assert "raised in 3 earlier run(s)" in seen.reason


# --- reading the history back ---------------------------------------------------


def test_history_counts_only_the_same_fingerprint(session: Session) -> None:
    finding = _finding()
    _store(session, finding, disposition="dismissed", note="the join is 1:1 by contract")
    _store(session, finding, disposition="dismissed")
    _store(session, _finding("F1001"), disposition="accepted")

    history = history_for(session, [finding], project="demo")[0]
    assert history is not None
    assert history.occurrences == 2
    assert history.dismissed == 2
    assert history.dismissal_rate == 1.0
    assert history.last_note == "the join is 1:1 by contract"


def test_a_finding_nobody_has_seen_has_no_history(session: Session) -> None:
    assert history_for(session, [_finding()], project="demo") == [None]


def test_history_is_scoped_to_the_project(session: Session) -> None:
    """The same rule on the same model name in another project is another finding."""
    finding = _finding()
    _store(session, finding, project="other", disposition="dismissed")
    _store(session, finding, project="other", disposition="dismissed")
    history = history_for(session, [finding], project="demo")[0]
    assert history is None or history.dismissed == 0


def test_past_judgements_prefer_the_same_model(session: Session) -> None:
    finding = _finding()
    _store(session, finding, model_name="dim_other", disposition="dismissed", note="elsewhere")
    _store(session, finding, disposition="accepted", note="on this model")

    history = history_for(session, [finding], project="demo", examples=2)[0]
    assert history is not None
    assert [e.same_model for e in history.examples] == [True, False]
    assert history.examples[0].note == "on this model"


def test_judgements_are_not_retrieved_when_the_lever_is_off(session: Session) -> None:
    finding = _finding()
    _store(session, finding, disposition="dismissed", note="n")
    history = history_for(session, [finding], project="demo", examples=0)[0]
    assert history is not None
    assert history.examples == ()


def test_only_dispositioned_findings_are_offered_as_precedent(session: Session) -> None:
    finding = _finding()
    _store(session, finding)
    history = history_for(session, [finding], project="demo")[0]
    assert history is not None
    assert history.examples == ()


# --- wiring ---------------------------------------------------------------------


def test_attach_history_hangs_each_history_on_its_own_finding() -> None:
    findings = [_finding("F1001"), _finding("F2001")]
    histories = [None, FindingHistory(occurrences=2, dismissed=2)]
    out = attach_history(findings, lambda _: histories)
    assert out[0].history is None
    assert out[1].history is not None and out[1].history.dismissed == 2


def test_a_store_that_cannot_be_read_does_not_fail_the_review() -> None:
    def broken(_: list[Finding]) -> list[FindingHistory | None]:
        raise RuntimeError("database is down")

    findings = [_finding()]
    assert attach_history(findings, broken) == findings


def test_no_lookup_means_no_history() -> None:
    findings = [_finding()]
    assert attach_history(findings, None)[0].history is None


def test_a_judgement_carries_what_the_reviewer_wrote() -> None:
    judgement = PriorJudgement(
        rule_id="F2001",
        model_name="fct_revenue",
        disposition="dismissed",
        title="Filter added",
        note="intended: the desk stopped booking these",
    )
    assert judgement.note is not None and "desk" in judgement.note


# --- what the reviewer sees -----------------------------------------------------


def test_the_report_says_a_finding_was_dismissed_before() -> None:
    from themis.report import markdown

    text = markdown.render(
        [_finding(history=FindingHistory(occurrences=4, dismissed=3, last_note="by design"))],
        skipped=[],
        models_reviewed=1,
        executed=False,
    )
    assert "Seen before:" in text
    assert "3 dismissed" in text
    assert "by design" in text


def test_a_recurrence_nobody_ruled_on_says_that() -> None:
    from themis.report import markdown

    text = markdown.render(
        [_finding(history=FindingHistory(occurrences=2))],
        skipped=[],
        models_reviewed=1,
        executed=False,
    )
    assert "never ruled on" in text


def test_the_json_carries_counts_and_not_the_note() -> None:
    import json as json_lib

    from themis.report import json_out

    payload = json_lib.loads(
        json_out.render(
            [_finding(history=FindingHistory(occurrences=4, dismissed=3, last_note="secret"))],
            skipped=[],
            grains={},
            models_reviewed=("fct_revenue",),
            executed=False,
        )
    )
    history = payload["findings"][0]["history"]
    assert history == {
        "occurrences": 4,
        "dismissed": 3,
        "accepted": 0,
        "fixed": 0,
        "deferred": 0,
    }
    assert "secret" not in json_lib.dumps(payload)
