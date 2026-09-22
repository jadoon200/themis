"""The pages, the one write they allow, and the chat.

Server-rendered on purpose. A single-page app would bring a JavaScript toolchain and a
registry of a thousand packages into a bank's dependency review, for pages that are mostly
tables. What little the browser does — record a decision, stream an answer — is a few
dozen lines of plain JavaScript with no dependencies at all.

Three rules the code below keeps:

- **Nothing the model or the project wrote is ever rendered as HTML.** Templates escape;
  the chat inserts text, never markup. The SQL under review can carry text written at
  whatever reads it (F7004), and a page that rendered it would be the easiest way in.
- **A decision has an author, and the deployment decides who that is.** Behind the
  organisation's sign-in proxy it is a trusted header; in a demo it is the name typed on
  the page, and the page says so.
- **A browser-initiated write carries a header a cross-site form cannot set**, which is the
  whole of the CSRF protection a same-origin page with no cookies-as-credentials needs.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import BaseModel, Field
from sqlalchemy import select

from themis.config import Settings, load_settings
from themis.db.base import session_scope
from themis.db.models import Finding as FindingRow
from themis.db.models import ReviewRun
from themis.db.store import record_disposition
from themis.logging import get_logger
from themis.web import views

log = get_logger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _ago(value: datetime | None) -> str:
    """ "3 hours ago", from a timestamp SQLite stores naive and Postgres stores aware."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


templates.env.filters["ago"] = _ago

_TICKS = re.compile(r"`([^`\n]{1,200})`")


def _code_spans(value: str | None) -> Markup:
    """Finding titles name models in backticks; show those as code, and nothing else.

    Escaped *first*, then wrapped: the text comes from the project (a model name) and from
    rules, and turning backticks into markup must never be a way to turn anything else into
    markup too.
    """
    escaped = str(escape(value or ""))
    return Markup(_TICKS.sub(r"<code>\1</code>", escaped))


templates.env.filters["code_spans"] = _code_spans


def _asset_version() -> str:
    """A short hash of the stylesheet and script, appended to their URLs.

    Found by looking at the pages rather than by a test: a CSS fix was on disk and the
    browser kept rendering the old file from its cache. After an upgrade at the office every
    user would do the same, running new pages against stale script. A URL that changes when
    the content changes is the only cache rule that is right both ways.
    """
    digest = hashlib.sha256()
    for name in ("themis.css", "themis.js"):
        digest.update((HERE / "static" / name).read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()

router = APIRouter(prefix="/ui", include_in_schema=False)

_DEMO_USER_COOKIE = "themis_user"


def _settings() -> Settings:
    return load_settings()


def _threshold(settings: Settings) -> str:
    return settings.fail_on_severity or "high"


def current_user(request: Request, settings: Settings) -> str | None:
    """Who is making this request, as the deployment defines it — or None."""
    if settings.ui_trusted_user_header:
        value = request.headers.get(settings.ui_trusted_user_header, "").strip()
        return value or None
    value = request.cookies.get(_DEMO_USER_COOKIE, "").strip()
    return value[:80] or None


def _require_ui_header(x_themis_ui: str | None = Header(default=None)) -> None:
    """Refuse a write that did not come from the pages' own script.

    A cross-site form can submit to this origin but cannot add a custom header without a
    CORS preflight, which this service never grants.
    """
    if x_themis_ui != "1":
        raise HTTPException(status_code=403, detail="missing X-Themis-UI header")


def _context(request: Request, settings: Settings, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "brand_name": settings.ui_brand_name,
        "brand_subtitle": settings.ui_brand_subtitle,
        "logo_url": settings.ui_logo_url,
        "user": current_user(request, settings),
        "trusted_identity": settings.ui_trusted_user_header is not None,
        "threshold": _threshold(settings),
        "severities": views.SEVERITY_ORDER,
        "asset_version": ASSET_VERSION,
        **extra,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request, settings: Settings = Depends(_settings)) -> Response:
    with session_scope() as session:
        data = views.overview(session, threshold=_threshold(settings))
        return templates.TemplateResponse(
            request, "overview.html", _context(request, settings, page="overview", data=data)
        )


@router.get("/prs", response_class=HTMLResponse)
def pull_requests_page(request: Request, settings: Settings = Depends(_settings)) -> Response:
    with session_scope() as session:
        rows = views.pull_requests(session, threshold=_threshold(settings))
        return templates.TemplateResponse(
            request, "prs.html", _context(request, settings, page="prs", rows=rows)
        )


@router.get("/pr/{run_key}", response_class=HTMLResponse)
def pull_request_page(
    run_key: str, request: Request, settings: Settings = Depends(_settings)
) -> Response:
    with session_scope() as session:
        page = views.pull_request_page(session, run_key, threshold=_threshold(settings))
        if page is None:
            raise HTTPException(status_code=404, detail=f"no review {run_key}")
        return templates.TemplateResponse(
            request, "pr.html", _context(request, settings, page="prs", pr=page)
        )


@router.get("/decisions", response_class=HTMLResponse)
def decisions_page(request: Request, settings: Settings = Depends(_settings)) -> Response:
    with session_scope() as session:
        rows = views.recent_decisions(session, limit=200)
        return templates.TemplateResponse(
            request, "decisions.html", _context(request, settings, page="decisions", rows=rows)
        )


class NameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/whoami", dependencies=[Depends(_require_ui_header)])
def set_demo_name(request_body: NameRequest, settings: Settings = Depends(_settings)) -> Response:
    """Demo only: remember the name decisions are recorded under.

    Refused once a trusted header is configured — a deployment with real sign-in must not
    also let anyone type a name into the record.
    """
    if settings.ui_trusted_user_header:
        raise HTTPException(status_code=409, detail="identity comes from sign-in here")
    response = JSONResponse({"name": request_body.name.strip()})
    response.set_cookie(
        _DEMO_USER_COOKIE, request_body.name.strip(), samesite="strict", httponly=True
    )
    return response


class DecisionRequest(BaseModel):
    disposition: str = Field(pattern="^(accepted|dismissed|fixed|deferred)$")
    note: str | None = Field(default=None, max_length=2000)


@router.post("/findings/{finding_id}/decision", dependencies=[Depends(_require_ui_header)])
def record_decision(
    finding_id: int,
    body: DecisionRequest,
    request: Request,
    settings: Settings = Depends(_settings),
) -> dict[str, Any]:
    actor = current_user(request, settings)
    if actor is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "no signed-in user on this request"
                if settings.ui_trusted_user_header
                else "say who you are before recording a decision"
            ),
        )
    with session_scope() as session:
        finding = session.get(FindingRow, finding_id)
        if finding is None:
            raise HTTPException(status_code=404, detail=f"no finding {finding_id}")
        event = record_disposition(
            session,
            finding,
            disposition=body.disposition,
            note=(body.note or "").strip() or None,
            actor=actor,
        )
        return {
            "finding": finding_id,
            "disposition": event.disposition,
            "actor": event.actor,
            "at": event.at.isoformat(),
            "note": event.note,
        }


class ChatRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def _event(kind: str, **payload: Any) -> str:
    """One server-sent event. JSON on a single line, so no field can end the frame early."""
    return f"data: {json.dumps({'type': kind, **payload}, default=str)}\n\n"


def _preview(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:160]


@router.post("/pr/{run_key}/chat")
def chat(run_key: str, body: ChatRequest, settings: Settings = Depends(_settings)) -> Response:
    """Stream the agent investigating a question about one stored review.

    POST, not an EventSource GET: the question is free text and a URL is logged by every
    proxy between the browser and here.
    """
    from themis.agent.loop import Step, agent_provider, investigate
    from themis.db.workspace import workspace_for_run

    question = body.question.strip()

    def events() -> Iterator[str]:
        yield _event("status", text="Loading the review")
        with session_scope() as session:
            run = session.scalar(select(ReviewRun).where(ReviewRun.run_key == run_key))
            if run is None:
                yield _event("error", text=f"There is no review {run_key}.")
                return
            workspace = workspace_for_run(session, run, dialect=settings.dialect)
        if workspace is None:
            yield _event(
                "error",
                text="This review was stored without its project snapshots, so there is "
                "nothing for the agent to investigate. Reviews stored from now on keep them.",
            )
            return

        updates: queue.Queue[tuple[str, Any]] = queue.Queue()

        def on_step(step: Step) -> None:
            updates.put(("step", step))

        def run_agent() -> None:
            try:
                provider = agent_provider(settings)
                outcome = investigate(
                    question, workspace, provider=provider, settings=settings, on_step=on_step
                )
                updates.put(("done", outcome))
            except Exception as exc:  # reported to the page, never swallowed
                log.warning("ui.chat_failed", error=str(exc)[:200])
                updates.put(("error", exc))

        threading.Thread(target=run_agent, daemon=True, name="themis-chat").start()
        yield _event("status", text="Choosing what to look up")

        while True:
            try:
                kind, payload = updates.get(timeout=300)
            except queue.Empty:
                yield _event("error", text="The model did not answer within five minutes.")
                return
            if kind == "step":
                yield _event(
                    "step",
                    n=payload.number,
                    tool=payload.tool,
                    arguments=payload.arguments,
                    ok=payload.result.ok,
                    preview=_preview(payload.result.text),
                )
            elif kind == "error":
                yield _event("error", text=f"The chat could not run: {payload}")
                return
            else:
                steps = {step.number: step for step in payload.steps}
                if payload.grounded:
                    yield _event(
                        "answer",
                        answer=payload.answer,
                        citations=[
                            {
                                "n": c.result,
                                "tool": steps[c.result].tool if c.result in steps else "?",
                                "quote": c.quote,
                            }
                            for c in payload.citations
                        ],
                        calls=payload.usage.calls,
                    )
                else:
                    yield _event("refusal", reason=payload.refusal_reason or "no reason given")
                return

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
