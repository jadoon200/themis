"""API and persistence.

The behaviours worth protecting are the ones that make history trustworthy: a
fingerprint that stays stable across runs, a queue that hands each run to exactly one
worker, and a failed review that never reads as a clean one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from themis.api import app as api_module
from themis.db.base import Base
from themis.db.models import Finding, ReviewRun, RunStatus, fingerprint_finding
from themis.db.store import (
    claim_next_run,
    dismissal_rate,
    enqueue_run,
    fail_run,
    prior_occurrences,
)


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        yield session

    api_module.app.dependency_overrides[api_module.get_session] = _override
    with TestClient(api_module.app) as c:
        yield c
    api_module.app.dependency_overrides.clear()


# --- fingerprints -------------------------------------------------------------


def test_fingerprint_is_stable_for_the_same_issue() -> None:
    args = dict(rule_id="F1001", model_name="fct_revenue", project="p", evidence_note="note")
    assert fingerprint_finding(**args) == fingerprint_finding(**args)


def test_fingerprint_ignores_whitespace_in_evidence() -> None:
    """Reformatted evidence is the same issue; forking the identity would make every
    recurrence look new."""
    a = fingerprint_finding(rule_id="F1", model_name="m", project="p", evidence_note="a  b\n c")
    b = fingerprint_finding(rule_id="F1", model_name="m", project="p", evidence_note="a b c")
    assert a == b


def test_fingerprint_separates_different_models() -> None:
    a = fingerprint_finding(rule_id="F1", model_name="m1", project="p", evidence_note="x")
    b = fingerprint_finding(rule_id="F1", model_name="m2", project="p", evidence_note="x")
    assert a != b


def test_fingerprint_separates_projects() -> None:
    """Two projects can hold identically-named models; their histories must not merge."""
    a = fingerprint_finding(rule_id="F1", model_name="m", project="p1", evidence_note="x")
    b = fingerprint_finding(rule_id="F1", model_name="m", project="p2", evidence_note="x")
    assert a != b


# --- the queue ----------------------------------------------------------------


def test_claim_returns_none_when_nothing_queued(session: Session) -> None:
    assert claim_next_run(session, worker_id="w1", timeout_s=60) is None


def test_a_run_is_claimed_once(session: Session) -> None:
    enqueue_run(session, project="p", base_ref="main", head_ref="HEAD")
    first = claim_next_run(session, worker_id="w1", timeout_s=60)
    second = claim_next_run(session, worker_id="w2", timeout_s=60)
    assert first is not None
    assert second is None, "a claimed run must not be handed to a second worker"


def test_claiming_marks_the_worker_and_status(session: Session) -> None:
    enqueue_run(session, project="p", base_ref="main", head_ref="HEAD")
    run = claim_next_run(session, worker_id="worker-a", timeout_s=60)
    assert run is not None
    assert run.status == RunStatus.RUNNING
    assert run.worker_id == "worker-a"
    assert run.started_at is not None


def test_a_stale_claim_is_reclaimed(session: Session) -> None:
    """A worker that dies mid-review must not strand the run forever."""
    enqueue_run(session, project="p", base_ref="main", head_ref="HEAD")
    claim_next_run(session, worker_id="dead", timeout_s=60)
    reclaimed = claim_next_run(session, worker_id="alive", timeout_s=-1)
    assert reclaimed is not None
    assert reclaimed.worker_id == "alive"


def test_runs_are_claimed_oldest_first(session: Session) -> None:
    first = enqueue_run(session, project="p", base_ref="main", head_ref="a").run_key
    enqueue_run(session, project="p", base_ref="main", head_ref="b")
    claimed = claim_next_run(session, worker_id="w", timeout_s=60)
    assert claimed is not None and claimed.run_key == first


def test_a_failed_run_is_not_succeeded(session: Session) -> None:
    """A review that could not complete must never read as a clean one."""
    run = enqueue_run(session, project="p", base_ref="main", head_ref="HEAD")
    fail_run(session, run, "dbt exploded")
    assert run.status == RunStatus.FAILED
    assert run.error == "dbt exploded"
    assert run.finished_at is not None


# --- history ------------------------------------------------------------------


def _add_finding(session: Session, run: ReviewRun, fingerprint: str, disposition=None) -> Finding:
    row = Finding(
        run_id=run.id,
        fingerprint=fingerprint,
        rule_id="F1001",
        family="F1",
        title="t",
        severity="high",
        confidence="likely",
        verdict="undecidable",
        model_name="m",
        disposition=disposition,
    )
    session.add(row)
    session.flush()
    return row


def test_prior_occurrences_counts_only_earlier_runs(session: Session) -> None:
    r1 = enqueue_run(session, project="p", base_ref="a", head_ref="b")
    r2 = enqueue_run(session, project="p", base_ref="a", head_ref="c")
    _add_finding(session, r1, "fp")
    _add_finding(session, r2, "fp")
    assert prior_occurrences(session, "fp", before_run_id=r2.id) == 1
    assert prior_occurrences(session, "fp", before_run_id=r1.id) == 0


def test_dismissal_rate_needs_dispositions(session: Session) -> None:
    run = enqueue_run(session, project="p", base_ref="a", head_ref="b")
    _add_finding(session, run, "fp")
    assert dismissal_rate(session, "fp") is None


def test_dismissal_rate_measures_what_humans_decided(session: Session) -> None:
    """The only false-positive signal that costs nobody any labelling effort."""
    run = enqueue_run(session, project="p", base_ref="a", head_ref="b")
    _add_finding(session, run, "fp", disposition="dismissed")
    _add_finding(session, run, "fp", disposition="dismissed")
    _add_finding(session, run, "fp", disposition="accepted")
    assert dismissal_rate(session, "fp") == pytest.approx(2 / 3)


# --- the API ------------------------------------------------------------------


def test_health_reports_database_reachability(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] in ("ok", "degraded")
    assert "database" in body


def test_creating_a_review_returns_202_and_a_key(client: TestClient) -> None:
    """202, not 200: the work has not happened yet, a worker will do it."""
    response = client.post("/reviews", json={"project": "demo_project", "base_ref": "main"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_key"]


def test_unknown_review_is_404(client: TestClient) -> None:
    assert client.get("/reviews/nope").status_code == 404


def test_listing_reviews_filters_by_project(client: TestClient) -> None:
    client.post("/reviews", json={"project": "alpha"})
    client.post("/reviews", json={"project": "beta"})
    rows = client.get("/reviews", params={"project": "alpha"}).json()
    assert [r["project"] for r in rows] == ["alpha"]


def test_disposition_is_recorded(client: TestClient, session: Session) -> None:
    run = enqueue_run(session, project="p", base_ref="a", head_ref="b")
    finding = _add_finding(session, run, "fp")
    response = client.post(
        f"/findings/{finding.id}/disposition",
        json={"disposition": "dismissed", "note": "intentional"},
    )
    assert response.status_code == 200
    assert response.json()["disposition"] == "dismissed"


def test_invalid_disposition_is_rejected(client: TestClient, session: Session) -> None:
    run = enqueue_run(session, project="p", base_ref="a", head_ref="b")
    finding = _add_finding(session, run, "fp")
    response = client.post(f"/findings/{finding.id}/disposition", json={"disposition": "whatever"})
    assert response.status_code == 422
