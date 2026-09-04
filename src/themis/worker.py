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
from pathlib import Path

from themis.capabilities import DEFAULT_CAPABILITIES, Capability, parse_capabilities
from themis.config import Settings, load_settings
from themis.db.base import session_scope
from themis.db.models import ReviewRun
from themis.db.store import claim_next_run, fail_run, heartbeat, save_result
from themis.logging import get_logger
from themis.pipeline import review as run_review

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


class _Heartbeat:
    """Keeps a claim alive while a long review runs.

    Stage 3 builds can take minutes. Without this a run would look abandoned and be
    reclaimed by another worker, producing two concurrent builds writing to the same
    schemas — which would corrupt the very measurement the review depends on.
    """

    def __init__(self, run_id: int, interval_s: float, url: str | None) -> None:
        self._run_id = run_id
        self._interval = interval_s
        self._url = url
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with session_scope(self._url) as session:
                    run = session.get(ReviewRun, self._run_id)
                    if run is not None:
                        heartbeat(session, run)
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
    worker_id = worker_identity(held)

    with session_scope(url) as session:
        run = claim_next_run(
            session,
            worker_id=worker_id,
            timeout_s=settings.worker_claim_timeout_s,
            can_execute=Capability.EXECUTE in held,
        )
        if run is None:
            return None
        run_id, run_key = run.id, run.run_key
        project = Path(run.project)
        base, head = run.base_ref, run.head_ref
        execute_requested = run.execute_requested

    log.info("worker.claimed", run_key=run_key, worker=worker_id)

    try:
        with _Heartbeat(run_id, settings.worker_poll_interval_s * 2, url):
            result = run_review(
                project,
                base=base,
                head=head,
                settings=settings,
                run_execution=execute_requested,
                capabilities=held,
            )
    except Exception as exc:
        # Record the failure rather than letting the run sit in RUNNING until it is
        # reclaimed. A review that could not complete must never read as a clean one.
        with session_scope(url) as session:
            run = session.get(ReviewRun, run_id)
            if run is not None:
                fail_run(session, run, f"{type(exc).__name__}: {exc}")
        log.warning("worker.run_failed", run_key=run_key, error=str(exc)[:300])
        return run_key

    with session_scope(url) as session:
        run = session.get(ReviewRun, run_id)
        if run is not None:
            save_result(session, run, result)
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
    # THEMIS_WORKER_CAPABILITIES=analyse,compile runs a fleet that cannot touch a
    # warehouse at all; "all" opts one worker in to Stage 3.
    serve(capabilities=parse_capabilities(os.environ.get("THEMIS_WORKER_CAPABILITIES")))
