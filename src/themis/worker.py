"""The review worker.

Claims queued runs and executes the same pipeline the CLI does. That sameness is
deliberate: the service is a caller of the pipeline, never a second implementation of
it, so the tests that drive the CLI cover the worker's behaviour too.
"""

from __future__ import annotations

import os
import socket
import threading
import time

from themis.capabilities import DEFAULT_CAPABILITIES, Capability, parse_capabilities, require
from themis.config import Settings, load_settings
from themis.db.base import session_scope
from themis.db.models import ReviewRun
from themis.db.store import claim_next_run, fail_run, heartbeat, load_owned_run, save_result
from themis.logging import get_logger
from themis.pipeline import review as run_review
from themis.projects import resolve_project

log = get_logger(__name__)


def worker_identity(capabilities: frozenset[Capability] | None = None) -> str:
    """Host, pid, and what this worker may do.

    The capabilities are part of the identity on purpose: a run's ``worker_id`` is what
    an auditor reads to find out what the machine that produced a review was able to
    do, and "it could not reach the warehouse" is exactly the kind of thing that should
    be legible from the record rather than reconstructed from deployment config.
    """
    base = f"{socket.gethostname()}:{os.getpid()}"
    if capabilities is None:
        return base
    return f"{base}[{','.join(sorted(c.value for c in capabilities))}]"


def require_reviewing_capabilities(held: frozenset[Capability]) -> None:
    """Refuse to start a worker that could not complete any review at all.

    Every review compiles and runs the rules, so a worker without COMPILE or ANALYSE has
    no work it can do. It used to claim runs anyway and compile regardless, because only
    EXECUTE was ever checked.
    """
    require(held, Capability.COMPILE, what="a worker (every review compiles)")
    require(held, Capability.ANALYSE, what="a worker (every review runs the rules)")


class _Heartbeat:
    """Keeps a claim alive while a long review runs.

    Stage 3 builds can take minutes. Without this a run would look abandoned and be
    reclaimed by another worker. The heartbeat stops refreshing the moment the claim is
    lost, so it never keeps a run alive on behalf of a worker that no longer owns it.
    """

    def __init__(self, run_id: int, interval_s: float, url: str | None, worker_id: str) -> None:
        self._run_id = run_id
        self._interval = interval_s
        self._url = url
        self._worker_id = worker_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with session_scope(self._url) as session:
                    run = session.get(ReviewRun, self._run_id)
                    if run is not None and not heartbeat(session, run, self._worker_id):
                        log.warning("worker.claim_lost", run_id=self._run_id)
                        return
            except Exception as exc:  # a failed heartbeat must not kill the review
                log.warning("worker.heartbeat_failed", error=str(exc)[:200])

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def process_one(
    settings: Settings,
    *,
    url: str | None = None,
    capabilities: frozenset[Capability] | None = None,
) -> str | None:
    """Claim and run a single review. Returns its run key, or None if none queued."""
    held = capabilities if capabilities is not None else DEFAULT_CAPABILITIES
    if Capability.COMPILE not in held or Capability.ANALYSE not in held:
        return None
    worker_id = worker_identity(held)

    with session_scope(url) as session:
        run = claim_next_run(
            session,
            worker_id=worker_id,
            timeout_s=settings.worker_claim_timeout_s,
            can_execute=Capability.EXECUTE in held,
            can_review=Capability.REVIEW in held,
        )
        if run is None:
            return None
        run_id, run_key = run.id, run.run_key
        project_ref = run.project
        base, head = run.base_ref, run.head_ref
        execute_requested = run.execute_requested
        llm_requested = run.llm_requested
        pr_description = run.pr_description

    log.info("worker.claimed", run_key=run_key, worker=worker_id)

    try:
        # Checked again here, not only when the API accepted the request: a row can reach
        # the queue by other routes, and this is the process that runs dbt on the path.
        project = resolve_project(project_ref, settings)
        with _Heartbeat(run_id, settings.worker_poll_interval_s * 2, url, worker_id):
            result = run_review(
                project,
                base=base,
                head=head,
                settings=settings,
                run_execution=execute_requested,
                run_llm=llm_requested,
                pr_description=pr_description,
                capabilities=held,
            )
    except Exception as exc:
        # Record the failure rather than letting the run sit in RUNNING until it is
        # reclaimed. A review that could not complete must never read as a clean one.
        with session_scope(url) as session:
            owned = load_owned_run(session, run_id, worker_id)
            if owned is not None:
                fail_run(session, owned, f"{type(exc).__name__}: {exc}")
        log.warning("worker.run_failed", run_key=run_key, error=str(exc)[:300])
        return run_key

    with session_scope(url) as session:
        owned = load_owned_run(session, run_id, worker_id)
        if owned is not None:
            save_result(session, owned, result)
    log.info("worker.finished", run_key=run_key, findings=len(result.findings))
    return run_key


def serve(
    settings: Settings | None = None,
    *,
    url: str | None = None,
    once: bool = False,
    capabilities: frozenset[Capability] | None = None,
) -> None:
    """Poll for work until interrupted."""
    settings = settings or load_settings()
    held = capabilities if capabilities is not None else DEFAULT_CAPABILITIES
    require_reviewing_capabilities(held)
    log.info(
        "worker.start",
        worker=worker_identity(held),
        capabilities=",".join(sorted(c.value for c in held)),
        poll_s=settings.worker_poll_interval_s,
    )
    while True:
        try:
            claimed = process_one(settings, url=url, capabilities=held)
        except Exception as exc:
            # The loop must survive a transient database outage; a worker that exits
            # on the first blip is a worker that is always down.
            log.warning("worker.loop_error", error=str(exc)[:300])
            claimed = None
        if once:
            return
        if claimed is None:
            time.sleep(settings.worker_poll_interval_s)


if __name__ == "__main__":
    from themis.logging import configure_logging

    configure_logging()
    # THEMIS_WORKER_CAPABILITIES=analyse,compile runs a fleet that cannot build anything
    # or call a model; "all" opts one worker in to Stage 3 and the model review too.
    serve(capabilities=parse_capabilities(os.environ.get("THEMIS_WORKER_CAPABILITIES")))
