"""What the pages show, computed from the database and nothing else.

Kept apart from the routes and templates so every number a manager reads can be tested
without a browser, and so no page can show a figure that some query here does not define.
A dashboard is the place a tool is most tempted to round an uncomfortable number into a
comfortable one; here each one is a named function with a docstring saying what it counts.

**Open**, throughout, means no human has settled the finding: no decision, or one
deferred. Accepted (a real problem, knowingly taken on), dismissed (not a problem) and
fixed all close it. That is the definition the verdict, the "awaiting a decision" count and
the overview all use, so they can never disagree with each other.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from themis.db.models import DispositionEvent, ReviewRun, RunSnapshot, RunStatus
from themis.db.models import Finding as FindingRow

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
_OPEN = (None, "deferred")
# A PR that restates a period already reported: X0004. Named once, used everywhere.
RESTATEMENT_RULE = "X0004"
# The trend chart never draws more than this many days, whatever the period.
TREND_DAYS = 30
PERIODS = (7, 30, 0)  # 0 is all time
DISPOSITIONS = ("fixed", "accepted", "dismissed", "deferred", "open")


def _rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


def aware(value: datetime) -> datetime:
    """SQLite hands back naive timestamps and Postgres aware ones; compare only the latter."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_open(finding: FindingRow) -> bool:
    return finding.disposition in _OPEN


@dataclass(frozen=True)
class Verdict:
    """One word for a pull request, and the reason for it in a sentence."""

    key: str  # "blocking" | "review" | "clear"
    label: str
    reason: str


def verdict_for(findings: list[FindingRow], *, threshold: str) -> Verdict:
    """Blocking if any open finding is at or above the gate's threshold.

    Uses the same threshold as the CLI's exit code, so the page and the merge check can
    never give one pull request two different answers.
    """
    limit = _rank(threshold)
    open_findings = [f for f in findings if is_open(f)]
    blocking = [f for f in open_findings if _rank(f.severity) <= limit]
    if blocking:
        worst = min(blocking, key=lambda f: _rank(f.severity))
        count = len(blocking)
        return Verdict(
            key="blocking",
            label="Blocking",
            reason=(
                f"{count} open finding{'s' if count != 1 else ''} at or above "
                f"{threshold}, the worst {worst.severity}"
            ),
        )
    if open_findings:
        return Verdict(
            key="review",
            label="Needs review",
            reason=f"{len(open_findings)} open finding(s) below the {threshold} threshold",
        )
    if findings:
        return Verdict(key="clear", label="Settled", reason="every finding has a decision")
    return Verdict(key="clear", label="Clear", reason="no findings")


@dataclass(frozen=True)
class PullRequestRow:
    run_key: str
    number: int | None
    title: str
    author: str | None
    base: str
    head: str
    reviewed_at: datetime
    verdict: Verdict
    severity_counts: dict[str, int]
    finding_count: int
    open_count: int
    restates: bool
    executed: bool
    models_reviewed: int

    @property
    def worst(self) -> str | None:
        return next((s for s in SEVERITY_ORDER if self.severity_counts.get(s)), None)

    @property
    def decided(self) -> int:
        return self.finding_count - self.open_count


def _pull_request_row(run: ReviewRun, *, threshold: str) -> PullRequestRow:
    findings = list(run.findings)
    counts = Counter(f.severity for f in findings)
    return PullRequestRow(
        run_key=run.run_key,
        number=run.pr_number,
        title=run.pr_title or f"{run.base_ref} ← {run.head_ref}",
        author=run.pr_author,
        base=run.base_ref,
        head=run.head_ref,
        reviewed_at=aware(run.finished_at or run.created_at),
        verdict=verdict_for(findings, threshold=threshold),
        severity_counts={s: counts.get(s, 0) for s in SEVERITY_ORDER},
        finding_count=len(findings),
        open_count=sum(1 for f in findings if is_open(f)),
        restates=any(f.rule_id == RESTATEMENT_RULE for f in findings),
        executed=run.executed,
        models_reviewed=run.models_reviewed,
    )


def _finished_runs(session: Session) -> list[ReviewRun]:
    return list(
        session.scalars(
            select(ReviewRun)
            .where(ReviewRun.status == RunStatus.SUCCEEDED)
            .options(selectinload(ReviewRun.findings))
            .order_by(ReviewRun.created_at.desc())
        )
    )


@dataclass(frozen=True)
class DecisionRow:
    at: datetime
    actor: str
    disposition: str
    note: str | None
    rule_id: str
    title: str
    severity: str
    model_name: str
    run_key: str
    pr_number: int | None
    pr_title: str | None = None
    finding_id: int | None = None


def recent_decisions(session: Session, *, limit: int = 50) -> list[DecisionRow]:
    """Every human decision, newest first — the audit trail, in the order it happened."""
    rows = session.execute(
        select(DispositionEvent, FindingRow, ReviewRun)
        .join(FindingRow, DispositionEvent.finding_id == FindingRow.id)
        .join(ReviewRun, FindingRow.run_id == ReviewRun.id)
        .order_by(DispositionEvent.at.desc(), DispositionEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        DecisionRow(
            at=aware(event.at),
            actor=event.actor,
            disposition=event.disposition,
            note=event.note,
            rule_id=finding.rule_id,
            title=finding.title,
            severity=finding.severity,
            model_name=finding.model_name,
            run_key=run.run_key,
            pr_number=run.pr_number,
            pr_title=run.pr_title,
            finding_id=finding.id,
        )
        for event, finding, run in rows
    ]


@dataclass(frozen=True)
class RuleRow:
    rule_id: str
    family: str
    count: int
    dismissed: int

    @property
    def dismissal_share(self) -> float:
        return self.dismissed / self.count if self.count else 0.0


@dataclass(frozen=True)
class DayBucket:
    day: date
    reviews: int
    severities: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.severities.values())


@dataclass(frozen=True)
class AttentionRow:
    """A blocking pull request, and how long it has been waiting for someone."""

    row: PullRequestRow
    waiting_hours: float


@dataclass(frozen=True)
class ActivityItem:
    at: datetime
    kind: str  # "review" | "decision"
    who: str | None
    headline: str
    detail: str
    run_key: str
    pr_number: int | None
    tone: str  # a severity for a review, a disposition for a decision


@dataclass
class Overview:
    """The manager's page: what was reviewed, what is holding things up, what recurs."""

    pull_requests: int = 0
    blocking: int = 0
    restating: int = 0
    awaiting_decision: int = 0
    decisions: int = 0
    severity_counts: dict[str, int] = field(default_factory=dict)
    top_rules: list[RuleRow] = field(default_factory=list)
    recent: list[PullRequestRow] = field(default_factory=list)
    recent_decisions: list[DecisionRow] = field(default_factory=list)
    threshold: str = "high"
    days: int = 0
    # The same count over the period before this one, so a number has a direction.
    # None over all time, where there is no "before".
    pull_requests_before: int | None = None
    findings_total: int = 0
    findings_before: int | None = None
    # Of the findings at or above the gate, how many a human has settled.
    gated_total: int = 0
    gated_decided: int = 0
    daily: list[DayBucket] = field(default_factory=list)
    # Every finding in the period by its latest decision; "open" for none.
    disposition_counts: dict[str, int] = field(default_factory=dict)
    needs_attention: list[AttentionRow] = field(default_factory=list)
    activity: list[ActivityItem] = field(default_factory=list)

    @property
    def decided_share(self) -> float:
        return self.gated_decided / self.gated_total if self.gated_total else 1.0


def _window(runs: list[ReviewRun], start: datetime | None, end: datetime | None) -> list[ReviewRun]:
    out = []
    for run in runs:
        at = aware(run.created_at)
        if start is not None and at < start:
            continue
        if end is not None and at >= end:
            continue
        out.append(run)
    return out


def _daily(runs: list[ReviewRun], *, now: datetime, days: int) -> list[DayBucket]:
    """Reviews and their findings per day, by severity, oldest day first."""
    span = min(days or TREND_DAYS, TREND_DAYS)
    first = (now - timedelta(days=span - 1)).date()
    buckets = {
        first + timedelta(days=i): {"reviews": 0, **{s: 0 for s in SEVERITY_ORDER}}
        for i in range(span)
    }
    for run in runs:
        day = aware(run.created_at).date()
        if day not in buckets:
            continue
        buckets[day]["reviews"] += 1
        for finding in run.findings:
            if finding.severity in buckets[day]:
                buckets[day][finding.severity] += 1
    return [
        DayBucket(
            day=day,
            reviews=counts["reviews"],
            severities={s: counts[s] for s in SEVERITY_ORDER},
        )
        for day, counts in sorted(buckets.items())
    ]


def _activity(
    runs: list[ReviewRun], decisions: list[DecisionRow], *, threshold: str
) -> list[ActivityItem]:
    items: list[ActivityItem] = []
    for run in runs[:12]:
        row = _pull_request_row(run, threshold=threshold)
        items.append(
            ActivityItem(
                at=row.reviewed_at,
                kind="review",
                who=row.author,
                headline=row.title,
                detail=(
                    f"{row.finding_count} finding{'s' if row.finding_count != 1 else ''} · "
                    f"{row.verdict.label.lower()}"
                ),
                run_key=row.run_key,
                pr_number=row.number,
                tone=row.worst or "clear",
            )
        )
    for decision in decisions:
        items.append(
            ActivityItem(
                at=decision.at,
                kind="decision",
                who=decision.actor,
                headline=f"{decision.disposition} {decision.rule_id}",
                detail=decision.note or decision.title.replace("`", ""),
                run_key=decision.run_key,
                pr_number=decision.pr_number,
                tone=decision.disposition,
            )
        )
    return sorted(items, key=lambda item: item.at, reverse=True)[:12]


def overview(
    session: Session, *, threshold: str, days: int = 0, now: datetime | None = None
) -> Overview:
    """Every figure on the overview page, each defined by the query that produces it.

    - pull requests: finished reviews in the period.
    - blocking: reviews in the period whose verdict is blocking *now* — decisions count.
    - restating: reviews carrying an X0004, a measured change to a period already reported.
    - awaiting a decision: open findings at or above the gate's threshold.
    - settled: of the findings at or above the gate, the share a human has decided.
    """
    now = now or datetime.now(UTC)
    all_runs = _finished_runs(session)
    since = now - timedelta(days=days) if days else None
    runs = _window(all_runs, since, None)
    before = _window(all_runs, now - timedelta(days=2 * days), since) if days and since else None

    rows = [_pull_request_row(run, threshold=threshold) for run in runs]
    findings = [f for run in runs for f in run.findings]
    limit = _rank(threshold)
    gated = [f for f in findings if _rank(f.severity) <= limit]

    by_rule: dict[str, list[FindingRow]] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule_id, []).append(finding)
    top_rules = sorted(
        (
            RuleRow(
                rule_id=rule,
                family=group[0].family,
                count=len(group),
                dismissed=sum(1 for f in group if f.disposition == "dismissed"),
            )
            for rule, group in by_rule.items()
        ),
        key=lambda r: (-r.count, r.rule_id),
    )[:8]

    blocking_rows = [r for r in rows if r.verdict.key == "blocking"]
    attention = sorted(
        (
            AttentionRow(row=r, waiting_hours=(now - r.reviewed_at).total_seconds() / 3600)
            for r in blocking_rows
        ),
        key=lambda a: -a.waiting_hours,
    )

    counts = Counter(f.severity for f in findings)
    settled = Counter(f.disposition or "open" for f in findings)
    decision_total = session.scalar(select(func.count(DispositionEvent.id))) or 0
    decisions = recent_decisions(session, limit=12)
    return Overview(
        pull_requests=len(rows),
        blocking=len(blocking_rows),
        restating=sum(1 for r in rows if r.restates),
        awaiting_decision=sum(1 for f in gated if is_open(f)),
        decisions=int(decision_total),
        severity_counts={s: counts.get(s, 0) for s in SEVERITY_ORDER},
        top_rules=top_rules,
        recent=rows[:15],
        recent_decisions=decisions[:8],
        threshold=threshold,
        days=days,
        pull_requests_before=len(before) if before is not None else None,
        findings_total=len(findings),
        findings_before=(sum(len(run.findings) for run in before) if before is not None else None),
        gated_total=len(gated),
        gated_decided=sum(1 for f in gated if not is_open(f)),
        daily=_daily(all_runs, now=now, days=days),
        disposition_counts={d: settled.get(d, 0) for d in DISPOSITIONS},
        needs_attention=attention[:6],
        activity=_activity(runs, decisions, threshold=threshold),
    )


@dataclass(frozen=True)
class Sidebar:
    """What every page's navigation shows: the latest reviews and what is blocked."""

    recent: list[PullRequestRow]
    blocking: int


def sidebar(session: Session, *, threshold: str, limit: int = 6) -> Sidebar:
    """Counted in SQL, not by loading every review: this runs on every page."""
    runs = session.scalars(
        select(ReviewRun)
        .where(ReviewRun.status == RunStatus.SUCCEEDED)
        .options(selectinload(ReviewRun.findings))
        .order_by(ReviewRun.created_at.desc())
        .limit(limit)
    )
    gated = SEVERITY_ORDER[: _rank(threshold) + 1]
    blocking = session.scalar(
        select(func.count(func.distinct(FindingRow.run_id)))
        .join(ReviewRun, FindingRow.run_id == ReviewRun.id)
        .where(
            ReviewRun.status == RunStatus.SUCCEEDED,
            FindingRow.severity.in_(gated),
            or_(FindingRow.disposition.is_(None), FindingRow.disposition == "deferred"),
        )
    )
    return Sidebar(
        recent=[_pull_request_row(run, threshold=threshold) for run in runs],
        blocking=int(blocking or 0),
    )


def pull_requests(session: Session, *, threshold: str) -> list[PullRequestRow]:
    return [_pull_request_row(run, threshold=threshold) for run in _finished_runs(session)]


@dataclass(frozen=True)
class FindingView:
    row: FindingRow
    events: list[DispositionEvent]

    @property
    def measured(self) -> bool:
        return self.row.confidence == "measured"

    @property
    def open(self) -> bool:
        return is_open(self.row)


@dataclass
class PullRequestPage:
    run: ReviewRun
    summary: PullRequestRow
    groups: list[tuple[str, list[FindingView]]]
    deltas: list[dict[str, object]]
    chat_available: bool
    suggestions: list[str]
    events: list[DecisionRow] = field(default_factory=list)
    # The finding a reviewer should read first: the worst one still open.
    focus_id: int | None = None

    @property
    def finding_count(self) -> int:
        return sum(len(members) for _, members in self.groups)


def _suggestions(run: ReviewRun, findings: list[FindingRow]) -> list[str]:
    """Questions worth asking about this change — built from it, never generic.

    Each is phrased the way the agent's evaluation shows it answers well: one named model
    or rule, one thing to find out about it.
    """
    out: list[str] = []
    models = list(run.reviewed_models or [])
    if findings:
        worst = min(findings, key=lambda f: _rank(f.severity))
        out.append(f"What does rule {worst.rule_id} check for?")
        out.append(f"Which rule fired on {worst.model_name} in this review?")
    if models:
        out.append(f"What changed in the SQL of {models[0]}?")
        out.append(f"Which models downstream of {models[0]} are tagged regulatory?")
    if run.executed and models:
        out.append(f"What did building both revisions measure for {models[0]}?")
    unique: list[str] = []
    for question in out:
        if question not in unique:
            unique.append(question)
    return unique[:5]


def pull_request_page(session: Session, run_key: str, *, threshold: str) -> PullRequestPage | None:
    run = session.scalar(
        select(ReviewRun)
        .where(ReviewRun.run_key == run_key)
        .options(
            selectinload(ReviewRun.findings).selectinload(FindingRow.disposition_events),
            selectinload(ReviewRun.deltas),
        )
    )
    if run is None:
        return None
    findings = sorted(run.findings, key=lambda f: (_rank(f.severity), f.rule_id, f.model_name))
    groups: list[tuple[str, list[FindingView]]] = []
    for severity in SEVERITY_ORDER:
        members = [
            FindingView(row=f, events=list(f.disposition_events))
            for f in findings
            if f.severity == severity
        ]
        if members:
            groups.append((severity, members))

    deltas: list[dict[str, object]] = []
    for delta in sorted(run.deltas, key=lambda d: (not d.material, d.model_name)):
        moved = {
            column: pair
            for column, pair in (delta.sum_deltas or {}).items()
            if isinstance(pair, list) and len(pair) == 2 and pair[0] != pair[1]
        }
        deltas.append(
            {
                "model": delta.model_name,
                "rows_before": delta.rows_before,
                "rows_after": delta.rows_after,
                "moved": moved,
                "keyed": delta.keyed_diff,
                "material": delta.material,
                "build_error": delta.build_error,
            }
        )

    events = [
        DecisionRow(
            at=aware(event.at),
            actor=event.actor,
            disposition=event.disposition,
            note=event.note,
            rule_id=finding.rule_id,
            title=finding.title,
            severity=finding.severity,
            model_name=finding.model_name,
            run_key=run.run_key,
            pr_number=run.pr_number,
            pr_title=run.pr_title,
            finding_id=finding.id,
        )
        for finding in findings
        for event in finding.disposition_events
    ]
    events.sort(key=lambda e: e.at, reverse=True)

    focus = next((f.id for f in findings if is_open(f)), findings[0].id if findings else None)
    has_snapshot = (
        session.scalar(select(func.count(RunSnapshot.id)).where(RunSnapshot.run_id == run.id)) or 0
    ) > 0
    return PullRequestPage(
        run=run,
        summary=_pull_request_row(run, threshold=threshold),
        groups=groups,
        deltas=deltas,
        chat_available=has_snapshot,
        suggestions=_suggestions(run, findings),
        events=events,
        focus_id=focus,
    )
