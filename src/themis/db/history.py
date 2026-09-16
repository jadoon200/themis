"""Binding stored judgements to a running review.

The pipeline takes a callable, not a session, so that a review needs no database to
run and gains history when one is there. This is the one place that knows both.
"""

from __future__ import annotations

from themis.db.base import session_scope
from themis.db.store import history_for
from themis.models import Finding, FindingHistory
from themis.pipeline import HistoryLookup


def history_lookup(project: str, *, examples: int = 3, url: str | None = None) -> HistoryLookup:
    """A lookup bound to one project's stored findings."""

    def lookup(findings: list[Finding]) -> list[FindingHistory | None]:
        with session_scope(url) as session:
            return history_for(session, findings, project=project, examples=examples)

    return lookup
