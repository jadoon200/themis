"""Replay THEMIS over changes that already merged — the first measurement on a new project.

The question a team asks first is not "is the tool clever" but "what would it have said
about the last month of our pull requests, and how much of it would have been noise".
That can be answered without anyone changing how they work: take the last N changes on
the main branch that touched the dbt project, review each against its first parent, and
count what came out.

Read-only by default: no build, no model, nothing written to the warehouse — the same
static review a pull request would get with `--no-execute --no-llm`. `--execute` measures
too, against the allowlisted target, and takes a build per revision.

The summary is counts only — commits, severities, rule ids, timings — so it can leave the
building. Commit subjects are shown only when asked for: at work a subject can name a
client, a deal or a regulator.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from themis.acquire import git
from themis.config import Settings
from themis.logging import get_logger

log = get_logger(__name__)

SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass(frozen=True)
class Change:
    """One change that merged: the commit, the one it was reviewed against, its subject."""

    commit: str
    parent: str
    subject: str


@dataclass(frozen=True)
class BacktestRow:
    change: Change
    models: int = 0
    severities: dict[str, int] = field(default_factory=dict)
    rules: tuple[str, ...] = ()
    skipped: int = 0
    incomplete: tuple[str, ...] = ()
    seconds: float = 0.0
    error: str | None = None

    @property
    def findings(self) -> int:
        return sum(self.severities.values())


def changes_to_replay(project_dir: Path, *, last: int, ref: str = "HEAD") -> list[Change]:
    """The last `last` changes on `ref`'s first-parent history that touched the project.

    First-parent, so each entry is one change as it landed — a merge commit and its
    first parent is exactly the diff a pull request review saw — and a branch's
    intermediate commits are not replayed as if each had been reviewed on its own.
    """
    repo = git.repo_root(project_dir)
    relative = project_dir.resolve().relative_to(repo.resolve())
    return [
        Change(commit=commit, parent=parent, subject=subject)
        for commit, parent, subject in git.first_parent_changes(
            repo, ref, str(relative), limit=last
        )
    ]


Reviewer = Callable[..., Any]


def backtest(
    project_dir: Path,
    changes: list[Change],
    *,
    settings: Settings,
    target: str = "dev",
    execute: bool = False,
    reviewer: Reviewer | None = None,
) -> list[BacktestRow]:
    """Review each change against its first parent, and keep counts only.

    A change that cannot be reviewed — it did not compile, the revision is gone — is a row
    with its error, never a row with no findings: "could not look" and "looked and found
    nothing" must not read the same.
    """
    if reviewer is None:
        from themis.pipeline import review

        reviewer = review
    run: Reviewer = reviewer

    rows: list[BacktestRow] = []
    for index, change in enumerate(changes, start=1):
        log.info("backtest.change", n=f"{index}/{len(changes)}", commit=change.commit[:10])
        started = time.monotonic()
        try:
            result = run(
                project_dir,
                base=change.parent,
                head=change.commit,
                settings=settings,
                target=target,
                run_execution=execute,
                run_llm=False,
            )
        except Exception as exc:  # one broken revision must not end the replay
            rows.append(
                BacktestRow(
                    change=change,
                    seconds=time.monotonic() - started,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            continue
        counts = Counter(str(f.severity.value) for f in result.findings)
        rows.append(
            BacktestRow(
                change=change,
                models=len(result.models_reviewed),
                severities={s: counts.get(s, 0) for s in SEVERITIES},
                rules=tuple(sorted({f.rule_id for f in result.findings})),
                skipped=len(result.skipped),
                incomplete=tuple(result.incomplete_reasons),
                seconds=time.monotonic() - started,
            )
        )
    return rows


def summarise(rows: list[BacktestRow]) -> dict[str, Any]:
    """What the replay says, in counts. Nothing here names a model, a column or a client."""
    reviewed = [r for r in rows if r.error is None]
    flagged = [r for r in reviewed if r.findings]
    rule_counts = Counter(rule for r in reviewed for rule in r.rules)
    severity_totals: Counter[str] = Counter()
    for r in reviewed:
        severity_totals.update(r.severities)
    return {
        "changes": len(rows),
        "reviewed": len(reviewed),
        "could_not_review": len(rows) - len(reviewed),
        "with_findings": len(flagged),
        "incomplete": sum(1 for r in reviewed if r.incomplete),
        "findings_median": statistics.median([r.findings for r in reviewed]) if reviewed else 0,
        "findings_max": max((r.findings for r in reviewed), default=0),
        "severities": {s: severity_totals.get(s, 0) for s in SEVERITIES},
        "rules": dict(rule_counts.most_common()),
        "seconds_median": round(statistics.median([r.seconds for r in reviewed]), 1)
        if reviewed
        else 0.0,
    }
