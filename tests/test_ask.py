"""The follow-up lane.

Almost every test here is about refusing. A reviewer who asks whether the FX table was
checked for duplicates and receives a confident invented "yes" is worse off than one
who received nothing, so the property under test is that the system declines rather
than fills a gap.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from themis.ask.answer import answer_question, render_facts
from themis.ask.retrieval import gather, latest_run, run_by_key
from themis.config import Settings
from themis.db.base import Base
from themis.db.models import Finding, GrainRecord, ModelDelta, ReviewRun, RunStatus
from themis.db.store import enqueue_run
from themis.llm.provider import LLMError, Response, Usage


class FakeProvider:
    def __init__(self, payload: dict[str, Any] | None = None, *, fail: bool = False):
        self._payload = payload or {}
        self._fail = fail
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str, schema: dict, model: str) -> Response:
        self.prompts.append(prompt)
        if self._fail:
            raise LLMError("unavailable")
        return Response(payload=self._payload, usage=Usage(calls=1))


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path}/ask.db", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def run(session: Session) -> ReviewRun:
    stored = enqueue_run(session, project="demo", base_ref="main", head_ref="HEAD")
    stored.status = RunStatus.SUCCEEDED
    stored.executed = True
    stored.models_reviewed = 2
    session.add(
        Finding(
            run_id=stored.id,
            fingerprint="fp1",
            rule_id="F1001",
            family="F1",
            title="New join to stg_fx_rates may fan out",
            severity="high",
            confidence="measured",
            verdict="breaking",
            model_name="fct_revenue",
            evidence_note="stg_fx_rates is not unique on currency_code",
            consequence="Amounts are tripled.",
        )
    )
    session.add(
        ModelDelta(
            run_id=stored.id,
            model_name="fct_revenue",
            rows_before=15,
            rows_after=45,
            sum_deltas={"amount_usd": [100.0, 300.0]},
            material=True,
        )
    )
    session.add(
        GrainRecord(
            run_id=stored.id,
            model_name="fct_revenue",
            columns=["entry_id"],
            source="measured",
            rows_per_key=3.0,
            note="45 rows, 15 distinct",
        )
    )
    session.flush()
    return stored


# --- retrieval ----------------------------------------------------------------


def test_latest_run_ignores_unfinished_runs(session: Session, run: ReviewRun) -> None:
    """A queued run has nothing to say yet, and answering from one would be answering
    from an empty review."""
    enqueue_run(session, project="demo", base_ref="main", head_ref="HEAD")
    assert latest_run(session) is not None
    assert latest_run(session).run_key == run.run_key


def test_run_lookup_by_key(session: Session, run: ReviewRun) -> None:
    assert run_by_key(session, run.run_key) is not None
    assert run_by_key(session, "nope") is None


def test_a_named_model_is_resolved(session: Session, run: ReviewRun) -> None:
    facts = gather(session, run, "what happened to fct_revenue?")
    assert facts.models_mentioned == ("fct_revenue",)
    assert facts.findings


def test_a_named_rule_is_resolved(session: Session, run: ReviewRun) -> None:
    facts = gather(session, run, "explain F1001")
    assert facts.rules_mentioned == ("F1001",)


def test_an_unknown_model_retrieves_nothing(session: Session, run: ReviewRun) -> None:
    """The basis for a refusal: no facts to answer from."""
    facts = gather(session, run, "what about stg_customer_pii?")
    assert facts.models_mentioned == ()
    assert not facts.findings


def test_absence_questions_are_recognised(session: Session, run: ReviewRun) -> None:
    assert gather(session, run, "why didn't you flag dim_accounts?").asked_about_absence
    assert not gather(session, run, "what changed in fct_revenue?").asked_about_absence


def test_longer_model_names_win(session: Session, run: ReviewRun) -> None:
    """fct_revenue_incremental must not be shadowed by fct_revenue."""
    session.add(
        GrainRecord(
            run_id=run.id,
            model_name="fct_revenue_incremental",
            columns=["entry_id"],
            source="structural",
        )
    )
    session.flush()
    facts = gather(session, run, "tell me about fct_revenue_incremental")
    assert facts.models_mentioned[0] == "fct_revenue_incremental"


# --- rendering ----------------------------------------------------------------


def test_absence_is_stated_positively(session: Session, run: ReviewRun) -> None:
    """An empty list is the answer to "why was X not flagged". Phrasing it as missing
    data would invite the model to fill the gap."""
    facts = gather(session, run, "why was dim_accounts not flagged?")
    facts.findings = []
    facts.models_mentioned = ("dim_accounts",)
    assert "No findings were recorded for dim_accounts" in render_facts(facts)


def test_measured_numbers_reach_the_model(session: Session, run: ReviewRun) -> None:
    text = render_facts(gather(session, run, "what changed in fct_revenue?"))
    assert "15" in text and "45" in text
    assert "3.00 rows per key" in text


# --- answering and refusing ---------------------------------------------------


def _answer(payload: dict[str, Any] | None, session: Session, run: ReviewRun, question: str):
    return answer_question(
        question,
        gather(session, run, question),
        provider=FakeProvider(payload),
        settings=Settings(),
    )


def test_a_grounded_answer_is_returned(session: Session, run: ReviewRun) -> None:
    result = _answer(
        {
            "can_answer": True,
            "answer": "Rows went from 15 to 45.",
            "evidence_quote": "stg_fx_rates is not unique on currency_code",
        },
        session,
        run,
        "what happened to fct_revenue?",
    )
    assert result.grounded
    assert "15 to 45" in result.text


def test_can_answer_false_becomes_a_refusal(session: Session, run: ReviewRun) -> None:
    result = _answer(
        {
            "can_answer": False,
            "answer": "The review says nothing about encryption.",
            "evidence_quote": "",
        },
        session,
        run,
        "is customer email encrypted at rest?",
    )
    assert not result.grounded
    assert result.refusal_reason


def test_a_fabricated_citation_is_discarded(session: Session, run: ReviewRun) -> None:
    """The failure this lane exists to prevent: a confident answer citing something
    the review never recorded."""
    result = _answer(
        {
            "can_answer": True,
            "answer": "Yes, a uniqueness test confirmed the key is safe.",
            "evidence_quote": "a uniqueness test on rate_date passed successfully",
        },
        session,
        run,
        "was the fx table checked?",
    )
    assert not result.grounded
    assert "not in the stored review" in (result.refusal_reason or "")
    assert result.text == ""


def test_an_unreachable_model_refuses_rather_than_guessing(
    session: Session, run: ReviewRun
) -> None:
    result = answer_question(
        "what happened?",
        gather(session, run, "what happened?"),
        provider=FakeProvider(fail=True),
        settings=Settings(),
    )
    assert not result.grounded
    assert "could not be reached" in (result.refusal_reason or "")


def test_the_model_never_receives_a_database_handle(session: Session, run: ReviewRun) -> None:
    """It is given rendered facts, not the means to fetch more."""
    provider = FakeProvider({"can_answer": True, "answer": "ok", "evidence_quote": ""})
    answer_question(
        "what changed?",
        gather(session, run, "what changed?"),
        provider=provider,
        settings=Settings(),
    )
    prompt = provider.prompts[0]
    assert "sqlite" not in prompt.lower()
    assert "select " not in prompt.lower()
