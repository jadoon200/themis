"""Keeping what each model call was shown, so a tuning set can exist at all.

Nothing here feeds back into a review. The point is narrower and comes first: until
the context pack is stored, no training set can be assembled even in principle, and
"why did it answer that" is unanswerable the moment the process exits.

What is worth protecting is the join. A human rules on a finding days after the call
that judged it, so the two are tied by the fingerprint rather than by a foreign key,
and a rejected answer is kept rather than dropped — it is the clearest label there is
for what this lane must not produce.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from themis.db.base import Base
from themis.db.models import Finding as FindingRow
from themis.db.models import ModelCallRow, RunStatus, utcnow
from themis.db.store import enqueue_run, export_calls, finding_fingerprint, save_result
from themis.models import Confidence, Evidence, Finding, ModelCall, Severity
from themis.pipeline import ReviewResult
from themis.review.supervisor import ReviewSummary


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path}/calls.db", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _finding(rule_id: str = "F1001") -> Finding:
    return Finding(
        rule_id=rule_id,
        family=rule_id[:2],
        title="join may fan out",
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        evidence=Evidence(model_name="fct_revenue", note="join key is not proven unique"),
        consequence="revenue could double",
    )


def _saved(session: Session, calls: list[ModelCall], *, project: str = "demo") -> str:
    run = enqueue_run(session, project=project, base_ref="main", head_ref="HEAD")
    run.status = RunStatus.RUNNING
    result = ReviewResult(
        findings=[c.finding for c in calls if c.finding],
        llm=ReviewSummary(calls=calls),
    )
    save_result(session, run, result)
    session.flush()
    return run.run_key


def test_a_call_is_stored_with_what_it_was_shown(session: Session) -> None:
    finding = _finding()
    _saved(
        session,
        [
            ModelCall(
                seat="grain",
                model="qwen3:8b",
                context="## The finding\njoin key is not proven unique",
                system="You judge grain findings.",
                response={"verdict": "confirm", "evidence_quote": "not proven unique"},
                finding=finding,
            )
        ],
    )
    row = session.execute(select(ModelCallRow)).scalars().one()
    assert row.seat == "grain"
    assert "join key is not proven unique" in row.context
    assert row.response["verdict"] == "confirm"
    assert row.fingerprint == finding_fingerprint(finding, project="demo")


def test_a_rejected_answer_is_kept_with_its_reason(session: Session) -> None:
    """The clearest label available for what this lane must not produce."""
    _saved(
        session,
        [
            ModelCall(
                seat="grain",
                model="qwen3:8b",
                context="ctx",
                system="sys",
                response={"verdict": "refute"},
                accepted=False,
                rejected_reason="evidence quote does not appear in the context it was given",
                finding=_finding(),
            )
        ],
    )
    row = session.execute(select(ModelCallRow)).scalars().one()
    assert row.accepted is False
    assert "does not appear" in (row.rejected_reason or "")


def test_a_call_with_no_finding_behind_it_is_still_kept(session: Session) -> None:
    """The intent pass judges the change as a whole, so it has no fingerprint."""
    _saved(
        session,
        [ModelCall(seat="intent", model="qwen3:8b", context="ctx", system="sys")],
    )
    row = session.execute(select(ModelCallRow)).scalars().one()
    assert row.seat == "intent"
    assert row.fingerprint is None


def test_a_review_with_no_model_layer_stores_nothing(session: Session) -> None:
    run = enqueue_run(session, project="demo", base_ref="main", head_ref="HEAD")
    save_result(session, run, ReviewResult(findings=[_finding()]))
    assert session.execute(select(ModelCallRow)).scalars().all() == []


# --- the export ----------------------------------------------------------------


def test_the_export_joins_a_later_judgement_to_the_call(session: Session) -> None:
    """The judgement lands days after the call. Fingerprints are what tie them."""
    finding = _finding()
    _saved(
        session,
        [
            ModelCall(
                seat="grain",
                model="qwen3:8b",
                context="ctx",
                system="sys",
                response={"verdict": "confirm"},
                finding=finding,
            )
        ],
    )
    stored = session.execute(select(FindingRow)).scalars().one()
    stored.disposition = "dismissed"
    stored.disposition_note = "the key is unique by contract upstream"
    stored.disposition_at = utcnow()
    session.flush()

    rows = export_calls(session)
    assert len(rows) == 1
    assert rows[0]["human_disposition"] == "dismissed"
    assert rows[0]["human_note"] == "the key is unique by contract upstream"
    assert rows[0]["seat"] == "grain"


def test_the_export_can_keep_only_what_a_human_ruled_on(session: Session) -> None:
    finding = _finding()
    other = _finding("F2001")
    _saved(
        session,
        [
            ModelCall(seat="grain", model="m", context="c", system="s", finding=finding),
            ModelCall(seat="filters", model="m", context="c", system="s", finding=other),
        ],
    )
    first = session.execute(select(FindingRow).where(FindingRow.rule_id == "F1001")).scalars().one()
    first.disposition = "accepted"
    first.disposition_at = utcnow()
    session.flush()

    assert len(export_calls(session)) == 2
    judged = export_calls(session, judged_only=True)
    assert [row["rule_id"] for row in judged] == ["F1001"]


def test_the_export_is_scoped_to_a_project(session: Session) -> None:
    _saved(session, [ModelCall(seat="grain", model="m", context="c", system="s")], project="a")
    _saved(session, [ModelCall(seat="grain", model="m", context="c", system="s")], project="b")
    assert len(export_calls(session, project="a")) == 1
    assert len(export_calls(session)) == 2


def test_an_empty_store_exports_nothing(session: Session) -> None:
    assert export_calls(session) == []
