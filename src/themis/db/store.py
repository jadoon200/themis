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
    ModelDelta,
    ReviewRun,
    RunSource,
    RunStatus,
    fingerprint_finding,
    utcnow,
)
from themis.logging import get_logger
from themis.models import Finding, Grain
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
) -> ReviewRun:
    """Queue a review. Returns immediately — a worker picks it up."""
    run = ReviewRun(
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


def claim_next_run(session: Session, *, worker_id: str, timeout_s: float) -> ReviewRun | None:
    """Claim one queued run, or reclaim one whose worker stopped reporting.

    ``SKIP LOCKED`` is what makes this safe to run from several workers at once: a row
    another transaction already holds is passed over rather than waited on, so workers
    never serialise behind each other.
    """
    stale_before = datetime.now(UTC) - timedelta(seconds=timeout_s)

    statement = (
        select(ReviewRun)
        .where(
            (ReviewRun.status == RunStatus.QUEUED)
            | (
                (ReviewRun.status == RunStatus.RUNNING)
                & (ReviewRun.heartbeat_at.is_(None) | (ReviewRun.heartbeat_at < stale_before))
            )
        )
        .order_by(ReviewRun.created_at)
        .limit(1)
    )
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


def heartbeat(session: Session, run: ReviewRun) -> None:
    """Report that a claimed run is still being worked on."""
    run.heartbeat_at = utcnow()
    session.flush()


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
                fingerprint=fingerprint_finding(
                    rule_id=finding.rule_id,
                    model_name=finding.evidence.model_name,
                    project=run.project,
                    evidence_note=finding.evidence.note,
                ),
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
    session.flush()
    log.info(
        "run.saved",
        run_key=run.run_key,
        findings=len(result.findings),
        executed=result.executed,
    )
    return run


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
