"""The queue, against a real Postgres.

`SELECT ... FOR UPDATE SKIP LOCKED` is the entire basis for handing a run to exactly
one worker, and SQLite has no row locking — the clause is skipped there, so every other
test in this suite exercises a code path that is *not* the one production runs.

These tests are skipped when no Postgres is reachable, so local runs stay fast, and CI
provides one so the mechanism is actually guarded.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from themis.db.base import Base
from themis.db.models import ReviewRun, RunStatus
from themis.db.store import claim_next_run, enqueue_run

POSTGRES_URL = os.environ.get(
    "THEMIS_TEST_POSTGRES_URL", "postgresql+psycopg://themis:themis@127.0.0.1:5436/themis"
)


def _postgres_available() -> bool:
    try:
        engine = create_engine(POSTGRES_URL, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(), reason="no Postgres reachable; set THEMIS_TEST_POSTGRES_URL"
)


@pytest.fixture
def engine():
    engine = create_engine(POSTGRES_URL, future=True)
    # A dedicated schema keeps these tests from touching anything a developer is using.
    with engine.begin() as conn:
        conn.execute(text("drop schema if exists themis_test cascade"))
        conn.execute(text("create schema themis_test"))
    for table in Base.metadata.tables.values():
        table.schema = "themis_test"
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("drop schema if exists themis_test cascade"))
        for table in Base.metadata.tables.values():
            table.schema = None


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def test_a_run_is_claimed_by_exactly_one_worker(engine) -> None:
    """The property SQLite cannot test.

    Without SKIP LOCKED two workers would either block on each other or both claim the
    same run — and two workers building into the same schemas concurrently would
    corrupt the measurement the review depends on.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as setup:
        for i in range(6):
            enqueue_run(setup, project="p", base_ref="main", head_ref=f"h{i}")
        setup.commit()

    claims: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        for _ in range(8):
            with factory() as s:
                run = claim_next_run(s, worker_id=name, timeout_s=600)
                if run is None:
                    s.commit()
                    return
                key = run.run_key
                s.commit()
            with lock:
                claims.append((name, key))

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    keys = [key for _, key in claims]
    assert len(keys) == 6, "every queued run should be claimed"
    assert len(set(keys)) == 6, "no run may be handed to two workers"


def test_claiming_is_oldest_first(session: Session) -> None:
    first = enqueue_run(session, project="p", base_ref="main", head_ref="a").run_key
    enqueue_run(session, project="p", base_ref="main", head_ref="b")
    session.commit()
    claimed = claim_next_run(session, worker_id="w", timeout_s=600)
    assert claimed is not None and claimed.run_key == first


def test_a_stale_claim_is_reclaimed(session: Session) -> None:
    """A worker that dies mid-review must not strand the run forever."""
    enqueue_run(session, project="p", base_ref="main", head_ref="h")
    session.commit()
    claim_next_run(session, worker_id="dead", timeout_s=600)
    session.commit()
    reclaimed = claim_next_run(session, worker_id="alive", timeout_s=-1)
    assert reclaimed is not None
    assert reclaimed.worker_id == "alive"


def test_jsonb_round_trips_measured_deltas(session: Session) -> None:
    """The delta payload is JSONB on Postgres and plain JSON elsewhere; the money has
    to survive the difference."""
    from themis.db.models import ModelDelta

    run = enqueue_run(session, project="p", base_ref="main", head_ref="h")
    session.add(
        ModelDelta(
            run_id=run.id,
            model_name="fct_revenue",
            rows_before=15,
            rows_after=45,
            sum_deltas={"amount_usd": [13112347.70, 39318036.05]},
            material=True,
        )
    )
    session.commit()
    session.expire_all()

    stored = session.query(ModelDelta).filter_by(model_name="fct_revenue").one()
    assert stored.sum_deltas["amount_usd"] == [13112347.70, 39318036.05]


def test_running_runs_are_not_reclaimed_while_heartbeating(session: Session) -> None:
    enqueue_run(session, project="p", base_ref="main", head_ref="h")
    session.commit()
    claim_next_run(session, worker_id="busy", timeout_s=600)
    session.commit()
    assert claim_next_run(session, worker_id="other", timeout_s=600) is None
    assert session.query(ReviewRun).one().status == RunStatus.RUNNING
