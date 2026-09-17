"""Helpers commands share: history lookup, persisting a run, and the merge gate's exit codes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from themis.cli._app import log
from themis.config import Settings
from themis.models import Finding, Severity

if TYPE_CHECKING:
    from themis.pipeline import HistoryLookup


def _history(project: str, settings: Settings) -> HistoryLookup | None:
    """Stored judgements for this project, or nothing if there is no store.

    Importing the store lazily keeps `themis review` usable on a machine with no
    database at all; a lookup that raises is caught by the pipeline and logged, because
    a review must not fail over history it could not read.
    """
    try:
        from themis.db.history import history_lookup
    except Exception:  # pragma: no cover - sqlalchemy missing is not a review failure
        return None
    return history_lookup(project, examples=settings.prior_judgement_examples)


def _persist(result: object, *, project: str, base: str, head: str, execute: bool) -> None:
    """Record a CLI run alongside the ones the service records.

    Without this the store only ever held API-driven runs, so `themis ask` could not
    answer about a review someone had just run and finding history had a hole in it
    exactly where the tool is used most.
    """
    from themis.db.base import session_scope
    from themis.db.models import RunSource, RunStatus
    from themis.db.store import enqueue_run, save_result
    from themis.pipeline import ReviewResult

    if not isinstance(result, ReviewResult):
        return
    try:
        with session_scope() as session:
            run = enqueue_run(
                session,
                project=project,
                base_ref=base,
                head_ref=head,
                source=RunSource.CLI,
                execute=execute,
            )
            run.status = RunStatus.RUNNING
            save_result(session, run, result)
            log.info("review.saved", run_key=run.run_key)
    except Exception as exc:
        # Never fail a review because history could not be written. The findings the
        # reviewer needs are already on screen.
        log.warning("review.not_saved", error=str(exc)[:200])


EXIT_INCOMPLETE = 3


def _review_exit_code(result: object, fail_on: str | None) -> int:
    """The merge gate's decision for a whole review, not just its findings.

    A blocking finding wins, because it blocks either way and says more. Otherwise an
    incomplete review blocks too: the gate used to pass a review that had skipped most of
    its rules, which is the one outcome a gate exists to prevent. Advisory mode (no
    threshold set) never blocks, as before.
    """
    from themis.pipeline import ReviewResult

    if not fail_on or not isinstance(result, ReviewResult):
        return 0
    blocking = _gate_exit_code(result.findings, fail_on)
    if blocking:
        return blocking
    return EXIT_INCOMPLETE if result.incomplete_reasons else 0


def _gate_exit_code(findings: list[Finding], fail_on: str | None) -> int:
    """Advisory by default: a review that blocks every merge stops being read.

    Blocking is opt-in per severity, and only findings at or above that severity
    fail the build.
    """
    if not fail_on:
        return 0
    order = [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.LOW,
        Severity.INFO,
    ]
    # Settings already refuse an unknown severity. Raising here too, rather than
    # returning 0, keeps a gate that is handed a bad threshold from failing open.
    threshold = order.index(Severity(fail_on.strip().lower()))
    return int(any(order.index(f.severity) <= threshold for f in findings))
