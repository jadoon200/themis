"""The service, where a request becomes a dbt run on somebody's machine.

Each test pins a gap a review of the service found: the model layer never ran for a
queued review, a worker without COMPILE compiled anyway, a slow worker wrote into a run
another worker had taken over, and a review request could name any path and any string
as a revision.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from themis.api import app as api_module
from themis.capabilities import Capability, CapabilityError
from themis.config import Settings
from themis.db.base import Base, get_engine, session_scope
from themis.db.models import ReviewRun, RunStatus
from themis.db.store import claim_next_run, enqueue_run, heartbeat, load_owned_run
from themis.models import Confidence, Evidence, Finding, Severity
from themis.projects import ProjectNotAllowedError, resolve_project, validate_project_ref
from themis.report import markdown


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _client(session: Session, settings: Settings) -> TestClient:
    def _session() -> Iterator[Session]:
        yield session

    api_module.app.dependency_overrides[api_module.get_session] = _session
    api_module.app.dependency_overrides[api_module.get_settings] = lambda: settings
    return TestClient(api_module.app)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    api_module.app.dependency_overrides.clear()


# --- the API -------------------------------------------------------------------------------


def test_a_configured_token_is_required(session: Session) -> None:
    client = _client(session, Settings(api_token="s3cret"))
    assert client.post("/reviews", json={"project": "demo_project"}).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.get("/reviews", headers=wrong).status_code == 401
    right = {"Authorization": "Bearer s3cret"}
    assert (
        client.post("/reviews", json={"project": "demo_project"}, headers=right).status_code == 202
    )


def test_health_needs_no_token(session: Session) -> None:
    client = _client(session, Settings(api_token="s3cret"))
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "-p", "main HEAD"])
def test_a_revision_git_would_read_as_an_option_is_rejected(session: Session, ref: str) -> None:
    client = _client(session, Settings())
    response = client.post("/reviews", json={"project": "demo_project", "head_ref": ref})
    assert response.status_code == 422


@pytest.mark.parametrize("project", ["../elsewhere", "/etc", "demo/../../x"])
def test_a_project_outside_the_roots_is_rejected(session: Session, project: str) -> None:
    client = _client(session, Settings())
    assert client.post("/reviews", json={"project": project}).status_code == 422


def test_an_absolute_project_under_a_configured_root_is_accepted(session: Session) -> None:
    client = _client(session, Settings(project_roots=("/srv/projects",)))
    response = client.post("/reviews", json={"project": "/srv/projects/finance"})
    assert response.status_code == 202


def test_the_description_is_queued_with_the_run(session: Session) -> None:
    client = _client(session, Settings())
    key = client.post(
        "/reviews",
        json={"project": "demo_project", "use_llm": True, "pr_description": "tidy the FX join"},
    ).json()["run_key"]
    run = session.query(ReviewRun).filter_by(run_key=key).one()
    assert run.llm_requested
    assert run.pr_description == "tidy the FX join"


def test_the_worker_resolves_a_project_against_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink out of the root is outside it, whatever its name says."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ok").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)
    settings = Settings()
    assert resolve_project("ok", settings) == (tmp_path / "ok").resolve()
    with pytest.raises(ProjectNotAllowedError):
        resolve_project("link", settings)
    with pytest.raises(ProjectNotAllowedError):
        validate_project_ref("../ok", settings)


# --- the queue and the worker -------------------------------------------------------------


def test_a_worker_without_review_leaves_a_model_review_queued(session: Session) -> None:
    enqueue_run(session, project="p", base_ref="main", head_ref="HEAD", use_llm=True)
    assert claim_next_run(session, worker_id="w1", timeout_s=60, can_review=False) is None
    assert claim_next_run(session, worker_id="w2", timeout_s=60, can_review=True) is not None


def test_a_worker_that_lost_its_claim_writes_nothing(session: Session) -> None:
    run = enqueue_run(session, project="p", base_ref="main", head_ref="HEAD")
    claim_next_run(session, worker_id="slow", timeout_s=60)
    claim_next_run(session, worker_id="fresh", timeout_s=-1)  # heartbeat went stale
    assert load_owned_run(session, run.id, "slow") is None
    assert load_owned_run(session, run.id, "fresh") is not None
    assert not heartbeat(session, run, "slow"), "must not keep the new owner's claim alive"


def test_the_worker_runs_the_model_review_it_was_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`llm_requested` was stored and never read, so intent never ran for a queued review."""
    from themis import worker
    from themis.pipeline import ReviewResult

    url = f"sqlite:///{tmp_path / 'q.db'}"
    Base.metadata.create_all(get_engine(url))
    monkeypatch.chdir(tmp_path)
    with session_scope(url) as s:
        enqueue_run(
            s,
            project="demo_project",
            base_ref="main",
            head_ref="HEAD",
            use_llm=True,
            pr_description="rename a CTE",
        )

    seen: dict[str, object] = {}

    def fake_review(project: Path, **kwargs: object) -> ReviewResult:
        seen.update(kwargs)
        return ReviewResult()

    monkeypatch.setattr(worker, "run_review", fake_review)
    key = worker.process_one(Settings(), url=url, capabilities=frozenset(Capability))
    assert key is not None
    assert seen["run_llm"] is True
    assert seen["pr_description"] == "rename a CTE"
    with session_scope(url) as s:
        assert s.query(ReviewRun).one().status == RunStatus.SUCCEEDED


def test_a_worker_that_cannot_compile_does_not_start() -> None:
    from themis import worker

    with pytest.raises(CapabilityError):
        worker.require_reviewing_capabilities(frozenset({Capability.ANALYSE}))


def test_a_review_needing_compile_refuses_before_touching_the_project(tmp_path: Path) -> None:
    from themis.pipeline import review

    with pytest.raises(CapabilityError, match="compile"):
        review(
            tmp_path,
            base="main",
            head="HEAD",
            settings=Settings(),
            capabilities=frozenset({Capability.ANALYSE, Capability.EXECUTE}),
        )


# --- the gate threshold and the report ---------------------------------------------------


def test_a_severity_is_read_case_insensitively() -> None:
    assert Settings(fail_on_severity="HIGH").fail_on_severity == "high"


def test_an_unknown_severity_is_refused_rather_than_never_blocking() -> None:
    """`HIGH` used to parse as nothing, and nothing meant exit code 0 on every review."""
    with pytest.raises(ValidationError):
        Settings(fail_on_severity="crit")


def test_the_gate_blocks_at_the_threshold() -> None:
    from themis.cli import _gate_exit_code

    finding = Finding(
        rule_id="F1001",
        family="F1",
        title="t",
        severity=Severity.CRITICAL,
        confidence=Confidence.MEASURED,
        evidence=Evidence(model_name="m"),
        consequence="c",
    )
    assert _gate_exit_code([finding], Settings(fail_on_severity="HIGH").fail_on_severity) == 1


def test_a_refuted_finding_is_set_apart_and_still_shown() -> None:
    """SARIF marks it suppressed; the Markdown must not list it among live findings."""
    refuted = Finding(
        rule_id="F1001",
        family="F1",
        title="New join to dim may fan out",
        severity=Severity.HIGH,
        confidence=Confidence.POSSIBLE,
        evidence=Evidence(model_name="m"),
        consequence="c",
        llm_rationale="dim is one row per key in the SQL shown",
        suppressed_reason="refuted by the grain reviewer",
    )
    output = markdown.render([refuted])
    assert "No findings" in output
    assert "1 finding(s) a reviewer refuted" in output
    assert "one row per key" in output
