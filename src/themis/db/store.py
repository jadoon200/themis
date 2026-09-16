"""Persisting reviews, and claiming them for work.

The queue is this database. Runs are claimed with ``FOR UPDATE SKIP LOCKED``, which
gives exactly-once handoff between concurrent workers without a broker to operate.
Reviews are minutes long and low-volume; a dedicated queue would be another service to
run for no benefit.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from themis.db.models import (
    Finding as FindingRow,
)
from themis.db.models import (
    GrainRecord,
    ModelCallRow,
    ModelDelta,
    ReviewRun,
    RunSource,
    RunStatus,
    fingerprint_finding,
    utcnow,
)
from themis.logging import get_logger
from themis.models import Finding, FindingHistory, Grain, PriorJudgement
from themis.pipeline import ReviewResult

log = get_logger(__name__)


def new_run_key() -> str:
    """An opaque public identifier a CI job can be handed."""
    return secrets.token_urlsafe(12)


def enqueue_run(
    session: Session,
    *,
    project: str,
    base_ref: str,
    head_ref: str,
    repo: str | None = None,
    source: RunSource = RunSource.API,
    execute: bool = False,
    use_llm: bool = False,
    pr_number: int | None = None,
    pr_url: str | None = None,
    pr_description: str | None = None,
) -> ReviewRun:
    """Queue a review. Returns immediately — a worker picks it up."""
    run = ReviewRun(
        pr_description=pr_description,
        run_key=new_run_key(),
        project=project,
        repo=repo,
        base_ref=base_ref,
        head_ref=head_ref,
        status=RunStatus.QUEUED,
        source=source,
        execute_requested=execute,
        llm_requested=use_llm,
        pr_number=pr_number,
        pr_url=pr_url,
    )
    session.add(run)
    session.flush()
    log.info("run.enqueued", run_key=run.run_key, project=project)
    return run


def claim_next_run(
    session: Session,
    *,
    worker_id: str,
    timeout_s: float,
    can_execute: bool = True,
    can_review: bool = True,
) -> ReviewRun | None:
    """Claim one queued run, or reclaim one whose worker stopped reporting.

    ``SKIP LOCKED`` is what makes this safe to run from several workers at once: a row
    another transaction already holds is passed over rather than waited on, so workers
    never serialise behind each other.

    ``can_execute`` is the scheduling half of the capability model. A worker without
    warehouse access does not claim a run that asked for execution — it leaves it for
    one that can, rather than taking it and returning a review quietly missing its
    strongest evidence.
    """
    stale_before = datetime.now(UTC) - timedelta(seconds=timeout_s)

    claimable = (ReviewRun.status == RunStatus.QUEUED) | (
        (ReviewRun.status == RunStatus.RUNNING)
        & (ReviewRun.heartbeat_at.is_(None) | (ReviewRun.heartbeat_at < stale_before))
    )
    if not can_execute:
        claimable = claimable & (ReviewRun.execute_requested.is_(False))
    if not can_review:
        # The same rule for the model review: a worker with no model endpoint leaves the
        # run for one that has one, rather than returning it with the review left out.
        claimable = claimable & (ReviewRun.llm_requested.is_(False))

    statement = select(ReviewRun).where(claimable).order_by(ReviewRun.created_at).limit(1)
    # SQLite has no row locking; the tests run single-worker, so skipping the clause
    # there is correct rather than merely convenient.
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(skip_locked=True)

    run = session.execute(statement).scalars().first()
    if run is None:
        return None

    if run.status == RunStatus.RUNNING:
        log.warning("run.reclaimed", run_key=run.run_key, previous_worker=run.worker_id)

    run.status = RunStatus.RUNNING
    run.worker_id = worker_id
    run.started_at = run.started_at or utcnow()
    run.heartbeat_at = utcnow()
    session.flush()
    return run


def still_owns(run: ReviewRun, worker_id: str | None) -> bool:
    """Whether a worker still holds its claim on a run.

    A worker whose heartbeats stopped reaching the database has its run reclaimed, and
    keeps running regardless. Without this both workers wrote findings into the same run
    when they finished, and the slower one's status was the one that stuck.
    ``worker_id=None`` is a caller that never claimed through the queue — the CLI.
    """
    if worker_id is None:
        return True
    return run.status == RunStatus.RUNNING and run.worker_id == worker_id


def load_owned_run(session: Session, run_id: int, worker_id: str | None) -> ReviewRun | None:
    """A run, row-locked, if this worker still owns it; otherwise None, logged."""
    run = session.get(ReviewRun, run_id, with_for_update=True)
    if run is None:
        return None
    if not still_owns(run, worker_id):
        log.warning(
            "run.claim_lost",
            run_key=run.run_key,
            worker=worker_id,
            current_worker=run.worker_id,
            status=run.status,
        )
        return None
    return run


def heartbeat(session: Session, run: ReviewRun, worker_id: str | None = None) -> bool:
    """Report that a claimed run is still being worked on. False once the claim is lost.

    A worker that lost its claim must not keep refreshing the heartbeat of the worker
    that took over — that would hide the new owner dying just as well.
    """
    if not still_owns(run, worker_id):
        return False
    run.heartbeat_at = utcnow()
    session.flush()
    return True


def finding_fingerprint(finding: Finding, *, project: str) -> str:
    """The stored identity of a finding, computed the one way it is computed anywhere.

    Reading history back has to hash exactly what writing it hashed, so both sides call
    this rather than each assembling the arguments themselves.
    """
    return fingerprint_finding(
        rule_id=finding.rule_id,
        model_name=finding.evidence.model_name,
        project=project,
        evidence_note=(
            finding.evidence.identity
            if finding.evidence.identity is not None
            else finding.evidence.note
        ),
    )


def _delta_payload(finding: Finding) -> dict[str, object] | None:
    delta = finding.execution_delta
    if delta is None:
        return None
    return {
        "rows_before": delta.rows_before,
        "rows_after": delta.rows_after,
        "sum_deltas": {k: list(v) for k, v in delta.sum_deltas.items()},
        "columns_added": list(delta.columns_added),
        "columns_removed": list(delta.columns_removed),
        "columns_retyped": {k: list(v) for k, v in delta.columns_retyped.items()},
        "build_error": delta.build_error,
    }


def save_result(session: Session, run: ReviewRun, result: ReviewResult) -> ReviewRun:
    """Write a completed review into the run."""
    run.status = RunStatus.SUCCEEDED
    run.finished_at = utcnow()
    run.executed = result.executed
    run.models_reviewed = len(result.models_reviewed)
    run.degraded_reason = result.degraded_reason

    for finding in result.findings:
        session.add(
            FindingRow(
                run_id=run.id,
                fingerprint=finding_fingerprint(finding, project=run.project),
                rule_id=finding.rule_id,
                family=finding.family,
                title=finding.title,
                severity=str(finding.severity),
                confidence=str(finding.confidence),
                verdict=str(finding.verdict),
                model_name=finding.evidence.model_name,
                file_path=finding.evidence.file_path,
                line=finding.evidence.line,
                consequence=finding.consequence,
                suggestion=finding.suggestion,
                evidence_note=finding.evidence.note,
                sql_after=finding.evidence.sql_after,
                llm_rationale=finding.llm_rationale,
                suppressed_reason=finding.suppressed_reason,
                blast_radius=list(finding.blast_radius),
                execution_delta=_delta_payload(finding),
            )
        )

    if result.execution is not None:
        for name, delta in result.execution.deltas.items():
            session.add(
                ModelDelta(
                    run_id=run.id,
                    model_name=name,
                    rows_before=delta.rows_before,
                    rows_after=delta.rows_after,
                    sum_deltas={k: list(v) for k, v in delta.sum_deltas.items()},
                    columns_added=list(delta.columns_added),
                    columns_removed=list(delta.columns_removed),
                    columns_retyped={k: list(v) for k, v in delta.columns_retyped.items()},
                    null_rate_deltas={k: list(v) for k, v in delta.null_rate_deltas.items()},
                    build_error=delta.build_error,
                    material=delta.is_material,
                )
            )

    _save_grains(session, run, result.grains)
    _save_model_calls(session, run, result)
    session.flush()
    log.info(
        "run.saved",
        run_key=run.run_key,
        findings=len(result.findings),
        executed=result.executed,
    )
    return run


def _save_model_calls(session: Session, run: ReviewRun, result: ReviewResult) -> None:
    """Keep what each model call was shown and what it answered.

    Every other row in this database is about the change under review. These are about
    the reviewer itself, and they are the only record from which a tuning set could
    ever be built — the pack is assembled in memory and, until now, discarded the
    moment the answer came back.

    Fingerprinted the same way the findings are, so a disposition recorded days later
    lands against the call that produced the answer.
    """
    if result.llm is None or not result.llm.calls:
        return
    for call in result.llm.calls:
        finding = call.finding
        session.add(
            ModelCallRow(
                run_id=run.id,
                seat=call.seat,
                llm_model=call.model,
                fingerprint=(
                    finding_fingerprint(finding, project=run.project)
                    if finding is not None
                    else None
                ),
                rule_id=finding.rule_id if finding is not None else None,
                model_name=finding.evidence.model_name if finding is not None else None,
                context=call.context,
                system=call.system,
                response=dict(call.response),
                accepted=call.accepted,
                rejected_reason=call.rejected_reason,
            )
        )
    log.info("run.calls_saved", run_key=run.run_key, calls=len(result.llm.calls))


def _save_grains(session: Session, run: ReviewRun, grains: dict[str, Grain]) -> None:
    for name, grain in grains.items():
        session.add(
            GrainRecord(
                run_id=run.id,
                model_name=name,
                columns=list(grain.columns),
                source=str(grain.source),
                rows_per_key=grain.rows_per_key,
                note=grain.note,
            )
        )


def fail_run(session: Session, run: ReviewRun, error: str) -> ReviewRun:
    """Record that a run could not complete. A failed review is not a clean one."""
    run.status = RunStatus.FAILED
    run.finished_at = utcnow()
    run.error = error[:4000]
    session.flush()
    log.warning("run.failed", run_key=run.run_key, error=error[:300])
    return run


def prior_occurrences(session: Session, fingerprint: str, *, before_run_id: int) -> int:
    """How many earlier runs raised this same finding.

    A finding raised repeatedly is either a real problem nobody has fixed or a false
    positive nobody believes. Which one it is shows in the dispositions.
    """
    rows = session.execute(
        select(FindingRow.id)
        .where(FindingRow.fingerprint == fingerprint)
        .where(FindingRow.run_id < before_run_id)
    ).all()
    return len(rows)


def dismissal_rate(session: Session, fingerprint: str) -> float | None:
    """Share of dispositioned occurrences that a human dismissed.

    The closest thing to a measured false-positive rate that costs no labelling
    effort — it is a by-product of people using the tool.
    """
    rows = (
        session.execute(
            select(FindingRow.disposition)
            .where(FindingRow.fingerprint == fingerprint)
            .where(FindingRow.disposition.is_not(None))
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    dismissed = sum(1 for r in rows if r == "dismissed")
    return dismissed / len(rows)


# How many past judgements a specialist is shown. More is not better: the pack is the
# model's whole world, and precedent crowding out the SQL under review is exactly the
# failure this lane is written to avoid.
_MAX_EXAMPLES = 3


def history_for(
    session: Session,
    findings: list[Finding],
    *,
    project: str,
    examples: int = _MAX_EXAMPLES,
) -> list[FindingHistory | None]:
    """What earlier runs did with each of these findings, aligned with the input.

    One pass over the two things the store knows that a fresh review cannot: how often
    this exact finding has been raised before, and what people decided about findings
    like it. ``None`` for a finding nobody has seen before — distinct from a history of
    zero dismissals, which means it was seen and nobody objected.
    """
    if not findings:
        return []

    fingerprints = [finding_fingerprint(f, project=project) for f in findings]
    rows = session.execute(
        select(
            FindingRow.fingerprint,
            FindingRow.disposition,
            FindingRow.disposition_note,
            FindingRow.disposition_at,
        ).where(FindingRow.fingerprint.in_(set(fingerprints)))
    ).all()

    counts: dict[str, dict[str, int]] = {}
    notes: dict[str, tuple[datetime | None, str | None]] = {}
    for fingerprint, disposition, note, at in rows:
        bucket = counts.setdefault(fingerprint, {"occurrences": 0})
        bucket["occurrences"] += 1
        if disposition:
            bucket[disposition] = bucket.get(disposition, 0) + 1
            previous = notes.get(fingerprint)
            newer = previous is None or (
                at is not None and previous[0] is not None and at > previous[0]
            )
            if note and newer:
                notes[fingerprint] = (at, note)

    judgements = _judgements_for(session, findings, limit=examples) if examples else {}

    out: list[FindingHistory | None] = []
    for finding, fingerprint in zip(findings, fingerprints, strict=True):
        found = counts.get(fingerprint)
        precedents = judgements.get(id(finding), ())
        if found is None and not precedents:
            out.append(None)
            continue
        bucket = found or {"occurrences": 0}
        out.append(
            FindingHistory(
                occurrences=bucket.get("occurrences", 0),
                dismissed=bucket.get("dismissed", 0),
                accepted=bucket.get("accepted", 0),
                fixed=bucket.get("fixed", 0),
                deferred=bucket.get("deferred", 0),
                last_note=notes.get(fingerprint, (None, None))[1],
                examples=precedents,
            )
        )
    return out


def _judgements_for(
    session: Session, findings: list[Finding], *, limit: int
) -> dict[int, tuple[PriorJudgement, ...]]:
    """Dispositioned findings of the same rule, most recently judged first.

    Same rule rather than same fingerprint: the precedent a reviewer wants is "what we
    decided about this rule on this model", and an exact repeat is rarer than the case
    where the judgement is still the relevant one.
    """
    rules = {f.rule_id for f in findings}
    if not rules:
        return {}
    rows = (
        session.execute(
            select(FindingRow)
            .where(FindingRow.rule_id.in_(rules))
            .where(FindingRow.disposition.is_not(None))
            .order_by(FindingRow.disposition_at.desc().nullslast(), FindingRow.id.desc())
            .limit(200)
        )
        .scalars()
        .all()
    )

    out: dict[int, tuple[PriorJudgement, ...]] = {}
    for finding in findings:
        model_name = finding.evidence.model_name
        candidates = [r for r in rows if r.rule_id == finding.rule_id]
        # Same model first: a judgement about this model is a stronger precedent than
        # the same rule somewhere else, and the pack says which it is.
        candidates.sort(key=lambda r: r.model_name != model_name)
        picked = tuple(
            PriorJudgement(
                rule_id=row.rule_id,
                model_name=row.model_name,
                disposition=str(row.disposition),
                title=row.title,
                note=row.disposition_note,
                same_model=row.model_name == model_name,
            )
            for row in candidates[:limit]
        )
        if picked:
            out[id(finding)] = picked
    return out


def export_calls(
    session: Session, *, project: str | None = None, judged_only: bool = False
) -> list[dict[str, object]]:
    """Every captured model call, joined to the judgement that later settled it.

    This is the shape a tuning set would be assembled from, and printing it is how the
    question "is there enough to tune on yet" gets a number instead of an opinion. The
    join is by fingerprint, so a disposition recorded weeks after the call still lands
    against the context that produced the answer.

    ``judged_only`` keeps the calls a human has ruled on — the only ones that carry a
    label at all.
    """
    query = select(ModelCallRow, ReviewRun).join(ReviewRun, ModelCallRow.run_id == ReviewRun.id)
    if project:
        query = query.where(ReviewRun.project == project)
    rows = session.execute(query.order_by(ModelCallRow.id)).all()
    if not rows:
        return []

    fingerprints = {call.fingerprint for call, _ in rows if call.fingerprint}
    judgements: dict[str, tuple[str, str | None]] = {}
    if fingerprints:
        judged = (
            session.execute(
                select(FindingRow)
                .where(FindingRow.fingerprint.in_(fingerprints))
                .where(FindingRow.disposition.is_not(None))
                .order_by(FindingRow.disposition_at.asc().nullsfirst(), FindingRow.id.asc())
            )
            .scalars()
            .all()
        )
        # Later rows overwrite earlier ones, so the most recent judgement wins.
        for row in judged:
            judgements[row.fingerprint] = (str(row.disposition), row.disposition_note)

    out: list[dict[str, object]] = []
    for call, run in rows:
        disposition, note = judgements.get(call.fingerprint or "", (None, None))
        if judged_only and disposition is None:
            continue
        out.append(
            {
                "run_key": run.run_key,
                "project": run.project,
                "seat": call.seat,
                "llm_model": call.llm_model,
                "rule_id": call.rule_id,
                "model_name": call.model_name,
                "fingerprint": call.fingerprint,
                "system": call.system,
                "context": call.context,
                "response": call.response,
                "accepted_by_selfcheck": call.accepted,
                "rejected_reason": call.rejected_reason,
                "human_disposition": disposition,
                "human_note": note,
                "created_at": call.created_at.isoformat() if call.created_at else None,
            }
        )
    return out
