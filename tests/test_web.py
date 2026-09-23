"""The web pages: the numbers a manager reads, the decision record, and the chat.

What is protected here, in order of how badly it would go if it broke:

- **Nothing the project or the model wrote is ever rendered as markup.** A PR title, an
  evidence note, a model name — all of it can carry text the project put there.
- **Every decision has an author, and the deployment decides who.** Behind sign-in that is
  a trusted header and nothing else; in a demo it is a typed name, and the pages say so.
- **The record is appended, never edited.** A decision reversed must still show both.
- **The overview's numbers mean what their labels say**, with one definition of "open"
  shared by the verdict, the counts and the gate.
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from themis.config import load_settings
from themis.db.base import Base, get_engine, session_scope
from themis.db.models import DispositionEvent, ModelDelta, ReviewRun, RunSnapshot, RunStatus
from themis.db.models import Finding as FindingRow
from themis.db.store import record_disposition
from themis.eval import synthetic
from themis.llm.provider import Response, Usage
from themis.web import views

UI = {"X-Themis-UI": "1"}


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("THEMIS_DATABASE_URL", f"sqlite:///{tmp_path}/web.db")
    monkeypatch.delenv("THEMIS_UI_TRUSTED_USER_HEADER", raising=False)
    monkeypatch.delenv("THEMIS_FAIL_ON_SEVERITY", raising=False)
    Base.metadata.create_all(get_engine())
    yield


@pytest.fixture
def client(db: None) -> Iterator[TestClient]:
    from themis.api.app import app

    with TestClient(app) as c:
        yield c


def _run(
    *,
    key: str = "RUNKEY0001",
    title: str = "Simplify the FX rate lookup",
    findings: tuple[tuple[str, str], ...] = (("F1001", "high"),),
    snapshot: bool = False,
    evidence: str = "the join key does not cover the grain",
    created_at: datetime | None = None,
) -> str:
    with session_scope() as session:
        stamps = {"created_at": created_at, "finished_at": created_at} if created_at else {}
        run = ReviewRun(
            **stamps,
            run_key=key,
            project="demo_project",
            base_ref="main",
            head_ref="feature/x",
            status=RunStatus.SUCCEEDED,
            pr_number=1418,
            pr_title=title,
            pr_author="priya",
            executed=True,
            models_reviewed=1,
            reviewed_models=["stg_0"],
        )
        session.add(run)
        session.flush()
        for rule, severity in findings:
            session.add(
                FindingRow(
                    run_id=run.id,
                    fingerprint=f"{rule}-{severity}-{key}",
                    rule_id=rule,
                    family=rule[:2] if rule[0] == "F" else "X",
                    title=f"`stg_0` {rule} finding",
                    severity=severity,
                    confidence="likely",
                    verdict="undecidable",
                    model_name="stg_0",
                    consequence="Amounts would be duplicated.",
                    evidence_note=evidence,
                    blast_radius=["fct_0"],
                )
            )
        session.add(
            ModelDelta(
                run_id=run.id,
                model_name="stg_0",
                rows_before=10,
                rows_after=20,
                sum_deltas={"amount": [100.0, 200.0]},
                material=True,
            )
        )
        if snapshot:
            project = synthetic.project(12)
            for side in ("before", "after"):
                session.add(
                    RunSnapshot(
                        run_id=run.id,
                        side=side,
                        payload=zlib.compress(project.model_dump_json().encode()),
                    )
                )
    return key


def _finding_ids() -> list[int]:
    with session_scope() as session:
        return [f.id for f in session.query(FindingRow).order_by(FindingRow.id)]


# --- the numbers ---------------------------------------------------------------------------


def test_the_verdict_uses_the_gates_threshold_and_one_definition_of_open(db: None) -> None:
    _run(findings=(("F1001", "high"), ("F6004", "medium")))
    with session_scope() as session:
        run = session.query(ReviewRun).one()
        findings = list(run.findings)
        assert views.verdict_for(findings, threshold="high").key == "blocking"
        # A higher bar: the high finding no longer blocks, but it is still open.
        assert views.verdict_for(findings, threshold="critical").key == "review"
        high = next(f for f in findings if f.severity == "high")
        record_disposition(session, high, disposition="dismissed", note=None, actor="alex")
        assert views.verdict_for(findings, threshold="high").key == "review"
        # Deferred is still open: deciding later is not deciding.
        medium = next(f for f in findings if f.severity == "medium")
        record_disposition(session, medium, disposition="deferred", note=None, actor="alex")
        assert views.verdict_for(findings, threshold="high").key == "review"
        record_disposition(session, medium, disposition="accepted", note=None, actor="alex")
        assert views.verdict_for(findings, threshold="high").label == "Settled"


def test_the_overview_counts_what_its_labels_say(db: None) -> None:
    _run(key="A", findings=(("F1001", "high"), ("X0004", "critical")))
    _run(key="B", findings=(("F6004", "medium"),))
    _run(key="C", findings=())
    with session_scope() as session:
        data = views.overview(session, threshold="high")
    assert data.pull_requests == 3
    assert data.blocking == 1  # only A has an open finding at or above high
    assert data.restating == 1  # only A carries an X0004
    assert data.awaiting_decision == 2  # A's high and critical; B's medium is below the bar
    assert data.severity_counts["critical"] == 1 and data.severity_counts["medium"] == 1
    assert {r.rule_id for r in data.top_rules} == {"F1001", "X0004", "F6004"}


# --- the record ----------------------------------------------------------------------------


def test_a_reversed_decision_keeps_both_and_names_both_authors(db: None) -> None:
    """The case the old single column lost: dismissed in one meeting, reopened in another."""
    _run()
    with session_scope() as session:
        finding = session.query(FindingRow).one()
        record_disposition(session, finding, disposition="dismissed", note="noise", actor="alex")
        record_disposition(session, finding, disposition="accepted", note="real", actor="morgan")
    with session_scope() as session:
        finding = session.query(FindingRow).one()
        assert finding.disposition == "accepted" and finding.disposition_by == "morgan"
        events = session.query(DispositionEvent).order_by(DispositionEvent.id).all()
        assert [(e.disposition, e.actor) for e in events] == [
            ("dismissed", "alex"),
            ("accepted", "morgan"),
        ]


def test_a_decision_through_the_machine_api_lands_in_the_same_record(client: TestClient) -> None:
    _run()
    (finding_id,) = _finding_ids()
    response = client.post(
        f"/findings/{finding_id}/disposition",
        json={"disposition": "fixed", "note": "follow-up commit", "by": "ci-relay"},
    )
    assert response.status_code == 200
    with session_scope() as session:
        (event,) = session.query(DispositionEvent).all()
        assert (event.actor, event.disposition) == ("ci-relay", "fixed")


# --- identity and writes -------------------------------------------------------------------


def test_a_write_without_the_pages_header_is_refused(client: TestClient) -> None:
    """A cross-site form can post here but cannot set this header."""
    _run()
    (finding_id,) = _finding_ids()
    response = client.post(f"/ui/findings/{finding_id}/decision", json={"disposition": "dismissed"})
    assert response.status_code == 403


def test_a_decision_with_no_author_is_refused_rather_than_recorded_anonymously(
    client: TestClient,
) -> None:
    _run()
    (finding_id,) = _finding_ids()
    response = client.post(
        f"/ui/findings/{finding_id}/decision", json={"disposition": "dismissed"}, headers=UI
    )
    assert response.status_code == 401
    with session_scope() as session:
        assert session.query(DispositionEvent).count() == 0


def test_a_demo_decision_is_recorded_under_the_name_given(client: TestClient) -> None:
    _run()
    (finding_id,) = _finding_ids()
    assert client.post("/ui/whoami", json={"name": "priya"}, headers=UI).status_code == 200
    response = client.post(
        f"/ui/findings/{finding_id}/decision",
        json={"disposition": "accepted", "note": "known and signed off"},
        headers=UI,
    )
    assert response.status_code == 200
    assert response.json()["actor"] == "priya"


def test_behind_sign_in_the_author_comes_from_the_trusted_header_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once sign-in decides who someone is, nobody may type a different name in."""
    monkeypatch.setenv("THEMIS_UI_TRUSTED_USER_HEADER", "X-Remote-User")
    _run()
    (finding_id,) = _finding_ids()
    assert client.post("/ui/whoami", json={"name": "anyone"}, headers=UI).status_code == 409

    no_header = client.post(
        f"/ui/findings/{finding_id}/decision", json={"disposition": "fixed"}, headers=UI
    )
    assert no_header.status_code == 401

    # A demo name left in a cookie must not win over sign-in.
    client.cookies.set("themis_user", "someone-else")
    signed = client.post(
        f"/ui/findings/{finding_id}/decision",
        json={"disposition": "fixed"},
        headers={**UI, "X-Remote-User": "morgan.lee"},
    )
    assert signed.status_code == 200
    assert signed.json()["actor"] == "morgan.lee"


# --- rendering -----------------------------------------------------------------------------


def test_the_pages_render_with_the_reviews_in_them(client: TestClient) -> None:
    key = _run()
    overview = client.get("/ui")
    assert overview.status_code == 200
    assert "Simplify the FX rate lookup" in overview.text
    page = client.get(f"/ui/pr/{key}")
    assert page.status_code == 200
    assert "F1001" in page.text and "the join key does not cover the grain" in page.text
    # A model name in backticks becomes code — and only that.
    assert "<code>stg_0</code>" in page.text
    assert client.get("/ui/decisions").status_code == 200
    assert client.get("/ui/prs").status_code == 200
    assert client.get("/ui/pr/NOSUCHKEY").status_code == 404


def test_text_from_the_project_is_never_rendered_as_markup(client: TestClient) -> None:
    """A title and an evidence note are both text the project controls."""
    key = _run(
        title="<script>alert('title')</script>",
        evidence="<img src=x onerror=alert('evidence')>",
    )
    for path in ("/ui", "/ui/prs", f"/ui/pr/{key}"):
        body = client.get(path).text
        assert "<script>alert('title')</script>" not in body
        assert "<img src=x onerror" not in body
    assert "&lt;script&gt;" in client.get(f"/ui/pr/{key}").text


def test_asset_urls_change_when_the_assets_do(client: TestClient) -> None:
    """Found by looking: the browser kept a fixed stylesheet from its cache."""
    from themis.web.routes import ASSET_VERSION

    _run()
    body = client.get("/ui").text
    assert f"themis.css?v={ASSET_VERSION}" in body and f"themis.js?v={ASSET_VERSION}" in body
    assert f"theme.js?v={ASSET_VERSION}" in body


def test_the_brand_is_configuration_and_generic_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This repository is public; an organisation's name belongs in its own settings."""
    assert load_settings().ui_brand_name == "THEMIS"
    monkeypatch.setenv("THEMIS_UI_BRAND_NAME", "Risk Review")
    assert "Risk Review" in client.get("/ui").text


# --- the chat ------------------------------------------------------------------------------


class _Scripted:
    """A model that looks one thing up, then answers by quoting it."""

    def __init__(self) -> None:
        self.chose = 0

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any], model: str) -> Response:
        properties = schema.get("properties", {})
        if "next" in properties:
            self.chose += 1
            choice = "model_details" if self.chose == 1 else "answer"
            return Response(payload={"thought": "look it up", "next": choice}, usage=Usage(calls=1))
        if "can_answer" in properties:
            return Response(
                payload={
                    "can_answer": True,
                    "answer": "stg_0 is materialized as a view.",
                    "citations": [{"result": 1, "quote": "materialization: view"}],
                },
                usage=Usage(calls=1),
            )
        return Response(payload={"model": "stg_0"}, usage=Usage(calls=1))


def _events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_the_chat_streams_each_step_and_a_grounded_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import themis.agent.loop as loop

    monkeypatch.setattr(loop, "agent_provider", lambda settings: _Scripted())
    key = _run(snapshot=True)
    response = client.post(f"/ui/pr/{key}/chat", json={"question": "How is stg_0 built?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and "step" in kinds and kinds[-1] == "answer"
    step = next(e for e in events if e["type"] == "step")
    assert step["tool"] == "model_details" and step["ok"]
    answer = events[-1]
    assert answer["citations"][0]["quote"] == "materialization: view"
    assert answer["citations"][0]["tool"] == "model_details"


def test_the_chat_says_so_when_a_review_kept_no_snapshots(client: TestClient) -> None:
    key = _run(snapshot=False)
    events = _events(client.post(f"/ui/pr/{key}/chat", json={"question": "Anything?"}).text)
    assert events[-1]["type"] == "error"
    assert "snapshots" in events[-1]["text"]


def test_a_stored_review_rebuilds_the_workspace_the_tools_read(db: None) -> None:
    """Findings, what moved and the project, back as the objects the agent reads."""
    from themis.db.workspace import workspace_for_run

    _run(snapshot=True, findings=(("F1001", "high"), ("X0004", "critical")))
    with session_scope() as session:
        run = session.query(ReviewRun).one()
        workspace = workspace_for_run(session, run)
    assert workspace is not None and workspace.is_review
    assert {f.rule_id for f in workspace.findings} == {"F1001", "X0004"}
    assert workspace.changed_models == ("stg_0",)
    assert workspace.execution is not None
    delta = workspace.execution.deltas["stg_0"]
    assert (delta.rows_before, delta.rows_after) == (10, 20)
    assert delta.sum_deltas["amount"] == (100.0, 200.0)


# --- the page contract: policy-clean, periods, charts, the small endpoints ------------------

_INLINE_STYLE = re.compile(r"\sstyle\s*=", re.IGNORECASE)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)


def test_no_page_needs_an_inline_script_or_style(client: TestClient) -> None:
    """The policy forbids both, and a browser enforcing it fails silently: the chart that
    lost its colour, the bar with no width. So every page is checked, not trusted."""
    key = _run(snapshot=True, findings=(("F1001", "high"), ("X0004", "critical")))
    client.cookies.set("themis_user", "priya")
    client.post(
        f"/ui/findings/{_finding_ids()[0]}/decision",
        json={"disposition": "deferred", "note": "asking"},
        headers=UI,
    )
    for path in ("/ui", "/ui?days=7", "/ui?days=0", "/ui/prs", f"/ui/pr/{key}", "/ui/decisions"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert not _INLINE_STYLE.search(response.text), path
        assert not _INLINE_SCRIPT.search(response.text), path
        policy = response.headers["content-security-policy"]
        assert "script-src 'self'" in policy and "style-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert response.headers["x-frame-options"] == "DENY"
    # The JSON API is not a page and keeps its own headers.
    assert "content-security-policy" not in client.get("/health").headers


def test_the_bare_address_leads_to_the_pages(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/ui"


def test_the_theme_is_chosen_before_the_first_paint(client: TestClient) -> None:
    """A saved light theme is applied by a script that runs before the stylesheet paints."""
    head = client.get("/ui").text.split("</head>")[0]
    assert 'data-theme="dark"' in head
    assert head.index("theme.js") < head.index("themis.css")


def test_a_period_counts_its_own_reviews_and_the_one_before(db: None) -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    _run(key="NEW", created_at=now - timedelta(days=2))
    _run(key="PRIOR", created_at=now - timedelta(days=10))
    _run(key="OLD", created_at=now - timedelta(days=40))
    with session_scope() as session:
        week = views.overview(session, threshold="high", days=7, now=now)
        month = views.overview(session, threshold="high", days=30, now=now)
        ever = views.overview(session, threshold="high", days=0, now=now)
    assert (week.pull_requests, week.pull_requests_before) == (1, 1)
    assert (month.pull_requests, month.pull_requests_before) == (2, 1)
    assert ever.pull_requests == 3 and ever.pull_requests_before is None
    assert len(month.daily) == 30 and sum(d.reviews for d in month.daily) == 2
    # All time still draws a readable chart, not one bar per day since the first review.
    assert len(ever.daily) == views.TREND_DAYS


def test_an_unknown_period_falls_back_rather_than_failing(client: TestClient) -> None:
    _run()
    body = client.get("/ui?days=999").text
    assert 'aria-current="true">30 days<' in body


def test_the_settled_breakdown_counts_each_findings_latest_decision(db: None) -> None:
    _run(findings=(("F1001", "high"), ("F6004", "medium"), ("X0004", "critical")))
    with session_scope() as session:
        rows = session.query(FindingRow).order_by(FindingRow.id).all()
        record_disposition(session, rows[0], disposition="deferred", note=None, actor="a")
        record_disposition(session, rows[0], disposition="fixed", note=None, actor="a")
        record_disposition(session, rows[1], disposition="dismissed", note=None, actor="a")
    with session_scope() as session:
        data = views.overview(session, threshold="high")
    assert data.disposition_counts == {
        "fixed": 1,
        "accepted": 0,
        "dismissed": 1,
        "deferred": 0,
        "open": 1,
    }
    # Found on the page: the header said 10 of 17 settled beside a card saying 8 of 17
    # decided, because it counted deferred as settled. One definition of open, everywhere.
    with session_scope() as session:
        rows = session.query(FindingRow).order_by(FindingRow.id).all()
        record_disposition(session, rows[2], disposition="deferred", note=None, actor="a")
    with session_scope() as session:
        data = views.overview(session, threshold="critical")
        closed = sum(1 for f in session.query(FindingRow) if not views.is_open(f))
    assert data.settled_total == closed == 2
    assert data.gated_total == 1 and data.gated_decided == 0  # the deferred critical


def test_the_navigation_counts_blocking_the_way_the_overview_does(db: None) -> None:
    """Two queries for one number — one in SQL for every page, one in Python for the
    overview. They must agree, including on deferred meaning still open."""
    _run(key="A", findings=(("F1001", "high"),))
    _run(key="B", findings=(("F6004", "medium"),))
    _run(key="C", findings=(("X0004", "critical"),))
    _run(key="D", findings=(("F1004", "high"),))
    with session_scope() as session:
        rows = {f.rule_id: f for f in session.query(FindingRow)}
        record_disposition(session, rows["X0004"], disposition="deferred", note=None, actor="a")
        record_disposition(session, rows["F1004"], disposition="fixed", note=None, actor="a")
    with session_scope() as session:
        nav = views.sidebar(session, threshold="high")
        data = views.overview(session, threshold="high")
    assert nav.blocking == data.blocking == 2
    assert len(nav.recent) == 4


def test_charts_are_drawn_with_classes_and_say_what_they_show() -> None:
    from themis.web import charts

    days = [
        views.DayBucket(
            day=date(2026, 9, 1) + timedelta(days=i),
            reviews=1,
            severities={"critical": 1, "high": 2, "medium": 0, "low": 0, "info": 0},
        )
        for i in range(30)
    ]
    trend = str(charts.trend(days))
    assert "sev-fill-critical" in trend and "sev-fill-high" in trend
    assert 'aria-label="90 findings over 30 days"' in trend
    # Dates under the bars: enough to read, never so many that they collide.
    assert 4 <= trend.count('text-anchor="middle"') <= 8
    assert str(charts.trend([])) == ""
    donut = str(charts.donut({"critical": 1, "high": 3}))
    assert 'aria-label="1 critical, 3 high"' in donut and ">4</text>" in donut
    assert 'class="meter-fill tone-accent" width="100"' in str(charts.meter(4.0))
    assert 'class="meter-fill tone-accent" width="0"' in str(charts.meter(-1.0))
    assert str(charts.stacked([("fixed", 1), ("open", 3), ("dismissed", 0)])).count("<rect") == 2
    for svg in (trend, donut, str(charts.sparkline([0, 2, 1])), str(charts.meter(0.5))):
        assert "style=" not in svg


def test_the_palette_searches_every_review(client: TestClient) -> None:
    key = _run()
    rows = client.get("/ui/api/prs").json()
    assert rows == [
        {
            "key": key,
            "number": 1418,
            "title": "Simplify the FX rate lookup",
            "author": "priya",
            "verdict": "blocking",
            "label": "Blocking",
        }
    ]


def test_the_model_status_says_so_when_the_host_does_not_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the office the model is on another machine; the pages must say when it is down."""
    from themis.web import routes

    monkeypatch.setenv("THEMIS_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setitem(routes._STATUS_CACHE, "value", None)
    status = client.get("/ui/api/status").json()
    assert status["reachable"] is False and status["pulled"] is False
    assert status["model"] == load_settings().llm_supervisor_model


def test_serve_refuses_to_expose_a_trusted_header_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With identity taken from a header, a port reachable around the proxy lets anyone
    name anyone. `themis serve` will not bind beyond loopback in that setup unless told
    the network guarantees it."""
    from typer.testing import CliRunner

    from themis.cli import app

    started: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append(k))
    monkeypatch.setenv("THEMIS_UI_TRUSTED_USER_HEADER", "X-Forwarded-User")
    runner = CliRunner()

    refused = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert refused.exit_code == 2 and "Refusing" in refused.output and not started

    assert runner.invoke(app, ["serve"]).exit_code == 0  # loopback behind the proxy
    assert runner.invoke(app, ["serve", "--host", "0.0.0.0", "--trust-network"]).exit_code == 0
    assert [s["host"] for s in started] == ["127.0.0.1", "0.0.0.0"]
