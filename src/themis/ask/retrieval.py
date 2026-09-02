"""Pulling facts out of a stored review to answer a question about it.

Retrieval is deliberate rather than agentic. A tool-calling loop lets a small model
decide what to look up, and an 8B model is unreliable at that — it will call the wrong
tool, or none, and then answer anyway. Resolving the entities in the question first and
handing over exactly the matching facts removes the opportunity.

Everything returned here was written by a deterministic stage. Nothing is computed at
question time, so an answer can only ever restate what the review actually found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from themis.db.models import Finding, GrainRecord, ModelDelta, ReviewRun
from themis.logging import get_logger

log = get_logger(__name__)

_RULE_ID = re.compile(r"\b([Ff]\d{4})\b")

# Identifiers shaped like dbt model names: snake_case with a recognisable prefix. Used
# only to notice that a question names something the review has no record of.
_MODEL_SHAPED = re.compile(r"\b((?:stg|int|fct|dim|raw|mart|base|src)_[a-z0-9_]+)\b", re.IGNORECASE)


@dataclass
class RetrievedFacts:
    """Facts relevant to one question, and what was searched for."""

    run: ReviewRun
    findings: list[Finding] = field(default_factory=list)
    deltas: list[ModelDelta] = field(default_factory=list)
    grains: list[GrainRecord] = field(default_factory=list)
    models_mentioned: tuple[str, ...] = ()
    rules_mentioned: tuple[str, ...] = ()
    # Model-shaped names in the question that this review has no record of. Surfaced
    # rather than ignored: a question naming an unknown model must not be answered
    # from unrelated facts about other models.
    unknown_entities: tuple[str, ...] = ()
    asked_about_absence: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.findings or self.deltas or self.grains)


def latest_run(session: Session, *, project: str | None = None) -> ReviewRun | None:
    statement = (
        select(ReviewRun)
        .where(ReviewRun.status == "succeeded")
        .order_by(ReviewRun.created_at.desc())
        .limit(1)
    )
    if project:
        statement = statement.where(ReviewRun.project == project)
    return session.execute(statement).scalars().first()


def run_by_key(session: Session, run_key: str) -> ReviewRun | None:
    return session.execute(
        select(ReviewRun).where(ReviewRun.run_key == run_key)
    ).scalar_one_or_none()


def _known_model_names(session: Session, run: ReviewRun) -> set[str]:
    """Every model this run has any record of."""
    names: set[str] = set()
    for table, column in (
        (Finding, Finding.model_name),
        (ModelDelta, ModelDelta.model_name),
        (GrainRecord, GrainRecord.model_name),
    ):
        rows = session.execute(select(column).where(table.run_id == run.id).distinct()).scalars()
        names.update(str(name) for name in rows)
    return names


def _mentions_absence(question: str) -> bool:
    """Whether the question is asking why something was *not* reported.

    This is the question a stored review is uniquely able to answer and a fresh run is
    not, so it is worth detecting explicitly: suppressed findings are persisted, and
    "nothing matched" is a real answer rather than a failure.
    """
    lowered = question.lower()
    patterns = (
        "why did",
        "why didn't",
        "why not",
        "why wasn",
        "not flag",
        "miss",
        "nothing about",
        "no finding",
    )
    return any(p in lowered for p in patterns)


def gather(session: Session, run: ReviewRun, question: str) -> RetrievedFacts:
    """Resolve the entities in a question and fetch the facts about them."""
    known = _known_model_names(session, run)
    lowered = question.lower()

    # Longest names first, so `fct_revenue_incremental` is not shadowed by
    # `fct_revenue` when both exist.
    mentioned = tuple(
        name for name in sorted(known, key=len, reverse=True) if name.lower() in lowered
    )
    rules = tuple({match.upper() for match in _RULE_ID.findall(question)})

    candidates = {name.lower() for name in _MODEL_SHAPED.findall(question)}
    unknown = tuple(sorted(c for c in candidates if c not in {k.lower() for k in known}))

    facts = RetrievedFacts(
        run=run,
        models_mentioned=mentioned,
        rules_mentioned=rules,
        unknown_entities=unknown,
        asked_about_absence=_mentions_absence(question),
    )

    # A question naming a model this review has never heard of gets nothing. Handing
    # over facts about other models invites an answer about the wrong thing, and the
    # absence is itself the honest answer.
    if unknown and not mentioned and not rules:
        log.debug("ask.unknown_entities", names=unknown)
        return facts

    finding_query = select(Finding).where(Finding.run_id == run.id)
    if mentioned:
        finding_query = finding_query.where(Finding.model_name.in_(mentioned))
    if rules:
        finding_query = finding_query.where(Finding.rule_id.in_(rules))
    if not mentioned and not rules:
        # A general question deserves the general picture.
        finding_query = finding_query.limit(20)
    facts.findings = list(session.execute(finding_query).scalars().all())

    delta_query = select(ModelDelta).where(ModelDelta.run_id == run.id)
    grain_query = select(GrainRecord).where(GrainRecord.run_id == run.id)
    if mentioned:
        delta_query = delta_query.where(ModelDelta.model_name.in_(mentioned))
        grain_query = grain_query.where(GrainRecord.model_name.in_(mentioned))
    else:
        delta_query = delta_query.where(ModelDelta.material.is_(True)).limit(10)
        grain_query = grain_query.limit(15)

    facts.deltas = list(session.execute(delta_query).scalars().all())
    facts.grains = list(session.execute(grain_query).scalars().all())

    log.debug(
        "ask.retrieved",
        models=len(mentioned),
        rules=len(rules),
        findings=len(facts.findings),
        deltas=len(facts.deltas),
    )
    return facts
