"""Fill a demo database with real reviews of real changes, for the web pages to show.

Nothing here is invented except the pull request around each change. Every finding, every
measured delta and every grain on the pages comes from THEMIS reviewing an actual commit
to the demo project — the same mutations the corpus is scored on — with both revisions
built and compared. The PR numbers, titles, authors and dates are the only fiction, and
they are labelled as demo data wherever they appear: `bitbucket.example.invalid` is not a
host anyone can reach.

    python scripts/seed_demo.py                       # into data/demo.db
    python scripts/seed_demo.py --database sqlite:///data/other.db
    THEMIS_DATABASE_URL=sqlite:///data/demo.db make api   # then open /ui

Takes a couple of minutes: each change is built twice. Needs the demo project built
(`make demo-build`) and a clean working tree — the reviews are of committed revisions.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "demo_project"
sys.path.insert(0, str(REPO / "src"))


@dataclass(frozen=True)
class DemoPR:
    mutation: str
    number: int
    title: str
    author: str
    branch: str
    days_ago: float
    # (disposition, actor, note) — decisions a reviewer took afterwards, oldest first.
    decisions: tuple[tuple[str, str, str], ...] = ()
    # Which findings the decisions apply to: rule ids, or () for all of them.
    decide_rules: tuple[str, ...] = ()


# Authors are first names only, and plainly placeholders — no one's real colleague.
SCENARIOS: tuple[DemoPR, ...] = (
    DemoPR(
        mutation="fanout_drop_join_predicate",
        number=1418,
        title="Simplify the FX rate lookup in GL conversion",
        author="priya",
        branch="feature/simplify-fx-lookup",
        days_ago=0.2,
    ),
    DemoPR(
        mutation="currency_dropped_from_regulatory_grain",
        number=1417,
        title="Report the regulatory summary per entity and period",
        author="alex",
        branch="feature/reg-summary-entity-period",
        days_ago=0.9,
        decisions=(("deferred", "morgan", "Waiting on finance to confirm the reporting grain"),),
        decide_rules=("F3004",),
    ),
    DemoPR(
        mutation="unruled_january_fx_rate_restated",
        number=1415,
        title="Correct the January USD rate to the published ECB figure",
        author="sam",
        branch="fix/jan-usd-rate",
        days_ago=1.6,
        decisions=(
            (
                "accepted",
                "morgan",
                "Restatement approved by the finance controller; disclosure note raised",
            ),
        ),
        decide_rules=("X0004",),
    ),
    DemoPR(
        mutation="latent_comment_addressed_to_the_reviewer",
        number=1413,
        title="Document the FX conversion step",
        author="jordan",
        branch="docs/fx-conversion",
        days_ago=2.5,
    ),
    DemoPR(
        mutation="money_cast_to_double",
        number=1411,
        title="Use DOUBLE for revenue aggregation performance",
        author="priya",
        branch="perf/revenue-double",
        days_ago=3.4,
        decisions=(("fixed", "priya", "Reverted to DECIMAL(38,6) in the follow-up commit"),),
    ),
    DemoPR(
        mutation="minor_units_divided_as_integers",
        number=1409,
        title="Simplify the minor-to-major money macro",
        author="sam",
        branch="refactor/money-macro",
        days_ago=4.8,
    ),
    DemoPR(
        mutation="approx_aggregate_in_regulatory",
        number=1406,
        title="Speed up distinct counts in regulatory reporting",
        author="jordan",
        branch="perf/approx-distinct",
        days_ago=6.1,
        decisions=(
            ("dismissed", "alex", "Count is for an internal dashboard tile, not the filing"),
            ("accepted", "morgan", "Reopened: the tile feeds the regulatory pack; must be exact"),
        ),
    ),
    DemoPR(
        mutation="control_add_comments",
        number=1402,
        title="Add explanatory comments to the FX conversion",
        author="alex",
        branch="docs/fx-comments",
        days_ago=8.3,
    ),
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(tree: Path, message: str) -> None:
    _git(
        tree,
        "-c",
        "user.email=demo@themis.invalid",
        "-c",
        "user.name=themis-demo",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        message,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=f"sqlite:///{REPO / 'data' / 'demo.db'}")
    parser.add_argument("--only", nargs="*", help="mutation ids to seed; default all")
    args = parser.parse_args()

    os.environ["THEMIS_DATABASE_URL"] = args.database
    if args.database.startswith("sqlite:///"):
        Path(args.database.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        check=True,
        capture_output=True,
    )

    from sqlalchemy import select

    from themis.config import load_settings
    from themis.db.base import session_scope
    from themis.db.models import ReviewRun, RunSource, RunStatus
    from themis.db.store import new_run_key, record_disposition, save_result
    from themis.eval.mutations import select as select_mutation
    from themis.pipeline import review

    if _git(REPO, "status", "--porcelain"):
        print("the working tree has uncommitted changes; reviews are of committed revisions")
        return 2

    settings = load_settings()
    base_sha = _git(REPO, "rev-parse", "HEAD")
    wanted = set(args.only or [])
    now = datetime.now(UTC)
    relative = PROJECT.relative_to(REPO)

    for pr in SCENARIOS:
        if wanted and pr.mutation not in wanted:
            continue
        mutation = select_mutation(pr.mutation)[0]
        print(f"#{pr.number} {pr.title} ({pr.mutation})", flush=True)
        with tempfile.TemporaryDirectory(prefix="themis-demo-") as tmp:
            tree = Path(tmp) / "tree"
            _git(REPO, "worktree", "add", "--detach", "--quiet", str(tree), base_sha)
            try:
                mutated = tree / relative
                if not mutation.apply(mutated):
                    print("  mutation anchor not found — skipped")
                    continue
                _git(tree, "add", "--", str((mutated / mutation.relative_path).resolve()))
                _commit(tree, f"demo: {pr.title}")
                result = review(
                    mutated,
                    base=base_sha,
                    head="HEAD",
                    settings=settings,
                    run_execution=True,
                    run_llm=False,
                    pr_description=mutation.pr_description or pr.title,
                    data_anchor=PROJECT,
                )
            finally:
                _git(REPO, "worktree", "remove", "--force", str(tree))

        reviewed_at = now - timedelta(days=pr.days_ago)
        with session_scope() as session:
            existing = session.scalar(select(ReviewRun).where(ReviewRun.pr_number == pr.number))
            if existing is not None:
                session.delete(existing)
                session.flush()
            run = ReviewRun(
                run_key=new_run_key(),
                project="demo_project",
                repo="finance-dbt (demo)",
                base_ref="main",
                head_ref=pr.branch,
                base_sha=base_sha,
                status=RunStatus.RUNNING,
                source=RunSource.WEBHOOK,
                pr_number=pr.number,
                pr_url=(
                    "https://bitbucket.example.invalid/projects/FIN/repos/finance-dbt/"
                    f"pull-requests/{pr.number}"
                ),
                pr_title=pr.title,
                pr_author=pr.author,
                pr_description=mutation.pr_description or pr.title,
                execute_requested=True,
                created_at=reviewed_at - timedelta(minutes=4),
                started_at=reviewed_at - timedelta(minutes=4),
            )
            session.add(run)
            session.flush()
            save_result(session, run, result)
            run.finished_at = reviewed_at

            targets = [
                f for f in run.findings if not pr.decide_rules or f.rule_id in pr.decide_rules
            ]
            for step, (disposition, actor, note) in enumerate(pr.decisions):
                for finding in targets:
                    event = record_disposition(
                        session, finding, disposition=disposition, note=note, actor=actor
                    )
                    # Spread over the hours after the review, in the order they were taken.
                    event.at = reviewed_at + timedelta(hours=2 + 5 * step)
                    finding.disposition_at = event.at
            session.flush()
            print(
                f"  {len(result.findings)} finding(s), executed={result.executed}, "
                f"{len(pr.decisions)} decision(s)",
                flush=True,
            )

    print(f"\nseeded {args.database}")
    print(f"serve it:  THEMIS_DATABASE_URL={args.database} make api   then open /ui")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
