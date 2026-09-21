"""The THEMIS HTTP API.

Read-mostly, and deliberately thin. Enqueueing writes a row; everything else reads.
The API never runs a review itself — a request that took several minutes of dbt builds
to answer would tie up a worker thread and time out anyway, so the work belongs to the
worker and the API hands back a key.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from themis import __version__
from themis.api.schemas import (
    DeltaOut,
    DispositionRequest,
    FindingOut,
    GrainOut,
    ReviewRequest,
    RunDetail,
    RunSummary,
)
from themis.config import Settings, load_settings
from themis.db.base import get_engine, session_scope
from themis.db.models import Finding, GrainRecord, ModelDelta, ReviewRun, RunSource
from themis.db.store import (
    dismissal_rate,
    enqueue_run,
    prior_occurrences,
    record_disposition,
)
from themis.logging import get_logger
from themis.projects import ProjectNotAllowedError, validate_project_ref
from themis.web.routes import HERE as WEB_ROOT
from themis.web.routes import router as web_router

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Open the connection pool once at startup rather than on the first request."""
    get_engine()
    settings = load_settings()
    if settings.api_token is None:
        # Said once, loudly: every endpoint is open, and a review request runs dbt on the
        # worker. Acceptable on the loopback interface, and nowhere else.
        log.warning("api.no_token", hint="set THEMIS_API_TOKEN before binding beyond 127.0.0.1")
    log.info("api.started", version=__version__)
    yield


app = FastAPI(
    title="THEMIS",
    version=__version__,
    description="Automated review of dbt model changes for financial SQL.",
    lifespan=lifespan,
)

# The pages share the process and the database with the API, not a second service: one
# thing to deploy behind the organisation's sign-in proxy, and one set of queries. Static
# files are the pages' own stylesheet and script — nothing fetched from anywhere else.
app.include_router(web_router)
app.mount("/ui/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="ui-static")


def get_session() -> Iterator[Session]:
    with session_scope() as session:
        yield session


def get_settings() -> Settings:
    return load_settings()


def require_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Require ``Authorization: Bearer <THEMIS_API_TOKEN>`` when a token is configured."""
    if settings.api_token is None:
        return
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, settings.api_token):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def _finding_out(session: Session, row: Finding) -> FindingOut:
    return FindingOut(
        id=row.id,
        fingerprint=row.fingerprint,
        rule_id=row.rule_id,
        family=row.family,
        title=row.title,
        severity=row.severity,
        confidence=row.confidence,
        model_name=row.model_name,
        file_path=row.file_path,
        consequence=row.consequence,
        suggestion=row.suggestion,
        evidence_note=row.evidence_note,
        sql_after=row.sql_after,
        blast_radius=list(row.blast_radius or []),
        execution_delta=row.execution_delta,
        disposition=row.disposition,
        seen_before=prior_occurrences(session, row.fingerprint, before_run_id=row.run_id),
        dismissal_rate=dismissal_rate(session, row.fingerprint),
    )


def _summary(row: ReviewRun, finding_count: int) -> RunSummary:
    return RunSummary(
        run_key=row.run_key,
        project=row.project,
        base_ref=row.base_ref,
        head_ref=row.head_ref,
        status=row.status,
        source=row.source,
        executed=row.executed,
        models_reviewed=row.models_reviewed,
        finding_count=finding_count,
        created_at=row.created_at,
        finished_at=row.finished_at,
        pr_url=row.pr_url,
        error=row.error,
    )


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness plus a real database check — a service that cannot reach its database
    is not healthy, however willing the process is."""
    try:
        with session_scope() as session:
            session.execute(select(func.count(ReviewRun.id)))
        database = True
    except Exception as exc:
        log.warning("health.database_unreachable", error=str(exc)[:200])
        database = False
    return {
        "status": "ok" if database else "degraded",
        "version": __version__,
        "database": database,
    }


@app.post(
    "/reviews", response_model=RunSummary, status_code=202, dependencies=[Depends(require_token)]
)
def create_review(
    request: ReviewRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RunSummary:
    """Queue a review. Returns 202 with a run key; a worker does the work."""
    try:
        validate_project_ref(request.project, settings)
    except ProjectNotAllowedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = enqueue_run(
        session,
        project=request.project,
        base_ref=request.base_ref,
        head_ref=request.head_ref,
        repo=request.repo,
        source=RunSource.API,
        execute=request.execute,
        use_llm=request.use_llm,
        pr_number=request.pr_number,
        pr_url=request.pr_url,
        pr_description=request.pr_description,
    )
    return _summary(run, 0)


@app.get("/reviews", response_model=list[RunSummary], dependencies=[Depends(require_token)])
def list_reviews(
    project: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    session: Session = Depends(get_session),
) -> list[RunSummary]:
    statement = select(ReviewRun).order_by(ReviewRun.created_at.desc()).limit(limit)
    if project:
        statement = statement.where(ReviewRun.project == project)
    if status:
        statement = statement.where(ReviewRun.status == status)
    runs = session.execute(statement).scalars().all()

    counts: dict[int, int] = {
        run_id: count
        for run_id, count in session.execute(
            select(Finding.run_id, func.count(Finding.id)).group_by(Finding.run_id)
        ).tuples()
    }
    return [_summary(run, counts.get(run.id, 0)) for run in runs]


@app.get("/reviews/{run_key}", response_model=RunDetail, dependencies=[Depends(require_token)])
def get_review(run_key: str, session: Session = Depends(get_session)) -> RunDetail:
    run = session.execute(
        select(ReviewRun)
        .where(ReviewRun.run_key == run_key)
        .options(
            selectinload(ReviewRun.findings),
            selectinload(ReviewRun.deltas),
            selectinload(ReviewRun.grains),
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail=f"no review with key {run_key}")

    return RunDetail(
        **_summary(run, len(run.findings)).model_dump(),
        degraded_reason=run.degraded_reason,
        findings=[_finding_out(session, f) for f in run.findings],
        deltas=[
            DeltaOut(
                model_name=d.model_name,
                rows_before=d.rows_before,
                rows_after=d.rows_after,
                sum_deltas=d.sum_deltas or {},
                columns_added=list(d.columns_added or []),
                columns_removed=list(d.columns_removed or []),
                columns_retyped=d.columns_retyped or {},
                build_error=d.build_error,
                material=d.material,
            )
            for d in run.deltas
        ],
        grains=[
            GrainOut(
                model_name=g.model_name,
                columns=list(g.columns or []),
                source=g.source,
                rows_per_key=g.rows_per_key,
                note=g.note,
            )
            for g in run.grains
        ],
    )


@app.post(
    "/findings/{finding_id}/disposition",
    response_model=FindingOut,
    dependencies=[Depends(require_token)],
)
def set_disposition(
    finding_id: int,
    request: DispositionRequest,
    session: Session = Depends(get_session),
) -> FindingOut:
    """Record how a human judged a finding.

    This is the only place the tool learns whether it was right. Everything the
    false-positive rate can ever be measured from comes through here.
    """
    row = session.get(Finding, finding_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no finding {finding_id}")
    # One record whichever way a decision arrives: the pages and this endpoint both append
    # to it, and a caller that does not say who it is acting for is recorded as the API.
    record_disposition(
        session,
        row,
        disposition=request.disposition,
        note=request.note,
        actor=(request.by or "api").strip()[:255] or "api",
    )
    return _finding_out(session, row)


@app.get("/models/{model_name}/grain", dependencies=[Depends(require_token)])
def model_grain_history(
    model_name: str,
    limit: int = Query(default=20, le=100),
    session: Session = Depends(get_session),
) -> list[GrainOut]:
    """How a model's grain has been established over time.

    Where measurement and derivation disagree repeatedly, the lattice is wrong about
    that model — and that is worth knowing without re-reading every report.
    """
    rows = (
        session.execute(
            select(GrainRecord)
            .where(GrainRecord.model_name == model_name)
            .order_by(GrainRecord.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        GrainOut(
            model_name=g.model_name,
            columns=list(g.columns or []),
            source=g.source,
            rows_per_key=g.rows_per_key,
            note=g.note,
        )
        for g in rows
    ]


@app.get(
    "/models/{model_name}/deltas",
    response_model=list[DeltaOut],
    dependencies=[Depends(require_token)],
)
def model_delta_history(
    model_name: str,
    limit: int = Query(default=20, le=100),
    session: Session = Depends(get_session),
) -> list[DeltaOut]:
    rows = (
        session.execute(
            select(ModelDelta)
            .where(ModelDelta.model_name == model_name)
            .order_by(ModelDelta.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        DeltaOut(
            model_name=d.model_name,
            rows_before=d.rows_before,
            rows_after=d.rows_after,
            sum_deltas=d.sum_deltas or {},
            columns_added=list(d.columns_added or []),
            columns_removed=list(d.columns_removed or []),
            columns_retyped=d.columns_retyped or {},
            build_error=d.build_error,
            material=d.material,
        )
        for d in rows
    ]
